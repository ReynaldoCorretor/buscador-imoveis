# -*- coding: utf-8 -*-
"""
regiao.py — as 10 cidades atendidas (Mogi das Cruzes e região). O MESMO arquivo vai no
pacote do PC e no pacote do site: se mudar aqui, mude nos dois.

A chave (slug) é só um nome interno. O endereço real de cada cidade em cada portal é
aprendido de endereços copiados do site (aprender_cidades.py) e gravado em cidades.json.
"""
import unicodedata

CIDADES_REGIAO = [
    ("mogi-das-cruzes", "Mogi das Cruzes"),
    ("suzano", "Suzano"),
    ("itaquaquecetuba", "Itaquaquecetuba"),
    ("ferraz-de-vasconcelos", "Ferraz de Vasconcelos"),
    ("poa", "Poá"),
    ("aruja", "Arujá"),
    ("guararema", "Guararema"),
    ("biritiba-mirim", "Biritiba Mirim"),
    ("santa-isabel", "Santa Isabel"),
    ("bertioga", "Bertioga"),
]
UF = "sp"
NOMES = dict(CIDADES_REGIAO)


def norm(txt) -> str:
    """'Poá' -> 'poa' (sem acento, minúsculas, espaços simples)."""
    t = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(t.split())


def slug_por_nome(nome):
    """'Poá', 'poa', 'POA' -> 'poa'; None se não for uma das 10 cidades."""
    alvo = norm(nome).replace("-", " ")
    for slug, n in CIDADES_REGIAO:
        if norm(n) == alvo or slug.replace("-", " ") == alvo:
            return slug
    return None
