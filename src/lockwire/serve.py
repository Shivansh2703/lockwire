"""lockwire's sidecar: the same core served over local HTTP.

**Private to the app, verbatim:** *"Token-bound, localhost
only, undocumented, may change with every release. The Python library and MCP are
the public doors. Can be opened later without breaking anyone."* Nothing
here carries a compatibility promise; a caller outside the app uses
``lockwire.api``, the CLI's ``--json``, or MCP.

Two addresses, always: ``GET /v1/hello`` (are you alive, what can you do) and
``POST /v1/run`` (everything else — one discriminated-union body naming the
command). The free commands are ``start``, ``check``, ``page``, ``declare-form``,
``declare``, ``add``, ``review``, ``commit``, ``undo``, ``place``, ``move``,
``rotate``, ``reparent``, ``mount-shape``, ``describe``, ``what-if``, ``plan``,
``diff``, ``log`` and ``report``. A plugin adds its own through
``plugins.SERVE_COMMANDS``; this module names none of them, and a command it does
not know is a 422, refused by the discriminated union before any lockwire code runs.
Robot mode lists the write commands but refuses each by name.

**The sidecar computes nothing of its own.** Each handler calls the
matching ``lockwire.api`` function and wraps the document it returns. ``report``
and ``page``'s scene lookup is ``api.load_scene`` (E1-at); ``page`` has no CLI
command (``api.page`` is not in ``FUNCTIONS`` — see its docstring), so it is not
one of the CLI-named twins either; it wraps ``api.page``, which wraps
``page.build_payload``, unchanged.

The reply is always ``{version, cli_line, exit_code, verdict, payload,
refusal}``: a refusal (bad path, parse error, the CLI's own refusal) comes back
as HTTP 200 with ``exit_code``/``refusal`` set, never a traceback.

``fastapi`` and ``uvicorn`` (the ``serve`` extra) are imported when the app is built, so
this module's request models import without them.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAMLError

from lockwire import __version__, api, plugins, vcs
from lockwire.cli_line import cli_line as _build_cli_line
from lockwire.edit import Change, contract_revision
from lockwire.plan import load_plan, validate_plan
from lockwire.report_html import write_html_report
from lockwire.report_html import _graph as _graph_view
from lockwire.scene import SceneRefusal
from lockwire.scene_urdf import load_scene_any
from lockwire.scene_view import scene_hash as _scene_hash
from lockwire.scene_view import scene_payload as _scene_payload
from lockwire.schema import ArtifactRef, load_contract

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# The reply shape (plan §3): always this, whichever command ran.


class RunReply(BaseModel):
    version: str
    cli_line: str
    exit_code: int
    verdict: str | None = None
    payload: Any = None
    refusal: str | None = None


class CommandInfo(BaseModel):
    name: str
    args_schema: dict[str, Any]


class HelloReply(BaseModel):
    version: str
    commands: list[CommandInfo]


def _ok(cli_line: str, *, exit_code: int, verdict: str | None, payload: Any) -> RunReply:
    return RunReply(
        version=__version__,
        cli_line=cli_line,
        exit_code=exit_code,
        verdict=verdict,
        payload=payload,
        refusal=None,
    )


def _refusal(cli_line: str, exit_code: int, message: str) -> RunReply:
    return RunReply(
        version=__version__,
        cli_line=cli_line,
        exit_code=exit_code,
        verdict=None,
        payload=None,
        refusal=message,
    )


def _document_reply(cli_line: str, document: dict[str, Any]) -> RunReply:
    """Wrap a ``lockwire.api`` document. The verdict and the exit code are the
    library's, read out of the document — the sidecar never decides either."""
    return _ok(
        cli_line,
        exit_code=document["exit_code"],
        verdict=document["verdict"],
        payload=document,
    )


def _no_contract(path: Path) -> str | None:
    """The CLI's own words for a missing contract, so a refusal reads the same
    through every door. ``None`` when the file is there and the failure is real."""
    return None if path.exists() else f"no contract at {path}"


# ---------------------------------------------------------------------------
# The command union. Each member's ``args`` fields mirror the matching typer
# command's arguments/options (names, defaults) from cli.py; a flag that only
# matters for a terminal session (``--open/--no-open``) is left off — the
# sidecar is headless and never opens a browser.


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckArgs(_StrictArgs):
    contract: str = "lockwire.yaml"


class SceneRequest(_StrictArgs):
    """The caller's half of the geometry-behind-a-hash negotiation (e1-b2):
    the sha256 (``scene_view.scene_hash``) of the geometry it already has."""

    have: str | None = None


class PlanArgs(_StrictArgs):
    contract: str
    plan: str


class DescribeArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    out: str | None = None


class DeclareFormArgs(_StrictArgs):
    contract: str = "lockwire.yaml"


class ReportArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    scene: str | None = None
    package_root: list[str] = Field(default_factory=list)
    out: str


class PageArgs(_StrictArgs):
    """Same shape as ``GuiArgs`` minus ``out``: the page has nowhere to write to
    here, it comes back in the reply's ``payload`` (e1-pg)."""

    contract: str = "lockwire.yaml"
    scene: str | None = None
    package_root: list[str] = Field(default_factory=list)
    plan: str | None = None


class LogArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    revision_range: str | None = None


class DiffArgs(_StrictArgs):
    contract: str = "lockwire.yaml"
    a: str = "HEAD"
    b: str | None = None


class DeclareArgs(_StrictArgs):
    contract: str
    path: str
    value: Any
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class AddArgs(_StrictArgs):
    contract: str
    kind: str
    id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class ReviewArgs(_StrictArgs):
    contract: str
    path: str
    state: Literal["approved", "questioned"]
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class CommitArgs(_StrictArgs):
    contract: str
    message: str
    paths: list[str]
    dry_run: bool = False
    expected_revision: str | None = None


class UndoArgs(_StrictArgs):
    contract: str
    source: str = "HEAD"
    apply: bool = False
    expected_revision: str | None = None


class PlaceArgs(_StrictArgs):
    contract: str
    target: str
    on: str
    offset: tuple[float, float, float]
    unit: str
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class MoveArgs(_StrictArgs):
    contract: str
    target: str
    offset: tuple[float, float, float]
    unit: str
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class RotateArgs(_StrictArgs):
    contract: str
    target: str
    rpy: tuple[float, float, float]
    unit: str
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class ReparentArgs(_StrictArgs):
    contract: str
    target: str
    to: str
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class MountShape(_StrictArgs):
    kind: Literal["plate", "bracket", "tube", "custom-mesh"]
    dimensions_mm: tuple[float, float, float]
    fastener_pattern: str | None = None
    mesh_path: str | None = None


class MountShapeArgs(_StrictArgs):
    contract: str
    target: str
    shape: MountShape
    source: str
    witness: str
    note: str | None = None
    locked_at: str | None = None
    dry_run: bool = False
    expected_revision: str | None = None


class StartArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    urdf: str
    overwrite: bool = False
    expected_revision: str | None = None


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["start"] = "start"
    args: StartArgs


class CheckCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["check"] = "check"
    args: CheckArgs = Field(default_factory=CheckArgs)
    #: Sibling of ``args``, not nested in it: the scene negotiation is a
    #: transport concern, not a `check` argument the CLI shares (e1-b2).
    scene: SceneRequest | None = None
    actuator_evidence: dict[str, ArtifactRef] | None = None
    evidence_root: str | None = None
    expected_revision: str | None = None


