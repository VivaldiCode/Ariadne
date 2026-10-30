"""Dobles de teste partilhados."""

import httpx

from ariadne.kiwix import KiwixClient
from tests.zimfactory import ZIM_UUIDS, kiwix_books

PT_BOOK = "wikipedia_pt_teste_2026-09"
EN_BOOK = "wikipedia_en_teste_2026-09"
EN_ARTICLES = {"London": ("London", "<p>London is the capital of the United Kingdom.</p>")}


class FakeKiwix:
    """kiwix-serve de brincar: devolve resultados fixos por livro e recusa línguas misturadas."""

    def __init__(self, results: dict[str, list[tuple[str, str]]]) -> None:
        self.results = results
        self.languages = {PT_BOOK: "por", EN_BOOK: "eng", "wikibooks_pt_teste_2026-09": "por"}
        self.requests: list[httpx.URL] = []
        self.down: set[str] = set()  # livros cujo pedido dá 500
        self.unknown: set[str] = set()  # livros que o kiwix não conhece

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog/v2/entries":
            # o catálogo OPDS: os livros que este kiwix conhece (os 'unknown' não estão lá)
            entries = "".join(
                f"<entry><id>urn:uuid:{uuid}</id><title>{book}</title></entry>"
                for uuid, book in ZIM_UUIDS.items()
                if book not in self.unknown
            )
            return httpx.Response(
                200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
            )
        self.requests.append(request.url)
        books = kiwix_books(request.url.params)
        asked = request.url.params.get_list("books.name") + request.url.params.get_list("books.id")
        if missing := next(
            (a for a, b in zip(asked, books, strict=True) if b in self.unknown), None
        ):
            # como o kiwix real: diz o livro tal como foi pedido (nome ou UUID)
            return httpx.Response(400, text=f"<detail>No such book: {missing}</detail>")
        if len({self.languages[b] for b in books}) > 1:
            return httpx.Response(400, text="<detail>books in different languages</detail>")
        if any(b in self.down for b in books):
            return httpx.Response(500)
        items = "".join(
            f"<item><title>{title}</title><link>/content/{book}/{path}</link>"
            f"<description>excerto de <b>{title}</b></description></item>"
            for book in books
            for path, title in self.results.get(book, [])
        )
        return httpx.Response(200, text=f"<rss><channel>{items}</channel></rss>")

    def client(self) -> KiwixClient:
        return KiwixClient("http://kiwix", client=httpx.Client(transport=httpx.MockTransport(self)))


def default_fake() -> FakeKiwix:
    return FakeKiwix(
        {
            PT_BOOK: [("Lisboa", "Lisboa"), ("Portugal", "Portugal"), ("Outro", "Outro")],
            EN_BOOK: [("London", "London")],
        }
    )
