import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import {
  createCalloutPool,
  labelStyle,
  projectAnchor,
  updateCalloutPositions,
  type CalloutPoolEntry,
} from "./callouts";
import {
  CalloutLayer,
  __getCalloutLayerRenderCountForTest,
  __resetCalloutLayerRenderCountForTest,
  type CalloutAnchor,
} from "./CalloutLayer";

// --- pure helpers -----------------------------------------------------------

describe("labelStyle", () => {
  it("test_a_derived_value_is_drawn_differently_from_a_declared_one", () => {
    expect(labelStyle("declared")).toBe("declared");
    for (const kind of ["measured", "derived", "catalog", "projected"] as const) {
      expect(labelStyle(kind)).toBe("derived");
    }
    expect(labelStyle("derived")).not.toBe(labelStyle("declared"));
  });
});

describe("createCalloutPool / updateCalloutPositions", () => {
  it("test_the_layer_uses_a_fixed_pool_of_divs_and_reuses_them", () => {
    const container = document.createElement("div");
    const pool = createCalloutPool(3, container);
    expect(container.querySelectorAll(".callout").length).toBe(3);
    const originalEls = pool.map((entry) => entry.el);

    updateCalloutPositions(pool, [
      { id: "x", x: 1, y: 2, text: "X", kind: "declared" },
      { id: "y", x: 3, y: 4, text: "Y", kind: "derived" },
    ]);
    // one frame later, a different set of ids (fewer, and different order):
    // no div is created or removed, the same three nodes are reused.
    updateCalloutPositions(pool, [{ id: "z", x: 9, y: 9, text: "Z", kind: "measured" }]);

    expect(container.querySelectorAll(".callout").length).toBe(3);
    expect(pool.map((entry) => entry.el)).toEqual(originalEls);
    expect(pool[0]!.el.hidden).toBe(false);
    expect(pool[0]!.el.textContent).toBe("Z");
    expect(pool[1]!.el.hidden).toBe(true);
    expect(pool[2]!.el.hidden).toBe(true);
    expect(pool[1]!.el.dataset.calloutPartId).toBeUndefined();
  });

  it("writes translate3d and the number-style onto the reused element", () => {
    const container = document.createElement("div");
    const pool = createCalloutPool(1, container);
    updateCalloutPositions(pool, [{ id: "a", x: 10, y: 20, text: "10 W", kind: "catalog" }]);
    expect(pool[0]!.el.style.transform).toBe("translate3d(10px, 20px, 0)");
    expect(pool[0]!.el.dataset.kind).toBe("derived");
  });
});

describe("projectAnchor", () => {
  it("projects a point in front of the camera onto the viewport, and flags one behind it", () => {
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();

    const front = projectAnchor(camera, new THREE.Vector3(0, 0, 0), 800, 600);
    expect(front.behindCamera).toBe(false);
    expect(front.x).toBeCloseTo(400, 0);
    expect(front.y).toBeCloseTo(300, 0);

    const behind = projectAnchor(camera, new THREE.Vector3(0, 0, 10), 800, 600);
    expect(behind.behindCamera).toBe(true);
  });
});

// --- CalloutLayer: fixed pool + no React state in the frame callback -------

let container: HTMLDivElement;
let root: Root | null;
let queuedFrames: Map<number, FrameRequestCallback>;
let rafSpy: ReturnType<typeof vi.spyOn>;
let cancelRafSpy: ReturnType<typeof vi.spyOn>;
let nextFrameId: number;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = null;
  queuedFrames = new Map();
  nextFrameId = 0;
  rafSpy = vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation((cb: FrameRequestCallback) => {
    const id = ++nextFrameId;
    queuedFrames.set(id, cb);
    return id;
  });
  cancelRafSpy = vi.spyOn(globalThis, "cancelAnimationFrame").mockImplementation((id) => {
    queuedFrames.delete(id);
  });
  __resetCalloutLayerRenderCountForTest();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container.remove();
  rafSpy.mockRestore();
  cancelRafSpy.mockRestore();
});

function flushOneFrame() {
  const frame = queuedFrames.entries().next().value as [number, FrameRequestCallback] | undefined;
  if (!frame) return;
  queuedFrames.delete(frame[0]);
  act(() => frame[1](0));
}

