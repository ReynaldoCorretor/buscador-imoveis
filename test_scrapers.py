# -*- coding: utf-8 -*-
"""
test_scrapers.py

Testes offline dos parsers de regex, usando HTML sintético (não faz
requisições reais à internet). Isso valida a LÓGICA de extração; o
comportamento contra os sites reais só pode ser confirmado após o deploy
(ver README.md), pois este ambiente de desenvolvimento não tem acesso
direto à internet fora das ferramentas de busca.

Rodar com: python test_scrapers.py
"""

import unittest
from unittest.mock import patch

import scrapers


HTML_VIVAREAL_SINTETICO = """
<html><body>
<a href="https://www.vivareal.com.br/imovel/casa-2-quartos-jardim-cintia-mogi-das-cruzes-com-garagem-100m2-venda-RS520000-id-2903134721/">Casa</a>
<a href="https://www.vivareal.com.br/imovel/apartamento-3-quartos-centro-mogi-das-cruzes-com-garagem-116m2-venda-RS980000-id-2874542204/">Apto</a>
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


class TestVivaReal(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_do_slug(self, mock_buscar):
        mock_buscar.side_effect = [HTML_VIVAREAL_SINTETICO, None, None]
        resultado = scrapers.buscar_vivareal("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 2)

        casa = next(im for im in resultado if "casa" in im.link)
        self.assertEqual(casa.preco, 520000.0)
        self.assertEqual(casa.area_m2, 100.0)
        self.assertEqual(casa.quartos, 2)
        self.assertFalse(casa.dados_incompletos)

    @patch("scrapers._buscar_pagina")
    def test_pagina_vazia_nao_quebra(self, mock_buscar):
        mock_buscar.return_value = HTML_VAZIO
        resultado = scrapers.buscar_vivareal("Cidade Fake", "XX", max_paginas=1)
        self.assertEqual(resultado, [])

    @patch("scrapers._buscar_pagina")
    def test_falha_de_rede_nao_quebra(self, mock_buscar):
        mock_buscar.return_value = None
        resultado = scrapers.buscar_vivareal("Mogi das Cruzes", "SP", max_paginas=2)
        self.assertEqual(resultado, [])


class TestChavesNaMao(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_do_slug(self, mock_buscar):
        mock_buscar.return_value = HTML_CHAVESNAMAO_SINTETICO
        resultado = scrapers.buscar_chavesnamao("Mogi das Cruzes", "SP")
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0].preco, 800000.0)
        self.assertEqual(resultado[0].quartos, 2)


class TestZap(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_da_janela_de_texto(self, mock_buscar):
        mock_buscar.side_effect = [HTML_ZAP_SINTETICO, None, None]
        resultado = scrapers.buscar_zap("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 1)
        im = resultado[0]
        self.assertEqual(im.preco, 650000.0)
        self.assertIn(im.quartos, (2, 3))  # pode vir do link (125m2->id tem "3-quartos" no link)
        self.assertEqual(im.banheiros, 2)

    @patch("scrapers._buscar_pagina")
    def test_deduplicacao_de_links(self, mock_buscar):
        html_duplicado = HTML_ZAP_SINTETICO + HTML_ZAP_SINTETICO
        mock_buscar.side_effect = [html_duplicado, None, None]
        resultado = scrapers.buscar_zap("Mogi das Cruzes", "SP", max_paginas=3)
        self.assertEqual(len(resultado), 1)


class TestImovelweb(unittest.TestCase):
    @patch("scrapers._buscar_pagina")
    def test_extrai_dados_da_janela_de_texto(self, mock_buscar):
        mock_buscar.side_effect = [HTML_IMOVELWEB_SINTETICO, None, None]
        resultado = scrapers.buscar_imovelweb("Mogi das Cruzes", "SP", max_paginas=3)
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
        # dado incompleto deve aparecer, não sumir, salvo quando o filtro
        # explicitamente exclui por causa do dado ausente ser None (que
        # neste código conta como "não sabemos, não excluir")
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
        mock_zap.return_value = [
            scrapers.Imovel(portal="ZAP Imóveis", titulo="Casa", link="z1", preco=300000)
        ]
        mock_iw.return_value = []
        mock_cnm.return_value = []

        resultado = scrapers.buscar_todos_portais("Mogi das Cruzes", "SP")

        self.assertIn("VivaReal", resultado["erros"])
        self.assertEqual(resultado["resultados_por_portal"]["VivaReal"], [])
        self.assertEqual(len(resultado["resultados_por_portal"]["ZAP Imóveis"]), 1)
        self.assertEqual(resultado["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
