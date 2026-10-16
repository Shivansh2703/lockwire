/**
 * The connection views (Power, Data, Mechanical): the payload's own per-lens graph
 * (`report_html._graph`, numbers on `GraphNode.value`/`.limit`) turned into the tree rows the
 * Connections panel lists and the tubes the scene draws between parts. Pure: no three.js, no DOM.
 */
import { fmtQty, tidyNumbers } from "../kit/format";
import type { GraphLens, GraphNode } from "../../shared/api.gen";

export type ConnLens = "power" | "data" | "mechanical";
export const CONN_LENSES: readonly string[] = ["power", "data", "mechanical"];

export interface ConnRow {
  id: string;
  name: string;
  meta: string;
  depth: number;
  /** bad: an overloaded rail. warn: a number the lockfile does not declare. */
  state?: "bad" | "warn";
  /** used / capacity, rails only. */
  frac?: number;
  /** This node and everything under it: what lights when the row is picked. */
  members: string[];
}

export type Vec3 = [number, number, number];
export interface Tube { from: string; to: string; a: Vec3; b: Vec3; radius: number; over: boolean }

const over = (n?: GraphNode) => n?.kind === "rail" && n.value != null && n.limit != null && n.value > n.limit;
const num = (v: number) => fmtQty(v);
const rate = (v: number) => {
  const [d, u] = v >= 1e9 ? [1e9, "G"] : v >= 1e6 ? [1e6, "M"] : v >= 1e3 ? [1e3, "k"] : [1, ""];
  return `${+(v / d).toFixed(1)} ${u}bit/s`;
};

function meta(n: GraphNode, lens: ConnLens): Pick<ConnRow, "meta" | "state" | "frac"> {
  const v = n.value;
  if (lens === "mechanical") return v == null ? { meta: "mass unknown", state: "warn" } : { meta: v < 1 ? `${num(v * 1000)} g` : `${num(v)} kg` };
  if (n.kind === "rail") {
    const lim = n.limit ?? 0;
    return { meta: `${v == null ? "?" : num(v)} / ${num(lim)} W`, state: over(n) ? "bad" : undefined, frac: v != null && lim > 0 ? v / lim : undefined };
  }
  if (lens === "power" && n.kind === "device") return v == null ? { meta: "draw unknown", state: "warn" } : { meta: `${num(v)} W` };
  if (n.kind === "bus") return { meta: [n.sub.split(" · ")[0], v != null && rate(v)].filter(Boolean).join(" ") };
  if (n.kind === "source" || n.kind === "host") return { meta: tidyNumbers(n.sub) };
  return { meta: "" };
}

/** Parent -> children. Power: a rail fed by no declared rail hangs under the battery, as
 * `describe.render_power_tree` draws it (the contract may leave that edge out on purpose). */
function childrenOf(g: GraphLens): Map<string, string[]> {
  const kids = new Map<string, string[]>();
  const add = (from: string, to: string) => kids.set(from, [...(kids.get(from) ?? []), to]);
  for (const e of g.edges) add(e.from, e.to);
  const fed = new Set(g.edges.map((e) => e.to));
  const source = g.nodes.find((n) => n.kind === "source");
  if (source) for (const n of g.nodes) if (n.kind === "rail" && !fed.has(n.id)) add(source.id, n.id);
  return kids;
}

export function connectionRows(g: GraphLens, lens: ConnLens, names: ReadonlyMap<string, string> = new Map()): ConnRow[] {
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  const kids = childrenOf(g);
  const fed = new Set([...kids.values()].flat());
  const rows: ConnRow[] = [];
  const walk = (id: string, depth: number, seen: Set<string>): string[] => {
    const n = byId.get(id);
    if (!n || seen.has(id)) return [];
    const row: ConnRow = { id, name: names.get(id) ?? n.label.charAt(0).toUpperCase() + n.label.slice(1), depth, members: [id], ...meta(n, lens) };
    rows.push(row);
    for (const k of kids.get(id) ?? []) row.members.push(...walk(k, depth + 1, new Set(seen).add(id)));
    return row.members;
  };
  for (const n of g.nodes) if (!fed.has(n.id)) walk(n.id, 0, new Set());
  return rows;
}

/** One tube per edge whose two ends both have a place on the robot. Thickness follows the
 * watts (power), the link's bit rate (data) or the carried mass (mechanical), on a log scale
 * between `size * 0.002` and `size * 0.008` (size: the robot's longest side). */
export function connectionTubes(g: GraphLens, centre: (id: string) => Vec3 | undefined, size: number): Tube[] {
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  const weight = (from: string, to: string) => {
    const f = byId.get(from), t = byId.get(to);
    return (f?.kind === "bus" ? f.value : t?.kind === "bus" ? t.value : t?.value) ?? 0;
  };
  const placed = g.edges.flatMap((e) => {
    const a = centre(e.from), b = centre(e.to);
    return a && b ? [{ e, a, b, w: weight(e.from, e.to) }] : [];
  });
  const top = Math.log1p(Math.max(0, ...placed.map((p) => p.w)));
  const [lo, hi] = [size * 0.002, size * 0.008];
  return placed.map(({ e, a, b, w }) => ({
    from: e.from, to: e.to, a, b,
    radius: lo + (hi - lo) * (top > 0 ? Math.min(Math.log1p(Math.max(w, 0)) / top, 1) : 0),
    over: over(byId.get(e.from)) || over(byId.get(e.to)),
  }));
}
