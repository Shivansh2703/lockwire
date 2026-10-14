/** The frame loop draws only when something changed: idle ticks render nothing, a camera change renders once. */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

let renders = 0;
vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    constructor(params: { canvas: HTMLCanvasElement }) { this.domElement = params.canvas; }
    setSize() {}
    setPixelRatio() {}
    render() { renders += 1; }
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});
vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
window.matchMedia = (() => ({ addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;

const frames: FrameRequestCallback[] = [];
vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => frames.push(cb));
vi.stubGlobal("cancelAnimationFrame", () => {});
const runFrames = (n: number) => { for (let i = 0; i < n; i++) frames.splice(0).forEach((cb) => cb(0)); };

const { Canvas, useScene } = await import("./Canvas");

describe("Canvas render on demand", () => {
  it("test_idle_frames_do_not_render_and_a_camera_change_renders_once", () => {
    let ctx!: ReturnType<typeof useScene>;
    const Grab = () => { ctx = useScene(); return null; };
    const root = createRoot(document.createElement("div"));
    act(() => root.render(<Canvas parts={[]} sceneKey="k" lens="off"><Grab /></Canvas>));
    runFrames(5);
    const settled = renders;
    expect(settled).toBeGreaterThan(0);
    runFrames(10);
    expect(renders).toBe(settled);
    ctx.controls.dispatchEvent({ type: "change" });
    runFrames(10);
    expect(renders).toBe(settled + 1);
    act(() => root.unmount());
  });
});
