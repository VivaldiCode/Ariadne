import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DOWNLOAD_PHASES } from "../lib/format";
import { OperationsPage } from "../pages/OperationsPage";
import { SourcesPage } from "../pages/SourcesPage";
import { ME, jsonError, mockApi, renderApp } from "./utils";

afterEach(() => vi.restoreAllMocks());

const MEMBER = { ...ME, role: "member" };

function item(overrides: Record<string, unknown> = {}) {
  return {
    id: "758a53be-3d67-65ae-ecac-b76a29a9d5e5",
    name: "wikipedia_cr_all",
    title: "Wikipedia (cree)",
    summary: "offline version of Wikipedia in Cree",
    language: "cre",
    category: "wikipedia",
    flavour: "maxi",
    tags: ["wikipedia"],
    article_count: 1,
    media_count: 8,
    size_bytes: 189440,
    updated: "2026-08-04",
    download_url: "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_cr_all_maxi_2026-08.zim.meta4",
    stem: "wikipedia_cr_all_maxi_2026-08",
    installed: false,
    installed_source: null,
    installed_version: null,
    update_available: false,
    archived: false,
    download: null,
    ...overrides,
  };
}

const COVERAGE = [
  { source_id: "zim:wikipedia_pt_teste", title: "Wikipédia PT", language: "pt", status: "active", articles: 10, documents: 3, indexed: 2, chunks: 5, with_article_vector: 1, imported: 0, curation: {}, links: 0 },
  { source_id: "zim:velha", title: "Velha", language: "en", status: "archived", articles: 4, documents: 0, indexed: 0, chunks: 0, with_article_vector: 0, imported: 0, curation: {}, links: 0 },
];

function routes(extra: Record<string, unknown> = {}, me: object = ME) {
  return {
    "GET /api/whoami": me,
    "GET /api/sources": [],
    "GET /api/stats/coverage": COVERAGE,
    "GET /api/catalog": { total: 2, start: 0, count: 2, items: [item(), item({ id: "2", name: "devdocs_en_python", title: "Python docs", installed: true, installed_version: "2026-10", stem: "x" })] },
    "GET /api/catalog/filters": { languages: [{ code: "cre", title: "Cree", count: 1 }], categories: [{ code: "wikipedia", title: "wikipedia", count: 1 }] },
    "GET /api/catalog/downloads": [],
    ...extra,
  };
}

