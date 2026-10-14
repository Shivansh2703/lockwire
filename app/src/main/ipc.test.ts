import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { IpcMainInvokeEvent } from "electron";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckPayload, PagePayload, WhatIfPayload } from "../shared/api.gen";

const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>();
let mockIsPackaged = false;

const { showOpenDialog } = vi.hoisted(() => ({ showOpenDialog: vi.fn() }));
vi.mock("electron", () => ({
  app: {
    get isPackaged() {
      return mockIsPackaged;
    },
  },
  dialog: { showOpenDialog },
  ipcMain: {
    handle: (channel: string, fn: (event: unknown, ...args: unknown[]) => unknown) => {
      handlers.set(channel, fn);
    },
  },
}));

// This file proves the free edition: it never loads the paid registry.
vi.mock("./proRegistry", () => ({ proMain: null }));

const OPEN_ARGS = { contract: "/tmp/robot/lockwire.yaml" };
const pickRobotMock = vi.fn();
vi.mock("./dialogs", () => ({ pickRobot: () => pickRobotMock() }));

const callSidecarMock = vi.fn();
const getSidecarStateMock = vi.fn();
const startSidecarMock = vi.fn();
vi.mock("./sidecar", () => ({
  callSidecar: (...args: unknown[]) => callSidecarMock(...args),
  getSidecarState: (...args: unknown[]) => getSidecarStateMock(...args),
  startSidecar: (...args: unknown[]) => startSidecarMock(...args),
  onSidecarState: vi.fn(),
}));

import {
  __resetSidecarRestartRateLimitForTests,
  dispatch,
  isPermittedCommand,
  isTrustedSender,
  registerIpc,
  RUN_CHANNEL,
} from "./ipc";

const TRUSTED_FRAME = { url: "http://localhost:5173/" } as IpcMainInvokeEvent["senderFrame"];
const TRUSTED_EVENT = { senderFrame: TRUSTED_FRAME } as IpcMainInvokeEvent;

// A complete `api.page` document shape, including every generated PagePayload
// requirement. Empty collections are valid output for an otherwise empty contract.
const PAGE_PAYLOAD: PagePayload = {
  robot: "Test Robot",
  contract_id: "test-robot",
  state: "go",
  headline: "Ready",
  qualifier: "Everything adds up",
  scored: 0,
  violations: 0,
  warnings: 0,
  unchecked: 0,
  adds_up: [],
  cards: [],
  joints: [],
  links: [],
  graph: {
    off: { nodes: [], edges: [], label: "Off", caption: "" },
    mechanical: { nodes: [], edges: [], label: "Mechanical", caption: "" },
    power: { nodes: [], edges: [], label: "Power", caption: "" },
    data: { nodes: [], edges: [], label: "Data", caption: "" },
    health: { nodes: [], edges: [], label: "Health", caption: "" },
  },
  budgets: [],
  constraints: [],
  setup: [],
  scene: null,
  modes: [],
  mode_labels: {},
  command_modes: {},
  faces: {
    design: { title: "Design", parts: [], cli: "lockwire check contract.yaml" },
    declare: { title: "Declare", lines: [] },
    plan: { title: "Plan", cli: "lockwire plan contract.yaml plan.yaml", outcomes: null },
    reference: { title: "Reference", devices: [], drawers: [], power_tree: null },
  },
  cli_line: null,
  document_identity: "contract.yaml \u00b7 generated for test",
};

const EDIT_PAYLOAD = {
  format_version: "1",
  schema_version: "1",
  artifact_type: "lockwire-edit-result" as const,
  contract_sha: null,
  written: true,
  diff: "",
  lines: [],
};

const EMPTY_FORCES: NonNullable<CheckPayload["forces"]> = {
  gravity: { value: null, unit: "m/s^2", kind: "derived" },
  mass_total: { value: null, unit: "kg", kind: "derived" },
  cg: { value: null, unit: "m", kind: "derived" },
  masses: [],
  mounts: [],
  mount_shapes: [],
  not_scored: [],
};

function runReply(payload: unknown, cli_line = "lockwire test") {
  const document = payload as Partial<Pick<CheckPayload, "verdict" | "exit_code">> | null;
  const verdict = document?.verdict ?? null;
  return {
    version: "0.1.0",
    cli_line,
    exit_code: verdict !== null && typeof document?.exit_code === "number" ? document.exit_code : 0,
    verdict,
    payload,
    refusal: null,
  };
}