class WhatIfChange(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    value: str
    source: str
    witness: str


class WhatIfArgs(_StrictArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str
    expected_revision: str
    change: WhatIfChange


class WhatIfCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["what-if"] = "what-if"
    args: WhatIfArgs


class PlanCommand(BaseModel):
    command: Literal["plan"] = "plan"
    args: PlanArgs


class DescribeCommand(BaseModel):
    command: Literal["describe"] = "describe"
    args: DescribeArgs = Field(default_factory=DescribeArgs)


class DeclareFormCommand(BaseModel):
    command: Literal["declare-form"] = "declare-form"
    args: DeclareFormArgs = Field(default_factory=DeclareFormArgs)


class ReportCommand(BaseModel):
    command: Literal["report"] = "report"
    args: ReportArgs




class PageCommand(BaseModel):
    command: Literal["page"] = "page"
    args: PageArgs = Field(default_factory=PageArgs)


class LogCommand(BaseModel):
    command: Literal["log"] = "log"
    args: LogArgs = Field(default_factory=LogArgs)


class DiffCommand(BaseModel):
    command: Literal["diff"] = "diff"
    args: DiffArgs = Field(default_factory=DiffArgs)


class DeclareCommand(BaseModel):
    command: Literal["declare"] = "declare"
    args: DeclareArgs


class AddCommand(BaseModel):
    command: Literal["add"] = "add"
    args: AddArgs


class ReviewCommand(BaseModel):
    command: Literal["review"] = "review"
    args: ReviewArgs


class CommitCommand(BaseModel):
    command: Literal["commit"] = "commit"
    args: CommitArgs


class UndoCommand(BaseModel):
    command: Literal["undo"] = "undo"
    args: UndoArgs


class PlaceCommand(BaseModel):
    command: Literal["place"] = "place"
    args: PlaceArgs


class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    args: MoveArgs


class RotateCommand(BaseModel):
    command: Literal["rotate"] = "rotate"
    args: RotateArgs


class ReparentCommand(BaseModel):
    command: Literal["reparent"] = "reparent"
    args: ReparentArgs


class MountShapeCommand(BaseModel):
    command: Literal["mount-shape"] = "mount-shape"
    args: MountShapeArgs


_LOAD_ERRORS = (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError)

#: The parsed-scene cache (e1-b2): keyed by ``(path, root-file digest)`` so a scene
#: edited on disk is re-parsed and a re-request for the same file is not.
#: Single-process, in-memory, unbounded — the sidecar serves one robot's
#: contract tree per run, so the key space is small by construction.
#: ponytail: no eviction; add an LRU cap if a session ever loads many scenes.
_SCENE_CACHE: dict[tuple[str, str], Any] = {}


def _cached_scene(contract_path: Path) -> tuple[Any, str | None]:
    """Discover the scene beside ``contract_path`` (T2 — ``check`` takes no
    ``--scene`` flag, `cli.py:454`) and load it, caching the parse by
    ``(path, root-file digest)``. Discovery and the actual parse (``load_scene_any``)
    are the same calls ``api.load_scene`` makes — this wraps them with a
    cache in front, which ``api.load_scene`` cannot do itself since it never
    exposes the scene's path/stat to key on.
    """
    from lockwire.cli import _find_scene  # noqa: PLC0415 - cli's tail loads plugins

    scene_path, _discovered = _find_scene(contract_path, None)
    if scene_path is None:
        return None, None
    loaded_contract = load_contract(contract_path)
    external_assets = api._selected_external_meshes(
        loaded_contract, contract_path, scene_path
    )
    from hashlib import sha256
    digest = sha256(scene_path.read_bytes()).hexdigest()
    key = (str(scene_path.resolve()), digest)
    # An external mesh can change without changing the selected root's digest.
    # Reparse every selected model: `{}` means its root is selected but it
    # declares no mesh identities, which must still refuse an external mesh.
    # `None` alone means this scene is unselected and can use the root cache.
    cached = None if external_assets is not None else _SCENE_CACHE.get(key)
    if cached is None:
        cached = load_scene_any(scene_path, external_assets=external_assets)
        if cached.asset_sha256 != digest:
            raise SceneRefusal(f"{scene_path}: selected asset changed while loading.")
        if external_assets is None:
            _SCENE_CACHE[key] = cached
    return cached, f"{scene_path.name} (found beside the contract)"


# ---------------------------------------------------------------------------
# Handlers. Each reuses the same functions cli.py's matching command calls.


def _handle_check(command: CheckCommand) -> RunReply:
    args, scene_request = command.args, command.scene
    line: list[object] = ["lockwire", "check", args.contract]
    if command.actuator_evidence is not None:
        for case_id in sorted(command.actuator_evidence):
            ref = command.actuator_evidence[case_id]
            line += ["--actuator-evidence", f"{case_id}={ref.path}@{ref.sha256}"]
    if command.evidence_root is not None:
        line += ["--evidence-root", command.evidence_root]
    if command.expected_revision is not None:
        line += ["--expected-revision", command.expected_revision]
    cli_line = _build_cli_line(*line)
    try:
        document = api.check(
            args.contract,
            actuator_evidence=command.actuator_evidence,
            evidence_root=command.evidence_root,
            expected_revision=command.expected_revision,
        )
        scene, scene_source = _cached_scene(Path(args.contract))
        loaded_contract = load_contract(Path(args.contract))
        from lockwire.scene_view import scene_asset_identity
        identity = (scene_asset_identity(loaded_contract, scene, Path(args.contract))
                    if scene is not None else {})
    except (*_LOAD_ERRORS, SceneRefusal) as error:
        return _refusal(cli_line, 1, _no_contract(Path(args.contract)) or str(error))
    if scene is not None:
        current_hash = _scene_hash(scene)
        if scene_request is not None and scene_request.have == current_hash:
            document["scene"] = {"unchanged": True, **identity}
        else:
            document["scene"] = _scene_payload(
                loaded_contract,
                scene,
                source=scene_source,
                contract_path=Path(args.contract),
                cards=[],
                scored_records=[],
                labels=[],
                decimate=False,
            )
        # The lens bar's words ride beside the scene they label (E2-j2), on the same
        # condition as `scene` above — `_graph_view` is `report_html._graph`, the one
        # function the page payload already uses for this (`page.py`), not a second
        # computation of it.
        document["graph"] = _graph_view(loaded_contract)
    return _document_reply(cli_line, document)


def _handle_what_if(args: WhatIfArgs) -> RunReply:
    """Compare one observed proposal through the existing non-mutating API door."""
    cli_line = _build_cli_line(
        "lockwire",
        "what-if",
        args.contract,
        "--change",
        f"{args.change.path}={args.change.value}",
        "--source",
        args.change.source,
        "--witness",
        args.change.witness,
    )
    try:
        if (
            len(args.expected_revision) != 64
            or args.expected_revision != args.expected_revision.lower()
            or any(character not in "0123456789abcdef" for character in args.expected_revision)
        ):
            raise ValueError("expected_revision must be the full lowercase raw contract sha256")
        if contract_revision(args.contract) != args.expected_revision:
            raise ValueError("contract changed since it was opened; reload before comparing")
        document = plugins.pick("what_if", api.what_if)(
            args.contract,
            [
                Change(
                    path=args.change.path,
                    value=args.change.value,
                    source=args.change.source,
                    witness=args.change.witness,
                )
            ],
        )
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, _no_contract(Path(args.contract)) or str(error))
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _label_findings(document: dict[str, Any], contract: str) -> None:
    """Sidecar-only: name each finding for the app's rows (the CLI documents stay as they are)."""
    from lockwire import page as gui  # noqa: PLC0415
    from lockwire.report import Finding  # noqa: PLC0415

    findings = [Finding.model_validate(f) for f in document["findings"]]
    document["finding_labels"] = gui.finding_labels(api.load(contract), findings)


def _handle_plan(args: PlanArgs) -> RunReply:
    cli_line = f"lockwire plan {args.contract} {args.plan}"
    try:
        document = api.plan(args.contract, args.plan)
        _label_findings(document, args.contract)
        from lockwire import page as gui  # noqa: PLC0415

        by_step = gui.step_targets(api.load(args.contract), args.plan)
        for step in document.get("steps", []):
            index = step["path"].split("/")[2:3]  # plan/steps/<i>/...
            if index and index[0].isdigit() and int(index[0]) < len(by_step):
                step["targets"] = by_step[int(index[0])]
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, str(error))
    return _document_reply(cli_line, document)


