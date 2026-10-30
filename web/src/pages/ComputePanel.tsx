import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, authedFetch, unwrap } from "../api/client";
import type { BatchRun, ComputeBackend, ComputeControl, ComputeOverview } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Kpi, Loading, Progress } from "../components/ui";
import {
  BACKEND_MODE_LABELS,
  formatDateTime,
  formatDuration,
  formatHours,
  formatInt,
  RUN_STATUS_LABELS,
} from "../lib/format";
import { t } from "../i18n";

const QUERY_KEY = ["compute"];
const POLL_MS = 10_000;

function stateLabels(): Record<string, string> {
  return { running: t("compute.state.running"), paused: t("compute.state.paused"), stopped: t("compute.state.stopped") };
}

function useCompute() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => unwrap(api.GET("/api/compute")),
    refetchInterval: POLL_MS,
  });
}

export function ComputePanel() {
  const query = useCompute();
  if (query.isLoading) return <Loading />;
  if (query.error) return <ErrorBox error={query.error} />;
  const data = query.data as ComputeOverview;
  return (
    <div className="stack">
      <Status data={data} />
      <Controls control={data.control} backends={data.backends} />
      <Destinations data={data} />
      <TaskTypes data={data} />
      <Runs data={data} />
    </div>
  );
}

// --- estado e progresso -----------------------------------------------------------------------

function statusLine(control: ComputeControl): { label: string; tone: string; detail: string } {
  if (control.state === "paused") return { label: t("compute.state.paused"), tone: "warn", detail: t("compute.status.paused.detail") };
  if (control.state === "stopped") return { label: t("compute.state.stopped"), tone: "danger", detail: t("compute.status.stopped.detail") };
  if (control.active_now) {
    const detail = control.manual_override
      ? t("compute.status.running.manual")
      : control.window_remaining_seconds != null
        ? t("compute.status.running.window", { duration: formatDuration(control.window_remaining_seconds) })
        : t("compute.status.running.always");
    return { label: t("compute.status.running"), tone: "ok", detail };
  }
  return {
    label: t("compute.status.waiting"),
    tone: "",
    detail: control.next_window_start ? t("compute.status.waiting.starts", { when: formatDateTime(control.next_window_start) }) : t("compute.status.waiting.outside"),
  };
}

