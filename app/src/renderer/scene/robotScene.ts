/**
 * Pure helpers behind `RobotView`: the joint/link skeleton for robots without a mesh (QArm),
 * Card.targets -> highlight list, and theme-token painting. Reuses buildScene's camera fit and pick.
 */
import * as THREE from "three";
import { mergeVertices } from "three/examples/jsm/utils/BufferGeometryUtils.js";

import type { Card, Finding, FindingLabel, RobotJoint, RobotLink, Target } from "../../shared/api.gen";
import { type SceneContext } from "./buildScene";
import { posed } from "./collide";

export type Level = "warn" | "bad" | "pass" | "selected";
export interface Highlight { id: string; level: Level; /** whole-robot finding: a faint tint, no outline */ faint?: boolean }
type V3 = [number, number, number];
export interface Skeleton {
  segments: { from: V3; to: V3; joint: string; /** the link this bone rides on when posed */ link: string }[];
  nodes: { id: string; position: V3; axis: V3; link: string }[];
  base: V3 | null;
  /** The root link's name: the pedestal answers to it, so a fixed root joint lights something. */
  baseLink?: string;
  tip: { position: V3; dir: V3; joint: string; link: string } | null;
  radius: number;
}

const v3 = (p: number[]): V3 => [p[0] ?? 0, p[1] ?? 0, p[2] ?? 0];
const dist = (a: V3, b: V3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/** Bone from each joint's parent-link origin to the joint, and joint to child-link origin
 * (zero-length ones skipped); a node per joint; a base at the root link. */
export function buildSkeleton(joints: RobotJoint[], links: RobotLink[]): Skeleton {
  const at = new Map(links.map((l) => [l.name, v3(l.position)]));
  const shaped = new Set(links.filter((l) => l.solids?.length).map((l) => l.name)); // a drawn primitive replaces the bone at that link
  const segments: Skeleton["segments"] = [];
  const nodes: Skeleton["nodes"] = [];
  for (const j of joints) {
    const p = v3(j.position);
    const parent = at.get(j.parent);
    const child = at.get(j.child);
    if (parent && !shaped.has(j.parent) && dist(parent, p) > 1e-6) segments.push({ from: parent, to: p, joint: j.name, link: j.parent });
    if (child && !shaped.has(j.child) && dist(child, p) > 1e-6) segments.push({ from: p, to: child, joint: j.name, link: j.child });
    if (j.type !== "fixed") nodes.push({ id: j.name, position: p, axis: v3(j.axis), link: j.parent });
  }
  const children = new Set(joints.map((j) => j.child));
  const root = joints.find((j) => !children.has(j.parent))?.parent;
  const pts = [...nodes.map((n) => n.position), ...at.values()];
  const extent = Math.max(0, ...[0, 1, 2].map((k) => Math.max(...pts.map((p) => p[k]!)) - Math.min(...pts.map((p) => p[k]!))));
  const parents = new Set(joints.map((j) => j.parent));
  const leaf = joints.find((j) => !parents.has(j.child) && j.type !== "fixed");
  const last = segments.at(-1);
  const tipAt = leaf && (at.get(leaf.child) ?? v3(leaf.position));
  const dir = last ? ([last.to[0] - last.from[0], last.to[1] - last.from[1], last.to[2] - last.from[2]] as V3) : null;
  const len = dir ? Math.hypot(...dir) : 0;
  const tip = leaf && tipAt && dir && len > 0 ? { position: tipAt, dir: dir.map((c) => c / len) as V3, joint: leaf.name, link: leaf.child } : null;
  return { segments, nodes, base: (root && at.get(root)) || null, baseLink: root, tip, radius: Math.max(extent * 0.05, 0.004) };
}

const Y = new THREE.Vector3(0, 1, 0);
const Z = new THREE.Vector3(0, 0, 1);
const tint = () => new THREE.MeshStandardMaterial({ color: 0x8899aa });
const quatFor = (axis: V3 | THREE.Vector3, from = Y) => {
  const a = new THREE.Vector3(...(axis as V3));
  return new THREE.Quaternion().setFromUnitVectors(from, a.lengthSq() > 0 ? a.normalize() : Z);
};

/** Meshes carry `userData.partId` = joint name, so the shared pickPart resolves a joint.
 * Link capsules taper base to tip, joints are motor-housing cylinders on their axis, with a
 * pedestal at the root and a two-finger gripper hint where the chain ends. */
export function skeletonMeshes(sk: Skeleton): THREE.Mesh[] {
  const out: THREE.Mesh[] = [];
  const taper = (i: number, n: number) => sk.radius * (1 - (0.5 * i) / Math.max(n - 1, 1));
  sk.segments.forEach((s, i) => {
    const r = taper(i, sk.segments.length);
    const a = new THREE.Vector3(...s.from);
    const d = new THREE.Vector3(...s.to).sub(a);
    const m = new THREE.Mesh(new THREE.CapsuleGeometry(r, Math.max(d.length() - 2 * r, 1e-4), 6, 20), tint());
    m.position.copy(a).addScaledVector(d, 0.5);
    m.quaternion.setFromUnitVectors(Y, d.normalize());
    m.userData = { partId: s.joint, bone: true, link: s.link }; // pickable, but only joints glow
    out.push(m);
  });
  sk.nodes.forEach((n, i) => {
    const hr = 1.3 * taper(i, sk.nodes.length);
    const q = quatFor(n.axis);
    const m = new THREE.Mesh(new THREE.CylinderGeometry(hr, hr, hr * 2.4, 28), tint());
    m.position.set(...n.position);
    m.quaternion.copy(q);
    m.userData.partId = n.id;
    m.userData.link = n.link;
    const halo = new THREE.Mesh(new THREE.TorusGeometry(hr * 1.3, hr * 0.07, 8, 40), tint());
    halo.position.copy(m.position);
    halo.quaternion.copy(q).multiply(new THREE.Quaternion().setFromUnitVectors(Z, Y)); // torus lies in XY; face the cylinder axis
    halo.userData = { partId: n.id, ring: true, link: n.link };
    // visible until `paint` hides it, so the camera fit leaves room for the halo
    out.push(m, halo);
  });
  if (sk.base) {
    const h = sk.radius * 1.6;
    const ped = new THREE.Mesh(new THREE.CylinderGeometry(sk.radius * 2.3, sk.radius * 2.6, h, 40), tint());
    ped.quaternion.setFromUnitVectors(Y, Z);
    ped.position.set(sk.base[0], sk.base[1], sk.base[2] + h / 2);
    ped.userData = { partId: sk.baseLink, bone: true, link: sk.baseLink };
    out.push(ped);
  }
  if (sk.tip) {
    const r = sk.radius * 0.5;
    const dir = new THREE.Vector3(...sk.tip.dir);
    const side = new THREE.Vector3().crossVectors(dir, Math.abs(dir.z) > 0.9 ? new THREE.Vector3(1, 0, 0) : Z).normalize();
    for (const k of [-1, 1]) {
      const len = r * 3.4;
      const f = new THREE.Mesh(new THREE.BoxGeometry(r * 0.6, len, r * 1.1), tint());
      f.quaternion.setFromUnitVectors(Y, dir);
      f.position.set(...sk.tip.position).addScaledVector(dir, len / 2 + r).addScaledVector(side, k * r * 1.3);
      f.userData = { partId: sk.tip.joint, bone: true, link: sk.tip.link };
      out.push(f);
    }
  }
  return out;
}

/** The scene ids one typed target lights. A joint is itself; a link is the joint that carries it
 * (skeleton robots); and the engine's `parts` are the mesh part ids (a device, a link, or the
 * devices on a rail), so nothing is guessed from text. Ids that match no mesh are inert. */
export function targetIds(t: Target, joints: RobotJoint[]): string[] {
  const own = t.kind === "joint" ? [t.id] : t.kind === "link" ? [joints.find((j) => j.child === t.id)?.name ?? t.id] : [];
  return [...own, ...(t.parts ?? [])];
}

/** Card.targets -> highlights (Check and Run cards). Bad outranks warn. */
export function highlightsFromCards(cards: Card[], joints: RobotJoint[]): Highlight[] {
  const byId = new Map<string, Level>();
  for (const c of cards) {
    if (c.severity !== "violation" && c.severity !== "warning") continue; // not checked: the part stays plain, as in highlightsFromFindings
    const level: Level = c.severity === "violation" ? "bad" : "warn";
    for (const t of c.targets) for (const id of targetIds(t, joints)) if (byId.get(id) !== "bad") byId.set(id, level);
  }
  return [...byId].map(([id, level]) => ({ id, level }));
}

/** Check lights every flagged part at once, at its own level; a picked whole-robot card tints the robot instead.
 * The picked card's parts are the selection (selectedIds), so focus isn't lost. */
export const checkHighlight = (cards: Card[], picked: Card | undefined, all: string[], joints: RobotJoint[]): Highlight[] =>
  picked?.whole ? wholeHighlight(picked.severity, all) : highlightsFromCards(cards, joints);

/** A whole-robot finding (no targets, a bad/warn severity) tints every part faintly in its status colour. */
export function wholeHighlight(severity: string, ids: string[]): Highlight[] {
  const level: Level | null = severity === "violation" ? "bad" : severity === "warning" ? "warn" : null;
  return level ? ids.map((id) => ({ id, level, faint: true })) : [];
}

/** The joints a quantity path names: any "/"-separated segment equal to a joint name, or to a
 * link that some joint carries (that joint's child is the link). Declare paths, plan steps and
 * preflight findings all spell targets this way. Order is first mention, no duplicates. */
export function jointsOfPath(path: string, joints: RobotJoint[]): string[] {
  const out: string[] = [];
  for (const seg of path.split("/")) {
    const id = joints.find((j) => j.name === seg || j.child === seg)?.name;
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

/** `jointsOfPath` plus the scene part ids a path names (mesh robots: "devices/dev.battery/..." lights dev.battery). */
export function partsOfPath(path: string, sceneIds: string[], joints: RobotJoint[]): string[] {
  const segs = path.split("/");
  const out = [...new Set([...segs.filter((x) => sceneIds.includes(x)), ...jointsOfPath(path, joints)])];
  if (!sceneIds.length || out.some((x) => sceneIds.includes(x))) return out;
  // No geometry of its own (an inertial-only link): climb to the nearest parent that has some.
  for (const seg of segs) {
    let link = seg;
    for (let j = joints.find((x) => x.child === link); j; j = joints.find((x) => x.child === link)) {
      link = j.parent;
      if (sceneIds.includes(link)) return [link];
    }
  }
  return out;
}

/** The engine's drawn part for a row whose own link has no geometry (ANYmal inertial links),
 * used when the path walk found nothing drawn. */
export function declarePart(rows: { path: string; part?: string | null }[] | undefined, path: string, found: string[], sceneIds: string[]): string[] {
  const part = rows?.find((r) => r.path === path)?.part;
  return part && sceneIds.includes(part) && !found.some((x) => sceneIds.includes(x)) ? [part] : found;
}

/** Findings (plan / test) -> highlights, same levels as the Check cards. Bad outranks warn. */
export function highlightsFromFindings(findings: Finding[], joints: RobotJoint[], labels: FindingLabel[] = []): Highlight[] {
  const byId = new Map<string, Level>();
  findings.forEach((f, i) => {
    if (f.severity !== "violation" && f.severity !== "warning") return; // not scored: the row is grey, so the part stays plain
    const level: Level = f.severity === "violation" ? "bad" : "warn";
    // The engine's typed targets when it sent them; the path walk only for an older sidecar.
    const ids = labels[i]
      ? labels[i].targets.flatMap((t) => targetIds(t, joints))
      : jointsOfPath(f.quantity, joints);
    for (const id of ids) if (byId.get(id) !== "bad") byId.set(id, level);
  });
  return [...byId].map(([id, level]) => ({ id, level }));
}

/** `warn`/`bad` here are the 3D glow tokens (--glow-*), not the text colours. */
export interface ThemeColors { canvas: THREE.Color; grid: THREE.Color; part: THREE.Color; warn: THREE.Color; bad: THREE.Color; pass: THREE.Color; accent: THREE.Color; outline: THREE.Color; /** opacity of ghosted parts (--ghost), per theme */ ghost?: number; /** opacity of the Docs whole-robot ghost (--fade) */ fade?: number }

/** Tokens from the computed CSS (so light/dark both work); null when no theme is loaded
 * (e2e harness pages) and the scene keeps its default look. */
export function readThemeColors(el: Element = document.documentElement): ThemeColors | null {
  const css = getComputedStyle(el);
  const get = (n: string) => css.getPropertyValue(`--${n}`).trim();
  if (!get("canvas-a")) return null;
  const c = (n: string) => new THREE.Color(get(n));
  return { canvas: c("canvas-a"), grid: c("grid"), part: c("part"), warn: c("glow-warn"), bad: c("glow-bad"), pass: c("go"), accent: c("accent"), outline: c("outline"), ghost: parseFloat(get("ghost")) || DIM, fade: parseFloat(get("fade")) || undefined };
}

/** Paint every mesh from its `userData.level` (undefined = plain part). */
export function paint(ctx: Pick<SceneContext, "meshes"> & { needsRender?: boolean }, colors: ThemeColors | null = readThemeColors()): void {
  ctx.needsRender = true;
  const ghost = colors?.ghost ?? DIM;
  for (const m of ctx.meshes) {
    // Docs dims the whole robot (fade); a selection dims every other part (dim). Transparent
    // parts do not write depth, so the ground grid stays behind the robot.
    const fade = colors?.fade ?? ghost;
    const base = m.userData.fade ? fade : greyed(m) ? OUT_OF_LENS : m.userData.dim ? ghost : 1;
    const opacity = m.userData.seeThrough && !m.userData.ring ? Math.min(base, SEE_THROUGH) : base;
    const mat = m.material as THREE.MeshStandardMaterial;
    if (mat.transparent !== opacity < 1) mat.needsUpdate = true; // the OPAQUE shader define is baked at compile time
    mat.transparent = opacity < 1;
    mat.opacity = opacity;
    mat.depthWrite = opacity === 1 || opacity === ghost || opacity === fade;
    mat.wireframe = !!m.userData.wireframe && !m.userData.ring && !m.userData.bone; // ghosted parts still hide the ones behind them, or dozens of overlapping meshes stack back to solid
  }
  if (!colors) return;
  for (const m of ctx.meshes) {
    const mat = m.material as THREE.MeshStandardMaterial;
    // A status colour stays under selection; a selected part with none is filled accent so a
    // buried or tiny part still reads. Bones/rings never fill.
    const level = m.userData.bone ? undefined : (m.userData.level as "warn" | "bad" | "pass" | undefined);
    const pick = !level && !m.userData.ring && !!m.userData.selected;
    const glow = level ? colors[level] : pick ? colors.accent : null;
    mat.color.copy(glow && m.userData.faint ? colors.part.clone().lerp(glow, FAINT_MIX) : glow ?? (greyed(m) ? colors.part.clone().lerp(colors.canvas, 0.7) : colors.part));
    mat.emissive.copy(glow ?? new THREE.Color(0));
    mat.emissiveIntensity = pick ? 0.6 : glow ? (m.userData.faint ? FAINT : 0.7) : 0;
    if (m.userData.ring) m.visible = (!!m.userData.level || !!m.userData.selected) && !m.userData.isolatedOut;
  }
  setOutlines(ctx.meshes, colors);
  setHalos(ctx.meshes, colors);
}

const HALO_SMALL = 0.04; // a flagged part under this share of the robot's radius gets a halo
// ponytail: 4% keeps a 5 mm caster on a ~0.16 m robot (~3%) and drops arm servo bodies (5-7%); tune here.
const HALO_MIN = 0.06; // halo radius floor, as a share of the robot's radius
/** A flagged part too small to show its glow (a caster under a big body) gets a see-through,
 * always-on-top sphere in its status colour. Non-pickable child, created and disposed with the flag. */
function setHalos(meshes: THREE.Mesh[], colors: ThemeColors): void {
  const ball = (m: THREE.Mesh) => { m.updateWorldMatrix(true, false); return m.geometry.boundingSphere ?? (m.geometry.computeBoundingSphere(), m.geometry.boundingSphere!); };
  const world = (m: THREE.Mesh) => ball(m).clone().applyMatrix4(m.matrixWorld);
  const flagged = (m: THREE.Mesh) => !m.userData.ring && !m.userData.bone && !m.userData.faint && (m.userData.level === "warn" || m.userData.level === "bad");
  const box = new THREE.Box3();
  if (meshes.some(flagged)) for (const m of meshes) if (!m.userData.ring) box.union(world(m).getBoundingBox(new THREE.Box3()));
  const robotR = box.getBoundingSphere(new THREE.Sphere()).radius;
  for (const m of meshes) {
    const old = m.userData.halo as THREE.Mesh | undefined;
    const s = flagged(m) ? world(m) : null;
    if (!s || s.radius >= robotR * HALO_SMALL) {
      if (old) { m.remove(old); (old.material as THREE.Material).dispose(); old.geometry.dispose(); delete m.userData.halo; }
      continue;
    }
    const halo = old ?? new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.3, depthTest: false }));
    if (!old) {
      halo.raycast = () => {};
      halo.renderOrder = 999;
      halo.userData.outlineHull = true; // not the robot: kept out of the camera fit
      m.userData.halo = halo;
      m.add(halo);
    }
    (halo.material as THREE.MeshBasicMaterial).color.copy(colors[m.userData.level as "warn" | "bad"]);
    halo.position.copy(ball(m).center);
    halo.scale.setScalar(Math.max(s.radius * 1.5, robotR * HALO_MIN) / Math.max(m.getWorldScale(new THREE.Vector3()).x, 1e-6));
  }
}