def _handle_describe(args: DescribeArgs) -> RunReply:
    cli_line_parts = ["lockwire", "describe", args.contract]
    if args.out is not None:
        cli_line_parts += ["--out", args.out]
    cli_line = " ".join(cli_line_parts)
    try:
        files = api.describe(args.contract)["files"]
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, str(error))
    if args.out is None:
        return _ok(cli_line, exit_code=0, verdict=None, payload="\n\n".join(files.values()))
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (out_dir / name).write_text(content, encoding="utf-8")
    except OSError as error:
        return _refusal(cli_line, 2, f"could not write to {out_dir}: {error}")
    text = "\n".join(f"wrote {out_dir / name}" for name in sorted(files))
    return _ok(cli_line, exit_code=0, verdict=None, payload=text)


def _handle_report(args: ReportArgs) -> RunReply:
    cli_line_parts = ["lockwire", "report", args.contract]
    if args.scene is not None:
        cli_line_parts += ["--scene", args.scene]
    for root in args.package_root:
        cli_line_parts += ["--package-root", root]
    cli_line_parts += ["--out", args.out]
    cli_line = " ".join(cli_line_parts)
    contract_path = Path(args.contract)
    if not contract_path.is_file():
        return _refusal(cli_line, 1, f"no contract at {contract_path}")
    try:
        loaded = load_contract(contract_path)
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 1, str(error))
    try:
        api.load_scene(contract_path, args.scene, args.package_root)
    except SceneRefusal as error:
        return _refusal(cli_line, 2, str(error))
    out_path = Path(args.out)
    try:
        write_html_report(loaded, out_path)
    except OSError as error:
        return _refusal(cli_line, 1, f"could not write {out_path}: {error}")
    return _ok(cli_line, exit_code=0, verdict=None, payload=f"wrote {out_path}")


