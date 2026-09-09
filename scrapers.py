# -*- coding: utf-8 -*-
"""
scrapers.py (v2)

Módulo responsável por buscar imóveis à venda em 4 portais (VivaReal, ZAP
Imóveis, Imovelweb e Chaves na Mão) a partir de um único formulário de busca.

MUDANÇAS NESTA VERSÃO (v2), feitas após o primeiro deploy real ter retornado
"0 imóveis encontrados" mesmo sem filtros:

1. CORREÇÃO DE BUG: os links reais do VivaReal (e possivelmente de outros
   portais) vêm com parâmetros de rastreamento no final, ex:
   ".../venda-RS980000-id-2694147297/?source=ranking%2Crp". O código antigo
   pegava o texto depois da ÚLTIMA barra "/" para extrair o "slug" do
   imóvel, mas como existe uma barra logo antes do "?", ele acabava
   pegando o parâmetro de rastreamento em vez do nome do imóvel. Agora o
   link é limpo (removendo tudo a partir do "?") antes de calcular o slug.

2. DIAGNÓSTICO VISÍVEL: antes, se um portal bloqueasse o pedido (comum em
   portais grandes, que usam proteção antirrobô contra servidores como o
   Render), o código simplesmente devolvia uma lista vazia, sem avisar o
   motivo. Agora cada busca registra o que aconteceu em cada página
   (sucesso, código de erro HTTP, timeout, etc.) e isso é devolvido junto
   com os resultados, para aparecer na tela e ajudar a diagnosticar.

3. CABEÇALHOS mais completos (parecidos com os de um navegador real), para
   reduzir a chance de bloqueio simples por User-Agent.

Continua valendo a decisão de arquitetura original: extração via regex
sobre os links e sobre o HTML ao redor deles (não parsing "bonito" de
HTML), e imóveis com dado faltante não são descartados — só marcados como
"dados_incompletos".
"""

import re
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

REQUEST_TIMEOUT = 8
MAX_PAGINAS_PADRAO = {
    "vivareal": 3,
    "zap": 3,
    "imovelweb": 3,
    "chavesnamao": 1,  # robots.txt bloqueia paginação
}


@dataclass
class Imovel:
    portal: str
    titulo: str
    link: str
    preco: Optional[float] = None
    area_m2: Optional[float] = None
    quartos: Optional[int] = None
    banheiros: Optional[int] = None
    dados_incompletos: bool = False

    def to_dict(self):
        return {
            "portal": self.portal,
            "titulo": self.titulo,
            "link": self.link,
            "preco": self.preco,
            "area_m2": self.area_m2,
            "quartos": self.quartos,
            "banheiros": self.banheiros,
            "dados_incompletos": self.dados_incompletos,
        }


# ---------------------------------------------------------------------------
# Funções auxiliares de parsing
# ---------------------------------------------------------------------------

def _slug_cidade(cidade: str) -> str:
    """Converte 'Mogi das Cruzes' -> 'mogi-das-cruzes' (sem acentos)."""
    import unicodedata

    txt = unicodedata.normalize("NFKD", cidade).encode("ascii", "ignore").decode()
    txt = txt.lower().strip()
    txt = re.sub(r"[^a-z0-9\s-]", "", txt)
    txt = re.sub(r"\s+", "-", txt)
    return txt


def _limpar_link(link: str) -> str:
    """Remove parâmetros de query (?...) e fragmentos (#...) de um link,
    devolvendo a URL "limpa" do anúncio. Isso corrige o bug em que o slug
    do imóvel era confundido com parâmetros de rastreamento como
    '?source=ranking,rp'."""
    partes = urlsplit(link)
    return urlunsplit((partes.scheme, partes.netloc, partes.path, "", ""))


def _extrair_preco_de_texto(texto: str) -> Optional[float]:
    padrao = re.compile(r"R\$\s*([\d\.]{4,})")
    m = padrao.search(texto)
    if not m:
        return None
    valor = m.group(1).replace(".", "")
    try:
        return float(valor)
    except ValueError:
        return None