const OUTLINE_FRACTION = 0.003; // hull thickness as a share of the robot's longest side
/** Hull geometry with smoothed normals: weld coincident vertices (position only) and recompute, so
 * hard-edged meshes don't tear into spikes and notches when inflated. */
function smoothHullGeometry(src: THREE.BufferGeometry): THREE.BufferGeometry {
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", src.getAttribute("position"));
  if (src.index) g.setIndex(src.index);
  const merged = mergeVertices(g, 1e-5);
  merged.computeVertexNormals();
  return merged;
}
const hullMaterial = (color: THREE.Color) =>
  new THREE.ShaderMaterial({
    uniforms: { color: { value: color.clone() }, t: { value: 0 } },
    vertexShader: "uniform float t; void main(){ gl_Position = projectionMatrix * modelViewMatrix * vec4(position + normalize(normal) * t, 1.0); }",
    fragmentShader: "uniform vec3 color; void main(){ gl_FragColor = vec4(color, 1.0); }",
    side: THREE.BackSide,
  });

/** A light inverted-hull outline on every lit part (warn, bad, selected) so it separates from its
 * neighbours and the grey body. The hull is a non-pickable child (pick only tests `ctx.meshes`),
 * created on highlight and disposed when the highlight goes. */
function setOutlines(meshes: THREE.Mesh[], colors: ThemeColors): void {
  const lit = (m: THREE.Mesh) => !m.userData.ring && (!!m.userData.selected || (!m.userData.bone && !!m.userData.level && !m.userData.faint));
  const box = new THREE.Box3();
  if (meshes.some(lit)) for (const m of meshes) box.expandByObject(m);
  const world = Math.max(...box.getSize(new THREE.Vector3()).toArray(), 0) * OUTLINE_FRACTION;
  // A sheet thinner than the hull (a propeller blade) is all hull: it takes the fill colour, not the darker rim.
  const thin = (m: THREE.Mesh) => Math.min(...new THREE.Box3().setFromObject(m).getSize(new THREE.Vector3()).toArray()) < world * 2;
  for (const m of meshes) {
    const old = m.userData.outline as THREE.Mesh | undefined;
    if (!lit(m)) {
      if (old) { m.remove(old); (old.material as THREE.Material).dispose(); old.geometry.dispose(); delete m.userData.outline; }
      continue;
    }
    const hull = old ?? new THREE.Mesh(smoothHullGeometry(m.geometry), hullMaterial(colors.outline));
    if (!old) {
      hull.raycast = () => {};
      hull.userData.outlineHull = true;
      m.userData.outline = hull;
      m.add(hull);
    }
    const u = (hull.material as THREE.ShaderMaterial).uniforms;
    // One grammar: a 1-2 px rim, a darker shade of the part's own fill (status token, else accent).
    const lv = m.userData.bone ? undefined : (m.userData.level as "warn" | "bad" | "pass" | undefined);
    const fill = lv ? colors[lv] : colors.accent;
    u.color!.value.copy(thin(m) ? fill : fill.clone().multiplyScalar(RIM_SHADE));
    u.t!.value = world / Math.max(m.getWorldScale(new THREE.Vector3()).x, 1e-6);
  }
}