function Status({ data }: { data: ComputeOverview }) {
  const { control } = data;
  const line = statusLine(control);
  const destination = data.backends.find((b) => b.id === control.destination);
  const totals = data.kinds.reduce(
    (acc, k) => {
      for (const [status, n] of Object.entries(k.counts)) acc[status] = (acc[status] ?? 0) + n;
      return acc;
    },
    {} as Record<string, number>,
  );
  const quota = destination?.quota;
  const mac = data.backends.find((b) => b.mode === "local");
  const macOffline = mac?.online === false;
  return (
    <section className="stack" aria-label={t("compute.status.title")}>
      <div className="card stack">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>{t("compute.status.title")}</h2>
          <span className={`badge ${line.tone}`}>{line.label}</span>
        </div>
        <p style={{ margin: 0 }}>
          {line.detail} {t("compute.status.destination")} <strong>{destination?.label ?? control.destination}</strong>.
        </p>
        {macOffline && (
          <div className="alert warn" role="status">
            <strong>{t("compute.status.macOffline")}</strong> {t("compute.status.macOfflineBody", { status: mac?.status ?? t("compute.status.noResponse") })}
          </div>
        )}
        {control.external_blocked_reason && (
          <div className="alert warn" role="status">
            {t("compute.status.externalBlocked", { reason: control.external_blocked_reason })}
            {control.fallback_local ? "." : t("compute.status.externalBlocked.fallbackOff")}
          </div>
        )}
        {data.run && (
          <p className="muted" style={{ margin: 0 }}>
            {t("compute.status.run", { id: data.run.id, backend: data.run.backend, status: RUN_STATUS_LABELS[data.run.status] ?? data.run.status })}
            {data.gateway.enabled && <TunnelProgress run={data.run} />}
          </p>
        )}
        <p className="muted" style={{ margin: 0 }}>
          {t("compute.status.tunnel")}{" "}
          {data.gateway.enabled ? (
            <>
              <span className="badge ok">{t("compute.status.tunnel.on")}</span> <code>{data.gateway.public_url}</code>
              {data.gateway.access_headers ? t("compute.status.tunnel.withAccess") : t("compute.status.tunnel.noAccess")}
            </>
          ) : (
            <>
              <span className="badge">{t("compute.status.tunnel.off")}</span> {t("compute.status.tunnel.offHint_pre")} <code>GPU_GATEWAY_PUBLIC_URL</code> {t("compute.status.tunnel.offHint_post")}
            </>
          )}
        </p>
      </div>
      <div className="grid cols-4">
        <Kpi label={t("compute.kpi.pending")} value={formatInt(totals.pending ?? 0)} hint={t("compute.kpi.pending.hint")} />
        <Kpi label={t("compute.kpi.running")} value={formatInt((totals.running ?? 0) + (totals.batched ?? 0))} hint={t("compute.kpi.running.hint", { n: formatInt(totals.batched ?? 0) })} />
        <Kpi label={t("compute.kpi.done")} value={formatInt(totals.done ?? 0)} />
        <Kpi label={t("compute.kpi.failed")} value={formatInt(totals.failed ?? 0)} />
      </div>
      {quota && (
        <div className="card stack">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{t("compute.quota.title", { destination: destination?.label ?? "" })}</strong>
            <span>
              {t("compute.quota.usage", { used: formatHours(quota.used_hours), weekly: formatHours(quota.weekly_hours), remaining: formatHours(quota.remaining_hours) })}
            </span>
          </div>
          <Progress fraction={quota.weekly_hours ? quota.used_hours / quota.weekly_hours : null} tone={quota.remaining_hours < 3 ? "warn" : undefined} />
          <small className="muted">{t("compute.quota.reset", { when: formatDateTime(quota.resets_at) })}</small>
        </div>
      )}
      {data.review.needs_review > 0 && (
        <div className="alert ok">
          {data.review.from_batch > 0 ? (
            <>
              {t("compute.review.fromBatch", { count: data.review.from_batch, n: formatInt(data.review.from_batch) })}{" "}
              <Link to="/curation?view=batch">{t("compute.review.fromBatch.link")}</Link>
            </>
          ) : (
            <>
              {t("compute.review.needs", { count: data.review.needs_review, n: formatInt(data.review.needs_review) })} <Link to="/curation?view=queue">{t("compute.review.needs.link")}</Link>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function TunnelProgress({ run }: { run: BatchRun }) {
  const progress = run.progress as { done?: number; total?: number };
  if (!run.last_heartbeat_at) return <> {t("compute.tunnel.noNews")}</>;
  const ago = Math.max(0, (Date.now() - new Date(run.last_heartbeat_at).getTime()) / 1000);
  return (
    <>
      {" "}
      {t("compute.tunnel.progress", { count: progress.total ?? run.request_count, done: formatInt(progress.done ?? 0), total: formatInt(progress.total ?? run.request_count), ago: formatDuration(ago) })}
    </>
  );
}

// --- controlo: play/pausa, destino, janela ----------------------------------------------------------

function Controls({ control, backends }: { control: ComputeControl; backends: ComputeBackend[] }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });
  const act = useMutation({
    mutationFn: (vars: { action: "play" | "pause" | "resume" | "stop"; ignore_window?: boolean }) =>
      unwrap(api.POST("/api/compute/state", { body: { action: vars.action, ignore_window: vars.ignore_window ?? false } })),
    onSuccess: refresh,
  });
  const running = control.state === "running";
  const paused = control.state === "paused";
  return (
    <section className="card stack" aria-label={t("compute.controls.title")}>
      <h2 style={{ margin: 0 }}>{t("compute.controls.title")}</h2>
      <div className="row">
        {isAdmin && !running && !paused && (
          <button className="primary" disabled={act.isPending} onClick={() => act.mutate({ action: "play" })}>
            {t("compute.controls.play")}
          </button>
        )}
        {isAdmin && paused && (
          <button className="primary" disabled={act.isPending} onClick={() => act.mutate({ action: "resume" })}>
            {t("compute.controls.resume")}
          </button>
        )}
        {isAdmin && running && (
          <button disabled={act.isPending} onClick={() => act.mutate({ action: "pause" })}>
            {t("compute.controls.pause")}
          </button>
        )}
        {isAdmin && (running || paused) && (
          <button className="danger" disabled={act.isPending} onClick={() => act.mutate({ action: "stop" })} title={t("compute.controls.stop.hint")}>
            {t("compute.controls.stop")}
          </button>
        )}
        {isAdmin && !(running && control.manual_override) && (
          <button disabled={act.isPending} onClick={() => act.mutate({ action: "play", ignore_window: true })} title={t("compute.controls.startNow.hint")}>
            {t("compute.controls.startNow")}
          </button>
        )}
        <span className="badge">{stateLabels()[control.state] ?? control.state}</span>
      </div>
      <ErrorBox error={act.error} />
      <p className="muted" style={{ margin: 0 }}>
        {t("compute.controls.explain")}
      </p>
      {isAdmin ? <Settings control={control} backends={backends} onSaved={refresh} /> : <p className="muted">{t("common.adminOnly")}</p>}
    </section>
  );
}

function Settings({ control, backends, onSaved }: { control: ComputeControl; backends: ComputeBackend[]; onSaved: () => void }) {
  const [form, setForm] = useState(() => draftOf(control));
  const dirty = useRef(false);
  // Sem apagar o que se está a editar: só se volta a ler do servidor se não houver alterações por guardar.
  useEffect(() => {
    if (!dirty.current) setForm(draftOf(control));
  }, [control]);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => {
    dirty.current = true;
    setForm((f) => ({ ...f, [key]: value }));
  };
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/compute/control", {
          body: {
            destination: form.destination,
            fallback_local: form.fallback_local,
            schedule_enabled: form.schedule_enabled,
            window_start: form.window_start,
            window_end: form.window_end,
            timezone: form.timezone,
            batch_size: Number(form.batch_size),
            max_run_hours: Number(form.max_run_hours),
          },
        }),
      ),
    onSuccess: () => {
      dirty.current = false;
      onSaved();
    },
  });
  const usable = backends.filter((b) => b.implemented);
  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend className="eyebrow">{t("compute.settings.destination")}</legend>
        <div className="row">
          {usable.map((b) => (
            <label key={b.id} className="inline">
              <input type="radio" name="destination" value={b.id} checked={form.destination === b.id} onChange={() => set("destination", b.id)} />
              {b.label}
              {!b.configured && b.mode !== "manual" && <span className="badge warn">{t("compute.settings.credentialsMissing")}</span>}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="inline">
        <input type="checkbox" checked={form.fallback_local} onChange={(e) => set("fallback_local", e.target.checked)} />
        {t("compute.settings.fallbackLocal")}
      </label>
      <label className="inline">
        <input type="checkbox" checked={form.schedule_enabled} onChange={(e) => set("schedule_enabled", e.target.checked)} />
        {t("compute.settings.scheduleEnabled")}
      </label>
      <div className="field-row">
        <label>
          {t("compute.settings.windowStart")}
          <input type="text" inputMode="numeric" pattern="[0-2][0-9]:[0-5][0-9]" placeholder="23:00" value={form.window_start} disabled={!form.schedule_enabled} onChange={(e) => set("window_start", e.target.value)} />
        </label>
        <label>
          {t("compute.settings.windowEnd")}
          <input type="text" inputMode="numeric" pattern="[0-2][0-9]:[0-5][0-9]" placeholder="07:00" value={form.window_end} disabled={!form.schedule_enabled} onChange={(e) => set("window_end", e.target.value)} />
        </label>
        <label className="grow">
          {t("compute.settings.timezone")}
          <input type="text" value={form.timezone} disabled={!form.schedule_enabled} onChange={(e) => set("timezone", e.target.value)} />
        </label>
        <label>
          {t("compute.settings.batchSize")}
          <input type="number" min={1} max={5000} value={form.batch_size} onChange={(e) => set("batch_size", e.target.value)} />
        </label>
        <label>
          {t("compute.settings.maxRunHours")}
          <input type="number" min={0.25} max={24} step={0.25} value={form.max_run_hours} onChange={(e) => set("max_run_hours", e.target.value)} />
        </label>
      </div>
      <ErrorBox error={save.error} />
      <div className="row">
        <button className="primary" type="submit" disabled={save.isPending}>
          {t("compute.settings.save")}
        </button>
        {save.isSuccess && !dirty.current && <span className="muted">{t("compute.settings.saved")}</span>}
      </div>
    </form>
  );
}

function draftOf(c: ComputeControl) {
  return {
    destination: c.destination,
    fallback_local: c.fallback_local,
    schedule_enabled: c.schedule_enabled,
    window_start: c.window_start,
    window_end: c.window_end,
    timezone: c.timezone,
    batch_size: String(c.batch_size),
    max_run_hours: String(c.max_run_hours),
  };
}

// --- destinos ---------------------------------------------------------------------------------

function Destinations({ data }: { data: ComputeOverview }) {
  return (
    <section className="stack" aria-label={t("compute.destinations.aria")}>
      <h2 style={{ margin: 0 }}>{t("compute.destinations.title")}</h2>
      <div className="grid cols-2">
        {data.backends.map((b) => (
          <BackendCard key={b.id} backend={b} current={b.id === data.control.destination} />
        ))}
      </div>
    </section>
  );
}

function BackendCard({ backend, current }: { backend: ComputeBackend; current: boolean }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const config = backend.config as Record<string, unknown>;
  const [model, setModel] = useState(String(config.model ?? ""));
  const [weekly, setWeekly] = useState(config.weekly_hours != null ? String(config.weekly_hours) : "");
  const [used, setUsed] = useState("");
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/compute/backends/{backend_id}/config", {
          params: { path: { backend_id: backend.id } },
          body: {
            model: model.trim() || undefined,
            weekly_hours: weekly.trim() ? Number(weekly) : undefined,
            used_hours: used.trim() ? Number(used) : undefined,
          },
        }),
      ),
    onSuccess: () => {
      setUsed("");
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
  const editable = isAdmin && backend.implemented && backend.mode !== "local";
  return (
    <div className="card stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>{backend.label}</h3>
        <span className="row">
          <span className="badge">{BACKEND_MODE_LABELS[backend.mode] ?? backend.mode}</span>
          {backend.mode === "local" && backend.online != null && (
            <span className={`badge ${backend.online ? "ok" : "warn"}`}>{backend.online ? t("compute.backend.macOnline") : t("compute.backend.macOffline")}</span>
          )}
          {current && <span className="badge primary">{t("compute.backend.inUse")}</span>}
        </span>
      </div>
      <p style={{ margin: 0 }}>{backend.description}</p>
      {backend.implemented && (
        <div>
          <strong>{t("compute.backend.credentials")}</strong>
          {backend.credentials.length === 0 && <p className="muted">{t("compute.backend.noCredentials")}</p>}
          <ul style={{ margin: "0.25rem 0 0", paddingLeft: "1.1rem" }}>
            {backend.credentials.map((c) => (
              <li key={c.name}>
                <code>{c.name}</code>{" "}
                <span className={`badge ${c.set ? "ok" : c.required ? "warn" : ""}`}>{c.set ? t("compute.backend.credSet") : c.required ? t("compute.backend.credMissing") : t("compute.backend.credOptional")}</span>
              </li>
            ))}
          </ul>
          <small className="muted">{t("compute.backend.credHint")}</small>
        </div>
      )}
      {editable && (
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <label>
            {t("compute.backend.model")}
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
          </label>
          {backend.mode === "auto" && (
            <div className="field-row">
              <label>
                {t("compute.backend.weeklyHours")}
                <input type="number" min={0} step={0.5} value={weekly} onChange={(e) => setWeekly(e.target.value)} />
              </label>
              <label>
                {t("compute.backend.usedHours")}
                <input type="number" min={0} step={0.1} value={used} placeholder={backend.quota ? String(backend.quota.used_hours) : ""} onChange={(e) => setUsed(e.target.value)} />
              </label>
            </div>
          )}
          <ErrorBox error={save.error} />
          <div>
            <button className="small" type="submit" disabled={save.isPending}>
              {t("compute.backend.save")}
            </button>
          </div>
        </form>
      )}
      {isAdmin && backend.mode === "manual" && <ManualRun backend={backend} />}
    </div>
  );
}

