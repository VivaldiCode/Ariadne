import { useQuery } from "@tanstack/react-query";
import cytoscape from "cytoscape";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, unwrap } from "../api/client";
import type { NeighborsResult } from "../api/types";
import { DocumentPicker } from "../components/DocumentPicker";
import { Empty, ErrorBox, Loading, PageHead } from "../components/ui";
import { t } from "../i18n";
import { sourceName } from "../lib/format";

const KINDS = ["zim_internal", "title_match", "wikidata", "vector", "langlink", "manual", "requires", "semantic"] as const;
function kindLabels(): Record<string, string> {
  return {
    zim_internal: t("graph.kind.zim_internal"),
    title_match: t("graph.kind.title_match"),
    wikidata: "Wikidata",
    vector: t("graph.kind.vector"),
    langlink: t("graph.kind.langlink"),
    manual: t("graph.kind.manual"),
    requires: t("graph.kind.requires"),
    semantic: t("graph.kind.semantic"),
  };
}
/** Cores da paleta do DS, lidas dos tokens: o DS tem um só acento (o fio), por isso as fontes distinguem-se
 *  pela escala de neutros e pelo fio; o nome da fonte está sempre no nó. */
const PALETTE_TOKENS = ["--thread", "--bone", "--stone", "--thread-text", "--body", "--wall", "--thread-edge-strong", "--muted"];

export function GraphPage() {
  const [params, setParams] = useSearchParams();
  const id = params.get("id") ?? "";
  const [kinds, setKinds] = useState<string[]>([...KINDS]);
  const [k, setK] = useState(40);

  const query = useQuery({
    queryKey: ["neighbors", id, kinds.join(","), k],
    enabled: !!id && kinds.length > 0,
    queryFn: () =>
      unwrap(api.GET("/api/neighbors", { params: { query: { document_id: id, kinds: kinds.length === KINDS.length ? undefined : kinds, k } } })),
  });

  const toggle = (kind: string) => setKinds((cur) => (cur.includes(kind) ? cur.filter((x) => x !== kind) : [...cur, kind]));

  return (
    <>
      <PageHead title={t("graph.title")} subtitle={t("graph.subtitle")} />
      <div className="card stack">
        <div className="field-row">
          <div className="grow">
            <DocumentPicker label={t("graph.article")} value={id} onChange={(doc) => setParams({ id: doc })} />
          </div>
          <label>
            {t("graph.maxNeighbors")}
            <input type="number" min={5} max={100} value={k} onChange={(e) => setK(Math.max(5, Math.min(100, Number(e.target.value) || 40)))} style={{ width: "6rem" }} />
          </label>
        </div>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="muted">{t("graph.linkTypes")}</legend>
          <div className="row">
            {KINDS.map((kind) => (
              <label key={kind} className="inline">
                <input type="checkbox" checked={kinds.includes(kind)} onChange={() => toggle(kind)} />
                {kindLabels()[kind]}
              </label>
            ))}
          </div>
        </fieldset>
      </div>

      <div style={{ marginTop: "1rem" }}>
        {!id && <Empty>{t("graph.pick")}</Empty>}
        {kinds.length === 0 && <Empty>{t("graph.pickKind")}</Empty>}
        <ErrorBox error={query.error} />
        {query.isLoading && <Loading />}
        {query.data && <GraphView data={query.data} onCenter={(doc) => setParams({ id: doc })} />}
      </div>
    </>
  );
}

