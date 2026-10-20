#!/usr/bin/env node
// e2-bd: stages the frozen Python sidecar where `sidecar.ts`'s resolveSidecarCommand()
// looks for it once packaged (`resourcesPath/sidecar-<platform>-<arch>/<binName>`,
// sidecar.ts:65-69), before electron-builder runs. electron-builder's own
// `${platform}`/`${arch}` macros pick the SAME live values on the SAME host in the
// SAME unsigned local build, so `build.extraResources` in package.json can copy this
// staged tree straight into Resources/ without a second naming convention to drift.
//
// PyInstaller's own spec (packaging/pro/lockwire.spec, else packaging/lockwire.spec) freezes an EXE named "lockwire",
// not "lockwire-sidecar" -- that's the CLI's own binary name and out of this box's
// scope to change (it's shared with CI/other consumers). This script renames only the
// staged copy.
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, renameSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const APP_ROOT = join(here, "..");
const REPO_ROOT = join(APP_ROOT, "..");

// electron-builder's CLI accepts these as boolean flags (electron-builder/out/builder.js:211-227)
// and its `${arch}` macro expands each to this exact string via app-builder-lib's
// `Arch[arch]` (app-builder-lib/out/platformPackager.js:138, builder-util/out/arch.js:12-16) --
// keyed on the PACKAGING TARGET, never on the build host.
const ARCH_FLAGS = ["ia32", "x64", "armv7l", "arm64", "universal"];

/**
 * e2-bd2: the ONE place that decides which arch we are packaging for. An explicit
 * `--<arch>` flag (the same flag electron-builder's own CLI reads, e.g. from
 * `npm run package -- --x64`) wins; otherwise this is a same-arch build and the
 * build host's own `process.arch` is correct. `main()` below and `scripts/package.mjs`
 * BOTH call this -- so the sidecar's staged directory and the arch electron-builder
 * packages for can never express two different values, which is what let
 * `npm run package -- --x64` on an arm64 host stage into `sidecar-darwin-arm64` while
 * the packaged app's resolver (sidecar.ts:69) looks in `sidecar-darwin-x64`.
 * e2-bd3a: `--universal` is not yet implemented. A lipo build would be needed to create
 * a true universal binary; without it, staging a single-arch binary under a `universal`
 * name is a correctly-formed lie that breaks downstream consumers trusting the name.
 * Throw instead until the lipo build is available (e2-bd3).
 */
export function resolveTargetArch(argv = process.argv.slice(2)) {
  const flag = argv.find((a) => ARCH_FLAGS.includes(a.replace(/^--/, "")));
  const arch = flag ? flag.replace(/^--/, "") : process.arch;
  if (arch === "universal") {
    // Tracking: box e2-bd3 implements the real lipo build.
    throw new Error(
      "universal build is not implemented. --universal would stage a single-arch binary " +
      "under a universal name, breaking downstream consumers. Use --arm64, --x64, or another " +
      "single-arch flag instead, or implement a lipo build."
    );
  }
  return arch;
}

/** Mirrors sidecar.ts:69's `sidecar-${process.platform}-${process.arch}`. */
function sidecarDirName(platform, arch) {
  return `sidecar-${platform}-${arch}`;
}

/** Mirrors sidecar.ts:67's binName choice. */
function sidecarBinName(platform) {
  return platform === "win32" ? "lockwire-sidecar.exe" : "lockwire-sidecar";
}

/**
 * Copies `sourceDir` (a PyInstaller COLLECT folder) into
 * `destRoot/sidecar-<platform>-<arch>/`, renaming `sourceBinName` to the name
 * sidecar.ts expects. Returns the staged binary's absolute path.
 */
export function stageSidecar({ sourceDir, sourceBinName, destRoot, platform, arch }) {
  const dir = join(destRoot, sidecarDirName(platform, arch));
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  // verbatimSymlinks: by default cpSync makes relative links absolute (pointing back at the build dir),
  // which codesign rejects inside the .app; a framework-Python sidecar has such links.
  cpSync(sourceDir, dir, { recursive: true, verbatimSymlinks: true });
  const staged = join(dir, sourceBinName);
  const target = join(dir, sidecarBinName(platform));
  if (staged !== target) renameSync(staged, target);
  return target;
}

/** The paid spec when packaging/pro/ exists (private tree), else the free one (public tree). */
export function specPath(repoRoot = REPO_ROOT) {
  return existsSync(join(repoRoot, "packaging", "pro", "lockwire.spec"))
    ? "packaging/pro/lockwire.spec"
    : "packaging/lockwire.spec";
}

function main() {
  const pyinstallerDist = join(REPO_ROOT, "dist", "lockwire");
  execFileSync("uv", ["run", "pyinstaller", specPath(), "--noconfirm"], {
    cwd: REPO_ROOT,
    stdio: "inherit",
  });
  if (!existsSync(pyinstallerDist)) {
    throw new Error(`pyinstaller did not produce ${pyinstallerDist}`);
  }
  const staged = stageSidecar({
    sourceDir: pyinstallerDist,
    sourceBinName: "lockwire",
    destRoot: join(APP_ROOT, "build", "sidecar-staging"),
    platform: process.platform,
    arch: resolveTargetArch(),
  });
  console.log(`staged sidecar at ${staged}`);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main();
}
