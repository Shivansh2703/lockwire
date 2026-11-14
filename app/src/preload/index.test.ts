import { beforeEach, describe, expect, it, vi } from "vitest";

const exposed: Record<string, unknown> = {};
const invokeCalls: unknown[][] = [];
let invokeFail: string | null = null;

// The free app: no preload/pro/ folder was found.
vi.mock("./proRegistry", () => ({ proApi: null }));

vi.mock("electron", () => ({
  contextBridge: {
    exposeInMainWorld: (name: string, api: Record<string, unknown>) => {
      exposed[name] = api;
    },
  },
  webUtils: { getPathForFile: () => "/x" },
  ipcRenderer: {
    on: () => undefined,
    removeListener: () => undefined,
    invoke: (...args: unknown[]) => {
      invokeCalls.push(args);
      if (invokeFail) return Promise.reject(new Error(invokeFail));
      return Promise.resolve({ accepted: true });
    },
  },
}));

beforeEach(() => {
  invokeCalls.length = 0;
  invokeFail = null;
});

it("shows a failed command as a plain sentence", async () => {
  await import("./index");
  invokeFail = "Error invoking remote method 'lockwire:run': Error: contract:page: invalid page payload";
  await expect((exposed.lockwire as { runProbe: (a: object) => Promise<unknown> }).runProbe({})).rejects.toThrow(/^The app and its engine disagree/);
  invokeFail = null;
});

describe("preload exposes a small, explicitly enumerated API", () => {
  it("test_exposed_surface_is_exactly_the_forty_four_named_methods", async () => {
    await import("./index");
    expect(Object.keys(exposed.lockwire as object).sort()).toEqual([
      "applyAssistant",
      "cancelAssistant",
      "checkPlan",
      "choosePackageFolder",
      "choosePlan",
      "compareContractWhatIf",
      "designPower",
      "diffContract",
      "editContract",
      "getAssistantSettings",
      "getContractPage",
      "getDeclareForm",
      "getHistory",
      "getSidecarState",
      "getStarters",
      "killTerminal",
      "newRobot",
      "onAgentEvent",
      "onAssistantEvent",
      "onCloseRobot",
      "onOpenedFromMenu",
      "onShowShortcuts",
      "onSidecarState",
      "onTerminalEvent",
      "onToggleTheme",
      "openDroppedPath",
      "openLockfile",
      "pathForFile",
      "printPart",
      "resizeTerminal",
      "restartSidecar",
      "runProbe",
      "savePicture",
      "sendAssistant",
      "sendRobotCommand",
      "setAssistantSettings",
      "startAgent",
      "startFromUrdf",
      "startTerminal",
      "stopAgent",
      "swapPreview",
      "undoContract",
      "writeContract",
      "writeTerminal",
    ]);
  });

  it("each method forwards to the single RUN_CHANNEL with its own hardcoded command class", async () => {
    const bridge = exposed.lockwire as {
      editContract: (a: unknown) => Promise<unknown>;
      runProbe: (a: unknown) => Promise<unknown>;
      sendRobotCommand: (a: unknown) => Promise<unknown>;
      openLockfile: (a: unknown) => Promise<unknown>;
      writeContract: (a: unknown) => Promise<unknown>;
      undoContract: (a: unknown) => Promise<unknown>;
      getContractPage: (a: unknown) => Promise<unknown>;
      getDeclareForm: (a: unknown) => Promise<unknown>;
      compareContractWhatIf: (a: unknown) => Promise<unknown>;
      checkPlan: (a: unknown) => Promise<unknown>;
      diffContract: (a: unknown) => Promise<unknown>;
      getHistory: (a: unknown) => Promise<unknown>;
      choosePlan: () => Promise<unknown>;
      startFromUrdf: (a?: unknown) => Promise<unknown>;
      designPower: (a: string, b: string, c: string) => Promise<unknown>;
    };
    await bridge.editContract({ path: "a.json", contents: "{}" });
    await bridge.runProbe({ probe: "battery" });
    await bridge.sendRobotCommand({ commandName: "stop" });
    await bridge.openLockfile({ contract: "c" });
    await bridge.writeContract({ command: "declare", args: { contract: "c" } });
    await bridge.undoContract({ contract: "c", apply: false });
    await bridge.getContractPage({ contract: "c" });
    await bridge.getDeclareForm({ contract: "c" });
    await bridge.compareContractWhatIf({ contract: "c", expected_revision: "a".repeat(64), change: { path: "p", value: "1 W", source: "measure", witness: "Maya" } });
    await bridge.checkPlan({ contract: "c", plan: "p" });
    await bridge.diffContract({ contract: "c", a: "HEAD~", b: "HEAD" });
    await bridge.getHistory({ contract: "c" });
    await bridge.choosePlan();
    await bridge.startFromUrdf();
    await bridge.designPower("c", "4S, 5 V 3 A", "r");

    expect(invokeCalls).toEqual([
      ["lockwire:run", "contract:edit", { path: "a.json", contents: "{}" }],
      ["lockwire:run", "contract:probe", { probe: "battery" }],
      ["lockwire:run", "robot:command", { commandName: "stop" }],
      ["lockwire:run", "file:open", { contract: "c" }],
      ["lockwire:run", "contract:write", { command: "declare", args: { contract: "c" } }],
      ["lockwire:run", "contract:undo", { contract: "c", apply: false }],
      ["lockwire:run", "contract:page", { contract: "c" }],
      ["lockwire:run", "contract:declare-form", { contract: "c" }],
      ["lockwire:run", "contract:what-if", { contract: "c", expected_revision: "a".repeat(64), change: { path: "p", value: "1 W", source: "measure", witness: "Maya" } }],
      ["lockwire:run", "contract:plan", { contract: "c", plan: "p" }],
      ["lockwire:run", "contract:diff", { contract: "c", a: "HEAD~", b: "HEAD" }],
      ["lockwire:run", "contract:history", { contract: "c" }],
      ["lockwire:run", "file:choose-plan", undefined],
      ["lockwire:run", "file:start-urdf", undefined],
      ["lockwire:run", "agent:power-design", { contract: "c", spec: "4S, 5 V 3 A", revision: "r" }],
    ]);
  });

  it("never exposes ipcRenderer.invoke itself, or any generic invoke(channel, ...args)", () => {
    const bridge = exposed.lockwire as Record<string, unknown>;
    expect(bridge.invoke).toBeUndefined();
    expect(bridge.ipcRenderer).toBeUndefined();
  });
});
