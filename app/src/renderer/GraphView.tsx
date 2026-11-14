import { useEffect, useMemo, useRef, useState } from "react";

import type { Card, RobotGraph } from "../shared/api.gen";
import { graphLabels, partNames } from "./partNames";
import { NODE_H, NODE_W, isolateIds, layout, partOf, relation, type Pos } from "./graphLayout";
import type { ViewInset } from "./scene/buildScene";
import { setSelectedPartId } from "./scene/selectionStore";
import { setOnlyParts, useViewState } from "./scene/viewState";
import "./GraphView.css";

// GRAPH-1: the robot as a node-and-link diagram. Which parts a check involves comes from the
// payload (`card.involves`); nothing here works out relevance.
const EDGE_WORD: Record<string, string> = { structure: "Structure", drives: "Drives", mounts: "Mounts", power: "Power", signal: "Signal", depends: "Depends on" };
const KIND_WORD: Record<string, string> = { link: "link", joint: "joint", device: "device", connector: "connector", host: "computer", bus: "bus", rail: "power rail", battery: "battery", wire: "wire", code: "code" };
const WALKED = new Set(["rail", "battery", "wire", "connector", "host", "bus"]);
const show = (n: number) => String(Math.round(n * 100) / 100);

const MIN_K = 0.95; // 12px labels at 0.95 stay about 11px on screen
type View = { k: number; x: number; y: number };

/** The path between two boxes: out of the right side, into the left, or looping round the right when they share a column. */
function edgePath(a: Pos, b: Pos): string {
  const same = Math.abs(a.x - b.x) < 1;
  const fwd = b.x > a.x;
  const sx = a.x + (fwd || same ? NODE_W : 0), tx = b.x + (fwd ? 0 : NODE_W), sy = a.y + NODE_H / 2, ty = b.y + NODE_H / 2;
  const d = same ? 50 : Math.max(40, Math.abs(tx - sx) / 2) * (fwd ? 1 : -1);
  return `M${sx} ${sy} C${sx + d} ${sy} ${tx + (same ? 50 : -d)} ${ty} ${tx} ${ty}`;
}

/** The checked parts with their neighbours, or just the checked parts when that is too wide to read at MIN_K. */
function boxOf(lit: Set<string>, W: number, H: number, pos: Map<string, Pos>, graph: RobotGraph): Pos[] {
  const near = new Set(lit);
  for (const e of graph.edges) { if (lit.has(e.from)) near.add(e.to); if (lit.has(e.to)) near.add(e.from); }
  const pick = (ids: Set<string>) => [...pos].filter(([id]) => ids.has(id)).map(([, p]) => p);
  const wide = (ps: Pos[]) => ps.length > 0 && Math.min(W / (Math.max(...ps.map((p) => p.x)) + NODE_W - Math.min(...ps.map((p) => p.x))), H / (Math.max(...ps.map((p) => p.y)) + NODE_H - Math.min(...ps.map((p) => p.y)))) < MIN_K;
  const withNear = pick(near), only = pick(lit);
  return withNear.length && !wide(withNear) ? withNear : only.length ? only : [...pos.values()];
}

