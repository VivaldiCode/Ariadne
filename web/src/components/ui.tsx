import type { ReactNode } from "react";

import { t } from "../i18n";
import { formatPercent, LEVEL_LABELS } from "../lib/format";

export function Loading({ label }: { label?: string }) {
  return (
    <p className="muted" role="status">
      {label ?? t("common.loading")}
    </p>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="alert error" role="alert">
      {message}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHead({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export function LevelBadge({ level }: { level: number }) {
  return <span className={`badge level-${level}`}>{LEVEL_LABELS[level] ?? t("common.levelFallback", { n: level })}</span>;
}

export function Kpi({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="card kpi">
      <span className="value">{value}</span>
      <span className="label">{label}</span>
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function Progress({ fraction, tone }: { fraction: number | null; tone?: "warn" | "danger" }) {
  const value = fraction == null ? 0 : Math.max(0, Math.min(1, fraction));
  return (
    <div
      className={`progress ${tone ?? ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
      aria-label={formatPercent(fraction)}
    >
      <span style={{ width: `${value * 100}%` }} />
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button key={tab.id} role="tab" aria-selected={tab.id === value} onClick={() => onChange(tab.id)}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}
