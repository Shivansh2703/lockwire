/**
 * e2-ss — the selection store: which part id the pointer picked, and nothing else. `Canvas.tsx`
 * is the only writer (via a pointer pick against the mounted scene); any other face reads it
 * with `useSelectedPartId()`. `null` is a real, distinct state — "nothing is selected" — not the
 * absence of one; a pick on empty space writes it explicitly rather than leaving the prior value.
 *
 * The id held here is always the payload's own `ScenePart.id` (adjacent: identity comes
 * from the payload, never a mesh index or a name composed in the renderer) — see
 * `buildScene.pickPart`, the only writer's source.
 */
import { useSyncExternalStore } from "react";

export type Selection = string | null;

type Listener = () => void;

let selected: Selection = null;
let selectedSceneKey: string | undefined;
const listeners = new Set<Listener>();

export function getSelectedPartId(): Selection {
  return selected;
}

/** a Parts-tree group header selected as a whole; its member ids all highlight. A part pick
 * (or an empty-space pick) always clears it. */
export type GroupSelection = { id: string; members: string[] } | null;
let group: GroupSelection = null;

export function getSelectedGroup(): GroupSelection {
  return group;
}

export function setSelectedPartId(id: Selection): void {
  if (id === selected && group === null) return;
  selected = id;
  group = null;
  for (const listener of listeners) listener();
}

export function setSelectedGroup(next: GroupSelection): void {
  selected = null;
  group = next;
  for (const listener of listeners) listener();
}

/** A health poll leaves the contract-plus-content key alone, so its payload ids remain selected.
 * A replacement asset or contract gets a new key: identical ids there must not retain the prior
 * asset's selection. */
export function setSelectionSceneKey(sceneKey: string | undefined): void {
  if (sceneKey && sceneKey === selectedSceneKey) return;
  setSelectedPartId(null);
  selectedSceneKey = sceneKey;
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The read side every consumer (a face that shows the selected part, the scene face) uses. */
export function useSelectedPartId(): Selection {
  return useSyncExternalStore(subscribe, getSelectedPartId);
}

export function useSelectedGroup(): GroupSelection {
  return useSyncExternalStore(subscribe, getSelectedGroup);
}
