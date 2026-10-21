// This file's tests exercise the main process only, never the DOM -- and must run under
// the "node" environment, not this project's default "jsdom" (set for the renderer's
// component tests). Measured: under jsdom, `vi.mock("node:child_process", ...)` mocks the
// binding this test file itself imports but NOT the binding sidecar.ts's own static
// `import { spawn } from "node:child_process"` resolves to -- `"mock" in spawn` measured
// `false` inside sidecar.ts and `true` in this file for the identical specifier, so the
// real (unmocked) spawn ran the real child successfully while every assertion reading the
// spy (`lastSpawnArgs()`/`lastSpawnedChild()`) saw zero recorded calls and threw "spawn was
// never called". Switching only this test file to the "node" environment (the same
// environment jsdom is opting *out* of at the project default) makes both bindings resolve
// to the one mocked module, and is the narrowest fix: it does not touch sidecar.ts, which
// never needed jsdom to begin with.
// @vitest-environment node
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `new URL(relative, import.meta.url)` does not yield a `file:` URL under this project's
// jsdom vitest environment (jsdom-30 / Node-26.5), so path resolution goes through
// `createRequire(...).resolve(...)` instead -- the same workaround used in
// electron-binary.test.ts for the identical pattern.
const require = createRequire(import.meta.url);

// Wraps the real node:child_process.spawn in a spy that still runs the real
// implementation, so tests can inspect exactly what sidecar.ts passed it (args, the
// live ChildProcess/pid) without adding any test-only export to the production module.
vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  return { ...actual, spawn: vi.fn(actual.spawn) };
});

// `mockIsPackaged` is read via a getter (not a fixed literal) so each test can flip it
// and see resolveSidecarCommand() react -- proves the switch is app.isPackaged, not
// process.resourcesPath's truthiness (which Electron sets in dev too, e2-sp).
let mockIsPackaged = false;
let mockUserDataDirSwitch = false;
vi.mock("electron", () => ({
  app: {
    commandLine: { hasSwitch: (name: string) => mockUserDataDirSwitch && name === "user-data-dir" },
    getPath: (name: string) => `/tmp/mock-${name}`,
    get isPackaged() {
      return mockIsPackaged;
    },
  },
}));

const logged: string[] = [];
vi.mock("./log", async (orig) => ({ ...(await orig<typeof import("./log")>()), writeLogLine: (l: string) => void logged.push(l) }));

const { spawn } = await import("node:child_process");
const sidecar = await import("./sidecar");

const FAKE = require.resolve("../../test/fake-sidecar.mjs");

function waitFor(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (predicate()) return resolve();
      if (Date.now() - start > timeoutMs) return reject(new Error("waitFor timed out"));
      setTimeout(tick, 10);
    };
    tick();
  });
}

/** The ChildProcess object from the most recent spawn() call, via the spy from vi.mock above. */
function lastSpawnedChild(): import("node:child_process").ChildProcess {
  const results = vi.mocked(spawn).mock.results;
  const last = results.at(-1);
  if (!last) throw new Error("spawn was never called");
  return last.value as import("node:child_process").ChildProcess;
}

/** The args array from the most recent spawn() call. */
function lastSpawnArgs(): string[] {
  const calls = vi.mocked(spawn).mock.calls;
  const last = calls.at(-1);
  if (!last) throw new Error("spawn was never called");
  return last[1] as string[];
}

function start(env: NodeJS.ProcessEnv = {}): void {
  sidecar.startSidecar({
    command: process.execPath,
    args: [FAKE],
    env: { ...process.env, ...env },
  });
}

afterEach(() => {
  sidecar.stopSidecar();
  vi.useRealTimers();
  vi.clearAllMocks();
  mockIsPackaged = false;
  mockUserDataDirSwitch = false;
  (process as unknown as { resourcesPath?: string }).resourcesPath = undefined;
});

const HAS_VENV = existsSync(join(dirname(require.resolve("../../../pyproject.toml")), ".venv"));

