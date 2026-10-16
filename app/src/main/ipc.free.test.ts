import type { IpcMainInvokeEvent } from "electron";
import { describe, expect, it, vi } from "vitest";

import { PERMITTED_COMMAND_CLASSES } from "../shared/ipc-contract";

vi.mock("electron", () => ({
  app: { isPackaged: false },
  dialog: { showOpenDialog: vi.fn() },
  ipcMain: { handle: vi.fn() },
}));
vi.mock("./dialogs", () => ({ pickRobot: vi.fn() }));
vi.mock("./sidecar", () => ({
  callSidecar: vi.fn(),
  getSidecarState: vi.fn(),
  startSidecar: vi.fn(),
  onSidecarState: vi.fn(),
}));
// The free app: no pro/ folder was found.
vi.mock("./proRegistry", () => ({ proMain: null }));

const { dispatch, isPermittedCommand } = await import("./ipc");

const TRUSTED_EVENT = { senderFrame: { url: "http://localhost:5173/" } } as IpcMainInvokeEvent;

// The whole free IPC surface, spelled out: a class added to the free list must be added here too.
const FREE_CLASSES = [
  "contract:declare-form",
  "contract:diff",
  "contract:edit",
  "contract:history",
  "contract:page",
  "contract:plan",
  "contract:probe",
  "contract:undo",
  "contract:what-if",
  "contract:write",
  "file:choose-plan",
  "file:new-robot",
  "file:open",
  "file:open-dropped",
  "file:start-urdf",
  "file:starters",
  "robot:command",
  "sidecar:restart",
  "sidecar:state",
];

describe("the free IPC surface (no pro/ folder)", () => {
  it("permits exactly the planning classes and nothing else", async () => {
    process.env.ELECTRON_RENDERER_URL = "http://localhost:5173/";
    expect([...PERMITTED_COMMAND_CLASSES].sort()).toEqual(FREE_CLASSES);
    for (const commandClass of FREE_CLASSES) expect(isPermittedCommand(commandClass)).toBe(true);
    // Anything outside the free list is refused (the paid classes are named in pro/ipc.pro.test.ts).
    for (const name of ["shell:exec", "constructor", "contract:unknown", "file:unknown"]) {
      expect(isPermittedCommand(name)).toBe(false);
      await expect(dispatch(TRUSTED_EVENT, name, {})).rejects.toThrow(`permission denied: unknown command "${name}"`);
    }
  });
});
