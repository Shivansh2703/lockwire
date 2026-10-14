import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, rmSync, copyFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Rule 42/e2-sm-r2: ownership of the resolution uniform was already tested and true; whether it
// holds the RIGHT value was not. This settles it BY EXECUTION, never by reading three's examples
// (that reading is what got the previous box bounced) — a real hidden-electron GPU readback
// (show:false, no screenshot, nothing on the owner's screen), same idiom as
// scene-pixel.e2e.test.ts, forced to devicePixelRatio 2 via Electron's
// force-device-scale-factor switch, drawing a real Line2/LineMaterial (the actual future
// consumer of `resolution`) and reading back its rasterized thickness in DEVICE pixels.
//
// WHAT EXECUTION FOUND, CONTRADICTING THE BRIEF'S PREMISE (rule 16 — a measurement that
// contradicts the brief wins): the brief assumed CSS-pixel vs drawing-buffer-pixel `resolution`
// would render visibly different line thickness. Measured, they render IDENTICALLY. Reading
// three's own LineSegments2.js (node_modules/three/examples/jsm/lines/LineSegments2.js:419-428)
// explains why: `onBeforeRender(renderer)` unconditionally overwrites
// `material.uniforms.resolution` with `renderer.getViewport()` — the renderer's live
// DRAWING-BUFFER size — before every single render call, regardless of what value the
// application (buildScene.ts's `SceneContext.resolution`) had set. So whatever convention
// buildScene.handleResize picks for its own `resolution` field is UNOBSERVABLE in the rendered
// pixels: three.js's own code always wins. That is why buildScene.ts's `resolution` assignment
// is untouched by this box — changing its convention cannot fix or break line rendering.
//
// The axis that DOES change the rendered result, and the one this box actually exists to fix, is
// whether `setPixelRatio` was called at all: without it the renderer's drawing buffer stays at
// 1x (CSS-pixel-sized), so the viewport three.js copies into `resolution` is 1x too, and a line
// asking for `linewidth` screen pixels draws at half the intended physical thickness on a 2x
// display. This test proves that with the real rasterizer, and proves it is the ONLY thing that
// moves the number — the "other convention" that must fail is "no setPixelRatio call", not a
// choice between CSS and drawing-buffer pixels, because execution showed there is no such choice
// to make.

describe("the resolution uniform's convention is settled by a real 2x GPU readback", () => {
  it("test_hidpi_line_thickness_is_fixed_by_setPixelRatio_and_the_resolution_convention_is_moot", () => {
    const require = createRequire(import.meta.url);
    const electronBin = require("electron") as unknown as string;
    const outDir = join(process.cwd(), "node_modules", ".cache", "lockwire-scene-hidpi");
    mkdirSync(outDir, { recursive: true });
    try {
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/hidpi-line-entry.ts",
          "--bundle",
          "--platform=browser",
          "--format=iife",
          `--outfile=${join(outDir, "hidpi-line-bundle.js")}`,
        ],
        { stdio: "pipe" },
      );
      copyFileSync(
        join(process.cwd(), "src/renderer/scene/hidpi-line-check.html"),
        join(outDir, "hidpi-line-check.html"),
      );

      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/hidpi-line-check.ts",
          "--bundle",
          "--platform=node",
          "--format=cjs",
          "--external:electron",
          `--outfile=${join(outDir, "check.cjs")}`,
        ],
        { stdio: "pipe" },
      );

      const linewidth = 8;
      // The scale-factor switch must be on electron's OWN command line — appendSwitch() from
      // inside the launched script is too late for this one (measured: dpr stayed 1 that way).
      const run = spawnSync(electronBin, ["--force-device-scale-factor=2", join(outDir, "check.cjs")], {
        env: {
          ...process.env,
          LOCKWIRE_E2E_HTML_PATH: join(outDir, "hidpi-line-check.html"),
          LOCKWIRE_E2E_LINEWIDTH: String(linewidth),
        },
        encoding: "utf8",
        timeout: 20_000,
      });

      const line = run.stdout.trim().split("\n").filter(Boolean).pop();
      expect(line, `electron exited ${run.status} signal ${run.signal}; stderr: ${run.stderr}`).toBeTruthy();
      const result = JSON.parse(line!) as {
        visible: boolean;
        dpr: number;
        cssThickness: number;
        dbThickness: number;
        noFixThickness: number;
        viaBuildSceneThickness: number;
      };

      expect(result.visible).toBe(false);
      // The switch actually took: without it this whole test is measuring dpr=1 and proves
      // nothing.
      expect(result.dpr).toBe(2);

      // MEASURED, not assumed: CSS-pixel and drawing-buffer-pixel `resolution` render
      // IDENTICALLY — proof the convention is moot (three.js overrides it before every render).
      expect(result.cssThickness).toBe(result.dbThickness);

      // The property that actually matters: with the fix (setPixelRatio applied), a line asking
      // for `linewidth` screen pixels renders at ~linewidth * dpr DEVICE pixels — the physically
      // correct HiDPI size (a small tolerance for the rasterizer's own rounding/AA).
      expect(result.cssThickness).toBeGreaterThanOrEqual(linewidth * 2 - 2);
      expect(result.cssThickness).toBeLessThanOrEqual(linewidth * 2 + 2);

      // THE FAILING CASE (rule 30): without setPixelRatio, the same line renders at ~linewidth
      // device pixels — half the correct physical thickness on this 2x display. A test pinned to
      // the fixed convention's band above would NOT tolerate this value; assert it explicitly so
      // the "other" (no-fix) case is shown, not just implied.
      expect(result.noFixThickness).toBeLessThan(result.cssThickness - 2);
      expect(result.noFixThickness).toBeGreaterThanOrEqual(linewidth - 2);
      expect(result.noFixThickness).toBeLessThanOrEqual(linewidth + 2);

      // THE BITE TARGET: same band as cssThickness above, but measured through the REAL
      // `buildScene()` — this is the assertion that goes red when buildScene.ts's own
      // `setPixelRatio` call is removed (see the report's mutation-bite section).
      expect(result.viaBuildSceneThickness).toBeGreaterThanOrEqual(linewidth * 2 - 2);
      expect(result.viaBuildSceneThickness).toBeLessThanOrEqual(linewidth * 2 + 2);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 25_000);
});
