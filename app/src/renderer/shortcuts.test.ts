import { describe, expect, it } from "vitest";

import { isHelpKey, screenKey, shortcutRows, viewKey } from "./shortcuts";

const key = (k: string, mods: Partial<KeyboardEvent> = {}) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false, ...mods });

describe("shortcuts", () => {
  it("view keys: F zoom, 1/3/7 front/side/top, Esc clear; nothing with a modifier or W/A/S/D", () => {
    expect(["f", "F", "1", "3", "7", "Escape"].map((k) => viewKey(key(k)))).toEqual(["zoom", "zoom", "front", "side", "top", "clear"]);
    expect(viewKey({ ...key("!"), code: "Digit1", shiftKey: true })).toBe("back");
    expect(["0", "5"].map((k) => viewKey(key(k)))).toEqual(["iso", "ortho"]);
    expect(viewKey({ ...key("1"), code: "Digit1", shiftKey: true, ctrlKey: true })).toBeNull();
    expect(viewKey(key("f", { metaKey: true }))).toBeNull();
    expect(["w", "a", "s", "d", "2"].map((k) => viewKey(key(k)))).toEqual([null, null, null, null, null]);
  });
  it("Cmd+1/2/3 on a Mac, Ctrl+1/2/3 elsewhere, switch Design/Check/Plan", () => {
    expect(["1", "2", "3", "4"].map((k) => screenKey(key(k, { metaKey: true }), true))).toEqual(["Design", "Check", "Plan", null]);
    expect(screenKey(key("2", { ctrlKey: true }), false)).toBe("Check");
    expect(screenKey(key("2", { ctrlKey: true }), true)).toBeNull();
    expect(screenKey(key("2"), true)).toBeNull();
  });
  it("? opens the list; the sheet shows Ctrl off macOS", () => {
    expect(isHelpKey(key("?"))).toBe(true);
    expect(isHelpKey(key("?", { metaKey: true }))).toBe(false);
    expect(shortcutRows(false).map(([k]) => k)).toContain("Ctrl+O");
    expect(shortcutRows(true).map(([k]) => k)).toContain("Shift+1 / 3 / 7");
    expect(shortcutRows(true)[0]).toEqual(["Arrow keys", "move the camera"]);
  });
});
