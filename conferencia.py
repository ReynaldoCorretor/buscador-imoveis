# -*- coding: utf-8 -*-
"""
conferencia.py — confere cada anúncio contra TODOS os critérios do pedido e diz o motivo.

Por quê: o portal aplica o filtro, mas COMPLETA a lista com imóveis "parecidos" (verificado em
25/10 e 31/10: 25 de 42 respeitavam o filtro). Aqui cada anúncio recebe:
    dentro_do_filtro = "sim"         -> todos os critérios pedidos foram conferidos e batem
                       "incompleto"  -> nenhum critério falhou, mas algum dado não veio no anúncio
                       "não"         -> pelo menos um critério falhou (o motivo diz qual)
    motivos          = "preço R$ 450.000 acima de R$ 400.000; quartos: 1 (pedido: 2 a 3)"
    nao_informado    = "vagas, banheiros"
    tipo             = tipo deduzido do título/link ("apartamento", "casa", "cobertura"...)

Regra: dado AUSENTE nunca vira "sim" — vira "incompleto", e o campo aparece em nao_informado.

Critérios aceitos em `filtros`: tipo ("apartamento"|"casa"), quartos ("2,3"), preco_min, preco_max,
area_min, area_max, banheiros_min, vagas_min, bairro (slug) + bairro_nome (opcional).

[NÃO VERIFICADO] a dedução do TIPO pelo título/link: o título "Apartamento para comprar com..."
aparece nos dados reais, mas a lista completa de palavras dos portais não foi conferida.
"""
import re

import regiao

# (palavra no texto, tipo deduzido) — ordem importa: o mais específico primeiro
_TIPOS = [
    (r"casa de condominio|casa em condominio", "casa de condomínio"),
    (r"cobertura", "cobertura"),
    (r"apartamento|apto\b", "apartamento"),
    (r"kitnet|kitinete|quitinete", "kitnet"),
    (r"studio|estudio", "studio"),
    (r"\bflat\b", "flat"),
    (r"\bloft\b", "loft"),
    (r"sobrado", "sobrado"),
    (r"\bcasa\b|\bcasas\b", "casa"),
    (r"terreno|\blote\b|loteamento", "terreno"),
    (r"galpao|deposito", "galpão"),
    (r"\bsala\b|conjunto comercial|\bloja\b|ponto comercial|predio", "comercial"),
    (r"chacara|\bsitio\b|fazenda", "rural"),
]

# o que conta como EXATO para cada tipo pedido; o resto é "parecido" com o motivo
_EXATOS = {
    "apartamento": {"apartamento"},
    "casa": {"casa", "sobrado", "casa de condomínio"},
}


def deduzir_tipo(anuncio: dict):
    """Tipo pelo título; se não der, pelo link (ex.: '/imovel/venda-apartamento-3-quartos-...')."""
    for fonte in (anuncio.get("titulo"), _trecho_link(anuncio.get("link"))):
        t = regiao.norm(fonte).replace("-", " ")
        if not t:
            continue
        for padrao, tipo in _TIPOS:
            if re.search(padrao, t):
                return tipo
    return None


def _trecho_link(link):
    m = re.search(r"/(?:imovel|propriedades)/([^/?#]+)", link or "")
    return m.group(1) if m else ""


def _reais(v) -> str:
    return "R$ " + f"{int(round(float(v))):,}".replace(",", ".")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _lista_quartos(q):
    if q in (None, ""):
        return None
    if isinstance(q, int):
        return [q]
    return sorted({int(x) for x in re.split(r"[,\s]+", str(q).strip()) if x})


def avaliar(anuncio: dict, filtros: dict) -> dict:
    f = filtros or {}
    motivos, faltam = [], []
    tipo = deduzir_tipo(anuncio)

    if f.get("tipo"):
        if tipo is None:
            faltam.append("tipo")
        elif tipo not in _EXATOS.get(f["tipo"], {f["tipo"]}):
            motivos.append(f"tipo: {tipo} (pedido: {f['tipo']})")

    lq = _lista_quartos(f.get("quartos"))
    if lq:
        q = _num(anuncio.get("quartos"))
        faixa = f"{lq[0]}" if lq[0] == lq[-1] else f"{lq[0]} a {lq[-1]}"
        if q is None:
            faltam.append("quartos")
        elif q < lq[0] or q > lq[-1]:
            motivos.append(f"quartos: {int(q)} (pedido: {faixa})")

    p = _num(anuncio.get("preco"))
    for chave, compara, texto in (("preco_max", lambda v, lim: v > lim, "acima de"),
                                  ("preco_min", lambda v, lim: v < lim, "abaixo de")):
        lim = _num(f.get(chave))
        if lim is None:
            continue
        if p is None:
            if "preço" not in faltam:
                faltam.append("preço")
        elif compara(p, lim):
            motivos.append(f"preço {_reais(p)} {texto} {_reais(lim)}")

    a = _num(anuncio.get("area_m2"))
    for chave, compara, texto in (("area_min", lambda v, lim: v < lim, "abaixo de"),
                                  ("area_max", lambda v, lim: v > lim, "acima de")):
        lim = _num(f.get(chave))
        if lim is None:
            continue
        if a is None:
            if "área" not in faltam:
                faltam.append("área")
        elif compara(a, lim):
            motivos.append(f"área {a:g} m² {texto} {lim:g} m²")

    for chave, campo, rotulo in (("banheiros_min", "banheiros", "banheiros"), ("vagas_min", "vagas", "vagas")):
        lim = _num(f.get(chave))
        if lim is None:
            continue
        v = _num(anuncio.get(campo))
        if v is None:
            faltam.append(rotulo)
        elif v < lim:
            motivos.append(f"{rotulo}: {int(v)} (pedido: {int(lim)} ou mais)")

    if f.get("bairro"):
        esperado = regiao.norm(f.get("bairro_nome") or f["bairro"].replace("-", " "))
        b = anuncio.get("bairro")
        if not b:
            faltam.append("bairro")
        elif regiao.norm(b) != esperado:
            motivos.append(f"bairro: {b}")

    estado = "não" if motivos else ("incompleto" if faltam else "sim")
    return {"dentro_do_filtro": estado, "motivos": "; ".join(motivos),
            "nao_informado": ", ".join(faltam), "tipo": tipo or ""}


def contar(anuncios: list) -> dict:
    """Totais por estado + aviso se NADA bate (sinal de que o portal não aplicou o filtro)."""
    est = [a.get("dentro_do_filtro") for a in anuncios]
    r = {"total": len(anuncios), "dentro": est.count("sim"), "sem_dado": est.count("incompleto"),
         "fora": est.count("não"), "avisos": []}
    if r["total"] >= 5 and r["dentro"] == 0 and r["sem_dado"] == 0:
        r["avisos"].append("nenhum anúncio respeita o filtro — o portal pode não tê-lo aplicado; confira no site")
    return r
