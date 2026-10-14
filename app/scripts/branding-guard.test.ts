import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import config from "../package.json";
import { BUNDLE_ID, PRODUCT_NAME } from "../src/shared/branding";
import { assertPackagingBranding } from "./branding-guard";

describe("packaging identity guard", () => {
  const runtime = { BUNDLE_ID, PRODUCT_NAME };
  it("accepts the local identity", () => {
    expect(config.build.appId).toBe(["com", "shivansh", "lockwire"].join("."));
    expect(config.build.productName).toBe(["Lock", "wire"].join(""));
    expect(() => assertPackagingBranding(config.build, runtime)).not.toThrow();
  });
  it("refuses placeholder and runtime mismatch before packaging", () => {
    expect(() => assertPackagingBranding({ ...config.build, appId: "com.valid_app.app" }, { ...runtime, BUNDLE_ID: "com.valid_app.app" })).toThrow("malformed");
    expect(() => assertPackagingBranding({ ...config.build, appId: "com.example.placeholder" }, runtime)).toThrow("placeholder");
    expect(() => assertPackagingBranding(config.build, { ...runtime, BUNDLE_ID: "com.other.identity" })).toThrow("differs");
  });
  it("the real package entry refuses a split identity before its build commands", () => {
    const root = mkdtempSync(join(tmpdir(), "package-branding-"));
    try {
      mkdirSync(join(root, "src/shared"), { recursive: true });
      cpSync(import.meta.dirname, join(root, "scripts"), { recursive: true });
      writeFileSync(join(root, "package.json"), JSON.stringify(config));
      writeFileSync(join(root, "src/shared/branding.ts"), 'export const PRODUCT_NAME = ' + JSON.stringify(PRODUCT_NAME) + '; export const BUNDLE_ID = "com.other.identity";');
      expect(() => execFileSync(process.execPath, [join(root, "scripts/package.mjs"), "--check-branding"], { stdio: "pipe" })).toThrow("differs from runtime branding");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
