/**
 * `Canvas.tsx` — the scene mount. Plain `three` only (charter #1): a `PerspectiveCamera` +
 * `OrbitControls` from three's own examples (#2), a `ResizeObserver` that owns the resolution
 * uniform (#3, see `buildScene.handleResize`), one `Mesh` per decoded part built from e2-e's
 * decoder (#4). `translation` is never applied to positions (#5) — `buildScene`/`decode.ts`
 * never read it.
 *
 * The lens mount (E2-f3): `parts[i]` and `ctx.meshes[i]` share an index (buildScene.ts's
 * `.map`), so which mesh is coloured for the active lens is exactly `partsForLens(parts, lens)`
 * (`./lenses`) — the payload's own `ScenePart.lenses`, nothing composed here. No lens word is
 * read or written in this file.
 *
 * `useScene()` is the small context every downstream box (e2-f, e2-j2, e4-d) consumes — see its
 * shape at the bottom of this file's exports: `SceneContext` from `./buildScene`.
 *
 * e2-ss: a `pointerdown` on the canvas raycasts against the real mounted meshes
 * (`buildScene.pickPart`) and writes the hit part's payload id — or `null` on a miss — to the
 * shared selection store (`./selectionStore`). Nothing else: no highlight, no detail panel, no
 * camera-fly-to; those are other boxes.
 */
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { PART_MIME } from "./placeOnBody";
import {
  buildScene,
  disposeScene,
  applyViewInset,
  attachWasd,
  handleResize,
  refit,
  pickHit,
  type SceneHit,
  updatePixelRatio,
  type ScenePart,
  type SceneContext,
  type ViewInset,
} from "./buildScene";
import { setSelectedPartId } from "./selectionStore";
import { applyLens, applyTheme, skeletonMeshes, type Skeleton } from "./robotScene";
import { partsForLens, type LensId, type LensedPart } from "./lenses";

const SceneReactContext = createContext<SceneContext | null>(null);

/** Throws outside a mounted `<Canvas>` — there is no scene to read before it exists. */
export function useScene(): SceneContext {
  const ctx = useContext(SceneReactContext);
  if (!ctx) throw new Error("useScene() must be called within <Canvas>");
  return ctx;
}

