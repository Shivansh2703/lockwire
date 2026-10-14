#!/usr/bin/env node
// the gate: a checked-in script, not a person's eye. Walks both palettes in
// theme.css and fails on any text pair under 4.5:1 or any non-text state edge
// under 3:1. Catches the actual failure mode: someone adds/edits a token in
// one theme block and forgets its twin.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
export const THEME_CSS_PATH = path.join(here, "../src/renderer/theme.css");

function parseTokens(block, aliases = {}) {
  const tokens = {};
  for (const m of block.matchAll(/--([a-z-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;/g)) {
    tokens[m[1]] = m[2];
  }
  // Legacy aliases (--bg: var(--canvas-a)) live once, in the plain :root block,
  // and resolve to their target's colour in every palette.
  for (const [name, target] of Object.entries(aliases)) {
    if (target in tokens) tokens[name] = tokens[target];
  }
  return tokens;
}

function extractBlock(css, re) {
  const m = css.match(re);
  if (!m) return null;
  return m[1];
}

// Each theme's tokens are declared in two places by construction (the
// OS-driven default and the explicit user-forced override) -- see theme.css's
// header comment. Both copies must define the same set of tokens.
export function readPalettes(css) {
  const blocks = {
    // `:root\s*\{` (no attribute selector) cannot match `:root[data-theme="light"]{`,
    // since `[...]` sits between "root" and the brace -- no lookbehind needed.
    lightDefault: extractBlock(css, /:root\s*\{([^}]*)\}/),
    lightOverride: extractBlock(css, /:root\[data-theme="light"\]\s*\{([^}]*)\}/),
    darkDefault: extractBlock(
      css,
      /@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}/,
    ),
    darkOverride: extractBlock(css, /:root\[data-theme="dark"\]\s*\{([^}]*)\}/),
  };
  const aliases = Object.fromEntries(
    [...(blocks.lightDefault ?? "").matchAll(/--([a-z-]+)\s*:\s*var\(--([a-z-]+)\)\s*;/g)].map(
      (m) => [m[1], m[2]],
    ),
  );
  const parsed = Object.fromEntries(
    Object.entries(blocks).map(([name, block]) => [
      name,
      block ? parseTokens(block, aliases) : null,
    ]),
  );
  return parsed;
}

function luminance(hex) {
  let h = hex.replace("#", "");
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const f = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const [rl, gl, bl] = [r, g, b].map(f);
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl;
}

export function contrastRatio(hexA, hexB) {
  const l1 = luminance(hexA);
  const l2 = luminance(hexB);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

// Text pairs: normal-weight body/label text against the surfaces it sits on
// (needs 4.5:1). State edges: the non-text signal a control's state relies on
// -- here, the focus ring / active-tab indicator colour (needs 3:1).
export const TEXT_PAIRS = [
  ["ink", "canvas-a"],
  ["ink", "canvas-b"],
  ["ink", "panel"],
  ["ink", "field"],
  ["muted", "canvas-a"],
  ["muted", "canvas-b"],
  ["muted", "panel"],
  ["muted", "field"],
  ["accent", "canvas-a"],
  ["accent", "panel"],
  ["bad", "canvas-a"],
  ["bad", "panel"],
  ["warn", "canvas-a"],
  ["warn", "panel"],
  ["go", "canvas-a"],
  ["go", "panel"],
  ["on-accent", "accent"],
];
export const STATE_EDGE_PAIRS = [
  ["accent", "canvas-a"],
  ["accent", "panel"],
];

export function checkPalette(name, tokens, requiredKeys) {
  const problems = [];
  for (const key of requiredKeys) {
    if (!(key in tokens)) problems.push(`${name}: missing token --${key}`);
  }
  for (const [fg, bg] of TEXT_PAIRS) {
    if (!(fg in tokens) || !(bg in tokens)) continue;
    const ratio = contrastRatio(tokens[fg], tokens[bg]);
    if (ratio < 4.5) {
      problems.push(
        `${name}: text pair --${fg} on --${bg} is ${ratio.toFixed(2)}:1, needs >= 4.5:1`,
      );
    }
  }
  for (const [fg, bg] of STATE_EDGE_PAIRS) {
    if (!(fg in tokens) || !(bg in tokens)) continue;
    const ratio = contrastRatio(tokens[fg], tokens[bg]);
    if (ratio < 3) {
      problems.push(
        `${name}: state edge --${fg} on --${bg} is ${ratio.toFixed(2)}:1, needs >= 3:1`,
      );
    }
  }
  return problems;
}

export function runContrastCheck(css) {
  const palettes = readPalettes(css);
  const allKeys = new Set();
  for (const tokens of Object.values(palettes)) {
    if (tokens) Object.keys(tokens).forEach((k) => allKeys.add(k));
  }
  const requiredKeys = [...allKeys];

  const results = [];
  for (const [name, tokens] of Object.entries(palettes)) {
    if (!tokens) {
      results.push({ name, problems: [`${name}: block not found in theme.css`] });
      continue;
    }
    results.push({ name, problems: checkPalette(name, tokens, requiredKeys) });
  }
  return results;
}

function main() {
  const css = readFileSync(THEME_CSS_PATH, "utf8");
  const results = runContrastCheck(css);
  let failed = false;
  for (const { name, problems } of results) {
    if (problems.length === 0) {
      console.log(`${name}: pass`);
    } else {
      failed = true;
      console.log(`${name}: FAIL`);
      for (const p of problems) console.log(`  ${p}`);
    }
  }
  process.exit(failed ? 1 : 0);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
