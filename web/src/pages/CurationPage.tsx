import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { CurationDetail, Decision } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, PageHead, Tabs } from "../components/ui";
import { t } from "../i18n";
import { DECISION_LABELS, formatDateTime, sourceName } from "../lib/format";
import { BatchReview } from "./BatchReview";
import { ComputePanel } from "./ComputePanel";

const DECISIONS: Decision[] = ["needs_review", "pending_curator", "published", "rejected"];
const TONE: Record<string, string> = { needs_review: "warn", pending_curator: "", published: "ok", rejected: "danger" };

type View = "control" | "queue" | "batch";
const VIEW_IDS: View[] = ["control", "queue", "batch"];
function views(): { id: View; label: string }[] {
  return VIEW_IDS.map((id) => ({ id, label: t(`curation.view.${id}`) }));
}

/** O módulo Curadoria: o painel de controlo dos destinos de computação, a fila de revisão e o ecrã da manhã. */
export function CurationPage() {
  const [params, setParams] = useSearchParams();
  const view = (VIEW_IDS.find((v) => v === params.get("view")) ?? "control") as View;
  return (
    <>
      <PageHead
        title={t("curation.title")}
        subtitle={t("curation.subtitle")}
      />
      <Tabs tabs={views()} value={view} onChange={(id) => setParams({ view: id })} />
      {view === "control" && <ComputePanel />}
      {view === "queue" && <ReviewQueue />}
      {view === "batch" && <BatchReview />}
    </>
  );
}

