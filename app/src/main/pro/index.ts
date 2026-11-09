// Paid IPC handlers (run, preflight, recordings, actuator drafts, structural analysis). Registered
// by main/ipc.ts only when this folder exists, and only the classes named in PRO_COMMAND_CLASSES.
import { dialog } from "electron";

import type { AnalysisJobStatePayload, CheckPayloadPro, PreflightPayload, RunStatePayload } from "../../shared/pro/types";
import type {
  AnalysisStartRequest,
  ContractActuatorReviewDraftRequest,
  PreflightRequest,
  RunStartRequest,
} from "../../shared/pro/ipc-contract";
import { hasOnlyKeys, hasStrings, isEmptyRequest, isRecord, isStringArray, runSidecarCommand } from "../ipcKit";
import { proValidators, validateAnalysisJobStatePayload, validatePreflightPayload, validateRunStatePayload } from "./payloads";

function isArtifactRef(value: unknown): value is { path: string; sha256: string } {
  return isRecord(value) && hasOnlyKeys(value, ["path", "sha256"]) && hasStrings(value, ["path", "sha256"]);
}

function isContractActuatorReviewDraftRequest(args: unknown): args is ContractActuatorReviewDraftRequest {
  if (!isRecord(args)) return false;
  return (
    hasOnlyKeys(args, [
      "contract", "case_id", "evidence", "evidence_root", "expected_revision", "provenance",
      "discrepancy", "alternatives", "proposed_target", "proposed_patch", "rationale",
      "correction_category", "required_recheck",
    ]) &&
    hasStrings(args, [
      "contract", "case_id", "evidence_root", "expected_revision", "provenance", "discrepancy",
      "proposed_patch", "rationale",
    ]) &&
    isArtifactRef(args.evidence) &&
    isStringArray(args.alternatives) &&
    isStringArray(args.required_recheck) &&
    ["physical", "firmware", "configuration", "model", "calibration"].includes(args.proposed_target as string) &&
    ["physical", "model_calibration", "spec_design"].includes(args.correction_category as string)
  );
}

function isPreflightRequest(args: unknown): args is PreflightRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "recording", "context"]) &&
    hasStrings(args, ["contract", "recording"]) &&
    (args.context === undefined || typeof args.context === "string")
  );
}

function isRunStartRequest(args: unknown): args is RunStartRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "recording", "context", "live", "container", "local", "seconds",
      "ssh_host", "ssh_port", "ssh_user", "ssh_identity_file", "ssh_known_hosts", "ssh_target", "ssh_image", "keep_bag"]) &&
    typeof args.contract === "string" &&
    (args.recording === undefined || args.recording === null || typeof args.recording === "string") &&
    (args.context === undefined || typeof args.context === "string") &&
    (args.live === undefined || typeof args.live === "boolean") &&
    (args.container === undefined || args.container === null || typeof args.container === "string") &&
    (args.local === undefined || typeof args.local === "boolean") &&
    (args.seconds === undefined || (typeof args.seconds === "number" && Number.isFinite(args.seconds))) &&
    (args.ssh_host === undefined || args.ssh_host === null || typeof args.ssh_host === "string") &&
    (args.ssh_port === undefined || (typeof args.ssh_port === "number" && Number.isInteger(args.ssh_port) && args.ssh_port >= 1 && args.ssh_port <= 65535)) &&
    (args.ssh_user === undefined || typeof args.ssh_user === "string") &&
    (args.ssh_identity_file === undefined || args.ssh_identity_file === null || typeof args.ssh_identity_file === "string") &&
    (args.ssh_known_hosts === undefined || args.ssh_known_hosts === null || typeof args.ssh_known_hosts === "string") &&
    (args.ssh_target === undefined || args.ssh_target === null || typeof args.ssh_target === "string") &&
    (args.ssh_image === undefined || args.ssh_image === null || typeof args.ssh_image === "string") &&
    (args.keep_bag === undefined || args.keep_bag === null || typeof args.keep_bag === "string")
  );
}

function isAnalysisStartRequest(args: unknown): args is AnalysisStartRequest {
  return (
    isRecord(args) &&
    hasOnlyKeys(args, ["contract", "expected_revision", "procedure_id", "evidence_root", "package_manifest"]) &&
    hasStrings(args, ["contract", "expected_revision", "procedure_id", "evidence_root", "package_manifest"]) &&
    typeof args.expected_revision === "string" &&
    /^[a-f0-9]{64}$/.test(args.expected_revision) &&
    typeof args.procedure_id === "string" &&
    args.procedure_id.trim().length > 0
  );
}

