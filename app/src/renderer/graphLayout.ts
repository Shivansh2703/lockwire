import type { RobotGraph } from "../shared/api.gen";

// GRAPH-1: a layered layout by hand. The structure tree (links and joints) runs left to right;
// every other kind gets one column to its right, each node as high as the parts it connects to.
export const COL = 200;
export const ROW = 40;
export const NODE_W = 160;
export const NODE_H = 28;
export type Pos = { x: number; y: number };

const AFTER = ["device", "connector", "wire", "rail", "battery", "host", "bus", "code"] as const;

export function layout(g: RobotGraph): Map<string, Pos> {
  const kind = new Map(g.nodes.map((n) => [n.id, n.kind]));
  const out = new Map<string, string[]>(), hasParent = new Set<string>();
  for (const e of g.edges) {
    if (e.kind !== "structure" || !kind.has(e.from) || !kind.has(e.to)) continue;
    out.set(e.from, [...(out.get(e.from) ?? []), e.to]);
    hasParent.add(e.to);
  }
  const tree = g.nodes.filter((n) => n.kind === "link" || n.kind === "joint").map((n) => n.id);
  const grid = new Map<string, { depth: number; row: number }>();
  let row = 0, deepest = 0;
  const place = (id: string, depth: number): number => {
    if (grid.has(id)) return grid.get(id)!.row;
    grid.set(id, { depth, row: 0 }); // seen, so a cycle cannot loop
    deepest = Math.max(deepest, depth);
    const kids = (out.get(id) ?? []).map((k) => place(k, depth + 1));
    const r = kids.length ? kids.reduce((a, b) => a + b, 0) / kids.length : row++;
    grid.get(id)!.row = r;
    return r;
  };
  for (const id of tree) if (!hasParent.has(id)) place(id, 0);
  for (const id of tree) place(id, 0);
  const pos = new Map<string, Pos>();
  for (const [id, p] of grid) pos.set(id, { x: p.depth * COL, y: p.row * ROW });

  const near = new Map<string, string[]>();
  for (const e of g.edges) {
    near.set(e.from, [...(near.get(e.from) ?? []), e.to]);
    near.set(e.to, [...(near.get(e.to) ?? []), e.from]);
  }
  let col = deepest + 1;
  for (const k of AFTER) {
    const ids = g.nodes.filter((n) => n.kind === k).map((n) => n.id);
    if (!ids.length) continue;
    const want = ids.map((id) => {
      const ys = (near.get(id) ?? []).flatMap((o) => pos.get(o)?.y ?? []);
      return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Infinity;
    });
    const order = ids.map((_, i) => i).sort((a, b) => want[a]! - want[b]! || a - b);
    let floor = -Infinity;
    for (const i of order) {
      const y = Math.max(Number.isFinite(want[i]!) ? want[i]! : 0, floor + ROW);
      pos.set(ids[i]!, { x: col * COL, y });
      floor = y;
    }
    col++;
  }
  return pos;
}

/** The 3D part a node stands for (the link or joint name, a device's id); the rest have no mesh. */
export const partOf = (id: string): string | null => (/^(link|joint|device):/.test(id) ? id.slice(id.indexOf(":") + 1) : null);

/** The part ids to keep in the 3D view for a set of involved nodes. */
export const isolateIds = (involves: readonly string[]): string[] => involves.flatMap((id) => partOf(id) ?? []);

/** GRAPH-2: what a picked node is connected to. Only walks the edges the payload gave; nothing is worked out here. */
export type Relation = { fed: string[]; actuators: string[]; carries: string[]; near: string[]; lit: Set<string> };
const WALK: Record<string, string[]> = { rail: ["power"], battery: ["power"], wire: ["power", "signal"], connector: ["power", "signal"], host: ["signal"], bus: ["signal"] };

export function relation(g: RobotGraph, id: string): Relation {
  const kind = new Map(g.nodes.map((n) => [n.id, n.kind]));
  const reach = (from: string, kinds: string[], stop: (k: string) => boolean): string[] => {
    const seen = new Set<string>([from]), todo = [from];
    while (todo.length) {
      const at = todo.pop()!;
      if (at !== from && stop(kind.get(at) ?? "")) continue;
      for (const e of g.edges) if (e.from === at && kinds.includes(e.kind) && !seen.has(e.to)) { seen.add(e.to); todo.push(e.to); }
    }
    seen.delete(from);
    return [...seen];
  };
  const k = kind.get(id) ?? "";
  const r: Relation = { fed: [], actuators: [], carries: [], near: [], lit: new Set([id]) };
  if (WALK[k]) r.fed = reach(id, WALK[k]!, (x) => x === "device");
  else if (k === "joint") {
    r.actuators = g.edges.filter((e) => e.kind === "drives" && e.to === id).map((e) => e.from);
    const down = reach(id, ["structure", "depends"], () => false);
    const mounted = g.edges.filter((e) => e.kind === "mounts" && down.includes(e.to)).map((e) => e.from);
    r.carries = [...down, ...mounted];
  } else r.near = [...new Set(g.edges.flatMap((e) => (e.from === id ? [e.to] : e.to === id ? [e.from] : [])))];
  for (const n of [...r.fed, ...r.actuators, ...r.carries, ...r.near]) r.lit.add(n);
  return r;
}
