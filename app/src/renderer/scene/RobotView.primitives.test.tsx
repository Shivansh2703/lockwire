/** CAD-02: the drawn link follows every new payload, not the one present at load. */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
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

const { RobotView } = await import("./RobotView");
const { useScene } = await import("./Canvas");

const ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1];
const links = (length: number) => [{ name: "arm", position: [0, 0, 0], solids: [{ kind: "cylinder", radius: 0.02, length, position: [0, 0, 0.1], rotation: ROT }] }] as never;

describe("RobotView link primitives", () => {
  it("test_a_size_changed_after_load_redraws_the_link_and_a_new_shape_appears", () => {
    let ctx!: ReturnType<typeof useScene>;
    const Grab = () => { ctx = useScene(); return null; };
    const root = createRoot(document.createElement("div"));
    const show = (l: unknown) => act(() => root.render(<RobotView sceneKey="k" links={l as never}><Grab /></RobotView>));
    const length = () => new THREE.Box3().setFromObject(ctx.meshes.find((m) => m.userData.primitive)!).getSize(new THREE.Vector3()).z;
    show([]);
    expect(ctx.meshes).toHaveLength(0);
    show(links(0.22));
    expect(length()).toBeCloseTo(0.22);
    show(links(0.3));
    expect(length()).toBeCloseTo(0.3);
    expect(ctx.meshes.filter((m) => m.userData.primitive)).toHaveLength(1);
    show([]);
    expect(ctx.meshes).toHaveLength(0);
    act(() => root.unmount());
  });
});
