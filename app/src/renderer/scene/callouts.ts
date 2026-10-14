// Callout layer mechanics (plan v8 line 547): project a 3D anchor to screen
// space and write `translate3d(...)` onto a FIXED POOL of divs, never through
// React state (a `useState` in the per-frame callback is the mistake v8 names
// outright — it re-renders the whole panel 60x/sec).
//
// This file does not know a source line or run: `SceneLabel` carries only a
// payload part id and label fields. `onLabelClick` therefore passes that id
// through untouched; callers may use their existing part selection path but
// must not invent source/run routing here.

import * as THREE from "three";

/** the five kinds, mirrored from `api_payload.NumberKind` — read here, not
 * re-declared as a source of truth; this file only asks "declared or not". */
export type LabelKind = "declared" | "measured" | "derived" | "catalog" | "projected";

/** a `declared` figure is drawn in the declared style; every other kind
 * (measured/derived/catalog/projected) is drawn as derived — a value this box
 * did not itself declare must never look identical to one that was. */
export function labelStyle(kind: LabelKind | null | undefined): "declared" | "derived" {
  return kind === "declared" ? "declared" : "derived";
}

export interface ScreenAnchor {
  x: number;
  y: number;
  /** Anchor is behind the camera — caller should not draw this callout at all. */
  behindCamera: boolean;
}

/** Pure projection: world position -> viewport pixels, via the camera's own
 * NDC transform (`Object3D.project`). No DOM, no React, no Canvas mount. */
export function projectAnchor(
  camera: THREE.Camera,
  position: THREE.Vector3,
  viewportWidth: number,
  viewportHeight: number,
): ScreenAnchor {
  const ndc = position.clone().project(camera);
  return {
    x: (ndc.x * 0.5 + 0.5) * viewportWidth,
    y: (-ndc.y * 0.5 + 0.5) * viewportHeight,
    behindCamera: ndc.z > 1,
  };
}

export interface CalloutPoolEntry {
  readonly el: HTMLDivElement;
  /** The callout id currently occupying this slot; `null` = slot unused this frame. */
  id: string | null;
}

/** Creates the fixed pool once. Never call this per frame — the whole point
 * of a pool is that no div is created or removed after this. */
export function createCalloutPool(size: number, container: HTMLElement): CalloutPoolEntry[] {
  const pool: CalloutPoolEntry[] = [];
  for (let i = 0; i < size; i++) {
    const el = document.createElement("div");
    el.className = "callout";
    el.style.position = "absolute";
    el.style.left = "0";
    el.style.top = "0";
    el.style.zIndex = "1";
    el.style.padding = "2px 4px";
    el.style.border = "1px solid var(--line)";
    el.style.background = "var(--surface)";
    el.style.color = "var(--ink)";
    el.style.cursor = "pointer";
    el.style.pointerEvents = "auto";
    el.hidden = true;
    container.appendChild(el);
    pool.push({ el, id: null });
  }
  return pool;
}

export interface PlacedCallout {
  id: string;
  x: number;
  y: number;
  text: string;
  kind: LabelKind;
}

/** THE animation-frame callback's body: writes `translate3d` directly onto the
 * already-created pool elements. No `setState`, no new/removed DOM nodes —
 * every slot beyond `placed.length` is just hidden, and slot i always reuses
 * the same `HTMLDivElement` it was given at pool-creation time. */
export function updateCalloutPositions(pool: readonly CalloutPoolEntry[], placed: readonly PlacedCallout[]): void {
  for (let i = 0; i < pool.length; i++) {
    const entry = pool[i];
    if (!entry) continue;
    const callout = placed[i];
    if (!callout) {
      entry.id = null;
      entry.el.hidden = true;
      delete entry.el.dataset.calloutPartId;
      continue;
    }
    entry.id = callout.id;
    entry.el.hidden = false;
    entry.el.textContent = callout.text;
    entry.el.dataset.calloutPartId = callout.id;
    entry.el.dataset.kind = labelStyle(callout.kind);
    entry.el.style.transform = `translate3d(${callout.x}px, ${callout.y}px, 0)`;
  }
}