// --- lote manual (Colab) ------------------------------------------------------------------------

function ManualRun({ backend }: { backend: ComputeBackend }) {
  const queryClient = useQueryClient();
  const overview = useCompute().data;
  const run = overview?.run && overview.run.backend === backend.id ? overview.run : null;
  const refresh = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/compute/runs", { body: { backend: backend.id } })),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => unwrap(api.POST("/api/compute/runs/{run_id}/cancel", { params: { path: { run_id: id } } })),
    onSuccess: refresh,
  });
  const upload = useMutation({
    mutationFn: async (vars: { id: number; file: File }) => {
      await authedFetch(`/api/compute/runs/${vars.id}/results`, { method: "POST", body: vars.file });
    },
    onSuccess: refresh,
  });
  const download = useMutation({
    mutationFn: async (id: number) => {
      const response = await authedFetch(`/api/compute/runs/${id}/package`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = t("compute.manual.fileName", { id });
      link.click();
      URL.revokeObjectURL(url);
    },
  });
  return (
    <div className="stack">
      <strong>{t("compute.manual.title")}</strong>
      {!run && (
        <div>
          <button className="small" disabled={create.isPending} onClick={() => create.mutate()}>
            {t("compute.manual.create")}
          </button>
        </div>
      )}
      {run && run.status === "awaiting_results" && (
        <div className="stack">
          <p style={{ margin: 0 }}>
            {t("compute.manual.info", {
              id: run.id,
              count: run.task_count,
              requests: t("compute.manual.requests", { count: run.request_count, n: formatInt(run.request_count) }),
            })}{" "}
            <code>deploy/gpu/ariadne_batch.ipynb</code> {t("compute.manual.infoMid")}{" "}
            {overview?.gateway.enabled ? t("compute.manual.viaTunnel") : t("compute.manual.viaUpload")}
          </p>
          <div className="row">
            <button className="small" disabled={download.isPending} onClick={() => download.mutate(run.id)}>
              {t("compute.manual.download")}
            </button>
            <label className="visually-hidden" htmlFor={`results-${run.id}`}>
              {t("compute.manual.resultsFile")}
            </label>
            <input
              id={`results-${run.id}`}
              type="file"
              accept=".zip,.jsonl,application/zip"
              disabled={upload.isPending}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) upload.mutate({ id: run.id, file });
              }}
            />
            <button className="small danger" disabled={cancel.isPending} onClick={() => cancel.mutate(run.id)}>
              {t("compute.manual.cancel")}
            </button>
          </div>
        </div>
      )}
      {run && run.status !== "awaiting_results" && <p className="muted">{t("compute.manual.runStatus", { id: run.id, status: RUN_STATUS_LABELS[run.status] ?? run.status })}</p>}
      <ErrorBox error={create.error ?? upload.error ?? download.error ?? cancel.error} />
    </div>
  );
}

