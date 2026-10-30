import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { Empty, ErrorBox, Kpi, Loading, PageHead, Tabs } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, formatInt, LICENSE_LABELS, sourceName } from "../lib/format";

type Tab = "fontes" | "itens" | "bloqueios";
const PAGE = 50;

export function LicensesPage() {
  const [tab, setTab] = useState<Tab>("fontes");
  const overview = useQuery({ queryKey: ["licenses"], queryFn: () => unwrap(api.GET("/api/licenses")) });

  return (
    <>
      <PageHead
        title={t("licenses.title")}
        subtitle={t("licenses.subtitle")}
      />
      <ErrorBox error={overview.error} />
      {overview.isLoading && <Loading />}
      {overview.data && (
        <>
          <div className="grid cols-4">
            <Kpi label={t("licenses.kpi.open_sources")} value={formatInt(overview.data.open_sources)} />
            <Kpi label={t("licenses.kpi.private_sources")} value={formatInt(overview.data.private_sources)} />
            <Kpi label={t("licenses.kpi.private_documents")} value={formatInt(overview.data.private_documents)} hint={t("licenses.kpi.private_documents_hint", { total: formatInt(overview.data.private_documents + overview.data.open_documents) })} />
            <Kpi label={t("licenses.kpi.blocks")} value={formatInt(overview.data.blocks.length)} hint={t("licenses.kpi.blocks_hint")} />
          </div>
          <Tabs
            value={tab}
            onChange={setTab}
            tabs={[
              { id: "fontes", label: t("licenses.tab.sources") },
              { id: "itens", label: t("licenses.tab.items") },
              { id: "bloqueios", label: t("licenses.tab.blocks") },
            ]}
          />
          {tab === "fontes" && (
            <>
              <div className="card table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("licenses.sources.source")}</th>
                      <th>{t("licenses.sources.class")}</th>
                      <th className="num">{t("licenses.sources.documents")}</th>
                      <th className="num">{t("licenses.sources.private")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.data.sources.map((s) => (
                      <tr key={s.id}>
                        <td>
                          <strong>{s.title}</strong>
                          <br />
                          <small className="muted">{sourceName(s.id)}</small>{" "}
                          {s.status !== "active" && <span className="badge danger">{s.status === "missing" ? t("licenses.sources.missing") : s.status}</span>}
                        </td>
                        <td>
                          <span className={`badge ${s.license_class === "private" ? "warn" : "ok"}`}>{LICENSE_LABELS[s.license_class] ?? s.license_class}</span>
                        </td>
                        <td className="num">{formatInt(s.documents)}</td>
                        <td className="num">{formatInt(s.private_documents)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <section className="card" style={{ marginTop: "1rem" }}>
                <h2>{t("licenses.outputs.title")}</h2>
                <p className="muted">{t("licenses.outputs.hint")}</p>
                <ul className="linklist">
                  {overview.data.outputs.map((o) => (
                    <li key={o.name}>
                      <strong>{o.name}</strong> <span className={`badge ${o.mode === "recusa" ? "danger" : "warn"}`}>{o.mode}</span>
                      <br />
                      <small className="muted">{o.description}</small>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}
          {tab === "itens" && <Items sources={overview.data.sources.map((s) => s.id)} />}
          {tab === "bloqueios" && (
            <div className="card">
              {overview.data.blocks.length === 0 && <Empty>{t("licenses.blocks.empty")}</Empty>}
              <ul className="linklist">
                {overview.data.blocks.map((b) => (
                  <li key={b.id}>
                    <span className={`badge ${b.action === "blocked" ? "danger" : "warn"}`}>{b.action === "blocked" ? t("licenses.blocks.blocked") : t("licenses.blocks.filtered")}</span>{" "}
                    <strong>{b.output}</strong> <small className="muted">{formatDateTime(b.created_at)} · {t("licenses.blocks.items", { n: formatInt(b.items) })}</small>
                    {b.sample.length > 0 && (
                      <small className="muted">
                        {" "}
                        — {b.sample.map((x) => `${x.kind} ${x.id}`).join(", ")}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </>
  );
}

function Items({ sources }: { sources: string[] }) {
  const [source, setSource] = useState("");
  const [klass, setKlass] = useState<"" | "open" | "private">("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const page = useQuery({
    queryKey: ["licenses", "documents", source, klass, q, offset],
    queryFn: () =>
      unwrap(
        api.GET("/api/licenses/documents", {
          params: { query: { source_id: source || undefined, license_class: klass || undefined, q: q || undefined, limit: PAGE, offset } },
        }),
      ),
  });
  return (
    <div className="card">
      <div className="row">
        <label>
          {t("licenses.items.source")}{" "}
          <select value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }}>
            <option value="">{t("licenses.items.all")}</option>
            {sources.map((s) => (
              <option key={s} value={s}>
                {sourceName(s)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("licenses.items.class")}{" "}
          <select value={klass} onChange={(e) => { setKlass(e.target.value as "" | "open" | "private"); setOffset(0); }}>
            <option value="">{t("licenses.items.all")}</option>
            <option value="open">{t("licenses.items.open")}</option>
            <option value="private">{t("licenses.items.private")}</option>
          </select>
        </label>
        <label>
          {t("licenses.items.title")} <input value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} placeholder={t("licenses.items.title_placeholder")} />
        </label>
      </div>
      <ErrorBox error={page.error} />
      {page.isLoading && <Loading />}
      {page.data && page.data.items.length === 0 && <Empty>{t("licenses.items.empty")}</Empty>}
      <ul className="linklist">
        {(page.data?.items ?? []).map((i) => (
          <li key={i.document_id}>
            <span className={`badge ${i.license_class === "private" ? "warn" : "ok"}`}>{LICENSE_LABELS[i.license_class] ?? i.license_class}</span>{" "}
            <Link to={`/read?id=${encodeURIComponent(i.document_id)}`}>{i.title}</Link> <small className="muted">{sourceName(i.source_id)} · {i.because}</small>
          </li>
        ))}
      </ul>
      {page.data && (
        <p className="row">
          <button className="small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            {t("licenses.items.prev")}
          </button>
          <small className="muted">
            {t("licenses.items.range", { from: formatInt(Math.min(offset + 1, page.data.total)), to: formatInt(Math.min(offset + PAGE, page.data.total)), total: formatInt(page.data.total) })}
          </small>
          <button className="small" disabled={offset + PAGE >= page.data.total} onClick={() => setOffset(offset + PAGE)}>
            {t("licenses.items.next")}
          </button>
        </p>
      )}
    </div>
  );
}
