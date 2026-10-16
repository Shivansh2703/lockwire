/**
 * What the Design page toggles on the shared RobotView: the active lens, whether callouts show,
 * how many exceed the label limit, and whether the analysis panel is open. One tiny store (like
 * `selectionStore.ts`) so the left Parts panel, the bottom tools and the canvas in the Shell
 * agree without App owning any of it.
 */
import { useSyncExternalStore } from "react";

import type { LensId } from "./lenses";

export interface DesignView {
  lens: LensId; callouts: boolean; overflow: number; analysis: boolean;
  /** Ids a Connections row under the pointer lights, without moving the camera or the selection. */
  hover: string[] | null;
}

const INITIAL: DesignView = { lens: "off", callouts: false, overflow: 0, analysis: false, hover: null };
let state = INITIAL;
const listeners = new Set<() => void>();

export function setDesignView(patch: Partial<DesignView>): void {
  const next = { ...state, ...patch };
  if ((Object.keys(next) as (keyof DesignView)[]).every((k) => next[k] === state[k])) return;
  state = next;
  for (const listener of listeners) listener();
}

export function resetDesignView(): void {
  setDesignView(INITIAL);
}

export function useDesignView(): DesignView {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    () => state,
  );
}

export const getDesignView = (): DesignView => state;
