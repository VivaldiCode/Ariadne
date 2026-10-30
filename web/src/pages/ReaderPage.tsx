import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { BookInfo, LessonInfo, NeighborInfo } from "../api/types";
import { Markdown } from "../components/Markdown";
import { Empty, ErrorBox, Loading, PageHead } from "../components/ui";
import { t, tDynamic } from "../i18n";
import { LICENSE_LABELS, formatInt, sourceName } from "../lib/format";

const STATUS_TONE: Record<string, string> = { accepted: "ok", proposed: "warn", computed: "" };

/** A lição no seu curso: onde está, o que requer, o que depende dela e os exercícios. */
function LessonPanel({ lesson }: { lesson: LessonInfo }) {
  return (
    <section className="alert info" aria-label={t("reader.lesson.aria")}>
      <p>
        <span className="badge primary">{t("reader.lesson.badge")}</span>{" "}
        <Link to={`/courses?course=${encodeURIComponent(lesson.course_id)}`}>{lesson.course_title}</Link> · {lesson.module_title} · {t("reader.lesson.number", { position: lesson.position })}{" "}
        <small className="muted">{t("reader.lesson.version", { version: lesson.course_version })}</small>
        {lesson.moodle_url && (
          <>
            {" "}
            <a href={lesson.moodle_url} target="_blank" rel="noreferrer">
              {t("reader.lesson.openMoodle")}
            </a>
          </>
        )}
      </p>
      {lesson.requires.length > 0 && (
        <p>
          {t("reader.lesson.requires")}{" "}
          {lesson.requires.map((r, i) => (
            <span key={r.document_id}>
              {i > 0 && ", "}
              <Link to={`/read?id=${encodeURIComponent(r.document_id)}`}>{r.title}</Link>
            </span>
          ))}
        </p>
      )}
      {lesson.required_by.length > 0 && (
        <p>
          {t("reader.lesson.requiredBy")}{" "}
          {lesson.required_by.map((r, i) => (
            <span key={r.document_id}>
              {i > 0 && ", "}
              <Link to={`/read?id=${encodeURIComponent(r.document_id)}`}>{r.title}</Link>
            </span>
          ))}
        </p>
      )}
      {lesson.exercises.map((e) => (
        <p key={e.id}>
          {t("reader.lesson.exercise", { title: e.title || e.id, language: e.language, mapped: e.mapped_tests, tests: e.tests })}{" "}
          <span className={`badge ${e.gradable ? "ok" : "warn"}`}>{e.gradable ? t("reader.lesson.gradable") : t("reader.lesson.notGradable")}</span>
        </p>
      ))}
      <small className="muted">
        {lesson.license_name} ({lesson.license_class === "private" ? t("reader.lesson.private") : t("reader.lesson.open")}) · {lesson.attribution}
      </small>
    </section>
  );
}

/** O capítulo no seu livro: onde está, o capítulo anterior e o seguinte, o leitor e a citação. */
function BookPanel({ book, citation }: { book: BookInfo; citation: string | null | undefined }) {
  return (
    <section className="alert info" aria-label={t("reader.book.aria")}>
      <p>
        <span className="badge primary">{t("reader.book.badge")}</span> <strong>{book.title}</strong>
        {book.authors.length > 0 && <> — {book.authors.join(", ")}</>} · {t("reader.book.chapterOf", { position: book.chapter_position, total: book.chapters })}
        {book.start_page ? ` · ${t("reader.book.pages", { start: book.start_page, end: book.end_page ?? "" })}` : ""}{" "}
        <Link to={`/books?book=${encodeURIComponent(book.book_id)}`}>{t("reader.book.view")}</Link>
        {book.reader_url && (
          <>
            {" "}
            <a href={book.reader_url} target="_blank" rel="noreferrer">
              {t("reader.book.openKavita")}
            </a>
          </>
        )}
      </p>
      {(book.previous || book.next) && (
        <p>
          {book.previous && <Link to={`/read?id=${encodeURIComponent(book.previous.document_id)}`}>← {book.previous.title}</Link>}
          {book.previous && book.next && " · "}
          {book.next && <Link to={`/read?id=${encodeURIComponent(book.next.document_id)}`}>{book.next.title} →</Link>}
        </p>
      )}
      {citation && (
        <p>
          {t("reader.book.citation")} <code>{citation}</code>
        </p>
      )}
      {book.held_reason && <p className="alert warn">{t("reader.book.held", { reason: book.held_reason })}</p>}
      <small className="muted">
        {book.license_name} ({LICENSE_LABELS[book.license_class] ?? book.license_class}){book.attribution ? ` · ${book.attribution}` : ""}
      </small>
    </section>
  );
}

