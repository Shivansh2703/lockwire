/**
 * VIEW-1: what the person has done to the 3D view (hidden parts, only-these parts, see-through,
 * wireframe, frames, centre of mass). View state only: it is never sent anywhere or written to the
 * lockfile. One tiny store, like `designView.ts`, so the View menu, the keys and the canvas agree.
 */
import { useSyncExternalStore } from "react";

import type { RobotJoint, WiringWire } from "../../shared/api.gen";
import type { ScenePart } from "./buildScene";
import type { CameraView } from "../shortcuts";
import { getSelectedGroup, getSelectedPartId } from "./selectionStore";

/** TEAM-02: an older revision laid over this one. Drawn only; the Pro diff fills it, nothing else writes it. */
export interface Comparison {
  /** The older revision's sha: which history row is showing. */
  sha: string;
  /** The older revision's parts; `mark` is null for a part that did not move (drawn plain). */
  ghost: { part: ScenePart; mark: "moved" | "removed" | null }[];
  wires: { added: string[]; removed: WiringWire[] };
  /** Mass change, in plain numbers, ready to show. */
  mass: { total: string | null; parts: string[] };
}
export interface ViewState { hidden: string[]; only: string[]; seeThrough: boolean; wireframe: boolean; frames: boolean; com: boolean; ortho: boolean;
  /** POSE-1: each posed joint's value, radians or metres. For viewing only; never written anywhere. */
  pose: Record<string, number>;
  /** TEAM-02: the version diff, or null. */
  compare: Comparison | null }
export type ViewAction = "hide" | "only" | "seeThrough" | "wireframe" | "frames" | "ortho" | "showAll";

const INITIAL: ViewState = { hidden: [], only: [], seeThrough: false, wireframe: false, frames: false, com: true, ortho: false, pose: {}, compare: null };
let state = INITIAL;
const listeners = new Set<() => void>();
const set = (next: ViewState) => { state = next; for (const l of listeners) l(); };

export const getViewState = (): ViewState => state;
export const useViewState = (): ViewState => useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, getViewState);

/** True when anything differs from the plain view (what "Show everything" would undo). */
export const viewChanged = (v: ViewState): boolean => !!(v.hidden.length || v.only.length || v.seeThrough || v.wireframe || v.frames || v.ortho || !v.com || isPosed(v) || !!v.compare);

const selection = (): string[] => getSelectedGroup()?.members ?? (getSelectedPartId() ? [getSelectedPartId()!] : []);
const flip = (list: string[], ids: string[]) => (ids.every((i) => list.includes(i)) ? list.filter((i) => !ids.includes(i)) : [...new Set([...list, ...ids])]);

/** Run one View action; the menu and the keys both come through here. */
export function viewAction(a: ViewAction): void {
  const ids = selection();
  if (a === "hide" && ids.length) set({ ...state, hidden: flip(state.hidden, ids) });
  else if (a === "only" && ids.length) set({ ...state, only: flip(state.only, ids) });
  else if (a === "seeThrough") set({ ...state, seeThrough: !state.seeThrough });
  else if (a === "wireframe") set({ ...state, wireframe: !state.wireframe });
  else if (a === "frames") set({ ...state, frames: !state.frames });
  else if (a === "ortho") set({ ...state, ortho: !state.ortho });
  else if (a === "showAll") set(INITIAL);
}
/** GAP-views: the menu asks the mounted 3D view to look from here (the keys reach the same place). */
export const CAMERA_VIEW_EVENT = "lockwire-camera-view";
export const cameraView = (v: CameraView): void => { window.dispatchEvent(new CustomEvent(CAMERA_VIEW_EVENT, { detail: v })); };
export const setCentreOfMass = (com: boolean): void => set({ ...state, com });

/** The part ids to keep drawn: GRAPH-1's list and the person's "only" list together (both apply). null keeps all. */
export function keepIds(graph: string[] | null, only: string[]): string[] | null {
  if (!only.length) return graph;
  return graph ? graph.filter((id) => only.includes(id)) : only;
}

/** GRAPH-2: keep only these parts (the graph's "only these"); an empty list brings everything back. */
export const setOnlyParts = (ids: string[]): void => set({ ...state, only: [...new Set(ids)] });

/** POSE-1: the joints a slider can move, and how far (radians or metres). A joint with no declared limit gets a plain half turn, or 100 mm. */
export const isMovable = (j: Pick<RobotJoint, "type">): boolean => j.type === "revolute" || j.type === "continuous" || j.type === "prismatic";
export const poseRange = (j: Pick<RobotJoint, "type" | "lower" | "upper">): [number, number] => {
  const wide = j.type === "prismatic" ? 0.1 : Math.PI;
  return [j.lower ?? -wide, j.upper ?? wide];
};
export const isPosed = (v: ViewState): boolean => Object.keys(v.pose).length > 0;

/** Pose one joint, held inside its limits. Zero drops it from the pose. */
export function setJointPose(j: Pick<RobotJoint, "name" | "type" | "lower" | "upper">, q: number): void {
  const [lo, hi] = poseRange(j);
  const v = Math.min(hi, Math.max(lo, q));
  const { [j.name]: _drop, ...rest } = state.pose;
  set({ ...state, pose: v === 0 ? rest : { ...rest, [j.name]: v } });
}
/** TEAM-02: lay an older revision over this one (null clears it). */
export const setComparison = (compare: Comparison | null): void => set({ ...state, compare });
export const zeroPose = (): void => set({ ...state, pose: {} });