export function GraphView({
  graph, cards, selected, onSelectCard, isolate, selectedPart, onPick, inset,
}: {
  graph: RobotGraph;
  cards: Card[];
  selected: number | null;
  onSelectCard: (index: number | null) => void;
  /** "Only what this check uses": everything the chosen check does not involve is left out. */
  isolate: boolean;
  /** The part picked anywhere (the 3D view, a row, this graph). */
  selectedPart: string | null;
  /** The same handler a click in 3D runs. */
  onPick: (partId: string) => void;
  inset?: ViewInset;
}) {
  const card = selected !== null ? cards[selected] : undefined;
  // GRAPH-2: a picked node (here or in 3D) lights what it is connected to; with none, the open check's parts light.
  const [pin, setPin] = useState<string | null>(null);
  useEffect(() => { setPin((p) => (p && partOf(p) !== selectedPart ? null : p)); }, [selectedPart]);
  const focusId = pin ?? (selectedPart ? graph.nodes.find((n) => n.id.slice(n.id.indexOf(":") + 1) === selectedPart)?.id ?? null : null);
  const rel = useMemo(() => (focusId ? relation(graph, focusId) : null), [graph, focusId]);
  const lit = useMemo(() => rel?.lit ?? new Set(card?.involves ?? []), [rel, card]);
  const on = lit.size > 0;
  // "Only these" is the 3D view's own only-list (VIEW-1), so the graph and the 3D body stay in step.
  const mine = rel ? isolateIds([...rel.lit]) : [];
  const viewOnly = useViewState().only;
  const only = mine.length > 0 && viewOnly.length === mine.length && mine.every((p) => viewOnly.includes(p));
  const keep = rel && only ? rel.lit : isolate && on ? lit : null;
  const shown = useMemo<RobotGraph>(() => {
    if (!keep) return graph;
    return { nodes: graph.nodes.filter((n) => keep.has(n.id)), edges: graph.edges.filter((e) => keep.has(e.from) && keep.has(e.to)) };
  }, [graph, keep]);
  const readers = focusId ? cards.flatMap((c, i) => (c.involves?.includes(focusId) ? [{ i, title: c.title }] : [])) : [];
  const pos = useMemo(() => layout(shown), [shown]);
  const [view, setView] = useState<View>({ k: 1, x: 40, y: 40 });
  const [hot, setHot] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const free = inset ?? { top: 70, right: 24, bottom: 70, left: 24 };
  const head = free.top + 52; // room for the floating notices (an example banner) above the controls

  // The open view is fitted to the checked parts and their neighbours and never smaller than MIN_K, so
  // labels stay readable; the Fit button fits the whole graph.
  const fit = (whole = false) => {
    const r = box.current?.getBoundingClientRect();
    const W = (r?.width || 900) - free.left - free.right, H = (r?.height || 560) - head - 100 - free.bottom;
    const ps = whole || !lit.size ? [...pos.values()] : boxOf(lit, W, H, pos, graph);
    if (!ps.length || W <= 0 || H <= 0) return;
    const x0 = Math.min(...ps.map((p) => p.x)), x1 = Math.max(...ps.map((p) => p.x)) + NODE_W;
    const y0 = Math.min(...ps.map((p) => p.y)), y1 = Math.max(...ps.map((p) => p.y)) + NODE_H;
    const k = Math.min(1.4, Math.max(whole ? 0 : MIN_K, Math.min(W / (x1 - x0), H / (y1 - y0))));
    // too big at the readable size: start at the top left and let the user pan
    setView({ k, x: free.left + Math.max(0, (W - (x1 - x0) * k) / 2) - x0 * k, y: head + 130 + Math.max(0, (H - (y1 - y0) * k) / 2) - y0 * k });
  };
  const moved = useRef(false); // the user has panned or zoomed since the last fit
  useEffect(() => { moved.current = false; fit(); }, [pos]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!moved.current) fit(); }, [selected]); // a newly chosen check brings its parts into view, unless the user moved the view

  const zoom = (f: number, cx?: number, cy?: number) => { moved.current = true; setView((v) => {
    const r = box.current?.getBoundingClientRect();
    const px = cx ?? (r?.width ?? 900) / 2, py = cy ?? (r?.height ?? 560) / 2;
    const k = Math.min(3, Math.max(0.2, v.k * f));
    return { k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
  }); };

  const label = new Map(graph.nodes.map((n) => [n.id, n]));
  const nameOf = partNames(graphLabels(graph));
  const usedNames = [...lit].map(nameOf);
  const hotNode = hot ? label.get(hot) : undefined;
  const numbers = hot && lit.has(hot) && card ? [card.observed && `reads ${show(card.observed.value)} ${card.observed.unit}`, card.expected && `expects ${show(card.expected.value)} ${card.expected.unit}`].filter(Boolean).join(", ") : "";
  const choose = (n: string) => onSelectCard(n === "" ? null : Number(n));
  const press = (id: string) => { setPin(id); const p = partOf(id); if (p) onPick(p); };
  const names = (ids: string[]) => ids.map((i) => label.get(i)?.label ?? i).join(", ");
  const focusNode = focusId ? label.get(focusId) : undefined;

  return (
    <div className="gv" ref={box} data-testid="graph-view">
      <svg className="gv-svg" role="group" aria-label="Robot graph: what is connected to what"
        onPointerDown={(e) => { if (e.target === e.currentTarget || (e.target as Element).classList.contains("gv-bg")) { drag.current = { x: e.clientX, y: e.clientY }; e.currentTarget.setPointerCapture?.(e.pointerId); } }}
        onPointerMove={(e) => { const d = drag.current; if (d) { moved.current = true; drag.current = { x: e.clientX, y: e.clientY }; setView((v) => ({ ...v, x: v.x + e.clientX - d.x, y: v.y + e.clientY - d.y })); } }}
        onPointerUp={() => { drag.current = null; }}
        onWheel={(e) => { const r = box.current?.getBoundingClientRect(); zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - (r?.left ?? 0), e.clientY - (r?.top ?? 0)); }}>
        <defs>
          {Object.keys(EDGE_WORD).map((k) => <marker key={k} id={`gv-arrow-${k}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8 z" className={`gv-arrow gv-e-${k}`} /></marker>)}
        </defs>
        <rect className="gv-bg" width="100%" height="100%" fill="transparent" />
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {shown.edges.map((e, i) => {
            const a = pos.get(e.from), b = pos.get(e.to);
            if (!a || !b) return null;
            const both = lit.has(e.from) && lit.has(e.to);
            return <path key={i} d={edgePath(a, b)} data-from={e.from} data-to={e.to} markerEnd={`url(#gv-arrow-${e.kind})`} className={`gv-edge gv-e-${e.kind}${both ? " is-lit" : on ? " is-dim" : ""}`}><title>{EDGE_WORD[e.kind]}</title></path>;
          })}
          {shown.nodes.map((n) => {
            const p = pos.get(n.id);
            if (!p) return null;
            const isLit = lit.has(n.id), part = partOf(n.id);
            const sel = part !== null && part === selectedPart;
            return (
              <g key={n.id} data-node={n.id} transform={`translate(${p.x} ${p.y})`} tabIndex={0} role="button"
                aria-label={`${n.label}, ${KIND_WORD[n.kind]}${isLit ? ", used by this check" : ""}`} aria-pressed={sel}
                className={`gv-node gv-k-${n.kind}${isLit ? " is-lit" : on ? " is-dim" : ""}${sel ? " is-selected" : ""}`}
                onClick={() => press(n.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); press(n.id); } }}
                onMouseEnter={() => setHot(n.id)} onMouseLeave={() => setHot(null)} onFocus={() => setHot(n.id)} onBlur={() => setHot(null)}>
                <rect width={NODE_W} height={NODE_H} rx="7" />
                <text x="10" y={NODE_H / 2 + 4}>{n.label.length > 21 ? `${n.label.slice(0, 20)}…` : n.label}</text>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="gv-bar" style={{ top: head, left: free.left }}>
        <label className="gv-pick">
          <span className="kit-sr">Check to show</span>
          <select value={selected ?? ""} onChange={(e) => choose(e.target.value)}>
            <option value="">Show a check…</option>
            {cards.map((c, i) => <option key={i} value={i}>{c.title}</option>)}
          </select>
        </label>
        <button type="button" className="kit-btn kit-btn-quiet" aria-label="Zoom out" onClick={() => zoom(1 / 1.25)}>−</button>
        <button type="button" className="kit-btn kit-btn-quiet" aria-label="Zoom in" onClick={() => zoom(1.25)}>+</button>
        <button type="button" className="kit-btn kit-btn-quiet" onClick={() => fit(true)}>Fit</button>
      </div>
      <p className="gv-uses" aria-live="polite" style={{ left: free.left, top: head + 40 }}>{focusNode ? <>Showing what {focusNode.label} is connected to.</> : on ? <>This check uses: {usedNames.join(", ")}</> : "Pick a part or a check to see what it is connected to."}</p>
      {focusNode && rel && (
        <aside className="gv-focus" aria-label="What this part is connected to" style={{ right: free.right, top: head }}>
          <p className="gv-focus-h"><b>{focusNode.label}</b>, {KIND_WORD[focusNode.kind]}</p>
          {WALKED.has(focusNode.kind) && <p>Feeds: {rel.fed.length ? names(rel.fed) : "nothing"}</p>}
          {focusNode.kind === "joint" && <><p>Driven by: {rel.actuators.length ? names(rel.actuators) : "nothing"}</p><p>Carries: {rel.carries.length ? names(rel.carries) : "nothing"}</p></>}
          {rel.near.length > 0 && <p>Connected to: {names(rel.near)}</p>}
          <p>Checks that read this part:</p>
          {readers.length ? <ul>{readers.map((r) => <li key={r.i}><button type="button" className="gv-link" onClick={() => onSelectCard(r.i)}>{r.title}</button></li>)}</ul> : <p className="gv-none">None.</p>}
          <div className="gv-focus-row">
            <button type="button" className="kit-btn kit-btn-quiet" aria-pressed={only} onClick={() => setOnlyParts(only ? [] : mine)}>{only ? "Show everything" : "Only these"}</button>
            <button type="button" className="kit-btn kit-btn-quiet" onClick={() => { if (only) setOnlyParts([]); setPin(null); setSelectedPartId(null); }}>Clear</button>
          </div>
        </aside>
      )}
      <div className="gv-key" aria-label="Line kinds" style={{ left: free.left, top: head + 88 }}>
        {Object.entries(EDGE_WORD).map(([k, w]) => <span key={k}><svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" className={`gv-edge gv-e-${k}`} /></svg>{w}</span>)}
      </div>
      <p className="gv-tip" role="status" style={{ left: free.left, bottom: Math.max(8, free.bottom - 44) }}>{hotNode ? <><b>{hotNode.label}</b>, {KIND_WORD[hotNode.kind]}{numbers ? `. This check ${numbers}.` : ""}</> : ""}</p>
    </div>
  );
}
