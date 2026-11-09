"""The paid half of the payload union: preflight, health, run state, analysis, and the
page fields only the paid app fills. Free members live in ``lockwire.api_payload``; this
module registers its own through ``plugins.PAYLOAD_*`` so ``emit_ts`` writes them to
``shared/pro/api.gen.ts``."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from lockwire import plugins
from lockwire.api_payload import (
    Card,
    CheckPayload,
    ForceDocument,  # noqa: F401  (the parent's forward references resolve here)
    Graph,  # noqa: F401
    PagePayload,
    ScenePayload,  # noqa: F401
    SceneUnchanged,  # noqa: F401
    WhatIfPayload,
    _build_kind_declaration,
)
from lockwire.plan_payload import FindingLabel, VerdictWord, _Document, _Strict, _VerdictDocument
from lockwire.report_json import (
    HEALTH_ARTIFACT_TYPE,
    ARTIFACT_TYPE as PREFLIGHT_ARTIFACT_TYPE,
)
from lockwire.report import Finding, ScoredRecord
from lockwire.review_draft import required_correction_category
from lockwire.schema import ArtifactRef, Provenance, Quantity
from lockwire_pro.preflight.report import EvidenceClass, PreflightProfile


class LimitRaisedRow(_Strict):
    budget: str
    mark: str


class PassedRow(_Strict):
    label: str
    observed: str
    expected: str


class PreflightPayload(_VerdictDocument):
    """``report_json.preflight_document`` — the go/no-go gate's document."""

    artifact_type: Literal["lockwire-preflight-report"]
    source: str
    profile: PreflightProfile = "analysis"
    required_evidence: list[EvidenceClass] = Field(default_factory=list)
    missing_required_evidence: list[EvidenceClass] = Field(default_factory=list)
    scored_by_stage: dict[str, int] | None = None
    # Every limit raised to clear a violation, as data. An
    # annotation only — `verdict`/`exit_code` above are untouched.
    limits_raised: list[LimitRaisedRow]
    finding_labels: list[FindingLabel] = []
    passed: list[PassedRow] = []  # sidecar-only: scored checks that agreed


class StageRow(_Strict):
    stage: int
    name: str
    scored: int | None
    violations: int
    unchecked: int


class RunSection(_Strict):
    recording: str
    cards: list[Card]
    stages: list[StageRow]
    violations: int
    silent: bool


# --------------------------------------------------------------------- health


class HealthSummary(_Strict):
    """``health.summary``'s dict — the page's `health` block and the health
    document's body are the same call, so they are the same model here.
    ``worst``/``last_run`` are None on an empty ledger, which ``api.health``
    can return (the page short-circuits to None first; the library does not)."""

    last_run: str | None
    watched: int
    worst: Literal["healthy", "degrading", "faulty", "stale", "not_watched", "absent"] | None
    per_device: dict[str, str]
    #: Corrupt/unparseable ledger lines `read_ledger` dropped before this
    #: summary was built (E3-A4-r2) — the count that used to reach nobody.
    skipped: int


class HealthPayload(_Document, HealthSummary):
    """``api.health`` — ``report_json.envelope`` merges ``health.summary``'s
    dict flat behind the header, so the member is literally the header plus
    ``HealthSummary``. Inheriting it is how the page's health block and the
    health document stay one shape."""

    artifact_type: Literal["lockwire-health-report"]


class HealthSeriesBand(_Strict):
    lo: float
    hi: float
    unit: str


class HealthSeriesPoint(_Strict):
    value: float
    unit: str
    run: str
    run_id: str
    kind: Literal["measured"]


class HealthSeries(_Strict):
    channel: str
    band: HealthSeriesBand | None
    points: list[HealthSeriesPoint]


class HealthCoverage(_Strict):
    applicable: int
    compared: int
    samples_observed: int | Literal["unknown"]
    newest_sample_age_s: float | None
    capture: Literal["absent", "silent", "captured"]
    reason_code: str


