import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import type { RobotJoint } from "../../shared/api.gen";
import { arcBasis, disposeJointOverlay, dockCallouts, jointOverlay, jointOverlayObject } from "./dock";

// jsdom has no WebGL; stub only the renderer (same stub as buildScene.test.ts).
vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    constructor(public params: { canvas: HTMLCanvasElement }) {}
    setSize() {}
    setPixelRatio() {}
    getPixelRatio() { return 1; }
    getSize(t: THREE.Vector2) { return t.set(1280, 800); }
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});
const { buildScene, applyViewInset } = await import("./buildScene");
const { zoomToCamera } = await import("./robotScene");
const { measureInset } = await import("../Shell");

function overlaps(a: { x: number; y: number; width: number; height: number }, b: typeof a): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

describe("dockCallouts", () => {
  it("test_the_docking_pass_leaves_no_two_callouts_overlapping", () => {
    const boxes = [
      { id: "a", x: 100, y: 100, width: 120, height: 24 },
      { id: "b", x: 105, y: 105, width: 120, height: 24 }, // deliberately overlapping "a"
      { id: "c", x: 108, y: 108, width: 120, height: 24 }, // overlaps both once nudged
    ];
    const docked = dockCallouts(boxes, { width: 800, height: 600 });
    for (let i = 0; i < docked.length; i++) {
      for (let j = i + 1; j < docked.length; j++) {
        expect(overlaps(docked[i]!, docked[j]!)).toBe(false);
      }
    }
  });

  it("clamps a callout inside the panel instead of letting it hang off the edge", () => {
    const docked = dockCallouts([{ id: "a", x: -50, y: 900, width: 120, height: 24 }], {
      width: 800,
      height: 600,
    });
    expect(docked[0]!.x).toBeGreaterThanOrEqual(0);
    expect(docked[0]!.x + docked[0]!.width).toBeLessThanOrEqual(800);
    expect(docked[0]!.y).toBeGreaterThanOrEqual(0);
  });
});

describe("F zoom (zoomToCamera)", () => {
  const W = 800, H = 800;
  // The real layout: a tall Parts column on the left, a tall right slot holding one short card (ends y=268).
  const rect = (x: number, y: number, w: number, h: number) => () => ({ left: x, top: y, right: x + w, bottom: y + h, width: w, height: h }) as DOMRect;
  function layout(): HTMLElement {
    const root = document.createElement("div");
    root.getBoundingClientRect = rect(0, 0, W, H);
    const slot = (n: string, r: () => DOMRect, child: () => DOMRect) => {
      const s = root.appendChild(document.createElement("div"));
      s.className = `kit-slot-${n}`; s.getBoundingClientRect = r;
      s.appendChild(document.createElement("div")).getBoundingClientRect = child;
    };
    slot("left", rect(12, 100, 221, 568), rect(12, 100, 221, 568));
    slot("right", rect(300, 0, 500, H), rect(308, 100, 480, 168));
    return root;
  }
  const px = (ctx: { camera: THREE.PerspectiveCamera }, v: THREE.Vector3) => { const p = v.project(ctx.camera); return [((p.x + 1) / 2) * W, ((1 - p.y) / 2) * H] as const; };

  it("frames the selection at the centre of the free area under the short card, and inside it", () => {
    const inset = measureInset(layout());
    const free = { l: 233 + 12, r: W - 24, t: 268 + 12, b: H - 60 }; // right of Parts, below the card
    expect(inset).toEqual({ top: free.t, right: W - free.r, bottom: H - free.b, left: free.l });
    const ctx = buildScene(document.createElement("canvas"), [], W, H, [], inset);
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
    mesh.userData.partId = "arm";
    ctx.meshes = [mesh];
    applyViewInset(ctx, inset, W, H);
    zoomToCamera(ctx, ["arm"]);
    ctx.camera.updateMatrixWorld();
    const [cx, cy] = px(ctx, new THREE.Vector3());
    expect(cx).toBeCloseTo((free.l + free.r) / 2, 0);
    expect(cy).toBeCloseTo((free.t + free.b) / 2, 0);
    const corners = [-0.5, 0.5].flatMap((x) => [-0.5, 0.5].flatMap((y) => [-0.5, 0.5].map((z) => px(ctx, new THREE.Vector3(x, y, z)))));
    expect(Math.min(...corners.map((c) => c[0]))).toBeGreaterThanOrEqual(free.l);
    expect(Math.max(...corners.map((c) => c[0]))).toBeLessThanOrEqual(free.r);
    expect(Math.min(...corners.map((c) => c[1]))).toBeGreaterThanOrEqual(free.t);
    expect(Math.max(...corners.map((c) => c[1]))).toBeLessThanOrEqual(free.b);
  });
});

