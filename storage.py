# -*- coding: utf-8 -*-
"""
storage.py — camada de armazenamento do buscador (imóveis coletados + pedidos de coleta).

Dois backends com a MESMA interface:
  * SQLiteBackend    — arquivo local. Usado nos testes e como alternativa em desenvolvimento.
                       No Render (plano gratuito) o disco é efêmero: NÃO serve para guardar de verdade.
  * SupabaseBackend  — Postgres do Supabase via HTTP (PostgREST), com a chave de serviço.
                       Ativado quando SUPABASE_URL e SUPABASE_SERVICE_KEY existem no ambiente.

Regras:
  * `texto_bruto` NUNCA é gravado (pode conter nomes de imobiliárias; ver handoff §4/LGPD).
  * Um imóvel é identificado pelo `link` (único). `anuncio_id` (número que aparece como
    `-id-NNNN` nos links do VivaReal e do ZAP, os dois compartilham o mesmo número —
    verificado nos dados reais, handoff §4) serve para achar o mesmo imóvel em portais
    diferentes. Para o Imovelweb o formato do id NÃO foi verificado: fica sem id.
  * Reenviar o mesmo link atualiza os dados e `ultima_vista`; `primeira_vista` não muda.

Verificação: o SQLite é coberto por testes automatizados; o SupabaseBackend é testado só contra
um servidor FALSO que imita a API (test_storage.py). Contra o Supabase de verdade: NÃO verificado
até alguém rodar `py verificar_supabase.py` com as chaves reais.
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

HOSTS_PORTAIS = {
    "vivareal.com.br": "VivaReal",
    "zapimoveis.com.br": "ZAP Imóveis",
    "imovelweb.com.br": "Imovelweb",
    "chavesnamao.com.br": "Chaves na Mão",
}

# Campos aceitos de cada anúncio (texto_bruto de propósito FORA).
CAMPOS_TEXTO = ("portal", "titulo", "bairro", "endereco", "cidade", "finalidade",
                "dentro_do_filtro", "possivel_duplicado", "busca", "tipo", "cidade_busca")
# Conferência do anúncio CONTRA UM PEDIDO (vai para a tabela resultados, não para imoveis)
CAMPOS_RESULTADO = ("dentro_do_filtro", "motivos", "nao_informado")
ESTADOS_CONFERENCIA = (None, "sim", "não", "incompleto")
CAMPOS_NUMERO = ("preco", "condominio", "iptu", "area_m2")
CAMPOS_INTEIRO = ("quartos", "banheiros", "vagas")
COLUNAS_IMOVEL = ("link", "anuncio_id") + CAMPOS_TEXTO + CAMPOS_NUMERO + CAMPOS_INTEIRO

ESTADOS_PEDIDO = ("pendente", "em_andamento", "concluido", "erro")


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_menos_minutos(minutos: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat(timespec="seconds")


MSG_EXPIRADO = "o coletor não terminou em {} minutos (o PC desligou ou o programa caiu?) — crie o pedido de novo"


class DadosInvalidos(ValueError):
    pass


# ---------------------------------------------------------------------------
# Validação / normalização de um anúncio recebido
# ---------------------------------------------------------------------------

def extrair_anuncio_id(link: str) -> Optional[str]:
    """Número do anúncio nos links do VivaReal/ZAP (`...-id-2745678901/`). Outros portais: None."""
    host = (urlsplit(link).hostname or "").lower()
    if host.endswith("vivareal.com.br") or host.endswith("zapimoveis.com.br"):
        m = re.search(r"-id-(\d+)", link)
        return m.group(1) if m else None
    return None


def _numero(valor, nome, inteiro=False):
    if valor in (None, ""):
        return None
    if isinstance(valor, bool):
        raise DadosInvalidos(f"{nome} inválido")
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise DadosInvalidos(f"{nome} não é número: {valor!r}")
    if n != n or n in (float("inf"), float("-inf")) or n < 0:
        raise DadosInvalidos(f"{nome} fora do intervalo: {valor!r}")
    return int(n) if inteiro else n


def normalizar_anuncio(bruto: Any) -> Dict[str, Any]:
    """Valida e limpa um anúncio. Levanta DadosInvalidos com o motivo (em português)."""
    if not isinstance(bruto, dict):
        raise DadosInvalidos("anúncio não é um objeto")
    link = bruto.get("link")
    if not isinstance(link, str) or not link.strip():
        raise DadosInvalidos("sem link")
    partes = urlsplit(link.strip())
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise DadosInvalidos("link inválido")
    host = partes.hostname.lower()
    if not any(host == h or host.endswith("." + h) for h in HOSTS_PORTAIS):
        raise DadosInvalidos(f"link de site que não é um portal conhecido ({host})")
    link_limpo = f"{partes.scheme}://{partes.netloc}{partes.path}"   # sem ?query e #fragmento
    if len(link_limpo) > 500:
        raise DadosInvalidos("link longo demais")
    out: Dict[str, Any] = {"link": link_limpo, "anuncio_id": extrair_anuncio_id(link_limpo)}
    for c in CAMPOS_TEXTO:
        v = bruto.get(c)
        out[c] = None if v in (None, "") else str(v)[:300]
    for c in ("motivos", "nao_informado"):
        v = bruto.get(c)
        out[c] = None if v in (None, "") else str(v)[:500]
    if out["dentro_do_filtro"] not in ESTADOS_CONFERENCIA:
        raise DadosInvalidos(f"dentro_do_filtro inválido: {out['dentro_do_filtro']!r}")
    if not out["portal"]:
        raise DadosInvalidos("sem portal")
    for c in CAMPOS_NUMERO:
        out[c] = _numero(bruto.get(c), c)
    for c in CAMPOS_INTEIRO:
        out[c] = _numero(bruto.get(c), c, inteiro=True)
    return out


def _sem_links_repetidos(anuncios: List[dict]) -> List[dict]:
    """Um link repetido no MESMO envio faria o Postgres recusar o lote inteiro
    ("ON CONFLICT ... cannot affect row a second time"). Fica a última ocorrência."""
    por_link = {}
    for a in anuncios:
        por_link[a["link"]] = a
    return list(por_link.values())


def _juntar_resultado(linha_imovel: dict, conf: dict) -> dict:
    d = dict(linha_imovel)
    for c in CAMPOS_RESULTADO:
        d[c] = conf.get(c)
    return d


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS imoveis (
    link TEXT PRIMARY KEY, anuncio_id TEXT,
    portal TEXT, titulo TEXT, bairro TEXT, endereco TEXT, cidade TEXT, finalidade TEXT,
    dentro_do_filtro TEXT, possivel_duplicado TEXT, busca TEXT,
    preco REAL, condominio REAL, iptu REAL, area_m2 REAL,
    quartos INTEGER, banheiros INTEGER, vagas INTEGER,
    primeira_vista TEXT NOT NULL, ultima_vista TEXT NOT NULL, pedido_id INTEGER,
    tipo TEXT, cidade_busca TEXT
);
CREATE TABLE IF NOT EXISTS resultados (
    pedido_id INTEGER NOT NULL, link TEXT NOT NULL REFERENCES imoveis(link) ON DELETE CASCADE,
    dentro_do_filtro TEXT, motivos TEXT, nao_informado TEXT,
    PRIMARY KEY (pedido_id, link)
);
CREATE INDEX IF NOT EXISTS idx_imoveis_anuncio ON imoveis(anuncio_id);
CREATE INDEX IF NOT EXISTS idx_imoveis_ultima ON imoveis(ultima_vista);
CREATE TABLE IF NOT EXISTS pedidos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filtros TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'pendente',
    criado_em TEXT NOT NULL, iniciado_em TEXT, concluido_em TEXT,
    resumo TEXT, erro TEXT
);
"""


