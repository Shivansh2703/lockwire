import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

type BoundedRun = {
  stdout: string;
  stderr: string;
  status: number | null;
  signal: NodeJS.Signals | null;
  timedOut: boolean;
  closed: boolean;
  error?: string;
};

// spawnSync's timeout sends SIGTERM and can still wait forever when Electron handles it.
// The detached POSIX group also owns descendants inheriting its output pipes.
function runBounded(command: string, args: string[], options: {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  timeout: number;
}): Promise<BoundedRun> {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let settled = false;
    let killError: string | undefined;
    let closeFallback: ReturnType<typeof setTimeout> | undefined;
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => { stdout += chunk; });
    child.stderr.on("data", (chunk: string) => { stderr += chunk; });
    const finish = (status: number | null, signal: NodeJS.Signals | null, closed: boolean, error?: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      if (closeFallback) clearTimeout(closeFallback);
      resolve({ stdout, stderr, status, signal, timedOut, closed, error: error ?? killError });
    };
    child.once("error", (error) => finish(null, null, false, String(error)));
    child.once("close", (status, signal) => finish(status, signal, true));
    const deadline = setTimeout(() => {
      timedOut = true;
      if (child.pid !== undefined && process.platform !== "win32") {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ESRCH") killError = String(error);
        }
      }
      // Direct-child fallback also covers Windows, where Node has no POSIX group signal.
      child.kill("SIGKILL");
      // A descendant outside the owned group may keep stdio open. Do not leave Vitest
      // blocked on close after the 45-second child deadline.
      closeFallback = setTimeout(() => {
        child.stdout.destroy();
        child.stderr.destroy();
        finish(null, "SIGKILL", false, "child pipes did not close after SIGKILL");
      }, 1_000);
    }, options.timeout);
  });
}

// E2-j2 rebase ("one source" proof): the App.tsx composition that came out of rebasing
// e2-j2 onto e2-app-r2 has ONE payload state, read by both the Check tab (the leaf components
// CheckFace assembles) and the Design tab (DesignPanel). This is the run that proves it is
// really one source rather than two designs that happen to agree today: it drives the REAL
// "Open robot" button through the REAL preload/ipc/sidecar path (scripts/e2e-drive.mjs,
// which also switches to the Design tab and reads a pixel), against a real
// X500 fixture, in ONE electron launch -- never the test-only `window.__lockwireLoadCheckPayload`
// door (design-mount.e2e.test.ts's proof, which stays and is a different, narrower claim: that
// DesignPanel can render A payload, not that the app's own open flow is what fed it).
//
// the window is never shown (launched through the product's hidden launch gate); the
// driver reports `win.isVisible()` and this test asserts it is false.
// The app carries no test code: scripts/e2e-drive.mjs drives it from outside (Playwright, native
// dialogs stubbed from the test side). Untyped on purpose; it is a script, not app source.
async function drive(
  options: { args?: string[]; env?: NodeJS.ProcessEnv } & Record<string, unknown>,
  body: "driveOpen",
): Promise<Record<string, any>> { // eslint-disable-line @typescript-eslint/no-explicit-any
  const driver = await import(/* @vite-ignore */ pathToFileURL(join(process.cwd(), "scripts", "e2e-drive.mjs")).href);
  const electronBin = createRequire(import.meta.url)("electron") as unknown as string;
  const { args, env, ...rest } = options;
  const launched = await driver.launchApp({ executablePath: electronBin, args: args ?? ["."], cwd: process.cwd(), env });
  try {
    return await driver[body]({ ...launched, ...rest });
  } finally {
    await launched.app.close().catch(() => {});
  }
}

function expectNoTestHooksInMain(mainEntry: string): void {
  expect(readFileSync(mainEntry, "utf8").includes("LOCKWIRE_E2E"), `the built main entry still reads LOCKWIRE_E2E_*: ${mainEntry}`).toBe(false);
}

