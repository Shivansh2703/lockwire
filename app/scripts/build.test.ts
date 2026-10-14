import { describe, it, expect, beforeAll } from "vitest";
import { execSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { BUNDLE_ID, PRODUCT_NAME } from "../src/shared/branding";

const here = dirname(fileURLToPath(import.meta.url));
const APP_ROOT = join(here, "..");

describe("Packaged build (identity)", () => {
  let appPath: string;
  let plistContent: Record<string, any>;

  beforeAll(() => {
    // Locate the packaged .app first
    const distPath = join(APP_ROOT, "dist");
    const appDirs = [
      join(distPath, "mac-arm64", `${PRODUCT_NAME}.app`),
      join(distPath, "mac-x64", `${PRODUCT_NAME}.app`),
      join(distPath, `${PRODUCT_NAME}.app`),
    ];

    appPath = appDirs.find((p) => existsSync(p));

    // Packaging is a sequential prerequisite for this inspection. Running it from
    // Vitest races the app suite and exceeds Vitest's hook timeout.
    if (!appPath) {
      throw new Error(
        `Packaged app required before this test. Run npm run package. Searched: ${appDirs.join(", ")}`
      );
    }

    console.log(`Found packaged app at: ${appPath}`);

    // Read Info.plist
    const plistPath = join(appPath, "Contents", "Info.plist");
    if (!existsSync(plistPath)) {
      throw new Error(`Info.plist not found at ${plistPath}`);
    }

    const plistXml = readFileSync(plistPath, "utf-8");
    // Simple XML parsing for plist (can't add xml2js dependency)
    // Use regex to extract key-value pairs
    plistContent = parsePlist(plistXml);
  });

  it(`should have display name '${PRODUCT_NAME}'`, () => {
    // In Info.plist, CFBundleDisplayName is the display name
    expect(plistContent["CFBundleDisplayName"]).toBe(PRODUCT_NAME);
  });

  it("should have the canonical bundle id", () => {
    // In Info.plist, CFBundleIdentifier is the bundle id
    expect(plistContent["CFBundleIdentifier"]).toBe(BUNDLE_ID);
  });

  it("should have icon resource", () => {
    // Check if the icon file exists in the app bundle
    // Electron-builder converts PNG to ICNS on macOS
    const iconPath = join(appPath, "Contents", "Resources", "icon.icns");
    expect(existsSync(iconPath)).toBe(true);
  });

  it("should have no developer-identity signature", () => {
    // `CSCodeSignatureData` is not a real Info.plist key (codesign reports
    // `Info.plist=not bound`), so its absence proves nothing and would still
    // pass once developer signing is wired up. macOS also ad-hoc signs arm64
    // binaries automatically, so this app is never literally "unsigned" —
    // the real claim is the absence of a DEVELOPER-IDENTITY signature, which
    // only `codesign`'s authority chain / Team ID can show.
    const report = codesignReport(appPath);
    expect(hasDeveloperIdentitySignature(report)).toBe(false);
    // Sanity: confirm this is macOS's automatic ad-hoc signature, not a
    // build that skipped signing entirely.
    expect(report).toMatch(/flags=0x(?:2|20002)\(adhoc(?:,linker-signed)?\)/);
  });

  // Rule 30: a guard whose failing condition nobody has observed is a guard
  // nobody has tested. Prove `hasDeveloperIdentitySignature` actually
  // discriminates by pointing it at a genuinely developer-signed app already
  // installed on this machine. Skips (rather than fails) when none of the
  // candidates are present, e.g. on a bare CI runner.
  const KNOWN_DEVELOPER_SIGNED_CANDIDATES = [
    "/Applications/Brave Browser.app",
    "/Applications/Google Chrome.app",
    "/Applications/Visual Studio Code.app",
    "/Applications/Slack.app",
    "/Applications/1Password.app",
  ];
  const knownSignedApp = KNOWN_DEVELOPER_SIGNED_CANDIDATES.find((p) =>
    existsSync(p)
  );

  it.skipIf(!knownSignedApp)(
    "discriminates: reports signed for a real developer-signed app",
    () => {
      const report = codesignReport(knownSignedApp!);
      expect(hasDeveloperIdentitySignature(report)).toBe(true);
    }
  );
});

/**
 * Runs `codesign -dvvv` against a bundle and returns its combined
 * stdout+stderr report. codesign writes the report to stderr and exits
 * non-zero for a fully unsigned bundle, so both streams and both exit
 * codes must be captured.
 */
function codesignReport(appPath: string): string {
  try {
    return execSync(`codesign -dvvv "${appPath}" 2>&1`, {
      encoding: "utf-8",
    });
  } catch (error: any) {
    return String(error.stdout ?? "") + String(error.stderr ?? "");
  }
}

/**
 * True only when `codesign`'s authority chain names a real developer
 * identity (Developer ID / Apple Development / Apple Distribution / 3rd
 * Party Mac Developer) AND a Team ID is bound — an ad-hoc signature (our
 * unsigned build) and Apple's own platform signature (e.g. system apps)
 * both fail this: they carry `TeamIdentifier=not set` and no such Authority
 * line.
 */
function hasDeveloperIdentitySignature(report: string): boolean {
  const hasDeveloperAuthority =
    /Authority=(Developer ID|Apple Development|Apple Distribution|3rd Party Mac Developer)/.test(
      report
    );
  const hasTeamId = /TeamIdentifier=(?!not set)\S/.test(report);
  return hasDeveloperAuthority && hasTeamId;
}

/**
 * Simple plist parser for XML format
 * Extracts key-value pairs from a macOS plist file
 */
function parsePlist(
  xml: string
): Record<string, any> {
  const result: Record<string, any> = {};

  // Match pattern: <key>KeyName</key><type>value</type>
  const keyRegex = /<key>([^<]+)<\/key>\s*<([^>]+)>([^<]*)<\/\2>/g;
  let match;

  while ((match = keyRegex.exec(xml)) !== null) {
    const key = match[1];
    const type = match[2];
    const value = match[3];

    if (type === "string") {
      result[key] = value;
    } else if (type === "true") {
      result[key] = true;
    } else if (type === "false") {
      result[key] = false;
    } else if (type === "integer") {
      result[key] = parseInt(value, 10);
    } else {
      result[key] = value;
    }
  }

  return result;
}