describe("the sidecar's lifecycle and token (e2-t)", () => {
  // a hostile .xacro reads `$(env NAME)`; the child must not inherit the app's secrets.
  it("test_M20_the_sidecar_child_env_is_an_allowlist_not_the_apps_whole_environment", () => {
    process.env.FAKE_SECRET_FOR_TEST = "x";
    process.env.LOCKWIRE_M20_PROBE = "kept";
    process.env.ROS_PACKAGE_PATH = "/opt/ros/pkgs";
    try {
      sidecar.startSidecar({ command: process.execPath, args: [FAKE] });
      const env = (vi.mocked(spawn).mock.calls.at(-1)![2] as { env: NodeJS.ProcessEnv }).env;
      expect(env.FAKE_SECRET_FOR_TEST).toBeUndefined();
      expect(env.LOCKWIRE_M20_PROBE).toBe("kept");
      expect(env.ROS_PACKAGE_PATH).toBe("/opt/ros/pkgs"); // scene_urdf.py resolves package:// from it
      expect(env.PATH).toBe(process.env.PATH);
      expect(env.HOME).toBe(process.env.HOME);
    } finally {
      delete process.env.FAKE_SECRET_FOR_TEST;
      delete process.env.LOCKWIRE_M20_PROBE;
      delete process.env.ROS_PACKAGE_PATH;
    }
  });

  it("test_M31_stderr_is_drained_into_the_log_so_a_chatty_child_cannot_block", async () => {
    logged.length = 0;
    start({ FAKE_SIDECAR_STDERR_KB: "300" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    expect(logged.some((l) => l.startsWith("sidecar stderr:"))).toBe(true);
  });

  it("test_L70_a_superseded_childs_late_ready_line_does_not_set_the_new_state", () => {
    start({ FAKE_SIDECAR_MODE: "silent" });
    const old = lastSpawnedChild();
    start({ FAKE_SIDECAR_MODE: "silent" });
    old.stdout!.emit("data", Buffer.from('{"lockwire":"ready","version":"0","port":1,"token":"old"}\n'));
    expect(sidecar.getSidecarState()).toEqual({ phase: "starting" });
  });

  it("test_the_ready_line_yields_a_port_and_a_token", async () => {
    start({ FAKE_SIDECAR_MODE: "ready", FAKE_SIDECAR_TOKEN: "secret-abc" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    const state = sidecar.getSidecarState();
    expect(state).toEqual({ phase: "ready", port: 45678 });
    // The token was captured (proven by callSidecar attaching it, not by any getter --
    // see test_the_token_is_never_returned_from_any_exported_function below).
    await expect(sidecar.callSidecar("/v1/hello")).rejects.toThrow(/fetch failed|ECONNREFUSED/);
  });

  it("test_a_malformed_ready_line_fails_with_a_named_error_not_a_hang", async () => {
    start({ FAKE_SIDECAR_MODE: "malformed" });
    await waitFor(() => sidecar.getSidecarState().phase === "error");
    const state = sidecar.getSidecarState();
    if (state.phase !== "error") throw new Error("expected error phase");
    expect(state.message).toMatch(/malformed ready line/);
  });

  it("test_a_spawn_error_is_actionable_and_cannot_be_replaced_by_a_late_exit", async () => {
    sidecar.startSidecar({ command: join(process.cwd(), "missing-sidecar-executable") });
    const child = lastSpawnedChild();
    await waitFor(() => sidecar.getSidecarState().phase === "error");
    const state = sidecar.getSidecarState();
    if (state.phase !== "error") throw new Error("expected error phase");
    expect(state.message).toMatch(/ENOENT/);
    expect(state.message).toMatch(/check the app installation and sidecar executable/);
    child.emit("exit", 127, null);
    expect(sidecar.getSidecarState()).toEqual(state);
  });

  it("test_startup_gives_up_after_ten_seconds_and_reports_the_starting_state_until_then", async () => {
    vi.useFakeTimers();
    start({ FAKE_SIDECAR_MODE: "silent" });
    expect(sidecar.getSidecarState()).toEqual({ phase: "starting" });
    await vi.advanceTimersByTimeAsync(9_999);
    expect(sidecar.getSidecarState()).toEqual({ phase: "starting" });
    await vi.advanceTimersByTimeAsync(1);
    const state = sidecar.getSidecarState();
    expect(state.phase).toBe("error");
    if (state.phase === "error") expect(state.message).toMatch(/did not send its ready line within 10000ms/);
  });

  // Needs the repo .venv (dev sidecar path); a fresh archive has none, so check:free skips it.
  it.skipIf(!HAS_VENV)("test_the_sidecar_is_spawned_by_an_absolute_path_never_a_path_search", () => {
    const { command } = sidecar.resolveSidecarCommand();
    expect(isAbsolute(command)).toBe(true);
    expect(command).not.toBe("lockwire");
    // Proves it's the real repo venv script, not just any absolute-looking string.
    const repoRoot = dirname(require.resolve("../../../pyproject.toml"));
    expect(existsSync(join(repoRoot, ".venv"))).toBe(true);
    expect(command.startsWith(join(repoRoot, ".venv"))).toBe(true);
  });

  // Needs the repo .venv (dev sidecar path); a fresh archive has none, so check:free skips it.
  it.skipIf(!HAS_VENV)("test_resolve_sidecar_command_switches_on_app_is_packaged_not_resources_path", () => {
    // Dev: app.isPackaged is false. Electron sets process.resourcesPath in dev too (it
    // points at Electron's own resources dir), so setting it here and still landing on
    // the .venv path proves the switch is isPackaged, not resourcesPath's truthiness.
    mockIsPackaged = false;
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = "/dev/electron/resources";
    const dev = sidecar.resolveSidecarCommand();
    const repoRoot = dirname(require.resolve("../../../pyproject.toml"));
    expect(dev.command.startsWith(join(repoRoot, ".venv"))).toBe(true);

    // Packaged: app.isPackaged is true -- resolves under resourcesPath instead.
    mockIsPackaged = true;
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = "/packaged/resources";
    const packaged = sidecar.resolveSidecarCommand();
    expect(packaged.command).toBe(
      join("/packaged/resources", `sidecar-${process.platform}-${process.arch}`, "lockwire-sidecar"),
    );
  });

  it("test_packaged_sidecar_ignores_the_manual_environment_override", () => {
    process.env.LOCKWIRE_SIDECAR_COMMAND = "/tmp/hostile-sidecar";
    process.env.LOCKWIRE_SIDECAR_ARGS = "hostile args";
    mockIsPackaged = true;
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = "/packaged/resources";
    try {
      const packaged = sidecar.resolveSidecarCommand();
      expect(packaged.command).toBe(
        join("/packaged/resources", `sidecar-${process.platform}-${process.arch}`, "lockwire-sidecar"),
      );
      expect(packaged.args).toEqual(["serve", "--exit-on-stdin-close"]);
    } finally {
      delete process.env.LOCKWIRE_SIDECAR_COMMAND;
      delete process.env.LOCKWIRE_SIDECAR_ARGS;
    }
  });

  it("test_exit_on_stdin_close_is_passed_on_every_spawn", async () => {
    // args deliberately omit the flag -- startSidecar must append it regardless.
    start({ FAKE_SIDECAR_MODE: "ready" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    expect(lastSpawnArgs()).toContain("--exit-on-stdin-close");
  });

  it("test_user_data_dir_runs_point_the_sidecar_log_dir_at_the_app_log_dir", () => {
    mockUserDataDirSwitch = true;
    start();
    const env = vi.mocked(spawn).mock.calls[0]?.[2]?.env;
    expect(env?.LOCKWIRE_LOG_DIR).toBe("/tmp/mock-userData/logs");
    sidecar.stopSidecar();
    vi.clearAllMocks();
    mockUserDataDirSwitch = false;
    start();
    expect(vi.mocked(spawn).mock.calls[0]?.[2]?.env?.LOCKWIRE_LOG_DIR).toBe(process.env.LOCKWIRE_LOG_DIR);
  });

  it("test_a_child_exit_emits_the_stopped_event_and_fails_every_in_flight_call", async () => {
    // A real server that never answers, so the in-flight fetch is genuinely pending
    // when the child exits -- not racing a same-tick ECONNREFUSED.
    const { createServer } = await import("node:http");
    const server = createServer(() => {});
    await new Promise<void>((resolve) => server.listen(0, resolve));
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("no port");
    try {
      start({ FAKE_SIDECAR_MODE: "ready", FAKE_SIDECAR_PORT: String(address.port) });
      await waitFor(() => sidecar.getSidecarState().phase === "ready");
      const inFlight = sidecar.callSidecar("/v1/slow");
      // Node flags a promise "unhandled" if nothing has subscribed by the time it
      // rejects; the real subscription (below) only happens after the awaits that
      // follow, so attach a no-op one now to close that window -- test-only, the
      // production caller awaits its call immediately with no such gap.
      inFlight.catch(() => {});
      lastSpawnedChild().kill();
      await waitFor(() => sidecar.getSidecarState().phase === "stopped");
      await expect(inFlight).rejects.toThrow(/sidecar stopped/);
    } finally {
      server.close();
    }
  });

  it("test_the_token_is_never_returned_from_any_exported_function", async () => {
    const secret = "super-secret-token-xyz";
    start({ FAKE_SIDECAR_MODE: "ready", FAKE_SIDECAR_TOKEN: secret });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    expect(JSON.stringify(sidecar.getSidecarState())).not.toContain(secret);
    // Every exported value that isn't a function is inert; every function's return
    // value (that we can call without side effects) must not surface the token either.
    expect(JSON.stringify(sidecar.resolveSidecarCommand())).not.toContain(secret);
  });

  it("test_quitting_kills_the_child_and_leaves_no_orphan", async () => {
    start({ FAKE_SIDECAR_MODE: "ready" });
    await waitFor(() => sidecar.getSidecarState().phase === "ready");
    const child = lastSpawnedChild();
    const pid = child.pid as number;
    expect(() => process.kill(pid, 0)).not.toThrow(); // alive

    sidecar.stopSidecar();
    await waitFor(() => sidecar.getSidecarState().phase === "stopped");
    await waitFor(() => {
      try {
        process.kill(pid, 0);
        return false;
      } catch {
        return true; // ESRCH: gone
      }
    });
    expect(() => process.kill(pid, 0)).toThrow();
  });
});