function ReviewQueue() {
  const [params, setParams] = useSearchParams();
  const decision = (DECISIONS.find((d) => d === params.get("state")) ?? "needs_review") as Decision;
  const selected = params.get("id") ?? "";

  const list = useInfiniteQuery({
    queryKey: ["curation", "list", decision],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => unwrap(api.GET("/api/curation", { params: { query: { decision, limit: 30, cursor: pageParam } } })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  function setDecision(next: Decision) {
    setParams({ view: "queue", state: next });
  }
  function select(id: string) {
    setParams({ view: "queue", state: decision, id });
  }

  return (
    <>
      <Tabs
        tabs={DECISIONS.map((d) => ({ id: d, label: DECISION_LABELS[d] }))}
        value={decision}
        onChange={setDecision}
      />
      <div className="grid split">
        <section className="card" style={{ padding: 0 }} aria-label={t("curation.queue.items")}>
          <ErrorBox error={list.error} />
          {list.isLoading && <Loading />}
          {list.data && items.length === 0 && <Empty>{t("curation.queue.empty")}</Empty>}
          <ul className="list">
            {items.map((item) => (
              <li key={item.document_id}>
                <button className="item" aria-current={item.document_id === selected} onClick={() => select(item.document_id)}>
                  <strong>{item.title}</strong>
                  <small className="muted">
                    {sourceName(item.source_id)} · {formatDateTime(item.updated_at)}
                  </small>
                  <span className="row">
                    {item.flags.map((f) => (
                      <span key={f} className="badge warn">
                        {f}
                      </span>
                    ))}
                    {item.fidelity_ok === false && <span className="badge danger">{t("curation.badge.fidelity")}</span>}
                    {item.sampled && <span className="badge">{t("curation.badge.sampled")}</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {list.hasNextPage && (
            <div style={{ padding: "0.75rem" }}>
              <button onClick={() => list.fetchNextPage()} disabled={list.isFetchingNextPage}>
                {list.isFetchingNextPage ? t("curation.loading") : t("curation.loadMore")}
              </button>
            </div>
          )}
        </section>
        <section aria-label={t("curation.queue.detail")}>
          {selected ? <Detail key={selected} documentId={selected} /> : <Empty>{t("curation.queue.pick")}</Empty>}
        </section>
      </div>
    </>
  );
}

function Detail({ documentId }: { documentId: string }) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const detail = useQuery({
    queryKey: ["curation", "item", documentId],
    queryFn: () => unwrap(api.GET("/api/curation/item", { params: { query: { document_id: documentId } } })),
  });

  const review = useMutation({
    mutationFn: (vars: { decision: "approve" | "reject"; fingerprint: string }) =>
      unwrap(api.POST("/api/curation/review", { body: { document_id: documentId, fingerprint: vars.fingerprint, decision: vars.decision, note: note.trim() || undefined } })),
    onSuccess: (data) => {
      queryClient.setQueryData(["curation", "item", documentId], data);
      queryClient.invalidateQueries({ queryKey: ["curation", "list"] });
      setNote("");
    },
    onError: () => {
      // 409: o texto mudou entretanto. Volta a ler o item para a decisão ser sobre o que existe.
      queryClient.invalidateQueries({ queryKey: ["curation", "item", documentId] });
    },
  });

  if (detail.isLoading) return <Loading />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const d = detail.data as CurationDetail;
  const open = d.decision === "needs_review" || d.decision === "pending_curator";

  return (
    <div className="stack">
      <div className="card stack">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>{d.title}</h2>
          <span className={`badge ${TONE[d.decision] ?? ""}`}>{DECISION_LABELS[d.decision] ?? d.decision}</span>
        </div>
        <div className="row">
          <span className="badge">{sourceName(d.source_id)}</span>
          {d.language && <span className="badge">{d.language}</span>}
          <span className="badge">{t("curation.badge.version", { version: d.source_version })}</span>
          {d.curator_model && <span className="badge">{t("curation.badge.curator", { model: d.curator_model })}</span>}
          {d.sampled && <span className="badge">{t("curation.badge.sampled")}</span>}
          {d.archived && <span className="badge">{t("curation.badge.archived")}</span>}
        </div>
        {d.reason && <p>{d.reason}</p>}
        <div className="row">
          <Link to={`/read?id=${encodeURIComponent(d.document_id)}`}>{t("curation.detail.openOriginal")}</Link>
        </div>
        {d.reviewed_by && (
          <small className="muted">
            {t("curation.detail.reviewedBy", { who: d.reviewed_by, when: formatDateTime(d.updated_at) })}
          </small>
        )}
      </div>

      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card">
          <h3>{t("curation.detail.flags")}</h3>
          {d.flag_details.length === 0 && <p className="muted">{t("curation.detail.noFlags")}</p>}
          <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
            {d.flag_details.map((f, i) => (
              <li key={i}>
                <span className="badge warn">{String(f.code)}</span>{" "}
                {Array.isArray(f.details) && f.details.length > 0 && <small className="muted">{(f.details as unknown[]).slice(0, 4).map(String).join(" · ")}</small>}
              </li>
            ))}
          </ul>
        </div>
        <div className="card">
          <h3>{t("curation.detail.fidelity")}</h3>
          <Fidelity fidelity={d.fidelity} />
        </div>
      </div>

      <div className="card">
        <h3>{t("curation.detail.compare")}</h3>
        <div className="compare">
          <div>
            <h4 style={{ margin: "0 0 0.4rem" }}>{t("curation.detail.originalClean")}</h4>
            <pre>{d.original_md}</pre>
          </div>
          <div>
            <h4 style={{ margin: "0 0 0.4rem" }}>{t("curation.detail.curated")}</h4>
            {d.curated_md ? <pre>{d.curated_md}</pre> : <p className="muted">{t("curation.detail.noRewrite")}</p>}
          </div>
        </div>
        {d.published_md && d.published_md !== d.curated_md && d.published_md !== d.original_md && (
          <details style={{ marginTop: "0.75rem" }}>
            <summary>{t("curation.detail.published")}</summary>
            <pre className="block">{d.published_md}</pre>
          </details>
        )}
      </div>

      {open && !isAdmin && <p className="muted">{t("common.adminOnly")}</p>}
      {open && isAdmin && (
        <div className="card stack">
          <h3>{t("curation.decision.title")}</h3>
          <label>
            {t("curation.note")}
            <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} style={{ fontFamily: "var(--font-text)" }} />
          </label>
          <ErrorBox error={review.error} />
          <div className="row">
            <button className="primary" disabled={review.isPending} onClick={() => review.mutate({ decision: "approve", fingerprint: d.fingerprint })}>
              {t("curation.approve")}
            </button>
            <button className="danger" disabled={review.isPending} onClick={() => review.mutate({ decision: "reject", fingerprint: d.fingerprint })}>
              {t("curation.reject")}
            </button>
          </div>
        </div>
      )}
      {!open && isAdmin && d.decision === "published" && (
        <div className="card">
          <button
            className="danger"
            disabled={review.isPending}
            onClick={() => review.mutate({ decision: "reject", fingerprint: d.fingerprint })}
            title={t("curation.retract.hint")}
          >
            {t("curation.retract")}
          </button>
          <ErrorBox error={review.error} />
        </div>
      )}
    </div>
  );
}

function Fidelity({ fidelity }: { fidelity: CurationDetail["fidelity"] }) {
  if (!fidelity) return <p className="muted">{t("curation.fidelity.none")}</p>;
  const problems = Array.isArray(fidelity.problems) ? (fidelity.problems as unknown[]).map(String) : [];
  const rest = Object.entries(fidelity).filter(([k]) => k !== "ok" && k !== "problems");
  return (
    <>
      <p>
        <span className={`badge ${fidelity.ok ? "ok" : "danger"}`}>{fidelity.ok ? t("curation.fidelity.ok") : t("curation.fidelity.failed")}</span>
      </p>
      {problems.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {rest.length > 0 && (
        <details>
          <summary>{t("curation.fidelity.details")}</summary>
          <pre className="block">{JSON.stringify(Object.fromEntries(rest), null, 2)}</pre>
        </details>
      )}
    </>
  );
}
