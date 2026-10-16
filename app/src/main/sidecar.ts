// The main process's one Python sidecar: spawned by absolute path, watched for
// its ready line, and torn down two ways -- an explicit stop() on quit, and the
// child's own "--exit-on-stdin-close" watching this process's stdin pipe, which the
// OS closes the instant this process dies for any reason (crash, SIGKILL, clean
// quit). The pipe is the orphan-proofing; stop() just makes the common case prompt.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { app } from "electron";

import { PRODUCT_NAME } from "../shared/branding";
import { logDirectory } from "./log";

const STARTUP_TIMEOUT_MS = 10_000;
// built from the one branding constant rather than typed again, so the header
// name (specified on the Python side reuse) doesn't become a second spelling.
const TOKEN_HEADER = `X-${PRODUCT_NAME}-Token`;

export type SidecarState =
  | { phase: "starting" }
  | { phase: "ready"; port: number }
  | { phase: "stopped"; code: number | null; signal: NodeJS.Signals | null }
  | { phase: "error"; message: string };

export interface SidecarOverrides {
  command?: string;
  args?: string[];
  env?: NodeJS.ProcessEnv;
}

interface ReadyLine {
  lockwire: "ready";
  version: string;
  port: number;
  token: string;
}

function isReadyLine(value: unknown): value is ReadyLine {
  const v = value as Partial<ReadyLine> | null;
  return (
    typeof v === "object" &&
    v !== null &&
    v.lockwire === "ready" &&
    typeof v.port === "number" &&
    typeof v.token === "string"
  );
}

// Dev: the venv console script, found by walking up from this file to the repo root
// (the directory holding .venv) -- never a PATH search, so a second `lockwire` on the
// operator's PATH cannot be spawned in its place. Packaged: the frozen binary
// electron-builder places under resourcesPath, one per platform/arch. The switch is
// `app.isPackaged`, not `process.resourcesPath`'s truthiness -- Electron sets
// resourcesPath in BOTH modes (in dev it points at the Electron binary's own resources
// dir), so testing it for truthiness picks the packaged branch the first time a
// packaged build runs too, by accident (e2-hl's reviewer, 00:0x).
// `LOCKWIRE_SIDECAR_COMMAND`/`_ARGS` are a manual-testing escape hatch only (not used
// by the unit tests, which pass overrides directly to startSidecar()).
export function resolveSidecarCommand(): { command: string; args: string[] } {
  const args = ["serve", "--exit-on-stdin-close"];
  if (!app.isPackaged && process.env.LOCKWIRE_SIDECAR_COMMAND) {
    return {
      command: process.env.LOCKWIRE_SIDECAR_COMMAND,
      args: process.env.LOCKWIRE_SIDECAR_ARGS?.split(" ").filter(Boolean) ?? [],
    };
  }
  if (app.isPackaged) {
    const resourcesPath = (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
    const binName = process.platform === "win32" ? "lockwire-sidecar.exe" : "lockwire-sidecar";
    return {
      command: join(resourcesPath ?? "", `sidecar-${process.platform}-${process.arch}`, binName),
      args,
    };
  }
  let dir = dirname(fileURLToPath(import.meta.url));
  while (!existsSync(join(dir, ".venv")) && dirname(dir) !== dir) dir = dirname(dir);
  const scriptsDir = process.platform === "win32" ? "Scripts" : "bin";
  const binName = process.platform === "win32" ? "lockwire.exe" : "lockwire";
  return { command: join(dir, ".venv", scriptsDir, binName), args };
}

interface PendingCall {
  reject: (err: Error) => void;
  abort: AbortController;
}

const events = new EventEmitter();
let child: ChildProcessWithoutNullStreams | null = null;
let state: SidecarState = { phase: "stopped", code: null, signal: null };
let token: string | null = null;
const pending = new Set<PendingCall>();

function setState(next: SidecarState): void {
  state = next;
  events.emit("state", next);
}

function failPending(message: string): void {
  for (const call of pending) {
    call.abort.abort();
    call.reject(new Error(message));
  }
  pending.clear();
}

export function getSidecarState(): SidecarState {
  return state;
}

export function onSidecarState(cb: (next: SidecarState) => void): () => void {
  events.on("state", cb);
  return () => events.off("state", cb);
}

/** Starts (or restarts) the sidecar. One child at a time -- a prior one is stopped first. */
export function startSidecar(overrides: SidecarOverrides = {}): void {
  stopSidecar();

  const resolved = overrides.command
    ? { command: overrides.command, args: overrides.args ?? [] }
    : resolveSidecarCommand();
  const args = resolved.args.includes("--exit-on-stdin-close")
    ? resolved.args
    : [...resolved.args, "--exit-on-stdin-close"];

  token = null;
  setState({ phase: "starting" });

  const proc = spawn(resolved.command, args, {
    stdio: ["pipe", "pipe", "pipe"],
    // --user-data-dir runs (e2e/capture) keep the sidecar's log out of the real ~/Library/Logs.
    env: app?.commandLine?.hasSwitch("user-data-dir")
      ? { ...(overrides.env ?? process.env), LOCKWIRE_LOG_DIR: logDirectory() }
      : (overrides.env ?? process.env),
  });
  child = proc;
  proc.stdin.on("error", () => {}); // EPIPE if the child exits before we ever write

  const timer = setTimeout(() => {
    if (state.phase === "starting") {
      setState({
        phase: "error",
        message: `sidecar did not send its ready line within ${STARTUP_TIMEOUT_MS}ms`,
      });
    }
  }, STARTUP_TIMEOUT_MS);
  timer.unref?.();

  const rl = createInterface({ input: proc.stdout });
  rl.once("line", (line) => {
    clearTimeout(timer);
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      setState({ phase: "error", message: "sidecar sent a malformed ready line" });
      return;
    }
    if (!isReadyLine(parsed)) {
      setState({ phase: "error", message: "sidecar sent a malformed ready line" });
      return;
    }
    token = parsed.token;
    setState({ phase: "ready", port: parsed.port });
  });

  proc.once("error", (err) => {
    clearTimeout(timer);
    rl.close();
    if (child !== proc) return;
    child = null;
    token = null;
    const code = (err as NodeJS.ErrnoException).code;
    setState({
      phase: "error",
      message: `could not start sidecar (${code ?? err.message}); check the app installation and sidecar executable`,
    });
    failPending("the sidecar could not be started");
  });

  proc.once("exit", (code, signal) => {
    clearTimeout(timer);
    rl.close();
    // A superseded child (already replaced by a later startSidecar()) reporting its
    // exit late must not clobber the *current* child's state.
    if (child !== proc) return;
    child = null;
    token = null;
    setState({ phase: "stopped", code, signal });
    failPending("the sidecar stopped");
  });
}

