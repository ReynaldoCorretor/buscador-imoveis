# -*- coding: utf-8 -*-
"""
scrapers.py

Módulo responsável por buscar imóveis à venda em 4 portais (VivaReal, ZAP
Imóveis, Imovelweb e Chaves na Mão) a partir de um único formulário de busca.

DECISÃO DE ARQUITETURA (ver claude/buscador-imoveis-app.md):
- A extração de dados NÃO é feita por parsing "bonito" de HTML/CSS (frágil e
  difícil de validar sem acesso direto à internet no ambiente de
  desenvolvimento). Em vez disso, usamos regex:
    * VivaReal e Chaves na Mão embutem quartos/área/preço no próprio slug
      da URL do anúncio -> extraímos direto da URL.
    * ZAP Imóveis e Imovelweb não têm esse padrão de slug tão completo,
      então usamos uma "janela" de texto ao redor de cada link no HTML
      bruto da página de resultados para procurar preço/área/quartos/
      banheiros nas proximidades.
- Quando um dado não é encontrado, o imóvel NÃO é descartado: ele aparece
  marcado como "dados_incompletos": True para o usuário conferir no anúncio
  original.
- Limitação conhecida: cada portal permite consultar poucas páginas por
  busca (Chaves na Mão só a página 1, por causa do robots.txt). Não há
  garantia de cobertura de 100% dos anúncios disponíveis em cada cidade.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_TIMEOUT = 15
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


def _extrair_preco_de_texto(texto: str) -> Optional[float]:
    """Procura um valor em R$ dentro de um trecho de texto/HTML."""
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
    """Retorna um trecho do HTML bruto ao redor de um link, para procurar
    preço/área/quartos/banheiros que estejam próximos ao link mas fora dele
    (usado para ZAP e Imovelweb)."""
    inicio = max(0, indice_inicio - tamanho)
    fim = min(len(html), indice_fim + tamanho)
    return html[inicio:fim]


def _buscar_pagina(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Status %s ao buscar %s", resp.status_code, url)
            return None
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Falha ao buscar %s: %s", url, exc)
        return None


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

def buscar_vivareal(cidade: str, uf: str, max_paginas: int = 3) -> List[Imovel]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []

    link_padrao = re.compile(
        r'href="(https://www\.vivareal\.com\.br/(?:imovel|imoveis-lancamentos)/[^"]+)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.vivareal.com.br/venda/{slug_uf}/{slug_cidade}/"
        else:
            url = f"https://www.vivareal.com.br/venda/{slug_uf}/{slug_cidade}/?pagina={pagina}"

        html = _buscar_pagina(url)
        if not html:
            continue

        links_encontrados = set(link_padrao.findall(html))
        for link in links_encontrados:
            slug = link.rstrip("/").rsplit("/", 1)[-1]

            preco_match = re.search(r"[Vv]enda-RS(\d+)", link)
            preco = float(preco_match.group(1)) if preco_match else None

            area_match = re.search(r"-(\d{2,4})m2-", link)
            area = float(area_match.group(1)) if area_match else None

            quartos_match = re.search(r"^[a-z-]*?(\d)-quartos?-", slug)
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

    return imoveis


# ---------------------------------------------------------------------------
# Chaves na Mão — dados também extraídos do slug da URL
# ---------------------------------------------------------------------------

def buscar_chavesnamao(cidade: str, uf: str, max_paginas: int = 1) -> List[Imovel]:
    # robots.txt deste portal bloqueia páginas além da 1; respeitamos isso.
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []

    link_padrao = re.compile(
        r'href="(https://www\.chavesnamao\.com\.br/imovel/[^"]+)"'
    )

    url = f"https://www.chavesnamao.com.br/imoveis-a-venda/{slug_uf}-{slug_cidade}/"
    html = _buscar_pagina(url)
    if not html:
        return imoveis

    links_encontrados = set(link_padrao.findall(html))
    for link in links_encontrados:
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        if slug.startswith("id-"):
            # pega o penúltimo segmento, que carrega os dados
            partes = link.rstrip("/").split("/")
            slug = partes[-2] if len(partes) >= 2 else slug

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

    return imoveis


# ---------------------------------------------------------------------------
# ZAP Imóveis — janela de texto ao redor do link no HTML bruto
# ---------------------------------------------------------------------------

def buscar_zap(cidade: str, uf: str, max_paginas: int = 3) -> List[Imovel]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []

    link_padrao = re.compile(
        r'href="(https://www\.zapimoveis\.com\.br/(?:imovel|lancamentos)/[^"]+)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.zapimoveis.com.br/venda/imoveis/{slug_uf}+{slug_cidade}/"
        else:
            url = f"https://www.zapimoveis.com.br/venda/imoveis/{slug_uf}+{slug_cidade}/?pagina={pagina}"

        html = _buscar_pagina(url)
        if not html:
            continue

        for m in link_padrao.finditer(html):
            link = m.group(1)
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

    # remove duplicados pelo link, mantendo o primeiro
    vistos = set()
    unicos = []
    for im in imoveis:
        if im.link not in vistos:
            vistos.add(im.link)
            unicos.append(im)
    return unicos


# ---------------------------------------------------------------------------
# Imovelweb — janela de texto ao redor do link no HTML bruto
# ---------------------------------------------------------------------------

def buscar_imovelweb(cidade: str, uf: str, max_paginas: int = 3) -> List[Imovel]:
    slug_cidade = _slug_cidade(cidade)
    slug_uf = uf.lower()
    imoveis: List[Imovel] = []

    link_padrao = re.compile(
        r'href="(https://www\.imovelweb\.com\.br/propriedades/[^"]+\.html)"'
    )

    for pagina in range(1, max_paginas + 1):
        if pagina == 1:
            url = f"https://www.imovelweb.com.br/imoveis-venda-{slug_cidade}-{slug_uf}.html"
        else:
            url = (
                f"https://www.imovelweb.com.br/imoveis-venda-{slug_cidade}-"
                f"{slug_uf}-pagina-{pagina}.html"
            )

        html = _buscar_pagina(url)
        if not html:
            continue

        for m in link_padrao.finditer(html):
            link = m.group(1)
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

    vistos = set()
    unicos = []
    for im in imoveis:
        if im.link not in vistos:
            vistos.add(im.link)
            unicos.append(im)
    return unicos


# ---------------------------------------------------------------------------
# Função principal: roda os 4 portais e aplica filtros
# ---------------------------------------------------------------------------

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
    """Roda os 4 scrapers e devolve um dicionário com resultados por portal
    mais uma lista combinada, já filtrada."""

    resultados_por_portal = {}
    erros = {}

    buscadores = {
        "VivaReal": buscar_vivareal,
        "ZAP Imóveis": buscar_zap,
        "Imovelweb": buscar_imovelweb,
        "Chaves na Mão": buscar_chavesnamao,
    }

    todos: List[Imovel] = []

    for nome, funcao in buscadores.items():
        try:
            imoveis = funcao(cidade, uf)
            imoveis_filtrados = _aplica_filtros_basicos(
                imoveis, tipo, quartos_min, banheiros_min, area_min, preco_min, preco_max
            )
            resultados_por_portal[nome] = [im.to_dict() for im in imoveis_filtrados]
            todos.extend(imoveis_filtrados)
        except Exception as exc:  # nunca deixar 1 portal quebrar os outros
            logger.exception("Erro ao buscar no portal %s", nome)
            resultados_por_portal[nome] = []
            erros[nome] = str(exc)

    return {
        "resultados_por_portal": resultados_por_portal,
        "todos": [im.to_dict() for im in todos],
        "erros": erros,
        "total": len(todos),
    }
