"""lockwire CLI: start, gui, init, check, describe, baseline, scan, preflight, hw, mcp, serve.

Every gated command's exit-code line is rendered from the one table in
``report.EXIT_CODE_TABLE`` (see `report.exit_code_help`), not copied by hand —
``check``/``plan``/``preflight``/``declare``/``health``/``hw`` all agree that a
bad or missing contract exits 2 (README.md:162-166 has the full map).
``hw`` records a W0 declaration and routes impact; same exit shape as ``declare``.
``gui``/``start`` are viewers, not gates: they exit 0 whenever the page was
written, so opening it in the browser never reports a verdict of its own.
``mcp`` serves lockwire's checks to an agent over MCP; the ``mcp`` package is an
optional extra, imported lazily so a plain install never needs it.
"""

import datetime as dt
import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from ruamel.yaml import YAML, YAMLError
from typer.core import TyperCommand

from lockwire import __version__, export_mhs, gitref
from lockwire import api, plugins
from lockwire.bench import DEFAULT_CORPUS_PATH, format_report, load_corpus, run_bench
from lockwire.checks import run_record_checks
from lockwire.cli_line import argv_from_context, cli_line as build_cli_line
from lockwire.describe import describe_all, describe_device, describe_system
from lockwire.edit import (
    Change,
    EditRefusal,
    add_entry,
    apply_changes,
    apply_review,
    contract_revision,
    move as edit_move,
    place as edit_place,
    relative_source,
    reparent as edit_reparent,
    resolve_witness,
    rotate as edit_rotate,
    set_mount_shape as edit_mount_shape,
)
from lockwire.factdiff import fact_diff, load_contract_text, render_lines
from lockwire.hw import record_hardware_change
from lockwire.plan import load_plan, validate_plan
from lockwire.report import (
    exit_code_help,
    render,
)
from lockwire.report_html import write_html_report
from lockwire.scan import (
    UnsupportedPlatformError,
    scan_machine,
)
from lockwire.scene import SceneRefusal
from lockwire.scene_import import (
    format_point,
    SceneImportRefusal,
    build_import,
    write_and_apply_import,
    write_change_file,
)
from lockwire.schema import (
    ArtifactRef,
    Contract,
    RobotInfo,
    UnsupportedSchemaVersion,
    save_contract,
)
from lockwire.schema import load_contract as _load_contract_lib
from lockwire.sdf import parse_sdf
from lockwire.urdf import _refuse_doctype_entity, parse_urdf

app = typer.Typer(
    name="lockwire",
    help="The physical-assumption contract for robots: physics lockfile + preflight gate.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


scene_app = typer.Typer(help="The scene door: read a 3D model of the robot.")
app.add_typer(scene_app, name="scene")

DEFAULT_CONTRACT = Path("lockwire.yaml")


def load_contract(path: Path) -> Contract:
    """The door onto ``schema.load_contract`` for every command that loads one
    itself.

    Most contract-taking commands call this module-global name, so a contract
    written by a newer lockwire is refused in exactly one place — the word
    REFUSED and exit 2 — instead of each command's own ``except``
    printing a plain "<cmd> failed:" line with whatever exit code it happened
    to pick (`gui`, `start` and `report` picked 1).

    The four that do not (`sample`, `declare`, `apply`, `add`) hand the path to
    a library that loads it (``sample_local``, ``edit``); those libraries raise
    the same typed ``UnsupportedSchemaVersion``, and ``_refused`` below renders
    it with the same word and code. No list of doors is kept here:
    ``test_every_verdict_door_refuses_an_unsupported_schema_version``
    enumerates them off the typer app, so a new door cannot go unnoticed.

    ``typer.Exit`` is a ``RuntimeError``, so the refusal passes straight
    through the ``except (OSError, TypeError, ValueError, YAMLError)`` blocks
    the commands wrap this call in. Every other failure is re-raised unchanged
    for those blocks to handle as they always have.
    """
    try:
        return _load_contract_lib(path)
    except UnsupportedSchemaVersion as error:
        console.print(f"[bold red]REFUSED[/] — {error}")
        raise typer.Exit(2) from error


def _refused(command: str, error: Exception) -> None:
    """Print one command failure line: REFUSED when the contract declares a
    schema_version this lockwire cannot read, the command's own "<cmd> failed:"
    line otherwise. For the commands that load their contract inside a library
    (`sample`, `declare`, `apply`, `add`) rather than through ``load_contract``.
    """
    if isinstance(error, UnsupportedSchemaVersion):
        console.print(f"[bold red]REFUSED[/] — {error}")
    else:
        console.print(f"[red]{command} failed:[/] {error}")


# invoke_without_command is what lets `--version` run at all: without it Click fails on
# the missing subcommand while parsing, before this body is ever reached, so the flag
# advertised in --help exited 2 with "Missing command". `no_args_is_help` on the Typer
# above still handles a bare `lockwire`.
@app.callback(invoke_without_command=True)
def _version_callback(
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        console.print(f"lockwire {__version__}")
        raise typer.Exit(0)


def _write_document(out: Path | None, document: dict) -> None:
    """Write one library document to ``out``, or do nothing when --json was not given."""
    if out is None:
        return
    out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]wrote {out}[/]")


def _frozen() -> bool:
    """True in the packaged binary (`packaging/lockwire.spec`), false from source.

    PyInstaller sets `sys.frozen`; nothing else in this repo does. This is the one
    frozen-mode guard — a later door that must refuse when packaged reuses it rather
    than inventing a second way to ask the same question.
    """
    return getattr(sys, "frozen", False)


def _refuse_default_out_when_frozen(ctx: typer.Context, param: str, command: str) -> None:
    """In the packaged binary, refuse a page path the caller did not choose.

    Frozen, this process is the desktop app's sidecar: its working directory is
    wherever the app was launched from, and there is no browser for it to open the page
    in. A defaulted relative `--out` would write the file somewhere the caller cannot
    find and then say nothing. One sentence, exit 2, and the caller names the file.
    """
    if not _frozen():
        return
    # By name, not by identity: typer and click each expose a `ParameterSource` enum and
    # they are not the same class, so `is ParameterSource.DEFAULT` silently never matched.
    source = ctx.get_parameter_source(param)
    if source is not None and source.name != "DEFAULT":
        return
    console.print(
        f"[red]{command} needs --out here:[/] this is the packaged sidecar, so there is no "
        "browser to open and no obvious folder to write into — say where the file goes."
    )
    raise typer.Exit(2)


def _file_kind_word(path: Path) -> str:
    """Name the file's root element (F3), not its extension: a `.sdf` some tool
    wrote as `<robot>` (or vice versa) is described for what it actually is."""
    raw = path.read_bytes()
    _refuse_doctype_entity(raw, path)  # before any parse touches the bytes
    root = ET.fromstring(raw)  # xacro refusal happens inside parse_urdf itself
    return "SDF" if root.tag == "sdf" else "URDF"


def _parse_model_file(path: Path, contract: Path) -> Any:
    """`init`/`start` accept a URDF or an SDF — dispatch by root element (F3),
    not only by extension: a `.sdf` some tool wrote as `<robot>` (or vice versa)
    is read for what it actually is.

    The model file's own path is the contract's first source, so it is stamped
    relative to the contract, never as the absolute path the caller typed.
    """
    model = parse_sdf(path) if _file_kind_word(path) == "SDF" else parse_urdf(path)
    spelling = relative_source(contract, path)
    return model.model_copy(
        update={
            "source": spelling,
            "provenance": model.provenance.model_copy(update={"source": spelling}),
        }
    )


def _find_scene(contract_path: Path, scene_flag: Path | None) -> tuple[Path | None, bool]:
    """Return (path, discovered). ``discovered`` is True only for sidecar auto-pickup."""
    if scene_flag is not None:
        return scene_flag, False
    for ext in (".glb", ".gltf", ".urdf", ".sdf"):
        candidate = contract_path.with_suffix(ext)
        if candidate.is_file():
            return candidate, True
    # `start` pins the model's meshes; a contract that carries them names its own scene.
    try:
        model = _load_contract_lib(contract_path).model
    except Exception:  # noqa: BLE001 - no readable contract means no scene to find
        return None, False
    if model is not None and model.external_assets:
        declared = contract_path.parent / model.source
        if declared.suffix.lower() in (".urdf", ".sdf") and declared.is_file():
            return declared, True
    return None, False


def _load_scene_for_cli(
    contract_path: Path,
    scene_flag: Path | None,
    *,
    command: str,
    package_roots: tuple[Path, ...] = (),
) -> tuple[Any, str | None]:
    """Load the scene named by --scene or discovered beside the contract, announcing it.

    `lockwire check` never sees the scene; this is only for gui/report. A
    ``SceneRefusal`` is the tool-failure posture (exit 2), same as an unresolved xacro.
    """
    scene_path, discovered = _find_scene(contract_path, scene_flag)
    if scene_path is None:
        return None, None
    try:
        loaded_scene, _ = api.load_scene(contract_path, scene_flag, package_roots)
    except SceneRefusal as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error
    if discovered:
        console.print(f"scene: {scene_path.name} (found beside the contract)")
        source = f"{scene_path.name} (found beside the contract)"
    else:
        console.print(f"scene: {scene_path.name}")
        source = f"{scene_path.name} (given)"
    return loaded_scene, source


@app.command(
    help="Start here: make a lockfile from your URDF.\n\n"
    "A fresh lockfile knows the frame and nothing else; the app shows what to fill in "
    "next.\n\n" + exit_code_help("start")
)
def start(
    ctx: typer.Context,
    urdf: Path = typer.Argument(..., exists=True, readable=True, help="Your robot's URDF or SDF."),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT, "--out", "-o", help="Lockfile to create or reuse."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing lockfile."),
) -> None:
    """Start here: make a lockfile from your URDF.

    A fresh lockfile knows the frame and nothing else; the app shows what to fill in next.
    """
    _refuse_default_out_when_frozen(ctx, "contract", "start")
    from lockwire.start_operation import start_contract

    try:
        result = start_contract(
            urdf,
            out=contract,
            overwrite=force and contract.exists(),
            expected_revision=contract_revision(contract) if force and contract.exists() else None,
        )
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]start failed:[/] {error}")
        raise typer.Exit(1) from error
    try:
        model = load_contract(contract).model
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]start failed:[/] {error}")
        raise typer.Exit(1) from error
    if not result["written"]:
        console.print(
            f"[yellow]{contract} already exists[/] — kept as is. "
            "Pass --force to start over from the URDF (this discards locked entries)."
        )
    else:
        console.print(f"[green]wrote {contract}[/] — {len(model.links)} links from {urdf}")
        if result.get("note"):
            console.print(f"[yellow]{result['note']}[/]")