// --- tipos de tarefa e corridas -----------------------------------------------------------------------

function TaskTypes({ data }: { data: ComputeOverview }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (vars: { kind: string; enabled: boolean }) => {
      const disabled = data.kinds.filter((k) => (k.kind === vars.kind ? !vars.enabled : !k.enabled)).map((k) => k.kind);
      return unwrap(api.PUT("/api/compute/control", { body: { disabled_kinds: disabled } }));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
  return (
    <section className="card stack" aria-label={t("compute.kinds.aria")}>
      <h2 style={{ margin: 0 }}>{t("compute.kinds.title")}</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t("compute.kinds.type")}</th>
              <th>{t("compute.kinds.enabled")}</th>
              <th className="num">{t("compute.kinds.pending")}</th>
              <th className="num">{t("compute.kinds.batched")}</th>
              <th className="num">{t("compute.kinds.running")}</th>
              <th className="num">{t("compute.kinds.done")}</th>
              <th className="num">{t("compute.kinds.failed")}</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => (
              <tr key={k.kind}>
                <th scope="row">
                  <code>{k.kind}</code> <small className="muted">{t("compute.kinds.prompt", { version: k.prompt_version })}</small>
                </th>
                <td>
                  {isAdmin ? (
                    <input type="checkbox" aria-label={t("compute.kinds.enable", { kind: k.kind })} checked={k.enabled} disabled={toggle.isPending} onChange={(e) => toggle.mutate({ kind: k.kind, enabled: e.target.checked })} />
                  ) : (
                    <span>{k.enabled ? "✓" : "–"}</span>
                  )}
                </td>
                <td className="num">{formatInt(k.counts.pending ?? 0)}</td>
                <td className="num">{formatInt(k.counts.batched ?? 0)}</td>
                <td className="num">{formatInt(k.counts.running ?? 0)}</td>
                <td className="num">{formatInt(k.counts.done ?? 0)}</td>
                <td className="num">{formatInt(k.counts.failed ?? 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ErrorBox error={toggle.error} />
    </section>
  );
}

function Runs({ data }: { data: ComputeOverview }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: (id: number) => unwrap(api.POST("/api/compute/runs/{run_id}/cancel", { params: { path: { run_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
  return (
    <section className="card stack" aria-label={t("compute.runs.aria")}>
      <h2 style={{ margin: 0 }}>{t("compute.runs.title")}</h2>
      {data.recent_runs.length === 0 && <Empty>{t("compute.runs.empty")}</Empty>}
      {data.recent_runs.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>{t("compute.runs.destination")}</th>
                <th>{t("compute.runs.status")}</th>
                <th className="num">{t("compute.runs.tasks")}</th>
                <th className="num">{t("compute.runs.toReview")}</th>
                <th className="num">{t("compute.runs.released")}</th>
                <th className="num">GPU</th>
                <th>{t("compute.runs.created")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.recent_runs.map((r) => (
                <RunRow key={r.id} run={r} canCancel={isAdmin} onCancel={() => cancel.mutate(r.id)} busy={cancel.isPending} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ErrorBox error={cancel.error} />
    </section>
  );
}

function RunRow({ run, canCancel, onCancel, busy }: { run: BatchRun; canCancel: boolean; onCancel: () => void; busy: boolean }) {
  const open = ["created", "queued", "running", "awaiting_results"].includes(run.status);
  const tone = run.status === "failed" ? "danger" : run.status === "done" ? "ok" : "";
  return (
    <tr>
      <td>{run.id}</td>
      <td>{run.backend}</td>
      <td>
        <span className={`badge ${tone}`}>{RUN_STATUS_LABELS[run.status] ?? run.status}</span>
        {run.error && <small className="muted"> {run.error}</small>}
      </td>
      <td className="num">{formatInt(run.task_count)}</td>
      <td className="num">
        {run.review_count + run.applied_count > 0 ? <Link to={`/curation?view=batch&run=${run.id}`}>{formatInt(run.review_count + run.applied_count)}</Link> : "0"}
      </td>
      <td className="num">{formatInt(run.released_count)}</td>
      <td className="num">{run.gpu_seconds != null ? formatDuration(run.gpu_seconds) : "–"}</td>
      <td>{formatDateTime(run.created_at)}</td>
      <td>
        {canCancel && open && (
          <button className="small danger" disabled={busy} onClick={onCancel}>
            {t("compute.runs.cancel")}
          </button>
        )}
      </td>
    </tr>
  );
}
