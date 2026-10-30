"""Classes de licença nas saídas (STRUCTURE.md, secção 7; ARCHITECTURE.md, secção 10).

Cada fonte e cada documento têm uma classe, `open` ou `private`. Os documentos herdam a da fonte
(`upsert_documents`, e o Postgres impede um documento aberto numa fonte privada) e tudo o que
deriva de um documento (chunks, vetores, ligações, registos de consulta) segue-o por
`document_id`: não se duplica a classe, verifica-se por junção.

`assert_exportable` é **a** verificação: toda a saída para fora do NAS (exportações de
vetores, o espelho do MemPalace, as releases da Fase 12, os endpoints de exportação) passa por
ela, e ela recusa tudo o que é privado ou deriva de privado. A pesquisa local (MCP, API, app) vê
as duas classes: não passa por aqui.

Uma saída que prefere **filtrar** a recusar (o espelho, uma release) usa `document_is_open()` na
consulta e regista o que deixou de fora com `record_filtered`.
"""

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import ColumnElement, and_, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from ariadne.db import models
from ariadne.sources.base import Source
from ariadne.sources.license import LicenseClass, parse_license_class

__all__ = [
    "Blocked",
    "LicenseBlocked",
    "LicenseClass",
    "OverrideKeepsPrivate",
    "PrivateSendBlocked",
    "assert_exportable",
    "assert_may_send",
    "clear_source_mark",
    "curator_gate",
    "curator_may_receive",
    "document_is_open",
    "embedder_endpoint",
    "find_blocked",
    "host_is_allowed",
    "mark_source_private",
    "parse_license_class",
    "private_document_ids",
    "private_sources",
    "private_wings",
    "record_filtered",
    "set_source_class",
]

BATCH = 5000
SAMPLE = 10


class LicenseBlocked(Exception):
    """Uma saída tentou levar conteúdo privado (ou derivado dele) para fora do NAS."""

    def __init__(self, output: str, blocked: Sequence["Blocked"]) -> None:
        self.output = output
        self.blocked = list(blocked)
        sample = ", ".join(f"{b.kind} {b.id}" for b in self.blocked[:3])
        more = f" (and {len(self.blocked) - 3} more)" if len(self.blocked) > 3 else ""
        super().__init__(
            f"Output blocked ({output}): {len(self.blocked)} item(s) of the private class or "
            f"derived from private, which never leave the NAS: {sample}{more}."
        )


@dataclass(frozen=True)
class Blocked:
    kind: str  # source | document | chunk | link
    id: str  # o id do item (para chunks e ligações, o número)
    because: str  # a fonte ou o documento privado de que vem


def document_is_open(document: Any = models.Document) -> ColumnElement[bool]:
    """Cláusula para consultas sobre `document` (ou um alias dele): o documento e a sua fonte são
    abertos. É a forma de uma saída **filtrar** em vez de recusar (a junção é barata: há um índice
    parcial só dos privados)."""
    owner = aliased(models.Source)  # um alias: a consulta de fora pode já ter `source` na junção
    return and_(
        document.license_class == LicenseClass.OPEN.value,
        ~exists()
        .where(
            owner.id == document.source_id,
            owner.license_class == LicenseClass.PRIVATE.value,
        )
        .correlate(document),
    )


def _batches(items: Iterable[Any]) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _private_documents(session: Session, document_ids: Iterable[str]) -> dict[str, str]:
    """document_id -> o que o torna privado (a fonte, ou o próprio documento)."""
    found: dict[str, str] = {}
    for batch in _batches(dict.fromkeys(document_ids)):
        rows = session.execute(
            select(models.Document.id, models.Document.source_id, models.Source.license_class)
            .join(models.Source, models.Source.id == models.Document.source_id)
            .where(
                models.Document.id.in_(batch),
                (models.Document.license_class == LicenseClass.PRIVATE.value)
                | (models.Source.license_class == LicenseClass.PRIVATE.value),
            )
        ).all()
        for document_id, source_id, source_class in rows:
            found[document_id] = source_id if source_class == "private" else document_id
    return found