function GraphView({ data, onCenter }: { data: NeighborsResult; onCenter: (id: string) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!container.current) return;
    const styles = getComputedStyle(document.documentElement);
    const token = (name: string) => styles.getPropertyValue(name).trim();
    const text = token("--bone");
    const muted = token("--stone");
    const surface = token("--ink-2");
    const thread = token("--thread");
    const palette = PALETTE_TOKENS.map(token);
    const sources = [...new Set([data.document_id.split("/")[0], ...data.neighbors.map((n) => n.source_id)])];
    const color = (source: string) => palette[sources.indexOf(source) % palette.length];

    const elements: cytoscape.ElementDefinition[] = [
      { data: { id: data.document_id, label: data.title, color: color(data.document_id.split("/")[0]), center: 1 } },
    ];
    const seen = new Set([data.document_id]);
    data.neighbors.forEach((n, i) => {
      if (!seen.has(n.document_id)) {
        seen.add(n.document_id);
        elements.push({ data: { id: n.document_id, label: n.title, color: color(n.source_id) } });
      }
      // «A requer B» e «A cita B» têm sentido: a seta vai de quem requer/cita para o outro.
      const reversed = n.direction === "in";
      elements.push({
        data: {
          id: `e${i}`,
          source: reversed ? n.document_id : data.document_id,
          target: reversed ? data.document_id : n.document_id,
          kind: n.kind,
          status: n.status,
          directed: n.direction !== "both" ? 1 : 0,
          label: kindLabels()[n.kind] ?? n.kind,
        },
      });
    });

    const cy = cytoscape({
      container: container.current,
      elements,
      layout: { name: "concentric", animate: false, concentric: (node) => (node.data("center") ? 2 : 1), levelWidth: () => 1, minNodeSpacing: 28, padding: 24 },
      style: [
        { selector: "node", style: { label: "data(label)", "font-family": token("--font-text"), "background-color": "data(color)", color: text, "font-size": 11, "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "ellipsis", "text-max-width": "110px", width: 22, height: 22, "text-background-color": surface, "text-background-opacity": 0.8, "text-background-padding": "2px" } },
        { selector: "node[center]", style: { width: 36, height: 36, "border-width": 3, "border-color": text, "font-weight": "bold" } },
        { selector: "edge", style: { width: 2, "line-color": muted, opacity: 0.8, "curve-style": "bezier" } },
        { selector: "edge[status = 'proposed']", style: { "line-style": "dashed", "line-color": thread } },
        { selector: "edge[status = 'computed']", style: { "line-style": "dotted" } },
        { selector: "edge[kind = 'manual']", style: { "line-color": text, width: 3 } },
        { selector: "edge[directed = 1]", style: { "target-arrow-shape": "triangle", "target-arrow-color": muted } },
        { selector: "edge[kind = 'requires']", style: { "line-color": token("--body"), "target-arrow-color": token("--body") } },
        { selector: "node:selected", style: { "border-width": 3, "border-color": thread } },
      ],
      minZoom: 0.3,
      maxZoom: 3,
    });
    cy.on("tap", "node", (event) => navigate(`/read?id=${encodeURIComponent(event.target.id())}`));
    cy.on("mouseover", "node", () => container.current && (container.current.style.cursor = "pointer"));
    cy.on("mouseout", "node", () => container.current && (container.current.style.cursor = "default"));
    return () => cy.destroy();
  }, [data, navigate]);

  const kindsPresent = [...new Set(data.neighbors.map((n) => n.kind))];

  return (
    <div className="grid split" style={{ gridTemplateColumns: "minmax(0,1fr) 320px" }}>
      <div>
        <div ref={container} className="cy" role="img" aria-label={t("graph.ariaLabel", { title: data.title })} />
        <p className="muted" style={{ marginTop: "0.5rem" }}>
          {t("graph.legend")}
          {kindsPresent.length === 0 && ` ${t("graph.noNeighbors")}`}
        </p>
      </div>
      <aside className="card sidepanel">
        <h3>{data.title}</h3>
        <p>
          <Link to={`/read?id=${encodeURIComponent(data.document_id)}`}>{t("graph.openInReader")}</Link>
        </p>
        <h3>{t("graph.neighbors", { count: data.neighbors.length })}</h3>
        <ul className="linklist">
          {data.neighbors.map((n, i) => (
            <li key={`${n.document_id}-${n.kind}-${i}`}>
              <button className="link" onClick={() => onCenter(n.document_id)} title={t("graph.centerHere")}>
                {n.title}
              </button>
              <span className="badge">{kindLabels()[n.kind] ?? n.kind}</span>
              <small>{sourceName(n.source_id)}</small>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
