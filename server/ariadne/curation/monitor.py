"""Está o curador (o llama-server do Mac) a responder?

O Mac nem sempre está ligado. Em vez de cada job de curadoria bater à porta e adiar-se sozinho,
o monitor pergunta ao `/health` do servidor de vez em quando e o worker só reclama `curate_page`
enquanto ele responde. Os jobs ficam na fila, intactos e sem gastar tentativas, e o painel mostra
«Mac offline». Quando o Mac volta, o primeiro teste bem-sucedido liberta a fila.
"""

import threading
import time
from dataclasses import dataclass

import httpx

UP_TTL_SECONDS = 60.0  # com o Mac online, volta a perguntar daqui a um minuto
DOWN_TTL_SECONDS = 15.0  # offline: tenta mais depressa, para a fila arrancar logo que ele volte
PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class CuratorStatus:
    configured: bool
    online: bool | None  # None: sem CURATOR_URL, não há o que perguntar
    detail: str


def probe(url: str, client: httpx.Client | None = None) -> tuple[bool, str]:
    """Pergunta ao servidor se está pronto. Qualquer resposta HTTP que não seja um erro do
    servidor conta como online (o `/health` do llama-server é público); 503 é «a carregar»."""
    base = url.rstrip("/")
    owns = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS))
    try:
        response = http.get(f"{base}/health")
    except httpx.HTTPError as exc:
        return False, f"no response ({type(exc).__name__})"
    finally:
        if owns:
            http.close()
    if response.status_code == 503:
        return False, "the server is loading the model"
    if response.status_code >= 500:
        return False, f"the server answered HTTP {response.status_code}"
    return True, "responding"


class CuratorMonitor:
    """Uma consulta em cache ao curador, partilhada pelas threads do processo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._url = ""
        self._online: bool | None = None
        self._detail = ""
        self._checked = float("-inf")

    def status(self, url: str, *, force: bool = False) -> CuratorStatus:
        if not url:
            return CuratorStatus(False, None, "CURATOR_URL not set")
        with self._lock:
            ttl = UP_TTL_SECONDS if self._online else DOWN_TTL_SECONDS
            fresh = url == self._url and time.monotonic() - self._checked < ttl
            if not fresh or force:
                self._online, self._detail = probe(url)
                self._url, self._checked = url, time.monotonic()
            return CuratorStatus(True, self._online, self._detail)

    def mark_down(self, url: str, detail: str) -> None:
        """Um pedido ao curador acabou de falhar: não esperar pela próxima consulta."""
        with self._lock:
            self._url, self._online, self._detail = url, False, detail
            self._checked = time.monotonic()
