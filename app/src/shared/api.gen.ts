// GENERATED — do not edit by hand.
// `uv run lockwire api-types --out app/src/shared/api.gen.ts` regenerates this
// file from `src/lockwire/api_payload.py`. CI regenerates and diffs on every run;
// a stale committed file fails the build (packaging/generate-types.md).


export interface AddPartNumber {
  key: string;
  value: number;
  unit: string;
  kind: "datasheet" | "source_file" | "assumed";
  source: string;
}

export interface AddPartOption {
  id: string;
  name: string;
  numbers: (AddPartNumber)[];
  fits: (string)[];
}

export interface AddPartPayload {
  rails: (string)[];
  parts: (AddPartOption)[];
}

export interface AddsUpRow {
  name: string;
  text: string;
  margin: string;
  raw: string;
  mark: string | null;
}

export interface Alternative {
  text: string;
  value: number;
  unit: string;
  kind: string;
  mark?: string | null;
  rail?: string | null;
  device?: string | null;
}

export interface AssertionOutcome {
  path: string;
  status: "PASS" | "FAIL" | "UNSCORED" | "ERROR";
}

export type BlamePayload = (BlameRow)[];

export interface BlameRow {
  path: string;
  value: Record<string, unknown>;
  source: string;
  witness: string;
  locked_at: string;
  compared_from?: string | null;
  last_changing_commit?: string | null;
}

export interface BudgetRow {
  id: string;
  kind: string;
  value: number | null;
  limit: number;
  unit: string;
  direction: string;
  state: string;
  note: string | null;
  source: string | null;
  witness: string | null;
  locked_at: string | null;
  limit_raised: string | null;
}

export interface Card {
  severity: "violation" | "warning" | "not_scored";
  tag: string;
  title: string;
  body: string;
  fix: string;
  raw: string;
  hits: (string)[];
  faults: (string)[];
  affected: (string)[];
  targets: (Target)[];
  whole?: boolean;
  count: number;
  count_figure: Figure;
  observed: Figure | null;
  expected: Figure | null;
  alternatives: (Alternative)[];
  figure_note: string | null;
  shape: string;
  label: string;
  group_title: string;
  instead: (Instead)[];
  also_in_run: boolean;
  consumer?: (string)[] | null;
  quantity?: string | null;
  owner?: string | null;
}

export interface CheckPayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  robot: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  counts: Counts;
  scored: number | null;
  findings: (Finding)[];
  artifact_type: "lockwire-check-report";
  scene?: ScenePayload | SceneUnchanged | null;
  headline: string;
  graph?: Graph | null;
  forces: ForceDocument;
  revision?: string | null;
}

export interface Constraint {
  id: string;
  label: string;
  domain: "mechanical" | "electrical" | "data" | "thermal" | "health";
  used: number | null;
  limit: number | null;
  unit: string;
  margin_pct: number | null;
  status: "ok" | "warn" | "bad" | "unset";
  used_kind: "declared" | "measured" | "derived" | "catalog" | "projected" | null;
  limit_kind: "declared" | "measured" | "derived" | "catalog" | "projected" | null;
  parts: (string)[];
  components: (string)[];
}

export interface Counts {
  violation: number;
  warning: number;
  not_scored: number;
}

export interface DeclareFace {
  title: string;
  lines: (string)[];
}

export interface DeclareFormPayload {
  rows: (DeclareRow)[];
  witness: DeclareWitnessBox;
  revision?: string | null;
  add_part?: AddPartPayload;
}

export interface DeclareRow {
  path: string;
  label: string;
  group: string;
  short: string;
  unit: string | null;
  kind: "declared" | "measured" | "derived" | "catalog" | "projected";
  value: number | (number)[] | null;
  provenance: Provenance | null;
  part?: string | null;
}

export interface DeclareWitnessBox {
  prefill: string;
}

export interface DescribePayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  artifact_type: "lockwire-describe";
  robot: string;
  files: Record<string, string>;
}

export interface DesignFace {
  title: string;
  parts: (DesignFacePart)[];
  cli: string;
}

export interface DesignFacePart {
  id: string;
  rail: string;
  draw: Figure | null;
}

export interface DiffPayload {
  changed: boolean;
  changes: (FieldChangeRow)[];
  added: (EntityChangeRow)[];
  removed: (EntityChangeRow)[];
  findings_new: (FindingRefRow)[];
  findings_gone: (FindingRefRow)[];
}

