import { existsSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";

import { dialog, ipcMain, type BrowserWindow, type IpcMainInvokeEvent } from "electron";

import type { CheckFacePayload } from "../renderer/checkFacePayload";
import { type CheckPayload, type DeclareFormPayload, type DiffPayload, type LogPayload, type PagePayload, type PlanPayload, type StartersPayload, type WhatIfPayload } from "../shared/api.gen";
import {
  PERMITTED_COMMAND_CLASSES,
  MENU_OPENED_CHANNEL,
  RUN_CHANNEL,
  SIDECAR_STATE_CHANGED_CHANNEL,
  type CommandClass,
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
import { handleApply, handleCancel, handleSend, handleSettingsGet, handleSettingsSet } from "./assistant";
import { checkPayloadToFace } from "./checkPayload";
import { pickNewRobotLocation, pickRobot } from "./dialogs";
import { hasOnlyKeys, hasStrings, isRecord, isStringArray, runSidecarCommand, type SidecarRunReply } from "./ipcKit";
import {
  validateCheckPayload,
  validateStartPayload,
  validateStartersPayload,
  validateDeclareFormPayload,
  validateDiffPayload,
  validateEditPayload,
  validateLogPayload,
  validatePagePayload,
  validatePlanPayload,
  validateWhatIfPayload,
} from "./pagePayload";
import { proMain } from "./proRegistry";
import { rendererUrl } from "./renderer-protocol";
import { getSidecarState, onSidecarState, startSidecar, type SidecarState } from "./sidecar";

type ContractEditArgs = { path: string; contents: string };
type ProbeArgs = { probe: string };
type RobotCommandArgs = { commandName: string; params?: Record<string, unknown> };

function isContractEditArgs(args: unknown): args is ContractEditArgs {
  const a = args as Record<string, unknown> | null;
  return typeof a === "object" && a !== null && typeof a.path === "string" && typeof a.contents === "string";
}
function isProbeArgs(args: unknown): args is ProbeArgs {
  const a = args as Record<string, unknown> | null;
  return typeof a === "object" && a !== null && typeof a.probe === "string";
}
function isRobotCommandArgs(args: unknown): args is RobotCommandArgs {
  const a = args as Record<string, unknown> | null;
  return typeof a === "object" && a !== null && typeof a.commandName === "string";
}


function hasOptionalFields(value: Record<string, unknown>): boolean {
  return (
    (value.note === undefined || typeof value.note === "string") &&
    (value.locked_at === undefined || typeof value.locked_at === "string") &&
    (value.dry_run === undefined || typeof value.dry_run === "boolean")
  );
}

function hasExpectedRevision(value: Record<string, unknown>): boolean {
  return typeof value.expected_revision === "string";
}

function isDeclareArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "path", "value", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasStrings(value, ["contract", "path", "source", "witness"]) &&
    Object.hasOwn(value, "value") &&
    value.value !== undefined &&
    hasOptionalFields(value) && hasExpectedRevision(value)
  );
}

function isAddArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "kind", "id", "fields", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasStrings(value, ["contract", "kind", "source", "witness"]) &&
    (value.id === undefined || typeof value.id === "string") &&
    (value.fields === undefined || isRecord(value.fields)) &&
    hasOptionalFields(value) && hasExpectedRevision(value)
  );
}

function isReviewArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "path", "state", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasStrings(value, ["contract", "path", "witness"]) &&
    (value.state === "approved" || value.state === "questioned") &&
    hasOptionalFields(value) && hasExpectedRevision(value)
  );
}

function isTriple(value: unknown): boolean {
  return Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

function hasWriteProvenance(value: Record<string, unknown>): boolean {
  return hasStrings(value, ["contract", "target", "source", "witness"]) && hasOptionalFields(value) && hasExpectedRevision(value);
}

function isPlaceArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "target", "on", "offset", "unit", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasWriteProvenance(value) &&
    hasStrings(value, ["on", "unit"]) &&
    isTriple(value.offset)
  );
}

function isMoveArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "target", "offset", "unit", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasWriteProvenance(value) &&
    typeof value.unit === "string" &&
    isTriple(value.offset)
  );
}

function isRotateArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "target", "rpy", "unit", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasWriteProvenance(value) &&
    typeof value.unit === "string" &&
    isTriple(value.rpy)
  );
}

function isReparentArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "target", "to", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) &&
    hasWriteProvenance(value) &&
    typeof value.to === "string"
  );
}