def _extrair_area_de_texto(texto: str) -> Optional[float]:
    padrao = re.compile(r"(\d{2,4})\s?m2|(\d{2,4})\s?m²|(\d{2,4})m2")
    m = padrao.search(texto)
    if not m:
        return None
    grupo = next((g for g in m.groups() if g), None)
    try:
        return float(grupo) if grupo else None
    except ValueError:
        return None


def _extrair_quartos_de_texto(texto: str) -> Optional[int]:
    padrao = re.compile(r"(\d)\s?(?:quarto|dormitorio|dormitório)", re.IGNORECASE)
    m = padrao.search(texto)
    return int(m.group(1)) if m else None


def _extrair_banheiros_de_texto(texto: str) -> Optional[int]:
    padrao = re.compile(r"(\d)\s?banheiro", re.IGNORECASE)
    m = padrao.search(texto)
    return int(m.group(1)) if m else None


def _janela_ao_redor(html: str, indice_inicio: int, indice_fim: int, tamanho: int = 400) -> str:
    inicio = max(0, indice_inicio - tamanho)
    fim = min(len(html), indice_fim + tamanho)
    return html[inicio:fim]


_PLAYWRIGHT_INSTALACAO_TENTADA = False


def _garantir_navegador_playwright_instalado() -> str:
    """Rede de segurança: se o Chromium do Playwright não estiver
    instalado (o que deveria acontecer durante o build no Render, via
    render.yaml), tenta instalar em tempo de execução, uma única vez por
    processo. Isso cobre o caso de o "Build Command" configurado no painel
    do Render não ter sido atualizado corretamente.

    A instalação em tempo de execução só baixa o navegador em si — não
    instala dependências de sistema via apt (isso precisaria de permissão
    de administrador, que o processo em execução normalmente não tem).
    Por isso, mesmo com esse reforço, pode ainda faltar alguma biblioteca
    do sistema operacional em ambientes muito enxutos — mas vale tentar
    antes de desistir.
    """
    global _PLAYWRIGHT_INSTALACAO_TENTADA
    if _PLAYWRIGHT_INSTALACAO_TENTADA:
        return "instalação em tempo de execução já foi tentada antes nesta sessão do servidor"
    _PLAYWRIGHT_INSTALACAO_TENTADA = True

    import subprocess
    import sys

    try:
        logger.info("Tentando instalar o Chromium do Playwright em tempo de execução...")
        resultado = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=180,
            capture_output=True,
            text=True,
        )
        logger.info("Instalação em tempo de execução concluída: %s", resultado.stdout[-500:])
        return "instalação em tempo de execução concluída com sucesso"
    except Exception as exc:
        logger.warning("Instalação em tempo de execução falhou: %s", exc)
        return f"instalação em tempo de execução também falhou ({exc})"


