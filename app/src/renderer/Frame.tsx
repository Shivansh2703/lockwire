/// <reference types="vite/client" />
import { pro } from "./proRegistry";

// The app's own mode list, not the robot's words. Free modes, plus
// whatever the optional pro/ folder registers (it sits before Docs, after Plan).
const BEFORE = ["Design", "Declare", "Check", "Plan"] as const;
export type FreeMode = (typeof BEFORE)[number] | "Reference";
export type Mode = FreeMode | (string & {});
export const MODES: readonly Mode[] = [...BEFORE, ...(pro?.modes.map((m) => m.id) ?? []), "Reference"];
