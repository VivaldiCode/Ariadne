import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { setLocale } from "../i18n";
import { CurationPage } from "../pages/CurationPage";
import { jsonError, ME, mockApi, renderApp } from "./utils";

const ITEM = {
  document_id: "zim:wikimed/Remedio",
  fingerprint: "abc123",
  title: "Remédio",
  source_id: "zim:wikimed",
  language: "pt",
  decision: "needs_review",
  reason: "A reescrita mudou um número.",
  flags: ["health"],
  sampled: false,
  curator_model: "gemma",
  fidelity_ok: false,
  reviewed_by: null,
  updated_at: "2026-10-24T10:00:00Z",
};

const DETAIL = {
  ...ITEM,
  source_version: "2026-09",
  flag_details: [{ code: "health", details: ["dosage"] }],
  fidelity: { ok: false, problems: ["número 500 desapareceu"] },
  original_md: "Tomar 500 mg por dia.",
  curated_md: "Tomar 50 mg por dia.",
  published_md: null,
  archived: false,
};

describe("CurationPage", () => {
  it("lista a fila, mostra original e curado lado a lado e aprova com a fingerprint", async () => {
    let detail: Record<string, unknown> = DETAIL;
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/curation": { items: [ITEM], next_cursor: null },
      "GET /api/curation/item": () => detail,
      "POST /api/curation/review": () => {
        detail = { ...DETAIL, decision: "published", reviewed_by: "1" };
        return detail;
      },
    });
    renderApp(<CurationPage />, "/?view=queue");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /Remédio/ }));

    expect(await screen.findByText("Tomar 500 mg por dia.")).toBeInTheDocument();
    expect(screen.getByText("Tomar 50 mg por dia.")).toBeInTheDocument();
    expect(screen.getByText("número 500 desapareceu")).toBeInTheDocument();
    expect(screen.getAllByText("health").length).toBeGreaterThan(0);

    await user.type(screen.getByLabelText(/Note/), "ok");
    await user.click(screen.getByRole("button", { name: "Approve and publish" }));

    await waitFor(() => expect(mock.calls.some((c) => c.path === "/api/curation/review")).toBe(true));
    const review = mock.calls.find((c) => c.path === "/api/curation/review");
    expect(review?.body).toEqual({ document_id: "zim:wikimed/Remedio", fingerprint: "abc123", decision: "approve", note: "ok" });
    // Já decidido: os botões de decisão desaparecem.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument());
  });

  it("rejeita, e um 409 (o texto mudou) mostra o aviso e volta a ler o item", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/curation": { items: [ITEM], next_cursor: null },
      "GET /api/curation/item": DETAIL,
      "POST /api/curation/review": () => jsonError(409, "O texto mudou desde que o viste."),
    });
    renderApp(<CurationPage />, "/?view=queue&id=zim:wikimed/Remedio");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Reject" }));

    expect(await screen.findByText("O texto mudou desde que o viste.")).toBeInTheDocument();
    const review = mock.calls.find((c) => c.path === "/api/curation/review");
    expect((review?.body as { decision: string }).decision).toBe("reject");
    await waitFor(() => expect(mock.calls.filter((c) => c.path === "/api/curation/item").length).toBeGreaterThan(1));
  });

  it("muda de estado e pede só os itens desse estado", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/curation": () => ({ items: [], next_cursor: null }),
    });
    renderApp(<CurationPage />, "/?view=queue");
    const user = userEvent.setup();

    await screen.findByText("Nothing in this state.");
    await user.click(screen.getByRole("tab", { name: "Published" }));

    await waitFor(() => {
      const last = mock.calls.filter((c) => c.path === "/api/curation").at(-1);
      expect(new URLSearchParams(last?.search).get("decision")).toBe("published");
    });
  });
});

describe("CurationPage em português", () => {
  it("mostra o texto português quando o idioma é pt", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/curation": () => ({ items: [], next_cursor: null }) });
    setLocale("pt");
    try {
      renderApp(<CurationPage />, "/?view=queue");
      expect(await screen.findByText("Nada neste estado.")).toBeInTheDocument();
    } finally {
      setLocale("en");
    }
  });

  it("um membro vê o detalhe mas não tem os botões de aprovar nem de rejeitar", async () => {
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /api/curation": { items: [ITEM], next_cursor: null },
      "GET /api/curation/item": DETAIL,
    });
    renderApp(<CurationPage />, "/?view=queue");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /Remédio/ }));

    expect(await screen.findByText("Tomar 500 mg por dia.")).toBeInTheDocument();
    expect(await screen.findByText(/Only administrators can change this/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Note/)).not.toBeInTheDocument();
  });

  it("um membro vê os resultados em lote mas não os pode aprovar nem rejeitar", async () => {
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /api/compute/runs": [],
      "GET /api/curation": { items: [{ ...ITEM, title: "Artigo 1" }], next_cursor: null },
    });
    renderApp(<CurationPage />, "/?view=batch");

    expect(await screen.findByText("Artigo 1")).toBeInTheDocument();
    expect(await screen.findByText(/Only administrators can change this/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Select Artigo 1")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve and publish/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject/ })).not.toBeInTheDocument();
  });
});
