import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { AuthProvider } from "../auth";

type Handler = (call: { url: URL; body: unknown; request: Request }) => unknown;

export interface Mock {
  calls: { method: string; path: string; search: string; body: unknown }[];
}

/** Troca o `fetch` por um servidor de brincar. As chaves são `"GET /api/search"`; o resultado é o
 *  JSON da resposta (ou um `Response` já feito). Um pedido sem rota falha o teste. */
export function mockApi(routes: Record<string, Handler | object>): Mock {
  const mock: Mock = { calls: [] };
  vi.stubGlobal("fetch", async (input: Request | string) => {
    const request = typeof input === "string" ? new Request(new URL(input, window.location.origin)) : input;
    const url = new URL(request.url);
    const method = request.method;
    const raw = method === "GET" || method === "HEAD" ? "" : await request.clone().text();
    const body = raw ? JSON.parse(raw) : undefined;
    mock.calls.push({ method, path: url.pathname, search: url.search, body });
    const route = routes[`${method} ${url.pathname}`];
    if (route === undefined) throw new Error(`Pedido sem rota no teste: ${method} ${url.pathname}${url.search}`);
    const result = typeof route === "function" ? (route as Handler)({ url, body, request }) : route;
    if (result instanceof Response) return result;
    return new Response(JSON.stringify(result), { status: 200, headers: { "content-type": "application/json" } });
  });
  return mock;
}

export function jsonError(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } });
}

export const ME = { id: 1, kind: "user", name: "pessoa", role: "admin" };

/** Renderiza dentro dos providers da app, já com sessão iniciada (a chave vai para sessionStorage). */
export function renderApp(ui: ReactElement, route = "/") {
  sessionStorage.setItem("ariadne.apiKey", "ariadne_teste");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