const OUT_OF_LENS = 0.35;
const SEE_THROUGH = 0.3; // VIEW-1 "See through": every part at this opacity
/** Outside the active view and not lit: drawn light grey so the robot stays recognisable. */
const greyed = (m: THREE.Object3D) => !!m.userData.outLens && !m.userData.selected && !m.userData.level;

/** The lens mount: parts the payload puts in the view keep their colours; the rest stay drawn,
 * greyed (and still pickable). Under "All" a part outside it is hidden, as before. */
export function applyLens(ctx: Pick<SceneContext, "meshes"> & { needsRender?: boolean }, inLens: readonly boolean[], lens: string): void {
  ctx.meshes.forEach((m, i) => {
    if (inLens[i] === undefined) return; // ctx lags parts by one rebuild
    m.visible = (inLens[i] || lens !== "off") && !m.userData.isolatedOut;
    m.userData.outLens = !inLens[i] && lens !== "off";
  });
  paint(ctx);
}
const DIM = 0.35; // ghosted parts while something is selected
const RIM_SHADE = 0.55;
const FAINT = 0.1; // whole-robot tint emissive, far under a single flagged part's 0.7
const FAINT_MIX = 0.5; // whole-robot tint colour mix toward the status token, the same red/amber in both themes

/** The selection wins over a finding's glow; while one is active every other part is dimmed so
 * the selection reads (Check: other findings; Design: the shell around an inner link). */