export function ReaderPage() {
  const [params, setParams] = useSearchParams();
  const id = params.get("id") ?? "";
  const section = params.get("section") ?? "";
  const offset = Number(params.get("offset") ?? "0") || 0;

  const doc = useQuery({
    queryKey: ["document", id, section, offset],
    enabled: !!id,
    queryFn: () =>
      unwrap(
        api.GET("/api/documents/{document_id}", {
          params: { path: { document_id: id }, query: { section: section || undefined, offset: offset || undefined } },
        }),
      ),
  });
  const neighbors = useQuery({
    queryKey: ["neighbors", id, "reader"],
    enabled: !!id,
    queryFn: () => unwrap(api.GET("/api/neighbors", { params: { query: { document_id: id, k: 60 } } })),
  });
  const translations = useQuery({
    queryKey: ["translations", id],
    enabled: !!id,
    queryFn: () => unwrap(api.GET("/api/translations", { params: { query: { document_id: id } } })),
  });

  if (!id) {
    return (
      <>
        <PageHead title={t("reader.title")} />
        <Empty>
          {t("reader.noDocument.before")} <Link to="/">{t("reader.noDocument.link")}</Link> {t("reader.noDocument.after")}
        </Empty>
      </>
    );
  }

  const cross = (neighbors.data?.neighbors ?? []).filter((n) => n.kind !== "zim_internal");
  const internal = (neighbors.data?.neighbors ?? []).filter((n) => n.kind === "zim_internal");
  const bySource = new Map<string, NeighborInfo[]>();
  cross.forEach((n) => bySource.set(n.source_id, [...(bySource.get(n.source_id) ?? []), n]));

  const others = (translations.data?.translations ?? []).filter((tr) => tr.document_id !== id);
  const concept = translations.data?.concept;

  return (
    <>
      <PageHead
        title={doc.data?.title ?? t("reader.title")}
        subtitle={doc.data ? `${t("reader.subtitle", { source: sourceName(doc.data.source_id), version: doc.data.version })}${doc.data.language ? ` · ${doc.data.language}` : ""}` : undefined}
        actions={
          <div className="row">
            <Link className="btn" to={`/graph?id=${encodeURIComponent(id)}`}>
              {t("reader.viewGraph")}
            </Link>
            {doc.data?.wiki_url && (
              <a className="btn" href={doc.data.wiki_url} target="_blank" rel="noreferrer">
                Wiki.js
              </a>
            )}
            {doc.data?.kiwix_url && (
              <a className="btn" href={doc.data.kiwix_url} target="_blank" rel="noreferrer">
                {t("reader.kiwix")}
              </a>
            )}
          </div>
        }
      />
      <ErrorBox error={doc.error} />
      {doc.isLoading && <Loading />}
      {doc.data && (
        <div className="grid reader">
          <article className="card">
            {doc.data.lesson && <LessonPanel lesson={doc.data.lesson} />}
            {doc.data.book && <BookPanel book={doc.data.book} citation={doc.data.citation} />}
            {offset > 0 && (
              <p>
                {t("reader.fromOffset", { offset: formatInt(offset) })}{" "}
                <button className="link" onClick={() => setParams({ id })}>
                  {t("reader.fullChapter")}
                </button>
              </p>
            )}
            {translations.data?.is_original && (
              <p>
                <span className="badge seal">{t("reader.original")}</span> <small className="muted">{t("reader.original.note")}</small>
              </p>
            )}
            {doc.data.room && (
              <p>
                <span className="badge" title={doc.data.wing ? t("reader.room.title", { wing: doc.data.wing }) : undefined}>
                  {t("reader.room", { label: doc.data.room.label })}
                </span>{" "}
                <Link to={`/?q=${encodeURIComponent(doc.data.title)}&room=${doc.data.room.id}`}>{t("reader.room.search")}</Link>
              </p>
            )}
            {doc.data.redirected_from && <p className="muted">{t("reader.redirected", { from: doc.data.redirected_from })}</p>}
            {doc.data.flags.length > 0 && <div className="alert warn">{t("reader.flags", { flags: doc.data.flags.join(", ") })}</div>}
            {section && (
              <p>
                {t("reader.section", { section })}{" "}
                <button className="link" onClick={() => setParams({ id })}>
                  {t("reader.fullArticle")}
                </button>
              </p>
            )}
            <Markdown text={doc.data.text} />
            {doc.data.truncated && (
              <p className="muted">
                {t("reader.truncated", { total: formatInt(doc.data.total_chars) })}
              </p>
            )}
            <TranslateBox text={doc.data.text} sourceLanguage={doc.data.language} documentId={doc.data.document_id} />
          </article>

          <aside className="card sidepanel" aria-label={t("reader.links.aria")}>
            <h3>{t("reader.links.title")}</h3>
            <ErrorBox error={neighbors.error} />
            {neighbors.isLoading && <Loading />}
            {neighbors.data && cross.length === 0 && <p className="muted">{t("reader.links.none")}</p>}
            {[...bySource.entries()].map(([source, items]) => (
              <div key={source}>
                <strong>{sourceName(source)}</strong>
                <ul className="linklist">
                  {items.map((n) => (
                    <li key={`${n.document_id}-${n.kind}`}>
                      <Link to={`/read?id=${encodeURIComponent(n.document_id)}`}>{n.title}</Link>
                      <span className="badge">{tDynamic(`reader.kind.${n.kind}`) ?? n.kind}</span>
                      <span className={`badge ${STATUS_TONE[n.status] ?? ""}`}>{n.status}</span>
                      {n.language && <small>{n.language}</small>}
                    </li>
                  ))}
                </ul>
              </div>
            ))}

            <h3>{t("reader.langs.title")}</h3>
            <ErrorBox error={translations.error} />
            {concept && (
              <p className="muted">
                {t("reader.langs.topic", { label: concept.label ?? concept.wikidata_qid ?? `#${concept.id}` })}
                {concept.origin_lang ? ` · ${t("reader.langs.origin", { lang: concept.origin_lang })}` : ""}
              </p>
            )}
            {translations.data && others.length === 0 && <p className="muted">{t("reader.langs.none")}</p>}
            <ul className="linklist">
              {others.map((tr) => (
                <li key={tr.document_id}>
                  <Link to={`/read?id=${encodeURIComponent(tr.document_id)}`}>{tr.title}</Link>
                  <span className="badge">{tr.language ?? "?"}</span>
                  {tr.original_language && <span className="badge seal">fonte original</span>}
                </li>
              ))}
            </ul>

            {doc.data.sections.length > 0 && (
              <>
                <h3>{t("reader.sections")}</h3>
                <ul>
                  {doc.data.sections.map((s) => (
                    <li key={s.title} style={{ paddingLeft: `${Math.max(0, s.level - 1) * 0.75}rem` }}>
                      <button className="link" onClick={() => setParams({ id, section: s.title })}>
                        {s.title}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {internal.length > 0 && (
              <details>
                <summary>{t("reader.internal", { count: internal.length })}</summary>
                <ul className="linklist">
                  {internal.map((n) => (
                    <li key={`${n.document_id}-${n.direction}`}>
                      <Link to={`/read?id=${encodeURIComponent(n.document_id)}`}>{n.title}</Link>
                      <small>{n.direction === "in" ? t("reader.internal.in") : "→"}</small>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </aside>
        </div>
      )}
    </>
  );
}

function TranslateBox({ text, sourceLanguage, documentId }: { text: string; sourceLanguage: string | null | undefined; documentId: string }) {
  const [target, setTarget] = useState(sourceLanguage === "en" ? "pt" : "en");
  const translate = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/translate", { body: { text, target, source: sourceLanguage ?? undefined, document_id: documentId } })),
  });
  return (
    <details style={{ marginTop: "1rem" }}>
      <summary>{t("reader.translate.summary")}</summary>
      <div className="row" style={{ margin: "0.5rem 0" }}>
        <label className="inline">
          {t("reader.translate.to")}
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="pt">{t("reader.translate.lang.pt")}</option>
            <option value="en">{t("reader.translate.lang.en")}</option>
            <option value="es">{t("reader.translate.lang.es")}</option>
            <option value="fr">{t("reader.translate.lang.fr")}</option>
          </select>
        </label>
        <button onClick={() => translate.mutate()} disabled={translate.isPending}>
          {translate.isPending ? t("reader.translate.busy") : t("reader.translate.submit")}
        </button>
      </div>
      <ErrorBox error={translate.error} />
      {translate.data && !translate.data.translated && (
        <div className="alert warn">{translate.data.warning ?? t("reader.translate.unavailable")}</div>
      )}
      {translate.data?.translated && <Markdown text={translate.data.text} />}
    </details>
  );
}
