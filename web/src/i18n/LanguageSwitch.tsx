import { getLocale, LOCALES, setLocale, t, type Locale } from "./index";

const NAMES: Record<Locale, string> = { en: "English", pt: "Português" };

export function LanguageSwitch() {
  return (
    <label className="lang-switch">
      <span className="sr-only">{t("common.language")}</span>
      <select value={getLocale()} onChange={(e) => setLocale(e.target.value as Locale)}>
        {LOCALES.map((l) => (
          <option key={l} value={l}>
            {NAMES[l]}
          </option>
        ))}
      </select>
    </label>
  );
}
