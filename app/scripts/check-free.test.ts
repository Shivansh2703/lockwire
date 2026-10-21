import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// @ts-expect-error -- plain .mjs script, no types
import { ALLOWED_STRING, BUNDLE_PAID, findPaid } from "./check-free.mjs";

describe("check:free's grep", () => {
  it("finds a paid identifier and names its line", () => {
    expect(findPaid("ok\nimport { RunPanel } from './x';")).toEqual([{ line: 2, id: "RunPanel" }]);
  });

  it("lets the one dim Pro sentence through and nothing else", () => {
    expect(findPaid(`<span>${ALLOWED_STRING}</span>`)).toEqual([]);
    expect(findPaid(`<span>${ALLOWED_STRING}</span> run:start`)).toEqual([{ line: 1, id: "run:start" }]);
  });
});

const PRO_INDEX = join(__dirname, "../src/main/pro/index.ts");

describe("check:free's bundle list", () => {
  // pro sources absent in the free tree
  it.skipIf(!existsSync(PRO_INDEX))("names every pro IPC command class", () => {
    const src = readFileSync(PRO_INDEX, "utf8");
    const block = /PRO_COMMAND_CLASSES = \[([^\]]*)\]/.exec(src)?.[1] ?? "";
    const classes = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(classes.length).toBeGreaterThanOrEqual(9);
    expect(classes.filter((c) => !BUNDLE_PAID.includes(c))).toEqual([]);
  });
});
