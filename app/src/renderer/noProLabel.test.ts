import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { expect, it } from "vitest";

import { PRODUCT_NAME } from "../shared/branding";

// Everything ships open source: no "Pro" product label may come back into the app's copy
// (a badge, "Part of <product> Pro", "Health is in <product> Pro", a " · <product> Pro" suffix).
const SRC = join(__dirname, "..");
const LABEL = new RegExp(`(\\$?\\{PRODUCT_NAME\\}|${PRODUCT_NAME}) Pro\\b|>Pro<|"Pro"|shell-pro-(tab|badge|pop|wrap)`);

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : /\.(tsx?|json|css|html)$/.test(e.name) ? [join(dir, e.name)] : []);
}

it("no Pro product label anywhere in app/src", () => {
  const hits = walk(SRC)
    .filter((f) => !/\.test\.tsx?$/.test(f)) // tests may name the label to assert it is gone
    .flatMap((f) => readFileSync(f, "utf8").split("\n").map((l, i) => [f, i + 1, l] as const))
    .filter(([, , l]) => LABEL.test(l))
    .map(([f, n, l]) => `${relative(SRC, f)}:${n}: ${l.trim()}`);
  expect(hits).toEqual([]);
});