describe("the app opens real lockfiles through the IPC door and both tabs read the same payload", () => {
  it("test_owned_child_group_is_bounded_when_term_is_ignored", async () => {
    if (process.platform === "win32") return; // POSIX process-group negative.
    const grandchild = "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);";
    const script = `const { spawn } = require("node:child_process");
      process.on("SIGTERM", () => {});
      spawn(process.execPath, ["-e", ${JSON.stringify(grandchild)}], { stdio: "inherit" });
      console.log("ready");
      setInterval(() => {}, 1000);`;
    const run = await runBounded(process.execPath, ["-e", script], { timeout: 2_000 });
    expect(run.stdout).toContain("ready");
    expect(run.timedOut).toBe(true);
    expect(run.closed, run.error).toBe(true);
    expect(run.signal).toBe("SIGKILL");
  }, 5_000);

  it(
    "test_hidden_real_ipc_open_fills_the_check_headline_and_the_design_pixel_from_one_open",
    async () => {
      const cwd = process.cwd();
      const repoRoot = join(cwd, "..");

      // The app build is a sequential prerequisite.
      const requiredArtifacts = [
        join(cwd, "out", "main", "index.js"),
        join(cwd, "out", "preload", "index.js"),
        join(cwd, "out", "renderer", "index.html"),
      ];
      const missingArtifacts = requiredArtifacts.filter((path) => !existsSync(path));
      if (missingArtifacts.length > 0) {
        throw new Error(
          `Built app artifacts required before this test. Run npm run build. Missing: ${missingArtifacts.join(", ")}`,
        );
      }
      const mainEntry = requiredArtifacts[0]!;
      expectNoTestHooksInMain(mainEntry);

      // 1. stage a real fixture the sidecar can `check` end to end: `_find_scene` (Python side)
      // discovers a scene by the CONTRACT's own stem, so the contract and the glb are copied to
      // matching stems (`robot.yaml` / `robot.glb`), same recipe as
      // scene/design-mount.e2e.test.ts's header comment. Neither the checked-in fixtures nor any
      // application source is modified.
      const fixtureDir = join(cwd, "node_modules", ".cache", "lockwire-app-e2e-open");
      rmSync(fixtureDir, { recursive: true, force: true });
      mkdirSync(fixtureDir, { recursive: true });
      cpSync(join(repoRoot, "tests/fixtures/x500/contract.yaml"), join(fixtureDir, "robot.yaml"));
      cpSync(join(repoRoot, "tests/fixtures/x500/x500.glb"), join(fixtureDir, "robot.glb"));
      writeFileSync(
        join(fixtureDir, "plan.yaml"),
        "steps:\n  - kind: rail_load\n    rail: rail.servo-motor\n    draw: {value: 500, unit: W}\n",
      );
      cpSync(join(repoRoot, "tests/fixtures/ulog/18a01095-f04e-4801-a9f3-4040190fa3bb.ulg"), join(fixtureDir, "recording.ulg"));

      // 2. launch the REAL built main entry (package.json "main": "./out/main/index.js") hidden and
      // drive it from outside: the native open dialog is stubbed to return the staged fixture;
      // nothing here reaches into main/ipc.ts or the sidecar directly.
      const result = await drive({
        openPath: process.env.LOCKWIRE_APP_FIXTURE_PATH ?? join(fixtureDir, "robot.yaml"),
        screenshotBase: join(fixtureDir, "workflow.png"),
        ...(process.env.LOCKWIRE_APP_FIXTURE_PATH ? {} : {
          planPath: join(fixtureDir, "plan.yaml"),
          recordingPath: join(fixtureDir, "recording.ulg"),
        }),
        env: process.env.ROS_PACKAGE_PATH ? { ROS_PACKAGE_PATH: process.env.ROS_PACKAGE_PATH } : {},
      }, "driveOpen") as {
        headlineText?: string;
        verdictClass?: string;
        designPixel?: [number, number, number, number];
        planVerdict?: string;
        testVerdict?: string;
        tabOrder?: string[];
        modeStates?: { mode: string; targetSelector: string; targetPresent: boolean }[];
        screenshots?: { checkpoint: string; path: string }[];
        design?: { selectedPart: string; canvas: { width: number; height: number; display: string; visibility: string } };
        themes?: { theme: string; background: string; color: string }[];
        visible?: boolean;
      };

      // the window was never shown.
      expect(result.visible).toBe(false);

      // Check tab: the real headline text, off the real sidecar reply (E2-hl).
      expect(result.headlineText, "the .check-headline assertion").toBeTruthy();
      expect(result.headlineText!.length).toBeGreaterThan(0);

      // Design tab: a non-background pixel from the SAME open (this is what makes "one
      // source" a measured claim: both reads above come from one runE2eOpenCheck() call, one
      // `handleFileOpen` round trip).
      expect(result.designPixel, "the designPixel assertion (window.__lockwireReadNonBackgroundPixel)").toBeTruthy();
      const [r, g, b] = result.designPixel!;
      expect(r === 0 && g === 0 && b === 0, "the designPixel assertion (window.__lockwireReadNonBackgroundPixel)").toBe(
        false,
      );
      // Test and Run are the optional pro/ tabs: expected exactly when that folder is in the tree.
      const hasPro = existsSync(join(process.cwd(), "src", "renderer", "pro", "index.tsx"));
      if (!process.env.LOCKWIRE_APP_FIXTURE_PATH) {
        expect(result.planVerdict, "the real Plan chooser callback and sidecar payload").toBeTruthy();
        if (hasPro) expect(result.testVerdict, "the real Test chooser callback and sidecar payload").toBeTruthy();
        const wanted = hasPro ? ["Plan-result", "Test-result"] : ["Plan-result"];
        const resultShots = result.screenshots?.filter(({ checkpoint }) => wanted.includes(checkpoint));
        expect(resultShots?.map(({ checkpoint }) => checkpoint)).toEqual(wanted);
        for (const shot of resultShots ?? []) {
          expect(readFileSync(shot.path).subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
        }
      }
      expect(result.tabOrder).toEqual(["Design", "Declare", "Check", "Plan", ...(hasPro ? ["Test", "Run"] : []), "Docs"]);
      expect(result.modeStates).toEqual([
        { mode: "Design", targetSelector: ".design-face", targetPresent: true },
        { mode: "Declare", targetSelector: ".declare-face input, .declare-face select", targetPresent: true },
        { mode: "Check", targetSelector: ".check-headline", targetPresent: true },
        { mode: "Plan", targetSelector: process.env.LOCKWIRE_APP_FIXTURE_PATH ? ".plan-input" : ".verdict-word", targetPresent: true },
        ...(hasPro ? [
          { mode: "Test", targetSelector: process.env.LOCKWIRE_APP_FIXTURE_PATH ? ".test-input" : ".verdict-word", targetPresent: true },
          { mode: "Run", targetSelector: ".run-panel", targetPresent: true },
        ] : []),
        { mode: "Docs", targetSelector: ".reference-face", targetPresent: true },
      ]);
      expect(result.design?.selectedPart).toBeTruthy();
      expect(result.design?.canvas.width).toBeGreaterThan(0);
      expect(result.design?.canvas.height).toBeGreaterThan(0);
      expect(result.design?.canvas.display).not.toBe("none");
      expect(result.design?.canvas.visibility).toBe("visible");
      expect(result.themes?.map(({ theme }) => theme)).toEqual(["dark", "light"]);
      expect(result.themes?.every(({ background, color }) => Boolean(background && color))).toBe(true);
    },
    90_000,
  );

  it("test_hidden_real_ipc_page_mounts_a_scene_with_declared_external_mesh_bytes", async () => {
    const cwd = process.cwd();
    for (const path of ["out/main/index.js", "out/preload/index.js", "out/renderer/index.html"]) {
      expect(existsSync(join(cwd, path)), `build the app before the mounted test: ${path}`).toBe(true);
    }
    const fixtureDir = join(cwd, "node_modules", ".cache", "lockwire-app-e2e-mesh");
    rmSync(fixtureDir, { recursive: true, force: true });
    mkdirSync(fixtureDir, { recursive: true });
    const urdf = '<robot name="meshbot"><link name="base"><visual><geometry><mesh filename="mesh.stl"/></geometry></visual></link></robot>\n';
    const mesh = `solid base
facet normal 0 0 1
outer loop
vertex -1 -1 0
vertex 1 -1 0
vertex 1 1 0
endloop
endfacet
facet normal 0 0 1
outer loop
vertex -1 -1 0
vertex 1 1 0
vertex -1 1 0
endloop
endfacet
endsolid base
`;
    const digest = (bytes: string) => createHash("sha256").update(bytes).digest("hex");
    writeFileSync(join(fixtureDir, "robot.urdf"), urdf);
    writeFileSync(join(fixtureDir, "mesh.stl"), mesh);
    // This is a real contract fixture. The page itself comes only from the sidecar's
    // getContractPage route after the App opens robot.yaml through preload and IPC.
    writeFileSync(join(fixtureDir, "robot.yaml"), `schema_version: "0.2"
robot: {name: meshbot}
model:
  source: robot.urdf
  source_sha256: ${digest(urdf)}
  structure_hash: ${digest("base")}
  external_assets:
    - {path: mesh.stl, sha256: ${digest(mesh)}}
  links:
    - {name: base}
  zero_mass_links: [base]
  provenance: {witness: w2, source: robot.urdf, locked_at: 2026-09-27}
`);
    expectNoTestHooksInMain(join(cwd, "out/main/index.js"));
    const result = await drive({ openPath: join(fixtureDir, "robot.yaml") }, "driveOpen") as {
      designPixel?: [number, number, number, number];
      design?: { selectedPart: string; assetIdentity: string };
    };
    expect(result.design?.selectedPart).toBe("base");
    expect(result.design?.assetIdentity).toBe(
      `Selected asset ${join(realpathSync(fixtureDir), "robot.urdf")} matches the declared model identity ${digest(urdf)}.`,
    );
    expect(result.designPixel).toBeTruthy();
    expect(result.designPixel!.slice(0, 3)).not.toEqual([0, 0, 0]);
  }, 90_000);
});