export interface EditPayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  artifact_type: "lockwire-edit-result";
  contract_sha: string | null;
  revision?: string | null;
  contract_sha_error?: string | null;
  written: boolean;
  diff: string;
  lines: (string)[];
}

export interface EntityChangeRow {
  kind: string;
  id: string;
}

export interface Faces {
  design: DesignFace;
  declare: DeclareFace;
  plan: PlanFace;
  reference: ReferenceFace;
}

export interface FieldChangeRow {
  path: string;
  old: unknown;
  new: unknown;
  witness: string | null;
  source: string | null;
  limit_raised: boolean;
}

export interface Figure {
  value: number;
  unit: string;
  kind: "declared" | "measured" | "derived" | "catalog" | "projected";
  by: string;
  mark?: string | null;
}

export interface Finding {
  severity: "violation" | "warning" | "not_scored";
  quantity: string;
  observed: string;
  expected: string;
  observed_quantity?: Quantity | null;
  expected_quantity?: Quantity | null;
  delta?: string | null;
  dependent?: string | null;
  owner?: string | null;
  source?: string | null;
  witness?: string | null;
  locked_at?: string | null;
  action: string;
  cluster_root?: string | null;
  cluster_members?: (string)[];
  stage?: number | null;
  alternatives?: (Alternative)[];
}

export interface FindingLabel {
  label: string;
  targets: (Target)[];
  whole?: boolean;
  instead?: (Instead)[];
  fix?: string;
}

export interface FindingRefRow {
  severity: string;
  quantity: string;
}

export interface ForceDocument {
  gravity: ForceFigure;
  mass_total: ForceFigure;
  cg: ForceFigure;
  masses: (ForceFigure)[];
  mounts: (ForceFigure)[];
  mount_shapes: (ForceMountShape)[];
  not_scored: (ForceFigure)[];
}

export interface ForceFigure {
  value: number | (number)[] | null;
  unit: string;
  kind: string;
  not_scored_because?: string | null;
  id?: string | null;
  quantity?: string | null;
  complete?: boolean | null;
  limitation?: string | null;
  frame?: string | null;
  note?: string | null;
  reaction_n?: number | null;
  moment_nm?: number | null;
  span_axis?: string | null;
}

export interface ForceMountShape {
  id: string;
  kind: "plate" | "bracket" | "tube" | "custom-mesh";
  dimensions: ForceFigure;
  fastener_pattern: string | null;
  mesh_path: string | null;
}

export interface Graph {
  off: GraphLens;
  mechanical: GraphLens;
  power: GraphLens;
  data: GraphLens;
  health: GraphLens;
}

export interface GraphEdge {
  from: string;
  to: string;
  label: string;
}

export interface GraphLens {
  nodes: (GraphNode)[];
  edges: (GraphEdge)[];
  label: string;
  caption: string;
}

export interface GraphNode {
  id: string;
  label: string;
  sub: string;
  kind: "link" | "device" | "rail" | "source" | "host" | "bus";
  value?: number | null;
  limit?: number | null;
}

export interface Instead {
  text: string;
  kind: string;
  rail?: string | null;
  device?: string | null;
}

export interface LogEntry {
  sha: string;
  author: string;
  date: string;
  message: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  fact_diff: DiffPayload | null;
}

export type LogPayload = (LogEntry)[];

export interface PackedIndices {
  type: "u16" | "u32";
  data: string;
}

export interface PackedVectors {
  type: "i16";
  data: string;
  scale?: number | null;
  offset?: (number)[] | null;
}

export interface PagePayload {
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
}

export interface PlanFace {
  title: string;
  cli: string;
  outcomes: (AssertionOutcome)[] | null;
}

export interface PlanPayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  robot: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  counts: Counts;
  scored: number | null;
  findings: (Finding)[];
  artifact_type: "lockwire-plan-report";
  outcomes?: (AssertionOutcome)[] | null;
  steps?: (PlanStepPayload)[];
  finding_labels?: (FindingLabel)[];
}

export interface PlanStepPayload {
  path: string;
  status: "PASS" | "FAIL" | "UNSCORED" | "ERROR";
  not_scored_reason: string | null;
  targets?: (Target)[];
}

export interface Provenance {
  witness: string;
  source: string;
  locked_at: string;
  bag_sha256?: string | null;
  conditions?: Record<string, number> | null;
  note?: string | null;
  compared_from?: string | null;
  reviews?: (ReviewNote)[] | null;
}

