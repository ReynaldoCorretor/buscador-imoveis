# -*- coding: utf-8 -*-
"""
rotas_busca.py — CAMINHO 2: a página única do buscador (celular), protegida por senha (SITE_SENHA).

    GET /                       formulário (cidade, tipo, quartos, preço, área, banheiros, vagas, bairro)
    GET /busca?cidade=...       resultados NA HORA:
                                  * VivaReal + ZAP: do BANCO (abastecido pela varredura do coletor no PC)
                                  * Chaves na Mão: AO VIVO, lido pelo servidor (aparece em seguida)
    GET /chaves/<job_id>        pedaço de página com os resultados do Chaves na Mão (quando prontos)
    GET /bairros?cidade=...     bairros já vistos na cidade (para a lista do formulário)

Por que o banco: o servidor é barrado pelo Cloudflare no VivaReal/ZAP (VERIFICADO em 01/11/2026,
/diagnostico-coletor), mas a conexão de casa não. Então o PC coleta quando está ligado, e o celular
consulta o que foi coletado — com a data da coleta à vista.

Todo imóvel é conferido aqui (conferencia.py) contra TODOS os critérios: exato / falta dado / parecido.
"""
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, render_template_string, request, url_for

import conferencia
import jobs
import regiao
import storage
from rotas_pedidos import (_ESTILO, _hora_br, _reais, agrupar_resultados, exigir_senha, resumo_filtros,
                           validar_filtros)

bp = Blueprint("busca", __name__)

DIAS_VALIDADE = 30          # imóveis não vistos há mais que isso ficam de fora (podem ter sido vendidos)
MAX_PARECIDOS = 30          # só os "quase": falham em UM critério
_FAMILIAS = {
    "apartamento": {"apartamento", "cobertura", "kitnet", "studio", "flat", "loft"},
    "casa": {"casa", "sobrado", "casa de condomínio"},
}
_RX_BAIRRO = re.compile(r"^[A-Za-zÀ-ÿ0-9'’ .\-]{2,60}$")


@bp.before_request
def _senha():
    return exigir_senha()


# ---------------------------------------------------------------------------
# Lógica (testável sem Flask)
# ---------------------------------------------------------------------------

def _plural(n, um, varios):
    return f"{n} {um if n == 1 else varios}"


def detalhes(g) -> str:
    """'62 m² · 2 quartos · 1 banheiro · 1 vaga' — só o que o anúncio informa (nada de 'vagas' sem número)."""
    partes = []
    try:
        if g.get("area_m2") not in (None, ""):
            partes.append(f"{float(g['area_m2']):g} m²")
    except (TypeError, ValueError):
        pass
    for campo, um, varios in (("quartos", "quarto", "quartos"), ("banheiros", "banheiro", "banheiros"),
                              ("vagas", "vaga", "vagas")):
        v = g.get(campo)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            partes.append(_plural(int(v), um, varios))
    return " · ".join(partes)


def bairro_do_titulo(titulo, cidade_nome):
    """O Chaves na Mão não tem bairro na lista, mas o título que ele gera traz:
    'Apartamento A Venda 2 Quartos Sp Suzano Cidade Boa Vista 56M2 Rs140000' -> 'Cidade Boa Vista'.
    Regra: palavras entre '<Sp> <cidade>' e o primeiro '<número>M2' / 'Rs<número>'. Sem acentos
    (o portal tira). [VERIFICADO só nos títulos vistos em 02/11; formato pode mudar.]"""
    palavras = (titulo or "").split()
    alvo = regiao.norm(cidade_nome).split()
    n = len(alvo)
    for i, p in enumerate(palavras):
        if p.lower() != "sp" or [regiao.norm(x) for x in palavras[i + 1:i + 1 + n]] != alvo:
            continue
        resto = []
        for q in palavras[i + 1 + n:]:
            if re.fullmatch(r"\d+(?:[.,]\d+)?m2", q, re.IGNORECASE) or re.fullmatch(r"rs\d+", q, re.IGNORECASE):
                break
            resto.append(q)
        return " ".join(resto) or None
    return None


