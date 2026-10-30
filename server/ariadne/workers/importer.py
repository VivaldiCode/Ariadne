"""Worker `importer`: leva documentos para o Wiki.js, passando pela curadoria.

`import_page` (payload `document_id`, opcional `force`) lê o artigo da fonte, converte-o em
Markdown (determinístico), regista o texto limpo em `curation` e decide:

- sem flags, fora da amostragem e não sendo de saúde: publica o texto limpo;
- caso contrário fica `pending_curator` e o job `curate_page` (worker `curator`) trata dele; só
  depois de passar a fidelidade ou de um humano aprovar é que o `import_page` publica.

Antes de preparar, `doctype.classify` separa o que não é artigo: um `skip` e um `book` não vão
para o Wiki.js (`meta.wiki_skip` diz porquê; o livro fica com `meta.route = "book"`), e uma página
que já estava publicada e passa a um deles fica arquivada. A língua da página (`language.detect`)
é o seu locale no Wiki.js (`/<locale>/<domínio>/<subdomínio>/<slug>`, secção 6 de
docs/WIKI-IMPORT.md): uma língua sem locale ativo não se publica. O domínio vem de
`domains.place`; uma página que ainda está em `por-classificar`, ou no formato antigo
(`<fonte>/<língua>/<slug>`), muda-se para o sítio certo com `move_page` quando é reimportada.
O lixo de cada fonte aprende-se uma vez (`source.meta.boilerplate`), as imagens vão para os assets
do Wiki.js e cada domínio tem a sua página de índice (job `wiki_domain_index`).

A página tem a proveniência no topo, o corpo, o bloco «Ligações» (arestas aceites, exceto
`zim_internal`) e a licença da origem. O bloco é refeito quando uma aresta aceite muda (o linker e
a curadoria enfileiram `import_page` dos documentos que já têm página). O texto limpo, as «Ligações»
e a licença juntam-se numa impressão digital: igual à da última publicação, nada se envia ao
Wiki.js. Um artigo que desapareceu da fonte fica arquivado (aviso no topo), nunca apagado.

A tarefa periódica `run_import_scan` escolhe o que importar: os documentos com calor ≥
`IMPORT_HEAT_THRESHOLD`, os vizinhos de 1.ª ordem (ligações aceites a outras fontes, no máximo
`IMPORT_NEIGHBORS_MAX_PER_DOC` por documento quente: um salto só, nunca transitivo) e os já
importados cuja fonte mudou de versão.
"""

import html as htmllib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from ariadne import heat, queue
from ariadne.books.info import reader_url
from ariadne.concepts.translations import translations
from ariadne.curation import flags as flagging
from ariadne.curation import pipeline
from ariadne.curation.curator import CURATOR_KIND, CuratorError, CuratorUnavailable
from ariadne.db.models import Book as BookRow
from ariadne.db.models import Curation, DomainAssignment, Job, Link
from ariadne.db.models import Document as DocumentRow
from ariadne.db.models import Domain as DomainRow
from ariadne.db.models import Source as SourceRow
from ariadne.documents import (
    LibraryError,
    document_info,
    make_document_id,
    read_article,
    register_book_candidate,
    split_document_id,
    upsert_documents,
)
from ariadne.embedder import EmbedderError
from ariadne.kiwix import content_url
from ariadne.licenses import PrivateSendBlocked, assert_may_send, curator_gate, document_is_open
from ariadne.queue import ClaimedJob, Defer, Permanent
from ariadne.services import Services
from ariadne.sources.base import NotAnArticle, NotFound, Source
from ariadne.sources.zim import ZimSource, content_root
from ariadne.wiki import convert, doctype, domains, language, paths, render, titles
from ariadne.wiki.assets import ImageUploader
from ariadne.wiki.base import WikiBackend, WikiError, WikiNotFound, WikiUnavailable
from ariadne.wiki.boilerplate import RULES, Boilerplate, learn_for_source
from ariadne.wiki.refresh import (
    INDEX_KIND,
    KIND,
    enqueue_domain_classify,
    enqueue_domain_index,
    enqueue_import,
    enqueue_refresh,
)

logger = logging.getLogger(__name__)

WIKI_RETRY = timedelta(minutes=2)  # Wiki.js em baixo: espera sem gastar tentativas
NOT_CONFIGURED_RETRY = timedelta(minutes=10)
UNAVAILABLE_RETRY = timedelta(minutes=5)  # o curador local não responde
CLASSIFY_RETRY = timedelta(minutes=30)  # o local não decide `domain_classify`: volta a olhar
FAILED_COOLDOWN = timedelta(days=1)
CURATE_PRIORITY = 0.05  # a curadoria corre quando a máquina está livre
REFRESH_LIMIT = 100  # páginas vizinhas refrescadas quando uma página nasce
LAYOUT = 2  # `meta.wiki_layout`: 1 (ausente) `<fonte>/<língua>/<slug>`, 2 `<locale>/<domínio>/…`
TREE_OK_TTL = 3600.0  # a árvore de domínios confere-se de hora a hora (idempotente)
TREE_RETRY_TTL = 600.0  # e sem embedder volta a tentar-se a cada 10 minutos
SNAPSHOT_TTL = 300.0  # a árvore e os vetores dos domínios, em memória (cache curta)
# O que o importador escreve em `document.meta` e que se limpa quando deixa de valer.
SKIP_KEYS = ("wiki_skip", "route", "format_rescue")


def curate_dedupe_key(document_id: str) -> str:
    return f"{CURATOR_KIND}:{document_id}"


@dataclass
class _Prepared:
    """O que a leitura da fonte produziu, pronto a registar na curadoria."""

    document_id: str
    markdown: str  # texto limpo com os links internos em `ariadne:<caminho>`
    flags: list[flagging.Flag]
    version: str
    rescue: str = ""  # porque a forma ficou má (`convert.format_quality`); vazio: está bem
    title: str = ""  # o título da página no Wiki.js (`titles.choose`); vazio: o do documento


