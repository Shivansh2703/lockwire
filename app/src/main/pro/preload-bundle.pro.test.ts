import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The paid half of the preload bundle: pairs with ../preload-bundle.test.ts. Run `npm run build` first.
describe("the preload bundle carries the paid methods when pro/ was present at build time", () => {
  const OUT = join(import.meta.dirname, "..", "..", "..", "out", "preload", "index.js");

  it("test_built_bundle_exposes_the_nine_paid_methods_by_name", () => {
    expect(existsSync(OUT)).toBe(true);
    const src = readFileSync(OUT, "utf8");
    for (const name of [
      "prepareActuatorReviewDraft", "checkRecording", "startRun", "pollRun", "cancelRun",
      "startAnalysis", "pollAnalysis", "cancelAnalysis", "chooseRecording",
    ]) expect(src).toContain(name);
  });
});
