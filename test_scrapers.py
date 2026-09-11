# -*- coding: utf-8 -*-
"""
test_scrapers.py (v2)

Testes offline dos parsers de regex, usando HTML sintético (não faz
requisições reais à internet). A partir da v2, cada função buscar_xxx
devolve uma tupla (lista_de_imoveis, lista_de_diagnosticos) — os testes
foram atualizados para refletir isso.

Rodar com: python test_scrapers.py
"""

import unittest
from unittest.mock import patch

import scrapers


HTML_VIVAREAL_SINTETICO = """
<html><body>
<a href="https://www.vivareal.com.br/imovel/casa-2-quartos-jardim-cintia-mogi-das-cruzes-com-garagem-100m2-venda-RS520000-id-2903134721/?source=ranking%2Crp">Casa</a>
<a href="https://www.vivareal.com.br/imovel/apartamento-3-quartos-centro-mogi-das-cruzes-com-garagem-116m2-venda-RS980000-id-2874542204/?source=ranking%2Crp">Apto</a>
</body></html>
"""

HTML_CHAVESNAMAO_SINTETICO = """
<html><body>
<a href="https://www.chavesnamao.com.br/imovel/casa-a-venda-2-quartos-com-garagem-sp-mogi-das-cruzes-jardim-sao-francisco-RS800000/id-41546364/">Casa</a>
</body></html>
"""

HTML_ZAP_SINTETICO = """
<html><body>
<div class="card">
  <span>3 quartos</span> <span>2 banheiros</span> <span>150 m2</span>
  <span>R$ 650.000</span>
  <a href="https://www.zapimoveis.com.br/imovel/venda-casa-3-quartos-vila-suissa-mogi-das-cruzes-125m2-id-2666021410/">Casa em Vila Suíssa</a>
</div>
</body></html>
"""

HTML_IMOVELWEB_SINTETICO = """
<html><body>
<div class="posting">
  <span>2 dormitorios</span> <span>1 banheiro</span> <span>60 m2</span> <span>R$ 350.000</span>
  <a href="https://www.imovelweb.com.br/propriedades/apartamento-com-2-dormitorios-a-venda-48-m-por-r$-3028351035.html">Apartamento</a>
</div>
</body></html>
"""

HTML_VAZIO = "<html><body><p>Nenhum resultado</p></body></html>"


def _mock_buscar_pagina_sequencia(respostas):
    """Ajuda a simular _buscar_pagina devolvendo (html, diagnostico) para
    cada chamada em sequência."""
    def _fake(url, marcador_de_conteudo=None):
        html = respostas.pop(0) if respostas else None
        diag = f"{url} -> " + ("OK" if html else "HTTP 403 (simulado)")
        return html, diag
    return _fake


class TestLimparLink(unittest.TestCase):
    def test_remove_query_string(self):
        link = "https://www.vivareal.com.br/imovel/casa-2-quartos-id-123/?source=ranking%2Crp"
        limpo = scrapers._limpar_link(link)
        self.assertEqual(
            limpo, "https://www.vivareal.com.br/imovel/casa-2-quartos-id-123/"
        )

    def test_link_sem_query_nao_muda(self):
        link = "https://www.vivareal.com.br/imovel/casa-2-quartos-id-123/"
        self.assertEqual(scrapers._limpar_link(link), link)


