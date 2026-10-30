import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, unwrap } from "../api/client";
import type { CatalogItem, DownloadView } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, Progress } from "../components/ui";
import { t } from "../i18n";
import { DOWNLOAD_PHASES, formatBytes, formatDuration, formatInt } from "../lib/format";

const PAGE = 24;
const ACTIVE = new Set(["pending", "running"]);

/** Catálogo do Kiwix (Fase 12): navegar, descarregar um ZIM (com progresso) e vê-lo registado e
 *  servido pelo kiwix sem intervenção manual. Descarregar é só dos administradores. */
export function CatalogSection() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [lang, setLang] = useState("");
  const [category, setCategory] = useState("");
  const [start, setStart] = useState(0);

  const filters = useQuery({ queryKey: ["catalog", "filters"], queryFn: () => unwrap(api.GET("/api/catalog/filters")), staleTime: 3_600_000 });
  const downloads = useQuery({
    queryKey: ["catalog", "downloads"],
    queryFn: () => unwrap(api.GET("/api/catalog/downloads")),
    refetchInterval: (query) => ((query.state.data ?? []).some((d) => ACTIVE.has(d.status)) ? 2_000 : 15_000),
  });
  const page = useQuery({
    queryKey: ["catalog", "entries", applied, lang, category, start],
    queryFn: () =>
      unwrap(
        api.GET("/api/catalog", {
          params: { query: { q: applied || undefined, lang: lang || undefined, category: category || undefined, start, count: PAGE } },
        }),
      ),
    placeholderData: (previous) => previous,
  });

  const active = (downloads.data ?? []).filter((d) => ACTIVE.has(d.status));
  const wasActive = useRef(0);
  useEffect(() => {
    // Quando uma descarga acaba, a fonte aparece nas Fontes e o catálogo passa a mostrá-la instalada.
    if (wasActive.current > active.length) {
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
      void queryClient.invalidateQueries({ queryKey: ["stats", "coverage"] });
      void queryClient.invalidateQueries({ queryKey: ["catalog", "entries"] });
    }
    wasActive.current = active.length;
  }, [active.length, queryClient]);

  const start_ = useMutation({
    mutationFn: (v: { book: string; unverified?: boolean }) =>
      unwrap(
        api.POST("/api/catalog/{book}/download", {
          params: { path: { book: v.book }, query: v.unverified ? { allow_unverified: true } : {} },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["catalog"] });
    },
  });
  const cancel = useMutation({
    mutationFn: (jobId: number) => unwrap(api.DELETE("/api/catalog/downloads/{job_id}", { params: { path: { job_id: jobId } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["catalog", "downloads"] }),
  });

  const total = page.data?.total ?? 0;
  return (
    <section className="card" style={{ marginTop: "1rem" }} aria-labelledby="catalogo">
      <h2 id="catalogo">{t("sources.catalog.title")}</h2>
      <p className="muted">
        {t("sources.catalog.intro")}
        {isAdmin ? "" : t("sources.catalog.adminOnly")}
      </p>
      <Downloads items={downloads.data ?? []} isAdmin={isAdmin} onCancel={(id) => cancel.mutate(id)} />
      <form
        className="field-row"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(q.trim());
          setStart(0);
        }}
      >
        <label className="grow">
          {t("sources.catalog.search")}
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("sources.catalog.searchPlaceholder")} />
        </label>
        <label>
          {t("sources.catalog.language")}
          <select
            value={lang}
            onChange={(e) => {
              setLang(e.target.value);
              setStart(0);
            }}
          >
            <option value="">{t("sources.catalog.all")}</option>
            {(filters.data?.languages ?? []).map((l) => (
              <option key={l.code} value={l.code}>
                {l.title} ({l.code})
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("sources.catalog.category")}
          <select
            value={category}
            onChange={(e) => {
              setCategory(e.target.value);
              setStart(0);
            }}
          >
            <option value="">{t("sources.catalog.all")}</option>
            {(filters.data?.categories ?? []).map((c) => (
              <option key={c.code} value={c.code}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">{t("sources.catalog.searchButton")}</button>
      </form>
      <ErrorBox error={page.error ?? filters.error ?? start_.error ?? cancel.error} />
      {page.isLoading && <Loading label={t("sources.catalog.loading")} />}
      {page.data && page.data.items.length === 0 && <Empty>{t("sources.catalog.empty")}</Empty>}
      <ul className="catalog-list" style={{ listStyle: "none", padding: 0, display: "grid", gap: "0.75rem", marginTop: "1rem" }}>
        {(page.data?.items ?? []).map((item) => (
          <CatalogRow key={item.id} item={item} isAdmin={isAdmin} busy={start_.isPending} onDownload={(unverified) => start_.mutate({ book: item.id, unverified })} />
        ))}
      </ul>
      {total > 0 && (
        <div className="field-row" style={{ justifyContent: "space-between", marginTop: "0.75rem" }}>
          <small className="muted">
            {t("sources.catalog.range", { from: formatInt(start + 1), to: formatInt(Math.min(start + PAGE, total)), total: formatInt(total) })}
          </small>
          <span>
            <button className="small" disabled={start === 0} onClick={() => setStart(Math.max(0, start - PAGE))}>
              {t("sources.catalog.previous")}
            </button>{" "}
            <button className="small" disabled={start + PAGE >= total} onClick={() => setStart(start + PAGE)}>
              {t("sources.catalog.next")}
            </button>
          </span>
        </div>
      )}
    </section>
  );
}

function CatalogRow({ item, isAdmin, busy, onDownload }: { item: CatalogItem; isAdmin: boolean; busy: boolean; onDownload: (unverified?: boolean) => void }) {
  const job = item.download;
  const running = job != null && ACTIVE.has(job.status);
  // Os espelhos são de terceiros: sem o sha-256 do catálogo a descarga recusa-se, e só um
  // administrador pode, de propósito, descarregar sem checksum.
  const noChecksum = job != null && job.status === "failed" && /sha-256/.test(job.error ?? "");
  return (
    <li className="card" style={{ margin: 0 }}>
      <div className="field-row" style={{ justifyContent: "space-between", alignItems: "start" }}>
        <div className="grow">
          <strong>{item.title}</strong> <small className="muted">{item.stem ?? item.name}</small>
          <br />
          <small className="muted">
            {item.summary || "–"} · {item.language || "?"} · {item.category || "?"}
            {item.flavour ? ` · ${item.flavour}` : ""} · {formatBytes(item.size_bytes)}
            {item.article_count != null ? t("sources.catalog.articleCount", { count: item.article_count, n: formatInt(item.article_count) }) : ""} · {item.updated}
          </small>
          <div style={{ marginTop: "0.25rem" }}>
            {item.installed && <span className="badge ok">{item.installed_version ? t("sources.catalog.installedVersion", { version: item.installed_version }) : t("sources.catalog.installed")}</span>}
            {item.update_available && <span className="badge warn">{t("sources.catalog.updateAvailable", { version: item.installed_version ?? "" })}</span>}
            {item.archived && <span className="badge">{t("sources.catalog.archivedBadge")}</span>}
            {running && <span className="badge primary">{DOWNLOAD_PHASES[job.phase] ?? job.phase}</span>}
            {job != null && job.status === "failed" && <span className="badge danger">{t("sources.catalog.failed", { error: job.error ?? t("sources.catalog.seeOperations") })}</span>}
          </div>
        </div>
        {isAdmin && !item.installed && !running && (
          <div>
            <button disabled={busy || !item.download_url} onClick={() => onDownload()}>
              {item.update_available ? t("sources.catalog.update") : t("sources.catalog.download")}
            </button>
            {noChecksum && (
              <button
                className="small"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(t("sources.catalog.confirmUnverified"))) onDownload(true);
                }}
              >
                {t("sources.catalog.downloadUnverified")}
              </button>
            )}
          </div>
        )}
      </div>
      {running && <ProgressLine job={job} />}
    </li>
  );
}