def _buscar_pagina_playwright(url: str) -> Tuple[Optional[str], str]:
    """Busca uma página usando um navegador de verdade (headless), via
    Playwright. Isso executa o JavaScript da página como um navegador
    normal faria — necessário para portais que só entregam o conteúdo real
    depois de carregar dados via JavaScript, ou que servem uma página
    "vazia" de propósito para pedidos que reconhecem como automatizados
    (requests/cloudscraper não executam JavaScript, então não conseguem
    superar essa barreira).

    Mais lento e pesado que um pedido HTTP comum — por isso só é usado como
    último recurso, depois que a tentativa rápida (requests/cloudscraper)
    falhou ou voltou sem conteúdo de verdade.

    Se o navegador não estiver instalado, tenta se autoinstalar uma vez
    (ver _garantir_navegador_playwright_instalado) e repete a tentativa.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        msg = f"{url} -> Playwright não está instalado neste ambiente ({exc})"
        logger.warning(msg)
        return None, msg

    def _tentar_uma_vez() -> Tuple[Optional[str], Optional[str]]:
        """Devolve (html, None) em sucesso, ou (None, mensagem_de_erro)."""
        try:
            with sync_playwright() as p:
                navegador = p.chromium.launch(
                    headless=True,
                    args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    contexto = navegador.new_context(
                        user_agent=HEADERS["User-Agent"],
                        locale="pt-BR",
                        viewport={"width": 1280, "height": 800},
                    )
                    pagina = contexto.new_page()

                    def _bloquear_recursos_pesados(rota):
                        if rota.request.resource_type in ("image", "media", "font"):
                            rota.abort()
                        else:
                            rota.continue_()

                    pagina.route("**/*", _bloquear_recursos_pesados)
                    pagina.goto(url, wait_until="domcontentloaded", timeout=12000)
                    pagina.wait_for_timeout(1500)
                    html = pagina.content()
                finally:
                    navegador.close()
            return html, None
        except Exception as exc:
            return None, str(exc)

    html, erro = _tentar_uma_vez()

    if html is not None:
        msg = f"{url} -> Playwright (navegador real) OK ({len(html)} caracteres)"
        logger.info(msg)
        return html, msg

    if erro and "Executable doesn't exist" in erro:
        resultado_instalacao = _garantir_navegador_playwright_instalado()
        if "sucesso" in resultado_instalacao:
            html, erro2 = _tentar_uma_vez()
            if html is not None:
                msg = (
                    f"{url} -> Playwright OK após autoinstalação em tempo de "
                    f"execução ({len(html)} caracteres)"
                )
                logger.info(msg)
                return html, msg
            msg = f"{url} -> Playwright falhou mesmo após autoinstalação: {erro2}"
            logger.warning(msg)
            return None, msg
        msg = f"{url} -> Playwright falhou: {erro}; {resultado_instalacao}"
        logger.warning(msg)
        return None, msg

    msg = f"{url} -> Playwright falhou: {erro}"
    logger.warning(msg)
    return None, msg


def _buscar_pagina(url: str, marcador_de_conteudo: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Busca uma página e devolve (html_ou_None, mensagem_de_diagnostico).

    Estratégia em 3 camadas, da mais rápida/barata para a mais pesada:
    1. Requisição HTTP normal (requests).
    2. Se vier 403/429: tenta cloudscraper (contorna proteções simples
       estilo Cloudflare, sem executar JavaScript de verdade).
    3. Se ainda assim falhar, OU se a página vier com HTTP 200 mas sem o
       "marcador_de_conteudo" esperado (indício de que o site entregou uma
       página "vazia" de propósito, sem executar JavaScript para os dados
       reais), tenta um navegador automatizado de verdade via Playwright.

    O parâmetro marcador_de_conteudo é uma pista textual (ex: "/imovel/")
    que DEVE aparecer no HTML quando a página tem conteúdo de verdade. Sem
    isso, não temos como distinguir "página vazia disfarçada de sucesso"
    de "página realmente sem resultados para esta busca".
    """

    def _tem_marcador(html: str) -> bool:
        return marcador_de_conteudo is None or marcador_de_conteudo in html

    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        msg_falha = f"{url} -> tempo esgotado (timeout) após {REQUEST_TIMEOUT}s"
        logger.warning(msg_falha)
        html_pw, msg_pw = _buscar_pagina_playwright(url)
        if html_pw and _tem_marcador(html_pw):
            return html_pw, f"{msg_falha}; {msg_pw}"
        return None, f"{msg_falha}; tentativa com Playwright também falhou ({msg_pw})"
    except requests.RequestException as exc:
        msg_falha = f"{url} -> erro de conexão: {exc}"
        logger.warning(msg_falha)
        html_pw, msg_pw = _buscar_pagina_playwright(url)
        if html_pw and _tem_marcador(html_pw):
            return html_pw, f"{msg_falha}; {msg_pw}"
        return None, f"{msg_falha}; tentativa com Playwright também falhou ({msg_pw})"

    if resp.status_code == 200 and len(resp.text) >= 2000 and _tem_marcador(resp.text):
        msg = f"{url} -> HTTP 200 OK ({len(resp.text)} caracteres)"
        return resp.text, msg

    # Chegou aqui: ou deu 403/429, ou HTTP 200 mas curto/sem o marcador
    # esperado (provável página "vazia" disfarçada). Em ambos os casos,
    # vale tentar as camadas seguintes.
    if resp.status_code == 200:
        motivo_inicial = (
            f"HTTP 200, mas conteúdo sem o marcador esperado "
            f"({len(resp.text)} caracteres) — provável conteúdo disfarçado/vazio"
        )
    else:
        motivo_inicial = f"HTTP {resp.status_code} (provável bloqueio antirrobô do portal para este servidor)"

    # Camada 2: cloudscraper (só faz sentido tentar em caso de bloqueio
    # HTTP explícito; se já veio 200 "vazio", pular direto pro Playwright)
    if resp.status_code in (403, 429):
        try:
            import cloudscraper

            scraper = cloudscraper.create_scraper()
            resp2 = scraper.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp2.status_code == 200 and len(resp2.text) >= 2000 and _tem_marcador(resp2.text):
                msg = f"{url} -> {motivo_inicial}; cloudscraper conseguiu contornar (HTTP 200, {len(resp2.text)} caracteres)"
                logger.info(msg)
                return resp2.text, msg
        except Exception:
            pass  # segue para a camada 3 (Playwright)

    # Camada 3: navegador automatizado de verdade
    html_pw, msg_pw = _buscar_pagina_playwright(url)
    if html_pw and _tem_marcador(html_pw):
        msg = f"{url} -> {motivo_inicial}; Playwright (navegador real) conseguiu obter os dados ({len(html_pw)} caracteres)"
        logger.info(msg)
        return html_pw, msg

    msg = f"{url} -> {motivo_inicial}; tentativa com Playwright também falhou ({msg_pw})"
    logger.warning(msg)
    return None, msg


