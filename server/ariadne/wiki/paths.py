"""Caminhos das páginas do Wiki.js.

Os antigos, `<fonte>/<língua>/<título-slug>` (ARCHITECTURE.md, secção 4.4), e os da importação v2,
`<domínio>/<subdomínio>/<título-slug>` (docs/WIKI-IMPORT.md, secção 6): o locale vai à parte
para o Wiki.js (`/<locale>/<caminho>`) e não faz parte do caminho."""

import hashlib
import re
import unicodedata
from collections.abc import Sequence

MAX_SLUG = 80
_NON_WORD = re.compile(r"[^\w]+")


def slugify(text: str) -> str:
    """Minúsculas, sem acentos nas letras latinas, `-` entre palavras, sem `/`: «Revolução dos
    Cravos» -> `revolucao-dos-cravos`. As letras de outras escritas (cirílico, CJK, devanagari…)
    mantêm-se inteiras: só se tiram as marcas combinantes que vêm depois de uma letra ASCII."""
    kept: list[str] = []
    for char in unicodedata.normalize("NFKD", text):
        if unicodedata.category(char) == "Mn" and kept and kept[-1].isascii():
            continue
        kept.append(char)
    slug = _NON_WORD.sub("-", unicodedata.normalize("NFC", "".join(kept)).casefold())
    slug = slug.replace("_", "-").strip("-")
    if len(slug) > MAX_SLUG:
        slug = slug[:MAX_SLUG].rsplit("-", 1)[0] if "-" in slug[:MAX_SLUG] else slug[:MAX_SLUG]
    return slug.strip("-")


def source_slug(source_id: str) -> str:
    """'zim:wikipedia_en_ray-charles_nopic' -> 'wikipedia-en-ray-charles-nopic'."""
    return slugify(source_id.split(":", 1)[-1])


def short_hash(text: str, size: int = 6) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:size]


def page_path(source_id: str, language: str | None, title: str, document_id: str) -> str:
    """O caminho do documento. Um título que não dê slug (vazio) usa o hash do documento."""
    slug = slugify(title) or short_hash(document_id, 10)
    return f"{source_slug(source_id)}/{slugify(language or '') or 'xx'}/{slug}"


def disambiguate(path: str, document_id: str) -> str:
    """Dois documentos com o mesmo slug (`A/B` e `A B`): o 2.º ganha um sufixo estável."""
    return f"{path}-{short_hash(document_id)}"


def domain_page_path(locale: str, domain_slugs: Sequence[str], title: str, document_id: str) -> str:
    """O caminho da página na árvore de domínios: `<domínio>/<subdomínio>/<título-slug>`, por
    exemplo `transportes/automoveis/mercedes-benz-battery-charge-warning-light`. `domain_slugs` são
    os *slugs* da raiz até ao nó, já no locale da página (`domains.slugs_for`). O `locale` não
    entra no caminho (o Wiki.js põe-no à frente); recebe-se para o chamador dizer de que página se
    trata. Um título que não dê slug (vazio) usa o hash do documento."""
    slug = slugify(title) or short_hash(document_id, 10)
    return "/".join([*_clean_slugs(domain_slugs), slug])


def domain_index_path(domain_slugs: Sequence[str]) -> str:
    """O caminho da página de um domínio (`transportes/automoveis`)."""
    return "/".join(_clean_slugs(domain_slugs))


def _clean_slugs(domain_slugs: Sequence[str]) -> list[str]:
    return [slug for slug in (slugify(s) for s in domain_slugs) if slug]