def import_page(services: Services, job: ClaimedJob, stop: threading.Event | None = None) -> None:
    """Handler do job: `Permanent` se o documento não se lê, `Defer` se o Wiki.js não responde."""
    raw = job.payload.get("document_id")
    if not isinstance(raw, str):
        raise Permanent(f"payload without document_id: {job.payload!r}")
    force = bool(job.payload.get("force"))
    wiki = services.get_wiki()
    if wiki is None:
        raise Defer(datetime.now(UTC) + NOT_CONFIGURED_RETRY, "Wiki.js not configured")
    try:
        source_id, path = split_document_id(raw)
    except LibraryError as exc:
        raise Permanent(str(exc)) from exc
    with services.sessions() as session:
        services.registry.sync_if_changed(session)
        session.commit()
    source = services.registry.get(source_id)
    if source is None:
        raise Permanent(f"Unknown source: {source_id!r}.")
    if source.content_type != "article":
        raise Permanent(
            f"{raw!r} is not an article ({source.content_type}): it does not go to Wiki.js"
        )
    try:
        with services.sessions() as session:
            assert_may_send(
                session,
                target="Wiki.js",
                url=services.settings.wikijs_url,
                allowed=services.settings.private_allowed_hosts,
                source_ids=[source.id],
                document_ids=[raw],
            )
    except PrivateSendBlocked as exc:
        raise Permanent(str(exc)) from exc
    try:
        article = read_article(source, path)
    except NotAnArticle as exc:
        with services.sessions() as session:
            found = register_book_candidate(session, source, path, exc)
            session.commit()
        if found is None:
            _archive(services, wiki, raw)
            return
        # PDF/EPUB dentro do ZIM: livro candidato para o Calibre (`route = "book"`, sem erro).
        zim_language = str(source.meta.get("zim_language") or source.language or "") or None
        locale = language.detect_locale(
            html=None, path=path, title=exc.title, text="", zim_language=zim_language
        )
        _ensure_tree(services)
        _set_aside(services, wiki, source, found[0], found[1], locale)
        return
    except NotFound:
        _archive(services, wiki, raw)
        return
    document_id = make_document_id(source.id, article.path)  # canónico: segue redirects
    with services.sessions() as session:
        upsert_documents(session, [document_info(source, article)])
        session.commit()
    _ensure_tree(services)

    text = content_root(article.html).get_text(" ")
    kind = doctype.classify(
        path=article.path,
        mimetype=article.mimetype,
        html=article.html,
        text=text,
        source_name=source.id,
    )
    locale = language.detect_locale(
        html=article.html,
        path=article.path,
        title=article.title,
        text=text,
        zim_language=str(source.meta.get("zim_language") or source.language or "") or None,
    )
    if kind.kind != "article":
        _set_aside(services, wiki, source, document_id, kind, locale)
        return
    inactive = _inactive_locale(services, wiki, locale)
    if inactive:
        _patch_meta(
            services, document_id, {"wiki_locale": locale, "wiki_skip": inactive}, SKIP_KEYS
        )
        return

    boilerplate = _boilerplate(services, source)
    try:
        prepared = _prepare(
            services, source, document_id, article.path, article.html, source.version,
            boilerplate, wiki, article.title,
        )  # fmt: skip
    except WikiUnavailable as exc:
        raise Defer(datetime.now(UTC) + WIKI_RETRY, f"Wiki.js unavailable: {exc}") from exc
    # Pode ter sido um `skip`/`book`/língua inativa antes: agora é um artigo (e a curadoria tira-o
    # do arquivo, `pipeline.register`).
    patch: dict[str, Any] = {"wiki_locale": locale, "wiki_title": prepared.title}
    if prepared.rescue:
        patch["format_rescue"] = prepared.rescue
    _patch_meta(services, document_id, patch, drop=SKIP_KEYS)
    with services.sessions() as session:
        row = pipeline.register(
            session,
            document_id=document_id,
            version=prepared.version,
            original_md=prepared.markdown,
            flags=prepared.flags,
            sampled=flagging.sampled(document_id, services.settings.curation_sample_rate),
        )
        decision = row.decision
        if (
            decision == pipeline.PENDING
            and services.curation_enabled()
            and curator_gate(session, services.settings, row)
        ):
            _enqueue_curation(session, document_id)
        session.commit()
    try:
        _publish(services, wiki, document_id, force=force)
    except WikiUnavailable as exc:
        raise Defer(datetime.now(UTC) + WIKI_RETRY, f"Wiki.js unavailable: {exc}") from exc
    except WikiError as exc:
        raise RuntimeError(str(exc)) from exc


def _prepare(
    services: Services,
    source: Source,
    document_id: str,
    path: str,
    html: str,
    version: str,
    boilerplate: Boilerplate,
    wiki: WikiBackend,
    zim_title: str = "",
) -> _Prepared:
    """HTML -> Markdown determinístico e flags. Os links internos ficam como `ariadne:<caminho>`.
    As imagens do ZIM vão para os assets do Wiki.js (`WikiUnavailable` se ele está em baixo) e o
    Markdown leva os URLs delas."""
    root, hidden = convert.prepare(html, boilerplate=boilerplate)
    chosen = titles.choose(zim_title, html, boilerplate, root=root)
    if chosen.heading is not None and not chosen.heading.decomposed:
        chosen.heading.decompose()  # o título da página já é este cabeçalho
    code = flagging.code_texts(root)
    targets: dict[str, str] = {}
    for href in convert.hrefs(root):
        found = source.resolve_link(path, href)
        if found is not None:
            targets[href] = pipeline.link_token(found[0])
    uploaded: dict[str, str] = {}
    refs = convert.images(root)  # antes de `to_markdown`, que reescreve a árvore
    if refs and isinstance(source, ZimSource):
        uploaded = ImageUploader(wiki, source).upload(path, refs, boilerplate)
    markdown = convert.to_markdown(root, targets, uploaded)
    found_flags = flagging.detect(
        htmllib.unescape(markdown),  # o texto que o leitor vê, sem as entidades do Markdown
        code=code,
        hidden=hidden,
        settings=services.settings,
        source_id=source.id,
        known_hidden=boilerplate.known_hidden,
    )
    quality = convert.format_quality(markdown)
    return _Prepared(
        document_id,
        markdown,
        found_flags,
        version,
        quality.reason if quality.needs_rescue else "",
        chosen.title,
    )


# --- tipo, língua, lixo da fonte e árvore de domínios -------------------------------------------


