/** POSE-1-fix: while the robot is posed, nothing drawn at the zero pose stays on screen unlabelled. */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

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

const { RobotView } = await import("./RobotView");
const { useScene } = await import("./Canvas");
const { setJointPose, viewAction, zeroPose } = await import("./viewState");

const joint = { name: "elbow", type: "revolute", parent: "base", child: "arm", parent_name: "Base", child_name: "Arm", position: [0, 0, 0.2], axis: [0, 1, 0], lower: -1, upper: 1, effort: null } as never;
const links = [{ name: "arm", position: [0, 0, 0], solids: [{ kind: "box", size: [0.1, 0.1, 0.1], position: [0, 0, 0.1], rotation: [1, 0, 0, 0, 1, 0, 0, 0, 1] }] }] as never;

describe("RobotView while posed", () => {
  afterEach(() => { act(() => zeroPose()); });

  it("hides the axis overlay and the frames while posed, and Back to zero restores both", () => {
    let ctx!: ReturnType<typeof useScene>;
    const Grab = () => { ctx = useScene(); return null; };
    const root = createRoot(document.createElement("div"));
    const overlay = () => ctx.scene.children.filter((c) => c.userData.jointOverlay).length;
    const frames = () => ctx.scene.children.filter((c) => c.userData.decor && c.type === "Group" && !c.userData.jointOverlay).length;
    act(() => viewAction("frames"));
    act(() => root.render(<RobotView sceneKey="k" links={links} joints={[joint]} selected="elbow"><Grab /></RobotView>));
    expect([overlay(), frames()]).toEqual([1, 1]);
    act(() => setJointPose(joint, 0.5));
    expect([overlay(), frames()]).toEqual([0, 0]);
    act(() => zeroPose());
    expect([overlay(), frames()]).toEqual([1, 1]);
    act(() => viewAction("frames")); // frames off first: a decor Group is not a disposable grid
    act(() => root.unmount());
  });

  it("draws every part neutral while posed and restores the status colour with Back to zero", () => {
    let ctx!: ReturnType<typeof useScene>;
    const Grab = () => { ctx = useScene(); return null; };
    const root = createRoot(document.createElement("div"));
    const level = () => ctx.meshes.find((m) => m.userData.primitive)?.userData.level;
    act(() => root.render(<RobotView sceneKey="k" links={links} joints={[joint]} highlight={[{ id: "arm", level: "bad" }]}><Grab /></RobotView>));
    expect(level()).toBe("bad");
    act(() => setJointPose(joint, 0.5));
    expect(level()).toBeUndefined();
    act(() => zeroPose());
    expect(level()).toBe("bad");
    act(() => root.unmount());
  });
});
