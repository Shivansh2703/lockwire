import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { contrastRatio, readPalettes, THEME_CSS_PATH } from "./contrast.mjs";

const css = readFileSync(THEME_CSS_PATH, "utf8");
const rule = (sel: string) => css.split(sel + " {").pop()!.split("}")[0];

describe("UI-POLISH: nothing clips, placeholders read", () => {
  it("test_placeholder_text_is_not_faded_below_4_5_to_1", () => {
    const opacity = Number(/opacity:\s*([\d.]+)/.exec(rule("input::placeholder, textarea::placeholder"))![1]);
    for (const t of Object.values(readPalettes(css)) as Record<string, string>[]) {
      expect(contrastRatio(t.muted, t.field)).toBeGreaterThanOrEqual(4.5);
      expect(opacity).toBe(1); // any fade blends muted toward the field and drops under 4.5
    }
  });

  it("test_example_chips_wrap_so_a_long_name_is_not_cut", () => {
    expect(rule(".open-face-new li button")).toContain("white-space: normal");
  });

  it("test_view_hint_wraps_inside_the_uncovered_canvas", () => {
    const r = rule(".shell-hint");
    expect(r).toContain("white-space: normal");
    expect(r).toContain("max-width");
  });

  it("test_notices_keep_a_readable_width_between_panels", () => {
    expect(rule(".shell-notices")).toContain("min-width");
  });
});