def page_reply(
    args: PageArgs, run_of: Callable[[Any], tuple[list[Any], str]] | None = None
) -> RunReply:
    """The window's document: the payload ``page.build_payload`` computes, returned in
    the reply (e1-pg). ``page`` has no CLI command of its own (a wrapper needs a
    twin only when one exists), so the ``cli_line`` names ``lockwire check``, the
    nearest command that reads the same contract. ``run_of(contract)`` is a plugin's
    way to hand the page extra findings; the free handler passes none."""
    cli_line = f"lockwire check {args.contract}"
    contract_path = Path(args.contract)
    if not contract_path.exists():
        return _refusal(cli_line, 1, f"no contract at {contract_path}")
    try:
        loaded = load_contract(contract_path)
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 1, str(error))
    run = None
    if run_of is not None:
        try:
            run = run_of(loaded)
        except _LOAD_ERRORS as error:
            return _refusal(cli_line, 1, str(error))
    try:
        loaded_scene, scene_source = api.load_scene(contract_path, args.scene, args.package_root)
    except SceneRefusal as error:
        return _refusal(cli_line, 2, str(error))
    plan_outcomes = None
    plan_path = Path(args.plan) if args.plan is not None else None
    if plan_path is not None:
        try:
            plan_report = validate_plan(loaded, load_plan(plan_path))
        except _LOAD_ERRORS as error:
            return _refusal(cli_line, 1, str(error))
        plan_outcomes = plan_report.outcomes
    document = api.page(
        loaded,
        preflight=run,
        scene=loaded_scene,
        scene_source=scene_source,
        contract_path=contract_path,
        plan_path=plan_path,
        plan_outcomes=plan_outcomes,
        cli_line=cli_line,
    )
    # No `exit_code`/`verdict` in the document itself (`PagePayload` carries
    # neither, seam 1 in api_payload.py) — the page's own convention: 0 whenever
    # the page was computed, never a decision gate.
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _handle_page(args: PageArgs) -> RunReply:
    return page_reply(args)


def _handle_log(args: LogArgs) -> RunReply:
    cli_line_parts = ["lockwire", "log"]
    if args.revision_range is not None:
        cli_line_parts.append(args.revision_range)
    cli_line_parts += ["--", args.contract]
    return _ok(
        " ".join(cli_line_parts),
        exit_code=0,
        verdict=None,
        payload=api.log(args.contract, args.revision_range),
    )


def _handle_diff(args: DiffArgs) -> RunReply:
    cli_line = f"lockwire diff {args.a}"
    if args.b is not None:
        cli_line += f" {args.b}"
    cli_line += f" --file {args.contract}"
    return _ok(
        cli_line,
        exit_code=0,
        verdict=None,
        payload=api.diff(args.contract, args.a, args.b),
    )


