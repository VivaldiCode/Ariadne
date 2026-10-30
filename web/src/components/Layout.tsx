import { Suspense, useEffect, useRef } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth";
import { t } from "../i18n";
import { LanguageSwitch } from "../i18n/LanguageSwitch";

interface Module {
  to: string;
  label: string;
  end?: boolean;
}

function modules(): Module[] {
  return [
    { to: "/", label: t("common.nav.reading"), end: true },
    { to: "/graph", label: t("common.nav.graph") },
    { to: "/stats", label: t("common.nav.stats") },
    { to: "/sources", label: t("common.nav.sources") },
    { to: "/courses", label: t("common.nav.courses") },
    { to: "/books", label: t("common.nav.books") },
    { to: "/curation", label: t("common.nav.curation") },
    { to: "/links", label: t("common.nav.links") },
    { to: "/licenses", label: t("common.nav.licenses") },
    { to: "/operations", label: t("common.nav.operations") },
  ];
}
/** Segredos e integrações: só os administradores a veem. */
function adminModules(): Module[] {
  return [{ to: "/integrations", label: t("common.nav.integrations") }];
}

/** No telemóvel as tabelas largas viram cartões (regra do DS): cada célula leva o nome da sua coluna em
 *  `data-label`, que o CSS mostra à esquerda do valor. Só apresentação; não toca nos dados. */
function useTableLabels(root: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const label = () => {
      el.querySelectorAll("table").forEach((table) => {
        const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent?.trim() ?? "");
        table.querySelectorAll("tbody tr").forEach((row) => {
          [...row.children].forEach((cell, i) => {
            if (cell.tagName === "TD" && heads[i] !== undefined && cell.getAttribute("data-label") !== heads[i]) {
              cell.setAttribute("data-label", heads[i]);
            }
          });
        });
      });
    };
    label();
    const observer = new MutationObserver(label);
    observer.observe(el, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [root]);
}

export function Layout() {
  const { me, logout } = useAuth();
  const main = useRef<HTMLElement>(null);
  useTableLabels(main);
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect x="0.5" y="0.5" width="31" height="31" fill="var(--ink-2)" stroke="var(--wall)" />
            <path d="M8 22c4-1 5-6 8-6s3 5 8 4M8 11c3 0 4 3 8 3s5-3 8-3" fill="none" stroke="var(--thread)" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
          Ariadne
        </div>
        <nav className="nav" aria-label={t("common.nav.label")}>
          {[...modules(), ...(me?.role === "admin" ? adminModules() : [])].map((m) => (
            <NavLink key={m.to} to={m.to} end={m.end}>
              {m.label}
            </NavLink>
          ))}
        </nav>
        <div className="spacer" />
        <LanguageSwitch />
        <div className="userbox">
          <div>
            <strong>{me?.name}</strong>
            <br />
            <small>{me?.role === "admin" ? t("common.role.admin") : t("common.role.user")}</small>
          </div>
          <button className="small" onClick={logout}>
            {t("common.signOut")}
          </button>
        </div>
      </aside>
      <main className="main" ref={main}>
        <Suspense fallback={<p className="muted">{t("common.loading")}</p>}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
