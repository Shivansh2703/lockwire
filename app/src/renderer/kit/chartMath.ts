// Pure maths for Chart.tsx. No DOM, no React.
export type Pt = [number, number];

const P = (a: Pt[], i: number) => a[i] as Pt;
const N = (a: number[], i: number) => a[i] as number;
const clean = (v: number) => Number(v.toPrecision(12));

/** Ticks at 1/2/5 x 10^n that cover [min, max]. */
export function niceTicks(min: number, max: number, target = 5): number[] {
  if (!(max > min)) {
    const pad = Math.abs(min) * 0.1 || 1;
    min -= pad;
    max += pad;
  }
  const rough = (max - min) / (target - 1);
  const mag = 10 ** Math.floor(Math.log10(rough));
  const f = rough / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const start = Math.floor(min / step + 1e-9);
  const end = Math.ceil(max / step - 1e-9);
  const out: number[] = [];
  for (let i = start; i <= end; i++) out.push(clean(i * step));
  return out;
}

/** [min, max] of values plus any must-include numbers (limits); equal ends are padded; null when empty. */
export function extent(values: number[], include: number[] = []): [number, number] | null {
  const all = [...values, ...include].filter(Number.isFinite);
  if (!all.length) return null;
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.1 || 1;
    lo -= pad;
    hi += pad;
  }
  return [lo, hi];
}

/** Drop non-finite points, sort by x. `dropped` counts what was removed. */
export function cleanPoints(points: Pt[]): { points: Pt[]; dropped: number } {
  const ok = points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  ok.sort((a, b) => a[0] - b[0]);
  return { points: ok, dropped: points.length - ok.length };
}

/** Min/max bucket decimation: each bucket keeps its lowest and highest y, so peaks survive. Input sorted by x. */
export function decimate(points: Pt[], max = 600): Pt[] {
  if (points.length <= max) return points;
  const buckets = Math.floor((max - 2) / 2);
  const size = (points.length - 2) / buckets;
  const out: Pt[] = [P(points, 0)];
  for (let b = 0; b < buckets; b++) {
    const from = 1 + Math.floor(b * size);
    const to = 1 + Math.floor((b + 1) * size);
    let lo = P(points, from);
    let hi = lo;
    for (let i = from; i < to; i++) {
      const p = P(points, i);
      if (p[1] < lo[1]) lo = p;
      if (p[1] > hi[1]) hi = p;
    }
    if (lo === hi) out.push(lo);
    else out.push(...(lo[0] < hi[0] ? [lo, hi] : [hi, lo]));
  }
  out.push(P(points, points.length - 1));
  return out;
}

/** Index in ascending `xs` whose value is nearest `x` (-1 when empty). */
export function nearestIndex(xs: number[], x: number): number {
  if (!xs.length) return -1;
  let lo = 0;
  let hi = xs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (N(xs, mid) < x) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 && x - N(xs, lo - 1) <= N(xs, lo) - x ? lo - 1 : lo;
}

/** At most 3 significant digits, optional unit. */
export function formatValue(v: number, unit?: string): string {
  const s = String(Number(v.toPrecision(3)));
  return unit ? `${s} ${unit}` : s;
}

export type Limit = { y: number; kind: "max" | "min" };
/** How far `y` is beyond the limit (<= 0 means inside). */
export const excess = (l: Limit, y: number) => (l.kind === "max" ? y - l.y : l.y - y);
export const beyond = (l: Limit, y: number) => excess(l, y) > 0;

/** The part of segment a-b that is beyond the limit, cut at the crossing by linear interpolation; null when none. */
export function splitAtLimit(a: Pt, b: Pt, l: Limit): [Pt, Pt] | null {
  const [ia, ib] = [beyond(l, a[1]), beyond(l, b[1])];
  if (ia && ib) return [a, b];
  if (!ia && !ib) return null;
  const t = (l.y - a[1]) / (b[1] - a[1]);
  const c: Pt = [a[0] + t * (b[0] - a[0]), l.y];
  return ia ? [a, c] : [c, b];
}

/** Tick labels with enough decimals (from the step) that neighbours never print the same. */
export function formatTicks(ticks: number[]): string[] {
  const step = ticks.length > 1 ? Math.abs((ticks[1] as number) - (ticks[0] as number)) : 1;
  const d = Math.min(12, Math.max(0, -Math.floor(Math.log10(step) + 1e-9)));
  return ticks.map((v) => v.toFixed(d));
}

/** Clamp label baselines into [top, bottom]; a label closer than `gap` to an earlier one moves down (up at the bottom). */
export function placeLabels(ys: number[], top: number, bottom: number, gap = 12): number[] {
  const out: number[] = [];
  for (const y of ys) {
    let c = Math.max(top, Math.min(bottom, y));
    const near = out.filter((p) => Math.abs(p - c) < gap);
    if (near.length) {
      c = Math.max(...near) + gap;
      if (c > bottom) c = Math.min(...near) - gap;
    }
    out.push(c);
  }
  return out;
}
