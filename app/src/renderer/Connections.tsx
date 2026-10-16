/**
 * The Design page's connection views: `ConnectionsPanel` (left, the tree of what feeds, talks to
 * or carries what) and `ConnectionLines` (inside the RobotView: one glowing tube per connection
 * between the two parts, thicker = more watts / bandwidth / mass, red off an overloaded rail).
 * Rows and tubes come from `scene/connections.ts`; selection is the shared store, hover is
 * `designView.hover` (lights without moving the camera).
 */
import { useEffect, useMemo } from "react";
import * as THREE from "three";

import type { CheckPayload, PagePayload } from "../shared/api.gen";
import { Panel } from "./kit";
import { useScene } from "./scene/Canvas";
import { CalloutLayer, type CalloutAnchor } from "./scene/CalloutLayer";
import { connectionRows, connectionTubes, type ConnLens, type ConnRow, type Vec3 } from "./scene/connections";
import { setDesignView, useDesignView } from "./scene/designView";
import { readThemeColors } from "./scene/robotScene";
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

/** LEFT, while a connection view is on. `drawn`: ids the robot can light; a row with none
 * under it is plain text, never a button that does nothing. */
export function ConnectionsPanel({ payload, lens, drawn }: { payload: Payload; lens: ConnLens; drawn: ReadonlySet<string> }) {
  const rows = useConnectionRows(payload, lens);
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
    </Panel>
  );
}

/** Mounted inside the RobotView while a connection view is on. Tubes live in their own group on
 * the scene (not `ctx.meshes`), so picking and outlines never see them; every change asks for
 * one frame (the canvas renders on demand). */
export function ConnectionLines({ payload, lens }: { payload: Payload; lens: ConnLens }) {
  const ctx = useScene();
  const { hover } = useDesignView();
  const selected = useSelectedPartId();
  const group = useSelectedGroup();
  const rows = useConnectionRows(payload, lens);
  const active = useMemo(() => new Set(hover ?? group?.members ?? (selected ? [selected] : [])), [hover, group, selected]);
  const graph = payload?.graph?.[lens];
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
    for (const t of tubes) {
      const a = new THREE.Vector3(...t.a), b = new THREE.Vector3(...t.b);
      const lift = a.distanceTo(b) * 0.25;
      const curve = new THREE.QuadraticBezierCurve3(a, a.clone().lerp(b, 0.5).add(new THREE.Vector3(0, 0, lift)), b);
      const lit = active.size === 0 || active.has(t.to);
      const color = t.over ? colors?.bad ?? new THREE.Color("#ef4444") : colors?.accent ?? new THREE.Color("#1560cc");
      for (const [r, opacity] of [[t.radius, lit ? 0.95 : 0.3], [t.radius * 1.8, lit ? 0.2 : 0.05]] as const) {
        const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 24, r, 8), new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthTest: false, depthWrite: false }));
        mesh.renderOrder = 10;
        mesh.raycast = () => {};
        root.add(mesh);
      }
    }
    ctx.scene.add(root);
    ctx.needsRender = true;
    return () => {
      ctx.scene.remove(root);
      root.traverse((o) => { if (o instanceof THREE.Mesh) { o.geometry.dispose(); (o.material as THREE.Material).dispose(); } });
      ctx.needsRender = true;
    };
  }, [ctx, tubes, active]);

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
  return <CalloutLayer poolSize={4} camera={ctx.camera} viewportWidth={ctx.resolution.x} viewportHeight={ctx.resolution.y} getAnchors={getAnchors} onLabelClick={setSelectedPartId} />;
}
