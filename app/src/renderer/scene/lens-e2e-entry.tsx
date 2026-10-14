/**
 * The browser-side entry for the lens e2e's hidden-window GPU readback (never a
 * screenshot, never a visible window). Mounts the REAL `Canvas` + `DesignLens` (the lens PillTabs) — nothing here is
 * reimplemented or mocked, same idiom as `e2e-pixel-entry.ts` for `buildScene` alone.
 *
 * The same rule governs this file: it authors no lens word. `window.__mount` takes the payload's own
 * `Graph`/parts as JSON: whatever labels the test fixture puts on the payload are what `DesignLens`
 * renders. Lens selection drives through a real DOM click on the real lens tab (`role="tab"`) button — never
 * a direct state call — so the proof exercises the same path a person clicking the app does.
 */
import { useEffect } from "react";
import { createRoot } from "react-dom/client";

import type { Graph } from "../../shared/api.gen";
import { Canvas, useScene } from "./Canvas";
import { DesignLens } from "../DesignPanel";
import { useDesignView } from "./designView";
import { LENS_IDS, type LensId, type LensedPart } from "./lenses";
import type { ScenePart, SceneContext } from "./buildScene";

type TestPart = ScenePart & LensedPart;

let liveCtx: SceneContext | null = null;

/** Resolved by an effect that has actually committed — the thing `__readCenterPixel` needs, not
 * a proxy for it (a `requestAnimationFrame` pair, which Chromium throttles in an unpainted
 * `show:false` window, the previous `nextTwoFrames()` could resolve before React had
 * committed `CtxCapture`'s effect at all, throwing "scene not mounted"). React's own
 * passive-effect flush is not rAF-driven, so awaiting it directly sidesteps the throttling
 * instead of racing it. Re-armed before each state change so the harness awaits exactly the
 * commit that change produces. */
let armedResolve: (() => void) | null = null;
function armSettle(): Promise<void> {
  return new Promise((resolve) => {
    armedResolve = resolve;
  });
}

/** Captures the real `SceneContext` `Canvas` builds, so the harness can force a render and read
 * the framebuffer back without guessing at the internal rAF loop's timing. This effect is also
 * `__mount`'s readiness signal: `ctx` becoming non-null (a state update local to `Canvas`, so it
 * does not re-render `TestApp` above it) is exactly the event `window.__mount` is waiting for. */
function CtxCapture() {
  const ctx = useScene();
  useEffect(() => {
    liveCtx = ctx;
    armedResolve?.();
    return () => {
      liveCtx = null;
    };
  }, [ctx]);
  return null;
}

function TestApp({ parts, graph }: { parts: TestPart[]; graph: Graph }) {
  // DesignLens shows its tabs only for a payload with a graph and a mesh scene.
  const payload = { graph, scene: { parts } } as unknown as Parameters<typeof DesignLens>[0]["payload"];
  const { lens } = useDesignView();
  // No dependency array: fires after every commit *of this component*, i.e. whenever `lens`
  // changes (a lens tab click, via the design-view store) — unlike `CtxCapture`'s effect above, this one
  // does not fire for `Canvas`'s own internal `setCtx`, since that re-renders only `Canvas`'s
  // subtree, not this ancestor. React flushes passive effects bottom-up, so by the time this
  // (the outermost component) runs, `Canvas`'s own mesh-visibility effect (deeper in the tree,
  // reacting to the same `lens` change) has already committed. That's `__selectLens`'s readiness
  // signal — `CtxCapture`'s effect above does not re-fire on a lens change (its dependency, `ctx`,
  // is unchanged), so it cannot serve this second wait.
  useEffect(() => {
    if (liveCtx) armedResolve?.();
  });
  return (
    <Canvas parts={parts} sceneKey="lens-e2e" lens={lens}>
      <CtxCapture />
      <DesignLens payload={payload} />
    </Canvas>
  );
}

declare global {
  interface Window {
    __mount: (partsJson: string, graphJson: string) => Promise<void>;
    __selectLens: (lens: string) => Promise<void>;
    __readCenterPixel: () => [number, number, number, number];
  }
}

window.__mount = async (partsJson, graphJson) => {
  const parts = JSON.parse(partsJson) as TestPart[];
  const graph = JSON.parse(graphJson) as Graph;
  const container = document.getElementById("root");
  if (!container) throw new Error("#root not found");
  const root = createRoot(container);
  const settled = armSettle();
  root.render(<TestApp parts={parts} graph={graph} />);
  await settled;
};

window.__selectLens = async (lens) => {
  const index = LENS_IDS.indexOf(lens as LensId);
  if (index < 0) throw new Error(`unknown lens ${lens}`);
  const buttons = document.querySelectorAll<HTMLButtonElement>('[aria-label="Lens"] [role="tab"]');
  const button = buttons[index];
  if (!button) throw new Error(`no lens tab at index ${index}`);
  const settled = armSettle();
  button.click();
  await settled;
};

window.__readCenterPixel = () => {
  if (!liveCtx) throw new Error("scene not mounted");
  liveCtx.renderer.render(liveCtx.scene, liveCtx.camera);
  const gl = liveCtx.renderer.getContext();
  const pixel = new Uint8Array(4);
  gl.readPixels(Math.floor(gl.canvas.width / 2), Math.floor(gl.canvas.height / 2), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
  return Array.from(pixel) as [number, number, number, number];
};
