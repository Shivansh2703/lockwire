import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, rmSync, copyFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The hidden e2e for the lens mount (E2-f3). A REAL electron launch, never a screenshot,
// nothing on the owner's screen — same idiom as scene-pixel.e2e.test.ts and
// scene-hidpi.e2e.test.ts, this time mounting the real `Canvas` + `DesignLens` and driving a real
// DOM click on a real lens tab button rather than calling app state directly.
//
// the proof, executed rather than read: a part tagged for exactly one lens in the payload
// (`ScenePart.lenses`) is invisible under "All" when not in it and drawn under its own — settled by
// a raw framebuffer read, which a mounted-but-empty or mounted-but-always-visible component would
// both fail differently (an always-hidden mesh never turns colored; an always-visible mesh is
// already colored before the click). Under a connection view (Power/Data/Mechanical) a
// non-member is no longer hidden but drawn greyed (owner pick "Keep body, greyed"); that
// branch is `robotScene.applyLens`, unit-tested in robotScene.test.ts. "All" is unchanged, so
// the assertions below (mounted under "All", then Power) still hold as written.

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

// Minimal payload Graph: DesignLens reads label/caption for all five keys (this fixture
// stands in for the payload's own strings, the app itself authors none of these words).
const GRAPH = {
  off: { nodes: [], edges: [], label: "off", caption: "off caption" },
  power: { nodes: [], edges: [], label: "Power", caption: "power caption" },
  data: { nodes: [], edges: [], label: "Data", caption: "data caption" },
  mechanical: { nodes: [], edges: [], label: "Mechanical", caption: "mechanical caption" },
  health: { nodes: [], edges: [], label: "Health", caption: "health caption" },
};

describe("a lens draws only the parts the payload puts in it, on a real mount", () => {
  it("test_a_lens_draws_only_the_parts_the_payload_puts_in_it", () => {
    const require = createRequire(import.meta.url);
    const electronBin = require("electron") as unknown as string;
    const outDir = join(process.cwd(), "node_modules", ".cache", "lockwire-lens-e2e");
    mkdirSync(outDir, { recursive: true });
    try {
      // 1. bundle the renderer-side entry (real Canvas, real DesignLens, real react-dom) for the
      // browser. --jsx=automatic matches tsconfig.json's "react-jsx".
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/lens-e2e-entry.tsx",
          "--bundle",
          "--platform=browser",
          "--format=iife",
          "--jsx=automatic",
          `--outfile=${join(outDir, "lens-e2e-bundle.js")}`,
        ],
        { stdio: "pipe" },
      );
      copyFileSync(
        join(process.cwd(), "src/renderer/scene/lens-e2e-check.html"),
        join(outDir, "lens-e2e-check.html"),
      );

      // 2. bundle the main-process check for node/electron, same idiom as e2e-pixel-check.ts.
      execFileSync(
        join(process.cwd(), "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/lens-e2e-check.ts",
          "--bundle",
          "--platform=node",
          "--format=cjs",
          "--external:electron",
          `--outfile=${join(outDir, "check.cjs")}`,
        ],
        { stdio: "pipe" },
      );

      // 3. one part, a big flat triangle covering the camera's center pixel, tagged in the
      // payload for the "power" lens only — never "off".
      const parts = [
        {
          positions: packVectorsI16([-5, -5, 0, 5, -5, 0, 0, 5, 0]),
          indices: packIndicesU16([0, 1, 2]),
          lenses: ["power"],
        },
      ];

      const look = (lens: string) => {
        const run = spawnSync(electronBin, [join(outDir, "check.cjs")], {
          env: {
            ...process.env,
            LOCKWIRE_E2E_HTML_PATH: join(outDir, "lens-e2e-check.html"),
            LOCKWIRE_E2E_PARTS: JSON.stringify(parts),
            LOCKWIRE_E2E_GRAPH: JSON.stringify(GRAPH),
            LOCKWIRE_E2E_SELECT_LENS: lens,
          },
          encoding: "utf8",
          timeout: 20_000,
        });
        const line = run.stdout.trim().split("\n").filter(Boolean).pop();
        expect(line, `electron exited ${run.status} signal ${run.signal}; stderr: ${run.stderr}`).toBeTruthy();
        return JSON.parse(line!) as {
          visible: boolean;
          beforePixel: [number, number, number, number];
          afterPixel: [number, number, number, number];
        };
      };
      const result = look("power");

      expect(result.visible).toBe(false);

      // Mounted under the default "off" lens: the triangle is tagged "power" only, so the
      // center pixel must still be the background clear color (0, 0, 0, 255) — THE BITE TARGET.
      // If Canvas.tsx ever draws every part regardless of the active lens, this line goes red
      // first, naming the "power" part drawn under the "off" lens.
      const [beforeR, beforeG, beforeB] = result.beforePixel;
      expect(beforeR === 0 && beforeG === 0 && beforeB === 0).toBe(true);

      // After a real click on the "Power" tab (DesignLens, driven by a real payload label): the
      // same part is now visible, so the center pixel is no longer the background color.
      const [afterR, afterG, afterB] = result.afterPixel;
      expect(afterR === 0 && afterG === 0 && afterB === 0).toBe(false);

      // Under a view it is NOT in (Data), the part is greyed, not hidden: still drawn.
      const [dataR, dataG, dataB] = look("data").afterPixel;
      expect(dataR === 0 && dataG === 0 && dataB === 0).toBe(false);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 45_000);
});
