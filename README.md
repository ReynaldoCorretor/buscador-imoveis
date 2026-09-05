# Buscador de Imóveis

Ferramenta pessoal para buscar imóveis à venda em **VivaReal, ZAP Imóveis,
Imovelweb e Chaves na Mão** a partir de um único formulário (cidade, UF,
tipo, dormitórios, banheiros, área e faixa de preço). Todos os resultados
aparecem numa única página, com link direto para o anúncio original, e
podem ser filtrados/ordenados no próprio navegador sem precisar buscar de
novo.

## Como publicar (passo a passo, sem precisar saber programar)

### 1. Criar uma conta no Render.com
1. Acesse [render.com](https://render.com) e clique em **Get Started**.
2. Crie a conta (pode usar login do Google/GitHub para facilitar).

### 2. Subir este projeto para o GitHub
1. Crie uma conta em [github.com](https://github.com), se ainda não tiver.
2. Crie um novo repositório (botão **New**), por exemplo `buscador-imoveis`.
3. Faça upload de **todos os arquivos e pastas deste pacote** (mantendo a
   pasta `templates/` como está) usando a opção "uploading an existing
   file" na página do repositório.

### 3. Conectar o Render ao repositório
1. No painel do Render, clique em **New +** → **Web Service**.
2. Selecione **Build and deploy from a Git repository** e conecte sua
   conta do GitHub.
3. Escolha o repositório `buscador-imoveis` que você acabou de criar.
4. O Render vai detectar automaticamente o arquivo `render.yaml` deste
   pacote e preencher as configurações sozinho (ambiente Python, comando
   de build e de start). Se pedir para confirmar manualmente:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
5. Escolha o plano **Free**.
6. Clique em **Create Web Service**.

### 4. Aguardar o deploy
O Render vai instalar as dependências e publicar o site — isso leva
alguns minutos na primeira vez. Quando terminar, você verá uma URL do
tipo `https://buscador-imoveis.onrender.com` — esse é o endereço do seu
app, já pronto para usar.

**Observação sobre o plano gratuito do Render**: no plano free, o serviço
"dorme" depois de um tempo sem uso e demora ~30-50 segundos para acordar
na primeira visita do dia. Isso é normal e não indica problema.

## Estrutura do projeto

```
buscador-imoveis/
├── app.py                    # aplicação Flask (rotas / e /buscar)
├── scrapers.py                # busca e extração de dados dos 4 portais
├── test_scrapers.py           # testes automatizados (HTML sintético)
├── templates/
│   ├── index.html              # formulário de busca
│   └── resultados.html         # lista de resultados + filtros em JS
├── requirements.txt
├── Procfile
└── render.yaml
```

## Como funciona a busca

1. Você preenche o formulário na página inicial.
2. O servidor consulta as primeiras páginas de cada um dos 4 portais.
3. Os dados de cada imóvel (preço, área, quartos, banheiros) são extraídos
   por padrões de texto (regex) a partir do link do anúncio e do HTML ao
   redor dele — não é feita nenhuma cópia de fotos ou textos completos dos
   anúncios, apenas o link para o anúncio original.
4. Todos os resultados (dos 4 portais juntos) aparecem numa única página.
   Você pode filtrar por portal, preço máximo, dormitórios mínimos e
   ordenar por preço/área — tudo isso acontece no seu navegador, sem
   precisar buscar de novo no servidor.
5. Imóveis cujo preço, área ou nº de quartos não pôde ser identificado
   automaticamente **não são descartados** — aparecem marcados como
   "dados incompletos", e você pode conferir esses detalhes clicando no
   anúncio original.

## Limitações conhecidas

- A busca cobre as primeiras 2-3 páginas de resultados de cada portal
  (ordenadas por relevância, não por "mais recente"). Não há garantia de
  cobertura de 100% dos milhares de anúncios disponíveis por cidade.
- O **Chaves na Mão** só permite consultar a página 1 dos resultados,
  porque a paginação desse portal é bloqueada pelo `robots.txt` do site.
- Os padrões de extração (regex) foram validados com HTML sintético em
  `test_scrapers.py` (13 testes, todos passando). O comportamento contra
  os sites reais só pode ser confirmado depois do deploy.
- **Bloqueio antirrobô**: portais grandes como VivaReal, ZAP e Imovelweb
  costumam ter proteção contra acessos automatizados, que pode bloquear
  pedidos vindos de servidores como o do Render (mesmo que o site
  funcione normalmente num navegador comum). Se isso acontecer, a página
  de resultados mostra 0 imóveis, mas o **"Diagnóstico técnico desta
  busca"** no final da página revela o motivo exato (código de erro HTTP,
  timeout etc.) — copie esse texto e envie para ajuste, se precisar.

## Se a busca voltar a dar 0 resultados

1. Abra o resultado da busca e clique em **"Diagnóstico técnico desta
   busca"**, no final da página.
2. Veja o que aparece para cada portal:
   - `HTTP 200 OK — N links encontrados`: o portal respondeu normalmente.
     Se mesmo assim não apareceu nenhum imóvel, o layout do site pode ter
     mudado (padrão de extração desatualizado).
   - `HTTP 403` ou `HTTP 429`: o portal bloqueou o pedido do servidor
     (proteção antirrobô). Isso é uma limitação do plano gratuito/servidor
     compartilhado, difícil de contornar sem soluções mais caras
     (ex: navegador automatizado ou serviço de proxy pago).
   - `erro de conexão` ou `tempo esgotado`: problema de rede pontual —
     tente buscar de novo.
3. Copie o texto do diagnóstico e envie para quem mantém o projeto — isso
   acelera bastante a correção, pois mostra exatamente o que o servidor
   recebeu de cada portal.

## Rodando os testes localmente (opcional)

Se quiser verificar a lógica de extração antes de publicar:

```bash
pip install -r requirements.txt
python test_scrapers.py
```

Isso roda os testes com HTML sintético (sem acessar a internet) e mostra
se cada parser está extraindo os dados corretamente.

## Rodando localmente (opcional, para quem tem Python instalado)

```bash
pip install -r requirements.txt
python app.py
```

Depois acesse `http://localhost:5000` no navegador.
