/**
 * Pure scene-graph construction: one `THREE.Mesh` per decoded part, plus the camera, controls
 * and the resize handler that owns the resolution uniform e2-f needs for `Line2`/`LineMaterial`
 * (`three/examples/jsm/lines/LineMaterial` reads `resolution` in screen pixels). No React here —
 * `Canvas.tsx` is the only consumer, so this stays testable under a stubbed renderer.
 *
 * Decoding is never reimplemented here: `decodePartPositions`/`decodePackedVectors`/
 * `decodePackedIndices` (./decode.ts) do it. `translation` on a part is a label-placement
 * origin (docs/wire-format.md) and is never applied to the mesh's geometry or transform.
 */
import { typing } from "../shortcuts";
import { getViewState } from "./viewState";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

import type { RobotLink } from "../../shared/api.gen";
import {
  decodePackedIndices,
  decodePackedVectors,
  decodePartPositions,
  type PackedIndices,
  type PackedVectors,
} from "./decode";

export interface ScenePart {
  /** The payload's own part id (`ScenePart.id` in `api_payload.py`, `str | None`) — carried onto
   * `mesh.userData.partId` unchanged so a pick resolves to the payload's identity, never a mesh
   * index or a name composed here (adjacent: identity comes from the payload). */
  id?: string | null;
  positions: PackedVectors;
  normals?: PackedVectors;
  indices: PackedIndices;
}

/** CSS px of the canvas covered by floating UI; the robot is framed in what is left. */
export interface ViewInset { top: number; right: number; bottom: number; left: number }
const NO_INSET: ViewInset = { top: 0, right: 0, bottom: 0, left: 0 };

/** One framing for every page: the robot is fitted to the same open area whichever panels are up
 * (about 1440x900: tabs on top, parts list left, inspector right, view pill below). */
export const FRAME_INSET: ViewInset = { top: 105, right: 450, bottom: 60, left: 280 };

export interface SceneContext {
  inset?: ViewInset;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  /** GAP-views: made the first time the flat view is switched on; see `activeCamera`. */
  orthoCamera?: THREE.OrthographicCamera;
  controls: OrbitControls;
  renderer: THREE.WebGLRenderer;
  /** Owned exclusively by `handleResize` below — in CSS pixels, matching `LineMaterial`'s
   * expectation (three's own examples). Consumers read it; nobody else writes it. */
  resolution: THREE.Vector2;
  meshes: THREE.Mesh[];
  /** Render on demand: whoever changes what is drawn sets this; Canvas's frame loop draws once and clears it. */
  needsRender: boolean;
}

/** One framing rule: every robot is as wide as 55% of the open width, centred. A tall robot
 * (humanoid) stops at 80% of the open height instead, so it never reaches the tab bar. */
const FIT_FILL_FRACTION = 0.55;
const FIT_HEIGHT_CAP = 0.8;
const LIGHT_SKY = 0xffffff;
const LIGHT_GROUND = 0x8890a0;
const ROBOT_UP = new THREE.Vector3(0, 0, 1);

/** Visible share of the canvas (0..1 per axis) once the inset is taken off. */
interface VisibleRect { w: number; h: number }
const FULL_RECT: VisibleRect = { w: 1, h: 1 };

function visibleRect(inset: ViewInset, width: number, height: number): VisibleRect {
  return {
    w: Math.max((width - inset.left - inset.right) / width, 0.2),
    h: Math.max((height - inset.top - inset.bottom) / height, 0.2),
  };
}

/** Shift the rendered image so the middle of the un-covered area sits at the canvas middle's
 * place: the camera aims at the robot, the view offset slides it into the open area. */
export function applyViewInset(ctx: SceneContext, inset: ViewInset, width: number, height: number): void {
  ctx.needsRender = true;
  ctx.inset = inset;
  const cx = (inset.left + width - inset.right) / 2;
  const cy = (inset.top + height - inset.bottom) / 2;
  const dx = width / 2 - cx;
  const dy = height / 2 - cy;
  if (dx === 0 && dy === 0) ctx.camera.clearViewOffset();
  else ctx.camera.setViewOffset(width, height, dx, dy, width, height);
}