@app.command()
def init(
    urdf: Path = typer.Argument(
        ..., exists=True, readable=True, help="URDF or SDF to bootstrap Tier A from."
    ),
    out: Path = typer.Option(DEFAULT_CONTRACT, "--out", "-o", help="Contract file to write."),
    robot: str = typer.Option(None, "--robot", help="Robot name (default: URDF stem)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing contract."),
) -> None:
    """Bootstrap a contract from a URDF: Tier A links, structure hash, zero-mass flags."""
    if out.exists() and not force:
        console.print(
            f"[red]{out} exists[/] — pass --force to overwrite (this discards locked entries)"
        )
        raise typer.Exit(1)

    # Unexpanded xacro is tool failure (§3.3 exit 2), not a NO-GO finding.
    try:
        model = _parse_model_file(urdf, out)
    except ValueError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error
    contract = Contract(robot=RobotInfo(name=robot or urdf.stem), model=model)
    save_contract(contract, out)

    console.print(
        f"[green]wrote {out}[/] — {len(model.links)} links, structure {model.structure_hash[:12]}…"
    )
    if model.zero_mass_links:
        console.print(
            f"[yellow]{len(model.zero_mass_links)} zero-mass link(s)[/]: "
            + ", ".join(model.zero_mass_links)
            + " — `lockwire check` will warn until resolved"
        )


@app.command(
    "upgrade",
    help=f"Write a new 0.4 contract from a pinned legacy file.\n\n{exit_code_help('upgrade')}",
)
def upgrade_cmd(
    contract: Path = typer.Argument(..., help="Legacy 0.2 or 0.3 contract to retain unchanged."),
    out: Path = typer.Option(..., "--out", help="New 0.4 contract path; never overwritten."),
    expected_revision: str = typer.Option(
        ..., "--expected-revision", help="Full SHA-256 of the legacy file's exact bytes."
    ),
) -> None:
    """Explicitly write a new 0.4 contract while retaining the legacy file."""
    try:
        api.upgrade(contract, out=out, expected_revision=expected_revision)
    except (OSError, UnicodeError, ValueError) as error:
        _refused("upgrade", error)
        raise typer.Exit(2) from error
    console.print(f"[green]wrote {out}[/] — legacy {contract} retained unchanged")


@app.command(
    help=f"Run record-level checks: do the records agree? (CI entry point)\n\n{exit_code_help('check')}"
)
def check(
    contract: Path = typer.Argument(DEFAULT_CONTRACT, help="Contract file to check."),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write this same run as JSON (the `lockwire.api.check` document)."
    ),
    actuator_evidence: list[str] | None = typer.Option(
        None,
        "--actuator-evidence",
        help="Repeat CASE_ID=RELATIVE_PATH@SHA256 for selected actuator-case evidence.",
    ),
    evidence_root: Path | None = typer.Option(
        None, "--evidence-root", help="Root containing the selected relative evidence paths."
    ),
    expected_revision: str | None = typer.Option(
        None, "--expected-revision", help="Full raw contract SHA-256 used to select evidence."
    ),
) -> None:
    """Run record-level checks: do the records agree? (CI entry point)"""
    if not contract.exists():
        console.print(f"[red]no contract at {contract}[/] — run `lockwire init <urdf>` first")
        raise typer.Exit(2)
    try:
        loaded = load_contract(contract)
        selected = _parse_actuator_evidence(actuator_evidence) if actuator_evidence else None
        selected, root, revision = api._actuator_check_context(
            loaded, contract, selected, evidence_root, expected_revision
        )
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]check failed:[/] {error}")
        raise typer.Exit(2) from error
    findings = run_record_checks(
        loaded,
        contract_path=contract,
        actuator_evidence=selected,
        evidence_root=root,
        contract_sha256=revision,
    )
    # The document is built by the library, never assembled here: `lockwire.api`
    # and this file must not be able to disagree about one run.
    # `check_document`, not `verdict_document` directly, so this JSON carries the
    # same `headline` `api.check` does -- one document builder, not two.
    _write_document(
        json_out,
        api.check_document(
            loaded,
            findings,
            revision=revision,
            actuator_evidence=selected,
        ),
    )
    raise typer.Exit(render(findings, console))