class TestVivaReal(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_do_slug_mesmo_com_query_string(self, mock_buscar):
        mock_buscar.side_effect = _mock_buscar_pagina_sequencia(
            [HTML_VIVAREAL_SINTETICO, None, None]
        )
        resultado, diagnosticos = scrapers.buscar_vivareal("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 2)
        self.assertEqual(len(diagnosticos), 3)

        casa = next(im for im in resultado if "casa" in im.link)
        self.assertEqual(casa.preco, 520000.0)
        self.assertEqual(casa.area_m2, 100.0)
        self.assertEqual(casa.quartos, 2)
        self.assertFalse(casa.dados_incompletos)
        self.assertNotIn("?source=", casa.link)

    @patch("scrapers._buscar_pagina")
    def test_pagina_vazia_nao_quebra(self, mock_buscar):
        mock_buscar.return_value = (HTML_VAZIO, "OK, mas vazio")
        resultado, diagnosticos = scrapers.buscar_vivareal("Cidade Fake", "XX", max_paginas=1)
        self.assertEqual(resultado, [])
        self.assertEqual(len(diagnosticos), 1)

    @patch("scrapers._buscar_pagina")
    def test_falha_de_rede_gera_diagnostico(self, mock_buscar):
        mock_buscar.return_value = (None, "HTTP 403 (provável bloqueio antirrobô)")
        resultado, diagnosticos = scrapers.buscar_vivareal("Mogi das Cruzes", "SP", max_paginas=2)
        self.assertEqual(resultado, [])
        self.assertEqual(len(diagnosticos), 2)
        self.assertIn("403", diagnosticos[0])


class TestChavesNaMao(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_do_slug(self, mock_buscar):
        mock_buscar.return_value = (HTML_CHAVESNAMAO_SINTETICO, "OK")
        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].preco, 800000.0)
        self.assertEqual(resultado[0].quartos, 2)

    @patch("scrapers._buscar_pagina")
    def test_tipo_especifico_usa_url_certa(self, mock_buscar):
        mock_buscar.return_value = (HTML_CHAVESNAMAO_SINTETICO, "OK")
        scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo="Casa")

        # A 1ª chamada é a página 1 (sem ?pg=). Como o mock sempre devolve
        # o MESMO link, a página 2 não traz nada novo e a busca para aí
        # (parada antecipada) — por isso 2 chamadas, não 5.
        self.assertEqual(mock_buscar.call_count, 2)
        primeira_url = mock_buscar.call_args_list[0][0][0]
        segunda_url = mock_buscar.call_args_list[1][0][0]
        self.assertIn("casas-a-venda", primeira_url)
        self.assertIn("sp-mogi-das-cruzes", primeira_url)
        self.assertNotIn("?pg=", primeira_url)
        self.assertIn("?pg=2", segunda_url)

    @patch("scrapers._buscar_pagina")
    def test_tipo_nao_especificado_combina_categorias(self, mock_buscar):
        mock_buscar.return_value = (HTML_CHAVESNAMAO_SINTETICO, "OK")
        scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo=None)

        # Todas as categorias foram tentadas ao menos uma vez
        urls_chamadas = [c[0][0] for c in mock_buscar.call_args_list]
        for slug in scrapers.TIPOS_AMPLOS_PADRAO_CHAVESNAMAO:
            self.assertTrue(any(slug in u for u in urls_chamadas))
        # Padrão atual: 1 página por categoria quando nenhum tipo é
        # especificado (reduzido de 2 para 1 por segurança de tempo) — 4
        # categorias × 1 página = 4 chamadas.
        self.assertEqual(mock_buscar.call_count, 4)

    @patch("scrapers._buscar_pagina")
    def test_tipo_desconhecido_cai_no_padrao_amplo(self, mock_buscar):
        mock_buscar.return_value = (HTML_CHAVESNAMAO_SINTETICO, "OK")
        scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo="Tipo Que Não Existe")
        self.assertEqual(mock_buscar.call_count, 4)  # mesmo padrão do teste acima

    @patch("scrapers._buscar_pagina")
    def test_pagina_maxima_respeitada_e_nao_ultrapassa_5(self, mock_buscar):
        # HTML sempre com um link NOVO (id diferente a cada chamada) para
        # forçar a busca a continuar paginando até o limite. Pede
        # explicitamente mais páginas do que o permitido (10), para
        # confirmar que o teto de 5 é respeitado mesmo assim.
        contador = {"n": 0}

        def _fake(url, marcador_de_conteudo=None):
            contador["n"] += 1
            html = (
                f'<html><a href="/imovel/casa-a-venda-2-quartos-sp-mogi-das-cruzes-'
                f'RS{500000 + contador["n"]}/id-{9000 + contador["n"]}/">Casa</a></html>'
            )
            return html, "OK"

        mock_buscar.side_effect = _fake

        scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo="Casa", max_paginas=10)

        # Mesmo pedindo 10 páginas, nunca deve passar de 5 (limite do
        # robots.txt: só ?pg=2 até ?pg=5 são permitidos)
        self.assertEqual(mock_buscar.call_count, scrapers.PAGINA_MAXIMA_PERMITIDA_CHAVESNAMAO)
        urls_chamadas = [c[0][0] for c in mock_buscar.call_args_list]
        self.assertNotIn("?pg=6", " ".join(urls_chamadas))

    @patch("scrapers._buscar_pagina")
    def test_max_paginas_customizado_e_respeitado(self, mock_buscar):
        mock_buscar.return_value = (
            '<html><a href="/imovel/casa-a-venda-2-quartos-sp-mogi-das-cruzes-'
            'RS500000/id-1/">Casa</a></html>',
            "OK",
        )
        # Pedindo explicitamente só 1 página, mesmo tendo tipo específico
        # (que por padrão iria até 5)
        scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo="Casa", max_paginas=1)
        self.assertEqual(mock_buscar.call_count, 1)

    @patch("scrapers._buscar_pagina")
    def test_combina_resultados_de_varias_categorias_sem_duplicar(self, mock_buscar):
        # Cada categoria devolve um imóvel DIFERENTE — o total deve somar
        # os 2, sem duplicar (o link é distinto em cada resposta simulada).
        html_casa = (
            '<html><a href="/imovel/casa-a-venda-2-quartos-sp-mogi-das-cruzes-'
            'RS500000/id-1111/">Casa</a></html>'
        )
        html_apto = (
            '<html><a href="/imovel/apartamento-a-venda-2-quartos-sp-mogi-das-cruzes-'
            'RS300000/id-2222/">Apto</a></html>'
        )
        respostas = {
            "casas-a-venda": (html_casa, "OK"),
            "apartamentos-a-venda": (html_apto, "OK"),
            "casas-em-condominio-a-venda": (None, "HTTP 403"),
            "terrenos-a-venda": (None, "HTTP 403"),
        }

        def _fake(url, marcador_de_conteudo=None):
            for slug, resposta in respostas.items():
                if slug in url:
                    return resposta
            return (None, "não mapeado")

        mock_buscar.side_effect = _fake

        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP", tipo=None)

        self.assertEqual(len(resultado), 2)
        precos = sorted(im.preco for im in resultado)
        self.assertEqual(precos, [300000.0, 500000.0])

    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_de_link_relativo(self, mock_buscar):
        html_relativo = (
            '<html><body>'
            '<a href="/imovel/casa-a-venda-2-quartos-com-garagem-sp-mogi-das-cruzes-'
            'jardim-sao-francisco-RS800000/id-41546364/">Casa</a>'
            '</body></html>'
        )
        mock_buscar.return_value = (html_relativo, "OK")
        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(len(resultado), 1)
        self.assertTrue(resultado[0].link.startswith("https://www.chavesnamao.com.br/"))
        self.assertEqual(resultado[0].preco, 800000.0)

    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_de_link_embutido_em_json_com_barras_escapadas(self, mock_buscar):
        # Simula o link aparecendo dentro de um bloco de dados JSON (comum
        # em sites Next.js/React), com barras escapadas e aspas simples.
        html_json = (
            '<html><body><script>'
            'var dados = {"url": "\\/imovel\\/casa-a-venda-2-quartos-sp-mogi-das-cruzes-'
            'jardim-sao-francisco-RS800000\\/id-41546364\\/"};'
            '</script></body></html>'
        )
        mock_buscar.return_value = (html_json, "OK")
        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].preco, 800000.0)
        self.assertIn("padrão amplo", diagnosticos[0])

    @patch("scrapers._buscar_pagina")
    def test_diagnostico_quando_nao_acha_nenhum_link(self, mock_buscar):
        html_sem_imoveis = "<html><body><p>Página genérica sem anúncios</p></body></html>"
        mock_buscar.return_value = (html_sem_imoveis, "HTTP 200 OK (40 caracteres)")
        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(resultado, [])
        self.assertIn("nenhuma ocorrência", diagnosticos[0])

    @patch("scrapers._buscar_pagina")
    def test_diagnostico_mostra_trecho_quando_pista_existe_mas_padrao_nao_bate(self, mock_buscar):
        html_com_pista_mas_formato_novo = (
            '<html><body><a data-url="/imovel/algum-formato-totalmente-novo-99999">'
            "Casa</a></body></html>"
        )
        mock_buscar.return_value = (html_com_pista_mas_formato_novo, "HTTP 200 OK")
        resultado, diagnosticos = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(resultado, [])
        self.assertIn("Trecho real encontrado", diagnosticos[0])


