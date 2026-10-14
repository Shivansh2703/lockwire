import { mkdirSync, mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("electron", () => ({ app: { isPackaged: true } }));

import { resolveTargetArch, specPath, stageSidecar } from "./stage-sidecar.mjs";

// The whole point of this test (e2-bd): prove the electron-builder staging step and
// sidecar.ts's resolveSidecarCommand() agree on WHERE the packaged sidecar lives --
// not by comparing two hand-typed path strings (a coincidence, not a test), but by
// staging a real file with stageSidecar() and then asking the REAL resolver, with a
// real process.resourcesPath, where it thinks the binary is -- and checking that path
// exists on disk. If either side's naming convention ever drifts from the other,
// resolveSidecarCommand()'s answer stops pointing at a real file and this goes red.
const dirs: string[] = [];
function tmpDir(): string {
  const d = mkdtempSync(join(tmpdir(), "lockwire-sidecar-stage-"));
  dirs.push(d);
  return d;
}

afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  delete (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
});

describe("the staged sidecar path is the one sidecar.ts resolves (e2-bd)", () => {
  it("test_staged_sidecar_path_matches_resolveSidecarCommand", async () => {
    const source = tmpDir();
    writeFileSync(join(source, "lockwire"), "#!/bin/sh\necho stub\n", { mode: 0o755 });
    const destRoot = tmpDir();

    const staged = stageSidecar({
      sourceDir: source,
      sourceBinName: "lockwire",
      destRoot,
      platform: process.platform,
      arch: process.arch,
    });
    expect(existsSync(staged)).toBe(true);

    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = destRoot;
    const sidecar = await import("../src/main/sidecar");
    const { command } = sidecar.resolveSidecarCommand();

    expect(command).toBe(staged);
    expect(existsSync(command)).toBe(true);
  });
});

// e2-bd2: the staging directory's arch and electron-builder's `${arch}` macro used to
// be two separate expressions -- stage-sidecar.mjs read the BUILD HOST's `process.arch`,
// electron-builder read its own `--x64`/`--arm64` CLI flag -- that only agreed when no
// flag was passed. `npm run package -- --x64` on an arm64 host staged into
// `sidecar-darwin-arm64` while electron-builder's macro (app-builder-lib's
// `Arch[Arch.x64]`, platformPackager.js:138) expanded to `sidecar-darwin-x64`: the build
// succeeds and the packaged app fails to find its sidecar on the user's machine.
describe("the packaging target arch is one source, not two (e2-bd2)", () => {
  it("test_resolve_target_arch_honors_an_explicit_flag_over_the_build_host", () => {
    const hostArch = process.arch;
    const crossArch = hostArch === "x64" ? "arm64" : "x64";

    expect(resolveTargetArch([`--${crossArch}`])).toBe(crossArch);
    expect(resolveTargetArch([`--${crossArch}`])).not.toBe(hostArch);
    // No explicit flag: a same-arch build, so the build host is correct.
    expect(resolveTargetArch([])).toBe(hostArch);
  });

  it("test_staged_sidecar_directory_follows_the_resolved_target_arch_not_the_build_host", () => {
    const hostArch = process.arch;
    const crossArch = hostArch === "x64" ? "arm64" : "x64";
    const source = tmpDir();
    writeFileSync(join(source, "lockwire"), "#!/bin/sh\necho stub\n", { mode: 0o755 });
    const destRoot = tmpDir();

    const staged = stageSidecar({
      sourceDir: source,
      sourceBinName: "lockwire",
      destRoot,
      platform: process.platform,
      arch: resolveTargetArch([`--${crossArch}`]),
    });

    // This is the exact directory name electron-builder's `${arch}` macro produces for
    // the same `--<crossArch>` flag (Arch[Arch.<crossArch>] in
    // app-builder-lib/out/platformPackager.js:138 / builder-util/out/arch.js:12-16).
    expect(staged).toContain(`sidecar-${process.platform}-${crossArch}`);
    expect(staged).not.toContain(`sidecar-${process.platform}-${hostArch}`);
  });
});

// e2-bd3a: --universal is not yet implemented. Staging a single-arch binary under a
// `universal` name is a correctly-formed lie that breaks downstream consumers trusting
// the name. The refusal must not break normal single-arch builds.
describe("--universal refusal (e2-bd3a)", () => {
  it("test_resolve_target_arch_refuses_universal_with_named_reason", () => {
    expect(() => resolveTargetArch(["--universal"])).toThrow();
    const error = (() => {
      try {
        resolveTargetArch(["--universal"]);
      } catch (e) {
        return e;
      }
    })();
    expect((error as Error).message).toContain("universal build is not implemented");
    expect((error as Error).message).toContain("single-arch binary");
  });

  it("test_resolve_target_arch_arm64_still_stages", () => {
    // Verify that the refusal does not break normal single-arch builds.
    expect(resolveTargetArch(["--arm64"])).toBe("arm64");
    const source = tmpDir();
    writeFileSync(join(source, "lockwire"), "#!/bin/sh\necho stub\n", { mode: 0o755 });
    const destRoot = tmpDir();

    const staged = stageSidecar({
      sourceDir: source,
      sourceBinName: "lockwire",
      destRoot,
      platform: process.platform,
      arch: resolveTargetArch(["--arm64"]),
    });

    expect(staged).toContain(`sidecar-${process.platform}-arm64`);
    expect(existsSync(staged)).toBe(true);
  });
});

describe("which PyInstaller spec is frozen", () => {
  it("test_spec_path_prefers_packaging_pro_and_falls_back_to_the_free_spec", () => {
    const root = tmpDir();
    mkdirSync(join(root, "packaging"));
    writeFileSync(join(root, "packaging", "lockwire.spec"), "");
    expect(specPath(root)).toBe("packaging/lockwire.spec");
    mkdirSync(join(root, "packaging", "pro"));
    writeFileSync(join(root, "packaging", "pro", "lockwire.spec"), "");
    expect(specPath(root)).toBe("packaging/pro/lockwire.spec");
  });
});
