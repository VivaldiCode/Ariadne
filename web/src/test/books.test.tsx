import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../i18n";
import { BooksPage } from "../pages/BooksPage";
import { LicensesPage } from "../pages/LicensesPage";
import { ReaderPage } from "../pages/ReaderPage";
import { Result } from "../pages/SearchPage";
import { ME, jsonError, mockApi, renderApp } from "./utils";

afterEach(() => {
  vi.restoreAllMocks();
  setLocale("en");
});

const CHAPTER_ID = "book:livro-de-teste/002-a-viagem";

function book(overrides: Record<string, unknown> = {}) {
  return {
    id: "livro-de-teste",
    source_id: "book:livro-de-teste",
    title: "Livro de Teste",
    authors: ["Ana Autora"],
    language: "pt",
    publisher: null,
    published: null,
    format: "epub",
    chapters: 3,
    pages: null,
    characters: 12000,
    license_class: "open",
    license_name: "Domínio público",
    attribution: "Projeto Gutenberg",
    status: "active",
    extraction_note: "índice do EPUB (nível 1, 3 capítulos)",
    calibre_id: 4,
    reader_url: "http://kavita/library/2/series/7/book/9",
    held_chapters: 1,
    indexed_chapters: 2,
    added_at: "2026-10-24T10:00:00Z",
    indexed_at: "2026-10-24T10:01:00Z",
    ...overrides,
  };
}

const DETAIL = {
  ...book(),
  description: "",
  identifiers: {},
  chapter_list: [
    { document_id: "book:livro-de-teste/001-um", position: 1, title: "Um", characters: 4000, start_page: null, end_page: null, held_reason: null, license_class: "open", indexed: true },
    { document_id: CHAPTER_ID, position: 2, title: "A viagem", characters: 5000, start_page: 3, end_page: 8, held_reason: "à espera do curador (flags: injection)", license_class: "open", indexed: false },
  ],
};

describe("BooksPage", () => {
  it("em português mostra o texto português", async () => {
    setLocale("pt");
    mockApi({ "GET /api/whoami": ME, "GET /api/books": [] });
    renderApp(<BooksPage />, "/books");

    expect(await screen.findByText(/Ainda não há livros/)).toBeInTheDocument();
  });

  it("lista os livros com a classe de licença, o leitor e o estado da curadoria", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/books": [book(), book({ id: "outro", title: "Livro comprado", license_class: "private", license_name: "Comprado", reader_url: null, held_chapters: 0 })] });
    renderApp(<BooksPage />, "/books");

    expect(await screen.findByText("Livro de Teste")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("private")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Read in Kavita" })).toHaveAttribute("href", "http://kavita/library/2/series/7/book/9");
    expect(screen.getByText("not in Kavita")).toBeInTheDocument();
    expect(screen.getByText("1 flagged by curation")).toBeInTheDocument();
    expect(screen.getAllByText(/índice do EPUB/)).toHaveLength(2);
  });

  it("mostra os capítulos extraídos e filtra os assinalados", async () => {
    const user = userEvent.setup();
    mockApi({ "GET /api/whoami": ME, "GET /api/books": [book()], "GET /api/books/livro-de-teste": DETAIL });
    renderApp(<BooksPage />, "/books?book=livro-de-teste");

    expect(await screen.findByRole("link", { name: "Um" })).toHaveAttribute("href", `/read?id=${encodeURIComponent("book:livro-de-teste/001-um")}`);
    expect(screen.getByText("level 3")).toBeInTheDocument();
    expect(screen.getByTitle("à espera do curador (flags: injection)")).toHaveTextContent("flagged");
    expect(screen.getByText(/pp\. 3–8/)).toBeInTheDocument();

    await user.click(screen.getByLabelText(/only those flagged/));
    expect(screen.queryByRole("link", { name: "Um" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "A viagem" })).toBeInTheDocument();
  });

  it("o administrador pede a indexação do livro", async () => {
    const user = userEvent.setup();
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/books": [book()],
      "POST /api/books/livro-de-teste/index": { book_id: "livro-de-teste", chapters: 3 },
    });
    renderApp(<BooksPage />, "/books");

    await user.click(await screen.findByRole("button", { name: "Index the chapters (level 3)" }));

    expect(await screen.findByText(/Indexing requested/)).toBeInTheDocument();
    expect(mock.calls.some((c) => c.method === "POST" && c.path === "/api/books/livro-de-teste/index")).toBe(true);
  });

  it("tornar um livro privado aberto pede confirmação e envia confirm", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/books": [book({ license_class: "private" })],
      "PUT /api/books/livro-de-teste/license": book(),
    });
    renderApp(<BooksPage />, "/books");

    await user.click(await screen.findByRole("button", { name: "Make open…" }));

    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({ license_class: "open", confirm: true });
  });

  it("se a pessoa não confirma, nada se envia", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const mock = mockApi({ "GET /api/whoami": ME, "GET /api/books": [book({ license_class: "private" })] });
    renderApp(<BooksPage />, "/books");

    await user.click(await screen.findByRole("button", { name: "Make open…" }));

    expect(mock.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("um membro vê os livros mas não gere nada", async () => {
    mockApi({ "GET /api/whoami": { ...ME, role: "member" }, "GET /api/books": [book()] });
    renderApp(<BooksPage />, "/books");

    await screen.findByText("Livro de Teste");
    expect(screen.queryByRole("button", { name: /Index|Make/ })).not.toBeInTheDocument();
  });

  it("sem livros explica como os adicionar, e um erro aparece", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/books": [] });
    const { unmount } = renderApp(<BooksPage />, "/books");
    expect(await screen.findByText(/No books yet/)).toBeInTheDocument();
    unmount();

    mockApi({ "GET /api/whoami": ME, "GET /api/books": () => jsonError(500, "base de dados em baixo") });
    renderApp(<BooksPage />, "/books");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("base de dados em baixo"));
  });
});

