import { mkdtempSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { watchContractMtime } from "./watch";

let dir: string;
let filePath: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "lockwire-watch-"));
  filePath = join(dir, "lockwire.yaml");
  writeFileSync(filePath, "robot: x500\n");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe("the contract mtime watcher (e2-k)", () => {
  // Named for the brief's decisive case ("do it with the window FOCUSED"), but this module has
  // no focus API to assert on -- its whole point is that BrowserWindow.isFocused() never enters
  // the loop. What this proves instead is the thing that actually matters: a plain mtime change
  // fires the callback with nothing about window focus anywhere in the mechanism, so it cannot
  // regress into the v2 behaviour (re-read only on focus) by construction.
  it("test_the_watcher_fires_while_the_window_has_focus", async () => {
    let fired = 0;
    const watcher = watchContractMtime(filePath, () => fired++, 20);
    await sleep(60);
    expect(fired).toBe(0); // no change yet -- must not fire on the initial read

    const future = new Date(Date.now() + 5000);
    utimesSync(filePath, future, future);
    await sleep(150);
    watcher.stop();
    expect(fired).toBeGreaterThanOrEqual(1);
  });

  // The box (e2-k row, e2-k's reviewer 22:0x): a rewrite that lands inside the mtime clock's
  // grain leaves mtimeMs unchanged, so a watcher comparing mtime alone reports a fired count of
  // 0 while the file's content actually changed -- the app keeps showing a stale robot. Uses
  // fake timers rather than a real sleep, because the whole defect under test is a change that
  // does NOT move the clock; sleeping to cross an mtime boundary would prove the opposite thing.
  it("test_a_same_mtime_content_rewrite_still_fires", async () => {
    vi.useFakeTimers();
    try {
      // Pin a whole-second mtime BEFORE the baseline: utimes cannot write back an arbitrary
      // nanosecond mtime exactly (Linux ext4 loses the last digits through the float seconds),
      // and a pin that drifts would make even the mtime-only code fire and hide the bug this
      // test exists to catch. A whole second round-trips exactly everywhere.
      const pinnedSeconds = Math.floor(Date.now() / 1000) - 60;
      utimesSync(filePath, pinnedSeconds, pinnedSeconds);
      const pinnedMtimeMs = statSync(filePath).mtimeMs;

      let fired = 0;
      const watcher = watchContractMtime(filePath, () => fired++, 20);

      // Let the initial poll run and capture the baseline mtime/hash.
      await vi.advanceTimersByTimeAsync(0);
      expect(fired).toBe(0);

      // Rewrite the content, then pin mtime back to the exact value already observed --
      // the clock does not move at all, let alone cross a grain boundary.
      writeFileSync(filePath, "robot: x900\n");
      utimesSync(filePath, pinnedSeconds, pinnedSeconds);
      expect(statSync(filePath).mtimeMs).toBe(pinnedMtimeMs);

      await vi.advanceTimersByTimeAsync(20);
      watcher.stop();

      expect(fired).toBeGreaterThanOrEqual(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
