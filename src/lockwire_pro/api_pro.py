"""The complete library door: the free ``lockwire.api`` plus the paid operations.

``lockwire.api`` holds what the open-source package ships (load, check, plan,
describe, the edit family, history, what-if). This module adds what needs paid
code: preflight, health, recordings, jobs, the app's page and form payloads and
the actuator review draft. It re-exports every free name, so a caller of the
paid surface can use ``api_pro`` as the one complete API.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from lockwire import api as _api
from lockwire_pro.analysis import MAX_PACKAGE_MANIFEST_BYTES
from lockwire.api import (
    DOCUMENT_VERSION,
    _actuator_check_context,  # noqa: F401 - re-exported for callers and tests
    _actuator_claim_document,  # noqa: F401 - re-exported for callers and tests
    _selected_external_meshes,  # noqa: F401 - re-exported for callers and tests
    add,
    api_schema,  # noqa: F401 - re-exported
    api_types,  # noqa: F401 - re-exported
    blame,
    check,
    check_document,
    declare,
    declare_form,  # noqa: F401 - re-exported
    describe,
    diff,
    is_stale,
    load,
    load_scene,
    log,
    mount_shape,
    move,
    page,  # noqa: F401 - re-exported
    place,
    plan,
    reparent,
    review,
    rotate,
    start,
    upgrade,
    verdict_document,
)
from lockwire_pro.baseline import BaselineArtifact, BaselineFinding
from lockwire.edit import Change, EditProposal, split_path
from lockwire_pro.health import build_document as _health_document
from lockwire_pro.health import (
    own_and_foreign,
    read_ledger_with_skipped,
    sidecar_path_for,
    what_if_health,
)
from lockwire_pro.health import summary as _health_summary
from lockwire_pro.jobs import Job, JobCancelled, start_analysis_job, start_job
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording, load_saved_recording
from lockwire.review_draft import (
    AffectedResult,
    CorrectionCategory,
    EvidenceCitation,
    NonSpecProposal,
    NonSpecTarget,
    prepare_review_draft,
)
from lockwire_pro.preflight.report import EvidenceClass, PreflightProfile, preflight_policy
from lockwire.report import ScoredRecord
from lockwire.report_json import HEALTH_ARTIFACT_TYPE, envelope, preflight_document
from lockwire_pro.ros2bag import load_ros2_bag
from lockwire.scan import CommandRunner
from lockwire.schema import (
    ArtifactRef,
    Contract,
    _guard_contract_raw,
    load_contract_bytes,
)
from lockwire_pro.ulog import load_ulog

#: Every library operation, free and paid. Each has a CLI command of the same name
#: (``tests/test_api_library.py``).
FUNCTIONS = (
    "start",
    "load",
    "check",
    "plan",
    "describe",
    "preflight",
    "health",
    "declare",
    "review",
    "diff",
    "log",
    "blame",
    "what-if",
    "add",
    "api-schema",
    "api-types",
    "move",
    "rotate",
    "reparent",
    "place",
    "mount-shape",
    "actuator-review-draft",
    "upgrade",
)

#: Plan v8's nine read operations are all available through the public library.
PENDING: tuple[str, ...] = ()

__all__ = [
    "DOCUMENT_VERSION",
    "add",
    "blame",
    "check",
    "check_document",
    "declare",
    "describe",
    "diff",
    "is_stale",
    "load",
    "load_scene",
    "log",
    "mount_shape",
    "move",
    "place",
    "plan",
    "reparent",
    "review",
    "rotate",
    "start",
    "upgrade",
    "verdict_document",
    "FUNCTIONS",
    "PENDING",
    "Job",
    "JobCancelled",
    "RunJobController",
    "actuator_review_draft",
    "api_schema",
    "api_types",
    "health",
    "health_document",
    "preflight",
    "start_job",
]


def what_if(contract: Path | str, changes: list[Change]) -> dict[str, Any]:
    """``api.what_if`` with the recorded-health ledger rows and what the proposal invalidates."""
    return _api.what_if(contract, changes, **what_if_health(contract))


# ``start_job`` is re-exported, not listed in ``FUNCTIONS``: FUNCTIONS is the set
# of library operations that must each have a CLI command of the same name
# (``test_every_library_function_has_a_cli_command``), and there is no ``lockwire
# start-job``. It is not a tenth document read — it is the way to run two of the
# existing nine without blocking the caller for the length of the run.
# ``jobs.py`` holds it.


class RunJobController:
    """One app-owned preflight job, with fresh health on every response."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job: Job | None = None
        self._contract: Path | None = None
        self._job_mode: str | None = None
        self._watching: list[str] | None = None
        self._analysis_expected_revision: str | None = None
        self._analysis_procedure_id: str | None = None
        self._analysis_package_manifest_sha256: str | None = None

    def start(
        self,
        contract: Path | str,
        *,
        recording: Path | str | None = None,
        context: str = "nominal-op",
        profile: PreflightProfile = "analysis",
        required_evidence: list[EvidenceClass] | None = None,
        live: bool = False,
        container: str | None = None,
        local: bool = False,
        seconds: float = 10.0,
        ssh_host: str | None = None,
        ssh_port: int = 22,
        ssh_user: str = "root",
        ssh_identity_file: Path | str | None = None,
        ssh_known_hosts: Path | str | None = None,
        ssh_target: str | None = None,
        ssh_image: str | None = None,
        keep_bag: Path | str | None = None,
    ) -> dict[str, Any]:
        profile, required = preflight_policy(profile, required_evidence)
        policy_args = (
            {"profile": profile, "required_evidence": required}
            if (profile != "analysis" or required)
            else {}
        )
        with self._lock:
            if self._job is not None and self._job.exit_code is None:
                raise RuntimeError("a Run job is already running")
            contract_path = Path(contract)
            job = start_job(
                "preflight",
                contract=contract_path,
                recording=recording,
                context=context,
                **policy_args,
                live=live,
                container=container,
                local=local,
                seconds=seconds,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                ssh_identity_file=ssh_identity_file,
                ssh_known_hosts=ssh_known_hosts,
                ssh_target=ssh_target,
                ssh_image=ssh_image,
                keep_bag=keep_bag,
            )
            self._contract = contract_path
            self._job = job
            self._job_mode = "run"
            # A live capture records exactly the devices that declare a driver topic
            # (preflight/source.py _capture_once); a recording's devices are unknown until read.
            self._watching = (
                [
                    d.id
                    for d in load(contract_path).devices
                    if d.driver is not None and d.driver.topic
                ]
                if live
                else None
            )
            self._analysis_expected_revision = None
            self._analysis_procedure_id = None
            self._analysis_package_manifest_sha256 = None
            return self.poll()

    def start_analysis(
        self,
        contract: Path | str,
        *,
        expected_revision: str,
        procedure_id: str,
        evidence_root: Path | str,
        package_manifest_path: Path | str,
    ) -> dict[str, Any]:
        """Start the existing admitted structural analysis as the app's one Job."""
        with self._lock:
            if self._job is not None and self._job.exit_code is None:
                raise RuntimeError("a Run or analysis job is already running")
            manifest_path = Path(package_manifest_path)
            with manifest_path.open("rb") as manifest:
                manifest_bytes = manifest.read(MAX_PACKAGE_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_PACKAGE_MANIFEST_BYTES:
                raise RuntimeError("analysis package manifest exceeds the size limit")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            job = start_analysis_job(
                Path(contract),
                expected_revision=expected_revision,
                procedure_id=procedure_id,
                evidence_root=Path(evidence_root),
                package_manifest_path=manifest_path,
            )
            self._contract = None
            self._job = job
            self._job_mode = "analysis"
            self._analysis_expected_revision = expected_revision
            self._analysis_procedure_id = procedure_id
            self._analysis_package_manifest_sha256 = manifest_sha256
            return self.poll_analysis()

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None or self._contract is None or self._job_mode != "run":
                raise RuntimeError("no Run job has been started")
            state = {**self._job.snapshot(), "health": health_document(self._contract)}
            return state if self._watching is None else {**state, "watching": self._watching}

    def poll_analysis(self) -> dict[str, Any]:
        with self._lock:
            if (
                self._job is None
                or self._job_mode != "analysis"
                or self._analysis_expected_revision is None
                or self._analysis_procedure_id is None
                or self._analysis_package_manifest_sha256 is None
            ):
                raise RuntimeError("no analysis job has been started")
            state = self._job.snapshot()
            document = state["document"]
            if document is not None:
                if document.get("kind") != "analysis_result":
                    raise RuntimeError("analysis job did not produce a completed structural result")
                if document.get("contract_revision") != self._analysis_expected_revision:
                    state["status"] = "failed"
                    state["document"] = None
                    state["error"] = (
                        "analysis result contract revision does not match the launched revision"
                    )
                    return state
                if document.get("procedure_id") != self._analysis_procedure_id:
                    state["status"] = "failed"
                    state["document"] = None
                    state["error"] = (
                        "analysis result procedure does not match the launched procedure"
                    )
                    return state
                if (
                    document.get("package_manifest_sha256")
                    != self._analysis_package_manifest_sha256
                ):
                    state["status"] = "failed"
                    state["document"] = None
                    state["error"] = (
                        "analysis result package manifest does not match the launched bytes"
                    )
                    return state
                from lockwire_pro.api_payload_pro import AnalysisResultPayload

                try:
                    AnalysisResultPayload.model_validate(
                        {key: value for key, value in document.items() if key != "output_text"}
                    )
                except ValueError as error:
                    state["status"] = "failed"
                    state["document"] = None
                    state["error"] = f"analysis result payload is invalid: {error}"
                    return state
                state["document"] = {
                    key: value for key, value in document.items() if key != "output_text"
                }
            return state

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None:
                raise RuntimeError("no Run job has been started")
            if self._job.exit_code is None:
                self._job.cancel()
            return self.poll() if self._job_mode == "run" else self.poll_analysis()

    def cancel_analysis(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None or self._job_mode != "analysis":
                raise RuntimeError("no analysis job has been started")
            if self._job.exit_code is None:
                self._job.cancel()
            return self.poll_analysis()


def open_source(recording: Path) -> RecordedSource:
    """Load a recording directory or PX4 ``.ulg`` the same way ``preflight`` does."""
    is_ulog = recording.is_file() and recording.suffix == ".ulg"
    if not recording.exists() or (not recording.is_dir() and not is_ulog):
        raise FileNotFoundError(f"no recording at {recording}")
    return RecordedSource(load_ulog(recording) if is_ulog else load_recording(recording))


def open_recording(
    recording: Path, contract: Contract, context: str = "nominal-op"
) -> RecordedSource:
    """Load a recording (directory, .ulg, or .mcap) the same way preflight does.

    Supports lockwire recording directories, PX4 .ulg files, and rosbag2 .mcap files
    (single file or bag directory with .mcap parts). Raises FileNotFoundError with a
    plain path if the recording does not exist or is not a recognized format.
    """
    is_ulog = recording.is_file() and recording.suffix == ".ulg"
    is_bag = (recording.is_file() and recording.suffix == ".mcap") or (
        recording.is_dir() and any(recording.glob("*.mcap"))
    )
    if not recording.exists() or (not recording.is_dir() and not is_ulog and not is_bag):
        raise FileNotFoundError(f"no recording at {recording}")

    if is_ulog:
        loaded_recording = load_ulog(recording, context=context)
    elif is_bag:
        loaded_recording = load_ros2_bag(recording, contract, context=context)
    else:
        loaded_recording = load_recording(recording)
    return RecordedSource(loaded_recording)


def actuator_review_draft(
    contract: Path | str,
    case_id: str,
    *,
    evidence: ArtifactRef | None = None,
    evidence_root: Path | str | None = None,
    expected_revision: str | None = None,
    provenance: str = "",
    discrepancy: str = "",
    alternatives: Iterable[str] = (),
    proposed_target: NonSpecTarget | None = None,
    proposed_patch: str = "",
    proposed_changes: Iterable[Change] | None = None,
    rationale: str = "",
    correction_category: CorrectionCategory | None = None,
    required_recheck: Iterable[str] = (),
) -> dict[str, Any]:
    """Prepare one evidence-bound actuator correction for engineer review.

    It calls the existing Check carrier once, carries both of that carrier's
    claim verdicts unchanged, and delegates draft validation to
    ``review_draft.prepare_review_draft``. An authored ``proposed_changes``
    list uses the existing validated Lockwire spec proposal for this case.
    Neither path can apply, write, flash, configure, or fabricate a recording.

    Omitting ``evidence`` is a useful read-only state, not malformed input: the
    returned carrier case retains its two named ``NOT_SCORED`` evidence gaps and
    ``draft`` remains ``None``.
    """
    from lockwire_pro.api_payload_pro import CheckPayloadPro as CheckPayload

    if evidence is None:
        checked = check(contract)
    else:
        if evidence_root is None or expected_revision is None:
            raise ValueError(
                "actuator review evidence requires evidence_root and expected_revision"
            )
        checked = check(
            contract,
            actuator_evidence={case_id: evidence},
            evidence_root=evidence_root,
            expected_revision=expected_revision,
        )

    selected = [item for item in checked["actuator_cases"] if item["case_id"] == case_id]
    if len(selected) != 1:
        raise ValueError(f"no actuator case {case_id!r} in {contract}")
    revision = checked["revision"]
    if not isinstance(revision, str):
        raise ValueError("actuator review drafting requires a path-backed contract")

    if evidence is None:
        checked["actuator_review_draft"] = None
        checked["actuator_review_next_step"] = (
            "Select hash-bound actuator evidence before preparing a correction draft."
        )
    else:
        if correction_category is None:
            raise ValueError("correction_category is required")
        assert evidence_root is not None and expected_revision is not None
        expected_class = {
            "physical": ("conformance", "physical_measurement"),
            "model_calibration": (
                "simulation_validity",
                "prediction_with_held_out_reference",
            ),
        }.get(correction_category)
        if expected_class is not None:
            claim_id, evidence_class = expected_class
            context = selected[0][claim_id]["context"]
            if (
                context["case_id"] != case_id
                or context["claim_id"] != claim_id
                or context["contract_revision"] != expected_revision
                or context["freshness"] != "bound_to_revision"
                or context["evidence_class"] != evidence_class
            ):
                raise ValueError(
                    f"{correction_category} correction requires admitted {claim_id} evidence"
                )
        if proposed_changes is not None:
            if proposed_target is not None or proposed_patch:
                raise ValueError("spec changes cannot be combined with a non-spec proposal")
            changes = list(proposed_changes)
            if any(not isinstance(change, Change) for change in changes):
                raise ValueError("proposed_changes must contain Change values")
            for change in changes:
                segments = split_path(change.path)
                if (
                    segments[:2] != ["actuator_cases", case_id]
                    or len(segments) < 3
                    or segments[2] == "id"
                ):
                    raise ValueError("spec changes must address the selected actuator case")
            proposal: list[Change] | NonSpecProposal = changes
        else:
            if proposed_target is None:
                raise ValueError("proposed_target or proposed_changes is required")
            proposal = NonSpecProposal(proposed_target, proposed_patch)
        if correction_category == "spec_design":
            if not any(
                selected[0][claim_id]["context"]["freshness"] == "bound_to_revision"
                for claim_id in ("conformance", "simulation_validity")
            ):
                raise ValueError("spec_design correction requires admitted actuator evidence")
        draft = prepare_review_draft(
            contract,
            base_raw_revision=expected_revision,
            discrepancy=discrepancy,
            evidence=(
                EvidenceCitation(evidence, provenance, case_id, "conformance"),
                EvidenceCitation(evidence, provenance, case_id, "simulation_validity"),
            ),
            alternatives=tuple(alternatives),
            proposed_patch=proposal,
            rationale=rationale,
            correction_category=correction_category,
            affected_results=(AffectedResult(case_id, ("conformance", "simulation_validity")),),
            required_recheck=tuple(required_recheck),
            evidence_root=evidence_root,
        )
        if isinstance(draft.proposed_patch, EditProposal) and not any(
            case.id == case_id for case in draft.proposed_patch.contract.actuator_cases
        ):
            raise ValueError("spec proposal must preserve the selected actuator case identity")
        draft_document = json.loads(draft.to_json())
        checked["actuator_review_draft"] = draft_document
        for claim_id in ("conformance", "simulation_validity"):
            citations = [
                item
                for item in draft_document["evidence"]
                if item["result_id"] == case_id and item["claim_id"] == claim_id
            ]
            selected[0][claim_id]["context"]["draft_binding"] = {
                "base_raw_revision": draft_document["base_raw_revision"],
                "result_id": case_id,
                "claim_id": claim_id,
                "evidence": [
                    {"artifact": item["artifact"], "provenance": item["provenance"]}
                    for item in citations
                ],
            }
        checked["actuator_review_next_step"] = (
            "Engineer review required; this draft has not been applied."
        )
    return CheckPayload.model_validate(checked).model_dump(mode="json")


def preflight(
    contract: Path | str | Contract,
    recording: Path | str,
    *,
    context: str = "nominal-op",
    profile: PreflightProfile | None = None,
    required_evidence: list[EvidenceClass] | None = None,
    saved_manifest: ArtifactRef | None = None,
    evidence_root: Path | str | None = None,
    selected_target_runner: CommandRunner | None = None,
    passed_out: list[ScoredRecord] | None = None,
) -> dict[str, Any]:
    """Run the go/no-go gate, optionally replaying a caller-pinned saved Test.

    An ordinary open retains its existing behavior. A saved replay requires an
    externally supplied manifest hash and a runner for the selected target.
    Its declared target identity must match the target's own identity document.
    """
    if (saved_manifest is None) != (evidence_root is None):
        raise ValueError("saved Test needs both manifest reference and evidence root")
    if saved_manifest is None and selected_target_runner is not None:
        raise ValueError("selected target runner is only valid for saved Test")
    if saved_manifest is not None and isinstance(contract, Contract):
        raise ValueError("saved Test needs a contract path to verify its exact bytes")
    contract_bytes = None
    if saved_manifest is not None:
        _guard_contract_raw(Path(contract))
        contract_bytes = Path(contract).read_bytes()
    if contract_bytes is not None:
        loaded = load_contract_bytes(contract_bytes)
    else:
        loaded = contract if isinstance(contract, Contract) else load(contract)
    if saved_manifest is None:
        source = open_recording(Path(recording), loaded, context=context)
        report = run_preflight(
            loaded,
            source,
            profile=profile if profile is not None else "analysis",
            required_evidence=required_evidence,
        )
        if (
            passed_out is not None
        ):  # the checks that agreed; the document itself stays as the CLI writes it
            passed_out.extend(report.scored_records)
        return preflight_document(loaded, report, str(recording))

    assert evidence_root is not None and contract_bytes is not None
    if selected_target_runner is None:
        raise ValueError("saved Test needs a selected target runner")
    if Path(contract).read_bytes() != contract_bytes:
        raise ValueError("saved Test contract changed while it was parsed")
    contract_hash = hashlib.sha256(contract_bytes).hexdigest()
    saved_policy: dict[str, Any] = {}
    saved, expected_output = load_saved_recording(
        Path(recording),
        manifest_ref=saved_manifest,
        evidence_root=Path(evidence_root),
        contract_sha256=contract_hash,
        selected_target_runner=selected_target_runner,
        policy_out=saved_policy,
    )
    chosen_profile, chosen_required = preflight_policy(
        profile if profile is not None else saved_policy["profile"],
        required_evidence if required_evidence is not None else saved_policy["required_evidence"],
    )
    if (chosen_profile, chosen_required) != (
        saved_policy["profile"],
        saved_policy["required_evidence"],
    ):
        raise ValueError(
            "saved Test policy differs from the requested profile or required evidence"
        )
    document = preflight_document(
        loaded,
        run_preflight(
            loaded, RecordedSource(saved), profile=chosen_profile, required_evidence=chosen_required
        ),
        str(recording),
    )
    output = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(output).hexdigest() != expected_output:
        # Only genuinely policy-absent manifests may retain their original
        # analysis output bytes. A powered manifest always binds all fields.
        legacy = {
            key: value
            for key, value in document.items()
            if key
            not in {
                "profile",
                "required_evidence",
                "missing_required_evidence",
            }
        }
        legacy_output = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if not (
            saved_policy["legacy"]
            and chosen_profile == "analysis"
            and not chosen_required
            and hashlib.sha256(legacy_output).hexdigest() == expected_output
        ):
            raise ValueError("saved Test output differs from the manifest")
    if Path(contract).read_bytes() != contract_bytes:
        raise ValueError("saved Test contract changed while it was evaluated")
    return document


def health(
    contract: Path | str, *, now: dt.datetime | None = None, ledger: Path | str | None = None
) -> dict[str, Any]:
    """The health ledger's summary: what ``lockwire health --json`` writes.

    ``health.summary`` is already the one source of truth the CLI and the page
    share; this reads the same sidecar, scopes it with the same one
    predicate (``health.own_and_foreign`` — rows scored against another contract
    version are history, never current state), and hands back the same dict
    behind the same envelope the CLI writes (E0-25), so the two documents stay
    the one document ``test_health_document_is_the_summary_the_cli_writes``
    says they are.

    ``ledger`` reads a ledger copied off the robot to anywhere on disk
    instead of the contract's own sidecar.
    """
    contract_path = Path(contract)
    ledger_path = Path(ledger) if ledger is not None else sidecar_path_for(contract_path)
    rows, skipped = read_ledger_with_skipped(ledger_path)
    own, _foreign = own_and_foreign(rows, contract_path)
    return envelope(
        HEALTH_ARTIFACT_TYPE,
        _health_summary(own, now or dt.datetime.now(dt.UTC), skipped=skipped),
    )


def health_document(
    contract: Path | str,
    *,
    now: dt.datetime | None = None,
    ledger: Path | str | None = None,
    health_anchor: tuple[int, str] | None = None,
    bundle_dependencies: tuple[ArtifactRef, ...] | None = None,
    evidence_root: Path | str | None = None,
) -> dict[str, Any]:
    """The whole health document the CLI renders and the MCP `health` tool serves.

    Everything ``lockwire health`` shows, decided once in ``health.build_document``
    (one evaluator, every surface calls it): the per-device rows with their state and reason, the foreign-row
    count kept as history, the chain-verify result, ``not_watched``, ``scored``,
    the summary, and the verdict with its exit code. No door re-derives any
    of it. ``health()`` above stays the narrower ``--json`` summary, which is one
    field of this document.

    ``ledger`` opens a ledger copied off the robot to anywhere on disk —
    the document's ``origin`` field then names that path and its copy time, and
    the chain is walked the same as for the contract's own sidecar.

    Not in ``FUNCTIONS``: that tuple asserts a CLI command of the same name and
    there is no ``lockwire health_document`` — this is the ``health`` command's
    full document, not a seventh operation.
    """
    path = Path(contract)
    ledger_path = Path(ledger) if ledger is not None else None
    anchored = any(
        value is not None for value in (health_anchor, bundle_dependencies, evidence_root)
    )
    parsed_digest = hashlib.sha256(path.read_bytes()).hexdigest() if anchored else None
    loaded = load(path)
    return _health_document(
        path,
        loaded,
        now=now or dt.datetime.now(dt.UTC),
        ledger=ledger_path,
        health_anchor=health_anchor,
        bundle_dependencies=bundle_dependencies,
        evidence_root=Path(evidence_root) if evidence_root is not None else None,
        parsed_contract_sha256=parsed_digest,
    ).as_dict()


# ------------------------------------------------------------------ pose edits (E5-p)
#
# The same one-function-per-surface rule for `lockwire move|rotate|reparent`. Each wraps `edit.py`'s
# operation and computes nothing of its own — this is the function the app's
# gizmo (e5-r) and the keyboard route (e5-x) must end in, so a mouse drag and a
# typed command land the same bytes through the same source/witness gate.


def passed_baseline_channels(
    findings: list[BaselineFinding], artifact: BaselineArtifact
) -> list[ScoredRecord]:
    """Every declared baseline channel that did not produce a violation — the
    health ledger's only source for a device reading "healthy" rather than
    "not watched". The one computation ``baseline-check`` needs, so
    no door carries a second copy of "which channels passed".
    """
    violated_channels = {channel for finding in findings for channel in finding.channels}
    return [
        ScoredRecord(
            quantity=channel_id, observed=profile.name, expected="within baseline", margin=""
        )
        for channel_id, profile in artifact.profiles.items()
        if channel_id not in violated_channels
    ]
