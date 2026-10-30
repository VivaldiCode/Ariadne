"""Nível 1 com muitos livros: pesquisa por UUID em lotes (o kiwix recusa URLs compridos)."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ariadne.kiwix import KiwixBookMissing, KiwixHit
from ariadne.search import _search_group


@dataclass
class _Source:
    meta: dict[str, Any]
    id: str = ""

    def archive_only_for(self, path: str) -> bool:
        return False


@dataclass
class _Kiwix:
    missing: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)
    catalog: set[str] | None = None  # None = catálogo indisponível

    def known_book_ids(self) -> set[str] | None:
        return self.catalog

    def search(self, query: str, books: Any = None, k: int = 10, *, book_ids: Any = None) -> Any:
        ids = list(book_ids or [])
        self.calls.append(ids)
        for book_id in ids:
            if book_id in self.missing:
                raise KiwixBookMissing(book_id)
        # cada livro dá um resultado, pela ordem pedida
        return [KiwixHit(book=f"livro{i[1:]}", path=f"A{i[1:]}", title=i, snippet="") for i in ids]


def _services(kiwix: _Kiwix, batch: int) -> Any:
    return SimpleNamespace(kiwix=kiwix, settings=SimpleNamespace(kiwix_search_batch=batch))


def _sources(n: int) -> list[Any]:
    return [_Source({"kiwix_book": f"livro{i}", "uuid": f"u{i}"}, id=f"zim:s{i}") for i in range(n)]


def test_lotes_por_uuid_e_intercalados() -> None:
    kiwix = _Kiwix()
    warnings: list[str] = []
    found = _search_group(_services(kiwix, 2), 0, _sources(5), "q", 10, warnings)

    assert sorted(map(sorted, kiwix.calls)) == [["u0", "u1"], ["u2", "u3"], ["u4"]]
    # intercalado: o 1.º de cada lote, depois o 2.º de cada lote
    assert [c.title for c in found] == ["u0", "u2", "u4", "u1", "u3"]
    assert [c.score for c in found] == [1, 1 / 2, 1 / 3, 1 / 4, 1 / 5]
    assert warnings == []


def test_livro_em_falta_so_sai_do_seu_lote() -> None:
    kiwix = _Kiwix(missing={"u1"})
    warnings: list[str] = []
    found = _search_group(_services(kiwix, 2), 0, _sources(4), "q", 10, warnings)

    assert {c.title for c in found} == {"u0", "u2", "u3"}
    assert any("livro1" in w for w in warnings)  # o aviso diz o nome do ficheiro, não o UUID


def test_corta_em_k() -> None:
    found = _search_group(_services(_Kiwix(), 3), 0, _sources(9), "q", 4, [])
    assert len(found) == 4


def test_livro_em_falta_que_nao_esta_no_lote_nao_fica_em_ciclo() -> None:
    """Se o kiwix disser um livro que não está no pedido, não há o que tirar: o lote desiste."""

    class Teimoso(_Kiwix):
        def search(
            self, query: str, books: Any = None, k: int = 10, *, book_ids: Any = None
        ) -> Any:
            self.calls.append(list(book_ids or []))
            raise KiwixBookMissing("outro-livro")

    kiwix = Teimoso()
    warnings: list[str] = []
    try:
        _search_group(_services(kiwix, 2), 0, _sources(2), "q", 10, warnings)
    except Exception as exc:  # SearchUnavailable: nenhum lote respondeu
        assert "outro-livro" in str(exc)
    assert len(kiwix.calls) == 1


def test_so_pede_os_livros_do_catalogo_do_kiwix() -> None:
    """ZIM novos que o kiwix ainda não carregou não custam um pedido falhado cada um."""
    kiwix = _Kiwix(catalog={"u0", "u2"})
    warnings: list[str] = []
    found = _search_group(_services(kiwix, 10), 0, _sources(5), "q", 10, warnings)

    assert kiwix.calls == [["u0", "u2"]]
    assert {c.title for c in found} == {"u0", "u2"}
    assert len(warnings) == 1
    assert "3 book(s) are not in kiwix-serve yet" in warnings[0]
    assert "livro1" in warnings[0]  # pelo nome do ficheiro, não pelo UUID
