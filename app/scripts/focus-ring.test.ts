import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The "blue highlight spills out of its box" defect (owner 2026-11-12 and 2026-11-14) came from focus
// rings drawn OUTSIDE their control: anything that scrolls, or has rounded corners, cuts such a ring
// off. This holds every focus rule in every stylesheet to one shape: the ring is inside the control.
// The measured twin, on the real app, is scripts/focus-audit.mjs.
const DIR = join(__dirname, "../src/renderer");
const sheets = readdirSync(DIR).filter((f) => f.endsWith(".css"));
const focusRules = sheets.flatMap((file) => {
  const css = readFileSync(join(DIR, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map((m) => ({ file, selector: m[1]!.trim().replace(/\s+/g, " "), body: m[2]! }))
    .filter((r) => r.selector.includes(":focus"));
});
const decl = (body: string, prop: string) => new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`).exec(body)?.[1]?.trim();

describe("focus rings stay inside their control", () => {
  it("finds the focus rules it is guarding", () => {
    expect(sheets).toContain("theme.css");
    expect(focusRules.length).toBeGreaterThan(10);
  });

  it("the base ring is 2 px wide and 2 px inside", () => {
    const base = focusRules.filter((r) => r.selector === ":focus-visible");
    expect(base.length).toBeGreaterThan(0);
    for (const r of base) {
      expect(decl(r.body, "outline"), r.file).toMatch(/^2px solid/);
      expect(decl(r.body, "outline-offset"), r.file).toBe("-2px");
    }
  });

  it("no focus rule moves the ring outside the control", () => {
    const outside = focusRules.filter((r) => {
      const offset = decl(r.body, "outline-offset");
      const width = parseFloat(/^([\d.]+)px/.exec(decl(r.body, "outline") ?? "")?.[1] ?? "2");
      return offset !== undefined && parseFloat(offset) > -width; // inside means offset <= -width
    });
    expect(outside.map((r) => `${r.file}: ${r.selector}`)).toEqual([]);
  });

  it("no focus rule draws a glow outside the control with box-shadow", () => {
    // Split on the commas between shadow layers, not the ones inside color-mix(...) or rgb(...).
    const layers = (v: string) => { const out = [""]; let depth = 0; for (const ch of v) { if (ch === "(") depth++; if (ch === ")") depth--; if (ch === "," && depth === 0) out.push(""); else out[out.length - 1] += ch; } return out.map((s) => s.trim()); };
    const glow = focusRules.filter((r) => {
      const shadow = decl(r.body, "box-shadow");
      return shadow !== undefined && shadow !== "none" && layers(shadow).some((l) => !l.startsWith("inset") && !l.startsWith("var(--shadow"));
    });
    expect(glow.map((r) => `${r.file}: ${r.selector}`)).toEqual([]);
  });

  it("only the command box's text line goes without a ring (its caret and lit row show the focus)", () => {
    const none = focusRules.filter((r) => /^(none|0)\b/.test(decl(r.body, "outline") ?? "") && !r.selector.includes(".cmdk"));
    expect(none.map((r) => `${r.file}: ${r.selector}`)).toEqual([]);
  });
});
