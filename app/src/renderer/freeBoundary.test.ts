import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

// Free build must not contain the paid agent buttons: only renderer/pro/ may name them.
const walk = (d: string): string[] => readdirSync(d).flatMap((n) => (n === "pro" ? [] : statSync(join(d, n)).isDirectory() ? walk(join(d, n)) : [join(d, n)]));

it("no free renderer file names the paid review or part-search buttons", () => {
  const hits = walk(import.meta.dirname).filter((f) => !f.endsWith("freeBoundary.test.ts") && /ReviewButton|PartSearchButton|Review my robot|Find a motor|agent:review|agent:part-search|Why did it brown out|Which pack fits|agent:brownout|agent:pack-fit/.test(readFileSync(f, "utf8")));
  expect(hits).toEqual([]);
});
