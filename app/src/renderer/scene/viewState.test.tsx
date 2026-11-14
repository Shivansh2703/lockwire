import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import { applyDisplay, applyIsolate } from "./robotScene";
import { setSelectedPartId } from "./selectionStore";
import { getViewState, keepIds, setJointPose, viewAction, zeroPose } from "./viewState";
import { viewKey } from "../shortcuts";

vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    constructor(params: { canvas: HTMLCanvasElement }) { this.domElement = params.canvas; }
    setSize() {}
    setPixelRatio() {}
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});
vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
window.matchMedia = (() => ({ addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
vi.stubGlobal("requestAnimationFrame", () => 0);
vi.stubGlobal("cancelAnimationFrame", () => {});
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { RobotView } = await import("./RobotView");
const { useScene } = await import("./Canvas");
const { ViewMenu } = await import("../ViewMenu");

const mesh = (id: string) => { const m = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial()); m.userData.partId = id; return m; };
const key = (k: string, shiftKey = false) => ({ key: k, shiftKey, metaKey: false, ctrlKey: false, altKey: false });
const joint = (name: string, position: number[], type = "revolute") => ({ name, type, parent: "a", child: name, parent_name: "a", child_name: name, position, axis: [0, 0, 1], lower: null, upper: null, effort: 1 }) as never;
afterEach(() => { viewAction("showAll"); setSelectedPartId(null); });

describe("VIEW-1 hide, show, isolate", () => {
  it("hiding a part sets its mesh invisible (not just faint); the rest stay", () => {
    const ms = [mesh("a"), mesh("b")];
    applyIsolate({ meshes: ms } as never, null, ["a"]);
    expect(ms.map((m) => m.visible)).toEqual([false, true]);
    expect((ms[0]!.material as THREE.MeshStandardMaterial).opacity).toBe(1);
  });
  it("only shows the chosen parts, and GRAPH-1's list narrows it further", () => {
    const ms = [mesh("a"), mesh("b"), mesh("c")];
    applyIsolate({ meshes: ms } as never, keepIds(null, ["a", "b"]));
    expect(ms.map((m) => m.visible)).toEqual([true, true, false]);
    expect(keepIds(["b", "c"], ["a", "b"])).toEqual(["b"]);
    expect(keepIds(["b", "c"], [])).toEqual(["b", "c"]);
  });
  it("see through and wireframe set the material state", () => {
    const ms = [mesh("a")];
    const mat = ms[0]!.material as THREE.MeshStandardMaterial;
    applyDisplay({ meshes: ms } as never, { seeThrough: true, wireframe: true });
    expect([mat.transparent, mat.opacity < 1, mat.wireframe]).toEqual([true, true, true]);
    applyDisplay({ meshes: ms } as never, { seeThrough: false, wireframe: false });
    expect([mat.transparent, mat.opacity, mat.wireframe]).toEqual([false, 1, false]);
  });
});

describe("VIEW-1 keys and Show everything", () => {
  it("the keys map to the same actions the menu runs, and Shift+H is Show everything", () => {
    expect(["h", "i", "t", "m", "l"].map((k) => viewKey(key(k)))).toEqual(["hide", "only", "seeThrough", "wireframe", "frames"]);
    expect(viewKey(key("H", true))).toBe("showAll");
    expect(viewKey(key("h", false) as never)).toBe("hide");
  });
  it("toggling state, then Show everything restores all and clears the modes", () => {
    setSelectedPartId("a");
    (["hide", "only", "seeThrough", "wireframe", "frames"] as const).forEach(viewAction);
    expect(getViewState()).toMatchObject({ hidden: ["a"], only: ["a"], seeThrough: true, wireframe: true, frames: true });
    viewAction("hide");
    expect(getViewState().hidden).toEqual([]);
    viewAction("showAll");
    expect(getViewState()).toEqual({ hidden: [], only: [], pose: {}, compare: null, ortho: false, seeThrough: false, wireframe: false, frames: false, com: true });
  });
  it("hide with nothing selected does nothing", () => {
    viewAction("hide");
    expect(getViewState().hidden).toEqual([]);
  });
});

describe("VIEW-1 in the real view", () => {
  it("GRAPH-1's isolate prop and the person's hide share one mechanism; Show everything leaves GRAPH-1's choice alone; frames draw", () => {
    let ctx!: ReturnType<typeof useScene>;
    const Grab = () => { ctx = useScene(); return null; };
    const root = createRoot(document.createElement("div"));
    const joints = [joint("YAW", [0, 0, 0.1]), joint("ELBOW", [0, 0, 0.3]), joint("WRIST", [0, 0, 0.4]), joint("fx", [0, 0, 0.5], "fixed")];
    act(() => root.render(<RobotView sceneKey="k" joints={joints} isolate={["YAW", "ELBOW"]}><Grab /></RobotView>));
    const shown = () => [...new Set(ctx.meshes.filter((m) => m.visible && !m.userData.ring).map((m) => m.userData.partId))].sort();
    expect(shown()).toEqual(["ELBOW", "YAW"]);
    setSelectedPartId("YAW");
    act(() => viewAction("hide"));
    expect(shown()).toEqual(["ELBOW"]);
    act(() => viewAction("showAll"));
    expect(shown()).toEqual(["ELBOW", "YAW"]);
    setSelectedPartId("WRIST");
    act(() => viewAction("only")); // WRIST is outside GRAPH-1's list: both apply, so nothing is left
    expect(shown()).toEqual([]);
    act(() => viewAction("showAll"));
    const lines = () => ctx.scene.children.filter((c) => c.userData.decor && c instanceof THREE.Group).flatMap((g) => g.children).length;
    expect(lines()).toBe(0);
    act(() => viewAction("frames"));
    expect(lines()).toBe(4 * 3 + 3); // a triad per joint, an axis line per moving joint
    act(() => viewAction("showAll"));
    expect(lines()).toBe(0);
  });
  it("the View menu hides the selected part and Show everything puts it back", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    act(() => root.render(<ViewMenu design />));
    const click = (label: string) => act(() => { [...host.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label))!.click(); });
    click("View");
    const item = (label: string) => [...host.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label))!;
    expect(item("Hide selected part").disabled).toBe(true);
    act(() => setSelectedPartId("a"));
    click("Hide selected part");
    expect(getViewState().hidden).toEqual(["a"]);
    expect(host.textContent).toContain("1 hidden");
    click("Show everything");
    expect(getViewState().hidden).toEqual([]);
    expect(item("Show everything").disabled).toBe(true);
    act(() => root.unmount());
    host.remove();
  });
});

describe("POSE-1 pose state", () => {
  const rev = { name: "elbow", type: "revolute", lower: -1, upper: 1 } as const;
  it("holds a slider value inside the joint's limits, in metres for a sliding joint", () => {
    setJointPose(rev, 5);
    expect(getViewState().pose.elbow).toBe(1);
    setJointPose(rev, -5);
    expect(getViewState().pose.elbow).toBe(-1);
    setJointPose({ name: "slide", type: "prismatic", lower: 0, upper: 0.05 }, 2);
    expect(getViewState().pose.slide).toBe(0.05);
  });
  it("Back to zero and Show everything both drop the pose", () => {
    setJointPose(rev, 0.5);
    zeroPose();
    expect(getViewState().pose).toEqual({});
    setJointPose(rev, 0.5);
    viewAction("showAll");
    expect(getViewState().pose).toEqual({});
  });
});