def _parse_actuator_evidence(values: list[str]) -> dict[str, ArtifactRef]:
    """Parse the Check CLI's repeatable, shell-friendly ArtifactRef spelling once."""
    parsed: dict[str, ArtifactRef] = {}
    for value in values:
        if value.count("=") != 1 or "@" not in value:
            raise ValueError("actuator evidence must be CASE_ID=RELATIVE_PATH@SHA256")
        case_id, encoded_ref = value.split("=", 1)
        relative_path, separator, sha256 = encoded_ref.rpartition("@")
        if not case_id or not separator or not relative_path or not sha256:
            raise ValueError("actuator evidence must be CASE_ID=RELATIVE_PATH@SHA256")
        if case_id in parsed:
            raise ValueError(f"duplicate actuator evidence for case {case_id!r}")
        parsed[case_id] = ArtifactRef(path=relative_path, sha256=sha256)
    return parsed


@app.command(
    "plan",
    help="Check an agent's intended plan against the contract before anything executes."
    f"\n\n{exit_code_help('plan')}",
)
def plan_cmd(
    contract: Path = typer.Argument(..., help="Contract file to check the plan against."),
    plan: Path = typer.Argument(..., help="Plan file (plan.yaml) to validate."),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write this same run as JSON (the `lockwire.api.plan` document)."
    ),
) -> None:
    """Check an agent's intended plan against the contract before anything executes."""
    if not contract.exists() or not plan.exists():
        console.print("[red]contract or plan file not found[/]")
        raise typer.Exit(2)
    try:
        loaded_contract = load_contract(contract)
        loaded_plan = load_plan(plan)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]plan check failed:[/] {error}")
        raise typer.Exit(2) from error
    findings = validate_plan(loaded_contract, loaded_plan)
    _write_document(json_out, plugins.pick("plan", api.plan)(loaded_contract, loaded_plan))
    raise typer.Exit(render(findings, console))


@app.command(
    "bench",
    help="Score a corpus of generated out-of-bound plans: detection (via `plan`'s own "
    "evaluator — missed/false-alarm/unscorable) and forwarding (bytes, order, count, "
    "regardless of verdict) measured separately, on the given contract."
    f"\n\n{exit_code_help('bench')}",
)
def bench_cmd(
    contract: Path = typer.Argument(..., help="Contract file to score the corpus against."),
    corpus: Path = typer.Option(
        DEFAULT_CORPUS_PATH,
        "--corpus",
        help="Corpus YAML file (id/category/step per case).",
    ),
) -> None:
    """Run `tests/fixtures/bench/corpus.yaml` (by default) against `contract`."""
    if not contract.exists() or not corpus.exists():
        console.print("[red]contract or corpus file not found[/]")
        raise typer.Exit(2)
    try:
        loaded_contract = load_contract(contract)
        cases = load_corpus(corpus)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]bench failed:[/] {error}")
        raise typer.Exit(2) from error
    result, _sink = run_bench(loaded_contract, cases)
    console.print(format_report(result, corpus))
    raise typer.Exit(result.exit_code)


@app.command()
def report(
    ctx: typer.Context,
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Contract file to report.",
    ),
    scene: Path | None = typer.Option(
        None,
        "--scene",
        help="A glTF scene (.glb/.gltf) or a URDF/SDF (.urdf/.sdf) of the robot; parts named "
        "by contract id in extras, or by link name for a URDF/SDF.",
    ),
    package_root: list[Path] = typer.Option(
        [],
        "--package-root",
        help="A folder containing a ROS package (repeatable), for resolving package:// / "
        "model:// mesh URIs in a URDF/SDF scene.",
    ),
    out: Path = typer.Option(..., "--out", "-o", help="Self-contained HTML file to write."),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="report never opens a browser; the flag is accepted so scripts can pass "
        "it everywhere.",
    ),
    redact_paths: bool = typer.Option(
        False,
        "--redact-paths",
        help="Shorten absolute paths to their basename and drop owner/witness names — "
        "for a page you're about to send someone.",
    ),
) -> None:
    """Emit a deterministic, self-contained contract report."""
    del open_browser  # accepted for CLI parity with gui/start; no-op.
    try:
        loaded = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]report failed:[/] {error}")
        raise typer.Exit(1) from error
    # Announces/refuses the scene for CLI parity; the printable report
    # does not yet draw it — that is the 3D renderer's ticket, not this one's.
    _load_scene_for_cli(contract, scene, command="report", package_roots=tuple(package_root))
    # --out is excluded and rendered as a placeholder, same convention as gui's
    # own line: the page's bytes must not depend on the chosen output filename.
    cli_line = build_cli_line(
        "lockwire", "report", *argv_from_context(ctx, exclude={"out"}), "--out", "<this file>"
    )
    write_html_report(
        loaded, out, contract_path=contract, redact_paths=redact_paths, cli_line=cli_line
    )
    console.print(f"[green]wrote {out}[/]")


@app.command()
def describe(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Contract file to describe.",
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Directory to write one Markdown file per device, plus SYSTEM.md."
    ),
) -> None:
    """Print a per-device reference file, generated from the contract."""
    try:
        loaded = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]describe failed:[/] {error}")
        raise typer.Exit(2) from error

    if out is None:
        for device in loaded.devices:
            console.print(describe_device(loaded, device))
        # The system page carries the force numbers. Printing only the
        # per-device pages would leave stdout without the figures --out writes.
        console.print(describe_system(loaded))
        return

    try:
        out.mkdir(parents=True, exist_ok=True)
        files = describe_all(loaded)
        for name, content in files.items():
            (out / name).write_text(content, encoding="utf-8")
    except OSError as error:
        console.print(f"[red]describe failed:[/] could not write to {out}: {error}")
        raise typer.Exit(2) from error

    for name in sorted(files):
        console.print(f"[green]wrote {out / name}[/]")


@app.command("export")
def export_cmd(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Contract file to export.",
    ),
    fmt: str = typer.Option(..., "--format", help="Output shape. Only 'mhs' exists."),
) -> None:
    """Render the contract into a third-party device-manifest shape. Outward only."""
    if fmt != "mhs":
        console.print(f"[red]export failed:[/] unknown --format {fmt!r}; only 'mhs' exists.")
        raise typer.Exit(2)
    try:
        loaded = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]export failed:[/] {error}")
        raise typer.Exit(2) from error
    console.print_json(data=export_mhs.export_mhs(loaded))


@app.command(
    "import",
    help="Refused: importing a third-party manifest into a contract is not built."
    f"\n\n{exit_code_help('import')}",
)
def import_cmd(
    manifest: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Manifest file (refused)."
    ),
) -> None:
    """Refused: importing a third-party manifest into a contract is not built."""
    console.print(
        "[bold red]REFUSED[/] — The inbound direction — importing a third-party manifest "
        "into a contract — is refused until Anthropic publishes the standard; import is "
        "where lock-in happens."
    )
    raise typer.Exit(2)


