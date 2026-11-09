import type { IpcMainInvokeEvent, OpenDialogOptions } from "electron";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CheckPayloadPro as CheckPayload } from "../../shared/pro/types";
import type { ActuatorCase, AnalysisJobStatePayload } from "../../shared/pro/types";

const { showOpenDialog } = vi.hoisted(() => ({ showOpenDialog: vi.fn() }));
vi.mock("electron", () => ({
  app: { isPackaged: false },
  dialog: { showOpenDialog },
  ipcMain: { handle: vi.fn() },
}));
vi.mock("../dialogs", () => ({ pickRobot: vi.fn() }));

const callSidecarMock = vi.fn();
vi.mock("../sidecar", () => ({
  callSidecar: (...args: unknown[]) => callSidecarMock(...args),
  getSidecarState: vi.fn(),
  startSidecar: vi.fn(),
  onSidecarState: vi.fn(),
}));

import { dispatch, isPermittedCommand } from "../ipc";
import { PRO_COMMAND_CLASSES } from "./index";

const TRUSTED_EVENT = { senderFrame: { url: "http://localhost:5173/" } } as IpcMainInvokeEvent;

const ACTUATOR_CASE: ActuatorCase = {
  case_id: "elbow.nominal",
  evidence: null,
  conformance: {
    label: "physical conformance",
    verdict: "NOT_SCORED",
    exit_code: 3,
    scored: null,
    context: { case_id: "elbow.nominal", claim_id: "conformance", contract_revision: null,
      selected_evidence: null, evidence_class: "unverified", source_witnesses: [],
      uncertainty: "unknown", dependencies: [], freshness: "unverified",
      validation_reference: null, reference_held_out: null, calibration_artifacts: [], draft_binding: null },
    gaps: [],
  },
  simulation_validity: {
    label: "simulation validity",
    verdict: "GO",
    exit_code: 0,
    scored: 1,
    context: { case_id: "elbow.nominal", claim_id: "simulation_validity", contract_revision: null,
      selected_evidence: null, evidence_class: "unverified", source_witnesses: [],
      uncertainty: "unknown", dependencies: [], freshness: "unverified",
      validation_reference: null, reference_held_out: null, calibration_artifacts: [], draft_binding: null },
    gaps: [],
  },
};

const RUN_STATE = {
  status: "running" as const,
  exit_code: null,
  rows_written: 0,
  document: null,
  output: "",
  errors: "",
  error: null,
  health: {
    path: "/tmp/contract.health.jsonl",
    origin: { path: "/tmp/contract.yaml", copied_at: null },
    contract_sha: "abc",
    devices: [],
    foreign_rows: 0,
    series_excluded_foreign: 0,
    series_excluded_unplaceable: 0,
    not_watched: 0,
    scored: 0,
    chain: { rows: 0, skipped: 0, ok: true, line: null, why: null },
    summary: { last_run: null, watched: 0, worst: null, per_device: {}, skipped: 0 },
    verdict: "NOT_SCORED" as const,
    exit_code: 3,
    interface_poll_s: 1,
    nothing_watched: true,
    next_step: "record a run",
  },
};

const ANALYSIS_STATE: AnalysisJobStatePayload = {
  status: "completed",
  exit_code: 0,
  rows_written: 0,
  output: "",
  errors: "",
  error: null,
  document: {
    document_version: "1",
    kind: "analysis_result",
    status: "COMPLETED",
    solver_started: true,
    contract_revision: "a".repeat(64),
    procedure_id: "static",
    procedure_kind: "structural",
    runtime_profile: "calculix-linear-static",
    package_manifest_sha256: "b".repeat(64),
    solver: { backend: "calculix", version: "2.23", binary_sha256: "c".repeat(64) },
    deck_template: "direct-b31-square-v1",
    artifacts: [],
    physical_conformance: "NOT_SCORED",
    simulation_validity: "GO",
    output_sha256: "d".repeat(64),
    results: [{ kind: "U", node_set: "TIP", node: 5, values: [0, -0.1904, 0], unit: "m" }],
    acceptance_limit_id: "cantilever-tip",
    acceptance_provenance: { witness: "fixture reviewer", source: "declared reference", locked_at: "2026-09-28" },
    threshold: { value: 0.02, unit: "1" },
    analytic_operands: { span: { value: 1, unit: "m" } },
    comparisons: [{ metric: "b31_tip_uy", observed: { value: -0.1904, unit: "m" }, expected: { value: -0.190476, unit: "m" }, relative_error: 0.0004, passed: true }],
    reason: "declared analytic B31 comparison passed; physical conformance remains unscored",
  },
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
  process.env.ELECTRON_RENDERER_URL = "http://localhost:5173/";
  callSidecarMock.mockReset();
  showOpenDialog.mockReset();
});

