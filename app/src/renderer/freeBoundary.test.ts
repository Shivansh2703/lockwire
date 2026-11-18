import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

// Free build must not contain the paid Check, More buttons or their questions: only renderer/pro/ and main/pro/ may name them.
const walk = (d: string): string[] => readdirSync(d).flatMap((n) => (n === "pro" ? [] : statSync(join(d, n)).isDirectory() ? walk(join(d, n)) : [join(d, n)]));

it("no free renderer file names the paid review or part-search buttons", () => {
  const hits = walk(import.meta.dirname).filter((f) => !f.endsWith("freeBoundary.test.ts") && /ReviewButton|PartSearchButton|Review my robot|Find a motor|assistant:review|assistant:part-search|Why did it brown out|Which pack fits|assistant:brownout|assistant:pack-fit|Follow the guide/.test(readFileSync(f, "utf8")));
  expect(hits).toEqual([]);
});
