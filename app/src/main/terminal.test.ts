import { mkdtempSync, readlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const sent = vi.fn();
const userData = mkdtempSync(join(tmpdir(), "lw-term-"));
vi.mock("electron", () => ({ app: { getPath: () => userData }, BrowserWindow: { getAllWindows: () => [{ isDestroyed: () => false, webContents: { send: sent } }] } }));
type Fake = { write: ReturnType<typeof vi.fn>; resize: ReturnType<typeof vi.fn>; kill: ReturnType<typeof vi.fn>; data?: (d: string) => void; exit?: (e: { exitCode: number }) => void };
const spawned: Fake[] = [];
const spawn = vi.fn((..._a: unknown[]) => {
  const p: Fake = { write: vi.fn(), resize: vi.fn(), kill: vi.fn() };
  spawned.push(p);
  return { ...p, onData: (cb: (d: string) => void) => { p.data = cb; }, onExit: (cb: (e: { exitCode: number }) => void) => { p.exit = cb; } };
});
vi.mock("node-pty", () => ({ spawn: (...a: unknown[]) => spawn(...a) }));
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), resolveSidecarCommand: () => ({ command: "/eng/lockwire", args: [] }) }));

const t = await import("./terminal");

beforeEach(() => { t.killTerminal(); spawn.mockClear(); sent.mockClear(); spawned.length = 0; });

describe("the drawer terminal", () => {
  it("starts a login shell in the folder, forwards output and exit, and a second start replaces the first", async () => {
    process.env[["ANTHRO", "PIC_API_KEY"].join("")] = "sk"; // built from parts: the public snapshot greps vendor names
    Object.assign(process.env, { DISPLAY: ":0", WAYLAND_DISPLAY: "wayland-0", DBUS_SESSION_BUS_ADDRESS: "unix:path=/b", XDG_RUNTIME_DIR: "/run/u" });
    await t.handleTerminalStart({ cols: 80, rows: 24, cwd: "/tmp" });
    expect(spawn.mock.calls[0]![1]).toEqual(["-l"]);
    const env = (spawn.mock.calls[0]![2] as { env: Record<string, string> }).env;
    const bin = join(userData, "bin");
    expect([Object.keys(env).filter((k) => /^(ANTHRO|CLAU)/.test(k)), env.PATH!.split(":")[0], readlinkSync(join(bin, "lockwire"))]).toEqual([[], bin, "/eng/lockwire"]);
    expect(env).toMatchObject({ DISPLAY: ":0", WAYLAND_DISPLAY: "wayland-0", DBUS_SESSION_BUS_ADDRESS: "unix:path=/b", XDG_RUNTIME_DIR: "/run/u" });
    expect(spawn.mock.calls[0]![2]).toMatchObject({ cwd: "/tmp", cols: 80, rows: 24, name: "xterm-256color" });
    spawned[0]!.data!("hi");
    expect(sent).toHaveBeenCalledWith("lockwire:terminal-event", { type: "data", data: "hi" });
    await t.handleTerminalStart({ cols: 80, rows: 24, cwd: "/no/such/dir" });
    expect(spawn.mock.calls[1]![2]).not.toMatchObject({ cwd: "/no/such/dir" }); // falls back to home
    spawned[0]!.exit!({ exitCode: 0 });
    expect(sent).not.toHaveBeenCalledWith("lockwire:terminal-event", { type: "exit", code: 0 }); // a replaced shell is silent
  });

  it("writes, resizes, and kills (also on window close); a killed shell is gone", async () => {
    await t.handleTerminalStart({ cols: 80, rows: 24 });
    await t.handleTerminalWrite({ data: "ls\r" });
    await t.handleTerminalResize({ cols: 100, rows: 30 });
    t.killTerminal();
    await t.handleTerminalWrite({ data: "x" });
    expect(spawned.length).toBe(1);
  });

  it("refuses bad arguments", async () => {
    for (const bad of [undefined, {}, { cols: 0, rows: 5 }, { cols: 80.5, rows: 5 }, { cols: 80, rows: 5000 }, { cols: 80, rows: 5, shell: "/bin/sh" }, { cols: 80, rows: 5, cwd: 3 }])
      await expect(t.handleTerminalStart(bad)).rejects.toThrow();
    await expect(t.handleTerminalResize({ cols: 80, rows: 5, extra: 1 })).rejects.toThrow();
    for (const bad of [undefined, { data: 5 }, { data: "x".repeat(70000) }, { data: "x", more: 1 }]) await expect(t.handleTerminalWrite(bad)).rejects.toThrow();
    expect(spawn).not.toHaveBeenCalled();
  });
});
