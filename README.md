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

## Status atual (atualizado após 2ª rodada de testes reais)

Depois da correção anterior (link relativo), o Chaves na Mão continuou
retornando 0 imóveis. Investigando melhor, percebemos que a ferramenta usada
para inspecionar o site durante o desenvolvimento processa a página de forma
diferente do que o código Python real faz no servidor — por isso o formato
exato do link só pode ser confirmado observando o que o próprio servidor do
Render recebe de verdade.

Por isso, esta versão faz duas coisas:

1. **Reconhece o link em mais formatos**: além do formato clássico
   (`href="/imovel/..."`), agora também reconhece o link se ele estiver
   embutido em dados JSON da página (comum em sites feitos com Next.js/React,
   com barras "escapadas" tipo `\/imovel\/...`) ou com aspas simples.

2. **Autodiagnóstico mais preciso**: se mesmo assim não encontrar nenhum link,
   o "Diagnóstico técnico" da página agora mostra um **trecho real do HTML**
   recebido pelo servidor ao redor da palavra `/imovel/`. Isso permite
   descobrir o formato exato usado pelo site sem precisar de mais tentativas
   às cegas.

### Se o Chaves na Mão ainda voltar com 0 resultados

Abra o "Diagnóstico técnico" no fim da página de resultados e copie a
mensagem completa que aparecer para o Chaves na Mão — ela vai indicar uma
destas situações:

- **"nenhuma ocorrência de '/imovel/' encontrada"**: o site pode estar
  bloqueando este servidor de forma parecida com os outros 3 portais, ou
  mudou completamente a estrutura da URL dos anúncios.
- **"aparece Nx no HTML, mas não no formato esperado... Trecho real
  encontrado: ..."**: esse trecho mostra exatamente como o link aparece de
  verdade — é só me enviar esse texto que ajusto o padrão de busca com
  precisão, sem mais tentativa e erro.

## Status atual (foco só no Chaves na Mão + correção pendente no Render)

Duas mudanças importantes nesta versão:

### 1. Só o Chaves na Mão é pesquisado por padrão agora

