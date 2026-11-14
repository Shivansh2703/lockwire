// Display only: one plain line for a failed open, the raw engine text kept for a fold-out.
// A retry is offered only where it can succeed (transient failure, or a refresh that re-reads the page); the same file with no change fails the same way.
export type OpenFailure = { line: string; raw?: string; retry: boolean };

// Only the strings main/sidecar.ts really produces; loose words like "timeout" also appear in engine text.
const TRANSIENT = /sidecar is not ready|the sidecar (stopped|could not be started)|sidecar did not send its ready line|could not start sidecar/;
// A missing mesh or package file inside the model is not "the file you opened is gone".
const MESHY = /mesh|package:\/\/|\.(stl|dae|obj|ply|glb|gltf)\b/i;
const OURS = /^(Drop on the robot|Lockfile was written)/;

export function explainOpenError(raw: string): OpenFailure {
  if (OURS.test(raw)) return { line: raw, retry: false };
  if (/^Opened the check, but document refresh failed/.test(raw)) {
    return { line: "The check opened, but its documents could not refresh. Open the file again to try once more.", raw, retry: true };
  }
  if (TRANSIENT.test(raw)) return { line: raw, retry: true };
  if (/no contract at /.test(raw) || (/ENOENT|no such file/i.test(raw) && !MESHY.test(raw))) return { line: "That file is no longer there. Choose a different file.", raw, retry: false };
  if (/EACCES|EPERM|permission denied/i.test(raw)) return { line: "Lockwire is not allowed to read that file. Fix its permissions or choose a different file.", raw, retry: false };
  return { line: "This file could not be opened as a robot model. Choose a different file.", raw, retry: false };
}
