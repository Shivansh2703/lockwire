import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import { THEME_CSS_PATH } from "./contrast.mjs";

const HARD = /(?<![\w-])(font-size|font|padding(?:-[a-z]+)?|gap|row-gap|column-gap):[^;}]*\d(?:px|rem|em)\b/;
const bare = (file: string) => readFileSync(join(dirname(THEME_CSS_PATH), file), "utf8").split("\n")
  .filter((l) => !l.trim().startsWith("--") && HARD.test(l.replace(/\/\*.*?\*\//g, "")));

describe("type and spacing come from tokens", () => {
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

describe("Faded parts stay readable on the light canvas", () => {
  it("test_light_ghost_is_at_least_half_opaque_and_dark_ghost_is_unchanged", () => {
    const css = readFileSync(THEME_CSS_PATH, "utf8");
    expect(Number(/^:root \{ --ghost: ([\d.]+); \}/m.exec(css)![1])).toBeGreaterThanOrEqual(0.5);
    expect(css).toContain(':root[data-theme="dark"] { --ghost: 0.6; }');
    expect(css).toContain('@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --ghost: 0.6; } }');
  });
});
