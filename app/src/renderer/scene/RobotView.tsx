/**
 * RobotView: the reusable themed robot canvas. Mesh robots draw their parts; robots with no mesh
 * (QArm) draw a joint/link skeleton from the page payload. Built on `Canvas`/`buildScene`.
 * `highlight` tints parts/joints by level, `focus` pans the camera to ids, `onPick` reports the
 * picked part id (skeleton: the joint name). Selection falls back to the shared store.
 */
import { useEffect, useMemo, type ReactNode } from "react";

import type { RobotJoint, RobotLink } from "../../shared/api.gen";
import { Canvas, useScene } from "./Canvas";
import { primitiveMeshes, syncPrimitives, type SceneHit, type ScenePart, type ViewInset } from "./buildScene";
import type { LensedPart, LensId } from "./lenses";
import * as THREE from "three";
import { applyDim, applyDisplay, applyHighlight, applyIsolate, applyPose, applyTheme, buildSkeleton, focusCamera, readThemeColors, type Highlight } from "./robotScene";
import { disposeJointOverlay, jointOverlay, jointOverlayObject } from "./dock";
import { useSelectedPartId } from "./selectionStore";
import { ghostMeshes } from "./ghost";
import { isPosed, keepIds, useViewState } from "./viewState";

/** A fixed joint has no motor of its own: selecting it lights both links it joins. */
function withLinks(ids: string[], joints: RobotJoint[]): string[] {
  const out = new Set(ids);
  for (const j of joints) if (out.has(j.name) && j.type === "fixed") { out.add(j.parent); out.add(j.child); }
  return [...out];
}

/** CAD-02: the link solids, redrawn from THIS payload every time the links change. */
function Primitives({ links }: { links: RobotLink[] }) {
  const ctx = useScene();
  useEffect(() => {
    const next = primitiveMeshes(links);
    if (!next.length && !ctx.meshes.some((m) => m.userData.primitive)) return; // nothing drawn, nothing to clear
    syncPrimitives(ctx, next);
    applyTheme(ctx);
  }, [ctx, links]);
  return null;
}

/** TEAM-02: the older revision, laid over this one while the version diff is on. Nothing without it. */
function Ghost() {
  const ctx = useScene();
  const { compare } = useViewState();
  useEffect(() => {
    if (!compare) return;
    const meshes = ghostMeshes(compare.ghost);
    for (const m of meshes) ctx.scene.add(m);
    ctx.needsRender = true;
    return () => {
      for (const m of meshes) { ctx.scene.remove(m); m.geometry.dispose(); (m.material as THREE.Material).dispose(); }
      ctx.needsRender = true;
    };
  }, [ctx, compare]);
  return null;
}

const FRAME_COLORS = ["#d64545", "#2f9e57", "#3b78e7"]; // x, y, z: the usual red, green, blue

/** VIEW-1: a small x/y/z triad at every link frame and a line along every moving joint's axis (from the payload's own position and axis, as given). */
function Frames({ joints }: { joints: RobotJoint[] }) {
  const ctx = useScene();
  const view = useViewState();
  const frames = view.frames && !isPosed(view); // zero-pose lines would look current on a posed robot
  useEffect(() => {
    if (!frames || !joints.length) return;
    const box = new THREE.Box3();
    for (const m of ctx.meshes) box.expandByObject(m);
    for (const j of joints) box.expandByPoint(new THREE.Vector3(...(j.position as [number, number, number])));
    const len = Math.max(...box.getSize(new THREE.Vector3()).toArray(), 0.05) * 0.06;
    const g = new THREE.Group();
    g.userData.decor = true; // not the robot: the pixel readers skip it
    const line = (a: THREE.Vector3, b: THREE.Vector3, color: string) => {
      const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color, depthTest: false }));
      l.renderOrder = 10;
      l.raycast = () => {};
      g.add(l);
    };
    for (const j of joints) {
      const p = new THREE.Vector3(...(j.position as [number, number, number]));
      [[1, 0, 0], [0, 1, 0], [0, 0, 1]].forEach((d, i) => line(p, p.clone().addScaledVector(new THREE.Vector3(...d), len), FRAME_COLORS[i]!));
      if (j.type !== "fixed") {
        const ax = new THREE.Vector3(...(j.axis as [number, number, number])).normalize();
        line(p.clone().addScaledVector(ax, -len * 1.6), p.clone().addScaledVector(ax, len * 1.6), "#e8a317");
      }
    }
    ctx.scene.add(g);
    ctx.needsRender = true;
    return () => {
      ctx.scene.remove(g);
      g.traverse((o) => { const l = o as THREE.Line; l.geometry?.dispose(); (l.material as THREE.Material | undefined)?.dispose(); });
      ctx.needsRender = true;
    };
  }, [ctx, frames, joints]);
  return null;
}

