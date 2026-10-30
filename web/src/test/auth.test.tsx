import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import { AuthProvider } from "../auth";
import { I18nProvider } from "../i18n";
import { ME, mockApi } from "./utils";

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter>
          <I18nProvider>
            <App />
          </I18nProvider>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("entrada com a chave de API", () => {
  it("sem chave mostra o ecrã de entrada e guarda a chave só em sessionStorage", async () => {
    const mock = mockApi({ "GET /api/whoami": ME, "GET /api/sources": [] });
    mount();
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText("API key"), "ariadne_boa");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("navigation", { name: "Modules" })).toBeInTheDocument();
    expect(sessionStorage.getItem("ariadne.apiKey")).toBe("ariadne_boa");
    expect(localStorage.getItem("ariadne.apiKey")).toBeNull();
    expect(mock.calls[0].path).toBe("/api/whoami");
  });

  it("recusa chaves inválidas e chaves de agentes", async () => {
    mockApi({
      "GET /api/whoami": ({ request }) =>
        request.headers.get("authorization") === "Bearer agente"
          ? { id: 2, kind: "agent", name: "bot", role: "member" }
          : new Response(JSON.stringify({ detail: "Chave de API inválida" }), { status: 401, headers: { "content-type": "application/json" } }),
    });
    mount();
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText("API key"), "errada");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid API key.");

    await user.clear(screen.getByLabelText("API key"));
    await user.type(screen.getByLabelText("API key"), "agente");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("user key");
    expect(sessionStorage.getItem("ariadne.apiKey")).toBeNull();
  });

  it("a navegação já não tem módulos por fazer: o catálogo do Kiwix está nas Fontes", async () => {
    sessionStorage.setItem("ariadne.apiKey", "ariadne_boa");
    mockApi({ "GET /api/whoami": ME, "GET /api/sources": [] });
    mount();

    const nav = await screen.findByRole("navigation", { name: "Modules" });
    // Fase 12: o catálogo do Kiwix deixou de ser «em breve» e vive no módulo Fontes e catálogo.
    expect(nav.querySelectorAll("[aria-disabled='true']")).toHaveLength(0);
    expect(screen.queryByText("coming soon")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sources and catalog" })).toHaveAttribute("href", "/sources");
    // Os Livros e as Licenças (Fase 11) já estão ativos, como os Cursos (Fase 10): ligações.
    expect(screen.getByRole("link", { name: "Books" })).toHaveAttribute("href", "/books");
    expect(screen.getByRole("link", { name: "Licenses" })).toHaveAttribute("href", "/licenses");
    // Os Cursos (Fase 10) já estão ativos: é uma ligação, não um módulo desativado.
    expect(screen.getByRole("link", { name: "Courses" })).toHaveAttribute("href", "/courses");
    expect(Array.from(nav.querySelectorAll("[aria-disabled='true']")).some((e) => e.textContent?.startsWith("Courses"))).toBe(false);
  });
});

describe("idioma", () => {
  it("o ecrã de entrada abre em inglês e a escolha do idioma troca os textos e fica guardada", async () => {
    mockApi({ "GET /api/whoami": ME });
    mount();
    const user = userEvent.setup();

    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Language" }), "pt");
    expect(await screen.findByLabelText("Chave de API")).toBeInTheDocument();
    expect(localStorage.getItem("ariadne.lang")).toBe("pt");
    expect(document.documentElement.lang).toBe("pt");
  });
});
