# -*- coding: utf-8 -*-
"""
rotas_pedidos.py — pedidos de coleta (o site pede, o coletor no PC atende) e resultados.

Páginas (para você, no navegador/celular) — protegidas por senha (variável SITE_SENHA):
    GET  /pedidos                    formulário + últimos pedidos
    POST /pedidos                    cria um pedido
    GET  /pedidos/<id>               andamento de um pedido (atualiza sozinha)
    GET  /pedidos/<id>/resultados    imóveis do pedido: exatos, incompletos, parecidos

API (para o coletor no PC) — token no cabeçalho "Authorization: Bearer <IMPORT_TOKEN>":
    POST /api/pedidos/proximo           reserva o pedido pendente mais antigo (ou devolve null)
    POST /api/pedidos/<id>/finalizar    {"estado": "concluido"|"erro", "resumo": {...}, "erro": "..."}

Sem SITE_SENHA, as páginas ficam DESLIGADAS (404); sem IMPORT_TOKEN, a API fica desligada.

Região: só as 10 cidades de regiao.py. O servidor confere o FORMATO dos filtros; se o coletor não
souber montar a busca (ex.: cidade ainda não cadastrada no PC), o pedido vira "erro" com o motivo.
"""
import hmac
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, jsonify, redirect, render_template_string, request, url_for

import regiao
import storage

bp = Blueprint("pedidos", __name__)

TIPOS = ("apartamento", "casa")          # os únicos verificados no busca_portais.py
MAX_PAGINAS = 5
MAX_PENDENTES = 3                        # pedidos esperando o coletor ao mesmo tempo
MAX_POR_HORA = 6                         # para não sobrecarregar os portais
MINUTOS_PARA_EXPIRAR = 30
FUSO_BR = timezone(timedelta(hours=-3))  # Brasília (sem horário de verão desde 2019)


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------

def token_coletor_ok() -> bool:
    """True se o cabeçalho traz o IMPORT_TOKEN. Aborta com 404 se o token não existe no servidor."""
    esperado = os.environ.get("IMPORT_TOKEN", "")
    if not esperado:
        abort(404)
    cab = request.headers.get("Authorization", "")
    enviado = cab[7:] if cab.startswith("Bearer ") else ""
    return hmac.compare_digest(enviado.encode(), esperado.encode())


@bp.before_request
def _proteger_paginas():
    if request.path.startswith("/api/"):
        return None   # a API usa o token do coletor (conferido em cada rota)
    senha = os.environ.get("SITE_SENHA", "")
    if not senha:
        abort(404)
    auth = request.authorization
    if not auth or auth.type != "basic" or not hmac.compare_digest(
            (auth.password or "").encode(), senha.encode()):
        return Response("Senha necessária.", 401,
                        {"WWW-Authenticate": 'Basic realm="Buscador - pedidos", charset="UTF-8"'})
    return None


# ---------------------------------------------------------------------------
# Validação dos filtros
# ---------------------------------------------------------------------------

def _inteiro(txt, nome, minimo, maximo):
    t = (txt or "").strip().replace(".", "")
    if not t:
        return None, None
    if not t.isdigit() or not (minimo <= int(t) <= maximo):
        return None, f"{nome}: só números, entre {minimo:,} e {maximo:,}.".replace(",", ".")
    return int(t), None


