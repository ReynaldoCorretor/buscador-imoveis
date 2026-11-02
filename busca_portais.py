# -*- coding: utf-8 -*-
"""
busca_portais.py — monta (e lê) endereços de BUSCA FILTRADA do VivaReal e do ZAP Imóveis.

O QUE ESTÁ VERIFICADO (aprendido de 12 endereços reais, colados em 19/10/2026):
  * Filtros aceitos pelos dois portais, como parâmetros da URL:
        tipos=apartamento_residencial | casa_residencial
        quartos=3%2C4        (lista separada por vírgula)
        precoMaximo=600000
        onde=<texto longo com cidade/bairro e COORDENADAS>
  * O caminho da URL também muda (tipo, N-quartos, bairro).
  * O nome (slug) do bairro difere entre os portais: Vila Mogilar = "vila-mogilar"
    no VivaReal e "vl-mogilar" no ZAP.

O QUE NÃO ESTÁ VERIFICADO (por isso o código recusa em vez de adivinhar):
  * Outros tipos (cobertura, terreno, kitnet...), preço mínimo, área, banheiros, vagas.
  * Se "quartos=3" sozinho (sem o 4) funciona — os endereços reais traziam "3,4".
  * Se os parâmetros bastam sem o trecho do caminho (ou o contrário).
  * Bairros além dos cadastrados: cada bairro novo precisa de 1 endereço do VivaReal
    e 1 do ZAP, filtrando só pelo bairro (ver --aprender-bairro).
  * Imovelweb: formato ainda desconhecido.

Por isso o extrator confere o RESULTADO (preço máximo, mínimo de quartos): se a
página não aplicou o filtro, o aviso aparece.

Uso:
    py busca_portais.py --tipo apartamento --quartos 3,4 --preco-max 600000 --bairro vila-mogilar
    py busca_portais.py --aprender-bairro "URL_DO_VIVAREAL" "URL_DO_ZAP"
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import quote_plus, unquote_plus

import regiao

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_BAIRROS = os.path.join(PASTA, "bairros.json")
ARQUIVO_CIDADES = os.path.join(PASTA, "cidades.json")

# ---------------------------------------------------------------------------
# Dados verificados
# ---------------------------------------------------------------------------

CIDADES = {
    "mogi-das-cruzes": {
        "nome": "Mogi das Cruzes",
        "uf": "sp",
        "vivareal": "mogi-das-cruzes",   # trecho do caminho no VivaReal (verificado)
        "zap": "mogi-das-cruzes",        # trecho do caminho no ZAP (verificado)
        "onde": ("%2CS%C3%A3o+Paulo%2CMogi+das+Cruzes%2C%2C%2C%2C%2Ccity%2CBR%3ESao+Paulo%3ENULL"
                 "%3EMogi+das+Cruzes%2C-23.539385%2C-46.216729%2C"),
    },
}

TIPOS = {
    "apartamento": {"param": "apartamento_residencial", "zap_caminho": "apartamentos"},
    "casa": {"param": "casa_residencial", "zap_caminho": "casas"},
}

BAIRROS_BASE = {
    ("mogi-das-cruzes", "vila-mogilar"): {
        "nome": "Vila Mogilar",
        "vivareal": "vila-mogilar",
        "zap": "vl-mogilar",
        "onde": ("%2CS%C3%A3o+Paulo%2CMogi+das+Cruzes%2C%2CVila+Mogilar%2C%2C%2Cneighborhood%2CBR%3ESao+Paulo"
                 "%3ENULL%3EMogi+das+Cruzes%3EBarrios%3EVila+Mogilar%2C-23.513085%2C-46.196002%2C"),
    },
}

DOMINIOS = {"vivareal": "https://www.vivareal.com.br", "zap": "https://www.zapimoveis.com.br"}


def carregar_bairros() -> dict:
    """Bairros verificados + os aprendidos em bairros.json. Chave: (cidade, slug)."""
    bairros = dict(BAIRROS_BASE)
    try:
        with open(ARQUIVO_BAIRROS, encoding="utf-8") as f:
            for item in json.load(f):
                bairros[(item["cidade"], item["slug"])] = {
                    k: item[k] for k in ("nome", "vivareal", "zap", "onde")}
    except (OSError, ValueError, KeyError):
        pass
    return bairros


def carregar_cidades() -> dict:
    """Cidades verificadas (Mogi) + as aprendidas em cidades.json. Chave: slug de regiao.py."""
    cidades = {k: dict(v) for k, v in CIDADES.items()}
    try:
        with open(ARQUIVO_CIDADES, encoding="utf-8") as f:
            for item in json.load(f):
                cidades[item["slug"]] = {k: item[k] for k in ("nome", "uf", "vivareal", "zap", "onde")}
    except (OSError, ValueError, KeyError):
        pass
    return cidades


def normalizar_onde(onde: str) -> str:
    """Deixa o parâmetro 'onde' sempre no mesmo formato codificado dos endereços reais
    (ex.: '%2CS%C3%A3o+Paulo%2C...'), não importa se o navegador copiou com ou sem acentos."""
    return quote_plus(unquote_plus(onde or ""), safe="")


def partes_onde(onde: str) -> list:
    """'onde' decodificado e separado por vírgulas. Formato visto nos endereços reais:
    ['', 'São Paulo', 'Mogi das Cruzes', '', 'Vila Mogilar', '', '', 'neighborhood', 'BR>...', lat, lon, '']"""
    return unquote_plus(onde or "").split(",")


# ---------------------------------------------------------------------------
# Montar
# ---------------------------------------------------------------------------

def _lista_quartos(q):
    if q is None or q == "":
        return None
    if isinstance(q, int):
        return [q]
    if isinstance(q, str):
        q = [x for x in re.split(r"[,\s]+", q.strip()) if x]
    lista = [int(x) for x in q]
    if not lista or any(n < 1 or n > 9 for n in lista):
        raise ValueError("quartos deve ter números de 1 a 9")
    return sorted(set(lista))


def montar_url(portal: str, *, tipo=None, quartos=None, preco_max=None, bairro=None,
               cidade: str = "mogi-das-cruzes") -> str:
    """Monta a URL de busca filtrada. Levanta ValueError (com o motivo) se pedir
    algo que ainda não foi verificado — nunca devolve uma URL "no chute"."""
    if portal not in DOMINIOS:
        raise ValueError(f"portal desconhecido: {portal!r} (use 'vivareal' ou 'zap'; o Imovelweb ainda não é suportado)")
    cidades = carregar_cidades()
    if cidade not in cidades:
        nome = regiao.NOMES.get(cidade, cidade)
        raise ValueError(f"cidade {nome!r} ainda não cadastrada neste computador. Cadastre com: "
                         f"py aprender_cidades.py")
    c = cidades[cidade]
    if tipo is not None and tipo not in TIPOS:
        raise ValueError(f"tipo {tipo!r} ainda não verificado (só: {', '.join(TIPOS)})")
    lista_q = _lista_quartos(quartos)
    if preco_max is not None and (not isinstance(preco_max, int) or preco_max <= 0):
        raise ValueError("preco_max deve ser um número inteiro positivo")

    info_b = None
    if bairro:
        info_b = carregar_bairros().get((cidade, bairro))
        if info_b is None:
            raise ValueError(f"bairro {bairro!r} ainda não cadastrado. Cadastre com: "
                             f"py busca_portais.py --aprender-bairro URL_VIVAREAL URL_ZAP")

    onde = info_b["onde"] if info_b else c["onde"]
    consulta = [f"onde={onde}"]
    if tipo:
        consulta.append(f"tipos={TIPOS[tipo]['param']}")
    if lista_q:
        consulta.append("quartos=" + "%2C".join(str(n) for n in lista_q))
    if preco_max:
        consulta.append(f"precoMaximo={preco_max}")

    if portal == "vivareal":
        caminho = f"/venda/{c['uf']}/{c['vivareal']}/"
        if info_b:
            caminho += f"bairros/{info_b['vivareal']}/"
        if tipo:
            caminho += f"{TIPOS[tipo]['param']}/"
        if lista_q:
            caminho += f"{lista_q[0]}-quartos/"
    else:  # zap
        tipo_caminho = TIPOS[tipo]["zap_caminho"] if tipo else "imoveis"
        local = f"{c['uf']}+{c['zap']}" + (f"++{info_b['zap']}" if info_b else "")
        caminho = f"/venda/{tipo_caminho}/{local}/"
        if lista_q:
            caminho += f"{lista_q[0]}-quartos/"
    return f"{DOMINIOS[portal]}{caminho}?" + "&".join(consulta)


# ---------------------------------------------------------------------------
# Ler (inverso) — usado nos testes e para aprender bairros
# ---------------------------------------------------------------------------

def ler_url(url: str) -> dict:
    """Extrai {portal, tipo, quartos, preco_max, bairro_slug_portal, onde} de uma URL real."""
    m = re.match(r"https?://(?:www\.)?(vivareal|zapimoveis)\.com\.br(/[^?#]*)(?:\?([^#]*))?", url.strip())
    if not m:
        raise ValueError("endereço não é do VivaReal nem do ZAP Imóveis")
    portal = "vivareal" if m.group(1) == "vivareal" else "zap"
    caminho, consulta = m.group(2), m.group(3) or ""
    params = {}
    for par in consulta.split("&"):
        if "=" in par:
            k, v = par.split("=", 1)
            params[k] = v
    out = {"portal": portal, "tipo": None, "quartos": None, "preco_max": None,
           "bairro_slug": None, "onde": params.get("onde")}
    for nome, d in TIPOS.items():
        if params.get("tipos") == d["param"]:
            out["tipo"] = nome
    if "quartos" in params:
        out["quartos"] = _lista_quartos(params["quartos"].replace("%2C", ","))
    if "precoMaximo" in params:
        out["preco_max"] = int(params["precoMaximo"])
    if portal == "vivareal":
        mb = re.search(r"/bairros/([^/]+)/", caminho)
    else:
        mb = re.search(r"\+\+([^/]+)/", caminho)
    out["bairro_slug"] = mb.group(1) if mb else None
    return out


def _slug_cidade_no_caminho(url: str):
    """Trecho da cidade no caminho: VivaReal '/venda/sp/<cidade>/...'; ZAP '/venda/<tipo>/sp+<cidade>/...'."""
    m = re.match(r"https?://(?:www\.)?(vivareal|zapimoveis)\.com\.br(/[^?#]*)", url.strip())
    if not m:
        return None, None
    if m.group(1) == "vivareal":
        mc = re.match(r"/venda/([a-z]{2})/([a-z0-9-]+)/", m.group(2))
    else:
        mc = re.match(r"/venda/[a-z-]+/([a-z]{2})\+([a-z0-9-]+)(?:/|\+)", m.group(2))
    return (mc.group(1), mc.group(2)) if mc else (None, None)


def aprender_cidade(url_vivareal: str, url_zap: str, slug: str) -> dict:
    """Cadastra uma das 10 cidades a partir de 1 endereço do VivaReal e 1 do ZAP, ambos de busca
    SÓ pela cidade (sem bairro e sem outros filtros). Confere tudo antes de gravar em cidades.json.
    Levanta ValueError com o motivo, em português."""
    if slug not in regiao.NOMES:
        raise ValueError(f"cidade {slug!r} não é uma das 10 da região")
    nome_esperado = regiao.NOMES[slug]
    v, z = ler_url(url_vivareal), ler_url(url_zap)
    if v["portal"] != "vivareal":
        raise ValueError("o primeiro endereço precisa ser do VivaReal (vivareal.com.br)")
    if z["portal"] != "zap":
        raise ValueError("o segundo endereço precisa ser do ZAP (zapimoveis.com.br)")
    for nome_portal, d in (("VivaReal", v), ("ZAP", z)):
        if d["bairro_slug"]:
            raise ValueError(f"o endereço do {nome_portal} é de um BAIRRO; escolha a sugestão da CIDADE inteira")
        if any(d[k] for k in ("tipo", "quartos", "preco_max")):
            raise ValueError(f"o endereço do {nome_portal} tem filtros (tipo/quartos/preço); "
                             "busque só pela cidade, sem filtros")
        if not d["onde"]:
            raise ValueError(f"o endereço do {nome_portal} não tem o trecho 'onde=' — copie o endereço "
                             "DEPOIS de escolher a cidade na lista de sugestões")
    onde_v, onde_z = normalizar_onde(v["onde"]), normalizar_onde(z["onde"])
    pv = partes_onde(onde_v)
    if len(pv) < 9 or pv[7] != "city":
        raise ValueError("o endereço do VivaReal não é de busca por cidade (o 'onde' não diz 'city')")
    if regiao.norm(pv[2]) != regiao.norm(nome_esperado):
        raise ValueError(f"o endereço do VivaReal é de {pv[2]!r}, não de {nome_esperado!r}")
    pz = partes_onde(onde_z)
    if len(pz) < 9 or regiao.norm(pz[2]) != regiao.norm(nome_esperado):
        raise ValueError(f"o endereço do ZAP é de {pz[2] if len(pz) > 2 else '?'!r}, não de {nome_esperado!r}")
    if onde_v != onde_z:
        # [NÃO VERIFICADO para cidades além de Mogi] nos endereços reais de Mogi os dois eram iguais
        raise ValueError("os trechos 'onde' do VivaReal e do ZAP não coincidem — confira se é a mesma cidade")
    uf_v, slug_v = _slug_cidade_no_caminho(url_vivareal)
    uf_z, slug_z = _slug_cidade_no_caminho(url_zap)
    if not slug_v:
        raise ValueError("não reconheci a cidade no caminho do endereço do VivaReal (/venda/sp/<cidade>/)")
    if not slug_z:
        raise ValueError("não reconheci a cidade no caminho do endereço do ZAP (/venda/imoveis/sp+<cidade>/)")
    if uf_v != regiao.UF or uf_z != regiao.UF:
        raise ValueError("o estado no endereço não é SP")
    novo = {"slug": slug, "nome": pv[2], "uf": regiao.UF, "vivareal": slug_v, "zap": slug_z, "onde": onde_v}
    existentes = []
    try:
        with open(ARQUIVO_CIDADES, encoding="utf-8") as f:
            existentes = json.load(f)
    except (OSError, ValueError):
        pass
    existentes = [e for e in existentes if e.get("slug") != slug] + [novo]
    tmp = ARQUIVO_CIDADES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQUIVO_CIDADES)
    return novo


def aprender_bairro(url_vivareal: str, url_zap: str, cidade: str = "mogi-das-cruzes") -> dict:
    """Cadastra um bairro novo a partir de 1 endereço do VivaReal e 1 do ZAP, ambos
    filtrando SÓ pelo bairro. Confere se os dois apontam para o mesmo lugar."""
    v, z = ler_url(url_vivareal), ler_url(url_zap)
    if v["portal"] != "vivareal" or z["portal"] != "zap":
        raise ValueError("passe primeiro o endereço do VivaReal e depois o do ZAP")
    if not v["bairro_slug"] or not z["bairro_slug"]:
        raise ValueError("os dois endereços precisam ser de busca por BAIRRO (selecione o bairro no site)")
    if not v["onde"] or normalizar_onde(v["onde"]) != normalizar_onde(z["onde"]):
        raise ValueError("os parâmetros 'onde' dos dois endereços não coincidem — confira se é o mesmo bairro")
    if any((v[k] or z[k]) for k in ("tipo", "quartos", "preco_max")):
        raise ValueError("use endereços SEM outros filtros (só o bairro), para não misturar")
    m = re.search(r"%2C%2C([^%]+(?:\+[^%]+)*)%2C%2C%2Cneighborhood", v["onde"])
    nome = m.group(1).replace("+", " ") if m else v["bairro_slug"]
    novo = {"cidade": cidade, "slug": v["bairro_slug"], "nome": nome,
            "vivareal": v["bairro_slug"], "zap": z["bairro_slug"], "onde": normalizar_onde(v["onde"])}
    existentes = []
    try:
        with open(ARQUIVO_BAIRROS, encoding="utf-8") as f:
            existentes = json.load(f)
    except (OSError, ValueError):
        pass
    existentes = [e for e in existentes if (e["cidade"], e["slug"]) != (cidade, novo["slug"])] + [novo]
    with open(ARQUIVO_BAIRROS, "w", encoding="utf-8") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)
    return novo


# ---------------------------------------------------------------------------
# Conferência do resultado (o filtro foi mesmo aplicado?)
# ---------------------------------------------------------------------------

def respeita_filtro(anuncio: dict, *, quartos=None, preco_max=None) -> str:
    """'sim' / 'não' / '' (sem dado para julgar) — o anúncio respeita preço máximo e
    mínimo de quartos? Só julga o que for objetivo; dado ausente não vira 'não'."""
    lista_q = _lista_quartos(quartos)
    veredito = None
    if preco_max and anuncio.get("preco") not in (None, ""):
        if float(anuncio["preco"]) > preco_max:
            return "não"
        veredito = "sim"
    if lista_q and anuncio.get("quartos") not in (None, ""):
        if int(anuncio["quartos"]) < lista_q[0]:
            return "não"
        veredito = "sim"
    return veredito or ""


def conformidade(anuncios: list, *, quartos=None, preco_max=None) -> dict:
    """Quantos anúncios lidos respeitam o filtro pedido.

    IMPORTANTE (aprendido com dados reais, 25/10/2026): o portal aplica o filtro, mas
    COMPLETA a lista com imóveis PARECIDOS que não o respeitam (mesmo bairro/vizinhos,
    preço um pouco acima, 2 quartos...). Por isso uma parte "fora" é NORMAL. O aviso só
    aparece quando quase nada respeita o filtro, o que indica filtro não aplicado."""
    julgados = [respeita_filtro(a, quartos=quartos, preco_max=preco_max) for a in anuncios]
    dentro, fora = julgados.count("sim"), julgados.count("não")
    res = {"total": len(anuncios), "dentro": dentro, "fora": fora, "sem_dado": julgados.count(""),
           "avisos": []}
    avaliados = dentro + fora
    if avaliados and dentro == 0:
        res["avisos"].append("nenhum anúncio respeita o filtro — o portal provavelmente NÃO o aplicou")
    elif avaliados >= 10 and dentro / avaliados < 0.3:
        res["avisos"].append(f"só {dentro} de {avaliados} anúncios respeitam o filtro — "
                             "o portal pode não tê-lo aplicado (ou há poucos resultados exatos)")
    return res


def main():
    ap = argparse.ArgumentParser(description="Monta URLs de busca filtrada (VivaReal e ZAP).")
    ap.add_argument("--tipo", choices=list(TIPOS))
    ap.add_argument("--quartos", help="ex.: 3,4")
    ap.add_argument("--preco-max", type=int)
    ap.add_argument("--bairro", help="slug cadastrado, ex.: vila-mogilar")
    ap.add_argument("--cidade", default="mogi-das-cruzes", choices=[c for c, _ in regiao.CIDADES_REGIAO])
    ap.add_argument("--aprender-bairro", nargs=2, metavar=("URL_VIVAREAL", "URL_ZAP"))
    a = ap.parse_args()
    try:
        if a.aprender_bairro:
            novo = aprender_bairro(*a.aprender_bairro)
            print(f"Bairro cadastrado: {novo['nome']}  (VivaReal: {novo['vivareal']} | ZAP: {novo['zap']})")
            print(f"Salvo em {ARQUIVO_BAIRROS}")
            return
        for portal in ("vivareal", "zap"):
            print(f"{portal}:\n  {montar_url(portal, tipo=a.tipo, quartos=a.quartos, preco_max=a.preco_max, bairro=a.bairro, cidade=a.cidade)}\n")
    except ValueError as exc:
        print(f"ERRO: {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
