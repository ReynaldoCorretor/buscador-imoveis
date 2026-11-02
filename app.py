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

import hmac
import json
import logging
import os
import re
import traceback

from flask import (Flask, abort, jsonify, redirect, render_template,
                   render_template_string, request, url_for)

import jobs
import regiao
import storage
from rotas_busca import bp as bp_busca
from rotas_pedidos import bp as bp_pedidos, token_coletor_ok
from scrapers import (buscar_todos_portais, diagnosticar_portais, _slug_cidade,
                      TIPO_PARA_SLUG_CHAVESNAMAO)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # /api/importar: no máximo 5 MB por envio
MAX_ANUNCIOS_POR_ENVIO = 1000
app.register_blueprint(bp_pedidos)
app.register_blueprint(bp_busca)   # página inicial "/" (caminho 2), com senha
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


PAGINA_AGUARDE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buscando…</title>
<style>
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f5f6f8; margin:0; padding:40px 16px; }
    .box { max-width: 560px; margin: 0 auto; background: white; border-radius: 10px;
           box-shadow: 0 4px 18px rgba(0,0,0,0.08); padding: 28px 24px; text-align:center; }
    h1 { font-size: 1.25rem; margin-top:0; }
    .barra { height: 8px; background:#e5e8ee; border-radius: 4px; overflow:hidden; margin: 18px 0; }
    .barra div { height:100%; width:35%; background:#1a5fb4; border-radius:4px; animation: vai 1.4s infinite ease-in-out; }
    @keyframes vai { 0%{margin-left:-35%} 100%{margin-left:100%} }
    .pequeno { font-size:0.85rem; color:#777; }
    #erro { display:none; color:#b23c17; }
</style>
</head>
<body>
<div class="box">
    <h1 id="titulo">🔎 {{ titulo }}</h1>
    <div class="barra" id="barra"><div></div></div>
    <p id="status">Iniciando…</p>
    <p class="pequeno">Esta página atualiza sozinha. Pode levar de 30 a 90 segundos; no primeiro
    acesso do dia o servidor pode demorar mais para acordar. Não precisa recarregar nem enviar de novo.</p>
    <p id="erro"></p>
    <p><a href="/">&larr; Voltar ao formulário</a></p>
</div>
<script>
const urlEstado = {{ url_estado | tojson }};
let falhasSeguidas = 0;
async function consultar() {
    try {
        const r = await fetch(urlEstado, { cache: 'no-store' });
        if (r.status === 404) { mostrarErro('Esta busca expirou ou o servidor foi reiniciado. Volte ao formulário e busque de novo.'); return; }
        const d = await r.json();
        falhasSeguidas = 0;
        if (d.estado === 'concluido') { window.location.reload(); return; }
        if (d.estado === 'erro') { mostrarErro(d.erro || 'A busca falhou.'); return; }
        document.getElementById('status').textContent =
            d.estado === 'fila' ? 'Na fila: posição ' + d.posicao + '…' : 'Buscando nos portais… ' + Math.round(d.decorrido) + 's';
    } catch (e) {
        falhasSeguidas++;
        document.getElementById('status').textContent = 'Reconectando ao servidor…';
        if (falhasSeguidas >= 15) { mostrarErro('Não foi possível falar com o servidor. Tente de novo em instantes.'); return; }
    }
    setTimeout(consultar, 2000);
}
function mostrarErro(msg) {
    document.getElementById('barra').style.display = 'none';
    document.getElementById('status').style.display = 'none';
    document.getElementById('titulo').textContent = '⚠️ A busca não pôde ser concluída';
    const e = document.getElementById('erro'); e.style.display = 'block'; e.textContent = msg;
}
consultar();
</script>
</body>
</html>
"""

PAGINA_DIAG = """
<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="UTF-8"><title>Diagnóstico dos portais</title>
<style>
 body { font-family:'Segoe UI',Arial,sans-serif; background:#f5f6f8; padding:30px 16px; }
 .box { max-width:760px; margin:0 auto; background:#fff; border-radius:10px; padding:24px; box-shadow:0 4px 18px rgba(0,0,0,.08); }
 .ok { color:#1b7a3d; font-weight:600 } .falha { color:#b23c17; font-weight:600 }
 pre { background:#f5f6f8; padding:10px; border-radius:6px; white-space:pre-wrap; word-break:break-word; font-size:.78rem }
</style></head><body><div class="box">
<h1>Diagnóstico de acesso aos portais</h1>
<p>Página 1 de cada portal, aberta a partir <strong>deste servidor</strong> (o IP do servidor é o que conta).</p>
{% for r in resultados %}
  <h3>{{ r.portal }} —
    {% if r.ok %}<span class="ok">✅ conteúdo real recebido</span> ({{ r.links }} links, {{ r.segundos }}s)
    {% else %}<span class="falha">❌ sem conteúdo</span> ({{ r.segundos }}s){% endif %}</h3>
  {% if not r.ok %}<p><strong>Causa:</strong> {{ r.causa }}</p>{% endif %}
  <pre>{{ r.detalhe }}</pre>
{% endfor %}
</div></body></html>
"""


PAGINA_DIAG_COLETOR = """
<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Teste do coletor no servidor</title>
<style>
 body { font-family:'Segoe UI',Arial,sans-serif; background:#f5f6f8; padding:24px 16px; }
 .box { max-width:820px; margin:0 auto; background:#fff; border-radius:10px; padding:22px; box-shadow:0 4px 18px rgba(0,0,0,.08); }
 .ok { color:#1b7a3d; font-weight:600 } .falha { color:#b23c17; font-weight:600 }
 table { border-collapse:collapse; width:100%; font-size:.9rem } td,th { text-align:left; padding:6px; border-bottom:1px solid #e5e8ee }
 pre { background:#f5f6f8; padding:10px; border-radius:6px; white-space:pre-wrap; word-break:break-word; font-size:.75rem }
</style></head><body><div class="box">
<h1>Teste do coletor novo, rodando NO SERVIDOR</h1>
<p>Cidade: <strong>{{ r.cidade or p.cidade }}</strong> · páginas por portal: {{ p.paginas }} · modo leve: {{ 'sim' if p.leve else 'não' }}
{% if r.segundos %} · tempo: {{ r.segundos }} s{% endif %}</p>
{% if not r.ok %}<p class="falha">❌ {{ r.erro }}</p>{% endif %}
{% if r.portais %}
<table><tr><th>Portal</th><th>Estado</th><th>Páginas</th><th>Anúncios</th><th>Motivo</th></tr>
{% for nome, i in r.portais.items() %}
<tr><td>{{ nome }}</td><td class="{{ 'ok' if i.estado == 'ok' else 'falha' }}">{{ i.estado }}</td><td>{{ i.paginas_lidas }}</td>
<td>{{ contagem.get(nome, 0) }}</td><td>{{ i.motivo }}</td></tr>{% endfor %}
</table>{% endif %}
{% if r.memoria %}<p><strong>Memória:</strong> pico da coleta {{ r.memoria.pico_coleta_mb or '?' }} MB ·
pico do servidor inteiro {{ r.memoria.pico_container_mb or '?' }} MB · limite {{ r.memoria.limite_container_mb or '?' }} MB</p>{% endif %}
{% if r.anuncios %}<p><strong>Exemplos lidos:</strong></p><table>
{% for a in r.anuncios[:6] %}<tr><td>{{ a.portal }}</td><td>R$ {{ a.preco }}</td><td>{{ a.quartos }} q</td><td>{{ a.area_m2 }} m²</td>
<td>{{ a.bairro }}</td><td>{{ a.dentro_do_filtro }}</td></tr>{% endfor %}</table>{% endif %}
{% if r.descartados %}<p>Descartados (aluguel/outra cidade): {{ r.descartados }}</p>{% endif %}
{% if r.log %}<details><summary>Registro técnico (últimas linhas)</summary><pre>{{ r.log | join('\n') }}</pre></details>{% endif %}
</div></body></html>
"""


def _executar_diag_coletor(**params):
    import coletor_servidor
    r = coletor_servidor.rodar(params, timeout_s=params.pop("timeout_s", 540))
    r.pop("anuncios_completos", None)
    return {"params": params, "r": r}


def validar_entrada(cidade: str, uf: str):
    """Devolve uma mensagem de erro em português, ou None se estiver tudo certo."""
    if not cidade:
        return "Informe a cidade."
    if len(cidade) < 2 or len(cidade) > 60:
        return "O nome da cidade deve ter entre 2 e 60 caracteres."
    if not re.fullmatch(r"[A-Za-zÀ-ÿ'’ .\-]+", cidade):
        return "O nome da cidade só pode ter letras, espaços, hífen e apóstrofo."
    if not _slug_cidade(cidade):
        return "Não foi possível interpretar o nome da cidade."
    if uf.upper() not in ESTADOS_BR:
        return "Escolha um estado (UF) válido."
    return None


def _executar_busca(**kwargs):
    return buscar_todos_portais(**kwargs)


def _executar_diagnostico():
    return diagnosticar_portais()


@app.route("/antigo", methods=["GET"])
def index():
    """Formulário antigo (só Chaves na Mão, sem senha). A página principal agora é "/" (rotas_busca)."""
    return render_template("index.html", estados=ESTADOS_BR, tipos=TIPOS_IMOVEL, erro=None)


@app.route("/saude", methods=["GET"])
def saude():
    """Resposta leve para monitoramento (não consulta nenhum portal)."""
    return jsonify({"status": "ok"})


@app.route("/buscar", methods=["POST"])
def buscar():
    cidade = request.form.get("cidade", "").strip()
    uf = request.form.get("uf", "").strip().upper()
    tipo = request.form.get("tipo", "").strip() or None
    if tipo and tipo not in TIPO_PARA_SLUG_CHAVESNAMAO:
        tipo = None

    def to_int(v):
        try:
            return int(v) if v and v.strip() else None
        except ValueError:
            return None

    def to_float(v):
        try:
            return float(v.replace(",", ".")) if v and v.strip() else None
        except ValueError:
            return None

    erro = validar_entrada(cidade, uf)
    if not erro and (uf != "SP" or regiao.slug_por_nome(cidade) is None):
        erro = ("Por ora, o buscador atende só Mogi das Cruzes e região: "
                + ", ".join(n for _, n in regiao.CIDADES_REGIAO) + " (SP).")
    if erro:
        return render_template("index.html", estados=ESTADOS_BR, tipos=TIPOS_IMOVEL, erro=erro), 400

    g = request.form.get
    try:
        job_id = jobs.gerenciador.enviar(
            _executar_busca,
            meta={"tipo_job": "busca", "cidade": cidade, "uf": uf},
            cidade=cidade, uf=uf, tipo=tipo,
            quartos_min=to_int(g("quartos_min", "")), quartos_max=to_int(g("quartos_max", "")),
            banheiros_min=to_int(g("banheiros_min", "")), banheiros_max=to_int(g("banheiros_max", "")),
            area_min=to_float(g("area_min", "")), area_max=to_float(g("area_max", "")),
            preco_min=to_float(g("preco_min", "")), preco_max=to_float(g("preco_max", "")),
        )
    except jobs.ServidorOcupado as exc:
        return render_template_string(PAGINA_ERRO, erro=str(exc)), 503
    return redirect(url_for("ver_busca", job_id=job_id), code=303)


@app.route("/busca/<job_id>", methods=["GET"])
def ver_busca(job_id):
    job = jobs.gerenciador.consultar(job_id)
    if job is None:
        return render_template_string(
            PAGINA_ERRO,
            erro="Esta busca expirou (os resultados ficam disponíveis por 30 minutos) "
                 "ou o servidor foi reiniciado. Volte ao formulário e busque de novo.",
        ), 404

    if job["estado"] == jobs.ERRO:
        return render_template_string(PAGINA_ERRO, erro=job["erro"] or "Erro desconhecido."), 500

    if job["estado"] == jobs.CONCLUIDO:
        resultado = job["resultado"]
        meta = job["meta"]
        try:
            if meta.get("tipo_job") == "diagnostico":
                return render_template_string(PAGINA_DIAG, resultados=resultado)
            if meta.get("tipo_job") == "diag_coletor":
                r = resultado["r"]
                contagem = {}
                for a in r.get("anuncios") or []:
                    contagem[a.get("portal")] = contagem.get(a.get("portal"), 0) + 1
                return render_template_string(PAGINA_DIAG_COLETOR, r=r, p=resultado["params"], contagem=contagem)
            return render_template(
                "resultados.html",
                cidade=meta.get("cidade", ""), uf=meta.get("uf", ""),
                resultado_json=json.dumps(resultado, ensure_ascii=False).replace("</", "<\\/"),
                total=resultado["total"], erros=resultado["erros"],
                diagnosticos_por_portal=resultado["diagnosticos_por_portal"],
            )
        except Exception as exc:
            logger.error("Erro ao montar resultados: %s\n%s", exc, traceback.format_exc())
            return render_template_string(PAGINA_ERRO, erro=f"{type(exc).__name__}: {exc}"), 500

    titulo = {"diagnostico": "Rodando diagnóstico dos portais…",
              "diag_coletor": "Testando o coletor no servidor (pode levar até 9 minutos)…"}.get(
        job["meta"].get("tipo_job"), "Buscando imóveis…")
    return render_template_string(
        PAGINA_AGUARDE, titulo=titulo, url_estado=url_for("estado_busca", job_id=job_id)
    )


@app.route("/busca/<job_id>/estado", methods=["GET"])
def estado_busca(job_id):
    job = jobs.gerenciador.consultar(job_id)
    if job is None:
        return jsonify({"estado": "expirada"}), 404
    return jsonify({
        "estado": job["estado"], "posicao": job["posicao"],
        "decorrido": job["decorrido"], "erro": job["erro"],
    })


@app.route("/api/importar", methods=["POST"])
def api_importar():
    """Recebe do coletor local o resultado de uma coleta e grava no armazenamento.

    Autenticação: cabeçalho `Authorization: Bearer <IMPORT_TOKEN>`. Sem IMPORT_TOKEN definido
    no servidor, a rota fica DESLIGADA (404), como /diagnostico-portais.
    Corpo (JSON): {"anuncios": [...], "coletado_em": "ISO-8601" (opcional), "pedido_id": N (opcional)}.
    `texto_bruto` é ignorado. Anúncios inválidos são recusados individualmente e listados na resposta;
    um envio com algum inválido NÃO impede a gravação dos válidos."""
    if not token_coletor_ok():   # 404 se IMPORT_TOKEN não existe no servidor
        return jsonify({"erro": "token inválido"}), 401

    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict) or not isinstance(corpo.get("anuncios"), list):
        return jsonify({"erro": "corpo deve ser JSON com a lista 'anuncios'"}), 400
    if len(corpo["anuncios"]) > MAX_ANUNCIOS_POR_ENVIO:
        return jsonify({"erro": f"máximo de {MAX_ANUNCIOS_POR_ENVIO} anúncios por envio"}), 413

    pedido_id = corpo.get("pedido_id")
    if pedido_id is not None and (not isinstance(pedido_id, int) or isinstance(pedido_id, bool)):
        return jsonify({"erro": "pedido_id deve ser inteiro"}), 400
    if pedido_id is not None:
        try:
            existe = storage.obter().obter_pedido(pedido_id) is not None
        except Exception as exc:
            return jsonify({"erro": "falha no armazenamento", "tipo": type(exc).__name__}), 502
        if not existe:
            return jsonify({"erro": f"pedido {pedido_id} não existe"}), 404
    coletado_em = corpo.get("coletado_em")
    if coletado_em is not None:
        try:
            from datetime import datetime
            datetime.fromisoformat(str(coletado_em).replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"erro": "coletado_em deve ser data/hora ISO-8601"}), 400
    else:
        coletado_em = storage.agora_iso()

    validos, recusados = [], []
    for i, bruto in enumerate(corpo["anuncios"]):
        try:
            validos.append(storage.normalizar_anuncio(bruto))
        except storage.DadosInvalidos as exc:
            recusados.append({"indice": i, "motivo": str(exc)})
    try:
        gravados = storage.obter().salvar_imoveis(validos, coletado_em, pedido_id)
    except Exception as exc:
        logger.error("Falha ao gravar importação: %s", type(exc).__name__)
        return jsonify({"erro": "falha ao gravar no armazenamento", "tipo": type(exc).__name__}), 502
    return jsonify({"gravados": gravados, "recusados": recusados[:50], "total_recusados": len(recusados)})


@app.route("/diagnostico-portais", methods=["GET"])
def diagnostico_portais():
    """Testa a página 1 de cada portal A PARTIR DO SERVIDOR. Fica DESLIGADO
    a menos que a variável de ambiente ADMIN_TOKEN esteja definida no Render;
    uso: /diagnostico-portais?token=SEU_TOKEN"""
    esperado = os.environ.get("ADMIN_TOKEN", "")
    enviado = request.args.get("token", "")
    if not esperado or not hmac.compare_digest(enviado.encode(), esperado.encode()):
        abort(404)
    try:
        job_id = jobs.gerenciador.enviar(_executar_diagnostico, meta={"tipo_job": "diagnostico"})
    except jobs.ServidorOcupado as exc:
        return render_template_string(PAGINA_ERRO, erro=str(exc)), 503
    return redirect(url_for("ver_busca", job_id=job_id), code=303)


@app.route("/diagnostico-coletor", methods=["GET"])
def diagnostico_coletor():
    """Roda o COLETOR NOVO (VivaReal + ZAP) a partir do servidor, para uma cidade, e mostra estado por
    portal, anúncios lidos e pico de memória. Desligado sem ADMIN_TOKEN. Uso:
    /diagnostico-coletor?token=SEU_TOKEN  (opcionais: &cidade=suzano &paginas=1 &leve=0)"""
    esperado = os.environ.get("ADMIN_TOKEN", "")
    enviado = request.args.get("token", "")
    if not esperado or not hmac.compare_digest(enviado.encode(), esperado.encode()):
        abort(404)
    cidade = request.args.get("cidade", "mogi-das-cruzes")   # Mogi já vem cadastrada no código
    if cidade not in regiao.NOMES:
        return render_template_string(PAGINA_ERRO, erro="cidade inválida (use o nome interno, ex.: suzano)"), 400
    try:
        paginas = max(1, min(3, int(request.args.get("paginas", "1"))))
    except ValueError:
        paginas = 1
    leve = request.args.get("leve", "1") != "0"
    filtros = {"tipo": "apartamento"} if request.args.get("tipo", "apartamento") == "apartamento" else {}
    try:
        job_id = jobs.gerenciador.enviar(_executar_diag_coletor, meta={"tipo_job": "diag_coletor", "limite_s": 600},
                                         cidade=cidade, paginas=paginas, leve=leve, filtros=filtros)
    except jobs.ServidorOcupado as exc:
        return render_template_string(PAGINA_ERRO, erro=str(exc)), 503
    return redirect(url_for("ver_busca", job_id=job_id), code=303)


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=False)
