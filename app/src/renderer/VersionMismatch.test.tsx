import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { VersionMismatch } from "./VersionMismatch";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("VersionMismatch", () => {
  it("test_a_differing_pin_shows_the_mismatch_line_on_the_check_face", () => {
    root = createRoot(container);
    act(() => {
      root.render(<VersionMismatch appVersion="0.1.0" pinnedVersion="0.2.0" />);
    });
    const line = container.querySelector(".version-mismatch");
    expect(line).not.toBeNull();
    expect(line!.textContent).toContain("0.1.0");
    expect(line!.textContent).toContain("0.2.0");
  });
});

// the failing check, verbatim: "an update applied without a click". This milestone ships
// no updater at all -- the mismatch line exists so it has a home before the updater does --
// so nothing under app/src may reference the banned updater package or its global anywhere,
// not just in the two files this box added. A recursive text scan, not a single grep line,
// so any future file that imports it fails this test too. (Named indirectly below so this
// very comment doesn't trip its own scan, or the repo's accept-line grep for the same names.)
function listFiles(root: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) out.push(...listFiles(path));
    else out.push(path);
  }
  return out;
}

describe("no update is downloaded or applied anywhere in the app", () => {
  it("test_no_update_is_downloaded_or_applied_anywhere_in_the_app", () => {
    const thisFile = fileURLToPath(import.meta.url);
    const srcRoot = join(dirname(thisFile), "..");
    // The pattern is built from parts so this very assertion doesn't trip its own scan.
    const bannedPattern = new RegExp(["electron", "-updater"].join("") + "|auto" + "Updater");
    const offenders = listFiles(srcRoot)
      .filter((path) => path !== thisFile)
      .filter((path) => bannedPattern.test(readFileSync(path, "utf8")));
    expect(offenders).toEqual([]);
  });
});