def _patch_meta(
    services: Services, document_id: str, patch: Mapping[str, Any], drop: Sequence[str] = ()
) -> None:
    """`document.meta` = (meta sem as chaves de `drop`) || patch."""
    with services.sessions() as session:
        session.execute(
            text(
                "UPDATE document SET meta = (meta - CAST(:drop AS text[])) || CAST(:patch AS jsonb)"
                " WHERE id = :id"
            ),
            {"drop": list(drop), "patch": json.dumps(patch), "id": document_id},
        )
        session.commit()


def _configured_locales(services: Services) -> frozenset[str]:
    return frozenset(
        part.strip().lower() for part in services.settings.wikijs_locales.split(",") if part.strip()
    )


def _active_locales(services: Services, wiki: WikiBackend) -> frozenset[str]:
    """Os locales em que se publica: `WIKIJS_LOCALES` se foi definido; senão os do Wiki.js
    (*namespacing*, em cache no backend); senão o valor por omissão (`en,pt`)."""
    configured = _configured_locales(services)
    if configured and "wikijs_locales" in services.settings.model_fields_set:
        return configured
    return wiki.active_locales() or configured or frozenset({"en", "pt"})


def _inactive_locale(services: Services, wiki: WikiBackend, locale: str) -> str:
    """O motivo de a língua não se publicar, ou `""` se tem locale ativo."""
    if locale not in language.WIKIJS_LOCALES:
        return f"language {locale} has no locale in Wiki.js"
    if locale in _active_locales(services, wiki):
        return ""
    return f"locale {locale} inactive in Wiki.js"


def _boilerplate(services: Services, source: Source) -> Boilerplate:
    """O lixo repetido da fonte (menus, logótipos): aprende-se à 1.ª importação da fonte, ou
    quando muda de versão, e fica em `source.meta.boilerplate`."""
    key = f"boilerplate:{source.id}:{source.version}"
    cached = services.wiki_cache.get(key)
    if isinstance(cached, Boilerplate):
        return cached
    with services.sessions() as session:
        stored = session.scalar(
            select(SourceRow.meta["boilerplate"]).where(SourceRow.id == source.id)
        )
    if (
        isinstance(stored, dict)
        and stored.get("version") == source.version
        and stored.get("rules") == RULES
    ):
        learned = Boilerplate.from_meta(stored.get("data"))
    else:
        learned = learn_for_source(source) if isinstance(source, ZimSource) else Boilerplate()
        saved = {
            "version": source.version,
            "rules": RULES,
            "learned_at": datetime.now(UTC).isoformat(),
            "data": learned.to_meta(),
        }
        with services.sessions() as session:
            session.execute(
                text("UPDATE source SET meta = meta || CAST(:patch AS jsonb) WHERE id = :id"),
                {"patch": json.dumps({"boilerplate": saved}), "id": source.id},
            )
            session.commit()
        logger.info(
            "Boilerplate of %s learned: %d lines, %d images",
            source.id, len(learned.lines), len(learned.images),
        )  # fmt: skip
    services.wiki_cache[key] = learned
    return learned


def _ensure_tree(services: Services) -> str | None:
    """Carrega a árvore de domínios (`sync_tree`) e vetoriza as descrições (`ensure_vectors`),
    ambos idempotentes; corre à 1.ª utilização do processo e de hora a hora. Devolve o `model_id`
    do embedder, ou `None` se não há embedder (nível 3 desligado): a colocação usa só o prior e o
    resto fica em `por-classificar`."""
    cache = services.wiki_cache
    now = time.monotonic()
    state = cache.get("tree")
    if state is not None and now < state["until"]:
        return str(state["model_id"]) or None
    for attempt in (1, 2):
        try:
            with services.sessions() as session:
                domains.sync_tree(session)
                session.commit()
            break
        except IntegrityError:  # outro processo carregou a árvore ao mesmo tempo: repete-se
            if attempt == 2:
                raise
    model_id = ""
    try:
        embedder = services.get_embedder()
        with services.sessions() as session:
            created = domains.ensure_vectors(session, embedder, embedder.model_id)
            session.commit()
        model_id = embedder.model_id
        if created:
            logger.info("Domains: %d new vectors (%s)", created, model_id)
    except EmbedderError as exc:
        logger.warning("No embedder: domains are placed by the source prior only (%s)", exc)
    except IntegrityError:  # idem, nos vetores
        model_id = ""
    cache["tree"] = {
        "model_id": model_id,
        "until": now + (TREE_OK_TTL if model_id else TREE_RETRY_TTL),
    }
    cache.pop("snapshot", None)
    return model_id or None


def _snapshot(services: Services, session: Session) -> tuple[domains.TreeSnapshot, str]:
    """A árvore e os vetores dos domínios em memória (cache curta) e o `model_id` dos vetores."""
    model_id = _ensure_tree(services) or ""
    cached = services.wiki_cache.get("snapshot")
    now = time.monotonic()
    if cached is not None and cached[0] == model_id and now < cached[1]:
        return cached[2], model_id
    snapshot = domains.load_snapshot(session, model_id)
    services.wiki_cache["snapshot"] = (model_id, now + SNAPSHOT_TTL, snapshot)
    return snapshot, model_id


def _place(
    session: Session,
    document_id: str,
    source_id: str,
    model_id: str,
    snapshot: domains.TreeSnapshot,
) -> domains.Placement:
    """`domains.place` e, se ficou sem certeza, o pedido ao lote LLM (`domain_classify`). Sem
    commit: vai na transação de quem coloca."""
    placement = domains.place(
        session,
        document_id=document_id,
        source_name=source_id,
        embedder_model_id=model_id,
        structure_hint=None,
        snapshot=snapshot,
    )
    if placement.needs_review and placement.method not in domains.PROTECTED_METHODS:
        enqueue_domain_classify(session, document_id)
    return placement


def domain_snapshot(services: Services, session: Session) -> tuple[domains.TreeSnapshot, str]:
    """A árvore, as âncoras e o atrator em memória (cache curta) e o `model_id` dos vetores: o
    mesmo que o importador usa para colocar, para quem prepara o lote `domain_classify`."""
    return _snapshot(services, session)