def validar_busca(args) -> tuple:
    """Mesmas regras do formulário de pedidos, mas o bairro é um NOME (vindo da lista de bairros
    vistos), não um slug cadastrado."""
    dados = {k: v for k, v in args.items() if k != "bairro"}
    filtros, erro = validar_filtros(dados)
    if erro:
        return None, erro
    filtros.pop("paginas", None)
    bairro = (args.get("bairro") or "").strip()
    if bairro:
        if not _RX_BAIRRO.match(bairro):
            return None, "Bairro: use um nome da lista (letras, números, espaço e hífen)."
        filtros["bairro_nome"] = bairro
        filtros["bairro"] = regiao.norm(bairro).replace(" ", "-")
    return filtros, None


def _criterios(filtros: dict) -> dict:
    return {k: v for k, v in filtros.items() if k != "cidade"}


def _fora_da_familia(tipo_pedido, tipo_anuncio) -> bool:
    """Casa pedida e anúncio de apartamento (ou terreno, sala...): nem entra como 'parecido'."""
    if not tipo_pedido or not tipo_anuncio:
        return False
    return tipo_anuncio not in _FAMILIAS.get(tipo_pedido, {tipo_pedido})


def buscar_no_banco(filtros: dict, b=None) -> dict:
    b = b or storage.obter()
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS_VALIDADE)).isoformat(timespec="seconds")
    linhas = b.imoveis_da_cidade(filtros["cidade"], desde_iso=desde)
    crit = _criterios(filtros)
    avaliadas = []
    for l in linhas:
        if l.get("finalidade") == "aluguel":
            continue
        c = conferencia.avaliar(l, crit)
        if _fora_da_familia(filtros.get("tipo"), c["tipo"] or l.get("tipo")):
            continue
        avaliadas.append(dict(l, **{k: c[k] for k in ("dentro_do_filtro", "motivos", "nao_informado")},
                              tipo=c["tipo"] or l.get("tipo")))
    g = agrupar_resultados(avaliadas)
    parecidos = [x for x in g["não"] if x.get("motivos") and ";" not in x["motivos"]][:MAX_PARECIDOS]
    vistas = sorted(filter(None, (l.get("ultima_vista") for l in linhas)))
    return {"exatos": g["sim"], "incompletos": g["incompleto"], "parecidos": parecidos,
            "total_guardados": len(linhas), "coletado_de": vistas[0] if vistas else None,
            "coletado_ate": vistas[-1] if vistas else None}


def bairros_da_cidade(cidade: str, b=None) -> list:
    b = b or storage.obter()
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS_VALIDADE)).isoformat(timespec="seconds")
    contagem = {}
    for l in b.imoveis_da_cidade(cidade, desde_iso=desde):
        nome = (l.get("bairro") or "").strip()
        if nome and len(nome) <= 60:
            chave = regiao.norm(nome)
            atual = contagem.get(chave)
            contagem[chave] = (atual[0] if atual else nome, (atual[1] if atual else 0) + 1)
    return sorted(contagem.values(), key=lambda x: regiao.norm(x[0]))


def executar_chaves(cidade: str, filtros: dict) -> dict:
    """Roda NA THREAD DE BUSCAS do site (o Chaves na Mão funciona a partir do servidor — verificado).
    Usa o leitor que já existia (scrapers.py) e depois confere cada imóvel com os mesmos critérios."""
    from scrapers import buscar_todos_portais
    tipo = {"apartamento": "Apartamento", "casa": "Casa"}.get(filtros.get("tipo"))
    lq = sorted(int(x) for x in str(filtros.get("quartos") or "").split(",") if x)
    r = buscar_todos_portais(
        cidade=regiao.NOMES[cidade], uf="SP", tipo=tipo,
        quartos_min=lq[0] if lq else None, quartos_max=lq[-1] if lq else None,
        banheiros_min=filtros.get("banheiros_min"), area_min=filtros.get("area_min"),
        area_max=filtros.get("area_max"), preco_min=filtros.get("preco_min"), preco_max=filtros.get("preco_max"))
    crit = _criterios(filtros)
    itens = []
    for im in r.get("todos", []):
        im = dict(im)
        if not im.get("bairro"):
            im["bairro"] = bairro_do_titulo(im.get("titulo"), regiao.NOMES[cidade])
        c = conferencia.avaliar(im, crit)
        itens.append(dict(im, **c, anuncio_id=None, primeira_vista=None))
    g = agrupar_resultados(itens)
    diag = "; ".join(r.get("diagnosticos_por_portal", {}).get("Chaves na Mão", [])[:3])
    return {"exatos": g["sim"] + g[None], "incompletos": g["incompleto"], "parecidos": g["não"][:MAX_PARECIDOS],
            "erro": r.get("erros", {}).get("Chaves na Mão"), "diagnostico": diag,
            "bairro_ignorado": False}


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------