function isMountShapeArgs(value: Record<string, unknown>): boolean {
  if (
    !hasOnlyKeys(value, ["contract", "target", "shape", "source", "witness", "note", "locked_at", "dry_run", "expected_revision"]) ||
    !hasWriteProvenance(value) ||
    !isRecord(value.shape)
  ) return false;
  const shape = value.shape;
  return (
    hasOnlyKeys(shape, ["kind", "dimensions_mm", "fastener_pattern", "mesh_path"]) &&
    (shape.kind === "plate" || shape.kind === "bracket" || shape.kind === "tube" || shape.kind === "custom-mesh") &&
    isTriple(shape.dimensions_mm) &&
    (shape.fastener_pattern === undefined || typeof shape.fastener_pattern === "string") &&
    (shape.mesh_path === undefined || typeof shape.mesh_path === "string")
  );
}

function isCommitArgs(value: Record<string, unknown>): boolean {
  return (
    hasOnlyKeys(value, ["contract", "message", "paths", "expected_revision", "dry_run"]) &&
    hasStrings(value, ["contract", "message", "expected_revision"]) &&
    Array.isArray(value.paths) &&
    value.paths.every((path) => typeof path === "string") &&
    (value.dry_run === undefined || typeof value.dry_run === "boolean")
  );
}

function isContractWriteRequest(value: unknown): value is ContractWriteRequest {
  if (!isRecord(value) || !hasOnlyKeys(value, ["command", "args"]) || !isRecord(value.args)) return false;
  switch (value.command) {
    case "declare":
      return isDeclareArgs(value.args);
    case "add":
      return isAddArgs(value.args);
    case "review":
      return isReviewArgs(value.args);
    case "commit":
      return isCommitArgs(value.args);
    case "place":
      return isPlaceArgs(value.args);
    case "move":
      return isMoveArgs(value.args);
    case "rotate":
      return isRotateArgs(value.args);
    case "reparent":
      return isReparentArgs(value.args);
    case "mount-shape":
      return isMountShapeArgs(value.args);
    default:
      return false;
  }
}

function isContractUndoRequest(args: unknown): args is ContractUndoRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "source", "apply", "expected_revision"]) &&
    typeof args.contract === "string" &&
    typeof args.expected_revision === "string" &&
    (args.source === undefined || typeof args.source === "string") &&
    (args.apply === undefined || typeof args.apply === "boolean")
  );
}

function isContractPageRequest(args: unknown): args is ContractPageRequest {
  return isRecord(args) && hasOnlyKeys(args, ["contract"]) && typeof args.contract === "string";
}

function isDeclareFormRequest(args: unknown): args is DeclareFormRequest {
  return isRecord(args) && hasOnlyKeys(args, ["contract"]) && typeof args.contract === "string";
}



function isContractWhatIfRequest(args: unknown): args is ContractWhatIfRequest {
  if (!isRecord(args) || !hasOnlyKeys(args, ["contract", "expected_revision", "change"]) || !hasStrings(args, ["contract", "expected_revision"]) || !isRecord(args.change)) return false;
  return hasOnlyKeys(args.change, ["path", "value", "source", "witness"]) && hasStrings(args.change, ["path", "value", "source", "witness"]);
}

function isPlanRequest(args: unknown): args is PlanRequest {
  return isRecord(args) && hasOnlyKeys(args, ["contract", "plan"]) && hasStrings(args, ["contract", "plan"]);
}


function isDiffRequest(args: unknown): args is DiffRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "a", "b"]) &&
    typeof args.contract === "string" &&
    (args.a === undefined || typeof args.a === "string") &&
    (args.b === undefined || typeof args.b === "string")
  );
}

function isHistoryRequest(args: unknown): args is HistoryRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "revision_range"]) &&
    typeof args.contract === "string" &&
    (args.revision_range === undefined || args.revision_range === null || typeof args.revision_range === "string")
  );
}






function isCommitPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, ["argv", "committed", "sha", "untouched", "reason"]) &&
    isStringArray(value.argv) &&
    typeof value.committed === "boolean" &&
    (value.sha === null || typeof value.sha === "string") &&
    isStringArray(value.untouched) &&
    (value.reason === undefined || typeof value.reason === "string")
  );
}

function isUndoPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    hasOnlyKeys(value, ["kind", "argv", "scope", "repo", "sentence"]) &&
    typeof value.kind === "string" &&
    isStringArray(value.argv) &&
    isStringArray(value.scope) &&
    typeof value.repo === "string" &&
    typeof value.sentence === "string"
  );
}


