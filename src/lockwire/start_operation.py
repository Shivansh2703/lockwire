"""The shared contract-only start operation; desktop callers never generate HTML."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from lockwire.edit import _contract_lock, contract_revision, relative_source
from lockwire.scene import SceneRefusal
from lockwire.scene_urdf import _resolve_mesh_path, mesh_root
from lockwire.schema import ArtifactRef, Contract, RobotInfo, save_contract
from lockwire.sdf import parse_sdf
from lockwire.urdf import _refuse_doctype_entity, parse_urdf, read_model_bytes


def _find_meshes(root: ET.Element, source: Path, target: Path) -> tuple[list[ArtifactRef], str]:
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
        return [], (
            f"{len(problems)} of {len(uris)} mesh files not found inside the robot's package folder "
            f"(e.g. {problems[0]}); the 3D model is not recorded."
        )
    refs = [
        ArtifactRef(path=rel, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for rel, p in sorted(found.items())
    ]
    return refs, ""


def start_contract(
    urdf: Path | str,
    *,
    out: Path | str | None = None,
    overwrite: bool = False,
    expected_revision: str | None = None,
) -> dict[str, str | bool]:
    """Create once, or replace only the exact bytes the caller observed."""
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
        raw = read_model_bytes(source)
        _refuse_doctype_entity(raw, source)
        xml = ET.fromstring(raw)
        model = parse_sdf(source) if xml.tag == "sdf" else parse_urdf(source)
        spelling = relative_source(target, source)
        meshes, note = _find_meshes(xml, source, target)
        model = model.model_copy(
            update={
                "external_assets": meshes,
                "source": spelling,
                "provenance": model.provenance.model_copy(update={"source": spelling}),
            }
        )
        contract = Contract(robot=RobotInfo(name=xml.get("name") or source.stem), model=model)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(fd)
        try:
            save_contract(contract, Path(temporary))
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
        done: dict[str, str | bool] = {
            "path": str(target),
            "revision": contract_revision(target),
            "written": True,
        }
        return {**done, "note": note} if note else done


EXAMPLES = Path(__file__).parent / "examples"


# Hidden aliases: the 0.2.1 model names still work but are never listed.
ALIASES = {"x500": "drone", "pi-servos": "servo-bench"}


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
    target.write_bytes(source.read_bytes())
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
