import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The ONE test in this suite that launches the real electron binary -- every other test mocks
// "electron" (never a window on the owner's screen). This is safe by construction: the
// window is built with `show: false` (windowOptions(true)), so nothing paints regardless of what
// this check finds -- that asymmetry is the whole point of box e2-i2 (TODO.md: "a launch that
// shows nothing is hidden-window-safe by construction"). Never remove the `show: false` half of this
// picture to "simplify" -- see window-options.ts and hidden-launch-check.ts.
describe("a real launch under the e2e flag shows nothing", () => {
  it("test_flag_present_window_invisible_and_dock_icon_hidden", () => {
    const require = createRequire(import.meta.url);
    const electronBin = require("electron") as unknown as string;
    // Written under node_modules so `require("electron")` from the bundle resolves the real
    // package (node's module resolution walks up looking for node_modules/electron).
    const outDir = join(process.cwd(), "node_modules", ".cache", "lockwire-hidden-launch");
    const bundle = join(outDir, "check.cjs");
    mkdirSync(outDir, { recursive: true });
    try {
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/main/hidden-launch-check.ts",
          "--bundle",
          "--platform=node",
          "--format=cjs",
          "--external:electron",
          `--outfile=${bundle}`,
        ],
        { stdio: "pipe" },
      );
      const run = spawnSync(electronBin, [bundle], {
        env: { ...process.env },
        encoding: "utf8",
        timeout: 15_000,
      });
      const line = run.stdout
        .trim()
        .split("\n")
        .filter(Boolean)
        .pop();
      expect(line, `electron exited ${run.status} signal ${run.signal}; stderr: ${run.stderr}`).toBeTruthy();
      const result = JSON.parse(line!) as { visible: boolean; dockVisible: boolean };
      expect(result.visible).toBe(false);
      expect(result.dockVisible).toBe(false);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 20_000);
});
