import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { CourseDetail, CourseExercise, CourseVersion } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, PageHead, Tabs } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime, formatInt, PUBLISH_LABELS } from "../lib/format";

const PUBLISH_TONE: Record<string, string> = { published: "ok", outdated: "warn", failed: "danger", unpublished: "" };

type Tab = "licoes" | "exercicios" | "historico";

export function CoursesPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("course") ?? "";
  const courses = useQuery({ queryKey: ["courses"], queryFn: () => unwrap(api.GET("/api/courses")) });

  return (
    <>
      <PageHead
        title={t("courses.title")}
        subtitle={t("courses.subtitle")}
      />
      <ErrorBox error={courses.error} />
      {courses.isLoading && <Loading />}
      {courses.data && courses.data.length === 0 && (
        <Empty>
          {t("courses.empty.before")} <code>ariadne courses import-fcc --source zim:freecodecamp_…</code> {t("courses.empty.middle")}{" "}
          <code>ariadne courses sync</code>.
        </Empty>
      )}
      {(courses.data ?? []).map((course) => (
        <section key={course.id} className="card" style={{ marginBottom: "1rem" }}>
          <h2>
            {course.title}{" "}
            <small className="muted">
              {course.id} · {course.language}
              {course.programming_language ? ` · ${course.programming_language}` : ""}
            </small>
          </h2>
          <p className="muted">
            {t("courses.card.version")} <strong>{course.current_version}</strong> · {course.license_name} (
            {course.license_class === "private" ? t("courses.card.private") : t("courses.card.open")}) · {t("courses.card.origin")}{" "}
            {course.origin_source_id ?? "–"}
          </p>
          <VersionsTable courseId={course.id} versions={course.versions} />
          <p>
            <button className="small" onClick={() => setParams(selected === course.id ? {} : { course: course.id })}>
              {selected === course.id ? t("courses.card.close") : t("courses.card.open_detail")}
            </button>
          </p>
          {selected === course.id && <CourseDetailView courseId={course.id} />}
        </section>
      ))}
    </>
  );
}