def find_blocked(
    session: Session | None,
    *,
    sources: Iterable[Source] = (),
    source_ids: Iterable[str] = (),
    document_ids: Iterable[str] = (),
    chunk_ids: Iterable[int] = (),
    link_ids: Iterable[int] = (),
) -> list[Blocked]:
    """O que, entre os itens dados, é privado ou deriva de privado. Sem base de dados
    (`session=None`) só se verificam as fontes passadas como objetos `Source`."""
    blocked: list[Blocked] = []
    for source in sources:
        if source.license_class is LicenseClass.PRIVATE:
            blocked.append(Blocked("source", source.id, source.id))
    ids = list(dict.fromkeys(source_ids))
    docs = list(dict.fromkeys(document_ids))
    chunks = list(dict.fromkeys(chunk_ids))
    links = list(dict.fromkeys(link_ids))
    if session is None:
        if ids or docs or chunks or links:
            raise ValueError("checking source, document, chunk or link ids requires the database")
        return blocked
    for batch in _batches(ids):
        for (source_id,) in session.execute(
            select(models.Source.id).where(
                models.Source.id.in_(batch),
                models.Source.license_class == LicenseClass.PRIVATE.value,
            )
        ):
            blocked.append(Blocked("source", source_id, source_id))
    for document_id, because in _private_documents(session, docs).items():
        blocked.append(Blocked("document", document_id, because))
    for batch in _batches(chunks):
        owners = {
            chunk_id: document_id
            for chunk_id, document_id in session.execute(
                select(models.Chunk.id, models.Chunk.document_id).where(models.Chunk.id.in_(batch))
            )
        }
        private = _private_documents(session, owners.values())
        blocked += [
            Blocked("chunk", str(chunk_id), private[document_id])
            for chunk_id, document_id in owners.items()
            if document_id in private
        ]
    for batch in _batches(links):
        ends = session.execute(
            select(models.Link.id, models.Link.src_document, models.Link.dst_document).where(
                models.Link.id.in_(batch)
            )
        ).all()
        private = _private_documents(session, [d for _, a, b in ends for d in (a, b)])
        for link_id, src, dst in ends:
            if src in private or dst in private:
                blocked.append(Blocked("link", str(link_id), private.get(src) or private[dst]))
    return blocked


# Os serviços do compose (docker-compose.yml, também o `curator` do perfil `llm`) e o localhost:
# onde o texto privado pode ir sem sair da máquina/da rede do NAS. Quem põe um serviço noutro
# sítio (o curador no Mac, por exemplo) acrescenta-o a `PRIVATE_ALLOWED_HOSTS`, de propósito.
DEFAULT_ALLOWED_HOSTS = (
    "localhost,postgres,kiwix,wikijs,moodle,jobe,jobe-gateway,calibre,kavita,"
    "libretranslate,curator,server,worker"
)


class PrivateSendBlocked(LicenseBlocked):
    """Conteúdo privado a caminho de um serviço que não está na lista local."""


def host_is_allowed(url: str, allowed: str = "") -> bool:
    """O URL aponta para um host da lista local (`PRIVATE_ALLOWED_HOSTS`; vazio = a de omissão)?
    Um URL vazio (serviço desligado) não envia nada, por isso passa."""
    if not url.strip():
        return True
    host = (urlparse(url if "://" in url else f"//{url}").hostname or "").lower()
    if not host:
        return False
    try:
        if ipaddress.ip_address(host).is_loopback:  # o loopback é sempre local
            return True
    except ValueError:
        pass
    entries = [e.strip().lower() for e in (allowed.strip() or DEFAULT_ALLOWED_HOSTS).split(",")]
    return any(
        host == entry or (entry.startswith("*.") and host.endswith(entry[1:]))
        for entry in entries
        if entry
    )


def embedder_endpoint(settings: Any) -> str:
    """O URL a que o embedder envia texto: só `http` e `llamacpp` falam com um serviço; o `onnx` e o
    `fake` correm no processo."""
    return str(settings.embedder_url) if settings.embedder in ("http", "llamacpp") else ""


