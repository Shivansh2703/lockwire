// The five lenses a viewer can look through: `off · power · data · mechanical`, plus
// `health` (plan v8 line 456). This file owns the lens's own state — which lens is
// active, and that it survives a payload poll replacing the data underneath it — and
// the one join a lens needs against the model: which parts it draws.
//
// e1-ln (E1-ln) landed the two contracts e2-f named as blocking: `Graph`'s five
// lenses each carry their own `label`/`caption` off the payload (`GraphLens.label` /
// `.caption`, `report_html._LENS_META`), and `ScenePart.lenses: LensId[]` names every
// lens a part belongs to (`scene_view.part_lenses`). Both are read here, composed
// nowhere — see `DesignLens` in `DesignPanel.tsx` for the words, `partsForLens` below for the membership.
//
// A comment used to stand here claiming none of `Graph`, `GraphLens`, `GraphNode`
// carries a caption or label string "anywhere" — and two independent workers, on two
// different bases (071c881 and 7301cd8), wrote that exact sub-claim in their own words.
// It was FALSE about `GraphNode` even at the time either was written: `GraphNode.label`
// already existed at e2-f's base (551ded1) and every later one, unchanged by e1-ln
// (`report_html._graph`, e.g. `{"id": device.id, "label": device.id, ...}` — a payload
// string, the node's device or link name). Checked: both workers' `lenses.ts` carry the
// wrong claim in different prose, not one copying the other's comment verbatim, so the
// shared error came from conflating "`GraphLens` has no label/caption" (true, and the
// real blocker) with "`GraphNode` has no label" (false, and never the blocker) rather
// than from reading each other's file. The corrected claim: only `GraphLens` was ever
// missing its words; `GraphNode.label` was always there.

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
    // no-op by design — see class docstring above
  }
}

/** A part carrying `lenses` (`ScenePart.lenses` off the payload, e.g. `app/src/shared/api.gen.ts`'s
 * `ScenePart`) — only the one field this join needs, so callers do not have to import the whole
 * generated type just to filter. */
export interface LensedPart {
  lenses: readonly LensId[];
}

/** The membership join a lens mount needs: which parts a lens draws is exactly the parts whose
 * `ScenePart.lenses` (payload-computed, `scene_view.part_lenses`) names that lens — nothing
 * inferred or guessed here. Whoever owns the render loop (Canvas.tsx, not built in this box — see
 * ThickLine.tsx) calls this per lens change and shows/hides meshes by the result. */
export function partsForLens<T extends LensedPart>(parts: readonly T[], lens: LensId): T[] {
  return parts.filter((part) => part.lenses.includes(lens));
}