/** Explicit, prompt teardown for the common case (app quit). Idempotent. The 'exit'
 * handler above (not this function) is what transitions state to "stopped". */
export function stopSidecar(): void {
  if (child && child.exitCode === null && child.signalCode === null) {
    child.kill();
  }
}

// The one authenticated door onto the sidecar's HTTP surface: `token` lives only
// in this module's closure, and this is the only function that ever reads it -- it is
// attached to the request header and never part of anything this module returns, so no
// caller (including a compromised renderer, which never reaches this main-process
// module at all) can extract the raw value through this module's exported surface.
export function callSidecar(path: string, init: RequestInit = {}): Promise<Response> {
  if (state.phase !== "ready" || token === null) {
    return Promise.reject(new Error(`sidecar is not ready (${state.phase})`));
  }
  const port = state.port;
  const controller = new AbortController();
  const entry: PendingCall = { reject: () => {}, abort: controller };
  // A promise that only ever rejects (when failPending() fires), raced against the
  // real fetch -- Promise.race attaches to both, so whichever loses is still
  // "handled" and never surfaces as a Node unhandled-rejection warning.
  const stoppedSignal = new Promise<never>((_resolve, reject) => {
    entry.reject = reject;
  });
  pending.add(entry);
  const fetchPromise = fetch(`http://127.0.0.1:${port}${path}`, {
    ...init,
    signal: controller.signal,
    headers: { ...init.headers, [TOKEN_HEADER]: token as string },
  });
  return Promise.race([fetchPromise, stoppedSignal]).finally(() => pending.delete(entry));
}