export function applyHighlight(ctx: SceneContext, highlight: Highlight[], selected?: string | string[] | null): void {
  const levels = new Map(highlight.map((h) => [h.id, h.level]));
  const faint = new Set(highlight.filter((h) => h.faint).map((h) => h.id));
  const sel = new Set(selected ? [selected].flat() : []);
  const live = ctx.meshes.some((m) => sel.has(m.userData.partId));
  for (const m of ctx.meshes) {
    const lv = levels.get(m.userData.partId);
    // Joint-level selection lights the joint cap, not the link capsule that shares its id (a bone only when no cap exists).
    const bare = m.userData.bone && ctx.meshes.some((o) => !o.userData.bone && !o.userData.ring && o.userData.partId === m.userData.partId);
    m.userData.selected = !bare && (sel.has(m.userData.partId) || lv === "selected");
    m.userData.level = lv === "selected" ? undefined : lv;
    m.userData.faint = faint.has(m.userData.partId);
    m.userData.dim = live && !(m.userData.selected);
  }
  paint(ctx);
}

/** Parts not in `ids` are hidden (not dimmed), and so is every id in `hidden`; null/empty bring them back. GRAPH-1 and the View menu both come through here (VIEW-1). */
export function applyIsolate(ctx: Pick<SceneContext, "meshes"> & { needsRender?: boolean }, ids: readonly string[] | null, hidden: readonly string[] = []): void {
  const keep = ids && new Set(ids);
  const gone = new Set(hidden);
  for (const m of ctx.meshes) {
    const out = (!!keep && !keep.has(m.userData.partId)) || gone.has(m.userData.partId);
    m.userData.isolatedOut = out;
    if (out) m.visible = false;
    else if (!m.userData.ring) m.visible = true;
  }
  paint(ctx);
}

