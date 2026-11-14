/**
 * TEAM-02: the older revision as a translucent ghost. Built through `partMesh` (one mesh per part,
 * same decode as the live scene) and added beside the live meshes, never into `ctx.meshes`, so it
 * is not picked, framed or themed. A part that did not move stays the plain neutral grey; only a
 * part that moved or went away takes a colour.
 */
import * as THREE from "three";

import { partMesh } from "./buildScene";
import type { Comparison } from "./viewState";

export const GHOST_NEUTRAL = 0x8899aa;
export const GHOST_MOVED = 0xe8730c;
export const GHOST_REMOVED = 0xb0237a;
const COLOUR = { moved: GHOST_MOVED, removed: GHOST_REMOVED } as const;

export function ghostMeshes(ghost: Comparison["ghost"]): THREE.Mesh[] {
  return ghost.map(({ part, mark }) => {
    const mesh = partMesh(part);
    mesh.material = new THREE.MeshStandardMaterial({
      color: mark ? COLOUR[mark] : GHOST_NEUTRAL, transparent: true, opacity: mark ? 0.45 : 0.18,
      depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1,
    });
    mesh.renderOrder = 5;
    mesh.userData = { decor: true, ghost: mark ?? "same" }; // not the robot: the pixel readers skip it
    mesh.raycast = () => {};
    return mesh;
  });
}
