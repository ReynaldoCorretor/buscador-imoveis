# -*- coding: utf-8 -*-
"""
coletor.py — NÚCLEO de coleta de anúncios (VivaReal, ZAP Imóveis, Imovelweb) com Playwright.

É uma biblioteca: não lê argumentos de terminal e não escreve arquivos por conta própria.
Quem usa:
  * extrator_imoveis.py  -> script de terminal (grava CSV/JSON no seu computador)
  * o buscador (app.py)  -> chama coletar() e guarda/mostra o resultado

Uso básico:
    import coletor
    resultado = coletor.coletar(filtros={"tipo": "apartamento", "quartos": "3,4",
                                         "preco_max": 600000, "bairro": "vila-mogilar"},
                                paginas=3, log=print)
    resultado["anuncios"]      # imóveis válidos (venda na cidade)
    resultado["descartados"]   # aluguéis / outras cidades, com o motivo
    resultado["portais"]       # estado de cada portal (ok / parcial / bloqueado / ...)

Como funciona (sem depender de classes CSS): acha os links de anúncio, sobe até o "card"
que contém um único anúncio e lê o texto visível com expressões regulares. Lê os cards
ENQUANTE rola a página (listas "virtualizadas" esvaziam cards fora da tela).

Limites conhecidos: os portais podem barrar (Cloudflare) IPs de servidores; os termos de uso
do VivaReal/ZAP vedam robôs sem autorização (ver README). Use com moderação.
"""

import json
import os
import random
import re
import time

# ---------------------------------------------------------------------------
# Log (quem chama decide para onde vão as mensagens)
# ---------------------------------------------------------------------------

_logger = print


def _log(mensagem=""):
    try:
        _logger(mensagem)
    except Exception:
        pass   # um log que falha nunca derruba a coleta


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

VERSAO = "2026-11-01-r2 (região: 10 cidades; conferência completa; duplicados pelo nº do anúncio; modo leve p/ servidor)"