describe("the paid IPC surface is exactly the named classes", () => {
  it("lists exactly the paid command classes, and each is dispatched", () => {
    expect([...PRO_COMMAND_CLASSES].sort()).toEqual([
      "analysis:cancel",
      "analysis:poll",
      "analysis:start",
      "contract:actuator-review-draft",
      "contract:preflight",
      "file:choose-recording",
      "run:cancel",
      "run:poll",
      "run:start",
    ]);
    for (const commandClass of PRO_COMMAND_CLASSES) expect(isPermittedCommand(commandClass)).toBe(true);
  });
});

describe("dispatch: Run controller", () => {
  it.each([
    ["run:start", { contract: "c", recording: "saved.mcap" }, "run-start"],
    ["run:start", { contract: "c", live: true, ssh_host: "127.0.0.1", ssh_port: 2222,
      ssh_user: "root", ssh_identity_file: "/keys/id", ssh_known_hosts: "/keys/known_hosts",
      ssh_target: "declared-robot", ssh_image: "declared-image", keep_bag: "/bags/partial" }, "run-start"],
    ["run:poll", undefined, "run-poll"],
    ["run:cancel", undefined, "run-cancel"],
  ])("validates %s replies before returning them", async (commandClass, args, sidecarCommand) => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(RUN_STATE)) });
    await expect(dispatch(TRUSTED_EVENT, commandClass, args)).resolves.toEqual(RUN_STATE);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: sidecarCommand, args: args ?? {} }),
    }));
  });

  it("rejects extra start fields and malformed RunStatePayload", async () => {
    await expect(dispatch(TRUSTED_EVENT, "run:start", { contract: "c", token: "x" })).rejects.toThrow("invalid arguments");
    await expect(dispatch(TRUSTED_EVENT, "run:start", { contract: "c", live: true, ssh_host: "127.0.0.1", ssh_port: 0 })).rejects.toThrow("invalid arguments");
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ status: "running" })) });
    await expect(dispatch(TRUSTED_EVENT, "run:poll", undefined)).rejects.toThrow("invalid payload");
  });
});

describe("dispatch: structural analysis controller", () => {
  it.each([
    ["analysis:start", { contract: "c", expected_revision: "a".repeat(64), procedure_id: "static", evidence_root: "/evidence", package_manifest: "/package.json" }, "analysis-start"],
    ["analysis:poll", undefined, "analysis-poll"],
    ["analysis:cancel", undefined, "analysis-cancel"],
  ])("validates %s and preserves the typed result", async (commandClass, args, sidecarCommand) => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(ANALYSIS_STATE)) });
    await expect(dispatch(TRUSTED_EVENT, commandClass, args)).resolves.toEqual(ANALYSIS_STATE);
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: sidecarCommand, args: args ?? {} }),
    }));
    expect(ANALYSIS_STATE.document?.physical_conformance).toBe("NOT_SCORED");
    expect(ANALYSIS_STATE.document?.simulation_validity).toBe("GO");
    expect(ANALYSIS_STATE.document).not.toHaveProperty("output_text");
  });

  it("rejects revision-missing starts, malformed state, and leaked raw solver text", async () => {
    await expect(dispatch(TRUSTED_EVENT, "analysis:start", { contract: "c", procedure_id: "static", evidence_root: "/e", package_manifest: "/m" })).rejects.toThrow("invalid arguments");
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ status: "running" })) });
    await expect(dispatch(TRUSTED_EVENT, "analysis:poll", undefined)).rejects.toThrow("invalid payload");
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ ...ANALYSIS_STATE, output: "raw solver text", errors: "raw solver stderr" })) });
    const redacted = await dispatch(TRUSTED_EVENT, "analysis:poll", undefined) as AnalysisJobStatePayload;
    expect(redacted.output).toBe("");
    expect(redacted.errors).toBe("");
    const leaked = structuredClone(ANALYSIS_STATE);
    (leaked.document as unknown as Record<string, unknown>).output_text = "raw CCX output";
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(leaked)) });
    await expect(dispatch(TRUSTED_EVENT, "analysis:poll", undefined)).rejects.toThrow("invalid payload");
  });
});

