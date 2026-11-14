import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import { viewAction } from "./viewState";

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
const { activeCamera } = await import("./buildScene");
const { ViewMenu } = await import("../ViewMenu");

const joint = (name: string, position: number[]) => ({ name, type: "revolute", parent: "a", child: name, parent_name: "a", child_name: name, position, axis: [0, 0, 1], lower: null, upper: null, effort: 1 }) as never;
afterEach(() => viewAction("showAll"));

function mount() {
  let ctx!: ReturnType<typeof useScene>;
  const Grab = () => { ctx = useScene(); return null; };
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  act(() => root.render(<RobotView sceneKey="k" joints={[joint("YAW", [0.2, 0.1, 0.1]), joint("ELBOW", [0.3, 0.1, 0.4])]}><Grab /></RobotView>));
  const canvas = host.querySelector("canvas")!;
  canvas.tabIndex = 0;
  canvas.focus();
  return { ctx, canvas, host, done: () => { act(() => root.unmount()); host.remove(); } };
}
const press = (key: string, code = "", shiftKey = false) => act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key, code, shiftKey, bubbles: true })); });
const sits = (ctx: ReturnType<typeof useScene>) => ctx.camera.position.clone().sub(ctx.controls.target).normalize().toArray().map((n) => Math.round(n * 100) / 100 + 0);

describe("GAP-views: every view key puts the camera on its axis, looking at the model centre", () => {
  it.each([
    ["1", "", false, [1, 0, 0]],
    ["!", "Digit1", true, [-1, 0, 0]],
    ["3", "", false, [0, 1, 0]],
    ["#", "Digit3", true, [0, -1, 0]],
    ["7", "", false, [0, 0, 1]],
    ["&", "Digit7", true, [0, 0, -1]],
    ["0", "", false, [0.58, 0.58, 0.58]],
  ] as [string, string, boolean, number[]][])("key %s", (k, code, shift, axis) => {
    const { ctx, done } = mount();
    const centre = new THREE.Box3().setFromObject(new THREE.Group().add(...ctx.meshes.map((m) => m.clone()))).getCenter(new THREE.Vector3());
    press(k, code, shift);
    expect(sits(ctx)).toEqual(axis);
    const toCentre = centre.clone().sub(ctx.camera.position).normalize();
    const looking = new THREE.Vector3(0, 0, -1).applyQuaternion(ctx.camera.quaternion);
    expect(looking.dot(toCentre)).toBeGreaterThan(0.99);
    done();
  });
  it("5 swaps in an orthographic camera that shows what the perspective one showed; 5 again swaps back", () => {
    const { ctx, done } = mount();
    expect(activeCamera(ctx)).toBe(ctx.camera);
    press("5");
    const o = activeCamera(ctx) as THREE.OrthographicCamera;
    expect(o.isOrthographicCamera).toBe(true);
    const halfH = ctx.camera.position.distanceTo(ctx.controls.target) * Math.tan((ctx.camera.fov * Math.PI) / 360);
    expect(o.top).toBeCloseTo(halfH);
    expect(o.position.toArray()).toEqual(ctx.camera.position.toArray());
    press("5");
    expect(activeCamera(ctx)).toBe(ctx.camera);
    done();
  });
  it("a view key typed into a text field does nothing", () => {
    const { ctx, done } = mount();
    press("1");
    const before = ctx.camera.position.toArray();
    const input = document.createElement("input");
    document.body.append(input);
    input.focus();
    press("3");
    press("5");
    press("!", "Digit1", true);
    expect(ctx.camera.position.toArray()).toEqual(before);
    expect(activeCamera(ctx)).toBe(ctx.camera);
    input.remove();
    done();
  });
  it("the View menu rows move the same camera", () => {
    const { ctx, done } = mount();
    const box = document.createElement("div");
    document.body.append(box);
    const root = createRoot(box);
    act(() => root.render(<ViewMenu design={false} />));
    act(() => box.querySelector<HTMLButtonElement>("button[aria-haspopup]")!.click());
    const row = (t: string) => [...box.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.startsWith(t))!;
    act(() => row("Back").click());
    expect(sits(ctx)).toEqual([-1, 0, 0]);
    act(() => row("Left side").click()); // the robot's left is +Y (X forward, Z up): the same camera as key 3
    expect(sits(ctx)).toEqual([0, 1, 0]);
    act(() => row("Right side").click());
    expect(sits(ctx)).toEqual([0, -1, 0]);
    act(() => row("Flat view").click());
    expect((activeCamera(ctx) as THREE.OrthographicCamera).isOrthographicCamera).toBe(true);
    act(() => root.unmount());
    box.remove();
    done();
  });
});
