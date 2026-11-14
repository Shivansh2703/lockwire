import type { Mode } from "./Frame";
import type { ViewAction } from "./scene/viewState";

// The app's keyboard shortcuts: the pure rules (what a key means) live here, the handlers elsewhere.
type KeyLike = Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey"> & { code?: string };

export const typing = (el: Element | null) => !!el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || (el as HTMLElement).isContentEditable);
export const onMac = () => typeof navigator !== "undefined" && /Mac/.test(navigator.platform);

export type CameraView = keyof typeof VIEW_DIRECTION;
export type ViewKey = "zoom" | "clear" | CameraView | ViewAction;
// h hide, i only, t see through, m wireframe ("mesh"), l frames, Shift+H show everything. w/a/s/d move the camera, g starts a typed move.
const VIEW_KEYS: Record<string, ViewKey> = { f: "zoom", "1": "front", "3": "side", "7": "top", Escape: "clear", h: "hide", i: "only", t: "seeThrough", m: "wireframe", l: "frames", "0": "iso", "5": "ortho" };
/** Where the camera sits, as a direction from the robot (Z up, X forward). Top and bottom are tilted a hair: straight up has no heading. Back, left and bottom are the far sides of front, side and top. */
export const VIEW_DIRECTION = { front: [1, 0, 0], side: [0, 1, 0], top: [0.001, 0, 1], back: [-1, 0, 0], left: [0, -1, 0], bottom: [0.001, 0, -1], iso: [1, 1, 1] } as const;
// Shift+1/3/7 look from the other side. Not Ctrl: Ctrl+1/2/3 already switches screen off macOS. Matched by code, since Shift+1 types "!".
const OPPOSITE: Record<string, CameraView> = { Digit1: "back", Digit3: "left", Digit7: "bottom" };

/** What a key does in the 3D view (the caller checks the view has focus and nobody is typing). */
export function viewKey(e: KeyLike & { shiftKey?: boolean }): ViewKey | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  if (e.shiftKey && e.code && OPPOSITE[e.code]) return OPPOSITE[e.code]!;
  if (e.shiftKey && e.key.toLowerCase() === "h") return "showAll";
  return VIEW_KEYS[e.key.length === 1 ? e.key.toLowerCase() : e.key] ?? null;
}

const SCREENS: readonly Mode[] = ["Design", "Check", "Plan"];
/** Cmd+1/2/3 (Ctrl off macOS): the screen to switch to. */
export function screenKey(e: KeyLike, mac: boolean): Mode | null {
  if (!(mac ? e.metaKey : e.ctrlKey) || e.altKey) return null;
  return SCREENS[Number(e.key) - 1] ?? null;
}

export const isAddPartKey = (e: KeyLike & Pick<KeyboardEvent, "shiftKey">) => e.key === "A" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey;
export const isHelpKey = (e: KeyLike) => e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey;

/** G, X, 20, Enter: a typed move. `axis` 0/1/2 is x/y/z; `text` is the millimetres typed so far. */
export type TypedMove = { axis: 0 | 1 | 2 | null; text: string };
export const isMoveKey = (e: KeyLike & Pick<KeyboardEvent, "shiftKey">) => e.key.toLowerCase() === "g" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey;
const AXES = { x: 0, y: 1, z: 2 } as const;

/** One key while a typed move is open: the next state (null closes it), and `commit` when Enter sent a usable number. Keys with a modifier are not ours (null). */
export function typedMoveStep(s: TypedMove, e: KeyLike): { state: TypedMove | null; commit?: number[] } | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  const k = e.key.toLowerCase();
  if (k === "escape") return { state: null };
  if (k === "x" || k === "y" || k === "z") return { state: { ...s, axis: AXES[k] } };
  if (/^[0-9.]$/.test(k) || (k === "-" && s.text === "")) return { state: { ...s, text: s.text + k } };
  if (k === "backspace") return { state: { ...s, text: s.text.slice(0, -1) } };
  if (k === "enter") {
    const n = Number(s.text);
    if (s.axis === null || s.text === "" || !Number.isFinite(n)) return { state: s };
    return { state: null, commit: [0, 1, 2].map((i) => (i === s.axis ? n : 0)) };
  }
  return { state: s }; // any other plain key is swallowed, so 1/3/7/F do not move the camera mid-entry
}

/** The sheet's rows: keys, then what they do. */
export function shortcutRows(mac: boolean, lengthUnit = "mm"): [string, string][] {
  const cmd = mac ? "Cmd" : "Ctrl";
  return [
    ["Arrow keys", "move the camera"],
    ["F", "zoom to selection"],
    ["1 / 3 / 7", "front, left side, top"],
    ["Shift+1 / 3 / 7", "back, right side, bottom"],
    ["0", "corner (isometric) view"],
    ["5", "flat (orthographic) view on or off"],
    ["Esc", "clear selection"],
    ["H / I", "hide the selected part / show only it"],
    ["T / M / L", "see through / wireframe / frames and axes"],
    ["Shift+H", "show everything"],
    [`${cmd}+1/2/3`, "switch screen"],
    [`${cmd}+O`, "open a robot"],
    [`${cmd}+F`, "find a part by name"],
    ["Shift+A", "add a part"],
    ["G, X/Y/Z, number, Enter", `move the selected part that many ${lengthUnit}`],
    ["Delete / Backspace", "delete the selected part (3D view or Parts list focused)"],
    [`${cmd}+Z`, "undo the last edit"],
    [`${cmd}+Shift+Z`, "redo it"],
    ["?", "this list"],
  ];
}