class TestBuscarPaginaComCamadas(unittest.TestCase):
    """Testa a lógica de 3 camadas de _buscar_pagina: requests normal ->
    cloudscraper (em caso de 403/429) -> Playwright (último recurso, ou
    quando o conteúdo vem sem o marcador esperado)."""

    @patch("requests.get")
    def test_sucesso_direto_nao_aciona_playwright(self, mock_get):
        resposta = unittest.mock.Mock(status_code=200, text="x" * 3000 + "/imovel/abc")
        mock_get.return_value = resposta
        with patch("scrapers._buscar_pagina_playwright") as mock_pw:
            html, diag = scrapers._buscar_pagina("http://x", marcador_de_conteudo="/imovel/")
            mock_pw.assert_not_called()
        self.assertIsNotNone(html)
        self.assertIn("HTTP 200 OK", diag)

    @patch("scrapers._buscar_pagina_playwright")
    @patch("requests.get")
    def test_conteudo_sem_marcador_aciona_playwright(self, mock_get, mock_pw):
        # HTTP 200 "vazio" (sem o marcador esperado) -> deve tentar Playwright
        resposta = unittest.mock.Mock(status_code=200, text="x" * 3000)
        mock_get.return_value = resposta
        mock_pw.return_value = ("y" * 3000 + "/imovel/real", "http://x -> Playwright OK")

        html, diag = scrapers._buscar_pagina("http://x", marcador_de_conteudo="/imovel/")

        mock_pw.assert_called_once()
        self.assertIsNotNone(html)
        self.assertIn("/imovel/real", html)
        self.assertIn("Playwright", diag)

    @patch("scrapers._buscar_pagina_playwright")
    @patch("requests.get")
    def test_403_aciona_playwright_quando_cloudscraper_nao_resolve(self, mock_get, mock_pw):
        resposta_bloqueada = unittest.mock.Mock(status_code=403, text="bloqueado")
        mock_get.return_value = resposta_bloqueada
        mock_pw.return_value = (None, "http://x -> Playwright falhou: timeout")

        html, diag = scrapers._buscar_pagina("http://x", marcador_de_conteudo="/imovel/")

        mock_pw.assert_called_once()
        self.assertIsNone(html)
        self.assertIn("403", diag)
        self.assertIn("Playwright também falhou", diag)

    @patch("scrapers._buscar_pagina_playwright")
    @patch("requests.get")
    def test_403_resolvido_pelo_playwright(self, mock_get, mock_pw):
        resposta_bloqueada = unittest.mock.Mock(status_code=403, text="bloqueado")
        mock_get.return_value = resposta_bloqueada
        mock_pw.return_value = ("z" * 3000 + "/imovel/ok", "http://x -> Playwright OK")

        html, diag = scrapers._buscar_pagina("http://x", marcador_de_conteudo="/imovel/")

        self.assertIsNotNone(html)
        self.assertIn("/imovel/ok", html)
        self.assertIn("Playwright (navegador real) conseguiu obter os dados", diag)

    def test_playwright_nao_instalado_nao_quebra(self):
        # Se a biblioteca playwright não estiver instalada no ambiente, a
        # busca deve reportar isso no diagnóstico em vez de travar com erro.
        html, diag = scrapers._buscar_pagina_playwright("http://x")
        # Neste ambiente de teste o pacote pode ou não estar instalado —
        # o importante é que a função NUNCA lance uma exceção não tratada.
        self.assertIsInstance(diag, str)


