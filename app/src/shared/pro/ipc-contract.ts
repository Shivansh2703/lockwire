// Request shapes for the paid IPC classes (main/pro/, preload/pro/). Free code never imports this file.
/** One explicit, read-only request for the existing actuator review-draft engine. */
export type ContractActuatorReviewDraftRequest = {
  contract: string;
  case_id: string;
  evidence: { path: string; sha256: string };
  evidence_root: string;
  expected_revision: string;
  provenance: string;
  discrepancy: string;
  alternatives: string[];
  proposed_target: "physical" | "firmware" | "configuration" | "model" | "calibration";
  proposed_patch: string;
  rationale: string;
  correction_category: "physical" | "model_calibration" | "spec_design";
  required_recheck: string[];
};


export type PreflightRequest = { contract: string; recording: string; context?: string };

export type RunStartRequest = {
  contract: string;
  recording?: string | null;
  context?: string;
  live?: boolean;
  container?: string | null;
  local?: boolean;
  seconds?: number;
  ssh_host?: string | null;
  ssh_port?: number;
  ssh_user?: string;
  ssh_identity_file?: string | null;
  ssh_known_hosts?: string | null;
  ssh_target?: string | null;
  ssh_image?: string | null;
  keep_bag?: string | null;
};


/** One revision-bound request for the admitted structural analysis Job. */
export type AnalysisStartRequest = {
  contract: string;
  expected_revision: string;
  procedure_id: string;
  evidence_root: string;
  package_manifest: string;
};
