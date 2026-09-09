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
import logging
import os
import traceback

from flask import Flask, render_template, render_template_string, request

from scrapers import buscar_todos_portais, TIPO_PARA_SLUG_CHAVESNAMAO

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ESTADOS_BR = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]

# A lista de tipos vem direto do mapeamento usado pelo scraper do Chaves na
# Mão (scrapers.py) — assim o formulário sempre mostra só tipos que
# realmente têm uma URL de busca específica naquele portal, sem duplicar a
# lista em dois lugares.
TIPOS_IMOVEL = list(TIPO_PARA_SLUG_CHAVESNAMAO.keys())

PAGINA_ERRO = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<title>Erro na busca</title>
<style>
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f5f6f8; margin:0; padding:40px 16px; }
    .box { max-width: 640px; margin: 0 auto; background: white; border-radius: 10px;
           box-shadow: 0 4px 18px rgba(0,0,0,0.08); padding: 28px 24px; }
    h1 { font-size: 1.3rem; color: #b23c17; margin-top:0; }
    pre { background: #f5f6f8; padding: 12px; border-radius: 6px; overflow-x: auto;
          font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; }
    a { color: #1a5fb4; }
</style>
</head>
<body>
<div class="box">
    <h1>⚠️ A busca não pôde ser concluída</h1>
    <p>
        Algo deu errado no servidor ao processar esta busca — provavelmente a
        busca demorou demais (o Render tem um limite de tempo por
        requisição) ou houve um erro inesperado no código.
    </p>
    <p><strong>Detalhe técnico do erro:</strong></p>
    <pre>{{ erro }}</pre>
    <p>
        Copie esse detalhe e envie para quem mantém o projeto — ele ajuda a
        identificar exatamente o que aconteceu, sem precisar acessar os logs
        do servidor.
    </p>
    <p><a href="/">&larr; Tentar uma nova busca</a></p>
</div>
</body>
</html>
"""


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

    try:
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
    except Exception as exc:
        # Protege contra qualquer erro não previsto (inclusive travamentos
        # do navegador automatizado, falta de memória, etc.), mostrando uma
        # mensagem útil em vez da tela genérica de erro do servidor.
        detalhe = f"{type(exc).__name__}: {exc}"
        logger.error("Erro inesperado na busca: %s\n%s", detalhe, traceback.format_exc())
        return render_template_string(PAGINA_ERRO, erro=detalhe), 500


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=False)
