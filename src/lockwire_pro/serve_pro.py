"""The paid sidecar commands: preflight, baseline-check/-review, health, the app-owned run and
analysis jobs, and the actuator review draft. ``register`` puts them (and the paid ``page``,
which also takes a recording) on ``plugins.SERVE_COMMANDS``; the free ``lockwire.serve``
names none of them."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console
from ruamel.yaml import YAML

from lockwire import plugins
from lockwire.confine import confine
from lockwire.cli_line import cli_line as _build_cli_line
from lockwire.copy import check_label
from lockwire.report import ScoredRecord, render
from lockwire.report import verdict as compute_verdict
from lockwire.report_json import HEALTH_ARTIFACT_TYPE, envelope
from lockwire.review_draft import ReviewSpecChange
from lockwire.schema import ArtifactRef, load_contract
from lockwire.serve import (
    PageArgs,
    RunReply,
    _document_reply,
    _label_findings,
    _LOAD_ERRORS,
    _no_contract,
    _ok,
    _refusal,
    _StrictArgs,
    page_reply,
)
from lockwire_pro import api_pro as api
from lockwire_pro.api_payload_pro import AnalysisJobStatePayload, RunStatePayload
from lockwire_pro.baseline import check_recording, parse_baseline_review, render_baseline_review
from lockwire_pro.baseline import baseline_record_report, parse_baseline_artifact
from lockwire_pro.health import append_health_rows
from lockwire_pro.preflight.report import EvidenceClass, PreflightProfile, preflight_policy
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.recording import load_recording


class PreflightArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    recording: str | None = None
    live: bool = False
    context: str = "nominal-op"
    profile: PreflightProfile = "analysis"
    required_evidence: list[EvidenceClass] = Field(default_factory=list)


class BaselineCheckArgs(_StrictArgs):
    recording: str
    baseline: str
    contract: str = "lockwire.yaml"
    no_ledger: bool = False


class BaselineReviewArgs(_StrictArgs):
    artifact: str


class HealthArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    json_out: str | None = None
    ledger: str | None = None
    anchor_rows: int | None = None
    anchor_head: str | None = None
    bundle_dependencies: list[ArtifactRef] | None = None
    evidence_root: str | None = None


class RunStartArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str
    recording: str | None = None
    context: str = "nominal-op"
    profile: PreflightProfile = "analysis"
    required_evidence: list[EvidenceClass] = Field(default_factory=list)
    live: bool = False
    container: str | None = None
    local: bool = False
    seconds: float = 10.0
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_identity_file: str | None = None
    ssh_known_hosts: str | None = None
    ssh_target: str | None = None
    ssh_image: str | None = None
    keep_bag: str | None = None


class RunControlArgs(_StrictArgs):
    pass


class AnalysisStartArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str
    expected_revision: str
    procedure_id: str
    evidence_root: str
    package_manifest: str


class AnalysisControlArgs(_StrictArgs):
    pass


class ActuatorReviewDraftArgs(_StrictArgs):
    """Explicit evidence-bound inputs for the read-only review-draft door."""

    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str
    case_id: str
    evidence: ArtifactRef
    evidence_root: str
    expected_revision: str
    provenance: str
    discrepancy: str
    alternatives: list[str]
    proposed_target: (
        Literal["physical", "firmware", "configuration", "model", "calibration"] | None
    ) = None
    proposed_patch: str = ""
    proposed_changes: list[ReviewSpecChange] | None = None
    rationale: str
    correction_category: Literal["physical", "model_calibration", "spec_design"]
    required_recheck: list[str]

    @model_validator(mode="after")
    def _one_proposal(self) -> "ActuatorReviewDraftArgs":
        if self.proposed_changes is not None:
            if (
                not self.proposed_changes
                or {"proposed_target", "proposed_patch"} & self.model_fields_set
            ):
                raise ValueError("proposed_changes require a nonempty spec-only proposal")
        elif self.proposed_target is None:
            raise ValueError("proposed_target or proposed_changes is required")
        return self


class ActuatorReviewDraftCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["actuator-review-draft"] = "actuator-review-draft"
    args: ActuatorReviewDraftArgs


class PreflightCommand(BaseModel):
    command: Literal["preflight"] = "preflight"
    args: PreflightArgs = Field(default_factory=PreflightArgs)


class BaselineCheckCommand(BaseModel):
    command: Literal["baseline-check"] = "baseline-check"
    args: BaselineCheckArgs


class BaselineReviewCommand(BaseModel):
    command: Literal["baseline-review"] = "baseline-review"
    args: BaselineReviewArgs


class HealthCommand(BaseModel):
    command: Literal["health"] = "health"
    args: HealthArgs = Field(default_factory=HealthArgs)


class RunStartCommand(BaseModel):
    command: Literal["run-start"] = "run-start"
    args: RunStartArgs


class RunPollCommand(BaseModel):
    command: Literal["run-poll"] = "run-poll"
    args: RunControlArgs = Field(default_factory=RunControlArgs)


class RunCancelCommand(BaseModel):
    command: Literal["run-cancel"] = "run-cancel"
    args: RunControlArgs = Field(default_factory=RunControlArgs)


class AnalysisStartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["analysis-start"] = "analysis-start"
    args: AnalysisStartArgs


class AnalysisPollCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["analysis-poll"] = "analysis-poll"
    args: AnalysisControlArgs = Field(default_factory=AnalysisControlArgs)


class AnalysisCancelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["analysis-cancel"] = "analysis-cancel"
    args: AnalysisControlArgs = Field(default_factory=AnalysisControlArgs)


def _handle_actuator_review_draft(args: ActuatorReviewDraftArgs) -> RunReply:
    """Transport one explicit draft request to the shared read-only API door."""
    line: list[object] = [
        "lockwire",
        "actuator-review-draft",
        args.contract,
        args.case_id,
        "--evidence",
        f"{args.evidence.path}@{args.evidence.sha256}",
        "--evidence-root",
        args.evidence_root,
        "--expected-revision",
        args.expected_revision,
        "--provenance",
        args.provenance,
        "--discrepancy",
        args.discrepancy,
    ]
    for alternative in args.alternatives:
        line += ["--alternative", alternative]
    if args.proposed_changes is None:
        line += [
            "--proposed-target",
            args.proposed_target,
            "--proposed-patch",
            args.proposed_patch,
        ]
    else:
        for change in args.proposed_changes:
            line += ["--proposed-change", change.model_dump_json(exclude_none=True)]
    line += [
        "--rationale",
        args.rationale,
        "--correction-category",
        args.correction_category,
    ]
    for recheck in args.required_recheck:
        line += ["--required-recheck", recheck]
    cli_line = _build_cli_line(*line)
    try:
        document = api.actuator_review_draft(
            args.contract,
            args.case_id,
            evidence=args.evidence,
            evidence_root=args.evidence_root,
            expected_revision=args.expected_revision,
            provenance=args.provenance,
            discrepancy=args.discrepancy,
            alternatives=args.alternatives,
            proposed_target=args.proposed_target,
            proposed_patch=args.proposed_patch,
            proposed_changes=(
                [change.to_change() for change in args.proposed_changes]
                if args.proposed_changes is not None
                else None
            ),
            rationale=args.rationale,
            correction_category=args.correction_category,
            required_recheck=args.required_recheck,
        )
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, _no_contract(Path(args.contract)) or str(error))
    return _document_reply(cli_line, document)


def _handle_preflight(args: PreflightArgs) -> RunReply:
    cli_line_parts = ["lockwire", "preflight", args.contract]
    if args.recording is not None:
        cli_line_parts += ["--from", args.recording]
    if args.profile != "analysis" or args.required_evidence:
        cli_line_parts += ["--profile", args.profile]
        for evidence in args.required_evidence:
            cli_line_parts += ["--require-evidence", evidence]
    cli_line = " ".join(cli_line_parts)
    if args.live:
        return _refusal(cli_line, 2, "--live is not served yet (this box serves --from only)")
    if args.recording is None:
        return _refusal(cli_line, 2, "pass recording (--from RECORDING)")
    try:
        passed: list[ScoredRecord] = []
        document = api.preflight(
            args.contract,
            args.recording,
            context=args.context,
            profile=args.profile,
            required_evidence=args.required_evidence,
            passed_out=passed,
        )
        _label_findings(document, args.contract)
        # Sidecar-only, like finding_labels: the checks that agreed, named for the Test page.
        document["passed"] = [
            {"label": check_label(r.quantity), "observed": r.observed, "expected": r.expected}
            for r in passed
        ]
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, _no_contract(Path(args.contract)) or str(error))
    except Exception as error:  # noqa: BLE001 - stage bodies are not an enumerable failure set
        return _refusal(cli_line, 2, str(error))
    return _document_reply(cli_line, document)


def _handle_baseline_check(args: BaselineCheckArgs) -> RunReply:
    cli_line = (
        f"lockwire baseline-check {args.recording} {args.baseline} --contract {args.contract}"
    )
    recording_path, baseline_path, contract_path = (
        Path(args.recording),
        Path(args.baseline),
        Path(args.contract),
    )
    if not recording_path.exists() or not baseline_path.exists():
        return _refusal(cli_line, 1, "recording or baseline file not found")
    try:
        artifact = parse_baseline_artifact(YAML(typ="safe").load(baseline_path))
        loaded_recording = load_recording(recording_path)
        findings = check_recording(loaded_recording, artifact)
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 1, str(error))
    passed = api.passed_baseline_channels(findings, artifact)
    report = baseline_record_report(findings, passed)
    text = _console_text(lambda console: render(report, console))
    exit_code = compute_verdict(report).exit_code
    if not args.no_ledger:
        try:
            loaded_contract = load_contract(contract_path)
            append_health_rows(
                contract_path,
                loaded_contract,
                report,
                source=f"baseline-check {args.recording} {args.baseline}",
                bag_sha=loaded_recording.sha256,
                context=artifact.context,
                now=dt.datetime.now(dt.UTC),
            )
        except Exception:  # noqa: BLE001 - a ledger failure must never change the verdict
            pass
    return _ok(cli_line, exit_code=exit_code, verdict=None, payload=text)


def _handle_baseline_review(args: BaselineReviewArgs) -> RunReply:
    cli_line = f"lockwire baseline-review {args.artifact}"
    artifact_path = Path(args.artifact)
    if not artifact_path.is_file():
        return _refusal(cli_line, 1, f"no baseline artifact at {artifact_path}")
    try:
        data = YAML(typ="safe").load(artifact_path)
        review = parse_baseline_review(data)
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 1, str(error))
    text = _console_text(lambda console: render_baseline_review(review, console))
    return _ok(cli_line, exit_code=0, verdict=None, payload=text)


def _handle_health(args: HealthArgs) -> RunReply:
    cli_line_parts = ["lockwire", "health", args.contract]
    if args.json_out is not None:
        cli_line_parts += ["--json", args.json_out]
    if args.ledger is not None:
        cli_line_parts += ["--ledger", args.ledger]
    if args.anchor_rows is not None:
        cli_line_parts += ["--anchor-rows", str(args.anchor_rows)]
    if args.anchor_head is not None:
        cli_line_parts += ["--anchor-head", args.anchor_head]
    if args.bundle_dependencies is not None:
        for ref in args.bundle_dependencies:
            cli_line_parts += ["--bundle-dependency", f"{ref.path}@{ref.sha256}"]
    if args.evidence_root is not None:
        cli_line_parts += ["--evidence-root", args.evidence_root]
    cli_line = _build_cli_line(*cli_line_parts)
    contract_path = Path(args.contract)
    try:
        load_contract(contract_path)  # the CLI refuses an unreadable contract first
        json_target = (  # inside the contract's folder, never over a lockfile
            confine(args.json_out, contract_path, contract_path.resolve().parent)
            if args.json_out is not None
            else None
        )
        if any(
            value is not None
            for value in (
                args.ledger,
                args.anchor_rows,
                args.anchor_head,
                args.bundle_dependencies,
                args.evidence_root,
            )
        ):
            document = api.health_document(
                contract_path,
                ledger=args.ledger,
                health_anchor=(args.anchor_rows, args.anchor_head)
                if args.anchor_rows is not None or args.anchor_head is not None
                else None,
                bundle_dependencies=tuple(args.bundle_dependencies)
                if args.bundle_dependencies is not None
                else None,
                evidence_root=args.evidence_root,
            )
            if json_target is not None:
                json_target.write_text(
                    json.dumps(envelope(HEALTH_ARTIFACT_TYPE, document["summary"]), default=str),
                    encoding="utf-8",
                )
            return _document_reply(cli_line, document)
        summary = api.health(contract_path)
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, str(error))
    # The ledger is not re-walked here: `worst` is the library's own word for the
    # whole fleet, and this only maps it onto the transport's exit code.
    worst = summary["worst"]
    exit_code = 1 if worst == "faulty" else (3 if worst in (None, "not_watched", "absent") else 0)
    if json_target is not None:
        try:
            json_target.write_text(json.dumps(summary, default=str), encoding="utf-8")
        except OSError as error:
            return _refusal(cli_line, 2, f"could not write {args.json_out}: {error}")
    return _ok(cli_line, exit_code=exit_code, verdict=None, payload=summary)


def _handle_run_start(args: RunStartArgs, controller: api.RunJobController) -> RunReply:
    state = controller.start(
        args.contract,
        recording=args.recording,
        context=args.context,
        profile=args.profile,
        required_evidence=args.required_evidence,
        live=args.live,
        container=args.container,
        local=args.local,
        seconds=args.seconds,
        ssh_host=args.ssh_host,
        ssh_port=args.ssh_port,
        ssh_user=args.ssh_user,
        ssh_identity_file=args.ssh_identity_file,
        ssh_known_hosts=args.ssh_known_hosts,
        ssh_target=args.ssh_target,
        ssh_image=args.ssh_image,
        keep_bag=args.keep_bag,
    )
    RunStatePayload.model_validate(state)
    return _ok("lockwire run-start", exit_code=0, verdict=None, payload=state)


def _handle_run_poll(_args: RunControlArgs, controller: api.RunJobController) -> RunReply:
    state = controller.poll()
    RunStatePayload.model_validate(state)
    return _ok("lockwire run-poll", exit_code=0, verdict=None, payload=state)


def _handle_run_cancel(_args: RunControlArgs, controller: api.RunJobController) -> RunReply:
    state = controller.cancel()
    RunStatePayload.model_validate(state)
    return _ok("lockwire run-cancel", exit_code=0, verdict=None, payload=state)


def _handle_analysis_start(args: AnalysisStartArgs, controller: api.RunJobController) -> RunReply:
    state = controller.start_analysis(
        args.contract,
        expected_revision=args.expected_revision,
        procedure_id=args.procedure_id,
        evidence_root=args.evidence_root,
        package_manifest_path=args.package_manifest,
    )
    AnalysisJobStatePayload.model_validate(state)
    return _ok("lockwire analysis-start", exit_code=0, verdict=None, payload=state)


def _handle_analysis_poll(_args: AnalysisControlArgs, controller: api.RunJobController) -> RunReply:
    state = controller.poll_analysis()
    AnalysisJobStatePayload.model_validate(state)
    return _ok("lockwire analysis-poll", exit_code=0, verdict=None, payload=state)


def _handle_analysis_cancel(
    _args: AnalysisControlArgs, controller: api.RunJobController
) -> RunReply:
    state = controller.cancel_analysis()
    AnalysisJobStatePayload.model_validate(state)
    return _ok("lockwire analysis-cancel", exit_code=0, verdict=None, payload=state)


def _console_text(render_into: Any) -> str:
    """Capture what the CLI would print to the terminal, as plain text."""
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=100)
    render_into(console)
    return buffer.getvalue()


class PageArgsPro(PageArgs):
    """The free page request plus a recording whose preflight findings the page shows."""

    recording: str | None = None


class PageCommandPro(BaseModel):
    command: Literal["page"] = "page"
    args: PageArgsPro = Field(default_factory=PageArgsPro)


def _handle_page(args: PageArgsPro) -> RunReply:
    if args.recording is None:
        return page_reply(args)
    return page_reply(
        args,
        lambda loaded: (
            run_preflight(loaded, api.open_source(Path(args.recording))),
            args.recording,
        ),
    )


# ---------------------------------------------------------------------------
# Registration. A plugin handler is called `(body, state)`; `state` is the app's `app.state`.


def _args(handler):
    return lambda body, state: handler(body.args)


def _jobs(handler):
    def call(body, state):
        controller = getattr(state, "run_jobs", None)
        if controller is None:
            raise RuntimeError("Run job commands need an app-owned controller")
        return handler(body.args, controller)

    return call


def policy(profile: str | None, required_evidence: list[str] | None):
    """`lockwire serve --profile/--require-evidence`: a server's required policy cannot be
    weakened by request defaults or an explicit request. Additional requirements still apply."""
    profile, required = preflight_policy(profile or "analysis", required_evidence)

    def rewrite(body):
        if profile == "powered-release" and body.command in {"preflight", "run-start"}:
            return body.model_copy(
                update={
                    "args": body.args.model_copy(
                        update={
                            "profile": profile,
                            "required_evidence": list(
                                dict.fromkeys(required + body.args.required_evidence)
                            ),
                        }
                    )
                }
            )
        return body

    return rewrite


_COMMANDS = {
    "page": (_args(_handle_page), PageArgsPro, PageCommandPro),
    "actuator-review-draft": (
        _args(_handle_actuator_review_draft),
        ActuatorReviewDraftArgs,
        ActuatorReviewDraftCommand,
    ),
    "preflight": (_args(_handle_preflight), PreflightArgs, PreflightCommand),
    "baseline-check": (_args(_handle_baseline_check), BaselineCheckArgs, BaselineCheckCommand),
    "baseline-review": (_args(_handle_baseline_review), BaselineReviewArgs, BaselineReviewCommand),
    "health": (_args(_handle_health), HealthArgs, HealthCommand),
    "run-start": (_jobs(_handle_run_start), RunStartArgs, RunStartCommand),
    "run-poll": (_jobs(_handle_run_poll), RunControlArgs, RunPollCommand),
    "run-cancel": (_jobs(_handle_run_cancel), RunControlArgs, RunCancelCommand),
    "analysis-start": (_jobs(_handle_analysis_start), AnalysisStartArgs, AnalysisStartCommand),
    "analysis-poll": (_jobs(_handle_analysis_poll), AnalysisControlArgs, AnalysisPollCommand),
    "analysis-cancel": (_jobs(_handle_analysis_cancel), AnalysisControlArgs, AnalysisCancelCommand),
}


def _setup(state: Any) -> None:
    state.run_jobs = api.RunJobController()


def register() -> None:
    plugins.SERVE_COMMANDS.update(
        {name: plugins.ServeCommand(*entry) for name, entry in _COMMANDS.items()}
    )
    if _setup not in plugins.SERVE_SETUP:
        plugins.SERVE_SETUP.append(_setup)
    plugins.SERVE_POLICY = policy
