# Usamos a imagem oficial do Playwright, que já vem com o Chromium e TODAS
# as bibliotecas de sistema necessárias pré-instaladas. Isso resolve de
# raiz o problema enfrentado no Render com o modo "Web Service" comum, que
# não permite instalar essas dependências (exige sudo, que não é liberado
# nesse modo). Construindo a imagem via Docker, a instalação roda com
# permissão total durante o build — sem depender de nada do Render.
#
# IMPORTANTE: a tag da imagem (v1.55.0) precisa bater com a versão do
# pacote "playwright" no requirements.txt. Se atualizar uma, atualize a
# outra também.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

# Copia só o requirements.txt primeiro para aproveitar cache do Docker
# (só reinstala as dependências Python se esse arquivo mudar)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do projeto
COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 10000

# O Render injeta a porta de verdade na variável de ambiente $PORT — por
# isso usamos a forma "shell" do CMD, que permite essa substituição.
CMD ["/bin/sh", "-c", "gunicorn app:app --timeout 120 --workers 1 --bind 0.0.0.0:${PORT:-10000}"]
