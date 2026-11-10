"""The shared contract-only start operation; desktop callers never generate HTML."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from lockwire.edit import _contract_lock, contract_revision, relative_source
from lockwire.scene import SceneRefusal
from lockwire.scene_urdf import _resolve_mesh_path, mesh_root
from lockwire.schema import (
    ArtifactRef,
    Contract,
    RobotInfo,
    SCHEMA_VERSION,
    load_contract,
    save_contract,
)
from lockwire.sdf import parse_sdf
from lockwire.urdf import _refuse_doctype_entity, parse_urdf, read_model_bytes


def _find_meshes(
    root: ET.Element, source: Path, target: Path
) -> tuple[list[ArtifactRef], str, dict[str, int | str] | None]:
    """The model's mesh files as sha256-pinned refs inside the robot's package folder.

    All-or-nothing: the scene loader refuses a mesh it cannot verify, so a partial list
    would break the whole scene. Anything missing or outside the package comes back as a note.
    """
    uris = sorted({u for m in root.iter("mesh") if (u := m.get("filename") or m.findtext("uri"))})
    folder, found, problems = mesh_root(source, target.parent), {}, []
    for uri in uris:
        try:
            path = _resolve_mesh_path(
                uri, source.parent, source, (source.parent, folder, *source.parents[1:3])
            ).resolve()
            found[path.relative_to(folder).as_posix()] = path
        except (SceneRefusal, ValueError):
            problems.append(uri)
    if problems:
        gap = {"missing": len(problems), "total": len(uris), "example": problems[0]}
        return (
            [],
            (
                f"{len(problems)} of {len(uris)} mesh files not found inside the robot's package folder "
                f"(e.g. {problems[0]}); the 3D model is not recorded."
            ),
            gap,
        )
    refs = [
        ArtifactRef(path=rel, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for rel, p in sorted(found.items())
    ]
    return refs, "", None


def start_contract(
    urdf: Path | str,
    *,
    out: Path | str | None = None,
    overwrite: bool = False,
    expected_revision: str | None = None,
    xacro_args: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Create once, or replace only the exact bytes the caller observed.

    ``warnings`` collects the model oddities a check reports (they are in the note too)."""
    source = Path(urdf).resolve()
    target = Path(out).resolve() if out is not None else source.parent / "lockwire.yaml"
    if source == target:
        raise ValueError("model and lockfile paths must differ")
    with _contract_lock(target):
        exists = target.exists()
        if exists and not overwrite:
            return {"path": str(target), "revision": contract_revision(target), "written": False}
        if overwrite and (
            not exists
            or expected_revision is None
            or expected_revision != contract_revision(target)
        ):
            raise ValueError("lockfile changed since it was opened; reload before overwriting")
        notes: list[str] = []
        warned = warnings if warnings is not None else []
        try:
            raw = read_model_bytes(source, xacro_args)
            _refuse_doctype_entity(raw, source)
            xml = ET.fromstring(raw)
            model = (
                parse_sdf(source)
                if xml.tag == "sdf"
                else parse_urdf(source, xacro_args, notes, warned)
            )
        except ValueError as error:  # name the model as the caller typed it, not resolved
            message = error.args[0] if len(error.args) == 1 else None
            if isinstance(message, str) and str(error) == message:  # a plain one-line message
                error.args = (message.replace(str(source), str(urdf)),)
            raise
        notes += warned
        spelling = relative_source(target, source)
        meshes, note, gap = _find_meshes(xml, source, target)
        note = "\n".join([*notes, note] if note else notes)
        model = model.model_copy(
            update={
                "external_assets": meshes,
                "xacro_args": xacro_args or None,
                "source": spelling,
                "provenance": model.provenance.model_copy(update={"source": spelling}),
            }
        )
        contract = Contract(robot=RobotInfo(name=xml.get("name") or source.stem), model=model)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(fd)
        try:
            save_contract(contract, Path(temporary))
            load_contract(Path(temporary))  # an unloadable lock never replaces the old one
            # Refuse even a non-cooperating writer that changed bytes during parsing.
            if exists and contract_revision(target) != expected_revision:
                raise ValueError("lockfile changed since it was opened; reload before overwriting")
            if exists:
                shutil.copymode(target, temporary)
                os.replace(temporary, target)
            else:
                # Installing a new file must never replace a writer racing this start.
                try:
                    os.link(temporary, target)
                except FileExistsError as exc:
                    raise ValueError(
                        "lockfile appeared during start; open it before overwriting"
                    ) from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
        done: dict[str, Any] = {
            "path": str(target),
            "revision": contract_revision(target),
            "written": True,
        }
        if gap:
            done["mesh_gap"] = gap
        return {**done, "note": note} if note else done