_CARD = """
{% macro card(g) %}
  <div class="card">
    <div class="preco">{{ reais(g.preco) }}</div>
    <div class="tit">{{ g.titulo or 'Sem título' }}</div>
    <div class="info">{{ detalhes(g) }}</div>
    <div class="info">{{ g.bairro or 'bairro não informado' }}{% if g.tipo %} · {{ g.tipo }}{% endif %}{% if g.condominio %} · cond. {{ reais(g.condominio) }}{% endif %}</div>
    {% if g.motivos %}<div class="motivo">Fora do pedido: {{ g.motivos }}</div>{% endif %}
    {% if g.nao_informado %}<div class="falta">Não informado no anúncio: {{ g.nao_informado }}</div>{% endif %}
    <div class="peq">{% for l in g.links %}<a href="{{ l.link }}" target="_blank" rel="noopener noreferrer">ver no {{ l.portal }}</a>{% if not loop.last %} · {% endif %}{% endfor %}
      {% if g.ultima_vista %} · coletado {{ hora(g.ultima_vista) }}{% endif %}</div>
  </div>
{% endmacro %}
{% macro blocos(r, rotulo_vazio) %}
  <h3>✅ Exatos ({{ r.exatos|length }})</h3>
  {% for g in r.exatos %}{{ card(g) }}{% else %}<p class="dica">{{ rotulo_vazio }}</p>{% endfor %}
  {% if r.incompletos %}<h3>🟡 Faltou algum dado no anúncio ({{ r.incompletos|length }})</h3>
    <p class="dica">Nada falhou, mas algum critério não pôde ser conferido. Confira no anúncio.</p>
    {% for g in r.incompletos %}{{ card(g) }}{% endfor %}{% endif %}
  {% if r.parecidos %}<details><summary>Quase: falham em um só critério ({{ r.parecidos|length }})</summary>
    {% for g in r.parecidos %}{{ card(g) }}{% endfor %}</details>{% endif %}
{% endmacro %}
"""

PAGINA_INICIO = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Buscador de imóveis</title>""" + _ESTILO + """
</head><body><div class="box">
  <h1>🏠 Buscador de imóveis — Mogi e região</h1>
  {% if erro %}<p class="erro">{{ erro }}</p>{% endif %}
  <form method="get" action="{{ url_for('busca.buscar') }}">
    <div class="linha">
      <div><label for="cidade">Cidade</label>
        <select id="cidade" name="cidade" required onchange="carregarBairros()"><option value="">Escolha…</option>
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
      <div><label for="preco_min">Preço mínimo (R$)</label><input id="preco_min" name="preco_min" inputmode="numeric" value="{{ form.preco_min or '' }}"></div>
      <div><label for="preco_max">Preço máximo (R$)</label><input id="preco_max" name="preco_max" inputmode="numeric" placeholder="ex.: 400000" value="{{ form.preco_max or '' }}"></div>
    </div>
    <div class="linha">
      <div><label for="area_min">Área mín. (m²)</label><input id="area_min" name="area_min" inputmode="numeric" value="{{ form.area_min or '' }}"></div>
      <div><label for="area_max">Área máx. (m²)</label><input id="area_max" name="area_max" inputmode="numeric" value="{{ form.area_max or '' }}"></div>
      <div><label for="banheiros_min">Banheiros (mín.)</label><input id="banheiros_min" name="banheiros_min" inputmode="numeric" value="{{ form.banheiros_min or '' }}"></div>
      <div><label for="vagas_min">Vagas (mín.)</label><input id="vagas_min" name="vagas_min" inputmode="numeric" value="{{ form.vagas_min or '' }}"></div>
    </div>
    <label for="bairro">Bairro (opcional — os que já apareceram nas coletas)</label>
    <input id="bairro" name="bairro" list="lista-bairros" autocomplete="off" value="{{ form.bairro or '' }}" placeholder="escolha a cidade e comece a digitar">
    <datalist id="lista-bairros"></datalist>
    <button type="submit">Buscar</button>
  </form>
  <p class="dica">VivaReal e ZAP: resultado na hora, a partir das coletas guardadas (feitas pelo coletor do
  seu PC quando ele está ligado). Chaves na Mão: lido na hora pelo servidor.</p>
  <p class="dica"><a href="{{ url_for('pedidos.lista') }}">Coletas e pedidos de atualização</a></p>