PORTAIS = {
    "vivareal": {
        "nome": "VivaReal",
        "url_pagina": "https://www.vivareal.com.br/venda/sp/mogi-das-cruzes/?pagina={n}",
        "url_primeira": "https://www.vivareal.com.br/venda/sp/mogi-das-cruzes/",
        "marcador_link": "/imovel/",
    },
    "zap": {
        "nome": "ZAP Imóveis",
        "url_pagina": "https://www.zapimoveis.com.br/venda/imoveis/sp+mogi-das-cruzes/?pagina={n}",
        "url_primeira": "https://www.zapimoveis.com.br/venda/imoveis/sp+mogi-das-cruzes/",
        "marcador_link": "/imovel/",
    },
    "imovelweb": {
        "nome": "Imovelweb",
        "url_pagina": "https://www.imovelweb.com.br/imoveis-venda-mogi-das-cruzes-sp-pagina-{n}.html",
        "url_primeira": "https://www.imovelweb.com.br/imoveis-venda-mogi-das-cruzes-sp.html",
        "marcador_link": "/propriedades/",
    },
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Pausa aleatória (segundos) entre páginas, para não sobrecarregar o portal
PAUSA_MIN, PAUSA_MAX = 4, 8

CIDADE_ALVO = "Mogi das Cruzes"

CAMPOS_CSV = [
    "portal", "busca", "cidade_busca", "titulo", "tipo", "preco", "condominio", "iptu", "area_m2",
    "quartos", "banheiros", "vagas", "bairro", "endereco", "cidade",
    "finalidade", "dentro_do_filtro", "motivos", "nao_informado", "possivel_duplicado",
    "anuncio_id", "link", "texto_bruto",
]
CAMPOS_DESCARTADOS = CAMPOS_CSV + ["motivo_descarte"]

# ---------------------------------------------------------------------------
# JavaScript executado dentro da página: acha os cards a partir dos links
# ---------------------------------------------------------------------------

JS_EXTRAIR_CARDS = """
(marcador) => {
  const links = Array.from(document.querySelectorAll('a[href]'))
    .filter(a => a.href.includes(marcador));

  const hrefsUnicos = new Set(links.map(a => a.href.split('?')[0]));
  const resultados = [];
  const descartados = [];   // links achados cujo card não tem "R$" (amostra p/ diagnóstico)
  let descartadosTotal = 0;
  const vistos = new Set();

  for (const a of links) {
    const href = a.href.split('?')[0];
    if (vistos.has(href)) continue;

    // Sobe enquanto o ancestral ainda contém só ESTE anúncio
    let card = a;
    for (let i = 0; i < 12; i++) {
      const pai = card.parentElement;
      if (!pai || pai === document.body) break;
      const hrefsNoPai = new Set(
        Array.from(pai.querySelectorAll('a[href]'))
          .filter(x => x.href.includes(marcador))
          .map(x => x.href.split('?')[0])
      );
      if (hrefsNoPai.size > 1) break;
      card = pai;
    }

    const texto = (card.innerText || '').trim();
    if (!texto.includes('R$')) {           // não parece um card de anúncio
      descartadosTotal++;
      if (descartados.length < 3) descartados.push({ href: a.href, texto: texto.slice(0, 300) });
      continue;
    }
    vistos.add(href);
    resultados.push({ href: a.href, texto: texto });
  }
  return { total_links: hrefsUnicos.size, cards: resultados, descartados: descartados, descartados_total: descartadosTotal, hrefs: Array.from(hrefsUnicos) };
}
"""

# ---------------------------------------------------------------------------
# Interpretação do texto de cada card
# ---------------------------------------------------------------------------


def _numero_br(texto: str):
    """'1.100.000' -> 1100000 (int). Devolve None se não der."""
    if texto is None:
        return None
    limpo = re.sub(r"[^\d,]", "", texto)
    if not limpo:
        return None
    limpo = limpo.replace(",", ".")
    try:
        valor = float(limpo)
    except ValueError:
        return None
    return int(valor) if valor == int(valor) else valor


def _primeiro(padrao: str, texto: str, flags=re.IGNORECASE):
    m = re.search(padrao, texto, flags)
    return m.group(1) if m else None


def _norm(txt: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower().strip()


_RX_VIA = re.compile(r"^(rua|av\.?|avenida|estrada|alameda|travessa|rodovia|pra[cç]a)\b", re.IGNORECASE)


def extrair_cidade(linhas, cidade=None):
    """Acha a linha de localização e devolve a cidade, ou None se não achar.
    Formatos vistos: 'Bairro, Cidade' (VivaReal/ZAP/Imovelweb) e
    'Cidade, São Paulo' (Imovelweb, sem bairro, com o estado)."""
    alvo = _norm(cidade or CIDADE_ALVO)
    for l in linhas:
        if len(l) > 70 or "R$" in l or _RX_VIA.match(l):
            continue
        m = re.fullmatch(r"([^,|]+),\s*([^,|]+)", l.strip())
        if not m:
            continue
        a, b = m.group(1).strip(), m.group(2).strip()
        if _norm(a) == alvo:
            return a
        if _norm(b) == alvo:
            return b
        if not re.search(r"\d", l):
            return b
    return None


def motivo_descarte(campos: dict, cidade=None) -> str:
    """Devolve por que o anúncio NÃO serve à busca (venda na cidade), ou ''."""
    if campos.get("finalidade") == "aluguel":
        return "aluguel"
    do_anuncio = campos.get("cidade")
    if do_anuncio and _norm(do_anuncio) != _norm(cidade or CIDADE_ALVO):
        return f"outra cidade ({do_anuncio})"
    return ""


def _corte_cidade(linha: str, alvo_norm: str):
    """Posição da vírgula que antecede o nome da cidade na linha ("Centro, Suzano - SP" -> 6), ou None."""
    if len(linha) > 120 or "R$" in linha:
        return None
    for m in re.finditer(r",", linha):
        resto = _norm(linha[m.end():])
        if resto == alvo_norm or re.match(re.escape(alvo_norm) + r"\b", resto):
            return m.start()
    return None


_RX_ID = re.compile(r"-id-(\d+)")


def anuncio_id(link: str):
    """Número do anúncio nos links do VivaReal/ZAP (os dois usam o MESMO número — verificado)."""
    if not link or not re.search(r"(vivareal|zapimoveis)\.com\.br", link):
        return None
    m = _RX_ID.search(link)
    return m.group(1) if m else None


def interpretar_card(texto: str, cidade=None) -> dict:
    """Extrai os campos de um anúncio a partir do texto visível do card."""
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    plano = " | ".join(linhas)

    # Preço: primeiro "R$ ..." que NÃO seja condomínio/IPTU
    preco = None
    for m in re.finditer(r"R\$\s*([\d\.]+(?:,\d+)?)", plano):
        antes = plano[max(0, m.start() - 12):m.start()].lower()
        depois = plano[m.end():m.end() + 14].lower()
        if "cond" in antes or "iptu" in antes or "condom" in depois:
            continue
        preco = _numero_br(m.group(1))
        break

    condominio = _numero_br(_primeiro(r"cond(?:om[ií]nio)?\.?\s*R\$\s*([\d\.]+(?:,\d+)?)", plano)
                            or _primeiro(r"R\$\s*([\d\.]+(?:,\d+)?)\s*cond", plano))
    iptu = _numero_br(_primeiro(r"iptu\s*R\$\s*([\d\.]+(?:,\d+)?)", plano))

    # Área: aceita "104 m²", "57 - 74 m²" (pega o primeiro), "300 m² tot."
    area = _numero_br(_primeiro(r"(\d[\d\.]*)\s*(?:-\s*\d[\d\.]*\s*)?m[²2]", plano))

    # VivaReal/ZAP mostram "Quantidade de quartos | 3" (rótulo e valor em linhas
    # separadas); Imovelweb mostra "3 quartos | 3 ban. | 2 vagas".
    quartos = (_primeiro(r"Quantidade de quartos\s*\|\s*(\d+)", plano)
               or _primeiro(r"(\d+)\s*(?:-\s*\d+\s*)?quartos?", plano))
    banheiros = (_primeiro(r"Quantidade de banheiros\s*\|\s*(\d+)", plano)
                 or _primeiro(r"(\d+)\s*(?:-\s*\d+\s*)?(?:banheiros?|ban\.)", plano))
    vagas = (_primeiro(r"Quantidade de vagas(?: de garagem)?\s*\|\s*(\d+)", plano)
             or _primeiro(r"(\d+)\s*(?:-\s*\d+\s*)?vagas?", plano))

    # Descrição tipo "Apartamento para comprar com 104 m², 4 quartos..."
    titulo = None
    for l in linhas:
        if re.search(r"(para comprar|à venda|apartamento|casa|terreno|sobrado|cobertura|sala|galpão)", l, re.IGNORECASE) \
                and "R$" not in l and len(l) > 15:
            titulo = l
            break

    # Bairro/cidade: linha "Bairro, <cidade da busca>" (sem diferenciar acentos: "Poá" = "Poa")
    bairro, endereco = None, None
    alvo_cidade = _norm(cidade or CIDADE_ALVO)
    for i, l in enumerate(linhas):
        corte = _corte_cidade(l, alvo_cidade)
        if corte is not None:
            bairro = l[:corte].strip()
            # Se a linha vier como "Rua X, Bairro", separa endereço e bairro
            if "," in bairro and re.match(r"^(rua|av\.?|avenida|estrada|alameda|travessa|rodovia|praça)\b", bairro, re.IGNORECASE):
                endereco, bairro = [x.strip() for x in bairro.rsplit(",", 1)]
            # Endereço costuma ser a linha ao lado (antes ou depois)
            for j in (i + 1, i - 1):
                if 0 <= j < len(linhas):
                    cand = linhas[j]
                    if re.match(r"^(rua|av\.?|avenida|estrada|alameda|travessa|rodovia|praça)\b", cand, re.IGNORECASE):
                        endereco = cand
                        break
            break
    if endereco is None:
        for l in linhas:
            if re.match(r"^(rua|av\.?|avenida|estrada|alameda|travessa|rodovia)\b", l, re.IGNORECASE):
                endereco = l
                break

    if titulo and len(titulo) > 120:
        titulo = titulo[:117].rstrip() + "..."   # descrição completa continua em texto_bruto

    return {
        "titulo": titulo,
        "preco": preco,
        "condominio": condominio,
        "iptu": iptu,
        "area_m2": area,
        "quartos": int(quartos) if quartos else None,
        "banheiros": int(banheiros) if banheiros else None,
        "vagas": int(vagas) if vagas else None,
        "bairro": bairro,
        "endereco": endereco,
        "cidade": extrair_cidade(linhas, cidade),
        "finalidade": "aluguel" if re.search(r"/\s*m[êe]s", plano, re.IGNORECASE) else "venda",
    }


# ---------------------------------------------------------------------------
# Navegação
# ---------------------------------------------------------------------------


def aceitar_cookies(pagina):
    """Fecha o banner de cookies, se aparecer (não é crítico)."""
    for texto in ("Aceito", "Aceitar", "Aceitar todos", "Concordo"):
        try:
            botao = pagina.get_by_role("button", name=texto, exact=True)
            if botao.count() > 0:
                botao.first.click(timeout=1500)
                return
        except Exception:
            pass


# Textos de botões que só DISPENSAM um aviso. Nunca incluir "Explorar novidades",
# "Aceitar", "Entrar", "Cadastrar" etc. (esses levam a outra ação).
_RX_DISPENSAR = re.compile(r"^\s*(agora n[ãa]o|n[ãa]o,? obrigad[oa]|mais tarde)\s*$", re.IGNORECASE)


def _fechou(item) -> bool:
    """True se o elemento sumiu (ou deixou de estar visível) — prova de que o clique funcionou."""
    try:
        return not item.is_visible()
    except Exception:
        return True    # elemento destruído = a janela foi removida


def _tentar_fechar(pagina, item, rotulo: str) -> bool:
    """Tenta fechar clicando em `item` por 3 métodos, CONFERINDO depois de cada um se a
    janela realmente sumiu (um clique forçado num botão coberto 'funciona' sem fazer nada)."""
    metodos = (("clique", lambda: item.click(timeout=2500)),
               ("clique forçado", lambda: item.click(timeout=2000, force=True)),
               ("clique via script", lambda: item.evaluate("e => e.click()")))
    for nome, acao in metodos:
        try:
            acao()
        except Exception:
            continue
        pagina.wait_for_timeout(600)
        if _fechou(item):
            _log(f"    🪟 aviso do portal fechado (cliquei em '{rotulo}' — {nome})")
            return True
    return False


def fechar_avisos(pagina) -> bool:
    """Fecha janelas de aviso que cobrem a lista (ex.: 'Sua busca ficou ainda melhor!',
    'Conheça o chat no Zap!', ambas com 'Explorar novidade' e 'Agora não'). Clica só em
    'Agora não' (ou equivalentes), depois no X da janela e, por último, na tecla Esc —
    nunca em 'Explorar novidade'. Cada método é conferido. Devolve True se fechou algo."""
    try:
        try:
            texto_tela = pagina.inner_text("body").lower()
        except Exception:
            texto_tela = ""
        tem_aviso = any(x in texto_tela for x in ("agora não", "explorar novidade", "evoluímos a forma como mostramos"))

        alvo = pagina.get_by_text(_RX_DISPENSAR)
        for i in range(min(alvo.count(), 3)):
            item = alvo.nth(i)
            try:
                visivel = item.is_visible()
                rotulo = (item.inner_text() or "").strip()
            except Exception:
                continue
            if visivel and _tentar_fechar(pagina, item, rotulo):
                return True

        x = pagina.locator('[role="dialog"] [aria-label*="echar" i], [aria-modal="true"] [aria-label*="echar" i], '
                           '[role="dialog"] button[aria-label*="lose" i]')
        if x.count() > 0:
            item = x.first
            try:
                if item.is_visible() and _tentar_fechar(pagina, item, "X da janela"):
                    return True
            except Exception:
                pass

        if tem_aviso:
            try:
                achados = pagina.get_by_text(re.compile(r"agora n[ãa]o", re.IGNORECASE))
                if achados.count() > 0:
                    html = achados.first.evaluate("e => (e.closest('button,a,[role=button]') || e).outerHTML")
                    _log(f"    🪟 aviso detectado, mas não consegui clicar. HTML do 'Agora não': {html[:300]!r}")
                else:
                    _log("    🪟 aviso detectado no texto da página, mas nenhum elemento 'Agora não' encontrado")
            except Exception:
                pass
            pagina.keyboard.press("Escape")
            pagina.wait_for_timeout(800)
            try:
                depois = pagina.inner_text("body").lower()
            except Exception:
                depois = texto_tela
            if not any(x in depois for x in ("agora não", "explorar novidade")):
                _log("    🪟 aviso do portal fechado (tecla Esc)")
                return True
    except Exception as exc:
        _log(f"    (não consegui tratar o aviso: {str(exc).strip().splitlines()[0][:120]})")
    return False


def coletar_rolando(pagina, marcador: str, passo_px: int = 600, max_passos: int = 40) -> dict:
    """Rola a página devagar e LÊ OS CARDS A CADA PASSO, juntando por link.

    Por quê: muitos portais usam lista "virtualizada" — só os cards perto da
    tela têm conteúdo; os que ficam fora são esvaziados. Ler só no fim da
    rolagem pega apenas os ~23 últimos e deixa metade dos links sem texto.
    Devolve o mesmo formato de JS_EXTRAIR_CARDS."""
    cards, todos = {}, set()

    def juntar():
        dados = pagina.evaluate(JS_EXTRAIR_CARDS, marcador)
        todos.update(dados.get("hrefs", []))
        for c in dados["cards"]:
            chave = c["href"].split("?")[0]
            anterior = cards.get(chave)
            if anterior is None or len(c["texto"]) > len(anterior["texto"]):
                cards[chave] = c

    try:
        pagina.evaluate("window.scrollTo(0, 0)")
        pagina.wait_for_timeout(600)
    except Exception:
        pass
    no_fim, y_anterior = 0, None
    for passo in range(max_passos):
        juntar()
        try:
            y, altura = pagina.evaluate(
                "() => [window.scrollY + window.innerHeight, document.documentElement.scrollHeight]")
        except Exception:
            break
        no_fim = no_fim + 1 if y >= altura - 4 else 0
        if no_fim >= 2:
            break
        # um aviso aberto pode travar a rolagem: fecha periodicamente e se a página não andou
        if passo % 6 == 5 or (y_anterior is not None and y == y_anterior and no_fim == 0):
            fechar_avisos(pagina)
        y_anterior = y
        pagina.mouse.wheel(0, passo_px)
        pagina.wait_for_timeout(350)
    pagina.wait_for_timeout(800)
    juntar()
    return {"total_links": len(todos), "cards": list(cards.values()), "descartados": [],
            "descartados_total": max(0, len(todos) - len(cards)), "hrefs": list(todos)}


def rolar_pagina(pagina, passos=10):
    """Rola devagar até o fim para disparar o carregamento (lazy-load)."""
    for _ in range(passos):
        pagina.mouse.wheel(0, 900)
        pagina.wait_for_timeout(400)
    pagina.wait_for_timeout(1200)


JS_PAGINACAO = """
() => {
  const rx = /pr[oó]xim|seguinte|next|pagin|›|»|ver mais|carregar mais/i;
  const out = [];
  for (const el of document.querySelectorAll('a, button, li, nav, [role="button"], [data-qa], [data-testid]')) {
    const cls = (typeof el.className === 'string') ? el.className : '';
    const attrs = [
      el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
      el.getAttribute('data-qa') || '', el.getAttribute('data-testid') || '', cls,
      el.getAttribute('href') || ''
    ].join(' ');
    if (!rx.test(attrs)) continue;
    if ((el.innerText || '').length > 200) continue;   // pula containers grandes
    out.push(el.outerHTML.slice(0, 400));
    if (out.length >= 40) break;
  }
  return out;
}
"""


def diagnosticar_paginacao(pagina, chave: str, pasta_debug, sufixo: str = ""):
    """Rola até o fim e salva em .txt os elementos que parecem ser de paginação.
    Sem pasta_debug, não faz nada."""
    if not pasta_debug:
        return
    try:
        pagina.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        pagina.wait_for_timeout(1500)
        itens = pagina.evaluate(JS_PAGINACAO)
        caminho = os.path.join(pasta_debug, f"{chave}_paginacao{sufixo}.txt")
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(f"URL: {pagina.url}\n")
            f.write(f"Elementos de paginação encontrados: {len(itens)}\n\n")
            for i, item in enumerate(itens, 1):
                f.write(f"--- {i} ---\n{item}\n\n")
        _log(f"    📄 elementos de paginação salvos em: {caminho} ({len(itens)} itens)")
    except Exception as exc:
        _log(f"    (não foi possível diagnosticar a paginação: {exc})")


SELETORES_PROXIMA = [
    '[data-qa="PAGING_NEXT"]',                 # Imovelweb
    'a[aria-label="próxima página"]',          # VivaReal / ZAP
    'a[aria-label*="róxima" i]',
    'button[aria-label*="róxima" i]',
    'a[title*="róxima" i]',
    'a[rel="next"]',
]


def tem_paginacao(pagina) -> bool:
    """Existe algum componente de paginação na página (botões 'página N', 'próxima página',
    PAGING_*)? Se não existe NENHUM, a busca cabe numa página só — não há o que clicar."""
    try:
        # específico de propósito: "[class*=pagination]" genérico pegaria as bolinhas dos
        # carrosséis de fotos ("swiper-pagination"), que existem em todo card.
        return pagina.locator('[aria-label^="página" i], [aria-label*="róxima página" i], '
                              '[data-qa^="PAGING" i], [class*="olx-core-pagination" i]').count() > 0
    except Exception:
        return True    # na dúvida, deixa o fluxo normal tentar


def clicar_proxima(pagina) -> str:
    """Acha o link de 'próxima página' e clica. Se o clique normal falhar
    (ex.: algo cobrindo o botão), dispara o clique direto no elemento.
    Devolve 'clicou', 'ultima' (botão desabilitado = fim da lista) ou
    'nao_achou' (nenhum botão reconhecido)."""
    candidatos = [(sel, pagina.locator(sel)) for sel in SELETORES_PROXIMA]
    candidatos.append(("link 'próxima'", pagina.get_by_role("link", name=re.compile(r"pr[oó]xima|next", re.IGNORECASE))))
    for nome, loc in candidatos:
        try:
            if loc.count() == 0:
                continue
            alvo = loc.last
            if (alvo.get_attribute("aria-disabled") or "").lower() == "true":
                _log("    (botão 'próxima' desabilitado — é a última página)")
                return "ultima"
            _log(f"    botão encontrado por: {nome}")
            try:
                alvo.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            pagina.wait_for_timeout(random.randint(700, 1600))
            try:
                alvo.click(timeout=4000)
                return "clicou"
            except Exception as exc:
                motivo = str(exc).strip().splitlines()[0][:140]
                _log(f"    clique normal falhou ({motivo}); fechando avisos e tentando de novo")
                fechar_avisos(pagina)
                try:
                    alvo.click(timeout=4000)
                    return "clicou"
                except Exception:
                    _log("    ainda falhou; tentando clique direto no elemento")
                    alvo.dispatch_event("click")
                    return "clicou"
        except Exception as exc:
            _log(f"    seletor {nome} falhou: {str(exc).strip().splitlines()[0][:140]}")
            continue
    return "nao_achou"


def _hrefs_atuais(pagina, marcador: str) -> set:
    try:
        lista = pagina.evaluate(
            "(m) => Array.from(document.querySelectorAll('a[href]'))"
            ".filter(a => a.href.includes(m)).map(a => a.href.split('?')[0])",
            marcador,
        )
        return set(lista)
    except Exception:
        return set()


def aguardar_troca_de_pagina(pagina, marcador: str, hrefs_antes: set, url_antes: str) -> bool:
    """Espera a navegação terminar: URL mudar, a lista de anúncios ser outra E
    parar de mudar (a lista pode ser trocada aos poucos; ler no primeiro
    instante misturaria anúncios da página anterior com os da nova)."""
    try:
        pagina.wait_for_url(lambda u: u != url_antes, timeout=20000)
    except Exception:
        pass  # pode ser troca sem mudar a URL (SPA); a checagem por links cobre
    try:
        pagina.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    mudou = False
    for _ in range(30):
        pagina.wait_for_timeout(500)
        atuais = _hrefs_atuais(pagina, marcador)
        if atuais and atuais != hrefs_antes:
            mudou = True
            break
    if not mudou:
        return False
    # estabilização: a lista precisa ficar igual por 2 s seguidos (máx. ~15 s)
    anterior, estavel = None, 0
    for _ in range(30):
        pagina.wait_for_timeout(500)
        atuais = _hrefs_atuais(pagina, marcador)
        estavel = estavel + 1 if atuais == anterior else 0
        anterior = atuais
        if estavel >= 4:
            break
    return True


# ---------------------------------------------------------------------------
# Classificação de falhas (por que uma página não trouxe anúncios?)
# ---------------------------------------------------------------------------


def classificar_pagina(status, titulo: str, texto: str) -> tuple:
    """Devolve (estado, explicação). Olha só título e TEXTO VISÍVEL (não o HTML
    bruto: 'cloudflare'/'captcha' aparecem em scripts de páginas normais).
    Estados: bloqueado, formato_mudou, vazio."""
    titulo_l = (titulo or "").lower()
    texto_l = (texto or "").lower()
    visivel = f"{titulo_l} {texto_l[:3000]}"
    if any(x in titulo_l for x in ("attention required", "just a moment", "um momento")) \
            or ("cloudflare" in visivel and (status in (403, 429, 503) or len(texto_l) < 1500)):
        return "bloqueado", "Cloudflare barrou o acesso (desafio antirrobô)"
    if any(x in visivel for x in ("captcha", "não sou um robô", "nao sou um robo", "are you human",
                                  "verify you are human", "tráfego incomum", "unusual traffic")):
        return "bloqueado", "o portal pediu verificação humana (captcha)"
    if status in (403, 429):
        return "bloqueado", f"HTTP {status} (acesso negado/limite de requisições)"
    if any(x in visivel for x in ("faça login", "faca login", "entre na sua conta",
                                  "entrar ou cadastrar", "crie sua conta para continuar")) \
            and len(texto_l) < 3000:
        return "bloqueado", "o portal pediu login/cadastro para continuar"
    if len(texto_l) < 300:
        return "vazio", f"página praticamente vazia ({len(texto_l)} caracteres de texto)"
    return "formato_mudou", ("a página parece normal mas não tem os links esperados — o portal pode ter "
                             "mudado o HTML (ajuste 'marcador_link' em PORTAIS)")


_RX_TOTAL = re.compile(r"(?<![+\w.,])(\d{1,3}(?:\.\d{3})+|\d+)\s+(?:im[óo]veis|an[úu]ncios|resultados)", re.IGNORECASE)


def extrair_total_com_trecho(texto: str):
    """(número, trecho do texto onde apareceu) ou (None, None)."""
    m = _RX_TOTAL.search((texto or "")[:4000])
    if not m:
        return None, None
    ini, fim = max(0, m.start() - 40), min(len(texto), m.end() + 40)
    return int(m.group(1).replace(".", "")), re.sub(r"\s+", " ", texto[ini:fim]).strip()


def extrair_total_informado(texto: str):
    """Número de resultados que o próprio portal diz ter (ex.: '13.429 imóveis à venda').
    Lê o primeiro número seguido de 'imóveis/anúncios/resultados' no começo do texto
    da página. Devolve int ou None. (Formato do texto ainda não verificado nos portais.)"""
    m = _RX_TOTAL.search((texto or "")[:4000])
    return int(m.group(1).replace(".", "")) if m else None


def _texto_da_pagina(pagina) -> str:
    try:
        return pagina.inner_text("body")
    except Exception:
        return ""


def _captura(pagina, pasta_debug, nome):
    """Salva um print da página em pasta_debug/nome. Devolve o caminho, ou None (sem pasta ou falha)."""
    if not pasta_debug:
        return None
    try:
        caminho = os.path.join(pasta_debug, nome)
        pagina.screenshot(path=caminho, full_page=False)
        return caminho
    except Exception:
        return None


def extrair_portal(contexto, chave: str, config: dict, num_paginas: int,
                   modo: str = "clique", ao_novos=None, pasta_debug=None, cidade=None):
    """Devolve (anuncios, info).

    modo 'clique': fica na mesma aba e clica em 'próxima página' (padrão).
    modo 'url': abre cada página por URL direta (costuma tomar 403 do Cloudflare).

    ao_novos(lista): chamada a cada página com os anúncios novos, para quem
    chama gravar em disco na hora (nada se perde se o script cair depois).

    info = {"estado": ok|parcial|bloqueado|formato_mudou|vazio|erro,
            "paginas_lidas": N, "motivo": texto}"""
    anuncios = []
    vistos = set()
    info = {"estado": "ok", "paginas_lidas": 0, "motivo": ""}
    ordem_paginas = []   # links lidos em cada página, na ordem em que aparecem (diagnóstico)
    if pasta_debug:
        os.makedirs(pasta_debug, exist_ok=True)

    def falhar(estado, motivo):
        """Marca a falha; se já há páginas lidas, vira 'parcial'."""
        if info["paginas_lidas"] > 0 and estado != "erro":
            info["estado"], info["motivo"] = "parcial", f"parou na página {info['paginas_lidas'] + 1}: {motivo}"
        else:
            info["estado"], info["motivo"] = estado, motivo
        _log(f"    ❌ {info['estado'].upper()}: {info['motivo']}")

    pagina = contexto.new_page()
    try:
        for n in range(1, num_paginas + 1):
            resposta = None
            url_antes = pagina.url
            try:
                if n == 1 or modo == "url":
                    url = config["url_primeira"] if n == 1 else config["url_pagina"].format(n=n)
                    _log(f"  [{config['nome']}] página {n}: {url}")
                    opcoes = {"wait_until": "domcontentloaded", "timeout": 30000}
                    if n > 1:
                        opcoes["referer"] = config["url_primeira"]
                    resposta = pagina.goto(url, **opcoes)
                else:
                    _log(f"  [{config['nome']}] página {n}: clicando em 'próxima página'")
                    fechar_avisos(pagina)   # um aviso por cima impediria o clique
                    if not tem_paginacao(pagina):
                        _log("    (a página não tem paginação — a busca cabe em uma página só)")
                        info["motivo"] = (f"fim da lista: o portal não mostra paginação "
                                          f"(a busca cabe em {info['paginas_lidas']} página)")
                        if len(anuncios) >= 48:   # páginas cheias dos portais têm ~43-52 anúncios
                            info["estado"], info["motivo"] = "parcial", (
                                f"a página veio cheia ({len(anuncios)} anúncios) e não achei paginação — "
                                "confira no site se existe página 2 (o botão pode ter mudado)")
                        break
                    antes = _hrefs_atuais(pagina, config["marcador_link"])
                    url_antes = pagina.url
                    resultado_clique = clicar_proxima(pagina)
                    if resultado_clique == "ultima":
                        info["motivo"] = f"fim da lista: o portal só tem {info['paginas_lidas']} página(s)"
                        break
                    if resultado_clique != "clicou":
                        _captura(pagina, pasta_debug, f"{chave}_sem_botao_pagina{n}.png")
                        diagnosticar_paginacao(pagina, chave, pasta_debug, "_falha")
                        estado, motivo = classificar_pagina(None, pagina.title(), _texto_da_pagina(pagina))
                        # sem botão: se a página é normal, é fim da lista ou botão mudou
                        if estado == "formato_mudou":
                            motivo = "botão 'próxima página' não encontrado (fim da lista ou o portal mudou o botão)"
                        falhar(estado if estado != "formato_mudou" else "parcial", motivo)
                        break
                    mudou = aguardar_troca_de_pagina(pagina, config["marcador_link"], antes, url_antes)
                    _log(f"    URL agora: {pagina.url} | conteúdo trocou: {'sim' if mudou else 'NÃO'}")
                    if not mudou:
                        # Pode ter sido um aviso que "engoliu" o clique: fecha e tenta uma vez mais.
                        _log("    ↻ a página não trocou; fechando avisos e clicando de novo (1 tentativa)")
                        fechar_avisos(pagina)
                        if clicar_proxima(pagina) == "clicou":
                            mudou = aguardar_troca_de_pagina(pagina, config["marcador_link"], antes, url_antes)
                            _log(f"    URL agora: {pagina.url} | conteúdo trocou: {'sim' if mudou else 'NÃO'}")

                try:
                    pagina.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                pagina.wait_for_timeout(2000)
                aceitar_cookies(pagina)
                fechar_avisos(pagina)
                dados = coletar_rolando(pagina, config["marcador_link"])
                fechar_avisos(pagina)   # alguns avisos só aparecem depois de rolar
                if n == 1:
                    diagnosticar_paginacao(pagina, chave, pasta_debug)
                    total_info, trecho_total = extrair_total_com_trecho(_texto_da_pagina(pagina))
                    if total_info is not None:
                        info["total_informado"] = total_info
                        _log(f"    ℹ número achado no texto da página: {total_info} (trecho: {trecho_total!r}) — "
                              "pode NÃO ser o total de resultados; compare com o que o site mostra")

                if pasta_debug:
                    with open(os.path.join(pasta_debug, f"{chave}_pagina{n}.html"), "w", encoding="utf-8") as f:
                        f.write(pagina.content())

                if dados["total_links"] == 0:
                    status = resposta.status if resposta else None
                    titulo = pagina.title()
                    estado, motivo = classificar_pagina(status, titulo, _texto_da_pagina(pagina))
                    _log(f"    🔎 DIAGNÓSTICO: status HTTP={status or '?'} | URL final={pagina.url} | título={titulo!r}")
                    caminho_png = _captura(pagina, pasta_debug, f"{chave}_pagina{n}.png")
                    if caminho_png:
                        _log(f"       captura salva em: {caminho_png}")
                    falhar(estado, motivo)
                    break
                if not dados["cards"] and dados["total_links"] > 0 and fechar_avisos(pagina):
                    pagina.wait_for_timeout(1500)
                    dados = pagina.evaluate(JS_EXTRAIR_CARDS, config["marcador_link"])
                if not dados["cards"]:
                    # Há links de anúncio, mas nenhum card tem "R$": a página pode ter vindo
                    # incompleta (carregando), com um aviso por cima, ou o portal mudou o texto.
                    titulo = pagina.title()
                    estado, motivo = classificar_pagina(None, titulo, _texto_da_pagina(pagina))
                    caminho_png = _captura(pagina, pasta_debug, f"{chave}_pagina{n}.png")
                    _log(f"    🔎 DIAGNÓSTICO: {dados['total_links']} links de anúncio, mas 0 cards com 'R$'")
                    _log(f"       título={titulo!r} | captura: {caminho_png}")
                    for d in dados.get("descartados", []):
                        trecho = " | ".join(l.strip() for l in d["texto"].splitlines() if l.strip())[:160]
                        _log(f"       amostra de card sem preço: {trecho!r}")
                    texto_tela = _texto_da_pagina(pagina).lower()
                    if "agora não" in texto_tela or "explorar novidades" in texto_tela:
                        estado, motivo = "bloqueado", ("uma janela de aviso do portal está cobrindo a lista "
                                                       "e não foi possível fechá-la sozinho")
                    elif estado == "formato_mudou":
                        motivo = (f"{dados['total_links']} links de anúncio achados, mas nenhum card com preço "
                                  "('R$') — página incompleta/carregando, aviso por cima ou texto do card mudou")
                    falhar(estado, motivo)
                    break
            except Exception as exc:
                msg = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
                falhar("erro", f"{type(exc).__name__}: {msg}")
                break

            if dados.get("descartados_total"):
                _log(f"    ℹ {dados['descartados_total']} link(s) de anúncio ficaram SEM card com preço (não lidos)")
                for d in dados.get("descartados", [])[:2]:
                    trecho = " | ".join(l.strip() for l in d["texto"].splitlines() if l.strip())[:140]
                    _log(f"       ex.: {trecho!r}")
            ordem_paginas.append([c["href"].split("?")[0] for c in dados["cards"]])
            novos_lista = []
            for card in dados["cards"]:
                link = card["href"].split("?")[0]
                if link in vistos:
                    continue
                vistos.add(link)
                campos = interpretar_card(card["texto"], cidade)
                novos_lista.append({
                    "portal": config["nome"],
                    **campos,
                    "link": link,
                    "texto_bruto": " | ".join(l.strip() for l in card["texto"].splitlines() if l.strip()),
                })
            anuncios.extend(novos_lista)
            info["paginas_lidas"] += 1
            if ao_novos and novos_lista:
                try:
                    ao_novos(novos_lista)   # grava em disco já
                except Exception as exc:
                    _log(f"    ⚠️ não consegui gravar esta página em disco: {exc}")

            _log(f"    links de anúncio na página: {dados['total_links']} | cards lidos: {len(dados['cards'])} | novos: {len(novos_lista)}")

            if not novos_lista:
                _log("    (nenhum anúncio novo — parando a paginação deste portal)")
                if n > 1:
                    if modo != "url" and pagina.url != url_antes:
                        # A URL avançou, mas a lista é a mesma: o portal repete a última página
                        # quando se pede uma página além do fim. É o FIM DA LISTA, não falha.
                        info["motivo"] = f"fim da lista: a página {n} repetiu a anterior (o portal só tem {n - 1} página(s))"
                    else:
                        info["estado"], info["motivo"] = "parcial", (
                            f"a página {n} repetiu a anterior (a navegação não avançou)")
                break

            if n < num_paginas:
                time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))
    finally:
        try:
            pagina.close()
        except Exception:
            pass
        if pasta_debug:
            try:
                with open(os.path.join(pasta_debug, f"{chave}_ordem.json"), "w", encoding="utf-8") as f:
                    json.dump(ordem_paginas, f, ensure_ascii=False)
            except Exception:
                pass

    if info["estado"] == "ok" and not anuncios:
        info["estado"], info["motivo"] = "vazio", "nenhum anúncio lido"
    return anuncios, info


