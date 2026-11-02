# -*- coding: utf-8 -*-
"""
jobs.py

Executa as buscas EM SEGUNDO PLANO, uma de cada vez, numa única thread.

Por que isso existe:
- O Render derruba qualquer requisição que passe de ~100 s. Antes, a busca
  inteira rodava dentro da requisição do formulário; se demorasse, o usuário
  via "Internal Server Error" e perdia tudo. Agora o formulário só cria uma
  "tarefa" (resposta imediata) e a tela de espera consulta o andamento.
- O Playwright (modo síncrono) NÃO pode ser usado por várias threads ao mesmo
  tempo. Uma única thread de trabalho resolve isso e deixa as buscas em fila
  em vez de brigar pelo mesmo navegador.

Limitações assumidas (plano gratuito, 1 worker):
- As tarefas ficam só na memória. Se o Render reiniciar/dormir, elas somem —
  quem abrir o link antigo vê "busca expirada" e refaz a busca.
- Se uma busca travar de vez dentro do navegador, a fila para até ela
  terminar. Os limites de tempo do scraper existem para evitar isso.
"""

import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

MAX_NA_FILA = 5            # acima disso, recusa novas buscas ("servidor ocupado")
TTL_CONCLUIDA_S = 30 * 60  # quanto tempo o resultado fica disponível
LIMITE_EXECUCAO_S = 170    # depois disso a tela de espera mostra erro de tempo

FILA = "fila"
EXECUTANDO = "executando"
CONCLUIDO = "concluido"
ERRO = "erro"


class ServidorOcupado(Exception):
    pass


class Gerenciador:
    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._fila: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # -- thread de trabalho ------------------------------------------------
    def _garantir_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="worker-buscas", daemon=True)
                self._thread.start()

    def _loop(self):
        while True:
            job_id = self._fila.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                job["estado"] = EXECUTANDO
                job["iniciado"] = time.time()
                funcao, kwargs = job["_funcao"], job["_kwargs"]
            try:
                resultado = funcao(**kwargs)
                with self._lock:
                    # se a tela já marcou "tempo excedido", não sobrescreve
                    if job["estado"] == EXECUTANDO:
                        job["resultado"] = resultado
                        job["estado"] = CONCLUIDO
            except Exception as exc:  # nunca deixar a thread morrer
                logger.exception("Erro na tarefa %s", job_id)
                with self._lock:
                    if job["estado"] == EXECUTANDO:
                        job["erro"] = f"{type(exc).__name__}: {exc}"
                        job["estado"] = ERRO
            finally:
                with self._lock:
                    job["terminado"] = time.time()
                    job.pop("_funcao", None)
                    job.pop("_kwargs", None)
                self._limpar_antigas()

    def _limpar_antigas(self):
        agora = time.time()
        with self._lock:
            velhas = [
                i for i, j in self._jobs.items()
                if j.get("terminado") and agora - j["terminado"] > TTL_CONCLUIDA_S
            ]
            for i in velhas:
                del self._jobs[i]

    # -- API pública ---------------------------------------------------------
    def enviar(self, funcao: Callable, meta: Optional[dict] = None, **kwargs) -> str:
        """Coloca uma tarefa na fila e devolve o id. Levanta ServidorOcupado
        se a fila já estiver cheia."""
        with self._lock:
            pendentes = sum(1 for j in self._jobs.values() if j["estado"] in (FILA, EXECUTANDO))
            if pendentes >= MAX_NA_FILA:
                raise ServidorOcupado(
                    "Há muitas buscas em andamento no momento. Aguarde um minuto e tente de novo."
                )
            job_id = uuid.uuid4().hex[:16]
            self._jobs[job_id] = {
                "id": job_id, "estado": FILA, "criado": time.time(),
                "iniciado": None, "terminado": None,
                "resultado": None, "erro": None, "meta": meta or {},
                "_funcao": funcao, "_kwargs": kwargs,
            }
        self._garantir_thread()
        self._fila.put(job_id)
        return job_id

    def consultar(self, job_id: str) -> Optional[dict]:
        """Cópia segura do estado da tarefa, ou None se não existe (expirou)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            # tempo excedido: a tela deixa de esperar para sempre
            limite = job["meta"].get("limite_s", LIMITE_EXECUCAO_S)   # tarefas longas (coleta) definem o seu
            if job["estado"] == EXECUTANDO and job["iniciado"] \
                    and time.time() - job["iniciado"] > limite:
                job["estado"] = ERRO
                job["erro"] = (
                    f"A busca passou de {limite}s e foi abandonada. "
                    "Tente de novo com filtros mais específicos (ex.: escolha o tipo do imóvel)."
                )
            posicao = 0
            if job["estado"] == FILA:
                anteriores = [j for j in self._jobs.values()
                              if j["estado"] == FILA and j["criado"] < job["criado"]]
                emexec = sum(1 for j in self._jobs.values() if j["estado"] == EXECUTANDO)
                posicao = len(anteriores) + emexec + 1
            copia = {k: v for k, v in job.items() if not k.startswith("_")}
            copia["posicao"] = posicao
            copia["decorrido"] = round(time.time() - (job["iniciado"] or job["criado"]), 1)
            return copia


gerenciador = Gerenciador()