def classify_domain_local(
    services: Services, job: ClaimedJob, stop: threading.Event | None = None
) -> None:
    """Handler local do job `domain_classify`: o mesmo pedido e a mesma validação do lote
    (`ariadne.compute.domain_classify`), mas respondido pelo curador local (o Mac). Sem curador
    o job espera pelo lote, adiado de cada vez que o local o reclama. Um job que já foi a um lote
    (`batch_tried`) e não voltou com resposta acaba aqui: a página fica em `needs_review`, para uma
    pessoa; o mesmo se a resposta do curador local não serve."""
    if job.payload.get(queue.BATCH_TRIED):
        return
    curator = services.get_curator()
    if curator is None:
        raise Defer(
            datetime.now(UTC) + CLASSIFY_RETRY, "waiting for the LLM batch (domain_classify)"
        )
    from ariadne.compute.domain_classify import classify_with_curator  # evita um import circular

    try:
        detail = classify_with_curator(services, job.id, curator)
    except CuratorUnavailable as exc:
        services.curator_monitor.mark_down(services.settings.curator_url, str(exc))
        raise Defer(datetime.now(UTC) + UNAVAILABLE_RETRY, str(exc)) from exc
    except CuratorError as exc:
        logger.warning("domain_classify local (job %d): %s", job.id, exc)
        return
    logger.info("domain_classify local (job %d): %s", job.id, detail)


def _set_aside(
    services: Services,
    wiki: WikiBackend,
    source: Source,
    document_id: str,
    kind: doctype.DocKind,
    locale: str,
) -> None:
    """`skip` e `book` não vão para o Wiki.js. Regista porquê (`meta.wiki_skip`; o livro leva
    `meta.route = "book"`, para o Calibre), coloca o livro na árvore (a página do domínio lista-o)
    e arquiva a página, se já havia uma."""
    patch: dict[str, Any] = {"wiki_skip": kind.reason, "wiki_locale": locale}
    if kind.kind == "book":
        patch["route"] = "book"
    _patch_meta(services, document_id, patch, drop=("route", "format_rescue"))
    if kind.kind == "book":
        with services.sessions() as session:
            snapshot, model_id = _snapshot(services, session)
            placement = _place(session, document_id, source.id, model_id, snapshot)
            enqueue_domain_index(session, locale, placement.domain_id)
            session.commit()
    with services.sessions() as session:
        published = session.scalar(
            select(DocumentRow.wiki_page_id).where(DocumentRow.id == document_id)
        )
    if published is not None:
        _archive(services, wiki, document_id)


def _enqueue_curation(session: Session, document_id: str) -> None:
    queue.enqueue(
        session,
        CURATOR_KIND,
        {"document_id": document_id},
        priority=CURATE_PRIORITY,
        dedupe_key=curate_dedupe_key(document_id),
    )


# --- publicar ---------------------------------------------------------------------------------


def _publish(
    services: Services, wiki: WikiBackend, document_id: str, *, force: bool, archived: bool = False
) -> bool:
    """Escreve a página no Wiki.js a partir do estado da curadoria (e das «Ligações» de agora).
    Devolve se enviou alguma coisa."""
    settings = services.settings
    with services.sessions() as session:
        document = session.get(DocumentRow, document_id)
        row = session.get(Curation, document_id)
        source = session.get(SourceRow, document.source_id) if document else None
        if document is None or row is None or source is None:
            return False
        withdrawn = False
        if row.decision == pipeline.PUBLISHED:
            if row.curated_md is not None and not row.curated_md.strip():
                return False  # uma reescrita vazia nunca cai para o original (que tinha flags)
            if row.curated_md is None and row.flags and not row.reviewed_by:
                return False  # com flags só se publica o que o curador ou uma pessoa passou
            body_md = row.original_md if row.curated_md is None else row.curated_md
            if row.reviewed_by:
                treatment = "aprovado por revisão humana"
            elif row.curated_md is not None:
                treatment = f"reescrito pelo curador ({row.curator_model or 'modelo desconhecido'})"
            else:
                treatment = "limpeza determinística, sem alterações ao texto"
        elif row.decision == pipeline.REJECTED and row.published_md is not None:
            body_md, withdrawn = "", True  # a revisão retirou o que estava publicado
            treatment = "retirado por revisão humana"
        elif row.published_md is not None:  # a versão nova ainda não passou: mantém-se a antiga
            body_md = row.published_md
            treatment = "versão anterior (a nova aguarda curadoria)"
        else:
            return False
        meta: dict[str, Any] = dict(document.meta)
        locale = _page_locale(services, wiki, document, meta)
        located = _locate(services, session, document, source, meta, locale)
        first_time = document.wiki_page_id is None
        title, language_code, version = document.title, document.language, source.version
        page_title = _page_title(document)
        book = source.meta.get("kiwix_book")
        source_title, source_zim = source.title, source.meta.get("zim_name")
        archived = archived or row.archived_at is not None
        languages = _language_entries(session, services, document_id)
        entries = [*languages[0], *_link_entries(session, services, document_id, languages[1])]
        body = pipeline.resolve_tokens(
            body_md,
            _link_lookup(session, services, pipeline.token_paths(body_md), document.source_id),
        )
        prior_hash, prior_page = meta.get("wiki_hash"), document.wiki_page_id
        document_path = document.path
    kiwix_url = render.kiwix_link(settings.kiwix_public_url, book, document_path)
    links = render.links_block(entries)
    foot = render.footer(
        render.license_for(document.source_id, str(source_zim or ""), language_code, title),
        source_title,
    )
    description = render.summary(body)
    tags = [t for t in (located.locale, located.domain_id) if t]
    # A versão da fonte não entra na impressão digital: uma release nova do ZIM com o mesmo texto
    # não reescreve a página (só `meta.wiki_version` se atualiza; o cabeçalho mostra a versão em
    # que o texto foi publicado). O sítio da página (locale, caminho, domínio) entra: se muda,
    # republica-se com as etiquetas novas.
    digest = render.digest(
        page_title, description, body, links, foot, treatment, source_title, kiwix_url or "",
        str(archived), str(withdrawn), located.path, ",".join(tags),
    )  # fmt: skip
    moved = False
    if located.move_from is not None and prior_page is not None:
        try:
            wiki.move_page(prior_page, located.rel, located.locale)
            moved = True
        except WikiNotFound:  # a página foi apagada no Wiki.js: escreve-se de novo
            force = True
    if not force and prior_page is not None and prior_hash == digest:
        _remember(services, document_id, located, version, digest, prior_page, row_md=None)
        return False
    page = render.assemble(
        render.header(
            source_title=source_title,
            source_id=document.source_id,
            version=version,
            path=document_path,
            kiwix_url=kiwix_url,
            curation=treatment,
            archived=archived,
            withdrawn=withdrawn,
        ),
        body,
        links,
        foot,
    )
    page_id = wiki.upsert_page(
        located.rel, page_title, page, description, tags, locale=located.locale
    )
    _remember(
        services,
        document_id,
        located,
        version,
        digest,
        page_id,
        row_md=None if withdrawn else body_md,
    )
    logger.info("Page %s (%s) published to Wiki.js", located.path, document_id)
    with services.sessions() as session:
        if first_time or moved:
            neighbors = _accepted_neighbors(session, document_id, REFRESH_LIMIT)
            enqueue_refresh(session, neighbors)
        if first_time or located.move_from is not None:
            enqueue_domain_index(session, located.locale, located.domain_id)
            if located.old_domain is not None and located.move_from is not None:
                enqueue_domain_index(
                    session, located.move_from.split("/", 1)[0], located.old_domain
                )
        session.commit()
    return True