class TestOrcamentoDeTempo(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_para_de_paginar_apos_estourar_orcamento_de_tempo(self, mock_buscar):
        # Simula cada chamada demorando "tempo real" o suficiente para
        # estourar o orçamento logo na 2ª chamada, usando time.sleep curto
        # + um orçamento reduzido via monkeypatch para o teste ser rápido.
        import scrapers as scrapers_mod

        chamadas = {"n": 0}

        def _fake(url, marcador_de_conteudo=None):
            chamadas["n"] += 1
            html = (
                f'<html><a href="/imovel/casa-a-venda-2-quartos-sp-mogi-das-cruzes-'
                f'RS{500000 + chamadas["n"]}/id-{9000 + chamadas["n"]}/">Casa</a></html>'
            )
            return html, "OK"

        mock_buscar.side_effect = _fake

        # Reduz drasticamente o "relógio" percebido pela função: a 1ª
        # chamada de time.time() é o início, a 2ª já estoura o orçamento.
        tempos = iter([1000.0, 1000.0, 1000.0 + 999])
        with patch("scrapers.time.time", side_effect=lambda: next(tempos, 1000.0 + 999)):
            resultado, diagnosticos = scrapers_mod.buscar_chavesnamao(
                "Mogi das Cruzes", "SP", tipo="Casa", max_paginas=5
            )

        # Deve ter parado bem antes das 5 páginas por causa do orçamento
        self.assertLess(mock_buscar.call_count, 5)
        self.assertTrue(any("limite de tempo" in d for d in diagnosticos))


class TestNavegadorPlaywrightCompartilhado(unittest.TestCase):
    def setUp(self):
        scrapers._PLAYWRIGHT_GLOBAL = None
        scrapers._NAVEGADOR_GLOBAL = None

    def tearDown(self):
        scrapers._PLAYWRIGHT_GLOBAL = None
        scrapers._NAVEGADOR_GLOBAL = None

    def test_reaproveita_navegador_entre_chamadas(self):
        navegador_fake = unittest.mock.Mock()
        navegador_fake.is_connected.return_value = True

        playwright_fake = unittest.mock.Mock()
        playwright_fake.chromium.launch.return_value = navegador_fake

        with patch("playwright.sync_api.sync_playwright") as mock_sp:
            mock_sp.return_value.start.return_value = playwright_fake

            nav1 = scrapers._obter_navegador_playwright_compartilhado()
            nav2 = scrapers._obter_navegador_playwright_compartilhado()

        # A 2ª chamada deve devolver o MESMO navegador, sem abrir outro
        self.assertIs(nav1, nav2)
        playwright_fake.chromium.launch.assert_called_once()

    def test_abre_novo_navegador_se_o_antigo_caiu(self):
        navegador_morto = unittest.mock.Mock()
        navegador_morto.is_connected.side_effect = Exception("conexão perdida")

        navegador_novo = unittest.mock.Mock()
        navegador_novo.is_connected.return_value = True

        playwright_fake = unittest.mock.Mock()
        playwright_fake.chromium.launch.side_effect = [navegador_morto, navegador_novo]

        with patch("playwright.sync_api.sync_playwright") as mock_sp:
            mock_sp.return_value.start.return_value = playwright_fake

            nav1 = scrapers._obter_navegador_playwright_compartilhado()
            nav2 = scrapers._obter_navegador_playwright_compartilhado()

        self.assertIsNot(nav1, nav2)
        self.assertEqual(playwright_fake.chromium.launch.call_count, 2)


class TestAutoinstalacaoPlaywright(unittest.TestCase):
    def setUp(self):
        # Reseta a flag de "já tentei instalar" antes de cada teste, já
        # que ela é global (persiste entre chamadas dentro do mesmo
        # processo, de propósito, para não tentar toda hora).
        scrapers._PLAYWRIGHT_INSTALACAO_TENTADA = False

    def tearDown(self):
        scrapers._PLAYWRIGHT_INSTALACAO_TENTADA = False

    @patch("subprocess.run")
    def test_instalacao_bem_sucedida(self, mock_run):
        mock_run.return_value = unittest.mock.Mock(stdout="Chromium instalado", returncode=0)
        resultado = scrapers._garantir_navegador_playwright_instalado()
        self.assertIn("sucesso", resultado)
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_instalacao_falha_nao_lanca_excecao(self, mock_run):
        mock_run.side_effect = Exception("sem espaço em disco")
        resultado = scrapers._garantir_navegador_playwright_instalado()
        self.assertIn("falhou", resultado)
        self.assertIn("sem espaço em disco", resultado)

    @patch("subprocess.run")
    def test_so_tenta_instalar_uma_vez_por_processo(self, mock_run):
        mock_run.return_value = unittest.mock.Mock(stdout="ok", returncode=0)
        scrapers._garantir_navegador_playwright_instalado()
        resultado2 = scrapers._garantir_navegador_playwright_instalado()
        mock_run.assert_called_once()  # a 2ª chamada não deve rodar de novo
        self.assertIn("já foi tentada", resultado2)


class TestDiagnosticarAusenciaDeLinks(unittest.TestCase):
    def test_sem_nenhuma_ocorrencia(self):
        msg = scrapers._diagnosticar_ausencia_de_links("<html>nada aqui</html>", "/imovel/")
        self.assertIn("nenhuma ocorrência", msg)

    def test_com_ocorrencia_mostra_trecho(self):
        html = "<html>" + "x" * 50 + "/imovel/abc-XYZ" + "y" * 50 + "</html>"
        msg = scrapers._diagnosticar_ausencia_de_links(html, "/imovel/")
        self.assertIn("aparece 1x", msg)
        self.assertIn("Trecho real encontrado", msg)


class TestZap(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_da_janela_de_texto(self, mock_buscar):
        mock_buscar.side_effect = _mock_buscar_pagina_sequencia(
            [HTML_ZAP_SINTETICO, None, None]
        )
        resultado, diagnosticos = scrapers.buscar_zap("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 1)
        im = resultado[0]
        self.assertEqual(im.preco, 650000.0)
        self.assertIn(im.quartos, (2, 3))
        self.assertEqual(im.banheiros, 2)

    @patch("scrapers._buscar_pagina")
    def test_deduplicacao_de_links(self, mock_buscar):
        html_duplicado = HTML_ZAP_SINTETICO + HTML_ZAP_SINTETICO
        mock_buscar.side_effect = _mock_buscar_pagina_sequencia(
            [html_duplicado, None, None]
        )
        resultado, diagnosticos = scrapers.buscar_zap("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 1)


class TestImovelweb(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_da_janela_de_texto(self, mock_buscar):
        mock_buscar.side_effect = _mock_buscar_pagina_sequencia(
            [HTML_IMOVELWEB_SINTETICO, None, None]
        )
        resultado, diagnosticos = scrapers.buscar_imovelweb("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 1)
        im = resultado[0]
        self.assertEqual(im.preco, 350000.0)
        self.assertEqual(im.area_m2, 60.0)
        self.assertEqual(im.quartos, 2)
        self.assertEqual(im.banheiros, 1)


class TestFiltros(unittest.TestCase):
    def test_filtro_preco_e_quartos(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Casa A", link="a", preco=200000, quartos=2),
            scrapers.Imovel(portal="X", titulo="Casa B", link="b", preco=800000, quartos=4),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=3, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None,
        )
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].titulo, "Casa B")

    def test_imovel_com_dado_faltante_nao_e_descartado(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Terreno", link="c", preco=None, quartos=None,
                             dados_incompletos=True),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=2, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None,
        )
        self.assertEqual(len(resultado), 1)

    def test_filtro_quartos_maximo(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Casa A", link="a", quartos=2),
            scrapers.Imovel(portal="X", titulo="Casa B", link="b", quartos=3),
            scrapers.Imovel(portal="X", titulo="Casa C", link="c", quartos=5),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=None, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None, quartos_max=3,
        )
        titulos = sorted(im.titulo for im in resultado)
        self.assertEqual(titulos, ["Casa A", "Casa B"])

    def test_filtro_quartos_min_e_max_juntos(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Casa A", link="a", quartos=1),
            scrapers.Imovel(portal="X", titulo="Casa B", link="b", quartos=3),
            scrapers.Imovel(portal="X", titulo="Casa C", link="c", quartos=6),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=2, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None, quartos_max=4,
        )
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].titulo, "Casa B")

    def test_filtro_banheiros_maximo(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Casa A", link="a", banheiros=1),
            scrapers.Imovel(portal="X", titulo="Casa B", link="b", banheiros=4),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=None, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None, banheiros_max=2,
        )
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].titulo, "Casa A")

    def test_filtro_area_maxima(self):
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Apto pequeno", link="a", area_m2=45),
            scrapers.Imovel(portal="X", titulo="Apto grande", link="b", area_m2=250),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=None, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None, area_max=100,
        )
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].titulo, "Apto pequeno")

    def test_dado_ausente_nao_e_descartado_pelo_filtro_maximo(self):
        # Um imóvel sem número de quartos identificado não deve ser
        # descartado só porque um filtro MÁXIMO foi definido — a regra é a
        # mesma já usada para os filtros mínimos: dado ausente = não sabemos,
        # então não excluímos por precaução.
        imoveis = [
            scrapers.Imovel(portal="X", titulo="Terreno", link="c", quartos=None),
        ]
        resultado = scrapers._aplica_filtros_basicos(
            imoveis, tipo=None, quartos_min=None, banheiros_min=None,
            area_min=None, preco_min=None, preco_max=None, quartos_max=2,
        )
        self.assertEqual(len(resultado), 1)


