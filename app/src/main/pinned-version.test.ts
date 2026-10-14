import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { findPinnedVersion, versionMismatchLine } from "./pinned-version";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "lockwire-pinned-version-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function makeRepo(withGit: boolean, workflowYaml: string | null): string {
  const contractPath = join(dir, "lockwire.yaml");
  writeFileSync(contractPath, "robot: x500\n");
  if (withGit) mkdirSync(join(dir, ".git"));
  if (workflowYaml !== null) {
    mkdirSync(join(dir, ".github", "workflows"), { recursive: true });
    writeFileSync(join(dir, ".github", "workflows", "lockwire.yml"), workflowYaml);
  }
  return contractPath;
}

describe("the pinned CI version", () => {
  it("test_a_matching_pin_shows_no_line", () => {
    const contractPath = makeRepo(true, "jobs:\n  check:\n    steps:\n      - uses: lockwire-action\n        with:\n          version: 0.1.0\n");
    expect(findPinnedVersion(contractPath)).toBe("0.1.0");
    expect(versionMismatchLine("0.1.0", findPinnedVersion(contractPath))).toBeNull();
  });

  it("test_an_absent_workflow_file_shows_nothing_and_raises_nothing", () => {
    const contractPath = makeRepo(true, null);
    expect(() => findPinnedVersion(contractPath)).not.toThrow();
    expect(findPinnedVersion(contractPath)).toBeNull();
    expect(versionMismatchLine("0.1.0", findPinnedVersion(contractPath))).toBeNull();
  });

  it("test_a_contract_outside_a_repository_shows_nothing", () => {
    // No `.git` anywhere from a fresh tmpdir up -- the walk must stop at the filesystem
    // root rather than throwing when `dirname` stops changing.
    const contractPath = makeRepo(false, "with:\n  version: 9.9.9\n");
    expect(findPinnedVersion(contractPath)).toBeNull();
  });
});