def _remember(
    services: Services,
    document_id: str,
    located: "_Located",
    version: str,
    digest: str,
    page_id: int,
    row_md: str | None,
) -> None:
    with services.sessions() as session:
        patch = {
            "wiki_path": located.path,
            "wiki_at": located.path,  # onde a página está agora no Wiki.js («locale/caminho»)
            "wiki_layout": LAYOUT,
            "wiki_locale": located.locale,
            "wiki_domain": located.domain_id,
            "wiki_domain_version": domains.PLACEMENT_VERSION,
            "wiki_version": version,
            "wiki_hash": digest,
            "wiki_imported_at": datetime.now(UTC).isoformat(),
        }
        session.execute(
            text(
                "UPDATE document SET wiki_page_id = :page_id, meta = meta || CAST(:patch AS jsonb)"
                " WHERE id = :id"
            ),
            {"page_id": page_id, "patch": json.dumps(patch), "id": document_id},
        )
        if row_md is not None:
            session.execute(
                text(
                    "UPDATE curation SET published_md = :md, published_at = now()"
                    " WHERE document_id = :id AND decision = 'published'"
                ),
                {"md": row_md, "id": document_id},
            )
        session.commit()


# --- onde fica a página -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Located:
    """O sítio da página no Wiki.js: `path` = `<locale>/<caminho>` (é o `meta.wiki_path` e, com o
    *namespacing* ligado, o URL), `rel` o caminho sem o locale (o que o Wiki.js recebe)."""

    path: str
    locale: str
    rel: str
    domain_id: str
    move_from: str | None = None  # onde a página está agora, se é outro sítio («locale/caminho»)
    old_domain: str | None = None  # o domínio do sítio de onde sai


def _page_title(document: DocumentRow) -> str:
    """O título da página no Wiki.js e no caminho: o escolhido na importação (`titles.choose`,
    em `meta.wiki_title`) ou, para as páginas de antes dele, o do documento."""
    chosen = document.meta.get("wiki_title")
    return chosen if isinstance(chosen, str) and chosen.strip() else document.title


def _page_locale(
    services: Services, wiki: WikiBackend, document: DocumentRow, meta: Mapping[str, Any]
) -> str:
    """O locale da página: o que a importação guardou; para as páginas de antes dela, a língua do
    documento se o Wiki.js a aceita, senão o locale do site."""
    stored = meta.get("wiki_locale")
    if isinstance(stored, str) and stored:
        return stored
    if document.language and document.language in _active_locales(services, wiki):
        return document.language
    return services.settings.wikijs_locale


def _domain_of(
    services: Services,
    session: Session,
    document: DocumentRow,
    source: SourceRow,
    meta: Mapping[str, Any],
) -> str:
    """O domínio da página. Uma colocação humana ou do LLM manda. Senão, uma página já colocada
    fica onde está, exceto em `por-classificar` ou colocada por uma versão anterior das regras
    (`domains.PLACEMENT_VERSION`), que se colocam de novo: mudar de domínio move a página."""
    assignment = session.get(DomainAssignment, document.id)
    if assignment is not None and assignment.method in domains.PROTECTED_METHODS:
        return assignment.domain_id
    kept = meta.get("wiki_domain")
    if (
        meta.get("wiki_layout") == LAYOUT
        and isinstance(kept, str)
        and kept != domains.FALLBACK_ID
        and meta.get("wiki_domain_version") == domains.PLACEMENT_VERSION
    ):
        return kept
    snapshot, model_id = _snapshot(services, session)
    return _place(session, document.id, source.id, model_id, snapshot).domain_id


def _locate(
    services: Services,
    session: Session,
    document: DocumentRow,
    source: SourceRow,
    meta: Mapping[str, Any],
    locale: str,
) -> _Located:
    """Decide e reserva o caminho `<locale>/<domínio>/<subdomínio>/<slug>` (em `meta.wiki_path`,
    antes de escrever no Wiki.js: o índice único recusa um caminho já tomado por outro documento
    e, nesse caso, ganha um sufixo estável). Uma página que já tem caminho no novo formato, no
    mesmo domínio e locale, fica como está. Uma no formato antigo (`<fonte>/<língua>/<slug>`, no
    locale do site) ou noutro domínio ou locale leva o caminho novo e `move_from` diz de onde."""
    domain_id = _domain_of(services, session, document, source, meta)
    session.commit()  # a colocação (`domain_assignment`) fica, mesmo que o caminho não mude
    current = meta.get("wiki_path")
    exists = document.wiki_page_id is not None
    new_layout = meta.get("wiki_layout") == LAYOUT
    at = meta.get("wiki_at")
    if exists and not at and current:  # antes da v2: a página está no locale do site, sem prefixo
        at = f"{services.settings.wikijs_locale}/{current}"
    old_domain = meta.get("wiki_domain") if new_layout else None

    def result(path: str) -> _Located:
        head, _, rel = path.partition("/")
        moving = exists and isinstance(at, str) and at != path
        return _Located(
            path, head, rel, domain_id, str(at) if moving else None,
            str(old_domain) if moving and isinstance(old_domain, str) else None,
        )  # fmt: skip

    display = _page_title(document)
    if (
        new_layout
        and isinstance(current, str)
        and meta.get("wiki_domain") == domain_id
        and current.partition("/")[0] == locale
        and meta.get("wiki_path_title", document.title) == display
    ):
        return result(current)
    snapshot, _ = _snapshot(services, session)
    slugs = domains.slugs_for(session, domain_id, locale)
    candidate = paths.domain_page_path(locale, slugs, display, document.id)
    reserved = snapshot.index_paths_for(locale)  # o caminho de um domínio não é de um artigo
    options = [] if candidate in reserved else [candidate]
    options.append(paths.disambiguate(candidate, document.id))
    patch: dict[str, Any] = {
        "wiki_layout": LAYOUT,
        "wiki_domain": domain_id,
        "wiki_path_title": display,  # o título de que o caminho vem: se mudar, a página muda
    }
    if exists and at:
        patch["wiki_at"] = at
    for rel in options:
        full = f"{locale}/{rel}"
        try:
            with session.begin_nested():
                session.execute(
                    text("UPDATE document SET meta = meta || CAST(:patch AS jsonb) WHERE id = :id"),
                    {"patch": json.dumps({**patch, "wiki_path": full}), "id": document.id},
                )
        except IntegrityError:
            continue
        session.commit()
        return result(full)
    raise Permanent(f"Could not reserve a path for {document.id!r}.")


