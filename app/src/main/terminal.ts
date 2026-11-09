import { existsSync, mkdirSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join } from "node:path";

import { app, BrowserWindow } from "electron";
import type { IPty } from "node-pty";

import { TERMINAL_EVENT_CHANNEL } from "../shared/ipc-contract";
import { hasOnlyKeys, isRecord } from "./ipcKit";
import { resolveSidecarCommand, scrubChildEnv } from "./sidecar";

// The drawer's Terminal: a real login shell, with the sidecar's allowlisted env so the
// Assistant's key and other app secrets stay behind, and the app's own engine first on PATH so
// `lockwire` works in it. One pty for the app; a second start replaces the first.
let pty: IPty | null = null;
const MAX_WRITE = 64 * 1024;
// The shell (not the sidecar or the agent's MCP child) also gets the desktop session, so
// browser-opening commands work on Linux; it already runs as the user.
const DESKTOP_ENV = /^(DISPLAY|WAYLAND_DISPLAY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)$/;

// Packaged, the engine is staged as `lockwire-sidecar`; a private bin dir gives it its CLI name.
// Rewritten each start so a moved or updated app never leaves a stale link.
function engineBinDir(engine: string): string {
  const dir = join(app.getPath("userData"), "bin");
  mkdirSync(dir, { recursive: true });
  const link = join(dir, process.platform === "win32" ? "lockwire.cmd" : "lockwire");
  rmSync(link, { force: true });
  if (process.platform === "win32") writeFileSync(link, `@"${engine}" %*\r\n`);
  else symlinkSync(engine, link);
  return dir;
}

function push(event: unknown): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send(TERMINAL_EVENT_CHANNEL, event);
  }
}

const dim = (v: unknown, max: number): v is number => Number.isInteger(v) && (v as number) >= 1 && (v as number) <= max;
const size = (a: unknown): { cols: number; rows: number } => {
  if (!isRecord(a) || !dim(a.cols, 1000) || !dim(a.rows, 1000)) throw new Error("terminal: cols and rows must be integers in 1..1000");
  return { cols: a.cols, rows: a.rows };
};

export function killTerminal(): void {
  const old = pty;
  pty = null;
  try { old?.kill(); } catch { /* already gone */ }
}

export async function handleTerminalStart(args: unknown): Promise<{ ok: true }> {
  const { cols, rows } = size(args);
  const cwdArg = (args as { cwd?: unknown }).cwd;
  if (!hasOnlyKeys(args as Record<string, unknown>, ["cols", "rows", "cwd"]) || (cwdArg !== undefined && typeof cwdArg !== "string")) throw new Error("terminal: bad start arguments");
  const cwd = typeof cwdArg === "string" && isAbsolute(cwdArg) && existsSync(cwdArg) && statSync(cwdArg).isDirectory() ? cwdArg : homedir();
  killTerminal();
  const { spawn } = await import("node-pty"); // lazy: a build where the native module is missing loses the Terminal, not the app
  const shell = process.env.SHELL || (process.platform === "darwin" ? "/bin/zsh" : "/bin/bash");
  const desktop = Object.entries(process.env).filter(([k]) => DESKTOP_ENV.test(k));
  const env = { ...scrubChildEnv(process.env), ...Object.fromEntries(desktop), TERM: "xterm-256color" } as Record<string, string>;
  const engine = resolveSidecarCommand().command; // a bare dev override (`uv`) must not put "." on PATH
  if (isAbsolute(engine)) {
    let bin = dirname(engine);
    try { bin = engineBinDir(engine); } catch { /* userData unwritable: the engine's own dir still works */ }
    env.PATH = [bin, env.PATH].filter(Boolean).join(delimiter);
  }
  const mine = spawn(shell, ["-l"], { name: "xterm-256color", cols, rows, cwd, env });
  pty = mine;
  mine.onData((data) => { if (pty === mine) push({ type: "data", data }); });
  mine.onExit(({ exitCode }) => { if (pty === mine) { pty = null; push({ type: "exit", code: exitCode }); } });
  return { ok: true };
}

export async function handleTerminalWrite(args: unknown): Promise<{ ok: true }> {
  if (!isRecord(args) || !hasOnlyKeys(args, ["data"]) || typeof args.data !== "string" || args.data.length > MAX_WRITE) throw new Error("terminal: data must be a string of at most 64 KiB");
  pty?.write(args.data);
  return { ok: true };
}

export async function handleTerminalResize(args: unknown): Promise<{ ok: true }> {
  const { cols, rows } = size(args);
  if (!hasOnlyKeys(args as Record<string, unknown>, ["cols", "rows"])) throw new Error("terminal: bad resize arguments");
  try { pty?.resize(cols, rows); } catch { /* exited between the check and the call */ }
  return { ok: true };
}

export async function handleTerminalKill(): Promise<{ ok: true }> {
  killTerminal();
  return { ok: true };
}
