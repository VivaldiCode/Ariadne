import { useState, type FormEvent } from "react";

import { errorMessage } from "../api/client";
import { useAuth } from "../auth";
import { ErrorBox } from "../components/ui";
import { t } from "../i18n";
import { LanguageSwitch } from "../i18n/LanguageSwitch";

export function LoginPage() {
  const { login } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(key);
    } catch (err) {
      const status = (err as { status?: number }).status;
      setError(status === 401 ? new Error(t("login.invalid")) : new Error(errorMessage(err)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <h1>Ariadne</h1>
      <p className="muted">{t("login.intro")}</p>
      <form className="card stack" onSubmit={submit}>
        <label>
          {t("login.keyLabel")}
          <input
            type="password"
            autoComplete="off"
            autoFocus
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="ariadne_…"
          />
        </label>
        <ErrorBox error={error} />
        <button className="primary" type="submit" disabled={busy || key.trim() === ""}>
          {busy ? t("login.checking") : t("login.submit")}
        </button>
        <small className="muted">
          {t("login.noKey")} <code>ariadne principal create --kind user --role admin --name …</code>
        </small>
      </form>
      <LanguageSwitch />
    </div>
  );
}