@app.command()
def scan(
    robot: str = typer.Option(
        "scanned-robot", "--robot", help="Robot name for the draft candidate."
    ),
) -> None:
    """Enumerate this Linux/ROS 2 machine into a draft, never an authoritative lock."""
    try:
        draft = scan_machine(robot)
    except UnsupportedPlatformError as error:
        console.print(f"[red]scan failed:[/] {error}")
        raise typer.Exit(1) from error

    yaml = YAML()
    yaml.default_flow_style = False
    output = io.StringIO()
    yaml.dump(draft.model_dump(mode="json", exclude_none=True), output)
    typer.echo(output.getvalue(), nl=False)


def _parse_hw_timestamp(raw: str | None) -> dt.datetime | None:
    if raw is None:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when


@scene_app.command(
    "import",
    help="Import device placements from a 3D scene into a change file.\n\n"
    f"{exit_code_help('scene import')}",
)
def scene_import_cmd(
    ctx: typer.Context,
    scene: Path = typer.Argument(
        ..., help="A glTF scene (.glb/.gltf) or a URDF/SDF (.urdf/.sdf) of the robot."
    ),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT,
        "--contract",
        "-c",
        help="Contract file to resolve ids against; --write also applies the changes.",
    ),
    witness: str | None = typer.Option(
        None, "--witness", help="Who/what witnesses these placements, e.g. 'human'."
    ),
    source_person: str | None = typer.Option(
        None, "--source-person", help="Name of the person confirming each placement."
    ),
    out: Path = typer.Option(
        Path("changes.yaml"), "--out", "-o", help="Where to write the change file."
    ),
    only: list[str] = typer.Option([], "--only", help="Limit to these device ids (repeatable)."),
    declared_too: bool = typer.Option(
        False,
        "--declared-too",
        help="Also correct devices that already declare mount_xyz but disagree with "
        "the model past 5 mm.",
    ),
    package_root: list[Path] = typer.Option(
        [],
        "--package-root",
        help="A folder containing a ROS package (repeatable), for resolving package:// / "
        "model:// mesh URIs in a URDF/SDF scene.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply the change file immediately (needs `lockwire apply`)."
    ),
    expected_revision: str | None = typer.Option(
        None,
        "--expected-revision",
        help="Full raw contract SHA-256 required with --write.",
    ),
) -> None:
    """Read undeclared device placements from a scene into an apply-ready change file.

    The scene alone never mutates the contract; --write applies the
    imported changes. Every entry gets its own source naming the model node
    and the confirming person, plus the given witness.
    For a URDF/SDF the parts are links (ids = link names); a device joins only
    when its id equals a link name, or the model gives it extras.lockwire.id.
    """
    if witness is None or source_person is None:
        console.print("[red]scene import failed:[/] --witness and --source-person are required.")
        raise typer.Exit(2)
    if write and expected_revision is None:
        console.print("[red]scene import failed:[/] --write requires --expected-revision.")
        raise typer.Exit(2)
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]scene import failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    if write and expected_revision != contract_revision(contract):
        console.print(
            "[red]scene import failed:[/] contract changed since it was opened; "
            "reload before applying this edit"
        )
        raise typer.Exit(2)
    try:
        loaded_contract = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]scene import failed:[/] {error}")
        raise typer.Exit(2) from error
    link_based = scene.suffix.lower() in (".urdf", ".sdf", ".xacro")
    try:
        loaded_scene, _ = api.load_scene(contract, scene, tuple(package_root))
        assert loaded_scene is not None
    except SceneRefusal as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error

    try:
        result = build_import(
            loaded_contract,
            loaded_scene,
            witness=witness,
            source_person=source_person,
            only=list(only) or None,
            declared_too=declared_too,
            link_based=link_based,
        )
    except SceneImportRefusal as error:
        console.print(f"[red]scene import failed:[/] {error}")
        raise typer.Exit(2) from error

    if result.explanation is not None:
        console.print(result.explanation)

    console.print("device · model position (m) · declared · action")
    for row in result.rows:
        model_str = format_point(row.model_position) if row.model_position is not None else "-"
        declared_str = format_point(row.declared) if row.declared is not None else "-"
        console.print(f"  {row.device_id} · {model_str} · {declared_str} · {row.action}")
    if result.skipped_non_devices:
        skipped = ", ".join(result.skipped_non_devices)
        console.print(f"skipped (not devices): {skipped}")

    try:
        if write:
            write_and_apply_import(
                contract,
                result.entries,
                out,
                scene_source=loaded_scene.source,
                expected_revision=expected_revision,
            )
        else:
            write_change_file(result.entries, out, scene_source=loaded_scene.source)
    except (EditRefusal, SceneImportRefusal, OSError, YAMLError) as error:
        console.print(f"[red]scene import failed:[/] {error}")
        raise typer.Exit(2) from error
    console.print(f"wrote {out} ({len(result.entries)} placements, witness {witness})")

    console.print(
        "ran: " + build_cli_line("lockwire", "scene", "import", *argv_from_context(ctx)),
        soft_wrap=True,
    )

    if write:
        console.print(f"applied {out} to {contract}")
    else:
        console.print(f"next: lockwire apply {contract} {out}")


@app.command(
    "hw",
    help="Record a W0 hardware-change event and route it to touched entries/owners."
    f"\n\n{exit_code_help('hw')}",
)
def hw(
    declaration: str = typer.Argument(
        ...,
        help="Hardware-change declaration, e.g. 'added FLIR, 42 g, nose mount'.",
    ),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT,
        "--contract",
        "-c",
        help="Contract file to route against (never modified).",
    ),
    device: str | None = typer.Option(
        None,
        "--device",
        help="Existing device id to bind bus/rail routing from the contract.",
    ),
    bus: str | None = typer.Option(
        None,
        "--bus",
        help="Bus id the change sits on (when not derived from --device).",
    ),
    rail: str | None = typer.Option(
        None,
        "--rail",
        help="Rail id the change draws from (when not derived from --device).",
    ),
    at: str | None = typer.Option(
        None,
        "--at",
        help="Event timestamp (ISO-8601). Default: now UTC. Pin for byte-identical output.",
    ),
) -> None:
    """Record a W0 hardware-change event and route it to touched entries/owners.

    Appends to a sidecar ``<contract-stem>.hw.yaml`` beside the contract. Does
    not mutate the contract. See ``report.EXIT_CODE_TABLE["hw"]`` for exit codes.
    """
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]hw failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    try:
        recorded_at = _parse_hw_timestamp(at)
        loaded = load_contract(contract)
        event, log_path, routing = record_hardware_change(
            loaded,
            contract,
            declaration,
            recorded_at=recorded_at,
            device=device,
            bus=bus,
            rail=rail,
        )
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]hw failed:[/] {error}")
        raise typer.Exit(2) from error

    qty = ", ".join(str(q) for q in event.quantities) if event.quantities else "(none parsed)"
    console.print(f"[green]recorded[/] {event.declaration}")
    console.print(f"  quantities: {qty}")
    console.print(f"  at: {event.recorded_at.isoformat()}")
    console.print(f"  log: {log_path}")
    console.print("possibly touched:")
    if routing.touched:
        for entry in routing.touched:
            owner = entry.owner if entry.owner is not None else "(no owner)"
            console.print(f"  {entry.path}  owner={owner}  ({entry.reason})")
    else:
        console.print("  (none)")
    if routing.undetermined:
        console.print("[yellow]undetermined:[/]")
        for gap in routing.undetermined:
            console.print(f"  {gap}")


