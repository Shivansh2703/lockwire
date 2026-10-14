import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { runContrastCheck, THEME_CSS_PATH } from "./contrast.mjs";

describe("theme.css's token layer gates both palettes", () => {
  it("test_every_text_pair_in_both_palettes_meets_4_5_to_1", () => {
    const css = readFileSync(THEME_CSS_PATH, "utf8");
    const results = runContrastCheck(css);
    const textFailures = results.flatMap((r) =>
      r.problems.filter((p) => p.includes("text pair")),
    );
    expect(textFailures).toEqual([]);
  });

  it("test_every_state_edge_in_both_palettes_meets_3_to_1", () => {
    const css = readFileSync(THEME_CSS_PATH, "utf8");
    const results = runContrastCheck(css);
    const edgeFailures = results.flatMap((r) =>
      r.problems.filter((p) => p.includes("state edge")),
    );
    expect(edgeFailures).toEqual([]);
  });
});
