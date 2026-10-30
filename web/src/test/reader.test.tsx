import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { setLocale } from "../i18n";

import { ReaderPage } from "../pages/ReaderPage";
import { SourcesPage } from "../pages/SourcesPage";
import { ME, mockApi, renderApp } from "./utils";

const ID = "zim:wikipedia_pt_cravos/Revolucao_dos_Cravos";

function document(room: { id: number; label: string } | null) {
  return {
    document_id: ID,
    title: "Revolução dos Cravos",
    source_id: "zim:wikipedia_pt_cravos",
    language: "pt",
    version: "2026-09",
    wing: "wikipedia_pt_cravos",
    room,
    section: null,
    text: "Golpe militar de 25 de abril de 1974.",
    truncated: false,
    total_chars: 37,
    sections: [],
    links: [],
    redirected_from: null,
    kiwix_url: null,
    wiki_url: null,
    flags: [],
  };
}

function reader(room: { id: number; label: string } | null) {
  return mockApi({
    "GET /api/whoami": ME,
    [`GET /api/documents/${encodeURIComponent(ID)}`]: document(room),
    "GET /api/neighbors": { document_id: ID, title: "x", neighbors: [], flags: [] },
    "GET /api/translations": { document_id: ID, title: "x", concept: null, is_original: false, translations: [], flags: [] },
  });
}

describe("ReaderPage", () => {
  it("mostra a sala do artigo e liga à pesquisa dentro dela", async () => {
    reader({ id: 7, label: "Revolução dos Cravos · 25 de Abril" });
    renderApp(<ReaderPage />, `/read?id=${encodeURIComponent(ID)}`);

    expect(await screen.findByText("room: Revolução dos Cravos · 25 de Abril")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Search in this room" })).toHaveAttribute(
      "href",
      "/?q=Revolu%C3%A7%C3%A3o%20dos%20Cravos&room=7",
    );
  });

  it("sem sala calculada não mostra nada", async () => {
    reader(null);
    renderApp(<ReaderPage />, `/read?id=${encodeURIComponent(ID)}`);

    await screen.findByText("Golpe militar de 25 de abril de 1974.");
    expect(screen.queryByText(/^room:/)).not.toBeInTheDocument();
  });
});

describe("ReaderPage em português", () => {
  it("mostra os textos em português com o idioma pt", async () => {
    setLocale("pt");
    try {
      reader(null);
      renderApp(<ReaderPage />, `/read?id=${encodeURIComponent(ID)}`);
      expect(await screen.findByText("Ligações a outras fontes")).toBeInTheDocument();
    } finally {
      setLocale("en");
    }
  });
});

describe("SourcesPage", () => {
  it("lista as salas de cada ala", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/sources": [
        {
          id: "zim:wikipedia_pt_cravos",
          title: "PT",
          kind: "zim",
          language: "pt",
          version: "1",
          articles: 5,
          wing: "wikipedia_pt_cravos",
          size_bytes: 10,
          rooms: { count: 2, top: [{ id: 7, wing: "wikipedia_pt_cravos", label: "Abril de 1974", size: 3 }] },
        },
      ],
      "GET /api/stats/coverage": [
        {
          source_id: "zim:wikipedia_pt_cravos",
          title: "PT",
          language: "pt",
          status: "active",
          articles: 5,
          documents: 5,
          indexed: 1,
          chunks: 2,
          with_article_vector: 5,
          imported: 0,
          curation: {},
          links: 0,
        },
      ],
    });
    renderApp(<SourcesPage />, "/sources");

    expect(await screen.findByText("2 rooms")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abril de 1974" })).toHaveAttribute("href", "/?room=7");
  });
});
