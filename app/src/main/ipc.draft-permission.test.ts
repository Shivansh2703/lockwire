import type { IpcMainInvokeEvent } from "electron";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Same mocking harness as ipc.test.ts: electron, dialogs and sidecar are stubbed so dispatch can
// run outside a real Electron process.
const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>();
let mockIsPackaged = false;

vi.mock("electron", () => ({
  app: {
    get isPackaged() {
      return mockIsPackaged;
    },
  },
  dialog: { showOpenDialog: vi.fn() },
  ipcMain: {
    handle: (channel: string, fn: (event: unknown, ...args: unknown[]) => unknown) => {
      handlers.set(channel, fn);
    },
  },
}));

vi.mock("./dialogs", () => ({ pickRobot: vi.fn() }));

const callSidecarMock = vi.fn();
vi.mock("./sidecar", () => ({
  callSidecar: (...args: unknown[]) => callSidecarMock(...args),
  getSidecarState: vi.fn(),
  startSidecar: vi.fn(),
  onSidecarState: vi.fn(),
}));

import { dispatch } from "./ipc";

const TRUSTED_FRAME = { url: "http://localhost:5173/" } as IpcMainInvokeEvent["senderFrame"];
const TRUSTED_EVENT = { senderFrame: TRUSTED_FRAME } as IpcMainInvokeEvent;
const HOSTILE_EVENT = { senderFrame: { url: "https://attacker.example/" } } as IpcMainInvokeEvent;

const VALID_WRITE_REQUEST = {
  command: "declare",
  args: { contract: "c", path: "p", value: null, source: "manual", witness: "Maya", expected_revision: "revision" },
};
const VALID_EDIT_REQUEST = { path: "a.json", contents: "{}" };
const VALID_ROBOT_REQUEST = { commandName: "__hostile_unknown_command__", params: { token: "guessed" } };

beforeEach(() => {
  mockIsPackaged = false;
  process.env.ELECTRON_RENDERER_URL = "http://localhost:5173/";
  handlers.clear();
  callSidecarMock.mockReset();
});

describe("the IPC permission gate on write-class commands", () => {
  it.each([
    ["contract:write", VALID_WRITE_REQUEST],
    ["contract:edit", VALID_EDIT_REQUEST],
    ["robot:command", VALID_ROBOT_REQUEST],
  ])("rejects %s from a hostile sender before the handler runs", async (commandClass, args) => {
    await expect(dispatch(HOSTILE_EVENT, commandClass, args)).rejects.toThrow(
      `permission denied: untrusted sender for "${commandClass}"`,
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("passes a trusted sender's contract:write through to the sidecar", async () => {
    const reply = {
      version: "0.1.0",
      cli_line: "lockwire write",
      exit_code: 0,
      verdict: null,
      payload: { format_version: "1", schema_version: "1", artifact_type: "lockwire-edit-result", contract_sha: null, written: true, diff: "", lines: [] },
      refusal: null,
    };
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(reply) });

    await expect(dispatch(TRUSTED_EVENT, "contract:write", VALID_WRITE_REQUEST)).resolves.toEqual(reply);
    expect(callSidecarMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["contract:edit", VALID_EDIT_REQUEST],
    ["robot:command", VALID_ROBOT_REQUEST],
  ])("passes a trusted sender past the gate for %s (handler is reached, not permission-denied)", async (commandClass, args) => {
    // Neither handler is implemented yet, so a trusted call still fails -- but it must fail with
    // "not supported" from the handler, never "permission denied" from the gate.
    await expect(dispatch(TRUSTED_EVENT, commandClass, args)).rejects.toThrow(`${commandClass}: not supported`);
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
});
