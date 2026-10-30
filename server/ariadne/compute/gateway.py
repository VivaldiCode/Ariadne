"""O gateway do lote de GPU: a única parte do Ariadne que o túnel da Cloudflare expõe.

Corre num processo à parte (`ariadne gpu-gateway`, porta 8810), com um papel Postgres que só mexe
em `batch_run`, e tem quatro rotas, todas de uma só corrida:

    GET  /gpu/v1/runs/{id}/tasks      os pedidos (NDJSON), `?offset=&limit=` em linhas
    POST /gpu/v1/runs/{id}/results    acrescenta resultados (NDJSON, até 5 MB por chamada)
    POST /gpu/v1/runs/{id}/heartbeat  `{done, total, gpu_seconds}`: o progresso no painel
    POST /gpu/v1/runs/{id}/finish     fecha a corrida: o worker importa

O gateway **não valida nem publica nada**: guarda os resultados em `batch_run.results_gz` e quem os
importa é o worker, com a mesma validação (esquema, fidelidade, sanitização) e a mesma revisão
humana do curador local. Um resultado forjado acaba em `needs_review`, nunca numa página publicada.

O token é por corrida (`gpugw_…`, 32 bytes aleatórios; na base só o SHA-256), vale até ao prazo da
corrida mais meia hora e deixa de valer quando a corrida acaba, é cancelada ou o notebook chama
`finish`. Qualquer falha de autenticação responde o mesmo 401 (não diz se a corrida existe).

Antes de tocar na base: nenhum corpo passa de `MAX_CALL_BYTES` (também os de `Transfer-Encoding:
chunked`, cortados enquanto chegam) e um IP (o `CF-Connecting-IP` que o cloudflared põe) com
demasiadas falhas de autenticação, ou demasiadas falhas no total, leva 429 sem consulta à base.
"""

import gzip
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import LargeBinary, func, literal, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ariadne.compute import tasks
from ariadne.db.models import BatchRun

TOKEN_PREFIX = "gpugw_"
TOKEN_GRACE = timedelta(minutes=30)  # além do prazo da corrida
OPEN_STATUSES = ("queued", "running", "awaiting_results")  # em que o gateway serve a corrida
MAX_CALL_BYTES = 5 * 1024 * 1024
MAX_RESULTS_BYTES = 200 * 1024 * 1024  # os resultados comprimidos de uma corrida
RATE_WINDOW_SECONDS = 60.0
RATE_MAX_CALLS = 240  # por corrida e janela
MAX_TASK_LINES = 5000
FAILURES_PER_IP = 20  # falhas de autenticação por IP e janela; acima disto, 429 sem ir à base
FAILURES_TOTAL = 300  # o mesmo para todos os IPs juntos (um ataque distribuído não esgota a base)
MAX_TRACKED_IPS = 10_000
MAX_RUN_ID = 2**62
MIN_COMPRESSION = 1 / 25  # resultados mais repetitivos do que isto não são de um LLM: 400

bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(run: BatchRun, valid_for: timedelta) -> str:
    """Dá à corrida um token novo (o anterior deixa de valer) e devolve-o: é a única vez que se
    vê. Não faz commit."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    run.token_hash = hash_token(token)
    run.token_expires_at = datetime.now(UTC) + valid_for + TOKEN_GRACE
    return token


def revoke_token(run: BatchRun) -> None:
    run.token_hash = None
    run.token_expires_at = None


class Heartbeat(BaseModel):
    done: int = Field(ge=0, le=10_000_000)
    total: int = Field(ge=0, le=10_000_000)
    gpu_seconds: float = Field(0.0, ge=0, le=7 * 24 * 3600)


class Finish(BaseModel):
    gpu_seconds: float | None = Field(None, ge=0, le=7 * 24 * 3600)


class RateLimiter:
    """Uma janela deslizante por chave (em memória: o gateway é um processo só). `max_keys` limita
    a memória quando as chaves vêm de fora (IPs): acima disso esquecem-se as chaves paradas."""

    def __init__(
        self,
        limit: int = RATE_MAX_CALLS,
        window: float = RATE_WINDOW_SECONDS,
        max_keys: int = MAX_TRACKED_IPS,
    ) -> None:
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._calls: dict[object, deque[float]] = defaultdict(deque)

    def _recent(self, key: object, now: float) -> deque[float]:
        calls = self._calls[key]
        while calls and now - calls[0] > self.window:
            calls.popleft()
        return calls

    def full(self, key: object, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._recent(key, now)) >= self.limit

    def record(self, key: object, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._calls) >= self.max_keys:
            for stale in [k for k, v in self._calls.items() if not v or now - v[-1] > self.window]:
                del self._calls[stale]
            if len(self._calls) >= self.max_keys:
                self._calls.clear()
        self._recent(key, now).append(now)

    def allow(self, key: object, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.full(key, now):
            return False
        self.record(key, now)
        return True


class _TooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status.HTTP_413_CONTENT_TOO_LARGE, "Request too large.")


class BodyLimit:
    """Corta qualquer corpo acima de `limit` bytes antes de a aplicação o ler todo: pelo
    `Content-Length` logo à entrada e, sem ele (chunked), à medida que os blocos chegam."""

    def __init__(self, app: ASGIApp, limit: int = MAX_CALL_BYTES) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            response = Response("Request too large.", status.HTTP_413_CONTENT_TOO_LARGE)
            await response(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _TooLarge()
            return message

        await self.app(scope, limited, send)


def create_gateway_app(
    sessions: sessionmaker[Session], *, rate_limit: int = RATE_MAX_CALLS
) -> FastAPI:
    app = FastAPI(
        title="Ariadne GPU gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # nada de documentação nem esquema: a superfície é esta e mais nenhuma
    )
    app.add_middleware(BodyLimit)
    limiter = RateLimiter(rate_limit)
    failures = RateLimiter(FAILURES_PER_IP)
    all_failures = RateLimiter(FAILURES_TOTAL)

    def get_session() -> Iterator[Session]:
        with sessions() as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]
    Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

    def denied() -> HTTPException:
        # Sempre uma exceção nova: reaproveitar a mesma fazia crescer o traceback a cada 401.
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.")

    def authorize(
        run_id: int, request: Request, credentials: Credentials, session: SessionDep
    ) -> BatchRun:
        client = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "?"
        )
        if failures.full(client) or all_failures.full("*"):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests.")
        token = credentials.credentials if credentials else ""
        if not (0 < run_id < MAX_RUN_ID) or not token.startswith(TOKEN_PREFIX):
            failures.record(client)
            all_failures.record("*")
            raise denied()
        run = session.get(BatchRun, run_id)
        expected = run.token_hash if run is not None and run.token_hash else "0" * 64
        matches = hmac.compare_digest(expected, hash_token(token))
        now = datetime.now(UTC)
        if (
            run is None
            or not matches
            or not token.startswith(TOKEN_PREFIX)
            or run.token_expires_at is None
            or run.token_expires_at < now
            or run.status not in OPEN_STATUSES
        ):
            failures.record(client)
            all_failures.record("*")
            raise denied()
        if not limiter.allow(run_id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests.")
        return run

    RunDep = Annotated[BatchRun, Depends(authorize)]

    @app.get("/gpu/v1/runs/{run_id}/tasks")
    def get_tasks(run: RunDep, offset: int = 0, limit: int = MAX_TASK_LINES) -> Response:
        if run.tasks_gz is None:
            raise HTTPException(status.HTTP_410_GONE, "The run no longer has requests.")
        lines = gzip.decompress(run.tasks_gz).decode("utf-8").split("\n")
        lines = [line for line in lines if line.strip()]
        offset = max(0, offset)
        chosen = lines[offset : offset + max(1, min(limit, MAX_TASK_LINES))]
        return Response(
            "".join(f"{line}\n" for line in chosen),
            media_type="application/x-ndjson",
            headers={"X-Total-Requests": str(len(lines))},
        )

    @app.post("/gpu/v1/runs/{run_id}/results")
    async def post_results(run: RunDep, request: Request, session: SessionDep) -> dict[str, int]:
        body = await request.body()  # o BodyLimit já o cortou em MAX_CALL_BYTES
        if body and not body.endswith(b"\n"):
            body += b"\n"  # os blocos juntam-se: a última linha não pode colar-se à seguinte
        count, chunk = await run_in_threadpool(_check_and_pack, body, run.id)
        await run_in_threadpool(_append, session, run.id, chunk)
        return {"accepted": count}

    @app.post("/gpu/v1/runs/{run_id}/heartbeat")
    def post_heartbeat(run: RunDep, body: Heartbeat, session: SessionDep) -> dict[str, bool]:
        now = datetime.now(UTC)
        values: dict[str, object] = {"progress": body.model_dump(), "last_heartbeat_at": now}
        if run.status == "queued":  # o notebook já corre: o destino ainda não o tinha dito
            values |= {"status": "running", "running_at": run.running_at or now}
        _update_open(session, run.id, values)
        return {"ok": True}

    @app.post("/gpu/v1/runs/{run_id}/finish")
    def post_finish(run: RunDep, body: Finish, session: SessionDep) -> dict[str, str]:
        values: dict[str, object] = {
            # sem ler os resultados (podem ser grandes): só se põe um gzip vazio se não há nenhum
            "results_gz": func.coalesce(
                BatchRun.results_gz, literal(gzip.compress(b""), LargeBinary())
            ),
            "status": "importing",
            "finished_at": datetime.now(UTC),
            "token_hash": None,
            "token_expires_at": None,
        }
        if body.gpu_seconds is not None:
            values["progress"] = {**run.progress, "gpu_seconds": body.gpu_seconds}
        _update_open(session, run.id, values)
        return {"status": "importing"}

    return app


def _update_open(session: Session, run_id: int, values: dict[str, object]) -> None:
    """Atualiza a corrida só se ainda está aberta (no SQL: uma corrida cancelada entretanto não
    ressuscita)."""
    stored = session.scalar(
        update(BatchRun)
        .where(BatchRun.id == run_id, BatchRun.status.in_(OPEN_STATUSES))
        .values(values)
        .returning(BatchRun.id)
        .execution_options(synchronize_session=False)
    )
    if stored is None:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "The run is no longer open.")
    session.commit()


def _check_and_pack(body: bytes, run_id: int) -> tuple[int, bytes]:
    count = _check_lines(body, run_id)
    chunk = gzip.compress(body)
    if len(body) > 64 * 1024 and len(chunk) < len(body) * MIN_COMPRESSION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unreadable results.")
    return count, chunk


def _append(session: Session, run_id: int, chunk: bytes) -> None:
    """Acrescenta um bloco gzip aos resultados (gzip aceita blocos seguidos), no SQL: sem ler nem
    reescrever o que já lá está."""
    stored = session.scalar(
        update(BatchRun)
        .where(
            BatchRun.id == run_id,
            BatchRun.status.in_(OPEN_STATUSES),
            func.coalesce(func.octet_length(BatchRun.results_gz), 0) < MAX_RESULTS_BYTES,
        )
        .values(
            results_gz=func.coalesce(BatchRun.results_gz, literal(b"", LargeBinary())).op("||")(
                literal(chunk, LargeBinary())
            )
        )
        .returning(BatchRun.id)
    )
    if stored is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The run accepts no more results.")
    session.commit()


def _check_lines(body: bytes, run_id: int) -> int:
    """Cada linha tem de ser JSON com um `id` desta corrida: o gateway não aceita mais nada."""
    count = 0
    for raw in body.decode("utf-8", errors="replace").split("\n"):
        if not raw.strip():
            continue
        try:
            ident = tasks.parse_request_id(str(json.loads(raw)["id"]))
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unreadable line.") from exc
        if ident is None or ident[0] != run_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Result from another run.")
        count += 1
    return count