beforeEach(() => {
  mockIsPackaged = false;
  process.env.ELECTRON_RENDERER_URL = "http://localhost:5173/";
  handlers.clear();
  pickRobotMock.mockReset();
  callSidecarMock.mockReset();
  getSidecarStateMock.mockReset();
  startSidecarMock.mockReset();
  __resetSidecarRestartRateLimitForTests();
});

describe("dispatch: sender validation", () => {
  it("test_untrusted_sender_is_refused_before_the_command_is_looked_at", async () => {
    const hostile = { senderFrame: { url: "https://attacker.example/" } } as IpcMainInvokeEvent;
    await expect(dispatch(hostile, "contract:probe", { probe: "x" })).rejects.toThrow(
      'permission denied: untrusted sender for "contract:probe"',
    );
  });

  it("isTrustedSender is false with no senderFrame (e.g. a destroyed frame)", () => {
    expect(isTrustedSender({ senderFrame: null })).toBe(false);
  });

  it("refuses a host that merely prefixes the dev-server URL", () => {
    process.env.ELECTRON_RENDERER_URL = "http://localhost:5173";
    const prefixHijack = { senderFrame: { url: "http://localhost:5173.attacker.example/" } } as IpcMainInvokeEvent;
    expect(isTrustedSender(prefixHijack)).toBe(false);
  });

  it("trusts a dev-server URL with a path component", () => {
    process.env.ELECTRON_RENDERER_URL = "http://localhost:5173";
    const devServerWithPath = { senderFrame: { url: "http://localhost:5173/index.html" } } as IpcMainInvokeEvent;
    expect(isTrustedSender(devServerWithPath)).toBe(true);
  });

  it("trusts only the packaged renderer's custom scheme and host", () => {
    delete process.env.ELECTRON_RENDERER_URL;
    const packaged = { senderFrame: { url: "lockwire://app/index.html" } } as IpcMainInvokeEvent;
    const wrongScheme = { senderFrame: { url: "hostile://app/index.html" } } as IpcMainInvokeEvent;
    const wrongHost = { senderFrame: { url: "lockwire://attacker/index.html" } } as IpcMainInvokeEvent;
    expect(isTrustedSender(packaged)).toBe(true);
    expect(isTrustedSender(wrongScheme)).toBe(false);
    expect(isTrustedSender(wrongHost)).toBe(false);
  });

  it("ignores an environment-selected renderer in a packaged app", () => {
    mockIsPackaged = true;
    process.env.ELECTRON_RENDERER_URL = "https://attacker.example/";
    expect(
      isTrustedSender({ senderFrame: { url: "https://attacker.example/" } as IpcMainInvokeEvent["senderFrame"] }),
    ).toBe(false);
    expect(
      isTrustedSender({ senderFrame: { url: "lockwire://app/index.html" } as IpcMainInvokeEvent["senderFrame"] }),
    ).toBe(true);
  });
});

describe("dispatch: the permission contract's negative case", () => {
  it("test_unauthorized_command_class_is_refused_not_executed", async () => {
    // "constructor" is not one of the three permitted classes, but it IS an inherited property
    // of the plain object HANDLERS is built from -- the exact case a bare truthy lookup would
    // get wrong. isPermittedCommand must say no, and dispatch must refuse before calling anything.
    expect(isPermittedCommand("constructor")).toBe(false);
    await expect(dispatch(TRUSTED_EVENT, "constructor", {})).rejects.toThrow(
      'permission denied: unknown command "constructor"',
    );
  });

  it("permits exactly the command classes named in the contract", () => {
    expect(isPermittedCommand("contract:edit")).toBe(true);
    expect(isPermittedCommand("contract:write")).toBe(true);
    expect(isPermittedCommand("contract:undo")).toBe(true);
    expect(isPermittedCommand("contract:probe")).toBe(true);
    expect(isPermittedCommand("contract:page")).toBe(true);
    expect(isPermittedCommand("contract:declare-form")).toBe(true);
    expect(isPermittedCommand("contract:what-if")).toBe(true);
    expect(isPermittedCommand("contract:plan")).toBe(true);
    expect(isPermittedCommand("contract:diff")).toBe(true);
    expect(isPermittedCommand("contract:history")).toBe(true);
    expect(isPermittedCommand("robot:command")).toBe(true);
    expect(isPermittedCommand("file:open")).toBe(true);
    expect(isPermittedCommand("file:choose-plan")).toBe(true);
    expect(isPermittedCommand("sidecar:state")).toBe(true);
    expect(isPermittedCommand("sidecar:restart")).toBe(true);
    expect(isPermittedCommand("shell:exec")).toBe(false);
    // the paid classes are asserted exactly in pro/ipc.pro.test.ts; the free-only surface in ipc.free.test.ts
  });
});