def _diagnosticar_ausencia_de_links(html: str, pista: str, tamanho_snippet: int = 250) -> str:
    """Quando nenhum link de imóvel é encontrado numa página que carregou
    normalmente (HTTP 200), esta função investiga o HTML bruto em busca de
    uma pista (ex: '/imovel/') para descobrir o formato real usado pelo
    site, sem precisar de acesso externo para inspecionar manualmente.
    Devolve uma mensagem pronta para aparecer no diagnóstico da busca."""
    ocorrencias = html.count(pista)
    if ocorrencias == 0:
        return (
            f"nenhuma ocorrência de '{pista}' encontrada no HTML recebido "
            f"({len(html)} caracteres) — o site pode ter mudado a estrutura "
            "da página, ou está servindo um conteúdo diferente para este servidor"
        )

    indice = html.find(pista)
    inicio = max(0, indice - 40)
    fim = min(len(html), indice + tamanho_snippet)
    trecho = html[inicio:fim].replace("\n", " ").replace("\t", " ")
    trecho = re.sub(r"\s+", " ", trecho)
    return (
        f"'{pista}' aparece {ocorrencias}x no HTML, mas não no formato "
        f"esperado pelo padrão de busca. Trecho real encontrado: ...{trecho}..."
    )


def _aplica_filtros_basicos(
    imoveis: List[Imovel],
    tipo: Optional[str],
    quartos_min: Optional[int],
    banheiros_min: Optional[int],
    area_min: Optional[float],
    preco_min: Optional[float],
    preco_max: Optional[float],
) -> List[Imovel]:
    resultado = []
    for im in imoveis:
        if tipo and tipo.lower() not in im.titulo.lower() and tipo.lower() not in im.link.lower():
            continue
        if quartos_min and im.quartos is not None and im.quartos < quartos_min:
            continue
        if banheiros_min and im.banheiros is not None and im.banheiros < banheiros_min:
            continue
        if area_min and im.area_m2 is not None and im.area_m2 < area_min:
            continue
        if preco_min and im.preco is not None and im.preco < preco_min:
            continue
        if preco_max and im.preco is not None and im.preco > preco_max:
            continue
        resultado.append(im)
    return resultado


# ---------------------------------------------------------------------------
# VivaReal — dados extraídos direto do slug da URL
# ---------------------------------------------------------------------------

