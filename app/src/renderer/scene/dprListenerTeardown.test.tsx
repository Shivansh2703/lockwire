/**
 * e2-sm2 — NOTHING PINS e2-sm-r2's HiDPI listener teardown (TODO.md row, found by e2-ss's
 * reviewer): Canvas.tsx watches devicePixelRatio via `window.matchMedia(...)`, re-subscribing
 * itself on every "change" (see Canvas.tsx's `watchRatio`), and its cleanup was verified only
 * BY READING at review time. No test asserted the listener registers on mount or is removed on
 * unmount. This is that guard.
 *
 * Idiom: same options-boundary trick as e2-hh3 (window-options.test.ts) — assert on the
 * arguments/calls at the boundary (`addEventListener`/`removeEventListener` on the object
 * `matchMedia` returns) rather than an observable side effect, and no launch needed. The
 * `window.matchMedia` stub shape and the `parts`/`lens` fixture are copied from
 * selectionStore.test.tsx (same file `mockImplementation`/`packVectorsI16`/`lenses: ["off"]`
 * shape it uses to satisfy Canvas's now-required `lens` prop and `ScenePart.lenses` field).
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("three", async () => {
  const actual = await vi.importActual<typeof import("three")>("three");
  class FakeWebGLRenderer {
    domElement: HTMLCanvasElement;
    constructor(params: { canvas: HTMLCanvasElement }) {
      this.domElement = params.canvas;
    }
    setSize() {}
    setPixelRatio() {}
    render() {}
    dispose() {}
  }
  return { ...actual, WebGLRenderer: FakeWebGLRenderer };
});

// jsdom has no ResizeObserver (Canvas.tsx uses one, unrelated to the DPR listener this test
// guards) — same no-op stub as selectionStore.test.tsx.
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", StubResizeObserver);

// The counters this test asserts on. Canvas.tsx calls `window.matchMedia(...)` fresh each time
// it (re-)subscribes (`watchRatio`), so each returned object gets its own addEventListener /
// removeEventListener spy — these totals are what the boundary actually crossed.
let addCalls = 0;
let removeCalls = 0;
window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  addEventListener: vi.fn(() => {
    addCalls += 1;
  }),
  removeEventListener: vi.fn(() => {
    removeCalls += 1;
  }),
})) as unknown as typeof window.matchMedia;

const { Canvas } = await import("./Canvas");

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

// Same fixture shape as selectionStore.test.tsx: one part, default "off" lens, so Canvas's
// (now-required) `lens` prop and `ScenePart.lenses` field are both satisfied.
const parts = [
  {
    id: "part-1",
    positions: packVectorsI16([-500, -500, 0, 500, -500, 0, 0, 500, 0], 0.0001),
    indices: packIndicesU16([0, 1, 2]),
    lenses: ["off"],
  },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  addCalls = 0;
  removeCalls = 0;
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  container.remove();
});

describe("Canvas — the DPR matchMedia listener's teardown", () => {
  it("test_the_dpr_listener_registers_on_mount_and_is_removed_on_unmount", () => {
    root = createRoot(container);
    act(() => {
      root.render(<Canvas parts={parts as never} sceneKey="fixture" lens="off" />);
    });

    // registers on mount — one direction of the claim.
    expect(addCalls).toBe(1);
    expect(removeCalls).toBe(0);

    act(() => root.unmount());

    // removed on unmount — the other direction. The count must return to baseline: every
    // listener this component added is gone, not merely "some listener was removed".
    expect(removeCalls).toBe(addCalls);
  });
});
