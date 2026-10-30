"""Corridas de lote: exportar tarefas para JSONL, importar os resultados, libertar o que sobra.

    pending ──export──▶ batched ──(destino externo)──▶ resultados ──import──▶ done
                           │                                │
                           └─ falhou / cancelado / não veio ┴──▶ pending + `batch_tried`
                                                                   (o curador local trata)

Genérico por tipo de tarefa (`ariadne.compute.tasks`). Nenhuma função de exportação ou
importação conhece o destino: o JSONL de pedidos e o de resultados são o contrato.

JSONL de pedidos (uma linha por pedido): `{"id", "kind", "prompt_version", "messages",
"max_tokens", "temperature"}`. JSONL de resultados: `{"id", "content", "finish_reason"}` ou
`{"id", "error"}`; o `id` é o do pedido.
"""

import gzip
import io
import json
import logging
import zipfile
import zlib
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ariadne import queue
from ariadne.compute import gateway, tasks
from ariadne.compute.tasks import Applied, BatchReply
from ariadne.config import Settings
from ariadne.db.models import BatchRun, BatchTask, Job
from ariadne.models_policy import ModelNotAllowed, assert_allowed_model
from ariadne.services import Services

logger = logging.getLogger(__name__)

IN_PROGRESS = ("created", "queued", "running", "awaiting_results", "importing")
MAX_RESULTS_UNZIPPED = (
    512 * 1024 * 1024
)  # resultados depois de abertos (zip ou o que veio pelo túnel)
MAX_META_BYTES = 1024 * 1024
MAX_EXPORT_BYTES = 48 * 1024 * 1024  # um dataset do Kaggle aguenta bem mais; isto cabe em memória
SCAN_FACTOR = 3  # candidatos olhados por cada tarefa pedida (alguns não vão em lote)


class RunError(Exception):
    """Um pedido de corrida que não pode ser satisfeito (já há uma, nada para enviar, …)."""


def current_run(session: Session) -> BatchRun | None:
    return session.scalars(
        select(BatchRun).where(BatchRun.status.in_(IN_PROGRESS)).order_by(BatchRun.id).limit(1)
    ).first()


def pack(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)


def unpack(data: bytes, limit: int | None = None) -> bytes:
    """Descomprime (vários membros gzip seguidos, como os que o gateway acrescenta). Com `limit`
    pára aí: resultados vindos de fora não enchem a memória do worker (uma bomba gzip passa os
    limites do gateway, que contam bytes comprimidos); o que passa do limite perde-se."""
    if limit is None:
        return gzip.decompress(data)
    out = bytearray()
    rest = data
    while rest and len(out) < limit:
        member = zlib.decompressobj(wbits=31)
        out += member.decompress(rest, limit - len(out))
        if not member.eof:  # chegou ao limite (ou o membro vinha cortado)
            break
        rest = member.unused_data
    return bytes(out)


def jsonl_line(row: dict[str, Any]) -> bytes:
    """Uma linha JSON sem separadores de linha Unicode lá dentro: o `splitlines()` do Python (no
    notebook) parte em U+2028, U+2029 e U+0085, que o `json.dumps(ensure_ascii=False)` deixa
    passar."""
    text = json.dumps(row, ensure_ascii=False)
    for char, escaped in (("\u2028", "\\u2028"), ("\u2029", "\\u2029"), ("\x85", "\\u0085")):
        text = text.replace(char, escaped)
    return text.encode() + b"\n"


# --- exportar -------------------------------------------------------------------------------


def untried_pending(session: Session, kinds: list[str]) -> int:
    """Quantos jobs pendentes e devidos ainda não foram a nenhum lote externo."""
    if not kinds:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status == queue.PENDING,
                Job.kind.in_(kinds),
                Job.run_after <= func.now(),
                ~cast(Any, Job.payload).has_key(queue.BATCH_TRIED),
            )
        )
        or 0
    )