VivaReal, ZAP Imóveis e Imovelweb têm bloqueio antirrobô confirmado (HTTP
403 persistente, mesmo com `cloudscraper` e com o navegador automatizado).
Continuar tentando os 3 a cada busca só consumia tempo à toa e aumentava o
risco de a busca inteira travar por demorar demais (o "Internal Server
Error" visto antes).

Por isso, esses 3 portais foram **desativados por padrão** (variável
`PORTAIS_ATIVOS` no topo da função `buscar_todos_portais`, em
`scrapers.py`). O código deles continua no projeto, pronto para ser
reativado (bastando trocar `False` por `True` nessa variável) se no futuro
isso deixar de ser um bloqueio — por exemplo, se um serviço pago de
scraping for adotado.

### 2. Correção necessária no painel do Render (ação manual sua)

O diagnóstico mostrou `Executable doesn't exist... Playwright was just
installed or updated` — ou seja, **o navegador Chromium nunca foi
instalado de verdade no servidor**, mesmo com o `render.yaml` pedindo isso.
A causa mais provável: se o serviço foi criado pelo assistente **"New Web
Service"** (em vez de **"Blueprint"**), o Render não relê o `render.yaml`
depois da criação — ele usa o "Build Command" salvo nas configurações do
serviço, que ainda deve estar com o comando antigo.

**Para corrigir:**
1. No painel do Render, abra o serviço → **Settings**.
2. Procure o campo **Build Command** e troque para:
   ```
   pip install -r requirements.txt && playwright install --with-deps chromium
   ```
3. Salve e vá em **Manual Deploy → Deploy latest commit** (ou "Clear build
   cache & deploy" para garantir que instale do zero).
4. Esse build vai demorar mais que o normal (o Chromium é pesado) — é
   esperado.

Depois dessa correção, o Chaves na Mão vai finalmente ser testado com o
navegador de verdade (a tentativa anterior falhou não porque o site
resistiu ao Playwright, mas porque o Playwright nem chegou a rodar).

## Status anterior (correção do "Internal Server Error")

Depois de ativar o Playwright, a busca chegou a mostrar uma tela genérica de
**"Internal Server Error"** depois de um tempo longo. A causa mais provável:
o Render tem um limite de tempo por requisição (por volta de 100 segundos)
que é aplicado *independente* da configuração do servidor — e a busca,
somando várias páginas de vários portais tentando o navegador automatizado
uma de cada vez, pode ter ultrapassado esse tempo.

Duas mudanças foram feitas para reduzir esse risco:

1. **Menos páginas por busca**: VivaReal, ZAP e Imovelweb agora buscam só a
   1ª página (antes eram 3). Isso reduz a cobertura de cada busca, mas
   aumenta muito a chance de ela terminar dentro do tempo permitido. Se
   quiser tentar mais páginas no futuro (arriscando mais timeouts), dá para
   ajustar isso no código (`scrapers.py`, parâmetro `max_paginas`).

2. **Página de erro com detalhes**: se mesmo assim algo inesperado
   acontecer no servidor durante a busca (erro de memória, travamento do
   navegador automatizado, etc.), agora aparece uma tela explicando que deu
   erro, com o texto técnico do problema — em vez da tela genérica e muda
   de antes. Se isso acontecer, copie o texto do erro e envie para ajuste.

### Se a busca ainda demorar demais ou continuar dando erro

Isso confirmaria que mesmo 1 página por portal, usando o navegador
automatizado, não cabe no tempo/memória do plano gratuito do Render. Nesse
ponto, as opções realistas passam a ser: (a) reduzir ainda mais o escopo
(por exemplo, tentar o navegador automatizado só no Chaves na Mão, que é o
único sem bloqueio HTTP explícito), ou (b) migrar para um serviço pago de
scraping ou um plano pago do Render com mais recursos.

## Status anterior (Playwright — navegador automatizado real)

Depois de confirmar que os 4 portais bloqueiam (ou disfarçam o conteúdo para)
pedidos HTTP simples, mesmo com cloudscraper, a busca agora usa como último
recurso um **navegador de verdade rodando escondido no servidor**
(Playwright + Chromium headless), que executa o JavaScript da página como um
navegador comum executaria. Essa é a tentativa mais forte disponível sem
recorrer a serviços pagos de scraping.

### Como funciona agora, em camadas (da mais rápida para a mais pesada)

1. Pedido HTTP comum (rápido, ~1 segundo).
2. Se bloqueado (403/429): tenta `cloudscraper`.
3. Se ainda assim falhar, OU se a página vier "OK" mas sem nenhum sinal de
   conteúdo real (o disfarce que vimos no Chaves na Mão): abre um Chromium
   invisível, carrega a página de verdade e pega o HTML já processado.

### Configuração adicional necessária no Render

Esta versão precisa que o Render instale o navegador Chromium durante o
build — isso já está configurado no `render.yaml` (`playwright install
--with-deps chromium`), mas deixa o build mais demorado (pode levar uns
5-10 minutos a mais na primeira publicação). O `Procfile`/`render.yaml`
também foram ajustados para dar mais tempo por busca (`--timeout 120`) e
usar só 1 processo (`--workers 1`), para não estourar a memória disponível
no plano gratuito.

### ⚠️ Riscos reais desta abordagem (leia antes de usar)

- **Memória**: o plano gratuito do Render tem só 512 MB de RAM. Um
  navegador Chromium, mesmo "enxuto", consome uma boa parte disso. Pode
  funcionar bem, mas também pode travar ou reiniciar o serviço em buscas
  mais pesadas. Se isso acontecer nos logs do Render (erros tipo "Out of
  memory" ou o serviço reiniciando sozinho), o próximo passo realista é
  migrar para um plano pago do Render com mais memória.
- **Lentidão**: abrir um navegador de verdade para cada página é bem mais
  lento que um pedido HTTP comum. A busca pode demorar bem mais que antes,
  principalmente se vários portais precisarem cair para essa camada ao
  mesmo tempo.
- **Sem garantia total**: proteções antirrobô mais fortes (Akamai,
  PerimeterX, DataDome) conseguem detectar navegadores automatizados
  mesmo assim. Esta é a tentativa mais robusta possível sem custo
  financeiro, mas "sem custo" não é sinônimo de "infalível".

### Se mesmo com Playwright continuar bloqueado

Abra o "Diagnóstico técnico" no fim da página de resultados. Se aparecer
`"Playwright falhou"` ou `"Playwright também falhou"` para os 4 portais,
significa que a proteção deles é mais forte do que essa tentativa
consegue superar — nesse caso, as opções que sobram são um serviço pago de
scraping (mais confiável, com custo mensal) ou aceitar cobertura parcial.

## Status dos 3 portais bloqueados originalmente (VivaReal, ZAP, Imovelweb)

Depois do primeiro deploy, testamos a busca ao vivo e encontramos dois problemas
diferentes, já corrigidos nesta versão:

1. **Chaves na Mão retornava 0 resultados mesmo com a página carregando (HTTP
   200)**: o site mudou para usar links relativos (`/imovel/...` em vez de
   `https://www.chavesnamao.com.br/imovel/...`), e o código só reconhecia links
   já completos. **Corrigido** — o portal deve voltar a funcionar normalmente.

2. **VivaReal, ZAP Imóveis e Imovelweb bloqueiam o servidor do Render com HTTP
   403** (proteção antirrobô contra acessos automatizados). Isso não é um bug no
   código — é uma barreira que esses portais colocam contra qualquer servidor
   fazendo pedidos automatizados, independente da ferramenta usada. Nesta
   versão foi adicionada uma tentativa de contorno de baixo custo (biblioteca
   `cloudscraper`, que resolve desafios simples de proteção estilo Cloudflare),
   mas **não há garantia de que vá funcionar** — portais desse porte costumam
   ter proteção mais robusta que isso.

### O que fazer se o bloqueio persistir

Depois de publicar esta versão, faça uma busca e abra o "Diagnóstico técnico"
no fim da página de resultados:

- Se aparecer `"cloudscraper conseguiu contornar"`: o portal voltou a
  funcionar, ótimo.
- Se continuar `"HTTP 403 ... tentativa com cloudscraper também falhou"`: o
  bloqueio é mais forte do que essa tentativa gratuita consegue resolver. As
  opções realistas nesse caso, em ordem de custo:
  1. **Aceitar cobertura parcial**: usar o app só com o Chaves na Mão
     funcionando de forma confiável (os outros 3 portais continuam tentando,
     mas podem falhar).
  2. **Serviço de scraping pago** (ex: ScraperAPI, ZenRows, Scrapfly, Bright
     Data): esses serviços mantêm infraestrutura própria para contornar
     proteções antirrobô. Custam a partir de alguns dólares/mês, mas são a
     forma mais confiável de resolver isso sem mudar a arquitetura do projeto.
  3. **Navegador automatizado (Playwright/Selenium)**: mais trabalhoso de
     configurar e mais pesado para rodar (pode não caber no plano gratuito do
     Render, que tem pouca memória disponível).

Se quiser seguir por uma dessas rotas, é só pedir — cada uma tem implicações
diferentes de custo e complexidade que vale conversar antes de implementar.

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
playwright install chromium
python app.py
```

Depois acesse `http://localhost:5000` no navegador.

**Nota**: o comando `playwright install chromium` baixa o navegador usado
como último recurso para superar bloqueios. Só precisa ser rodado uma vez.