</div>
<script>
async function carregarBairros() {
  const c = document.getElementById('cidade').value, dl = document.getElementById('lista-bairros');
  dl.innerHTML = '';
  if (!c) return;
  try {
    const r = await fetch('{{ url_for("busca.bairros") }}?cidade=' + encodeURIComponent(c), {credentials: 'same-origin'});
    const lista = await r.json();
    for (const b of lista) { const o = document.createElement('option'); o.value = b.nome; o.label = b.nome + ' (' + b.qtd + ')'; dl.appendChild(o); }
  } catch (e) {}
}
carregarBairros();
</script>
</body></html>"""

PAGINA_RESULTADOS = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Resultados</title>""" + _ESTILO + """
</head><body>""" + _CARD + """<div class="box">
  <h1>{{ resumo }}</h1>
  <p><a href="{{ url_for('busca.inicio', **form) }}">&larr; Mudar a busca</a></p>
</div>
<div class="box">
  <h2>VivaReal + ZAP</h2>
  {% if r.total_guardados %}
    <p class="dica">{{ r.total_guardados }} imóveis guardados de {{ cidade }}, coletados entre {{ hora(r.coletado_de) }} e
    {{ hora(r.coletado_ate) }}. O mesmo imóvel nos dois portais aparece num card só. É uma amostra do que os portais
    mostraram nas coletas, não o estoque completo.</p>
    {{ blocos(r, 'Nenhum imóvel guardado bate com todos os critérios.') }}
  {% else %}
    <p class="falta">Ainda não há imóveis guardados de {{ cidade }} (dos últimos {{ dias }} dias). O coletor do PC
    guarda as 10 cidades quando está ligado.</p>
  {% endif %}
  <form method="post" action="{{ url_for('pedidos.criar') }}">
    {% for k, v in pedido.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
    <button type="submit">Pedir coleta nova desta busca (o PC atende quando ligar)</button>
  </form>
</div>
<div class="box" id="chaves">
  <h2>Chaves na Mão (ao vivo)</h2>
  <p id="chaves-status" class="dica">{% if job_chaves %}Buscando no Chaves na Mão… (30 a 90 segundos){% else %}{{ erro_chaves }}{% endif %}</p>
  <div id="chaves-conteudo"></div>
</div>
{% if job_chaves %}<script>
const urlEstado = {{ url_estado | tojson }}, urlChaves = {{ url_chaves | tojson }};
let falhas = 0;
async function verChaves() {
  try {
    const r = await fetch(urlEstado, {cache: 'no-store'});
    if (r.status === 404) { document.getElementById('chaves-status').textContent = 'A busca no Chaves na Mão expirou. Recarregue a página.'; return; }
    const d = await r.json(); falhas = 0;
    if (d.estado === 'concluido' || d.estado === 'erro') {
      const h = await fetch(urlChaves, {cache: 'no-store', credentials: 'same-origin'});
      document.getElementById('chaves-conteudo').innerHTML = await h.text();
      document.getElementById('chaves-status').textContent = '';
      return;
    }
    document.getElementById('chaves-status').textContent = d.estado === 'fila'
      ? 'Na fila do servidor (posição ' + d.posicao + ')…' : 'Buscando no Chaves na Mão… ' + Math.round(d.decorrido) + ' s';
  } catch (e) { if (++falhas > 15) { document.getElementById('chaves-status').textContent = 'Sem resposta do servidor.'; return; } }
  setTimeout(verChaves, 2500);
}
verChaves();
</script>{% endif %}
</body></html>"""