async function handleActuatorReviewDraft(args: unknown): Promise<{ document: CheckPayloadPro; cliLine: string }> {
  if (!isContractActuatorReviewDraftRequest(args)) {
    throw new Error("contract:actuator-review-draft: invalid arguments");
  }
  const reply = await runSidecarCommand({ command: "actuator-review-draft", args });
  if (reply.refusal || !reply.payload) {
    throw new Error(reply.refusal ?? "the sidecar returned no actuator review draft payload");
  }
  return { document: proValidators.check(reply.payload), cliLine: reply.cli_line };
}

async function handlePreflight(args: unknown): Promise<PreflightPayload> {
  if (!isPreflightRequest(args)) throw new Error("contract:preflight: invalid arguments");
  const reply = await runSidecarCommand({ command: "preflight", args });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? "the sidecar returned no preflight payload");
  return validatePreflightPayload(reply.payload);
}

async function runJob(command: "run-start" | "run-poll" | "run-cancel", args: unknown): Promise<RunStatePayload> {
  const reply = await runSidecarCommand({ command, args: args ?? {} });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? `the sidecar returned no ${command} payload`);
  return validateRunStatePayload(reply.payload);
}

async function handleRunStart(args: unknown): Promise<RunStatePayload> {
  if (!isRunStartRequest(args)) throw new Error("run:start: invalid arguments");
  return runJob("run-start", args);
}

async function handleRunPoll(args: unknown): Promise<RunStatePayload> {
  if (!isEmptyRequest(args)) throw new Error("run:poll: invalid arguments");
  return runJob("run-poll", args);
}

async function handleRunCancel(args: unknown): Promise<RunStatePayload> {
  if (!isEmptyRequest(args)) throw new Error("run:cancel: invalid arguments");
  return runJob("run-cancel", args);
}

async function analysisJob(command: "analysis-start" | "analysis-poll" | "analysis-cancel", args: unknown): Promise<AnalysisJobStatePayload> {
  const reply = await runSidecarCommand({ command, args: args ?? {} });
  if (reply.refusal || !reply.payload) throw new Error(reply.refusal ?? `the sidecar returned no ${command} payload`);
  const state = validateAnalysisJobStatePayload(reply.payload);
  // The renderer consumes the hash-bound result document and bounded error only. Do not forward
  // captured child stdout/stderr, which may contain raw solver output.
  return { ...state, output: "", errors: "" };
}

async function handleAnalysisStart(args: unknown): Promise<AnalysisJobStatePayload> {
  if (!isAnalysisStartRequest(args)) throw new Error("analysis:start: invalid arguments");
  return analysisJob("analysis-start", args);
}

async function handleAnalysisPoll(args: unknown): Promise<AnalysisJobStatePayload> {
  if (!isEmptyRequest(args)) throw new Error("analysis:poll: invalid arguments");
  return analysisJob("analysis-poll", args);
}

async function handleAnalysisCancel(args: unknown): Promise<AnalysisJobStatePayload> {
  if (!isEmptyRequest(args)) throw new Error("analysis:cancel: invalid arguments");
  return analysisJob("analysis-cancel", args);
}

async function handleChooseRecording(_args: unknown): Promise<string | null> {
  const result = await dialog.showOpenDialog({
    // A rosbag2 recording is usually a folder (metadata.yaml + .mcap parts), so folders are pickable.
    // ponytail: macOS honours openFile+openDirectory together; Windows/Linux show a folder picker
    // only. Split into two buttons if those platforms ship. .db3 is not offered: lockwire reads MCAP.
    properties: ["openFile", "openDirectory"],
    filters: [{ name: "ROS 2 bag or recording", extensions: ["mcap", "ulg", "zst"] }],
  });
  return result.canceled ? null : (result.filePaths[0] ?? null);
}

export const PRO_COMMAND_CLASSES = [
  "contract:actuator-review-draft",
  "contract:preflight",
  "run:start",
  "run:poll",
  "run:cancel",
  "analysis:start",
  "analysis:poll",
  "analysis:cancel",
  "file:choose-recording",
] as const;

export type ProCommandClass = (typeof PRO_COMMAND_CLASSES)[number];

const handlers = {
  "contract:actuator-review-draft": handleActuatorReviewDraft,
  "contract:preflight": handlePreflight,
  "run:start": handleRunStart,
  "run:poll": handleRunPoll,
  "run:cancel": handleRunCancel,
  "analysis:start": handleAnalysisStart,
  "analysis:poll": handleAnalysisPoll,
  "analysis:cancel": handleAnalysisCancel,
  "file:choose-recording": handleChooseRecording,
} satisfies Record<ProCommandClass, (args: unknown) => Promise<unknown>>;

export default { classes: PRO_COMMAND_CLASSES, handlers, validators: proValidators };
