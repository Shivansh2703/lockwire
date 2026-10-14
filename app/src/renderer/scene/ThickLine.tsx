// A rail, bus or mechanical link drawn THICK — a real polyline whose width is measured
// in screen pixels, not a browser `LineBasicMaterial` line (always one pixel regardless
// of `linewidth`, plan v8 line 549). Built from `three/addons/lines/` (ships inside
// the already-installed `three` dependency, no new package, no hand-rolled shader).
//
// `resolution` is a shader uniform `LineMaterial` needs to convert `linewidth` (screen
// pixels) correctly; it does NOT track the canvas on its own. `Canvas.tsx`'s
// `ResizeObserver` is the one writer of `SceneContext.resolution` (`buildScene.ts`); the
// caller here must feed that same value in and call `updateThickLineResolution` again on
// every resize, or the line's apparent width drifts the first time the window is dragged —
// exactly the defect v8 names.
import * as THREE from "three";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";

export interface ThickLineOptions {
  /** Flat `[x0,y0,z0, x1,y1,z1, ...]` — world-space, already-decoded positions. */
  points: number[];
  color: number | string;
  linewidth: number;
  /** `SceneContext.resolution` at construction time, in CSS pixels. */
  resolution: THREE.Vector2;
}

/** Builds one thick line. The caller adds it to the scene and keeps the returned `Line2`
 * around so it can pass it to `updateThickLineResolution` on every resize. */
export function createThickLine({ points, color, linewidth, resolution }: ThickLineOptions): Line2 {
  const geometry = new LineGeometry();
  geometry.setPositions(points);
  const material = new LineMaterial({ color, linewidth });
  material.resolution.copy(resolution);
  const line = new Line2(geometry, material);
  line.computeLineDistances();
  return line;
}

/** Call on every canvas resize, for every live `ThickLine` — the only place this uniform
 * is written for a given line. Pass `SceneContext.resolution`'s current x/y straight
 * through; do not re-derive them. */
export function updateThickLineResolution(line: Line2, width: number, height: number): void {
  (line.material as LineMaterial).resolution.set(width, height);
}
