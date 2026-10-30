import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setLocale } from "../i18n";
import { IntegrationsPage } from "../pages/IntegrationsPage";
import { jsonError, ME, mockApi, renderApp } from "./utils";

const FIELD = {
  env: "CALIBRE_API_KEY",
  kind: "secret",
  secret: true,
  help: "",
  generable: true,
  shared_with: "NAS (calibre.env)",
  defined: true,
  source: "vault",
  hint: "9abc",
  value: null,
  updated_at: "2026-10-25T10:00:00Z",
  updated_by: "alice",
};

function overview(patch: Record<string, unknown> = {}, vault: Record<string, unknown> = {}) {
  return {
    vault: { enabled: true, has_master_key: true, entries: 1, problem: null, ...vault },
    integrations: [
      {
        id: "calibre",
        label: "Calibre",
        description: "Gestão da biblioteca.",
        configured: true,
        fields: [
          { ...FIELD, key: "calibre_api_key", label: "Chave de API", ...patch },
          { ...FIELD, key: "calibre_url", env: "CALIBRE_URL", label: "URL do agente", kind: "url", secret: false, generable: false, shared_with: "", hint: "", value: "http://calibre-externo:8807", source: "env", updated_at: null, updated_by: null },
        ],
      },
    ],
  };
}

const AUDIT = [{ id: 1, at: "2026-10-25T10:00:00Z", actor: "alice", action: "rotate", key: "calibre_api_key", integration: "calibre", detail: null }];

describe("Integrations", () => {
  it("um membro não vê os segredos nem faz pedidos", async () => {
    const mock = mockApi({ "GET /api/whoami": { ...ME, role: "member" } });
    renderApp(<IntegrationsPage />);
    expect(await screen.findByText(/Only administrators/)).toBeInTheDocument();
    expect(mock.calls.some((c) => c.path.startsWith("/api/integrations"))).toBe(false);
  });

  it("lista o estado, a origem e só o fim do segredo; o URL mostra-se", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview(),
      "GET /api/integrations/audit": AUDIT,
    });
    renderApp(<IntegrationsPage />);
    expect(await screen.findByText("Calibre")).toBeInTheDocument();
    expect(await screen.findByLabelText("Secret set")).toHaveTextContent("••••9abc");
    expect(screen.getByText("http://calibre-externo:8807")).toBeInTheDocument();
    expect(screen.getByText(/Vault active/)).toBeInTheDocument();
    expect(await screen.findByText("rotated")).toBeInTheDocument();
  });

  it("avisa quando o cofre não está ativo e bloqueia a edição", async () => {
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview({}, { enabled: false, has_master_key: false, problem: "Falta a chave mestra do cofre: define ARIADNE_MASTER_KEY" }),
      "GET /api/integrations/audit": [],
    });
    renderApp(<IntegrationsPage />);
    expect(await screen.findByText(/The vault is not working/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Replace|Edit/ })[0]).toBeDisabled();
  });

  it("guarda um segredo novo sem o repetir no ecrã", async () => {
    const user = userEvent.setup();
    const mock = mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview(),
      "GET /api/integrations/audit": AUDIT,
      "POST /api/integrations/fields/calibre_api_key/rotate": () => null,
    });
    renderApp(<IntegrationsPage />);
    await user.click((await screen.findAllByRole("button", { name: "Replace" }))[0]);
    const input = screen.getByLabelText("Value of Chave de API");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "valor-novo-secreto");
    await user.click(screen.getByRole("button", { name: "Save to vault" }));
    await waitFor(() => expect(screen.queryByLabelText("Value of Chave de API")).not.toBeInTheDocument());
    const sent = mock.calls.find((c) => c.method === "POST" && c.path.endsWith("/rotate"));
    expect(sent?.body).toEqual({ value: "valor-novo-secreto" });
    expect(document.body.textContent).not.toContain("valor-novo-secreto");
  });

  it("gera um segredo nosso e mostra-o uma única vez", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview(),
      "GET /api/integrations/audit": AUDIT,
      "POST /api/integrations/fields/calibre_api_key/rotate": { key: "calibre_api_key", value: "abcdef0123456789", shown_once: true },
    });
    renderApp(<IntegrationsPage />);
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(await screen.findByText("abcdef0123456789")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "I've saved it" }));
    expect(screen.queryByText("abcdef0123456789")).not.toBeInTheDocument();
  });

  it("testa a ligação e mostra o resultado", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview(),
      "GET /api/integrations/audit": AUDIT,
      "POST /api/integrations/calibre/test": { integration: "calibre", ok: false, message: "o serviço respondeu, mas recusou a chave (HTTP 401)" },
    });
    renderApp(<IntegrationsPage />);
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    const notice = await screen.findByText(/recusou a chave/);
    expect(notice.closest(".alert")).toHaveClass("error");
  });

  it("mostra o erro do servidor ao guardar", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /api/whoami": ME,
      "GET /api/integrations": overview(),
      "GET /api/integrations/audit": AUDIT,
      "PUT /api/integrations/fields/calibre_url": () => jsonError(422, "O URL tem de começar por http:// ou https://."),
    });
    renderApp(<IntegrationsPage />);
    const row = (await screen.findByText("URL do agente")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Value of URL do agente"), "mac.local");
    await user.click(screen.getByRole("button", { name: "Save to vault" }));
    expect(await screen.findByText(/tem de começar por http/)).toBeInTheDocument();
  });

  it("mostra a interface em português com o idioma pt", async () => {
    setLocale("pt");
    try {
      mockApi({ "GET /api/whoami": { ...ME, role: "member" } });
      renderApp(<IntegrationsPage />);
      expect(await screen.findByText(/Só os administradores/)).toBeInTheDocument();
    } finally {
      setLocale("en");
    }
  });
});
