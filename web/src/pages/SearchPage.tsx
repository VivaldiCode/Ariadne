import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { SearchHit } from "../api/types";
import { Empty, ErrorBox, LevelBadge, Loading, PageHead } from "../components/ui";
import { plainText, sourceName } from "../lib/format";
import { t } from "../i18n";

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const lang = params.get("lang") ?? "";
  const sources = params.getAll("source");
  const room = params.get("room") ?? "";
  const [text, setText] = useState(q);

  const sourceList = useQuery({
    queryKey: ["sources"],
    queryFn: () => unwrap(api.GET("/api/sources")),
  });
  // As salas (temas) das alas: só aparece o filtro quando já foram calculadas.
  const roomList = useQuery({
    queryKey: ["rooms"],
    queryFn: () => unwrap(api.GET("/api/rooms", { params: { query: { limit: 500 } } })),
  });
  const rooms = roomList.data ?? [];
  const languages = [...new Set((sourceList.data ?? []).map((s) => s.language).filter((l): l is string => !!l))].sort();

  const results = useQuery({
    queryKey: ["search", q, lang, sources.join(","), room],
    enabled: q.trim() !== "",
    queryFn: () =>
      unwrap(
        api.GET("/api/search", {
          params: {
            query: {
              q,
              lang: lang || undefined,
              sources: sources.length ? sources : undefined,
              rooms: room ? [Number(room)] : undefined,
              k: 20,
            },
          },
        }),
      ),
  });

  function update(next: { q?: string; lang?: string; sources?: string[]; room?: string }) {
    const p = new URLSearchParams();
    const nq = next.q ?? q;
    const nl = next.lang ?? lang;
    const ns = next.sources ?? sources;
    const nr = next.room ?? room;
    if (nq) p.set("q", nq);
    if (nl) p.set("lang", nl);
    ns.forEach((s) => p.append("source", s));
    if (nr) p.set("room", nr);
    setParams(p);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    update({ q: text.trim() });
  }

  function toggleSource(id: string) {
    update({ sources: sources.includes(id) ? sources.filter((s) => s !== id) : [...sources, id] });
  }

  return (
    <>
      <PageHead title={t("search.title")} subtitle={t("search.subtitle")} />
      <form className="card stack" onSubmit={submit} role="search">
        <div className="field-row">
          <label className="grow">
            {t("search.query")}
            <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder={t("search.query.placeholder")} autoFocus />
          </label>
          <label>
            {t("search.language")}
            <select value={lang} onChange={(e) => update({ lang: e.target.value })}>
              <option value="">{t("search.all")}</option>
              {languages.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <button className="primary" type="submit" disabled={text.trim() === ""}>
            {t("search.submit")}
          </button>
        </div>
        {rooms.length > 0 && (
          <label>
            {t("search.room")}
            <select value={room} onChange={(e) => update({ room: e.target.value })}>
              <option value="">{t("search.all")}</option>
              {rooms.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label} ({r.size})
                </option>
              ))}
            </select>
          </label>
        )}
        {sourceList.data && sourceList.data.length > 1 && (
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="muted">{t("search.sources")}</legend>
            <div className="row">
              {sourceList.data.map((s) => (
                <label key={s.id} className="inline">
                  <input type="checkbox" checked={sources.includes(s.id)} onChange={() => toggleSource(s.id)} />
                  {sourceName(s.id)}
                  {s.language ? ` (${s.language})` : ""}
                </label>
              ))}
            </div>
          </fieldset>
        )}
      </form>

      <div style={{ marginTop: "1rem" }}>
        {results.isFetching && <Loading label={t("search.loading")} />}
        <ErrorBox error={results.error} />
        {results.data?.warnings.map((w) => (
          <div key={w} className="alert warn" style={{ marginBottom: "0.5rem" }}>
            {w}
          </div>
        ))}
        {results.data && results.data.results.length === 0 && <Empty>{t("search.none")}</Empty>}
        {results.data && results.data.results.length > 0 && (
          <ol className="results" aria-label={t("search.results")}>
            {results.data.results.map((hit) => (
              <Result key={hit.document_id} hit={hit} onRoom={(id) => update({ room: String(id) })} />
            ))}
          </ol>
        )}
        {!q && <Empty>{t("search.prompt")}</Empty>}
      </div>
    </>
  );
}

