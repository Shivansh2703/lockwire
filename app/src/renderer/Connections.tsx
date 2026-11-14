/**
 * The Design page's connection views: `ConnectionsPanel` (left, the tree of what feeds, talks to
 * or carries what) and `ConnectionLines` (inside the RobotView: one glowing tube per connection
 * between the two parts, thicker = more watts / bandwidth / mass, red off an overloaded rail).
 * Rows and tubes come from `scene/connections.ts`; selection is the shared store, hover is
 * `designView.hover` (lights without moving the camera).
 */
import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";

import type { CheckPayload, PagePayload } from "../shared/api.gen";
import { Panel } from "./kit";
import { activeCamera } from "./scene/buildScene";
import { useScene } from "./scene/Canvas";
import { useViewState } from "./scene/viewState";
import { CalloutLayer, type CalloutAnchor } from "./scene/CalloutLayer";
import { connectionRows, connectionTubes, sagHeat, sagRows, type ConnLens, type ConnRow, type SagRow, type Vec3 } from "./scene/connections";
import { setDesignView, useDesignView } from "./scene/designView";
import { readThemeColors, type ThemeColors } from "./scene/robotScene";
import { setSelectedGroup, setSelectedPartId, useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";

type Payload = CheckPayload | PagePayload | null;

const EMPTY: Record<ConnLens, string> = {
  power: "No power rails declared for this robot.",
  data: "No data links declared for this robot.",
  mechanical: "No mounts declared for this robot.",
};

/** id -> the scene's own human label for it ("Companion Jetson Orin Nano"). */
function sceneNames(payload: Payload): Map<string, string> {
  const scene = payload?.scene;
  return new Map(scene && "parts" in scene ? scene.parts.map((p) => [p.group.id, p.group.label]) : []);
}

export function useConnectionRows(payload: Payload, lens: ConnLens): ConnRow[] {
  return useMemo(() => (payload?.graph ? connectionRows(payload.graph[lens], lens, sceneNames(payload)) : []), [payload, lens]);
}

/** The wire colour for a sagged fraction of nominal: the theme's go, then warn, then bad. */
export function sagColor(colors: Pick<ThemeColors, "pass" | "warn" | "bad">, sag: number): THREE.Color {
  const heat = sagHeat(sag);
  return heat < 0.5 ? colors.pass.clone().lerp(colors.warn, heat * 2) : colors.warn.clone().lerp(colors.bad, (heat - 0.5) * 2);
}
const cssSag = (sag: number) => {
  const heat = sagHeat(sag);
  return heat < 0.5 ? `color-mix(in srgb, var(--glow-warn) ${heat * 200}%, var(--go))` : `color-mix(in srgb, var(--glow-bad) ${(heat - 0.5) * 200}%, var(--glow-warn))`;
};

/** True while the person asks their system for less motion; follows a change while open. */
function useReducedMotion(): boolean {
  const query = () => window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const [reduce, setReduce] = useState(() => !!query()?.matches);
  useEffect(() => {
    const q = query();
    const on = () => setReduce(q!.matches);
    q?.addEventListener?.("change", on);
    return () => q?.removeEventListener?.("change", on);
  }, []);
  return reduce;
}

/** What the wire colours and the moving dashes mean; only the rails the payload swept are named. */
function SagLegend({ rows }: { rows: SagRow[] }) {
  return (
    <div className="conn-legend" role="group" aria-label="Wire colours">
      <p>Wire colour: the volts the servos get when they all pull as hard as they can, against what the rail should give.</p>
      <span className="conn-scale" aria-hidden="true" />
      <p className="conn-scale-ends"><span>Full volts</span><span>A quarter low</span></p>
      <p>Moving dashes: more current, faster.</p>
      <ul>
        {rows.map((r) => {
          const text = `${+r.volts.toFixed(2)} V of ${+r.nominal.toFixed(2)} V (${Math.round(r.sag * 100)}%)`;
          return (
            <li key={r.id} tabIndex={0} title={text} aria-label={`${r.name}: ${text}`}>
              <i className="conn-swatch" style={{ background: cssSag(r.sag) }} aria-hidden="true" />
              <span>{r.name}</span>
              <small>{text}</small>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** LEFT, while a connection view is on. `drawn`: ids the robot can light; a row with none
 * under it is plain text, never a button that does nothing. */
export function ConnectionsPanel({ payload, lens, drawn }: { payload: Payload; lens: ConnLens; drawn: ReadonlySet<string> }) {
  const rows = useConnectionRows(payload, lens);
  const sags = useMemo(() => (lens === "power" && payload?.graph ? sagRows(payload.graph.power, sceneNames(payload)) : []), [payload, lens]);
  const selected = useSelectedPartId();
  const group = useSelectedGroup();
  useEffect(() => () => setDesignView({ hover: null }), []);
  const pick = (id: string, ids: string[]) => (ids.length === 1 ? setSelectedPartId(ids[0]!) : setSelectedGroup({ id, members: ids }));
  return (
    <Panel title="Connections">
      {rows.length === 0 ? <p className="conn-empty">{EMPTY[lens]}</p> : (
        <ul className="kit-tree conn-tree" onMouseLeave={() => setDesignView({ hover: null })}>
          {rows.map((r) => {
            const lit = r.members.filter((id) => drawn.has(id));
            const on = lit.length > 0 && (group ? group.id === r.id : selected === lit[0] && lit.length === 1);
            const body = (
              <>
                <span>{r.name}</span>
                <small className={r.state ? `kit-s-${r.state}` : undefined}>{r.meta}</small>
                {r.frac !== undefined && <i className={`conn-bar${r.state === "bad" ? " is-over" : ""}`} style={{ ["--f" as string]: Math.min(r.frac, 1) }} aria-hidden="true" />}
              </>
            );
            const pad = { paddingLeft: `calc(var(--sp-2) + ${r.depth} * var(--sp-3))` };
            return (
              <li key={r.id}>
                {lit.length ? (
                  <button type="button" className={`kit-tree-row conn-row${on ? " is-selected" : ""}`} aria-pressed={on} data-conn-id={r.id} style={pad}
                    onClick={() => pick(r.id, lit)} onMouseEnter={() => setDesignView({ hover: lit })} onFocus={() => setDesignView({ hover: lit })} onBlur={() => setDesignView({ hover: null })}>{body}</button>
                ) : <div className="kit-tree-row conn-row is-plain" data-conn-id={r.id} style={pad}>{body}</div>}
              </li>
            );
          })}
        </ul>
      )}
      {sags.length > 0 && <SagLegend rows={sags} />}
    </Panel>
  );
}

/** Mounted inside the RobotView while a connection view is on. Tubes live in their own group on
 * the scene (not `ctx.meshes`), so picking and outlines never see them; every change asks for
 * one frame (the canvas renders on demand). */
export function ConnectionLines({ payload, lens }: { payload: Payload; lens: ConnLens }) {
  const ctx = useScene();
  useViewState();
  const { hover } = useDesignView();
  const selected = useSelectedPartId();
  const group = useSelectedGroup();
  const rows = useConnectionRows(payload, lens);
  const active = useMemo(() => new Set(hover ?? group?.members ?? (selected ? [selected] : [])), [hover, group, selected]);
  const graph = payload?.graph?.[lens];
  const reduce = useReducedMotion();
  const tubes = useMemo(() => {
    if (!ctx.meshes?.length || !graph) return [];
    const boxes = new Map<string, THREE.Box3>();
    const all = new THREE.Box3();
    for (const m of ctx.meshes) {
      const id = m.userData.partId as string | undefined;
      if (!id) continue;
      const b = new THREE.Box3().setFromObject(m);
      all.union(b);
      boxes.set(id, (boxes.get(id) ?? new THREE.Box3()).union(b));
    }
    const centre = (id: string) => boxes.get(id)?.getCenter(new THREE.Vector3()).toArray() as Vec3 | undefined;
    return connectionTubes(graph, centre, Math.max(...all.getSize(new THREE.Vector3()).toArray()));
  }, [ctx, graph]);

  useEffect(() => {
    if (!ctx.scene || !tubes.length) return;
    const colors = readThemeColors();
    const root = new THREE.Group();
    const flowing: { map: THREE.Texture; rate: number }[] = [];
    for (const t of tubes) {
      const a = new THREE.Vector3(...t.a), b = new THREE.Vector3(...t.b);
      const lift = a.distanceTo(b) * 0.25;
      const curve = new THREE.QuadraticBezierCurve3(a, a.clone().lerp(b, 0.5).add(new THREE.Vector3(0, 0, lift)), b);
      const lit = active.size === 0 || active.has(t.to);
      // An overloaded rail stays red; a swept rail takes the sag scale (the payload's figure, not pose); the rest keep the accent.
      const color = t.over ? colors?.bad ?? new THREE.Color("#ef4444") : t.sag !== undefined && colors ? sagColor(colors, t.sag) : colors?.accent ?? new THREE.Color("#1560cc");
      for (const [i, [r, opacity]] of ([[t.radius, lit ? 0.95 : 0.3], [t.radius * 1.8, lit ? 0.2 : 0.05]] as const).entries()) {
        const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthTest: false, depthWrite: false });
        if (i === 0 && t.rate !== undefined) {
          // Dashes: an alpha stripe along the tube; moved by `rate` unless the person asked for less motion.
          const map = new THREE.DataTexture(Uint8Array.from([255, 255, 0, 0, 0, 0, 0, 0].flatMap((g) => [255, g, 255, 255])), 8, 1);
          map.wrapS = THREE.RepeatWrapping;
          map.repeat.x = Math.max(2, Math.round(curve.getLength() / (t.radius * 8)));
          map.needsUpdate = true;
          material.alphaMap = map;
          flowing.push({ map, rate: t.rate });
        }
        const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 24, r, 8), material);
        mesh.renderOrder = 10;
        mesh.raycast = () => {};
        root.add(mesh);
      }
    }
    ctx.scene.add(root);
    ctx.needsRender = true;
    let frame = 0, last = 0;
    const tick = (now: number) => {
      for (const f of flowing) f.map.offset.x -= (f.rate * (now - (last || now))) / 1000;
      last = now;
      ctx.needsRender = true;
      frame = requestAnimationFrame(tick);
    };
    if (flowing.length && !reduce) frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      ctx.scene.remove(root);
      root.traverse((o) => { if (o instanceof THREE.Mesh) { o.geometry.dispose(); (o.material as THREE.MeshBasicMaterial).alphaMap?.dispose(); (o.material as THREE.Material).dispose(); } });
      ctx.needsRender = true;
    };
  }, [ctx, tubes, active, reduce]);

  // Labels only where they matter: the overloaded rail, then the lit branch's ends.
  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const getAnchors = (): CalloutAnchor[] => {
    const ends = new Map<string, Vec3>();
    for (const t of tubes) if (t.over && byId.get(t.from)?.state === "bad") ends.set(t.from, t.a);
    for (const t of tubes) if (active.has(t.to) && !ends.has(t.to)) ends.set(t.to, t.b);
    return [...ends].map(([id, at]) => {
      const row = byId.get(id);
      return { id, position: new THREE.Vector3(...at), text: row ? `${row.name} · ${row.meta}` : id, kind: "derived" };
    });
  };
  if (!ctx.camera || !tubes.length) return null;
  return <CalloutLayer poolSize={4} camera={activeCamera(ctx)} viewportWidth={ctx.resolution.x} viewportHeight={ctx.resolution.y} getAnchors={getAnchors} onLabelClick={setSelectedPartId} />;
}