/** Fit the whole robot in the open area (the initial framing, again after the inset changes). */
export function refit(ctx: SceneContext, viewDirection?: readonly [number, number, number]): void {
  ctx.needsRender = true;
  const { x: w, y: h } = ctx.resolution;
  const visible = visibleRect(ctx.inset ?? NO_INSET, w, h);
  fitCameraToBounds(ctx.camera, ctx.controls, ctx.meshes, FIT_FILL_FRACTION, ctx.renderer, ctx.scene, visible, [], viewDirection && new THREE.Vector3(...viewDirection).normalize());
}

function defaultViewDirection(): THREE.Vector3 {
  const elevation = (35 * Math.PI) / 180;
  const azimuth = (45 * Math.PI) / 180;
  return new THREE.Vector3(
    Math.cos(elevation) * Math.cos(azimuth),
    Math.cos(elevation) * Math.sin(azimuth),
    Math.sin(elevation),
  ).normalize();
}

/** Fit the real mesh vertices, rather than a mostly-empty union bounding box. */
function fitCameraToBounds(
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
  meshes: THREE.Mesh[],
  fillFraction: number = FIT_FILL_FRACTION,
  renderer?: THREE.WebGLRenderer,
  scene?: THREE.Scene,
  visible: VisibleRect = FULL_RECT,
  extra: THREE.Vector3[] = [],
  viewDirection?: THREE.Vector3,
): void {
  const box = new THREE.Box3();
  for (const mesh of meshes) box.expandByObject(mesh);
  if (box.isEmpty()) return;

  const center = box.getCenter(new THREE.Vector3());
  const direction = viewDirection ?? defaultViewDirection();
  const right = new THREE.Vector3()
    .crossVectors(ROBOT_UP, direction)
    .normalize();
  const up = new THREE.Vector3().crossVectors(direction, right).normalize();
  const vertex = new THREE.Vector3();
  let halfWidth = 0;
  let halfHeight = 0;

  for (const mesh of meshes) {
    const positions = mesh.geometry.getAttribute("position");
    for (let i = 0; i < positions.count; i++) {
      vertex
        .fromBufferAttribute(positions, i)
        .applyMatrix4(mesh.matrixWorld)
        .sub(center);
      halfWidth = Math.max(halfWidth, Math.abs(vertex.dot(right)));
      halfHeight = Math.max(halfHeight, Math.abs(vertex.dot(up)));
    }
  }
  for (const p of extra) {
    vertex.copy(p).sub(center);
    halfWidth = Math.max(halfWidth, Math.abs(vertex.dot(right)));
    halfHeight = Math.max(halfHeight, Math.abs(vertex.dot(up)));
  }

  const verticalFov = (camera.fov * Math.PI) / 180;
  const horizontalFov =
    2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect);
  let distance = Math.max(
    halfHeight / (FIT_HEIGHT_CAP * visible.h * Math.tan(verticalFov / 2)),
    halfWidth / (fillFraction * visible.w * Math.tan(horizontalFov / 2)),
  );
  const placeCamera = () => {
    camera.position.copy(center).addScaledVector(direction, distance);
    camera.near = Math.max(distance / 100, 0.01);
    camera.far = distance * 100;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
  };

  camera.up.copy(ROBOT_UP);
  placeCamera();

  // The box-based distance is orthographic; perspective makes the near end bigger. Re-measure the
  // projected vertices and rescale until the width (or capped height) hits its target.
  for (let pass = 0; pass < 4; pass++) {
    camera.updateMatrixWorld();
    let lo = Infinity, hi = -Infinity, bottom = Infinity, top = -Infinity;
    for (const mesh of meshes) {
      const positions = mesh.geometry.getAttribute("position");
      for (let i = 0; i < positions.count; i++) {
        vertex.fromBufferAttribute(positions, i).applyMatrix4(mesh.matrixWorld).project(camera);
        lo = Math.min(lo, vertex.x); hi = Math.max(hi, vertex.x);
        bottom = Math.min(bottom, vertex.y); top = Math.max(top, vertex.y);
      }
    }
    for (const p of extra) {
      vertex.copy(p).project(camera);
      lo = Math.min(lo, vertex.x); hi = Math.max(hi, vertex.x);
      bottom = Math.min(bottom, vertex.y); top = Math.max(top, vertex.y);
    }
    const scale = Math.max((hi - lo) / 2 / visible.w / fillFraction, (top - bottom) / 2 / visible.h / FIT_HEIGHT_CAP);
    if (!(scale > 0) || Math.abs(scale - 1) < 0.005) break;
    distance *= scale;
    placeCamera();
  }

  // The analytic fit includes vertices hidden by the current view. Correct it from the actual
  // painted silhouette, but back off whenever the readback might already be clipped.
  if (!renderer || !scene) return;
  try {
    for (let pass = 0; pass < 8; pass++) {
      const measured = measurePaintedSilhouette(renderer, scene, camera, visible, fillFraction);
      if (!measured) return;
      const scale = measured.touchesEdge
        ? 1.08
        : Math.max(measured.fraction, 0.85);
      // Perspective makes the near end of a long robot look bigger than the box predicts, so the
      // silhouette drifts off-centre; pan by the measured offset (fractions of the view).
      const viewHeight = 2 * distance * Math.tan(verticalFov / 2);
      const pan = Math.hypot(measured.offsetX, measured.offsetY);
      center
        .addScaledVector(right, measured.offsetX * viewHeight * camera.aspect)
        .addScaledVector(up, measured.offsetY * viewHeight);
      if (!measured.touchesEdge && Math.abs(scale - 1) < 0.015 && pan < 0.01) return;
      distance *= scale;
      placeCamera();
    }
  } catch {
    // Unit-test renderers and unavailable readback still retain the analytic fit.
  }
}