def buscar_vivareal(cidade: str, uf: str, tipo: Optional[str] = None, max_paginas: int = 1) -> Tuple[List[Imovel], List[str]]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []
    diagnosticos: List[str] = []

    link_padrao = re.compile(
        r'href="((?:https://www\.vivareal\.com\.br)?/(?:imovel|imoveis-lancamentos)/[^"]+)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.vivareal.com.br/venda/{slug_uf}/{slug_cidade}/"
        else:
            url = f"https://www.vivareal.com.br/venda/{slug_uf}/{slug_cidade}/?pagina={pagina}"

        html, diag = _buscar_pagina(url, marcador_de_conteudo="/imovel/")
        diagnosticos.append(diag)
        if not html:
            continue

        links_brutos = set(link_padrao.findall(html))
        for link_bruto in links_brutos:
            if link_bruto.startswith("/"):
                link_bruto = "https://www.vivareal.com.br" + link_bruto
            link = _limpar_link(link_bruto)
            slug = link.rstrip("/").rsplit("/", 1)[-1]

            preco_match = re.search(r"[Vv]enda-RS(\d+)", link)
            preco = float(preco_match.group(1)) if preco_match else None

            area_match = re.search(r"-(\d{2,4})m2-", link)
            area = float(area_match.group(1)) if area_match else None

            quartos_match = re.search(r"(\d)-quartos?-", slug)
            quartos = int(quartos_match.group(1)) if quartos_match else None

            titulo = slug.replace("-", " ").replace("id ", "").strip().title()

            incompleto = preco is None or area is None or quartos is None

            imoveis.append(
                Imovel(
                    portal="VivaReal",
                    titulo=titulo,
                    link=link,
                    preco=preco,
                    area_m2=area,
                    quartos=quartos,
                    dados_incompletos=incompleto,
                )
            )

        diagnosticos[-1] += f" — {len(links_brutos)} links de imóveis encontrados nesta página"

    return imoveis, diagnosticos


# ---------------------------------------------------------------------------
# Chaves na Mão — dados também extraídos do slug da URL
# ---------------------------------------------------------------------------

# Mapeia cada opção do formulário para o trecho de URL usado pelo Chaves na
# Mão para aquele tipo de imóvel (confirmado a partir da estrutura real do
# site em 09/2026). Usar essa URL específica é mais preciso do que buscar
# tudo e filtrar depois pelo título.
TIPO_PARA_SLUG_CHAVESNAMAO = {
    "Apartamento": "apartamentos-a-venda",
    "Casa": "casas-a-venda",
    "Casa de condomínio": "casas-em-condominio-a-venda",
    "Cobertura": "coberturas-a-venda",
    "Terreno/Lote": "terrenos-a-venda",
    "Terreno em condomínio": "terrenos-em-condominio-a-venda",
    "Chácara": "chacaras-a-venda",
    "Fazenda/Sítio": "fazendas-a-venda",
    "Flat": "flat-a-venda",
    "Kitnet": "kitnet-a-venda",
    "Loft": "loft-a-venda",
    "Sala comercial": "sala-comercial-a-venda",
    "Casa comercial": "casa-comercial-a-venda",
    "Prédio comercial": "predio-a-venda",
    "Galpão/Depósito": "galpao-a-venda",
    "Ponto comercial": "ponto-comercial-a-venda",
    "Terreno comercial": "terreno-comercial-a-venda",
    "Garagem": "garagem-a-venda",
}

# Quando o usuário NÃO especifica um tipo ("Qualquer tipo"), em vez de
# buscar só a página 1 de uma categoria genérica (poucos resultados),
# combinamos a página 1 de vários tipos comuns. Isso multiplica a
# quantidade de resultados SEM violar o robots.txt do portal — que
# bloqueia paginação dentro de uma mesma categoria, não o acesso a
# categorias diferentes (cada uma tem sua própria "página 1" permitida).
TIPOS_AMPLOS_PADRAO_CHAVESNAMAO = [
    "casas-a-venda",
    "apartamentos-a-venda",
    "casas-em-condominio-a-venda",
    "terrenos-a-venda",
]


def buscar_chavesnamao(cidade: str, uf: str, tipo: Optional[str] = None, max_paginas: int = 1) -> Tuple[List[Imovel], List[str]]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []
    diagnosticos: List[str] = []

    # Tentativa 1: link clássico dentro de href="..." (aspas duplas),
    # absoluto ou relativo.
    link_padrao = re.compile(
        r'href="((?:https://www\.chavesnamao\.com\.br)?/imovel/[^"]+)"'
    )
    # Tentativa 2 (mais ampla): reconhece o formato do link em qualquer
    # lugar do HTML — inclusive dentro de dados JSON embutidos na página
    # (comum em sites feitos com Next.js/React), onde as barras podem vir
    # "escapadas" (\/) e as aspas podem ser simples.
    link_padrao_amplo = re.compile(
        r'\\?/imovel\\?/[a-z0-9\-]+\\?/id-\d+\\?/?', re.IGNORECASE
    )

    if tipo and tipo in TIPO_PARA_SLUG_CHAVESNAMAO:
        slugs_categoria = [TIPO_PARA_SLUG_CHAVESNAMAO[tipo]]
    else:
        slugs_categoria = TIPOS_AMPLOS_PADRAO_CHAVESNAMAO

    links_vistos = set()

    for slug_categoria in slugs_categoria:
        url = f"https://www.chavesnamao.com.br/{slug_categoria}/{slug_uf}-{slug_cidade}/"
        html, diag = _buscar_pagina(url, marcador_de_conteudo="/imovel/")

        if not html:
            diagnosticos.append(diag)
            continue

        links_brutos = set(link_padrao.findall(html))
        metodo = "padrão href=\"...\""

        if not links_brutos:
            achados_amplos = set(m.group(0) for m in link_padrao_amplo.finditer(html))
            links_brutos = {trecho.replace("\\/", "/") for trecho in achados_amplos}
            if links_brutos:
                metodo = "padrão amplo (formato alternativo/JSON)"

        if links_brutos:
            diag += f" — {len(links_brutos)} links encontrados ({metodo})"
        else:
            diag += " — 0 links encontrados. " + _diagnosticar_ausencia_de_links(html, "/imovel/")
        diagnosticos.append(diag)

        for link_bruto in links_brutos:
            if link_bruto.startswith("/"):
                link_bruto = "https://www.chavesnamao.com.br" + link_bruto
            link = _limpar_link(link_bruto)

            if link in links_vistos:
                continue
            links_vistos.add(link)

            partes = link.rstrip("/").split("/")
            slug = partes[-2] if len(partes) >= 2 and partes[-1].startswith("id-") else partes[-1]

            preco_match = re.search(r"RS(\d+)", link)
            preco = float(preco_match.group(1)) if preco_match else None

            area_match = re.search(r"-(\d{2,5})m2-", link)
            area = float(area_match.group(1)) if area_match else None

            quartos_match = re.search(r"(\d)-quartos?-", link)
            quartos = int(quartos_match.group(1)) if quartos_match else None

            titulo = slug.replace("-", " ").title()
            incompleto = preco is None or area is None or quartos is None

            imoveis.append(
                Imovel(
                    portal="Chaves na Mão",
                    titulo=titulo,
                    link=link,
                    preco=preco,
                    area_m2=area,
                    quartos=quartos,
                    dados_incompletos=incompleto,
                )
            )

    return imoveis, diagnosticos


# ---------------------------------------------------------------------------
# ZAP Imóveis — janela de texto ao redor do link no HTML bruto
# ---------------------------------------------------------------------------

def buscar_zap(cidade: str, uf: str, tipo: Optional[str] = None, max_paginas: int = 1) -> Tuple[List[Imovel], List[str]]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []
    diagnosticos: List[str] = []

    link_padrao = re.compile(
        r'href="((?:https://www\.zapimoveis\.com\.br)?/(?:imovel|lancamentos)/[^"]+)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.zapimoveis.com.br/venda/imoveis/{slug_uf}+{slug_cidade}/"
        else:
            url = f"https://www.zapimoveis.com.br/venda/imoveis/{slug_uf}+{slug_cidade}/?pagina={pagina}"

        html, diag = _buscar_pagina(url, marcador_de_conteudo="/imovel/")
        diagnosticos.append(diag)
        if not html:
            continue

        encontrados_nesta_pagina = 0
        for m in link_padrao.finditer(html):
            link_bruto = m.group(1)
            if link_bruto.startswith("/"):
                link_bruto = "https://www.zapimoveis.com.br" + link_bruto
            link = _limpar_link(link_bruto)
            janela = _janela_ao_redor(html, m.start(), m.end())

            preco = _extrair_preco_de_texto(janela)
            area = _extrair_area_de_texto(link) or _extrair_area_de_texto(janela)
            quartos = _extrair_quartos_de_texto(link) or _extrair_quartos_de_texto(janela)
            banheiros = _extrair_banheiros_de_texto(janela)

            slug = link.rstrip("/").rsplit("/", 1)[-1]
            titulo = slug.replace("-", " ").title()

            incompleto = preco is None or area is None or quartos is None

            imoveis.append(
                Imovel(
                    portal="ZAP Imóveis",
                    titulo=titulo,
                    link=link,
                    preco=preco,
                    area_m2=area,
                    quartos=quartos,
                    banheiros=banheiros,
                    dados_incompletos=incompleto,
                )
            )
            encontrados_nesta_pagina += 1

        diagnosticos[-1] += f" — {encontrados_nesta_pagina} links de imóveis encontrados nesta página"

    vistos = set()
    unicos = []
    for im in imoveis:
        if im.link not in vistos:
            vistos.add(im.link)
            unicos.append(im)
    return unicos, diagnosticos


# ---------------------------------------------------------------------------
# Imovelweb — janela de texto ao redor do link no HTML bruto
# ---------------------------------------------------------------------------

def buscar_imovelweb(cidade: str, uf: str, tipo: Optional[str] = None, max_paginas: int = 1) -> Tuple[List[Imovel], List[str]]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []
    diagnosticos: List[str] = []

    link_padrao = re.compile(
        r'href="((?:https://www\.imovelweb\.com\.br)?/propriedades/[^"]+\.html)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.imovelweb.com.br/imoveis-venda-{slug_cidade}-{slug_uf}.html"
        else:
            url = (
                f"https://www.imovelweb.com.br/imoveis-venda-{slug_cidade}-"
                f"{slug_uf}-pagina-{pagina}.html"
            )

        html, diag = _buscar_pagina(url, marcador_de_conteudo="/propriedades/")
        diagnosticos.append(diag)
        if not html:
            continue

        encontrados_nesta_pagina = 0
        for m in link_padrao.finditer(html):
            link_bruto = m.group(1)
            if link_bruto.startswith("/"):
                link_bruto = "https://www.imovelweb.com.br" + link_bruto
            link = _limpar_link(link_bruto)
            janela = _janela_ao_redor(html, m.start(), m.end())

            preco = _extrair_preco_de_texto(janela)
            area = _extrair_area_de_texto(janela)
            quartos = _extrair_quartos_de_texto(janela) or _extrair_quartos_de_texto(link)
            banheiros = _extrair_banheiros_de_texto(janela)

            slug = link.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
            titulo = slug.replace("-", " ").title()

            incompleto = preco is None or area is None or quartos is None

            imoveis.append(
                Imovel(
                    portal="Imovelweb",
                    titulo=titulo,
                    link=link,
                    preco=preco,
                    area_m2=area,
                    quartos=quartos,
                    banheiros=banheiros,
                    dados_incompletos=incompleto,
                )
            )
            encontrados_nesta_pagina += 1

        diagnosticos[-1] += f" — {encontrados_nesta_pagina} links de imóveis encontrados nesta página"

    vistos = set()
    unicos = []
    for im in imoveis:
        if im.link not in vistos:
            vistos.add(im.link)
            unicos.append(im)
    return unicos, diagnosticos


# ---------------------------------------------------------------------------
# Função principal: roda os portais ATIVOS e aplica filtros
# ---------------------------------------------------------------------------

# Depois de confirmar bloqueio real (HTTP 403 persistente, mesmo com
# cloudscraper e Playwright) em VivaReal, ZAP Imóveis e Imovelweb, esses 3
# portais foram temporariamente desativados por padrão. Insistir neles a
# cada busca só consome tempo à toa e aumenta o risco de a busca inteira
# estourar o limite de tempo do Render (causa mais provável do "Internal
# Server Error" visto anteriormente).
#
# O código de cada um continua abaixo, pronto para ser reativado (mudando
# True/False aqui) se no futuro isso deixar de ser um bloqueio — por
# exemplo, se um serviço pago de scraping for adotado no lugar de
# requests/cloudscraper/Playwright.
PORTAIS_ATIVOS = {
    "VivaReal": False,
    "ZAP Imóveis": False,
    "Imovelweb": False,
    "Chaves na Mão": True,
}


def buscar_todos_portais(
    cidade: str,
    uf: str,
    tipo: Optional[str] = None,
    quartos_min: Optional[int] = None,
    banheiros_min: Optional[int] = None,
    area_min: Optional[float] = None,
    preco_min: Optional[float] = None,
    preco_max: Optional[float] = None,
) -> dict:
    """Roda os scrapers dos portais ATIVOS (ver PORTAIS_ATIVOS) e devolve um
    dicionário com resultados por portal, lista combinada já filtrada, e um
    diagnóstico técnico por portal (o que aconteceu em cada página buscada
    — sucesso, bloqueio, erro etc., ou "desativado" para os que estão
    pausados)."""

    resultados_por_portal = {}
    diagnosticos_por_portal = {}
    erros = {}

    buscadores = {
        "VivaReal": buscar_vivareal,
        "ZAP Imóveis": buscar_zap,
        "Imovelweb": buscar_imovelweb,
        "Chaves na Mão": buscar_chavesnamao,
    }

    todos: List[Imovel] = []

    for nome, funcao in buscadores.items():
        if not PORTAIS_ATIVOS.get(nome, True):
            resultados_por_portal[nome] = []
            diagnosticos_por_portal[nome] = [
                "Portal temporariamente desativado (bloqueio antirrobô "
                "confirmado nas tentativas anteriores) — não foi consultado "
                "nesta busca, para acelerar e evitar erro de tempo esgotado."
            ]
            continue

        try:
            imoveis, diagnosticos = funcao(cidade, uf, tipo=tipo)
            # Chaves na Mão já busca na URL certa do tipo pedido (ou combina
            # vários tipos comuns quando nenhum foi especificado) — filtrar
            # de novo pelo texto do título aqui seria redundante e poderia
            # descartar por engano imóveis válidos com título escrito
            # diferente do nome exato do tipo.
            tipo_para_filtro_extra = None if nome == "Chaves na Mão" else tipo
            imoveis_filtrados = _aplica_filtros_basicos(
                imoveis, tipo_para_filtro_extra, quartos_min, banheiros_min, area_min, preco_min, preco_max
            )
            resultados_por_portal[nome] = [im.to_dict() for im in imoveis_filtrados]
            diagnosticos_por_portal[nome] = diagnosticos
            todos.extend(imoveis_filtrados)

            if len(imoveis) == 0:
                erros[nome] = "; ".join(diagnosticos) if diagnosticos else "sem detalhes"

        except Exception as exc:  # nunca deixar 1 portal quebrar os outros
            logger.exception("Erro ao buscar no portal %s", nome)
            resultados_por_portal[nome] = []
            diagnosticos_por_portal[nome] = [f"Erro inesperado: {exc}"]
            erros[nome] = str(exc)

    return {
        "resultados_por_portal": resultados_por_portal,
        "diagnosticos_por_portal": diagnosticos_por_portal,
        "todos": [im.to_dict() for im in todos],
        "erros": erros,
        "total": len(todos),
    }
