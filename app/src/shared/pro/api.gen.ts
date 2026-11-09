// GENERATED — do not edit by hand.
// Written next to `api.gen.ts` by the same `lockwire api-types` run, from the payload
// members a plugin registers. A stale committed file fails the build.


import type { AcceptedRow, AddsUpRow, BudgetRow, Card, Constraint, Counts, Faces, Finding, FindingLabel, ForceDocument, Graph, NotCompared, Provenance, Quantity, RecordComparison, RobotJoint, RobotLink, ScenePayload, SceneUnchanged, ScoredRecord, SetupGap } from "../api.gen";

export interface ActuatorCase {
  case_id: string;
  evidence: ArtifactRef | null;
  conformance: ActuatorClaim;
  simulation_validity: ActuatorClaim;
}

export interface ActuatorClaim {
  comparisons?: (ScoredRecord)[];
  label: "physical conformance" | "simulation validity";
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  scored: number | null;
  gaps: (Finding)[];
  context: ClaimContext;
}

export interface AnalysisJobStatePayload {
  status: "running" | "completed" | "failed" | "cancelled";
  exit_code: number | null;
  rows_written: number;
  document: AnalysisResultPayload | null;
  output: string;
  errors: string;
  error: string | null;
}

export interface AnalysisResultPayload {
  document_version: string;
  kind: "analysis_result";
  status: "COMPLETED";
  solver_started: true;
  contract_revision: string;
  procedure_id: string;
  procedure_kind: string;
  runtime_profile: string;
  package_manifest_sha256: string;
  solver: Record<string, unknown>;
  deck_template: string;
  artifacts: (Record<string, unknown>)[];
  physical_conformance: "NOT_SCORED";
  simulation_validity: "GO" | "NO_GO" | "NOT_SCORED";
  output_sha256: string;
  results: (Record<string, unknown>)[];
  acceptance_limit_id?: string | null;
  acceptance_provenance?: Provenance | null;
  threshold?: Quantity | null;
  analytic_operands?: Record<string, unknown> | null;
  comparisons?: (Record<string, unknown>)[];
  reason: string;
}

export interface ArtifactRef {
  path: string;
  sha256: string;
}

export interface CheckPayloadPro {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  robot: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  counts: Counts;
  scored: number | null;
  findings: (Finding)[];
  not_compared?: (NotCompared)[];
  artifact_type: "lockwire-check-report";
  scene?: ScenePayload | SceneUnchanged | null;
  headline: string;
  graph?: Graph | null;
  forces: ForceDocument;
  revision?: string | null;
  actuator_cases?: (ActuatorCase)[];
  actuator_review_draft?: ReviewDraftPayload | null;
  actuator_review_next_step?: string | null;
}

export interface ClaimContext {
  case_id: string;
  claim_id: "conformance" | "simulation_validity";
  contract_revision: string | null;
  selected_evidence: ArtifactRef | null;
  evidence_class: "unverified" | "physical_measurement" | "prediction_with_held_out_reference";
  source_witnesses: (ClaimSource)[];
  uncertainty: "unknown";
  dependencies: (ArtifactRef)[];
  freshness: "unverified" | "bound_to_revision";
  validation_reference: ArtifactRef | null;
  reference_held_out: boolean | null;
  calibration_artifacts: (ArtifactRef)[];
  draft_binding: ClaimDraftBinding | null;
}

export interface ClaimDraftBinding {
  base_raw_revision: string;
  result_id: string;
  claim_id: "conformance" | "simulation_validity";
  evidence: (ClaimDraftEvidence)[];
}

export interface ClaimDraftEvidence {
  artifact: ArtifactRef;
  provenance: string;
}

export interface ClaimSource {
  source: string;
  witness: string;
}

export interface HealthChain {
  rows: number;
  skipped: number;
  ok: boolean;
  line: number | null;
  why: string | null;
}

export interface HealthCoverage {
  applicable: number;
  compared: number;
  samples_observed: number | "unknown";
  newest_sample_age_s: number | null;
  capture: "absent" | "silent" | "captured";
  reason_code: string;
}

export interface HealthDevice {
  id: string;
  name?: string;
  kind: "device" | "rail" | "battery";
  state: "healthy" | "degrading" | "faulty" | "watched" | "not_watched" | "absent" | "stale";
  word: string;
  run: string | null;
  reason: string;
  runs: number;
  series: (HealthSeries)[];
  contract_sha: string | null;
  bag_sha: string | null;
  recording: string | null;
  coverage: HealthCoverage;
  drives_joint?: string | null;
  mount_frame?: string | null;
}

export interface HealthDocumentPayload {
  path: string;
  origin: HealthOrigin;
  contract_sha: string;
  devices: (HealthDevice)[];
  foreign_rows: number;
  series_excluded_foreign: number;
  series_excluded_unplaceable: number;
  not_watched: number;
  scored: number;
  chain: HealthChain;
  integrity?: HealthIntegrity | null;
  summary: HealthSummary;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  interface_poll_s: number;
  nothing_watched: boolean;
  next_step: string | null;
  next_action?: string | null;
}