class HealthDevice(_Strict):
    id: str
    #: Plain part name for the row (copy.human_name); the id stays the tooltip.
    name: str = ""
    kind: Literal["device", "rail", "battery"]
    state: Literal["healthy", "degrading", "faulty", "watched", "not_watched", "absent", "stale"]
    word: str
    run: str | None
    reason: str
    runs: int
    series: list[HealthSeries]
    contract_sha: str | None
    bag_sha: str | None
    recording: str | None
    coverage: HealthCoverage
    drives_joint: str | None = (
        None  # the contract's devices[].drives_joint; the robot view lights this joint
    )
    mount_frame: str | None = (
        None  # devices[].physical.mount_frame; lit when the device has no mesh of its own
    )


class HealthChain(_Strict):
    rows: int
    skipped: int
    ok: bool
    line: int | None
    why: str | None


class HealthOrigin(_Strict):
    path: str
    copied_at: str | None


class HealthIntegrity(_Strict):
    ok: bool
    issues: list[str]


class HealthDocumentPayload(_Strict):
    """``api.health_document``'s JSON shape, consumed unchanged by Run."""

    path: str
    origin: HealthOrigin
    contract_sha: str
    devices: list[HealthDevice]
    foreign_rows: int
    series_excluded_foreign: int
    series_excluded_unplaceable: int
    not_watched: int
    scored: int
    chain: HealthChain
    integrity: HealthIntegrity | None = None
    summary: HealthSummary
    verdict: VerdictWord
    exit_code: int
    interface_poll_s: float
    nothing_watched: bool
    next_step: str | None
    next_action: str | None = None


class RunStatePayload(_Strict):
    """One ``RunJobController`` snapshot, including its freshly read health."""

    status: Literal["running", "completed", "failed", "cancelled"]
    exit_code: int | None
    rows_written: int
    document: PreflightPayload | None
    output: str
    errors: str
    error: str | None
    health: HealthDocumentPayload
    # Live runs only: ids of the devices the capture records (those declaring a driver topic).
    watching: list[str] | None = None


class AnalysisResultPayload(_Strict):
    """Strict outer shape for a completed analysis Job after digest validation.

    Nested solver/result records are the analysis producer's existing records;
    declared threshold and provenance reuse the contract's canonical models.
    """

    document_version: str
    kind: Literal["analysis_result"]
    status: Literal["COMPLETED"]
    solver_started: Literal[True]
    contract_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    procedure_id: str
    procedure_kind: str
    runtime_profile: str
    package_manifest_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    solver: dict[str, Any]
    deck_template: str
    artifacts: list[dict[str, Any]]
    physical_conformance: Literal["NOT_SCORED"]
    simulation_validity: Literal["GO", "NO_GO", "NOT_SCORED"]
    output_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    results: list[dict[str, Any]]
    acceptance_limit_id: str | None = None
    acceptance_provenance: Provenance | None = None
    threshold: Quantity | None = None
    analytic_operands: dict[str, Any] | None = None
    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    reason: str

    @model_validator(mode="after")
    def _go_requires_finite_passing_comparisons(self) -> "AnalysisResultPayload":
        """A simulated GO is evidence, rather than an empty result label."""
        if self.simulation_validity != "GO":
            return self

        def finite_number(value: Any) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )

        def measurement(value: Any) -> bool:
            if not isinstance(value, dict) or not isinstance(value.get("unit"), str):
                return False
            if "value" in value:
                return finite_number(value["value"])
            values = value.get("values")
            return (
                isinstance(values, list)
                and bool(values)
                and all(finite_number(item) for item in values)
            )

        has_declared_limit = (
            bool(self.acceptance_limit_id)
            and self.acceptance_provenance is not None
            and self.threshold is not None
        )
        comparisons_are_passing = bool(self.comparisons) and all(
            isinstance(comparison.get("metric"), str)
            and bool(comparison["metric"].strip())
            and comparison.get("passed") is True
            and measurement(comparison.get("observed"))
            and measurement(comparison.get("expected"))
            and finite_number(comparison.get("relative_error"))
            and comparison["relative_error"] >= 0
            for comparison in self.comparisons
        )
        if not has_declared_limit or not comparisons_are_passing:
            raise ValueError(
                "GO analysis result requires a declared limit and finite passing comparisons"
            )
        return self


