import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The owner, on screenshots of the app: "Can't see the robot though." (E2-j2). Every piece of
// the scene mount already existed on this branch -- Canvas.tsx, buildScene.ts, lenses.ts,
// the lens tabs, the decoded wire format, the X500 fixture -- and none of it was reachable from
// the app a person opens: `main.tsx` rendered a bare `<Frame>`, and the mount lived only under
// its own test harness (lens-e2e-entry.tsx / e2e-pixel-entry.ts).
//
// This is the one hidden e2e in this file that does NOT bundle a parallel renderer entry. It
// runs `electron-vite build` for real and opens the REAL `out/renderer/index.html` -- the exact
// file the shipped app loads -- so a non-background pixel read here proves the Design tab draws
// the robot in the app the owner opens, not in a stand-in page (see design-mount-check.ts).
//
// Fixture provenance: `src/renderer/scene/fixtures/x500-check-payload.json` is
// `lockwire.serve._handle_check`'s own `payload` field (the real `/v1/run` reply for `check`,
// scene+graph included) against `tests/fixtures/x500/contract.yaml` + `x500.glb` -- 24 real
// parts, not a hand-built triangle. Reproduce from the worktree root:
//
//   mkdir -p /tmp/x500fixture && cp tests/fixtures/x500/contract.yaml /tmp/x500fixture/robot.yaml \
//     && cp tests/fixtures/x500/x500.glb /tmp/x500fixture/robot.glb
//   uv run python -c '
//   import json, sys; sys.path.insert(0, "src")
//   from lockwire.serve import _handle_check, CheckArgs, CheckCommand
//   reply = _handle_check(CheckCommand(args=CheckArgs(contract="/tmp/x500fixture/robot.yaml")))
//   d = reply.model_dump(by_alias=True, mode="json")
//   p = d["payload"]
//   p["scene"]["asset_path"] = "robot.glb"  # scrub the machine-specific temp path
//   json.dump(p, open("app/src/renderer/scene/fixtures/x500-check-payload.json", "w"), indent=2)
//   '
//
// (`_find_scene` discovers a scene by the CONTRACT's own stem + `.glb`/`.gltf`/`.urdf`/`.sdf`,
// so `x500.glb` is copied to `robot.glb` beside a `robot.yaml` copy of the contract -- neither
// the checked-in fixture files nor any application source is modified by this.)

describe("the Design tab mounts the real scene from a real check payload", () => {
  it("test_hidden_real_app_reads_back_a_drawn_pixel_from_the_x500_check_payload", () => {
    const cwd = process.cwd();

    // 1. build the REAL app -- the exact `out/renderer/index.html` electron-vite build.ts
    // produces for `npm run build`. Never the dev server, never `--watch`.
    execFileSync(
      join(cwd, "node_modules", ".bin", "electron-vite"),
      ["build"],
      {
        stdio: "pipe",
        cwd,
      },
    );

    const require = createRequire(import.meta.url);
    const electronBin = require("electron") as unknown as string;
    const outDir = join(
      cwd,
      "node_modules",
      ".cache",
      "lockwire-design-mount-e2e",
    );
    mkdirSync(outDir, { recursive: true });
    try {
      // 2. bundle the main-process check for node/electron -- same idiom as
      // e2e-pixel-check.ts / lens-e2e-check.ts. No renderer-side bundle: the renderer is the
      // real build from step 1.
      execFileSync(
        join(cwd, "node_modules", ".bin", "esbuild"),
        [
          "src/renderer/scene/design-mount-check.ts",
          "--bundle",
          "--platform=node",
          "--format=cjs",
          "--external:electron",
          `--outfile=${join(outDir, "check.cjs")}`,
        ],
        { stdio: "pipe" },
      );

      const appHtmlPath = join(cwd, "out", "renderer", "index.html");
      const payloadPath = join(
        cwd,
        "src",
        "renderer",
        "scene",
        "fixtures",
        "x500-check-payload.json",
      );

      const run = spawnSync(electronBin, [join(outDir, "check.cjs")], {
        env: {
          ...process.env,
          LOCKWIRE_E2E_APP_HTML_PATH: appHtmlPath,
          LOCKWIRE_E2E_PAYLOAD_PATH: payloadPath,
        },
        encoding: "utf8",
        timeout: 30_000,
      });

      const line = run.stdout.trim().split("\n").filter(Boolean).pop();
      expect(
        line,
        `electron exited ${run.status} signal ${run.signal}; stderr: ${run.stderr}`,
      ).toBeTruthy();
      const result = JSON.parse(line!) as {
        visible: boolean;
        pixel: [number, number, number, number];
        silhouette: {
          width: number;
          height: number;
          minX: number;
          maxX: number;
          minY: number;
          maxY: number;
          inset: { top: number; right: number; bottom: number; left: number };
        } | null;
      };

      // the window was never shown.
      expect(result.visible).toBe(false);

      // Background clear color is (0, 0, 0, 255) -- anything else at the center means the
      // real app drew the robot there, not merely that some component "mounted".
      const [r, g, b] = result.pixel;
      expect(r === 0 && g === 0 && b === 0).toBe(false);

      // The robot is framed in the OPEN area: the canvas minus the px the Parts and Inspector
      // panels cover (`inset`, the same one the Shell hands RobotView and the camera fit uses).
      // Fill is measured against that area, not the whole canvas, so a camera that parks the
      // robot small behind the panels cannot pass. The ceiling and margins reject a camera
      // that gets close by cropping the airframe.
      expect(
        result.silhouette,
        "the real X500 silhouette was not drawn",
      ).not.toBeNull();
      const { width, height, minX, maxX, minY, maxY, inset } = result.silhouette!;
      const openW = width - inset.left - inset.right;
      const openH = height - inset.top - inset.bottom;
      const fill = Math.max((maxX - minX) / openW, (maxY - minY) / openH);
      expect(fill).toBeGreaterThanOrEqual(0.6);
      expect(fill).toBeLessThanOrEqual(0.95);
      expect(minX).toBeGreaterThanOrEqual(inset.left);
      expect(maxX).toBeLessThanOrEqual(width - inset.right);
      expect(minY).toBeGreaterThanOrEqual(inset.top);
      expect(maxY).toBeLessThanOrEqual(height - inset.bottom);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 60_000);
});