describe("dispatch: contract writes and undo", () => {
  it.each([
    [
      "declare",
      { command: "declare", args: { contract: "c", path: "p", value: null, source: "manual", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "add",
      { command: "add", args: { contract: "c", kind: "bus", id: "bus.new", fields: {}, source: "manual", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "review",
      { command: "review", args: { contract: "c", path: "p", state: "questioned", witness: "Maya", note: "why", expected_revision: "revision" } },
    ],
    [
      "commit",
      { command: "commit", args: { contract: "c", message: "witness: Maya", paths: ["c"], expected_revision: "revision", dry_run: true } },
    ],
    [
      "place",
      { command: "place", args: { contract: "c", target: "camera", on: "frame", offset: [1, 2, 3], unit: "mm", source: "cad", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "move",
      { command: "move", args: { contract: "c", target: "camera", offset: [1, 2, 3], unit: "mm", source: "cad", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "rotate",
      { command: "rotate", args: { contract: "c", target: "frame", rpy: [0, 0, 90], unit: "deg", source: "cad", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "reparent",
      { command: "reparent", args: { contract: "c", target: "camera", to: "frame", source: "cad", witness: "Maya", expected_revision: "revision" } },
    ],
    [
      "mount-shape",
      { command: "mount-shape", args: { contract: "c", target: "camera", shape: { kind: "plate", dimensions_mm: [100, 50, 3], fastener_pattern: "4x M3" }, source: "cad", witness: "Maya", expected_revision: "revision" } },
    ],
  ])("accepts and forwards valid %s arguments", async (_command, request) => {
    const reply = runReply(
      request.command === "commit"
        ? { argv: [], committed: false, sha: null, untouched: [], reason: "not a repository" }
        : EDIT_PAYLOAD,
      "lockwire write",
    );
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(reply) });

    await expect(dispatch(TRUSTED_EVENT, "contract:write", request)).resolves.toEqual(reply);
    expect(callSidecarMock).toHaveBeenCalledTimes(1);
  });

  it("forwards an allowed write request unchanged to the sidecar", async () => {
    const request = {
      command: "review",
      args: { contract: "/tmp/lockwire.yaml", path: "devices/d/power/draw", state: "approved", witness: "Maya", expected_revision: "revision" },
    };
    const reply = runReply(EDIT_PAYLOAD, "lockwire review");
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(reply) });

    await expect(dispatch(TRUSTED_EVENT, "contract:write", request)).resolves.toEqual(reply);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
  });

  it("refuses an otherwise valid contract write without its loaded revision", async () => {
    await expect(
      dispatch(TRUSTED_EVENT, "contract:write", {
        command: "declare",
        args: { contract: "c", path: "p", value: 1, source: "manual", witness: "Maya" },
      }),
    ).rejects.toThrow("contract:write: invalid arguments");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("refuses a sidecar command outside the write union", async () => {
    await expect(
      dispatch(TRUSTED_EVENT, "contract:write", { command: "check", args: { contract: "x" } }),
    ).rejects.toThrow("contract:write: invalid arguments");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it.each([
    ["declare missing required fields", { command: "declare", args: {} }],
    [
      "declare missing its value",
      { command: "declare", args: { contract: "c", path: "p", source: "manual", witness: "Maya" } },
    ],
    [
      "add with non-object fields",
      { command: "add", args: { contract: "c", kind: "bus", fields: [], source: "manual", witness: "Maya" } },
    ],
    [
      "add with a non-record object",
      { command: "add", args: { contract: "c", kind: "bus", fields: new Date(0), source: "manual", witness: "Maya" } },
    ],
    [
      "review with an unknown state",
      { command: "review", args: { contract: "c", path: "p", state: "ignored", witness: "Maya" } },
    ],
    [
      "review with an undeclared source field",
      { command: "review", args: { contract: "c", path: "p", state: "approved", witness: "Maya", source: "x" } },
    ],
    ["commit missing its raw revision", { command: "commit", args: { contract: "c", message: "witness: Maya", paths: ["c"] } }],
    ["commit with non-array paths", { command: "commit", args: { contract: "c", message: "witness: Maya", paths: "c", expected_revision: "revision" } }],
    [
      "move with a non-triple offset",
      { command: "move", args: { contract: "c", target: "camera", offset: [1, 2], unit: "mm", source: "cad", witness: "Maya" } },
    ],
    [
      "mount shape with an extra field",
      { command: "mount-shape", args: { contract: "c", target: "camera", shape: { kind: "plate", dimensions_mm: [1, 2, 3], color: "red" }, source: "cad", witness: "Maya" } },
    ],
    [
      "write request with an extra top-level field",
      { command: "declare", args: { contract: "c", path: "p", value: 1, source: "manual", witness: "Maya" }, extra: true },
    ],
  ])("refuses malformed %s before calling the sidecar", async (_case, request) => {
    await expect(dispatch(TRUSTED_EVENT, "contract:write", request)).rejects.toThrow(
      "contract:write: invalid arguments",
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("wraps typed undo arguments in the undo sidecar command", async () => {
    const args = { contract: "/tmp/lockwire.yaml", source: "HEAD~1", apply: true, expected_revision: "revision" };
    const reply = runReply(
      { kind: "restore", argv: ["git", "restore"], scope: ["lockwire.yaml"], repo: "/tmp", sentence: "restore" },
      "lockwire undo",
    );
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(reply) });

    await expect(dispatch(TRUSTED_EVENT, "contract:undo", args)).resolves.toEqual(reply);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "undo", args }),
    });
  });

  it.each([
    ["missing contract", {}],
    ["missing expected revision", { contract: "c", apply: true }],
    ["non-string source", { contract: "c", source: 1 }],
    ["non-boolean apply", { contract: "c", apply: "yes" }],
    ["extra field", { contract: "c", apply: false, token: "must-not-cross" }],
  ])("refuses malformed undo: %s", async (_case, args) => {
    await expect(dispatch(TRUSTED_EVENT, "contract:undo", args)).rejects.toThrow(
      "contract:undo: invalid arguments",
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
});

describe("dispatch: contract:diff", () => {
  const diffPayload = {
    changed: true,
    changes: [],
    added: [{ kind: "device", id: "camera" }],
    removed: [],
    findings_new: [],
    findings_gone: [],
  };

  it("forwards the existing api.diff arguments and validates its reply", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(diffPayload, "lockwire diff HEAD~ HEAD")) });
    const args = { contract: "c", a: "HEAD~", b: "HEAD" };
    await expect(dispatch(TRUSTED_EVENT, "contract:diff", args)).resolves.toEqual(diffPayload);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "diff", args }),
    });
  });

  it("refuses malformed requests and incomplete replies", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:diff", { contract: "c", token: "x" })).rejects.toThrow(
      "contract:diff: invalid arguments",
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ changed: true })) });
    await expect(dispatch(TRUSTED_EVENT, "contract:diff", { contract: "c" })).rejects.toThrow(
      "contract:diff: invalid payload",
    );
  });
});

