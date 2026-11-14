import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { beyond, cleanPoints, decimate, excess, extent, formatTicks, formatValue, nearestIndex, niceTicks, placeLabels, splitAtLimit, type Pt } from "./chartMath";

export type ChartSeries = { id: string; label: string; unit?: string; points: Pt[]; kind?: "line" | "step" };
export type ChartLimit = { y: number; label: string; kind: "max" | "min" };
export type ChartBand = { from: number; to: number; label: string };
export type ChartProps = {
  series: ChartSeries[];
  xLabel: string;
  yLabel: string;
  xUnit?: string;
  limits?: ChartLimit[];
  bands?: ChartBand[];
  height?: number;
  title: string;
  /** Only hides the "different units" note. */
  allowMixedUnits?: boolean;
};

// Colour is never the only cue: each series also has its own dash pattern.
const DASHES = ["", "7 3", "2 3", "9 3 2 3", "1 4", "12 4"];
const M = { l: 46, r: 12, t: 24, b: 38 };
const ROW_CAP = 200;

const P = (a: Pt[], i: number) => a[i] as Pt;

export function Chart({ series: all, xLabel, yLabel, xUnit, limits = [], bands = [], height = 220, title, allowMixedUnits = false }: ChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(520);
  const [cursor, setCursor] = useState<number | null>(null);
  const series = all.slice(0, 6);

  useEffect(() => {
    const el = host.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => el.clientWidth > 0 && setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const cleaned = useMemo(() => all.slice(0, 6).map((s) => cleanPoints(s.points)), [all]);
  const dropped = cleaned.reduce((n, c) => n + c.dropped, 0);
  const note = dropped ? <p className="chart-note">{dropped} value{dropped === 1 ? "" : "s"} skipped (not a number).</p> : null;

  const total = cleaned.reduce((n, c) => n + c.points.length, 0);
  const longest = Math.max(0, ...cleaned.map((c) => c.points.length));
  if (longest < 2) {
    return (
      <div className="chart" ref={host} data-testid="chart">
        <p className="chart-empty">{total === 0 ? "Nothing to plot yet." : "Only one point so far, not enough to draw a line."}</p>
        {note}
      </div>
    );
  }

  const shown = cleaned.map((c) => decimate(c.points));
  const xs = [...new Set(shown.flatMap((p) => p.map((q) => q[0])))].sort((a, b) => a - b);
  const xe = extent(xs)!;
  const ye = extent(shown.flatMap((p) => p.map((q) => q[1])), limits.map((l) => l.y))!;
  const xt = niceTicks(xe[0], xe[1]);
  const yt = niceTicks(ye[0], ye[1]);
  const [x0, x1, y0, y1] = [xt[0], xt[xt.length - 1], yt[0], yt[yt.length - 1]] as [number, number, number, number];
  const pw = Math.max(40, width - M.l - M.r);
  const ph = Math.max(40, height - M.t - M.b);
  const sx = (x: number) => M.l + ((x - x0) / (x1 - x0)) * pw;
  const sy = (y: number) => M.t + ph - ((y - y0) / (y1 - y0)) * ph;
  const f = (n: number) => n.toFixed(1);

  const seg = (a: Pt, b: Pt, step: boolean) =>
    step ? `H${f(sx(b[0]))}V${f(sy(b[1]))}` : `L${f(sx(b[0]))} ${f(sy(b[1]))}`;
  const pathOf = (pts: Pt[], step: boolean) =>
    pts.map((p, i) => (i === 0 ? `M${f(sx(p[0]))} ${f(sy(p[1]))}` : seg(P(pts, i - 1), p, step))).join("");
  const overOf = (pts: Pt[], step: boolean) => {
    let d = "";
    for (let i = 1; i < pts.length; i++) {
      const [a, b] = [P(pts, i - 1), P(pts, i)];
      for (const l of limits) {
        // a step holds a's level across the segment; the riser is not coloured
        const s: [Pt, Pt] | null = step ? (beyond(l, a[1]) ? [a, [b[0], a[1]]] : null) : splitAtLimit(a, b, l);
        if (s) d += `M${f(sx(s[0][0]))} ${f(sy(s[0][1]))}${step ? `H${f(sx(s[1][0]))}` : `L${f(sx(s[1][0]))} ${f(sy(s[1][1]))}`}`;
      }
    }
    return d;
  };
  const xTickText = formatTicks(xt);
  const yTickText = formatTicks(yt);
  const limitY = placeLabels(limits.map((l) => sy(l.y) + (l.kind === "max" ? -4 : 12)), M.t + 10, M.t + ph - 3);
  const units = new Set(series.map((s) => s.unit ?? ""));
  const unitNote = units.size > 1 && !allowMixedUnits ? <p className="chart-note">These series use different units; compare shapes, not heights.</p> : null;
  const refUnit = series.find((s) => s.unit)?.unit;

  const cx = cursor === null ? null : (xs[Math.min(cursor, xs.length - 1)] as number);
  const at = (i: number) => {
    const pts = cleaned[i]!.points;
    return pts.length === 0 ? null : cx === null ? P(pts, pts.length - 1) : P(pts, nearestIndex(pts.map((p) => p[0]), cx));
  };

  const move = (i: number) => setCursor(Math.max(0, Math.min(xs.length - 1, i)));
  const onKey = (e: KeyboardEvent) => {
    const cur = cursor ?? xs.length - 1;
    const next = { ArrowLeft: cur - 1, ArrowRight: cur + 1, Home: 0, End: xs.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    move(next);
  };
  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (!r.width) return;
    const px = ((e.clientX - r.left) / r.width) * width;
    move(nearestIndex(xs, x0 + ((px - M.l) / pw) * (x1 - x0)));
  };

  const rows = xs.slice(0, ROW_CAP);
  const lookups = shown.map((p) => new Map(p));
  const xName = xUnit ? `${xLabel} (${xUnit})` : xLabel;

  return (
    <div className="chart" ref={host} data-testid="chart">
      <div className="chart-plot" role="img" aria-label={title} tabIndex={0} onKeyDown={onKey} onPointerMove={onMove}>
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
          {bands.map((b, i) => {
            const [a, z] = [Math.max(x0, Math.min(x1, b.from)), Math.max(x0, Math.min(x1, b.to))];
            if (!(z > a)) return null;
            return (
              <g key={`b${i}`} className="chart-band">
                <rect x={f(sx(a))} y={M.t} width={f(sx(z) - sx(a))} height={ph} />
                <text x={f(sx(a) + 4)} y={M.t + 11}>{b.label}</text>
              </g>
            );
          })}
          {yt.map((t, k) => (
            <g key={`y${t}`}>
              <line className="chart-grid" x1={M.l} x2={M.l + pw} y1={f(sy(t))} y2={f(sy(t))} />
              <text className="chart-tick" x={M.l - 6} y={sy(t) + 4} textAnchor="end">{yTickText[k]}</text>
            </g>
          ))}
          {xt.map((t, k) => (
            <text key={`x${t}`} className="chart-tick" x={f(sx(t))} y={M.t + ph + 14} textAnchor="middle">{xTickText[k]}</text>
          ))}
          <line className="chart-axis" x1={M.l} x2={M.l + pw} y1={M.t + ph} y2={M.t + ph} />
          <line className="chart-axis" x1={M.l} x2={M.l} y1={M.t} y2={M.t + ph} />
          <text className="chart-label" x={M.l} y={12}>{yLabel}</text>
          <text className="chart-label" x={M.l + pw / 2} y={height - 6} textAnchor="middle">{xName}</text>
          {shown.map((pts, i) => {
            const step = series[i]!.kind === "step";
            const over = overOf(pts, step);
            return (
              <g key={series[i]!.id} data-series={series[i]!.id}>
                <path className="chart-line" d={pathOf(pts, step)} stroke={`var(--chart-${i + 1})`} strokeDasharray={DASHES[i] || undefined} />
                {over && <path className="chart-over" d={over} />}
                {pts.filter((p) => limits.some((l) => beyond(l, p[1]))).map((p, k) => (
                  <circle key={k} className="chart-marker" cx={f(sx(p[0]))} cy={f(sy(p[1]))} r={3} />
                ))}
              </g>
            );
          })}
          {limits.map((l, i) => (
            <g key={`l${i}`} className="chart-limit">
              <line x1={M.l} x2={M.l + pw} y1={f(sy(l.y))} y2={f(sy(l.y))} />
              <text className="chart-limit-label" x={M.l + pw - 4} y={f(limitY[i] as number)} textAnchor="end">{l.label}</text>
            </g>
          ))}
          {cx !== null && <line className="chart-cursor" x1={f(sx(cx))} x2={f(sx(cx))} y1={M.t} y2={M.t + ph} />}
        </svg>
      </div>
      <div className="chart-readout" role="status" aria-live="polite">
        {cx === null ? "\u00a0" : `At ${formatValue(cx, xUnit)}: ${series.map((s, i) => `${s.label} ${at(i) ? formatValue((at(i) as Pt)[1], s.unit) : "no data"}`).join(", ")}`}
      </div>
      <ul className="chart-legend">
        {series.map((s, i) => {
          const p = at(i);
          const marks = limits.flatMap((l) => {
            const worst = cleaned[i]!.points.reduce((m, q) => Math.max(m, excess(l, q[1])), 0);
            return worst > 0 ? [`${l.kind === "max" ? "over" : "under"} ${l.kind} '${l.label}' by ${formatValue(worst, s.unit)}`] : [];
          });
          return (
            <li key={s.id}>
              <svg className="chart-swatch" width="22" height="8" aria-hidden="true">
                <line x1="0" x2="22" y1="4" y2="4" stroke={`var(--chart-${i + 1})`} strokeWidth="2" strokeDasharray={DASHES[i] || undefined} />
              </svg>
              <span className="chart-name">{s.label}</span>
              <span className="chart-value">{p ? formatValue(p[1], s.unit) : "no data"}</span>
              {marks.map((m) => <span key={m} className="chart-flag">{m}</span>)}
            </li>
          );
        })}
      </ul>
      {note}
      {unitNote}
      <table className="sr-only" aria-label={`${title}, data`}>
        <caption>{title}</caption>
        <thead>
          <tr><th>{xName}</th>{series.map((s) => <th key={s.id}>{s.unit ? `${s.label} (${s.unit})` : s.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x}><td>{formatValue(x)}</td>{lookups.map((m, i) => <td key={series[i]!.id}>{m.has(x) ? formatValue(m.get(x)!) : ""}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {(limits.length > 0 || bands.length > 0) && (
        <ul className="sr-only" aria-label={`${title}, limits and bands`}>
          {limits.map((l, i) => <li key={`l${i}`}>{`${l.label}: ${l.kind} limit ${formatValue(l.y, refUnit)}`}</li>)}
          {bands.map((b, i) => <li key={`b${i}`}>{`${b.label}: from ${formatValue(b.from, xUnit)} to ${formatValue(b.to, xUnit)}`}</li>)}
        </ul>
      )}
      {xs.length > ROW_CAP && <p className="sr-only">Showing the first {ROW_CAP} of {xs.length} rows.</p>}
    </div>
  );
}
