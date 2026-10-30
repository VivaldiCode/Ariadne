// Os catálogos por área: cada ficheiro em en/ e pt/ exporta um objeto plano `{ "area.chave": "texto" }`.
// Os dois idiomas têm de ter exatamente as mesmas chaves (o tipo e o teste i18n.test.ts garantem-no).
import { books as enBooks } from "./en/books";
import { common as enCommon } from "./en/common";
import { compute as enCompute } from "./en/compute";
import { courses as enCourses } from "./en/courses";
import { curation as enCuration } from "./en/curation";
import { graph as enGraph } from "./en/graph";
import { integrations as enIntegrations } from "./en/integrations";
import { licenses as enLicenses } from "./en/licenses";
import { links as enLinks } from "./en/links";
import { login as enLogin } from "./en/login";
import { operations as enOperations } from "./en/operations";
import { reader as enReader } from "./en/reader";
import { search as enSearch } from "./en/search";
import { sources as enSources } from "./en/sources";
import { stats as enStats } from "./en/stats";
import { books as ptBooks } from "./pt/books";
import { common as ptCommon } from "./pt/common";
import { compute as ptCompute } from "./pt/compute";
import { courses as ptCourses } from "./pt/courses";
import { curation as ptCuration } from "./pt/curation";
import { graph as ptGraph } from "./pt/graph";
import { integrations as ptIntegrations } from "./pt/integrations";
import { licenses as ptLicenses } from "./pt/licenses";
import { links as ptLinks } from "./pt/links";
import { login as ptLogin } from "./pt/login";
import { operations as ptOperations } from "./pt/operations";
import { reader as ptReader } from "./pt/reader";
import { search as ptSearch } from "./pt/search";
import { sources as ptSources } from "./pt/sources";
import { stats as ptStats } from "./pt/stats";

export const en = {
  ...enCommon, ...enLogin, ...enSearch, ...enReader, ...enGraph, ...enLinks, ...enSources, ...enCourses,
  ...enBooks, ...enCuration, ...enCompute, ...enOperations, ...enIntegrations, ...enStats, ...enLicenses,
};

export type MessageKey = keyof typeof en;

/** O português tem de ter as mesmas chaves que o inglês (o inglês é a referência). */
export const pt: Record<MessageKey, string> = {
  ...ptCommon, ...ptLogin, ...ptSearch, ...ptReader, ...ptGraph, ...ptLinks, ...ptSources, ...ptCourses,
  ...ptBooks, ...ptCuration, ...ptCompute, ...ptOperations, ...ptIntegrations, ...ptStats, ...ptLicenses,
};
