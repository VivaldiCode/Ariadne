import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { setLocale } from "../i18n";

import { CoursesPage } from "../pages/CoursesPage";
import { ReaderPage } from "../pages/ReaderPage";
import { ME, jsonError, mockApi, renderApp } from "./utils";

const LESSON_ID = "course:curso-teste/bases/somar";

function version(overrides: Record<string, unknown> = {}) {
  return {
    version: "2026-10",
    current: true,
    source_version: "2026-10",
    lessons: 3,
    exercises: 1,
    tests: 4,
    unmapped_tests: 1,
    git_commit: "0123456789abcdef",
    imported_at: "2026-10-24T10:00:00Z",
    publish_status: "unpublished",
    platform_url: null,
    published_at: null,
    publish_error: null,
    held: 0,
    ...overrides,
  };
}

const COURSE = {
  id: "curso-teste",
  title: "Curso de teste",
  language: "pt",
  programming_language: "javascript",
  level: null,
  summary: "",
  origin_source_id: "zim:freecodecamp_en_teste",
  license_name: "CC BY-SA 4.0",
  license_class: "open",
  attribution: "freeCodeCamp",
  current_version: "2026-10",
  versions: [version(), version({ version: "2026-08", current: false, lessons: 2, publish_status: "published", platform_url: "http://moodle/course/view.php?id=3", published_at: "2026-10-20T10:00:00Z", unmapped_tests: 0 })],
};

const DETAIL = {
  ...COURSE,
  lessons: [
    { document_id: "course:curso-teste/bases/variaveis", module_id: "bases", module_title: "Bases", lesson_id: "variaveis", position: 1, title: "Variáveis", requires: [], exercises: 0, platform_kind: "page", platform_url: "http://moodle/mod/page/view.php?id=2", held_reason: null },
    { document_id: LESSON_ID, module_id: "bases", module_title: "Bases", lesson_id: "somar", position: 2, title: "Somar", requires: ["course:curso-teste/bases/variaveis"], exercises: 1, platform_kind: "quiz", platform_url: "http://moodle/mod/quiz/view.php?id=3", held_reason: null },
    { document_id: "course:curso-teste/bases/perigo", module_id: "bases", module_title: "Bases", lesson_id: "perigo", position: 3, title: "Perigosa", requires: [], exercises: 0, platform_kind: null, platform_url: null, held_reason: "à espera do curador" },
  ],
  commits: [{ sha: "abcdef0123456789", date: "2026-10-24T10:00:00Z", subject: "Importa Curso de teste 2026-10" }],
};

const EXERCISES = [
  {
    document_id: LESSON_ID,
    key: "somar-ex",
    title: "Somar dois números",
    language: "javascript",
    module_title: "Bases",
    lesson_title: "Somar",
    gradable: true,
    has_solution: true,
    test_count: 2,
    mapped_count: 1,
    platform_question_id: 20,
    platform_url: null,
    tests: [
      { id: "t1", description: "soma é uma função", mapped: true, unmapped_reason: null },
      { id: "t2", description: "usa o DOM", mapped: false, unmapped_reason: "usa o browser (document)" },
    ],
  },
];