describe("dispatch: contract history", () => {
  const entries = [{
    sha: "abc123",
    author: "Maya",
    date: "2026-09-09",
    message: "lock the mount",
    verdict: "GO",
    fact_diff: null,
  }];

  it("forwards log and validates the generated LogPayload", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(entries, "lockwire log c --json")) });
    await expect(dispatch(TRUSTED_EVENT, "contract:history", { contract: "c" })).resolves.toEqual(entries);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: "log", args: { contract: "c" } }),
    }));
  });

  it("refuses malformed requests and unvalidated rows", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:history", { contract: "c", path: "invented" })).rejects.toThrow("invalid arguments");
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply([{ sha: "missing-fields" }])) });
    await expect(dispatch(TRUSTED_EVENT, "contract:history", { contract: "c" })).rejects.toThrow("invalid payload");
  });
});

describe("dispatch: contract:page", () => {
  it("forwards only the existing page command and returns its complete api.page payload", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply(PAGE_PAYLOAD, "lockwire gui c --out lockwire-gui.html")),
    });

    await expect(dispatch(TRUSTED_EVENT, "contract:page", { contract: "c" })).resolves.toEqual(PAGE_PAYLOAD);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "page", args: { contract: "c" } }),
    });
  });

  it("refuses an incomplete sidecar page reply before the renderer", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply({ robot: "Test Robot", faces: {} }, "lockwire gui c --out lockwire-gui.html")),
    });

    await expect(dispatch(TRUSTED_EVENT, "contract:page", { contract: "c" })).rejects.toThrow(
      "contract:page: invalid page payload",
    );
  });

  it("refuses malformed requests before the sidecar", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:page", { contract: "c", extra: true })).rejects.toThrow(
      "contract:page: invalid arguments",
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
});