FRAGMENTO_CHAVES = _CARD + """
{% if erro_job %}<p class="erro">{{ erro_job }}</p>
{% else %}
  <p class="dica">No Chaves na Mão o bairro é lido do título do anúncio (o portal escreve sem acentos).</p>
  {% if r.erro and not (r.exatos or r.incompletos or r.parecidos) %}<p class="falta">Sem resultados do Chaves na Mão: {{ r.erro }}</p>{% endif %}
  {{ blocos(r, 'Nenhum imóvel do Chaves na Mão bate com todos os critérios.') }}
  <p class="dica">Amostra: até 5 páginas por tipo (limite do próprio portal para robôs).</p>
{% endif %}
"""


@bp.route("/", methods=["GET"])
def inicio():
    return render_template_string(PAGINA_INICIO, form=request.args, erro=None, cidades=regiao.CIDADES_REGIAO,
                                  tipos=("apartamento", "casa"))


@bp.route("/busca", methods=["GET"])
def buscar():
    filtros, erro = validar_busca(request.args)
    if erro:
        return render_template_string(PAGINA_INICIO, form=request.args, erro=erro, cidades=regiao.CIDADES_REGIAO,
                                      tipos=("apartamento", "casa")), 400
    try:
        r = buscar_no_banco(filtros)
    except Exception as exc:
        r = {"exatos": [], "incompletos": [], "parecidos": [], "total_guardados": 0,
             "coletado_de": None, "coletado_ate": None}
        erro = f"Não consegui ler o banco ({type(exc).__name__})."
    job_chaves, erro_chaves = None, ""
    try:
        job_chaves = jobs.gerenciador.enviar(executar_chaves, meta={"tipo_job": "chaves"},
                                             cidade=filtros["cidade"], filtros=filtros)
    except jobs.ServidorOcupado as exc:
        erro_chaves = str(exc)
    form = {k: v for k, v in request.args.items() if v}
    pedido = {k: v for k, v in form.items() if k != "bairro"}   # pedido de coleta: bairro só se cadastrado
    pedido["paginas"] = "2"
    resumo = resumo_filtros(dict(filtros, bairro=filtros.get("bairro_nome"), paginas=None)).replace(", None pág.", "")
    return render_template_string(
        PAGINA_RESULTADOS, r=r, form=form, pedido=pedido, cidade=regiao.NOMES[filtros["cidade"]],
        resumo=resumo + (f" — {erro}" if erro else ""), dias=DIAS_VALIDADE, hora=_hora_br, reais=_reais, detalhes=detalhes,
        job_chaves=job_chaves, erro_chaves=erro_chaves,
        url_estado=url_for("estado_busca", job_id=job_chaves) if job_chaves else None,
        url_chaves=url_for("busca.chaves", job_id=job_chaves) if job_chaves else None)


@bp.route("/chaves/<job_id>", methods=["GET"])
def chaves(job_id):
    job = jobs.gerenciador.consultar(job_id)
    if job is None or job["meta"].get("tipo_job") != "chaves":
        abort(404)
    if job["estado"] == jobs.ERRO:
        return render_template_string(FRAGMENTO_CHAVES, erro_job=f"A busca no Chaves na Mão falhou: {job['erro']}",
                                      r={}, hora=_hora_br, reais=_reais, detalhes=detalhes)
    if job["estado"] != jobs.CONCLUIDO:
        return "", 202
    return render_template_string(FRAGMENTO_CHAVES, erro_job=None, r=job["resultado"], hora=_hora_br, reais=_reais, detalhes=detalhes)


@bp.route("/bairros", methods=["GET"])
def bairros():
    cidade = request.args.get("cidade", "")
    if cidade not in regiao.NOMES:
        return jsonify([])
    try:
        return jsonify([{"nome": n, "qtd": q} for n, q in bairros_da_cidade(cidade)])
    except Exception:
        return jsonify([])
