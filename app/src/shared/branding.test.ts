import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { BUNDLE_ID, PRODUCT_NAME } from "./branding";

const APP = join(import.meta.dirname, "..", "..");
const REPO = join(APP, "..");
const SKIP = new Set(["node_modules", "dist", "out", "package-lock.json", "package.json"]);

/** Every authored source/test text file under app/. Build metadata is skipped. */
function appFiles(dir = APP): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (SKIP.has(entry.name)) return [];
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return appFiles(path);
    return entry.isFile() ? [path] : [];
  });
}

/** Occurrences of `needle` across the whole app tree, not just the file count. */
function occurrences(needle: string): number {
  return appFiles().reduce((n, f) => n + readFileSync(f, "utf8").split(needle).length - 1, 0);
}

describe("build metadata owns the local identity", () => {
  it("derives runtime branding without a second authored literal", () => {
    const { build } = JSON.parse(readFileSync(join(APP, "package.json"), "utf8"));
    expect(BUNDLE_ID).toBe(build.appId);
    expect(PRODUCT_NAME).toBe(build.productName);
    expect(occurrences(BUNDLE_ID)).toBe(0);
    expect(occurrences(PRODUCT_NAME)).toBe(0);
  });
});

describe("the package manager is npm", () => {
  it("test_no_pnpm_lockfile_and_no_pnpm_script", () => {
    for (const dir of [APP, REPO]) expect(existsSync(join(dir, "pnpm-lock.yaml"))).toBe(false);
    expect(readFileSync(join(APP, "package.json"), "utf8")).not.toContain("pnpm");
  });
});

describe("the dependency floor and ceiling are both exact", () => {
  const pkg = JSON.parse(readFileSync(join(APP, "package.json"), "utf8"));

  it("test_every_dependency_is_pinned_to_an_exact_version", () => {
    const all = { ...pkg.dependencies, ...pkg.devDependencies };
    expect(Object.keys(all).length).toBeGreaterThan(0);
    for (const [name, version] of Object.entries(all)) {
      expect(version, `${name} is not pinned exactly`).toMatch(/^\d+\.\d+\.\d+$/);
    }
  });

  it("test_the_node_floor_satisfies_the_locked_engines", () => {
    expect(pkg.engines?.node).toBe(">=22.22.2");
  });
});