def create_run(
    services: Services,
    session: Session,
    *,
    backend: str,
    kinds: list[str],
    limit: int,
    model: str | None,
    requested_by: str | None,
    manual: bool,
    deadline_seconds: int | None,
) -> BatchRun | None:
    """Escolhe até `limit` jobs pendentes dos `kinds`, prepara os pedidos de cada um, tira-os da
    fila (`batched`) e guarda o JSONL na corrida (`status = created`). `RunError` se já há uma
    corrida em curso; `None` se não há nada que siga em lote (os jobs que não seguem ficam
    marcados `batch_tried`: quem chama tem de fazer commit). Não faz commit."""
    if not kinds:
        raise RunError("No active task kind.")
    try:
        assert_allowed_model(model or "", "batch model")
    except ModelNotAllowed as exc:
        raise RunError(str(exc)) from exc
    run = BatchRun(
        backend=backend,
        status="created",
        manual=manual,
        kinds=kinds,
        model=model,
        requested_by=requested_by,
        deadline_seconds=deadline_seconds,
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError as exc:
        raise RunError("A run is already in progress: wait for it to finish or cancel it.") from exc
    candidates = session.scalars(
        select(Job)
        .where(
            Job.status == queue.PENDING,
            Job.kind.in_(kinds),
            Job.run_after <= func.now(),
            ~cast(Any, Job.payload).has_key(queue.BATCH_TRIED),
        )
        .order_by(Job.priority.desc(), Job.id)
        .limit(limit * SCAN_FACTOR)
        .with_for_update(skip_locked=True)
    ).all()
    lines: list[bytes] = []
    job_ids: list[int] = []
    size = 0
    for job in candidates:
        if len(job_ids) >= limit:
            break
        task_type = tasks.get(job.kind)
        if task_type is None:
            continue
        prepared = task_type.prepare(services, session, job)
        if prepared is None or not prepared.requests:
            # Não vai em lote (já decidido, privado, …): fica para o curador local.
            job.payload = {**job.payload, queue.BATCH_TRIED: True}
            continue
        encoded = [
            jsonl_line(
                {
                    "id": tasks.request_id(run.id, job.id, request.key),
                    "kind": job.kind,
                    "prompt_version": task_type.prompt_version,
                    "messages": request.messages,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                }
            )
            for request in prepared.requests
        ]
        extra = sum(len(line) for line in encoded)
        if lines and size + extra > MAX_EXPORT_BYTES:
            break
        size += extra
        lines.extend(encoded)
        job_ids.append(job.id)
        session.add(
            BatchTask(
                run_id=run.id,
                job_id=job.id,
                kind=job.kind,
                meta=prepared.meta,
                request_ids=[tasks.request_id(run.id, job.id, r.key) for r in prepared.requests],
            )
        )
    if not job_ids:
        session.delete(run)
        session.flush()
        return None
    queue.mark_batched(session, job_ids)
    run.task_count = len(job_ids)
    run.request_count = len(lines)
    run.tasks_gz = pack(b"".join(lines))
    session.flush()
    return run


def tasks_jsonl(run: BatchRun) -> bytes:
    if run.tasks_gz is None:
        raise RunError("This run no longer has the requests JSONL (it was already imported).")
    return unpack(run.tasks_gz)


def gateway_info(settings: Settings, token: str) -> dict[str, Any]:
    """Como o notebook fala com o gateway (túnel): o URL público, o token desta corrida e, se há,
    os cabeçalhos do service token do Cloudflare Access."""
    headers: dict[str, str] = {}
    if settings.gpu_gateway_access_id and settings.gpu_gateway_access_secret:
        headers = {
            "CF-Access-Client-Id": settings.gpu_gateway_access_id,
            "CF-Access-Client-Secret": settings.gpu_gateway_access_secret,
        }
    return {"url": settings.gpu_gateway_public_url.rstrip("/"), "token": token, "headers": headers}


def build_manifest(
    run: BatchRun,
    deadline_seconds: int,
    config: dict[str, Any],
    gateway: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """O que o notebook precisa de saber (e que o JSONL não diz)."""
    manifest = {
        "run_id": run.id,
        "backend": run.backend,
        "model": run.model or config.get("model"),
        "deadline_seconds": deadline_seconds,
        "max_model_len": int(config.get("max_model_len", 24576)),
        "tensor_parallel_size": int(config.get("tensor_parallel_size", 0)),  # 0: todas as GPU
        "requests": run.request_count,
    }
    if gateway is not None:
        manifest["gateway"] = gateway
    return manifest


def bundle(run: BatchRun, manifest: dict[str, Any], *, slim: bool = False) -> bytes:
    """O pacote para o modo manual (Colab): `tasks.jsonl` + `manifest.json` num zip. `slim`: só o
    manifesto (com o túnel, o notebook puxa os pedidos sozinho)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        if not slim:
            archive.writestr("tasks.jsonl", tasks_jsonl(run))
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return out.getvalue()


# --- importar -------------------------------------------------------------------------------


def parse_results(data: bytes, run_id: int) -> tuple[dict[int, dict[str, BatchReply]], int]:
    """O JSONL de resultados agrupado por job -> chave do pedido. Devolve também quantas linhas
    foram ignoradas (ilegíveis ou de outra corrida)."""
    replies: dict[int, dict[str, BatchReply]] = {}
    ignored = 0
    for raw in data.decode("utf-8", errors="replace").split("\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            ident = tasks.parse_request_id(str(row["id"]))
        except (ValueError, KeyError, TypeError):
            ignored += 1
            continue
        if ident is None or ident[0] != run_id or not isinstance(row, dict):
            ignored += 1
            continue
        content = row.get("content")
        error = row.get("error")
        replies.setdefault(ident[1], {})[ident[2]] = BatchReply(
            content=content if isinstance(content, str) else None,
            error=str(error)[:500] if error else None,
            finish_reason=str(row["finish_reason"]) if row.get("finish_reason") else None,
        )
    return replies, ignored


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Lê um ficheiro do zip sem passar de `limit` bytes descomprimidos (o tamanho que o cabeçalho
    declara pode mentir: lê-se com teto)."""
    with archive.open(name) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{name} exceeds {limit // (1024 * 1024)} MB uncompressed")
    return data


def read_results_file(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """O ficheiro de resultados que se carrega: o `results.jsonl` simples, ou o zip do notebook
    (`results.jsonl` + `results.meta.json`). `ValueError`/`BadZipFile` se não serve."""
    if data[:2] != b"PK":
        return data, {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = {n.rsplit("/", 1)[-1]: n for n in archive.namelist()}
        if "results.jsonl" not in names:
            raise ValueError("the zip has no results.jsonl")
        meta: dict[str, Any] = {}
        if "results.meta.json" in names:
            loaded = json.loads(_read_member(archive, names["results.meta.json"], MAX_META_BYTES))
            meta = loaded if isinstance(loaded, dict) else {}
        return _read_member(archive, names["results.jsonl"], MAX_RESULTS_UNZIPPED), meta


def store_results(
    session: Session, run: BatchRun, data: bytes, meta: dict[str, Any] | None = None
) -> int:
    """Guarda os resultados da corrida e passa-a a `importing`. Devolve quantas linhas dizem
    respeito a esta corrida. `RunError` se nenhuma diz (ficheiro de outra corrida), a menos que
    o `results.meta.json` do notebook confirme que é desta corrida (e que não fez nada)."""
    if run.status not in ("awaiting_results", "queued", "running"):
        raise RunError(f"Run {run.id} is '{run.status}': it no longer accepts results.")
    if run.results_gz is not None:  # já vieram resultados pelo túnel: juntam-se (o último vale)
        data = unpack(run.results_gz, MAX_RESULTS_UNZIPPED) + b"\n" + data
    replies, _ignored = parse_results(data, run.id)
    count = sum(len(v) for v in replies.values())
    own = bool(meta) and meta is not None and meta.get("run_id") == run.id
    if count == 0 and not own:
        raise RunError(f"The file has no result from run {run.id}.")
    run.results_gz = pack(data)
    run.status = "importing"
    run.finished_at = run.finished_at or datetime.now(UTC)
    session.flush()
    return count


def import_run(services: Services, run_id: int) -> dict[str, int]:
    """Aplica os resultados guardados, tarefa a tarefa (cada uma na sua transação, por isso é
    seguro repetir), e devolve à fila o que não veio ou não serviu. Fecha a corrida."""
    with services.sessions() as session:
        run = session.get(BatchRun, run_id)
        if run is None or run.status != "importing" or run.results_gz is None:
            return {}
        replies, ignored = parse_results(unpack(run.results_gz, MAX_RESULTS_UNZIPPED), run_id)
        model = run.model
        task_ids = list(
            session.scalars(
                select(BatchTask.id)
                .where(BatchTask.run_id == run_id, BatchTask.status == "exported")
                .order_by(BatchTask.id)
            )
        )
    for task_id in task_ids:
        _import_task(services, task_id, replies, run_id, model)
    with services.sessions() as session:
        counts = _close_run(session, run_id, ignored)
        session.commit()
    return counts


def _import_task(
    services: Services,
    task_id: int,
    replies: dict[int, dict[str, BatchReply]],
    run_id: int,
    model: str | None,
) -> None:
    with services.sessions() as session:
        task = session.get(BatchTask, task_id, with_for_update=True)
        if task is None or task.status != "exported":
            return
        job = session.get(Job, task.job_id)
        got = replies.get(task.job_id, {})
        task_type = tasks.get(task.kind)
        if job is None or task_type is None or not got:
            result = Applied(tasks.INVALID, "no reply from the batch")
            task.status = "missing"
        else:
            try:
                with session.begin_nested():
                    result = task_type.apply(
                        services, session, job, task.meta, got, run_id=run_id, model=model
                    )
            except Exception as exc:  # um resultado estranho não pode parar a importação toda
                logger.exception("Importing task %d (job %d) failed", task.id, task.job_id)
                result = Applied(tasks.INVALID, f"{type(exc).__name__}: {exc}"[:300])
            task.status = result.outcome
        task.detail = result.detail[:500] or None
        consumed = result.outcome in (tasks.APPLIED, tasks.REVIEW, tasks.STALE)
        queue.release_batched(session, [task.job_id], tried=True, done=consumed)
        session.commit()


def _close_run(session: Session, run_id: int, ignored: int) -> dict[str, int]:
    run = session.get(BatchRun, run_id, with_for_update=True)
    assert run is not None
    rows = session.execute(
        select(BatchTask.status, func.count())
        .where(BatchTask.run_id == run_id)
        .group_by(BatchTask.status)
    ).all()
    by_status = {status: n for status, n in rows}
    run.applied_count = by_status.get(tasks.APPLIED, 0)
    run.review_count = by_status.get(tasks.REVIEW, 0)
    run.invalid_count = by_status.get(tasks.INVALID, 0) + by_status.get("missing", 0)
    run.released_count = run.invalid_count + by_status.get("exported", 0)
    run.status = "done"
    run.imported_at = datetime.now(UTC)
    run.finished_at = run.finished_at or run.imported_at
    run.tasks_gz = None
    run.results_gz = None
    gateway.revoke_token(run)
    session.flush()
    return {**by_status, "ignored_lines": ignored}


# --- terminar sem importar --------------------------------------------------------------------


def release_run(session: Session, run: BatchRun, *, tried: bool) -> int:
    """Devolve à fila todas as tarefas da corrida que ainda estão exportadas. `tried=True`: o
    destino externo já teve a sua oportunidade e o curador local fica com elas."""
    ids = list(
        session.scalars(
            select(BatchTask.job_id).where(
                BatchTask.run_id == run.id, BatchTask.status == "exported"
            )
        )
    )
    released = queue.release_batched(session, ids, tried=tried)
    for task in session.scalars(
        select(BatchTask).where(BatchTask.run_id == run.id, BatchTask.status == "exported")
    ):
        task.status = "missing"
        task.detail = "the run ended without a result"
    run.released_count = released
    return released


def finish_run(
    session: Session, run: BatchRun, status: str, error: str | None, *, tried: bool
) -> None:
    """Fecha uma corrida que falhou ou foi cancelada, devolvendo as tarefas à fila."""
    release_run(session, run, tried=tried)
    run.status = status
    run.error = (error or "")[:1000] or None
    run.finished_at = run.finished_at or datetime.now(UTC)
    run.tasks_gz = None
    run.results_gz = None
    gateway.revoke_token(run)
    session.flush()