/** VIEW-1: see-through and wireframe, as flags `paint` turns into material state. */
export function applyDisplay(ctx: Pick<SceneContext, "meshes"> & { needsRender?: boolean }, mode: { seeThrough: boolean; wireframe: boolean }): void {
  for (const m of ctx.meshes) { m.userData.seeThrough = mode.seeThrough; m.userData.wireframe = mode.wireframe; }
  paint(ctx);
}

/** Docs: the robot ghosts at the shared ghost opacity (--ghost), the grid behind it. */
export function applyDim(ctx: SceneContext, dim: boolean): void {
  for (const m of ctx.meshes) m.userData.fade = dim;
  paint(ctx);
}

/** Background, ground grid and part colours from the tokens; call again on a theme change. */
export function applyTheme(ctx: SceneContext): void {
  ctx.needsRender = true;
  const colors = readThemeColors();
  if (!colors) return;
  ctx.scene.background = colors.canvas;
  // Only the ground grid is rebuilt here; another decor object (the support polygon) is not ours to dispose.
  for (const old of ctx.scene.children.filter((o): o is THREE.GridHelper => o instanceof THREE.GridHelper && !!o.userData.decor)) {
    ctx.scene.remove(old);
    old.dispose();
  }
  const box = new THREE.Box3();
  for (const m of ctx.meshes) box.expandByObject(m);
  if (box.isEmpty()) box.set(new THREE.Vector3(-3, -3, 0), new THREE.Vector3(3, 3, 0)); // Open: no robot yet, the same floor as the other pages
  {
    const size = box.getSize(new THREE.Vector3());
    const grid = new THREE.GridHelper(Math.max(size.x, size.y, 0.1) * 1.8, 16, colors.grid, colors.grid);
    grid.rotation.x = Math.PI / 2; // GridHelper lies in XZ; the robot is Z-up.
    grid.position.set((box.min.x + box.max.x) / 2, (box.min.y + box.max.y) / 2, box.min.z);
    grid.userData.decor = true;
    ctx.scene.add(grid);
  }
  paint(ctx, colors);
}