export function Canvas({
  parts,
  sceneKey,
  lens,
  skeleton,
  onPick,
  onDropPart,
  inset,
  children,
}: {
  parts: (ScenePart & LensedPart)[];
  /** Contract plus content hash: geometry/camera state changes only for a new scene asset. */
  sceneKey: string;
  lens: LensId;
  /** Joint/link skeleton for robots with no mesh parts (see `robotScene.ts`). */
  skeleton?: Skeleton;
  /** Called with the picked part/joint id (or null) alongside the selection-store write. */
  onPick?: (id: string | null) => void;
  /** A listed part dropped on the canvas: its id and what the drop point hit (null on empty space). */
  onDropPart?: (part: string, hit: SceneHit | null) => void;
  /** Px of the canvas under floating UI; the robot is framed in the rest. */
  inset?: ViewInset;
  children?: ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const partsRef = useRef(parts);
  const [ctx, setCtx] = useState<SceneContext | null>(null);
  partsRef.current = parts;
  const skeletonRef = useRef(skeleton);
  skeletonRef.current = skeleton;
  const onPickRef = useRef(onPick);
  onPickRef.current = onPick;
  const onDropRef = useRef(onDropPart);
  onDropRef.current = onDropPart;
  const insetRef = useRef(inset);
  insetRef.current = inset;

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const width = container.clientWidth || 1;
    const height = container.clientHeight || 1;
    const sceneCtx = buildScene(
      canvas,
      partsRef.current,
      width,
      height,
      skeletonRef.current ? skeletonMeshes(skeletonRef.current) : [],
      insetRef.current,
    );
    applyTheme(sceneCtx);
    // The theme toggle flips data-theme: repaint (ponytail: an OS-scheme flip with no override repaints on next mount) from the new tokens.
    const retheme = () => applyTheme(sceneCtx);
    const themeObserver = new MutationObserver(retheme);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    setCtx(sceneCtx);

    let raf = 0;
    // Render on demand: orbiting ('change'), or anything that sets needsRender, draws one frame.
    sceneCtx.controls.addEventListener("change", () => { sceneCtx.needsRender = true; });
    const wasd = attachWasd(sceneCtx);
    let last = performance.now();
    const tick = (now = performance.now()) => {
      wasd.step(Math.min((now - last) / 1000, 0.1));
      last = now;
      sceneCtx.controls.update();
      if (sceneCtx.needsRender) {
        sceneCtx.needsRender = false;
        sceneCtx.renderer.render(sceneCtx.scene, sceneCtx.camera);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width: w, height: h } = entry.contentRect;
      if (w > 0 && h > 0) handleResize(sceneCtx, w, h);
    });
    observer.observe(container);

    // devicePixelRatio changes (a window dragged between displays) fire no resize event, so a
    // media query pinned to the CURRENT ratio is the only signal — it re-subscribes itself at
    // the new ratio each time it fires, since it stops matching once the ratio it names is gone.
    let mql: MediaQueryList | undefined;
    const watchRatio = () => {
      mql?.removeEventListener("change", onRatioChange);
      mql = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
      mql.addEventListener("change", onRatioChange);
    };
    function onRatioChange() {
      updatePixelRatio(sceneCtx, window.devicePixelRatio);
      watchRatio();
    }
    watchRatio();

    // e2-ss: pointer pick -> raycast against the real mounted scene -> the shared selection
    // store. A pick that hits nothing writes `null` explicitly (see selectionStore.ts) rather
    // than leaving whatever was selected before.
    const handlePick = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      const ndcX = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      const ndcY = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
      const id = pickHit(sceneCtx, ndcX, ndcY)?.id ?? null;
      setSelectedPartId(id);
      onPickRef.current?.(id);
    };
    canvas.addEventListener("pointerdown", handlePick);
    // A part dragged from the list: raycast at the drop point like a pick, but select nothing.
    const allow = (event: DragEvent) => { if (event.dataTransfer?.types.includes(PART_MIME)) event.preventDefault(); };
    const handleDrop = (event: DragEvent) => {
      const part = event.dataTransfer?.getData(PART_MIME);
      if (!part) return;
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const ndcX = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      const ndcY = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
      onDropRef.current?.(part, pickHit(sceneCtx, ndcX, ndcY));
    };
    canvas.addEventListener("dragover", allow);
    canvas.addEventListener("drop", handleDrop);

    return () => {
      cancelAnimationFrame(raf);
      wasd.dispose();
      themeObserver.disconnect();
      observer.disconnect();
      mql?.removeEventListener("change", onRatioChange);
      canvas.removeEventListener("pointerdown", handlePick);
      canvas.removeEventListener("dragover", allow);
      canvas.removeEventListener("drop", handleDrop);
      disposeScene(sceneCtx);
      setCtx(null);
    };
  }, [sceneKey]);

  // A new inset (panel opened, wider panel) re-frames the robot in the open area.
  const insetKey = inset ? `${inset.top},${inset.right},${inset.bottom},${inset.left}` : "";
  useEffect(() => {
    if (!ctx || !inset) return;
    applyViewInset(ctx, inset, ctx.resolution.x, ctx.resolution.y);
    refit(ctx);
  }, [ctx, insetKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Lens membership only — never rebuilds geometry. `partsForLens` is the one join (payload
  // `ScenePart.lenses`); members keep their colours, the rest are greyed under a view (hidden
  // only under "All"), see `applyLens`. This file composes no lens word or rule of its own.
  useEffect(() => {
    if (!ctx) return;
    const members = new Set(partsForLens(parts, lens));
    applyLens(ctx, parts.map((part) => members.has(part)), lens);
  }, [ctx, parts, lens]);

  return (
    <div ref={containerRef} style={{ width: "100%", height: "100%" }}>
      <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block" }} />
      {ctx ? <SceneReactContext.Provider value={ctx}>{children}</SceneReactContext.Provider> : null}
    </div>
  );
}
