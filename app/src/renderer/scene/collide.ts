// CAD-14: which parts overlap or sit too close. Hand-written for the primitives the format has (box,
// sphere, cylinder), in metres, with no new dependency. Boxes and spheres are exact in overlap/no
// overlap (box vs box by the separating axis theorem, 15 axes). A cylinder is checked as its bounding
// box, so any pair with a cylinder is marked approximate. Meshes are not checked.
import type { LinkSolid, PagePayload } from "../../shared/api.gen";

type V3 = [number, number, number];
const dot = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a: V3, b: V3): V3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a: V3) => Math.hypot(a[0], a[1], a[2]);

/** A box (centre, three unit axes, half sizes) or a sphere; `approx` when a cylinder stands in as its bounding box. */
type Body = { box: true; c: V3; u: [V3, V3, V3]; h: V3; approx: boolean } | { box: false; c: V3; r: number; approx: false };

export function bodyOf(s: LinkSolid): Body {
  const c = s.position as V3;
  if (s.kind === "sphere") return { box: false, c, r: s.radius ?? 0, approx: false };
  const R = s.rotation;
  const u: [V3, V3, V3] = [0, 1, 2].map((k) => [R[k]!, R[3 + k]!, R[6 + k]!] as V3) as [V3, V3, V3]; // columns of the row-major 3x3
  const h: V3 = s.kind === "box" ? (s.size!.map((x) => x / 2) as V3) : [s.radius ?? 0, s.radius ?? 0, (s.length ?? 0) / 2];
  return { box: true, c, u, h, approx: s.kind === "cylinder" };
}

const volume = (b: Body) => (b.box ? 8 * b.h[0] * b.h[1] * b.h[2] : (4 / 3) * Math.PI * b.r ** 3);
const inside = (b: Body, p: V3) => {
  const d = sub(p, b.c);
  return b.box ? b.u.every((u, i) => Math.abs(dot(d, u)) <= b.h[i]!) : norm(d) <= b.r;
};

export interface PairResult {
  overlap: boolean;
  /** m3; only when overlapping. */
  volume: number | null;
  volumeExact: boolean;
  /** m; only when apart. Exact for two spheres, a sphere and a box, and two boxes sharing axes; else a lower bound. */
  gap: number | null;
  gapExact: boolean;
  /** A cylinder was checked as its bounding box. */
  approximate: boolean;
}

/** Box vs box: the separating axis theorem over 15 axes. `sep` is the largest separation found (negative: overlapping). */
function satBoxBox(a: Extract<Body, { box: true }>, b: Extract<Body, { box: true }>) {
  const t = sub(b.c, a.c);
  const axes: V3[] = [...a.u, ...b.u];
  for (const x of a.u) for (const y of b.u) axes.push(cross(x, y));
  let sep = -Infinity;
  for (const raw of axes) {
    const n = norm(raw);
    if (n < 1e-9) continue; // parallel edges: no new axis
    const l: V3 = [raw[0] / n, raw[1] / n, raw[2] / n];
    const ra = a.u.reduce((s, u, i) => s + a.h[i]! * Math.abs(dot(u, l)), 0);
    const rb = b.u.reduce((s, u, i) => s + b.h[i]! * Math.abs(dot(u, l)), 0);
    sep = Math.max(sep, Math.abs(dot(t, l)) - ra - rb);
  }
  return sep;
}

/** Closest point of a box to a point. */
function nearestOnBox(b: Extract<Body, { box: true }>, p: V3): V3 {
  const d = sub(p, b.c);
  const q: V3 = [...b.c];
  b.u.forEach((u, i) => { const k = Math.max(-b.h[i]!, Math.min(b.h[i]!, dot(d, u))); for (let j = 0; j < 3; j++) q[j]! += k * u[j]!; });
  return q;
}

/** Overlap volume by a fixed 16x16x16 grid over the smaller body: an estimate, a few percent off at worst. */
function estimateVolume(a: Body, b: Body): number {
  const [s, o] = volume(a) <= volume(b) ? [a, b] : [b, a];
  const half: V3 = s.box ? s.h : [s.r, s.r, s.r];
  const axes: V3[] = s.box ? s.u : [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  const N = 16;
  let kept = 0, hit = 0;
  for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) for (let k = 0; k < N; k++) {
    const f = [i, j, k].map((n) => ((n + 0.5) / N) * 2 - 1);
    const p: V3 = [...s.c];
    axes.forEach((u, m) => { for (let d = 0; d < 3; d++) p[d]! += f[m]! * half[m]! * u[d]!; });
    if (!inside(s, p)) continue;
    kept++;
    if (inside(o, p)) hit++;
  }
  return kept ? (hit / kept) * volume(s) : 0;
}