def assert_may_send(
    session: Session | None,
    *,
    target: str,
    url: str,
    allowed: str = "",
    sources: Iterable[Source] = (),
    source_ids: Iterable[str] = (),
    document_ids: Iterable[str] = (),
    chunk_ids: Iterable[int] = (),
    link_ids: Iterable[int] = (),
    record: bool = True,
) -> None:
    """Guarda técnica única do **envio** de texto a um serviço (curador, embeddings por HTTP,
    tradutor, Moodle, Wiki.js, Jobe): o conteúdo privado, ou derivado de privado, só vai para um
    URL da lista local (`PRIVATE_ALLOWED_HOSTS`). Fora dela levanta `PrivateSendBlocked` e regista
    o bloqueio em `license_block`: quem chama não enfileira nem despacha. Para um URL local não
    toca na base de dados."""
    if host_is_allowed(url, allowed):
        return
    blocked = find_blocked(
        session,
        sources=sources,
        source_ids=source_ids,
        document_ids=document_ids,
        chunk_ids=chunk_ids,
        link_ids=link_ids,
    )
    if not blocked:
        return
    if record and session is not None:
        _record(session, f"send to {target}", "blocked", blocked)
    raise PrivateSendBlocked(f"send to {target} ({urlparse(url).hostname or url})", blocked)


def curator_may_receive(session: Session, settings: Any, document_id: str) -> bool:
    """O curador pode receber o texto deste documento? `False` (e registado) se o documento é
    privado e `CURATOR_URL` não está na lista local."""
    try:
        assert_may_send(
            session,
            target="curator",
            url=settings.curator_url,
            allowed=settings.private_allowed_hosts,
            document_ids=[document_id],
        )
    except PrivateSendBlocked:
        return False
    return True


def curator_gate(session: Session, settings: Any, row: Any) -> bool:
    """Antes de enfileirar o curador para `row` (uma linha de `curation` `pending_curator`): se o
    documento é privado e o curador é remoto, não se enfileira e o item passa à revisão humana,
    com o motivo (`needs_review` não volta à fila do curador). Devolve se pode enfileirar."""
    if curator_may_receive(session, settings, row.document_id):
        return True
    row.decision = "needs_review"
    row.reason = (
        "private content: the curator is not in PRIVATE_ALLOWED_HOSTS (it gets no private text)"
    )
    return False


def assert_exportable(
    session: Session | None,
    *,
    output: str,
    sources: Iterable[Source] = (),
    source_ids: Iterable[str] = (),
    document_ids: Iterable[str] = (),
    chunk_ids: Iterable[int] = (),
    link_ids: Iterable[int] = (),
    record: bool = True,
) -> None:
    """Recusa (`LicenseBlocked`) a saída `output` se algum dos itens é privado ou deriva de
    privado: fontes, documentos, chunks (e os vetores deles), ligações (privada se uma das pontas
    for) e registos de consulta (pelo `document_id`). O bloqueio fica registado em
    `license_block` (a app mostra-o). Não faz commit da sessão do chamador: o registo usa a sua
    própria transação."""
    blocked = find_blocked(
        session,
        sources=sources,
        source_ids=source_ids,
        document_ids=document_ids,
        chunk_ids=chunk_ids,
        link_ids=link_ids,
    )
    if not blocked:
        return
    if record and session is not None:
        _record(session, output, "blocked", blocked)
    raise LicenseBlocked(output, blocked)


def private_document_ids(session: Session, document_ids: Iterable[str]) -> set[str]:
    """Os documentos, entre os dados, que são privados ou de uma fonte privada (as chaves `mcp`
    dos clientes externos não os veem). Um documento ainda sem linha (nunca lido) decide-se pela
    classe da fonte do id (`<source_id>/<caminho>`)."""
    ids = list(document_ids)
    found = set(_private_documents(session, ids))
    owners = {document_id.partition("/")[0] for document_id in ids if document_id not in found}
    if owners:
        private = set(
            session.scalars(
                select(models.Source.id).where(
                    models.Source.id.in_(owners),
                    models.Source.license_class == LicenseClass.PRIVATE.value,
                )
            )
        )
        found |= {document_id for document_id in ids if document_id.partition("/")[0] in private}
    return found


def private_wings(session: Session) -> set[str]:
    """As alas das fontes privadas (cada livro e cada curso é a sua ala), ativas ou não."""
    return set(
        session.scalars(
            select(models.Source.wing).where(
                models.Source.license_class == LicenseClass.PRIVATE.value
            )
        )
    )


