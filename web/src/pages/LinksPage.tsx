import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { LinkStatus, LinkView } from "../api/types";
import { useAuth } from "../auth";
import { DocumentPicker } from "../components/DocumentPicker";
import { Empty, ErrorBox, Loading, PageHead, Tabs } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, sourceName } from "../lib/format";

const KINDS = ["", "vector", "title_match", "wikidata", "langlink", "manual"];

export function LinksPage() {
  return (
    <>
      <PageHead title={t("links.title")} subtitle={t("links.subtitle")} />
      <ProposalsSection />
      <div className="grid cols-2" style={{ marginTop: "1rem", alignItems: "start" }}>
        <ManualLink />
        <ConceptOrigin />
      </div>
      <Collections />
    </>
  );
}

function ProposalsSection() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<LinkStatus>("proposed");
  const [kind, setKind] = useState("");
  const [documentId, setDocumentId] = useState("");

  const list = useInfiniteQuery({
    queryKey: ["links", status, kind, documentId],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/api/links", { params: { query: { status, kind: kind || undefined, document_id: documentId || undefined, limit: 30, cursor: pageParam } } })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  const review = useMutation({
    mutationFn: (vars: { id: number; status: LinkStatus }) =>
      unwrap(api.POST("/api/links/{link_id}", { params: { path: { link_id: vars.id } }, body: { status: vars.status } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["links"] }),
  });

  return (
    <section className="card">
      <h2>{t("links.proposals.title")}</h2>
      <Tabs
        tabs={[
          { id: "proposed", label: t("links.tab.proposed") },
          { id: "accepted", label: t("links.tab.accepted") },
          { id: "rejected", label: t("links.tab.rejected") },
        ]}
        value={status}
        onChange={setStatus}
      />
      <div className="field-row" style={{ marginBottom: "0.75rem" }}>
        <label>
          {t("links.kind")}
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k || t("links.kind.all")}
              </option>
            ))}
          </select>
        </label>
        <div className="grow">
          <DocumentPicker label={t("links.onlyDocument")} value={documentId} onChange={setDocumentId} />
        </div>
        {documentId && (
          <button className="small" onClick={() => setDocumentId("")}>
            {t("links.clear")}
          </button>
        )}
      </div>
      <ErrorBox error={list.error ?? review.error} />
      {list.isLoading && <Loading />}
      {list.data && items.length === 0 && <Empty>{t("links.empty")}</Empty>}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("links.col.link")}</th>
                <th>{t("links.kind")}</th>
                <th className="num">{t("links.col.score")}</th>
                <th>{t("links.col.created")}</th>
                <th aria-label={t("links.col.actions")} />
              </tr>
            </thead>
            <tbody>
              {items.map((l) => (
                <LinkRow key={l.id} link={l} canReview={isAdmin} busy={review.isPending} onReview={(next) => review.mutate({ id: l.id, status: next })} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {list.hasNextPage && (
        <p>
          <button onClick={() => list.fetchNextPage()} disabled={list.isFetchingNextPage}>
            {list.isFetchingNextPage ? t("links.loading") : t("links.loadMore")}
          </button>
        </p>
      )}
    </section>
  );
}

function LinkRow({ link, canReview, busy, onReview }: { link: LinkView; canReview: boolean; busy: boolean; onReview: (status: LinkStatus) => void }) {
  return (
    <tr>
      <td>
        <Link to={`/read?id=${encodeURIComponent(link.src_document)}`}>{link.src_title}</Link> <small className="muted">({sourceName(link.src_source)})</small>
        <br />
        <span aria-hidden="true">↔ </span>
        <Link to={`/read?id=${encodeURIComponent(link.dst_document)}`}>{link.dst_title}</Link> <small className="muted">({sourceName(link.dst_source)})</small>
      </td>
      <td>
        <span className="badge">{link.kind}</span>
      </td>
      <td className="num">{link.score == null ? "–" : link.score.toFixed(3)}</td>
      <td>
        <small>
          {formatDateTime(link.created_at)}
          {link.created_by ? ` · ${link.created_by}` : ""}
        </small>
      </td>
      <td>
        {canReview && (
          <div className="row end">
            {link.status !== "accepted" && (
              <button className="small primary" disabled={busy} onClick={() => onReview("accepted")}>
                {t("links.accept")}
              </button>
            )}
            {link.status !== "rejected" && (
              <button className="small danger" disabled={busy} onClick={() => onReview("rejected")}>
                {t("links.reject")}
              </button>
            )}
            {link.status === "rejected" && (
              <button className="small" disabled={busy} onClick={() => onReview("proposed")}>
                {t("links.reopen")}
              </button>
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

function ManualLink() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/links", { body: { src_document: src, dst_document: dst } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["links"] }),
  });
  if (!isAdmin) {
    return (
      <section className="card stack">
        <h2>{t("links.manual.title")}</h2>
        <p className="muted">{t("common.adminOnly")}</p>
      </section>
    );
  }
  return (
    <section className="card stack">
      <h2>{t("links.manual.title")}</h2>
      <p className="muted">{t("links.manual.help")}</p>
      <DocumentPicker label={t("links.manual.docA")} value={src} onChange={setSrc} />
      <DocumentPicker label={t("links.manual.docB")} value={dst} onChange={setDst} />
      <ErrorBox error={create.error} />
      {create.data && (
        <div className="alert ok">
          {t("links.manual.created", {
            id: create.data.id,
            status: create.data.status === "accepted" ? t("links.manual.accepted") : create.data.status,
            src: create.data.src_title,
            dst: create.data.dst_title,
          })}
        </div>
      )}
      <div>
        <button className="primary" disabled={!src || !dst || src === dst || create.isPending} onClick={() => create.mutate()}>
          {t("links.manual.create")}
        </button>
      </div>
    </section>
  );
}

function ConceptOrigin() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [ref, setRef] = useState("");
  const [lookup, setLookup] = useState("");
  const [lang, setLang] = useState("");

  const concept = useQuery({
    queryKey: ["concept", lookup],
    enabled: lookup !== "",
    queryFn: () => unwrap(api.GET("/api/concepts/{ref}", { params: { path: { ref: lookup } } })),
  });
  const save = useMutation({
    mutationFn: (origin: string | null) => unwrap(api.POST("/api/concepts/{ref}", { params: { path: { ref: lookup } }, body: { origin_lang: origin } })),
    onSuccess: (data) => {
      queryClient.setQueryData(["concept", lookup], data);
      setLang("");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setLookup(ref.trim());
  }

  return (
    <section className="card stack">
      <h2>{t("links.origin.title")}</h2>
      <p className="muted">{t("links.origin.help")}</p>
      <form className="field-row" onSubmit={submit}>
        <label className="grow">
          {t("links.origin.ref")}
          <input type="text" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="Q1187566" />
        </label>
        <button type="submit" disabled={ref.trim() === ""}>
          {t("links.origin.lookup")}
        </button>
      </form>
      <ErrorBox error={concept.error ?? save.error} />
      {concept.data && (
        <div className="stack">
          <p>
            <strong>{concept.data.label ?? concept.data.wikidata_qid ?? `#${concept.data.id}`}</strong> · {t("links.origin.documents", { count: concept.data.documents })}
            <br />
            {t("links.origin.current")}{" "}
            {concept.data.origin_lang ? (
              <>
                <span className="badge seal">{concept.data.origin_lang}</span> <small className="muted">({concept.data.origin_source ?? "?"})</small>
              </>
            ) : (
              <span className="muted">{t("links.origin.unset")}</span>
            )}
          </p>
          {!isAdmin && <p className="muted">{t("common.adminOnly")}</p>}
          {isAdmin && (
            <div className="field-row">
              <label>
                {t("links.origin.newLang")}
                <input type="text" value={lang} onChange={(e) => setLang(e.target.value)} style={{ width: "7rem" }} maxLength={8} />
              </label>
              <button className="primary" disabled={lang.trim() === "" || save.isPending} onClick={() => save.mutate(lang.trim().toLowerCase())}>
                {t("links.origin.set")}
              </button>
              {concept.data.origin_lang && (
                <button className="danger" disabled={save.isPending} onClick={() => save.mutate(null)}>
                  {t("links.origin.delete")}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Collections() {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const sources = useQuery({ queryKey: ["sources"], queryFn: () => unwrap(api.GET("/api/sources")) });
  const list = useQuery({ queryKey: ["collections"], queryFn: () => unwrap(api.GET("/api/collections")) });
  const [name, setName] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [paths, setPaths] = useState("");

  const chosen = sourceId || sources.data?.[0]?.id || "";
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/collections", {
          body: { name: name.trim(), source_id: chosen, paths: paths.split("\n").map((p) => p.trim()).filter(Boolean), priority: 0.1 },
        }),
      ),
    onSuccess: () => {
      setPaths("");
      setName("");
      queryClient.invalidateQueries({ queryKey: ["collections"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("links.collections.title")}</h2>
      <p className="muted">{t("links.collections.help")}</p>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        {!isAdmin && <p className="muted">{t("common.adminOnly")}</p>}
        {isAdmin && (
          <div className="stack">
            <div className="field-row">
              <label className="grow">
                {t("links.collections.name")}
                <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("links.collections.namePlaceholder")} />
              </label>
              <label>
                {t("links.collections.source")}
                <select value={chosen} onChange={(e) => setSourceId(e.target.value)}>
                  {sources.data?.map((s) => (
                    <option key={s.id} value={s.id}>
                      {sourceName(s.id)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              {t("links.collections.paths")}
              <textarea rows={8} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder={t("links.collections.pathsPlaceholder")} />
            </label>
            <ErrorBox error={create.error} />
            {create.data && (
              <div className="alert ok">
                {t("links.collections.result", { name: create.data.name, queued: create.data.queued, known: create.data.known })}
                {create.data.ignored.length > 0 &&
                  `, ${t("links.collections.ignored", { count: create.data.ignored.length, list: create.data.ignored.slice(0, 5).join(", ") })}`}
                .
              </div>
            )}
            <div>
              <button className="primary" disabled={!name.trim() || !chosen || !paths.trim() || create.isPending} onClick={() => create.mutate()}>
                {create.isPending ? t("links.collections.queuing") : t("links.collections.queue")}
              </button>
            </div>
          </div>
        )}
        <div>
          <h3>{t("links.collections.requested")}</h3>
          <ErrorBox error={list.error} />
          {list.data && list.data.length === 0 && <p className="muted">{t("links.collections.none")}</p>}
          {list.data && list.data.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("links.collections.name")}</th>
                    <th>{t("links.collections.source")}</th>
                    <th className="num">{t("links.collections.col.queued")}</th>
                    <th className="num">{t("links.collections.col.known")}</th>
                    <th>{t("links.collections.col.when")}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.map((c) => (
                    <tr key={c.id}>
                      <td>{c.name}</td>
                      <td>{sourceName(c.source_id)}</td>
                      <td className="num">{c.queued}</td>
                      <td className="num">{c.known}</td>
                      <td>
                        <small>
                          {formatDateTime(c.created_at)} · {c.created_by}
                        </small>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
