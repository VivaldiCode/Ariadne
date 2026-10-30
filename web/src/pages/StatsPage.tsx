import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { LatencyChart, QueriesChart } from "../components/charts";
import { Empty, ErrorBox, Kpi, Loading, PageHead, Progress, Tabs } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, formatInt, formatMs, formatPercent, sourceName } from "../lib/format";

type By = "heat" | "used" | "shown";
const DAY_OPTIONS = [7, 14, 30, 90];

export function StatsPage() {
  const [days, setDays] = useState(14);
  const [by, setBy] = useState<By>("heat");

  const overview = useQuery({
    queryKey: ["stats", "overview", days],
    queryFn: () => unwrap(api.GET("/api/stats/overview", { params: { query: { days } } })),
  });
  const top = useQuery({
    queryKey: ["stats", "top", days, by],
    queryFn: () => unwrap(api.GET("/api/stats/top-documents", { params: { query: { days, by, limit: 15 } } })),
  });
  const principals = useQuery({
    queryKey: ["stats", "principals", days],
    queryFn: () => unwrap(api.GET("/api/stats/principals", { params: { query: { days } } })),
  });
  const coverage = useQuery({
    queryKey: ["stats", "coverage"],
    queryFn: () => unwrap(api.GET("/api/stats/coverage")),
  });

  const o = overview.data;
  return (
    <>
      <PageHead
        title={t("stats.title")}
        subtitle={t("stats.subtitle")}
        actions={
          <label className="inline">
            {t("stats.range")}
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {DAY_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  {t("stats.last_days", { n: d })}
                </option>
              ))}
            </select>
          </label>
        }
      />
      <ErrorBox error={overview.error} />
      {overview.isLoading && <Loading />}
      {o && (
        <>
          <div className="grid cols-4">
            <Kpi label={t("stats.kpi.queries")} value={formatInt(o.queries)} />
            <Kpi label={t("stats.kpi.level3")} value={formatPercent(o.level3_rate)} hint={t("stats.kpi.level3_hint")} />
            <Kpi label={t("stats.kpi.p50")} value={formatMs(o.latency_p50_ms)} />
            <Kpi label={t("stats.kpi.p95")} value={formatMs(o.latency_p95_ms)} />
          </div>
          <div className="grid cols-4" style={{ marginTop: "1rem" }}>
            <Kpi label={t("stats.kpi.documents")} value={formatInt(o.totals.documents)} />
            <Kpi label={t("stats.kpi.chunks")} value={formatInt(o.totals.chunks)} hint={t("stats.kpi.chunks_hint", { n: formatInt(o.totals.chunk_vectors) })} />
            <Kpi label={t("stats.kpi.article_vectors")} value={formatInt(o.totals.article_vectors)} />
            <Kpi
              label={t("stats.kpi.links")}
              value={formatInt(Object.values(o.totals.links).reduce((a, b) => a + b, 0))}
              hint={Object.entries(o.totals.links)
                .map(([status, n]) => `${n} ${status}`)
                .join(" · ")}
            />
          </div>
          <div className="grid cols-2" style={{ marginTop: "1rem" }}>
            <section className="card">
              <h2>{t("stats.chart.queries_per_day")}</h2>
              <QueriesChart series={o.series} />
            </section>
            <section className="card">
              <h2>{t("stats.chart.latency")}</h2>
              <LatencyChart series={o.series} />
            </section>
          </div>
        </>
      )}

      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>{t("stats.docs.title")}</h2>
        <Tabs
          tabs={[
            { id: "heat", label: t("stats.docs.heat") },
            { id: "used", label: t("stats.docs.used") },
            { id: "shown", label: t("stats.docs.shown") },
          ]}
          value={by}
          onChange={setBy}
        />
        <ErrorBox error={top.error} />
        {top.data && top.data.length === 0 && <Empty>{t("stats.docs.empty")}</Empty>}
        {top.data && top.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("stats.docs.document")}</th>
                  <th>{t("stats.docs.source")}</th>
                  <th className="num">{t("stats.docs.heat_col")}</th>
                  <th className="num">{t("stats.docs.shown_col")}</th>
                  <th className="num">{t("stats.docs.used_col")}</th>
                </tr>
              </thead>
              <tbody>
                {top.data.map((d) => (
                  <tr key={d.document_id}>
                    <td>
                      <Link to={`/read?id=${encodeURIComponent(d.document_id)}`}>{d.title}</Link>
                    </td>
                    <td>{sourceName(d.source_id)}</td>
                    <td className="num">{d.heat.toFixed(2)}</td>
                    <td className="num">{d.shown}</td>
                    <td className="num">{d.used}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid cols-2" style={{ marginTop: "1rem", alignItems: "start" }}>
        <section className="card">
          <h2>{t("stats.principals.title")}</h2>
          <ErrorBox error={principals.error} />
          {principals.data && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("stats.principals.principal")}</th>
                    <th className="num">{t("stats.principals.searches")}</th>
                    <th className="num">{t("stats.principals.calls")}</th>
                    <th className="num">{t("stats.principals.errors")}</th>
                    <th>{t("stats.principals.last")}</th>
                  </tr>
                </thead>
                <tbody>
                  {principals.data.map((p) => (
                    <tr key={p.principal_id}>
                      <td>
                        {p.name} <span className="badge">{p.kind}</span>
                        {p.revoked && <span className="badge danger">{t("stats.principals.revoked")}</span>}
                      </td>
                      <td className="num">{p.queries}</td>
                      <td className="num">{p.tool_calls}</td>
                      <td className="num">{p.errors}</td>
                      <td>{formatDateTime(p.last_seen)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="card">
          <h2>{t("stats.coverage.title")}</h2>
          <ErrorBox error={coverage.error} />
          {coverage.data && coverage.data.length === 0 && <Empty>{t("stats.coverage.empty")}</Empty>}
          <div className="stack">
            {coverage.data?.map((c) => (
              <div key={c.source_id}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>{sourceName(c.source_id)}</strong>
                  <small>
                    {t("stats.coverage.indexed", { indexed: formatInt(c.indexed), articles: formatInt(c.articles ?? c.documents) })}
                  </small>
                </div>
                <Progress fraction={c.articles ? c.indexed / c.articles : null} />
                <small className="muted">
                  {t("stats.coverage.detail", { documents: formatInt(c.documents), imported: formatInt(c.imported), vectors: formatInt(c.with_article_vector) })}
                </small>
              </div>
            ))}
          </div>
        </section>
      </div>
    </>
  );
}
