import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// @ts-expect-error the repo's contrast script is plain .mjs with no types
import { THEME_CSS_PATH } from "../../scripts/contrast.mjs";

const css = readFileSync(THEME_CSS_PATH, "utf8");
const rule = (sel: string) => [...css.matchAll(new RegExp(`(?:^|\\n|\\})\\s*${sel.replace(/[.[\]()>:"=^$*+?|\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`, "g"))].map((m) => m[1]).join(";");
const px = (body: string, prop: string) => Number([...body.matchAll(new RegExp(`(?<![\\w-])${prop}:\\s*(-?\\d+)px`, "g"))].pop()?.[1] ?? 0);

describe("hit targets are at least 24px (visual size unchanged)", () => {
  it("a checkbox's label is 24px tall", () => {
    expect(px(rule('label:has(> input[type="checkbox"])'), "min-height")).toBeGreaterThanOrEqual(24);
  });
  it("a chip button reaches 24px: 20px chip plus 2px each side", () => {
    expect(px(rule(".kit-chip"), "min-height") + 2 * Math.abs(Number(/inset:\s*(-\d+)px/.exec(rule(".kit-chip-btn::after"))?.[1] ?? 0))).toBeGreaterThanOrEqual(24);
  });
  it("the finding info icon and icon buttons are at least 24px", () => {
    expect(px(rule(".finding-info"), "width")).toBeGreaterThanOrEqual(24);
    expect(px(rule(".finding-info"), "height")).toBeGreaterThanOrEqual(24);
    expect(px(rule(".kit-iconbtn"), "width")).toBeGreaterThanOrEqual(24);
  });
});