def _parse_locked_at(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value is not None else None


def _handle_declare(args: DeclareArgs) -> RunReply:
    cli_line = f"lockwire declare {args.contract} {args.path}"
    document = api.declare(
        args.contract,
        args.path,
        args.value,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _handle_declare_form(args: DeclareFormArgs) -> RunReply:
    cli_line = f"lockwire declare-form {args.contract}"
    return _ok(
        cli_line,
        exit_code=0,
        verdict=None,
        payload=api.declare_form(args.contract),
    )


def _handle_add(args: AddArgs) -> RunReply:
    cli_line = f"lockwire add {args.kind} {args.contract}"
    document = api.add(
        args.contract,
        args.kind,
        id=args.id,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
        **args.fields,
    )
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _handle_review(args: ReviewArgs) -> RunReply:
    cli_line = f"lockwire review {args.contract} {args.path} {args.state}"
    document = api.review(
        args.contract,
        args.path,
        args.state,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _handle_commit(args: CommitArgs) -> RunReply:
    cli_line = f"lockwire commit {args.contract}"
    document = vcs.commit_contract(
        args.contract,
        message=args.message,
        paths=args.paths,
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(cli_line, exit_code=0, verdict=None, payload=document)


def _handle_undo(args: UndoArgs) -> RunReply:
    cli_line = f"lockwire undo {args.contract} --source {args.source}"
    plan = vcs.undo_plan(args.contract, source=args.source)
    if args.apply:
        if args.expected_revision is None:
            vcs.apply_undo(plan)
        else:
            vcs.apply_undo(plan, contract=args.contract, expected_revision=args.expected_revision)
    return _ok(cli_line, exit_code=0, verdict=None, payload=plan)


def _handle_place(args: PlaceArgs) -> RunReply:
    document = api.place(
        args.contract,
        args.target,
        args.on,
        args.offset,
        unit=args.unit,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(
        f"lockwire place {args.contract} {args.target}", exit_code=0, verdict=None, payload=document
    )


def _handle_move(args: MoveArgs) -> RunReply:
    document = api.move(
        args.contract,
        args.target,
        args.offset,
        unit=args.unit,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(
        f"lockwire move {args.contract} {args.target}", exit_code=0, verdict=None, payload=document
    )


def _handle_rotate(args: RotateArgs) -> RunReply:
    document = api.rotate(
        args.contract,
        args.target,
        args.rpy,
        unit=args.unit,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(
        f"lockwire rotate {args.contract} {args.target}",
        exit_code=0,
        verdict=None,
        payload=document,
    )


def _handle_reparent(args: ReparentArgs) -> RunReply:
    document = api.reparent(
        args.contract,
        args.target,
        args.to,
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(
        f"lockwire reparent {args.contract} {args.target}",
        exit_code=0,
        verdict=None,
        payload=document,
    )


def _handle_mount_shape(args: MountShapeArgs) -> RunReply:
    document = api.mount_shape(
        args.contract,
        args.target,
        args.shape.model_dump(exclude_none=True),
        source=args.source,
        witness=args.witness,
        note=args.note,
        locked_at=_parse_locked_at(args.locked_at),
        dry_run=args.dry_run,
        expected_revision=args.expected_revision,
    )
    return _ok(
        f"lockwire mount-shape {args.contract} {args.target}",
        exit_code=0,
        verdict=None,
        payload=document,
    )


def _handle_start(args: StartArgs) -> RunReply:
    cli_line = f"lockwire start {args.urdf} --out {Path(args.urdf).resolve().parent / 'lockwire.yaml'}"
    try:
        result = api.start(
            args.urdf, overwrite=args.overwrite, expected_revision=args.expected_revision
        )
    except _LOAD_ERRORS as error:
        return _refusal(cli_line, 2, str(error))
    return _ok(cli_line, exit_code=0, verdict=None, payload=result)


_FREE_COMMANDS = {
    "start": plugins.ServeCommand(_handle_start, StartArgs, StartCommand),
    "check": plugins.ServeCommand(_handle_check, CheckArgs, CheckCommand),
    "what-if": plugins.ServeCommand(_handle_what_if, WhatIfArgs, WhatIfCommand),
    "plan": plugins.ServeCommand(_handle_plan, PlanArgs, PlanCommand),
    "describe": plugins.ServeCommand(_handle_describe, DescribeArgs, DescribeCommand),
    "declare-form": plugins.ServeCommand(_handle_declare_form, DeclareFormArgs, DeclareFormCommand),
    "report": plugins.ServeCommand(_handle_report, ReportArgs, ReportCommand),
    "page": plugins.ServeCommand(_handle_page, PageArgs, PageCommand),
    "log": plugins.ServeCommand(_handle_log, LogArgs, LogCommand),
    "diff": plugins.ServeCommand(_handle_diff, DiffArgs, DiffCommand),
    "declare": plugins.ServeCommand(_handle_declare, DeclareArgs, DeclareCommand),
    "add": plugins.ServeCommand(_handle_add, AddArgs, AddCommand),
    "review": plugins.ServeCommand(_handle_review, ReviewArgs, ReviewCommand),
    "commit": plugins.ServeCommand(_handle_commit, CommitArgs, CommitCommand),
    "undo": plugins.ServeCommand(_handle_undo, UndoArgs, UndoCommand),
    "place": plugins.ServeCommand(_handle_place, PlaceArgs, PlaceCommand),
    "move": plugins.ServeCommand(_handle_move, MoveArgs, MoveCommand),
    "rotate": plugins.ServeCommand(_handle_rotate, RotateArgs, RotateCommand),
    "reparent": plugins.ServeCommand(_handle_reparent, ReparentArgs, ReparentCommand),
    "mount-shape": plugins.ServeCommand(_handle_mount_shape, MountShapeArgs, MountShapeCommand),
}


def commands() -> dict[str, plugins.ServeCommand]:
    """The commands this sidecar serves: the free ones, then whatever a plugin registered."""
    return {**_FREE_COMMANDS, **plugins.SERVE_COMMANDS}


def request_type(table: dict[str, plugins.ServeCommand] | None = None) -> Any:
    """The `/v1/run` body: a discriminated union of the table's command models, built
    when the app is, so a plugin's commands are in it and nothing else is."""
    table = commands() if table is None else table
    return Annotated[Union[tuple(c.command for c in table.values())], Field(discriminator="command")]


_REVISION_GUARDED_COMMANDS = frozenset(
    {"declare", "add", "review", "commit", "undo", "place", "move", "rotate", "reparent", "mount-shape"}
)


def _sidecar_write_needs_revision(body: Any) -> bool:
    """The app must name the exact bytes it opened before changing them."""
    if body.command not in _REVISION_GUARDED_COMMANDS:
        return False
    return body.args.apply if body.command == "undo" else not body.args.dry_run


def dispatch(body: Any, state: Any = None) -> RunReply:
    """Run one parsed request. A plugin's command gets `(body, state)`, `state` being
    the app's `app.state`; a free handler gets its `args` (`check` its whole command)."""
    try:
        if _sidecar_write_needs_revision(body) and body.args.expected_revision is None:
            return _refusal(
                f"lockwire {body.command}",
                2,
                "expected_revision is required before applying a sidecar edit; reload first",
            )
        if body.command in plugins.SERVE_COMMANDS:
            return plugins.SERVE_COMMANDS[body.command].handler(body, state)
        handler = _FREE_COMMANDS[body.command].handler
        return handler(body) if isinstance(body, CheckCommand) else handler(body.args)
    except Exception as error:  # noqa: BLE001 - never a traceback over the wire
        return _refusal(f"lockwire {body.command}", 2, str(error))


# ---------------------------------------------------------------------------
# The app. Bound to 127.0.0.1 only by ``lockwire serve``; every route needs the
# per-launch token, hello included.
#
# The token is checked in **middleware**, not in a route dependency, and that is
# the whole point: FastAPI reads and validates a request body *before* it solves
# a route's dependencies, so a `Depends(_auth)` guard lets an unauthenticated
# caller reach pydantic with an arbitrary body and read a 422 back. Middleware
# runs before routing, so an unauthenticated request is a 401 and nothing of it
# is ever parsed (`test_token_checked_before_body_is_parsed`).
#
# There is deliberately **no CORS middleware**: a browser page must not be able
# to reach this sidecar at all. The Host check below is the other half of that —
# without it a page on any site can point a name it owns at 127.0.0.1 (DNS
# rebinding) and the same-origin rules would let it through.

#: Commands that remain discoverable but are refused by name on the robot.
_WRITE_COMMANDS = frozenset(
    {
        "start",
        "declare",
        "add",
        "review",
        "commit",
        "undo",
        "place",
        "move",
        "rotate",
        "reparent",
        "mount-shape",
        "report",
    }
)
_HOST_ONLY_COMMANDS = frozenset({"diff", "log"})

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_is_loopback(host_header: str | None) -> bool:
    """``Host`` must name the loopback we bound. Absent is fine (a raw HTTP/1.0
    client); anything else is someone else's name."""
    if host_header is None:
        return True
    host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else "::1"
    return host in _LOOPBACK_HOSTS


def _token_ok(offered: str | None, expected: str) -> bool:
    """Constant time, always — a length or prefix leak is a token oracle."""
    if offered is None:
        return False
    return hmac.compare_digest(offered.encode("utf-8"), expected.encode("utf-8"))


_ACCESS_LOG = logging.getLogger("lockwire.serve")


def create_app(
    token: str,
    *,
    robot: bool = False,
    policy: Callable[[Any], Any] | None = None,
) -> FastAPI:
    """The app. `policy`, if given, rewrites each parsed request before it runs (a plugin's
    server-side limits); `plugins.load()` runs first so the command union includes its commands."""
    from fastapi import FastAPI, Request  # noqa: PLC0415 - the `serve` extra
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    plugins.ensure()
    table = commands()
    app = FastAPI(title="lockwire sidecar", version=__version__)
    app.state.token = token
    app.state.robot = robot
    for setup in plugins.SERVE_SETUP:
        setup(app.state)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202 - starlette's shape
        # Method, path and status only — never headers, so the token (carried
        # only in the X-Lockwire-Token header) never reaches the log,
        # on any of the three exits below.
        if not _host_is_loopback(request.headers.get("host")):
            _ACCESS_LOG.info("%s %s -> 400", request.method, request.url.path)
            return JSONResponse({"detail": "Host is not this loopback"}, status_code=400)
        if not _token_ok(request.headers.get("X-Lockwire-Token"), app.state.token):
            _ACCESS_LOG.info("%s %s -> 401", request.method, request.url.path)
            return JSONResponse({"detail": "missing or invalid X-Lockwire-Token"}, status_code=401)
        response = await call_next(request)
        _ACCESS_LOG.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response

    @app.get("/v1/hello", response_model=HelloReply)
    def hello() -> HelloReply:
        listed = [
            CommandInfo(name=name, args_schema=entry.args.model_json_schema())
            for name, entry in table.items()
        ]
        return HelloReply(version=__version__, commands=listed)

    def run(body):  # noqa: ANN001, ANN202 - annotated below, once the union exists
        if policy is not None:
            body = policy(body)
        if app.state.robot and body.command in _WRITE_COMMANDS:
            return _refusal(
                f"lockwire {body.command}",
                2,
                f"write commands are not served on the robot: {body.command}",
            )
        if app.state.robot and body.command in _HOST_ONLY_COMMANDS:
            return _refusal(
                f"lockwire {body.command}",
                2,
                f"host-only commands are not served on the robot: {body.command}",
            )
        return dispatch(body, app.state)

    run.__annotations__ = {"body": request_type(table), "return": RunReply}
    app.post("/v1/run", response_model=RunReply)(run)
    return app


# ---------------------------------------------------------------------------
# ``lockwire serve``: bind, mint a token, print the one ready line, run until
# stdin closes (the app was force-killed) or SIGTERM (uvicorn's own handler,
# installed because this runs on the main thread).


def _watch_stdin(server: uvicorn.Server) -> None:
    """A closed stdin is the whole orphan story: no stray process survives Electron."""
    while True:
        line = sys.stdin.readline()
        if line == "":
            server.should_exit = True
            return


def _log_dir() -> Path:
    """The platform's real log location — never the cwd (a packaged app's
    working directory is wherever it happened to be launched from) and never a
    location the release checklist's uninstall promise ("only the log folder"
    survives, docs/release_checklist.md §2) would have to special-case.

    macOS (the only shipping platform today): ``~/Library/Logs/Lockwire`` —
    the OS's own per-app log convention, shown by Console.app and untouched by an
    app bundle's own uninstall (dragging the .app to Trash). Linux/Windows follow
    each platform's equivalent convention so the same code holds if E6 ships them;
    no third-party dependency (stdlib + environment variables only).

    ``LOCKWIRE_LOG_DIR`` overrides everything above. The test suite sets it via an
    autouse fixture (``tests/conftest.py``) so no test run — including the
    subprocess-launched ``lockwire serve`` cases — ever writes into a developer's
    real ``~/Library/Logs``.
    """
    override = os.environ.get("LOCKWIRE_LOG_DIR")
    if override:
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    name = "Lockwire"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / name
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        base = base / name / "logs"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        base = base / "lockwire" / "log"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _configure_sidecar_logging(log_dir: Path) -> Path:
    """Rotating file, stdlib only (``logging.handlers.RotatingFileHandler`` — no
    dependency, no hand-rolled rotator): a size bound and a small number of kept
    files, oldest discarded, so a long-running sidecar cannot fill a disk and a
    user who reports "it stopped working" has real files to send.

    Formats only level/time/logger/message — request logging (the ``guard``
    middleware above) passes method, path and status, never headers, so the
    per-launch token never reaches this file.
    """
    log_path = log_dir / "sidecar.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=200_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _ACCESS_LOG.handlers = [handler]
    _ACCESS_LOG.setLevel(logging.INFO)
    _ACCESS_LOG.propagate = False
    return log_path


def run_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    robot: bool = False,
    exit_on_stdin_close: bool = False,
    profile: str | None = None,
    required_evidence: list[str] | None = None,
) -> int:
    """Serve the sidecar; returns a process exit code (never raises for a bind refusal).

    ``exit_on_stdin_close`` is opt-in, not always-on: the Electron parent passes it
    so no sidecar outlives a force-killed app, while a human running
    ``lockwire serve`` in a terminal (or CI redirecting stdin from /dev/null)
    keeps a server that does not vanish the moment stdin is not a live pipe.
    """
    if host not in _LOOPBACK_HOSTS:
        print(f"lockwire serve refuses to bind {host!r} — loopback only", file=sys.stderr)
        return 2

    policy = None
    if profile is not None or required_evidence:
        plugins.ensure()
        try:
            if plugins.SERVE_POLICY is None:
                raise ValueError("this install has no server policy to apply")
            policy = plugins.SERVE_POLICY(profile, required_evidence)
        except ValueError as error:
            print(f"lockwire serve refused: {error}", file=sys.stderr)
            return 2
    log_path = _configure_sidecar_logging(_log_dir())
    token = secrets.token_urlsafe(32)
    import uvicorn  # noqa: PLC0415 - the `serve` extra

    app = create_app(token, robot=robot, policy=policy)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    original_startup = server.startup

    async def startup(sockets=None) -> None:  # noqa: ANN001 - matches uvicorn's own signature
        await original_startup(sockets=sockets)
        if server.started:
            bound_port = server.servers[0].sockets[0].getsockname()[1]
            # Written straight to stdout, never through the rich console: the
            # parent parses this line, and one ANSI escape makes it unparseable.
            sys.stdout.write(
                json.dumps(
                    {
                        "lockwire": "ready",
                        "version": __version__,
                        "port": bound_port,
                        "token": token,
                        "log": str(log_path),
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            if exit_on_stdin_close:
                threading.Thread(target=_watch_stdin, args=(server,), daemon=True).start()

    server.startup = startup  # type: ignore[method-assign]
    server.run()
    return 0