class AnalysisJobStatePayload(_Strict):
    """Public state for a cancellable structural Job; prep remains distinct."""

    status: Literal["running", "completed", "failed", "cancelled"]
    exit_code: int | None
    rows_written: int
    document: AnalysisResultPayload | None
    output: str
    errors: str
    error: str | None


class PagePayloadPro(PagePayload):
    """``PagePayload`` plus the fields only the paid page fills."""

    run: RunSection | None
    health: HealthSummary | None
    #: `health.WORDS` with `{days}` filled from `health.STALE_DAYS` — the page's
    #: JS reads this rather than carrying its own copy.
    health_words: dict[str, str]
    health_devices: list[dict[str, Any]] | None


class ClaimSource(_Strict):
    source: str
    witness: str


class ClaimDraftEvidence(_Strict):
    artifact: ArtifactRef
    provenance: str


class ClaimDraftBinding(_Strict):
    base_raw_revision: str
    result_id: str
    claim_id: Literal["conformance", "simulation_validity"]
    evidence: list[ClaimDraftEvidence]


class ClaimContext(_Strict):
    """The admitted identity behind one verdict, never borrowed from its sibling."""

    case_id: str
    claim_id: Literal["conformance", "simulation_validity"]
    contract_revision: str | None
    selected_evidence: ArtifactRef | None
    evidence_class: Literal[
        "unverified", "physical_measurement", "prediction_with_held_out_reference"
    ]
    source_witnesses: list[ClaimSource]
    uncertainty: Literal["unknown"]
    dependencies: list[ArtifactRef]
    freshness: Literal["unverified", "bound_to_revision"]
    validation_reference: ArtifactRef | None
    reference_held_out: bool | None
    calibration_artifacts: list[ArtifactRef]
    draft_binding: ClaimDraftBinding | None

    @model_validator(mode="after")
    def _binding_is_admitted(self) -> "ClaimContext":
        if self.freshness == "bound_to_revision" and (
            self.contract_revision is None
            or self.selected_evidence is None
            or not self.dependencies
            or self.evidence_class == "unverified"
        ):
            raise ValueError(
                "a fresh actuator claim requires its raw revision and admitted evidence"
            )
        if self.evidence_class == "physical_measurement" and self.claim_id != "conformance":
            raise ValueError("physical measurement belongs only to conformance")
        if self.evidence_class == "prediction_with_held_out_reference" and (
            self.claim_id != "simulation_validity"
            or self.validation_reference is None
            or self.reference_held_out is not True
        ):
            raise ValueError("simulation claim requires a held-out reference identity")
        return self


class ActuatorClaim(_Strict):
    comparisons: list[ScoredRecord] = Field(default_factory=list)
    label: Literal["physical conformance", "simulation validity"]
    verdict: VerdictWord
    exit_code: int
    scored: int | None
    gaps: list[Finding]
    context: ClaimContext


class ActuatorCase(_Strict):
    case_id: str
    evidence: ArtifactRef | None
    conformance: ActuatorClaim
    simulation_validity: ActuatorClaim


class ReviewEvidenceCitation(_Strict):
    artifact: ArtifactRef
    provenance: str
    result_id: str
    claim_id: str


class ReviewAffectedResult(_Strict):
    result_id: str
    claim_ids: list[str]


class ReviewProposedPatch(_Strict):
    kind: Literal["lockwire_spec", "physical", "firmware", "configuration", "model", "calibration"]
    patch: str