describe("dispatch: contract:declare-form", () => {
  it("returns only a generated-schema-valid declare form", async () => {
    const payload = { rows: [], witness: { prefill: "Maya" } };
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(payload, "lockwire declare-form c")) });
    await expect(dispatch(TRUSTED_EVENT, "contract:declare-form", { contract: "c" })).resolves.toEqual(payload);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: "declare-form", args: { contract: "c" } }),
    }));
  });

  it("refuses an invalid declare form before the renderer", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ rows: [] })) });
    await expect(dispatch(TRUSTED_EVENT, "contract:declare-form", { contract: "c" })).rejects.toThrow(
      "contract:declare-form: invalid payload",
    );
  });

  it("refuses an invalid run envelope before the renderer", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve({ payload: { rows: [], witness: { prefill: "Maya" } } }) });
    await expect(dispatch(TRUSTED_EVENT, "contract:declare-form", { contract: "c" })).rejects.toThrow(
      "sidecar: invalid run reply",
    );
  });
});

describe("dispatch: contract:what-if", () => {
  const request = {
    contract: "/tmp/robot/lockwire.yaml",
    expected_revision: "a".repeat(64),
    change: { path: "topology/rails/rail.5v/capacity", value: "1 W", source: "bench", witness: "Maya" },
  };
  const side = {
    format_version: "1",
    schema_version: "1",
    robot: "Test Robot",
    verdict: "GO" as const,
    exit_code: 0,
    counts: { violation: 0, warning: 0, not_scored: 0 },
    scored: 0,
    findings: [],
    artifact_type: "lockwire-record-comparison" as const,
    facts: [],
    comparisons: [],
  };
  const payload: WhatIfPayload = {
    format_version: "1",
    schema_version: "1",
    artifact_type: "lockwire-what-if-report",
    current: side,
    proposed: side,
    diff: "",
  };

  it("routes one strict, read-only request to the existing what-if API payload", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(payload, "lockwire what-if c")) });
    await expect(dispatch(TRUSTED_EVENT, "contract:what-if", request)).resolves.toEqual(payload);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: "what-if", args: request }),
    }));
  });

  it("denies malformed paths and unknown write-shaped fields before reaching the sidecar", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:what-if", { ...request, change: { ...request.change, apply: true } })).rejects.toThrow(
      "contract:what-if: invalid arguments",
    );
    await expect(dispatch(TRUSTED_EVENT, "contract:what-if", { ...request, change: { ...request.change, path: 3 } })).rejects.toThrow(
      "contract:what-if: invalid arguments",
    );
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("surfaces the sidecar stale refusal without a write fallback", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve({ version: "0.1.0", cli_line: "lockwire what-if c", exit_code: 2, verdict: null, payload: null, refusal: "contract changed since it was opened; reload before comparing" }),
    });
    await expect(dispatch(TRUSTED_EVENT, "contract:what-if", request)).rejects.toThrow(
      "contract changed since it was opened; reload before comparing",
    );
    expect(callSidecarMock).toHaveBeenCalledOnce();
  });
});

describe("dispatch: plan", () => {
  it("validates and returns the served plan payload", async () => {
    const payload = {
      format_version: "2", schema_version: "1", robot: "X500", verdict: "GO", exit_code: 0,
      counts: { violation: 0, warning: 0, not_scored: 0 }, scored: 1, findings: [],
      artifact_type: "lockwire-plan-report", steps: [],
    };
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(payload, "lockwire plan c p")) });
    await expect(dispatch(TRUSTED_EVENT, "contract:plan", { contract: "c", plan: "p" })).resolves.toEqual(payload);
  });

  it.each([
    ["contract:plan", { contract: "c", plan: "p" }],
  ])("refuses an invalid %s payload before the renderer", async (command, request) => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ robot: "X500" })) });
    await expect(dispatch(TRUSTED_EVENT, command, request)).rejects.toThrow("invalid payload");
  });
});

// e9-join: the renderer is sandboxed and can no longer import "../main/sidecar" directly (that
// import is what broke `npm run build`) -- these two command classes are its only legal reach
// into the sidecar's state, routed through the same dispatch gate as every other command.
describe("dispatch: sidecar:state and sidecar:restart", () => {
  it("sidecar:state returns whatever getSidecarState() reports, untouched", async () => {
    getSidecarStateMock.mockReturnValue({ phase: "ready", port: 4321 });
    await expect(dispatch(TRUSTED_EVENT, "sidecar:state", undefined)).resolves.toEqual({
      phase: "ready",
      port: 4321,
    });
  });

  it("sidecar:restart calls startSidecar() with no overrides and reports accepted", async () => {
    await expect(dispatch(TRUSTED_EVENT, "sidecar:restart", undefined)).resolves.toEqual({ accepted: true });
    expect(startSidecarMock).toHaveBeenCalledWith();
  });

  it("an untrusted sender is refused before either sidecar handler runs", async () => {
    const hostile = { senderFrame: { url: "https://attacker.example/" } } as IpcMainInvokeEvent;
    await expect(dispatch(hostile, "sidecar:state", undefined)).rejects.toThrow("permission denied");
    expect(getSidecarStateMock).not.toHaveBeenCalled();
  });
});