export interface Quantity {
  value: number;
  unit: string;
  frame?: string | null;
}

export interface RecordComparison {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  robot: string;
  verdict: "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";
  exit_code: number;
  counts: Counts;
  scored: number | null;
  findings: (Finding)[];
  artifact_type: "lockwire-record-comparison";
  facts: (WhatIfFact)[];
  comparisons: (ScoredRecord)[];
}

export interface ReferenceDrawer {
  title: string;
  content: string;
  provenance: string;
  cli_line: string;
}

export interface ReferenceFace {
  title: string;
  devices: (ReferenceFaceDevice)[];
  drawers: (ReferenceDrawer)[];
  power_tree: string | null;
}

export interface ReferenceFaceDevice {
  id: string;
  name: string;
  text: string;
}

export interface ReviewNote {
  state: "approved" | "questioned";
  witness: string;
  at: string;
  note?: string | null;
}

export interface RobotJoint {
  name: string;
  type: "fixed" | "revolute" | "continuous" | "prismatic" | "floating" | "planar";
  parent: string;
  child: string;
  parent_name: string;
  child_name: string;
  position: (number)[];
  axis: (number)[];
  lower: number | null;
  upper: number | null;
  effort: number | null;
}

export interface RobotLink {
  name: string;
  position: (number)[];
}

export interface SceneGroup {
  id: string;
  label: string;
  kind: "device" | "rail" | "bus" | "link" | "other";
}

export interface SceneLabel {
  part: string;
  severity: string;
  title: string;
  observed: Figure | null;
  comparator: string;
  expected: Figure | null;
}

export interface ScenePart {
  id: string | null;
  node: string;
  name: string;
  state: "fault" | "warn" | "affected" | "ok" | "unchecked" | "plain";
  positions: PackedVectors;
  normals: PackedVectors | null;
  indices: PackedIndices;
  color: (number)[];
  translation: (number)[];
  lenses: ("off" | "mechanical" | "power" | "data" | "health")[];
  group: SceneGroup;
  mount_frame?: string | null;
}

export interface ScenePayload {
  asset_path?: string | null;
  asset_sha256?: string | null;
  expected_asset_sha256?: string | null;
  source: string;
  hash: string;
  parts: (ScenePart)[];
  labels: (SceneLabel)[];
  scene_only: (string)[];
  contract_only: (string)[];
  bounds: ((number)[])[];
  triangles: number;
  vertices: number;
  display_triangles: number;
  honesty: string;
}

export interface SceneUnchanged {
  unchanged: true;
  asset_path?: string | null;
  asset_sha256?: string | null;
  expected_asset_sha256?: string | null;
}

export interface ScoredRecord {
  quantity: string;
  observed: string;
  expected: string;
  margin: string;
  provenance?: Provenance | null;
  observed_quantity?: Quantity | null;
  expected_quantity?: Quantity | null;
  uncertainty?: Quantity | null;
  detail?: string | null;
}

export interface SetupGap {
  title: string;
  why: string;
  snippet: string;
}

export interface StartPayload {
  path: string;
  revision: string;
  written: boolean;
  note?: string | null;
}

export interface Starter {
  name: string;
  title: string;
  based_on: string;
}

export interface StartersPayload {
  starters: (Starter)[];
}

export interface Target {
  kind: "joint" | "link" | "device" | "rail";
  id: string;
  label: string;
  parts?: (string)[];
}

export interface WhatIfFact {
  quantity: string;
  kind: "finding" | "passed_comparison";
  severity: string | null;
  source: string | null;
  witness: string | null;
  provenance_state: "recorded" | "missing_evidence";
}

export interface WhatIfPayload {
  format_version: string;
  schema_version: string;
  cli_line?: string | null;
  artifact_type: "lockwire-what-if-report";
  current: RecordComparison;
  proposed: RecordComparison;
  diff: string;
}

export const VERDICT_EXIT_CODES = {
  "REFUSED": 2,
  "NO-GO": 1,
  "GO_WITH_NOTES": 0,
  "GO": 0,
  "NOT_SCORED": 3
} as const satisfies Record<CheckPayload["verdict"], number>;

export type MachineDocument = WhatIfPayload | CheckPayload | PlanPayload | DescribePayload | EditPayload;