/** Pan toward the middle of the meshes with these ids, as far as the whole robot still fits the
 * open area (inside the inset, 4% margin). Distance and angle never change, so no zoom-in can
 * crop the arm; the selection stays in frame because the whole robot does. */
export function focusCamera(ctx: SceneContext, ids: string[]): void {
  const box = new THREE.Box3();
  for (const m of ctx.meshes) if (ids.includes(m.userData.partId) && !m.userData.ring) box.expandByObject(m);
  if (box.isEmpty()) return;
  const shift = box.getCenter(new THREE.Vector3()).sub(ctx.controls.target);
  // The robot's corners: each mesh's own world box, so a long arm is not one loose union box.
  const corners: THREE.Vector3[] = [];
  const mb = new THREE.Box3();
  for (const m of ctx.meshes) {
    mb.setFromObject(m);
    for (const x of [mb.min.x, mb.max.x]) for (const y of [mb.min.y, mb.max.y]) for (const z of [mb.min.z, mb.max.z]) corners.push(new THREE.Vector3(x, y, z));
  }
  const { x: w, y: h } = ctx.resolution;
  const i = ctx.inset ?? { top: 0, right: 0, bottom: 0, left: 0 };
  const m = 0.04;
  const lo = new THREE.Vector2(-1 + (2 * i.left) / w, -1 + (2 * i.bottom) / h);
  const hi = new THREE.Vector2(1 - (2 * i.right) / w, 1 - (2 * i.top) / h);
  const padX = (hi.x - lo.x) * m;
  const padY = (hi.y - lo.y) * m;
  const home = ctx.camera.position.clone();
  const out = (t: number) => {
    ctx.camera.position.copy(home).addScaledVector(shift, t);
    ctx.camera.updateMatrixWorld();
    let worst = 0;
    for (const c of corners) {
      const p = c.clone().project(ctx.camera);
      worst = Math.max(worst, lo.x + padX - p.x, p.x - (hi.x - padX), lo.y + padY - p.y, p.y - (hi.y - padY));
    }
    return worst;
  };
  // Corners overstate the silhouette, so the resting view may already read "out" a little:
  // allow no more overshoot than it has now.
  const limit = Math.max(out(0), 0) + 1e-6;
  let t = 1;
  if (out(1) > limit) {
    let a = 0, b = 1;
    for (let k = 0; k < 10; k++) { const mid = (a + b) / 2; if (out(mid) <= limit) a = mid; else b = mid; }
    t = a;
  }
  ctx.camera.position.copy(home);
  ctx.controls.target.addScaledVector(shift, t);
  ctx.camera.position.addScaledVector(shift, t);
  ctx.controls.update();
  ctx.needsRender = true;
}