export function checkPair(sa: LinkSolid, sb: LinkSolid): PairResult {
  const a = bodyOf(sa), b = bodyOf(sb);
  const approximate = a.approx || b.approx;
  let gap: number; // negative: overlapping
  let exactGap = true;
  let volumeExact = false;
  let volumeValue: number | null = null;
  if (!a.box && !b.box) {
    const d = norm(sub(b.c, a.c));
    gap = d - a.r - b.r;
    if (gap < 0) { // lens of two spheres; one inside the other is the smaller sphere whole
      volumeExact = true;
      const [r1, r2] = [a.r, b.r];
      volumeValue = d <= Math.abs(r1 - r2) ? volume(r1 <= r2 ? a : b) : (Math.PI * (r1 + r2 - d) ** 2 * (d * d + 2 * d * (r1 + r2) - 3 * (r1 - r2) ** 2)) / (12 * d);
    }
  } else if (a.box !== b.box) {
    const [bx, sp] = (a.box ? [a, b] : [b, a]) as [Extract<Body, { box: true }>, Extract<Body, { box: false }>];
    gap = norm(sub(sp.c, nearestOnBox(bx, sp.c))) - sp.r;
  } else {
    const A = a as Extract<Body, { box: true }>, B = b as Extract<Body, { box: true }>;
    const m = A.u.map((u) => B.u.map((v) => Math.abs(dot(u, v))));
    const shared = m.every((row) => row.every((x) => x < 1e-9 || x > 1 - 1e-9));
    const t = sub(B.c, A.c);
    if (shared) { // both are axis-aligned boxes in one common frame: exact gap and exact volume
      const per = A.u.map((u, i) => { const d = Math.abs(dot(t, u)), rb = m[i]!.reduce((s, x, j) => s + x * B.h[j]!, 0); return { gap: d - A.h[i]! - rb, ov: Math.max(0, Math.min(A.h[i]!, dot(t, u) + rb) - Math.max(-A.h[i]!, dot(t, u) - rb)) }; });
      gap = per.every((p) => p.gap < 0) ? -1 : Math.hypot(...per.map((p) => Math.max(0, p.gap)));
      if (gap < 0) { volumeExact = true; volumeValue = per.reduce((s, p) => s * p.ov, 1); }
    } else {
      gap = satBoxBox(A, B);
      exactGap = false; // the largest separating-axis distance is a lower bound on the true gap
    }
  }
  const overlap = gap < 0;
  return {
    overlap,
    volume: overlap ? (volumeValue ?? estimateVolume(a, b)) : null,
    volumeExact: overlap && volumeExact && !approximate,
    gap: overlap ? null : gap,
    gapExact: !overlap && exactGap && !approximate,
    approximate,
  };
}

export interface Finding extends PairResult { a: string; b: string }
export interface Interference {
  /** Overlapping pairs, largest overlap first. */
  overlaps: Finding[];
  /** Apart but closer than `clearance` (m), nearest first. */
  close: Finding[];
  /** The declared pairs left out of the check, with the reason given. */
  ignored: { link1: string; link2: string; reason: string }[];
}

/** Every pair of links that carry primitives, except a pair joined by a joint and a pair on the ignore list. */
export function findInterference(page: PagePayload, clearance = 0.005): Interference {
  const joined = new Set(page.joints.flatMap((j) => [j.parent + "\u0000" + j.child, j.child + "\u0000" + j.parent]));
  const skipped = new Set((page.collision_pairs ?? []).flatMap((p) => [p.link1 + "\u0000" + p.link2, p.link2 + "\u0000" + p.link1]));
  const links = page.links.map((l) => ({ name: l.name, solids: l.solids ?? [] })).filter((l) => l.solids.length > 0);
  const found: Finding[] = [];
  for (let i = 0; i < links.length; i++) for (let j = i + 1; j < links.length; j++) {
    const [p, q] = [links[i]!, links[j]!], key = p.name + "\u0000" + q.name;
    if (joined.has(key) || skipped.has(key)) continue;
    const results = p.solids.flatMap((x) => q.solids.map((y) => checkPair(x, y)));
    const worst = results.reduce((w, r) => (!w || (r.overlap ? !w.overlap || r.volume! > w.volume! : !w.overlap && r.gap! < w.gap!) ? r : w), undefined as PairResult | undefined);
    if (worst) found.push({ ...worst, a: p.name, b: q.name });
  }
  return {
    overlaps: found.filter((f) => f.overlap).sort((x, y) => y.volume! - x.volume!),
    close: found.filter((f) => !f.overlap && f.gap! < clearance).sort((x, y) => x.gap! - y.gap!),
    ignored: page.collision_pairs ?? [],
  };
}

// CAD-15: sweep each limited joint through its range and report where its moving parts hit something.
export interface SweepResult {
  /** The joint's child link, as the app names it. */
  joint: string;
  unit: "deg" | "mm";
  /** One step of the sweep, in `unit`. */
  step: number;
  /** The first step (from the lower limit) where a moving part hits a fixed one, `at` in degrees or mm. */
  hit: { a: string; b: string; at: number } | null;
  /** The smallest gap seen over the steps before any hit (m), and where. */
  closest: { a: string; b: string; at: number; gap: number } | null;
}
export interface Sweep {
  swept: SweepResult[];
  /** Movable joints with no declared limits, left unswept. */
  notSwept: string[];
}

