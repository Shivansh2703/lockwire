"""``lockwire scene import``: undeclared device placements read from a scene into an
apply-ready change file (bulk import is allowed).

The YAML is the truth, a scene never changes a contract value on its
own. Imports propose one sourced and witnessed entry per device. The
explicit --write shortcut applies those entries through the guarded contract editor.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

from lockwire.checks import PLACEMENT_TOLERANCE_M, _fixed_frame_transforms
from lockwire.edit import (
    Change,
    EditConflict,
    _backup_temp,
    _locked_write,
    _replace_then_backup,
    apply_changes,
    propose_changes,
    validate_source,
    validate_witness,
)
from lockwire.scene import Scene
from lockwire.scene_view import resolve_scene
from lockwire.schema import Contract

_ROUND = 4


class SceneImportRefusal(ValueError):
    """The import cannot proceed; message names the defect. CLI maps this to exit 2."""


@dataclass(frozen=True)
class ImportRow:
    device_id: str
    model_position: tuple[float, float, float] | None
    declared: tuple[float, float, float] | None
    action: str


@dataclass(frozen=True)
class ImportResult:
    rows: tuple[ImportRow, ...]
    entries: tuple[dict[str, Any], ...]
    skipped_non_devices: tuple[str, ...]
    root: str
    explanation: str | None = None


def format_point(point: tuple[float, ...]) -> str:
    return "(" + ", ".join(f"{v:.4f}" for v in point) + ")"


def build_import(
    contract: Contract,
    scene: Scene,
    *,
    witness: str,
    source_person: str,
    only: list[str] | None = None,
    declared_too: bool = False,
    link_based: bool = False,
) -> ImportResult:
    root, _transforms, _limitations = _fixed_frame_transforms(contract)
    if root is None:
        raise SceneImportRefusal(
            "the contract's model has no single root link; mount_frame cannot be "
            "assigned automatically."
        )

    resolved = resolve_scene(contract, scene)
    parts_by_id = {}
    for part, part_id in zip(scene.parts, resolved.part_ids, strict=True):
        if part_id is not None:
            parts_by_id.setdefault(part_id, part)

    device_ids = {device.id for device in contract.devices}
    skipped_non_devices = tuple(sorted({pid for pid in parts_by_id if pid not in device_ids}))

    devices = contract.devices
    if only is not None:
        wanted = set(only)
        devices = [d for d in devices if d.id in wanted]

    if not any(d.id in parts_by_id for d in devices):
        if link_based:
            # A URDF/SDF's parts are links, not devices (the name join). Nothing joining
            # is a normal outcome of a fresh model, not a tool failure — explain it in
            # plain words and hand back zero entries rather than refusing.
            link_ids = ", ".join(sorted(parts_by_id)) or "no links at all"
            device_ids_str = ", ".join(sorted(d.id for d in devices)) or "no devices"
            explanation = (
                f"the model names {link_ids}; the contract's devices are {device_ids_str}; "
                "no device is called that — name a device after its link, or give the "
                "model extras.lockwire.id"
            )
            return ImportResult(
                rows=tuple(
                    ImportRow(device.id, None, None, "not in the model") for device in devices
                ),
                entries=(),
                skipped_non_devices=skipped_non_devices,
                root=root,
                explanation=explanation,
            )
        raise SceneImportRefusal(
            "no device parts found in the scene for the requested devices; nothing to import."
        )

    today = dt.date.today().isoformat()
    rows: list[ImportRow] = []
    entries: list[dict[str, Any]] = []

    for device in devices:
        part = parts_by_id.get(device.id)
        if part is None:
            rows.append(ImportRow(device.id, None, None, "not in the model"))
            continue

        model_pos = tuple(round(v, _ROUND) for v in part.translation)
        physical = device.physical
        source = f"{scene.source} node {device.id}; confirmed by {source_person}"
        note = f"position read from the model on {today} and confirmed by {source_person}"

        if physical is not None and physical.mount_xyz is not None:
            declared = tuple(round(c.base(), _ROUND) for c in physical.mount_xyz)
            distance_mm = (
                math.sqrt(sum((a - b) ** 2 for a, b in zip(model_pos, declared, strict=True)))
                * 1000
            )
            if distance_mm / 1000 <= PLACEMENT_TOLERANCE_M:
                rows.append(ImportRow(device.id, model_pos, declared, "agrees, not touched"))
                continue
            if not declared_too:
                rows.append(
                    ImportRow(
                        device.id,
                        model_pos,
                        declared,
                        f"differs by {distance_mm:.1f} mm, not touched",
                    )
                )
                continue
            entries.append(
                {
                    "path": f"devices/{device.id}/physical/mount_xyz",
                    "value": list(model_pos),
                    "unit": "m",
                    "source": source,
                    "witness": witness,
                    "note": note,
                }
            )
            rows.append(
                ImportRow(
                    device.id,
                    model_pos,
                    declared,
                    f"differs by {distance_mm:.1f} mm, written",
                )
            )
            continue

        # No mount_xyz declared: the bulk-import path.
        entries.append(
            {
                "path": f"devices/{device.id}/physical/mount_xyz",
                "value": list(model_pos),
                "unit": "m",
                "source": source,
                "witness": witness,
                "note": note,
            }
        )
        if physical is None or physical.mount_frame is None:
            entries.append(
                {
                    "path": f"devices/{device.id}/physical/mount_frame",
                    "value": root,
                    "source": source,
                    "witness": witness,
                    "note": (
                        f"mount_frame set to the model's root link ({root}) from the "
                        f"model on {today}, confirmed by {source_person}"
                    ),
                }
            )
        rows.append(ImportRow(device.id, model_pos, None, "written"))

    return ImportResult(
        rows=tuple(rows),
        entries=tuple(entries),
        skipped_non_devices=skipped_non_devices,
        root=root,
    )


def write_change_file(entries: tuple[dict[str, Any], ...], out: Path, *, scene_source: str) -> None:
    """The single place this module writes bytes to disk — every witness and
    every source in ``entries`` is validated here, before the file is opened, so no write
    path around this module (direct library call included) can land a blank or "you"
    witness, or an absolute source path.
    """
    for witness in {entry["witness"] for entry in entries if "witness" in entry}:
        validate_witness(witness, refusal=f"--witness must name a real person, not {witness!r}")
    for source in {str(entry["source"]) for entry in entries if "source" in entry}:
        validate_source(source)
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False
    today = dt.date.today().isoformat()

    def _flow_lists(entry: dict[str, Any]) -> dict[str, Any]:
        out_entry = dict(entry)
        value = out_entry.get("value")
        if isinstance(value, list):
            seq = CommentedSeq(value)
            seq.fa.set_flow_style()
            out_entry["value"] = seq
        return out_entry

    fd, temporary_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"# written by lockwire scene import — {scene_source}, {today}\n")
            yaml.dump([_flow_lists(entry) for entry in entries], handle)
        if out.exists():
            _replace_then_backup(temporary_path, out, out.read_text(encoding="utf-8"))
        else:
            os.replace(temporary_path, out)
    finally:
        temporary_path.unlink(missing_ok=True)


@_locked_write
def write_and_apply_import(
    contract_path: Path,
    entries: tuple[dict[str, Any], ...],
    out: Path,
    *,
    scene_source: str,
    expected_revision: str | None = None,
) -> None:
    """Keep the scene-import shortcut's draft and contract under one revision lock."""
    if expected_revision is None:
        raise EditConflict("--write requires --expected-revision")
    backup = out.with_name(out.name + ".bak")
    if {out.resolve(), backup.resolve()} & {
        contract_path.resolve(),
        contract_path.with_name(contract_path.name + ".bak").resolve(),
    }:
        raise SceneImportRefusal("change file must differ from the contract and its backup")

    changes = [Change(**entry) for entry in entries]
    propose_changes(contract_path, changes)
    prior_out = _backup_temp(out, out.read_bytes()) if out.exists() else None
    try:
        prior_backup = _backup_temp(backup, backup.read_bytes()) if backup.exists() else None
    except BaseException:
        if prior_out is not None:
            prior_out.unlink(missing_ok=True)
        raise
    retain: set[Path] = set()
    try:
        write_change_file(entries, out, scene_source=scene_source)
        try:
            apply_changes(contract_path, changes, expected_revision=expected_revision)
        except BaseException as apply_error:
            failures: list[str] = []
            for target, prior in ((out, prior_out), (backup, prior_backup)):
                try:
                    if prior is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(prior, target)
                except BaseException as restore_error:
                    if prior is not None:
                        retain.add(prior)
                    failures.append(f"{target}: {restore_error}")
            if failures:
                artifacts = (
                    ", ".join(str(path) for path in sorted(retain)) or "none (prior file absent)"
                )
                raise OSError(
                    "scene import could not restore draft after contract failure; "
                    f"recovery bytes at {artifacts}; {'; '.join(failures)}"
                ) from apply_error
            raise
    finally:
        for prior in (prior_out, prior_backup):
            if prior is not None and prior not in retain:
                prior.unlink(missing_ok=True)