// e9-rate: e9-join's own reviewer flagged this -- sidecar:restart is a state-changing class
// reachable from the renderer with no guard, so a buggy component (a restart call in a render
// loop) or a hostile one can thrash the sidecar process indefinitely. The guard lives in the
// main process, at the handler, because the renderer is the untrusted side.
describe("dispatch: sidecar:restart is rate-limited in the main process", () => {
  it("test_a_tight_loop_of_restart_calls_restarts_the_sidecar_at_most_once_in_the_window_and_the_rest_come_back_refused", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(0);
      const results: Array<"ok" | string> = [];
      for (let i = 0; i < 5; i++) {
        try {
          await dispatch(TRUSTED_EVENT, "sidecar:restart", undefined);
          results.push("ok");
        } catch (err) {
          results.push((err as Error).message);
        }
      }

      // Exactly one restart happened -- the tight loop did not spawn-storm the sidecar.
      expect(startSidecarMock).toHaveBeenCalledTimes(1);
      // The first call succeeded; every excess call was refused, honestly and visibly (a
      // rejected promise carrying "rate limited"), never silently swallowed as a success.
      expect(results[0]).toBe("ok");
      expect(results.slice(1)).toEqual(results.slice(1).map(() => expect.stringMatching(/rate limited/)));

      // Advancing past the window lets a real restart through again.
      vi.setSystemTime(2_001);
      await expect(dispatch(TRUSTED_EVENT, "sidecar:restart", undefined)).resolves.toEqual({ accepted: true });
      expect(startSidecarMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("dispatch: file:open asks the sidecar for a real check, never a fixture", () => {
  it("routes the native URDF selection through start, cancels without sidecar work, and refuses unobserved overwrite", async () => {
    pickRobotMock.mockResolvedValueOnce(null).mockResolvedValueOnce("/tmp/robot.urdf");
    expect(await dispatch(TRUSTED_EVENT, "file:start-urdf", undefined)).toEqual({ started: false });
    expect(callSidecarMock).not.toHaveBeenCalled();
    callSidecarMock.mockResolvedValueOnce({ json: async () => runReply({ path: "/tmp/lockwire.yaml", written: false, revision: "a".repeat(64) }) });
    callSidecarMock.mockResolvedValueOnce({ json: async () => runReply(checkPayload("GO", 0)) });
    expect(await dispatch(TRUSTED_EVENT, "file:start-urdf", undefined)).toMatchObject({ started: true, start: { written: false }, opened: { opened: true } });
    expect(callSidecarMock.mock.calls[0]?.[1].body).toBe(JSON.stringify({ command: "start", args: { urdf: "/tmp/robot.urdf" } }));
    pickRobotMock.mockResolvedValueOnce("/tmp/robot/lockwire.yaml");
    callSidecarMock.mockResolvedValueOnce({ json: async () => runReply(checkPayload("GO", 0)) });
    expect(await dispatch(TRUSTED_EVENT, "file:start-urdf", undefined)).toMatchObject({ started: false, opened: { opened: true, path: "/tmp/robot/lockwire.yaml" } });
    await expect(dispatch(TRUSTED_EVENT, "file:start-urdf", { urdf: "/other.urdf", expected_revision: "a".repeat(64) })).rejects.toThrow("invalid overwrite");
    callSidecarMock.mockResolvedValueOnce({ json: async () => ({ ...runReply(null), exit_code: 2, refusal: "lockfile changed since it was opened" }) });
    await expect(dispatch(TRUSTED_EVENT, "file:start-urdf", { urdf: "/tmp/robot.urdf", expected_revision: "a".repeat(64) })).rejects.toThrow("changed since");
  });
  it("refuses a bad extension before any sidecar call, and runs the start flow for a good one", async () => {
    await expect(dispatch(TRUSTED_EVENT, "file:open-dropped", { path: "/tmp/notes.txt" })).rejects.toThrow("Can't open that file");
    await expect(dispatch(TRUSTED_EVENT, "file:open-dropped", { path: "/tmp/x", extra: 1 })).rejects.toThrow("expected { path }");
    expect(callSidecarMock).not.toHaveBeenCalled();
    callSidecarMock.mockResolvedValueOnce({ json: async () => runReply({ path: "/tmp/lockwire.yaml", written: false, revision: "a".repeat(64) }) });
    callSidecarMock.mockResolvedValueOnce({ json: async () => runReply(checkPayload("GO", 0)) });
    const real = join(mkdtempSync(join(tmpdir(), "lw-drop-")), "robot.URDF");
    writeFileSync(real, "<robot/>");
    expect(await dispatch(TRUSTED_EVENT, "file:open-dropped", { path: real })).toMatchObject({ started: true });
    expect(callSidecarMock.mock.calls[0]?.[1].body).toBe(JSON.stringify({ command: "start", args: { urdf: real } }));
  });
  it("refuses a relative path before any sidecar call", async () => {
    await expect(dispatch(TRUSTED_EVENT, "file:open-dropped", { path: "../x.urdf" })).rejects.toThrow("full path");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
  it("refuses a missing file before any sidecar call", async () => {
    await expect(dispatch(TRUSTED_EVENT, "file:open-dropped", { path: "/nonexistent-lw-dir/x.urdf" })).rejects.toThrow("full path");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
  function checkPayload(verdict: CheckPayload["verdict"], exit_code: number): CheckPayload {
    return {
      format_version: "1",
      schema_version: "1",
      cli_line: "lockwire check /tmp/robot/lockwire.yaml",
      robot: "Test Robot",
      verdict,
      exit_code,
      counts: { violation: 0, warning: 0, not_scored: 0 },
      scored: 0,
      headline: "Measured headline",
      findings: [],
      artifact_type: "lockwire-check-report",
      forces: EMPTY_FORCES,
    };
  }

  it("file:open without a contract path is refused before the sidecar", async () => {
    await expect(dispatch(TRUSTED_EVENT, "file:open", undefined)).rejects.toThrow("file:open: invalid request");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("test_a_chosen_path_is_checked_and_the_real_payload_is_mapped_to_a_check_face", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () =>
        Promise.resolve({
          version: "0.1.0",
          cli_line: "lockwire check /tmp/robot/lockwire.yaml",
          exit_code: 0,
          verdict: "GO",
          refusal: null,
          payload: {
            format_version: "1",
            schema_version: "1",
            cli_line: "lockwire check /tmp/robot/lockwire.yaml",
            robot: "Test Robot",
            verdict: "GO",
            exit_code: 0,
            counts: { violation: 0, warning: 0, not_scored: 0 },
            scored: 0,
            headline: "Nothing could be compared yet",
            findings: [],
            artifact_type: "lockwire-check-report",
            forces: EMPTY_FORCES,
          },
        }),
    });

    const result = await dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS);
    expect(callSidecarMock).toHaveBeenCalledWith(
      "/v1/run",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ command: "check", args: { contract: "/tmp/robot/lockwire.yaml" } }),
      }),
    );
    expect(result).toEqual({
      opened: true,
      path: "/tmp/robot/lockwire.yaml",
      payload: {
        robot: "Test Robot",
        verdict: "GO",
        headline: "Nothing could be compared yet",
        qualifier: "",
        scored: 0,
        violations: 0,
        warnings: 0,
        unchecked: 0,
        findings: [],
        cards: [],
              cli_line: "lockwire check /tmp/robot/lockwire.yaml",
      },
      // E2-j2 rebase: the raw sidecar reply rides along unabridged (scene/graph included when
      // present) so the Design tab reads the SAME open, never a second round trip.
      checkPayload: {
        format_version: "1",
        schema_version: "1",
        cli_line: "lockwire check /tmp/robot/lockwire.yaml",
        robot: "Test Robot",
        verdict: "GO",
        exit_code: 0,
        counts: { violation: 0, warning: 0, not_scored: 0 },
        scored: 0,
        headline: "Nothing could be compared yet",
        findings: [],
        artifact_type: "lockwire-check-report",
        forces: EMPTY_FORCES,
      },
    });
  });

  it.each([
    ["GO", 0],
    ["GO_WITH_NOTES", 0],
    ["NO-GO", 1],
    ["REFUSED", 2],
    ["NOT_SCORED", 3],
  ] as const)("preserves the schema-valid %s domain result at exit %i", async (verdict, exit_code) => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply(checkPayload(verdict, exit_code))),
    });

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).resolves.toMatchObject({
      opened: true,
      payload: { verdict },
      checkPayload: { verdict, exit_code },
    });
  });

  it("rejects a contradictory GO document and envelope at exit 1", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply(checkPayload("GO", 1))),
    });

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).rejects.toThrow("sidecar: invalid run reply");
  });

  it("rejects a run envelope missing its required payload", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve({
        version: "0.1.0",
        cli_line: "lockwire check /tmp/robot/lockwire.yaml",
        exit_code: 0,
        verdict: "GO",
        refusal: null,
      }),
    });

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).rejects.toThrow("sidecar: invalid run reply");
  });

  it.each(["", "   ", "\t\n"])("rejects a check document with a blank headline", async (headline) => {
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply({ ...checkPayload("GO", 0), headline })),
    });

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).rejects.toThrow("file:open: invalid payload");
  });

  it("keeps a transport failure distinct from a valid non-GO document", async () => {
    callSidecarMock.mockRejectedValueOnce(new Error("sidecar transport unavailable"));

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).rejects.toThrow("sidecar transport unavailable");
  });

  it("test_a_sidecar_refusal_surfaces_as_an_error_not_a_thrown_exception", async () => {
    callSidecarMock.mockResolvedValueOnce({
      json: () =>
        Promise.resolve({
          version: "0.1.0",
          cli_line: "lockwire check /tmp/robot/lockwire.yaml",
          exit_code: 2,
          verdict: null,
          refusal: "no contract at /tmp/robot/lockwire.yaml",
          payload: null,
        }),
    });

    await expect(dispatch(TRUSTED_EVENT, "file:open", OPEN_ARGS)).resolves.toEqual({
      opened: true,
      path: "/tmp/robot/lockwire.yaml",
      error: "no contract at /tmp/robot/lockwire.yaml",
    });
  });
});