describe("CoursesPage", () => {
  afterEach(() => setLocale("en"));

  it("em português mostra o texto português", async () => {
    setLocale("pt");
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": [] });
    renderApp(<CoursesPage />, "/courses");

    expect(await screen.findByText(/Ainda não há cursos/)).toBeInTheDocument();
  });

  it("lista os cursos com as versões e o estado da publicação no Moodle", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": [COURSE] });
    renderApp(<CoursesPage />, "/courses");

    expect(await screen.findByText("Curso de teste")).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    const current = rows.find((r) => within(r).queryByText("current"))!;
    expect(within(current).getByText("unpublished")).toBeInTheDocument();
    expect(within(current).getByText("(1 unmapped)")).toBeInTheDocument();
    const old = rows.find((r) => within(r).queryByText("2026-08"))!;
    expect(within(old).getByText("published")).toBeInTheDocument();
    expect(within(old).getByRole("link", { name: "open" })).toHaveAttribute("href", "http://moodle/course/view.php?id=3");
    expect(within(old).getByRole("button", { name: "Republish" })).toBeInTheDocument();
  });

  it("o administrador pede a publicação de uma versão", async () => {
    const user = userEvent.setup();
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/courses": [COURSE],
      "POST /api/courses/curso-teste/publish": { course: "curso-teste", version: "2026-10", job_id: 5 },
    });
    renderApp(<CoursesPage />, "/courses");

    await user.click(await screen.findByRole("button", { name: "Publish to Moodle" }));

    expect(await screen.findByText(/Publish requested/)).toBeInTheDocument();
    const post = mock.calls.find((c) => c.method === "POST")!;
    expect(post.search).toBe("?version=2026-10");
  });

  it("um membro vê os cursos mas não pode publicar", async () => {
    mockApi({ "GET /api/whoami": { ...ME, role: "member" }, "GET /api/courses": [COURSE] });
    renderApp(<CoursesPage />, "/courses");

    await screen.findByText("Curso de teste");
    expect(screen.queryByRole("button", { name: /Publish|Republish/ })).not.toBeInTheDocument();
  });

  it("mostra as lições com os pré-requisitos, o Moodle e as retidas pela curadoria", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": [COURSE], "GET /api/courses/curso-teste": DETAIL });
    renderApp(<CoursesPage />, "/courses?course=curso-teste");

    expect(await screen.findByRole("link", { name: "Somar" })).toHaveAttribute("href", `/read?id=${encodeURIComponent(LESSON_ID)}`);
    expect(screen.getByText("requires Variáveis")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Moodle · quiz" })).toHaveAttribute("href", "http://moodle/mod/quiz/view.php?id=3");
    expect(screen.getByTitle("à espera do curador")).toHaveTextContent("held");
  });

  it("mostra os exercícios e os testes extraídos, com o motivo dos que não mapeiam", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/courses": [COURSE],
      "GET /api/courses/curso-teste": DETAIL,
      "GET /api/courses/curso-teste/exercises": EXERCISES,
    });
    renderApp(<CoursesPage />, "/courses?course=curso-teste");

    await user.click(await screen.findByRole("tab", { name: "Exercises and tests" }));

    expect(await screen.findByText("Somar dois números")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 tests · javascript")).toBeInTheDocument();
    expect(screen.getByText("unmapped")).toBeInTheDocument();
    expect(screen.getByText(/usa o browser \(document\)/)).toBeInTheDocument();
  });

  it("mostra o histórico do Git", async () => {
    const user = userEvent.setup();
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": [COURSE], "GET /api/courses/curso-teste": DETAIL });
    renderApp(<CoursesPage />, "/courses?course=curso-teste");

    await user.click(await screen.findByRole("tab", { name: "History (Git)" }));

    expect(await screen.findByText("Importa Curso de teste 2026-10")).toBeInTheDocument();
    expect(screen.getByText("abcdef0123")).toBeInTheDocument();
  });

  it("sem cursos explica como os criar", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": [] });
    renderApp(<CoursesPage />, "/courses");

    expect(await screen.findByText(/No courses yet/)).toBeInTheDocument();
  });

  it("um erro do servidor aparece", async () => {
    mockApi({ "GET /api/whoami": ME, "GET /api/courses": () => jsonError(500, "base de dados em baixo") });
    renderApp(<CoursesPage />, "/courses");

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("base de dados em baixo"));
  });
});

describe("ReaderPage numa lição", () => {
  it("mostra o curso, os pré-requisitos e os exercícios", async () => {
    mockApi({
      "GET /api/whoami": ME,
      [`GET /api/documents/${encodeURIComponent(LESSON_ID)}`]: {
        document_id: LESSON_ID,
        title: "Somar",
        source_id: "course:curso-teste",
        language: "pt",
        version: "2026-10",
        wing: "course-curso-teste",
        room: null,
        section: null,
        text: "Escreve soma.",
        truncated: false,
        total_chars: 13,
        sections: [],
        links: [],
        flags: [],
        content_type: "lesson",
        lesson: {
          course_id: "curso-teste",
          course_title: "Curso de teste",
          course_version: "2026-10",
          module_id: "bases",
          module_title: "Bases",
          position: 2,
          requires: [{ document_id: "course:curso-teste/bases/variaveis", title: "Variáveis" }],
          required_by: [{ document_id: "course:curso-teste/funcoes/repetir", title: "Repetir" }],
          exercises: [{ id: "somar-ex", title: "Somar", language: "javascript", tests: 3, mapped_tests: 3, gradable: true, has_solution: true }],
          license_class: "open",
          license_name: "CC BY-SA 4.0",
          attribution: "freeCodeCamp",
          moodle_url: "http://moodle/mod/quiz/view.php?id=3",
        },
      },
      "GET /api/neighbors": { document_id: LESSON_ID, title: "Somar", neighbors: [], flags: [] },
      "GET /api/translations": { document_id: LESSON_ID, title: "Somar", concept: null, is_original: false, translations: [], flags: [] },
    });
    renderApp(<ReaderPage />, `/read?id=${encodeURIComponent(LESSON_ID)}`);

    const panel = await screen.findByRole("region", { name: "A lesson from a course" });
    expect(within(panel).getByRole("link", { name: "Curso de teste" })).toHaveAttribute("href", "/courses?course=curso-teste");
    expect(within(panel).getByRole("link", { name: "Variáveis" })).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Repetir" })).toBeInTheDocument();
    expect(within(panel).getByText(/3 of 3 tests run in CodeRunner/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Open in Moodle" })).toHaveAttribute("href", "http://moodle/mod/quiz/view.php?id=3");
  });
});