/** Steps per sweep: the hit angle is exact to one step (a 180 degree range is 5 degrees a step). */
export const SWEEP_STEPS = 36;

/** A solid (or anything with a position and a row-major rotation) moved by one joint at value q: radians about the joint's point, or metres along its axis. POSE-1 poses the scene with this same function. */
export const posed = <S extends { position: number[]; rotation: number[] }>(s: S, joint: { type: string; position: number[]; axis: number[] }, q: number): S => {
  const p = joint.position as V3, n = norm(joint.axis as V3) || 1, k = (joint.axis as V3).map((x) => x / n) as V3;
  if (joint.type === "prismatic") return { ...s, position: s.position.map((x, i) => x + q * k[i]!) };
  // Rodrigues: R = I cos + (1 - cos) k k^T + sin [k]x, turned about the joint's point
  const c = Math.cos(q), sn = Math.sin(q), v = 1 - c;
  const R = [c + v * k[0] * k[0], v * k[0] * k[1] - sn * k[2], v * k[0] * k[2] + sn * k[1], v * k[0] * k[1] + sn * k[2], c + v * k[1] * k[1], v * k[1] * k[2] - sn * k[0], v * k[0] * k[2] - sn * k[1], v * k[1] * k[2] + sn * k[0], c + v * k[2] * k[2]];
  const d = sub(s.position as V3, p);
  return { ...s, position: [0, 1, 2].map((i) => p[i]! + R[3 * i]! * d[0] + R[3 * i + 1]! * d[1] + R[3 * i + 2]! * d[2]), rotation: [0, 1, 2].flatMap((i) => [0, 1, 2].map((j) => [0, 1, 2].reduce((t, m) => t + R[3 * i + m]! * s.rotation[3 * m + j]!, 0))) };
};

/** One joint at a time, the rest at the zero pose (ponytail: no two joints moved together; a combined sweep is a search, not a loop). */
export function sweepJoints(page: PagePayload, steps = SWEEP_STEPS): Sweep {
  const key = (a: string, b: string) => a + "\u0000" + b;
  const skip = new Set([...page.joints.flatMap((j) => [key(j.parent, j.child), key(j.child, j.parent)]), ...(page.collision_pairs ?? []).flatMap((p) => [key(p.link1, p.link2), key(p.link2, p.link1)])]);
  const human = new Map(page.joints.flatMap((j) => [[j.parent, j.parent_name], [j.child, j.child_name]] as [string, string][]));
  const solids = new Map(page.links.map((l) => [l.name, l.solids ?? []]));
  const out: Sweep = { swept: [], notSwept: [] };
  for (const j of page.joints) {
    if (j.type === "fixed") continue;
    if (!(j.type === "revolute" || j.type === "prismatic") || j.lower == null || j.upper == null) { out.notSwept.push(human.get(j.child) ?? j.child); continue; }
    const moving = new Set([j.child]); // the child and everything hung below it
    for (let grew = true; grew;) { grew = false; for (const k of page.joints) if (moving.has(k.parent) && !moving.has(k.child)) { moving.add(k.child); grew = true; } }
    const fixedLinks = [...solids.keys()].filter((n) => !moving.has(n) && solids.get(n)!.length);
    // a pair already overlapping at rest belongs to the plain Overlaps list, not to the sweep
    const pairs = [...moving].flatMap((m) => fixedLinks.map((f) => [m, f] as const)).filter(([m, f]) => !skip.has(key(m, f)) && solids.get(m)!.length && !solids.get(m)!.some((x) => solids.get(f)!.some((y) => checkPair(x, y).overlap)));
    const r: SweepResult = { joint: human.get(j.child) ?? j.child, unit: j.type === "prismatic" ? "mm" : "deg", step: ((j.upper - j.lower) / steps) * (j.type === "prismatic" ? 1000 : 180 / Math.PI), hit: null, closest: null };
    const shown = (q: number) => (r.unit === "deg" ? Math.round((q * 180) / Math.PI) : Math.round(q * 1000));
    for (let i = 0; i <= steps && !r.hit; i++) {
      const q = j.lower + ((j.upper - j.lower) * i) / steps;
      for (const [m, f] of pairs) {
        const res = solids.get(m)!.flatMap((x) => solids.get(f)!.map((y) => checkPair(posed(x, j, q), y)));
        if (res.some((x) => x.overlap)) { r.hit = { a: human.get(m) ?? m, b: human.get(f) ?? f, at: shown(q) }; break; }
        const gap = Math.min(...res.map((x) => x.gap!));
        if (!r.closest || gap < r.closest.gap) r.closest = { a: human.get(m) ?? m, b: human.get(f) ?? f, at: shown(q), gap };
      }
    }
    out.swept.push(r);
  }
  return out;
}
