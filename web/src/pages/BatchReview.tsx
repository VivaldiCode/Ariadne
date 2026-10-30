import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { BatchReviewResult, CurationDetail, CurationItem } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, formatInt, RUN_STATUS_LABELS, sourceName } from "../lib/format";

/** O ecrã da manhã: os resultados que voltaram da GPU externa, para aprovar ou rejeitar em lote. */
export function BatchReview() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const runId = Number(params.get("run")) || undefined;
  const [selected, setSelected] = useState<Record<string, string>>({}); // document_id -> fingerprint
  const [note, setNote] = useState("");
  const [outcome, setOutcome] = useState<BatchReviewResult | null>(null);

  const runs = useQuery({ queryKey: ["compute", "runs"], queryFn: () => unwrap(api.GET("/api/compute/runs", { params: { query: { limit: 20 } } })) });
  const list = useInfiniteQuery({
    queryKey: ["curation", "list", "batch", runId ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/curation", {
          params: { query: { decision: "needs_review", limit: 50, cursor: pageParam, batch_run_id: runId, from_batch: runId ? undefined : true } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") =>
      unwrap(
        api.POST("/api/curation/review-batch", {
          body: {
            decision,
            note: note.trim() || undefined,
            items: Object.entries(selected).map(([document_id, fingerprint]) => ({ document_id, fingerprint })),
          },
        }),
      ),
    onSuccess: (result) => {
      setOutcome(result);
      setSelected({});
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["curation"] });
      queryClient.invalidateQueries({ queryKey: ["compute"] });
    },
  });

  const toggle = (item: CurationItem) =>
    setSelected((s) => {
      const next = { ...s };
      if (item.document_id in next) delete next[item.document_id];
      else next[item.document_id] = item.fingerprint;
      return next;
    });
  const select = (predicate: (i: CurationItem) => boolean) =>
    setSelected(Object.fromEntries(items.filter(predicate).map((i) => [i.document_id, i.fingerprint])));
  const count = Object.keys(selected).length;
  const faithful = items.filter((i) => i.fidelity_ok === true).length;

  return (
    <div className="stack">
      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("curation.batch.title")}</h2>
        <p style={{ margin: 0 }}>
          {t("curation.batch.intro")}
        </p>
        <label>
          {t("curation.batch.run")}
          <select value={runId ?? ""} onChange={(e) => setParams(e.target.value ? { view: "batch", run: e.target.value } : { view: "batch" })}>
            <option value="">{t("curation.batch.allRuns")}</option>
            {(runs.data ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} · {r.backend} · {RUN_STATUS_LABELS[r.status] ?? r.status} · {formatDateTime(r.created_at)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {!isAdmin && <p className="muted">{t("common.adminOnly")}</p>}
      {isAdmin && (
        <div className="card stack">
          <div className="row">
            <button className="small" onClick={() => select((i) => i.fidelity_ok === true)} disabled={faithful === 0}>
              {t("curation.batch.selectFaithful", { n: formatInt(faithful) })}
            </button>
            <button className="small" onClick={() => select(() => true)} disabled={items.length === 0}>
              {t("curation.batch.selectAll")}
            </button>
            <button className="small" onClick={() => setSelected({})} disabled={count === 0}>
              {t("curation.batch.clear")}
            </button>
            <span className="muted">{t("curation.batch.selected", { count, n: formatInt(count) })}</span>
          </div>
          <label>
            {t("curation.note")}
            <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} style={{ fontFamily: "var(--font-text)" }} />
          </label>
          <div className="row">
            <button className="primary" disabled={count === 0 || decide.isPending} onClick={() => decide.mutate("approve")}>
              {t("curation.batch.approve", { n: formatInt(count) })}
            </button>
            <button className="danger" disabled={count === 0 || decide.isPending} onClick={() => decide.mutate("reject")}>
              {t("curation.batch.reject", { n: formatInt(count) })}
            </button>
          </div>
          <ErrorBox error={decide.error} />
          {outcome && (
            <div className={`alert ${outcome.conflicts.length > 0 ? "warn" : "ok"}`} role="status">
              {t("curation.batch.done", { count: outcome.done.length, n: formatInt(outcome.done.length) })}
              {outcome.conflicts.length > 0 && t("curation.batch.conflicts", { count: outcome.conflicts.length, n: formatInt(outcome.conflicts.length) })}
              {outcome.missing.length > 0 && t("curation.batch.missing", { count: outcome.missing.length, n: formatInt(outcome.missing.length) })}.
            </div>
          )}
        </div>
      )}

      <ErrorBox error={list.error} />
      {list.isLoading && <Loading />}
      {list.data && items.length === 0 && <Empty>{t("curation.batch.empty")}</Empty>}
      <ul className="list card" style={{ padding: 0 }}>
        {items.map((item) => (
          <BatchItem key={item.document_id} item={item} selectable={isAdmin} checked={item.document_id in selected} onToggle={() => toggle(item)} />
        ))}
      </ul>
      {list.hasNextPage && (
        <div>
          <button onClick={() => list.fetchNextPage()} disabled={list.isFetchingNextPage}>
            {list.isFetchingNextPage ? t("curation.loading") : t("curation.loadMore")}
          </button>
        </div>
      )}
    </div>
  );
}

function BatchItem({ item, selectable, checked, onToggle }: { item: CurationItem; selectable: boolean; checked: boolean; onToggle: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <li style={{ padding: "0.75rem 1rem", borderBottom: "var(--border)" }}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        {selectable && <input type="checkbox" aria-label={t("curation.batch.select", { title: item.title })} checked={checked} onChange={onToggle} />}
        <div className="stack" style={{ flex: 1, minWidth: 0 }}>
          <strong>{item.title}</strong>
          <small className="muted">
            {sourceName(item.source_id)} · {item.curator_model ?? t("curation.batch.unknownModel")}
          </small>
          {item.reason && <small>{item.reason}</small>}
          <span className="row">
            <span className={`badge ${item.fidelity_ok ? "ok" : "danger"}`}>{item.fidelity_ok ? t("curation.batch.faithful") : t("curation.batch.fidelityFailed")}</span>
            {item.flags.map((f) => (
              <span key={f} className="badge warn">
                {f}
              </span>
            ))}
          </span>
        </div>
        <button className="small" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? t("curation.batch.close") : t("curation.batch.view")}
        </button>
      </div>
      {open && <Compare documentId={item.document_id} />}
    </li>
  );
}

function Compare({ documentId }: { documentId: string }) {
  const detail = useQuery({
    queryKey: ["curation", "item", documentId],
    queryFn: () => unwrap(api.GET("/api/curation/item", { params: { query: { document_id: documentId } } })),
  });
  if (detail.isLoading) return <Loading />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const d = detail.data as CurationDetail;
  return (
    <div className="compare" style={{ marginTop: "0.75rem" }}>
      <div>
        <h4 style={{ margin: "0 0 0.4rem" }}>{t("curation.batch.original")}</h4>
        <pre>{d.original_md}</pre>
      </div>
      <div>
        <h4 style={{ margin: "0 0 0.4rem" }}>{t("curation.batch.curated")}</h4>
        {d.curated_md ? <pre>{d.curated_md}</pre> : <p className="muted">{t("curation.batch.noRewrite")}</p>}
      </div>
    </div>
  );
}
