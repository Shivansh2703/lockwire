// The callout layer component (plan v8 line 547). Mounts a FIXED POOL of
// divs once, then moves them every animation frame by writing `translate3d`
// directly onto the DOM nodes — never through `useState`/`useReducer` — so
// orbiting the model never re-renders this component.
//
// NOT WIRED HERE (named, not invented, per the box brief): the real camera
// and real anchors come from `Canvas.tsx`, which this box does not build —
// it is blocked on the lens box (e2-f, no commit past its base as of this
// box) and on the scene-hash cache / selection read (e1-b2/e1-b3), neither
// merged. `getAnchors`/`camera` are accepted as props so this component is
// ready to be handed real ones the moment they exist, without this file
// changing.

import { useEffect, useRef } from "react";
import * as THREE from "three";

import { dockCallouts } from "./dock";
import {
  createCalloutPool,
  projectAnchor,
  updateCalloutPositions,
  type CalloutPoolEntry,
  type LabelKind,
} from "./callouts";

export interface CalloutAnchor {
  id: string;
  position: THREE.Vector3;
  text: string;
  kind: LabelKind;
}

export interface CalloutLayerProps {
  poolSize: number;
  camera: THREE.Camera;
  viewportWidth: number;
  viewportHeight: number;
  getAnchors: () => CalloutAnchor[];
  /** Fires with the callout's payload part id. */
  onLabelClick?: (id: string) => void;
  /** Reports anchors omitted because the fixed pool is full. */
  onOverflowChange?: (count: number) => void;
}

// Test-only render counter: a `useState` anywhere in the frame callback would
// make this component's own function body run again every frame, and this is
// the cheapest legible way to prove it does not (no `setState` call to spy on
// directly — the absence of a re-render is what the claim is about).
let _renderCount = 0;
export function __resetCalloutLayerRenderCountForTest(): void {
  _renderCount = 0;
}
export function __getCalloutLayerRenderCountForTest(): number {
  return _renderCount;
}

export function CalloutLayer(props: CalloutLayerProps) {
  _renderCount++;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<number | null>(null);
  // Props can change while the fixed pool stays mounted. Refs keep the direct DOM loop and
  // click handlers current without tearing down or rebuilding that pool.
  const getAnchorsRef = useRef(props.getAnchors);
  const onLabelClickRef = useRef(props.onLabelClick);
  const onOverflowChangeRef = useRef(props.onOverflowChange);
  const overflowRef = useRef<number | null>(null);
  const projectionRef = useRef({
    camera: props.camera,
    width: props.viewportWidth,
    height: props.viewportHeight,
  });
  getAnchorsRef.current = props.getAnchors;
  onLabelClickRef.current = props.onLabelClick;
  onOverflowChangeRef.current = props.onOverflowChange;
  projectionRef.current = {
    camera: props.camera,
    width: props.viewportWidth,
    height: props.viewportHeight,
  };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const pool: CalloutPoolEntry[] = createCalloutPool(props.poolSize, container);
    const onClick = (entry: CalloutPoolEntry) => () => {
      if (entry.id) onLabelClickRef.current?.(entry.id);
    };
    pool.forEach((entry) => entry.el.addEventListener("click", onClick(entry)));

    // Idle frames do nothing: the pool is only re-placed when the camera, viewport or anchors differ from last frame.
    let lastKey = "";
    const tick = () => {
      frameRef.current = requestAnimationFrame(tick);
      const { camera, width, height } = projectionRef.current;
      const allAnchors = getAnchorsRef.current();
      const key = [
        ...camera.matrixWorld.elements, ...camera.projectionMatrix.elements, width, height,
        ...allAnchors.flatMap((a) => [a.id, a.text, a.kind, a.position.x, a.position.y, a.position.z]),
      ].join(",");
      if (key === lastKey) return;
      lastKey = key;
      const overflow = Math.max(0, allAnchors.length - props.poolSize);
      if (overflowRef.current !== overflow) {
        overflowRef.current = overflow;
        onOverflowChangeRef.current?.(overflow);
      }
      const anchors = allAnchors.slice(0, props.poolSize);
      const projected = anchors.map((anchor) => {
        const screen = projectAnchor(camera, anchor.position, width, height);
        return { ...anchor, ...screen };
      });
      const visible = projected.filter((p) => !p.behindCamera);
      const docked = dockCallouts(
        visible.map((p) => ({ id: p.id, x: p.x, y: p.y, width: 120, height: 24 })),
        { width, height },
      );
      // dockCallouts returns exactly one box per input box, in order (see dock.ts).
      const placed = visible.map((p, i) => ({ id: p.id, x: docked[i]!.x, y: docked[i]!.y, text: p.text, kind: p.kind }));
      updateCalloutPositions(pool, placed);
    };
    frameRef.current = requestAnimationFrame(tick);

    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      pool.forEach((entry) => entry.el.remove());
    };
    // Pool lifetime is poolSize only; refs above keep callbacks current without remounting it.
  }, [props.poolSize]);

  return <div ref={containerRef} className="callout-layer" style={{ position: "absolute", inset: 0, pointerEvents: "none" }} />;
}
