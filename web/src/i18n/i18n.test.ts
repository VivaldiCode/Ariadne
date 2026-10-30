import { describe, expect, it } from "vitest";

import { en, pt } from "./messages";
import { getLocale, setLocale, t, tDynamic, type TKey } from "./index";

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe("catálogos", () => {
  it("o português tem exatamente as chaves do inglês, com os mesmos {marcadores}", () => {
    const enKeys = Object.keys(en).sort();
    expect(Object.keys(pt).sort()).toEqual(enKeys);
    for (const key of enKeys) {
      expect(placeholders((pt as Record<string, string>)[key]), key).toEqual(placeholders((en as Record<string, string>)[key]));
    }
  });

  it("nenhuma mensagem está vazia", () => {
    for (const [key, text] of Object.entries(en)) expect(text.trim(), key).not.toBe("");
    for (const [key, text] of Object.entries(pt)) expect(text.trim(), key).not.toBe("");
  });
});

describe("t()", () => {
  it("o idioma por omissão é o inglês e a escolha guarda-se", () => {
    expect(getLocale()).toBe("en");
    setLocale("pt");
    expect(getLocale()).toBe("pt");
    expect(localStorage.getItem("ariadne.lang")).toBe("pt");
    expect(document.documentElement.lang).toBe("pt");
  });

  it("uma chave desconhecida mostra-se a si própria e um marcador em falta fica como está", () => {
    expect(t("nao.existe" as TKey)).toBe("nao.existe");
    expect(tDynamic("nao.existe")).toBeUndefined();
  });
});