function renderLayer(anchors: CalloutAnchor[]) {
  const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
  camera.position.set(0, 0, 5);
  camera.updateMatrixWorld();
  camera.updateProjectionMatrix();
  const onLabelClick = vi.fn();
  root = createRoot(container);
  act(() => {
    root!.render(
      createElement(CalloutLayer, {
        poolSize: 4,
        camera,
        viewportWidth: 800,
        viewportHeight: 600,
        getAnchors: () => anchors,
        onLabelClick,
      }),
    );
  });
  return { onLabelClick };
}

describe("CalloutLayer", () => {
  it("test_the_layer_uses_a_fixed_pool_of_divs_and_reuses_them (component level)", () => {
    renderLayer([{ id: "p1", position: new THREE.Vector3(0, 0, 0), text: "hot", kind: "measured" }]);
    const nodesBefore = Array.from(container.querySelectorAll(".callout"));
    expect(nodesBefore.length).toBe(4); // the pool, not the anchor count

    flushOneFrame();
    flushOneFrame();
    flushOneFrame();

    const nodesAfter = Array.from(container.querySelectorAll(".callout"));
    expect(nodesAfter.length).toBe(4);
    expect(nodesAfter).toEqual(nodesBefore); // same DOM nodes, never recreated
  });

  it("test_no_react_state_update_happens_in_the_animation_frame_callback", () => {
    renderLayer([{ id: "p1", position: new THREE.Vector3(0, 0, 0), text: "hot", kind: "measured" }]);
    const countAfterMount = __getCalloutLayerRenderCountForTest();

    flushOneFrame();
    flushOneFrame();
    flushOneFrame();
    flushOneFrame();

    // If the frame callback called setState, React would re-run this
    // component's function body on every one of those frames.
    expect(__getCalloutLayerRenderCountForTest()).toBe(countAfterMount);
  });

  it("moves a callout div's translate3d without any React state, across frames", () => {
    renderLayer([{ id: "p1", position: new THREE.Vector3(0, 0, 0), text: "hot", kind: "measured" }]);
    flushOneFrame();
    const el = container.querySelector(".callout") as HTMLDivElement;
    expect(el.style.transform).toMatch(/^translate3d\(/);
    expect(el.hidden).toBe(false);
  });

  it("hides an anchor behind the mounted camera", () => {
    renderLayer([{ id: "behind", position: new THREE.Vector3(0, 0, 10), text: "hidden", kind: "measured" }]);
    flushOneFrame();
    expect((container.querySelector(".callout") as HTMLDivElement).hidden).toBe(true);
  });

  it("keeps the fixed pool but uses current anchors and click ids after a rerender", () => {
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    const firstClick = vi.fn();
    const secondClick = vi.fn();
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      getAnchors: () => [{ id: "first", position: new THREE.Vector3(0, 0, 0), text: "first", kind: "derived" as const }],
      onLabelClick: firstClick,
    })));
    flushOneFrame();
    const callout = container.querySelector<HTMLDivElement>(".callout")!;

    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      getAnchors: () => [{ id: "second", position: new THREE.Vector3(0, 0, 0), text: "second", kind: "derived" as const }],
      onLabelClick: secondClick,
    })));
    flushOneFrame();

    expect(container.querySelector(".callout")).toBe(callout);
    expect(callout.dataset.calloutPartId).toBe("second");
    act(() => callout.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(firstClick).not.toHaveBeenCalled();
    expect(secondClick).toHaveBeenCalledWith("second");
  });

  it("uses the current viewport after a rerender without adding an animation loop", () => {
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    const anchors = [{ id: "center", position: new THREE.Vector3(0, 0, 0), text: "center", kind: "derived" as const }];
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      getAnchors: () => anchors,
    })));
    flushOneFrame();
    const callout = container.querySelector<HTMLDivElement>(".callout")!;
    expect(callout.style.transform).toContain("400px, 300px");

    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera,
      viewportWidth: 400,
      viewportHeight: 600,
      getAnchors: () => anchors,
    })));
    expect(queuedFrames).toHaveLength(1);
    flushOneFrame();

    expect(callout.style.transform).toContain("200px, 300px");
    expect(queuedFrames).toHaveLength(1);
  });

  it("uses a replacement camera and cancels the one remaining animation frame on unmount", () => {
    const perspective = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    perspective.position.set(0, 0, 5);
    perspective.updateMatrixWorld();
    perspective.updateProjectionMatrix();
    const orthographic = new THREE.OrthographicCamera(-2, 2, 1.5, -1.5, 0.1, 100);
    orthographic.position.set(0, 0, 5);
    orthographic.lookAt(0, 0, 0);
    orthographic.updateMatrixWorld();
    orthographic.updateProjectionMatrix();
    const anchors = [{ id: "right", position: new THREE.Vector3(1, 0, 0), text: "right", kind: "derived" as const }];
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera: perspective,
      viewportWidth: 800,
      viewportHeight: 600,
      getAnchors: () => anchors,
    })));
    flushOneFrame();
    const callout = container.querySelector<HTMLDivElement>(".callout")!;
    expect(callout.style.transform).not.toContain("600px");

    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 1,
      camera: orthographic,
      viewportWidth: 800,
      viewportHeight: 600,
      getAnchors: () => anchors,
    })));
    expect(queuedFrames).toHaveLength(1);
    flushOneFrame();

    expect(callout.style.transform).toContain("600px, 300px");
    expect(queuedFrames).toHaveLength(1);
    act(() => root!.unmount());
    root = null;
    expect(cancelRafSpy).toHaveBeenCalledTimes(1);
    expect(queuedFrames).toHaveLength(0);
  });

  it("reports anchors omitted by the fixed pool", () => {
    const onOverflowChange = vi.fn();
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 4,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      onOverflowChange,
      getAnchors: () => [
      { id: "a1", position: new THREE.Vector3(0, 0, 0), text: "anchor1", kind: "declared" as const },
      { id: "a2", position: new THREE.Vector3(0, 0, 0), text: "anchor2", kind: "measured" as const },
      { id: "a3", position: new THREE.Vector3(0, 0, 0), text: "anchor3", kind: "derived" as const },
      { id: "a4", position: new THREE.Vector3(0, 0, 0), text: "anchor4", kind: "catalog" as const },
      { id: "a5", position: new THREE.Vector3(0, 0, 0), text: "anchor5_dropped", kind: "declared" as const },
      ],
    })));
    flushOneFrame();

    expect(container.querySelectorAll(".callout")).toHaveLength(4);
    expect(onOverflowChange).toHaveBeenCalledWith(1);
  });

  it("reports that the overflow cleared without creating a new animation loop", () => {
    const onOverflowChange = vi.fn();
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    let anchors: CalloutAnchor[] = Array.from({ length: 5 }, (_, index) => ({
      id: `a${index}`,
      position: new THREE.Vector3(0, 0, 0),
      text: `anchor${index}`,
      kind: "derived",
    }));
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 4,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      onOverflowChange,
      getAnchors: () => anchors,
    })));
    flushOneFrame();
    anchors = anchors.slice(0, 2);
    flushOneFrame();

    expect(onOverflowChange).toHaveBeenLastCalledWith(0);
    expect(queuedFrames).toHaveLength(1);
  });

  it("reports overflow without a console warning", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const camera = new THREE.PerspectiveCamera(50, 800 / 600, 0.1, 100);
    camera.position.set(0, 0, 5);
    camera.updateMatrixWorld();
    camera.updateProjectionMatrix();
    root = createRoot(container);
    act(() => root!.render(createElement(CalloutLayer, {
      poolSize: 4,
      camera,
      viewportWidth: 800,
      viewportHeight: 600,
      onOverflowChange: vi.fn(),
      getAnchors: () => Array.from({ length: 5 }, (_, index) => ({
        id: `a${index}`,
        position: new THREE.Vector3(0, 0, 0),
        text: `anchor${index}`,
        kind: "derived" as const,
      })),
    })));
    flushOneFrame();

    expect(warnSpy).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});

// --- click-through: NOT BUILT, named with its blocker ----------------------
//
// test_a_label_is_click_through_to_its_source_and_its_run is intentionally
// absent. `SceneLabel` (api_payload.py) has no source-line or run-id field to
// click through to, and the selection read (e1-b3) hasn't landed to give one
// a destination. `CalloutLayer.onLabelClick` (above) already fires with the
// callout's own id -- the mechanism this test would exercise -- but "its
// source and its run" cannot be proven true without inventing the very
// contract e2-e refused to invent for the same reason.