function VersionsTable({ courseId, versions }: { courseId: string; versions: CourseVersion[] }) {
  const { me } = useAuth();
  const client = useQueryClient();
  const publish = useMutation({
    mutationFn: (version: string) =>
      unwrap(api.POST("/api/courses/{course_id}/publish", { params: { path: { course_id: courseId }, query: { version } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["courses"] }),
  });
  return (
    <div className="table-wrap">
      <ErrorBox error={publish.error} />
      {publish.isSuccess && <div className="alert ok">{t("courses.versions.publish_requested")}</div>}
      <table>
        <thead>
          <tr>
            <th>{t("courses.versions.version")}</th>
            <th className="num">{t("courses.versions.lessons")}</th>
            <th className="num">{t("courses.versions.exercises")}</th>
            <th className="num">{t("courses.versions.tests")}</th>
            <th>{t("courses.versions.commit")}</th>
            <th>{t("courses.versions.moodle")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version}>
              <td>
                <strong>{v.version}</strong> {v.current && <span className="badge primary">{t("courses.versions.current")}</span>}
              </td>
              <td className="num">{formatInt(v.lessons)}</td>
              <td className="num">{formatInt(v.exercises)}</td>
              <td className="num">
                {formatInt(v.tests)}
                {v.unmapped_tests > 0 && (
                  <small className="muted" title={t("courses.versions.unmapped_title")}> ({t("courses.versions.unmapped", { n: v.unmapped_tests })})</small>
                )}
              </td>
              <td>
                <code>{v.git_commit?.slice(0, 10) ?? "–"}</code>
              </td>
              <td>
                <span className={`badge ${PUBLISH_TONE[v.publish_status] ?? ""}`}>{PUBLISH_LABELS[v.publish_status] ?? v.publish_status}</span>{" "}
                {v.platform_url && (
                  <a href={v.platform_url} target="_blank" rel="noreferrer">
                    {t("courses.versions.open")}
                  </a>
                )}
                {v.published_at && <small className="muted"> {formatDateTime(v.published_at)}</small>}
                {v.held > 0 && (
                  <div>
                    <span className="badge warn">{t("courses.versions.held", { n: v.held })}</span>
                  </div>
                )}
                {v.publish_error && <div className="alert error">{v.publish_error}</div>}
              </td>
              <td>
                {me?.role === "admin" && (
                  <button className="small" disabled={publish.isPending} onClick={() => publish.mutate(v.version)}>
                    {v.publish_status === "published" ? t("courses.versions.republish") : t("courses.versions.publish")}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CourseDetailView({ courseId }: { courseId: string }) {
  const [tab, setTab] = useState<Tab>("licoes");
  const detail = useQuery({
    queryKey: ["course", courseId],
    queryFn: () => unwrap(api.GET("/api/courses/{course_id}", { params: { path: { course_id: courseId } } })),
  });
  const exercises = useQuery({
    queryKey: ["course", courseId, "exercises"],
    enabled: tab === "exercicios",
    queryFn: () => unwrap(api.GET("/api/courses/{course_id}/exercises", { params: { path: { course_id: courseId } } })),
  });
  return (
    <div>
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "licoes", label: t("courses.tab.lessons") },
          { id: "exercicios", label: t("courses.tab.exercises") },
          { id: "historico", label: t("courses.tab.history") },
        ]}
      />
      <ErrorBox error={detail.error ?? exercises.error} />
      {detail.isLoading && <Loading />}
      {detail.data && tab === "licoes" && <Lessons detail={detail.data} />}
      {tab === "exercicios" && exercises.isLoading && <Loading />}
      {tab === "exercicios" && exercises.data && <Exercises items={exercises.data} />}
      {detail.data && tab === "historico" && (
        <ul className="linklist">
          {detail.data.commits.map((c) => (
            <li key={c.sha}>
              <code>{c.sha.slice(0, 10)}</code> {c.subject} <small className="muted">{formatDateTime(c.date)}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Lessons({ detail }: { detail: CourseDetail }) {
  const modules = new Map<string, { title: string; lessons: CourseDetail["lessons"] }>();
  detail.lessons.forEach((l) => {
    const group = modules.get(l.module_id) ?? { title: l.module_title, lessons: [] };
    group.lessons.push(l);
    modules.set(l.module_id, group);
  });
  const title = new Map(detail.lessons.map((l) => [l.document_id, l.title]));
  return (
    <>
      {[...modules.entries()].map(([id, group]) => (
        <div key={id}>
          <h3>
            {group.title} <small className="muted">{t("courses.lessons.count", { count: group.lessons.length })}</small>
          </h3>
          <ol className="linklist">
            {group.lessons.map((l) => (
              <li key={l.document_id}>
                <Link to={`/read?id=${encodeURIComponent(l.document_id)}`}>{l.title}</Link>{" "}
                {l.exercises > 0 && <span className="badge">{t("courses.lessons.exercise")}</span>}
                {l.platform_kind && (
                  <a className="badge ok" href={l.platform_url ?? undefined} target="_blank" rel="noreferrer">
                    Moodle · {l.platform_kind === "quiz" ? t("courses.lessons.quiz") : t("courses.lessons.page")}
                  </a>
                )}
                {l.held_reason && <span className="badge warn" title={l.held_reason}>{t("courses.lessons.held")}</span>}
                {l.requires.length > 0 && (
                  <small className="muted"> {t("courses.lessons.requires")} {l.requires.map((r) => title.get(r) ?? r).join(", ")}</small>
                )}
              </li>
            ))}
          </ol>
        </div>
      ))}
    </>
  );
}

function Exercises({ items }: { items: CourseExercise[] }) {
  if (items.length === 0) return <Empty>{t("courses.ex.empty")}</Empty>;
  return (
    <>
      {items.map((e) => (
        <details key={`${e.document_id}-${e.key}`}>
          <summary>
            <strong>{e.title}</strong> <small className="muted">{e.module_title}</small>{" "}
            <span className={`badge ${e.gradable ? "ok" : "warn"}`}>{e.gradable ? t("courses.ex.gradable") : t("courses.ex.not_gradable")}</span>{" "}
            <small className="muted">
              {t("courses.ex.tests", { mapped: e.mapped_count, total: e.test_count, language: e.language })}
              {e.has_solution ? "" : t("courses.ex.no_solution")}
            </small>
          </summary>
          <ul className="linklist">
            {e.tests.map((tc) => (
              <li key={tc.id}>
                <span className={`badge ${tc.mapped ? "ok" : "warn"}`}>{tc.mapped ? t("courses.ex.mapped") : t("courses.ex.unmapped")}</span> {tc.description}
                {tc.unmapped_reason && <small className="muted"> — {tc.unmapped_reason}</small>}
              </li>
            ))}
          </ul>
        </details>
      ))}
    </>
  );
}
