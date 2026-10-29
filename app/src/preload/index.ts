import { contextBridge, ipcRenderer, webUtils } from "electron";

import type { FileOpenResult, StartUrdfResult } from "../main/ipc";
import type { SidecarState } from "../main/sidecar";
import type { DeclareFormPayload, DiffPayload, LogPayload, PagePayload, PlanPayload, Starter, WhatIfPayload } from "../shared/api.gen";
import { proApi } from "./proRegistry";
import {
  ASSISTANT_EVENT_CHANNEL,
  MENU_OPENED_CHANNEL,
  RUN_CHANNEL,
  TERMINAL_EVENT_CHANNEL,
  SIDECAR_STATE_CHANGED_CHANNEL,
  type CommandClass,
  type AssistantApplyResult,
  type AssistantEvent,
  type AssistantSendRequest,
  type AssistantSettings,
  type AssistantSetRequest,
  type AssistantSetResult,
  type AssistantTurn,
  type TerminalEvent,
  type TerminalStartRequest,
  type StartUrdfRequest,
  type ContractWhatIfRequest,
  type ContractPageRequest,
  type DeclareFormRequest,
  type DiffRequest,
  type HistoryRequest,
  type PlanRequest,
  type ContractUndoRequest,
  type ContractWriteRequest,
} from "../shared/ipc-contract";

// The renderer's entire reach into the main process: named methods, each hardcoding its
// own command class -- never a channel or command name taken from a caller. This is what makes
// it a permission contract instead of `invoke(channel, ...args)` with a bridge over it: a
// renderer that wants to do something not listed here has no path to ask for it, not even by
// guessing a string.
const lockwire = {
  ...proApi?.(<T>(commandClass: string, args: unknown): Promise<T> => ipcRenderer.invoke(RUN_CHANNEL, commandClass, args)),
  editContract: (args: { path: string; contents: string }) => run("contract:edit", args),
  writeContract: (request: ContractWriteRequest) => run("contract:write", request),
  undoContract: (request: ContractUndoRequest) => run("contract:undo", request),
  runProbe: (args: { probe: string }) => run("contract:probe", args),
  getContractPage: (request: ContractPageRequest): Promise<PagePayload> => run("contract:page", request),
  getDeclareForm: (request: DeclareFormRequest): Promise<DeclareFormPayload> => run("contract:declare-form", request),
  compareContractWhatIf: (request: ContractWhatIfRequest): Promise<WhatIfPayload> => run("contract:what-if", request),
  checkPlan: (request: PlanRequest): Promise<PlanPayload | null> => run("contract:plan", request),
  diffContract: (request: DiffRequest): Promise<DiffPayload> => run("contract:diff", request),
  getHistory: (request: HistoryRequest): Promise<LogPayload> => run("contract:history", request),
  choosePlan: (): Promise<string | null> => run("file:choose-plan", undefined),
  sendRobotCommand: (args: { commandName: string; params?: Record<string, unknown> }) =>
    run("robot:command", args),
  openLockfile: (request: ContractPageRequest): Promise<FileOpenResult> => run("file:open", request),
  startFromUrdf: (request?: StartUrdfRequest): Promise<StartUrdfResult> => run("file:start-urdf", request),
  newRobot: (starter: string | null): Promise<StartUrdfResult> => run("file:new-robot", { starter }),
  getStarters: (): Promise<Starter[]> => run("file:starters", undefined),
  // Drag-to-open: pathForFile turns a dropped File into its path (webUtils), openDroppedPath
  // runs the same start/open+check flow; main refuses any extension but urdf/sdf/xacro/yaml/yml.
  pathForFile: (file: File): string => webUtils.getPathForFile(file),
  openDroppedPath: (path: string): Promise<StartUrdfResult> => run("file:open-dropped", { path }),
  // File > Open Robot...: fires with the same result startFromUrdf() returns, or { error }.
  onOpenedFromMenu: (cb: (payload: { result: StartUrdfResult } | { error: string }) => void): (() => void) => {
    const listener = (_event: unknown, payload: { result: StartUrdfResult } | { error: string }) => cb(payload);
    ipcRenderer.on(MENU_OPENED_CHANNEL, listener);
    return () => ipcRenderer.removeListener(MENU_OPENED_CHANNEL, listener);
  },
  // e9-join: the renderer is sandboxed (no nodeIntegration) and can't import "./sidecar"
  // itself -- these three are its only legal reach into the sidecar's state.
  getSidecarState: (): Promise<SidecarState> => run("sidecar:state", undefined),
  restartSidecar: (): Promise<{ accepted: true }> => run("sidecar:restart", undefined),
  getAssistantSettings: (): Promise<AssistantSettings> => run("assistant:settings-get", undefined),
  setAssistantSettings: (request: AssistantSetRequest): Promise<AssistantSetResult> => run("assistant:settings-set", request),
  sendAssistant: (request: AssistantSendRequest): Promise<AssistantTurn> => run("assistant:send", request),
  applyAssistant: (request: { id: string; witness: string }): Promise<AssistantApplyResult> => run("assistant:apply", request),
  cancelAssistant: (): Promise<{ ok: true }> => run("assistant:cancel", undefined),
  onAssistantEvent: (cb: (event: AssistantEvent) => void): (() => void) => {
    const listener = (_event: unknown, payload: AssistantEvent) => cb(payload);
    ipcRenderer.on(ASSISTANT_EVENT_CHANNEL, listener);
    return () => ipcRenderer.removeListener(ASSISTANT_EVENT_CHANNEL, listener);
  },
  startTerminal: (request: TerminalStartRequest): Promise<{ ok: true }> => run("terminal:start", request),
  writeTerminal: (data: string): Promise<{ ok: true }> => run("terminal:write", { data }),
  resizeTerminal: (cols: number, rows: number): Promise<{ ok: true }> => run("terminal:resize", { cols, rows }),
  killTerminal: (): Promise<{ ok: true }> => run("terminal:kill", undefined),
  onTerminalEvent: (cb: (event: TerminalEvent) => void): (() => void) => {
    const listener = (_event: unknown, payload: TerminalEvent) => cb(payload);
    ipcRenderer.on(TERMINAL_EVENT_CHANNEL, listener);
    return () => ipcRenderer.removeListener(TERMINAL_EVENT_CHANNEL, listener);
  },
  onSidecarState: (cb: (state: SidecarState) => void): (() => void) => {
    const listener = (_event: unknown, state: SidecarState) => cb(state);
    ipcRenderer.on(SIDECAR_STATE_CHANGED_CHANNEL, listener);
    return () => ipcRenderer.removeListener(SIDECAR_STATE_CHANGED_CHANNEL, listener);
  },
};

function run<T>(commandClass: CommandClass, args: unknown): Promise<T> {
  return ipcRenderer.invoke(RUN_CHANNEL, commandClass, args);
}

contextBridge.exposeInMainWorld("lockwire", lockwire);

export type PreloadApi = typeof lockwire;
