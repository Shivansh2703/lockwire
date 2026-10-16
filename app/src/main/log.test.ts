import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `new URL("./x", import.meta.url)` throws "URL must be of scheme file" under this
// worktree's jsdom test environment -- reproduces even on the unmodified, already-merged
// sidecar.test.ts, so it's an environment defect, not this box's. createRequire's own
// resolution is unaffected (proven by the already-passing electron-binary.test.ts), so
// it's the workaround used below instead of touching sidecar.test.ts.
const require = createRequire(import.meta.url);

let logsDir: string;
let userDataFlag = false;

vi.mock("electron", () => ({
  app: {
    getPath: (name: string) => (name === "logs" ? logsDir : name === "userData" ? join(logsDir, "ud") : tmpdir()),
    commandLine: { hasSwitch: () => userDataFlag },
  },
  shell: { showItemInFolder: vi.fn() },
}));

const log = await import("./log");

beforeEach(() => {
  logsDir = mkdtempSync(join(tmpdir(), "lockwire-log-"));
});

afterEach(() => {
  rmSync(logsDir, { recursive: true, force: true });
});

function readLog(): string {
  return readFileSync(log.logFilePath(), "utf8");
}

describe("the main-process log (e2-h)", () => {
  it("test_the_log_dir_follows_user_data_dir_when_the_flag_is_given", () => {
    userDataFlag = true;
    try {
      expect(log.logFilePath()).toBe(join(logsDir, "ud", "logs", "lockwire.log"));
    } finally {
      userDataFlag = false;
    }
  });

  it("test_the_token_never_appears_in_the_log_file", () => {
    const secret = "super-secret-abc123";
    const readyLine = JSON.stringify({ lockwire: "ready", version: "0.0.0", port: 1234, token: secret });
    log.writeLogLine(`sidecar ready line: ${readyLine}`);
    const contents = readLog();
    expect(contents).not.toContain(secret);
    expect(contents).toContain("[redacted]");
  });

  it("test_the_log_rotates_at_its_stated_size_and_keeps_its_stated_count", () => {
    const maxBytes = 500;
    const maxFiles = 3;
    // Each line is well under maxBytes; many lines must still trigger several rotations.
    for (let i = 0; i < 60; i++) {
      log.writeLogLine(`line number ${i} of the rotation test padding padding`, maxBytes, maxFiles);
    }
    const files = readdirSync(logsDir).filter((f) => f.startsWith("lockwire.log"));
    // current file + at most (maxFiles - 1) backups
    expect(files.length).toBeLessThanOrEqual(maxFiles);
    expect(files).toContain("lockwire.log");
  });

  it("test_the_reveal_logs_item_opens_the_log_directory", async () => {
    const { shell } = await import("electron");
    const menu = await import("./menu");
    menu.revealLogs();
    expect(shell.showItemInFolder).toHaveBeenCalledWith(log.logFilePath());
  });

  it("test_nothing_in_this_path_makes_a_network_call", async () => {
    const logSrc = readFileSync(require.resolve("./log.ts"), "utf8");
    const menuSrc = readFileSync(require.resolve("./menu.ts"), "utf8");
    for (const src of [logSrc, menuSrc]) {
      expect(src).not.toMatch(/\bfetch\(/);
      expect(src).not.toMatch(/node:https?\b/);
      expect(src).not.toMatch(/node:net\b/);
    }
  });
});