def _link_lookup(
    session: Session, services: Services, wanted: set[str], source_id: str
) -> Callable[[str], str | None]:
    """Para um caminho da mesma fonte: a página do Wiki.js se o destino está importado, senão o
    kiwix (`KIWIX_PUBLIC_URL`), senão nada."""
    ids = [make_document_id(source_id, p) for p in wanted]
    wiki_paths: dict[str, str | None] = {
        row[0]: row[1]
        for row in session.execute(
            select(DocumentRow.id, text("meta ->> 'wiki_path'")).where(
                DocumentRow.id.in_(ids), DocumentRow.wiki_page_id.is_not(None)
            )
        )
    }
    book = session.scalar(
        select(SourceRow.meta["kiwix_book"].astext).where(SourceRow.id == source_id)
    )

    def lookup(path: str) -> str | None:
        wiki_path = wiki_paths.get(make_document_id(source_id, path))
        if wiki_path:
            return f"/{wiki_path}"
        return content_url(services.settings.kiwix_public_url, book, path) if book else None

    return lookup


def _accepted_neighbors(session: Session, document_id: str, limit: int) -> list[str]:
    """Documentos com uma aresta aceite com este, em qualquer sentido (os internos também: as
    páginas deles podem apontar para esta no corpo)."""
    src = session.scalars(
        select(Link.dst_document)
        .where(Link.src_document == document_id, Link.status == "accepted")
        .limit(limit)
    ).all()
    dst = session.scalars(
        select(Link.src_document)
        .where(Link.dst_document == document_id, Link.status == "accepted")
        .limit(limit)
    ).all()
    return sorted({*src, *dst})[:limit]


def _language_entries(
    session: Session, services: Services, document_id: str
) -> tuple[list[render.LinkEntry], set[str]]:
    """As versões do conceito noutras línguas («Outras línguas»), com o selo «fonte original» na da
    língua original; e os ids que cobrem, para as arestas não as repetirem."""
    found = translations(services, session, document_id)
    if not found.translations:
        return [], set()
    sources = {
        row.id: row
        for row in session.scalars(
            select(SourceRow).where(SourceRow.id.in_({t.source_id for t in found.translations}))
        )
    }
    wiki_base = services.settings.wikijs_public_url.rstrip("/")
    entries = []
    for version in found.translations:
        source = sources.get(version.source_id)
        wiki = version.wiki_url
        url = wiki[len(wiki_base) :] if wiki and wiki.startswith(wiki_base) else version.kiwix_url
        entries.append(
            render.LinkEntry(
                "langlink",
                source.title if source else version.source_id,
                version.language,
                version.title,
                url,
                original=version.original_language,
            )
        )
    return entries, {t.document_id for t in found.translations}


