import { t } from "../i18n";
import { formatDay, formatMs } from "../lib/format";

interface DayPoint {
  day: string;
  queries: number;
  level1: number;
  level2: number;
  level3: number;
  empty: number;
  p50_ms: number | null;
  p95_ms: number | null;
}

const W = 720;
const H = 220;
const PAD = { top: 12, right: 12, bottom: 28, left: 40 };

/** As séries empilhadas; os rótulos calculam-se à chamada para seguirem o idioma ativo. */
function seriesList() {
  return [
    { key: "level3", label: t("stats.chart.level3"), color: "var(--level-3)" },
    { key: "level2", label: t("stats.chart.level2"), color: "var(--level-2)" },
    { key: "level1", label: t("stats.chart.level1"), color: "var(--level-1)" },
    { key: "empty", label: t("stats.chart.empty"), color: "var(--level-empty)" },
  ] as const;
}

function niceMax(value: number): number {
  if (value <= 5) return 5;
  const pow = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / pow) * pow;
}

function labelEvery(count: number): number {
  return Math.max(1, Math.ceil(count / 8));
}

/** Consultas por dia, empilhadas pelo nível que respondeu. */
export function QueriesChart({ series }: { series: DayPoint[] }) {
  const SERIES = seriesList();
  const max = niceMax(Math.max(0, ...series.map((d) => d.queries)));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const band = innerW / Math.max(series.length, 1);
  const barW = Math.max(2, band * 0.7);
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const every = labelEvery(series.length);
  return (
    <figure style={{ margin: 0 }}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t("stats.chart.aria_queries")}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(max * f)} y2={y(max * f)} stroke="var(--line)" />
            <text x={PAD.left - 6} y={y(max * f) + 4} textAnchor="end">
              {Math.round(max * f)}
            </text>
          </g>
        ))}
        {series.map((d, i) => {
          let acc = 0;
          const x = PAD.left + i * band + (band - barW) / 2;
          return (
            <g key={d.day}>
              <title>{t("stats.chart.bar_title", { day: formatDay(d.day), queries: d.queries, l3: d.level3, l2: d.level2, l1: d.level1, empty: d.empty })}</title>
              {SERIES.map((s) => {
                const v = d[s.key];
                if (!v) return null;
                const rect = <rect key={s.key} x={x} width={barW} y={y(acc + v)} height={y(acc) - y(acc + v)} fill={s.color} />;
                acc += v;
                return rect;
              })}
              {i % every === 0 && (
                <text x={x + barW / 2} y={H - 8} textAnchor="middle">
                  {formatDay(d.day)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="legend">
        {SERIES.map((s) => (
          <span key={s.key}>
            <i style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </figure>
  );
}

/** Latência p50 e p95 por dia (os dias sem consultas ficam em branco). */
export function LatencyChart({ series }: { series: DayPoint[] }) {
  const max = niceMax(Math.max(0, ...series.map((d) => d.p95_ms ?? 0)));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (series.length <= 1 ? innerW / 2 : (i / (series.length - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const every = labelEvery(series.length);

  function path(pick: (d: DayPoint) => number | null): string {
    let out = "";
    let pen = false;
    series.forEach((d, i) => {
      const v = pick(d);
      if (v == null) {
        pen = false;
        return;
      }
      out += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    });
    return out;
  }

  return (
    <figure style={{ margin: 0 }}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t("stats.chart.aria_latency")}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(max * f)} y2={y(max * f)} stroke="var(--line)" />
            <text x={PAD.left - 6} y={y(max * f) + 4} textAnchor="end">
              {formatMs(max * f)}
            </text>
          </g>
        ))}
        <path d={path((d) => d.p95_ms)} fill="none" stroke="var(--level-2)" strokeWidth="2" />
        <path d={path((d) => d.p50_ms)} fill="none" stroke="var(--level-3)" strokeWidth="2" />
        {series.map((d, i) => (
          <g key={d.day}>
            {d.p95_ms != null && <circle cx={x(i)} cy={y(d.p95_ms)} r="3" fill="var(--level-2)"><title>{`${formatDay(d.day)}: p95 ${formatMs(d.p95_ms)}`}</title></circle>}
            {d.p50_ms != null && <circle cx={x(i)} cy={y(d.p50_ms)} r="3" fill="var(--level-3)"><title>{`${formatDay(d.day)}: p50 ${formatMs(d.p50_ms)}`}</title></circle>}
            {i % every === 0 && (
              <text x={x(i)} y={H - 8} textAnchor="middle">
                {formatDay(d.day)}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="legend">
        <span>
          <i style={{ background: "var(--level-3)" }} />
          p50
        </span>
        <span>
          <i style={{ background: "var(--level-2)" }} />
          p95
        </span>
      </div>
    </figure>
  );
}
