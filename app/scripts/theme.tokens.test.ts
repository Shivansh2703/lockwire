import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import { THEME_CSS_PATH } from "./contrast.mjs";

const HARD = /(?<![\w-])(font-size|font|padding(?:-[a-z]+)?|gap|row-gap|column-gap):[^;}]*\d(?:px|rem|em)\b/;
const bare = (file: string) => readFileSync(join(dirname(THEME_CSS_PATH), file), "utf8").split("\n")
  .filter((l) => !l.trim().startsWith("--") && HARD.test(l.replace(/\/\*.*?\*\//g, "")));

describe("UI-POLISH-2: type and spacing come from tokens", () => {
  it("test_theme_css_has_no_hard_coded_font_size_or_padding_outside_the_token_block", () => {
    expect(bare("./theme.css")).toEqual([]);
  });
  it("test_graph_css_has_no_hard_coded_font_size_or_padding", () => {
    expect(bare("./GraphView.css")).toEqual([]);
  });
  it("test_graph_nodes_keep_a_visible_focus_ring", () => {
    const css = readFileSync(join(dirname(THEME_CSS_PATH), "GraphView.css"), "utf8");
    expect(css).not.toMatch(/\.gv-node\s*\{[^}]*outline:\s*none/);
    expect(css).toMatch(/\.gv-node:focus-visible\s*\{[^}]*outline:\s*2px/);
  });
});