async function handleContractWrite(args: unknown): Promise<SidecarRunReply> {
  if (!isContractWriteRequest(args)) throw new Error("contract:write: invalid arguments");
  const reply = await runSidecarCommand(args);
  if (!reply.refusal && args.command !== "commit") validateEditPayload(reply.payload);
  if (!reply.refusal && args.command === "commit" && !isCommitPayload(reply.payload)) {
    throw new Error("contract:write: invalid commit payload");
  }
  return reply;
}

async function handleContractUndo(args: unknown): Promise<SidecarRunReply> {
  if (!isContractUndoRequest(args)) throw new Error("contract:undo: invalid arguments");
  const reply = await runSidecarCommand({ command: "undo", args });
  if (!reply.refusal && !isUndoPayload(reply.payload)) throw new Error("contract:undo: invalid payload");
  return reply;
}

async function handleContractPage(args: unknown): Promise<PagePayload> {
  if (!isContractPageRequest(args)) throw new Error("contract:page: invalid arguments");
  const reply = await runSidecarCommand({ command: "page", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no page payload");
  return proMain ? proMain.validators.page(reply.payload) : validatePagePayload(reply.payload);
}

async function handleDeclareForm(args: unknown): Promise<DeclareFormPayload> {
  if (!isDeclareFormRequest(args)) throw new Error("contract:declare-form: invalid arguments");
  const reply = await runSidecarCommand({ command: "declare-form", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no declare-form payload");
  return validateDeclareFormPayload(reply.payload);
}


async function handleWhatIf(args: unknown): Promise<WhatIfPayload> {
  if (!isContractWhatIfRequest(args)) throw new Error("contract:what-if: invalid arguments");
  const reply = await runSidecarCommand({ command: "what-if", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no what-if payload");
  return proMain ? proMain.validators.whatIf(reply.payload) : validateWhatIfPayload(reply.payload);
}

/** An empty `plan` means "the one beside the lockfile": `<lockfile stem>.plan.yaml`, or null when absent. */
async function handlePlan(args: unknown): Promise<PlanPayload | null> {
  if (!isPlanRequest(args)) throw new Error("contract:plan: invalid arguments");
  if (args.plan === "") {
    const sibling = args.contract.replace(/\.[^./\\]*$/, "") + ".plan.yaml";
    if (!existsSync(sibling)) return null;
    args = { contract: args.contract, plan: sibling };
  }
  const reply = await runSidecarCommand({ command: "plan", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no plan payload");
  return validatePlanPayload(reply.payload);
}


async function handleDiff(args: unknown): Promise<DiffPayload> {
  if (!isDiffRequest(args)) throw new Error("contract:diff: invalid arguments");
  const reply = await runSidecarCommand({ command: "diff", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no diff payload");
  return validateDiffPayload(reply.payload);
}

async function handleHistory(args: unknown): Promise<LogPayload> {
  if (!isHistoryRequest(args)) throw new Error("contract:history: invalid arguments");
  const reply = await runSidecarCommand({ command: "log", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no log payload");
  return validateLogPayload(reply.payload);
}









async function handleChoosePlan(_args: unknown): Promise<string | null> {
  const result = await dialog.showOpenDialog({
    properties: ["openFile"],
    filters: [{ name: "Plan", extensions: ["yaml", "yml", "json"] }],
  });
  return result.canceled ? null : (result.filePaths[0] ?? null);
}


// These legacy preload methods have no implementation or renderer caller. Keep their API shape
// for compatibility, but fail closed until a real handler and supported operation are defined.
async function handleContractEdit(args: unknown) {
  if (!isContractEditArgs(args)) throw new Error("contract:edit: invalid arguments");
  throw new Error("contract:edit: not supported");
}
async function handleProbe(args: unknown) {
  if (!isProbeArgs(args)) throw new Error("contract:probe: invalid arguments");
  throw new Error("contract:probe: not supported");
}
async function handleRobotCommand(args: unknown) {
  if (!isRobotCommandArgs(args)) throw new Error("robot:command: invalid arguments");
  throw new Error("robot:command: not supported");
}

// e9-join: the renderer is sandboxed (contextIsolation + no nodeIntegration, window-options.ts)
// and cannot import "./sidecar" itself -- that direct import is what made `npm run build`
// resolve main-process code with browser externals and fail. These two handlers are the whole
// fix: the renderer's only legal path to the sidecar's state is through this same dispatch gate
// every other command already goes through.
async function handleSidecarState(_args: unknown): Promise<SidecarState> {
  return getSidecarState();
}

// e9-rate (review flag on e9-join): sidecar:restart is state-changing and reachable from the
// renderer, so it needs a rate limit enforced HERE, in the main process -- a limit the renderer
// enforced on itself would not be a limit, since the renderer is the untrusted side. The knob is
// this one named constant (same pattern as sidecar.ts's STARTUP_TIMEOUT_MS): a slow machine that
// needs a longer cooldown tunes it in one place. A refused call throws, same as every other
// dispatch-level refusal in this file -- the caller sees a rejected promise, never a lied-about
// `{ accepted: true }`.
const SIDECAR_RESTART_MIN_INTERVAL_MS = 2_000;
let lastSidecarRestartAt = -Infinity;

/** Test-only: the rate limiter's state is module-level (it has to persist across separate
 * dispatch() calls, which is the whole point), so tests that exercise it back-to-back need a
 * way to reset it instead of inheriting whatever an earlier test left behind. */
export function __resetSidecarRestartRateLimitForTests(): void {
  lastSidecarRestartAt = -Infinity;
}

async function handleSidecarRestart(_args: unknown) {
  const now = Date.now();
  const elapsed = now - lastSidecarRestartAt;
  if (elapsed < SIDECAR_RESTART_MIN_INTERVAL_MS) {
    throw new Error(
      `sidecar:restart: rate limited, wait ${SIDECAR_RESTART_MIN_INTERVAL_MS - elapsed}ms before retrying`,
    );
  }
  lastSidecarRestartAt = now;
  startSidecar();
  return { accepted: true as const };
}

export type FileOpenResult =
  | { opened: false }
  | { opened: true; path: string; payload: CheckFacePayload; checkPayload: CheckPayload }
  | { opened: true; path: string; error: string };

/** Open a lockfile at a given path: ask the sidecar to `check`
 * it and hand the renderer a real `CheckFacePayload` (`checkPayloadToFace` fills only
 * the half `lockwire.api.check` emits -- see that file for the known gap). A
 * cancelled dialog never reaches the sidecar.
 *
 * `checkPayload` (E2-j2 rebase) is the SAME sidecar reply `checkPayloadToFace` narrows --
 * carried through unabridged, scene/graph included, so the Design tab has one source for the
 * same open rather than a second round trip or the test-only `window.__lockwireLoadCheckPayload`
 * door. */
async function handleFileOpen(args: unknown): Promise<FileOpenResult> {
  if (!isRecord(args) || !hasOnlyKeys(args, ["contract"]) || !hasStrings(args, ["contract"])) {
    throw new Error("file:open: invalid request");
  }
  return openContract((args as ContractPageRequest).contract);
}

async function openContract(path: string): Promise<FileOpenResult> {

  const reply = await runSidecarCommand({ command: "check", args: { contract: path } });
  if (reply.refusal || !reply.payload) {
    return { opened: true, path, error: reply.refusal ?? "the sidecar returned no payload" };
  }
  const checked = proMain ? proMain.validators.check(reply.payload) : validateCheckPayload(reply.payload);
  return {
    opened: true,
    path,
    payload: checkPayloadToFace(checked, reply.cli_line),
    checkPayload: checked,
  };
}

export type StartUrdfResult =
  | { started: false; /** a lockfile was picked: opened and checked, no start */ opened?: FileOpenResult }
  | { started: true; urdf: string; start: import("../shared/api.gen").StartPayload; opened: FileOpenResult };

let pendingUrdf: string | null = null;

// Chosen (dialog or drop) path -> lockfile check, or remembered URDF + the start call below.
async function startFromPath(selected: string): Promise<StartUrdfResult> {
  if (/\.ya?ml$/i.test(selected)) return { started: false, opened: await openContract(selected) };
  pendingUrdf = selected;
  return runStart(selected, undefined);
}

async function readStarters(): Promise<StartersPayload["starters"]> {
  const reply = await runSidecarCommand({ command: "starters", args: {} });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "starters returned no payload");
  return validateStartersPayload(reply.payload).starters;
}

async function handleStarters(_args: unknown): Promise<StartersPayload["starters"]> {
  return readStarters();
}

/** "New robot": a starter (named from the sidecar's own list, never a path) or null for blank; the sidecar writes it where the user chose, then it is opened like any lockfile. */
async function handleNewRobot(args: unknown): Promise<StartUrdfResult> {
  if (!isRecord(args) || !hasOnlyKeys(args, ["starter"]) || !(args.starter === null || typeof args.starter === "string")) {
    throw new Error("file:new-robot: expected { starter: string | null }");
  }
  const starter = args.starter;
  if (starter !== null && !(await readStarters()).some((s) => s.name === starter)) {
    throw new Error(`file:new-robot: no starter named ${JSON.stringify(starter)}`);
  }
  const out = await pickNewRobotLocation(starter ?? "blank");
  if (out === null) return { started: false };
  const reply = await runSidecarCommand({
    command: "start",
    args: starter === null ? { blank: true, out } : { example: starter, out },
  });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "start returned no payload");
  return { started: false, opened: await openContract(validateStartPayload(reply.payload).path) };
}

const DROPPABLE = /\.(urdf|sdf|xacro|ya?ml)$/i;

async function handleOpenDropped(args: unknown): Promise<StartUrdfResult> {
  if (!isRecord(args) || !hasOnlyKeys(args, ["path"]) || !hasStrings(args, ["path"])) {
    throw new Error("file:open-dropped: expected { path }");
  }
  const path = args.path as string;
  if (!DROPPABLE.test(path)) {
    throw new Error("Can't open that file. Drop a .urdf, .sdf, .xacro or .yaml robot file.");
  }
  let isFile = false;
  try {
    isFile = isAbsolute(path) && statSync(path).isFile();
  } catch {
    // missing or unreadable: refused below
  }
  if (!isFile) throw new Error("Can't open that file. It must be an existing file given by its full path.");
  return startFromPath(path);
}

/** File > Open Robot...: the same flow as the Open robot button, result pushed to the window. */
export async function openRobotFromMenu(win: Pick<BrowserWindow, "isDestroyed" | "webContents">): Promise<void> {
  let payload: { result: StartUrdfResult } | { error: string };
  try {
    payload = { result: await handleStartUrdf(undefined) };
  } catch (error) {
    payload = { error: error instanceof Error ? error.message : String(error) };
  }
  if (!win.isDestroyed()) win.webContents.send(MENU_OPENED_CHANNEL, payload);
}

async function handleStartUrdf(args: unknown): Promise<StartUrdfResult> {
  let urdf: string;
  let request: StartUrdfRequest | undefined;
  if (args === undefined) {
    const selected = await pickRobot();
    if (selected === null) return { started: false };
    return startFromPath(selected);
  } else {
    if (!isRecord(args) || !hasOnlyKeys(args, ["urdf", "expected_revision"]) ||
        !hasStrings(args, ["urdf", "expected_revision"]) || args.urdf !== pendingUrdf ||
        !/^[a-f0-9]{64}$/.test(args.expected_revision as string)) {
      throw new Error("file:start-urdf: invalid overwrite request; choose the URDF again");
    }
    request = args as StartUrdfRequest;
    urdf = request.urdf;
  }
  return runStart(urdf, request);
}

async function runStart(urdf: string, request: StartUrdfRequest | undefined): Promise<StartUrdfResult> {
  const reply = await runSidecarCommand({ command: "start", args: {
    urdf, ...(request ? { overwrite: true, expected_revision: request.expected_revision } : {}),
  } });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "start returned no payload");
  const start = validateStartPayload(reply.payload);
  if (start.written) pendingUrdf = null;
  try {
    return { started: true, urdf, start, opened: await openContract(start.path) };
  } catch (error) {
    return { started: true, urdf, start, opened: { opened: true, path: start.path, error: String(error) } };
  }
}

/**
 * The whole permission contract in one object: every command class the renderer's enumerated
 * preload API may ever name, each bound to its own validator+handler. `PERMITTED_COMMAND_CLASSES`
 * (shared/ipc-contract.ts) is the list this is built from and is checked against at import time
 * (the `satisfies` below), so the two can't drift -- add a class to one and not the other and
 * TypeScript refuses to build.
 */
const HANDLERS = {
  "contract:edit": handleContractEdit,
  "contract:write": handleContractWrite,
  "contract:undo": handleContractUndo,
  "contract:probe": handleProbe,
  "contract:page": handleContractPage,
  "contract:declare-form": handleDeclareForm,
  "contract:what-if": handleWhatIf,
  "contract:plan": handlePlan,
  "contract:diff": handleDiff,
  "contract:history": handleHistory,
  "robot:command": handleRobotCommand,
  "file:open": handleFileOpen,
  "file:start-urdf": handleStartUrdf,
  "file:open-dropped": handleOpenDropped,
  "file:new-robot": handleNewRobot,
  "file:starters": handleStarters,
  "file:choose-plan": handleChoosePlan,
  "sidecar:state": handleSidecarState,
  "sidecar:restart": handleSidecarRestart,
  "assistant:settings-get": handleSettingsGet,
  "assistant:settings-set": handleSettingsSet,
  "assistant:send": handleSend,
  "assistant:apply": handleApply,
  "assistant:cancel": handleCancel,
} satisfies Record<CommandClass, (args: unknown) => Promise<unknown>>;

// Exhaustiveness check: every name in the shared list must be a key of HANDLERS (and vice versa,
// since HANDLERS is typed Record<CommandClass, ...>). This line only exists to fail the build if
// PERMITTED_COMMAND_CLASSES ever grows a name HANDLERS doesn't.
const _exhaustive: Record<CommandClass, unknown> = HANDLERS;
void _exhaustive;

// The paid classes join only by name: main/pro/ exports its explicit class list and one handler
// per class (never a glob over handlers), and a build without pro/ has exactly the free surface.
const REGISTERED: Record<string, (args: unknown) => Promise<unknown>> = { ...HANDLERS };
for (const commandClass of proMain?.classes ?? []) REGISTERED[commandClass] = proMain!.handlers[commandClass]!;

export function isPermittedCommand(commandClass: string): boolean {
  // Object.hasOwn, never a bare `HANDLERS[x]` truthiness check: indexing a plain object with a
  // renderer-controlled string also resolves inherited names ("constructor", "toString", ...) to
  // something callable, which would let an unlisted command class silently succeed instead of
  // being refused. hasOwn is what makes PERMITTED_COMMAND_CLASSES the actual gate.
  return Object.hasOwn(REGISTERED, commandClass);
}

export function isTrustedSender(event: Pick<IpcMainInvokeEvent, "senderFrame">): boolean {
  const frame = event.senderFrame;
  if (!frame) return false;
  try {
    const frameUrl = new URL(frame.url);
    const trustedUrl = new URL(rendererUrl());
    // URL.origin is "null" for custom schemes, so compare every authority component directly.
    if (
      frameUrl.protocol !== trustedUrl.protocol ||
      frameUrl.host !== trustedUrl.host ||
      frameUrl.username !== trustedUrl.username ||
      frameUrl.password !== trustedUrl.password
    ) {
      return false;
    }
    // Frame's path must equal or be under the trusted path (at / boundary)
    const framePathname = frameUrl.pathname;
    const trustedPathname = trustedUrl.pathname;
    return framePathname === trustedPathname || framePathname.startsWith(trustedPathname.replace(/\/$/, "") + "/");
  } catch {
    return false;
  }
}

/**
 * The single entry point every exposed preload method funnels into. Both checks run in the main
 * process regardless of what the renderer already validated: an untrusted sender is refused
 * before the command class is even looked at, and a command class outside HANDLERS is refused
 * before any handler runs.
 */
export async function dispatch(event: IpcMainInvokeEvent, commandClass: string, args: unknown): Promise<unknown> {
  if (!isTrustedSender(event)) {
    throw new Error(`permission denied: untrusted sender for "${commandClass}"`);
  }
  if (!isPermittedCommand(commandClass)) {
    throw new Error(`permission denied: unknown command "${commandClass}"`);
  }
  return REGISTERED[commandClass]!(args);
}

export function registerIpc(): void {
  ipcMain.handle(RUN_CHANNEL, (event, commandClass: string, args: unknown) => dispatch(event, commandClass, args));
}

/** The push half of the sidecar bridge: forwards every state change to this one window,
 * unprompted (the renderer's `sidecar:state` invoke above only answers when asked). Never
 * sends to a destroyed window -- the sidecar can outlive the window by a tick on quit. */
export function forwardSidecarState(win: Pick<BrowserWindow, "isDestroyed" | "webContents">): () => void {
  return onSidecarState((state) => {
    if (!win.isDestroyed()) win.webContents.send(SIDECAR_STATE_CHANGED_CHANNEL, state);
  });
}

export { RUN_CHANNEL };