MODEL_SUFFIXES = (".urdf", ".xacro", ".sdf")


def add_mass_check(
    lock: Path,
    mass: str,
    *,
    source: str | None = None,
    tolerance_pct: float = 5.0,
    witness: str | None = None,
) -> None:
    """The datasheet mass as the `mass_total` budget; the limit is derived, not asked for."""
    import getpass

    from lockwire.edit import add_entry, parse_value, resolve_witness
    from pydantic import ValidationError

    from lockwire.schema import Quantity

    try:
        nominal = Quantity.model_validate(parse_value(mass, field_name="nominal"))
    except ValidationError as error:
        raise ValueError(f"--mass must look like '20.6 kg', not {mass!r}") from error
    if nominal.dimension() != "mass" or nominal.value <= 0:
        raise ValueError(f"--mass must be a positive mass like '20.6 kg', not {mass!r}")
    if tolerance_pct < 0:
        raise ValueError("--tolerance-pct must be 0 or more")
    fields = {
        "id": "bud.mass",
        "budget_kind": "mass_total",
        "sources": "links",
        "nominal": mass,
        "tolerance_pct": tolerance_pct,
        "limit": f"{nominal.base() * (1 + tolerance_pct / 100):.6g} kg",
        "owner": "you",
    }
    # the repo refuses the bare word "you" as a witness: use the machine's name for the user
    witness = witness or resolve_witness(None) or getpass.getuser()
    add_entry(lock, "budget", fields, source=source or "typed on the command line", witness=witness)


def model_lock(model: Path, folder: Path, *, mass: str | None = None, **options) -> Path:
    """A lock of `model` inside `folder` (a temp dir unless the caller wants it kept)."""
    lock = folder / "lockwire.yaml"
    start_contract(
        model,
        out=lock,
        xacro_args=options.pop("xacro_args", None),
        warnings=options.pop("warnings", None),
    )
    if mass:
        add_mass_check(lock, mass, **options)
    return lock


def robot_by_name(name: str) -> Path:
    """The URDF `robot_descriptions` holds for `go2` / `go2_description` (xacro expanded by it).

    Raises LookupError with the line to print: extra missing, MJCF only, or unknown name
    (with up to three close matches). Importing a description module git-clones it once
    into ~/.cache/robot_descriptions; its "Cloning ..." goes to stderr so `--json -` stays clean.
    """
    import contextlib
    import difflib
    import importlib
    import sys

    if not sys.stderr.isatty():  # clone progress bars: ~20 KB of noise in a pipe
        os.environ.setdefault("TQDM_DISABLE", "1")
    try:
        from robot_descriptions import DESCRIPTIONS
        from robot_descriptions._xacro import get_urdf_path  # expands xacro without ROS
        from tqdm import tqdm  # robot_descriptions' clone progress bar
    except ImportError:
        raise LookupError(
            f"no file named {name}; to check a robot by name: "
            f'uvx --from "lockwire[robots]" lockwire check {name}'
        ) from None
    key = name.lower().replace("-", "_").removesuffix("_description")
    found = DESCRIPTIONS.get(f"{key}_description")
    if found is None or not found.has_urdf:
        if found is not None or f"{key}_mj_description" in DESCRIPTIONS:
            raise LookupError(f"only an MJCF exists for {name}; try `lockwire check-sim` on it")
        names = sorted({k.removesuffix("_description").removesuffix("_mj") for k in DESCRIPTIONS})
        close = difflib.get_close_matches(key, names, n=3)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        raise LookupError(f"no file or robot named {name}{hint}")
    print(f"fetching {name} from robot_descriptions (first time only)…", file=sys.stderr)
    tqdm.monitor_interval = 0  # its monitor daemon thread segfaulted CPython 3.12 at exit
    try:
        with contextlib.redirect_stdout(sys.stderr):
            module = importlib.import_module(f"robot_descriptions.{key}_description")
            return Path(get_urdf_path(module))
    except Exception as error:  # git, network and xacro failures surface as many types
        raise LookupError(f"could not fetch {name}: {error}") from error