/** How far the camera sits so a sphere of this radius fills the view with a 30% margin. A zero-size
 * selection (a point) gets a small floor so the camera does not land on it. */
export function framingDistance(radius: number, fovDeg: number): number {
  return (1.3 * Math.max(radius, 0.02)) / Math.sin((fovDeg * Math.PI) / 360);
}

/** F: keep the view direction, centre on the selection, and come close enough that it fills the view. */
export function zoomToCamera(ctx: SceneContext, ids: string[]): void {
  const box = new THREE.Box3();
  for (const m of ctx.meshes) if (ids.includes(m.userData.partId) && !m.userData.ring) box.expandByObject(m);
  if (box.isEmpty()) return;
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const cam = ctx.camera;
  const dir = cam.position.clone().sub(ctx.controls.target).normalize();
  // The open area (inside the panels), not the window: the view offset already centres it, so only its size sets the distance.
  const { x: w, y: h } = ctx.resolution ?? { x: 1, y: 1 };
  const i = ctx.inset ?? { top: 0, right: 0, bottom: 0, left: 0 };
  const half = Math.tan((cam.fov * Math.PI) / 360);
  const tanHalf = Math.min(half * Math.max((h - i.top - i.bottom) / h, 0.2), half * cam.aspect * Math.max((w - i.left - i.right) / w, 0.2));
  const dist = framingDistance(sphere.radius, 2 * Math.atan(tanHalf) * (180 / Math.PI));
  ctx.controls.target.copy(sphere.center);
  cam.position.copy(sphere.center).addScaledVector(dir, dist);
  cam.near = Math.min(cam.near, dist / 10);
  cam.updateProjectionMatrix();
  ctx.controls.update();
  ctx.needsRender = true;
}