describe("Resultado e leitor de um capítulo", () => {
  it("o resultado mostra o livro, a citação e o link do Kavita", async () => {
    mockApi({ "GET /api/whoami": ME });
    renderApp(
      <Result
        hit={{
          rank: 1,
          document_id: CHAPTER_ID,
          title: "A viagem",
          source_id: "book:livro-de-teste",
          language: "pt",
          snippet: "<<<DATA>>>O comboio parte.<<<END_DATA>>>",
          level: 2,
          score: 1,
          kiwix_url: null,
          flags: [],
          original_source: false,
          translated_query: false,
          content_type: "chapter",
          position: 321,
          citation: `${CHAPTER_ID}#321`,
          reader_url: "http://kavita/library/2/series/7/book/9",
          book: { ...DETAIL, chapter_position: 2, chapters: 3, chapter_path: "002-a-viagem", format: "epub", start_page: null, end_page: null } as never,
        }}
      />,
    );

    expect(await screen.findByText("chapter")).toBeInTheDocument();
    expect(screen.getByText(/Livro de Teste — Ana Autora · chapter 2 of 3/)).toBeInTheDocument();
    expect(screen.getByText(`${CHAPTER_ID}#321`)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Kavita" })).toHaveAttribute("href", "http://kavita/library/2/series/7/book/9");
    expect(screen.getByRole("link", { name: "A viagem" })).toHaveAttribute("href", `/read?id=${encodeURIComponent(CHAPTER_ID)}&offset=321`);
  });

  it("o leitor mostra o livro, os capítulos vizinhos e a citação, e lê a partir da posição", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      [`GET /api/documents/${encodeURIComponent(CHAPTER_ID)}`]: {
        document_id: CHAPTER_ID,
        title: "A viagem",
        source_id: "book:livro-de-teste",
        language: "pt",
        version: "abc",
        wing: "book-livro-de-teste",
        room: null,
        section: null,
        text: "O comboio parte.",
        truncated: false,
        total_chars: 400,
        sections: [],
        links: [],
        flags: [],
        content_type: "chapter",
        offset: 321,
        citation: `${CHAPTER_ID}#321`,
        book: {
          book_id: "livro-de-teste",
          title: "Livro de Teste",
          authors: ["Ana Autora"],
          language: "pt",
          format: "epub",
          chapter_position: 2,
          chapters: 3,
          chapter_path: "002-a-viagem",
          license_class: "private",
          license_name: "Comprado",
          attribution: "",
          reader_url: "http://kavita/library/2/series/7/book/9",
          start_page: null,
          end_page: null,
          held_reason: null,
          previous: { document_id: "book:livro-de-teste/001-um", title: "Um" },
          next: { document_id: "book:livro-de-teste/003-tres", title: "Três" },
        },
      },
      "GET /api/neighbors": { document_id: CHAPTER_ID, title: "A viagem", neighbors: [], flags: [] },
      "GET /api/translations": { document_id: CHAPTER_ID, title: "A viagem", concept: null, is_original: false, translations: [], flags: [] },
    });
    renderApp(<ReaderPage />, `/read?id=${encodeURIComponent(CHAPTER_ID)}&offset=321`);

    const panel = await screen.findByRole("region", { name: "A chapter from a book" });
    expect(within(panel).getByText(/chapter 2 of 3/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "← Um" })).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Três →" })).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Open in Kavita" })).toHaveAttribute("href", "http://kavita/library/2/series/7/book/9");
    expect(within(panel).getByText(`${CHAPTER_ID}#321`)).toBeInTheDocument();
    expect(within(panel).getByText(/Comprado \(private\)/)).toBeInTheDocument();
    expect(screen.getByText(/Showing from position 321/)).toBeInTheDocument();
    expect(mock.calls.find((c) => c.path.startsWith("/api/documents/"))?.search).toContain("offset=321");
  });
});

