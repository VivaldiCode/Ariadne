import { Fragment, useEffect, useState, type ReactNode } from "react";

import { en, pt, type MessageKey } from "./messages";

export type Locale = "en" | "pt";
export const LOCALES: Locale[] = ["en", "pt"];
export const DEFAULT_LOCALE: Locale = "en";
const STORAGE_KEY = "ariadne.lang";

const CATALOGS: Record<Locale, Record<string, string>> = { en, pt };
/** O locale do `Intl` para números, datas e plurais. */
const INTL: Record<Locale, string> = { en: "en", pt: "pt-PT" };

type PluralSuffix = "zero" | "one" | "two" | "few" | "many" | "other";
type BaseKey<K extends string> = K extends `${infer B}_${PluralSuffix}` ? B : K;
/** Chave aceite por `t()`: as do catálogo e, para as que têm plural (`x_one`, `x_other`), a base `x`. */
export type TKey = BaseKey<MessageKey>;
export type Params = Record<string, string | number>;

function readStored(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "en" || stored === "pt") return stored;
  } catch {
    /* sem armazenamento: fica o idioma por omissão */
  }
  return DEFAULT_LOCALE;
}

let current: Locale = readStored();
const listeners = new Set<() => void>();

export function getLocale(): Locale {
  return current;
}

/** O locale do `Intl` do idioma ativo («en», «pt-PT»). */
export function intlLocale(): string {
  return INTL[current];
}

export function setLocale(locale: Locale): void {
  if (locale === current) return;
  current = locale;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    /* idem */
  }
  if (typeof document !== "undefined") document.documentElement.lang = locale;
  listeners.forEach((fn) => fn());
}

function lookup(key: string, params?: Params): string {
  const catalog = CATALOGS[current];
  let message: string | undefined;
  if (typeof params?.count === "number") {
    const category = new Intl.PluralRules(INTL[current]).select(params.count);
    message = catalog[`${key}_${category}`] ?? catalog[`${key}_other`];
  }
  message ??= catalog[key];
  // Uma chave que não existe mostra-se a si própria: salta à vista em vez de rebentar.
  if (message === undefined) return key;
  return params ? message.replace(/\{(\w+)\}/g, (whole, name: string) => (name in params ? String(params[name]) : whole)) : message;
}

/** Traduz. `{nome}` no texto é substituído por `params.nome`; com `params.count` escolhe a forma do plural
 *  (`chave_one` / `chave_other`). Chama-se sempre dentro de funções e componentes, nunca ao carregar o módulo
 *  (senão o texto fica preso ao idioma de então). */
export function t(key: TKey, params?: Params): string {
  return lookup(key, params);
}

/** Como `t()`, mas aceita uma chave só conhecida em tempo de execução (rótulos vindos de um dicionário, por exemplo). */
export function tDynamic(key: string, params?: Params): string | undefined {
  const catalog = CATALOGS[current];
  return key in catalog || `${key}_other` in catalog ? lookup(key, params) : undefined;
}

/** Remonta a árvore quando o idioma muda, para os textos já desenhados se refazerem. */
export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(current);
  useEffect(() => {
    document.documentElement.lang = current;
    const sync = () => setLocaleState(current);
    listeners.add(sync);
    return () => {
      listeners.delete(sync);
    };
  }, []);
  return <Fragment key={locale}>{children}</Fragment>;
}