def validar_filtros(form) -> tuple:
    """Devolve (filtros, None) ou (None, mensagem de erro em português)."""
    g = lambda k: (form.get(k) or "").strip()
    filtros = {}
    cidade = g("cidade")
    if cidade not in regiao.NOMES:
        return None, "Escolha a cidade (uma das 10 da região)."
    filtros["cidade"] = cidade
    tipo = g("tipo")
    if tipo:
        if tipo not in TIPOS:
            return None, f"Tipo deve ser {' ou '.join(TIPOS)}."
        filtros["tipo"] = tipo
    quartos = g("quartos").replace(" ", "")
    if quartos:
        if not re.fullmatch(r"[1-9](,[1-9])*", quartos):
            return None, "Quartos: use números de 1 a 9 separados por vírgula (ex.: 3,4)."
        filtros["quartos"] = ",".join(sorted(set(quartos.split(",")), key=int))
    for chave, nome, mn, mx in (("preco_min", "Preço mínimo", 10_000, 100_000_000),
                                ("preco_max", "Preço máximo", 10_000, 100_000_000),
                                ("area_min", "Área mínima", 10, 100_000),
                                ("area_max", "Área máxima", 10, 100_000),
                                ("banheiros_min", "Banheiros (mínimo)", 1, 9),
                                ("vagas_min", "Vagas (mínimo)", 1, 9)):
        v, erro = _inteiro(g(chave), nome, mn, mx)
        if erro:
            return None, erro
        if v is not None:
            filtros[chave] = v
    for a, b, nome in (("preco_min", "preco_max", "Preço"), ("area_min", "area_max", "Área")):
        if a in filtros and b in filtros and filtros[a] > filtros[b]:
            return None, f"{nome}: o mínimo é maior que o máximo."
    bairro = g("bairro").lower()
    if bairro:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", bairro) or len(bairro) > 60:
            return None, "Bairro: use o nome cadastrado, em minúsculas e com hífens (ex.: vila-mogilar)."
        filtros["bairro"] = bairro
    paginas = g("paginas") or "2"
    if not paginas.isdigit() or not (1 <= int(paginas) <= MAX_PAGINAS):
        return None, f"Páginas por portal: de 1 a {MAX_PAGINAS}."
    filtros["paginas"] = int(paginas)
    return filtros, None