const OVERVIEW = {
  sources: [
    { id: "zim:wikipedia_pt", title: "Wikipédia", kind: "zim", status: "active", license_class: "open", documents: 10, private_documents: 0 },
    { id: "book:livro-de-teste", title: "Livro de Teste", kind: "book", status: "active", license_class: "private", documents: 3, private_documents: 3 },
  ],
  private_sources: 1,
  open_sources: 1,
  private_documents: 3,
  open_documents: 10,
  outputs: [
    { name: "Exportação de vetores (ariadne vectors build --out)", mode: "recusa", description: "Recusa qualquer fonte privada." },
    { name: "Espelho MemPalace", mode: "filtra", description: "Deixa de fora o privado." },
  ],
  blocks: [{ id: 1, created_at: "2026-10-24T10:00:00Z", output: "vectors build --out", action: "blocked", items: 1, sample: [{ kind: "source", id: "book:livro-de-teste", because: "book:livro-de-teste" }] }],
};

describe("LicensesPage", () => {
  it("mostra a classe de cada fonte, as saídas guardadas e os bloqueios aplicados", async () => {
    const user = userEvent.setup();
    mockApi({ "GET /api/whoami": ME, "GET /api/licenses": OVERVIEW });
    renderApp(<LicensesPage />, "/licenses");

    expect(await screen.findByText("Private sources")).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    const privateRow = rows.find((r) => within(r).queryByText("Livro de Teste"))!;
    expect(within(privateRow).getByText("private")).toBeInTheDocument();
    expect(screen.getByText("Espelho MemPalace")).toBeInTheDocument();
    expect(screen.getByText("recusa")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Applied blocks" }));
    expect(screen.getByText("refused")).toBeInTheDocument();
    expect(screen.getByText(/source book:livro-de-teste/)).toBeInTheDocument();
  });

  it("lista os itens com a classe e filtra por classe", async () => {
    const user = userEvent.setup();
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/licenses": OVERVIEW,
      "GET /api/licenses/documents": ({ url }: { url: URL }) => ({
        total: 1,
        items: [{ document_id: "book:livro-de-teste/001-um", title: "Um", source_id: "book:livro-de-teste", content_type: "chapter", license_class: url.searchParams.get("license_class") ?? "private", because: "fonte privada" }],
      }),
    });
    renderApp(<LicensesPage />, "/licenses");

    await user.click(await screen.findByRole("tab", { name: "Items" }));
    expect(await screen.findByRole("link", { name: "Um" })).toBeInTheDocument();
    expect(screen.getByText(/fonte privada/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Class"), "private");
    await waitFor(() => expect(mock.calls.some((c) => c.search.includes("license_class=private"))).toBe(true));
  });
});