export interface SilhouetteBounds {
  width: number;
  height: number;
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export function readPaintedSilhouetteBounds(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
): SilhouetteBounds | null {
  const decor: THREE.Object3D[] = [];
  scene.traverse((o) => { if (o.userData.decor || o.userData.outlineHull) decor.push(o); }); // grid and highlight outlines are not the robot
  decor.forEach((o) => (o.visible = false)); // the ground grid is not the robot
  try {
    return readBounds(renderer, scene, camera);
  } finally {
    decor.forEach((o) => (o.visible = true));
  }
}

function readBounds(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
): SilhouetteBounds | null {
  renderer.render(scene, camera);
  const gl = renderer.getContext();
  const { width, height } = gl.canvas as HTMLCanvasElement;
  if (width === 0 || height === 0) return null;

  const row = new Uint8Array(width * 4);
  gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, row);
  const [backgroundR, backgroundG, backgroundB] = row;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (let y = 0; y < height; y++) {
    gl.readPixels(0, y, width, 1, gl.RGBA, gl.UNSIGNED_BYTE, row);
    for (let x = 0; x < width; x++) {
      const i = x * 4;
      const difference =
        Math.abs(row[i]! - backgroundR!) +
        Math.abs(row[i + 1]! - backgroundG!) +
        Math.abs(row[i + 2]! - backgroundB!);
      if (difference <= 12) continue;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
  }
  return maxX < minX ? null : { width, height, minX, maxX, minY, maxY };
}

function measurePaintedSilhouette(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
  visible: VisibleRect = FULL_RECT,
  fillFraction: number = FIT_FILL_FRACTION,
): { fraction: number; touchesEdge: boolean; offsetX: number; offsetY: number } | null {
  const bounds = readPaintedSilhouetteBounds(renderer, scene, camera);
  if (!bounds) return null;
  const { width, height, minX, maxX, minY, maxY } = bounds;
  // The view offset slides the image: the open area's middle is where a centred robot lands.
  const v = camera.view?.enabled ? camera.view : null;
  const homeX = 0.5 - (v ? v.offsetX / v.fullWidth : 0);
  const homeY = 0.5 + (v ? v.offsetY / v.fullHeight : 0);
  return {
    fraction: Math.max((maxX - minX) / width / visible.w / fillFraction, (maxY - minY) / height / visible.h / FIT_HEIGHT_CAP), // 1 = on target
    offsetX: (minX + maxX) / 2 / width - homeX,
    offsetY: (minY + maxY) / 2 / height - homeY, // readPixels rows run bottom-up: +y is up
    touchesEdge:
      minX === 0 || maxX === width - 1 || minY === 0 || maxY === height - 1,
  };
}

/** The one part material: matte with a little sheen, so a face reads as a solid surface under the key light and the shadows. */
export const partMaterial = () => new THREE.MeshStandardMaterial({ color: 0x8899aa, roughness: 0.62, metalness: 0.08 });

/** One mesh for one decoded part (TEAM-02's ghost of an older revision is built through the same door). */
export function partMesh(part: ScenePart): THREE.Mesh {
  const positions = decodePartPositions(part);
  const indices = decodePackedIndices(part.indices);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(Array.from(positions), 3));
  if (part.normals) {
    const normals = decodePackedVectors(part.normals);
    geometry.setAttribute("normal", new THREE.Float32BufferAttribute(Array.from(normals), 3));
  } else {
    geometry.computeVertexNormals();
  }
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));

  const mesh = new THREE.Mesh(geometry, partMaterial());
  mesh.castShadow = mesh.receiveShadow = true;
  mesh.userData.partId = part.id ?? null;
  return mesh;
}

