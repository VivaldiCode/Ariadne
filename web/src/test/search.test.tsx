import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { setLocale } from "../i18n";

import { SearchPage } from "../pages/SearchPage";
import { ME, mockApi, renderApp } from "./utils";

const ROOMS = [
  { id: 7, wing: "wikipedia_pt_cravos", label: "Revolução dos Cravos · 25 de Abril · Salgueiro Maia", size: 12 },
  { id: 8, wing: "wikipedia_pt_cravos", label: "Fado · Amália Rodrigues", size: 5 },
];

const SOURCES = [
  { id: "zim:wikipedia_pt_cravos", title: "PT", kind: "zim", language: "pt", version: "1", articles: 5, wing: "w", size_bytes: 10, rooms: { count: 0, top: [] } },
  { id: "zim:wikipedia_en_cravos", title: "EN", kind: "zim", language: "en", version: "1", articles: 4, wing: "w", size_bytes: 10, rooms: { count: 0, top: [] } },
];

const HIT = {
  rank: 1,
  document_id: "zim:wikipedia_pt_cravos/Revolucao_dos_Cravos",
  title: "Revolução dos Cravos",
  source_id: "zim:wikipedia_pt_cravos",
  language: "pt",
  snippet: "Golpe militar de <b>25 de abril</b> de 1974.\n\n## Notas",
  level: 3,
  score: 0.905,
  kiwix_url: "http://kiwix/x",
  wiki_url: null,
  wing: "wikipedia_pt_cravos",
  room: null,
  chunk_id: 1,
  original_source: true,
  translated_query: false,
  flags: [],
};

describe("SearchPage", () => {
  it("pesquisa e mostra o nível, a pontuação e o selo de fonte original", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": SOURCES,
      "GET /api/rooms": [],
      "GET /api/search": {
        query_id: 7,
        results: [HIT, { ...HIT, rank: 2, document_id: "zim:wikipedia_en_cravos/Carnation", title: "Carnation Revolution", level: 1, score: 0.5, original_source: false, source_id: "zim:wikipedia_en_cravos", language: "en", snippet: "A coup." }],
        warnings: ["Nível 2 indisponível"],
      },
    });
    renderApp(<SearchPage />);
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText(/Question or terms/), "revolução dos cravos");
    await user.selectOptions(screen.getByLabelText("Language"), "pt");
    await user.click(screen.getByRole("button", { name: "Search" }));

    const list = await screen.findByRole("list", { name: "Results" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(within(first).getByRole("link", { name: "Revolução dos Cravos" })).toHaveAttribute(
      "href",
      "/read?id=zim%3Awikipedia_pt_cravos%2FRevolucao_dos_Cravos",
    );
    expect(within(first).getByText("Level 3 · Semantic")).toBeInTheDocument();
    expect(within(first).getByText("0.91")).toBeInTheDocument();
    expect(within(first).getByText("original source")).toBeInTheDocument();
    expect(first).toHaveTextContent("Golpe militar de 25 de abril de 1974. Notas"); // sem HTML nem marcas
    expect(within(second).getByText("Level 1 · Kiwix")).toBeInTheDocument();
    expect(within(second).queryByText("original source")).not.toBeInTheDocument();
    expect(screen.getByText("Nível 2 indisponível")).toBeInTheDocument();

    const search = mock.calls.find((c) => c.path === "/api/search");
    expect(new URLSearchParams(search?.search).get("q")).toBe("revolução dos cravos");
    expect(new URLSearchParams(search?.search).get("lang")).toBe("pt");
  });

  it("filtra por fonte", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": SOURCES,
      "GET /api/rooms": [],
      "GET /api/search": { query_id: 1, results: [], warnings: [] },
    });
    renderApp(<SearchPage />, "/?q=cravos");
    const user = userEvent.setup();

    await screen.findByText("Nothing found.");
    await user.click(screen.getByLabelText(/wikipedia_en_cravos/));

    await waitFor(() => {
      const last = mock.calls.filter((c) => c.path === "/api/search").at(-1);
      expect(new URLSearchParams(last?.search).getAll("sources")).toEqual(["zim:wikipedia_en_cravos"]);
    });
  });

  it("filtra por sala e mostra a sala de cada resultado", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": SOURCES,
      "GET /api/rooms": ROOMS,
      "GET /api/search": {
        query_id: 3,
        results: [{ ...HIT, room: { id: 7, label: ROOMS[0].label } }],
        warnings: [],
      },
    });
    renderApp(<SearchPage />, "/?q=cravos");
    const user = userEvent.setup();

    const select = await screen.findByLabelText("Room (topic)");
    expect(within(select).getAllByRole("option")).toHaveLength(3); // «todas» + as duas salas
    await user.selectOptions(select, "8");

    await waitFor(() => {
      const last = mock.calls.filter((c) => c.path === "/api/search").at(-1);
      expect(new URLSearchParams(last?.search).getAll("rooms")).toEqual(["8"]);
    });
    expect(screen.getByText(/room: Revolução dos Cravos · 25 de Abril/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Search only in room Revolução dos Cravos/ }));
    await waitFor(() => {
      const last = mock.calls.filter((c) => c.path === "/api/search").at(-1);
      expect(new URLSearchParams(last?.search).getAll("rooms")).toEqual(["7"]);
    });
  });

  it("sem salas calculadas não mostra o filtro por sala", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": SOURCES,
      "GET /api/rooms": [],
      "GET /api/search": { query_id: 1, results: [], warnings: [] },
    });
    renderApp(<SearchPage />, "/?q=cravos");

    await screen.findByText("Nothing found.");
    expect(screen.queryByLabelText("Room (topic)")).not.toBeInTheDocument();
  });

  it("mostra o erro da API", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": SOURCES,
      "GET /api/rooms": [],
      "GET /api/search": () => new Response(JSON.stringify({ detail: "Línguas disponíveis: pt, en" }), { status: 400, headers: { "content-type": "application/json" } }),
    });
    renderApp(<SearchPage />, "/?q=x&lang=zz");
    expect(await screen.findByRole("alert")).toHaveTextContent("Línguas disponíveis: pt, en");
  });

  it("mostra os textos em português com o idioma pt", async () => {
    setLocale("pt");
    try {
      mockApi({ "GET /api/whoami": ME, "GET /api/sources": SOURCES, "GET /api/rooms": [] });
      renderApp(<SearchPage />);
      expect(await screen.findByLabelText(/Pergunta ou termos/)).toBeInTheDocument();
    } finally {
      setLocale("en");
    }
  });
});
