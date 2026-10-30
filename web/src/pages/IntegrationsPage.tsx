import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap } from "../api/client";
import type { IntegrationAudit, IntegrationField, IntegrationTest, IntegrationView } from "../api/types";
import { useAuth } from "../auth";
import { Empty, ErrorBox, Loading, PageHead } from "../components/ui";
import { t } from "../i18n";
import { formatDateTime } from "../lib/format";

function sources(): Record<string, { label: string; tone: string }> {
  return {
    vault: { label: t("integrations.source.vault"), tone: "ok" },
    env: { label: t("integrations.source.env"), tone: "primary" },
    default: { label: t("integrations.source.default"), tone: "" },
    none: { label: "–", tone: "" },
  };
}

function actions(): Record<string, string> {
  return {
    set: t("integrations.action.set"),
    rotate: t("integrations.action.rotate"),
    generate: t("integrations.action.generate"),
    delete: t("integrations.action.delete"),
    import: t("integrations.action.import"),
    rekey: t("integrations.action.rekey"),
    test: t("integrations.action.test"),
  };
}

export function IntegrationsPage() {
  const { isAdmin } = useAuth();
  if (!isAdmin) {
    return (
      <>
        <PageHead title={t("integrations.title")} subtitle={t("integrations.subtitle.member")} />
        <section className="card">
          <p className="muted">{t("integrations.adminOnly")}</p>
        </section>
      </>
    );
  }
  return <Admin />;
}

function Admin() {
  const list = useQuery({ queryKey: ["integrations"], queryFn: () => unwrap(api.GET("/api/integrations")) });
  const vault = list.data?.vault;
  return (
    <>
      <PageHead
        title={t("integrations.title")}
        subtitle={t("integrations.subtitle.admin")}
      />
      <ErrorBox error={list.error} />
      {list.isLoading && <Loading />}
      {vault && <VaultNotice vault={vault} />}
      <div className="stack">
        {list.data?.integrations.map((integration) => (
          <IntegrationCard key={integration.id} integration={integration} vaultEnabled={vault?.enabled ?? false} />
        ))}
      </div>
      <AuditCard />
    </>
  );
}

function VaultNotice({ vault }: { vault: { enabled: boolean; has_master_key: boolean; entries: number; problem?: string | null } }) {
  if (vault.enabled && !vault.problem) {
    return (
      <div className="alert ok" role="status" style={{ marginBottom: "1rem" }}>
        {t("integrations.vault.active", { count: vault.entries })}
      </div>
    );
  }
  return (
    <div className="alert warn stack" role="alert" style={{ marginBottom: "1rem" }}>
      <strong>{t("integrations.vault.broken")}</strong>
      <span>{vault.problem ?? t("integrations.vault.noKey")}</span>
      <small>
        {t("integrations.vault.hintA")} <code>ariadne secrets generate-key</code>
        {t("integrations.vault.hintB")} <code>ARIADNE_MASTER_KEY</code> {t("integrations.vault.hintC")}
      </small>
    </div>
  );
}

