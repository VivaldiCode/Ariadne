import { lazy } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { t } from "./i18n";
import { LoginPage } from "./pages/LoginPage";
import { SearchPage } from "./pages/SearchPage";

// O Cytoscape e os gráficos só se descarregam quando a página é aberta.
const BooksPage = lazy(() => import("./pages/BooksPage").then((m) => ({ default: m.BooksPage })));
const CoursesPage = lazy(() => import("./pages/CoursesPage").then((m) => ({ default: m.CoursesPage })));
const CurationPage = lazy(() => import("./pages/CurationPage").then((m) => ({ default: m.CurationPage })));
const GraphPage = lazy(() => import("./pages/GraphPage").then((m) => ({ default: m.GraphPage })));
const IntegrationsPage = lazy(() => import("./pages/IntegrationsPage").then((m) => ({ default: m.IntegrationsPage })));
const LicensesPage = lazy(() => import("./pages/LicensesPage").then((m) => ({ default: m.LicensesPage })));
const LinksPage = lazy(() => import("./pages/LinksPage").then((m) => ({ default: m.LinksPage })));
const OperationsPage = lazy(() => import("./pages/OperationsPage").then((m) => ({ default: m.OperationsPage })));
const ReaderPage = lazy(() => import("./pages/ReaderPage").then((m) => ({ default: m.ReaderPage })));
const SourcesPage = lazy(() => import("./pages/SourcesPage").then((m) => ({ default: m.SourcesPage })));
const StatsPage = lazy(() => import("./pages/StatsPage").then((m) => ({ default: m.StatsPage })));

export function App() {
  const { me } = useAuth();
  if (!me) return <LoginPage />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<SearchPage />} />
        <Route path="read" element={<ReaderPage />} />
        <Route path="graph" element={<GraphPage />} />
        <Route path="stats" element={<StatsPage />} />
        <Route path="sources" element={<SourcesPage />} />
        <Route path="courses" element={<CoursesPage />} />
        <Route path="books" element={<BooksPage />} />
        <Route path="curation" element={<CurationPage />} />
        <Route path="links" element={<LinksPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="licenses" element={<LicensesPage />} />
        <Route path="operations" element={<OperationsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
      <Route path="/login" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function NotFound() {
  return (
    <div className="card">
      <h1>{t("common.notFound.title")}</h1>
      <p>
        <NavLink to="/">{t("common.notFound.back")}</NavLink>
      </p>
    </div>
  );
}
