/**
 * e2-ss — drives the pick through the real path as far as a headless test can: a `pointerdown`
 * DOM event on the mounted `<Canvas>`, which raycasts (real `THREE.Raycaster`, pure geometry
 * math — needs no WebGL context, so jsdom is enough) against the real mounted meshes. Only the
 * renderer (no GL in jsdom) is stubbed, same idiom as `buildScene.test.ts`.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PartTree } from "../kit";
import { toScenePart } from "../DesignPanel";

vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    constructor(params: { canvas: HTMLCanvasElement }) {
      rendererBuilds += 1;
      this.domElement = params.canvas;
    }
    setSize() {}
    setPixelRatio() {}
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});

// jsdom has no ResizeObserver (Canvas.tsx uses one to own the resolution uniform on resize —
// unrelated to picking); a no-op stub is enough to mount.
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", StubResizeObserver);

// jsdom does not implement matchMedia; Canvas.tsx now also watches devicePixelRatio via a media
// query (e2-sm/e2-f, landed after this box) — same mock idiom as App.test.tsx/CheckFace.test.tsx.
window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})) as unknown as typeof window.matchMedia;

const { Canvas } = await import("./Canvas");
const { getSelectedPartId, setSelectedPartId, setSelectionSceneKey, useSelectedPartId } =
  await import("./selectionStore");

function packVectorsI16(values: number[], scale = 1) {
  const buf = Buffer.alloc(values.length * 2);
  values.forEach((v, i) => buf.writeInt16LE(v, i * 2));
  return { type: "i16" as const, data: buf.toString("base64"), scale, offset: [0, 0, 0] as [number, number, number] };
}

function packIndicesU16(values: number[]) {
  const buf = Buffer.alloc(values.length * 2);
  values.forEach((v, i) => buf.writeUInt16LE(v, i * 2));
  return { type: "u16" as const, data: buf.toString("base64") };
}

// A small (0.05-unit) triangle straddling the origin, on the camera->target ray (default camera
// at (2,2,2) looking at (0,0,0)) but far from the screen edges — so the screen center hits it
// and a corner pick does not.
const PART_ID = "part-42";
// e2-f3 (landed after this box) made Canvas.tsx's `lens` prop required and toggles
// `mesh.visible` by `ScenePart.lenses` membership. This test is about pick-to-part-id
// resolution, not lenses, so the part carries the default "off" lens and Canvas is
// mounted on "off" — every part stays visible exactly as it was before that prop
// existed, and picking behaves identically.
const parts = [
  {
    id: PART_ID,
    positions: packVectorsI16([-500, -500, 0, 500, -500, 0, 0, 500, 0], 0.0001),
    indices: packIndicesU16([0, 1, 2]),
    group: { id: "other", label: "Other", kind: "other" },
    lenses: ["off"],
  },
];

let container: HTMLDivElement;
let root: Root;
let rendererBuilds = 0;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  setSelectionSceneKey(undefined);
  rendererBuilds = 0;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function mountAndGetCanvas(): HTMLCanvasElement {
  root = createRoot(container);
  act(() => {
      root.render(<Canvas parts={parts as never} sceneKey="fixture" lens="off" />);
  });
  const canvas = container.querySelector("canvas")!;
  // jsdom's canvas has no pointer-capture implementation; OrbitControls (also listening on this
  // canvas, unrelated to picking) calls it on every pointerdown.
  canvas.setPointerCapture = () => {};
  canvas.releasePointerCapture = () => {};
  canvas.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  return canvas;
}

function pick(canvas: HTMLCanvasElement, clientX: number, clientY: number) {
  act(() => {
    canvas.dispatchEvent(
      new window.PointerEvent("pointerdown", { clientX, clientY, bubbles: true, cancelable: true }),
    );
  });
}

function Tree({ id, name }: { id: string; name: string }) {
  return <PartTree parts={[{ id, name }]} selected={useSelectedPartId() ?? undefined} onSelect={setSelectedPartId} />;
}

describe("selection store — pointer pick on the mounted scene", () => {
  it("keeps the payload id shared by keyboard rows and pointer picks", () => {
    const apiPart = {
      id: PART_ID,
      node: "Part 42",
      name: "Part 42",
      state: "ok",
      positions: packVectorsI16([-500, -500, 0, 500, -500, 0, 0, 500, 0], 0.0001),
      normals: null,
      indices: packIndicesU16([0, 1, 2]),
      color: [],
      translation: [],
      group: { id: "other", label: "Other", kind: "other" },
      lenses: ["off"],
    } as Parameters<typeof toScenePart>[0];
    const mappedPart = toScenePart(apiPart);

    root = createRoot(container);
    act(() => {
      root.render(
        <>
          <Tree id={apiPart.id!} name={apiPart.node} />
          <Canvas parts={[mappedPart]} sceneKey="fixture" lens="off" />
        </>,
      );
    });
    const row = container.querySelector<HTMLButtonElement>(".kit-tree-row")!;
    const canvas = container.querySelector("canvas")!;
    canvas.setPointerCapture = () => {};
    canvas.releasePointerCapture = () => {};
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    act(() => row.click());
    expect(getSelectedPartId()).toBe(PART_ID);
    pick(canvas, 400, 300);
    expect(getSelectedPartId()).toBe(PART_ID);
    expect(row.getAttribute("aria-pressed")).toBe("true");
  });

  it("test_a_pick_on_a_part_yields_that_parts_payload_id", () => {
    const canvas = mountAndGetCanvas();
    // screen center -> NDC (0, 0) -> the camera->target ray, which the triangle straddles.
    pick(canvas, 400, 300);
    expect(getSelectedPartId()).toBe(PART_ID);
  });

  it("test_a_pick_on_empty_space_yields_the_nothing_selected_state", () => {
    const canvas = mountAndGetCanvas();
    pick(canvas, 400, 300);
    expect(getSelectedPartId()).toBe(PART_ID);
    // a screen corner -> nowhere near the triangle -> the raycast hits nothing.
    pick(canvas, 780, 20);
    expect(getSelectedPartId()).toBeNull();
  });

  it("clears selection when its valid scene becomes absent", () => {
    setSelectionSceneKey("robot.yaml:fixture");
    setSelectedPartId(PART_ID);

    setSelectionSceneKey(undefined);

    expect(getSelectedPartId()).toBeNull();
  });

  it("keeps the mounted geometry and camera when a fresh payload has the same scene key", () => {
    root = createRoot(container);
    act(() => {
      root.render(<Canvas parts={parts as never} sceneKey="same-asset" lens="off" />);
    });
    expect(rendererBuilds).toBe(1);

    act(() => {
      root.render(<Canvas parts={[...parts] as never} sceneKey="same-asset" lens="off" />);
    });
    expect(rendererBuilds).toBe(1);

    act(() => {
      root.render(<Canvas parts={parts as never} sceneKey="replaced-asset" lens="off" />);
    });
    expect(rendererBuilds).toBe(2);
  });

  it("test_the_state_is_readable_by_a_consumer_that_did_not_do_the_picking", () => {
    const canvas = mountAndGetCanvas();

    // A second, independent reader — its own React root, mounted separately from the Canvas
    // that does the picking — reading via the public hook, not the module internals.
    const readerHost = document.createElement("div");
    document.body.appendChild(readerHost);
    const readerRoot = createRoot(readerHost);
    function Reader() {
      const selected = useSelectedPartId();
      return <span data-testid="selected">{selected ?? "none"}</span>;
    }
    act(() => {
      readerRoot.render(<Reader />);
    });
    expect(readerHost.textContent).toBe("none");

    pick(canvas, 400, 300);
    expect(readerHost.textContent).toBe(PART_ID);

    act(() => readerRoot.unmount());
    readerHost.remove();
  });
});
