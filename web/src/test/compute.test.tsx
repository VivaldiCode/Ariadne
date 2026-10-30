import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CurationPage } from "../pages/CurationPage";
import { jsonError, ME, mockApi, renderApp } from "./utils";

const CONTROL = {
  state: "running",
  destination: "local",
  fallback_local: true,
  schedule_enabled: false,
  window_start: "23:00",
  window_end: "07:00",
  timezone: "UTC",
  manual_override: false,
  disabled_kinds: [],
  batch_size: 200,
  max_run_hours: 8,
  external_blocked_reason: null,
  active_now: true,
  in_window: true,
  window_remaining_seconds: null,
  next_window_start: null,
  local_can_work: true,
  updated_at: "2026-10-25T10:00:00Z",
  updated_by: "1",
};

const BACKENDS = [
  { id: "local", label: "Mac (Metal)", mode: "local", description: "O Mac Studio.", implemented: true, configured: true, missing: [], credentials: [{ name: "CURATOR_URL", set: true, required: true }], config: {}, quota: null, online: true, status: "a responder" },
  {
    id: "kaggle",
    label: "Google gratuito (Kaggle)",
    mode: "auto",
    description: "GPU gratuita do Kaggle.",
    implemented: true,
    configured: false,
    missing: ["KAGGLE_USERNAME"],
    credentials: [
      { name: "KAGGLE_USERNAME", set: false, required: true },
      { name: "KAGGLE_API_TOKEN", set: true, required: false },
    ],
    config: { model: "solidrust/Mistral-7B-Instruct-v0.3-AWQ", weekly_hours: 30 },
    quota: { weekly_hours: 30, used_hours: 12, remaining_hours: 18, period_start: "2026-10-24T00:00:00Z", resets_at: "2026-10-31T00:00:00Z" },
  },
  { id: "colab", label: "Google Colab (manual)", mode: "manual", description: "Manual.", implemented: true, configured: true, missing: [], credentials: [], config: { model: "solidrust/Mistral-7B-Instruct-v0.3-AWQ" }, quota: null },
  { id: "paid", label: "Serviço pago", mode: "reserved", description: "Ainda não implementado.", implemented: false, configured: false, missing: [], credentials: [], config: {}, quota: null },
];

const RUN = {
  id: 3,
  backend: "kaggle",
  status: "done",
  manual: false,
  kinds: ["curate_page"],
  model: "gemma",
  task_count: 40,
  request_count: 55,
  applied_count: 0,
  review_count: 37,
  invalid_count: 3,
  released_count: 3,
  created_at: "2026-10-25T01:00:00Z",
  submitted_at: "2026-10-25T01:01:00Z",
  running_at: "2026-10-25T01:05:00Z",
  finished_at: "2026-10-25T05:00:00Z",
  gpu_seconds: 14400,
  deadline_seconds: 28800,
  error: null,
  requested_by: null,
  last_heartbeat_at: null,
  progress: {},
};

function overview(patch: Record<string, unknown> = {}) {
  return {
    control: CONTROL,
    backends: BACKENDS,
    kinds: [{ kind: "curate_page", prompt_version: "curate-1", enabled: true, counts: { pending: 12, batched: 5, running: 1, done: 80, failed: 2 } }],
    run: null,
    recent_runs: [RUN],
    review: { needs_review: 37, from_batch: 37 },
    gateway: { enabled: false, public_url: null, access_headers: false },
    ...patch,
  };
}

