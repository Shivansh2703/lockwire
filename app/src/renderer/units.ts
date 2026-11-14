import { useSyncExternalStore } from "react";

/**
 * GAP-units: the one Units setting. It changes how lengths and angles are SHOWN and TYPED, nothing else.
 * Engine requests and the lockfile keep their own units (mm or rad as before): every typed number goes
 * through `lenToMm` / `angleToRad` on the way out, every shown number through `lenNum` / `angleNum`.
 */
export interface Units { length: "mm" | "m"; angle: "deg" | "rad" }

const KEY = "lockwire.units";
const DEFAULT: Units = { length: "mm", angle: "deg" };

function load(): Units {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<Units> | null;
    return { length: raw?.length === "m" ? "m" : "mm", angle: raw?.angle === "rad" ? "rad" : "deg" };
  } catch { return DEFAULT; }
}
let state: Units = load();
const listeners = new Set<() => void>();

export const getUnits = (): Units => state;
export const useUnits = (): Units => useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, getUnits, getUnits);
/** Per viewer, in this browser only. Never sent to the engine. */
export function setUnits(next: Partial<Units>): void {
  state = { ...state, ...next };
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* private window or blocked storage: the choice lasts until reload */ }
  for (const l of listeners) l();
}

const K = (): { len: number; ang: number } => ({ len: state.length === "m" ? 0.001 : 1, ang: state.angle === "rad" ? 1 : 180 / Math.PI });
const trim = (v: number, d: number, pad: boolean): string => { const s = v.toFixed(d); return pad ? s : String(Number(s)); };

/** Millimetres to the number shown (0.1 mm resolution, whichever unit). `pad` keeps trailing zeros. */
export const lenNum = (mm: number, pad = false): string => trim(mm * K().len, state.length === "m" ? 4 : 1, pad);
export const lenUnit = (): string => state.length;
/** Radians to the number shown. */
export const angleNum = (rad: number, pad = false): string => trim(rad * K().ang, state.angle === "rad" ? 3 : 1, pad);
export const angleUnit = (): string => state.angle;
export const angleWord = (): string => (state.angle === "rad" ? "radian" : "degree");
/** A sweep's own number (whole degrees or mm, from the 3D check) in the shown unit. */
export const sweepNum = (v: number, unit: "deg" | "mm", pad = false): string => (unit === "deg" ? angleNum((v * Math.PI) / 180, pad) : lenNum(v, pad));
export const sweepUnit = (unit: "deg" | "mm"): string => (unit === "deg" ? angleUnit() : lenUnit());

/** What the person typed, back to what the engine takes: millimetres, radians. */
const r9 = (v: number): number => Math.round(v * 1e9) / 1e9; // 0.4 m is 400 mm, not 400.00000000000006
export const lenToMm = (typed: number): number => r9(state.length === "m" ? typed * 1000 : typed);
export const angleToRad = (typed: number): number => r9(state.angle === "rad" ? typed : (typed * Math.PI) / 180);

/** A value kept in the unit `unit` (a named size's own unit), as `{value, unit}` in the shown unit. Other units pass through. */
export function shown(value: number, unit: string | null | undefined): { value: number; unit: string } {
  const u = unit ?? "m";
  const mm = u === "mm" ? value : u === "m" ? value * 1000 : null;
  if (mm !== null) return { value: Number((mm * K().len).toPrecision(10)), unit: state.length };
  const rad = u === "rad" ? value : u === "deg" ? (value * Math.PI) / 180 : null;
  return rad === null ? { value, unit: u } : { value: Number((rad * K().ang).toPrecision(10)), unit: state.angle };
}
/** The inverse of `shown`: a typed number back to the unit it is kept in. */
export function kept(typed: number, unit: string | null | undefined): number {
  const u = unit ?? "m";
  if (u === "mm" || u === "m") return Number((lenToMm(typed) / (u === "m" ? 1000 : 1)).toPrecision(10));
  if (u === "rad" || u === "deg") return Number((angleToRad(typed) * (u === "deg" ? 180 / Math.PI : 1)).toPrecision(10));
  return typed;
}
