// The five lenses a viewer can look through: `off · power · data · mechanical`, plus
// `health`. This file owns the lens's own state: which lens is
// active, and that it survives a payload poll replacing the data underneath it: and
// the one join a lens needs against the model: which parts it draws.
//
// The payload now carries the two contracts this file needed: `Graph`'s five
// lenses each carry their own `label`/`caption` off the payload (`GraphLens.label` /
// `.caption`, `report_html._LENS_META`), and `ScenePart.lenses: LensId[]` names every
// lens a part belongs to (`scene_view.part_lenses`). Both are read here, composed
// nowhere: see `DesignLens` in `DesignPanel.tsx` for the words, `partsForLens` below for the membership.
//
// `GraphNode.label` (the node's device or link name, `report_html._graph`) has always been on the
// payload; only `GraphLens` once lacked its label and caption.

export type LensId = "off" | "power" | "data" | "mechanical" | "health";

export const LENS_IDS: readonly LensId[] = ["off", "power", "data", "mechanical", "health"];

/** Which lens is active. View state, not data: a payload replacement (a poll tick
 * swapping the whole payload object for a fresh one) must never reset it, and it is
 * restored rather than re-derived when returning from the full-screen view. */
export class LensState {
  private active: LensId;

  constructor(initial: LensId = "off") {
    this.active = initial;
  }

  get(): LensId {
    return this.active;
  }

  set(lens: LensId): void {
    this.active = lens;
  }

  /** Call this when a fresh payload replaces the old one. Deliberately a no-op: only an
   * explicit `set` may change the active lens. */
  onPayloadReplaced(): void {
    // no-op by design: see class docstring above
  }
}

/** A part carrying `lenses` (`ScenePart.lenses` off the payload, e.g. `app/src/shared/api.gen.ts`'s
 * `ScenePart`): only the one field this join needs, so callers do not have to import the whole
 * generated type just to filter. */
export interface LensedPart {
  lenses: readonly LensId[];
}

/** The membership join a lens mount needs: which parts a lens draws is exactly the parts whose
 * `ScenePart.lenses` (payload-computed, `scene_view.part_lenses`) names that lens: nothing
 * inferred or guessed here. Canvas.tsx, which owns the render loop, calls this per lens change and shows or hides
 * meshes by the result. */
export function partsForLens<T extends LensedPart>(parts: readonly T[], lens: LensId): T[] {
  return parts.filter((part) => part.lenses.includes(lens));
}
