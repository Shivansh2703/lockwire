#!/usr/bin/env node
// e2-bd2: replaces the `package` script's shell chain so the sidecar staging step and
// the electron-builder invocation both read `resolveTargetArch()` (stage-sidecar.mjs)
// for which arch they are packaging -- ONE source instead of two expressions that only
// agreed by coincidence (stage-sidecar.mjs read the build host's `process.arch`;
// electron-builder read its own `--x64`/`--arm64` CLI flag). `npm run package -- --x64`
// on an arm64 host used to stage into `sidecar-darwin-arm64` while electron-builder's
// `${arch}` macro (app-builder-lib/out/platformPackager.js:138) expanded to
// `sidecar-darwin-x64` -- a build that succeeds and fails at runtime on the user's
// machine. See scripts/stage-sidecar.mjs's resolveTargetArch() for the full citation.
import { execFileSync } from "node:child_process";
import config from "../package.json" with { type: "json" };
import { BUNDLE_ID, PRODUCT_NAME } from "../src/shared/branding.ts";
import { assertPackagingBranding } from "./branding-guard.ts";

import { resolveTargetArch } from "./stage-sidecar.mjs";

assertPackagingBranding(config.build, { BUNDLE_ID, PRODUCT_NAME });
if (process.argv.includes("--check-branding")) {
  console.log("packaging branding: OK");
  process.exit(0);
}

const arch = resolveTargetArch();
const platforms = ["mac", "linux"];
const requestedPlatforms = platforms.filter((platform) => process.argv.includes(`--${platform}`));
if (requestedPlatforms.length > 1) throw new Error("package exactly one platform at a time");
const platform = requestedPlatforms[0] ?? "mac";
if (platform === "linux" && arch !== "x64") throw new Error("Linux packaging is x64-only");
console.log(`packaging for platform=${platform} arch=${arch}`);

execFileSync("npx", ["electron-vite", "build"], { stdio: "inherit" });
execFileSync("node", ["scripts/stage-sidecar.mjs", `--${arch}`], { stdio: "inherit" });
// Linux also yields the AppImage download; the dir target is what the packaged e2e runs.
const targets = platform === "linux" ? ["dir", "AppImage"] : ["dir"];
execFileSync("npx", ["electron-builder", `--${platform}`, ...targets, `--${arch}`, "--publish=never"], {
  stdio: "inherit",
  env: { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: "false" },
});
