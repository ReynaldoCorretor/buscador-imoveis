# -*- coding: utf-8 -*-
"""
coletor_servidor.py — roda o coletor (coletor.py, o MESMO do PC) num PROCESSO SEPARADO no servidor.

Por que processo separado:
  * o site já usa um navegador próprio (Chaves na Mão) na thread de buscas; dois Playwright
    síncronos na mesma thread não convivem;
  * se faltar memória, o sistema derruba este processo, não o site;
  * dá para medir o pico de memória da coleta (o Render gratuito tem 512 MB).

Uso interno (o site chama via rodar()):
    python coletor_servidor.py '{"cidade": "suzano", "filtros": {...}, "paginas": 1, "leve": true}'
A última linha da saída é  @@RESULTADO@@{json}.
"""
import json
import os
import subprocess
import sys
import threading
import time

MARCA = "@@RESULTADO@@"
PASTA = os.path.dirname(os.path.abspath(__file__))
CAMPOS = ("portal", "titulo", "link", "preco", "condominio", "iptu", "area_m2", "quartos", "banheiros", "vagas",
          "bairro", "endereco", "cidade", "finalidade", "dentro_do_filtro", "motivos", "nao_informado",
          "possivel_duplicado", "busca", "tipo", "cidade_busca", "anuncio_id")


# ---------------------------------------------------------------------------
# Medição de memória (Linux; no Windows devolve None)
# ---------------------------------------------------------------------------

def _pss_kb(pid: int):
    """PSS: memória do processo com a parte COMPARTILHADA dividida entre quem a usa. Somar RSS de
    vários processos do Chromium conta as bibliotecas compartilhadas várias vezes (infla o total)."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for linha in f:
                if linha.startswith("Pss:"):
                    return float(linha.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _rss_arvore_mb(pid_raiz: int):
    """Soma a memória (PSS; se indisponível, RSS) deste processo e de todos os descendentes."""
    try:
        filhos = {}
        rss = {}
        pagina_kb = os.sysconf("SC_PAGE_SIZE") / 1024
        for nome in os.listdir("/proc"):
            if not nome.isdigit():
                continue
            try:
                with open(f"/proc/{nome}/stat") as f:
                    campos = f.read().rsplit(")", 1)[1].split()
                ppid, paginas = int(campos[1]), int(campos[21])
            except (OSError, IndexError, ValueError):
                continue
            filhos.setdefault(ppid, []).append(int(nome))
            rss[int(nome)] = paginas * pagina_kb
        total, pilha = 0.0, [pid_raiz]
        while pilha:
            p = pilha.pop()
            total += _pss_kb(p) or rss.get(p, 0)
            pilha.extend(filhos.get(p, []))
        return round(total / 1024, 1)
    except Exception:
        return None


def _ler_cgroup(*nomes):
    for n in nomes:
        try:
            with open(n) as f:
                v = f.read().strip()
            mb = None if v == "max" else round(int(v) / 1024 / 1024, 1)
            return mb if mb is None or mb < 1_000_000 else None   # "sem limite" aparece como número gigante
        except (OSError, ValueError):
            continue
    return None


class Medidor(threading.Thread):
    def __init__(self, intervalo=1.5):
        super().__init__(daemon=True)
        self.pico_mb, self._parar, self.intervalo = 0.0, threading.Event(), intervalo

    def run(self):
        while not self._parar.is_set():
            v = _rss_arvore_mb(os.getpid())
            if v:
                self.pico_mb = max(self.pico_mb, v)
            self._parar.wait(self.intervalo)

    def parar(self):
        self._parar.set()


# ---------------------------------------------------------------------------
# Execução (dentro do processo separado)
# ---------------------------------------------------------------------------

def executar(params: dict, log=lambda m: None) -> dict:
    import coletor
    medidor = Medidor()
    medidor.start()
    t0 = time.time()
    try:
        r = coletor.coletar(cidade_slug=params["cidade"], filtros=params.get("filtros") or {},
                            paginas=int(params.get("paginas", 1)), leve=bool(params.get("leve", True)),
                            nome_busca=params.get("nome_busca", ""), log=log)
    except coletor.ColetorErro as exc:
        return {"ok": False, "erro": f"O coletor não pôde montar esta busca: {exc}"}
    finally:
        medidor.parar()
    return {
        "ok": True, "segundos": round(time.time() - t0, 1), "versao": r.get("versao"), "cidade": r.get("cidade"),
        "memoria": {"pico_coleta_mb": medidor.pico_mb or None,
                    "limite_container_mb": _ler_cgroup("/sys/fs/cgroup/memory.max",
                                                       "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
                    "pico_container_mb": _ler_cgroup("/sys/fs/cgroup/memory.peak",
                                                     "/sys/fs/cgroup/memory/memory.max_usage_in_bytes")},
        "portais": r.get("portais", {}), "conformidade": r.get("conformidade", {}), "avisos": r.get("avisos", []),
        "erro_navegador": r.get("erro_navegador"), "descartados": len(r.get("descartados", [])),
        "urls_usadas": r.get("urls_usadas", {}),
        "anuncios": [{k: a.get(k) for k in CAMPOS} for a in r.get("anuncios", [])],
    }


def main():
    params = json.loads(sys.argv[1])
    def log(m):
        m = str(m).rstrip()
        if m:
            print(m, file=sys.stderr, flush=True)
    try:
        out = executar(params, log)
    except Exception as exc:   # nunca sair sem resposta
        out = {"ok": False, "erro": f"{type(exc).__name__}: {str(exc)[:300]}"}
    print(MARCA + json.dumps(out, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Lado do site: dispara o processo e interpreta a saída
# ---------------------------------------------------------------------------

def rodar(params: dict, timeout_s: int = 600) -> dict:
    """Roda a coleta em processo separado. Nunca levanta: falhas viram {"ok": False, "erro": ...}."""
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, os.path.join(PASTA, "coletor_servidor.py"), json.dumps(params)],
                           cwd=PASTA, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        log = (exc.stderr or b"")
        log = log.decode("utf-8", "replace") if isinstance(log, bytes) else log
        return {"ok": False, "erro": f"a coleta passou de {timeout_s}s e foi interrompida",
                "log": log.splitlines()[-40:], "segundos": round(time.time() - t0, 1)}
    log = (p.stderr or "").splitlines()[-40:]
    for linha in reversed((p.stdout or "").splitlines()):
        if linha.startswith(MARCA):
            out = json.loads(linha[len(MARCA):])
            out["log"] = log
            return out
    motivo = f"o processo da coleta terminou sem resposta (código {p.returncode})"
    if p.returncode in (-9, 137):
        motivo += " — provável FALTA DE MEMÓRIA (o sistema encerrou o processo)"
    return {"ok": False, "erro": motivo, "log": log, "segundos": round(time.time() - t0, 1)}


if __name__ == "__main__":
    main()