function IntegrationCard({ integration, vaultEnabled }: { integration: IntegrationView; vaultEnabled: boolean }) {
  const queryClient = useQueryClient();
  const [generated, setGenerated] = useState<{ key: string; label: string; value: string } | null>(null);
  const [tested, setTested] = useState<IntegrationTest | null>(null);
  const test = useMutation({
    mutationFn: () => unwrap(api.POST("/api/integrations/{integration_id}/test", { params: { path: { integration_id: integration.id } } })),
    onSuccess: (result) => {
      setTested(result);
      queryClient.invalidateQueries({ queryKey: ["integrations-audit"] });
    },
  });
  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2 style={{ marginBottom: 0 }}>
            {integration.label}{" "}
            <span className={`badge ${integration.configured ? "ok" : ""}`}>{integration.configured ? t("integrations.configured") : t("integrations.notConfigured")}</span>
          </h2>
          <p className="muted" style={{ margin: "0.25rem 0 0" }}>
            {integration.description}
          </p>
        </div>
        <button className="small" disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? t("integrations.testing") : t("integrations.test")}
        </button>
      </div>
      <ErrorBox error={test.error} />
      {tested && (
        <div className={`alert ${tested.ok ? "ok" : "error"}`} role="status" style={{ margin: "0.75rem 0" }}>
          {tested.ok ? t("integrations.testOk") : t("integrations.testFailed")}{" "}
          {tested.message}
        </div>
      )}
      {generated && (
        <div className="alert ok stack" role="status" style={{ margin: "0.75rem 0" }}>
          <strong>
            {t("integrations.generated", { label: generated.label })}
          </strong>
          <div className="secret">{generated.value}</div>
          <div className="row">
            <button className="small" onClick={() => navigator.clipboard?.writeText(generated.value)}>
              {t("integrations.copy")}
            </button>
            <button className="small" onClick={() => setGenerated(null)}>
              {t("integrations.saved")}
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t("integrations.col.field")}</th>
              <th>{t("integrations.col.state")}</th>
              <th>{t("integrations.col.origin")}</th>
              <th>{t("integrations.col.value")}</th>
              <th>{t("integrations.col.updated")}</th>
              <th aria-label={t("integrations.col.actions")} />
            </tr>
          </thead>
          <tbody>
            {integration.fields.map((field) => (
              <FieldRow
                key={field.key}
                field={field}
                vaultEnabled={vaultEnabled}
                onGenerated={(value) => setGenerated({ key: field.key, label: field.label, value })}
              />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function FieldRow({ field, vaultEnabled, onGenerated }: { field: IntegrationField; vaultEnabled: boolean; onGenerated: (value: string) => void }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["integrations"] });
    queryClient.invalidateQueries({ queryKey: ["integrations-audit"] });
  };
  const path = { params: { path: { key: field.key } } };
  const save = useMutation({
    mutationFn: (rotate: boolean) =>
      rotate
        ? unwrap(api.POST("/api/integrations/fields/{key}/rotate", { ...path, body: { value } }))
        : unwrap(api.PUT("/api/integrations/fields/{key}", { ...path, body: { value } })),
    onSuccess: () => {
      setEditing(false);
      setValue("");
      refresh();
    },
  });
  const generate = useMutation({
    mutationFn: (rotate: boolean) =>
      rotate
        ? unwrap(api.POST("/api/integrations/fields/{key}/rotate", { ...path, body: {} }))
        : unwrap(api.POST("/api/integrations/fields/{key}/generate", path)),
    onSuccess: (data) => {
      if (data && "value" in data) onGenerated(data.value);
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/integrations/fields/{key}", path)),
    onSuccess: refresh,
  });
  const source = sources()[field.source] ?? sources().none;
  const inVault = field.source === "vault";
  const busy = save.isPending || generate.isPending || remove.isPending;

  return (
    <>
      <tr>
        <td>
          {field.label}
          <br />
          <small className="muted">
            <code>{field.env}</code>
          </small>
          {field.shared_with && (
            <>
              <br />
              <small className="muted">{t("integrations.sharedWith", { name: field.shared_with })}</small>
            </>
          )}
        </td>
        <td>
          <span className={`badge ${field.defined ? "ok" : "warn"}`}>{field.defined ? t("integrations.defined") : t("integrations.undefined")}</span>
        </td>
        <td>
          <span className={`badge ${source.tone}`}>{source.label}</span>
        </td>
        <td>
          {field.secret ? (
            field.defined ? (
              <code aria-label={t("integrations.secretSet")}>{field.hint ? `••••${field.hint}` : "••••"}</code>
            ) : (
              "–"
            )
          ) : (
            <span style={{ overflowWrap: "anywhere" }}>{field.value || "–"}</span>
          )}
        </td>
        <td>
          {inVault ? (
            <small>
              {formatDateTime(field.updated_at)}
              {field.updated_by ? ` · ${field.updated_by}` : ""}
            </small>
          ) : (
            "–"
          )}
        </td>
        <td>
          <div className="row">
            <button className="small" disabled={!vaultEnabled || busy} onClick={() => setEditing((v) => !v)}>
              {editing ? t("integrations.cancel") : field.defined && field.secret ? t("integrations.replace") : t("integrations.edit")}
            </button>
            {field.generable && (
              <button
                className="small"
                disabled={!vaultEnabled || busy}
                onClick={() => {
                  if (!field.defined || window.confirm(t("integrations.confirmGenerate", { label: field.label, other: field.shared_with || t("integrations.theService") }))) {
                    generate.mutate(field.defined);
                  }
                }}
              >
                {field.defined ? t("integrations.rotate") : t("integrations.generate")}
              </button>
            )}
            {inVault && (
              <button
                className="small danger"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(t("integrations.confirmDelete", { label: field.label }))) remove.mutate();
                }}
              >
                {t("integrations.delete")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {(editing || save.error || generate.error || remove.error) && (
        <tr>
          <td colSpan={6}>
            <ErrorBox error={save.error ?? generate.error ?? remove.error} />
            {editing && (
              <form
                className="field-row"
                onSubmit={(e) => {
                  e.preventDefault();
                  save.mutate(field.secret && field.defined);
                }}
              >
                <label className="grow">
                  {field.secret ? t("integrations.newValue") : t("integrations.value")}
                  <input
                    type={field.secret ? "password" : "text"}
                    autoComplete="off"
                    spellCheck={false}
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    aria-label={t("integrations.valueOf", { label: field.label })}
                    placeholder={field.help || undefined}
                  />
                </label>
                <button className="primary" type="submit" disabled={!value.trim() || save.isPending}>
                  {t("integrations.saveToVault")}
                </button>
              </form>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function AuditCard() {
  const audit = useQuery({ queryKey: ["integrations-audit"], queryFn: () => unwrap(api.GET("/api/integrations/audit", { params: { query: { limit: 50 } } })) });
  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>{t("integrations.audit.title")}</h2>
      <p className="muted">{t("integrations.audit.subtitle")}</p>
      <ErrorBox error={audit.error} />
      {audit.isLoading && <Loading />}
      {audit.data && audit.data.length === 0 && <Empty>{t("integrations.audit.empty")}</Empty>}
      {audit.data && audit.data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("integrations.audit.when")}</th>
                <th>{t("integrations.audit.who")}</th>
                <th>{t("integrations.audit.what")}</th>
                <th>{t("integrations.col.field")}</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.map((entry: IntegrationAudit) => (
                <tr key={entry.id}>
                  <td>
                    <small>{formatDateTime(entry.at)}</small>
                  </td>
                  <td>{entry.actor}</td>
                  <td>
                    {actions()[entry.action] ?? entry.action}
                    {entry.detail ? <small className="muted"> · {entry.detail}</small> : null}
                  </td>
                  <td>{entry.key ? <code>{entry.key.toUpperCase()}</code> : (entry.integration ?? "–")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
