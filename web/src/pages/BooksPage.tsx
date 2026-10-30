import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { BookChapter, BookView } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, PageHead } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, formatInt, LICENSE_LABELS } from "../lib/format";

export function BooksPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("book") ?? "";
  const books = useQuery({ queryKey: ["books"], queryFn: () => unwrap(api.GET("/api/books")) });

  return (
    <>
      <PageHead
        title={t("books.title")}
        subtitle={t("books.subtitle")}
      />
      <ErrorBox error={books.error} />
      {books.isLoading && <Loading />}
      {books.data && books.data.length === 0 && (
        <Empty>
          {t("books.empty.before")} <code>{t("books.empty.command")}</code>.
        </Empty>
      )}
      {(books.data ?? []).map((book) => (
        <section key={book.id} className="card" style={{ marginBottom: "1rem" }}>
          <BookHeader book={book} />
          <p>
            <button className="small" onClick={() => setParams(selected === book.id ? {} : { book: book.id })}>
              {selected === book.id ? t("books.close_chapters") : t("books.open_chapters")}
            </button>
          </p>
          {selected === book.id && <Chapters bookId={book.id} />}
        </section>
      ))}
    </>
  );
}

function BookHeader({ book }: { book: BookView }) {
  const { me } = useAuth();
  const client = useQueryClient();
  const index = useMutation({
    mutationFn: () => unwrap(api.POST("/api/books/{book_id}/index", { params: { path: { book_id: book.id } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["books"] }),
  });
  const license = useMutation({
    mutationFn: (license_class: "open" | "private") =>
      unwrap(
        api.PUT("/api/books/{book_id}/license", {
          params: { path: { book_id: book.id } },
          body: { license_class, confirm: license_class === "open" },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["books"] }),
  });
  const makeOpen = () => {
    if (window.confirm(t("books.confirm_open", { title: book.title }))) license.mutate("open");
  };
  return (
    <>
      <h2>
        {book.title}{" "}
        <small className="muted">
          {book.authors.join(", ") || t("books.unknown_author")} · {book.format.toUpperCase()}
          {book.language ? ` · ${book.language}` : ""}
        </small>
      </h2>
      <p>
        <span className={`badge ${book.license_class === "private" ? "warn" : "ok"}`} title={t("books.license_class")}>
          {LICENSE_LABELS[book.license_class] ?? book.license_class}
        </span>{" "}
        {book.license_name}
        {book.attribution && <small className="muted"> · {book.attribution}</small>}
      </p>
      <p className="muted">
        {t("books.chapters", { count: book.chapters, n: formatInt(book.chapters) })}
        {book.pages ? t("books.pages", { n: formatInt(book.pages) }) : ""}
        {t("books.characters", { n: formatInt(book.characters) })} · {t("books.at_level3", { n: book.indexed_chapters })}
        {book.held_chapters > 0 && (
          <>
            {" "}
            · <span className="badge warn">{t("books.held", { n: book.held_chapters })}</span>
          </>
        )}
        {book.extraction_note && <small>{t("books.extracted_by", { note: book.extraction_note })}</small>}
        {book.indexed_at && <small> · {formatDateTime(book.indexed_at)}</small>}
      </p>
      <div className="row">
        {book.reader_url ? (
          <a className="btn" href={book.reader_url} target="_blank" rel="noreferrer">
            {t("books.read_kavita")}
          </a>
        ) : (
          <span className="badge" title={t("books.no_kavita_title")}>
            {t("books.no_kavita")}
          </span>
        )}
        {me?.role === "admin" && (
          <>
            <button className="small" disabled={index.isPending} onClick={() => index.mutate()}>
              {t("books.index")}
            </button>
            {book.license_class === "private" ? (
              <button className="small" disabled={license.isPending} onClick={makeOpen}>
                {t("books.make_open")}
              </button>
            ) : (
              <button className="small" disabled={license.isPending} onClick={() => license.mutate("private")}>
                {t("books.make_private")}
              </button>
            )}
          </>
        )}
      </div>
      <ErrorBox error={index.error ?? license.error} />
      {index.isSuccess && <div className="alert ok">{t("books.index_requested")}</div>}
    </>
  );
}

function Chapters({ bookId }: { bookId: string }) {
  const [only, setOnly] = useState(false);
  const detail = useQuery({
    queryKey: ["book", bookId],
    queryFn: () => unwrap(api.GET("/api/books/{book_id}", { params: { path: { book_id: bookId } } })),
  });
  const items: BookChapter[] = (detail.data?.chapter_list ?? []).filter((c) => !only || c.held_reason);
  return (
    <div>
      <ErrorBox error={detail.error} />
      {detail.isLoading && <Loading />}
      {detail.data && (
        <>
          <label>
            <input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> {t("books.only_held")}
          </label>
          <ol className="linklist">
            {items.map((c) => (
              <li key={c.document_id}>
                <Link to={`/read?id=${encodeURIComponent(c.document_id)}`}>{c.title}</Link>{" "}
                <small className="muted">
                  {t("books.chapter.characters", { n: formatInt(c.characters) })}
                  {c.start_page ? t("books.chapter.pages", { start: c.start_page, end: c.end_page ?? "" }) : ""}
                </small>{" "}
                {c.indexed && <span className="badge ok">{t("books.chapter.level3")}</span>}
                {c.license_class === "private" && <span className="badge warn">{t("books.chapter.private")}</span>}
                {c.held_reason && (
                  <span className="badge warn" title={c.held_reason}>
                    {t("books.chapter.held")}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