class ReviewDraftPayload(_Strict):
    """The accepted review-draft core's stable, non-applying document."""

    base_raw_revision: str
    discrepancy: str
    evidence: list[ReviewEvidenceCitation]
    alternatives: list[str]
    proposed_patch: ReviewProposedPatch
    rationale: str
    correction_category: Literal["physical", "model_calibration", "spec_design"]
    affected_results: list[ReviewAffectedResult]
    required_recheck: list[str]

    @model_validator(mode="after")
    def _citations_are_distinct(self) -> "ReviewDraftPayload":
        required = required_correction_category(self.proposed_patch.kind)
        if required is None or self.correction_category != required:
            raise ValueError("actuator review category does not match its proposed change")
        keys = [
            (
                item.artifact.path,
                item.artifact.sha256,
                item.provenance,
                item.result_id,
                item.claim_id,
            )
            for item in self.evidence
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate actuator review citation")
        return self


class CheckPayloadPro(CheckPayload):
    """``CheckPayload`` plus the actuator-case reports and review draft only the paid check fills."""

    actuator_cases: list[ActuatorCase] = []
    actuator_review_draft: ReviewDraftPayload | None = None
    actuator_review_next_step: str | None = None

    @model_validator(mode="after")
    def _draft_binds_the_claims(self) -> "CheckPayloadPro":
        draft = self.actuator_review_draft
        citations: dict[tuple[str, str], list[ReviewEvidenceCitation]] = {}
        if draft is not None:
            for citation in draft.evidence:
                citations.setdefault((citation.result_id, citation.claim_id), []).append(citation)
        bound: set[tuple[str, str]] = set()
        for case in self.actuator_cases:
            for claim_id in ("conformance", "simulation_validity"):
                context = getattr(case, claim_id).context
                if context.case_id != case.case_id or context.claim_id != claim_id:
                    raise ValueError("actuator claim identity does not match its case and lane")
                binding = context.draft_binding
                claim_citations = citations.get((case.case_id, claim_id))
                if claim_citations is None:
                    if binding is not None:
                        raise ValueError("actuator claim has a draft binding without a citation")
                    continue
                bound.add((case.case_id, claim_id))
                expected_evidence = [
                    ClaimDraftEvidence(artifact=item.artifact, provenance=item.provenance)
                    for item in claim_citations
                ]
                if (
                    binding is None
                    or draft is None
                    or (
                        binding.base_raw_revision != draft.base_raw_revision
                        or binding.result_id != case.case_id
                        or binding.claim_id != claim_id
                        or binding.evidence != expected_evidence
                        or context.selected_evidence
                        not in [item.artifact for item in expected_evidence]
                        or context.contract_revision != draft.base_raw_revision
                    )
                ):
                    raise ValueError("actuator review draft is missing its claim identity binding")
        if bound != set(citations):
            raise ValueError("actuator review draft cites a claim outside this check")
        return self


class RecordedHealth(_Strict):
    """Existing ledger rows, before and after proposal dependency invalidation."""

    baseline: list[dict[str, Any]]
    invalidated: list[dict[str, Any]]


class WhatIfPayloadPro(WhatIfPayload):
    """``WhatIfPayload`` plus the recorded-health rows only the paid what-if reads."""

    recorded_health: RecordedHealth


PreflightPayload.model_rebuild()

NUMBER_KINDS: dict[str, dict[str, str]] = {
    "ActuatorClaim.exit_code": _build_kind_declaration("derived", "report.verdict"),
    "ActuatorClaim.scored": _build_kind_declaration("derived", "report.verdict"),
    "PreflightPayload.scored_by_stage": _build_kind_declaration(
        "derived", "preflight.runner.run_preflight"
    ),
    # --- the health document
    "HealthSummary.watched": _build_kind_declaration("derived", "health.summary"),
    "HealthSummary.skipped": _build_kind_declaration("derived", "health.read_ledger_with_skipped"),
    "HealthSeriesBand.lo": _build_kind_declaration("declared", "schema.Contract"),
    "HealthSeriesBand.hi": _build_kind_declaration("declared", "schema.Contract"),
    "HealthSeriesPoint.value": _build_kind_declaration("measured", "health.device_series"),
    "HealthCoverage.applicable": _build_kind_declaration("derived", "health._applicable_count"),
    "HealthCoverage.compared": _build_kind_declaration("derived", "health.build_document"),
    "HealthCoverage.samples_observed": _build_kind_declaration(
        "measured", "health.append_health_rows"
    ),
    "HealthCoverage.newest_sample_age_s": _build_kind_declaration("derived", "health.row_age"),
    "HealthDevice.runs": _build_kind_declaration("derived", "health.history"),
    "HealthChain.rows": _build_kind_declaration("derived", "health.read_ledger_lines"),
    "HealthChain.skipped": _build_kind_declaration("derived", "health.read_ledger_with_skipped"),
    "HealthChain.line": _build_kind_declaration("derived", "health.chain_break"),
    "HealthDocumentPayload.foreign_rows": _build_kind_declaration(
        "derived", "health.own_and_foreign"
    ),
    "HealthDocumentPayload.series_excluded_foreign": _build_kind_declaration(
        "derived", "health.own_and_foreign"
    ),
    "HealthDocumentPayload.series_excluded_unplaceable": _build_kind_declaration(
        "derived", "health.device_series"
    ),
    "HealthDocumentPayload.not_watched": _build_kind_declaration(
        "derived", "health.build_document"
    ),
    "HealthDocumentPayload.scored": _build_kind_declaration("derived", "health.build_document"),
    "HealthDocumentPayload.exit_code": _build_kind_declaration("derived", "report.verdict"),
    "HealthDocumentPayload.interface_poll_s": _build_kind_declaration(
        "declared", "health.INTERFACE_POLL_S"
    ),
    "RunStatePayload.exit_code": _build_kind_declaration("derived", "jobs.Job.exit_code"),
    "RunStatePayload.rows_written": _build_kind_declaration("derived", "jobs.Job.rows_written"),
    "AnalysisJobStatePayload.exit_code": _build_kind_declaration("derived", "jobs.Job.exit_code"),
    "AnalysisJobStatePayload.rows_written": _build_kind_declaration(
        "derived", "jobs.Job.rows_written"
    ),
    "StageRow.stage": _build_kind_declaration("declared", "schema.Contract (contract section 3.2)"),
    "StageRow.scored": _build_kind_declaration("derived", "preflight.runner.run_preflight"),
    "StageRow.violations": _build_kind_declaration("derived", "preflight.runner.run_preflight"),
    "StageRow.unchecked": _build_kind_declaration("derived", "preflight.runner.run_preflight"),
    "RunSection.violations": _build_kind_declaration("derived", "report.verdict"),
}

MEMBERS: dict[str, type[_Document]] = {
    PREFLIGHT_ARTIFACT_TYPE: PreflightPayload,
    HEALTH_ARTIFACT_TYPE: HealthPayload,
}

EXTRA: tuple[type, ...] = (
    PagePayloadPro,
    CheckPayloadPro,
    WhatIfPayloadPro,
    RunStatePayload,
    AnalysisResultPayload,
    AnalysisJobStatePayload,
)

ALL_MEMBERS: tuple[type, ...] = (*MEMBERS.values(), *EXTRA)


def register() -> None:
    plugins.PAYLOAD_MEMBERS.update(MEMBERS)
    plugins.PAYLOAD_EXTRA.extend(m for m in EXTRA if m not in plugins.PAYLOAD_EXTRA)
    plugins.PAYLOAD_NUMBER_KINDS.update(NUMBER_KINDS)