# ---------------------------------------------------------------------------
# API pública: coletar()
# ---------------------------------------------------------------------------


class ColetorErro(ValueError):
    """Parâmetro inválido (antes de abrir o navegador)."""


class PlaywrightAusente(RuntimeError):
    """O Playwright (ou o Chromium) não está instalado neste computador/servidor."""


def _host_base(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _marcar_duplicados(anuncios):
    """Marca 'possivel_duplicado' em anúncios do mesmo imóvel em portais diferentes.
    1) VivaReal x ZAP: pelo NÚMERO DO ANÚNCIO (exato — os dois portais usam o mesmo número).
    2) Demais (sem número comum): mesmo preço E mesma área — só uma PISTA, confira os links.
    Nada é apagado. Devolve o número de grupos."""
    n = 0
    por_id = {}
    for a in anuncios:
        if a.get("anuncio_id"):
            por_id.setdefault(a["anuncio_id"], []).append(a)
    for itens in por_id.values():
        if len({i["portal"] for i in itens}) > 1:
            n += 1
            for i in itens:
                i["possivel_duplicado"] = f"mesmo anúncio (grupo {n})"
    grupos = {}
    for a in anuncios:
        if a.get("preco") and a.get("area_m2") and not a.get("possivel_duplicado"):
            grupos.setdefault((a["preco"], a["area_m2"]), []).append(a)
    for itens in grupos.values():
        if len({i["portal"] for i in itens}) > 1:
            n += 1
            for i in itens:
                i["possivel_duplicado"] = f"pista: mesmo preço e área (grupo {n})"
    return n


def _preparar_busca(portais, urls, filtros, config_portais, cidade, cidade_slug=None):
    """Valida os parâmetros e devolve (configs, portais_a_rodar, urls_usadas, avisos).
    Levanta ColetorErro — SEM abrir o navegador.

    Com cidade_slug (uma das 10 cidades de regiao.py): VivaReal e ZAP SEMPRE recebem o endereço
    montado para aquela cidade (com ou sem filtros); o Imovelweb fica de fora (formato desconhecido)."""
    if cidade_slug:
        return _preparar_busca_regiao(portais, filtros, config_portais, cidade_slug)
    base = config_portais if config_portais is not None else PORTAIS
    explicito = portais is not None
    portais = list(portais) if portais else list(base)
    for p in portais:
        if p not in base:
            raise ColetorErro(f"portal desconhecido: {p!r} (use: {', '.join(base)})")
    cfgs = {k: dict(v) for k, v in base.items()}
    avisos = []

    urls_usadas = {}
    for chave, url in (urls or {}).items():
        chave = chave.strip().lower()
        if chave not in base:
            raise ColetorErro(f"portal desconhecido na URL: {chave!r} (use: {', '.join(base)})")
        if _host_base(url) != _host_base(base[chave]["url_primeira"]):
            raise ColetorErro(f"a URL de {chave} precisa ser do próprio portal "
                              f"({_host_base(base[chave]['url_primeira'])}); recebi {_host_base(url) or url[:40]!r}")
        urls_usadas[chave] = url.strip()

    f = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    if urls_usadas:
        if f.get("tipo") or f.get("bairro"):
            raise ColetorErro("com URLs prontas, só 'quartos' e 'preco_max' valem (para conferir o resultado); "
                              "'tipo' e 'bairro' só servem para montar a URL")
    elif any(f.get(k) for k in ("tipo", "quartos", "preco_max", "bairro")):
        try:
            import busca_portais as bp
        except ImportError:
            raise ColetorErro("falta o arquivo busca_portais.py na mesma pasta do coletor")
        for chave in ("vivareal", "zap"):
            if chave in cfgs:
                try:
                    urls_usadas[chave] = bp.montar_url(chave, tipo=f.get("tipo"), quartos=f.get("quartos"),
                                                       preco_max=f.get("preco_max"), bairro=f.get("bairro"))
                except ValueError as exc:
                    raise ColetorErro(str(exc))
        if "imovelweb" in portais:
            avisos.append("Imovelweb: formato de filtro ainda desconhecido — não foi consultado nesta busca")
            portais = [p for p in portais if p != "imovelweb"]
        if not urls_usadas:
            # pediram filtros, mas nenhum portal sabe montar a busca filtrada: NÃO rodar sem filtro em silêncio
            raise ColetorErro("nenhum dos portais escolhidos aceita busca filtrada automática "
                              "(só VivaReal e ZAP); use urls= com a busca copiada do site")

    if urls_usadas:
        for chave, url in urls_usadas.items():
            cfgs[chave]["url_primeira"] = url
        if not explicito:
            portais = list(urls_usadas)       # só os portais que receberam URL
    if not portais:
        raise ColetorErro("nenhum portal para consultar")
    return cfgs, portais, urls_usadas, avisos


def _preparar_busca_regiao(portais, filtros, config_portais, cidade_slug):
    try:
        import busca_portais as bp
    except ImportError:
        raise ColetorErro("falta o arquivo busca_portais.py na mesma pasta do coletor")
    base = config_portais if config_portais is not None else PORTAIS
    cfgs = {k: dict(v) for k, v in base.items()}
    pedidos = list(portais) if portais else ["vivareal", "zap"]
    avisos = []
    if "imovelweb" in pedidos:
        avisos.append("Imovelweb: busca por cidade ainda não suportada — não foi consultado")
    pedidos = [p for p in pedidos if p in ("vivareal", "zap")]
    for p in pedidos:
        if p not in base:
            raise ColetorErro(f"portal desconhecido: {p!r}")
    if not pedidos:
        raise ColetorErro("nenhum portal para consultar (só VivaReal e ZAP aceitam busca por cidade)")
    f = filtros or {}
    urls_usadas = {}
    for chave in pedidos:
        try:
            urls_usadas[chave] = bp.montar_url(chave, tipo=f.get("tipo"), quartos=f.get("quartos"),
                                               preco_max=f.get("preco_max"), bairro=f.get("bairro"),
                                               cidade=cidade_slug)
        except ValueError as exc:
            raise ColetorErro(str(exc))
        cfgs[chave]["url_primeira"] = urls_usadas[chave]
    return cfgs, pedidos, urls_usadas, avisos


_TIPOS_PESADOS = ("image", "media", "font")
# rastreadores/anúncios: não têm anúncio de imóvel e criam iframes/scripts que gastam memória
# (a mesma lista que o leitor do Chaves na Mão usa no servidor, em produção)
_RASTREADORES = ("googletagmanager", "google-analytics", "doubleclick", "googlesyndication",
                 "facebook.net", "facebook.com/tr", "hotjar", "clarity.ms", "criteo",
                 "taboola", "adsystem", "adnxs")
_ARGS_LEVES = ["--renderer-process-limit=2", "--disable-extensions", "--disable-background-networking",
               "--disable-features=site-per-process,IsolateOrigins,Translate", "--mute-audio"]


def _bloquear_pesados(rota):
    """Modo leve (servidor com pouca memória): não baixa imagens, vídeos, fontes nem rastreadores.
    O texto dos cards — que é o que lemos — não depende deles."""
    try:
        url = rota.request.url
        if rota.request.resource_type in _TIPOS_PESADOS or any(d in url for d in _RASTREADORES):
            rota.abort()
        else:
            rota.continue_()
    except Exception:
        pass


def coletar(*, portais=None, paginas=2, modo="clique", urls=None, filtros=None, cidade=None,
            cidade_slug=None, leve=False,
            nome_busca="", mostrar_janela=False, pasta_debug=None, log=print,
            ao_pagina=None, config_portais=None) -> dict:
    """Roda a coleta e devolve o resultado. NÃO escreve arquivos (só pasta_debug, se dada).

    portais        lista de chaves ('vivareal', 'zap', 'imovelweb'); None = todos (ou só os com URL)
    paginas        páginas por portal
    urls           {'vivareal': URL, ...} — busca já filtrada, copiada do site
    filtros        {'tipo','quartos','preco_max','bairro'} — monta as URLs sozinho (VivaReal e ZAP);
                   com `urls`, só 'quartos'/'preco_max' valem (para marcar dentro_do_filtro)
    cidade         padrão: Mogi das Cruzes; anúncios de outras cidades vão para 'descartados'
    leve           True no servidor: não baixa imagens/vídeos/fontes (economiza memória). Padrão: False
    cidade_slug    uma das 10 cidades de regiao.py (ex.: 'suzano'); monta as URLs dessa cidade e
                   define `cidade` pelo nome cadastrado. Todos os filtros de `filtros` são conferidos
                   anúncio a anúncio (conferencia.py): tipo, quartos, preço mín/máx, área, banheiros,
                   vagas, bairro
    log            função(str) que recebe as mensagens de andamento
    ao_pagina      função(validos, descartados) chamada a cada página lida (para gravar na hora)
    config_portais usado nos testes (portais simulados)

    Levanta ColetorErro (parâmetro inválido) ou PlaywrightAusente. Falhas durante a coleta NÃO
    levantam: aparecem em resultado['portais'][nome]['estado'] e resultado['erro_navegador'].
    """
    global _logger
    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    if cidade_slug:
        try:
            import busca_portais as _bp
            import regiao as _rg
        except ImportError as exc:
            raise ColetorErro(f"falta arquivo do pacote: {exc}")
        if cidade_slug not in _rg.NOMES:
            raise ColetorErro(f"cidade {cidade_slug!r} não é uma das 10 da região")
        cad = _bp.carregar_cidades().get(cidade_slug)
        cidade = (cad or {}).get("nome") or _rg.NOMES[cidade_slug]
        if filtros.get("bairro"):
            info_b = _bp.carregar_bairros().get((cidade_slug, filtros["bairro"]))
            if info_b:
                filtros["bairro_nome"] = info_b["nome"]
    cidade = (cidade or CIDADE_ALVO).strip()
    nome_busca = re.sub(r"[^A-Za-z0-9_-]+", "-", (nome_busca or "").strip()).strip("-")
    cfgs, a_rodar, urls_usadas, avisos = _preparar_busca(portais, urls, filtros, config_portais, cidade,
                                                         cidade_slug=cidade_slug)
    if urls_usadas:
        modo = "clique"            # com URL personalizada, o modo 'url' não funciona

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightAusente("Playwright não está instalado (pip install playwright; "
                                "playwright install chromium)") from exc

    try:
        import conferencia
    except ImportError:
        conferencia = None   # pacote antigo sem conferencia.py: segue sem conferir
    criterios = {k: v for k, v in filtros.items() if k not in ("paginas", "cidade")}

    todos, descartados, status_portais = [], [], {}
    interrompido, erro_navegador = False, None

    def ao_novos(lista):
        validos, desc = [], []
        for a in lista:
            a["busca"] = nome_busca
            a["cidade_busca"] = cidade_slug or ""
            a["anuncio_id"] = anuncio_id(a.get("link"))
            a["dentro_do_filtro"], a["motivos"], a["nao_informado"], a["tipo"] = "", "", "", ""
            if conferencia is not None:
                a.update(conferencia.avaliar(a, criterios))
                if not criterios:
                    a["dentro_do_filtro"] = ""   # sem filtros não há o que conferir
            motivo = motivo_descarte(a, cidade)
            if motivo:
                desc.append({**a, "motivo_descarte": motivo})
            else:
                validos.append(a)
        todos.extend(validos)
        descartados.extend(desc)
        if ao_pagina:
            ao_pagina(validos, desc)

    _logger = log
    try:
        for aviso in avisos:
            _log(f"  ({aviso})")
        if urls_usadas:
            _log("Busca filtrada:")
            for chave, url in urls_usadas.items():
                _log(f"  {chave}: {url}")
        try:
            with sync_playwright() as p:
                navegador = None
                try:
                    navegador = p.chromium.launch(
                        headless=not mostrar_janela,
                        args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"] + (_ARGS_LEVES if leve else []),
                    )
                    for chave in a_rodar:
                        config = cfgs[chave]
                        _log(f"\n=== {config['nome']} ===")
                        contexto = None
                        try:
                            # contexto novo por portal: o travamento de um não contamina o seguinte
                            contexto = navegador.new_context(
                                user_agent=USER_AGENT,
                                locale="pt-BR",
                                viewport={"width": 1280, "height": 800},
                                extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
                            )
                            if leve:
                                contexto.route("**/*", _bloquear_pesados)
                            _, info = extrair_portal(contexto, chave, config, paginas, modo,
                                                     ao_novos=ao_novos, pasta_debug=pasta_debug, cidade=cidade)
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            info = {"estado": "erro", "paginas_lidas": 0,
                                    "motivo": f"{type(exc).__name__}: {str(exc).strip()[:200]}"}
                            _log(f"  ❌ falha inesperada em {config['nome']}: {info['motivo']}")
                        finally:
                            if contexto:
                                try:
                                    contexto.close()
                                except Exception:
                                    pass
                        status_portais[config["nome"]] = info
                except KeyboardInterrupt:
                    interrompido = True
                    _log("\n⚠️ Interrompido pelo usuário — mantendo o que já foi lido…")
                finally:
                    if navegador:
                        try:
                            navegador.close()
                        except Exception:
                            pass
        except Exception as exc:
            erro_navegador = f"{type(exc).__name__}: {exc}"
            _log(f"\n❌ Erro ao iniciar/encerrar o navegador: {erro_navegador}")
    finally:
        _logger = print

    grupos = _marcar_duplicados(todos)
    conformidade = {}
    if conferencia is not None and criterios:
        for chave in a_rodar:
            nome = cfgs[chave]["nome"]
            meus = [a for a in todos if a["portal"] == nome]
            if meus:
                conformidade[nome] = conferencia.contar(meus)
    return {
        "versao": VERSAO, "nome_busca": nome_busca, "cidade": cidade, "portais_rodados": a_rodar,
        "anuncios": todos, "descartados": descartados, "portais": status_portais,
        "interrompido": interrompido, "erro_navegador": erro_navegador, "grupos_duplicados": grupos,
        "conformidade": conformidade, "urls_usadas": urls_usadas, "avisos": avisos,
        "cidade_slug": cidade_slug or "", "filtros": filtros,
    }