def copy_beside(model: Path, folder: Path, name: str) -> Path:
    """The fetched URDF copied into the user's folder as `<name>.urdf`, so the lockfile
    points at a file they own and edit, not the cache. Refuses to replace different bytes."""
    copy = folder / f"{name}.urdf"
    data = model.read_bytes()
    if copy.exists() and copy.read_bytes() != data:
        raise ValueError(
            f"{copy} exists and differs from the fetched {name}; move it or pass a path"
        )
    copy.write_bytes(data)
    return copy


def rerun_command(findings, command: str) -> None:
    """Point a datasheet-mass mismatch at the command that re-checks it."""
    for finding in findings:
        if finding.severity == "violation" and finding.quantity.endswith("/nominal"):
            finding.next_command = finding.next_command or command


def accept_hint(findings, contract: Path) -> None:
    """A datasheet gap in a real lockfile can be accepted on record: name that command."""
    from lockwire.cli_line import cli_line

    for finding in findings:
        if finding.severity != "not_scored" and finding.quantity.startswith("datasheet/"):
            finding.next_command = cli_line(
                "lockwire", "accept", finding.quantity, contract,
                "--reason", "<why the gap is known>", "--witness", "<who checked>",
            )  # fmt: skip


def typed_paths(findings, lock: Path, model: Path, shown: str | None = None) -> None:
    """Hints from a throwaway lock name the model as typed (`shown`: a robot name) and a
    lock beside it, or in the working folder for a name; nothing is locked, so no hint
    says "at lock" or re-locks with --force."""
    from lockwire.cli_line import cli_line

    rel = relative_source(lock, model)
    beside = "lockwire.yaml" if shown else str(model.parent / "lockwire.yaml")
    shown = shown or str(model)
    swaps = [  # longest first: the resolved spelling ends with `rel`
        (str(lock.resolve().parent / rel), shown),
        (str(model.resolve()), shown),
        (str(lock), beside),
        (rel, shown),
    ]
    for finding in findings:
        finding.observed = finding.observed.replace(" not found at lock", " not found")
        if " --force" in (finding.next_command or ""):
            finding.next_command = None
        for field, quote in (("source", str), ("action", str), ("next_command", shlex.quote)):
            text = getattr(finding, field)
            if not text:
                continue
            for old, new in swaps:
                text = text.replace(quote(old), quote(new))
            setattr(finding, field, text)
        if finding.action and not Path(beside).exists():  # swap needs a lockfile; start makes one
            finding.action = re.sub(
                r"find a stronger servo: lockwire swap .*$",
                f"find a stronger servo: lock the model first with `lockwire {cli_line('start', shown)}`",
                finding.action,
            )


def drop_same_figure(findings, mass: str | None) -> None:
    """A `run:` line that re-runs the mass comparison just made says nothing new: drop it.

    Without a typed --mass the comparison was the catalog's own figure, which is the one
    every datasheet re-run line carries."""
    import math

    from lockwire.edit import parse_value
    from lockwire.schema import Quantity

    def kg(text: str) -> float:
        return Quantity.model_validate(parse_value(text, field_name="nominal")).base()

    for finding in findings:
        words = shlex.split(finding.next_command or "")
        if "--mass" in words:
            figure = words[words.index("--mass") + 1]
            if mass is None or math.isclose(kg(figure), kg(mass)):
                finding.next_command = None


def zero_if_nothing_compared(findings) -> None:
    """A fresh lock agrees with its own file: that hash match compares nothing."""
    if not set(findings.scored_keys or ()) - {"model/structure_hash"}:
        findings.scored = 0


def warning_findings(warned: list[str]) -> list:
    """The loader's warnings about a model file, as findings (CLI and MCP alike)."""
    from lockwire.report import Finding

    return [
        Finding(
            severity="warning",
            quantity="model/source",
            observed=line,
            expected="a model lockwire can read as written",
            action="fix the model file if this was not meant",
        )
        for line in warned
    ]


