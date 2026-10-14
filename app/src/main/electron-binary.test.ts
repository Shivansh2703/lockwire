import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

// D_e2af: npm 11's allow-scripts gate blocks *dependency* install scripts (electron-winstaller,
// esbuild, fsevents all warn "not yet covered by allowScripts" on a plain `npm ci`), but electron
// itself ships no postinstall script in 44.2.0 -- it lazy-downloads its binary the first time
// something requires it, and only if that first require happens with network access. A fresh
// `npm ci` in a networkless CI/robot environment therefore leaves `electron/dist` missing and
// `electron-vite dev` dies before any window opens. The fix is app/package.json's own
// "postinstall": "node node_modules/electron/install.js" -- a root-level lifecycle script, which
// npm always runs regardless of the dependency-script allowlist -- so the binary lands during
// `npm ci` itself, not on first launch.
describe("the electron binary is installed by npm ci, not deferred to first launch", () => {
  it("test_electron_dist_exists_after_install", () => {
    // Deliberately does NOT `require("electron")`: its index.js lazy-downloads the binary
    // on demand if `dist` is missing, which would silently heal the very failure this test
    // exists to catch. Instead it inspects the package directory directly, as the installer
    // itself does (electron/install.js's isInstalled()).
    const require = createRequire(import.meta.url);
    const electronPkgDir = dirname(require.resolve("electron/package.json"));
    expect(existsSync(join(electronPkgDir, "dist"))).toBe(true);
    expect(existsSync(join(electronPkgDir, "path.txt"))).toBe(true);
  });
});
