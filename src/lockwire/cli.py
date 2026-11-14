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

import csv
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from ruamel.yaml import YAML, YAMLError
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from lockwire import __version__, export_mhs, export_model, gitref
from lockwire import api, plugins, units
from lockwire.bench import DEFAULT_CORPUS_PATH, format_report, load_corpus, run_bench
from lockwire.catalog import load_catalog, load_robots, recognise
from lockwire.checks import SIDE as _SIDE
from lockwire.checks import bus_address_map
from lockwire.checks import is_lock_drift, run_record_checks, servo_stall_torque, shown_path
from lockwire.confine import confine
from lockwire.cli_line import argv_from_context, cli_line, cli_line as build_cli_line
from lockwire.describe import describe_all, describe_device, describe_system
from lockwire.edit import (
    Change,
    EditRefusal,
    add_entry,
    delete_entry,
    apply_changes,
    apply_review,
    contract_revision,
    move as edit_move,
    place as edit_place,
    relative_source,
    rename_entry,
    reparent as edit_reparent,
    resolve_witness,
    rotate as edit_rotate,
    set_mount_shape as edit_mount_shape,
)
from lockwire.factdiff import fact_diff, load_contract_text, render_lines
from lockwire.hw import record_hardware_change
from lockwire import power_budget
from lockwire.plan import load_plan, validate_plan
from lockwire.report import (
    RecordReport,
    exit_code_help,
    render,
    strict_exit,
    verdict,
    verdict_word,
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
    ContractError,
    RobotInfo,
    UnsupportedSchemaVersion,
    save_contract,
)
from lockwire.schema import load_contract as _load_contract_lib
from lockwire.sdf import parse_sdf
from lockwire.start_operation import MODEL_SUFFIXES
from lockwire.urdf import ModelDefect, _refuse_doctype_entity, parse_urdf, robot_name


class _TildeGroup(TyperGroup):
    """Expand a quoted ``~`` the shell left alone: ``"~/robot.yaml"`` means $HOME.

    The group rewrites the words handed to the chosen subcommand, but only a bare ``~`` or
    a ``~/`` prefix, and only where the parameter's type is a path: free text such as
    ``--note '~/todo'`` reaches the command unchanged.
    """

    def list_commands(self, ctx):
        first = ("check", "start", "accept", "mcp", "agent-setup")
        names = super().list_commands(ctx)
        return sorted(names, key=lambda n: first.index(n) if n in first else len(first))

    def resolve_command(self, ctx, args):
        name, command, rest = super().resolve_command(ctx, args)
        if command is None:
            return name, command, rest
        options = {o: p for p in command.params if isinstance(p, TyperOption) for o in p.opts}
        arguments = [p for p in command.params if isinstance(p, TyperArgument)]
        out: list[str] = []
        pending: TyperOption | TyperArgument | None = None  # option whose value is the next word
        positional = 0
        after_dashes = False
        for word in rest:
            param: TyperOption | TyperArgument | None = None
            if pending is not None:
                param, pending = pending, None
            elif not after_dashes and word == "--":
                after_dashes = True
            elif not after_dashes and word.startswith("-") and word != "-":
                flag, eq, value = word.partition("=")
                option = options.get(flag)
                if option is not None and eq and _is_path(option):
                    word = flag + "=" + _tilde(value)
                elif option is not None and not eq and not option.is_flag and option.nargs == 1:
                    pending = option
            elif arguments:
                param = arguments[min(positional, len(arguments) - 1)]
                positional += 1
            out.append(_tilde(word) if param is not None and _is_path(param) else word)
        return name, command, out


def _is_path(param) -> bool:
    return "Path" in type(param.type).__name__  # typer's own TyperPath (it vendors click)


def _tilde(word: str) -> str:
    return os.path.expanduser(word) if word == "~" or word.startswith("~/") else word


# `--help` lists 5 verbs (check start accept mcp agent-setup, in that order); every
# other free verb is hidden=True, still callable, and listed by `lockwire help --all`.
app = typer.Typer(
    cls=_TildeGroup,
    name="lockwire",
    help="The physical-assumption contract for robots: checks that a robot's declared parts (power, weight, bus, torque) add up.",
    no_args_is_help=True,
    epilog=f"Also: {cli_line('lockwire', 'report')}, {cli_line('lockwire', 'export', '--format', 'bom|budget')}. More: {cli_line('lockwire', 'help', '--all')}",
    add_completion=False,
    pretty_exceptions_enable=False,  # a crash reaches `main`, which reports it as REFUSED
)
console = Console(emoji=False)


scene_app = typer.Typer(cls=_TildeGroup, help="The scene door: read a 3D model of the robot.")
app.add_typer(scene_app, name="scene", hidden=True)

catalog_app = typer.Typer(
    cls=_TildeGroup, help="The bundled catalog: robots with datasheets, and parts."
)
app.add_typer(catalog_app, name="catalog", hidden=True)

materials_app = typer.Typer(cls=_TildeGroup, help="Catalog materials, tried on a link.")
app.add_typer(materials_app, name="materials", hidden=True)

DEFAULT_CONTRACT = Path("lockwire.yaml")
_UPGRADE_HINT = "`lockwire upgrade` writes a 0.7 copy"


def load_contract(path: Path, out: Console = console) -> Contract:
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
        out.print(f"[bold red]REFUSED[/] — {escape(str(error))}")
        raise typer.Exit(2) from error
    except ContractError as error:  # the user's file, not a lockwire bug: plain lines, exit 2
        for line in error.lines:
            if _UPGRADE_HINT in line:
                shown = shown_path(path)
                copy = str(Path(shown).with_suffix("")) + ".0.6" + Path(shown).suffix
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                line = line.replace(
                    _UPGRADE_HINT,
                    "run "
                    + cli_line(
                        "lockwire", "upgrade", shown, "--out", copy, "--expected-revision", sha
                    ),
                )
            out.print(escape(line), soft_wrap=True, highlight=False)
        raise typer.Exit(2) from error


def _not_found(path: Path) -> str:
    """Why a model path cannot be opened: a dangling or looping symlink is not "not found"."""
    return "broken or looping symlink" if path.is_symlink() else "file not found"


def _robot_by_name(word: Path, code: int, out: Console = console) -> Path | None:
    """A bare word that is no file is a robot name, e.g. `lockwire check go2`."""
    if word.exists() or str(word) != word.stem:
        return None
    from lockwire.start_operation import robot_by_name

    try:
        return robot_by_name(word.name)
    except LookupError as error:
        out.print(str(error), style="red", markup=False)
        raise typer.Exit(code) from error


def plain_os_error(error: OSError) -> str:
    """A file-system failure as one plain line naming the real file (never a temp file)."""
    where = Path(str(error.filename2 or error.filename or "")).expanduser()
    if where.name.startswith("."):  # a temp file beside the target: name the folder instead
        where = where.parent
    shown = shown_path(where) if str(where) else "the file"
    if shown == ".":
        shown = "this folder"
    if isinstance(error, FileExistsError):
        return f"{shown} already exists and is never overwritten; pick another name"
    if isinstance(error, FileNotFoundError):
        more = ", or make one with lockwire start <robot.urdf>" if shown.endswith(".yaml") else ""
        return f"{shown} was not found; check the path{more}"
    if isinstance(error, IsADirectoryError):
        return f"{shown} is a folder, not a file; give a file inside it"
    if isinstance(error, PermissionError) or error.errno == 30:  # 30: read-only file system
        return (
            f"cannot write to {shown}: it is read-only or not yours; pick a folder you can write to"
        )
    return f"{shown}: {error.strerror or error}"


def _xacro_args(items: list[str], out: Console) -> dict[str, str]:
    """`--xacro-arg name=value` as a dict; a bare word is refused in one line, never dropped."""
    bad = next((a for a in items if "=" not in a), None)
    if bad is not None:
        out.print(f"--xacro-arg must look like name=value, not {escape(repr(bad))}")
        raise typer.Exit(2)
    return dict(a.split("=", 1) for a in items)


def _why(error: Exception) -> str:
    return plain_os_error(error) if isinstance(error, OSError) else str(error)


def _refused(command: str, error: Exception, out: Console = console) -> None:
    """Print one command failure line: REFUSED when the contract declares a
    schema_version this lockwire cannot read, the command's own "<cmd> failed:"
    line otherwise. For the commands that load their contract inside a library
    (`sample`, `declare`, `apply`, `add`) rather than through ``load_contract``.
    """
    if isinstance(error, UnsupportedSchemaVersion):
        out.print(f"[bold red]REFUSED[/] — {escape(str(error))}")
    elif isinstance(error, ContractError):
        for line in error.lines:
            out.print(escape(line), soft_wrap=True, highlight=False)
    else:
        why = plain_os_error(error) if isinstance(error, OSError) else error
        out.print(f"[red]{command} failed:[/] {escape(str(why))}")


