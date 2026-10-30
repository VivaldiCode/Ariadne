"""Tradução de textos e perguntas (STRUCTURE.md, secção 9): partir, proteger, traduzir, repor,
guardar em cache. As falhas degradam sem erro: devolve-se o texto como veio, com um aviso."""

import hashlib
import logging
import re
import threading
import time
from collections.abc import Sequence
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ariadne.config import Settings
from ariadne.db.models import TranslationCache
from ariadne.documents import LibraryError
from ariadne.licenses import PrivateSendBlocked, assert_may_send
from ariadne.router_rules import detect_language
from ariadne.textutil import clean_text
from ariadne.translate import protect
from ariadne.translate.base import TranslateError, TranslateUnavailable, Translator

logger = logging.getLogger(__name__)

_LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_PARAGRAPHS = re.compile(r"(\n[ \t]*\n\s*)")
_SENTENCES = re.compile(r"(?<=[.!?])(\s+)")
AUTO = "auto"


class TranslateRequestError(LibraryError):
    """O pedido de tradução não faz sentido (vazio, grande demais, língua inválida)."""


class TranslateBusy(LibraryError):
    """Há traduções de mais em curso (`TRANSLATE_CONCURRENCY`): tenta daqui a pouco."""


class TranslateResult(BaseModel):
    text: str  # a tradução; igual ao original se `translated` é falso
    source: str  # a língua de origem (detetada, ou «auto» se o motor a decide)
    target: str
    translated: bool  # false: não se traduziu (mesma língua, ou o serviço falhou: ver `warning`)
    cached: bool = False  # veio da cache, sem chamar o LibreTranslate
    warning: str | None = None

    def call_summary(self) -> dict[str, object]:
        """O que fica em `tool_call.result`: línguas e tamanhos, nunca o texto."""
        return {
            "source": self.source,
            "target": self.target,
            "translated": self.translated,
            "cached": self.cached,
            "chars": len(self.text),
            "warning": self.warning,
        }


def normalize_code(code: str) -> str:
    """'PT', 'pt-BR' (mantém a região se vier) -> minúsculas na língua e maiúsculas na região."""
    code = code.strip()
    if not _LANG.match(code):
        raise TranslateRequestError(f"Invalid language code: {code!r} (e.g. 'pt', 'en').")
    language, _, region = code.partition("-")
    return language.casefold() + (
        f"-{region.upper() if len(region) == 2 else region.title()}" if region else ""
    )


def _pieces(text: str, limit: int) -> list[str]:
    """Parágrafos e frases de até `limit` carateres (uma frase maior é cortada à força)."""
    pieces: list[str] = []
    for part in _PARAGRAPHS.split(text):
        if len(part) <= limit:
            pieces.append(part)
            continue
        for sentence in _SENTENCES.split(part):
            for start in range(0, len(sentence), limit):
                pieces.append(sentence[start : start + limit])
    return pieces


def split_chunks(text: str, limit: int) -> list[str]:
    """Parte o texto em pedaços de no máximo `limit` carateres, de preferência entre parágrafos,
    depois entre frases. Um bloco de código com cercas (```) é um átomo: nunca se corta; se for
    maior do que `limit` fica sozinho num pedaço maior (que depois é só um marcador e nunca vai
    ao motor). `''.join(chunks) == text`."""
    if len(text) <= limit and not protect.FENCED.search(text):
        return [text]
    units: list[tuple[str, bool]] = []  # (texto, é um bloco de código)
    cursor = 0
    for match in protect.FENCED.finditer(text):
        units += [(p, False) for p in _pieces(text[cursor : match.start()], limit)]
        units.append((match.group(0), True))
        cursor = match.end()
    units += [(p, False) for p in _pieces(text[cursor:], limit)]
    chunks: list[str] = []
    current = ""
    for unit, is_fence in units:
        if is_fence and len(unit) > limit:  # sozinho
            if current:
                chunks.append(current)
            chunks.append(unit)
            current = ""
            continue
        if current and len(current) + len(unit) > limit:
            chunks.append(current)
            current = ""
        current += unit
    if current:
        chunks.append(current)
    return [c for c in chunks if c] or [text]