export function Result({ hit, onRoom }: { hit: SearchHit; onRoom?: (id: number) => void }) {
  const snippet = plainText(hit.snippet);
  return (
    <li className="result">
      <div className="meta">
        <LevelBadge level={hit.level} />
        <span className="badge">{hit.score.toFixed(2)}</span>
        {hit.original_source && (
          <span className="badge seal" title={t("search.hit.original.title")}>
            {t("search.hit.original")}
          </span>
        )}
        {hit.translated_query && (
          <span className="badge" title={t("search.hit.translatedQuery.title")}>
            {t("search.hit.translatedQuery")}
          </span>
        )}
        <span className="badge">{sourceName(hit.source_id)}</span>
        {hit.content_type === "lesson" && (
          <span className="badge primary" title={t("search.hit.lesson.title")}>
            {t("search.hit.lesson")}
          </span>
        )}
        {hit.content_type === "chapter" && (
          <span className="badge primary" title={t("search.hit.chapter.title")}>
            {t("search.hit.chapter")}
          </span>
        )}
        {hit.book?.license_class === "private" && (
          <span className="badge warn" title={t("search.hit.private.title")}>
            {t("search.hit.private")}
          </span>
        )}
        {hit.language && <span className="badge">{hit.language}</span>}
        {hit.room && (
          <span className="badge" title={t("search.hit.room.title", { id: hit.room.id, wing: hit.wing ?? "" })}>
            {t("search.hit.room", { label: hit.room.label })}
            {onRoom && (
              <>
                {" "}
                <button className="link" type="button" onClick={() => onRoom(hit.room!.id)} aria-label={t("search.hit.roomOnly.aria", { label: hit.room.label })}>
                  {t("search.hit.roomOnly")}
                </button>
              </>
            )}
          </span>
        )}
        {hit.flags.length > 0 && <span className="badge warn">{hit.flags.join(", ")}</span>}
      </div>
      <h3>
        <Link to={`/read?id=${encodeURIComponent(hit.document_id)}${hit.position ? `&offset=${hit.position}` : ""}`}>{hit.title}</Link>
      </h3>
      {hit.book && (
        <p className="muted">
          {hit.book.title}
          {hit.book.authors.length > 0 && ` — ${hit.book.authors.join(", ")}`} · {t("search.hit.chapterOf", { position: hit.book.chapter_position, total: hit.book.chapters })}
          {hit.book.start_page ? ` · ${t("search.hit.page", { page: hit.book.start_page })}` : ""}
        </p>
      )}
      <p className="snippet">{snippet.length > 320 ? `${snippet.slice(0, 320)}…` : snippet}</p>
      {hit.citation && (
        <p>
          <small className="muted">{t("search.hit.citation")}</small>
          <code>{hit.citation}</code>
        </p>
      )}
      <div className="row" style={{ marginTop: "0.4rem" }}>
        <Link to={`/graph?id=${encodeURIComponent(hit.document_id)}`}>{t("search.hit.graph")}</Link>
        {hit.wiki_url && (
          <a href={hit.wiki_url} target="_blank" rel="noreferrer">
            Wiki.js
          </a>
        )}
        {hit.kiwix_url && (
          <a href={hit.kiwix_url} target="_blank" rel="noreferrer">
            Kiwix
          </a>
        )}
        {hit.reader_url && (
          <a href={hit.reader_url} target="_blank" rel="noreferrer">
            Kavita
          </a>
        )}
      </div>
    </li>
  );
}

