import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../i18n";

import { LinksPage } from "../pages/LinksPage";
import { OperationsPage } from "../pages/OperationsPage";
import { ME, mockApi, renderApp } from "./utils";

const JOBS = {
  counts: { index_document: { done: 3, failed: 1 } },
  budget: { daily: 100, model_id: "m@1", used: 40, remaining: 60, resets_at: "2026-10-25T00:00:00Z" },
  next: [],
  failed: [
    { id: 9, kind: "index_document", priority: 0.1, document_id: "zim:x/A", run_after: "2026-10-24T10:00:00Z", due: true, attempts: 5, last_error: "boom", status: "failed", finished_at: "2026-10-24T10:01:00Z" },
  ],
};

const CONFIG = {
  embedder_model_id: "m@1",
  index_daily_budget: 100,
  heat_half_life_days: 30,
  neighbors_heat_threshold: 5,
  neighbors_max_per_doc: 50,
  link_tau: 0.88,
  link_tau_accept: 0.94,
  router_tau_high: 0.86,
  router_tau_low: 0.8,
  router_historical_rule: true,
  import_heat_threshold: 3,
  curation_sample_rate: 0,
  translate_queries: true,
  services: { wiki: true, curator: false, translate: false, embedder: "fake" },
};

const BACKUPS = {
  schedule: { enabled: false, recipient_configured: false, interval_days: 7, next_due: null, due_now: false, reason: "desligado", build_pending: false },
  rehearsal: { last_ok_at: null, overdue: false, max_days: 90 },
  last_backup: null,
  last_release: null,
  history: [],
};

const HEALTH = { status: "ok", version: "0.1.0", checks: { database: "ok", pgvector: "0.8.7", schema_revision: "0008", kiwix: "ok", wikijs: "ok" } };

describe("OperationsPage", () => {
  afterEach(() => setLocale("en"));

  it("retries a failed job and shows the new key only once", async () => {
    const created = { id: 5, kind: "agent", name: "claude", role: "member", created_at: "2026-10-24T10:00:00Z", revoked_at: null, active: true, api_key: "ariadne_SEGREDO" };
    let principals = [{ id: 1, kind: "user", name: "pessoa", role: "admin", created_at: "2026-10-24T09:00:00Z", revoked_at: null, active: true }];
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /health": HEALTH,
      "GET /api/jobs": JOBS,
      "GET /api/config": CONFIG,
      "GET /api/backups": BACKUPS,
      "POST /api/jobs/9/retry": { ...JOBS.failed[0], status: "pending", attempts: 0, last_error: null },
      "GET /api/principals": () => principals,
      "POST /api/principals": () => {
        principals = [...principals, { ...created, api_key: undefined } as never];
        return created;
      },
    });
    renderApp(<OperationsPage />);
    const user = userEvent.setup();

    expect(await screen.findByText(/40 of 100 chunks/)).toBeInTheDocument();
    expect(await screen.findByText("schema_revision")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mock.calls.some((c) => c.path === "/api/jobs/9/retry")).toBe(true));

    await user.type(await screen.findByLabelText("Name"), "claude");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByText("ariadne_SEGREDO")).toBeInTheDocument();
    const post = mock.calls.find((c) => c.method === "POST" && c.path === "/api/principals");
    expect(post?.body).toEqual({ name: "claude", kind: "agent", role: "member", scope: "full" });

    await user.click(screen.getByRole("button", { name: "I've saved it" }));
    expect(screen.queryByText("ariadne_SEGREDO")).not.toBeInTheDocument();
    expect(await screen.findByText("claude")).toBeInTheDocument(); // na lista, sem a chave
  });

  it("shows the interface in Portuguese when the locale is pt", async () => {
    setLocale("pt");
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /health": HEALTH,
      "GET /api/jobs": JOBS,
      "GET /api/config": CONFIG,
      "GET /api/backups": BACKUPS,
    });
    renderApp(<OperationsPage />);
    expect(await screen.findByText(/Só os administradores gerem principais/)).toBeInTheDocument();
    expect(await screen.findByText(/40 de 100 chunks/)).toBeInTheDocument();
  });

  it("a member sees the failed jobs but cannot retry them", async () => {
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /health": HEALTH,
      "GET /api/jobs": JOBS,
      "GET /api/config": CONFIG,
      "GET /api/backups": BACKUPS,
    });
    renderApp(<OperationsPage />);
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("non-administrators do not see key management", async () => {
    const mock = mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /health": HEALTH,
      "GET /api/jobs": JOBS,
      "GET /api/config": CONFIG,
      "GET /api/backups": BACKUPS,
    });
    renderApp(<OperationsPage />);
    expect(await screen.findByText(/Only administrators manage principals/)).toBeInTheDocument();
    expect(mock.calls.some((c) => c.path === "/api/principals")).toBe(false);
  });
});

const LINK = { id: 3, src_document: "zim:a/X", dst_document: "zim:b/Y", src_title: "X", dst_title: "Y", src_source: "zim:a", dst_source: "zim:b", kind: "vector", score: 0.9, model_id: "m", status: "proposed", created_by: "linker", created_at: "2026-10-24T10:00:00Z", reviewed_by: null, reviewed_at: null };

describe("LinksPage", () => {
  it("aceita uma proposta", async () => {
    const link = { id: 3, src_document: "zim:a/X", dst_document: "zim:b/Y", src_title: "X", dst_title: "Y", src_source: "zim:a", dst_source: "zim:b", kind: "vector", score: 0.9, model_id: "m", status: "proposed", created_by: "linker", created_at: "2026-10-24T10:00:00Z", reviewed_by: null, reviewed_at: null };
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/links": { items: [link], next_cursor: null },
      "POST /api/links/3": { ...link, status: "accepted" },
      "GET /api/sources": [],
      "GET /api/collections": [],
    });
    renderApp(<LinksPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() => expect(mock.calls.some((c) => c.path === "/api/links/3")).toBe(true));
    expect(mock.calls.find((c) => c.path === "/api/links/3")?.body).toEqual({ status: "accepted" });
    const first = mock.calls.find((c) => c.path === "/api/links");
    expect(new URLSearchParams(first?.search).get("status")).toBe("proposed");
  });

  it("um membro vê as propostas mas não as pode rever nem criar ligações", async () => {
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /api/links": { items: [LINK], next_cursor: null },
      "GET /api/sources": [],
      "GET /api/collections": [],
    });
    renderApp(<LinksPage />);

    expect(await screen.findByText("Y")).toBeInTheDocument();
    expect(await screen.findAllByText(/Only administrators can change this/)).not.toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create link" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add to queue" })).not.toBeInTheDocument();
  });
});
