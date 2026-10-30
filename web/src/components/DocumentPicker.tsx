import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, unwrap } from "../api/client";
import { t } from "../i18n";
import { plainText, sourceName } from "../lib/format";

/** Escolhe um documento pesquisando por título (ou aceita um `document_id` escrito à mão). */
export function DocumentPicker({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (documentId: string) => void;
}) {
  const [text, setText] = useState(value);
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => setText(value), [value]);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(text.trim()), 300);
    return () => clearTimeout(id);
  }, [text]);

  const looksLikeId = debounced.includes("/");
  const hits = useQuery({
    queryKey: ["picker", debounced],
    enabled: open && debounced.length >= 2 && !looksLikeId,
    queryFn: () => unwrap(api.GET("/api/search", { params: { query: { q: debounced, k: 8 } } })),
  });

  return (
    <label className="picker">
      {label}
      <input
        type="text"
        value={text}
        placeholder={t("search.picker.placeholder")}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          if (e.target.value.includes("/")) onChange(e.target.value.trim());
        }}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onFocus={() => setOpen(true)}
      />
      {open && hits.data && hits.data.results.length > 0 && (
        <ul role="listbox">
          {hits.data.results.map((hit) => (
            <li key={hit.document_id}>
              <button
                type="button"
                role="option"
                aria-selected={hit.document_id === value}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onChange(hit.document_id);
                  setText(hit.document_id);
                  setOpen(false);
                }}
              >
                <strong>{hit.title}</strong>
                <small>
                  {sourceName(hit.source_id)} · {plainText(hit.snippet).slice(0, 80)}
                </small>
              </button>
            </li>
          ))}
        </ul>
      )}
    </label>
  );
}
