import { describe, expect, it } from "vitest";

import type { LinkSolid, PagePayload } from "../../shared/api.gen";
import { checkPair, findInterference, sweepJoints } from "./collide";

const I = [1, 0, 0, 0, 1, 0, 0, 0, 1];
const rotZ = (deg: number) => { const c = Math.cos((deg * Math.PI) / 180), s = Math.sin((deg * Math.PI) / 180); return [c, -s, 0, s, c, 0, 0, 0, 1]; };
const rotX = (deg: number) => { const c = Math.cos((deg * Math.PI) / 180), s = Math.sin((deg * Math.PI) / 180); return [1, 0, 0, 0, c, -s, 0, s, c]; };
const rotY = (deg: number) => { const c = Math.cos((deg * Math.PI) / 180), s = Math.sin((deg * Math.PI) / 180); return [c, 0, s, 0, 1, 0, -s, 0, c]; };
const mul = (a: number[], b: number[]) => [0, 1, 2].flatMap((i) => [0, 1, 2].map((j) => [0, 1, 2].reduce((t, k) => t + a[3 * i + k]! * b[3 * k + j]!, 0)));
const box = (size: number[], position: number[], rotation = I): LinkSolid => ({ kind: "box", size, position, rotation });
const ball = (radius: number, position: number[]): LinkSolid => ({ kind: "sphere", radius, position, rotation: I });
const cyl = (radius: number, length: number, position: number[]): LinkSolid => ({ kind: "cylinder", radius, length, position, rotation: I });

describe("checkPair", () => {
  it("two aligned boxes overlapping by half report the exact overlap volume", () => {
    const r = checkPair(box([1, 1, 1], [0, 0, 0]), box([1, 1, 1], [0.5, 0, 0]));
    expect(r.overlap).toBe(true);
    expect(r.volume).toBeCloseTo(0.5, 9);
    expect(r.volumeExact).toBe(true);
  });
  it("aligned boxes apart on a diagonal report the exact gap", () => {
    const r = checkPair(box([1, 1, 1], [0, 0, 0]), box([1, 1, 1], [4, 4, 0]));
    expect(r.overlap).toBe(false);
    expect(r.gap).toBeCloseTo(3 * Math.SQRT2, 9); // 3 m apart in x and in y
    expect(r.gapExact).toBe(true);
  });
  it("a rotated bar whose bounding box overlaps the cube but which does not touch it is apart", () => {
    // bar 2 x 0.2 x 0.2 turned 45 degrees, centred (1.2, 1.2): bounding boxes overlap in [0.42, 0.5]
    const r = checkPair(box([1, 1, 1], [0, 0, 0]), box([2, 0.2, 0.2], [1.2, 1.2, 0], rotZ(-45)));
    expect(r.overlap).toBe(false);
    expect(r.gap).toBeCloseTo(1.4 / Math.SQRT2 - 0.1, 6); // separating axis along the bar's width
    expect(r.gapExact).toBe(false); // a lower bound, said so
  });
  it("a tilted thin bar is separated from the cube only by an edge-against-edge axis", () => {
    // bar 2 x 0.1 x 0.1 under Rz(45) Ry(30) Rx(30) at (0.5, 0.75, 0.5): the six face axes all overlap
    // (0.109 deep on the worst), the nine edge pairs separate it by 0.089 m; sampled by hand, true gap 0.0890
    const r = checkPair(box([1, 1, 1], [0, 0, 0]), box([2, 0.1, 0.1], [0.5, 0.75, 0.5], mul(rotZ(45), mul(rotY(30), rotX(30)))));
    expect(r.overlap).toBe(false);
    expect(r.gap!).toBeCloseTo(0.089, 3);
  });
  it("a unit cube and the same cube turned 45 degrees overlap by about 0.828 and say it is an estimate", () => {
    const r = checkPair(box([1, 1, 1], [0, 0, 0]), box([1, 1, 1], [0, 0, 0], rotZ(45)));
    expect(r.overlap).toBe(true);
    expect(r.volume!).toBeGreaterThan(0.79);
    expect(r.volume!).toBeLessThan(0.86);
    expect(r.volumeExact).toBe(false);
  });
  it("two spheres give the exact lens volume and the exact gap", () => {
    expect(checkPair(ball(1, [0, 0, 0]), ball(1, [1, 0, 0])).volume).toBeCloseTo((Math.PI * 5) / 12, 9);
    const apart = checkPair(ball(1, [0, 0, 0]), ball(1, [3, 0, 0]));
    expect([apart.overlap, apart.gap, apart.gapExact]).toEqual([false, 1, true]);
  });
  it("a sphere against a box: gap is to the nearest face", () => {
    expect(checkPair(box([1, 1, 1], [0, 0, 0]), ball(1, [2, 0, 0])).gap).toBeCloseTo(0.5, 9);
  });
  it("a cylinder is checked as its bounding box and marked approximate", () => {
    const r = checkPair(cyl(0.5, 1, [0, 0, 0]), box([1, 1, 1], [0.5, 0, 0]));
    expect([r.overlap, r.approximate, r.volumeExact]).toEqual([true, true, false]);
  });
});