export function buildScene(
  canvas: HTMLCanvasElement,
  parts: ScenePart[],
  width: number,
  height: number,
  extraMeshes: THREE.Mesh[] = [],
  inset: ViewInset = NO_INSET,
): SceneContext {
  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 1000);
  camera.up.copy(ROBOT_UP); // the world is Z-up; fitCameraToBounds never runs on an empty scene
  camera.position.set(3, -3, 1.2); // low and far, so the Open floor recedes to a horizon
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true }) /* Save picture reads the canvas between frames */;
  // Capped at 2: uncapped, a 4x-DPR machine pays 4x the fragments for no visible gain.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height, false);
  if (renderer.shadowMap) { // absent under a stubbed renderer (tests)
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; // zoom stays on for touch pinch; attachWheel takes the wheel first

  const resolution = new THREE.Vector2(width, height);

  // One mesh per decoded part (charter #4) — do not collapse, skip or merge parts here.
  const meshes: THREE.Mesh[] = parts.map((part) => {
    const mesh = partMesh(part);
    scene.add(mesh);
    return mesh;
  });

  // Joint skeleton (no-mesh robots) rides in the same list, so fit, pick and dispose are shared.
  for (const m of extraMeshes) scene.add(m);
  meshes.push(...extraMeshes);

  // Light is neutral by design (not a themed colour): white sky, cool-grey bounce.
  scene.add(new THREE.HemisphereLight(LIGHT_SKY, LIGHT_GROUND, 0.62));
  // A soft studio around the robot: faces pick up a little light from every side, so a flat grey
  // panel has a gradient across it instead of one flat tone. Neutral (no colour cast), and only
  // where there is a real GL context (a stubbed renderer in tests has no capabilities).
  if ((renderer as { capabilities?: unknown }).capabilities) {
    try {
      const studio = new THREE.PMREMGenerator(renderer);
      scene.environment = studio.fromScene(new RoomEnvironment(), 0.04).texture;
      scene.environmentIntensity = 0.42;
      studio.dispose();
    } catch { /* no studio light: the hemisphere and key lights still light the robot */ }
  }
  // The key comes from in front of the home view, high and to the left, and casts the shadows
  // (`aimKeyLight` fits its shadow box to the robot). It is found again by `userData.key`.
  const key = new THREE.DirectionalLight(LIGHT_SKY, 0.95);
  key.position.copy(KEY_FROM);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.userData.key = true;
  scene.add(key, key.target);
  // A soft light from behind: edges facing away from the key still separate from the floor.
  const rim = new THREE.DirectionalLight(LIGHT_SKY, 0.4);
  rim.position.set(5, 10, 7);
  scene.add(rim);

  const ctx: SceneContext = { scene, camera, controls, renderer, resolution, meshes, needsRender: true };
  applyViewInset(ctx, inset, width, height);
  refit(ctx);
  return ctx;
}

const KEY_FROM = new THREE.Vector3(-3.2, -5.5, 8.5);
/** Point the key light at the robot and fit its shadow box around it, with room for the shadow the robot throws on the floor. */
export function aimKeyLight(scene: THREE.Scene, box: THREE.Box3, intensity = 0.95): void {
  const key = scene.children.find((o): o is THREE.DirectionalLight => !!o.userData.key);
  if (!key || box.isEmpty()) return;
  key.intensity = intensity;
  const ball = box.getBoundingSphere(new THREE.Sphere());
  const r = Math.max(ball.radius, 0.05);
  key.target.position.copy(ball.center);
  key.position.copy(ball.center).addScaledVector(KEY_FROM.clone().normalize(), r * 4);
  const cam = key.shadow.camera;
  Object.assign(cam, { left: -r * 2.4, right: r * 2.4, top: r * 2.4, bottom: -r * 2.4, near: r * 0.5, far: r * 8 });
  cam.updateProjectionMatrix();
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = r * 0.006; // about two shadow texels: no banding on faces that graze the light
  key.shadow.radius = 5;
  key.shadow.needsUpdate = true;
}