/**
 * POSE-1: move every mesh the way its link moves when the joints in `pose` (radians or metres)
 * are turned. Each mesh walks up its link's chain of joints and is moved by collide.ts's `posed`,
 * the same transform the sweep uses, starting from where it was drawn (kept in userData.rest).
 * A mesh with no link, or a joint at zero, stays put; an empty pose puts everything back.
 */
export function applyPose(ctx: SceneContext, joints: RobotJoint[], pose: Record<string, number>, linkOf: (m: THREE.Mesh) => string | undefined): void {
  const byChild = new Map(joints.map((j) => [j.child, j]));
  for (const m of ctx.meshes) {
    const rest = (m.userData.rest ??= { p: m.position.clone(), q: m.quaternion.clone() }) as { p: THREE.Vector3; q: THREE.Quaternion };
    const e = new THREE.Matrix4().makeRotationFromQuaternion(rest.q).elements;
    let s = { position: rest.p.toArray(), rotation: [0, 1, 2].flatMap((r) => [0, 1, 2].map((c) => e[c * 4 + r]!)) };
    let j = byChild.get(linkOf(m) ?? "");
    for (let n = 0; j && n < joints.length; n++, j = byChild.get(j.parent)) if (pose[j.name]) s = posed(s, j, pose[j.name]!);
    const r = s.rotation;
    m.position.fromArray(s.position);
    m.quaternion.setFromRotationMatrix(new THREE.Matrix4().set(r[0]!, r[1]!, r[2]!, 0, r[3]!, r[4]!, r[5]!, 0, r[6]!, r[7]!, r[8]!, 0, 0, 0, 0, 1));
    m.updateMatrixWorld();
  }
  ctx.needsRender = true;
}