class TestBuscarTodosPortais(unittest.TestCase):
    @patch("scrapers.buscar_chavesnamao")
    @patch("scrapers.buscar_imovelweb")
    @patch("scrapers.buscar_zap")
    @patch("scrapers.buscar_vivareal")
    def test_portais_desativados_nao_sao_chamados(
        self, mock_vr, mock_zap, mock_iw, mock_cnm
    ):
        """Por padrão, só o Chaves na Mão está ativo (PORTAIS_ATIVOS). Os
        outros 3 (bloqueio confirmado) não devem nem ser chamados — isso
        economiza tempo e evita esgotar o limite de requisição do Render."""
        mock_cnm.return_value = (
            [scrapers.Imovel(portal="Chaves na Mão", titulo="Casa", link="c1", preco=300000)],
            ["https://chavesnamao... -> HTTP 200 OK — 1 links de imóveis encontrados"],
        )

        resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        mock_vr.assert_not_called()
        mock_zap.assert_not_called()
        mock_iw.assert_not_called()
        mock_cnm.assert_called_once()

        self.assertEqual(resultado["total"], 1)
        self.assertNotIn("VivaReal", resultado["erros"])  # desativado ≠ erro
        self.assertIn("desativado", resultado["diagnosticos_por_portal"]["VivaReal"][0])
        self.assertIn("desativado", resultado["diagnosticos_por_portal"]["ZAP Imóveis"][0])
        self.assertIn("desativado", resultado["diagnosticos_por_portal"]["Imovelweb"][0])

    @patch("scrapers.buscar_chavesnamao")
    def test_chaves_na_mao_bloqueado_gera_diagnostico_claro(self, mock_cnm):
        """Simula o cenário relatado pelo usuário: mesmo o único portal
        ativo (Chaves na Mão) volta bloqueado. O diagnóstico deve deixar
        isso claro, não apenas devolver uma lista vazia muda."""
        mock_cnm.return_value = (
            [], ["https://chavesnamao... -> HTTP 403 (provável bloqueio antirrobô)"]
        )

        resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        self.assertEqual(resultado["total"], 0)
        self.assertIn("Chaves na Mão", resultado["erros"])
        self.assertIn("403", resultado["erros"]["Chaves na Mão"])

    @patch("scrapers.buscar_chavesnamao")
    @patch("scrapers.buscar_vivareal")
    def test_reativar_um_portal_manualmente(self, mock_vr, mock_cnm):
        """Confirma que o interruptor PORTAIS_ATIVOS realmente controla
        quem é chamado — reativando o VivaReal temporariamente só para
        este teste, sem afetar o padrão dos outros testes."""
        mock_vr.return_value = (
            [scrapers.Imovel(portal="VivaReal", titulo="Casa", link="v1", preco=400000)],
            ["https://vivareal... -> HTTP 200 OK — 1 links de imóveis encontrados"],
        )
        mock_cnm.return_value = ([], ["https://chavesnamao... -> HTTP 403 (bloqueio)"])

        with patch.dict(scrapers.PORTAIS_ATIVOS, {"VivaReal": True}):
            resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        mock_vr.assert_called_once()
        self.assertEqual(len(resultado["resultados_por_portal"]["VivaReal"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
