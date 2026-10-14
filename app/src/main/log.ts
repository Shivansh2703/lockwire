// The main process's own rotating log file (v8 S5.10: "Local rotating log files only. An
// in-app 'reveal logs'. No telemetry, no error-reporting service, no phone-home.").
// This is NOT the sidecar's log -- src/lockwire imports `logging` nowhere (measured), so
// the sidecar's own log is unbuilt and out of scope here (PLAN_ISSUES #6). This file only
// ever writes lines the main process hands it -- lifecycle events, failures, restarts.
import { appendFileSync, existsSync, mkdirSync, renameSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";

import { app } from "electron";

export const MAX_LOG_BYTES = 262_144; // 256 KiB per file, stated size
export const MAX_LOG_FILES = 5; // lockwire.log + 4 rotated backups, stated count

function logDirectory(): string {
  return app.getPath("logs");
}

export function logFilePath(): string {
  return join(logDirectory(), "lockwire.log");
}

// The sidecar's ready line carries its token as `"token":"<value>"` (sidecar.ts's
// ReadyLine shape). Every line this module writes is scrubbed of that shape regardless
// of what the caller passed in -- v8 is explicit that the token is never written, and a
// scrub the caller could just remember to do is not a guard (DECORATION LAW).
function scrub(line: string): string {
  return line.replace(/"token"\s*:\s*"[^"]*"/g, '"token":"[redacted]"');
}

function rotate(path: string, maxBytes: number, maxFiles: number): void {
  if (!existsSync(path) || statSync(path).size < maxBytes) return;
  const oldest = `${path}.${maxFiles - 1}`;
  if (existsSync(oldest)) unlinkSync(oldest);
  for (let i = maxFiles - 2; i >= 1; i--) {
    const src = `${path}.${i}`;
    if (existsSync(src)) renameSync(src, `${path}.${i + 1}`);
  }
  renameSync(path, `${path}.1`);
}

/**
 * Appends one scrubbed, timestamped line to the log, rotating first if the current file
 * has already reached its stated size. `maxBytes`/`maxFiles` are overridable only so the
 * rotation test can force it in milliseconds instead of writing 256 KiB for real; every
 * production call uses the stated defaults.
 */
export function writeLogLine(line: string, maxBytes = MAX_LOG_BYTES, maxFiles = MAX_LOG_FILES): void {
  const dir = logDirectory();
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const path = logFilePath();
  rotate(path, maxBytes, maxFiles);
  appendFileSync(path, `${new Date().toISOString()} ${scrub(line)}\n`);
}
