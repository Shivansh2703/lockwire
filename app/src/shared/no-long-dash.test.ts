// A long dash (em or en) anywhere in app/src non-test source is a bug (owner rule, as
// tests/test_no_long_dash.py for the engine): strings, JSX text, comments and stylesheets alike.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
const ROOT = join(__dirname, "..");
const DASH = /[—–]/;
const SKIP_DIR = new Set(["assistant-eval", "node_modules"]);
const SKIP_FILE = /\.test\.|\.gen\.|fixture|\.d\.ts$/;

export function longDashes(file: string, text: string): string[] {
  return text.split("\n").flatMap((line, i) => (DASH.test(line) ? [`${file}:${i + 1}: ${line.trim().slice(0, 80)}`] : []));
}

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return SKIP_DIR.has(e.name) ? [] : sources(p);
    return /\.(tsx?|json|css)$/.test(e.name) && !SKIP_FILE.test(e.name) ? [p] : [];
  });
}

describe("no long dash in app source", () => {
  it("the scanner sees a string, JSX text, a template, a comment and a stylesheet glyph", () => {
    expect(longDashes("a.tsx", 'const a = "x — y";\nconst b = <p>x – y</p>;\nconst c = `x — ${a}`;\n// c — d\n/* e – f */ const d = 1;')).toHaveLength(5);
    expect(longDashes("a.css", '.x::before { content: "–"; }\n.y::before { content: "−"; }')).toHaveLength(1); // a minus sign is not a dash
  });
  it("app/src source has none", () => {
    const hits = sources(ROOT).flatMap((f) => longDashes(f.slice(ROOT.length + 1), readFileSync(f, "utf8")));
    expect(hits).toEqual([]);
  });
});