describe("dispatch: the plan chooser always use the native dialog", () => {
  it("test_LOCKWIRE_E2E_env_cannot_fabricate_a_plan_choice", async () => {
    const env = { LOCKWIRE_E2E_HIDDEN: "1", LOCKWIRE_E2E_OPEN_PATH: "/tmp/e2e/contract.yaml",
      LOCKWIRE_E2E_PLAN_PATH: "/tmp/e2e/plan.yaml", LOCKWIRE_E2E_RECORDING_PATH: "/tmp/e2e/recording.ulg" };
    for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value);
    try {
      showOpenDialog.mockResolvedValueOnce({ canceled: false, filePaths: ["/native/plan.yaml"] });
      await expect(dispatch(TRUSTED_EVENT, "file:choose-plan", undefined)).resolves.toBe("/native/plan.yaml");
      expect(showOpenDialog).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("dispatch: argument validation runs in the main process", () => {
  it("rejects a contract:edit payload missing contents even from a trusted sender", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:edit", { path: "a.json" })).rejects.toThrow(
      "contract:edit: invalid arguments",
    );
  });

  it.each([
    ["contract:edit", { path: "a.json", contents: "{}" }],
    ["contract:probe", { probe: "battery" }],
    ["robot:command", { commandName: "__hostile_unknown_command__", params: { token: "guessed" } }],
  ])("refuses the unimplemented %s command instead of claiming it was accepted", async (command, args) => {
    await expect(dispatch(TRUSTED_EVENT, command, args)).rejects.toThrow(`${command}: not supported`);
    expect(callSidecarMock).not.toHaveBeenCalled();
  });

  it("rejects a robot:command payload with no commandName", async () => {
    await expect(dispatch(TRUSTED_EVENT, "robot:command", { params: {} })).rejects.toThrow(
      "robot:command: invalid arguments",
    );
  });
});

describe("registerIpc wires the one channel through ipcMain.handle", () => {
  it("registers RUN_CHANNEL and nothing else", () => {
    registerIpc();
    expect(Array.from(handlers.keys())).toEqual([RUN_CHANNEL]);
  });

  it("the registered handler is dispatch itself, exercised end to end", async () => {
    registerIpc();
    const handle = handlers.get(RUN_CHANNEL)!;
    getSidecarStateMock.mockReturnValue({ phase: "ready", port: 4321 });
    await expect(handle(TRUSTED_EVENT, "sidecar:state", undefined)).resolves.toEqual({
      phase: "ready",
      port: 4321,
    });
    await expect(handle(TRUSTED_EVENT, "constructor", {})).rejects.toThrow(/permission denied/);
  });
});
