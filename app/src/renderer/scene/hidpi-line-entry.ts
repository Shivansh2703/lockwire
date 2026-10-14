// Browser-side entry for the HiDPI resolution-uniform settling check (see
// scene-hidpi.e2e.test.ts). Draws a real `LineMaterial`/`Line2` fat line — the consumer
// buildScene's `resolution` uniform exists for — through a plain `WebGLRenderer` at whatever
// `devicePixelRatio` the hidden window was launched with, once per resolution convention, and
// reports how many DEVICE pixels the line's thickness actually occupies. Nothing here is
// reimplemented from LineMaterial/Line2/LineGeometry; only the harness plumbing is new.
import * as THREE from "three";
import { Line2 } from "three/examples/jsm/lines/Line2.js";
import { LineGeometry } from "three/examples/jsm/lines/LineGeometry.js";
import { LineMaterial } from "three/examples/jsm/lines/LineMaterial.js";

import { buildScene } from "./buildScene";

declare global {
  interface Window {
    __runHidpiLineCheck: (
      canvasId: string,
      mode: "css" | "drawing-buffer" | "no-fix",
      linewidth: number,
    ) => number;
    __runHidpiLineCheckViaBuildScene: (canvasId: string, linewidth: number) => number;
  }
}

const CSS_WIDTH = 64;
const CSS_HEIGHT = 64;

window.__runHidpiLineCheck = function (canvasId, mode, linewidth) {
  const canvas = document.getElementById(canvasId) as HTMLCanvasElement;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
  // "no-fix" reproduces the box's actual bug: no setPixelRatio call at all (renderer stays at
  // its 1x default), exactly the pre-fix state buildScene.ts/Canvas.tsx were bounced for.
  if (mode !== "no-fix") renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(CSS_WIDTH, CSS_HEIGHT, false);

  const drawingWidth = renderer.domElement.width;
  const drawingHeight = renderer.domElement.height;

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(
    -CSS_WIDTH / 2,
    CSS_WIDTH / 2,
    CSS_HEIGHT / 2,
    -CSS_HEIGHT / 2,
    0.1,
    10,
  );
  camera.position.z = 5;

  const geometry = new LineGeometry();
  geometry.setPositions([-CSS_WIDTH / 2, 0, 0, CSS_WIDTH / 2, 0, 0]);

  // The two candidate conventions buildScene.handleResize could have chosen between.
  const resolution =
    mode === "css" ? new THREE.Vector2(CSS_WIDTH, CSS_HEIGHT) : new THREE.Vector2(drawingWidth, drawingHeight);

  const material = new LineMaterial({ color: 0xffffff, linewidth, resolution });
  const line = new Line2(geometry, material);
  scene.add(line);

  renderer.render(scene, camera);

  const gl = renderer.getContext();
  const col = new Uint8Array(drawingHeight * 4);
  // A single-pixel-wide column through the horizontal line's x-center, full drawing-buffer
  // height — readPixels operates in DEVICE pixels regardless of CSS size, which is exactly the
  // unit this check needs.
  gl.readPixels(Math.floor(drawingWidth / 2), 0, 1, drawingHeight, gl.RGBA, gl.UNSIGNED_BYTE, col);

  let thickness = 0;
  for (let y = 0; y < drawingHeight; y++) {
    const r = col[y * 4]!;
    const g = col[y * 4 + 1]!;
    const b = col[y * 4 + 2]!;
    if (!(r === 0 && g === 0 && b === 0)) thickness++;
  }

  renderer.dispose();
  return thickness;
};

/** Exercises the REAL `buildScene` (the actual fix location: buildScene.ts's own
 * `setPixelRatio` call) — this is what the mutation bite in the box brief's accept line targets.
 * A probe `Line2` is added to buildScene's own scene/renderer after construction; buildScene
 * itself never gains a Line2 (charter #1-#4 stay untouched, this box's fix is setPixelRatio
 * only). */
window.__runHidpiLineCheckViaBuildScene = function (canvasId, linewidth) {
  const canvas = document.getElementById(canvasId) as HTMLCanvasElement;
  const ctx = buildScene(canvas, [], CSS_WIDTH, CSS_HEIGHT);

  const drawingWidth = ctx.renderer.domElement.width;
  const drawingHeight = ctx.renderer.domElement.height;

  const geometry = new LineGeometry();
  geometry.setPositions([-CSS_WIDTH / 2, 0, 0, CSS_WIDTH / 2, 0, 0]);
  const material = new LineMaterial({ color: 0xffffff, linewidth, resolution: ctx.resolution });
  const line = new Line2(geometry, material);
  ctx.scene.add(line);

  const camera = new THREE.OrthographicCamera(
    -CSS_WIDTH / 2,
    CSS_WIDTH / 2,
    CSS_HEIGHT / 2,
    -CSS_HEIGHT / 2,
    0.1,
    10,
  );
  camera.position.z = 5;

  ctx.renderer.render(ctx.scene, camera);

  const gl = ctx.renderer.getContext();
  const col = new Uint8Array(drawingHeight * 4);
  gl.readPixels(Math.floor(drawingWidth / 2), 0, 1, drawingHeight, gl.RGBA, gl.UNSIGNED_BYTE, col);

  let thickness = 0;
  for (let y = 0; y < drawingHeight; y++) {
    const r = col[y * 4]!;
    const g = col[y * 4 + 1]!;
    const b = col[y * 4 + 2]!;
    if (!(r === 0 && g === 0 && b === 0)) thickness++;
  }

  ctx.renderer.dispose();
  return thickness;
};