const link = (name: string, ...solids: LinkSolid[]) => ({ name, position: [0, 0, 0], solids });
const page = (links: ReturnType<typeof link>[], collision_pairs: { link1: string; link2: string; reason: string }[] = []) =>
  ({ links, collision_pairs, joints: [{ parent: "base", child: "arm" }] }) as unknown as PagePayload;
const overlapping = [link("base", box([1, 1, 1], [0, 0, 0])), link("arm", box([1, 1, 1], [0.5, 0, 0])), link("hand", box([1, 1, 1], [0.5, 0, 0]))];

describe("findInterference", () => {
  it("flags overlapping links but not a pair joined by a joint", () => {
    const r = findInterference(page(overlapping));
    expect(r.overlaps.map((f) => [f.a, f.b].join("+")).sort()).toEqual(["arm+hand", "base+hand"]);
  });
  it("an ignored pair stays quiet, in either order, and is listed with its reason", () => {
    const r = findInterference(page(overlapping, [{ link1: "hand", link2: "base", reason: "bolted together" }]));
    expect(r.overlaps.map((f) => [f.a, f.b].join("+"))).toEqual(["arm+hand"]);
    expect(r.ignored[0]!.reason).toBe("bolted together");
  });
  it("a pair closer than the clearance is listed as close, one farther is not", () => {
    const r = findInterference(page([link("a", box([1, 1, 1], [0, 0, 0])), link("b", box([1, 1, 1], [1.003, 0, 0])), link("c", box([1, 1, 1], [5, 0, 0]))]));
    expect(r.close.map((f) => [f.a, f.b].join("+"))).toEqual(["a+b"]);
  });
  it("a link with no primitives is left out", () => {
    expect(findInterference(page([link("a", box([1, 1, 1], [0, 0, 0])), link("b")])).overlaps).toEqual([]);
  });
});

// A 1 m bar hinged at the origin about z, turned from 0 to `upper` degrees. A 1 cm ball (the elbow's
// stop) sits 0.8 m out at 120 degrees: (-0.4, 0.6928). The bar is 2 cm wide, so it touches the ball only
// within about 1.4 degrees of 120, and the 5 degree steps (36 over 180) land on 120 exactly.
const hinge = (upper: number, pairs: { link1: string; link2: string; reason: string }[] = [], extra: object[] = []) =>
  ({
    links: [link("base"), link("arm", box([1, 0.02, 0.02], [0.5, 0, 0])), link("post", ball(0.01, [-0.4, 0.8 * Math.sin((120 * Math.PI) / 180), 0]))],
    collision_pairs: pairs,
    joints: [
      { name: "elbow", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Elbow", position: [0, 0, 0], axis: [0, 0, 1], lower: 0, upper: (upper * Math.PI) / 180 },
      { name: "pin", type: "fixed", parent: "base", child: "post", parent_name: "Base", child_name: "Stop", position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null },
      ...extra,
    ],
  }) as unknown as PagePayload;

describe("sweepJoints", () => {
  it("an elbow that hits the stop at 120 degrees is reported at 120, in the middle of the range", () => {
    const r = sweepJoints(hinge(180)).swept[0]!;
    expect(r.hit).toEqual({ a: "Elbow", b: "Stop", at: 120 });
  });
  it("a joint whose range stops short of the stop is not flagged and says how close it comes", () => {
    const r = sweepJoints(hinge(60)).swept[0]!;
    expect(r.hit).toBeNull();
    expect(r.closest!.at).toBe(60);
    expect(r.closest!.gap).toBeCloseTo(0.8 * Math.sin(Math.PI / 3) - 0.02, 6);
  });
  it("an ignored pair stays quiet through the sweep", () => {
    expect(sweepJoints(hinge(180, [{ link1: "post", link2: "arm", reason: "never meet" }])).swept[0]!.hit).toBeNull();
  });
  it("a joint with no limits is listed as not swept", () => {
    const free = { name: "spin", type: "continuous", parent: "base", child: "arm", parent_name: "Base", child_name: "Wheel", position: [0, 0, 0], axis: [0, 0, 1], lower: null, upper: null };
    const r = sweepJoints(hinge(60, [], [free]));
    expect([r.swept.length, r.notSwept]).toEqual([1, ["Wheel"]]);
  });
  it("a prismatic joint slides its part along the axis and reports the travel in mm", () => {
    const page = hinge(0, [], []) as unknown as { joints: { type: string; axis: number[]; lower: number; upper: number; position: number[] }[]; links: { name: string; solids: LinkSolid[] }[] };
    Object.assign(page.joints[0]!, { type: "prismatic", axis: [0, 1, 0], lower: 0, upper: 1 });
    page.links[2]!.solids = [ball(0.01, [0.5, 0.6, 0])]; // the bar centre is 0.6 m above the ball's path: touches within 2 cm of y = 0.6, the first of 36 steps of 27.8 mm past 0.58 m is 0.5833
    expect(sweepJoints(page as unknown as PagePayload).swept[0]!.hit!.at).toBe(583);
  });
});
