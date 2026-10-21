"""The public library door: ``import lockwire.api``.

lockwire answers the same questions through four doors — the command line, the
MCP server, the HTTP sidecar (``serve.py``) and this module. The project allows
exactly one implementation behind them, so this file **wraps** what already
exists and computes nothing of its own:

- ``check``     -> ``checks.run_record_checks``
- ``plan``      -> ``plan.validate_plan``
- ``preflight`` -> ``preflight.runner.run_preflight`` + ``report_json.preflight_document``
- ``describe``  -> ``describe.describe_all``
- ``health``    -> ``health.read_ledger_with_skipped`` + ``health.own_and_foreign`` + ``health.summary``
- ``health_document`` -> ``health.build_document`` (the whole document every door reads)
- ``declare``   -> ``edit.apply_changes`` (which owns the source/witness gate)
- ``diff``      -> ``factdiff.fact_diff`` (ref resolution through ``gitref``, git's one door)
- ``add``       -> ``edit.add_entry`` (one-submit device/family/model path included)
- ``upgrade``   -> the pinned, non-overwriting legacy-file migration below

Each read function returns **exactly the document the CLI's ``--json`` writes**;
``verdict_document`` is that shape, and the CLI, the MCP server and the sidecar
all call it rather than assembling their own.

Importing this module must not drag in a command line or a web server: no
``typer``, ``rich``, ``fastapi``, ``uvicorn`` or ``mcp``. That is asserted by
``tests/test_api_library.py::test_library_import_pulls_no_server_or_cli_deps``,
and it is why ``report.py`` imports ``rich`` lazily.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ruamel.yaml import YAMLError

from lockwire import blame as blame_history
from lockwire import gitref
from lockwire.checks import run_record_checks
from lockwire.describe import describe_all
from lockwire.edit import (
    Change,
    add_entry,
    apply_changes,
    apply_review,
    contract_revision,
    move as _move,
    place as _place,
    reparent as _reparent,
    rotate as _rotate,
    set_mount_shape as _set_mount_shape,
)
from lockwire.factdiff import fact_diff, load_contract_text
from lockwire.forces import force_document
from lockwire.plan import Plan, load_plan
from lockwire.plan import validate_plan as _validate_plan
from lockwire.plan_payload import PlanPayload
from lockwire.report import (
    Finding,
    ScoredRecord,
    verdict,
    verdict_headline,
    verdict_word,
)
from lockwire.report_json import (
    CHECK_ARTIFACT_TYPE,
    FORMAT_VERSION,
    PLAN_ARTIFACT_TYPE,
    envelope,
)
from lockwire.schema import (
    ArtifactRef,
    Contract,
    UnsupportedSchemaVersion,
    load_contract,
    load_contract_bytes,
    upgrade_contract_text,
)
from lockwire.scene_urdf import load_scene_any

#: The document's own version, separate from the contract ``SCHEMA_VERSION`` and
#: from any transport wrapper. Shared with ``report_json`` so
#: the preflight document and the check document never version apart.
DOCUMENT_VERSION = FORMAT_VERSION


def contract_sha(contract_path: Path) -> str:
    """Short sha of the contract file; the same 12 hex ``health.contract_sha`` returns.

    Duplicated (one line) so the free library never imports ``health``.
    """
    return hashlib.sha256(contract_path.read_bytes()).hexdigest()[:12]


def start(
    urdf: Path | str, *, overwrite: bool = False, expected_revision: str | None = None
) -> dict[str, str | bool]:
    """Start beside the selected model, with revision-bound replacement."""
    from lockwire.start_operation import start_contract

    return start_contract(urdf, overwrite=overwrite, expected_revision=expected_revision)


def from_parts(
    text: str, *, out: Path | str | None = None, catalog: Path | str | None = None
) -> dict[str, Any]:
    """Match a pasted parts list to the catalog; with ``out``, also write a draft lockfile.

    Information, not a gate: a mismatch is listed, never refused. ``parts_list.match_parts``.
    """
    from lockwire.parts_list import match_parts, write_draft

    if not isinstance(text, str):  # same plain ValueError the neighbouring functions raise
        raise ValueError("from_parts needs the parts list as text")
    document = match_parts(text, catalog)
    if out is not None and not Path(out).exists():
        write_draft(document, Path(out), catalog)
        document["written"] = str(out)
    return document


def api_schema(out: Path | str) -> None:
    """Write the payload union's JSON schema — ``api_payload.export_openapi``.

    ``api_payload`` is imported here rather than at module scope so
    ``import lockwire.api`` stays as thin as
    ``test_library_import_pulls_no_server_or_cli_deps`` requires: the schema
    export is the one operation nothing else on the read path needs.
    """
    from lockwire.api_payload import export_openapi

    export_openapi(out)


def api_types(out: Path | str) -> None:
    """Write the payload union's TypeScript types — ``emit_ts.write`` (e1-d3).

    ``emit_ts`` is imported here rather than at module scope for the same
    reason ``api_schema`` imports ``api_payload`` locally:
    ``test_library_import_pulls_no_server_or_cli_deps`` keeps ``import
    lockwire.api`` thin.
    """
    from lockwire.emit_ts import write

    write(out)


def declare_form(contract: Path | str | Contract) -> dict[str, Any]:
    """The Declare form's payload (E4-df): every quantity ``declare`` could
    write in `contract` — present or not — plus a pre-filled witness box.
    ``page.build_declare_form`` — read-only, nothing here writes.

    Not in ``FUNCTIONS``: there is no ``lockwire declare-form`` command, only
    the three face boxes (e4-b/c/d) reading this document (same "not a
    seventh operation" reasoning as ``health_document`` above).
    """
    loaded = contract if isinstance(contract, Contract) else load(contract)
    from lockwire.page import build_declare_form  # noqa: PLC0415 - keeps `import lockwire.api` thin

    document = build_declare_form(loaded)
    if not isinstance(contract, Contract):
        document["revision"] = contract_revision(contract)
        from lockwire.cli import _find_scene  # noqa: PLC0415 - deferred, as load_scene
        from lockwire.scene_urdf import urdf_visual_parents  # noqa: PLC0415

        scene_path, _ = _find_scene(Path(contract), None)
        if scene_path is not None and scene_path.suffix.lower() == ".urdf":
            try:
                up = urdf_visual_parents(scene_path)
            except (OSError, ValueError, SyntaxError):  # SyntaxError: ET.ParseError
                up = {}
            for row in document["rows"]:
                seg = row["path"].split("/")
                if seg[:2] == ["model", "links"] and seg[2] in up:
                    row["part"] = up[seg[2]]
    return document


def page(
    contract: Path | str | Contract,
    *,
    preflight: tuple[list[Finding], str] | None = None,
    scene: Any = None,
    scene_source: str | None = None,
    contract_path: Path | str | None = None,
    plan_path: Path | str | None = None,
    plan_outcomes: list[Any] | None = None,
    cli_line: str | None = None,
) -> dict[str, Any]:
    """The page dict ``page.build_payload`` computes, wrapped unchanged —
    the root gap e1-pg closes: ``PagePayload`` (``api_payload.py``) was
    defined with nothing behind it. This does not recompute anything
    ``build_payload`` does not already compute; it only loads the contract the
    way every other door in this file does.

    ``contract_path`` is a separate override (defaulting to ``contract`` when
    that is a path, ``None`` when it is already a ``Contract``) because
    ``build_payload`` itself takes a parsed ``Contract`` *and* its path as two
    fields — the path drives the health summary and the describe faces, not
    just the loader — so a caller that already parsed the contract (the
    sidecar, which also needs it for scene/preflight/plan lookups first) can
    pass both without a second parse.

    Not in ``FUNCTIONS``/``__all__``: there is no ``lockwire page`` CLI
    command, and ``test_every_cli_command_has_a_library_twin`` refuses a
    ``FUNCTIONS`` entry with no CLI leaf of the same name. So this sits beside
    ``load_scene``/``passed_baseline_channels`` above — a computation more
    than one door needs (the sidecar's ``page`` command) without being the
    twin of its own CLI command. the page's CLI command remains the one that
    writes this same payload to a file; this returns the dict itself for a
    caller — the sidecar, today — that wants the document.
    """
    from lockwire.page import build_payload as _build_payload  # noqa: PLC0415

    loaded = contract if isinstance(contract, Contract) else load(contract)
    if contract_path is not None:
        resolved_path = Path(contract_path)
    else:
        resolved_path = None if isinstance(contract, Contract) else Path(contract)
    document = _build_payload(
        loaded,
        preflight=preflight,
        scene=scene,
        scene_source=scene_source,
        contract_path=resolved_path,
        plan_path=Path(plan_path) if plan_path is not None else None,
        plan_outcomes=plan_outcomes,
        cli_line=cli_line,
    )
    if resolved_path is not None:
        document["revision"] = contract_revision(resolved_path)
    return document


#: The free library operations. Every name here has a CLI command of the same
#: name. ``api_pro.FUNCTIONS`` is the complete inventory with the paid ones.
FUNCTIONS = (
    "start",
    "load",
    "check",
    "plan",
    "describe",
    "declare",
    "review",
    "diff",
    "log",
    "blame",
    "what-if",
    "add",
    "move",
    "rotate",
    "reparent",
    "place",
    "mount-shape",
    "upgrade",
    "api-schema",
    "api-types",
)

__all__ = [
    "start",
    "DOCUMENT_VERSION",
    "FUNCTIONS",
    "add",
    "api_schema",
    "from_parts",
    "api_types",
    "blame",
    "what_if",
    "check",
    "check_document",
    "declare",
    "describe",
    "diff",
    "is_stale",
    "load",
    "mount_shape",
    "move",
    "log",
    "place",
    "plan",
    "reparent",
    "review",
    "rotate",
    "upgrade",
    "verdict_document",
]


def verdict_document(
    contract: Contract, findings: list[Finding], artifact_type: str
) -> dict[str, Any]:
    """The canonical findings document, shared by every door.

    The verdict is not recomputed here any more than it is in ``report_json``:
    ``report.verdict`` decides once and this reports what it decided. ``scored``
    and ``outcomes`` ride along when the report carries them (a plain list does
    not) rather than being invented.
    """
    decision = verdict(findings)
    document = envelope(
        artifact_type,
        {
            "robot": contract.robot.name,
            # Hyphenated: byte-identical to report_json's and the
            # terminal's spelling, not the raw underscored VerdictCode.
            "verdict": verdict_word(decision.code),
            "exit_code": decision.exit_code,
            "counts": {
                "violation": decision.violations,
                "warning": decision.warnings,
                "not_scored": decision.not_scored,
            },
            "scored": decision.scored,
            "findings": [finding.model_dump(mode="json") for finding in findings],
        },
    )
    outcomes = getattr(findings, "outcomes", None)
    if outcomes is not None:
        document["outcomes"] = [outcome.model_dump(mode="json") for outcome in outcomes]
    if artifact_type == CHECK_ARTIFACT_TYPE:
        # the force panel gets no command of its own -- the numbers ride in
        # the check document the CLI and the library already produce, so every
        # figure on the panel has a CLI twin. The CG is reused, never recomputed
        # `information` is exactly what compute_system_cg returned.
        document["forces"] = force_document(contract, getattr(findings, "information", []))
    return document


def load(contract: Path | str) -> Contract:
    """Load and validate a contract file — ``schema.load_contract``, nothing more."""
    return load_contract(Path(contract))


def check_document(
    contract: Contract,
    findings: list[Finding],
    *,
    revision: str | None = None,
    actuator_evidence: Mapping[str, ArtifactRef] | None = None,
) -> dict[str, Any]:
    """The `check` document: `verdict_document` plus `headline`.

    A thin wrapper, not a second document builder — `cli.py`'s ``check``
    command (it and this file must not be able to disagree about one
    run) already computes ``findings`` itself for its own console render, so
    it calls this rather than duplicating the ``headline`` line; ``api.check``
    below is the same wrapper for a caller that has not computed ``findings``
    yet.
    """
    document = verdict_document(contract, findings, CHECK_ARTIFACT_TYPE)
    # The plain-words sentence for the verdict, `report.verdict_headline` -- the
    # same producer `gui.py`'s HTML page reuses, not a second
    # implementation. `adds_up` is `findings.scored_records`, byte-identical to
    # `gui.build_payload`'s own source for that count.
    scored_records = getattr(findings, "scored_records", [])
    document["headline"] = verdict_headline(
        document["counts"]["violation"],
        document["counts"]["warning"],
        document["counts"]["not_scored"],
        len(scored_records),
    )
    # Only the paid check slot produces case reports; without it the key is absent.
    case_reports = getattr(findings, "actuator_case_reports", [])
    if case_reports:
        document["actuator_cases"] = [
            {
                "case_id": report.case_id,
                "evidence": (
                    actuator_evidence[report.case_id].model_dump(mode="json")
                    if actuator_evidence is not None and report.case_id in actuator_evidence
                    else None
                ),
                "conformance": _actuator_claim_document(
                    report.case_id,
                    report.conformance,
                    report.findings,
                    "conformance",
                    report.comparisons,
                    report=report,
                    evidence=(actuator_evidence or {}).get(report.case_id),
                    revision=revision,
                ),
                "simulation_validity": _actuator_claim_document(
                    report.case_id,
                    report.simulation_validity,
                    report.findings,
                    "simulation_validity",
                    report.comparisons,
                    report=report,
                    evidence=(actuator_evidence or {}).get(report.case_id),
                    revision=revision,
                ),
            }
            for report in case_reports
        ]
    document["revision"] = revision
    return document


def _actuator_claim_document(
    case_id: str,
    decision: Any,
    findings: tuple[Finding, ...],
    lane: str,
    comparisons: tuple[ScoredRecord, ...] = (),
    *,
    report: Any = None,
    evidence: ArtifactRef | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    case_prefix = f"actuator_cases/{case_id}/"
    generic_evidence_gap = case_prefix + "evidence"
    simulation_prefix = f"actuator_cases/{case_id}/simulation_validity"
    admitted = (
        (report.physical_evidence if lane == "conformance" else report.simulation_evidence)
        if report is not None
        else None
    )
    artifacts = (
        (report.physical_artifacts if lane == "conformance" else report.simulation_artifacts)
        if report is not None
        else ()
    )
    sources = {
        (row.provenance.source, row.provenance.witness)
        for row in (admitted.signals if admitted is not None else ())
        if row.provenance is not None
        and row.provenance.source.strip()
        and row.provenance.witness.strip()
    }
    evidence_class = "unverified"
    if admitted is not None and decision.scored and evidence is not None:
        evidence_class = (
            "physical_measurement"
            if lane == "conformance"
            else "prediction_with_held_out_reference"
        )
    reference = (
        admitted.validation_reference if lane == "simulation_validity" and admitted else None
    )
    return {
        "comparisons": [
            record.model_dump(mode="json")
            for record in comparisons
            if record.quantity.startswith(case_prefix)
            and (
                (
                    lane == "simulation_validity"
                    and record.quantity.startswith(simulation_prefix + "/")
                )
                or (
                    lane == "conformance"
                    and record.quantity != simulation_prefix
                    and not record.quantity.startswith(simulation_prefix + "/")
                )
            )
        ],
        "label": "physical conformance" if lane == "conformance" else "simulation validity",
        "verdict": verdict_word(decision.code),
        "exit_code": decision.exit_code,
        "scored": decision.scored,
        "context": {
            "case_id": case_id,
            "claim_id": lane,
            "contract_revision": revision,
            "selected_evidence": evidence.model_dump(mode="json") if evidence else None,
            "evidence_class": evidence_class,
            "source_witnesses": [
                {"source": source, "witness": witness} for source, witness in sorted(sources)
            ],
            "uncertainty": "unknown",
            "dependencies": [
                {"path": path, "sha256": sha256}
                for path, sha256 in dict.fromkeys((item.path, item.sha256) for item in artifacts)
            ],
            "freshness": "bound_to_revision"
            if evidence_class != "unverified" and revision
            else "unverified",
            "validation_reference": (
                reference.artifact.model_dump(mode="json") if reference is not None else None
            ),
            "reference_held_out": reference.held_out if reference is not None else None,
            "calibration_artifacts": [
                item.model_dump(mode="json")
                for item in (
                    report.simulation_calibration_artifacts
                    if lane == "simulation_validity" and report is not None
                    else ()
                )
            ],
            "draft_binding": None,
        },
        "gaps": [
            finding.model_dump(mode="json")
            for finding in findings
            if finding.quantity.startswith(case_prefix)
            and (
                f"/{lane}/" in finding.quantity
                or finding.quantity == generic_evidence_gap
                or (
                    lane == "conformance"
                    and finding.quantity != simulation_prefix
                    and not finding.quantity.startswith(simulation_prefix + "/")
                )
            )
        ],
    }


def _actuator_check_context(
    contract: Contract,
    contract_path: Path | None,
    actuator_evidence: Mapping[str, ArtifactRef] | None,
    evidence_root: Path | str | None,
    expected_revision: str | None,
) -> tuple[Mapping[str, ArtifactRef] | None, Path | None, str | None]:
    """Admit one path-backed actuator evidence selection for every Check door."""
    supplied = (
        actuator_evidence is not None,
        evidence_root is not None,
        expected_revision is not None,
    )
    if any(supplied) and not all(supplied):
        raise ValueError(
            "actuator evidence requires actuator_evidence, evidence_root, and expected_revision together"
        )
    if not any(supplied):
        return None, None, contract_revision(contract_path) if contract_path is not None else None
    if contract_path is None:
        raise ValueError("actuator evidence requires a path-backed contract")
    assert (
        actuator_evidence is not None
        and evidence_root is not None
        and expected_revision is not None
    )
    if (
        len(expected_revision) != 64
        or expected_revision != expected_revision.lower()
        or any(character not in "0123456789abcdef" for character in expected_revision)
    ):
        raise ValueError("expected_revision must be the full lowercase raw contract sha256")
    revision = contract_revision(contract_path)
    if expected_revision != revision:
        raise ValueError("contract changed since evidence was selected; reload before checking")
    declared_case_ids = {case.id for case in contract.actuator_cases}
    foreign = sorted(set(actuator_evidence) - declared_case_ids)
    if foreign:
        raise ValueError("actuator evidence names undeclared case(s): " + ", ".join(foreign))
    return actuator_evidence, Path(evidence_root), revision


def check(
    contract: Path | str | Contract,
    *,
    actuator_evidence: Mapping[str, ArtifactRef] | None = None,
    evidence_root: Path | str | None = None,
    expected_revision: str | None = None,
    contract_path: Path | str | None = None,
) -> dict[str, Any]:
    """Record-level checks: the document ``lockwire check --json`` writes.

    ``contract_path`` retains raw-revision and path-backed evidence semantics when
    a transport has already loaded ``contract`` through its shared refusal boundary.
    """
    loaded = contract if isinstance(contract, Contract) else load(contract)
    path = (
        Path(contract_path)
        if isinstance(contract, Contract) and contract_path is not None
        else None
        if isinstance(contract, Contract)
        else Path(contract)
    )
    admitted_evidence, admitted_root, revision = _actuator_check_context(
        loaded, path, actuator_evidence, evidence_root, expected_revision
    )
    findings = run_record_checks(
        loaded,
        contract_path=path,
        actuator_evidence=admitted_evidence,
        evidence_root=admitted_root,
        contract_sha256=revision,
    )
    return check_document(
        loaded,
        findings,
        revision=revision,
        actuator_evidence=admitted_evidence,
    )


def is_stale(document: Mapping[str, Any], contract: Path | str) -> bool:
    """Whether ``document`` — any check/edit result carrying ``revision``
    was computed against contract bytes that are no longer the ones
    on disk, e.g. after ``vcs.apply_undo`` restores an earlier revision.

    Reuses the existing ``revision`` field rather than a second document
    shape: a caller holding a cached result re-checks it against the current
    file instead of trusting it silently.
    """
    return document.get("revision") != contract_revision(Path(contract))


def plan(
    contract: Path | str | Contract, plan: Path | str | dict[str, Any] | Plan
) -> dict[str, Any]:
    """Score an intended plan: the document ``lockwire plan --json`` writes.

    ``plan`` is a plan.yaml path (what the CLI has) or an already-parsed plan
    (what MCP and the sidecar have, inline in the request body).
    """
    loaded = contract if isinstance(contract, Contract) else load(contract)
    if isinstance(plan, Plan):
        parsed = plan
    elif isinstance(plan, dict):
        parsed = Plan.model_validate(plan)
    else:
        parsed = load_plan(Path(plan))
    report = _validate_plan(loaded, parsed)
    # Joined into the typed shape (steps, finding_labels) so every install writes one document.
    document = verdict_document(loaded, report, PLAN_ARTIFACT_TYPE)
    return PlanPayload.model_validate(document).model_dump(mode="json")


def describe(contract: Path | str | Contract) -> dict[str, Any]:
    """Every generated reference file, keyed by filename — ``describe.describe_all``."""
    loaded = contract if isinstance(contract, Contract) else load(contract)
    return envelope(
        "lockwire-describe",
        {"robot": loaded.robot.name, "files": describe_all(loaded)},
    )


def declare(
    contract: Path | str,
    path: str,
    value: Any,
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Declare one fact — the write door, through the same gate as the CLI.

    ``source`` and ``witness`` are not defaulted and not filled in here: they go
    to ``edit.apply_changes``, which refuses an empty one. A wrapper that
    substituted a placeholder would be a write with no provenance behind it.
    """
    contract_path = Path(contract)
    result = apply_changes(
        contract_path,
        [
            Change(
                path=path,
                value=value,
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            )
        ],
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    # ``apply_changes`` above is the commit point (same shape as
    # ``api.add``): by the time this line runs,
    # ``result.written`` already names whether the write landed. The
    # ``contract_sha`` call below still has to re-read the file from disk —
    # a value computed before the write would name the pre-write file, not
    # what a caller asking "what did I just declare" wants — so the read
    # stays after the write, but it can no longer raise bare: an unhandled
    # ``OSError`` there (e.g. the contract becoming unreadable in the window
    # between the write and this re-read) used to surface as an ordinary
    # exception, indistinguishable from nothing having been written, for a
    # declare that had already landed on disk. Catch it and report a
    # distinct, named disposition instead.
    body: dict[str, Any] = {
        "written": result.written,
        "diff": result.diff,
        "lines": list(result.lines),
    }
    try:
        body["contract_sha"] = contract_sha(contract_path)
        body["revision"] = contract_revision(contract_path)
    except OSError as error:
        body["contract_sha"] = None
        body["revision"] = None
        body["contract_sha_error"] = str(error)
    return envelope("lockwire-edit-result", body)


def add(
    contract: Path | str,
    kind: str,
    *,
    id: str | None = None,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Add a device/rail/bus/family/model/budget/battery block — the write
    door behind ``lockwire add``, through ``edit.add_entry``.

    ``fields`` carries the same keys the CLI builds (``model``, ``owner``,
    ``family``, ``physical_mass``, ...) — see ``add_entry``'s docstring.
    The one-submit path: for ``kind="device"``, passing both ``family``
    and ``model`` among ``fields`` creates the family and/or model alongside
    the device, atomically, when either does not already exist.
    """
    contract_path = Path(contract)
    result = add_entry(
        contract_path,
        kind,
        {"id": id, **fields},
        source=source,
        witness=witness,
        note=note,
        locked_at=locked_at,
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    # ``add_entry`` above is the commit point: by the time this
    # line runs, ``result.written`` already names whether the write landed.
    # ``contract_sha`` still has to re-read the file from disk to answer "sha
    # of what's there now" — a value computed *before* the write would name
    # the pre-write file, which is not what a caller asking "what did I just
    # write" wants — so the read stays after the write, but it can no longer
    # be allowed to raise bare: an unhandled ``OSError`` here (e.g. the
    # contract becoming unreadable in the window between the write and this
    # re-read) surfaced as an ordinary exception, indistinguishable from
    # nothing having been written, for an add that had already landed on
    # disk. The obvious caller response to an exception is to retry the add,
    # and a retry after a real write is a second write. Catch it and report a
    # distinct, named disposition instead — ``written`` stays exactly what
    # ``add_entry`` returned, and ``contract_sha`` is ``None`` with
    # ``contract_sha_error`` explaining why, never a bare raise.
    document: dict[str, Any] = {
        "written": result.written,
        "diff": result.diff,
        "lines": list(result.lines),
    }
    try:
        document["contract_sha"] = contract_sha(contract_path)
        document["revision"] = contract_revision(contract_path)
    except OSError as error:
        document["contract_sha"] = None
        document["revision"] = None
        document["contract_sha_error"] = str(error)
    return envelope("lockwire-edit-result", document)


def review(
    contract: Path | str,
    path: str,
    state: str,
    *,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Approve or question one declared value, writing the reviewer's note into
    the file — the same document ``--json`` prints.

    Wraps ``edit.apply_review`` exactly the way ``declare`` wraps
    ``apply_changes``; the witness/questioned-needs-a-note gate lives in
    ``edit.apply_review``, not here. ``locked_at`` mirrors ``declare``'s own
    parameter — tests only, so a review's date can be pinned.
    """
    contract_path = Path(contract)
    result = apply_review(
        contract_path,
        path,
        state,
        witness=witness,
        note=note,
        locked_at=locked_at,
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    return envelope(
        "lockwire-edit-result",
        {
            "contract_sha": contract_sha(contract_path),
            "revision": contract_revision(contract_path),
            "written": result.written,
            "diff": result.diff,
            "lines": list(result.lines),
        },
    )


def _edit_document(contract_path: Path, result: Any) -> dict[str, Any]:
    """Use ``declare``'s result envelope for every pose and mount write."""
    document: dict[str, Any] = {
        "written": result.written,
        "diff": result.diff,
        "lines": list(result.lines),
    }
    try:
        document["contract_sha"] = contract_sha(contract_path)
        document["revision"] = contract_revision(contract_path)
    except OSError as error:
        document["contract_sha"] = None
        document["revision"] = None
        document["contract_sha_error"] = str(error)
    return envelope("lockwire-edit-result", document)


def place(
    contract: Path | str,
    target: str,
    on: str,
    offset: tuple[float, float, float],
    *,
    unit: str,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Place a device on a link, with frame and offset committed atomically."""
    path = Path(contract)
    return _edit_document(
        path,
        _place(
            path,
            target,
            on,
            offset,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
            expected_revision=expected_revision,
        ),
    )


def mount_shape(
    contract: Path | str,
    target: str,
    shape: dict[str, Any],
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Set a device's mount geometry through the contract writer."""
    path = Path(contract)
    return _edit_document(
        path,
        _set_mount_shape(
            path,
            target,
            shape,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
            expected_revision=expected_revision,
        ),
    )


def move(
    contract: Path | str,
    target: str,
    offset: tuple[float, float, float],
    *,
    unit: str,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Move a device in its mount frame, or a link in its parent's — `edit.move`."""
    path = Path(contract)
    return _edit_document(
        path,
        _move(
            path,
            target,
            offset,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
            expected_revision=expected_revision,
        ),
    )


def rotate(
    contract: Path | str,
    target: str,
    rpy: tuple[float, float, float],
    *,
    unit: str,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Set a link's orientation, unit required, never defaulted — `edit.rotate`."""
    path = Path(contract)
    return _edit_document(
        path,
        _rotate(
            path,
            target,
            rpy,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
            expected_revision=expected_revision,
        ),
    )


def reparent(
    contract: Path | str,
    target: str,
    to: str,
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Change a device's mount link or a link's joint parent — `edit.reparent`.

    The cycle and orphan refusals are `edit.py`'s, not re-implemented here; the
    structure-hash move arrives in `lines`, the same text the CLI prints.
    """
    path = Path(contract)
    return _edit_document(
        path,
        _reparent(
            path,
            target,
            to,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
            expected_revision=expected_revision,
        ),
    )


def _diff_side(spec: str, contract: Path, repo: Path | None) -> Contract:
    """One side of ``diff``: a contract file on disk, or the contract at a git ref.

    Mirrors ``cli._diff_side``'s resolution rules (a spec naming an existing file
    is read straight off disk and never touches git) without its console/typer
    parts, which belong to the CLI door, not the library.
    """
    on_disk = Path(spec)
    if on_disk.exists() and on_disk.is_file():
        return load_contract(on_disk)
    if repo is None:
        raise ValueError("not a git repository — nothing to compare against")
    if gitref.rev_parse(spec, repo=repo) is None:
        raise ValueError(f"no such ref {spec!r}")
    relpath = gitref.relpath_in_repo(contract, repo)
    if relpath is None:
        raise ValueError(f"{contract} is outside the repository at {repo}")
    text = gitref.show(spec, relpath, repo=repo)
    if text is None:
        raise FileNotFoundError(f"{contract.name} did not exist at {spec}")
    return load_contract_text(text)


def diff(contract: Path | str, a: str = "HEAD", b: str | None = None) -> dict[str, Any]:
    """What changed in the facts between two refs: the document ``lockwire diff --json`` writes.

    ``a`` and ``b`` are refs (or contract paths), the same as the CLI's arguments;
    ``b`` defaults to the contract file on disk, same as ``lockwire diff`` does.
    """
    contract_path = Path(contract)
    if b is None and not contract_path.exists():
        raise FileNotFoundError(f"no contract at {contract_path}")
    repo = gitref.repo_root(contract_path if contract_path.exists() else Path.cwd())
    before = _diff_side(a, contract_path, repo)
    after = _diff_side(b if b is not None else str(contract_path), contract_path, repo)
    return fact_diff(before, after).as_json()


def _contract_at(ref: str, relpath: str, *, repo: Path) -> Contract | None:
    text = gitref.show(ref, relpath, repo=repo)
    if text is None:
        return None
    try:
        return load_contract_text(text)
    except UnsupportedSchemaVersion:
        raise
    except (OSError, TypeError, ValueError, YAMLError):
        return None


def log(contract: Path | str, revision_range: str | None = None) -> list[dict[str, Any]]:
    """The ``lockwire log --json`` rows for commits touching ``contract``."""
    contract_path = Path(contract)
    repo = gitref.repo_root(contract_path if contract_path.exists() else Path.cwd())
    if repo is None:
        return []
    relpath = gitref.relpath_in_repo(contract_path, repo)
    if relpath is None:
        raise ValueError(f"{contract_path} is outside the repository at {repo}")
    commits = gitref.log(revision_range, [relpath], repo=repo, follow=revision_range is None)
    if commits is None:
        raise ValueError(f"no such ref {revision_range!r}")

    entries: list[dict[str, Any]] = []
    historical_relpath = relpath
    for sha, author, date, message in commits:
        row = {"sha": sha, "author": author, "date": date, "message": message}
        parent_relpath = gitref.parent_relpath_after_rename(sha, historical_relpath, repo=repo)
        try:
            current = _contract_at(sha, historical_relpath, repo=repo)
        except UnsupportedSchemaVersion:
            entries.append({**row, "verdict": "REFUSED", "fact_diff": None})
            historical_relpath = parent_relpath
            continue
        if current is None:
            historical_relpath = parent_relpath
            continue
        try:
            parent = _contract_at(f"{sha}^", parent_relpath, repo=repo)
        except UnsupportedSchemaVersion:
            parent = None
        entries.append(
            {
                **row,
                "verdict": verdict_word(verdict(run_record_checks(current)).code),
                "fact_diff": fact_diff(parent, current).as_json() if parent else None,
            }
        )
        historical_relpath = parent_relpath
    return entries


def blame(contract: Path | str) -> tuple[list[dict[str, Any]], bool]:
    """Per-declared-fact provenance rows for ``lockwire blame``.

    ``blame.rows`` owns validation, source positions, and git's follow-aware
    attribution. This public door transports that result without deriving a
    second history view.
    """
    return blame_history.rows(Path(contract))


def what_if(
    contract: Path | str,
    changes: list[Change],
    *,
    recorded_health: Any = None,
    watch: Any = (),
) -> dict[str, Any]:
    """Non-mutating current/proposed record comparison; no robot or simulation claim.

    ``recorded_health`` and ``watch`` are ``whatif.what_if``'s optional health
    provider; ``api_pro.what_if`` supplies them. Without them the recorded-health
    part is empty, exactly as for a contract with no ledger.
    """
    from dataclasses import asdict
    from lockwire.whatif import what_if as compare

    loaded = load_contract(Path(contract))
    result = compare(contract, changes, recorded_health=recorded_health, watch=watch)
    sides = {}
    for name in ("current", "proposed"):
        side = getattr(result, name)
        sides[name] = verdict_document(loaded, side.report, "lockwire-record-comparison")
        sides[name]["facts"] = [asdict(fact) for fact in side.facts]
        sides[name]["comparisons"] = [
            row.model_dump(mode="json") for row in side.report.scored_records
        ]
    document = {**sides, "diff": result.diff}
    if recorded_health is not None:  # only the paid what-if supplies a ledger
        document["recorded_health"] = {
            "baseline": [row.model_dump(mode="json") for row in result.recorded_health.baseline],
            "invalidated": [
                row.model_dump(mode="json") for row in result.recorded_health.invalidated
            ],
        }
    return envelope("lockwire-what-if-report", document)


def upgrade(
    contract: Path | str,
    *,
    out: Path | str,
    expected_revision: str,
) -> None:
    """Write a new 0.4 contract from an exact legacy revision without changing it.

    The destination is published only by a hard link from a fully fsynced sibling
    temporary file, so an occupied target is refused and the selected legacy
    bytes are checked both before and immediately before publication.
    """
    contract_path = Path(contract)
    out_path = Path(out)
    if len(expected_revision) != 64 or any(
        character not in "0123456789abcdef" for character in expected_revision
    ):
        raise ValueError("expected_revision must be the full lowercase raw contract sha256")
    if contract_path.resolve() == out_path.resolve():
        raise ValueError("upgrade output must differ from the legacy contract")
    raw = contract_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_revision:
        raise ValueError("legacy contract changed since its revision was selected")
    upgraded = upgrade_contract_text(raw.decode("utf-8")).encode("utf-8")
    legacy = load_contract_bytes(raw)
    if legacy.schema_version not in {"0.2", "0.3"}:
        raise ValueError("upgrade requires an explicit legacy schema_version 0.2 or 0.3")
    load_contract_bytes(upgraded)
    if contract_path.read_bytes() != raw:
        raise ValueError("legacy contract changed during upgrade")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(upgraded)
            handle.flush()
            os.fsync(handle.fileno())
        if contract_path.read_bytes() != raw:
            raise ValueError("legacy contract changed during upgrade")
        os.link(temporary_name, out_path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Shared pieces below ``FUNCTIONS``: not CLI-command twins of their own (no
# ``lockwire load-scene`` or ``lockwire passed-baseline-channels``), so they
# stay out of ``FUNCTIONS``/``__all__`` — but each is a computation more than
# one door needs, so it lives once. Both were duplicated verbatim
# between ``cli.py`` and ``serve.py``; the
# sidecar now calls these instead of carrying its own copy. ``cli.py`` keeps
# its own long-standing version (``_load_scene_for_cli``), a pre-existing
# duplicate this narrows rather than clears.


def _selected_external_meshes(
    contract: Contract, contract_path: Path, scene_path: Path
) -> Mapping[Path, str] | None:
    """Return declared mesh bytes only for the contract's selected model root."""

    if contract.model is None:
        return None
    declared = Path(contract.model.source)
    if not declared.is_absolute():
        declared = contract_path.parent / declared
    if declared.resolve() != scene_path.resolve():
        return None
    from lockwire.scene_urdf import mesh_root  # noqa: PLC0415 - same root `start` records against

    root = mesh_root(declared, contract_path.parent)
    declared_assets: dict[Path, str] = {}
    for asset in contract.model.external_assets:
        resolved = (root / asset.path).resolve()
        if not resolved.is_relative_to(root):
            from lockwire.scene import SceneRefusal  # noqa: PLC0415 - public scene error

            raise SceneRefusal(
                f"{contract_path}: declared external asset {asset.path!r} escapes the robot's package folder."
            )
        declared_assets[resolved] = asset.sha256
    return declared_assets


def load_scene(
    contract_path: Path | str, scene: Path | str | None, package_root: Iterable[Path | str] = ()
) -> tuple[Any, str | None]:
    """Find and load the scene named by ``--scene``, or discovered beside the
    contract — the one "which scene, from where" decision ``report`` and
    ``gui`` both need. Returns ``(None, None)`` when there is nothing to load;
    raises ``lockwire.scene.SceneRefusal`` on a scene that fails to parse.

    Scene *discovery* (``_find_scene``) is CLI-argument shaped and stays in
    ``cli.py``; imported here deferred so importing ``lockwire.api`` alone never
    pulls in typer/rich (``test_library_import_pulls_no_server_or_cli_deps``).
    """
    from lockwire.cli import _find_scene  # noqa: PLC0415 - deferred, see docstring

    scene_path, discovered = _find_scene(Path(contract_path), Path(scene) if scene else None)
    if scene_path is None:
        return None, None
    loaded_contract = load_contract(Path(contract_path))
    loaded_scene = load_scene_any(
        scene_path,
        package_roots=tuple(Path(p) for p in package_root),
        external_assets=_selected_external_meshes(loaded_contract, Path(contract_path), scene_path),
    )
    from lockwire.scene_view import scene_asset_identity

    scene_asset_identity(loaded_contract, loaded_scene, Path(contract_path))
    if discovered:
        return loaded_scene, f"{scene_path.name} (found beside the contract)"
    return loaded_scene, f"{scene_path.name} (given)"