def _data(txt):
    if not txt:
        return None
    try:
        d = datetime.fromisoformat(str(txt).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _hora_br(txt) -> str:
    d = _data(txt)
    return d.astimezone(FUSO_BR).strftime("%d/%m %H:%M") if d else "—"


def _reais(v) -> str:
    try:
        return "R$ " + f"{int(round(float(v))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def _limite_excedido(pedidos) -> str:
    pendentes = sum(1 for p in pedidos if p["estado"] == "pendente")
    if pendentes >= MAX_PENDENTES:
        return (f"Já há {pendentes} pedidos esperando o coletor. Aguarde terminarem "
                "(o coletor no PC está ligado?).")
    uma_hora = datetime.now(timezone.utc) - timedelta(hours=1)
    recentes = sum(1 for p in pedidos if (_data(p.get("criado_em")) or uma_hora) > uma_hora)
    if recentes >= MAX_POR_HORA:
        return f"Limite de {MAX_POR_HORA} pedidos por hora atingido (para não sobrecarregar os portais)."
    return ""


def resumo_filtros(f) -> str:
    f = f or {}
    partes = [regiao.NOMES.get(f.get("cidade"), f.get("cidade") or "cidade?")]
    if f.get("tipo"):
        partes.append(f["tipo"])
    if f.get("quartos"):
        partes.append(f"{f['quartos']} quartos")
    if f.get("preco_min") and f.get("preco_max"):
        partes.append(f"{_reais(f['preco_min'])} a {_reais(f['preco_max'])}")
    elif f.get("preco_max"):
        partes.append(f"até {_reais(f['preco_max'])}")
    elif f.get("preco_min"):
        partes.append(f"a partir de {_reais(f['preco_min'])}")
    if f.get("area_min") or f.get("area_max"):
        partes.append(f"{f.get('area_min') or '…'}–{f.get('area_max') or '…'} m²")
    if f.get("banheiros_min"):
        partes.append(f"{f['banheiros_min']}+ banh.")
    if f.get("vagas_min"):
        partes.append(f"{f['vagas_min']}+ vagas")
    if f.get("bairro"):
        partes.append(f["bairro"])
    partes.append(f"{f.get('paginas', 2)} pág.")
    return ", ".join(partes)


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------

_ESTILO = """
<style>
 :root { --azul:#1a5fb4; --fundo:#f5f6f8; --texto:#222; --cinza:#666; --caixa:#fff; --borda:#dde1e7;
         --verde:#1b7a3d; --amarelo:#8a6d00; --vermelho:#b23c17; }
 body { font-family:'Segoe UI',Arial,sans-serif; background:var(--fundo); color:var(--texto); margin:0; padding:24px 16px; }
 .box { max-width:760px; margin:0 auto 18px; background:var(--caixa); border-radius:10px; padding:20px 22px;
        box-shadow:0 4px 18px rgba(0,0,0,.07); }
 h1 { font-size:1.3rem; margin:0 0 12px; } h2 { font-size:1.05rem; margin:0 0 10px; }
 label { display:block; font-size:.85rem; color:var(--cinza); margin:10px 0 4px; }
 input, select { width:100%; box-sizing:border-box; padding:9px 10px; border:1px solid var(--borda); border-radius:6px; font-size:1rem; }
 .linha { display:flex; gap:12px; flex-wrap:wrap; } .linha > div { flex:1 1 140px; }
 button { margin-top:16px; background:var(--azul); color:#fff; border:0; border-radius:6px; padding:11px 18px; font-size:1rem; cursor:pointer; }
 .erro { background:#fdecea; color:var(--vermelho); padding:10px 12px; border-radius:6px; }
 .dica { font-size:.8rem; color:var(--cinza); }
 table { width:100%; border-collapse:collapse; font-size:.9rem; } td, th { text-align:left; padding:7px 6px; border-bottom:1px solid var(--borda); }
 .estado { font-weight:600; } .pendente{color:var(--amarelo)} .em_andamento{color:var(--azul)} .concluido{color:var(--verde)} .erro_e{color:var(--vermelho)}
 pre { background:var(--fundo); padding:10px; border-radius:6px; white-space:pre-wrap; word-break:break-word; font-size:.82rem; }
 a { color:var(--azul); }
 .card { border:1px solid var(--borda); border-radius:8px; padding:12px 14px; margin:10px 0; }
 .card .preco { font-size:1.15rem; font-weight:700; } .card .tit { margin:4px 0; }
 .card .info { font-size:.9rem; color:var(--texto); } .card .peq { font-size:.8rem; color:var(--cinza); }
 .motivo { font-size:.85rem; color:var(--vermelho); } .falta { font-size:.85rem; color:var(--amarelo); }
 .selo { display:inline-block; font-size:.75rem; padding:2px 8px; border-radius:10px; background:var(--fundo); margin-right:4px; }
 details > summary { cursor:pointer; font-weight:600; margin:8px 0; }
</style>
"""

_ROTULO = {"pendente": "⏳ esperando o coletor", "em_andamento": "🔄 coletando",
           "concluido": "✅ concluído", "erro": "❌ erro"}

PAGINA_LISTA = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pedidos de coleta</title>""" + _ESTILO + """
</head><body>
<div class="box">
  <h1>Nova busca</h1>
  <p class="dica">O coletor no seu PC pega o pedido em até 2 minutos (se estiver ligado), lê o VivaReal
  e o ZAP e envia os imóveis para cá. Cada imóvel é conferido contra todos os critérios abaixo.</p>
  {% if erro %}<p class="erro">{{ erro }}</p>{% endif %}
  <form method="post" action="{{ url_for('pedidos.criar') }}">
    <div class="linha">
      <div><label for="cidade">Cidade</label>
        <select id="cidade" name="cidade" required><option value="">Escolha…</option>
        {% for slug, nome in cidades %}<option value="{{ slug }}" {% if form.cidade == slug %}selected{% endif %}>{{ nome }}</option>{% endfor %}
        </select></div>
      <div><label for="tipo">Tipo</label>
        <select id="tipo" name="tipo"><option value="">Qualquer</option>
        {% for t in tipos %}<option value="{{ t }}" {% if form.tipo == t %}selected{% endif %}>{{ t }}</option>{% endfor %}
        </select></div>
      <div><label for="quartos">Quartos</label>
        <input id="quartos" name="quartos" placeholder="ex.: 2,3" value="{{ form.quartos or '' }}"></div>
    </div>
    <div class="linha">
      <div><label for="preco_min">Preço mínimo (R$)</label>
        <input id="preco_min" name="preco_min" inputmode="numeric" placeholder="opcional" value="{{ form.preco_min or '' }}"></div>
      <div><label for="preco_max">Preço máximo (R$)</label>
        <input id="preco_max" name="preco_max" inputmode="numeric" placeholder="ex.: 400000" value="{{ form.preco_max or '' }}"></div>
    </div>
    <div class="linha">
      <div><label for="area_min">Área mínima (m²)</label>
        <input id="area_min" name="area_min" inputmode="numeric" value="{{ form.area_min or '' }}"></div>
      <div><label for="area_max">Área máxima (m²)</label>
        <input id="area_max" name="area_max" inputmode="numeric" value="{{ form.area_max or '' }}"></div>
      <div><label for="banheiros_min">Banheiros (mín.)</label>
        <input id="banheiros_min" name="banheiros_min" inputmode="numeric" value="{{ form.banheiros_min or '' }}"></div>
      <div><label for="vagas_min">Vagas (mín.)</label>
        <input id="vagas_min" name="vagas_min" inputmode="numeric" value="{{ form.vagas_min or '' }}"></div>
    </div>
    <div class="linha">
      <div><label for="bairro">Bairro (opcional, só cadastrados)</label>
        <input id="bairro" name="bairro" placeholder="ex.: vila-mogilar" value="{{ form.bairro or '' }}"></div>
      <div><label for="paginas">Páginas por portal</label>
        <input id="paginas" name="paginas" inputmode="numeric" value="{{ form.paginas or 2 }}"></div>
    </div>
    <p class="dica">O portal filtra por cidade, tipo, quartos, preço máximo e bairro. Os demais critérios
    (preço mínimo, área, banheiros, vagas) são conferidos aqui, anúncio a anúncio. Bairro: hoje só
    vila-mogilar está cadastrado.</p>
    <button type="submit">Buscar</button>
  </form>
</div>
<div class="box">
  <h2>Últimas buscas</h2>
  {% if pedidos %}
  <table><tr><th>#</th><th>Criado</th><th>Busca</th><th>Estado</th></tr>
  {% for p in pedidos %}
    <tr><td><a href="{{ url_for('pedidos.ver', pid=p.id) }}">{{ p.id }}</a></td><td>{{ hora(p.criado_em) }}</td>
        <td>{{ resumo_filtros(p.filtros) }}</td>
        <td class="estado {{ 'erro_e' if p.estado == 'erro' else p.estado }}">
          {% if p.estado == 'concluido' %}<a href="{{ url_for('pedidos.resultados', pid=p.id) }}">{{ rotulo[p.estado] }}</a>
          {% else %}{{ rotulo[p.estado] }}{% endif %}</td></tr>
  {% endfor %}</table>
  {% else %}<p class="dica">Nenhuma busca ainda.</p>{% endif %}
</div>
</body></html>"""

PAGINA_PEDIDO = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if p.estado in ('pendente', 'em_andamento') %}<meta http-equiv="refresh" content="20">{% endif %}
<title>Pedido {{ p.id }}</title>""" + _ESTILO + """
</head><body><div class="box">
  <h1>Busca #{{ p.id }} — <span class="{{ 'erro_e' if p.estado == 'erro' else p.estado }}">{{ rotulo[p.estado] }}</span></h1>
  <p><strong>{{ resumo_filtros(p.filtros) }}</strong></p>
  <table>
    <tr><td>Criado</td><td>{{ hora(p.criado_em) }}</td></tr>
    <tr><td>Coletor começou</td><td>{{ hora(p.iniciado_em) }}</td></tr>
    <tr><td>Terminou</td><td>{{ hora(p.concluido_em) }}</td></tr>
  </table>
  {% if p.estado == 'pendente' %}<p class="dica">Esperando o coletor do PC. Esta página atualiza a cada 20 s.
     Se ficar parada mais de 5 minutos, confira se o coletor está aberto no PC.</p>{% endif %}
  {% if p.estado == 'em_andamento' %}<p class="dica">O coletor está lendo os portais (costuma levar alguns minutos).
     Esta página atualiza a cada 20 s.</p>{% endif %}
  {% if p.erro %}<p class="erro">{{ p.erro }}</p>{% endif %}
  {% if p.estado == 'concluido' and p.resumo %}
    {% set r = p.resumo %}
    <h2>Resultado</h2>
    <p><a href="{{ url_for('pedidos.resultados', pid=p.id) }}"><strong>Ver os {{ r.total }} imóveis</strong></a> —
       {{ r.exatos }} exatos · {{ r.incompletos }} com dado faltando · {{ r.parecidos }} parecidos
       {% if r.descartados %}· {{ r.descartados }} descartados (aluguel/outra cidade){% endif %}</p>
    {% if r.portais %}<table>{% for nome, info in r.portais.items() %}
      <tr><td>{{ nome }}</td><td>{{ info.anuncios }} anúncios</td><td>{{ info.estado }}</td>
          <td class="peq">{{ info.motivo }}</td></tr>{% endfor %}</table>{% endif %}
    {% for a in r.avisos or [] %}<p class="falta">⚠️ {{ a }}</p>{% endfor %}
  {% endif %}
  <p><a href="{{ url_for('pedidos.lista') }}">&larr; Buscas</a></p>
</div></body></html>"""

PAGINA_RESULTADOS = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Resultados {{ p.id }}</title>""" + _ESTILO + """
</head><body><div class="box">
  <h1>{{ resumo_filtros(p.filtros) }}</h1>
  <p class="dica">Busca #{{ p.id }}, coletada em {{ hora(p.concluido_em) }}. Fontes: VivaReal e ZAP (mesmo grupo;
  o mesmo imóvel nos dois aparece num card só). É uma amostra do que os portais mostraram, não o estoque completo.</p>
  {% macro card(g) %}
    <div class="card">
      <div class="preco">{{ reais(g.preco) }}</div>
      <div class="tit">{{ g.titulo or 'Sem título' }}</div>
      <div class="info">{% if g.area_m2 %}{{ '%g' % g.area_m2 }} m² · {% endif %}{% if g.quartos is not none %}{{ g.quartos }} quartos · {% endif %}{% if g.banheiros is not none %}{{ g.banheiros }} banh. · {% endif %}{% if g.vagas is not none %}{{ g.vagas }} vagas{% endif %}</div>
      <div class="info">{{ g.bairro or 'bairro não informado' }}{% if g.tipo %} · {{ g.tipo }}{% endif %}{% if g.condominio %} · cond. {{ reais(g.condominio) }}{% endif %}</div>
      {% if g.motivos %}<div class="motivo">Fora do pedido: {{ g.motivos }}</div>{% endif %}
      {% if g.nao_informado %}<div class="falta">Não informado no anúncio: {{ g.nao_informado }}</div>{% endif %}
      <div class="peq">{% for l in g.links %}<a href="{{ l.link }}" target="_blank" rel="noopener noreferrer">ver no {{ l.portal }}</a>{% if not loop.last %} · {% endif %}{% endfor %}
        · visto pela 1ª vez {{ hora(g.primeira_vista) }}</div>
    </div>
  {% endmacro %}
  <h2>✅ Exatos ({{ exatos|length }})</h2>
  {% for g in exatos %}{{ card(g) }}{% else %}<p class="dica">Nenhum imóvel bate com todos os critérios.</p>{% endfor %}
  {% if incompletos %}<h2>🟡 Faltou algum dado no anúncio ({{ incompletos|length }})</h2>
  <p class="dica">Nada falhou, mas algum critério não pôde ser conferido. Confira no anúncio.</p>
  {% for g in incompletos %}{{ card(g) }}{% endfor %}{% endif %}
  {% if parecidos %}<details><summary>Parecidos que o portal acrescentou ({{ parecidos|length }})</summary>
  {% for g in parecidos %}{{ card(g) }}{% endfor %}</details>{% endif %}
  {% if sem_conferencia %}<h2>Sem critérios para conferir ({{ sem_conferencia|length }})</h2>
  {% for g in sem_conferencia %}{{ card(g) }}{% endfor %}{% endif %}
  <p><a href="{{ url_for('pedidos.ver', pid=p.id) }}">&larr; Busca #{{ p.id }}</a> · <a href="{{ url_for('pedidos.lista') }}">Nova busca</a></p>
