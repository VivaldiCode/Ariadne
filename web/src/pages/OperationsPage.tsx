import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap } from "../api/client";
import type { CreatedPrincipal, Health } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, PageHead, Progress } from "../components/ui";
import { t } from "../i18n";
import { formatBytes, formatDateTime, formatInt } from "../lib/format";

export function OperationsPage() {
  const { isAdmin } = useAuth();
  return (
    <>
      <PageHead title={t("operations.page.title")} subtitle={t("operations.page.subtitle")} />
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <HealthCard />
        <BudgetCard />
      </div>
      <QueuesCard />
      <BackupsCard />
      <ConfigCard />
      {isAdmin ? <PrincipalsCard /> : <section className="card" style={{ marginTop: "1rem" }}><h2>{t("operations.keys.restrictedTitle")}</h2><p className="muted">{t("operations.keys.restrictedBody")}</p></section>}
    </>
  );
}

function statusTone(value: string): string {
  if (value.startsWith("ok")) return "ok";
  if (value.startsWith("not configured")) return "";
  return "danger";
}

function HealthCard() {
  const health = useQuery({
    queryKey: ["health"],
    refetchInterval: 15_000,
    // O /health responde 503 quando a base de dados cai, e o corpo continua a ser útil.
    queryFn: async (): Promise<Health> => {
      const response = await fetch("/health");
      return (await response.json()) as Health;
    },
  });
  const h = health.data;
  return (
    <section className="card">
      <h2>{t("operations.health.title")}</h2>
      <ErrorBox error={health.error} />
      {health.isLoading && <Loading />}
      {h && (
        <>
          <p>
            {t("operations.health.state")}{" "}
            <span className={`badge ${h.status === "ok" ? "ok" : h.status === "degraded" ? "warn" : "danger"}`}>{h.status}</span>{" "}
            {t("operations.health.version", { version: h.version })}
          </p>
          <div className="table-wrap">
            <table>
              <tbody>
                {Object.entries(h.checks).map(([name, value]) => (
                  <tr key={name}>
                    <th scope="row" style={{ textTransform: "none", fontSize: "0.9rem", color: "var(--body)" }}>
                      {name}
                    </th>
                    <td>
                      <span className={`badge ${statusTone(String(value ?? "not configured"))}`}>{String(value ?? "–")}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function useJobs() {
  return useQuery({
    queryKey: ["jobs"],
    refetchInterval: 10_000,
    queryFn: () => unwrap(api.GET("/api/jobs", { params: { query: { limit: 15, failed_limit: 15 } } })),
  });
}

function BudgetCard() {
  const jobs = useJobs();
  const b = jobs.data?.budget;
  const fraction = b && b.used != null && b.daily > 0 ? b.used / b.daily : null;
  return (
    <section className="card">
      <h2>{t("operations.budget.title")}</h2>
      <ErrorBox error={jobs.error} />
      {b && (
        <div className="stack">
          <Progress fraction={fraction} tone={fraction != null && fraction >= 1 ? "danger" : fraction != null && fraction > 0.8 ? "warn" : undefined} />
          <p>
            {b.used == null
              ? t("operations.budget.noEmbedder")
              : t("operations.budget.used", { used: formatInt(b.used), daily: formatInt(b.daily), remaining: formatInt(b.remaining) })}
          </p>
          <small className="muted">
            {t("operations.budget.model", { model: b.model_id ?? "–", time: formatDateTime(b.resets_at) })}
          </small>
        </div>
      )}
    </section>
  );
}

function QueuesCard() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const jobs = useJobs();
  const retry = useMutation({
    mutationFn: (id: number) => unwrap(api.POST("/api/jobs/{job_id}/retry", { params: { path: { job_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const counts = jobs.data?.counts ?? {};
  const states = ["pending", "running", "done", "failed"];
  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("operations.queues.title")}</h2>
      <ErrorBox error={jobs.error ?? retry.error} />
      {jobs.isLoading && <Loading />}
      {jobs.data && Object.keys(counts).length === 0 && <Empty>{t("operations.queues.empty")}</Empty>}
      {Object.keys(counts).length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("operations.queues.kind")}</th>
                <th className="num">{t("operations.queues.pending")}</th>
                <th className="num">{t("operations.queues.running")}</th>
                <th className="num">{t("operations.queues.done")}</th>
                <th className="num">{t("operations.queues.failed")}</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(counts).map(([kind, byState]) => (
                <tr key={kind}>
                  <td>{kind}</td>
                  {states.map((s) => (
                    <td key={s} className="num">
                      {s === "failed" && (byState[s] ?? 0) > 0 ? <span className="badge danger">{byState[s]}</span> : (byState[s] ?? 0)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {jobs.data && jobs.data.failed.length > 0 && (
        <>
          <h3 style={{ marginTop: "1rem" }}>{t("operations.queues.failedHeading")}</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("operations.queues.job")}</th>
                  <th>{t("operations.queues.error")}</th>
                  <th>{t("operations.queues.when")}</th>
                  <th aria-label={t("operations.queues.actions")} />
                </tr>
              </thead>
              <tbody>
                {jobs.data.failed.map((j) => (
                  <tr key={j.id}>
                    <td>
                      #{j.id} {j.kind}
                      <br />
                      <small className="muted">{j.document_id ?? ""}</small>
                    </td>
                    <td>
                      <small>{j.last_error ?? "–"}</small>
                    </td>
                    <td>
                      <small>{formatDateTime(j.finished_at)}</small>
                    </td>
                    <td>
                      {isAdmin && (
                        <button className="small" disabled={retry.isPending} onClick={() => retry.mutate(j.id)}>
                          {t("operations.queues.retry")}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {jobs.data && jobs.data.next.length > 0 && (
        <>
          <h3 style={{ marginTop: "1rem" }}>{t("operations.queues.nextHeading")}</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("operations.queues.job")}</th>
                  <th className="num">{t("operations.queues.priority")}</th>
                  <th className="num">{t("operations.queues.attempts")}</th>
                  <th>{t("operations.queues.runAfter")}</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.next.map((j) => (
                  <tr key={j.id}>
                    <td>
                      #{j.id} {j.kind}
                      <br />
                      <small className="muted">{j.document_id ?? ""}</small>
                    </td>
                    <td className="num">{j.priority.toFixed(2)}</td>
                    <td className="num">{j.attempts}</td>
                    <td>
                      <small>{j.due ? t("operations.queues.now") : formatDateTime(j.run_after)}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function ConfigCard() {
  const config = useQuery({ queryKey: ["config"], queryFn: () => unwrap(api.GET("/api/config")) });
  const c = config.data;
  const onOff = (flag: boolean) => (flag ? t("operations.config.on") : t("operations.config.off"));
  const rows: [string, string][] = c
    ? [
        [t("operations.config.dailyBudget"), formatInt(c.index_daily_budget)],
        [t("operations.config.heatHalfLife"), String(c.heat_half_life_days)],
        [t("operations.config.neighborsHeat"), String(c.neighbors_heat_threshold)],
        [t("operations.config.neighborsMax"), String(c.neighbors_max_per_doc)],
        [t("operations.config.linkTau"), String(c.link_tau)],
        [t("operations.config.linkTauAccept"), String(c.link_tau_accept)],
        [t("operations.config.routerTau"), `${c.router_tau_high} / ${c.router_tau_low}`],
        [t("operations.config.historicalRule"), onOff(c.router_historical_rule)],
        [t("operations.config.importHeat"), String(c.import_heat_threshold)],
        [t("operations.config.curationSample"), String(c.curation_sample_rate)],
        [t("operations.config.translateQueries"), onOff(c.translate_queries)],
      ]
    : [];
  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("operations.config.title")}</h2>
      <p className="muted">{t("operations.config.readonly")}</p>
      <ErrorBox error={config.error} />
      {c && (
        <>
          <div className="row" style={{ marginBottom: "0.5rem" }}>
            <span className={`badge ${c.services.wiki ? "ok" : ""}`}>{c.services.wiki ? t("operations.config.wikiOn") : t("operations.config.wikiOff")}</span>
            <span className={`badge ${c.services.curator ? "ok" : ""}`}>{c.services.curator ? t("operations.config.curatorOn") : t("operations.config.curatorOff")}</span>
            <span className={`badge ${c.services.translate ? "ok" : ""}`}>{c.services.translate ? t("operations.config.translateOn") : t("operations.config.translateOff")}</span>
            <span className="badge">{t("operations.config.embedder", { name: c.services.embedder })}</span>
            {c.embedder_model_id && <span className="badge">{c.embedder_model_id}</span>}
          </div>
          <div className="table-wrap">
            <table>
              <tbody>
                {rows.map(([label, value]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td className="num">
                      <code>{value}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function PrincipalsCard() {
  const queryClient = useQueryClient();
  const { me } = useAuth();
  const list = useQuery({ queryKey: ["principals"], queryFn: () => unwrap(api.GET("/api/principals")) });
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"agent" | "user">("agent");
  const [role, setRole] = useState<"member" | "admin">("member");
  const [created, setCreated] = useState<CreatedPrincipal | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["principals"] });
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/principals", { body: { name: name.trim(), kind, role: kind === "user" ? role : "member", scope: "full" } })),
    onSuccess: (data) => {
      setCreated(data);
      setName("");
      refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: number) => unwrap(api.DELETE("/api/principals/{principal_id}", { params: { path: { principal_id: id } } })),
    onSuccess: refresh,
  });
  const changeRole = useMutation({
    mutationFn: (vars: { id: number; role: "admin" | "member" }) =>
      unwrap(api.PATCH("/api/principals/{principal_id}", { params: { path: { principal_id: vars.id } }, body: { role: vars.role } })),
    onSuccess: refresh,
  });

  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("operations.principals.title")}</h2>
      <p className="muted">
        {t("operations.principals.intro")}
      </p>
      {created && (
        <div className="alert ok stack" role="status">
          <strong>{t("operations.principals.createdKey", { name: created.name })}</strong>
          <div className="secret">{created.api_key}</div>
          <div className="row">
            <button className="small" onClick={() => navigator.clipboard?.writeText(created.api_key)}>
              {t("operations.principals.copy")}
            </button>
            <button className="small" onClick={() => setCreated(null)}>
              {t("operations.principals.saved")}
            </button>
          </div>
        </div>
      )}
      <form
        className="field-row"
        style={{ margin: "0.75rem 0" }}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <label className="grow">
          {t("operations.principals.name")}
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("operations.principals.namePlaceholder")} maxLength={80} />
        </label>
        <label>
          {t("operations.principals.kind")}
          <select value={kind} onChange={(e) => setKind(e.target.value as "agent" | "user")}>
            <option value="agent">{t("operations.principals.kindAgent")}</option>
            <option value="user">{t("operations.principals.kindUser")}</option>
          </select>
        </label>
        {kind === "user" && (
          <label>
            {t("operations.principals.role")}
            <select value={role} onChange={(e) => setRole(e.target.value as "member" | "admin")}>
              <option value="member">{t("operations.principals.roleMember")}</option>
              <option value="admin">{t("operations.principals.roleAdmin")}</option>
            </select>
          </label>
        )}
        <button className="primary" type="submit" disabled={!name.trim() || create.isPending}>
          {t("operations.principals.create")}
        </button>
      </form>
      <ErrorBox error={list.error ?? create.error ?? revoke.error ?? changeRole.error} />
      {list.isLoading && <Loading />}
      {list.data && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("operations.principals.name")}</th>
                <th>{t("operations.principals.kind")}</th>
                <th>{t("operations.principals.role")}</th>
                <th>{t("operations.principals.created")}</th>
                <th>{t("operations.principals.key")}</th>
                <th aria-label={t("operations.principals.actions")} />
              </tr>
            </thead>
            <tbody>
              {list.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    {p.name} {p.id === me?.id && <span className="badge primary">{t("operations.principals.you")}</span>}
                  </td>
                  <td>{p.kind === "user" ? t("operations.principals.kindUser") : t("operations.principals.kindAgent")}</td>
                  <td>
                    {p.kind === "user" && p.active ? (
                      <select
                        aria-label={t("operations.principals.roleOf", { name: p.name })}
                        value={p.role}
                        disabled={changeRole.isPending}
                        onChange={(e) => changeRole.mutate({ id: p.id, role: e.target.value as "admin" | "member" })}
                      >
                        <option value="member">{t("operations.principals.roleMember")}</option>
                        <option value="admin">{t("operations.principals.roleAdmin")}</option>
                      </select>
                    ) : (
                      p.role
                    )}
                  </td>
                  <td>
                    <small>{formatDateTime(p.created_at)}</small>
                  </td>
                  <td>{p.active ? <span className="badge ok">{t("operations.principals.active")}</span> : <span className="badge danger">{t("operations.principals.revoked", { date: formatDateTime(p.revoked_at) })}</span>}</td>
                  <td>
                    {p.active && (
                      <button
                        className="small danger"
                        disabled={revoke.isPending}
                        onClick={() => {
                          if (window.confirm(t("operations.principals.confirmRevoke", { name: p.name }))) revoke.mutate(p.id);
                        }}
                      >
                        {t("operations.principals.revoke")}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function runTone(status: string): string {
  return status === "ok" ? "ok" : status === "failed" ? "danger" : "primary";
}

/** Backups e releases (Fase 12): a release semanal cifrada, o ensaio de restauro e o backup local. */
function BackupsCard() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const state = useQuery({
    queryKey: ["backups"],
    refetchInterval: 15_000,
    queryFn: () => unwrap(api.GET("/api/backups")),
  });
  const build = useMutation({
    mutationFn: () => unwrap(api.POST("/api/backups/release")),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });
  const rehearsal = useMutation({
    mutationFn: (v: { id: number; status: "ok" | "failed" }) =>
      unwrap(api.POST("/api/backups/{run_id}/rehearsal", { params: { path: { run_id: v.id } }, body: { status: v.status, notes: "registado na app" } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });
  const b = state.data;
  const release = b?.last_release ?? null;
  const backup = b?.last_backup ?? null;
  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("operations.backups.title")}</h2>
      <p className="muted">
        {t("operations.backups.intro")}
      </p>
      <ErrorBox error={state.error ?? build.error ?? rehearsal.error} />
      {state.isLoading && <Loading />}
      {b && (
        <div className="grid cols-2" style={{ alignItems: "start" }}>
          <div>
            <h3>{t("operations.backups.release")}</h3>
            <div className="table-wrap">
              <table>
                <tbody>
                  <tr>
                    <th scope="row">{t("operations.backups.schedule")}</th>
                    <td>
                      {b.schedule.enabled ? <span className="badge ok">{t("operations.backups.scheduleOn")}</span> : <span className="badge">{t("operations.backups.scheduleOff")}</span>}{" "}
                      {b.schedule.enabled && b.schedule.reason !== "ok" && <small className="muted">{b.schedule.reason}</small>}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">{t("operations.backups.next")}</th>
                    <td>
                      {b.schedule.next_due ? formatDateTime(b.schedule.next_due) : "–"}
                      {b.schedule.due_now && <small className="muted"> {t("operations.backups.dueNow")}</small>}
                      <br />
                      <small className="muted">{t("operations.backups.interval", { count: b.schedule.interval_days })}</small>
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">{t("operations.backups.publicKey")}</th>
                    <td>{b.schedule.recipient_configured ? <span className="badge ok">{t("operations.backups.configured")}</span> : <span className="badge warn">{t("operations.backups.missingRecipient")}</span>}</td>
                  </tr>
                  <tr>
                    <th scope="row">{t("operations.backups.last")}</th>
                    <td>
                      {release ? (
                        <>
                          <span className={`badge ${runTone(release.status)}`}>{release.status === "ok" ? t("operations.backups.releaseDone") : release.status === "failed" ? t("operations.backups.runFailed") : t("operations.backups.running")}</span>{" "}
                          {formatDateTime(release.started_at)}
                          <br />
                          <small className="muted">
                            {t("operations.backups.size", { name: release.name ?? "", size: formatBytes(release.size_bytes), count: release.parts ?? 0 })}
                            {release.counts
                              ? t("operations.backups.counts", {
                                  pages: t("operations.backups.pages", { count: release.counts.wiki_pages ?? 0 }),
                                  courses: t("operations.backups.courses", { count: release.counts.courses ?? 0 }),
                                  sources: t("operations.backups.sources", { count: release.counts.sources ?? 0 }),
                                })
                              : ""}
                          </small>
                          {release.error && <div className="alert error">{release.error}</div>}
                          <br />
                          {release.published_url ? <a href={release.published_url}>{t("operations.backups.publishedOnGithub")}</a> : <small className="muted">{t("operations.backups.notPublished")}</small>}
                        </>
                      ) : (
                        <span className="muted">{t("operations.backups.noReleases")}</span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">{t("operations.backups.rehearsal")}</th>
                    <td>
                      {release == null ? (
                        "–"
                      ) : (
                        <>
                          {b.rehearsal.last_ok_at ? <span className="badge ok">{t("operations.backups.rehearsalOk", { date: formatDateTime(b.rehearsal.last_ok_at) })}</span> : <span className="badge warn">{t("operations.backups.neverRehearsed")}</span>}{" "}
                          {b.rehearsal.overdue && <span className="badge danger">{t("operations.backups.overdue", { count: b.rehearsal.max_days })}</span>}
                          {release.rehearsal_status === "failed" && <span className="badge danger">{t("operations.backups.lastRehearsalFailed")}</span>}
                          <br />
                          <small className="muted">
                            {t("operations.backups.rehearsalHint")} <code>ariadne release verify &lt;pasta&gt; --identity chave.txt --record</code>
                          </small>
                          {isAdmin && release.status === "ok" && (
                            <div>
                              <button className="small" disabled={rehearsal.isPending} onClick={() => rehearsal.mutate({ id: release.id, status: "ok" })}>
                                {t("operations.backups.recordRehearsal")}
                              </button>
                            </div>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            {isAdmin && (
              <p>
                <button disabled={build.isPending || b.schedule.build_pending || !b.schedule.recipient_configured} onClick={() => build.mutate()}>
                  {b.schedule.build_pending ? t("operations.backups.buildPending") : t("operations.backups.buildNow")}
                </button>{" "}
                <small className="muted">{b.schedule.enabled ? t("operations.backups.willPublish") : t("operations.backups.buildOnly")}</small>
              </p>
            )}
          </div>
          <div>
            <h3>{t("operations.backups.local")}</h3>
            {backup ? (
              <p>
                <span className={`badge ${runTone(backup.status)}`}>{backup.status === "ok" ? t("operations.backups.backupDone") : backup.status === "failed" ? t("operations.backups.runFailed") : t("operations.backups.running")}</span> {formatDateTime(backup.started_at)}
                <br />
                <small className="muted">
                  {backup.name} · {formatBytes(backup.size_bytes)}
                </small>
                {backup.error && <span className="alert error">{backup.error}</span>}
              </p>
            ) : (
              <p className="muted">
                {t("operations.backups.noLocalBefore")} <code>ariadne backup create</code> {t("operations.backups.noLocalAfter")}
              </p>
            )}
            <h3>{t("operations.backups.history")}</h3>
            {b.history.length === 0 ? (
              <Empty>{t("operations.backups.historyEmpty")}</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <tbody>
                    {b.history.map((r) => (
                      <tr key={r.id}>
                        <td>{formatDateTime(r.started_at)}</td>
                        <td>{r.kind === "release" ? "release" : "backup"}</td>
                        <td>
                          <span className={`badge ${runTone(r.status)}`}>{r.status}</span>
                        </td>
                        <td className="num">{formatBytes(r.size_bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