describe("jointOverlay (CAD-06u)", () => {
  const joint = (o: Partial<RobotJoint>): RobotJoint => ({
    name: "elbow", type: "revolute", parent: "a", child: "b", parent_name: "a", child_name: "b",
    position: [0.1, 0.2, 0.3], axis: [0, 1, 0], rpy: [0, 0, Math.PI / 2], lower: -1, upper: 2, effort: 5, ...o,
  } as RobotJoint);
  const ang = (ov: NonNullable<ReturnType<typeof jointOverlay>>, i: number, j: RobotJoint) => {
    const { u, v } = arcBasis(new THREE.Vector3(...(j.axis as [number, number, number])));
    const p = new THREE.Vector3(...ov.arc!.points[i]!).sub(new THREE.Vector3(...(j.position as [number, number, number])));
    return Math.atan2(p.dot(v), p.dot(u));
  };

  it("test_the_axis_arrow_follows_the_robot_frame_axis_of_a_rotated_parent", () => {
    const ov = jointOverlay(joint({}), 0.5)!;
    const d = new THREE.Vector3(...ov.axis.to).sub(new THREE.Vector3(...ov.axis.from));
    expect(d.toArray().map((c) => +c.toFixed(6))).toEqual([0, 0.5, 0]); // not turned again by the parent's rpy
    expect(ov.axis.from).toEqual([0.1, 0.2, 0.3]);
  });

  it("test_the_limit_arc_spans_exactly_lower_to_upper_with_rest_inside", () => {
    const j = joint({});
    const ov = jointOverlay(j, 0.5)!;
    expect(ang(ov, 0, j)).toBeCloseTo(-1, 6);
    expect(ang(ov, ov.arc!.points.length - 1, j)).toBeCloseTo(2, 6);
    const r = new THREE.Vector3(...ov.arc!.rest.from).distanceTo(new THREE.Vector3(...ov.axis.from));
    expect(r).toBeCloseTo(0.5 * 0.7 * 0.85, 6);
  });

  it("test_prismatic_draws_its_travel_and_missing_limits_draw_no_arc", () => {
    const p = jointOverlay(joint({ type: "prismatic", lower: 0, upper: 0.04 }), 0.5)!;
    expect(p.arc).toBeNull();
    expect(p.travel!.to[1] - p.travel!.from[1]).toBeCloseTo(0.04, 6);
    const none = jointOverlay(joint({ lower: null, upper: null }), 0.5)!;
    expect([none.arc, none.travel]).toEqual([null, null]);
    expect(none.axis.to[1]).toBeCloseTo(0.7, 6);
    expect(jointOverlay(joint({ type: "fixed" }), 0.5)).toBeNull();
  });

  it("test_the_overlay_object_is_lines_only_and_disposes_clean", () => {
    const g = jointOverlayObject(jointOverlay(joint({}), 0.5)!, 0xffffff, new THREE.Vector2(1280, 800));
    const holder = new THREE.Scene();
    holder.add(g);
    expect(g.children.length).toBeGreaterThan(5);
    disposeJointOverlay(g);
    expect(holder.children).toHaveLength(0);
  });
});
