"""O túnel do lote de GPU só existe enquanto há uma corrida que precisa dele.

O `cloudflared` corre dentro do contentor do gateway, filho deste supervisor. A cada `interval`
segundos o supervisor pergunta à base se alguma corrida tem um token do túnel válido (emitido
quando a corrida arranca, revogado quando acaba, é cancelada, expira ou o notebook chama `finish`):

- há: o `cloudflared` sobe (ou volta a subir, se morreu);
- não há durante mais de `linger` segundos: o `cloudflared` desliga-se.

Fora das corridas não há ligação nenhuma à Cloudflare: o hostname do lote responde o erro da
Cloudflare de «túnel sem ligação» e nada do ambiente fica alcançável. Se a base não responder, o
supervisor conta como «não precisa» (fecha-se por omissão).

O ingress gera-se aqui e só deixa passar as quatro rotas do gateway (`INGRESS_PATH`) para
`localhost`; tudo o resto é 404 na própria Cloudflare. As credenciais do túnel são um ficheiro
gerado pelo `cloudflared tunnel create` (no NAS, montado só de leitura): só se lê o `TunnelID`
e o resto nunca passa por este processo nem pelos registos.
"""

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from ariadne.compute.gateway import OPEN_STATUSES
from ariadne.db.models import BatchRun

logger = logging.getLogger(__name__)

INGRESS_PATH = r"^/gpu/v1/runs/[0-9]+/(tasks|results|heartbeat|finish)$"
POLL_SECONDS = 10.0
LINGER_SECONDS = 45.0  # a resposta ao `finish` ainda tem de voltar pelo túnel
STOP_TIMEOUT = 20.0


class Process(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def tunnel_needed(session: Session, now: datetime | None = None) -> bool:
    """Há alguma corrida aberta com um token do túnel ainda válido?"""
    now = now or datetime.now(UTC)
    return bool(
        session.scalar(
            select(
                exists().where(
                    BatchRun.token_hash.is_not(None),
                    BatchRun.token_expires_at > now,
                    BatchRun.status.in_(OPEN_STATUSES),
                )
            )
        )
    )


def tunnel_config(public_url: str, credentials_file: str, *, port: int) -> dict[str, Any]:
    """O ficheiro de configuração do `cloudflared` (JSON, que o YAML aceita)."""
    hostname = urlsplit(public_url).hostname
    if not hostname:
        raise ValueError("GPU_GATEWAY_PUBLIC_URL has no hostname.")
    data = json.loads(Path(credentials_file).read_text(encoding="utf-8"))
    tunnel_id = str(data.get("TunnelID") or "")
    if not tunnel_id:
        raise ValueError(
            f"{credentials_file} has no TunnelID (not from `cloudflared tunnel create`)."
        )
    return {
        "tunnel": tunnel_id,
        "credentials-file": credentials_file,
        "no-autoupdate": True,
        # por omissão escuta em todas as interfaces: os vizinhos da rede chegavam lá
        "metrics": "localhost:20241",
        "ingress": [
            {
                "hostname": hostname,
                "path": INGRESS_PATH,
                "service": f"http://localhost:{port}",
                "originRequest": {"connectTimeout": "10s"},
            },
            {"service": "http_status:404"},
        ],
    }


def cloudflared_starter(config: dict[str, Any], workdir: Path) -> Callable[[], Process]:
    """Grava a configuração (fora do repositório e sem segredos: só o caminho das credenciais) e
    devolve como arrancar o `cloudflared` com ela."""
    path = workdir / "cloudflared.yml"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def start() -> Process:
        return subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--config", str(path), "run"],
            stdin=subprocess.DEVNULL,
        )

    return start


class TunnelSupervisor:
    """Liga o `cloudflared` só enquanto `needed()` diz que sim (ver o topo do módulo)."""

    def __init__(
        self,
        needed: Callable[[], bool],
        start: Callable[[], Process],
        *,
        linger: float = LINGER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.needed, self.start, self.linger, self.clock = needed, start, linger, clock
        self.process: Process | None = None
        self.last_needed: float | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def step(self) -> None:
        try:
            wanted = self.needed()
        except Exception as exc:  # a base não respondeu: fecha-se por omissão
            logger.warning("Tunnel: could not check for runs (%s)", type(exc).__name__)
            wanted = False
        now = self.clock()
        if wanted:
            self.last_needed = now
            if not self.running:
                if self.process is not None:
                    code = self.process.poll()
                    logger.warning("Tunnel: cloudflared exited (%s); starting it again", code)
                logger.info("Tunnel: a run has a token; starting cloudflared")
                try:
                    self.process = self.start()
                except OSError as exc:  # tenta outra vez na volta seguinte
                    logger.error("Tunnel: cloudflared did not start (%s)", exc)
                    self.process = None
        elif self.process is not None and (
            self.last_needed is None or now - self.last_needed >= self.linger
        ):
            logger.info("Tunnel: no run needs it; stopping cloudflared")
            self.stop()

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT)

    def run(self, stop: threading.Event, interval: float = POLL_SECONDS) -> None:
        try:
            while not stop.is_set():
                self.step()
                stop.wait(interval)
        finally:
            self.stop()


def supervisor_for(
    sessions: sessionmaker[Session],
    public_url: str,
    credentials_file: str,
    *,
    port: int,
    workdir: Path,
) -> TunnelSupervisor:
    def needed() -> bool:
        with sessions() as session:
            return tunnel_needed(session)

    config = tunnel_config(public_url, credentials_file, port=port)
    return TunnelSupervisor(needed, cloudflared_starter(config, workdir))