describe("Kiwix catalog", () => {
  it("lists the catalog with what is already installed and lets the administrator download", async () => {
    const user = userEvent.setup();
    const mock = mockApi(
      routes({
        "POST /api/catalog/758a53be-3d67-65ae-ecac-b76a29a9d5e5/download": new Response(
          JSON.stringify({ job_id: 4, book: "758a53be-3d67-65ae-ecac-b76a29a9d5e5", name: "wikipedia_cr_all", title: "Wikipedia (cree)", status: "pending", phase: "queued", bytes: 0, total: 189440, filename: null, source_id: null, served: null, error: null, requested_by: "pessoa", created_at: "2026-10-25T00:00:00Z", finished_at: null }),
          { status: 202, headers: { "content-type": "application/json" } },
        ),
      }),
    );
    renderApp(<SourcesPage />, "/sources");

    expect(await screen.findByText("Wikipedia (cree)")).toBeInTheDocument();
    expect(screen.getByText("Python docs")).toBeInTheDocument();
    expect(screen.getByText(/installed \(2026-10\)/)).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Download" });
    expect(buttons).toHaveLength(1); // o instalado não tem botão

    await user.click(buttons[0]);
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.path.endsWith("/download"))).toBe(true));
  });

  it("without a sha-256 the download fails and the administrator can allow it without a checksum, on purpose", async () => {
    const user = userEvent.setup();
    const failed = { job_id: 6, book: "758a53be-3d67-65ae-ecac-b76a29a9d5e5", name: "wikipedia_cr_all", title: "Wikipedia (cree)", status: "failed", phase: "failed", bytes: 0, total: null, filename: null, source_id: null, served: null, error: "o catálogo não dá o sha-256 deste ficheiro", requested_by: "pessoa", created_at: "2026-10-25T00:00:00Z", finished_at: "2026-10-25T00:00:05Z" };
    const mock = mockApi(
      routes({
        "GET /api/catalog": { total: 1, start: 0, count: 1, items: [item({ download: failed })] },
        "GET /api/catalog/downloads": [failed],
        "POST /api/catalog/758a53be-3d67-65ae-ecac-b76a29a9d5e5/download": new Response(JSON.stringify({ ...failed, status: "pending", phase: "queued", error: null }), { status: 202, headers: { "content-type": "application/json" } }),
      }),
    );
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderApp(<SourcesPage />, "/sources");
    await user.click(await screen.findByRole("button", { name: "Download without checksum" }));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.search.includes("allow_unverified=true"))).toBe(true));
  });

  it("a member sees the catalog but cannot download", async () => {
    mockApi(routes({}, MEMBER));
    renderApp(<SourcesPage />, "/sources");
    expect(await screen.findByText("Wikipedia (cree)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
    expect(screen.getByText(/Only administrators can download/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("shows progress and status and allows cancelling", async () => {
    const user = userEvent.setup();
    const running = { job_id: 4, book: "758a53be-3d67-65ae-ecac-b76a29a9d5e5", name: "wikipedia_cr_all", title: "Wikipedia (cree)", status: "running", phase: "downloading", bytes: 94720, total: 189440, filename: "x.zim", source_id: null, served: null, error: null, requested_by: "pessoa", created_at: "2026-10-25T00:00:00Z", finished_at: null };
    const mock = mockApi(
      routes({
        "GET /api/catalog": { total: 1, start: 0, count: 1, items: [item({ download: running })] },
        "GET /api/catalog/downloads": [running],
        "DELETE /api/catalog/downloads/4": { result: "cancelling" },
      }),
    );
    renderApp(<SourcesPage />, "/sources");
    const panel = (await screen.findByRole("heading", { name: "Downloads" })).closest("div") as HTMLElement;
    expect(within(panel).getByText(DOWNLOAD_PHASES.downloading)).toBeInTheDocument();
    expect(within(panel).getByText(/92.5 KB of 185 KB/)).toBeInTheDocument();
    expect(within(panel).getAllByRole("progressbar")[0]).toHaveAttribute("aria-valuenow", "50");
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE" && c.path === "/api/catalog/downloads/4")).toBe(true));
  });

  it("a finished download says kiwix is already serving it", async () => {
    const done = { job_id: 5, book: "x", name: "wikipedia_cr_all", title: "Wikipedia (cree)", status: "done", phase: "done", bytes: 189440, total: 189440, filename: "x.zim", source_id: "zim:wikipedia_cr_all_maxi", served: true, error: null, requested_by: "pessoa", created_at: "2026-10-25T00:00:00Z", finished_at: "2026-10-25T00:01:00Z" };
    mockApi(routes({ "GET /api/catalog/downloads": [done] }));
    renderApp(<SourcesPage />, "/sources");
    expect(await screen.findByText("served by kiwix")).toBeInTheDocument();
    expect(screen.getByText(/registered as zim:wikipedia_cr_all_maxi/)).toBeInTheDocument();
  });

  it("filters by language and search, and shows the catalog error", async () => {
    const user = userEvent.setup();
    let fail = false;
    const mock = mockApi(
      routes({
        "GET /api/catalog": () => (fail ? jsonError(502, "catálogo inacessível (ConnectError)") : { total: 1, start: 0, count: 1, items: [item()] }),
      }),
    );
    renderApp(<SourcesPage />, "/sources");
    await screen.findByText("Wikipedia (cree)");
    await user.selectOptions(screen.getByLabelText("Language"), "cre");
    await user.type(screen.getByLabelText("Search the catalog"), "cree{Enter}");
    await waitFor(() => expect(mock.calls.some((c) => c.path === "/api/catalog" && c.search.includes("lang=cre") && c.search.includes("q=cree"))).toBe(true));
    fail = true;
    await user.type(screen.getByLabelText("Search the catalog"), "x{Enter}");
    expect(await screen.findByText(/catálogo inacessível/)).toBeInTheDocument();
  });
});

describe("Managing ZIMs in Sources", () => {
  it("archives, restores and only deletes the file after typing the id", async () => {
    const user = userEvent.setup();
    const mock = mockApi(
      routes({
        "DELETE /api/sources/zim%3Awikipedia_pt_teste": { source_id: "zim:wikipedia_pt_teste", action: "archived", file: "x.zim" },
        "POST /api/sources/zim%3Avelha/restore": { source_id: "zim:velha", action: "restored", file: "v.zim" },
        "DELETE /api/sources/zim%3Avelha": { source_id: "zim:velha", action: "deleted", file: "v.zim" },
      }),
    );
    renderApp(<SourcesPage />, "/sources");
    await screen.findByText("Wikipédia PT");
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE" && c.path === "/api/sources/zim%3Awikipedia_pt_teste" && c.search === "")).toBe(true));

    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.path.endsWith("/restore"))).toBe(true));

    const apagar = screen.getAllByRole("button", { name: "Delete" })[1];
    expect(apagar).toBeDisabled();
    await user.type(screen.getByLabelText("Confirm the id of zim:velha"), "zim:velha");
    expect(apagar).toBeEnabled();
    await user.click(apagar);
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE" && c.path === "/api/sources/zim%3Avelha" && c.search.includes("delete_file=true") && c.search.includes("confirm=zim%3Avelha"))).toBe(true));
  });
});

const NO_BACKUPS = {
  schedule: { enabled: false, recipient_configured: true, interval_days: 7, next_due: null, due_now: false, reason: "desligado", build_pending: false },
  rehearsal: { last_ok_at: null, overdue: false, max_days: 90 },
  last_backup: null,
  last_release: null,
  history: [],
};

describe("Backups and releases (Operations)", () => {
  const base = {
    "GET /health": { status: "ok", version: "0.1.0", checks: { database: "ok" } },
    "GET /api/jobs": { counts: {}, budget: { daily: 10, model_id: "m", used: 0, remaining: 10, resets_at: "2026-10-25T00:00:00Z" }, next: [], failed: [] },
    "GET /api/config": { embedder_model_id: null, index_daily_budget: 1, heat_half_life_days: 30, neighbors_heat_threshold: 5, neighbors_max_per_doc: 50, link_tau: 0.88, link_tau_accept: 0.94, router_tau_high: 0.86, router_tau_low: 0.8, router_historical_rule: true, import_heat_threshold: 3, curation_sample_rate: 0, translate_queries: true, services: { wiki: false, curator: false, translate: false, embedder: "fake" } },
    "GET /api/principals": [],
  };

  it("shows the last release, the overdue rehearsal and the next schedule; the administrator builds now", async () => {
    const user = userEvent.setup();
    const release = { id: 7, kind: "release", status: "ok", name: "ariadne-release-20261101T000000Z", started_at: "2026-11-01T00:00:00Z", finished_at: "2026-11-01T00:01:00Z", size_bytes: 5_000_000, parts: 3, requested_by: "agendamento", error: null, published_at: "2026-11-01T00:02:00Z", published_url: "https://github.com/dono/repo/releases/tag/x", rehearsal_status: null, rehearsed_at: null, counts: { sources: 4, wiki_pages: 10, courses: 1 } };
    const mock = mockApi({
      ...base,
      "GET /api/whoami": ME,
      "GET /api/backups": {
        ...NO_BACKUPS,
        schedule: { ...NO_BACKUPS.schedule, enabled: true, reason: "ok", next_due: "2026-11-08T00:00:00Z" },
        rehearsal: { last_ok_at: null, overdue: true, max_days: 90 },
        last_release: release,
        last_backup: { ...release, id: 2, kind: "backup", name: "backup-20261101T000000Z", parts: 2, size_bytes: 9_000_000, published_url: null, counts: null },
        history: [release],
      },
      "POST /api/backups/release": new Response(JSON.stringify({ job_id: 3 }), { status: 202, headers: { "content-type": "application/json" } }),
      "POST /api/backups/7/rehearsal": { ...release, rehearsal_status: "ok", rehearsed_at: "2026-11-02T00:00:00Z" },
    });
    renderApp(<OperationsPage />, "/operations");
    expect(await screen.findByText(/ariadne-release-20261101T000000Z/)).toBeInTheDocument();
    expect(screen.getByText(/3 parts/)).toBeInTheDocument();
    expect(screen.getByText(/10 pages, 1 course, 4 sources/)).toBeInTheDocument();
    expect(within(screen.getByText("Schedule").closest("tr") as HTMLElement).getByText("on")).toBeInTheDocument();
    expect(screen.getByText("never rehearsed")).toBeInTheDocument();
    expect(screen.getByText(/overdue \(more than 90 days\)/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "published on GitHub" })).toBeInTheDocument();
    expect(screen.getByText(/backup-20261101T000000Z/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Build release now" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.path === "/api/backups/release")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Record a successful rehearsal" }));
    await waitFor(() => expect(mock.calls.some((c) => c.path === "/api/backups/7/rehearsal" && (c.body as { status: string }).status === "ok")).toBe(true));
  });

  it("a member sees the status but cannot build", async () => {
    mockApi({ ...base, "GET /api/whoami": MEMBER, "GET /api/backups": NO_BACKUPS });
    renderApp(<OperationsPage />, "/operations");
    expect(await screen.findByText(/no releases yet/)).toBeInTheDocument();
    expect(screen.getByText(/RELEASE_ENABLED=false/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Build release now" })).not.toBeInTheDocument();
  });
});