# invoke_without_command is what lets `--version` run at all: without it Click fails on
# the missing subcommand while parsing, before this body is ever reached, so the flag
# advertised in --help exited 2 with "Missing command". `no_args_is_help` on the Typer
# above still handles a bare `lockwire`.
@app.callback(invoke_without_command=True)
def _version_callback(
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        # a source checkout has .git at its root; a wheel or the packaged app never does
        dev = " (dev build)" if (Path(__file__).parents[2] / ".git").exists() else ""
        console.print(f"lockwire {__version__}{dev}")
        raise typer.Exit(0)


def _confined(out: Path, contract: Path, root: Path | None = None) -> Path:
    """``confine.confine`` for the CLI: a refusal is one line and exit 2."""
    try:
        return confine(out, contract, root)
    except ValueError as error:
        console.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(2) from error


def _require_folder(out: Path) -> None:
    """A missing folder is the tool failing, not a NO-GO: one plain line, exit 2."""
    if not out.resolve().parent.is_dir():
        console.print(
            f"[red]could not write {escape(str(out))}:[/] the folder {escape(str(out.parent))} does not exist"
        )
        raise typer.Exit(2)


def _write_document(out: Path | None, document: dict, contract: Path) -> None:
    """Write one library document to ``out``, or do nothing when --json was not given."""
    if out is None:
        return
    if str(out) == "-":
        print(json.dumps(document, indent=2, default=str))
        return
    target = _confined(out, contract)
    _require_folder(target)
    try:
        target.write_text(json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as error:  # a missing folder is the tool failing, not a NO-GO
        console.print(
            f"[red]could not write {escape(str(out))}:[/] {escape(str(error.strerror or error))}"
        )
        raise typer.Exit(2) from error
    console.print(f"[green]wrote {escape(str(out))}[/]")


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
    """Name the file's root element, not its extension: a `.sdf` some tool
    wrote as `<robot>` (or vice versa) is described for what it actually is."""
    raw = path.read_bytes()
    _refuse_doctype_entity(raw, path)  # before any parse touches the bytes
    root = ET.fromstring(raw)  # xacro refusal happens inside parse_urdf itself
    return "SDF" if root.tag == "sdf" else "URDF"


def _parse_model_file(path: Path, contract: Path) -> Any:
    """`init`/`start` accept a URDF or an SDF — dispatch by root element,
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


def _is_stand_in(contract_path: Path, candidate: Path) -> bool:
    """A glb/gltf beside a shape-declaring lockfile whose parts name none of its ids."""
    if candidate.suffix not in (".glb", ".gltf"):
        return False
    try:
        contract = _load_contract_lib(contract_path)
        if not any(k.collision or k.geometry for k in contract.model.links):
            return False
        from lockwire.scene import load_scene  # noqa: PLC0415 - only on a shaped lockfile
        from lockwire.scene_view import contract_ids, resolve_scene  # noqa: PLC0415

        claimed = set(resolve_scene(contract, load_scene(candidate)).part_ids)
        return not claimed & contract_ids(contract)
    except Exception:  # noqa: BLE001 - unreadable scene or no model: the old pick
        return False


def _find_scene(contract_path: Path, scene_flag: Path | None) -> tuple[Path | None, bool]:
    """Return (path, discovered). ``discovered`` is True only for sidecar auto-pickup."""
    if scene_flag is not None:
        return scene_flag, False
    # A sidecar .glb/.gltf that describes none of the lockfile's parts is a stand-in: skipped when
    # the links declare shapes (they draw from those). A real mesh scene is kept; its declared
    # shapes are drawn with it (api.load_scene). URDF/SDF sidecars are real models, kept.
    for ext in (".glb", ".gltf", ".urdf", ".sdf"):
        candidate = contract_path.with_suffix(ext)
        if candidate.is_file() and not _is_stand_in(contract_path, candidate):
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
        console.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(2) from error
    if discovered:
        console.print(f"scene: {scene_path.name} (found beside the contract)")
        source = f"{scene_path.name} (found beside the contract)"
    else:
        console.print(f"scene: {scene_path.name}")
        source = f"{scene_path.name} (given)"
    return loaded_scene, source


@app.command(
    help="Lock the model so CI catches drift.\n\n"
    "A fresh lockfile knows the frame and nothing else; the app shows what to fill in "
    "next.\n\n" + exit_code_help("start")
)
def start(
    ctx: typer.Context,
    urdf: Path | None = typer.Argument(None, help="Your robot's URDF, xacro or SDF (check also reads MuJoCo and Webots files)."),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT, "--out", "-o", help="Lockfile to create or reuse."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing lockfile."),
    example: str | None = typer.Option(
        None,
        "--example",
        help="Write a ready example lockfile instead: a starter (drone, rover, arm, quadruped, humanoid), arm-power or servo-bench (built to fail on purpose, to show a catch), or a parametric one (hexapod-param, rover-param).",
    ),
    xacro_arg: list[str] = typer.Option(
        [], "--xacro-arg", help="A xacro argument, name=value. Repeat for several."
    ),
    mass: str | None = typer.Option(
        None, "--mass", help="Datasheet mass to compare with the model, e.g. '20.6 kg'."
    ),
    source: str | None = typer.Option(None, "--source", help="Where --mass comes from (url/text)."),
    tolerance_pct: float = typer.Option(5.0, "--tolerance-pct", help="Percent --mass may differ."),
    witness: str | None = typer.Option(
        None, "--witness", help="Who declared --mass (default: you)."
    ),
    no_check: bool = typer.Option(False, "--no-check", help="Write the lockfile, run no check."),
    blank: bool = typer.Option(
        False,
        "--blank",
        help="Write an empty lockfile with no URDF, to design the robot with `add`.",
    ),
) -> None:
    """Lock the model so CI catches drift.

    A fresh lockfile knows the frame and nothing else; the app shows what to fill in next.
    """
    _refuse_default_out_when_frozen(ctx, "contract", "start")
    _require_folder(contract)
    if example is not None:
        from lockwire.start_operation import ALIASES, example_label, example_names, write_example

        names = example_names()
        if example not in names and example not in ALIASES:
            console.print(
                f"[red]no example named {escape(repr(example))}[/] — available: {escape(', '.join(map(example_label, names)))}"
            )
            raise typer.Exit(1)
        if contract.exists() and not force:
            console.print(
                f"[yellow]{escape(str(contract))} already exists[/] — kept as is. Pass --force to overwrite."
            )
            return
        note = write_example(example, contract)
        console.print(
            f"[green]wrote {escape(str(contract))}[/] — example {example_label(example)}. "
            f"Next: {cli_line('lockwire', 'check', contract)}. Parts: {cli_line('lockwire', 'catalog', 'parts')}",
            soft_wrap=True,
        )
        if note:
            console.print(note)
        return
    if blank:
        from lockwire.start_operation import start_blank

        kept = contract.exists() and not force
        if not kept:
            contract.unlink(missing_ok=True)
            start_blank(contract)
        console.print(
            f"[yellow]{escape(str(contract))} already exists[/] — kept as is. Pass --force to overwrite."
            if kept
            else f"[green]wrote {escape(str(contract))}[/] — empty. Next: "
            + cli_line(
                "lockwire",
                "add",
                "link",
                contract,
                "--name",
                "base",
                "--shape",
                "box 0.1 0.1 0.05",
                "--material",
                "Aluminium 6061-T6",
                "--source",
                "me",
                "--witness",
                "me",
            ),
            soft_wrap=True,
        )
        return
    cache_note = ""
    if urdf is not None and (named := _robot_by_name(urdf, 1)):
        from lockwire.start_operation import copy_beside

        name, urdf = urdf.name, named  # a kept lockfile (no --force) writes nothing, no copy
        try:
            if force or not contract.exists():
                urdf = copy_beside(named, contract.resolve().parent, name)
        except ValueError as error:
            console.print(f"[red]start failed:[/] {escape(_why(error))}", soft_wrap=True)
            raise typer.Exit(1) from error
        cache_note = " The meshes stay in the robot_descriptions cache."
    if urdf is not None and not urdf.is_file():
        console.print(f"[red]{_not_found(urdf)}: {escape(str(urdf))}[/]", highlight=False)
        raise typer.Exit(2)
    if urdf is not None and urdf.suffix.lower() in (".yaml", ".yml"):
        console.print(
            f"{escape(str(urdf))} is not a URDF, xacro or SDF file; to check a lockfile: lockwire check {escape(str(urdf))}"
        )
        raise typer.Exit(1)
    if urdf is None:
        console.print(
            "[red]give your robot's URDF/SDF, or pick a shape: --example"
            " arm|drone|rover|quadruped|humanoid (servo-bench is built to fail, to show a catch)[/]"
        )
        raise typer.Exit(1)
    from lockwire.start_operation import start_contract

    existed = contract.exists()
    result: dict = {"written": False}
    try:
        result = start_contract(
            urdf,
            out=contract,
            overwrite=force and contract.exists(),
            expected_revision=contract_revision(contract) if force and contract.exists() else None,
            xacro_args=_xacro_args(xacro_arg, console),
        )
        if mass and result["written"]:
            from lockwire.start_operation import add_mass_check

            add_mass_check(
                contract, mass, source=source, tolerance_pct=tolerance_pct, witness=witness
            )
    # exit 1 is `start`'s documented code for a URDF/SDF that cannot be parsed (EXIT_CODE_TABLE).
    except (OSError, SyntaxError, TypeError, ValueError, YAMLError) as error:
        if result["written"] and not existed:
            contract.unlink(missing_ok=True)  # a failed start leaves no new lockfile behind
        console.print(f"[red]start failed:[/] {escape(_why(error))}")
        raise typer.Exit(1) from error
    try:
        model = load_contract(contract).model
    except (OSError, SyntaxError, TypeError, ValueError, YAMLError) as error:
        if result["written"] and not existed:
            contract.unlink(missing_ok=True)
        console.print(f"[red]start failed:[/] {escape(_why(error))}")
        raise typer.Exit(1) from error
    if not result["written"]:
        console.print(
            f"[yellow]{escape(str(contract))} already exists[/] — kept as is. "
            "Pass --force to start over from the URDF (this discards locked entries)."
        )
    else:
        console.print(
            f"[green]wrote {escape(str(contract))}[/] — {len(model.links)} links from {escape(str(urdf))}"
        )
        if result.get("note"):
            console.print(f"[yellow]{escape(result['note'])}{cache_note}[/]", soft_wrap=True)
        if not no_check:
            from lockwire.start_operation import rerun_command

            from lockwire.start_operation import accept_hint

            findings = run_record_checks(load_contract(contract), contract_path=contract)
            rerun_command(findings, cli_line("lockwire", "check", contract))
            accept_hint(findings, contract)
            render(findings, console, more=cli_line("lockwire", "check", contract, "--all"))


@app.command(hidden=True)
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
            f"[red]{escape(str(out))} exists[/] — pass --force to overwrite (this discards locked entries)"
        )
        raise typer.Exit(1)

    # Unexpanded xacro is tool failure (exit 2), not a NO-GO finding.
    try:
        model = _parse_model_file(urdf, out)
    except (SyntaxError, ValueError) as error:  # SyntaxError: ET.ParseError on malformed XML
        console.print(f"[red]init failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    contract = Contract(robot=RobotInfo(name=robot or robot_name(urdf)), model=model)
    _require_folder(out)
    save_contract(contract, out)

    console.print(
        f"[green]wrote {escape(str(out))}[/] — {len(model.links)} links, structure {model.structure_hash[:12]}…"
    )
    if model.zero_mass_links:
        console.print(
            f"[yellow]{len(model.zero_mass_links)} zero-mass link(s)[/]: "
            + ", ".join(model.zero_mass_links)
            + " — `lockwire check` will warn until resolved"
        )


@app.command(
    "upgrade",
    hidden=True,
    help=f"Write a new current-version contract from a pinned legacy file.\n\n{exit_code_help('upgrade')}",
)
def upgrade_cmd(
    contract: Path = typer.Argument(
        ..., help="Legacy 0.2, 0.3, 0.4, 0.5 or 0.6 contract to retain unchanged."
    ),
    out: Path = typer.Option(..., "--out", help="New contract path; never overwritten."),
    expected_revision: str = typer.Option(
        ..., "--expected-revision", help="Full SHA-256 of the legacy file's exact bytes."
    ),
) -> None:
    """Explicitly write a new current-version contract while retaining the legacy file."""
    try:
        notices = api.upgrade(contract, out=out, expected_revision=expected_revision)
    except (OSError, UnicodeError, ValueError) as error:
        _refused("upgrade", error)
        raise typer.Exit(2) from error
    console.print(
        f"[green]wrote {escape(str(out))}[/] — legacy {escape(str(contract))} retained unchanged"
    )
    for notice in notices:
        console.print(
            f"[yellow]warning:[/] {escape(str(notice))}; `lockwire check` reports it until you re-lock"
        )


def _torque_claims(torque: list[str], source: str | None, out) -> dict[str, float]:
    """The `--torque 'JOINT=VALUE N*m'` claims, or one plain line and exit 2."""
    import re

    claims = {}
    for text in torque:
        m = re.fullmatch(r"\s*(.+?)\s*=\s*([0-9.]+(?:e[-+]?\d+)?)\s*(\S+)\s*", text, re.I)
        line = (
            "--torque needs --source <url> saying where the published torque comes from"
            if not source
            else f"--torque must look like 'knee=120 N*m', not {text!r}"
            if not m
            else f"--torque takes N*m only, not {m[3]!r}"
            if m[3] not in ("N*m", "N·m")
            else "--torque needs a value above 0"
            if float(m[2]) <= 0
            else None
        )
        if line:
            out.print(line, highlight=False, markup=False)
            raise typer.Exit(2)
        claims[m[1]] = float(m[2])
    return claims


def _add_torque_claims(findings: RecordReport, loaded, claims: dict[str, float], source) -> None:
    from lockwire.checks import _ScoreBook, check_torque_claims

    book = _ScoreBook()
    findings += check_torque_claims(loaded, claims, source, book)
    findings.scored = (findings.scored or 0) + len(book)
    findings.scored_keys = (findings.scored_keys or frozenset()) | book.keys
    findings.scored_records += book.records


def _check_verdicts(report: RecordReport) -> dict[str, str]:
    """One verdict per check (its contract path): passes are GO, findings override."""
    rank = {"GO": 0, "GO-WITH-NOTES": 1, "NO-GO": 2}
    got = {r.quantity: "GO" for r in report.scored_records}
    for f in report:
        word = {"violation": "NO-GO", "warning": "GO-WITH-NOTES"}.get(f.severity)
        if word and not f.advice_unsourced and rank[word] >= rank[got.get(f.quantity, "GO")]:
            got[f.quantity] = word
    return got


def _verdict_changes(base: RecordReport, head: RecordReport) -> list[tuple[str, str]]:
    """(line, severity) per check whose verdict changed. Rule: present on both sides with a
    different verdict, or new in head and not GO. A check that is absent from head (its input
    went missing) or new and GO is silent."""
    before, after = _check_verdicts(base), _check_verdicts(head)
    return [
        (f"{q}: {before.get(q, 'GO')} -> {after[q]}", after[q])
        for q in sorted(after)
        if after[q] != before.get(q, "GO")
    ]


@app.command(
    help="Start here: check a robot model (URDF, xacro, SDF, MuJoCo or Webots) and print a verdict.\n\n"
    f"{exit_code_help('check')}"
)
def check(
    ctx: typer.Context,
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT, help="Contract file to check, or a model: .urdf, .xacro, .sdf, MuJoCo .xml/.mjcf, Webots .wbt/.proto."
    ),
    json_out: Path | None = typer.Option(
        None,
        "--json",
        help="Also write this same run as JSON (the `lockwire.api.check` document); `-` prints it to stdout.",
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
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as failures (a GO with notes exits 1) and any unjudged check as exit 3.",
    ),
    max_not_scored: int | None = typer.Option(
        None,
        "--max-not-scored",
        help="Exit 3 when more than N checks went unjudged (default: unlimited; 0 with --strict).",
    ),
    mass: str | None = typer.Option(
        None, "--mass", help="Model file only: datasheet mass to compare, e.g. '20.6 kg'."
    ),
    source: str | None = typer.Option(None, "--source", help="Where --mass comes from (url/text)."),
    tolerance_pct: float = typer.Option(5.0, "--tolerance-pct", help="Percent --mass may differ."),
    witness: str | None = typer.Option(
        None, "--witness", help="Who declared --mass (default: you)."
    ),
    xacro_arg: list[str] = typer.Option(
        [], "--xacro-arg", help="Model file only: a xacro argument, name=value."
    ),
    lock: Path | None = typer.Option(None, "--lock", help="Model file only: keep the lock here."),
    torque: list[str] = typer.Option(
        [], "--torque", help="Model file only: published 'JOINT=VALUE N*m'; needs --source."
    ),
    payload: str | None = typer.Option(None, "--payload", help="Model file only: load, '2 kg'."),
    at: str | None = typer.Option(None, "--at", help="Link that carries --payload."),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="`github` also prints PR annotations and writes $GITHUB_STEP_SUMMARY; "
        "`csv` or `tsv` prints one row per finding to stdout (report goes to stderr).",
    ),
    robot_model: str | None = typer.Option(
        None, "--model", help="Compare with this catalog robot (`lockwire catalog robots`)."
    ),
    servo: str | None = typer.Option(
        None,
        "--servo",
        help="Catalog servo driving the joints, e.g. feetech.sts3215: effort above its stall fails.",
    ),
    badge_json: Path | None = typer.Option(
        None, "--badge-json", hidden=True, help="Write a shields.io badge of the datasheet check."
    ),
    adopt: str | None = typer.Option(
        None,
        "--adopt",
        help="Comma-separated finding ids/prefixes that annotate but never fail, e.g. 'datasheet/,model/links/'.",
    ),
    baseline: Path | None = typer.Option(
        None, "--baseline", help="Fail only on findings not in this committed baseline file."
    ),
    write_baseline: Path | None = typer.Option(
        None, "--write-baseline", help="Write every current finding to this baseline file."
    ),
    compare_to: Path | None = typer.Option(
        None,
        "--compare-to",
        help="Base lockfile: print one line per check whose verdict changed, e.g. 'x: GO -> NO-GO'.",
    ),
    plan_file: Path | None = typer.Option(
        None,
        "--plan",
        help="plan.yaml: also warn where a segment's pose overloads a joint (the same plan goes to --compare-to).",
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Also print warnings, unjudged checks' detail and passed checks."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Print only the verdict line; the exit code is unchanged."
    ),
) -> None:
    """Start here: check a robot model (URDF, xacro, SDF, MuJoCo or Webots) and print a verdict."""
    # `--json -` makes stdout exactly one JSON document; the human report and errors go to stderr.
    # `--format csv|tsv` owns stdout the same way: the human report goes to stderr.
    tabular = output_format in ("csv", "tsv")
    out = Console(stderr=True, emoji=False) if str(json_out) == "-" or tabular else console
    if output_format not in ("text", "github", "csv", "tsv"):
        out.print(
            f"[red]unknown --format {escape(output_format)}[/] — use text, github, csv or tsv"
        )
        raise typer.Exit(2)
    import tempfile

    from lockwire.start_operation import add_payload, model_lock

    model_file = (
        contract if contract.suffix.lower() in MODEL_SUFFIXES and contract.is_file() else None
    )
    shown = None  # the robot name as typed, when the model came from robot_descriptions
    if model_file is None and (model_file := _robot_by_name(contract, 2, out)):
        shown = contract.name
    given = [
        f
        for f, v in (
            ("--mass", mass),
            ("--source", source),
            ("--witness", witness),
            ("--xacro-arg", xacro_arg),
            ("--lock", lock),
            ("--torque", torque),
            ("--payload", payload),
            ("--at", at),
        )
        if v
    ]
    if (
        model_file is None
        and given
        and contract.is_file()
        and contract.suffix not in (".yaml", ".yml")
    ):
        out.print(
            f"{escape(str(contract))} is not a model lockwire reads (URDF, xacro, SDF or MJCF).",
            highlight=False,
            markup=False,
        )
        raise typer.Exit(2)
    if model_file is None and given:
        out.print(
            f"{', '.join(given)} works on a model file only: lockwire check <robot.urdf> "
            f"{given[0]} ...",
            highlight=False,
            markup=False,
        )
        raise typer.Exit(2)
    if contract.is_dir():
        out.print(f"'{escape(str(contract))}' is a folder; pass the lockfile or model inside it")
        raise typer.Exit(2)
    warned: list[str] = []
    claims = _torque_claims(torque, source, out)
    if bool(payload) != bool(at):
        out.print("--payload and --at go together: --payload '2 kg' --at <link>", markup=False)
        raise typer.Exit(2)
    if model_file is not None:
        folder = lock.parent if lock else Path(tempfile.mkdtemp(prefix="lockwire-"))
        if not lock:
            ctx.call_on_close(lambda: shutil.rmtree(folder, ignore_errors=True))
        elif lock.exists():
            out.print(
                f"{lock} already exists; run `lockwire check {lock}` to check against it, "
                "or pick a new name.",
                highlight=False,
                markup=False,
            )
            raise typer.Exit(2)
        try:
            contract = model_lock(
                model_file,
                folder,
                mass=mass,
                source=source,
                tolerance_pct=tolerance_pct,
                witness=witness,
                xacro_args=_xacro_args(xacro_arg, out),
                warnings=warned,
            )
            if payload:
                add_payload(contract, at, payload, out)
            if lock:
                contract = contract.rename(lock)
        except ModelDefect as error:  # a real defect in the model: NO-GO, not a tool failure
            from lockwire.start_operation import defect_report

            _write_document(json_out, api.defect_document(model_file, error), model_file)
            raise typer.Exit(render(defect_report(error), out, unlocked=True)) from error
        except (OSError, SyntaxError, TypeError, ValueError, YAMLError) as error:
            out.print(f"[red]check failed:[/] {escape(_why(error))}")
            raise typer.Exit(2) from error
    if not contract.exists():
        if contract.suffix.lower() in MODEL_SUFFIXES:
            out.print(f"[red]{_not_found(contract)}: {escape(str(contract))}[/]", highlight=False)
            raise typer.Exit(2)
        out.print(
            f"[red]no contract at {escape(str(contract))}[/] — run `lockwire start <urdf>` first, "
            "check a model directly: `lockwire check robot.urdf`, "
            "or try `lockwire start --example`"
        )
        raise typer.Exit(2)
    try:
        loaded = load_contract(contract, out)
        if plugins.FAILED and loaded.actuator_cases:
            gone = plugins.FAILED[0]
            out.print(
                f"[red]check refused:[/] this contract needs the recording checks, which did not load "
                f"({gone.name or gone}) - fix that install and run again"
            )
            raise typer.Exit(2)
        selected = _parse_actuator_evidence(actuator_evidence) if actuator_evidence else None
        selected, root, revision = api._actuator_check_context(
            loaded, contract, selected, evidence_root, expected_revision
        )
        links = [link.name for link in loaded.model.links] if loaded.model else []
        robot = recognise(loaded.robot.name, links, robot_model)
        joints = [j.name for j in loaded.model.joints] if loaded.model else []
        if missing := [n for n in claims if n not in joints]:
            out.print(
                f"no joint named {missing[0]}; the joints are: {', '.join(joints)}",
                highlight=False,
                markup=False,
            )
            raise typer.Exit(2)
        load_catalog()  # a bad ~/.lockwire/parts.yaml stops here as one line, not mid-check
        if servo:
            servo_stall_torque(servo)  # an unknown id stops here, before any verdict
        plan = load_plan(plan_file) if plan_file else None
    except (OSError, TypeError, ValueError, YAMLError) as error:
        out.print(f"[red]check failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    run_kw = {  # one argument set: the base run and every variant run take exactly these
        "contract_path": contract,
        "actuator_evidence": selected,
        "evidence_root": root,
        "contract_sha256": revision,
        "model": robot_model,
        "servo": servo,
        "plan": plan,
    }
    findings = run_record_checks(loaded, **run_kw)
    if claims:
        _add_torque_claims(findings, loaded, claims, source)
    if model_file is not None:
        from lockwire.start_operation import (
            drop_same_figure,
            typed_paths,
            warning_findings,
            zero_if_nothing_compared,
        )

        findings += warning_findings(warned)
        if not lock:
            typed_paths(findings, contract, model_file, shown)
        drop_same_figure(findings, mass)
        zero_if_nothing_compared(findings)
    else:
        from lockwire.start_operation import accept_hint

        accept_hint(findings, contract)
    # The document is built by the library, never assembled here: `lockwire.api`
    # and this file must not be able to disagree about one run.
    # `check_document`, not `verdict_document` directly, so this JSON carries the
    # same `headline` `api.check` does -- one document builder, not two.
    document = api.check_document(
        loaded,
        findings,
        revision=revision,
        actuator_evidence=selected,
    )

    def finish(code: int) -> int:  # the JSON carries the exit code the process returns (--strict)
        if document["exit_code"] != code:
            from lockwire.report import VERDICT_EXIT_CODES

            word = next(v for v, c in VERDICT_EXIT_CODES.items() if c == code)
            document.update(exit_code=code, verdict=verdict_word(word))
        _write_document(json_out, document, contract)
        return code

    if badge_json is not None:  # silent: stdout may be the `--json -` document
        from lockwire.report_json import datasheet_badge

        target = _confined(badge_json, contract)
        _require_folder(target)
        badge = datasheet_badge(robot, findings, findings.scored_keys or frozenset())
        target.write_text(json.dumps(badge) + "\n", encoding="utf-8")
    if servo and model_file is None and loaded.servo is None:
        out.print(
            f"to keep this servo, add this line to {escape(str(contract))}: servo: {servo}",
            highlight=False,
        )
    if compare_to is not None:
        try:
            base_loaded = load_contract(compare_to, out)
            base = run_record_checks(  # same inputs as the head run, or checks differ by input
                base_loaded,
                contract_path=compare_to,
                actuator_evidence=selected,
                evidence_root=root,
                model=robot_model,
                servo=servo,
                plan=plan,
            )
            if claims:
                _add_torque_claims(base, base_loaded, claims, source)
        except (OSError, TypeError, ValueError, YAMLError) as error:
            out.print(
                f"[red]check failed:[/] --compare-to {escape(str(compare_to))}: {escape(_why(error))}"
            )
            raise typer.Exit(2) from error
        for line, now in _verdict_changes(base, findings):
            out.print(line, highlight=False, markup=False)
            if output_format == "github":  # annotate like github_output does
                level = "error" if now == "NO-GO" else "warning" if now != "GO" else "notice"
                typer.echo(f"::{level}::{line}", err=str(json_out) == "-")
    if write_baseline is not None:
        _require_folder(write_baseline)
        rows = sorted({(f.quantity, f.observed) for f in findings if not is_lock_drift(f)})
        doc = {"findings": [{"id": q, "observed": o} for q, o in rows]}
        write_baseline.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        out.print(f"wrote {write_baseline} ({len(rows)} findings)", highlight=False, markup=False)
    try:
        known = (
            {
                (r["id"], r["observed"])
                for r in json.loads(baseline.read_text(encoding="utf-8"))["findings"]
            }
            if baseline
            else set()
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        out.print(f"[red]check failed:[/] baseline {escape(str(baseline))}: {escape(_why(error))}")
        raise typer.Exit(2) from error
    prefixes = tuple(p.strip() for p in (adopt or "").split(",") if p.strip())
    # Lock drift is always new and never adopted: only a re-lock clears it.
    new = [f for f in findings if is_lock_drift(f) or (f.quantity, f.observed) not in known]
    adopted = [f for f in new if f.quantity.startswith(prefixes) and not is_lock_drift(f)]

    def subset(rows: list) -> RecordReport:  # same run, fewer findings
        return RecordReport(
            rows,
            findings.information,
            scored=findings.scored,
            scored_keys=findings.scored_keys,
            scored_records=findings.scored_records,
            actuator_case_reports=findings.actuator_case_reports,
            accepted=findings.accepted,
        )

    counting = subset([f for f in new if f not in adopted])

    def table() -> None:
        if tabular:
            from lockwire.findings_export import table as findings_table

            typer.echo(
                findings_table(new, counting, "," if output_format == "csv" else "\t"), nl=False
            )

    if quiet:
        decision = verdict(counting)
        code = strict_exit(
            counting,
            decision.exit_code,
            Console(quiet=True),
            strict=strict,
            max_not_scored=max_not_scored,
        )
        table()
        out.print(decision.summary.strip(), highlight=False)
        raise typer.Exit(finish(_with_variants(loaded, run_kw, code, out, strict, max_not_scored)))
    if baseline and not new:  # nothing changed: one quiet line, no annotations
        decision = verdict(counting)
        table()
        out.print(
            f"{verdict_word(decision.code)} — no new findings since {escape(str(baseline))} "
            f"({len(findings)} unchanged)",
            highlight=False,
            markup=False,
        )
        code = strict_exit(
            counting, decision.exit_code, out, strict=strict, max_not_scored=max_not_scored
        )
        raise typer.Exit(finish(_with_variants(loaded, run_kw, code, out, strict, max_not_scored)))
    # CI logs (--format github) and --strict (warnings fail) keep every detail.
    more = None
    if not show_all and not strict and output_format == "text":
        # `--all` replaces the excluded `show_all` (false here), as `--out` does for report.
        more = build_cli_line(
            "lockwire",
            "check",
            *argv_from_context(ctx, exclude={"lock", "badge_json", "show_all"}),
            "--all",
        )
    code = render(subset(new), out, unlocked=model_file is not None and not lock, more=more)
    if adopted:
        code = verdict(counting).exit_code
        out.print(
            f"adopted: {len(adopted)} finding(s) under {', '.join(prefixes)} annotate, never fail; "
            f"without them: {verdict_word(verdict(counting).code)}",
            highlight=False,
            markup=False,
        )
    if len(new) < len(findings):
        out.print(f"{len(findings) - len(new)} finding(s) unchanged since {escape(str(baseline))}")
    from lockwire.page import _scored_title  # lazy: page is heavy and imports this layer

    passed = [_scored_title(loaded, r) for r in getattr(findings, "scored_records", [])]
    if passed:
        out.print(f"Passed: {len(passed)} check{'s' * (len(passed) != 1)}", highlight=False)
        for title in passed if not more else ():
            out.print(f"  {title}", highlight=False, markup=False)
    if model_file is not None and robot is None:
        out.print(
            "not a recognised robot; pass --model <id> to compare with a datasheet "
            "(`lockwire catalog robots` lists them)",
            highlight=False,
            markup=False,
        )
    if model_file is not None:
        if findings.scored == 0 and code != 1:
            out.print("nothing was compared; add --mass '<datasheet mass>' to compare")
        target = DEFAULT_CONTRACT if shown else model_file.parent / DEFAULT_CONTRACT
        beside = cli_line(
            "lockwire",
            "start",
            shown or model_file,
            *(["--out", target] if target != DEFAULT_CONTRACT else []),
        )
        out.print(
            f"Lock it so CI catches drift: {beside}", highlight=False, markup=False, soft_wrap=True
        )
    before_strict = code
    code = strict_exit(counting, code, out, strict=strict, max_not_scored=max_not_scored)
    if strict and code != before_strict:  # the printed word must match the exit code
        word = {1: "[bold red]NO-GO[/]", 3: "[bold yellow]NOT SCORED[/]"}.get(code)
        if word:
            out.print(f"\n{word} — under --strict")
    code = _with_variants(loaded, run_kw, code, out, strict, max_not_scored)
    table()
    if output_format == "github":
        from lockwire.report import github_output, github_summary

        demoted = [f.model_copy(update={"severity": "warning"}) for f in adopted]
        for line in github_output([*counting, *demoted], model_file or contract):
            typer.echo(line, err=str(json_out) == "-")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                summary.write(github_summary(counting, code))
    raise typer.Exit(finish(code))


def _with_variants(loaded, run_kw, code, out, strict, max_not_scored):
    """One "variant <name>: <verdict>" line each; the exit is the worst of base and variants.

    Worst: 2 (refused) > 1 (NO-GO) > 3 (not scored) > 0, the order --strict already uses
    (a failing code beats NOT SCORED). Each variant gets the same --strict/--max-not-scored rule.
    """
    if not loaded.variants:
        return code
    from lockwire.report import VERDICT_EXIT_CODES
    from lockwire.variants import check_variants

    rank = {2: 3, 1: 2, 3: 1, 0: 0}
    for name, vcode, vfound in check_variants(loaded, **run_kw):
        out.print(f"variant {name}: {verdict_word(vcode)}", highlight=False, markup=False)
        vexit = strict_exit(
            vfound,
            VERDICT_EXIT_CODES[vcode],
            Console(quiet=True),
            strict=strict,
            max_not_scored=max_not_scored,
        )
        code = max(code, vexit, key=lambda c: rank.get(c, 3))
    return code


@app.command(
    "check-sim",
    hidden=True,
    help="Compare a MuJoCo (MJCF) or SDF sim file with the lock, value by value: "
    f"masses, COM, inertia, joint limits. Never runs a simulation.\n\n{exit_code_help('check')}",
)
def check_sim_cmd(
    sim_file: Path = typer.Argument(..., help="MJCF (.xml/.mjcf with <mujoco>) or .sdf file."),
    contract: Path | None = typer.Option(
        None, "--lock", help="The lockfile; default: lockwire.yaml beside the sim file, else here."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write the run as JSON; `-` prints it to stdout."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Print every detail even when the sim file agrees."
    ),
) -> None:
    """Compare a sim file with the lock: does the sim file agree with the lock?"""
    from lockwire.check_sim import sim_document

    if contract is None:
        beside = sim_file.parent / DEFAULT_CONTRACT
        contract = beside if beside.is_file() else DEFAULT_CONTRACT
    try:
        loaded = load_contract(contract)
        findings, document = sim_document(sim_file, loaded, contract)
    except (OSError, ET.ParseError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]check-sim failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    _write_document(json_out, document, contract)
    out = Console(stderr=True, emoji=False) if str(json_out) == "-" else console
    clean = not any(f.severity in ("violation", "warning") for f in findings)
    if not verbose and clean and document["verdict"] == "GO":
        # Pre-launch gate: agreement is one line, so `check-sim x && simulate x` stays quiet.
        skipped = len(document["not_compared"])
        tail = f", {skipped} not compared; --verbose lists them" if skipped else ""
        out.print(
            f"lockwire: {sim_file.name} agrees with {contract.name} "
            f"({document['scored']} values compared{tail})",
            highlight=False,
            markup=False,
            soft_wrap=True,
        )
        raise typer.Exit(0)
    code = render(findings, out)
    # render() already lists what was not compared; this adds what the format cannot hold.
    if document["cannot_express"]:
        out.print("Sim cannot express (the lock holds it, the format has no field):")
    for row in document["cannot_express"]:
        out.print(f"  {row['lock_has']}", highlight=False, markup=False)
    raise typer.Exit(code)


@materials_app.command("compare")
def materials_compare(
    contract: Path = typer.Argument(DEFAULT_CONTRACT, help="The lockfile."),
    link: str = typer.Option(..., "--link", help="The link to try in every catalog material."),
) -> None:
    """Show a link's mass, the robot's total mass and the worst torque margin in each catalog material."""
    from lockwire.material_compare import compare_materials

    loaded = load_contract(contract)
    try:
        got = compare_materials(loaded, link)
    except ValueError as error:
        console.print(str(error), highlight=False, markup=False)
        raise typer.Exit(2)
    for r in got["rows"]:
        margin = (
            "no torque limit"
            if r["worst_margin_nm"] is None
            else (f"{r['worst_margin_nm']:.3f} N*m at {r['worst_joint']}")
        )
        line = (
            f"{r['material']}: link {r['link_mass_kg']:.3f} kg, robot {r['total_mass_kg']:.3f} kg, "
            f"worst margin {margin}" + ("  (current)" if r["current"] else "")
        )
        console.print(line, highlight=False, markup=False)
    if got["note"]:
        console.print(got["note"], highlight=False, markup=False)


@catalog_app.command("robots")
def catalog_robots() -> None:
    """List the robots `check` compares with a vendor datasheet; pass an id as --model."""
    for robot in load_robots():
        console.print(f"{robot.id}  {robot.name}", highlight=False, markup=False)


@catalog_app.command("parts")
def catalog_parts(
    words: list[str] = typer.Argument(
        None, help="Words the part id, name or family must all hold."
    ),
) -> None:
    """List catalog parts with their numbers; add words to narrow the list."""
    from lockwire.catalog import load_catalog

    parts = load_catalog().parts
    shown = [
        p
        for p in parts
        if all(w.lower() in f"{p.id} {p.name} {p.family}".lower() for w in words or [])
    ]
    if not shown:
        console.print(
            f"no part matches {' '.join(words)!r}; {cli_line('lockwire', 'catalog', 'parts')}"
            f" lists all {len(parts)}",
            highlight=False,
            markup=False,
        )
        return
    for p in shown:
        n = p.numbers
        lo, hi = n.get("input_voltage_min"), n.get("input_voltage_max")
        bits = [
            f"{label} {n[key].value:g} {n[key].unit}"
            for key, label in (("draw_max", "draw"), ("mass", "mass"), ("stall_torque", "stall"))
            if key in n
        ]
        if lo and hi:
            bits.append(
                f"{lo.value:g}-{hi.value:g} V" if lo.value != hi.value else f"{lo.value:g} V"
            )
        elif "nominal_voltage" in n:
            bits.append(f"{n['nominal_voltage'].value:g} V")
        console.print(
            f"{p.id}  {p.name}  {p.label}  {' · '.join(bits)}".rstrip(),
            highlight=False,
            markup=False,
            soft_wrap=True,
        )
    console.print(
        f"add one: {cli_line('lockwire', 'add', 'device')} <lockfile> --part <id>"
        f" (a servo goes on a joint: {cli_line('lockwire', 'swap')} <lockfile> <joint> --use <id>)",
        highlight=False,
        markup=False,
    )


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
    hidden=True,
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
        console.print(f"[red]plan check failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    findings = validate_plan(loaded_contract, loaded_plan)
    _write_document(
        json_out, plugins.pick("plan", api.plan)(loaded_contract, loaded_plan), contract
    )
    out = Console(stderr=True, emoji=False) if str(json_out) == "-" else console
    code = render(findings, out)
    timing = plugins.OVERRIDES.get("time_optimal_plan")
    best = timing(loaded_contract.model, loaded_plan) if timing else None
    if best is not None:  # None: nothing guessed
        out.print(
            f"Pro: shortest feasible plan time {best.total_s:.1f} s "
            f"(segment durations x{best.scale:.2f})"
        )
    raise typer.Exit(code)


@app.command(
    "bench",
    hidden=True,
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
    if not corpus.exists():
        hint = (
            "the benchmark corpus ships only in the source repo; pass your own with --corpus"
            if corpus == DEFAULT_CORPUS_PATH
            else "corpus file not found"
        )
        console.print(f"[red]{hint}[/]")
        raise typer.Exit(2)
    if not contract.exists():
        console.print("[red]contract file not found[/]")
        raise typer.Exit(2)
    try:
        loaded_contract = load_contract(contract)
        cases = load_corpus(corpus)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]bench failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    result, _sink = run_bench(loaded_contract, cases)
    console.print(format_report(result, corpus))
    raise typer.Exit(result.exit_code)


@app.command(hidden=True)
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
        False,
        "--open/--no-open",
        help="Accepted and ignored: report never opens a browser, so scripts can pass "
        "the flag everywhere.",
    ),
    redact_paths: bool = typer.Option(
        False,
        "--redact-paths",
        help="Shorten absolute paths to their basename and drop owner/witness names — "
        "for a page you're about to send someone.",
    ),
    plan_file: Path | None = typer.Option(
        None, "--plan", help="plan.yaml: adds the Pro requirements panel to the report."
    ),
) -> None:
    """Emit a deterministic, self-contained contract report."""
    del open_browser  # accepted for CLI parity with gui/start; no-op.
    try:
        loaded = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]report failed:[/] {escape(_why(error))}")
        raise typer.Exit(1) from error
    try:
        plan = load_plan(plan_file) if plan_file else None
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]report failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    # Announces/refuses the scene for CLI parity; the printable report
    # does not yet draw it — that is the 3D renderer's ticket, not this one's.
    _load_scene_for_cli(contract, scene, command="report", package_roots=tuple(package_root))
    # --out is excluded and rendered as a placeholder, same convention as gui's
    # own line: the page's bytes must not depend on the chosen output filename.
    cli_line = build_cli_line(
        "lockwire", "report", *argv_from_context(ctx, exclude={"out"}), "--out", "<this file>"
    )
    target = _confined(out, contract)
    try:
        write_html_report(
            loaded,
            target,
            contract_path=contract,
            redact_paths=redact_paths,
            cli_line=cli_line,
            plan=plan,
        )
    except OSError as error:  # tool failure, one line
        console.print(f"[red]report failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    console.print(f"[green]wrote {escape(str(out))}[/]")


@app.command(hidden=True)
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
        console.print(f"[red]describe failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error

    if out is None:
        for device in loaded.devices:
            console.print(describe_device(loaded, device))
        # The system page carries the force numbers. Printing only the
        # per-device pages would leave stdout without the figures --out writes.
        console.print(describe_system(loaded))
        rows = bus_address_map(loaded)
        if rows:  # no addresses declared: no section, no empty table
            table = Table("bus", "kind", "address", "device", title="Bus address map")
            for row in rows:
                table.add_row(*(escape(cell) for cell in row))
            console.print(table)
        return

    files = describe_all(loaded)
    # Every name is confined before the first write: a device id is not a path.
    targets = {_confined(out / name, contract, root=out): text for name, text in files.items()}
    try:
        out.mkdir(parents=True, exist_ok=True)
        for target, content in targets.items():
            target.write_text(content, encoding="utf-8")
    except OSError as error:
        console.print(f"[red]describe failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error

    for name in sorted(files):
        console.print(f"[green]wrote {out / name}[/]")


@app.command("export", hidden=True)
def export_cmd(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Contract file to export.",
    ),
    fmt: str = typer.Option(
        ...,
        "--format",
        help="Output shape: 'urdf', 'sdf', 'mjcf' (model file), 'stl', 'obj', 'dae' (3D meshes, --out is a folder), 'mhs', 'bom' (parts list, CSV) or 'budget' (power budget, CSV).",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write here: the bom CSV, or the model file (default beside the lockfile).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing --out file."),
    plan_file: Path | None = typer.Option(
        None, "--plan", help="budget: plan.yaml, so typical draw is weighted by the plan's duty."
    ),
    units: str = typer.Option("m", "--units", help="stl/obj/dae: 'm' (metres) or 'mm'."),
    segments: int = typer.Option(
        export_model.SEGMENTS, "--segments", help="stl/obj/dae: sides of a cylinder or sphere."
    ),
) -> None:
    """Render the contract into a third-party device-manifest shape, or a parts list. Outward only."""
    if fmt == "step":
        console.print(f"[red]export failed:[/] {escape(export_model.STEP_REFUSAL)}")
        raise typer.Exit(2)
    meshes = ("stl", "obj", "dae")
    if fmt not in ("mhs", "bom", "budget", *meshes, *export_model.SUFFIX):
        console.print(
            f"[red]export failed:[/] unknown --format {escape(repr(fmt))}; use urdf, sdf, mjcf, stl, obj, dae, mhs, bom or budget."
        )
        raise typer.Exit(2)
    if fmt == "budget":
        out = out or contract.with_name(f"{contract.stem}.budget.csv")
        if str(out) != "-" and out.exists() and not force:
            console.print(f"[red]export failed:[/] {escape(str(out))} exists, pass --force")
            raise typer.Exit(2)
        try:
            text = power_budget.budget_csv(
                load_contract(contract), load_plan(plan_file) if plan_file else None
            )
            if str(out) == "-":
                sys.stdout.write(text)
            else:
                out.write_text(text, encoding="utf-8")
                console.print(f"[green]wrote {escape(str(out))}[/]")
        except (OSError, TypeError, ValueError, YAMLError) as error:
            console.print(f"[red]export failed:[/] {escape(_why(error))}")
            raise typer.Exit(2) from error
        return
    if out is not None and (fmt == "mhs" or (fmt == "bom" and out.exists() and not force)):
        why = (
            "--out is for --format bom"
            if fmt == "mhs"
            else f"{escape(str(out))} exists, pass --force"
        )
        console.print(f"[red]export failed:[/] {why}")
        raise typer.Exit(2)
    try:
        loaded = load_contract(contract)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]export failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    if fmt in meshes:
        if loaded.model is None:
            console.print("[red]export failed:[/] the lockfile holds no model to export.")
            raise typer.Exit(2)
        try:
            files, notes = export_model.write_mesh(
                loaded.model,
                out or export_model.default_stl_dir(contract, fmt),
                fmt,
                units,
                segments,
                force,
                source_root=contract.parent,
            )
        except (OSError, ValueError) as error:
            console.print(f"[red]export failed:[/] {escape(_why(error))}")
            raise typer.Exit(2) from error
        named = "millimetres" if units == "mm" else "metres"
        console.print(
            f"[green]wrote {len(files)} {fmt.upper()} files to {escape(str(files[0].parent))}, units: {named}[/]"
        )
        for note in notes:
            console.print(escape(note))
        return
    if fmt in export_model.SUFFIX:
        if loaded.model is None:
            console.print("[red]export failed:[/] the lockfile holds no model to export.")
            raise typer.Exit(2)
        try:
            target = export_model.write(
                loaded.model,
                fmt,
                out or export_model.default_out(contract, fmt),
                force=force,
                source_root=contract.parent,
            )
        except (OSError, ValueError) as error:
            console.print(f"[red]export failed:[/] {escape(_why(error))}")
            raise typer.Exit(2) from error
        console.print(f"[green]wrote {escape(str(target))}[/]")
        return
    if fmt == "mhs":
        console.print_json(data=export_mhs.export_mhs(loaded))
        return
    rows = api.bom_rows(loaded)
    sink = io.StringIO()
    writer = csv.DictWriter(sink, api.BOM_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows({**r, "notes": "; ".join(r["notes"])} for r in rows)
    try:
        if out is None:
            sys.stdout.write(sink.getvalue())
        else:
            out.write_text(sink.getvalue(), encoding="utf-8")
    except OSError as error:
        console.print(f"[red]export failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    note = api.bom_mass_note(loaded, rows)
    if note:
        typer.echo(note, err=True)
    for r in rows:
        if r["notes"]:
            typer.echo(f"{r['part']}: {'; '.join(r['notes'])}", err=True)


@app.command(
    "import",
    hidden=True,
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
        "into a contract — is refused until a standard manifest format exists; import is "
        "where lock-in happens."
    )
    raise typer.Exit(2)


@app.command(hidden=True)
def scan(
    robot: str = typer.Option(
        "scanned-robot", "--robot", help="Robot name for the draft candidate."
    ),
) -> None:
    """Enumerate this Linux/ROS 2 machine into a draft, never an authoritative lock."""
    try:
        draft = scan_machine(robot)
    except UnsupportedPlatformError as error:
        console.print(f"[red]scan failed:[/] {escape(_why(error))}")
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
        console.print(f"[red]scene import failed:[/] no contract at {escape(str(contract))}")
        raise typer.Exit(2)
    if (
        out.resolve() == contract.resolve()
    ):  # a changes file is yaml by design, so only the contract itself is refused
        console.print(
            f"[red]scene import failed:[/] refusing to write over the contract: {escape(str(out))}"
        )
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
        console.print(f"[red]scene import failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    link_based = scene.suffix.lower() in (".urdf", ".sdf", ".xacro")
    try:
        loaded_scene, _ = api.load_scene(contract, scene, tuple(package_root))
        assert loaded_scene is not None
    except SceneRefusal as error:
        console.print(f"[red]{escape(str(error))}[/]")
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
        console.print(f"[red]scene import failed:[/] {escape(_why(error))}")
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
        console.print(f"[red]scene import failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    console.print(f"wrote {escape(str(out))} ({len(result.entries)} placements, witness {witness})")

    console.print(
        "ran: " + build_cli_line("lockwire", "scene", "import", *argv_from_context(ctx)),
        soft_wrap=True,
    )

    if write:
        console.print(f"applied {escape(str(out))} to {escape(str(contract))}")
    else:
        console.print(f"next: lockwire apply {escape(str(contract))} {escape(str(out))}")


@app.command(
    "hw",
    hidden=True,
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
        console.print(f"[red]hw failed:[/] no contract at {escape(str(contract))}")
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
        console.print(f"[red]hw failed:[/] {escape(_why(error))}")
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
    hidden=True,
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
        console.print(f"[red]declare failed:[/] no contract at {escape(str(contract))}")
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


@app.command("accept")
def accept_cmd(
    finding: str = typer.Argument(..., help="Finding id, e.g. datasheet/<robot>/mass_total."),
    contract: Path = typer.Argument(DEFAULT_CONTRACT, help="Lockfile to record it in."),
    reason: str = typer.Option(..., "--reason", help="Why this gap is known and fine."),
    witness: str | None = typer.Option(None, "--witness", help="Who checked it."),
) -> None:
    """Accept a known gap on record: this finding, at its current value, stops failing.

    It still shows as accepted (reason, witness); a changed value fires again.
    """
    from lockwire.edit import source_is_absolute

    try:
        findings = run_record_checks(load_contract(contract), contract_path=contract)
        found = next((f for f in findings if f.quantity == finding), None)
        if found is None:
            raise EditRefusal(
                f"no current finding {finding!r}; `lockwire check {contract}` lists them"
            )
        if is_lock_drift(found):
            raise EditRefusal(
                "drift between the model and its lock can't be accepted; re-lock with "
                "`lockwire start <model> --force` if the change is intended"
            )
        source = (
            found.source
            if found.source and not source_is_absolute(found.source)
            else "lockwire check"
        )
        row = {"id": finding, "observed": found.observed, "reason": reason}
        change = Change(
            path="accepted", value=[row], source=source, witness=resolve_witness(witness)
        )
        result = apply_changes(contract, [change])
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError, ValueError) as error:
        _refused("accept", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)


@app.command("undo", hidden=True)
def undo_cmd(contract: Path = typer.Argument(Path("lockwire.yaml"), help="Lockfile.")) -> None:
    """Take back the last change Lockwire made to the lockfile."""
    _undo_redo("undo", contract)


@app.command("redo", hidden=True)
def redo_cmd(contract: Path = typer.Argument(Path("lockwire.yaml"), help="Lockfile.")) -> None:
    """Put back the change `undo` took back."""
    _undo_redo("redo", contract)


def _undo_redo(which: str, contract: Path) -> None:
    from lockwire import edit

    try:
        (edit.undo_edit if which == "undo" else edit.redo_edit)(contract)
    except (EditRefusal, OSError) as error:
        _refused(which, error)
        raise typer.Exit(2) from error
    console.print(f"{which} done: {escape(contract.name)}")


@app.command("swap", hidden=True)
def swap_cmd(
    contract: Path = typer.Argument(..., help="Lockfile to read."),
    joint: str = typer.Argument(..., help="The revolute/continuous joint whose servo to swap."),
    use: str | None = typer.Option(
        None, "--use", help="Write this catalog servo for the joint (without it: preview only)."
    ),
) -> None:
    """Rank catalog servos for one joint by its real gravity load, and show the joints above it."""
    from lockwire.swap import apply_swap, preview, shown_ratio

    if not contract.is_file():
        console.print(f"no contract at {escape(str(contract))} — run `lockwire start <urdf>` first")
        raise typer.Exit(2)
    try:
        if use is None:
            pv = preview(load_contract(contract), joint)
        else:
            pv, result = apply_swap(
                contract, joint, use, expected_revision=contract_revision(contract)
            )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError, ValueError) as error:
        _refused("swap", error)
        raise typer.Exit(2) from error
    console.print(
        f"{escape(str(joint))}: {escape(pv.current or 'no servo declared')}, gravity load {pv.load:.2f} N*m (fit = stall >= 2x load)"
    )
    if not pv.candidates:
        console.print("no cited alternatives yet")
        return
    colour = {"fit": "green", "tight": "yellow", "too weak": "red"}

    def times(r: float) -> str:
        return f"{shown_ratio(r):.1f}x" if r < 100 else ">100x"

    for x in pv.candidates:
        flags = f"  [yellow]{escape(', '.join(x.flags))}[/]" if x.flags else ""
        console.print(
            f"  [{colour[x.status]}]{x.status:<8}[/] {escape(f'{x.id:<22}')} {escape(f'{x.label:<10}')} "
            f"{x.stall:.2f} N*m  {times(x.ratio)}  {x.mass_g:g} g{flags}"
        )
    c = pv.chosen
    console.print(
        f"With {escape(c.id)} ({pv.delta_g:+g} g on {escape(str(joint))}'s child link), the joints above:"
    )
    for k in pv.knock_on:
        if k.before is None or k.after is None:
            console.print(f"  {escape(k.joint)} load not scored")
            continue
        line = f"  {escape(k.joint)} {k.before:.2f} → {k.after:.2f} N*m"
        if k.status is not None:
            line += (
                f", [bold red]warning: now {k.status}[/] ({times(k.ratio)}), was {k.was}"
                if k.dropped
                else f", still {k.status} ({times(k.ratio)})"
            )
        console.print(line)
    if use is None:  # with --use the notes print once, among the edit's lines
        for note in pv.notes:
            console.print(f"[yellow]note:[/] {escape(note)}")
        console.print(
            f"preview only; write it with: lockwire swap {escape(str(contract))} {escape(str(joint))} --use {escape(c.id)}"
        )
    else:
        _print_edit_result(result)
        if c.flags:  # the table row above scrolls away; say it again, last
            console.print(f"[yellow]warning:[/] {escape(c.id)}: {escape(', '.join(c.flags))}")


@app.command(
    "what-if",
    hidden=True,
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
    hidden=True,
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
    expected_revision: str | None = typer.Option(
        None,
        "--expected-revision",
        help="Full SHA-256 of the contract bytes you read; refuses if the file changed since. "
        "Omitted: no stale-file check.",
    ),
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
        console.print(f"[red]review failed:[/] no contract at {escape(str(contract))}")
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
            expected_revision=expected_revision,
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
        console.print(f"[red]refused:[/] {flag} {escape(repr(raw))} is not three numbers")
        raise typer.Exit(2) from None


def _run_pose_edit(ctx: typer.Context, name: str, contract: Path, call) -> None:
    """Shared existence, refusal and replay handling for pose and mount edits."""
    if not contract.exists() or not contract.is_file():
        console.print(f"[red]{name} failed:[/] no contract at {escape(str(contract))}")
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
    hidden=True,
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
    hidden=True,
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
    hidden=True,
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
    hidden=True,
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
    hidden=True,
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


@app.command("apply", hidden=True)
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
        console.print(f"[red]apply failed:[/] no contract at {escape(str(contract))}")
        raise typer.Exit(2)
    if not changes_file.exists() or not changes_file.is_file():
        console.print(f"[red]apply failed:[/] no changes file at {escape(str(changes_file))}")
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


@app.command("from-parts", hidden=True)
def from_parts_cmd(
    parts: Path = typer.Argument(..., help="Text file, one part per line."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Also write a draft lockfile here."),
    catalog: Path | None = typer.Option(None, "--catalog", help="Catalog file (default: bundled)."),
) -> None:
    """Match a pasted parts list to the catalog; list matches, misses and number mismatches.

    The mismatch list is information, not a gate: a successful run exits 0. With --out, the
    matched parts (quantities repeated) become a draft lockfile through the `add` door; an
    existing --out is kept as is.
    """
    if not parts.is_file():
        console.print(f"[red]from-parts failed:[/] no parts list at {escape(str(parts))}")
        raise typer.Exit(2)
    if out is not None:
        _require_folder(out)
    document = api.from_parts(parts.read_text(encoding="utf-8"), out=out, catalog=catalog)
    table = Table("line", "qty", "part", "result", "catalog id", "confidence", "label")
    table.columns[4].overflow = "fold"
    for row in document["lines"]:
        hit = row["status"] == "MATCHED"
        table.add_row(
            str(row["line"]),
            str(row["quantity"]),
            row["text"],
            "[green]MATCHED[/]"
            if hit
            else "[yellow]NO MATCH[/]" + (f" {escape(row['note'])}" if row.get("note") else ""),
            row["id"] or "",
            f"{row['confidence']:.2f}" if hit else "",
            row["label"] or "",
        )
    console.print(table)
    for m in document["mismatches"]:
        console.print(
            f"[red]MISMATCH[/] line {m['line']} ({m['id']}): you wrote {m['written']}, "
            f"catalog says {m['catalog']} ({m['field']})"
        )
    if out is not None:
        wrote = "written" in document
        console.print(
            f"draft lockfile {'written to' if wrote else 'kept as is:'} {escape(str(out))}"
        )


def _number_in(raw: str, unit: str, flag: str) -> float:
    """'0.5 m' / '50 cm' / '0.5' -> a plain number in `unit` (the unit the field name ends in)."""
    parts = raw.split()
    try:
        if len(parts) in (1, 2):
            number = float(parts[0])
            if len(parts) == 2:
                number = units.convert(number, parts[1], unit)
            if number - number == 0:  # finite
                return number
    except (ValueError, OverflowError):
        pass
    raise EditRefusal(f'{flag} must be a number like "1.5 {unit}"; got "{raw.strip()}".')


def _three_numbers(raw: str, flag: str) -> list[float]:
    try:
        nums = [float(x) for x in raw.split()]
        if len(nums) == 3 and all(n - n == 0 for n in nums):
            return nums
    except ValueError:
        pass
    raise EditRefusal(f'{flag} must be three numbers like "0 0 0.1"; got "{raw}".')


def _shape_from(text: str) -> dict[str, Any]:
    """'box 0.04 0.04 0.2 at 0 0 0.1' -> a link shape in metres (the `at` part is optional)."""
    body, _, at = text.partition(" at ")
    kind, *dims = body.split()
    names = {"box": ("size",), "cylinder": ("radius", "length"), "sphere": ("radius",)}
    want = {"box": 3, "cylinder": 2, "sphere": 1}
    try:
        if kind not in want or len(dims) != want[kind]:
            raise ValueError
        nums = [float(d) for d in dims]
    except ValueError:
        raise EditRefusal(
            f'--shape must be "box W D H", "cylinder RADIUS LENGTH" or "sphere RADIUS" in metres, '
            f'optionally followed by " at X Y Z"; got "{text}".'
        ) from None
    shape: dict[str, Any] = {"kind": kind}
    if kind == "box":
        shape["size"] = nums
    else:
        shape.update(zip(names[kind], nums))
    if at:
        shape["origin"] = {"xyz": _three_numbers(at, "--shape")}
    return shape


def _wire_end(raw: str, flag: str) -> dict[str, Any]:
    parts = raw.split(":")
    if len(parts) not in (2, 3) or not all(p.strip() for p in parts):
        raise EditRefusal(f'--{flag} must be device:pin or device:connector:pin; got "{raw}".')
    pin = parts[-1].strip()
    end: dict[str, Any] = {"device": parts[0].strip(), "pin": int(pin) if pin.isdigit() else pin}
    if len(parts) == 3:
        end["connector"] = parts[1].strip()
    return end


@app.command("add", hidden=True)
def add_cmd(
    ctx: typer.Context,
    kind: str = typer.Argument(
        ...,
        help="device | rail | bus | family | model | budget | battery | wire | connector | link | joint | promote | mirror | group",
    ),
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    id: str | None = typer.Option(None, "--id", help="New entry's id (not used for battery)."),
    source: str | None = typer.Option(
        None, "--source", help="Where every field comes from (required without --part)."
    ),
    witness: str | None = typer.Option(
        None, "--witness", help="Who/what declared it (required without --part)."
    ),
    note: str | None = typer.Option(None, "--note"),
    part: str | None = typer.Option(
        None, "--part", help="device: catalog part id; fills model, mass, draw, source, rail."
    ),
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
    nominal: str | None = typer.Option(
        None,
        "--nominal",
        help="budget (mass_total): datasheet mass the sum must match, e.g. '1.25 kg'.",
    ),
    tolerance_pct: float | None = typer.Option(
        None, "--tolerance-pct", help="budget: percent the sum may differ from --nominal."
    ),
    nominal_voltage: str | None = typer.Option(
        None, "--nominal-voltage", help="battery: voltage, e.g. '25.9 V'."
    ),
    chemistry: str | None = typer.Option(
        None,
        "--chemistry",
        help="battery: chemistry, e.g. 'Li-ion' (no schema field — folded into a comment).",
    ),
    dropout: str | None = typer.Option(
        None, "--dropout", help="rail: regulator dropout, e.g. '0.3 V' (stored as dropout_v)."
    ),
    min_input: str | None = typer.Option(
        None, "--min-input", help="rail: lowest input it holds at, e.g. '5 V' (min_input_v)."
    ),
    regulator_kind: str | None = typer.Option(
        None, "--regulator-kind", help="rail: ldo|buck|boost."
    ),
    length: str | None = typer.Option(
        None, "--length", help="bus/wire: cable run, e.g. '1.5 m' (stored as length_m)."
    ),
    twisted: bool | None = typer.Option(
        None, "--twisted/--no-twisted", help="bus: twisted-pair cabling."
    ),
    cells_parallel: str | None = typer.Option(
        None, "--cells-parallel", help="battery: cells in parallel, a whole number."
    ),
    reserve: str | None = typer.Option(
        None, "--reserve", help="battery: capacity held back, e.g. '10 %' (reserve_pct)."
    ),
    awg: str | None = typer.Option(None, "--awg", help="wire: gauge, a whole number (required)."),
    installation: str | None = typer.Option(
        None, "--installation", help="wire: free_air|enclosed (required)."
    ),
    net_class: str | None = typer.Option(
        None, "--net-class", help="wire: power|motor|logic (net_class)."
    ),
    connector: str | None = typer.Option(None, "--connector", help="wire: xt60|jst-xh."),
    from_end: str | None = typer.Option(
        None, "--from", help="wire: one end, device:pin or device:connector:pin (needs --to)."
    ),
    to_end: str | None = typer.Option(None, "--to", help="wire: other end, same shape as --from."),
    device: str | None = typer.Option(None, "--device", help="connector: device id it sits on."),
    connector_type: str | None = typer.Option(
        None, "--type", help="connector: connector type id (pins are named from its labels)."
    ),
    placement: str | None = typer.Option(
        None, "--placement", help="connector: where on the device, e.g. 'left edge'."
    ),
    parent: str | None = typer.Option(None, "--parent", help="joint: the link it hangs from."),
    child: str | None = typer.Option(None, "--child", help="joint: the link it carries."),
    axis: str | None = typer.Option(None, "--axis", help='joint: axis, e.g. "0 0 1".'),
    lower: float | None = typer.Option(None, "--lower", help="joint: lower limit (rad or m)."),
    upper: float | None = typer.Option(None, "--upper", help="joint: upper limit (rad or m)."),
    effort: float | None = typer.Option(None, "--effort", help="joint: effort limit (N or N*m)."),
    velocity: float | None = typer.Option(None, "--velocity", help="joint: velocity limit."),
    shape: list[str] = typer.Option(
        [],
        "--shape",
        help='link: a shape in metres, repeatable: "box 0.04 0.04 0.2 at 0 0 0.1", '
        '"cylinder 0.02 0.3" or "sphere 0.05" (mass and CoM come from shape + --material).',
    ),
    mesh: Path | None = typer.Option(
        None,
        "--mesh",
        help="link: an STL or DAE mesh inside the lockfile's folder (needs --scale).",
    ),
    scale: str | None = typer.Option(
        None,
        "--scale",
        help="link: the mesh's unit, mm, m or a number such as 0.001 (never guessed).",
    ),
    material: str | None = typer.Option(
        None,
        "--material",
        help='link: a catalog material name, e.g. "Aluminium 6061-T6" (a wrong name lists the closest).',
    ),
    origin: str | None = typer.Option(
        None, "--origin", help='joint: where it sits on its parent link in metres, e.g. "0 0 0.1".'
    ),
    of: str | None = typer.Option(
        None, "--of", help="mirror: the link to mirror (with its subtree)."
    ),
    plane: str | None = typer.Option(None, "--plane", help="mirror: xz, yz or xy."),
    rename: str | None = typer.Option(
        None, "--rename", help='mirror: swap text in the copies\' names, "arm=arm-r".'
    ),
    subtree: bool | None = typer.Option(
        None, "--subtree/--no-subtree", help="mirror: copy the descendants too (default yes)."
    ),
    members: str | None = typer.Option(None, "--members", help="group: member names, a,b,c."),
    value: float | None = typer.Option(
        None, "--value", help="param: its value (metres if no --unit)."
    ),
    unit: str | None = typer.Option(None, "--unit", help="param: e.g. 'mm'; leave out for metres."),
    param_min: float | None = typer.Option(None, "--min", help="param: lowest allowed value."),
    param_max: float | None = typer.Option(None, "--max", help="param: highest allowed value."),
    link: str | None = typer.Option(
        None, "--link", help="shape: the existing link (takes one --shape)."
    ),
    frame: str | None = typer.Option(
        None, "--frame", help='attach: where it sits on --parent, metres: "x y z".'
    ),
    frame_rpy: str | None = typer.Option(
        None, "--frame-rpy", help='attach: its orientation, radians: "r p y" (default 0 0 0).'
    ),
    frame_id: str | None = typer.Option(
        None, "--frame-id", help="attach: or a named frame on the catalog --part."
    ),
    joint_name: str | None = typer.Option(
        None, "--joint-name", help="attach: the new joint's name."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Add a new device/rail/bus/family/model/budget/battery/wire/connector/link/joint block, with provenance.

    `add param` takes --name --value (--unit --min --max); `add shape` takes --link and one
    --shape; `add attach` takes --parent --part (--name), --frame "x y z" (--frame-rpy) or
    --frame-id, and the joint as --type --axis --lower --upper --effort --velocity --joint-name.

    `add link` and `add joint` take --name (or --id); a joint also needs --type, --parent and
    --child, and refuses a loop, a second parent or a link joined to itself.

    Note: `DeviceModel` carries no schema field for a human name —
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
        console.print(f"[red]add failed:[/] no contract at {escape(str(contract))}")
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
        "nominal": nominal,
        "tolerance_pct": tolerance_pct,
        "rail": rail,
        "nominal_voltage": nominal_voltage,
        "chemistry": chemistry,
        "regulator_kind": regulator_kind,
        "twisted": twisted,
        "awg": awg,
        "installation": installation,
        "net_class": net_class,
        "connector": connector,
        "device": device,
        "type": connector_type,
        "placement": placement,
        "mass": mass,
        "parent": parent,
        "child": child,
    }
    if kind in ("link", "joint"):
        fields["name"] = name or id
    fields["material"] = material
    rail_note = None
    try:
        if (mesh is None) != (scale is None):
            raise EditRefusal(
                "--mesh needs --scale mm, m or a number such as 0.001; Lockwire does not guess the unit."
                if scale is None
                else "--scale goes with --mesh."
            )
        if mesh is not None:
            if kind != "link":
                raise EditRefusal("--mesh is for `add link`.")
            fields["mesh"], fields["scale"] = str(mesh), scale
        if shape:
            fields["geometry"] = [_shape_from(text) for text in shape]
        if origin is not None:
            fields["origin"] = {"xyz": _three_numbers(origin, "--origin")}
        if axis is not None:
            parts = axis.split()
            try:
                if len(parts) != 3:
                    raise ValueError
                fields["axis"] = [float(x) for x in parts]
            except ValueError:
                raise EditRefusal(
                    f'--axis must be a list of three numbers like "0 0 1"; got "{axis}".'
                ) from None
        limits = {
            k: v
            for k, v in (
                ("lower", lower),
                ("upper", upper),
                ("effort", effort),
                ("velocity", velocity),
            )
            if v is not None
        }
        if limits:
            fields["limits"] = limits
        if kind == "promote":  # --device, --name, then the joint from --type/--axis/limits
            fields["joint"] = {
                "type": connector_type,
                "axis": fields.get("axis"),
                "limits": fields.get("limits"),
            }
        if kind == "mirror":
            old, eq, new = (rename or "").partition("=")
            if rename is not None and not (old and eq):
                raise EditRefusal(f'--rename must look like "arm=arm-r"; got "{rename}".')
            fields |= {"of": of, "plane": plane}
            fields |= {"rename": {"from": old, "to": new}} if rename else {}
            fields |= {} if subtree is None else {"subtree": subtree}
        if kind == "param":
            fields |= {"value": value, "unit": unit, "min": param_min, "max": param_max}
        if kind == "shape":
            if len(shape) != 1:
                raise EditRefusal("`add shape` takes --link and exactly one --shape.")
            fields |= {"link": link, "shape": fields.pop("geometry")[0]}
        if kind == "attach":
            fields |= {
                "frame_id": frame_id,
                "joint": {
                    "type": connector_type,
                    "axis": fields.get("axis"),
                    "limits": fields.get("limits"),
                    "name": joint_name,
                },
            }
            if frame is not None:
                fields["frame"] = {"xyz": _three_numbers(frame, "--frame")}
                if frame_rpy is not None:
                    fields["frame"]["rpy"] = _three_numbers(frame_rpy, "--frame-rpy")
        if members is not None:  # group
            fields["members"] = [m.strip() for m in members.split(",") if m.strip()]
        for end, raw in (("from", from_end), ("to", to_end)):
            if raw is not None:
                fields[end] = _wire_end(raw, end)
        for key, raw, unit, flag in (
            ("dropout_v", dropout, "V", "--dropout"),
            ("min_input_v", min_input, "V", "--min-input"),
            ("length_m", length, "m", "--length"),
            ("reserve_pct", reserve, "%", "--reserve"),
        ):
            if raw is not None:
                fields[key] = _number_in(raw, unit, flag)
        if cells_parallel is not None:
            if not cells_parallel.strip().isdigit():
                raise EditRefusal(
                    f'--cells-parallel must be a whole number; got "{cells_parallel}".'
                )
            fields["cells_parallel"] = int(cells_parallel)
        catalog_add = bool(part) and kind != "attach"
        if part and kind == "attach":
            fields["part"] = part
        if catalog_add:
            if kind != "device" or draw or mass:
                raise EditRefusal(
                    "--part is for `add device` and takes the draw and mass from the catalog; "
                    "drop --draw and --mass, or drop --part"
                )
            from lockwire.edit import resolve_witness
            from lockwire.parts_list import part_to_add

            catalog_fields, catalog_source, rail_note = part_to_add(contract, part, rail)
            fields = {**catalog_fields, **{k: v for k, v in fields.items() if v is not None}}
            source = source or catalog_source
            witness = resolve_witness(witness) or "w0"
        elif source is None or witness is None:
            raise EditRefusal("--source and --witness are required (or use --part <catalog id>)")
        result = add_entry(
            contract, kind, fields, source=source, witness=witness, note=note, dry_run=dry_run
        )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("add", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    if rail_note:
        console.print(rail_note, highlight=False, markup=False)
    if catalog_add and not dry_run:
        console.print(f"Next: {cli_line('lockwire', 'check', contract)}", highlight=False)
    console.print(
        "ran: " + build_cli_line("lockwire", "add", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command("delete", hidden=True)
def delete_cmd(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="link | joint | device"),
    contract: Path = typer.Argument(..., help="Contract file to edit."),
    id: str = typer.Option(..., "--id", help="The link or joint name, or the device id."),
    source: str = typer.Option(..., "--source", help="Why it goes."),
    witness: str = typer.Option(..., "--witness", help="Who decided."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Delete a link, joint or device. Refuses a link a joint still holds, a device still named
    elsewhere, and an imported model; nothing is written on refusal. Exit 0, 2 on refusal."""
    try:
        if not contract.is_file():
            raise EditRefusal(f"no contract at {contract}")
        result = delete_entry(contract, kind, id, source=source, witness=witness, dry_run=dry_run)
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("delete", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "delete", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command("rename", hidden=True)
def rename_cmd(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="link | joint | device | rail | wire | param"),
    old: str = typer.Argument(..., help="The current name."),
    new: str = typer.Argument(..., help="The new name."),
    contract: Path = typer.Argument(Path("lockwire.yaml"), help="Lockfile."),
    source: str = typer.Option(..., "--source", help="Why it is renamed."),
    witness: str = typer.Option(..., "--witness", help="Who decided."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the diff, write nothing."),
) -> None:
    """Rename a link, joint, device, rail, wire or param and every reference to it, in one write.
    Nothing is written on refusal. Exit 0, 2 on refusal."""
    try:
        if not contract.is_file():
            raise EditRefusal(f"no contract at {contract}")
        result = rename_entry(
            contract, kind, old, new, source=source, witness=witness, dry_run=dry_run
        )
    except (EditRefusal, UnsupportedSchemaVersion, OSError, YAMLError) as error:
        _refused("rename", error)
        raise typer.Exit(2) from error
    _print_edit_result(result)
    console.print(
        "ran: " + build_cli_line("lockwire", "rename", *argv_from_context(ctx)), soft_wrap=True
    )


@app.command("gearbox", hidden=True)
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
        console.print(f"[red]gearbox failed:[/] no contract at {escape(str(contract))}")
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


@app.command("agent-setup")
def agent_setup_cmd(
    directory: Path = typer.Argument(Path("."), help="Repo to set up (default: here)."),
    claude: bool = typer.Option(
        False,
        "--claude",
        help="Also add the `lockwire hook-check` edit hook to .claude/settings.json.",
    ),
    ci: bool = typer.Option(
        False, "--ci", help="Write only the CI workflow, .github/workflows/lockwire.yml."
    ),
) -> None:
    """Add Lockwire to a repo: agent rules, a pre-commit hook and a CI workflow (--ci: the workflow only)."""
    from lockwire.agent_setup import agent_setup, claude_hook

    if not directory.is_dir():
        console.print(f"[red]{escape(str(directory))} is not a folder[/]; pass your repo's folder")
        raise typer.Exit(2)
    for line in agent_setup(directory.resolve(), ci_only=ci):
        console.print(line, highlight=False)
    if claude:
        console.print(claude_hook(directory.resolve()), highlight=False, markup=False)
    hint = cli_line("lockwire", "check") + " <your robot.urdf>"
    if (directory / DEFAULT_CONTRACT).is_file():
        hint = "commit lockwire.yaml and the files above"
    console.print(f"next: {hint}", highlight=False, markup=False, soft_wrap=True)


@app.command("help", hidden=True)
def help_cmd(
    ctx: typer.Context,
    show_all: bool = typer.Option(False, "--all", help="List every command, hidden ones too."),
) -> None:
    """List lockwire's commands; --all includes the ones --help leaves out."""
    if not show_all:
        typer.echo(ctx.parent.get_help())
        return
    cmds = typer.main.get_command(app).commands
    for name, command in cmds.items():
        if name != "help" and name not in plugins.PRO_COMMANDS:
            typer.echo(f"{name:24} {command.get_short_help_str(limit=70)}")
    # Listed even when the Pro package is not installed (the public build): names only, then.
    typer.echo(f"\nPro\n{plugins.WAITLIST_LINE}")
    for name in sorted(plugins.PRO_COMMANDS):
        hint = cmds[name].get_short_help_str(limit=70) if name in cmds else ""
        typer.echo(f"{name:24} {hint}".rstrip())


@app.command("hook-check", hidden=True)
def hook_check_cmd() -> None:
    """Claude Code PreToolUse hook: block a model/lockfile edit that breaks the lock (exit 2)."""
    from lockwire.hook_check import main

    raise typer.Exit(main())


@app.command("mcp")
def mcp_cmd() -> None:
    """Serve lockwire's checks, descriptions, and plan validation to an agent over MCP (stdio)."""
    try:
        from lockwire.mcp_server import build_server
    except ImportError:
        import sys

        print(
            "install the mcp extra: pip install 'lockwire[mcp]' (or: uv add 'lockwire[mcp]')",
            file=sys.stderr,
        )
        raise typer.Exit(2) from None
    build_server().run("stdio")


_MODEL_NOISE = tuple(f"model/{k}" for k in ("source", "structure_hash", "provenance"))


def _pair_sides(before: Contract, after: Contract) -> None:
    """Rename `after`'s links and joints to `before`'s when they differ only by a side token."""
    import re

    def swaps(old: list[Any], new: list[Any]) -> dict[str, str]:
        key = lambda n: re.sub(_SIDE, "_~", n.lower())  # noqa: E731
        ours = {key(x.name): x.name for x in old}
        return {x.name: ours[key(x.name)] for x in new if ours.get(key(x.name), x.name) != x.name}

    a, b = before.model, after.model
    joints, links = swaps(a.joints, b.joints), swaps(a.links, b.links)
    if len(joints) * 2 <= len(b.joints):  # most joints must pair, else leave the names alone
        return
    for link in b.links:
        link.frame = links.get(link.frame, link.frame)
        link.name = links.get(link.name, link.name)
    for joint in b.joints:
        joint.name = joints.get(joint.name, joint.name)
        joint.parent, joint.child = (
            links.get(joint.parent, joint.parent),
            links.get(joint.child, joint.child),
        )


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
    if on_disk.is_file() and on_disk.suffix.lower() in MODEL_SUFFIXES:
        import tempfile

        from lockwire.start_operation import model_lock

        try:
            with tempfile.TemporaryDirectory() as folder:
                return _load_contract_lib(model_lock(on_disk, Path(folder)))
        except (OSError, ModelDefect, ValueError) as error:
            console.print(f"[red]diff failed:[/] {escape(_why(error))}")
            raise typer.Exit(2) from error
    if not (on_disk.exists() and on_disk.is_file()):
        if repo is None:
            console.print("not a git repository — nothing to compare against")
            raise typer.Exit(0)
        if gitref.rev_parse(spec, repo=repo) is None:
            console.print(f"[red]no such ref[/] {escape(repr(spec))}")
            raise typer.Exit(2)
        relpath = gitref.relpath_in_repo(contract, repo)
        if relpath is None:
            console.print(
                f"[red]{escape(str(contract))} is outside the repository at {escape(str(repo))}[/]"
            )
            raise typer.Exit(2)
        text = gitref.show(spec, relpath, repo=repo)
        if text is None:
            console.print(f"{contract.name} did not exist at {escape(str(spec))}")
            raise typer.Exit(1 if exit_code else 0)
    try:
        return load_contract_text(text) if text is not None else _load_contract_lib(on_disk)
    except UnsupportedSchemaVersion as error:
        console.print(
            f"[bold red]REFUSED[/] — side {side} ({escape(repr(spec))}): {escape(str(error))}"
        )
        raise typer.Exit(2) from error
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]diff failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error


@app.command("diff", hidden=True)
def diff_cmd(
    a: str = typer.Argument("HEAD", help="Ref (or contract path) to compare from."),
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
        console.print(
            f"[red]no contract at {escape(str(contract))}[/] — run `lockwire start <urdf>` first"
        )
        raise typer.Exit(2)
    repo = gitref.repo_root(contract if contract.exists() else Path.cwd())
    before = _diff_side(a, contract, repo, exit_code=exit_code, side="a")
    after = _diff_side(
        b if b is not None else str(contract), contract, repo, exit_code=exit_code, side="b"
    )

    models = all(Path(x).suffix.lower() in MODEL_SUFFIXES for x in (a, b))
    if models:
        _pair_sides(before, after)
    diff = fact_diff(before, after)
    if models:  # two model files: the file's own path and hashes always differ
        diff.changes = [
            replace(c, witness=None, source=None)  # the witness is the lock's own temp file
            for c in diff.changes
            if not c.path.startswith(_MODEL_NOISE)
        ]
        if not json_output:
            mass = [sum(x.mass.value for x in c.model.links if x.mass) for c in (before, after)]
            console.print(f"mass: {mass[0]:g} kg -> {mass[1]:g} kg", highlight=False)
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


@app.command("log", cls=_LogCommand, hidden=True)
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
        console.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(2) from error
    contract = Path(path) if path is not None else DEFAULT_CONTRACT
    repo = gitref.repo_root(contract if contract.exists() else Path.cwd())
    if repo is None:
        console.print("[red]log failed:[/] not a git repository")
        raise typer.Exit(2)  # the tool cannot answer, which is not "no history"
    try:
        entries = api.log(contract, revision_range)
    except ValueError as error:
        console.print(f"[red]{escape(str(error))}[/]")
        raise typer.Exit(2) from error
    if not entries and not contract.exists():  # a deleted file's history is still shown
        console.print(f"[red]log failed:[/] no such file {escape(str(contract))}")
        raise typer.Exit(2)
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


@app.command("blame", hidden=True)
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
    hidden=True,
    # One short line on purpose: `_help_commands` in tests/test_parity.py reads
    # the first token of every line in `--help`'s Commands panel, so a
    # description that wraps invents a command that does not exist.
    help="Write the payload union's JSON schema, for the app's TypeScript types.",
)
def api_schema_cmd(
    out: Path = typer.Option(..., "--out", help="Where to write the JSON schema document."),
) -> None:
    """Write the payload union's JSON schema — every member document
     lockwire's doors emit, which is what the TypeScript types are generated from
     (packaging/generate-types.md). The library builds it, never this file
    : `api.api_schema` is the twin.
    """
    _require_folder(out)
    api.api_schema(out)
    console.print(f"wrote {escape(str(out))}")


@app.command(
    "api-types",
    hidden=True,
    # One short line on purpose: `_help_commands` in tests/test_parity.py reads
    # the first token of every line in `--help`'s Commands panel, so a
    # description that wraps invents a command that does not exist.
    help="Write app/src/shared/api.gen.ts directly from the payload union.",
)
def api_types_cmd(
    out: Path = typer.Option(..., "--out", help="Where to write the generated TypeScript."),
) -> None:
    """Write the payload union's TypeScript types — no generator
     dependency, walked straight from `api_payload.py` (v8 line 265: the
     mainstream generator's own docs say a discriminated union like ours
     narrows badly through it). The library builds it, never this file
    : `api.api_types` is the twin.
    """
    _require_folder(out)
    api.api_types(out)
    console.print(f"wrote {escape(str(out))}")


@app.command("serve", hidden=True)
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
        help="Robot mode: serve only an allowlist of read commands; every other command is refused by name.",
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
        print(
            'install the serve extra: pip install "lockwire[serve]" (repo: uv sync --extra serve)',
            file=sys.stderr,
        )
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


@app.command("record-servos", hidden=True)
def record_servos_cmd(
    port: str = typer.Option(..., "--port", help="Serial port of the servo bus adapter."),
    ids: str = typer.Option("1-6", "--ids", help="Servo IDs, e.g. 1-6 or 1,3,5."),
    kind: str = typer.Option(
        "sts", "--kind", help="Servo family: sts, scs (Feetech) or dxl (Dynamixel X, Protocol 2.0)."
    ),
    baud: int | None = typer.Option(
        None, "--baud", help="Bus baud rate (default 1000000; Dynamixel X factory 57600)."
    ),
    rate: float = typer.Option(20.0, "--rate", help="Samples per second."),
    seconds: float = typer.Option(10.0, "--seconds", help="How long to record."),
    out: Path = typer.Option(..., "--out", help="Recording directory to write."),
) -> None:
    """Record Feetech or Dynamixel servo settings and live readings (read-only; never writes to a servo)."""
    from lockwire import servo_bus

    try:
        if kind not in ("sts", "scs", "dxl"):
            raise ValueError("--kind must be sts, scs or dxl")
        servo_bus.check_rate(rate)  # before the port opens, so a refused rate leaves none open
        wanted = servo_bus.parse_ids(ids)
        manifest = servo_bus.record_servos(
            servo_bus._port_open(port, baud or (57_600 if kind == "dxl" else 1_000_000)),
            wanted,
            out,
            kind=kind,
            rate_hz=rate,
            seconds=seconds,
            port=port,
        )
    except (RuntimeError, OSError, ValueError) as error:
        console.print(f"[red]record-servos failed:[/] {escape(_why(error))}")
        raise typer.Exit(2) from error
    for gap in manifest["generator"]["gaps"]:
        console.print(f"[yellow]gap:[/] {escape(str(gap['message']))} (from {gap['from_s']} s)")
    console.print(f"[green]wrote {escape(str(out))}[/]")


plugins.load(app=app)  # after every free command is defined, so paid ones append to the same app


def _pro_stub(name: str) -> None:
    @app.command(
        name,
        hidden=True,
        help=f"Part of Lockwire Pro. {plugins.WAITLIST_LINE}",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def _stub() -> None:
        typer.echo(f"lockwire {name} is part of Lockwire Pro. {plugins.WAITLIST_LINE}")
        raise typer.Exit(2)


for _name in sorted(plugins.PRO_COMMANDS - set(typer.main.get_command(app).commands)):
    _pro_stub(_name)  # free package: a Pro name answers with the waitlist, not "No such command"
