"""O tipo de tarefa `curate_page` (a curadoria de um artigo) em lote.

`prepare` parte o texto limpo em pedaços (`BATCH_PART_CHARS`) e escreve um pedido por pedaço, com o
mesmo prompt do curador local. `apply` lê as respostas pela mesma validação do local
(`parse_reply`, limpeza, fidelidade, flags que têm de desaparecer; ver `conclude_curation`) e o
resultado fica **para revisão humana** (como o do curador local): só se publica sozinho com
`BATCH_AUTO_PUBLISH`.
"""

from typing import Any

from sqlalchemy.orm import Session

from ariadne.compute import tasks
from ariadne.compute.tasks import Applied, BatchReply, BatchRequest, Prepared, TaskType
from ariadne.curation import pipeline
from ariadne.curation.curator import (
    CURATOR_KIND,
    Curated,
    CuratorError,
    build_messages,
    parse_reply,
    split_fixed,
    split_parts,
)
from ariadne.db.models import Curation, Job
from ariadne.db.models import Document as DocumentRow
from ariadne.licenses import PrivateSendBlocked, assert_may_send
from ariadne.services import Services
from ariadne.workers.curator import conclude_curation

EXTERNAL_TARGET = "https://gpu.externo.invalid"  # nunca está em PRIVATE_ALLOWED_HOSTS
CHARS_PER_TOKEN = 2.5  # estimativa prudente para escolher o `max_tokens` da resposta
MAX_REPLY_TOKENS = 8192


class CurateTask(TaskType):
    kind = CURATOR_KIND
    prompt_version = "curate-2"

    def prepare(self, services: Services, session: Session, job: Job) -> Prepared | None:
        document_id = job.payload.get("document_id")
        if not isinstance(document_id, str):
            return None
        row = session.get(Curation, document_id)
        document = session.get(DocumentRow, document_id)
        if row is None or document is None or row.decision != pipeline.PENDING:
            return None  # já decidido: o handler local conclui o job sem trabalho
        try:  # conteúdo privado nunca sai do ambiente
            assert_may_send(
                session,
                target="external GPU batch",
                url=EXTERNAL_TARGET,
                allowed=services.settings.private_allowed_hosts,
                document_ids=[document_id],
            )
        except PrivateSendBlocked:
            return None
        parts = split_parts(split_fixed(row.original_md)[0], services.settings.batch_part_chars)
        requests = [
            BatchRequest(
                key=str(number),
                messages=build_messages(document.title, part, (number + 1, len(parts))),
                max_tokens=min(MAX_REPLY_TOKENS, int(len(part) / CHARS_PER_TOKEN) + 1024),
            )
            for number, part in enumerate(parts)
        ]
        meta = {
            "document_id": document_id,
            "fingerprint": row.fingerprint,
            "parts": len(parts),
            "source_id": document.source_id,
        }
        return Prepared(requests, meta)

    def apply(
        self,
        services: Services,
        session: Session,
        job: Job,
        meta: dict[str, Any],
        replies: dict[str, BatchReply],
        *,
        run_id: int,
        model: str | None,
    ) -> Applied:
        document_id, digest = str(meta["document_id"]), str(meta["fingerprint"])
        row = session.get(Curation, document_id)
        if row is None or row.fingerprint != digest or row.decision != pipeline.PENDING:
            return Applied(tasks.STALE, "the text changed or was already decided")
        curated: list[Curated] = []
        for number in range(int(meta["parts"])):
            reply = replies.get(str(number))
            if reply is None or reply.content is None:
                why = reply.error if reply and reply.error else "part without a reply"
                return Applied(tasks.INVALID, f"part {number + 1}: {why}")
            if reply.finish_reason == "length":
                return Applied(tasks.INVALID, f"part {number + 1}: reply cut off (length)")
            try:
                curated.append(parse_reply(reply.content))
            except CuratorError as exc:
                return Applied(tasks.INVALID, f"part {number + 1}: {exc}")
        decision = conclude_curation(
            services,
            document_id,
            digest,
            row.original_md,
            str(meta["source_id"]),
            curated,
            model,
            batch_run_id=run_id,
        )
        outcome = tasks.APPLIED if decision == pipeline.PUBLISHED else tasks.REVIEW
        return Applied(outcome, decision)


tasks.register(CurateTask())