class SQLiteBackend:
    nome = "sqlite"

    def __init__(self, caminho: str = "dados.sqlite"):
        self.caminho = caminho
        self._lock = threading.Lock()
        self._con = sqlite3.connect(caminho, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.executescript(_SQL_SQLITE)

    def salvar_imoveis(self, anuncios: List[dict], coletado_em: str, pedido_id: Optional[int] = None) -> int:
        cols = list(COLUNAS_IMOVEL)
        marcas = ",".join("?" for _ in cols)
        atualiza = ",".join(f"{c}=excluded.{c}" for c in cols if c != "link")
        sql = (f"INSERT INTO imoveis ({','.join(cols)}, primeira_vista, ultima_vista, pedido_id) "
               f"VALUES ({marcas}, ?, ?, ?) ON CONFLICT(link) DO UPDATE SET {atualiza}, "
               f"ultima_vista=excluded.ultima_vista, pedido_id=excluded.pedido_id")
        anuncios = _sem_links_repetidos(anuncios)
        with self._lock, self._con:
            self._con.execute("PRAGMA foreign_keys = ON")
            for a in anuncios:
                self._con.execute(sql, [a.get(c) for c in cols] + [coletado_em, coletado_em, pedido_id])
                if pedido_id is not None:
                    self._con.execute(
                        "INSERT INTO resultados (pedido_id, link, dentro_do_filtro, motivos, nao_informado) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(pedido_id, link) DO UPDATE SET "
                        "dentro_do_filtro=excluded.dentro_do_filtro, motivos=excluded.motivos, "
                        "nao_informado=excluded.nao_informado",
                        (pedido_id, a["link"], a.get("dentro_do_filtro"), a.get("motivos"), a.get("nao_informado")))
        return len(anuncios)

    def buscar_resultados(self, pedido_id: int, limite: int = 2000) -> List[dict]:
        """Imóveis de um pedido, cada um com a conferência DAQUELE pedido."""
        with self._lock:
            rows = self._con.execute(
                "SELECT i.*, r.dentro_do_filtro AS r_dentro, r.motivos AS r_motivos, "
                "r.nao_informado AS r_nao_inf FROM resultados r JOIN imoveis i ON i.link = r.link "
                "WHERE r.pedido_id = ? LIMIT ?", (pedido_id, limite)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            conf = {"dentro_do_filtro": d.pop("r_dentro"), "motivos": d.pop("r_motivos"),
                    "nao_informado": d.pop("r_nao_inf")}
            out.append(_juntar_resultado(d, conf))
        return out

    def buscar_imoveis(self, portal=None, preco_max=None, quartos_min=None, dentro_do_filtro=None,
                       limite: int = 500) -> List[dict]:
        onde, args = [], []
        if portal:
            onde.append("portal = ?"); args.append(portal)
        if preco_max is not None:
            onde.append("preco <= ?"); args.append(preco_max)
        if quartos_min is not None:
            onde.append("quartos >= ?"); args.append(quartos_min)
        if dentro_do_filtro:
            onde.append("dentro_do_filtro = ?"); args.append(dentro_do_filtro)
        sql = "SELECT * FROM imoveis" + (" WHERE " + " AND ".join(onde) if onde else "") + \
              " ORDER BY ultima_vista DESC, preco ASC LIMIT ?"
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, args + [limite])]

    def imoveis_da_cidade(self, cidade_busca: str, desde_iso: Optional[str] = None,
                          limite: int = 20000) -> List[dict]:
        """Todos os imóveis guardados de uma cidade (coletados pela varredura ou por pedidos),
        vistos desde `desde_iso` (ISO-8601). Base da busca instantânea do caminho 2."""
        sql, args = "SELECT * FROM imoveis WHERE cidade_busca = ?", [cidade_busca]
        if desde_iso:
            sql += " AND ultima_vista >= ?"
            args.append(desde_iso)
        with self._lock:
            return [dict(r) for r in self._con.execute(sql + " LIMIT ?", args + [limite])]

    def criar_pedido(self, filtros: dict) -> int:
        with self._lock, self._con:
            cur = self._con.execute(
                "INSERT INTO pedidos (filtros, estado, criado_em) VALUES (?, 'pendente', ?)",
                (json.dumps(filtros, ensure_ascii=False), agora_iso()))
            return cur.lastrowid

    def pegar_proximo_pedido(self) -> Optional[dict]:
        """Reserva o pedido pendente mais antigo (pendente -> em_andamento). Atômico."""
        with self._lock, self._con:
            r = self._con.execute(
                "SELECT id FROM pedidos WHERE estado='pendente' ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            self._con.execute("UPDATE pedidos SET estado='em_andamento', iniciado_em=? WHERE id=?",
                              (agora_iso(), r["id"]))
            return self._pedido(r["id"])

    def _pedido(self, pid):
        r = self._con.execute("SELECT * FROM pedidos WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["filtros"] = json.loads(d["filtros"])
        d["resumo"] = json.loads(d["resumo"]) if d["resumo"] else None   # igual ao Supabase (jsonb -> objeto)
        return d

    def obter_pedido(self, pid: int) -> Optional[dict]:
        with self._lock:
            return self._pedido(pid)

    def finalizar_pedido(self, pid: int, estado: str, resumo: Optional[dict] = None,
                         erro: Optional[str] = None) -> bool:
        if estado not in ("concluido", "erro"):
            raise ValueError("estado final deve ser 'concluido' ou 'erro'")
        with self._lock, self._con:
            cur = self._con.execute(
                "UPDATE pedidos SET estado=?, concluido_em=?, resumo=?, erro=? WHERE id=?",
                (estado, agora_iso(), json.dumps(resumo, ensure_ascii=False) if resumo else None, erro, pid))
            return cur.rowcount > 0

    def expirar_pedidos(self, minutos: int = 30) -> int:
        """Pedidos 'em_andamento' há mais de `minutos` viram 'erro' (o coletor caiu ou o PC desligou)."""
        corte = _iso_menos_minutos(minutos)
        with self._lock, self._con:
            cur = self._con.execute(
                "UPDATE pedidos SET estado='erro', concluido_em=?, erro=? WHERE estado='em_andamento' AND iniciado_em < ?",
                (agora_iso(), MSG_EXPIRADO.format(minutos), corte))
            return cur.rowcount

    def listar_pedidos(self, limite: int = 50) -> List[dict]:
        with self._lock:
            ids = [r["id"] for r in self._con.execute("SELECT id FROM pedidos ORDER BY id DESC LIMIT ?", (limite,))]
            return [self._pedido(i) for i in ids]


# ---------------------------------------------------------------------------
# Supabase (PostgREST via HTTP)
# ---------------------------------------------------------------------------

class SupabaseErro(RuntimeError):
    pass


class SupabaseBackend:
    nome = "supabase"

    def __init__(self, url: str, chave_servico: str, sessao=None, timeout: int = 20):
        import requests
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") \
                and not url.startswith("http://localhost"):
            raise ValueError("SUPABASE_URL deve começar com https://")
        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.s = sessao or requests.Session()
        # Chave nova (sb_secret_...): vai SÓ no cabeçalho apikey. A documentação do Supabase diz que
        # enviá-la também em "Authorization: Bearer" faz o banco recusar (não é um JWT).
        # Chave antiga (service_role, um JWT que começa com "eyJ"): vai nos dois cabeçalhos.
        # Segundo a mesma documentação, as chaves antigas só funcionam até o fim de 2026.
        self.s.headers.update({"apikey": chave_servico})
        if chave_servico.startswith("eyJ"):
            self.s.headers["Authorization"] = f"Bearer {chave_servico}"

    def _req(self, metodo, tabela, params=None, json_body=None, extra=None):
        h = {"Content-Type": "application/json"}
        h.update(extra or {})
        r = self.s.request(metodo, f"{self.base}/{tabela}", params=params, json=json_body,
                           headers=h, timeout=self.timeout)
        if r.status_code >= 300:
            # nunca incluir cabeçalhos (têm a chave) na mensagem
            raise SupabaseErro(f"{metodo} {tabela} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def salvar_imoveis(self, anuncios: List[dict], coletado_em: str, pedido_id: Optional[int] = None) -> int:
        if not anuncios:
            return 0
        anuncios = _sem_links_repetidos(anuncios)
        linhas = []
        for a in anuncios:
            l = {c: a.get(c) for c in COLUNAS_IMOVEL}
            l["ultima_vista"] = coletado_em
            l["pedido_id"] = pedido_id
            linhas.append(l)   # primeira_vista NÃO é enviada: o banco usa o default (now()) só na inserção
        for i in range(0, len(linhas), 200):
            self._req("POST", "imoveis", params={"on_conflict": "link"}, json_body=linhas[i:i + 200],
                      extra={"Prefer": "resolution=merge-duplicates,return=minimal"})
        if pedido_id is not None:
            res = [{"pedido_id": pedido_id, "link": a["link"], **{c: a.get(c) for c in CAMPOS_RESULTADO}}
                   for a in anuncios]
            for i in range(0, len(res), 200):
                self._req("POST", "resultados", params={"on_conflict": "pedido_id,link"}, json_body=res[i:i + 200],
                          extra={"Prefer": "resolution=merge-duplicates,return=minimal"})
        return len(linhas)

    def buscar_resultados(self, pedido_id: int, limite: int = 2000) -> List[dict]:
        """Imóveis de um pedido, com a conferência DAQUELE pedido. Usa o vínculo
        resultados.link -> imoveis.link (chave estrangeira) para trazer tudo numa consulta."""
        linhas = self._req("GET", "resultados", params={
            "select": "dentro_do_filtro,motivos,nao_informado,imoveis(*)",
            "pedido_id": f"eq.{pedido_id}", "limit": str(limite)}) or []
        return [_juntar_resultado(l.get("imoveis") or {}, l) for l in linhas if l.get("imoveis")]

    def buscar_imoveis(self, portal=None, preco_max=None, quartos_min=None, dentro_do_filtro=None,
                       limite: int = 500) -> List[dict]:
        p = {"select": "*", "order": "ultima_vista.desc,preco.asc", "limit": str(limite)}
        if portal:
            p["portal"] = f"eq.{portal}"
        if preco_max is not None:
            p["preco"] = f"lte.{preco_max}"
        if quartos_min is not None:
            p["quartos"] = f"gte.{quartos_min}"
        if dentro_do_filtro:
            p["dentro_do_filtro"] = f"eq.{dentro_do_filtro}"
        return self._req("GET", "imoveis", params=p) or []

    def imoveis_da_cidade(self, cidade_busca: str, desde_iso: Optional[str] = None,
                          limite: int = 20000) -> List[dict]:
        """Igual ao SQLite. O Supabase devolve no máximo 1000 linhas por consulta (configuração
        padrão "Max rows"), então busca em páginas de 1000 até acabar."""
        saida, pagina = [], 1000
        while len(saida) < limite:
            p = {"select": "*", "cidade_busca": f"eq.{cidade_busca}", "order": "link.asc",
                 "limit": str(pagina), "offset": str(len(saida))}
            if desde_iso:
                p["ultima_vista"] = f"gte.{desde_iso}"
            lote = self._req("GET", "imoveis", params=p) or []
            if not lote:      # só para com página VAZIA: o limite do projeto pode ser menor que 1000
                break
            saida.extend(lote)
        return saida[:limite]

    def criar_pedido(self, filtros: dict) -> int:
        r = self._req("POST", "pedidos", json_body={"filtros": filtros},
                      extra={"Prefer": "return=representation"})
        return r[0]["id"]

    def pegar_proximo_pedido(self) -> Optional[dict]:
        """Reserva o pedido pendente mais antigo. Atômico: o PATCH só afeta a linha se ela ainda
        estiver 'pendente'; se outro coletor chegou antes, devolve vazio e tentamos o próximo."""
        for _ in range(5):
            lista = self._req("GET", "pedidos", params={"select": "id", "estado": "eq.pendente",
                                                         "order": "id.asc", "limit": "1"})
            if not lista:
                return None
            pid = lista[0]["id"]
            r = self._req("PATCH", "pedidos", params={"id": f"eq.{pid}", "estado": "eq.pendente"},
                          json_body={"estado": "em_andamento", "iniciado_em": agora_iso()},
                          extra={"Prefer": "return=representation"})
            if r:
                return r[0]
        return None

    def obter_pedido(self, pid: int) -> Optional[dict]:
        r = self._req("GET", "pedidos", params={"select": "*", "id": f"eq.{pid}"})
        return r[0] if r else None

    def finalizar_pedido(self, pid: int, estado: str, resumo: Optional[dict] = None,
                         erro: Optional[str] = None) -> bool:
        if estado not in ("concluido", "erro"):
            raise ValueError("estado final deve ser 'concluido' ou 'erro'")
        r = self._req("PATCH", "pedidos", params={"id": f"eq.{pid}"},
                      json_body={"estado": estado, "concluido_em": agora_iso(), "resumo": resumo, "erro": erro},
                      extra={"Prefer": "return=representation"})
        return bool(r)

    def expirar_pedidos(self, minutos: int = 30) -> int:
        """Pedidos 'em_andamento' há mais de `minutos` viram 'erro' (o coletor caiu ou o PC desligou)."""
        r = self._req("PATCH", "pedidos",
                      params={"estado": "eq.em_andamento", "iniciado_em": f"lt.{_iso_menos_minutos(minutos)}"},
                      json_body={"estado": "erro", "concluido_em": agora_iso(), "erro": MSG_EXPIRADO.format(minutos)},
                      extra={"Prefer": "return=representation"})
        return len(r or [])

    def listar_pedidos(self, limite: int = 50) -> List[dict]:
        return self._req("GET", "pedidos", params={"select": "*", "order": "id.desc", "limit": str(limite)}) or []


# ---------------------------------------------------------------------------
# Escolha do backend
# ---------------------------------------------------------------------------

_instancia = None
_trava = threading.Lock()


def obter():
    """Backend único do processo: Supabase se SUPABASE_URL e SUPABASE_SERVICE_KEY existirem, senão SQLite."""
    global _instancia
    with _trava:
        if _instancia is None:
            url, chave = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
            if url and chave:
                _instancia = SupabaseBackend(url, chave)
            else:
                _instancia = SQLiteBackend(os.environ.get("DB_PATH", "dados.sqlite"))
        return _instancia


def definir(backend) -> None:
    """Para testes."""
    global _instancia
    with _trava:
        _instancia = backend