function ProgressLine({ job }: { job: DownloadView }) {
  const fraction = job.total ? job.bytes / job.total : null;
  return (
    <div style={{ marginTop: "0.5rem" }}>
      <Progress fraction={fraction} />
      <small className="muted">
        {formatBytes(job.bytes)}
        {job.total ? t("sources.catalog.progressOf", { total: formatBytes(job.total) }) : ""} · {DOWNLOAD_PHASES[job.phase] ?? job.phase}
      </small>
    </div>
  );
}

function Downloads({ items, isAdmin, onCancel }: { items: DownloadView[]; isAdmin: boolean; onCancel: (id: number) => void }) {
  const shown = items.filter((d) => ACTIVE.has(d.status) || d.phase === "done" || d.status === "failed").slice(0, 6);
  const started = useRef<Map<number, number>>(new Map());
  const now = Date.now();
  for (const d of shown) if (!started.current.has(d.job_id)) started.current.set(d.job_id, now);
  if (shown.length === 0) return null;
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <h3>{t("sources.catalog.downloads")}</h3>
      <ul className="linklist">
        {shown.map((d) => {
          const fraction = d.total ? d.bytes / d.total : null;
          const elapsed = (now - (started.current.get(d.job_id) ?? now)) / 1000;
          const eta = d.status === "running" && fraction && fraction > 0.02 && elapsed > 3 ? (elapsed / fraction) * (1 - fraction) : null;
          return (
            <li key={d.job_id}>
              <strong>{d.title || d.name}</strong>{" "}
              <span className={`badge ${d.status === "failed" ? "danger" : d.phase === "done" ? "ok" : "primary"}`}>{DOWNLOAD_PHASES[d.phase] ?? d.phase}</span>{" "}
              {d.phase === "done" && d.served === true && <span className="badge ok">{t("sources.catalog.served")}</span>}
              {d.phase === "done" && d.served === false && <span className="badge warn">{t("sources.catalog.notServed")}</span>}
              {ACTIVE.has(d.status) && (
                <>
                  <Progress fraction={fraction} />
                  <small className="muted">
                    {formatBytes(d.bytes)}
                    {d.total ? t("sources.catalog.progressOf", { total: formatBytes(d.total) }) : ""}
                    {eta != null ? t("sources.catalog.eta", { time: formatDuration(eta) }) : ""}
                  </small>{" "}
                  {isAdmin && (
                    <button className="small" onClick={() => onCancel(d.job_id)}>
                      {t("sources.catalog.cancel")}
                    </button>
                  )}
                </>
              )}
              {d.status === "failed" && d.error && <div className="alert error">{d.error}</div>}
              {d.phase === "done" && d.source_id && <small className="muted">{t("sources.catalog.registeredAs", { id: d.source_id })}</small>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
