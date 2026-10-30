import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { useAuth } from "../auth";
import { CatalogSection } from "../components/CatalogSection";
import { Empty, ErrorBox, Loading, PageHead, Progress } from "../components/ui";
import { t } from "../i18n";
import { CLASS_LABELS, DECISION_LABELS, formatBytes, formatInt, formatPercent, LICENSE_LABELS, sourceName } from "../lib/format";

export function SourcesPage() {
  const { isAdmin } = useAuth();
  const sources = useQuery({ queryKey: ["sources"], queryFn: () => unwrap(api.GET("/api/sources")) });
  const coverage = useQuery({ queryKey: ["stats", "coverage"], queryFn: () => unwrap(api.GET("/api/stats/coverage")) });

  const info = new Map((sources.data ?? []).map((s) => [s.id, s]));
  const rows = coverage.data ?? [];
  const total = (sources.data ?? []).reduce((acc, s) => acc + (s.size_bytes ?? 0), 0);

  return (
    <>
      <PageHead
        title={t("sources.page.title")}
        subtitle={sources.data ? t("sources.page.subtitleSize", { size: formatBytes(total) }) : t("sources.page.subtitle")}
      />
      <ErrorBox error={sources.error ?? coverage.error} />
      {(sources.isLoading || coverage.isLoading) && <Loading />}
      {coverage.data && rows.length === 0 && <Empty>{t("sources.empty")}</Empty>}
      {rows.length > 0 && (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("sources.col.source")}</th>
                <th>{t("sources.col.language")}</th>
                <th>{t("sources.col.license")}</th>
                <th>{t("sources.col.version")}</th>
                <th className="num">{t("sources.col.articles")}</th>
                <th className="num">{t("sources.col.size")}</th>
                <th>{t("sources.col.rooms")}</th>
                <th>{t("sources.col.indexing")}</th>
                <th>{t("sources.col.wiki")}</th>
                <th>{t("sources.col.curation")}</th>
                {isAdmin && <th>{t("sources.col.manage")}</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const s = info.get(c.source_id);
                return (
                  <tr key={c.source_id}>
                    <td>
                      <strong>{c.title}</strong>
                      <br />
                      <small className="muted">{sourceName(c.source_id)}</small>{" "}
                      {s && s.content_class !== "article" && (
                        <span className="badge primary" title={s.archive_only ? t("sources.archiveOnlyTitle") : undefined}>
                          {CLASS_LABELS[s.content_class] ?? s.content_class}
                          {s.archive_only ? t("sources.archiveSuffix") : ""}
                        </span>
                      )}{" "}
                      {c.status === "archived" && <span className="badge">{t("sources.archived")}</span>}
                      {c.status !== "active" && c.status !== "archived" && <span className="badge danger">{c.status === "missing" ? t("sources.missingFile") : c.status}</span>}
                    </td>
                    <td>{c.language ?? "–"}</td>
                    <td>
                      {s ? (
                        <span className={`badge ${s.license_class === "private" ? "warn" : "ok"}`} title={t("sources.licenseClass")}>
                          {LICENSE_LABELS[s.license_class] ?? s.license_class}
                        </span>
                      ) : (
                        "–"
                      )}
                    </td>
                    <td>{s?.version ?? "–"}</td>
                    <td className="num">{formatInt(c.articles)}</td>
                    <td className="num">{formatBytes(s?.size_bytes)}</td>
                    <td>
                      {s && s.rooms.count > 0 ? (
                        <details>
                          <summary>{t("sources.rooms", { count: s.rooms.count })}</summary>
                          <ul className="linklist">
                            {s.rooms.top.map((r) => (
                              <li key={r.id}>
                                <Link to={`/?room=${r.id}`}>{r.label}</Link> <small className="muted">{formatInt(r.size)}</small>
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : (
                        <small className="muted">{t("sources.noRooms")}</small>
                      )}
                    </td>
                    <td style={{ minWidth: "9rem" }}>
                      <Progress fraction={c.articles ? c.indexed / c.articles : null} />
                      <small className="muted">
                        {t("sources.chunks", { indexed: formatInt(c.indexed), percent: formatPercent(c.articles ? c.indexed / c.articles : null, 1), chunks: formatInt(c.chunks) })}
                      </small>
                    </td>
                    <td>
                      {formatInt(c.imported)} <small className="muted">{t("sources.imported")}</small>
                    </td>
                    <td>
                      {Object.keys(c.curation).length === 0 && <small className="muted">–</small>}
                      {Object.entries(c.curation).map(([decision, n]) => (
                        <div key={decision}>
                          <span className={`badge ${decision === "published" ? "ok" : decision === "rejected" ? "danger" : "warn"}`}>
                            {DECISION_LABELS[decision] ?? decision}
                          </span>{" "}
                          {n}
                        </div>
                      ))}
                    </td>
                    {isAdmin && <td>{c.source_id.startsWith("zim:") ? <SourceActions id={c.source_id} status={c.status} /> : <small className="muted">–</small>}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <CatalogSection />
    </>
  );
}

/** Arquivar (o ficheiro vai para .archive e o kiwix deixa de o servir), restaurar e apagar o ficheiro
 *  (só com o id repetido como confirmação). Só administradores. */
function SourceActions({ id, status }: { id: string; status: string }) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState("");
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["stats", "coverage"] });
    void queryClient.invalidateQueries({ queryKey: ["catalog"] });
  };
  const remove = useMutation({
    mutationFn: (deleteFile: boolean) =>
      unwrap(api.DELETE("/api/sources/{source_id}", { params: { path: { source_id: id }, query: deleteFile ? { delete_file: true, confirm } : {} } })),
    onSuccess: () => {
      setConfirm("");
      refresh();
    },
  });
  const restore = useMutation({
    mutationFn: () => unwrap(api.POST("/api/sources/{source_id}/restore", { params: { path: { source_id: id } } })),
    onSuccess: refresh,
  });
  return (
    <div>
      {status === "active" && (
        <button className="small" disabled={remove.isPending} onClick={() => remove.mutate(false)} title={t("sources.actions.archiveTitle")}>
          {t("sources.actions.archive")}
        </button>
      )}
      {status === "archived" && (
        <button className="small" disabled={restore.isPending} onClick={() => restore.mutate()}>
          {t("sources.actions.restore")}
        </button>
      )}
      <details>
        <summary>{t("sources.actions.deleteFile")}</summary>
        <small className="muted">{t("sources.actions.deleteWarning")}</small>
        <input aria-label={t("sources.actions.confirmId", { id })} value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={id} />{" "}
        <button className="small danger" disabled={confirm !== id || remove.isPending} onClick={() => remove.mutate(true)}>
          {t("sources.actions.delete")}
        </button>
      </details>
      <ErrorBox error={remove.error ?? restore.error} />
    </div>
  );
}
