import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { expect, it } from "vitest";

// One word per concept (UIA-4): the signer is "Checked by", the thing on the robot is a "part", a
// result line is a "finding", and a check that was not scored is "Not scored". The old synonyms
// may not come back into a string the user reads. Identifiers, lowercase one-word keys and the
// CLI flag `--witness` are not user-visible and are skipped.
const OLD = /\b(witness|devices?|components?|problems?|not compared yet|unchecked)\b/i;
const LITERAL = /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`|>([^<>{}\n][^<>{}]*)</g;

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : /\.tsx?$/.test(e.name) && !/\.(test|gen)\.|fixture/.test(e.name) ? [join(dir, e.name)] : []);
}

it("no old synonym in a user-visible renderer string", () => {
  const hits = walk(__dirname).flatMap((f) => readFileSync(f, "utf8").split("\n").flatMap((line, i) => {
    if (/^\s*(import |\/\/|\*|\/\*)/.test(line)) return [];
    return [...line.matchAll(LITERAL)]
      .map((m) => (m[1] ?? m[2] ?? m[3] ?? m[4] ?? "").replace(/\$\{[^}]*\}/g, "").trim())
      .filter((t) => OLD.test(t) && !/^[a-z_.\-/]+$/.test(t) && !t.startsWith("--") && !/-$/.test(t) && !(f.endsWith("pro/DeviceRows.tsx") && t === "devices") // Run's live devices that answer at run time, not model parts (CALL UIA-4-PRO)
      && !(/^device file$/.test(t) && line.includes("const FIELDS")))
      .map((t) => `${relative(__dirname, f)}:${i + 1}: ${t}`);
  }));
  expect(hits).toEqual([]);
});