def _link_entries(
    session: Session,
    services: Services,
    document_id: str,
    in_languages: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[render.LinkEntry]:
    """As arestas aceites (sem `zim_internal`) para fontes ativas, da mais à menos pontuada. As
    de línguas (`langlink`, e as `wikidata` para uma língua que já está em «Outras línguas»)
    vêm do conceito, não daqui."""
    other = aliased(DocumentRow)
    rows = session.execute(
        select(Link, other, SourceRow)
        .join(
            other,
            or_(
                and_(Link.src_document == document_id, other.id == Link.dst_document),
                and_(Link.dst_document == document_id, other.id == Link.src_document),
            ),
        )
        .join(SourceRow, SourceRow.id == other.source_id)
        .where(
            Link.status == "accepted",
            Link.kind != "zim_internal",
            SourceRow.status == "active",
            # As páginas do Wiki.js saem nas releases: não apontam para conteúdo privado (que só
            # se vê na app, no MCP e em `neighbors`): a mesma regra de todas as saídas.
            document_is_open(other),
        )
        .order_by(Link.score.desc().nulls_last(), other.id, Link.kind)
        .limit(services.settings.import_links_max)
    ).all()
    entries: list[render.LinkEntry] = []
    for link, linked, source in rows:
        if link.kind == "langlink" or (link.kind == "wikidata" and linked.id in in_languages):
            continue
        wiki_path = linked.meta.get("wiki_path") if linked.wiki_page_id is not None else None
        book = source.meta.get("kiwix_book")
        # Uma lição aponta para o Moodle, onde o curso foi publicado (artigo ↔ lição, secção 6.4).
        lesson_url = linked.meta.get("moodle_url") if linked.content_type == "lesson" else None
        # Um capítulo aponta para o livro no Kavita (artigo ↔ capítulo, secção 6.4).
        chapter_url = _chapter_reader_url(session, services, linked)
        url = (
            f"/{wiki_path}"
            if wiki_path
            else (
                str(lesson_url)
                if lesson_url
                else (
                    chapter_url
                    or (
                        content_url(services.settings.kiwix_public_url, str(book), linked.path)
                        if book
                        else None
                    )
                )
            )
        )
        entries.append(
            render.LinkEntry(
                link.kind, source.title, source.language, linked.title, url, link.score
            )
        )
    return entries


def _chapter_reader_url(session: Session, services: Services, document: DocumentRow) -> str | None:
    if document.content_type != "chapter":
        return None
    book = session.get(BookRow, str(document.meta.get("book", "")))
    return reader_url(services.settings.kavita_public_url, book) if book is not None else None


def _archive(services: Services, wiki: WikiBackend, document_id: str) -> None:
    """O artigo já não existe na fonte: se tinha página, fica arquivada (nunca se apaga)."""
    with services.sessions() as session:
        row = session.get(Curation, document_id)
        document = session.get(DocumentRow, document_id)
        if row is None or document is None or document.wiki_page_id is None:
            raise Permanent(
                f"{document_id!r} does not exist in the source and has no page to archive."
            )
        if row.archived_at is None:
            row.archived_at = datetime.now(UTC)
            session.commit()
    try:
        _publish(services, wiki, document_id, force=True, archived=True)
    except WikiUnavailable as exc:
        raise Defer(datetime.now(UTC) + WIKI_RETRY, f"Wiki.js unavailable: {exc}") from exc
    except WikiError as exc:
        raise RuntimeError(str(exc)) from exc


# --- páginas de domínio ------------------------------------------------------------------------


def update_domain_index(
    services: Services, job: ClaimedJob, stop: threading.Event | None = None
) -> None:
    """Handler do job `wiki_domain_index` (payload `locale`, `domain_id`): escreve a página do
    domínio no locale e, se faltam, as dos domínios de cima (que o listam como subdomínio)."""
    locale, domain_id = job.payload.get("locale"), job.payload.get("domain_id")
    if not isinstance(locale, str) or not isinstance(domain_id, str):
        raise Permanent(f"payload without locale and domain_id: {job.payload!r}")
    wiki = services.get_wiki()
    if wiki is None:
        raise Defer(datetime.now(UTC) + NOT_CONFIGURED_RETRY, "Wiki.js not configured")
    try:
        with services.sessions() as session:
            chain: list[str] = []
            current: str | None = domain_id
            while current is not None and current not in chain:
                node = session.get(DomainRow, current)
                if node is None:
                    raise Permanent(f"Domain {current!r} does not exist.")
                chain.append(current)
                current = node.parent_id
            for node_id in chain:  # o próprio primeiro: é o que mudou
                _write_domain_index(services, session, wiki, node_id, locale)
    except WikiUnavailable as exc:
        raise Defer(datetime.now(UTC) + WIKI_RETRY, f"Wiki.js unavailable: {exc}") from exc
    except WikiError as exc:
        raise RuntimeError(str(exc)) from exc


def _write_domain_index(
    services: Services,
    session: Session,
    wiki: WikiBackend,
    domain_id: str,
    locale: str,
) -> bool:
    """A página `/<locale>/<domínio>`: nome, descrição, subdomínios, artigos e livros, tudo
    ordenado por título. Só se escreve se o texto mudou desde a última vez neste processo (os
    domínios de cima, que só listam subdomínios, escrevem-se uma vez)."""
    node = session.get(DomainRow, domain_id)
    if node is None or not node.active:
        return False
    name = node.names.get(locale) or node.names.get(domains.LOCALE_FALLBACK) or domain_id
    description = (
        node.descriptions.get(locale) or node.descriptions.get(domains.LOCALE_FALLBACK) or ""
    )
    slugs = domains.slugs_for(session, domain_id, locale)
    path = paths.domain_index_path(slugs)
    parent = None
    if node.parent_id is not None:
        up = session.get(DomainRow, node.parent_id)
        if up is not None:
            parent = render.IndexEntry(
                up.names.get(locale) or up.names.get(domains.LOCALE_FALLBACK) or up.id,
                f"/{locale}/{paths.domain_index_path(domains.slugs_for(session, up.id, locale))}",
            )
    children = [
        render.IndexEntry(
            child.names.get(locale) or child.names.get(domains.LOCALE_FALLBACK) or child.id,
            f"/{locale}/{paths.domain_index_path(domains.slugs_for(session, child.id, locale))}",
        )
        for child in session.scalars(
            select(DomainRow)
            .where(DomainRow.parent_id == domain_id, DomainRow.active)
            .order_by(DomainRow.position, DomainRow.id)
        )
    ]
    articles, more_articles = _index_documents(services, session, domain_id, locale, books=False)
    books, more_books = _index_documents(services, session, domain_id, locale, books=True)
    content = render.domain_index(
        locale=locale,
        description=description,
        parent=parent,
        children=children,
        articles=articles,
        books=books,
        more_articles=more_articles,
        more_books=more_books,
    )
    key = f"index:{locale}:{domain_id}"
    digest = render.digest(name, description, content)
    if services.wiki_cache.get(key) == digest:
        return False
    wiki.upsert_page(
        path,
        name,
        content,
        render.summary(description),
        [locale, domain_id, render.DOMAIN_TAG],
        locale=locale,
    )
    services.wiki_cache[key] = digest
    logger.info("Domain page %s/%s written", locale, path)
    return True


def _index_documents(
    services: Services, session: Session, domain_id: str, locale: str, *, books: bool
) -> tuple[list[render.IndexEntry], int]:
    """Os artigos publicados (ou os livros) do domínio neste locale, por título, até
    `render.INDEX_LIMIT`; e quantos ficaram de fora."""
    query = (
        select(DocumentRow, SourceRow)
        .join(DomainAssignment, DomainAssignment.document_id == DocumentRow.id)
        .join(SourceRow, SourceRow.id == DocumentRow.source_id)
        .where(
            DomainAssignment.domain_id == domain_id,
            DocumentRow.meta["wiki_locale"].astext == locale,
            document_is_open(DocumentRow),
        )
    )
    if books:
        query = query.where(
            DocumentRow.meta["route"].astext == "book", DocumentRow.wiki_page_id.is_(None)
        )
    else:
        query = query.join(Curation, Curation.document_id == DocumentRow.id).where(
            DocumentRow.wiki_page_id.is_not(None),
            Curation.archived_at.is_(None),
            Curation.decision != pipeline.REJECTED,
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    shown = func.coalesce(DocumentRow.meta["wiki_title"].astext, DocumentRow.title)
    rows = session.execute(
        query.order_by(func.lower(shown), DocumentRow.id).limit(render.INDEX_LIMIT)
    ).all()
    entries = []
    for document, source in rows:
        if books:
            url = render.kiwix_link(
                services.settings.kiwix_public_url, source.meta.get("kiwix_book"), document.path
            )
        else:
            wiki_path = document.meta.get("wiki_path")
            url = f"/{wiki_path}" if wiki_path else None
        entries.append(render.IndexEntry(_page_title(document), url))
    return entries, max(0, total - len(entries))


# --- escolher o que importar -------------------------------------------------------------------


@dataclass
class ScanReport:
    hot: int = 0
    neighbors: int = 0
    stale: int = 0
    curation: int = 0  # `pending_curator` que voltaram à fila do curador


def _not_queued(document_id: Any) -> ColumnElement[bool]:
    """Sem job `import_page` pendente nem falhado há pouco para o documento."""
    return ~exists().where(
        Job.dedupe_key == func.concat(f"{KIND}:", document_id),
        or_(
            Job.status == "pending",
            and_(Job.status == "failed", Job.finished_at > func.now() - FAILED_COOLDOWN),
        ),
    )


def run_import_scan(services: Services, stop: threading.Event | None = None) -> ScanReport:
    """Enfileira `import_page` para os documentos quentes ainda sem página, os vizinhos dos quentes
    que já têm e as páginas cuja fonte mudou de versão. Sem Wiki.js não faz nada."""
    report = ScanReport()
    if services.get_wiki() is None:
        return report
    try:
        _ensure_tree(services)
    except Exception:  # a árvore é só o caminho das páginas: o scan segue sem ela
        logger.exception("Could not load the domain tree")
    report.curation = _requeue_pending_curation(services)
    settings = services.settings
    half_life = settings.heat_half_life_days
    current = heat.document_heat(half_life)
    threshold = settings.import_heat_threshold
    room = settings.import_batch
    with services.sessions() as session:
        hot = session.execute(
            select(DocumentRow.id, current.label("heat"))
            .join(SourceRow, SourceRow.id == DocumentRow.source_id)
            .where(
                DocumentRow.heat >= threshold,
                current >= threshold,
                SourceRow.status == "active",
                DocumentRow.wiki_page_id.is_(None),
                DocumentRow.content_type == "article",
                DocumentRow.meta["wiki_skip"].astext.is_(None),  # `skip`, livro, língua inativa
                ~exists().where(Curation.document_id == DocumentRow.id),
                _not_queued(DocumentRow.id),
            )
            .order_by(current.desc(), DocumentRow.id)
            .limit(room)
        ).all()
        for document_id, document_heat in hot:
            enqueue_import(session, document_id, float(document_heat))
        report.hot = len(hot)
        room -= len(hot)

        if room > 0:  # um salto só: vizinhos dos documentos quentes que já têm página
            report.neighbors = _enqueue_neighbors(session, services, current, room)
            room -= report.neighbors

        if room > 0:
            stale = session.scalars(
                select(DocumentRow.id)
                .join(SourceRow, SourceRow.id == DocumentRow.source_id)
                .where(
                    DocumentRow.wiki_page_id.is_not(None),
                    SourceRow.status == "active",
                    text("(document.meta ->> 'wiki_version') IS DISTINCT FROM source.version"),
                    _not_queued(DocumentRow.id),
                )
                .order_by(DocumentRow.id)
                .limit(room)
            ).all()
            for document_id in stale:
                enqueue_import(session, document_id, 0.2)
            report.stale = len(stale)
        session.commit()
    if report.hot or report.neighbors or report.stale or report.curation:
        logger.info(
            "Importer: %d hot, %d neighbors, %d with a new source, %d for the curator",
            report.hot,
            report.neighbors,
            report.stale,
            report.curation,
        )
    return report


def _requeue_pending_curation(services: Services) -> int:
    """Põe na fila do curador os `pending_curator` que ficaram sem job: guardados antes de o
    `CURATOR_URL` existir, ou cujo job falhou de vez (volta a tentar-se ao fim do recuo).
    Sem curador local também conta um destino de lote automático: `curation_enabled`."""
    if not services.curation_enabled():
        return 0
    key = func.concat(f"{CURATOR_KIND}:", Curation.document_id)
    with services.sessions() as session:
        waiting = session.scalars(
            select(Curation.document_id)
            .where(
                Curation.decision == pipeline.PENDING,
                ~exists().where(
                    Job.dedupe_key == key,
                    or_(
                        Job.status.in_(("pending", "running", "batched")),
                        and_(
                            Job.status == "failed", Job.finished_at > func.now() - FAILED_COOLDOWN
                        ),
                    ),
                ),
            )
            .order_by(Curation.updated_at, Curation.document_id)
            .limit(services.settings.import_batch)
        ).all()
        for document_id in waiting:
            held = session.get(Curation, document_id)
            if held is not None and not curator_gate(session, services.settings, held):
                continue  # privado e curador remoto: passou à revisão humana
            _enqueue_curation(session, document_id)
        session.commit()
    return len(waiting)


def _enqueue_neighbors(session: Session, services: Services, current: Any, room: int) -> int:
    settings = services.settings
    threshold = settings.import_heat_threshold
    hot_imported = session.execute(
        select(DocumentRow.id, current.label("heat"))
        .where(
            DocumentRow.heat >= threshold,
            current >= threshold,
            DocumentRow.wiki_page_id.is_not(None),
        )
        .order_by(current.desc(), DocumentRow.id)
        .limit(room)
    ).all()
    queued = 0
    for document_id, document_heat in hot_imported:
        if queued >= room:
            break
        other = aliased(DocumentRow)
        candidates = session.execute(
            select(other.id)
            .join(SourceRow, SourceRow.id == other.source_id)
            .join(
                Link,
                or_(
                    and_(Link.src_document == document_id, Link.dst_document == other.id),
                    and_(Link.dst_document == document_id, Link.src_document == other.id),
                ),
            )
            .where(
                Link.status == "accepted",
                Link.kind != "zim_internal",
                SourceRow.status == "active",
                other.wiki_page_id.is_(None),
                other.content_type == "article",
                other.meta["wiki_skip"].astext.is_(None),
                ~exists().where(Curation.document_id == other.id),
                _not_queued(other.id),
            )
            .order_by(Link.score.desc().nulls_last(), other.id)
            .limit(min(settings.import_neighbors_max_per_doc, room - queued))
        ).scalars()
        for neighbor in candidates:
            enqueue_import(session, neighbor, float(document_heat) * 0.5)
            queued += 1
    return queued


__all__ = ["INDEX_KIND", "KIND", "import_page", "run_import_scan", "update_domain_index"]
