import { intlLocale, t, tDynamic } from "../i18n";

export function formatBytes(n: number | null | undefined): string {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function formatInt(n: number | null | undefined): string {
  return n == null ? "–" : new Intl.NumberFormat(intlLocale()).format(n);
}

export function formatPercent(fraction: number | null | undefined, digits = 0): string {
  return fraction == null ? "–" : `${(fraction * 100).toFixed(digits)}%`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "–";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "–";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(intlLocale(), { dateStyle: "short", timeStyle: "short" });
}

export function formatDay(value: string): string {
  const d = new Date(`${value}T00:00:00`);
  return d.toLocaleDateString(intlLocale(), { day: "2-digit", month: "2-digit" });
}

/** O snippet do kiwix pode trazer marcação: mostra-se sempre como texto, nunca como HTML. */
export function plainText(snippet: string): string {
  return snippet
    .replace(/<[^>]*>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/(^|\s)#{1,6}\s/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export function sourceName(sourceId: string): string {
  return sourceId.replace(/^(zim|course|book):/, "");
}

/** Dicionário de rótulos traduzidos: `LABELS[valor]` lê do catálogo no idioma ativo (e dá `undefined` se o valor não existe,
 *  para o `?? valor` dos chamadores continuar a funcionar). Calcula-se a cada leitura, por isso segue a troca de idioma. */
function labels(prefix: string): Record<string, string> {
  return new Proxy({} as Record<string, string>, {
    get: (_target, key) => (typeof key === "string" ? tDynamic(`${prefix}.${key}`) : undefined),
  });
}

export const LEVEL_LABELS = labels("common.level");
export const DECISION_LABELS = labels("common.decision");
export const CONTENT_LABELS = labels("common.content");
export const CLASS_LABELS = labels("common.class");
export const PUBLISH_LABELS = labels("common.publish");
export const LICENSE_LABELS = labels("common.license");
export const DOWNLOAD_PHASES = labels("common.download");
export const RUN_STATUS_LABELS = labels("common.run");
export const BACKEND_MODE_LABELS = labels("common.backend");

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "–";
  if (seconds < 90) return t("common.duration.seconds", { n: Math.round(seconds) });
  if (seconds < 5400) return t("common.duration.minutes", { n: Math.round(seconds / 60) });
  return t("common.duration.hours", { n: (seconds / 3600).toFixed(1) });
}

export function formatHours(hours: number | null | undefined): string {
  return hours == null ? "–" : t("common.duration.hours", { n: new Intl.NumberFormat(intlLocale(), { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(hours) });
}
