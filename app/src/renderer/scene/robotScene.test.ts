import { describe, expect, it } from "vitest";
import * as THREE from "three";

import type { Card, Finding, RobotJoint, RobotLink } from "../../shared/api.gen";
import { applyDim, applyHighlight, buildSkeleton, declarePart, highlightsFromCards, highlightsFromFindings, jointsOfPath, partsOfPath, paint, skeletonMeshes, wholeHighlight, type ThemeColors } from "./robotScene";

const joint = (name: string, parent: string, child: string, position: number[]): RobotJoint => ({
  name, type: "revolute", parent, child, position, axis: [0, 0, 1], lower: null, upper: null, effort: 5,
});
// base -> YAW -> SHOULDER -> ELBOW -> WRIST; each child link sits at its joint (URDF frame), so
// the bones are parent-link-origin -> joint; the root has none above it.
const joints = [
  { ...joint("fix", "world", "base", [0, 0, 0]), type: "fixed" as const },
  joint("YAW", "base", "l1", [0, 0, 0.1]),
  joint("SHOULDER", "l1", "l2", [0, 0, 0.3]),
  joint("ELBOW", "l2", "l3", [0.3, 0, 0.3]),
  joint("WRIST", "l3", "l4", [0.5, 0, 0.3]),
];
const links: RobotLink[] = [
  { name: "world", position: [0, 0, 0] }, { name: "base", position: [0, 0, 0] }, { name: "l1", position: [0, 0, 0.1] }, { name: "l2", position: [0, 0, 0.3] },
  { name: "l3", position: [0.3, 0, 0.3] }, { name: "l4", position: [0.5, 0, 0.3] },
];
const card = (severity: Card["severity"], targets: Card["targets"]) => ({ severity, targets }) as Card;

