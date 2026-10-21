import { describe, expect, it, vi } from "vitest";

const onHandlers: Record<string, Array<() => void>> = {};
const stopSidecar = vi.fn();

vi.mock("electron", () => ({
  app: {
    whenReady: () => Promise.resolve(),
    on: vi.fn((event: string, cb: () => void) => {
      (onHandlers[event] ??= []).push(cb);
    }),
    quit: vi.fn(),
  },
  BrowserWindow: class {
    loadFile() {
      return Promise.resolve();
    }
    loadURL() {
      return Promise.resolve();
    }
    once() {}
    static getAllWindows() {
      return [];
    }
  },
  protocol: { handle: vi.fn(), registerSchemesAsPrivileged: vi.fn() },
}));
vi.mock("./ipc", () => ({ forwardSidecarState: vi.fn(() => vi.fn()), registerIpc: vi.fn() }));
vi.mock("./security", () => ({ applySecurity: vi.fn() }));
const writeLogLine = vi.fn();
vi.mock("./log", () => ({ writeLogLine }));
vi.mock("./sidecar", () => ({ startSidecar: vi.fn(), stopSidecar }));

describe("the app quit path", () => {
  it("test_before_quit_stops_the_sidecar", async () => {
    await import("./index");
    // whenReady's .then callback runs on a microtask; flush it so app.on("before-quit", ...) is registered.
    await Promise.resolve();
    await Promise.resolve();

    expect(stopSidecar).not.toHaveBeenCalled();
    for (const cb of onHandlers["before-quit"] ?? []) cb();
    expect(stopSidecar).toHaveBeenCalledTimes(1);
  });

  it("test_L72_startup_creates_the_log_and_an_unhandled_rejection_is_logged", async () => {
    const before = process.listeners("unhandledRejection");
    vi.resetModules();
    await import("./index");
    await Promise.resolve();
    await Promise.resolve();
    expect(writeLogLine).toHaveBeenCalled(); // the file Reveal Logs opens now exists
    const added = process.listeners("unhandledRejection").filter((l) => !before.includes(l));
    process.off("unhandledRejection", added[0]!);
    (added[0] as (r: unknown) => void)(new Error("boom"));
    expect(writeLogLine).toHaveBeenLastCalledWith(expect.stringContaining("boom"));
  });
});
