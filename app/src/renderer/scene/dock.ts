import * as THREE from "three";

import type { RobotJoint } from "../../shared/api.gen";
import { createThickLine } from "./ThickLine";

// The docking pass (plan v8 line 547): keep every callout inside the panel
// and clear of every other callout. Pure geometry — no Canvas, no camera, no
// React — so it needs nothing that is still queued upstream.

export interface CalloutBox {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Panel {
  width: number;
  height: number;
}

function overlaps(a: CalloutBox, b: CalloutBox): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

/** Clamps each box inside the panel, then nudges any box that still overlaps
 * an already-docked one straight down until clear.
 * ponytail: O(n^2) greedy vertical nudge, fine at the label counts a person
 * can actually read on one part count; replace with a real layout solver only
 * if a contract ever produces hundreds of simultaneous callouts. */
export function dockCallouts(boxes: readonly CalloutBox[], panel: Panel): CalloutBox[] {
  const docked: CalloutBox[] = [];
  for (const box of boxes) {
    let next: CalloutBox = {
      ...box,
      x: Math.min(Math.max(box.x, 0), Math.max(panel.width - box.width, 0)),
      y: Math.min(Math.max(box.y, 0), Math.max(panel.height - box.height, 0)),
    };
    let moved = true;
    while (moved) {
      moved = false;
      for (const placed of docked) {
        if (overlaps(next, placed)) {
          next = { ...next, y: placed.y + placed.height };
          moved = true;
        }
      }
    }
    docked.push(next);
  }
  return docked;
}

// ---------------------------------------------------------------- CAD-06u: the selected joint's axis and limits
// Drawn in the robot frame, the same one the scene and the payload use: `position` and `axis` on a
// RobotJoint already have every parent rotation composed in, so nothing here rotates by `rpy` again.

type V3 = [number, number, number];
export interface JointOverlay {
  /** An arrow from the joint origin along its axis. */
  axis: { from: V3; to: V3 };
  /** Revolute with both limits declared: points from lower to upper, radians, plus the rest-angle tick. */
  arc: { points: V3[]; rest: { from: V3; to: V3 } } | null;
  /** Prismatic with both limits declared: the travel, lower end to upper end. */
  travel: { from: V3; to: V3 } | null;
}

/** The two unit vectors spanning the plane the joint turns in; the arc's zero angle is `u`. */
export function arcBasis(axis: THREE.Vector3): { u: THREE.Vector3; v: THREE.Vector3 } {
  const a = axis.clone().normalize();
  const pick = Math.abs(a.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
  const u = new THREE.Vector3().crossVectors(a, pick).normalize();
  return { u, v: new THREE.Vector3().crossVectors(a, u) };
}

/** `size` is the arrow length in metres; the arc radius is 0.7 of it. Null for a joint with no motion. */
export function jointOverlay(j: RobotJoint, size: number): JointOverlay | null {
  if (j.type === "fixed" || j.type === "floating" || j.type === "planar" || j.type === "ball") return null;
  const a = new THREE.Vector3(j.axis[0] ?? 0, j.axis[1] ?? 0, j.axis[2] ?? 0);
  if (a.lengthSq() < 1e-12) return null;
  a.normalize();
  const o = new THREE.Vector3(j.position[0] ?? 0, j.position[1] ?? 0, j.position[2] ?? 0);
  const at = (p: THREE.Vector3): V3 => [p.x, p.y, p.z];
  const out: JointOverlay = { axis: { from: at(o), to: at(o.clone().addScaledVector(a, size)) }, arc: null, travel: null };
  const { lower, upper } = j;
  if (lower == null || upper == null || upper <= lower) return out;
  if (j.type === "prismatic") {
    out.travel = { from: at(o.clone().addScaledVector(a, lower)), to: at(o.clone().addScaledVector(a, upper)) };
  } else if (j.type === "revolute") {
    const { u, v } = arcBasis(a);
    const r = size * 0.7;
    const on = (t: number, k = 1) => o.clone().addScaledVector(u, Math.cos(t) * r * k).addScaledVector(v, Math.sin(t) * r * k);
    const steps = Math.max(8, Math.ceil(((upper - lower) / (Math.PI * 2)) * 64));
    const points = Array.from({ length: steps + 1 }, (_, i) => at(on(lower + ((upper - lower) * i) / steps)));
    const rest = Math.min(Math.max(0, lower), upper); // the rest pose is angle 0, kept inside the limits
    out.arc = { points, rest: { from: at(on(rest, 0.85)), to: at(on(rest, 1.15)) } };
  }
  return out;
}

/** Lines for one overlay, coloured from the theme accent. Call `disposeJointOverlay` when done. */
export function jointOverlayObject(ov: JointOverlay, color: number, resolution: THREE.Vector2): THREE.Group {
  const g = new THREE.Group();
  g.userData.jointOverlay = true;
  const line = (pts: V3[], linewidth: number) => {
    const l = createThickLine({ points: pts.flat(), color, linewidth, resolution });
    l.material.depthTest = false; // readable through the link it sits inside
    l.renderOrder = 10;
    g.add(l);
  };
  line([ov.axis.from, ov.axis.to], 3);
  // arrow head: two short barbs back from the tip
  const from = new THREE.Vector3(...ov.axis.from);
  const tip = new THREE.Vector3(...ov.axis.to);
  const back = from.clone().sub(tip).multiplyScalar(0.18);
  const { u, v } = arcBasis(tip.clone().sub(from));
  for (const s of [u, v]) for (const k of [-1, 1]) line([ov.axis.to, tip.clone().add(back).addScaledVector(s, back.length() * 0.5 * k).toArray() as V3], 3);
  if (ov.arc) {
    line(ov.arc.points, 2.5);
    line([ov.arc.rest.from, ov.arc.rest.to], 4);
  }
  if (ov.travel) line([ov.travel.from, ov.travel.to], 5);
  return g;
}

export function disposeJointOverlay(g: THREE.Object3D): void {
  g.traverse((o) => {
    const m = o as THREE.Mesh;
    if (m.geometry) { m.geometry.dispose(); (m.material as THREE.Material).dispose(); }
  });
  g.removeFromParent();
}