def _parse_locked_at(raw: str | None) -> dt.date | None:
    return dt.date.fromisoformat(raw) if raw is not None else None


def _print_edit_result(result) -> None:
    if result.diff:
        console.print(result.diff, end="")
    for line in result.lines:
        console.print(f"[green]{line}[/]")
    if not result.written:
        console.print("[yellow](dry run — nothing written)[/]")


@app.command(
    "declare",
    help="Declare one fact in the contract: a value with a source and a witness."
    f"\n\n{exit_code_help('declare')}",
)
def declare_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    path: str = typer.Argument(
        ..., help="'/'-separated contract path, e.g. devices/dev.x/physical/mount_xyz."
    ),
    value: str = typer.Argument(
        ..., help="Value to declare (number, '25 W', a quoted string, true/false)."
    ),
    source: str = typer.Option(..., "--source", help="Where the fact comes from (required)."),
    witness: str | None = typer.Option(
        None,
        "--witness",
        help="Who/what measured it; omitted falls back to the per-machine config "
        'default, marked "(default)" — still refused if neither is given.',
    ),
    note: str | None = typer.Option(None, "--note", help="Appended to the owning block's note."),
    locked_at: str | None = typer.Option(
        None, "--locked-at", help="ISO date to stamp instead of today (tests only)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Declare one fact in the contract: a value with a source and a witness.

    Comments, key order and quoting elsewhere in the file survive (ruamel
    round-trip). Refuses without --source/--witness or a value the schema
    rejects; nothing is written on refusal. A --witness omitted (rather than
    passed blank) may still be supplied by the per-machine config default,
    marked "(default)" — see ``resolve_witness``.
    """
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]declare failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    try:
        change = Change(
            path=path,
            value=value,
            source=source,
            witness=resolve_witness(witness),
            note=note,
            locked_at=_parse_locked_at(locked_at),
        )
        result = apply_changes(contract, [change], dry_run=dry_run)
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("declare", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "declare", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command(
    "what-if",
    help="Compare a proposal without changing the contract or health ledger."
    f"\n\n{exit_code_help('what-if')}",
)
def what_if_cmd(
    contract: Path = typer.Argument(..., help="Contract to compare."),
    change: list[str] = typer.Option(..., "--change", help="Repeat PATH=VALUE for each proposal."),
    source: str = typer.Option(..., "--source", help="Proposal source."),
    witness: str | None = typer.Option(
        None, "--witness", help="Proposal witness; uses declare's default."
    ),
) -> None:
    try:
        changes = []
        for entry in change:
            path, separator, value = entry.partition("=")
            if not separator or not path or not value:
                raise EditRefusal("--change requires PATH=VALUE")
            changes.append(
                Change(path=path, value=value, source=source, witness=resolve_witness(witness))
            )
        document = plugins.pick("what_if", api.what_if)(contract, changes)
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("what-if", error)
        raise typer.Exit(2) from error
    console.print_json(json.dumps(document))


@app.command(
    "review",
    help="Approve or question one already-declared value, with a note and a witness."
    f"\n\n{exit_code_help('review')}",
)
def review_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    path: str = typer.Argument(
        ..., help="'/'-separated contract path of the already-declared fact to review."
    ),
    approve: bool = typer.Option(False, "--approve", help="Mark the value approved."),
    question: bool = typer.Option(False, "--question", help="Mark the value questioned."),
    note: str | None = typer.Option(
        None, "--note", help="The reviewer's note (required for --question)."
    ),
    witness: str = typer.Option(..., "--witness", help="Who is reviewing it (required)."),
    locked_at: str | None = typer.Option(
        None, "--locked-at", help="ISO date to stamp instead of today (tests only)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Approve or question one declared value, appending the reviewer's note
    into the file. Exactly one of --approve/--question is required; a
    questioned review needs a --note — that gate, and the
    blank/"you"/assistant-name witness refusal, live in ``edit.apply_review``,
    the same gate every review door shares.
    """
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]review failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    if approve == question:
        console.print("[red]review failed:[/] exactly one of --approve or --question is required")
        raise typer.Exit(2)
    state = "approved" if approve else "questioned"
    try:
        result = apply_review(
            contract,
            path,
            state,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("review", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "review", *argv_from_context(ctx)), soft_wrap=True
    )


def _pose_triple(raw: str, flag: str) -> tuple[float, float, float]:
    """Parse ``x,y,z`` (commas or spaces) into three floats, or refuse legibly."""
    parts = [p for p in raw.replace(",", " ").split() if p]
    if len(parts) != 3:
        console.print(
            f"[red]refused:[/] {flag} needs three numbers, e.g. {flag} 0.010,0.000,0.025 "
            f"— got {raw!r}"
        )
        raise typer.Exit(2)
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        console.print(f"[red]refused:[/] {flag} {raw!r} is not three numbers")
        raise typer.Exit(2) from None


def _run_pose_edit(ctx: typer.Context, name: str, contract: Path, call) -> None:
    """Shared existence, refusal and replay handling for pose and mount edits."""
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]{name} failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    try:
        result = call()
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused(name, error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", name, *argv_from_context(ctx)), soft_wrap=True
    )