describe("Compute panel", () => {
  it("mostra o estado, a fila, as horas de GPU e se as credenciais estão definidas (nunca o valor)", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/compute": overview() });
    renderApp(<CurationPage />);

    expect(await screen.findByText("Running", { selector: ".badge" })).toBeInTheDocument();
    const kpis = document.querySelector(".grid.cols-4") as HTMLElement;
    expect(within(kpis).getByText("Queued").closest(".kpi")).toHaveTextContent("12");
    expect(within(kpis).getByText("Done").closest(".kpi")).toHaveTextContent("80");
    // O destino em uso é o local, sem quota; o Kaggle mostra as credenciais em falta.
    const card = screen.getByRole("heading", { name: "Google gratuito (Kaggle)" }).closest(".card") as HTMLElement;
    expect(within(card).getByText("KAGGLE_USERNAME")).toBeInTheDocument();
    expect(within(card).getByText("missing")).toBeInTheDocument();
    expect(within(card).getByText("set")).toBeInTheDocument();
    expect(screen.getByText(/37 results from external runs/)).toBeInTheDocument();
    expect(screen.getByText("Serviço pago")).toBeInTheDocument();
  });

  it("mostra o túnel e o progresso que o notebook comunica", async () => {
    const running = { ...RUN, status: "running", last_heartbeat_at: new Date(Date.now() - 90_000).toISOString(), progress: { done: 12, total: 55, gpu_seconds: 600 } };
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute": overview({ run: running, gateway: { enabled: true, public_url: "https://gpu.exemplo.pt", access_headers: true } }),
    });
    renderApp(<CurationPage />);
    expect(await screen.findByText("https://gpu.exemplo.pt")).toBeInTheDocument();
    expect(screen.getByText(/12 of 55 requests/)).toBeInTheDocument();
    expect(screen.getByText(/with Cloudflare Access/)).toBeInTheDocument();
  });

  it("sem túnel diz que só há ficheiros", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/compute": overview() });
    renderApp(<CurationPage />);
    expect(await screen.findByText(/files only/)).toBeInTheDocument();
  });

  it("diz «Mac offline» e que os trabalhos esperam na fila quando o Mac não responde", async () => {
    const offline = BACKENDS.map((b) => (b.id === "local" ? { ...b, online: false, status: "sem resposta (ConnectError)" } : b));
    mockApi({ "GET /api/whoami": ME, "GET /api/compute": overview({ backends: offline }) });
    renderApp(<CurationPage />);

    const notice = await screen.findByText(/curation jobs wait in the queue/);
    expect(notice.closest(".alert")).toHaveTextContent("Mac offline");
    const card = screen.getByRole("heading", { name: "Mac (Metal)" }).closest(".card") as HTMLElement;
    expect(within(card).getByText("Mac offline", { selector: ".badge" })).toBeInTheDocument();
  });

  it("mostra o Mac online no destino local", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/compute": overview() });
    renderApp(<CurationPage />);
    const card = (await screen.findByRole("heading", { name: "Mac (Metal)" })).closest(".card") as HTMLElement;
    expect(within(card).getByText("Mac online")).toBeInTheDocument();
    expect(screen.queryByText(/curation jobs wait in the queue/)).not.toBeInTheDocument();
  });

  it("pausa, retoma e arranca fora da janela", async () => {
    let control = { ...CONTROL };
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute": () => overview({ control }),
      "POST /api/compute/state": ({ body }) => {
        const action = (body as { action: string }).action;
        control = { ...control, state: action === "pause" ? "paused" : "running", manual_override: (body as { ignore_window: boolean }).ignore_window };
        return control;
      },
    });
    renderApp(<CurationPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(mock.calls.find((c) => c.path === "/api/compute/state")?.body).toEqual({ action: "pause", ignore_window: false });

    await user.click(screen.getByRole("button", { name: "Start now" }));
    await waitFor(() => expect(mock.calls.filter((c) => c.path === "/api/compute/state").at(-1)?.body).toEqual({ action: "play", ignore_window: true }));
  });

  it("guarda o destino e a janela agendada", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute": overview(),
      "PUT /api/compute/control": CONTROL,
    });
    renderApp(<CurationPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /Google gratuito/ }));
    await user.click(screen.getByLabelText(/Only work within a time window/));
    const start = screen.getByLabelText("Starts at");
    await user.clear(start);
    await user.type(start, "22:30");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toMatchObject({
      destination: "kaggle",
      schedule_enabled: true,
      window_start: "22:30",
      window_end: "07:00",
      batch_size: 200,
    });
  });

  it("desativa um tipo de tarefa e mostra o erro do servidor", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute": overview(),
      "PUT /api/compute/control": () => jsonError(400, "Tipos de tarefa desconhecidos: x."),
    });
    renderApp(<CurationPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Enable curate_page"));
    expect(await screen.findByText(/Tipos de tarefa desconhecidos/)).toBeInTheDocument();
  });

  it("gera o lote manual para o Colab e mostra o pacote à espera de resultados", async () => {
    const waiting = { ...RUN, id: 4, backend: "colab", status: "awaiting_results" };
    let current = null as null | typeof waiting;
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute": () => overview({ run: current }),
      "POST /api/compute/runs": () => {
        current = waiting;
        return waiting;
      },
    });
    renderApp(<CurationPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Generate batch" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.path === "/api/compute/runs")).toBe(true));
    expect(mock.calls.find((c) => c.path === "/api/compute/runs")?.body).toEqual({ backend: "colab" });
    expect(await screen.findByRole("button", { name: "Download package" })).toBeInTheDocument();
    expect(screen.getByLabelText("Results file")).toBeInTheDocument();
  });
});

