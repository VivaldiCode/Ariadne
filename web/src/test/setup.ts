import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

import { setLocale } from "../i18n";

// Os testes correm sempre em inglês (o idioma por omissão), mesmo que um teste tenha trocado de idioma.
beforeEach(() => {
  localStorage.clear();
  setLocale("en");
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});
