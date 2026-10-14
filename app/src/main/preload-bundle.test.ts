import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Pairs with the accept line (`npm run build`, then read this file): a green typecheck and a
// green vitest run both exercise src/preload/index.ts, never the bundle electron-vite actually
// ships in out/preload/index.js. e2-a's lesson (E0 worker protocol 4b) was that "it builds" is
// not an accept line -- so this checks the artefact electron's webPreferences.preload path
// (src/main/index.ts) points at actually exists and still carries the three enumerated methods,
// not a generic invoke passthrough. Run `npm run build` first; this does not build it for you.
describe("the preload bundle electron actually loads", () => {
  const OUT = join(import.meta.dirname, "..", "..", "out", "preload", "index.js");

  it("test_out_preload_index_js_exists_after_build", () => {
    expect(existsSync(OUT)).toBe(true);
  });

  it("test_built_bundle_exposes_the_three_enumerated_methods_and_no_generic_invoke", () => {
    const src = readFileSync(OUT, "utf8");
    expect(src).toContain("editContract");
    expect(src).toContain("writeContract");
    expect(src).toContain("undoContract");
    expect(src).toContain("runProbe");
    expect(src).toContain("sendRobotCommand");
    expect(src).toContain('exposeInMainWorld("lockwire"');
    // The bug class this box exists to prevent: a bridge that just forwards any channel name.
    expect(src).not.toMatch(/exposeInMainWorld\([^)]*,\s*(electron\.)?ipcRenderer\)/);
  });
});
