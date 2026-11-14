import { describe, expect, it } from "vitest";
import { explainOpenError } from "./openError";

const MISSING = "That file is no longer there. Choose a different file.";
const DENIED = "Lockwire is not allowed to read that file. Fix its permissions or choose a different file.";
const GENERIC = "This file could not be opened as a robot model. Choose a different file.";
const REFRESH = "Opened the check, but document refresh failed: ";

describe("explainOpenError", () => {
  it.each([
    ["no contract at /x/arm.lock", MISSING, false],
    ["ENOENT: no such file or directory, open '/x/arm.urdf'", MISSING, false],
    ["EACCES: permission denied, open '/x/arm.urdf'", DENIED, false],
    ["ENOENT: no such file or directory, open '/x/meshes/link0.stl'", GENERIC, false],
    ["the URDF mesh is unavailable, restart the file after a timeout", GENERIC, false],
    ["sidecar is not ready (starting)", "sidecar is not ready (starting)", true],
    ["the sidecar stopped", "the sidecar stopped", true],
    ["Error invoking remote method 'start': Error: the sidecar could not be started", undefined, true],
    ["sidecar did not send its ready line within 20000ms", undefined, true],
    ["could not start sidecar (ENOENT); check the app installation and sidecar executable", undefined, true],
    ["Drop on the robot's body, or use Place on… in the part panel.", "Drop on the robot's body, or use Place on… in the part panel.", false],
    ["Lockfile was written, but opening failed: sidecar is not ready (starting)", "Lockfile was written, but opening failed: sidecar is not ready (starting)", false],
  ])("%s", (raw, line, retry) => {
    const f = explainOpenError(raw);
    if (line) expect(f.line).toBe(line);
    expect(f.retry).toBe(retry);
  });

  it("a refresh failure wins over transient words in its inner text and keeps its fold", () => {
    const raw = REFRESH + "the sidecar stopped (timeout)";
    const f = explainOpenError(raw);
    expect(f.line).toContain("Open the file again to try once more.");
    expect([f.retry, f.raw]).toEqual([true, raw]);
  });
});