/** POSE-1: the scene follows the pose in the view state. A part of a mesh robot rides on the link it names (mount frame, else its node). */
function Pose({ joints, links, parts }: { joints: RobotJoint[]; links: RobotLink[]; parts: unknown[] }) {
  const ctx = useScene();
  const { pose } = useViewState();
  useEffect(() => {
    const own = new Map((parts as { id?: string | null; node?: string; mount_frame?: string | null }[]).map((p) => [p.id, p.mount_frame ?? p.node]));
    applyPose(ctx, joints, pose, (m) => (m.userData.link as string | undefined) ?? (m.userData.primitive ? (m.userData.partId as string) : own.get(m.userData.partId)));
  }, [ctx, joints, links, parts, pose]);
  return null;
}

function Effects({ highlight: given, selected, focus, dim, joints, isolate, parts }: { highlight: Highlight[]; selected: string[]; focus?: string[]; dim: boolean; joints: RobotJoint[]; isolate: string[] | null; parts: unknown }) {
  const ctx = useScene();
  const view = useViewState();
  const posed = isPosed(view);
  // The status colours are zero-pose figures: while posed every part is drawn neutral (a selection still shows).
  const highlight = useMemo(() => (posed ? given.filter((h) => h.level === "selected") : given), [posed, given]);
  const keep = keepIds(isolate, view.only);
  const isoKey = keep?.join("\u0000");
  const hiddenKey = view.hidden.join("\u0000");
  useEffect(() => { applyIsolate(ctx, keep, view.hidden); }, [ctx, isoKey, hiddenKey, parts, highlight, dim]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { applyDisplay(ctx, view); }, [ctx, view.seeThrough, view.wireframe, parts, highlight, dim]); // eslint-disable-line react-hooks/exhaustive-deps
  const selKey = selected.join("\u0000");
  useEffect(() => { applyHighlight(ctx, highlight, withLinks(selected, joints)); applyDim(ctx, dim); }, [ctx, highlight, selKey, dim, joints]); // eslint-disable-line react-hooks/exhaustive-deps
  const focusKey = focus?.join("\u0000");
  useEffect(() => {
    if (focus?.length) focusCamera(ctx, focus);
  }, [ctx, focusKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** CAD-06u: the one selected joint shows its axis and limits; nothing is drawn for any other state. */
// POSE-1-fix: hidden while posed, because it is drawn at the zero pose (following the pose is not small).
function JointAxes({ selected, joints }: { selected: string[]; joints: RobotJoint[] }) {
  const ctx = useScene();
  const posed = isPosed(useViewState());
  const key = selected.join("\u0000");
  useEffect(() => {
    if (posed) return;
    const j = selected.length === 1 ? joints.find((x) => x.name === selected[0]) ?? joints.find((x) => x.child === selected[0])/* a picked link shows the joint that turns it */  : undefined;
    const pts = joints.map((x) => x.position);
    const extent = Math.max(0, ...[0, 1, 2].map((k) => Math.max(...pts.map((p) => p[k] ?? 0)) - Math.min(...pts.map((p) => p[k] ?? 0))));
    const ov = j && jointOverlay(j, Math.max(extent * 0.32, 0.04));
    if (!ov) return;
    const obj = jointOverlayObject(ov, readThemeColors()?.accent.getHex() ?? 0x4c9aff, ctx.resolution);
    ctx.scene.add(obj);
    ctx.needsRender = true;
    return () => { disposeJointOverlay(obj); ctx.needsRender = true; };
  }, [ctx, key, joints, posed]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

export function RobotView({
  sceneKey, parts = [], joints = [], links = [], highlight = [], selected, focus, onPick, onDropPart, lens = "off", dim = false, isolate = null, children, viewInset,
}: {
  sceneKey: string;
  parts?: (ScenePart & LensedPart)[];
  joints?: RobotJoint[];
  links?: RobotLink[];
  highlight?: Highlight[];
  selected?: string | string[] | null;
  focus?: string[];
  onPick?: (id: string | null) => void;
  onDropPart?: (part: string, hit: SceneHit | null) => void;
  /** Design lens: dims parts outside it. */
  lens?: LensId;
  /** Docs: the robot at ~40% opacity, grid behind it. */
  dim?: boolean;
  /** GRAPH-1: show only these part ids; every other part is hidden. null shows everything. */
  isolate?: string[] | null;
  /** Mounted inside the canvas (callout layers, capture hooks). */
  children?: ReactNode;
  /** Px under floating UI (tabs, panels): the robot is framed in the rest. */
  viewInset?: ViewInset;
}) {
  const stored = useSelectedPartId();
  const skeleton = useMemo(
    () => (parts.length === 0 && joints.length > 0 ? buildSkeleton(joints, links) : undefined),
    [parts.length, joints, links],
  );
  return (
    <Canvas parts={parts} sceneKey={sceneKey} lens={lens} skeleton={skeleton} onPick={onPick} onDropPart={onDropPart} inset={viewInset}>
      {parts.length === 0 && <Primitives links={links} />}
      <Effects highlight={highlight} selected={[(selected === undefined ? stored : selected) ?? []].flat()} focus={focus} dim={dim} joints={joints} isolate={isolate} parts={parts} />
      <Pose joints={joints} links={links} parts={parts} />
      <Frames joints={joints} />
      <Ghost />
      <JointAxes selected={[(selected === undefined ? stored : selected) ?? []].flat()} joints={joints} />
      {children}
    </Canvas>
  );
}