@app.command(
    "place",
    help="Place a device on a model link, writing its frame and offset together."
    f"\n\n{exit_code_help('place')}",
)
def place_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    target: str = typer.Argument(..., help="Existing device id."),
    on: str = typer.Option(..., "--on", help="Model link that holds the device."),
    offset: str = typer.Option(..., "--offset", help="x,y,z in --unit."),
    unit: str | None = typer.Option(None, "--unit", help="Length unit (required)."),
    source: str = typer.Option(..., "--source", help="Where the placement comes from."),
    witness: str = typer.Option(..., "--witness", help="Who placed it."),
    note: str | None = typer.Option(None, "--note"),
    locked_at: str | None = typer.Option(None, "--locked-at", help="ISO date (tests only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Place a device by writing mount_frame and mount_xyz in one transaction."""
    triple = _pose_triple(offset, "--offset")
    _run_pose_edit(
        ctx,
        "place",
        contract,
        lambda: edit_place(
            contract,
            target,
            on,
            triple,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        ),
    )


@app.command(
    "mount-shape",
    help="Set a device's plate, bracket, tube or custom-mesh mount geometry."
    f"\n\n{exit_code_help('mount-shape')}",
)
def mount_shape_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    target: str = typer.Argument(..., help="Existing device id."),
    kind: str = typer.Argument(..., help="plate | bracket | tube | custom-mesh"),
    dimensions_mm: str = typer.Option(..., "--dimensions-mm", help="Three positive dimensions."),
    fastener_pattern: str | None = typer.Option(None, "--fastener-pattern"),
    mesh_path: str | None = typer.Option(None, "--mesh-path"),
    source: str = typer.Option(..., "--source", help="Where the geometry comes from."),
    witness: str = typer.Option(..., "--witness", help="Who declared it."),
    note: str | None = typer.Option(None, "--note"),
    locked_at: str | None = typer.Option(None, "--locked-at", help="ISO date (tests only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Write one mount_shape record through the same contract writer as declare."""
    dimensions = _pose_triple(dimensions_mm, "--dimensions-mm")
    shape = {
        "kind": kind,
        "dimensions_mm": list(dimensions),
        **({"fastener_pattern": fastener_pattern} if fastener_pattern is not None else {}),
        **({"mesh_path": mesh_path} if mesh_path is not None else {}),
    }
    _run_pose_edit(
        ctx,
        "mount-shape",
        contract,
        lambda: edit_mount_shape(
            contract,
            target,
            shape,
            source=source,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        ),
    )


@app.command(
    "move",
    help="Move a device or link: set its offset in the frame that already holds it."
    f"\n\n{exit_code_help('move')}",
)
def move_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    target: str = typer.Argument(..., help="Device id, or a link name in the recorded model."),
    offset: str = typer.Option(..., "--offset", help="x,y,z in --unit, e.g. 0.010,0,0.025."),
    unit: str | None = typer.Option(None, "--unit", help="Length unit: m, cm or mm (required)."),
    source: str = typer.Option(..., "--source", help="Where the fact comes from (required)."),
    witness: str = typer.Option(..., "--witness", help="Who measured it (required)."),
    note: str | None = typer.Option(None, "--note", help="Appended to the owning block's note."),
    locked_at: str | None = typer.Option(
        None, "--locked-at", help="ISO date to stamp instead of today (tests only)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Move a device in its mount frame, or a link in its parent link's frame.

    Writes `devices/<id>/physical/mount_xyz` for a device and the placing joint's
    `origin/xyz` for a link, through the same writer `declare` uses.
    """
    triple = _pose_triple(offset, "--offset")
    _run_pose_edit(
        ctx,
        "move",
        contract,
        lambda: edit_move(
            contract,
            target,
            triple,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        ),
    )


@app.command(
    "rotate",
    help="Rotate a link: set its placing joint's orientation, unit required."
    f"\n\n{exit_code_help('rotate')}",
)
def rotate_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    target: str = typer.Argument(..., help="Link name in the recorded model."),
    rpy: str = typer.Option(..., "--rpy", help="roll,pitch,yaw in --unit, e.g. 0,0,1.5708."),
    unit: str | None = typer.Option(None, "--unit", help="Angle unit: rad or deg (required)."),
    source: str = typer.Option(..., "--source", help="Where the fact comes from (required)."),
    witness: str = typer.Option(..., "--witness", help="Who measured it (required)."),
    note: str | None = typer.Option(None, "--note", help="Appended to the owning block's note."),
    locked_at: str | None = typer.Option(
        None, "--locked-at", help="ISO date to stamp instead of today (tests only)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Set a link's orientation in its parent's frame, written in radians.

    A device target is refused: `schema.Physical` records no orientation and the
    schema is frozen. A missing `--unit` is refused, never defaulted.
    """
    triple = _pose_triple(rpy, "--rpy")
    _run_pose_edit(
        ctx,
        "rotate",
        contract,
        lambda: edit_rotate(
            contract,
            target,
            triple,
            unit=unit,
            source=source,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        ),
    )


@app.command(
    "reparent",
    help="Re-parent a device's mount frame or a link's joint parent."
    f"\n\n{exit_code_help('reparent')}",
)
def reparent_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    target: str = typer.Argument(..., help="Device id, or a link name in the recorded model."),
    to: str = typer.Option(..., "--to", help="The link it should hang off instead."),
    source: str = typer.Option(..., "--source", help="Where the fact comes from (required)."),
    witness: str = typer.Option(..., "--witness", help="Who decided it (required)."),
    note: str | None = typer.Option(None, "--note", help="Appended to the owning block's note."),
    locked_at: str | None = typer.Option(
        None, "--locked-at", help="ISO date to stamp instead of today (tests only)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Change what a device is mounted to, or what a link hangs off.

    Refuses, naming which: a parent that does not resolve, a re-parent that closes
    a cycle, and one that would orphan the moved subtree. A link re-parent moves
    `structure_hash`, and the new hash is printed rather than moved silently.
    """
    _run_pose_edit(
        ctx,
        "reparent",
        contract,
        lambda: edit_reparent(
            contract,
            target,
            to,
            source=source,
            witness=witness,
            note=note,
            locked_at=_parse_locked_at(locked_at),
            dry_run=dry_run,
        ),
    )


@app.command("apply")
def apply_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    changes_file: Path = typer.Argument(
        ..., help="YAML list of {path, value, source, witness, note?}."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the combined diff, write nothing."
    ),
) -> None:
    """Apply many declared facts from one change file, atomically.

    All entries validate before anything is written; a bad entry refuses the
    whole apply and names the failing entry (index + path). Exit 0 on
    success, 2 on refusal.
    """
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]apply failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    if not changes_file.exists() or not changes_file.is_file():
        console.print(f"[red]apply failed:[/] no changes file at {changes_file}")
        raise typer.Exit(2)
    try:
        with open(changes_file, encoding="utf-8") as handle:
            rows = YAML().load(handle) or []
        if not isinstance(rows, list):
            raise EditRefusal(f"{changes_file} must contain a YAML list of changes")
        changes = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise EditRefusal(f"entry {index}: must be a mapping")
            missing = [k for k in ("path", "value", "source", "witness") if k not in row]
            if missing:
                raise EditRefusal(f"entry {index}: missing {', '.join(missing)}")
            changes.append(
                Change(
                    path=str(row["path"]),
                    value=row["value"],
                    source=str(row["source"]),
                    witness=str(row["witness"]),
                    note=row.get("note"),
                    unit=row.get("unit"),
                )
            )
        result = apply_changes(contract, changes, dry_run=dry_run)
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("apply", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "apply", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="device | rail | bus | family | model | budget | battery"),
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    id: str | None = typer.Option(None, "--id", help="New entry's id (not used for battery)."),
    source: str = typer.Option(..., "--source", help="Where every field comes from (required)."),
    witness: str = typer.Option(..., "--witness", help="Who/what declared it (required)."),
    note: str | None = typer.Option(None, "--note"),
    model: str | None = typer.Option(None, "--model", help="device: -> DeviceModel.id."),
    owner: str | None = typer.Option(
        None, "--owner", help="device/budget: owning team (both required)."
    ),
    mass: str | None = typer.Option(None, "--mass", help="device: physical.mass, e.g. '25 g'."),
    mount_frame: str | None = typer.Option(
        None, "--mount-frame", help="device: physical.mount_frame."
    ),
    rail: str | None = typer.Option(
        None, "--rail", help="device: power.rail id. budget: rail_power's Rail.id."
    ),
    draw: str | None = typer.Option(None, "--draw", help="device: power.draw, e.g. '5 W'."),
    bus_ref: str | None = typer.Option(None, "--bus-ref", help="device: identity.bus_ref id."),
    host: str | None = typer.Option(None, "--host", help="device/bus: host id."),
    voltage: str | None = typer.Option(None, "--voltage", help="rail: voltage, e.g. '5.2 V'."),
    capacity: str | None = typer.Option(None, "--capacity", help="rail: capacity, e.g. '15.6 W'."),
    upstream: str | None = typer.Option(None, "--upstream", help="rail: upstream Rail.id."),
    efficiency: str | None = typer.Option(None, "--efficiency", help="rail: converter efficiency."),
    bus_kind: str | None = typer.Option(
        None, "--bus-kind", help="bus: usb|ethernet|serial|can|i2c|spi."
    ),
    bitrate: str | None = typer.Option(None, "--bitrate", help="bus: raw bits/s."),
    name: str | None = typer.Option(
        None,
        "--name",
        help="family: DeviceFamily.name (required). device: names a family this device "
        "creates via --family, when --family is new (optional — defaults from the id).",
    ),
    min_capture_window_s: str | None = typer.Option(
        None, "--min-capture-window-s", help="family: seconds, e.g. '60'."
    ),
    notes: str | None = typer.Option(None, "--notes", help="family: DeviceFamily.notes."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="model: -> DeviceFamily.id. device: -> DeviceFamily.id, created with --model "
        "if either is new.",
    ),
    input_voltage_min: str | None = typer.Option(
        None, "--input-voltage-min", help="model: input_voltage_range.min, e.g. '5.0 V'."
    ),
    input_voltage_max: str | None = typer.Option(
        None, "--input-voltage-max", help="model: input_voltage_range.max, e.g. '5.5 V'."
    ),
    budget_kind: str | None = typer.Option(
        None, "--kind", help="budget: rail_power|endurance|mass_total (required)."
    ),
    limit: str | None = typer.Option(None, "--limit", help="budget: limit, e.g. '15.6 W'."),
    draw_mode: str | None = typer.Option(
        None, "--draw-mode", help="budget: named power mode this limit applies to."
    ),
    sources: str | None = typer.Option(
        None, "--sources", help="budget (mass_total only): devices|links|links+devices."
    ),
    nominal_voltage: str | None = typer.Option(
        None, "--nominal-voltage", help="battery: voltage, e.g. '25.9 V'."
    ),
    chemistry: str | None = typer.Option(
        None,
        "--chemistry",
        help="battery: chemistry, e.g. 'Li-ion' (no schema field — folded into a comment).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Add a new device/rail/bus/family/model/budget/battery block, with provenance.

    Note (F2, model): `DeviceModel` carries no schema field for a human name —
    only `hard_limits`, `actuator`, `thermal_limit`, `radio` and `match` beyond
    what this command exposes, all sparse/nested datasheet shapes; author them
    with `declare`/`apply` for now, the same way `scene import` writes
    structured facts.

    Note (battery): `Battery` (schema.py) has no `provenance` field and
    forbids extras — its source/witness/chemistry are recorded as a YAML
    comment on the `battery:` key, not a structured block; `check`/`describe`
    never read it back. `battery` creates or updates the singleton block
    (there is no id, and no duplicate refusal).

    One datasheet, one submit: ``add device`` with both --family and
    --model creates the family and/or model when they do not already exist,
    the device, and all three in one write — one witness line for the whole
    submit (a family/model created this way carries a provenance that points
    at the device instead of repeating the witness's name). --model alone,
    with no --family, keeps the old behaviour: refuses an unknown model.

    Refuses a duplicate id, an unknown rail/bus/family/model reference, or a
    value the schema rejects; nothing is written on refusal. Exit 0 on
    success, 2 on refusal.
    """
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]add failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    fields = {
        "id": id,
        "model": model,
        "owner": owner,
        "physical_mass": mass,
        "physical_mount_frame": mount_frame,
        "power_rail": rail,
        "power_draw": draw,
        "identity_bus_ref": bus_ref,
        "identity_host": host,
        "voltage": voltage,
        "capacity": capacity,
        "upstream": upstream,
        "efficiency": efficiency,
        "kind": bus_kind,
        "host": host,
        "bitrate": bitrate,
        "name": name,
        "min_capture_window_s": min_capture_window_s,
        "notes": notes,
        "family": family,
        "input_voltage_min": input_voltage_min,
        "input_voltage_max": input_voltage_max,
        "budget_kind": budget_kind,
        "limit": limit,
        "draw_mode": draw_mode,
        "sources": sources,
        "rail": rail,
        "nominal_voltage": nominal_voltage,
        "chemistry": chemistry,
    }
    try:
        result = add_entry(
            contract, kind, fields, source=source, witness=witness, note=note, dry_run=dry_run
        )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("add", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "add", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command("gearbox")
def gearbox_cmd(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    id: str = typer.Option(..., "--id", help="New gearbox id."),
    ratio: str = typer.Option(..., "--ratio", help="Reduction ratio; must be > 0."),
    input_joint: str = typer.Option(..., "--input-joint", help="Input model joint name."),
    output_joint: str = typer.Option(..., "--output-joint", help="Output model joint name."),
    source: str = typer.Option(..., "--source", help="Where the gearbox data comes from."),
    witness: str = typer.Option(..., "--witness", help="Who declared it."),
    note: str | None = typer.Option(None, "--note"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Add a gearbox template with provenance to a contract."""
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]gearbox failed:[/] no contract at {contract}")
        raise typer.Exit(2)
    try:
        result = add_entry(
            contract,
            "gearbox",
            {"id": id, "ratio": ratio, "input_joint": input_joint, "output_joint": output_joint},
            source=source,
            witness=witness,
            note=note,
            dry_run=dry_run,
        )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("gearbox", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "gearbox", *argv_from_context(ctx)),
        soft_wrap=True,
    )


@app.command("mcp")
def mcp_cmd() -> None:
    """Serve lockwire's checks, descriptions, and plan validation to an agent over MCP (stdio)."""
    try:
        from lockwire.mcp_server import build_server
    except ImportError:
        import sys

        print("install the mcp extra: uv sync --extra mcp", file=sys.stderr)
        raise typer.Exit(2) from None
    build_server().run("stdio")


def _diff_side(
    spec: str, contract: Path, repo: Path | None, *, exit_code: bool, side: str
) -> Contract:
    """One side of `lockwire diff`: a contract file on disk, or the contract at a git ref.

    A spec naming an existing file is read from disk and never touches git, so
    `diff a.yaml b.yaml` works outside a repository. Anything else is a ref,
    and every ref question goes through `gitref` (the one git door).

    Both branches load through ``_load_contract_lib`` directly (not the
    module-global ``load_contract`` wrapper) so an unsupported schema version
    is caught in exactly one place below, whichever side it came from, and the
    refusal names ``side`` and ``spec`` — either side
    declaring a schema version this lockwire can't read is REFUSED, and the
    message says which side.
    """
    on_disk = Path(spec)
    text: str | None = None
    if not (on_disk.exists() and on_disk.is_file()):
        if repo is None:
            console.print("not a git repository — nothing to compare against")
            raise typer.Exit(0)
        if gitref.rev_parse(spec, repo=repo) is None:
            console.print(f"[red]no such ref[/] {spec!r}")
            raise typer.Exit(2)
        relpath = gitref.relpath_in_repo(contract, repo)
        if relpath is None:
            console.print(f"[red]{contract} is outside the repository at {repo}[/]")
            raise typer.Exit(2)
        text = gitref.show(spec, relpath, repo=repo)
        if text is None:
            console.print(f"{contract.name} did not exist at {spec}")
            raise typer.Exit(1 if exit_code else 0)
    try:
        return load_contract_text(text) if text is not None else _load_contract_lib(on_disk)
    except UnsupportedSchemaVersion as error:
        console.print(f"[bold red]REFUSED[/] — side {side} ({spec!r}): {error}")
        raise typer.Exit(2) from error
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]diff failed:[/] {error}")
        raise typer.Exit(2) from error


@app.command("diff")
def diff_cmd(
    a: str = typer.Argument("HEAD", help="Ref (or contract path) to compare from. Default: HEAD."),
    b: str | None = typer.Argument(
        None, help="Ref (or contract path) to compare to. Default: the file on disk."
    ),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT, "--file", "-f", help="Contract file the refs are resolved against."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fact diff as JSON."),
    exit_code: bool = typer.Option(
        False, "--exit-code", help="Exit 1 when anything changed, like `git diff --exit-code`."
    ),
) -> None:
    """What changed in the facts — declared values, entries, verdict — not in the text."""
    if b is None and not contract.exists():
        console.print(f"[red]no contract at {contract}[/] — run `lockwire init <urdf>` first")
        raise typer.Exit(2)
    repo = gitref.repo_root(contract if contract.exists() else Path.cwd())
    before = _diff_side(a, contract, repo, exit_code=exit_code, side="a")
    after = _diff_side(
        b if b is not None else str(contract), contract, repo, exit_code=exit_code, side="b"
    )

    diff = fact_diff(before, after)
    if json_output:
        typer.echo(json.dumps(diff.as_json(), indent=2))
    else:
        for line in render_lines(diff):
            # markup off: a provenance bracket in a value is text, not rich markup.
            console.print(line, markup=False, highlight=False)
    raise typer.Exit(1 if (exit_code and diff.changed) else 0)


class _LogCommand(TyperCommand):
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        ctx.meta["log_path_separator"] = "--" in args
        return super().parse_args(ctx, args)


def _split_log_pathspec(
    tokens: list[str], *, path_separator: bool
) -> tuple[str | None, str | None]:
    """Split git's ``[<revision-range>] [-- <path>]`` shape for one contract."""
    if len(tokens) > 2:
        raise ValueError("log accepts one revision range and one contract path")
    if not tokens:
        if path_separator:
            raise ValueError("log requires a contract path after --")
        return None, None
    if not path_separator:
        if len(tokens) == 2:
            raise ValueError("separate the contract path with --")
        return tokens[0], None
    return (None, tokens[0]) if len(tokens) == 1 else (tokens[0], tokens[1])


@app.command("log", cls=_LogCommand)
def log_cmd(
    ctx: typer.Context,
    pathspec: list[str] = typer.Argument(
        None, help="[<revision-range>] [-- <path>]. Default: full history, the contract file."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit each commit as a JSON object."),
) -> None:
    """Git history for the contract: who, when, verdict, and fact diff."""
    try:
        revision_range, path = _split_log_pathspec(
            pathspec or [], path_separator=bool(ctx.meta["log_path_separator"])
        )
    except ValueError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error
    contract = Path(path) if path is not None else DEFAULT_CONTRACT
    repo = gitref.repo_root(contract if contract.exists() else Path.cwd())
    if repo is None:
        console.print("not a git repository")
        raise typer.Exit(0)
    try:
        entries = api.log(contract, revision_range)
    except ValueError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(2) from error
    if json_output:
        typer.echo(json.dumps(entries, indent=2))
        raise typer.Exit(0)
    if not entries:
        console.print("no commits touch this file")
        raise typer.Exit(0)
    for entry in entries:
        console.print(
            f"{entry['sha'][:7]}  {entry['date']}  {entry['author']}  "
            f"{entry['verdict']}  {entry['message']}",
            markup=False,
            highlight=False,
        )
    raise typer.Exit(0)


@app.command("blame")
def blame_cmd(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT, help="Contract file whose declared facts to attribute."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit provenance rows as JSON."),
) -> None:
    """One provenance row per declared fact, with its witness and git commit."""
    try:
        entries, in_repository = api.blame(contract)
    except (UnsupportedSchemaVersion, OSError, TypeError, ValueError, YAMLError) as error:
        _refused("blame", error)
        raise typer.Exit(2) from error
    if json_output:
        typer.echo(json.dumps(entries, indent=2))
        raise typer.Exit(0)
    if not entries:
        console.print("no declared facts")
        raise typer.Exit(0)
    if not in_repository:
        console.print("not a git repository — rows have no commit attribution")
    for entry in entries:
        commit = entry.get("last_changing_commit", "unattributed")
        compared_from = (
            f"  compared_from={entry['compared_from']}" if entry.get("compared_from") else ""
        )
        console.print(
            f"{entry['path']}  {entry['witness']}  {entry['source']}{compared_from}  {commit}",
            markup=False,
            highlight=False,
        )
    raise typer.Exit(0)


@app.command(
    "api-schema",
    # One short line on purpose: `_help_commands` in tests/test_parity.py reads
    # the first token of every line in `--help`'s Commands panel, so a
    # description that wraps invents a command that does not exist.
    help="Write the payload union's JSON schema, for the app's TypeScript types.",
)
def api_schema_cmd(
    out: Path = typer.Option(..., "--out", help="Where to write the JSON schema document."),
) -> None:
    """Write the payload union's JSON schema (E1-b) — every member document
    lockwire's doors emit, which is what the TypeScript types are generated from
    (packaging/generate-types.md). The library builds it, never this file
   : `api.api_schema` is the twin.
    """
    api.api_schema(out)
    console.print(f"wrote {out}")


@app.command(
    "api-types",
    # One short line on purpose: `_help_commands` in tests/test_parity.py reads
    # the first token of every line in `--help`'s Commands panel, so a
    # description that wraps invents a command that does not exist.
    help="Write app/src/shared/api.gen.ts directly from the payload union.",
)
def api_types_cmd(
    out: Path = typer.Option(..., "--out", help="Where to write the generated TypeScript."),
) -> None:
    """Write the payload union's TypeScript types (e1-d3) — no generator
    dependency, walked straight from `api_payload.py` (v8 line 265: the
    mainstream generator's own docs say a discriminated union like ours
    narrows badly through it). The library builds it, never this file
   : `api.api_types` is the twin.
    """
    api.api_types(out)
    console.print(f"wrote {out}")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Bind address. Loopback only — anything else is refused."
    ),
    port: int = typer.Option(
        0, "--port", help="Port to bind, or 0 to pick a free one (the default)."
    ),
    robot: bool = typer.Option(
        False,
        "--robot",
        help="Robot mode: write commands are absent, not refused (plan v8 line 418).",
    ),
    exit_on_stdin_close: bool = typer.Option(
        False,
        "--exit-on-stdin-close",
        help="Exit when stdin closes. The app passes this so no sidecar outlives it.",
    ),
    profile: str | None = typer.Option(
        None, "--profile", hidden=True, help="Server policy a plugin enforces."
    ),
    required_evidence: list[str] | None = typer.Option(
        None,
        "--require-evidence",
        hidden=True,
        help="Evidence a plugin's server policy requires; repeat for each class.",
    ),
) -> None:
    """Start the sidecar: the same core, over local HTTP, for the Electron app.

    Private to the app: loopback only, a fresh token every launch, no
    compatibility promise. A caller outside the app uses `lockwire.api`, this
    CLI's `--json`, or MCP.

    Prints one JSON line on stdout when ready, then serves until SIGTERM — or,
    with --exit-on-stdin-close, until its stdin closes.
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

        from lockwire.serve import run_server
    except ImportError:
        print("install the serve extra: uv sync --extra serve", file=sys.stderr)
        raise typer.Exit(2) from None
    raise typer.Exit(
        run_server(
            host=host,
            port=port,
            robot=robot,
            exit_on_stdin_close=exit_on_stdin_close,
            profile=profile,
            required_evidence=required_evidence,
        )
    )


plugins.load(app=app)  # after every free command is defined, so paid ones append to the same app
