"""A língua original de um conceito (ARCHITECTURE.md, secção 8): `concept.origin_lang`.

Define-se à mão (curadoria: `ariadne concepts set-origin`, `POST /api/concepts/{qid}`) ou por um TSV
`QID<TAB>país` extraído offline do Wikidata (P17 país, P495 país de origem) com uma tabela pequena
país → língua. Sem dados fica NULL: não se inventa. Uma língua posta à mão nunca é substituída pelo
TSV.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ariadne.db.models import Concept
from ariadne.db.models import Document as DocumentRow
from ariadne.documents import LibraryError
from ariadne.sources.zim import LANGUAGE_2
from ariadne.wiki.refresh import enqueue_refresh

MANUAL = "manual"
COUNTRY_PREFIX = "country:"
TSV_CHUNK = 1000

# País (ISO 3166-1 alfa-2) -> língua do evento que aí acontece. Só os países de língua clara: um
# país plurilingue (BE, CH, CA, IN, …) não está na tabela, por isso o conceito fica sem origem.
COUNTRY_LANG = {
    "PT": "pt", "BR": "pt", "AO": "pt", "MZ": "pt", "CV": "pt", "GW": "pt", "ST": "pt", "TL": "pt",
    "ES": "es", "MX": "es", "AR": "es", "CL": "es", "CO": "es", "PE": "es", "VE": "es", "CU": "es",
    "UY": "es", "PY": "es", "BO": "es", "EC": "es", "GT": "es", "HN": "es", "NI": "es", "SV": "es",
    "CR": "es", "PA": "es", "DO": "es",
    "FR": "fr", "DE": "de", "AT": "de", "IT": "it",
    "GB": "en", "US": "en", "IE": "en", "AU": "en", "NZ": "en",
    "NL": "nl", "SE": "sv", "NO": "no", "DK": "da", "FI": "fi", "PL": "pl", "CZ": "cs", "HU": "hu",
    "RO": "ro", "GR": "el", "TR": "tr", "RU": "ru", "UA": "uk", "JP": "ja", "KR": "ko", "CN": "zh",
    "TW": "zh", "VN": "vi", "ID": "id", "IL": "he", "EG": "ar", "SA": "ar", "IR": "fa",
}  # fmt: skip
_QID = re.compile(r"(Q\d+)\s*$", re.IGNORECASE)
_LANG = re.compile(r"^[a-z]{2,3}$")


class ConceptNotFound(LibraryError):
    pass


class ConceptError(LibraryError):
    pass


class ConceptInfo(BaseModel):
    id: int
    wikidata_qid: str | None
    label: str | None
    origin_lang: str | None
    origin_source: str | None
    documents: int  # quantos documentos pertencem ao conceito


def normalize_lang(code: str) -> str:
    """'POR', 'pt', 'pt-BR' -> 'pt'. Recusa o que não parece um código de língua."""
    base = code.strip().casefold().replace("_", "-").split("-")[0]
    base = LANGUAGE_2.get(base, base)
    if not _LANG.match(base):
        raise ConceptError(f"Invalid language code: {code!r} (use ISO 639, e.g. 'pt', 'en').")
    return base


def find_concept(session: Session, ref: str) -> Concept:
    """O conceito por QID (`Q1187566`) ou por id numérico da base de dados (`42`)."""
    ref = ref.strip()
    if re.fullmatch(r"[Qq]\d+", ref):
        concept = session.scalar(select(Concept).where(Concept.wikidata_qid == ref.upper()))
    elif ref.isdigit() and len(ref) < 18:
        concept = session.get(Concept, int(ref))
    else:
        raise ConceptNotFound(f"Invalid concept: {ref!r} (use a QID, e.g. Q1187566).")
    if concept is None:
        raise ConceptNotFound(f"Unknown concept: {ref!r}.")
    return concept


def info(session: Session, concept: Concept) -> ConceptInfo:
    count = session.scalar(
        select(func.count()).select_from(DocumentRow).where(DocumentRow.concept_id == concept.id)
    )
    return ConceptInfo(
        id=concept.id,
        wikidata_qid=concept.wikidata_qid,
        label=concept.label,
        origin_lang=concept.origin_lang,
        origin_source=concept.origin_source,
        documents=count or 0,
    )


def _members(session: Session, concept_ids: list[int]) -> list[str]:
    return list(
        session.scalars(select(DocumentRow.id).where(DocumentRow.concept_id.in_(concept_ids)))
    )


def set_origin(session: Session, ref: str, lang: str | None) -> ConceptInfo:
    """Define (ou, com `lang=None`, limpa) a língua original à mão. As páginas do Wiki.js do
    conceito refazem-se para mostrar o selo «fonte original». Sem commit."""
    concept = find_concept(session, ref)
    code = normalize_lang(lang) if lang else None
    concept.origin_lang = code
    concept.origin_source = MANUAL if code else None
    session.flush()
    enqueue_refresh(session, _members(session, [concept.id]))
    return info(session, concept)


@dataclass
class OriginsReport:
    lines: int = 0
    updated: int = 0  # conceitos com a origem posta ou atualizada
    unchanged: int = 0
    unknown_concept: int = 0  # QID que a biblioteca não tem
    unknown_country: int = 0  # país fora da tabela
    ambiguous: int = 0  # países com línguas diferentes: não se inventa
    manual_kept: int = 0  # já tinham origem posta à mão
    bad_lines: int = 0
    touched: set[int] = field(default_factory=set)

    def summary(self) -> list[str]:
        return [
            f"{self.lines} lines: {self.updated} concepts with an origin, "
            f"{self.unchanged} already had one, {self.manual_kept} set by hand (kept)",
            f"ignored: {self.unknown_concept} QIDs outside the library, {self.unknown_country} "
            f"countries outside the table, {self.ambiguous} ambiguous, "
            f"{self.bad_lines} unreadable lines",
        ]


def import_origins(session: Session, path: Path) -> OriginsReport:
    """Importa um TSV `QID<TAB>país` (cabeçalho e `#` ignorados; vários países por QID são
    permitidos se dão a mesma língua). Só toca nos conceitos que a biblioteca já tem e nunca
    substitui uma origem posta à mão. Sem commit."""
    report = OriginsReport()
    wanted: dict[str, dict[str, str]] = defaultdict(dict)  # QID -> {país: língua}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        report.lines += 1
        columns = line.split("\t")
        match = _QID.search(columns[0].strip()) if len(columns) >= 2 else None
        if match is None:
            if report.lines > 1 or len(columns) < 2:
                report.bad_lines += 1  # a 1.ª linha pode ser o cabeçalho
            else:
                report.lines -= 1
            continue
        country = columns[1].strip().upper()
        language = COUNTRY_LANG.get(country)
        if language is None:
            report.unknown_country += 1
            continue
        wanted[match.group(1).upper()][country] = language
    qids = sorted(wanted)
    for start in range(0, len(qids), TSV_CHUNK):
        chunk = qids[start : start + TSV_CHUNK]
        concepts = {
            c.wikidata_qid: c
            for c in session.scalars(select(Concept).where(Concept.wikidata_qid.in_(chunk)))
        }
        report.unknown_concept += len(chunk) - len(concepts)
        for qid, concept in concepts.items():
            countries = wanted[str(qid)]
            languages = set(countries.values())
            if len(languages) != 1:
                report.ambiguous += 1
                continue
            if concept.origin_source == MANUAL:
                report.manual_kept += 1
                continue
            language = languages.pop()
            source = COUNTRY_PREFIX + sorted(countries)[0]
            if (concept.origin_lang, concept.origin_source) == (language, source):
                report.unchanged += 1
                continue
            session.execute(
                update(Concept)
                .where(Concept.id == concept.id)
                .values(origin_lang=language, origin_source=source)
            )
            report.updated += 1
            report.touched.add(concept.id)
    if report.touched:
        enqueue_refresh(session, _members(session, sorted(report.touched)))
    return report