export interface HealthIntegrity {
  ok: boolean;
  issues: (string)[];
}

export interface HealthOrigin {
  path: string;
  copied_at: string | null;
}

export interface HealthPayload {
  last_run: string | null;
  watched: number;
  worst: "healthy" | "degrading" | "faulty" | "stale" | "not_watched" | "absent" | null;
  per_device: Record<string, string>;
  skipped: number;
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  artifact_type: "lockwire-health-report";
}

export interface HealthSeries {
  channel: string;
  band: HealthSeriesBand | null;
  points: (HealthSeriesPoint)[];
}

export interface HealthSeriesBand {
  lo: number;
  hi: number;
  unit: string;
}

export interface HealthSeriesPoint {
  value: number;
  unit: string;
  run: string;
  run_id: string;
  kind: "measured";
}

export interface HealthSummary {
  last_run: string | null;
  watched: number;
  worst: "healthy" | "degrading" | "faulty" | "stale" | "not_watched" | "absent" | null;
  per_device: Record<string, string>;
  skipped: number;
}

export interface LimitRaisedRow {
  budget: string;
  mark: string;
}

export interface PagePayloadPro {
  robot: string;
  revision?: string | null;
  contract_id: string;
  state: "stop" | "warn" | "neutral" | "go";
  headline: string;
  qualifier: string;
  scored: number | null;
  violations: number;
  warnings: number;
  unchecked: number;
  adds_up: (AddsUpRow)[];
  cards: (Card)[];
  accepted?: (AcceptedRow)[];
  model_changed?: boolean;
  joints: (RobotJoint)[];
  links: (RobotLink)[];
  graph: Graph;
  budgets: (BudgetRow)[];
  constraints: (Constraint)[];
  setup: (SetupGap)[];
  scene: ScenePayload | null;
  modes: (string)[];
  mode_labels: Record<string, string>;
  command_modes: Record<string, string>;
  faces: Faces;
  cli_line: string | null;
  document_identity: string;
  run: RunSection | null;
  health: HealthSummary | null;
  health_words: Record<string, string>;
  health_devices: (Record<string, unknown>)[] | null;
}

export interface PassedRow {
  label: string;
  observed: string;
  expected: string;
}

export interface PreflightPayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  robot: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  counts: Counts;
  scored: number | null;
  findings: (Finding)[];
  not_compared?: (NotCompared)[];
  artifact_type: "lockwire-preflight-report";
  source: string;
  profile?: "analysis" | "powered-release";
  required_evidence?: ("watched-command-stream" | "live-hardware-software-reconciliation" | "comm-links-under-representative-load")[];
  missing_required_evidence?: ("watched-command-stream" | "live-hardware-software-reconciliation" | "comm-links-under-representative-load")[];
  scored_by_stage?: Record<string, number> | null;
  limits_raised: (LimitRaisedRow)[];
  finding_labels?: (FindingLabel)[];
  passed?: (PassedRow)[];
}

export interface RecordedHealth {
  baseline: (Record<string, unknown>)[];
  invalidated: (Record<string, unknown>)[];
}

export interface ReviewAffectedResult {
  result_id: string;
  claim_ids: (string)[];
}

export interface ReviewDraftPayload {
  base_raw_revision: string;
  discrepancy: string;
  evidence: (ReviewEvidenceCitation)[];
  alternatives: (string)[];
  proposed_patch: ReviewProposedPatch;
  rationale: string;
  correction_category: "physical" | "model_calibration" | "spec_design";
  affected_results: (ReviewAffectedResult)[];
  required_recheck: (string)[];
}

export interface ReviewEvidenceCitation {
  artifact: ArtifactRef;
  provenance: string;
  result_id: string;
  claim_id: string;
}

export interface ReviewProposedPatch {
  kind: "lockwire_spec" | "physical" | "firmware" | "configuration" | "model" | "calibration";
  patch: string;
}

export interface RunSection {
  recording: string;
  cards: (Card)[];
  stages: (StageRow)[];
  violations: number;
  silent: boolean;
}

export interface RunStatePayload {
  status: "running" | "completed" | "failed" | "cancelled";
  exit_code: number | null;
  rows_written: number;
  document: PreflightPayload | null;
  output: string;
  errors: string;
  error: string | null;
  health: HealthDocumentPayload;
  watching?: (string)[] | null;
}

export interface StageRow {
  stage: number;
  name: string;
  scored: number | null;
  violations: number;
  unchecked: number;
}

export interface WhatIfPayloadPro {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  artifact_type: "lockwire-what-if-report";
  current: RecordComparison;
  proposed: RecordComparison;
  diff: string;
  recorded_health: RecordedHealth;
}

export type MachineDocumentPlugin = PreflightPayload | HealthPayload;
