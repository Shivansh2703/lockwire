// the app never updates silently; when it differs from the version pinned in the
// contract repository's own CI workflow, the Check face shows one line. This file only
// *detects* the pin -- present and matching means nothing, present and differing is the
// caller's job to render, and absent (no workflow file yet, or the contract sits outside a
// repository at all) means nothing too, never an error and never a thrown exception.
//
// Measured on this branch: `.github/workflows/lockwire.yml` does not exist in this repo --
// the action that would carry the `version:` pin is a later milestone's box (PLAN_ISSUES
// #7) -- so every real read today takes the "absent" path. No update is downloaded or
// applied anywhere in this file, or anywhere in this app: E6's updater does not exist yet.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

/** Walk up from `contractPath` looking for a repository root (a `.git` entry -- a plain repo
 * has it as a directory, a worktree as a file; `existsSync` doesn't care which). Returns
 * null once the walk reaches the filesystem root without finding one. */
function findRepoRoot(contractPath: string): string | null {
  let dir = dirname(contractPath);
  for (;;) {
    if (existsSync(join(dir, ".git"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** The `version:` input pinned in the repo's `.github/workflows/lockwire.yml`, or null when
 * the contract sits outside a repository or that repository has no such workflow file. */
export function findPinnedVersion(contractPath: string): string | null {
  const root = findRepoRoot(contractPath);
  if (root === null) return null;
  const workflowPath = join(root, ".github", "workflows", "lockwire.yml");
  if (!existsSync(workflowPath)) return null;
  const text = readFileSync(workflowPath, "utf8");
  const match = /^\s*version:\s*["']?([^"'\s]+?)["']?\s*$/m.exec(text);
  return match ? match[1]! : null;
}

/** Pure comparison, no filesystem access: null when there's nothing to say (no pin found,
 * or it matches the running app), else the exact line the Check face renders. */
export function versionMismatchLine(appVersion: string, pinnedVersion: string | null): string | null {
  if (pinnedVersion === null || pinnedVersion === appVersion) return null;
  return `This app is v${appVersion}; the team's CI pins v${pinnedVersion}.`;
}