describe("dispatch: contract:actuator-review-draft", () => {
  const request = {
    contract: "/tmp/robot/lockwire.yaml",
    case_id: "elbow.nominal",
    evidence: { path: "runs/elbow.mcap", sha256: "a".repeat(64) },
    evidence_root: "/tmp/robot/evidence",
    expected_revision: "b".repeat(64),
    provenance: "bench capture",
    discrepancy: "tracking differs",
    alternatives: ["repeat capture"],
    proposed_target: "physical" as const,
    proposed_patch: "inspect linkage",
    rationale: "hash-bound evidence",
    correction_category: "physical" as const,
    required_recheck: ["rerun elbow.nominal"],
  };

  function draftPayload(): CheckPayload {
    return {
      format_version: "1", schema_version: "1", robot: "Test Robot", verdict: "NO-GO", exit_code: 1,
      counts: { violation: 1, warning: 0, not_scored: 0 }, scored: 1, findings: [],
      artifact_type: "lockwire-check-report", headline: "Draft needs review", forces: EMPTY_FORCES,
      actuator_cases: [ACTUATOR_CASE],
      actuator_review_draft: {
        base_raw_revision: request.expected_revision,
        discrepancy: request.discrepancy,
        evidence: [{ artifact: request.evidence, provenance: request.provenance, result_id: request.case_id, claim_id: "conformance" }],
        alternatives: request.alternatives,
        proposed_patch: { kind: request.proposed_target, patch: request.proposed_patch },
        rationale: request.rationale,
        correction_category: request.correction_category,
        affected_results: [{ result_id: request.case_id, claim_ids: ["conformance", "simulation_validity"] }],
        required_recheck: request.required_recheck,
      },
      actuator_review_next_step: "Engineer review required; this draft has not been applied.",
    };
  }

  it("routes one strict explicit request to the existing sidecar command", async () => {
    const payload = draftPayload();
    callSidecarMock.mockResolvedValueOnce({
      json: () => Promise.resolve(runReply(payload, "lockwire actuator-review-draft /tmp/robot/lockwire.yaml elbow.nominal")),
    });
    await expect(dispatch(TRUSTED_EVENT, "contract:actuator-review-draft", request)).resolves.toEqual({
      document: payload,
      cliLine: "lockwire actuator-review-draft /tmp/robot/lockwire.yaml elbow.nominal",
    });
    expect(callSidecarMock).toHaveBeenCalledWith("/v1/run", expect.objectContaining({
      body: JSON.stringify({ command: "actuator-review-draft", args: request }),
    }));
  });

  it("refuses unknown fields before the sidecar", async () => {
    await expect(dispatch(TRUSTED_EVENT, "contract:actuator-review-draft", { ...request, apply: true })).rejects.toThrow(
      "contract:actuator-review-draft: invalid arguments",
    );
    await expect(dispatch(TRUSTED_EVENT, "contract:actuator-review-draft", {
      ...request,
      evidence: { ...request.evidence, fabricated_association: true },
    })).rejects.toThrow("contract:actuator-review-draft: invalid arguments");
    expect(callSidecarMock).not.toHaveBeenCalled();
  });
});

describe("dispatch: preflight", () => {
  it("validates and returns the served preflight payload", async () => {
    const payload = {
      format_version: "2", schema_version: "1", robot: "X500", verdict: "GO", exit_code: 0,
      counts: { violation: 0, warning: 0, not_scored: 0 }, scored: 1, findings: [],
      artifact_type: "lockwire-preflight-report", source: "r.mcap", limits_raised: [],
    };
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply(payload, "lockwire preflight c r")) });
    await expect(dispatch(TRUSTED_EVENT, "contract:preflight", { contract: "c", recording: "r" })).resolves.toEqual(payload);
  });

  it("refuses an invalid contract:preflight payload before the renderer", async () => {
    callSidecarMock.mockResolvedValueOnce({ json: () => Promise.resolve(runReply({ robot: "X500" })) });
    await expect(dispatch(TRUSTED_EVENT, "contract:preflight", { contract: "c", recording: "r" })).rejects.toThrow("invalid payload");
  });
});

describe("dispatch: the recording chooser always uses the native dialog", () => {
  it("test_LOCKWIRE_E2E_env_cannot_fabricate_a_recording_choice", async () => {
    vi.stubEnv("LOCKWIRE_E2E_RECORDING_PATH", "/tmp/e2e/recording.ulg");
    try {
      showOpenDialog.mockResolvedValueOnce({ canceled: true, filePaths: [] });
      await expect(dispatch(TRUSTED_EVENT, "file:choose-recording", undefined)).resolves.toBeNull();
      expect(showOpenDialog).toHaveBeenCalledTimes(1);
      const [options] = showOpenDialog.mock.calls[0] as [OpenDialogOptions];
      expect(options.properties).toEqual(["openFile", "openDirectory"]);
      expect(options.filters?.[0]?.extensions).not.toContain("db3");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
