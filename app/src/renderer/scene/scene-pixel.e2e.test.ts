import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync, copyFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";


// The hidden e2e: a REAL electron launch, never a screenshot, nothing on the owner's screen
//. Every other test in this repo mocks "electron" or three's renderer; this is the
// one exception, same shape as e2-i2's `hidden-launch.test.ts` — the checked's own `show: false`,
// always hidden, makes it
// hidden-window-safe by construction, so the window never paints regardless of what this check finds.
// Reading back a non-background PIXEL (not asserting a component mounted) proves the mesh graph
// in buildScene.ts actually drew something: a component that "mounted" but built zero meshes, or
// built meshes with empty geometry, would still pass a mount assertion and fail this one.

function packVectorsI16(values: number[]) {
  const buf = Buffer.alloc(values.length * 2);
  values.forEach((v, i) => buf.writeInt16LE(v, i * 2));
  return { type: "i16" as const, data: buf.toString("base64"), scale: 1, offset: [0, 0, 0] as [number, number, number] };
}

function packIndicesU16(values: number[]) {
  const buf = Buffer.alloc(values.length * 2);
  values.forEach((v, i) => buf.writeUInt16LE(v, i * 2));
  return { type: "u16" as const, data: buf.toString("base64") };
}

describe("the real scene graph draws a non-background pixel in a hidden window", () => {
  it("test_hidden_launch_reads_back_a_drawn_pixel_not_the_background", () => {
    const require = createRequire(import.meta.url);
    const electronBin = require("electron") as unknown as string;
    const outDir = join(process.cwd(), "node_modules", ".cache", "lockwire-scene-pixel");
    mkdirSync(outDir, { recursive: true });
    try {
      // 1. bundle the renderer-side entry (real buildScene, real three) for the browser.
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/e2e-pixel-entry.ts",
          "--bundle",
          "--platform=browser",
          "--format=iife",
          `--outfile=${join(outDir, "e2e-pixel-bundle.js")}`,
        ],
        { stdio: "pipe" },
      );
      copyFileSync(join(process.cwd(), "src/renderer/scene/e2e-pixel-check.html"), join(outDir, "e2e-pixel-check.html"));

      // 2. bundle the main-process check for node/electron, same idiom as hidden-launch-check.ts.
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/e2e-pixel-check.ts",
          "--bundle",
          "--platform=node",
          "--format=cjs",
          "--external:electron",
          `--outfile=${join(outDir, "check.cjs")}`,
        ],
        { stdio: "pipe" },
      );

      // 3. one part: a big flat triangle guaranteed to cover the camera's center pixel.
      const parts = [
        {
          positions: packVectorsI16([-5, -5, 0, 5, -5, 0, 0, 5, 0]),
          indices: packIndicesU16([0, 1, 2]),
        },
      ];

      const run = spawnSync(electronBin, [join(outDir, "check.cjs")], {
        env: {
          ...process.env,
          LOCKWIRE_E2E_HTML_PATH: join(outDir, "e2e-pixel-check.html"),
          LOCKWIRE_E2E_PARTS: JSON.stringify(parts),
        },
        encoding: "utf8",
        timeout: 20_000,
      });

      const line = run.stdout.trim().split("\n").filter(Boolean).pop();
      expect(line, `electron exited ${run.status} signal ${run.signal}; stderr: ${run.stderr}`).toBeTruthy();
      const result = JSON.parse(line!) as { visible: boolean; pixel: [number, number, number, number] };

      expect(result.visible).toBe(false);
      // Background clear color is (0, 0, 0, 255) — anything else at the center means the
      // triangle was actually rasterized there, not merely constructed in memory.
      const [r, g, b] = result.pixel;
      expect(r === 0 && g === 0 && b === 0).toBe(false);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 25_000);
});