/** Arrow-key walk: hold the arrows to move camera and target together over the ground plane (Z-up),
 * speed scaled to the framing (camera-to-target distance per second). Keyboard-only; only while the
 * 3D view has focus, never while typing. (W/A/S/D are kept free for later tools.) */
const WASD = { arrowup: [1, 0], arrowdown: [-1, 0], arrowright: [0, 1], arrowleft: [0, -1] } as const;
export function attachWasd(ctx: SceneContext): { step: (dt: number) => void; dispose: () => void } {
  const held = new Set<keyof typeof WASD>();
  const down = (e: KeyboardEvent) => {
    const k = e.key.toLowerCase() as keyof typeof WASD;
    if (!(k in WASD) || e.metaKey || e.ctrlKey || e.altKey || document.activeElement !== ctx.renderer.domElement || typing(document.activeElement)) return;
    e.preventDefault(); // the page must not scroll
    held.add(k);
    ctx.needsRender = true;
  };
  const up = (e: KeyboardEvent) => void held.delete(e.key.toLowerCase() as keyof typeof WASD);
  const clear = () => held.clear();
  window.addEventListener("keydown", down);
  window.addEventListener("keyup", up);
  window.addEventListener("blur", clear);
  const fwd = new THREE.Vector3(), side = new THREE.Vector3(), move = new THREE.Vector3();
  return {
    step(dt) {
      if (!held.size) return;
      fwd.subVectors(ctx.controls.target, ctx.camera.position);
      const dist = fwd.length();
      fwd.z = 0; // ground plane
      if (fwd.lengthSq() < 1e-12) return; // looking straight down: no ground heading
      fwd.normalize();
      side.crossVectors(fwd, ROBOT_UP);
      move.set(0, 0, 0);
      for (const k of held) move.addScaledVector(fwd, WASD[k][0]).addScaledVector(side, WASD[k][1]);
      if (move.lengthSq() === 0) return;
      move.normalize().multiplyScalar(dist * 0.8 * dt);
      ctx.camera.position.add(move);
      ctx.controls.target.add(move);
      ctx.needsRender = true;
    },
    dispose() { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); window.removeEventListener("blur", clear); },
  };
}

/** Trackpad wheel: a pinch (macOS sends it as a wheel with ctrlKey) zooms, a two-finger scroll
 * moves the view with the fingers. A plain mouse wheel moves the view too (CALL: one rule, no device sniffing). */
export function wheelAction(e: Pick<WheelEvent, "deltaX" | "deltaY" | "ctrlKey">): { zoom: number } | { pan: [number, number] } {
  return e.ctrlKey ? { zoom: Math.exp(e.deltaY * 0.01) } : { pan: [-e.deltaX, -e.deltaY] };
}
export function attachWheel(ctx: SceneContext): () => void {
  const el = ctx.renderer.domElement;
  const onWheel = (e: WheelEvent) => {
    e.preventDefault();
    e.stopImmediatePropagation(); // capture phase runs before OrbitControls' own wheel zoom on the canvas
    const a = wheelAction(e);
    if ("zoom" in a) ctx.controls.dollyIn(a.zoom); else ctx.controls.pan(...a.pan);
  };
  el.addEventListener("wheel", onWheel, { passive: false, capture: true });
  return () => el.removeEventListener("wheel", onWheel, { capture: true });
}

/** The ONE place the resolution uniform is written. Owns the camera aspect and the renderer's
 * size too, so a `ResizeObserver` on the container is the single caller. */
export function handleResize(ctx: SceneContext, width: number, height: number): void {
  ctx.needsRender = true;
  ctx.resolution.set(width, height);
  ctx.camera.aspect = width / height;
  if (ctx.inset) applyViewInset(ctx, ctx.inset, width, height);
  ctx.camera.updateProjectionMatrix();
  ctx.renderer.setSize(width, height, false);
}

/** Re-applies the capped device pixel ratio (a window dragged to a different display changes
 * it) at the CSS size the renderer already has, without touching `resolution` — that uniform
 * stays in CSS pixels regardless of DPR (settled by execution, see scene-hidpi.e2e.test.ts). */
export function updatePixelRatio(ctx: SceneContext, ratio: number): void {
  ctx.needsRender = true;
  ctx.renderer.setPixelRatio(Math.min(ratio, 2));
  const size = ctx.renderer.getSize(new THREE.Vector2());
  ctx.renderer.setSize(size.x, size.y, false);
}