</div></body></html>"""


def agrupar_resultados(linhas):
    """Junta o mesmo imóvel de portais diferentes (mesmo número de anúncio) num card só e separa
    por estado da conferência. Dentro de cada grupo, ordena por preço."""
    grupos, ordem = {}, []
    for l in linhas:
        chave = ("id", l["anuncio_id"]) if l.get("anuncio_id") else ("link", l["link"])
        if chave not in grupos:
            g = dict(l)
            g["links"] = []
            grupos[chave] = g
            ordem.append(chave)
        grupos[chave]["links"].append({"portal": l.get("portal") or "portal", "link": l["link"]})
    saida = {"sim": [], "incompleto": [], "não": [], None: []}
    for chave in ordem:
        g = grupos[chave]
        saida.get(g.get("dentro_do_filtro"), saida[None]).append(g)
    for lista in saida.values():
        lista.sort(key=lambda g: (g.get("preco") is None, g.get("preco") or 0))
    return saida


def _render_lista(erro=None, form=None, status=200):
    try:
        pedidos = storage.obter().listar_pedidos(20)
    except Exception as exc:
        pedidos, erro = [], erro or f"Não consegui ler os pedidos do banco ({type(exc).__name__})."
    return render_template_string(PAGINA_LISTA, pedidos=pedidos, erro=erro, form=form or {}, tipos=TIPOS,
                                  cidades=regiao.CIDADES_REGIAO, hora=_hora_br, rotulo=_ROTULO,
                                  resumo_filtros=resumo_filtros), status


@bp.route("/pedidos", methods=["GET"])
def lista():
    return _render_lista()


@bp.route("/pedidos", methods=["POST"])
def criar():
    filtros, erro = validar_filtros(request.form)
    if erro:
        return _render_lista(erro=erro, form=request.form, status=400)
    b = storage.obter()
    try:
        b.expirar_pedidos(MINUTOS_PARA_EXPIRAR)
        limite = _limite_excedido(b.listar_pedidos(50))
        if limite:
            return _render_lista(erro=limite, form=request.form, status=429)
        pid = b.criar_pedido(filtros)
    except Exception as exc:
        return _render_lista(erro=f"Não consegui gravar o pedido no banco ({type(exc).__name__}).",
                             form=request.form, status=502)
    return redirect(url_for("pedidos.ver", pid=pid), code=303)


@bp.route("/pedidos/<int:pid>", methods=["GET"])
def ver(pid):
    p = storage.obter().obter_pedido(pid)
    if p is None:
        abort(404)
    return render_template_string(PAGINA_PEDIDO, p=p, hora=_hora_br, rotulo=_ROTULO, resumo_filtros=resumo_filtros)


@bp.route("/pedidos/<int:pid>/resultados", methods=["GET"])
def resultados(pid):
    b = storage.obter()
    p = b.obter_pedido(pid)
    if p is None:
        abort(404)
    g = agrupar_resultados(b.buscar_resultados(pid))
    return render_template_string(PAGINA_RESULTADOS, p=p, exatos=g["sim"], incompletos=g["incompleto"],
                                  parecidos=g["não"], sem_conferencia=g[None], hora=_hora_br, reais=_reais,
                                  resumo_filtros=resumo_filtros)


# ---------------------------------------------------------------------------
# API do coletor
# ---------------------------------------------------------------------------

@bp.route("/api/pedidos/proximo", methods=["POST"])
def api_proximo():
    if not token_coletor_ok():
        return jsonify({"erro": "token inválido"}), 401
    b = storage.obter()
    try:
        b.expirar_pedidos(MINUTOS_PARA_EXPIRAR)
        p = b.pegar_proximo_pedido()
    except Exception as exc:
        return jsonify({"erro": "falha no armazenamento", "tipo": type(exc).__name__}), 502
    return jsonify({"pedido": p})


@bp.route("/api/pedidos/<int:pid>/finalizar", methods=["POST"])
def api_finalizar(pid):
    if not token_coletor_ok():
        return jsonify({"erro": "token inválido"}), 401
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict) or corpo.get("estado") not in ("concluido", "erro"):
        return jsonify({"erro": "corpo deve ser JSON com estado 'concluido' ou 'erro'"}), 400
    resumo, erro = corpo.get("resumo"), corpo.get("erro")
    if resumo is not None and not isinstance(resumo, dict):
        return jsonify({"erro": "resumo deve ser um objeto"}), 400
    if erro is not None and not isinstance(erro, str):
        return jsonify({"erro": "erro deve ser texto"}), 400
    b = storage.obter()
    try:
        atual = b.obter_pedido(pid)
        if atual is None:
            return jsonify({"erro": "pedido não existe"}), 404
        if atual["estado"] != "em_andamento":
            # ex.: expirou por demora; não sobrescreve
            return jsonify({"erro": f"pedido está '{atual['estado']}', não 'em_andamento'"}), 409
        b.finalizar_pedido(pid, corpo["estado"], resumo=resumo, erro=(erro or None) and erro[:1000])
    except Exception as exc:
        return jsonify({"erro": "falha no armazenamento", "tipo": type(exc).__name__}), 502
    return jsonify({"ok": True})
