import { describe, expect, it, vi } from "vitest";

const created: Record<string, unknown>[] = [];

vi.mock("electron", () => ({
  app: {
    whenReady: () => Promise.resolve(),
    on: vi.fn(),
  },
  BrowserWindow: class {
    constructor(opts: Record<string, unknown>) {
      created.push(opts);
    }
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
  Menu: { setApplicationMenu: vi.fn() },
  protocol: { handle: vi.fn(), registerSchemesAsPrivileged: vi.fn() },
}));
vi.mock("./ipc", () => ({ registerIpc: vi.fn(), forwardSidecarState: vi.fn(() => vi.fn()) }));
vi.mock("./menu", () => ({ buildMenu: vi.fn() }));
vi.mock("./security", () => ({ applySecurity: vi.fn() }));
vi.mock("./sidecar", () => ({ startSidecar: vi.fn() }));

describe("the window's webPreferences trust boundary", () => {
  it("test_context_isolation_on_node_integration_off_sandbox_on_with_a_preload", async () => {
    await import("./index");
    // whenReady's .then callback runs on a microtask; flush it before asserting.
    await Promise.resolve();
    await Promise.resolve();
    const opts = created[0]!.webPreferences as Record<string, unknown>;
    expect(opts.contextIsolation).toBe(true);
    expect(opts.nodeIntegration).toBe(false);
    expect(opts.sandbox).toBe(true);
    expect(typeof opts.preload).toBe("string");
    expect(opts.preload).toMatch(/preload[/\\]index\.js$/);
  });
});