def prune_cache(session: Session, ttl_days: float, max_rows: int) -> int:
    """Apaga da cache as traduções com mais de `ttl_days` dias e, se ainda houver mais de
    `max_rows`, as mais antigas. Devolve quantas apagou. Sem commit."""
    removed = len(
        session.scalars(
            delete(TranslationCache)
            .where(TranslationCache.created_at < func.now() - timedelta(days=ttl_days))
            .returning(TranslationCache.key)
        ).all()
    )
    excess = (session.scalar(select(func.count()).select_from(TranslationCache)) or 0) - max_rows
    if excess > 0:
        oldest = (
            select(TranslationCache.key)
            .order_by(TranslationCache.created_at, TranslationCache.key)
            .limit(excess)
        )
        removed += len(
            session.scalars(
                delete(TranslationCache)
                .where(TranslationCache.key.in_(oldest))
                .returning(TranslationCache.key)
            ).all()
        )
    return int(removed)


class TranslationService:
    def __init__(
        self,
        translator: Translator | None,
        sessions: sessionmaker[Session] | None,
        settings: Settings,
    ) -> None:
        self.translator = translator
        self._sessions = sessions
        self._settings = settings
        self._terms = protect.parse_terms(
            settings.translate_protected_terms.strip() or protect.DEFAULT_TERMS
        )  # vazio = a lista de omissão
        self._terms_digest = hashlib.sha256("|".join(self._terms).encode()).hexdigest()[:12]
        self._down_until = 0.0
        # Quantas traduções correm ao mesmo tempo: um motor CPU só aguenta umas poucas.
        self._slots = threading.BoundedSemaphore(max(1, settings.translate_concurrency))

    @property
    def enabled(self) -> bool:
        return self.translator is not None

    # --- textos ------------------------------------------------------------------------------

    def translate(
        self,
        text: str,
        target: str,
        source: str | None = None,
        *,
        document_id: str | None = None,
    ) -> TranslateResult:
        """Traduz um texto (código e termos protegidos). Se o serviço não existe ou falha, devolve
        o texto original com um aviso: nunca levanta por causa do LibreTranslate. Com
        `document_id` (o documento de que o texto vem), o texto de um documento privado só segue
        para um tradutor da lista local (`PRIVATE_ALLOWED_HOSTS`); sem ele, o texto não se sabe
        de onde vem e vale a palavra de quem chama."""
        text = clean_text(text)
        if not text.strip():
            raise TranslateRequestError("The text to translate is empty.")
        limit = self._settings.translate_max_chars
        if len(text) > limit:
            raise TranslateRequestError(
                f"The text has {len(text)} characters; the maximum is {limit}. Translate in parts."
            )
        target = normalize_code(target)
        if document_id is not None and self._sessions is not None:
            try:
                with self._sessions() as session:
                    assert_may_send(
                        session,
                        target="translator",
                        url=self._settings.libretranslate_url,
                        allowed=self._settings.private_allowed_hosts,
                        document_ids=[clean_text(document_id)],
                    )
            except PrivateSendBlocked:
                return self._degraded(
                    text,
                    source or AUTO,
                    target,
                    "Private text: the translator is not in PRIVATE_ALLOWED_HOSTS.",
                )
        source_code = (
            normalize_code(source)
            if source
            else (detect_language(text, ascii_default=None) or AUTO)
        )
        if source_code == target:
            return TranslateResult(text=text, source=source_code, target=target, translated=False)
        return self._run(text, source_code, target, self._settings.translate_timeout_seconds)

    def translate_query(
        self, query: str, source: str, target: str
    ) -> tuple[str | None, str | None]:
        """(pergunta traduzida, aviso). A tradução é `None` se não há serviço, se falhou (e então há
        um aviso) ou se nada mudou: a pesquisa segue só com a pergunta original."""
        if not self.enabled or not self._settings.translate_queries:
            return None, None
        try:
            result = self._run(
                clean_text(query), source, target, self._settings.translate_query_timeout_seconds
            )
        except TranslateBusy:
            return (
                None,
                "Translation busy; the question was searched in its original language only.",
            )
        if not result.translated:
            return None, result.warning
        if result.text.strip().casefold() == query.strip().casefold():
            return None, None
        return result.text, None

    # --- por dentro --------------------------------------------------------------------------

    def _run(self, text: str, source: str, target: str, timeout: float) -> TranslateResult:
        if self.translator is None:
            return self._degraded(
                text, source, target, "Translation is off: LIBRETRANSLATE_URL is missing."
            )
        if time.monotonic() < self._down_until:
            return self._degraded(
                text,
                source,
                target,
                "Translation unavailable (it failed a moment ago); retrying later.",
            )
        if not self._slots.acquire(timeout=timeout):
            raise TranslateBusy(
                "Too many translations in progress; try again shortly (TRANSLATE_CONCURRENCY)."
            )
        try:
            translated, cached = self._translate_chunks(text, source, target, timeout)
        except TranslateUnavailable as exc:
            self._down_until = time.monotonic() + self._settings.translate_down_seconds
            logger.warning("Translation unavailable: %s", exc)
            return self._degraded(text, source, target, f"Translation unavailable ({exc}).")
        except TranslateError as exc:
            logger.warning("Translation failed: %s", exc)
            return self._degraded(text, source, target, f"Translation failed ({exc}).")
        finally:
            self._slots.release()
        return TranslateResult(
            text=translated, source=source, target=target, translated=True, cached=cached
        )

    @staticmethod
    def _degraded(text: str, source: str, target: str, warning: str) -> TranslateResult:
        return TranslateResult(
            text=text, source=source, target=target, translated=False, warning=warning
        )

    def _server_code(self, code: str) -> str:
        """O código que o servidor usa (o português pode apontar para um modelo próprio, PT-PT)."""
        return self._settings.libretranslate_pt_code if code == "pt" else code

    def _translate_chunks(
        self, text: str, source: str, target: str, timeout: float
    ) -> tuple[str, bool]:
        assert self.translator is not None
        chunks = split_chunks(text, self._settings.translate_chunk_chars)
        keys = [self._key(source, target, chunk) for chunk in chunks]
        known = self._cached(keys)
        out: list[str] = []
        all_cached = True
        for chunk, key in zip(chunks, keys, strict=True):
            if key in known:
                out.append(known[key])
                continue
            if len(re.findall(r"```|~~~", chunk)) % 2:
                raise TranslateError("A code block was cut when splitting the text")
            prefix = protect.choose_prefix(chunk)  # um nonce se o texto já tem algo como QXZ0000
            masked, saved = protect.protect(chunk, self._terms, prefix)
            if protect.has_words(masked, prefix):
                all_cached = False
                translated = protect.restore(
                    self.translator.translate(
                        masked,
                        self._server_code(source),
                        self._server_code(target),
                        timeout=timeout,
                    ),
                    saved,
                    prefix,
                )
                self._remember(key, source, target, translated)
            else:
                translated = chunk  # só código, números ou espaços: nada para traduzir
            out.append(translated)
        return "".join(out), all_cached and bool(chunks)

    def _key(self, source: str, target: str, chunk: str) -> str:
        assert self.translator is not None
        raw = "\x00".join(
            [
                self.translator.name,
                self._settings.libretranslate_pt_code,  # outro modelo de português, outra tradução
                protect.PROTECT_VERSION,
                self._terms_digest,
                source,
                target,
                chunk,
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # --- cache (opcional: se a base falhar, traduz-se na mesma) -------------------------------

    def _cached(self, keys: Sequence[str]) -> dict[str, str]:
        if self._sessions is None:
            return {}
        try:
            with self._sessions() as session:
                rows = session.execute(
                    select(TranslationCache.key, TranslationCache.translated).where(
                        TranslationCache.key.in_(list(keys))
                    )
                )
                return {key: translated for key, translated in rows}
        except Exception:
            logger.exception("Translation cache unreadable")
            return {}

    def _remember(self, key: str, source: str, target: str, translated: str) -> None:
        if self._sessions is None or self.translator is None:
            return
        try:
            with self._sessions() as session:
                session.execute(
                    insert(TranslationCache)
                    .values(
                        key=key,
                        source_lang=source,
                        target_lang=target,
                        translated=translated,
                        backend=self.translator.name,
                    )
                    .on_conflict_do_nothing(index_elements=["key"])
                )
                session.commit()
        except Exception:
            logger.exception("Could not store the translation in the cache")