def private_sources(session: Session) -> list[Blocked]:
    """As fontes privadas (ativas): o que uma saída que filtra deixa de fora."""
    return [
        Blocked("source", source_id, source_id)
        for (source_id,) in session.execute(
            select(models.Source.id)
            .where(
                models.Source.license_class == LicenseClass.PRIVATE.value,
                models.Source.status == "active",
            )
            .order_by(models.Source.id)
        )
    ]


def record_filtered(session: Session, output: str, blocked: Sequence[Blocked]) -> None:
    """Regista que uma saída deixou de fora conteúdo privado (filtrou em vez de recusar). Uma
    saída que corre de minuto a minuto não enche a tabela: o mesmo evento só se regista outra vez
    passadas 24 horas."""
    if not blocked:
        return
    recent = session.scalar(
        select(models.LicenseBlock.id).where(
            models.LicenseBlock.output == output,
            models.LicenseBlock.action == "filtered",
            models.LicenseBlock.items == len(blocked),
            models.LicenseBlock.created_at > func.now() - timedelta(hours=24),
        )
    )
    if recent is None:
        _record(session, output, "filtered", blocked)


def _record(session: Session, output: str, action: str, blocked: Sequence[Blocked]) -> None:
    """Escreve o evento numa transação à parte (a do chamador pode ir a rollback)."""
    bind = session.get_bind()
    with Session(bind) as own:
        own.add(
            models.LicenseBlock(
                output=output,
                action=action,
                items=len(blocked),
                sample=[
                    {"kind": b.kind, "id": b.id, "because": b.because} for b in blocked[:SAMPLE]
                ],
            )
        )
        own.commit()


class OverrideKeepsPrivate(ValueError):
    """A fonte está marcada privada à mão: só se abre desmarcando-a primeiro, de propósito."""


def set_source_class(session: Session, source_id: str, license_class: LicenseClass) -> int:
    """Muda a classe de uma fonte **e** dos seus documentos (decisão explícita do utilizador).
    Passar a privado é sempre permitido; passar a aberto também (é o utilizador a afirmar que tem
    direitos de redistribuição), exceto numa fonte marcada privada à mão. Num livro, acorda a
    assinatura do registo (`book.updated_at`) para que todos os processos vejam a classe nova.
    Devolve quantos documentos mudaram. Sem commit."""
    source = session.get(models.Source, source_id)
    if source is None:
        raise KeyError(source_id)
    if license_class is LicenseClass.OPEN and source.license_override == "private":
        raise OverrideKeepsPrivate(
            f"{source_id} is marked private by hand: unmark it first "
            "(`ariadne licenses clear-mark`)."
        )
    to_change = int(
        session.scalar(
            select(func.count())
            .select_from(models.Document)
            .where(
                models.Document.source_id == source_id,
                models.Document.license_class != license_class.value,
            )
        )
        or 0
    )
    source.license_class = license_class.value
    session.flush()  # passar a privado leva já os documentos (trigger); a aberto fazemo-lo aqui
    session.execute(
        update(models.Document)
        .where(
            models.Document.source_id == source_id,
            models.Document.license_class != license_class.value,
        )
        .values(license_class=license_class.value)
    )
    if source.kind == "book":
        session.execute(
            update(models.Book)
            .where(models.Book.id == source_id.removeprefix("book:"))
            .values(license_class=license_class.value, updated_at=func.now())
        )
    return to_change


def mark_source_private(session: Session, source_id: str) -> int:
    """Marca a fonte privada de forma **persistente** (sobrevive à sincronização, que reescreve a
    classe a partir do objeto Source): a classe mais restritiva ganha sempre. Leva os documentos.
    Devolve quantos mudaram. Sem commit."""
    source = session.get(models.Source, source_id)
    if source is None:
        raise KeyError(source_id)
    source.license_override = "private"
    return set_source_class(session, source_id, LicenseClass.PRIVATE)


def clear_source_mark(session: Session, source_id: str) -> None:
    """Tira a marca; a classe volta à que a fonte declara na próxima sincronização (que também
    reabre os documentos). Sem commit."""
    source = session.get(models.Source, source_id)
    if source is None:
        raise KeyError(source_id)
    source.license_override = None


def count_private(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(models.Document)
            .where(models.Document.license_class == LicenseClass.PRIVATE.value)
        )
        or 0
    )