describe("RobotView helpers", () => {
  it("builds an arm: a bone per link span, a motor housing per joint, a base at the root", () => {
    const sk = buildSkeleton(joints, links);
    expect(sk.segments.map((s) => s.joint)).toEqual(["YAW", "SHOULDER", "ELBOW", "WRIST"]);
    expect(sk.nodes).toHaveLength(4);
    expect(sk.base).toEqual([0, 0, 0]);
    const meshes = skeletonMeshes(sk);
    expect(meshes.filter((m) => m.geometry.type === "CylinderGeometry" && !m.userData.bone)).toHaveLength(4);
    expect(meshes.filter((m) => m.geometry.type === "CapsuleGeometry")).toHaveLength(4);
    expect(sk.tip?.joint).toBe("WRIST");
    const box = new THREE.Box3();
    meshes.forEach((m) => box.expandByObject(m));
    expect(box.max.x).toBeGreaterThan(0.5);
    expect(box.min.z).toBeLessThan(0.01);
  });

  it("maps card targets to highlights: joints by name, links to their joint, bad over warn", () => {
    const h = highlightsFromCards(
      [
        card("warning", [{ kind: "joint", id: "ELBOW", label: "ELBOW" }, { kind: "device", id: "d", label: "d" }]),
        card("violation", [{ kind: "joint", id: "ELBOW", label: "ELBOW" }, { kind: "link", id: "l1", label: "l1" }]),
      ],
      joints,
    );
    expect(h).toEqual([{ id: "ELBOW", level: "bad" }, { id: "YAW", level: "bad" }]);
  });

  it("plan/test findings and declare paths light the joint (or the joint carrying the link) they name", () => {
    expect(jointsOfPath("model/joints/ELBOW/velocity", joints)).toEqual(["ELBOW"]);
    expect(jointsOfPath("model/links/l1/mass", joints)).toEqual(["YAW"]);
    expect(jointsOfPath("metadata/revision", joints)).toEqual([]);
    const f = (severity: Finding["severity"], quantity: string) => ({ severity, quantity }) as Finding;
    expect(highlightsFromFindings([f("warning", "plan/steps/0/ELBOW/target"), f("violation", "model/joints/ELBOW/limit"), f("not_scored", "x/WRIST")], joints))
      .toEqual([{ id: "ELBOW", level: "bad" }]); // not_scored lights nothing
  });

  it("findings light what the engine's typed targets name, not what their path spells", () => {
    const f = { severity: "violation", quantity: "plan/steps/0/rail_load/rail" } as Finding;
    expect(highlightsFromFindings([f], joints)).toEqual([]);
    expect(highlightsFromCards([card("not_scored", [{ kind: "joint", id: "ELBOW", label: "E" }])], joints)).toEqual([]);
    expect(partsOfPath("model/links/inert/mass", ["base"], [...joints, { ...joint("fx", "base", "inert", [0, 0, 0]), type: "fixed" as const }])).toEqual(["base"]);
    expect(highlightsFromFindings([f], joints, [{ label: "Rail load", targets: [{ kind: "link", id: "l2", label: "l2" }, { kind: "rail", id: "r", label: "r" }] }]))
      .toEqual([{ id: "SHOULDER", level: "bad" }]);
  });

  it("device and rail targets light the scene parts the engine names; bad beats warn", () => {
    const h = highlightsFromCards(
      [
        card("warning", [{ kind: "device", id: "dev.cam", label: "cam", parts: ["dev.cam"] }]),
        card("violation", [{ kind: "rail", id: "rail.5v", label: "5v", parts: ["rail.5v", "dev.cam", "dev.jet"] }]),
      ],
      joints,
    );
    expect(h).toEqual([{ id: "dev.cam", level: "bad" }, { id: "rail.5v", level: "bad" }, { id: "dev.jet", level: "bad" }]);
  });

  it("bad beats warn whichever card comes first", () => {
    const h = highlightsFromCards(
      [card("violation", [{ kind: "joint", id: "ELBOW", label: "ELBOW" }]), card("warning", [{ kind: "joint", id: "ELBOW", label: "ELBOW" }])],
      joints,
    );
    expect(h).toEqual([{ id: "ELBOW", level: "bad" }]);
  });

  it("paints a highlighted joint with its level colour and shows its ring (a bone never glows)", () => {
    const [sphere, ring] = skeletonMeshes(buildSkeleton(joints, links)).filter((m) => m.userData.partId === "YAW").slice(-2);
    const c = (hex: number) => new THREE.Color(hex);
    const colors: ThemeColors = { canvas: c(0), grid: c(0), part: c(0x888888), warn: c(0xfbbf24), bad: c(0xff0000), accent: c(0x0000ff), outline: c(0xffffff) };
    sphere!.userData.level = "warn";
    ring!.userData.level = "warn";
    paint({ meshes: [sphere!, ring!] }, colors);
    expect((sphere!.material as THREE.MeshStandardMaterial).emissive.getHex()).toBe(0xfbbf24);
    expect(ring!.visible).toBe(true);
  });

  it("a selection dims every other part, and a selected bone glows (fixed joint = both links)", () => {
    const ms = skeletonMeshes(buildSkeleton(joints, links));
    const ctx = { meshes: ms } as never;
    applyHighlight(ctx, [{ id: "ELBOW", level: "bad" }], "YAW");
    const op = (id: string) => ms.filter((m) => m.userData.partId === id).map((m) => (m.material as THREE.MeshStandardMaterial).opacity);
    expect(op("YAW").every((o) => o === 1)).toBe(true);
    expect(op("ELBOW").every((o) => o < 1)).toBe(true);
    const ped = ms.find((m) => m.userData.partId === "world")!;
    expect(ped).toBeDefined(); // the root link is pickable/lightable, so a fixed root joint shows
    applyHighlight(ctx, [], null);
    expect(op("ELBOW").every((o) => o === 1)).toBe(true);
    applyDim(ctx, true);
    expect(op("WRIST").every((o) => o === 0.4)).toBe(true);
    expect(ms.every((m) => (m.material as THREE.MeshStandardMaterial).depthWrite === false)).toBe(true);
  });

  it("every lit part gets a non-pickable outline hull; clearing the highlight removes it with no leak", () => {
    const ms = skeletonMeshes(buildSkeleton(joints, links));
    const ctx = { meshes: ms } as never;
    const count = () => ms.reduce((n, m) => n + m.children.length, 0);
    const colors: ThemeColors = { canvas: new THREE.Color(0), grid: new THREE.Color(0), part: new THREE.Color(0x888888), warn: new THREE.Color(0xfbbf24), bad: new THREE.Color(0xff0000), accent: new THREE.Color(0x0000ff), outline: new THREE.Color(0xffffff) };
    const yaw = ms.filter((m) => m.userData.partId === "YAW" && !m.userData.bone && !m.userData.ring);
    expect(yaw).toHaveLength(1);
    for (let i = 0; i < 3; i++) { // repeated toggles never pile up hulls
      yaw[0]!.userData.level = "warn";
      ms.find((m) => m.userData.partId === "YAW" && m.userData.bone)!.userData.level = "warn"; // a bone never outlines unless selected
      paint(ctx as { meshes: THREE.Mesh[] }, colors);
      expect(count()).toBe(1);
      const hull = yaw[0]!.children[0] as THREE.Mesh;
      expect(hull.userData.outlineHull).toBe(true);
      expect(new THREE.Raycaster().intersectObject(hull, false)).toEqual([]);
      yaw[0]!.userData.level = undefined;
      paint(ctx as { meshes: THREE.Mesh[] }, colors);
      expect(count()).toBe(0);
    }
  });

  it("a selected finding keeps its status colour and thickness; a selected bone gets a same-thickness accent hull", () => {
    for (const [k, v] of Object.entries({ "canvas-a": "#fff", grid: "#ccc", part: "#888", "glow-warn": "#fa0", "glow-bad": "#f00", accent: "#00f", outline: "#fff" })) document.documentElement.style.setProperty(`--${k}`, v);
    const ms = skeletonMeshes(buildSkeleton(joints, links));
    const ctx = { meshes: ms } as never;
    const sphere = ms.find((m) => m.userData.partId === "ELBOW" && !m.userData.bone && !m.userData.ring)!;
    const t = () => ((sphere.children[0] as THREE.Mesh).material as THREE.ShaderMaterial).uniforms.t!.value as number;
    applyHighlight(ctx, [{ id: "ELBOW", level: "bad" }], null);
    const statusT = t();
    applyHighlight(ctx, [{ id: "ELBOW", level: "bad" }], "ELBOW");
    expect((sphere.material as THREE.MeshStandardMaterial).emissiveIntensity).toBeGreaterThan(0); // still the status glow
    expect(t()).toBeCloseTo(statusT, 9); // one style: selection does not thicken the hull
    applyHighlight(ctx, [], "ELBOW");
    const plainT = t();
    applyHighlight(ctx, [], "WRIST");
    const bone = ms.find((m) => m.userData.partId === "WRIST" && m.userData.bone)!;
    expect(bone.children).toHaveLength(1);
    const bu = (bone.children[0] as THREE.Mesh).material as THREE.ShaderMaterial;
    expect(bu.uniforms.color!.value.getHexString()).toBe("0000ff"); // accent, visible in both themes
    expect(bu.uniforms.t!.value / bone.getWorldScale(new THREE.Vector3()).x).toBeCloseTo(plainT / sphere.getWorldScale(new THREE.Vector3()).x, 9);
  });

  it("the outline hull uses welded, smoothed normals so hard edges do not tear into spikes", () => {
    const box = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial()); // 24 verts, hard-edged normals
    box.userData = { partId: "BOX", level: "warn" };
    const colors: ThemeColors = { canvas: new THREE.Color(0), grid: new THREE.Color(0), part: new THREE.Color(0), warn: new THREE.Color(0), bad: new THREE.Color(0), accent: new THREE.Color(0), outline: new THREE.Color(0xffffff) };
    paint({ meshes: [box] } as never, colors);
    const g = (box.userData.outline as THREE.Mesh).geometry;
    expect(g.getAttribute("position").count).toBe(8);
    const n = new THREE.Vector3().fromBufferAttribute(g.getAttribute("normal"), 0);
    expect(n.x * n.x).toBeLessThan(0.5);
  });

  it("a sheet thinner than the hull (a propeller blade) is outlined in the accent, not white", () => {
    const box = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial());
    const blade = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 0.001), new THREE.MeshStandardMaterial());
    const z = new THREE.Color(0);
    const [accent, white] = [new THREE.Color(0x0000ff), new THREE.Color(0xffffff)];
    box.userData = { partId: "BOX", selected: true };
    blade.userData = { partId: "BLADE", selected: true };
    paint({ meshes: [box, blade] } as never, { canvas: z, grid: z, part: z, warn: z, bad: z, accent, outline: white });
    const hue = (m: THREE.Mesh) => ((m.userData.outline as THREE.Mesh).material as THREE.ShaderMaterial).uniforms.color!.value.getHex();
    expect([hue(box), hue(blade)]).toEqual([0xffffff, 0x0000ff]);
  });

  it("not-scored findings light nothing; a path names mesh parts; a selected part keeps its fill", () => {
    const f = { severity: "not_scored", quantity: "ELBOW/x" } as Finding;
    expect(highlightsFromFindings([f], joints)).toEqual([]);
    expect(highlightsFromCards([card("not_scored", [{ kind: "joint", id: "ELBOW", label: "E" }])], joints)).toEqual([]);
    expect(partsOfPath("model/links/inert/mass", ["base"], [...joints, { ...joint("fx", "base", "inert", [0, 0, 0]), type: "fixed" as const }])).toEqual(["base"]);
    expect(declarePart([{ path: "model/links/inert/mass", part: "base" }], "model/links/inert/mass", [], ["base"])).toEqual(["base"]);
    expect(partsOfPath("devices/dev.battery/physical/mass", ["dev.battery"], [])).toEqual(["dev.battery"]);
    for (const [k, v] of Object.entries({ "canvas-a": "#fff", grid: "#ccc", part: "#888", "glow-warn": "#fa0", "glow-bad": "#f00", accent: "#00f", outline: "#fff" })) document.documentElement.style.setProperty(`--${k}`, v);
    const m = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial());
    m.userData.partId = "a";
    applyHighlight({ meshes: [m] } as never, [{ id: "a", level: "bad" }], ["a"]);
    expect((m.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("ff0000");
    applyHighlight({ meshes: [m] } as never, [], ["a"]);
    expect((m.material as THREE.MeshStandardMaterial).color.getHexString()).toBe("0000ff"); // no status: accent fill
  });

  it("a whole-robot finding tints every part faintly: no outline, far weaker than a single flagged part", () => {
    const ms = ["a", "b"].map((id) => { const m = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial()); m.userData.partId = id; return m; });
    applyHighlight({ meshes: ms } as never, wholeHighlight("violation", ["a", "b"]), null);
    const mat = (i: number) => ms[i]!.material as THREE.MeshStandardMaterial;
    expect(mat(0).color.getHexString()).not.toBe("ff0000"); // mixed toward red, not filled
    expect(mat(0).emissiveIntensity).toBeGreaterThan(0);
    expect(mat(0).emissiveIntensity).toBeLessThan(0.3);
    expect(ms[0]!.userData.outline).toBeUndefined();
    expect(wholeHighlight("not_scored", ["a"])).toEqual([]);
  });
});
