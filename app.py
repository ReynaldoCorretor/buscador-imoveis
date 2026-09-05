# -*- coding: utf-8 -*-
"""
app.py

Buscador de imóveis: formulário único que consulta 4 portais (VivaReal,
ZAP Imóveis, Imovelweb e Chaves na Mão) e mostra todos os resultados numa
única página, com filtros/ordenação adicionais rodando em JavaScript no
navegador (sem nova requisição ao servidor).

A busca de verdade acontece no servidor (rota /buscar) porque os portais
bloqueiam requisições feitas diretamente do navegador (CORS) e não
oferecem uma API pública.
"""

import json
import os

from flask import Flask, render_template, request

from scrapers import buscar_todos_portais

app = Flask(__name__)

ESTADOS_BR = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]

TIPOS_IMOVEL = [
    "Apartamento", "Casa", "Sobrado", "Casa de condomínio", "Terreno/Lote",
    "Chácara", "Flat", "Imóvel comercial",
]


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", estados=ESTADOS_BR, tipos=TIPOS_IMOVEL)


@app.route("/buscar", methods=["POST"])
def buscar():
    cidade = request.form.get("cidade", "").strip()
    uf = request.form.get("uf", "").strip()
    tipo = request.form.get("tipo", "").strip() or None
    quartos_min = request.form.get("quartos_min", "").strip()
    banheiros_min = request.form.get("banheiros_min", "").strip()
    area_min = request.form.get("area_min", "").strip()
    preco_min = request.form.get("preco_min", "").strip()
    preco_max = request.form.get("preco_max", "").strip()

    def to_int(v):
        try:
            return int(v) if v else None
        except ValueError:
            return None

    def to_float(v):
        try:
            return float(v) if v else None
        except ValueError:
            return None

    resultado = buscar_todos_portais(
        cidade=cidade,
        uf=uf,
        tipo=tipo,
        quartos_min=to_int(quartos_min),
        banheiros_min=to_int(banheiros_min),
        area_min=to_float(area_min),
        preco_min=to_float(preco_min),
        preco_max=to_float(preco_max),
    )

    return render_template(
        "resultados.html",
        cidade=cidade,
        uf=uf,
        resultado_json=json.dumps(resultado, ensure_ascii=False),
        total=resultado["total"],
        erros=resultado["erros"],
        diagnosticos_por_portal=resultado["diagnosticos_por_portal"],
    )


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=False)