describe("Batch review", () => {
  const ITEM = (n: number, ok: boolean) => ({
    document_id: `zim:x/${n}`,
    fingerprint: `fp${n}`,
    title: `Artigo ${n}`,
    source_id: "zim:x",
    language: "pt",
    decision: "needs_review",
    reason: "lote externo: fiel ao original, aguarda a tua validação",
    flags: [],
    sampled: false,
    curator_model: "gemma",
    fidelity_ok: ok,
    reviewed_by: null,
    batch_run_id: 3,
    updated_at: "2026-10-25T05:00:00Z",
  });

  it("seleciona os fiéis e aprova-os de uma vez com as fingerprints vistas", async () => {
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute/runs": [RUN],
      "GET /api/curation": { items: [ITEM(1, true), ITEM(2, false), ITEM(3, true)], next_cursor: null },
      "POST /api/curation/review-batch": { done: ["zim:x/1", "zim:x/3"], conflicts: [], missing: [] },
    });
    renderApp(<CurationPage />, "/?view=batch");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /Select the faithful ones \(2\)/ }));
    await user.click(screen.getByRole("button", { name: "Approve and publish (2)" }));

    expect(await screen.findByText(/2 decided/)).toBeInTheDocument();
    const body = mock.calls.find((c) => c.path === "/api/curation/review-batch")?.body as { decision: string; items: { document_id: string; fingerprint: string }[] };
    expect(body.decision).toBe("approve");
    expect(body.items).toEqual([
      { document_id: "zim:x/1", fingerprint: "fp1" },
      { document_id: "zim:x/3", fingerprint: "fp3" },
    ]);
    expect(mock.calls.find((c) => c.path === "/api/curation")?.search).toContain("from_batch=true");
  });

  it("avisa dos itens que mudaram entretanto", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/compute/runs": [RUN],
      "GET /api/curation": { items: [ITEM(1, true)], next_cursor: null },
      "POST /api/curation/review-batch": { done: [], conflicts: ["zim:x/1"], missing: [] },
    });
    renderApp(<CurationPage />, "/?view=batch&run=3");
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Select Artigo 1"));
    await user.click(screen.getByRole("button", { name: /Reject \(1\)/ }));
    expect(await screen.findByText(/1 changed in the meantime/)).toBeInTheDocument();
  });

  it("um membro vê o estado e as corridas mas não tem controlos de escrita", async () => {
    const running = { ...RUN, id: 7, status: "running" };
    mockApi({
      "GET /api/whoami": { ...ME, role: "member" },
      "GET /api/compute": overview({ recent_runs: [running] }),
    });
    renderApp(<CurationPage />);

    expect(await screen.findByText("Running", { selector: ".badge" })).toBeInTheDocument();
    expect(await screen.findByText(/Only administrators can change this/)).toBeInTheDocument();
    expect(screen.getByText("curate_page")).toBeInTheDocument();
    for (const name of ["Pause", "Stop", "Start now", "Save", "Cancel", "Play"]) {
      expect(screen.queryByRole("button", { name: new RegExp(`^${name}`) })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