/**
 * e2-ss — resolve a pointer pick (NDC coordinates, each in [-1, 1]) against the mounted scene's
 * real meshes via `THREE.Raycaster` (pure geometry math; needs no WebGL context, so this runs
 * headless). Returns the hit mesh's payload part id, or `null` when the pick hits nothing —
 * empty space is a real, distinct result, not an absence of one.
 */
export function pickPart(ctx: SceneContext, ndcX: number, ndcY: number): string | null {
  return pickHit(ctx, ndcX, ndcY)?.id ?? null;
}

/**
 * GAP-views: the camera to draw and pick with. The perspective camera stays the one pose (orbit, fit,
 * view keys all move it); with the flat view on, a real orthographic camera copies that pose and is
 * sized to show what the perspective camera shows at the orbit target, so the framing does not jump.
 */
export function activeCamera(ctx: SceneContext): THREE.PerspectiveCamera | THREE.OrthographicCamera {
  if (!getViewState().ortho) return ctx.camera;
  const p = ctx.camera;
  const o = (ctx.orthoCamera ??= new THREE.OrthographicCamera(-1, 1, 1, -1));
  const halfH = p.position.distanceTo(ctx.controls.target) * Math.tan((p.fov * Math.PI) / 360);
  Object.assign(o, { left: -halfH * p.aspect, right: halfH * p.aspect, top: halfH, bottom: -halfH, near: p.near, far: p.far });
  o.position.copy(p.position);
  o.quaternion.copy(p.quaternion);
  o.up.copy(p.up);
  if (p.view?.enabled) o.setViewOffset(p.view.fullWidth, p.view.fullHeight, p.view.offsetX, p.view.offsetY, p.view.width, p.view.height);
  else o.clearViewOffset();
  o.updateProjectionMatrix();
  o.updateMatrixWorld();
  return o;
}

/** The part under the pointer and the world point the ray meets it at (a drop's mount point). */
export function pickHit(ctx: SceneContext, ndcX: number, ndcY: number): SceneHit | null {
  ctx.camera.updateMatrixWorld(); // the pose may have moved since the last render (refit, orbit); never ray from a stale one
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(new THREE.Vector2(ndcX, ndcY), activeCamera(ctx));
  const hit = raycaster.intersectObjects(ctx.meshes, false)[0];
  const id = hit?.object.userData.partId;
  return hit && typeof id === "string" ? { id, point: hit.point.toArray() } : null;
}

export interface SceneHit { id: string; point: number[] }

/** Free a part's crease lines (`robotScene.setEdges` draws them as a child of the part). */
export function disposeEdges(mesh: THREE.Mesh): void {
  const edges = mesh.userData.edges as THREE.LineSegments<THREE.BufferGeometry, THREE.Material> | undefined;
  if (!edges) return;
  mesh.remove(edges);
  edges.geometry.dispose();
  edges.material.dispose();
  delete mesh.userData.edges;
}

export function disposeScene(ctx: SceneContext): void {
  for (const o of ctx.scene.children.filter((c) => c.userData.decor)) {
    const d = o as Partial<THREE.Mesh<THREE.BufferGeometry, THREE.Material>> & { dispose?: () => void };
    if (d.dispose) d.dispose(); // a helper disposes itself
    else { d.geometry?.dispose(); d.material?.dispose(); } // the floor and its shadow are plain meshes
  }
  ctx.controls.dispose();
  for (const mesh of ctx.meshes) {
    mesh.geometry.dispose();
    (mesh.material as THREE.Material).dispose();
    disposeEdges(mesh);
  }
  ctx.scene.environment?.dispose();
  ctx.renderer.dispose();
}

// ---------------------------------------------------------------- torque headroom and support polygon (CHK-06u)

/** Torque to spare, as the engine sends it (`RobotJoint.headroom`): available / needed. */
export interface HeadroomJoint { name: string; headroom?: { ratio: number } | null }

// ponytail: 2x and 3x are conventions (hobby-servo rule of thumb), not sourced numbers; move them here.
const HEADROOM_BAD = 2;
const HEADROOM_WARN = 3;

/** The glow level for a joint's headroom ratio, never its absolute torque. */
export const headroomLevel = (ratio: number): "bad" | "warn" | "pass" => (ratio < HEADROOM_BAD ? "bad" : ratio < HEADROOM_WARN ? "warn" : "pass");

