import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// package.json's `build` key is the CANONICAL packaging config (decided 2026-09-09). The rival app/electron-builder.yml (93 lines, preserved at 7a4da3e,
// branch closed) is NOT: it contradicts the unsigned-local-nothing-leaves-the-machine
// minimum twice over -- draft GitHub releases IS publishing, and hardened runtime plus
// entitlements delegation IS signing. Only two things were carried across from that file:
// the eight Electron security fuses, and the arm64-only target. Nothing else -- no
// release/publish target, no hardenedRuntime, no entitlements, no notarisation, no
// dmg/zip packaging -- came with them.
const PKG_PATH = join(import.meta.dirname, "..", "..", "package.json");

function readBuildConfig(): {
  electronFuses?: Record<string, boolean>;
  files?: string[];
  mac?: { target?: Array<{ target: string; arch?: string[] }> };
  linux?: { target?: Array<{ target: string; arch?: string[] }> };
} {
  return JSON.parse(readFileSync(PKG_PATH, "utf8")).build;
}

describe("the packaging config in package.json's build key (E6-C2)", () => {
  it("test_all_eight_electron_fuses_and_the_ad_hoc_reset_are_named", () => {
    const fuses = readBuildConfig().electronFuses;
    expect(fuses, "no build.electronFuses in package.json").toBeDefined();

    // The four that turn the shipped binary into a general-purpose Node if left on:
    // ELECTRON_RUN_AS_NODE, --inspect, NODE_OPTIONS, and file:// getting the extra
    // privileges of a standard scheme. The app never needs any of them.
    expect(fuses!.runAsNode).toBe(false);
    expect(fuses!.enableNodeCliInspectArguments).toBe(false);
    expect(fuses!.enableNodeOptionsEnvironmentVariable).toBe(false);
    expect(fuses!.grantFileProtocolExtraPrivileges).toBe(false);

    // The two that keep the app the app. onlyLoadAppFromAsar means code is loaded only
    // from the asar, never from a directory beside it.
    //
    // CEILING, carried across from 7a4da3e's comment -- enableEmbeddedAsarIntegrityValidation
    // is ENABLED here but its ENFORCEMENT is UNPROVEN. The fuse asks macOS to hash-check
    // the asar's contents against the header embedded in the signed binary, and macOS
    // only performs that check under a VALID CODE SIGNATURE. This repo signs nothing
    //: what is verified is that the fuse is SET; that it bites is assumed, from
    // Electron's documented behaviour, not measured. A signed build is where that becomes
    // measurable -- flip a byte in the packaged asar and the app must refuse to start.
    expect(fuses!.enableEmbeddedAsarIntegrityValidation).toBe(true);
    expect(fuses!.onlyLoadAppFromAsar).toBe(true);

    // Cookies encrypted at rest with the OS keychain. The app stores no cookies today;
    // on is the setting that stays correct when a future box stores one.
    expect(fuses!.enableCookieEncryption).toBe(true);

    // Off: this app ships no custom V8 snapshot, so the loader must not look for one.
    expect(fuses!.loadBrowserProcessSpecificV8Snapshot).toBe(false);

    // This is electron-builder's local post-fuse ad-hoc signature reset. It is not a
    // ninth @electron/fuses switch, and needs no signing identity or credentials.
    expect(fuses!.resetAdHocDarwinSignature).toBe(true);
    expect(Object.keys(fuses!).sort()).toEqual(
      [
        "runAsNode",
        "enableNodeCliInspectArguments",
        "enableNodeOptionsEnvironmentVariable",
        "grantFileProtocolExtraPrivileges",
        "enableEmbeddedAsarIntegrityValidation",
        "onlyLoadAppFromAsar",
        "enableCookieEncryption",
        "loadBrowserProcessSpecificV8Snapshot",
        "resetAdHocDarwinSignature",
      ].sort(),
    );
  });

  it("test_packaging_includes_the_built_renderer_entry", () => {
    expect(readBuildConfig().files).toContain("out/**/*");
  });

  it("test_the_only_mac_architecture_is_arm64", () => {
    const mac = readBuildConfig().mac;
    expect(mac, "no build.mac in package.json").toBeDefined();
    // Apple-silicon only until an Intel machine tests the Intel build -- adding
    // x64 or universal here ships an untested artefact, which is the named failure.
    // Ceiling carried across unchanged from 7a4da3e.
    //
    // measured: electron-builder's MacConfiguration schema (app-builder-lib
    // options/macOptions.d.ts, this repo's installed 26.15.3) has no top-level
    // `mac.arch` field -- arch is only settable per-target
    // (`TargetConfiguration.arch`, core.d.ts). 7a4da3e's `mac.arch: [arm64]` was never
    // schema-validated (its own commit message: "accept line not run") and would have
    // failed the same way this box's first attempt at that shape did:
    // "configuration.mac should be one of these: null". So the arch ceiling is carried
    // across as `target: [{ target: <existing>, arch: ["arm64"] }]`, not as a bare
    // `mac.arch` key.
    expect(mac!.target).toBeDefined();
    for (const t of mac!.target!) {
      expect(t.arch, `target ${JSON.stringify(t)} has no arch, or one broader than arm64-only`).toEqual(["arm64"]);
    }
  });

  it("test_the_linux_package_is_an_x64_directory_and_an_x64_appimage", () => {
    expect(readBuildConfig().linux?.target).toEqual([
      { target: "dir", arch: ["x64"] },
      { target: "AppImage", arch: ["x64"] },
    ]);
  });
});
