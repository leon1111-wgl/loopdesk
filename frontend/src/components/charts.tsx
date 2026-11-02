import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";

// Lightweight SVG charts for the dashboard (no chart library). Series colours come from the
// --chart-* tokens, validated as a pair for colour-vision deficiency in both themes.

function useWidth<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

const niceMax = (v: number) => {
  if (v <= 4) return 4;
  const step = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / step) * step;
};

/** The second series is hatched as well as coloured, so it does not depend on hue alone. */
const swatch = (color: string, k: number) =>
  k === 1 ? { background: `repeating-linear-gradient(135deg, ${color} 0 2px, color-mix(in srgb, ${color} 45%, transparent) 2px 4px)` } : { background: color };

export interface Series {
  key: string;
  label: string;
  color: string; // a CSS colour, normally var(--chart-n)
}

/** Grouped columns per day, one axis, a hover tooltip per day and a table view for screen readers
 * and anyone who wants the numbers. */
export function ColumnChart({ rows, series, label, height = 220 }: {
  rows: { label: string; values: Record<string, number>; title: string }[];
  series: Series[];
  label: string;
  height?: number;
}) {
  const [box, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const hatchId = `hatch${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const max = niceMax(Math.max(1, ...rows.flatMap((r) => series.map((s) => r.values[s.key] ?? 0))));
  const pad = { top: 16, right: 8, bottom: 28, left: 32 };
  const plotW = Math.max(0, width - pad.left - pad.right);
  const plotH = height - pad.top - pad.bottom;
  const slot = rows.length ? plotW / rows.length : 0;
  const barW = Math.max(4, Math.min(16, (slot - 12) / series.length - 2));
  const groupW = series.length * barW + (series.length - 1) * 2;
  const y = (v: number) => pad.top + plotH - (v / max) * plotH;
  const ticks = [0, max / 4, max / 2, (3 * max) / 4, max];
  return (
    <div className="column-chart">
      <div className="chart-toolbar">
        <div className="chart-legend" aria-hidden="true">
          {series.map((s, k) => <span key={s.key}><i style={swatch(s.color, k)} />{s.label}</span>)}
        </div>
        <button type="button" className="btn btn-text btn-sm" aria-pressed={table} onClick={() => setTable((v) => !v)}>
          {table ? "查看图表" : "查看数据表"}
        </button>
      </div>
      {table ? (
        <div className="table-wrap">
          <table className="table chart-table">
            <caption className="sr-only">{label}</caption>
            <thead><tr><th>日期</th>{series.map((s) => <th key={s.key} className="num">{s.label}</th>)}</tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label}><td>{r.label}</td>{series.map((s) => <td key={s.key} className="num">{r.values[s.key] ?? 0}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={box} className="trend-chart" style={{ height }} onMouseLeave={() => setHover(null)}>
          {width > 0 && (
            <svg width={width} height={height} role="img" aria-label={`${label}：${rows.map((r) => r.title).join("；")}`}>
              {series[1] && (
                <defs>
                  <pattern id={hatchId} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                    <rect width="4" height="4" fill={series[1].color} opacity="0.45" />
                    <rect width="2" height="4" fill={series[1].color} />
                  </pattern>
                </defs>
              )}
              {ticks.map((t) => (
                <g key={t}>
                  <line x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} stroke="var(--chart-grid)" strokeWidth={1} />
                  <text x={pad.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="axis-label">{Math.round(t)}</text>
                </g>
              ))}
              {rows.map((r, i) => {
                const x0 = pad.left + i * slot + (slot - groupW) / 2;
                return (
                  <g key={r.label}>
                    {hover === i && <rect x={pad.left + i * slot + 2} y={pad.top} width={slot - 4} height={plotH} rx={4} fill="var(--hover)" />}
                    {series.map((s, k) => {
                      const v = r.values[s.key] ?? 0;
                      const h = Math.max(v > 0 ? 2 : 0, (v / max) * plotH);
                      const x = x0 + k * (barW + 2);
                      const top = pad.top + plotH - h;
                      // Rounded data end, square at the baseline.
                      const rr = Math.min(4, h, barW / 2);
                      return h > 0 ? (
                        <path key={s.key} fill={k === 1 ? `url(#${hatchId})` : s.color}
                          d={`M${x},${pad.top + plotH} V${top + rr} Q${x},${top} ${x + rr},${top} H${x + barW - rr} Q${x + barW},${top} ${x + barW},${top + rr} V${pad.top + plotH} Z`} />
                      ) : null;
                    })}
                    {i === rows.length - 1 && series.map((s, k) => (
                      <text key={s.key} x={x0 + k * (barW + 2) + barW / 2} y={y(r.values[s.key] ?? 0) - 5} textAnchor="middle" className="value-label">
                        {r.values[s.key] ?? 0}
                      </text>
                    ))}
                    <text x={pad.left + i * slot + slot / 2} y={height - 8} textAnchor="middle" className="x-label">{r.label}</text>
                    <rect x={pad.left + i * slot} y={pad.top} width={slot} height={plotH + pad.bottom} fill="transparent"
                      onMouseEnter={() => setHover(i)} />
                  </g>
                );
              })}
              <line x1={pad.left} x2={width - pad.right} y1={pad.top + plotH} y2={pad.top + plotH} stroke="var(--border)" strokeWidth={1} />
            </svg>
          )}
          {hover !== null && rows[hover] && (
            <div className="chart-tip" role="presentation"
              style={{ left: Math.min(Math.max(pad.left + hover * slot + slot / 2, 80), width - 80), top: 4 }}>
              <b>{rows[hover].label}</b>
              {series.map((s) => (
                <div key={s.key} className="row"><i style={swatch(s.color, series.indexOf(s))} />{s.label}<span className="spacer" /><b>{rows[hover].values[s.key] ?? 0}</b></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One horizontal bar per category, longest first; a single series needs no legend. */
export function BarList({ data, labels, empty }: { data: Record<string, number>; labels?: Record<string, string>; empty: ReactNode }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  const total = entries.reduce((n, [, v]) => n + v, 0);
  if (!entries.length) return <>{empty}</>;
  return (
    <ul className="bar-list">
      {entries.map(([k, v]) => (
        <li key={k} title={`${labels?.[k] ?? k}：${v}（${Math.round((v / Math.max(1, total)) * 100)}%）`}>
          <div className="bar-list-row">
            <span className="ellipsis">{labels?.[k] ?? k}</span>
            <span className="bar-list-value">{v}<span className="faint"> · {Math.round((v / Math.max(1, total)) * 100)}%</span></span>
          </div>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / max) * 100}%` }} /></div>
        </li>
      ))}
    </ul>
  );
}

/** A 2px line of daily values for a KPI card. */
export function Sparkline({ values, label }: { values: number[]; label: string }) {
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const w = 120;
  const h = 32;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * (w - 4) + 2},${h - 3 - (v / max) * (h - 6)}`).join(" ");
  return (
    <svg className="sparkline" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
      <polyline points={points} fill="none" stroke="var(--chart-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
