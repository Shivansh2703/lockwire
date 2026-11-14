import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import * as THREE from "three";

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

const joint = (name: string, position: number[]) => ({ name, type: "revolute", parent: "a", child: name, parent_name: "a", child_name: name, position, axis: [0, 0, 1], lower: null, upper: null, effort: 1 }) as never;

it("an edit that moves a part on the same robot keeps the camera; a different robot refits", () => {
  let ctx!: ReturnType<typeof useScene>;
  const Grab = () => { ctx = useScene(); return null; };
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const show = (key: string, x: number) => act(() => root.render(<RobotView sceneKey={key} joints={[joint("YAW", [x, 0.1, 0.1]), joint("ELBOW", [0.3, 0.1, 0.4])]}><Grab /></RobotView>));
  show("/r.yaml:h1", 0.2);
  ctx.camera.position.set(0.5, -0.4, 0.3); // the user zoomed in
  ctx.controls.target.set(0.2, 0.1, 0.2);
  show("/r.yaml:h2", 0.9); // a nudge: same contract, new hash
  expect(ctx.camera.position.distanceTo(new THREE.Vector3(0.5, -0.4, 0.3))).toBeLessThan(1e-6);
  expect(ctx.controls.target.distanceTo(new THREE.Vector3(0.2, 0.1, 0.2))).toBeLessThan(1e-6);
  show("/other.yaml:h3", 0.9);
  expect(ctx.camera.position.distanceTo(new THREE.Vector3(0.5, -0.4, 0.3))).toBeGreaterThan(0.01);
  act(() => root.unmount());
  host.remove();
});
