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
    def _fake(url):
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


class TestBuscarTodosPortais(unittest.TestCase):
    @patch("scrapers.buscar_chavesnamao")
    @patch("scrapers.buscar_imovelweb")
    @patch("scrapers.buscar_zap")
    @patch("scrapers.buscar_vivareal")
    def test_um_portal_falhando_nao_derruba_os_outros(
        self, mock_vr, mock_zap, mock_iw, mock_cnm
    ):
        mock_vr.side_effect = Exception("Falha simulada no VivaReal")
        mock_zap.return_value = (
            [scrapers.Imovel(portal="ZAP Imóveis", titulo="Casa", link="z1", preco=300000)],
            ["https://zap... -> HTTP 200 OK — 1 links de imóveis encontrados nesta página"],
        )
        mock_iw.return_value = ([], ["https://imovelweb... -> HTTP 403 (bloqueio)"])
        mock_cnm.return_value = ([], ["https://chavesnamao... -> HTTP 200 OK — 0 links"])

        resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        self.assertIn("VivaReal", resultado["erros"])
        self.assertIn("Imovelweb", resultado["erros"])
        self.assertEqual(resultado["resultados_por_portal"]["VivaReal"], [])
        self.assertEqual(len(resultado["resultados_por_portal"]["ZAP Imóveis"]), 1)
        self.assertEqual(resultado["total"], 1)
        self.assertIn("diagnosticos_por_portal", resultado)
        self.assertIn("ZAP Imóveis", resultado["diagnosticos_por_portal"])

    @patch("scrapers.buscar_chavesnamao")
    @patch("scrapers.buscar_imovelweb")
    @patch("scrapers.buscar_zap")
    @patch("scrapers.buscar_vivareal")
    def test_todos_portais_bloqueados_gera_diagnostico_claro(
        self, mock_vr, mock_zap, mock_iw, mock_cnm
    ):
        """Simula o cenário relatado pelo usuário: 0 imóveis encontrados em
        todos os portais. O diagnóstico deve deixar claro que houve
        bloqueio, em vez de simplesmente devolver uma lista vazia muda."""
        resposta_bloqueio = ([], ["https://portal... -> HTTP 403 (provável bloqueio antirrobô)"])
        mock_vr.return_value = resposta_bloqueio
        mock_zap.return_value = resposta_bloqueio
        mock_iw.return_value = resposta_bloqueio
        mock_cnm.return_value = resposta_bloqueio

        resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        self.assertEqual(resultado["total"], 0)
        self.assertEqual(len(resultado["erros"]), 4)
        for nome, motivo in resultado["erros"].items():
            self.assertIn("403", motivo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
