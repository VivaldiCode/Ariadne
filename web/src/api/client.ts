import createClient, { type Middleware } from "openapi-fetch";

import { t } from "../i18n";
import type { paths } from "./schema";

const STORAGE_KEY = "ariadne.apiKey";

/** A chave de API vive só em `sessionStorage`: fecha-se o separador e perde-se. */
export function getKey(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setKey(key: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, key);
  } catch {
    /* sem armazenamento: a sessão dura enquanto a página não recarregar */
  }
}

export function clearKey(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* idem */
  }
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** Substitui a chave só para este pedido (o ecrã de entrada valida a chave antes de a guardar). */
let overrideKey: string | null = null;

const auth: Middleware = {
  onRequest({ request }) {
    const key = overrideKey ?? getKey();
    if (key) request.headers.set("Authorization", `Bearer ${key}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401 && overrideKey === null) onUnauthorized?.();
    return response;
  },
};

export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  // O `fetch` global resolve-se a cada pedido (e não quando o cliente nasce): os testes trocam-no.
  fetch: (request) => globalThis.fetch(request),
});
api.use(auth);

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function detailOf(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : ""))
        .filter(Boolean)
        .join("; ");
    }
  }
  return t("common.requestRefused");
}

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Desembrulha a resposta do openapi-fetch: devolve os dados ou lança `ApiError` com o `detail`. */
export async function unwrap<T>(pending: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await pending;
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? t("common.noConnection", { message: err.message }) : t("common.noConnectionShort"));
  }
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(result.response.status, detailOf(result.error));
  }
  return result.data as T;
}

/** Valida uma chave (GET /api/whoami) sem a guardar. */
export async function whoamiWith(key: string) {
  overrideKey = key;
  try {
    return await unwrap(api.GET("/api/whoami"));
  } finally {
    overrideKey = null;
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : t("common.unknownError");
}

/** Pedido à mão (ficheiros): leva a chave como o cliente tipado. Para o que o `openapi-fetch` não faz
 *  (descarregar o zip de um lote, carregar o ficheiro de resultados em bruto). */
export async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const key = getKey();
  if (key) headers.set("Authorization", `Bearer ${key}`);
  let response: Response;
  try {
    response = await globalThis.fetch(new URL(path, window.location.origin), { ...init, headers });
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? t("common.noConnection", { message: err.message }) : t("common.noConnectionShort"));
  }
  if (response.status === 401) onUnauthorized?.();
  if (!response.ok) {
    let detail = t("common.requestRefused");
    try {
      detail = detailOf(await response.json());
    } catch {
      /* sem corpo JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response;
}