/** One highlight per joint the engine has headroom for; a joint with none gets no colour. */
export const headroomHighlights = (joints: HeadroomJoint[]): { id: string; level: "bad" | "warn" | "pass" }[] =>
  joints.flatMap((j) => (j.headroom ? [{ id: j.name, level: headroomLevel(j.headroom.ratio) }] : []));

/** The number in the callout, in plain words: "wrist: 1.5x torque to spare". */
export function headroomText(name: string, ratio: number): string {
  const word = name.replace(/_/g, " ");
  const x = ratio >= 100 ? "99x or more" : `${ratio.toFixed(1)}x`;
  if (ratio < 1) return `${word}: only ${x}, cannot hold the pose`;
  return `${word}: ${x} spare torque${ratio < HEADROOM_WARN ? " (rule of thumb)" : ""}`; // the 2x/3x colours are advice, not a sourced limit
}

/** The support polygon as a flat translucent fill and an outline on the ground plane. */
export function supportPolygonObject(poly: { z: number; points: number[][] }, color: THREE.Color): THREE.Group {
  const shape = new THREE.Shape(poly.points.map((p) => new THREE.Vector2(p[0], p[1])));
  const fill = new THREE.Mesh(new THREE.ShapeGeometry(shape), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.3, side: THREE.DoubleSide, depthWrite: false }));
  const edge = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(poly.points.map((p) => new THREE.Vector3(p[0], p[1], 0))), new THREE.LineBasicMaterial({ color }));
  const group = new THREE.Group();
  group.add(fill, edge);
  group.position.z = poly.z;
  group.name = "support-polygon";
  group.userData.decor = true; // not the robot: the pixel readers skip it
  group.traverse((o) => { o.raycast = () => {}; }); // never picked
  return group;
}

// ---------------------------------------------------------------- link primitives (CAD-02)

/** One mesh per link solid. The engine resolves a named dimension to metres on every payload, so
 * call this on every payload: a size edited after load must redraw the link (see `syncPrimitives`). Cylinders run along the
 * shape's own z (URDF). Meshes carry `userData.partId` = the link name, so a pick finds the link. */
export function primitiveMeshes(links: readonly RobotLink[]): THREE.Mesh[] {
  const out: THREE.Mesh[] = [];
  for (const link of links) {
    for (const shape of link.solids ?? []) {
      const [a, b, c] = shape.kind === "box" ? [0, 1, 2].map((i) => shape.size?.[i] ?? NaN) : [shape.radius ?? NaN, shape.length ?? 0, 0];
      if (![a, b, c].every(Number.isFinite)) continue; // a size the engine could not resolve: draw nothing, not a guess
      const geometry = shape.kind === "box" ? new THREE.BoxGeometry(a, b, c)
        : shape.kind === "cylinder" ? new THREE.CylinderGeometry(a, a, b, 32).rotateX(Math.PI / 2)
          : new THREE.SphereGeometry(a, 32, 16);
      const mesh = new THREE.Mesh(geometry, partMaterial());
      mesh.castShadow = mesh.receiveShadow = true;
      const r = shape.rotation;
      mesh.setRotationFromMatrix(new THREE.Matrix4().set(r[0]!, r[1]!, r[2]!, 0, r[3]!, r[4]!, r[5]!, 0, r[6]!, r[7]!, r[8]!, 0, 0, 0, 0, 1));
      mesh.position.set(shape.position[0]!, shape.position[1]!, shape.position[2]!);
      mesh.userData = { partId: link.name, primitive: true };
      out.push(mesh);
    }
  }
  return out;
}

/** Swap the drawn primitives for `next` without rebuilding the scene (the camera stays where the
 * user put it, unless this is the first primitive drawn). */
export function syncPrimitives(ctx: SceneContext, next: THREE.Mesh[]): void {
  const first = !ctx.meshes.some((m) => m.userData.primitive);
  for (const old of ctx.meshes.filter((m) => m.userData.primitive)) {
    ctx.scene.remove(old);
    old.geometry.dispose();
    (old.material as THREE.Material).dispose();
    disposeEdges(old);
  }
  ctx.meshes = ctx.meshes.filter((m) => !m.userData.primitive);
  for (const m of next) ctx.scene.add(m);
  ctx.meshes.push(...next);
  ctx.needsRender = true;
  if (first && next.length) refit(ctx);
}