def defect_report(error: Exception):
    """A dup/missing link or joint in the model file: one NO-GO violation, not a tool failure."""
    from lockwire.report import Finding, RecordReport

    return RecordReport(
        [
            Finding(
                severity="violation",
                quantity="model/structure",
                observed=str(error),
                expected="every joint names links the model has, and every name is used once",
                action="fix the model file, then run the check again",
            )
        ]
    )


EXAMPLES = Path(__file__).parent / "examples"


# Hidden aliases: the 0.2.1 model names still work but are never listed.
ALIASES = {"x500": "drone", "pi-servos": "servo-bench"}


# The one failing demo is never named without saying it fails on purpose.
SERVO_BENCH = "servo-bench (built to fail: a 5 V rail overloaded and a wrist servo too weak, on purpose, so you can see what a catch looks like)"


def example_label(name: str) -> str:
    """An example's name as shown to people: servo-bench carries its built-to-fail label."""
    return SERVO_BENCH if ALIASES.get(name, name) == "servo-bench" else name


def example_names() -> list[str]:
    """Bundled examples: ``<name>.yaml``, or ``<name>/contract.yaml`` with an optional ``scene.glb``."""
    return sorted(
        {p.stem for p in EXAMPLES.glob("*.yaml")}
        | {p.parent.name for p in EXAMPLES.glob("*/contract.yaml")}
    )


def write_example(name: str, target: Path) -> str | None:
    """Write the example's lockfile to ``target`` and its scene and example plan (if any) beside it as ``<target stem>.glb`` / ``<target stem>.plan.yaml``.

    An existing scene or plan beside ``target`` is kept; the returned note names a kept scene."""
    name = ALIASES.get(name, name)
    folder = EXAMPLES / name
    source = folder / "contract.yaml" if folder.is_dir() else EXAMPLES / f"{name}.yaml"
    # byte-for-byte copy; only the version line moves to the current version
    text = re.sub(
        r"^(schema_version:[ \t]*)([\"']?)[^\"'\s#]+\2",
        lambda m: f"{m[1]}{m[2]}{SCHEMA_VERSION}{m[2]}",
        source.read_bytes().decode("utf-8"),
        count=1,
        flags=re.M,
    )
    target.write_bytes(text.encode("utf-8"))
    scene, glb, note = folder / "scene.glb", target.with_suffix(".glb"), None
    if scene.is_file() and glb.exists():  # never overwrite a user's scene
        note = f"kept your existing {glb.name}; the example's scene was not copied"
    elif scene.is_file():
        glb.write_bytes(scene.read_bytes())
    plan = folder / "plan.yaml"
    sibling = target.with_name(f"{target.stem}.plan.yaml")
    if plan.is_file() and not sibling.exists():
        sibling.write_bytes(plan.read_bytes())
    return note


def start_example(name: str, out: Path | str) -> dict[str, str | bool]:
    """Copy a bundled example to ``out``; an existing file is kept, never overwritten."""
    name = ALIASES.get(name, name)
    if name not in example_names() or "/" in name or "\\" in name:
        raise ValueError(f"no example named {name!r}")
    target = Path(out).resolve()
    if target.exists():
        return {"path": str(target), "revision": contract_revision(target), "written": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    note = write_example(name, target)
    done = {"path": str(target), "revision": contract_revision(target), "written": True}
    return {**done, "note": note} if note else done


def starters() -> list[dict[str, str]]:
    """The "New robot" starters: every examples folder that has a ``starter.json``."""
    found = []
    for meta in sorted(EXAMPLES.glob("*/starter.json")):
        info = json.loads(meta.read_text(encoding="utf-8"))
        found.append(
            {"name": meta.parent.name, "title": info["title"], "based_on": info["based_on"]}
        )
    return found


def start_blank(out: Path | str) -> dict[str, str | bool]:
    """Write a contract holding only the robot's name; an existing file is kept, never overwritten."""
    target = Path(out).resolve()
    if target.exists():
        return {"path": str(target), "revision": contract_revision(target), "written": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    save_contract(Contract(robot=RobotInfo(name="New robot")), target)
    return {"path": str(target), "revision": contract_revision(target), "written": True}
