"""``lockwire declare`` / ``lockwire apply`` / ``lockwire add`` — the edit primitive.

The YAML is the truth. Every edit lands as one declared fact carrying a
source and a witness, written through a ruamel round-trip load/modify/dump
so existing comments, key order and quoting style survive untouched outside the
edited lines. Nothing is written unless the edited document validates through
``Contract`` first — the same byte cap and anchor/alias refusal the rest of the
codebase applies to a lockfile apply here too.
"""

from __future__ import annotations

import base64
import datetime as dt
import copy
import difflib
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import types
import tomllib
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache, wraps
from pathlib import Path
import typing
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML, CommentedMap
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarfloat import ScalarFloat

from lockwire.schema import (
    Contract,
    MAX_CONTRACT_BYTES,
    _guard_contract_raw,
    plain_message,
    _load_yaml_guarded,
    _model_structure_hash,
    _reject_yaml_anchors_aliases,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    UnsupportedSchemaVersion,
    load_contract,
)
from lockwire import _filelock, meshread, schema, units
from lockwire.report import LIMIT_RAISED_MARK


class EditRefusal(ValueError):
    """A declare/apply/add refused before writing anything."""


class EditConflict(EditRefusal):
    """The bytes a caller edited are no longer the bytes on disk."""


_locked_contracts: ContextVar[frozenset[Path]] = ContextVar("locked_contracts", default=frozenset())
_journal_off: ContextVar[bool] = ContextVar("journal_off", default=False)  # staging a batch


def contract_revision(path: Path | str) -> str:
    """Opaque revision for the exact contract bytes, not its semantic hash."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def _contract_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating writers with an advisory lock in `$TMPDIR/lockwire-locks/`.

    Keyed by the contract's resolved path, outside the user's folder. The holder
    unlinks the file before releasing it, so nothing piles up; a waiter that opened the
    old inode wins its lock, sees the path no longer names that inode, and retries on the
    fresh file, so two writers never hold different inodes for one contract.
    ponytail: the temp folder is per user on macOS, so two accounts editing one contract do
    not serialize; share a lock folder if that ever matters. A crashed holder leaves one
    file, removed by the next writer of that contract.
    """
    # os.path strings, not Path(): Path() picks its flavour from os.name at call time.
    folder = os.path.join(tempfile.gettempdir(), "lockwire-locks")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
    lock_path = os.path.join(folder, f"{digest}.lock")
    while True:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _filelock.lock(fd)
        except TimeoutError:
            os.close(fd)
            raise EditConflict("contract is locked by another lockwire process; retry") from None
        try:
            if os.path.samestat(os.fstat(fd), os.stat(lock_path)):
                break
        except FileNotFoundError:
            pass
        _filelock.unlock(fd)
        os.close(fd)
    try:
        yield
    finally:
        try:
            os.unlink(lock_path)  # while still held; Windows refuses an open file: it stays
        except OSError:
            pass
        _filelock.unlock(fd)
        os.close(fd)


def _locked_write(function: Any) -> Any:
    """Lock, then compare raw bytes before any writer reads its document."""

    @wraps(function)
    def wrapped(
        contract_path: Path, *args: Any, expected_revision: str | None = None, **kwargs: Any
    ) -> Any:
        resolved = contract_path.resolve()
        if resolved in _locked_contracts.get():
            return function(contract_path, *args, expected_revision=expected_revision, **kwargs)
        with _contract_lock(resolved):
            actual = contract_revision(resolved)
            if expected_revision is not None and expected_revision != actual:
                raise EditConflict(
                    "contract changed since it was opened; reload before applying this edit"
                )
            token = _locked_contracts.set(_locked_contracts.get() | {resolved})
            try:
                return function(resolved, *args, expected_revision=expected_revision, **kwargs)
            finally:
                _locked_contracts.reset(token)

    return wrapped


# the style `init` writes when no existing list tells us otherwise —
# schema.py's `save_contract` uses the same setting, so the two agree.
_DEFAULT_SEQUENCE_STYLE = (4, 2)  # (sequence indent, dash offset)

_SEQUENCE_STYLE_RE = re.compile(r"^([ \t]*)\S[^\n:]*:[ \t]*\n\1([ \t]*)-(?:[ \t]|$)", re.MULTILINE)


def _sniff_sequence_style(text: str) -> tuple[int, int]:
    """This file's own list style: (sequence indent, dash offset).

    An `init`-written contract opens a list right under its key
    (``links:\\n  - name:``, offset 0); a file the editor already touched, or
    one written by hand at the tool's other historic default, indents the
    dash under its key (``devices:\\n  - id:``, offset 2). ruamel's indent
    setting is document-wide, not per-key, so ANY block list in the file
    (top-level or nested) tells us the whole file's style — read it from the
    first ``key:`` line immediately followed by a ``- `` item at the same or
    deeper indent. No block list anywhere (a brand-new/list-less file) falls
    back to the same style `init` writes, so the two always agree.
    """
    match = _SEQUENCE_STYLE_RE.search(text)
    if not match:
        return _DEFAULT_SEQUENCE_STYLE
    offset = len(match.group(2))
    return offset + 2, offset


_SRC_TEXT_ATTR = "_lockwire_src_text"


def _construct_yaml_float_keeping_src(constructor: Any, node: Any) -> Any:
    """Load a float the normal ruamel way, then remember its exact source text.

    `ScalarFloat` does not keep the characters it was parsed from — only
    width/precision/exponent metadata, which its own representer uses to
    *reconstruct* a string. That reconstruction is lossy: on a full
    double-precision mantissa it can drift the last digit by one ULP even
    though the field was never touched (seen on a
    real `init anymal_d.urdf` -> `add family`: 10 untouched floats drifted —
    comparing the reconstructed text's parsed *value* to the original
    misses all of them, since a drifted string still parses back to
    the same float64). Stashing the raw text here and having the representer
    (below) emit it verbatim for an unchanged node is the only reliable fix.
    """
    value = RoundTripConstructor.construct_yaml_float(constructor, node)
    if isinstance(value, ScalarFloat):
        setattr(value, _SRC_TEXT_ATTR, node.value)
    return value


def _represent_scalar_float_keeping_src(representer: Any, data: Any) -> Any:
    """Emit a `ScalarFloat`'s ORIGINAL text verbatim when it carries one.

    Only a node untouched since load keeps `_lockwire_src_text` (it lives on
    the same `ScalarFloat` instance `_construct_yaml_float_keeping_src`
    tagged); anything `declare`/`add` set is a plain float/int/new
    `ScalarFloat` from `parse_value` and goes through ruamel's normal
    (reconstructing) path instead — which is fine, it has no "original" text
    to preserve.
    """
    node = RoundTripRepresenter.represent_scalar_float(representer, data)
    src = getattr(data, _SRC_TEXT_ATTR, None)
    if src is not None:
        node.value = src
    return node


def _yaml(sequence: int = 4, offset: int = 2) -> YAML:
    """This module's own round-trip settings — never schema.py's loader.

    Preserve existing quote characters, the file's own sniffed list style
    (see `_sniff_sequence_style`), and never re-wrap a long line (ruamel's
    default width is 80 and counts against every scalar, not just prose — a
    long provenance note would otherwise get rewrapped on every write that
    merely touches a sibling key). The float constructor/representer pair is
    registered so an untouched float scalar survives byte-for-byte.
    """
    y = YAML()
    y.default_flow_style = False
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=sequence, offset=offset)
    y.width = 10_000
    y.constructor.add_constructor("tag:yaml.org,2002:float", _construct_yaml_float_keeping_src)
    y.representer.add_representer(ScalarFloat, _represent_scalar_float_keeping_src)
    return y


# ---------------------------------------------------------------- value parsing

_BOOL = {"true": True, "false": False}
_UNIT_CHARS = r"[A-Za-z0-9µ/*^%]+"
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_SINGLE_RE = re.compile(rf"^({_NUM})\s*({_UNIT_CHARS})$")
_TRIPLE_RE = re.compile(rf"^\[?\s*({_NUM})[,\s]+({_NUM})[,\s]+({_NUM})\s*\]?\s+({_UNIT_CHARS})$")


def _flow_quantity(value: float, unit: str) -> CommentedMap:
    """A `{value, unit}` quantity written flow-style, matching every one in the fixtures."""
    q = CommentedMap([("value", value), ("unit", unit)])
    q.fa.set_flow_style()
    return q


@cache
def _quantity_field_names() -> frozenset[str]:
    """Every schema field whose declared type is a Quantity (the type decides, not the string)."""

    def has_quantity(annotation: Any) -> bool:
        if annotation is schema.Quantity:
            return True
        return any(has_quantity(arg) for arg in typing.get_args(annotation))

    return frozenset(
        name
        for model in vars(schema).values()
        if isinstance(model, type) and issubclass(model, BaseModel)
        for name, info in model.model_fields.items()
        if has_quantity(info.annotation)
    )


# Flag-typed fields the schema types as a bare number (not a Quantity).
_PLAIN_NUMBER_FIELDS = ("bitrate", "efficiency", "min_capture_window_s")


def parse_value(raw: str, *, field_name: str, flag: str | None = None) -> Any:
    """`_parse_value`, then refuse text/bool for a field the schema types as a Quantity.

    `flag` is the CLI flag the person typed; the refusal names it instead of the field.
    """
    value = _parse_value(raw, field_name=field_name)
    if isinstance(value, str | bool) and field_name in _quantity_field_names():
        raise EditRefusal(
            f'{flag or field_name} must be a number with a unit, like "12 V" or "2.5 kg"; got "{raw.strip()}".'
        )
    if flag and isinstance(value, str | bool) and field_name in _PLAIN_NUMBER_FIELDS:
        raise EditRefusal(f'{flag} must be a plain number, like 5; got "{raw.strip()}".')
    return value


# Plain (unitless-in-text) 3-float fields: com is metres, mount_rpy radians, fixed by the schema.
_VECTOR_FIELDS = ("com", "mount_rpy")


def _parse_vector(raw: str, field_name: str) -> list[float]:
    """"x y z", "x,y,z" or "[x, y, z]" -> three finite floats, else one plain refusal."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        nums = [float(p) for p in text.replace(",", " ").split()]
    except ValueError:
        nums = []
    if len(nums) != 3 or not all(math.isfinite(n) for n in nums):
        raise EditRefusal(
            f'{field_name} needs three numbers, like "0.1 0 0.02", "0.1,0,0.02" or "[0.1, 0, 0.02]"; got "{raw.strip()}".'
        )
    return nums


def _parse_value(raw: str, *, field_name: str) -> Any:
    """Parse a `declare`/`apply` VALUE string into a YAML-writable python value.

    Grammar: a bare number -> number; "25 W" -> {value, unit}; a triple
    ("0.12 0 0.05 m" or "[0.12, 0, 0.05] m") -> a 3-tuple of quantities, only
    for ``mount_xyz``; true/false -> bool; a quoted string -> string; anything
    else is kept as a plain string.
    """
    text = raw.strip()
    if field_name in _VECTOR_FIELDS:
        return _parse_vector(raw, field_name)
    if field_name == "mount_shape":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise EditRefusal(
                "mount_shape must be a JSON object with kind and dimensions_mm"
            ) from error
        if not isinstance(value, dict):
            raise EditRefusal("mount_shape must be a JSON object")
        return value
    if text.lower() in _BOOL:
        return _BOOL[text.lower()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(number):
            raise EditRefusal(f"{raw!r} is not a usable number; write a plain number like 20.6")
        return number
    if field_name == "mount_xyz":
        triple = _TRIPLE_RE.match(text)
        if triple:
            x, y, z, unit = triple.groups()
            if not units.known(unit):
                raise EditRefusal(f"unknown unit {unit!r} in {raw!r}")
            return [
                _flow_quantity(float(x), unit),
                _flow_quantity(float(y), unit),
                _flow_quantity(float(z), unit),
            ]
    single = _SINGLE_RE.match(text)
    if single:
        value, unit = single.groups()
        if units.known(unit):
            if not math.isfinite(float(value)):
                raise EditRefusal(f"{raw!r} is not a usable number; write a plain number like 20.6")
            return _flow_quantity(float(value), unit)
        raise EditRefusal(f"unknown unit {unit!r} in {raw!r}")
    return text


def _attach_unit(value: Any, unit: str) -> Any:
    """Merge a change entry's sibling `unit:` key into its `value:`.

    `lockwire scene import` writes the raw model numbers this way: a bare
    3-list for a mount_xyz triple, or a bare number for a single quantity —
    the schema's `{value, unit}` shape is the schema layer's job, not the importer's.
    """
    if not units.known(unit):
        raise EditRefusal(f"unknown unit {unit!r}")
    if (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(v, int | float) for v in value)
    ):
        return [_flow_quantity(float(v), unit) for v in value]
    if isinstance(value, int | float):
        return _flow_quantity(float(value), unit)
    return value


def format_value(value: Any) -> str:
    """Render a parsed value the way `declared ...` and diffs describe it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict) and "value" in value and "unit" in value:
        return f"{value['value']:g} {value['unit']}"
    if (
        isinstance(value, list)
        and value
        and all(isinstance(v, dict) and "value" in v and "unit" in v for v in value)
    ):
        nums = ", ".join(f"{v['value']:g}" for v in value)
        return f"[{nums}] {value[0]['unit']}"
    return str(value)


# ---------------------------------------------------------------- path grammar

_TOPOLOGY_OWNER_COLLECTIONS = {"rails", "buses", "hosts", "wires"}
_TOP_LEVEL_ID_COLLECTIONS = {"devices", "device_models", "budgets", "families"}


def split_path(path: str) -> list[str]:
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise EditRefusal(f"empty path {path!r}")
    return segments


def _navigate(doc: Any, segments: list[str], *, create: bool) -> Any:
    """Walk `doc` along `segments`; create missing dict children when `create`.

    Never creates a new list entry — reaching a list segment that no id/name
    matches is always a refusal (that is `lockwire add`'s job, not declare's).
    """
    node = doc
    walked: list[str] = []
    for seg in segments:
        walked.append(seg)
        if isinstance(node, dict):
            if seg not in node or node[seg] is None:
                if not create:
                    raise EditRefusal(f"no key {seg!r} at {'/'.join(walked)}")
                node[seg] = CommentedMap()
            node = node[seg]
        elif isinstance(node, list):
            hit = next(
                (
                    x
                    for x in node
                    if isinstance(x, dict) and (x.get("id") == seg or x.get("name") == seg)
                ),
                None,
            )
            if hit is None:
                raise EditRefusal(
                    f"no entry with id/name {seg!r} at {'/'.join(walked)} "
                    "(use `lockwire add` to create a new device/rail/bus)"
                )
            node = hit
        else:
            raise EditRefusal(f"{'/'.join(walked)} reached a leaf, not a container")
    return node


def provenance_owner_path(segments: list[str]) -> list[str]:
    """Prefix of `segments` naming the block whose `provenance` owns this fact."""
    if (
        len(segments) >= 3
        and segments[0] == "topology"
        and segments[1] in _TOPOLOGY_OWNER_COLLECTIONS
    ):
        return segments[:3]
    if len(segments) >= 2 and segments[0] in _TOP_LEVEL_ID_COLLECTIONS:
        return segments[:2]
    if len(segments) >= 2 and segments[0] == "topology" and segments[1] == "battery":
        return segments[:2]
    # `model/links` is a name-addressed list, same shape as the
    # topology owner collections above -- the owning block is the individual
    # link entry (`model/links/<name>`), not the `links` list itself. Checked
    # BEFORE the general `model/...` fallback below so a link's own name-scoped
    # owner wins over the whole-record catch-all that would otherwise claim it.
    if len(segments) >= 3 and segments[0] == "model" and segments[1] == "links":
        return segments[:3]
    # The model record owns every OTHER fact under it. `model/joints` (and
    # `model/links` before the branch above existed) are LISTS, and neither
    # `Joint` nor `Link` carries a `provenance` field (schema.py, frozen),
    # so the default `segments[:2]` resolved the owner to a list and
    # refused every `model/...` declare outright:
    # `declare model/joints/imu_mount/limits/effort 3.0` exited 2 with "cannot
    # stamp provenance at model/joints". `ModelRecord.provenance` is the block
    # that already covers the structure, so it is the owner.
    if segments[0] == "model":
        return ["model"]
    return segments[: min(2, len(segments))]


# owners with no `provenance` field of their own (schema.py forbids
# extras — a field is only added by a schema change) get a comment stamp instead.
# `Link` and `ActuatorCase` likewise have no provenance field; their changed
# facts receive source/witness comments rather than invalid extra schema keys.
# Entries are matched by PREFIX because the resolved owner includes its name
# or id while the family tuple stays fixed.
_UNSTRUCTURED_PROVENANCE_OWNERS = {
    ("topology", "battery"),
    ("model", "links"),
    ("actuator_cases",),
}


def _is_unstructured_owner(owner_segments: list[str]) -> bool:
    return any(
        tuple(owner_segments[: len(family)]) == family for family in _UNSTRUCTURED_PROVENANCE_OWNERS
    )


def _by(witness: str | None) -> str:
    """", witness NAME", or nothing for an anonymous edit (witness None): no name is ever invented."""
    return f", witness {witness}" if witness else ""


def _att(witness: str | None, source: str) -> str:
    return f"witness {witness}, source {source}" if witness else f"source {source}"


def _witnessless(node: Any) -> bool:
    """True when any provenance block in the document names no witness (a 0.7-only shape)."""
    if isinstance(node, dict):
        prov = node.get("provenance")
        if isinstance(prov, dict) and not prov.get("witness"):
            return True
        return any(_witnessless(v) for v in node.values())
    return isinstance(node, list) and any(_witnessless(v) for v in node)


def _bump_anonymous_model(doc: Any, lines: list[str]) -> None:
    """A model with no provenance, or any provenance with no witness, is 0.7-only: move an older file to 0.7 and say so."""
    model = doc.get("model") if isinstance(doc, dict) else None
    declared = doc.get("schema_version") if isinstance(doc, dict) else None
    if declared is None:
        return
    if not (isinstance(model, dict) and "provenance" not in model) and not _witnessless(doc):
        return
    if str(declared) == SCHEMA_VERSION or str(declared) not in SUPPORTED_SCHEMA_VERSIONS:
        return
    doc["schema_version"] = type(declared)(SCHEMA_VERSION) if isinstance(declared, str) else float(SCHEMA_VERSION)
    lines.append(f"format moved to {SCHEMA_VERSION}: this file now records an anonymous model")


def _bump_mesh_scale(doc: Any, lines: list[str]) -> None:
    """A mesh scale is a 0.7-only field: move an older file to 0.7 and say so."""
    model = doc.get("model") if isinstance(doc, dict) else None
    declared = doc.get("schema_version") if isinstance(doc, dict) else None
    if not isinstance(model, dict) or declared is None:
        return
    shapes = [
        s
        for link in model.get("links") or ()
        for s in (*(link.get("geometry") or ()), *(link.get("collision") or ()))
    ]
    if not any(isinstance(s, dict) and "scale" in s for s in shapes):
        return
    if str(declared) == SCHEMA_VERSION or str(declared) not in SUPPORTED_SCHEMA_VERSIONS:
        return
    doc["schema_version"] = type(declared)(SCHEMA_VERSION) if isinstance(declared, str) else float(SCHEMA_VERSION)
    lines.append(f"format moved to {SCHEMA_VERSION}: this file now records a mesh scale")


def _bump_holes(doc: Any, lines: list[str]) -> None:
    """Holes are a 0.7-only field: move an older file that uses them to 0.7 and say so."""
    model = doc.get("model") if isinstance(doc, dict) else None
    declared = doc.get("schema_version") if isinstance(doc, dict) else None
    if not isinstance(model, dict) or declared is None:
        return
    shapes = [
        s
        for link in model.get("links") or ()
        for s in (*(link.get("geometry") or ()), *(link.get("collision") or ()))
    ]
    if not any(isinstance(s, dict) and s.get("holes") is not None for s in shapes):
        return
    if str(declared) == SCHEMA_VERSION or str(declared) not in SUPPORTED_SCHEMA_VERSIONS:
        return
    doc["schema_version"] = type(declared)(SCHEMA_VERSION) if isinstance(declared, str) else float(SCHEMA_VERSION)
    lines.append(f"format moved to {SCHEMA_VERSION}: this file now records holes")


def _bump_groups(doc: Any, lines: list[str]) -> None:
    """Groups are 0.7-only: move an older file that uses them to 0.7 and say so."""
    declared = doc.get("schema_version") if isinstance(doc, dict) else None
    if not doc.get("groups") or declared is None:
        return
    if str(declared) == SCHEMA_VERSION or str(declared) not in SUPPORTED_SCHEMA_VERSIONS:
        return
    doc["schema_version"] = type(declared)(SCHEMA_VERSION) if isinstance(declared, str) else float(SCHEMA_VERSION)
    lines.append(f"format moved to {SCHEMA_VERSION}: this file now records groups")


def _mesh_scale(raw: Any) -> float:
    unit = {"mm": 0.001, "m": 1.0}
    text = str(raw).strip().lower() if raw is not None and not isinstance(raw, bool) else ""
    try:
        value = unit[text] if text in unit else float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise EditRefusal(
            "A mesh needs its scale: say mm, m or a number such as 0.001. "
            "Lockwire does not guess the unit."
        )
    return value


def _mesh_link_fields(contract_path: Path, fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Turn `mesh` + `scale` fields into a link's mesh geometry, plus the result lines."""
    scale = _mesh_scale(fields.get("scale"))
    mesh_file = Path(fields["mesh"])
    rel = relative_source(contract_path, mesh_file)
    if source_is_absolute(rel) or rel.split(os.sep)[0] == "..":
        raise EditRefusal("The mesh file must sit inside the lockfile's folder.")
    try:
        count, lo, hi = meshread.read_mesh(mesh_file)
    except meshread.MeshRefusal as error:
        raise EditRefusal(str(error)) from None
    size = " x ".join(f"{(h - lo_) * scale:.3f}" for lo_, h in zip(lo, hi, strict=True))
    shape = {"kind": "mesh", "mesh": rel.replace(os.sep, "/"), "scale": scale}
    lines = [f"imported {mesh_file.name}: {size} m ({count} triangles)"]
    if not fields.get("mass"):
        lines.append(
            "mass not derived for a mesh; declare one with: lockwire declare <lockfile> "
            f"model/links/{fields.get('name')}/mass \"<value> kg\" --source <where it comes from>"
        )
    return {**fields, "geometry": [shape]}, lines


def _stamp_provenance(
    doc: Any,
    owner_segments: list[str],
    *,
    field_leaf: str,
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> None:
    """Stamp the owning block's provenance without erasing another fact's.

    A device/rail/bus has ONE `provenance` block for every fact it carries
    (mass, radio numbers, mount point, ...) — the honest fix is provenance
    per fact, a schema change. Until then:
    a fresh provenance is written plainly; an EXISTING one keeps its
    witness/source (the facts it already covers) and gets the new fact
    appended to `source` and `note` instead of overwritten.
    """
    owner = _navigate(doc, owner_segments, create=False)
    if not isinstance(owner, dict):
        raise EditRefusal(f"cannot stamp provenance at {'/'.join(owner_segments)}")
    if _is_unstructured_owner(owner_segments):
        if owner_segments[0] == "actuator_cases" and doc.fa.flow_style():
            # A JSON-shaped contract cannot serialize a nested YAML comment
            # while its root/list stay flow-styled. The proposal must expose
            # the authored source and witness in its reviewable diff.
            doc.fa.set_block_style()
            doc["actuator_cases"].fa.set_block_style()
            owner.fa.set_block_style()
        _stamp_comment_provenance(
            owner,
            field_leaf=field_leaf,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
        )
        return
    prov = owner.get("provenance")
    if witness is None:
        # Anonymous: the value changed, so the old name must not vouch for it. Record the
        # edit's source (no name), or drop the block when the edit gave no source.
        _stamp_add_provenance(owner, source=source, witness=None, locked_at=locked_at, note=note)
        return
    stamp = f"{locked_at.isoformat()}: {field_leaf} declared{_by(witness)}"
    if prov is None:
        prov = CommentedMap()
        owner["provenance"] = prov
        prov["witness"] = witness
        prov["source"] = source
        prov["note"] = stamp
    else:
        if not prov.get("witness"):
            prov["witness"] = witness
        old_source = prov.get("source")
        appended = f"{field_leaf} from {source} ({locked_at.isoformat()})"
        prov["source"] = f"{old_source}; {appended}" if old_source else source
        old_note = prov.get("note")
        prov["note"] = f"{old_note}\n{stamp}" if old_note else stamp
    prov["locked_at"] = locked_at
    if note:
        prov["note"] = f"{prov['note']}\n{locked_at.isoformat()}: {note}"


def _stamp_comment_provenance(
    owner: Any,
    *,
    field_leaf: str,
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> None:
    """Record a fact's source/witness as a YAML comment when the owner has no
    `provenance` field of its own (`_UNSTRUCTURED_PROVENANCE_OWNERS`).

    Stamped directly above `field_leaf`'s own key inside `owner` (the block
    that was just navigated to and already carries the newly-written field) --
    works whether `owner` is a dict-shaped singleton reached by a fixed key
    (`topology/battery`) or a name-addressed list entry reached by its own
    `name` (`model/links/x500_frame`); both are plain `CommentedMap`s once
    resolved, so the same call handles either.

    `yaml_set_comment_before_after_key` is additive, not a replace — each
    call adds one more comment line above the key, so successive stamps
    accumulate the same way a structured provenance's `source`/`note` do.
    Unstructured and best-effort: nothing here is read back by `check` or
    `describe`.
    """
    stamp = f"{locked_at.isoformat()}: {field_leaf} declared{_by(witness)}, source {source}"
    if note:
        stamp += f"; {note}"
    owner.yaml_set_comment_before_after_key(field_leaf, before=stamp, indent=2)


# ---------------------------------------------------------------- change model


# ---------------------------------------------------------------- the standing mark


def _budget_limit_changes(changes: list["Change"]) -> dict[str, "Change"]:
    """The changes that declare a `budgets/<id>/limit`, keyed by budget id."""
    out: dict[str, Change] = {}
    for change in changes:
        segments = split_path(change.path)
        if len(segments) == 3 and segments[0] == "budgets" and segments[2] == "limit":
            out[segments[1]] = change
    return out


def _cleared_by_raising(before: Any, after: Any) -> set[str]:
    """Budget ids whose limit GREW and whose violation went away with it.

    The two halves are both load-bearing: a limit that grew but left the finding
    standing is not a cleared violation, and a violation that went away while the
    limit fell was cleared by shedding real draw, which needs no mark. Violations
    are matched by their full contract path, so the raised-limit case that trades
    an overload for a *different* finding (draw > limit becomes limit > rail
    capacity) is still a cleared overload and is still marked.
    """
    from lockwire.checks import check_budgets  # local: checks.py imports report/scene

    old_limits = {budget.id: budget.limit.base() for budget in before.budgets}
    raised = {
        budget.id
        for budget in after.budgets
        if budget.id in old_limits and budget.limit.base() > old_limits[budget.id]
    }
    if not raised:
        return set()
    gone = {
        finding.quantity for finding in check_budgets(before) if finding.severity == "violation"
    } - {finding.quantity for finding in check_budgets(after) if finding.severity == "violation"}
    return {
        budget_id
        for budget_id in raised
        if any(
            quantity == f"budgets/{budget_id}" or quantity.startswith(f"budgets/{budget_id}/")
            for quantity in gone
        )
    }


def _stamp_limit_raised(doc: Any, budget_id: str, *, witness: str | None, locked_at: dt.date) -> bool:
    """Append the standing mark to the budget's provenance note. No new field:
    `Provenance.note` is a schema field and the mark is text in it."""
    for entry in doc.get("budgets") or []:
        if not isinstance(entry, dict) or entry.get("id") != budget_id:
            continue
        prov = entry.get("provenance")
        if not isinstance(prov, dict):
            return False
        mark = f"{LIMIT_RAISED_MARK}{locked_at.isoformat()}{_by(witness)}"
        old_note = prov.get("note")
        prov["note"] = f"{old_note}\n{mark}" if old_note else mark
        return True
    return False


@dataclass
class Change:
    path: str
    value: Any  # raw VALUE string, or an already-structured python value
    source: str
    witness: str | None  # None: anonymous, nothing is stamped (the free app, serve)
    note: str | None = None
    locked_at: dt.date | None = None
    unit: str | None = None  # sibling `unit:` key (lockwire scene import's change-file shape)


@dataclass
class EditResult:
    diff: str
    lines: list[str] = field(default_factory=list)
    written: bool = False


@dataclass
class EditProposal:
    """A validated edit that has not touched the filesystem."""

    result: EditResult
    contract: Contract
    text: str
    original_text: str


def _dump(doc: Any, style: tuple[int, int] = _DEFAULT_SEQUENCE_STYLE) -> str:
    buf = io.StringIO()
    _yaml(*style).dump(doc, buf)
    return buf.getvalue()


def _load_doc_guarded(contract_path: Path) -> tuple[str, Any, tuple[int, int]]:
    _guard_contract_raw(contract_path)
    return _load_doc_bytes_guarded(contract_path.read_bytes())


def _load_doc_bytes_guarded(raw_bytes: bytes) -> tuple[str, Any, tuple[int, int]]:
    size = len(raw_bytes)
    if size > MAX_CONTRACT_BYTES:
        raise ValueError(f"contract file exceeds size limit ({size} bytes > {MAX_CONTRACT_BYTES})")
    text = raw_bytes.decode("utf-8")
    lf_text = text.replace("\r\n", "\n")  # CRLF file: parse/sniff/diff as LF, write back as CRLF
    style = _sniff_sequence_style(lf_text)
    doc = _load_yaml_guarded(_yaml(*style), io.StringIO(lf_text))
    _reject_yaml_anchors_aliases(doc)
    return text, doc, style


def _loc_to_path(doc: Any, loc: tuple[Any, ...]) -> str:
    """Best-effort readable path for a pydantic error location (index -> id)."""
    node = doc
    parts: list[str] = []
    for seg in loc:
        parts.append(str(seg))
        if isinstance(node, dict):
            node = node.get(seg)
        elif isinstance(node, list) and isinstance(seg, int) and 0 <= seg < len(node):
            item = node[seg]
            if isinstance(item, dict) and "id" in item:
                parts[-1] = str(item["id"])
            node = item
        else:
            node = None
    return "/".join(parts)


def _apply_collection_append(doc: Any, index: int, change: Change, collection_key: str) -> str:
    """`apply` entry whose path IS a top-level collection: append each row.

    `apply` could stamp a fact inside a
    device/rail/bus, but a whole new `budgets`/`families`/`device_models` row
    has no owning block yet to stamp provenance onto — `declare`'s generic
    "set a leaf, stamp the parent's provenance" model does not fit "create the
    parent". Each row gets its own provenance (kept as authored if the row
    already carries one, e.g. from a hand-written change file; stamped from
    this entry's --source/--witness otherwise).
    """
    rows = change.value
    if not isinstance(rows, list) or not rows:
        raise EditRefusal(f"entry {index} ({change.path}): expected a non-empty list of entries")
    collection = _ensure_list(doc, [collection_key])
    locked_at = change.locked_at or dt.date.today()
    added: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            raise EditRefusal(f"entry {index} ({change.path}): every row needs an id")
        _refuse_duplicate(collection, row["id"], collection_key)
        entry = CommentedMap(row) if not isinstance(row, CommentedMap) else row
        if not entry.get("provenance"):
            _stamp_add_provenance(
                entry, source=change.source, witness=change.witness, locked_at=locked_at, note=change.note
            )
        collection.append(entry)
        added.append(str(entry["id"]))
    return f"added {collection_key} {', '.join(added)} ({_att(change.witness, change.source)})"


_ASSISTANT_NAME_DENYLIST = frozenset(
    {"claude", "gpt", "chatgpt", "codex", "copilot", "gemini", "assistant", "ai"}
)


def _fdopen_text(fd: int, original_text: str) -> Any:
    """Text handle that writes LF text back in the original file's own newline style."""
    return os.fdopen(fd, "w", encoding="utf-8", newline="\r\n" if "\r\n" in original_text else "\n")


def _backup_temp(path: Path, contents: bytes) -> Path:
    """Stage backup bytes beside their destination without changing it."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _named(error: Exception, tmp_path: Path, contract_path: Path) -> str:
    """A refusal about the staged temp file names the user's file instead."""
    return str(error).replace(tmp_path.name, contract_path.name)


def _replace(tmp_path: Path, contract_path: Path, *, journal: bool = True) -> None:
    """Install a written file atomically with the file's own permissions.

    Nothing is left beside it (no `.bak`): inside git, git is the undo; outside it
    there is none. A failed replace leaves the old bytes untouched.
    """
    shutil.copymode(contract_path, tmp_path)
    before = contract_path.read_bytes()
    after = tmp_path.read_bytes()
    os.replace(tmp_path, contract_path)
    if journal and not _journal_off.get():
        _journal_record(contract_path, before, after)


_UNDO_LIMIT = 50


def _journal_path(contract_path: Path) -> Path:
    """The step journal: one hidden JSON file beside the lockfile, never inside it."""
    return contract_path.with_name(f".{contract_path.name}.lockwire-undo.json")


def _journal_load(contract_path: Path) -> dict[str, list[dict[str, str]]]:
    try:
        data = json.loads(_journal_path(contract_path).read_text(encoding="utf-8"))
        return {"undo": list(data["undo"]), "redo": list(data["redo"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"undo": [], "redo": []}


def _journal_save(contract_path: Path, journal: dict[str, list[dict[str, str]]]) -> None:
    path = _journal_path(contract_path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(journal), encoding="utf-8")
    os.replace(tmp, path)


def _step(data: bytes, expect: bytes) -> dict[str, str]:
    """`data` is what to restore; `expect` is the file that must be on disk to do it."""
    return {
        "bytes": base64.b64encode(data).decode("ascii"),
        "expect": hashlib.sha256(expect).hexdigest(),
    }


def _journal_record(contract_path: Path, before: bytes, after: bytes) -> None:
    """Remember the bytes this write replaced; a new edit clears the redo stack."""
    if before == after:
        return
    journal = _journal_load(contract_path)
    journal["undo"] = [*journal["undo"], _step(before, after)][-_UNDO_LIMIT:]
    journal["redo"] = []
    try:
        _journal_save(contract_path, journal)
    except OSError:
        pass  # a failed journal only loses the undo step; the edit itself stands


def _journal_move(contract_path: Path, source: str, target: str) -> None:
    """Pop one step off `source`, restore its bytes, push the inverse onto `target`."""
    resolved = contract_path.resolve()
    with _contract_lock(resolved):
        journal = _journal_load(resolved)
        if not journal[source]:
            raise EditRefusal(f"nothing to {'undo' if source == 'undo' else 'redo'}")
        step = journal[source][-1]
        current = resolved.read_bytes()
        if hashlib.sha256(current).hexdigest() != step["expect"]:
            raise EditConflict(
                f"{resolved.name} changed outside Lockwire since the last step; "
                f"not overwriting it, so there is nothing to {source}"
            )
        data = base64.b64decode(step["bytes"])
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{resolved.name}.", suffix=".tmp", dir=str(resolved.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            shutil.copymode(resolved, tmp_path)
            os.replace(tmp_path, resolved)
        finally:
            tmp_path.unlink(missing_ok=True)
        journal[source].pop()
        journal[target].append(_step(current, data))
        _journal_save(resolved, journal)


def undo_edit(contract_path: Path) -> None:
    """Put back the bytes before the last Lockwire write; refuses if the file moved since."""
    _journal_move(contract_path, "undo", "redo")


def redo_edit(contract_path: Path) -> None:
    """Re-apply the step the last undo took back."""
    _journal_move(contract_path, "redo", "undo")


def _is_valid_witness_name(witness: str) -> bool:
    """True for a name ``validate_witness`` would accept: non-blank, not "you",
    not an assistant name. Shared so a config default gets the identical bar a
    hand-typed ``--witness`` does — a bad default must fail open into
    the existing refusal, never bypass it under the "(default)" mark.
    """
    if not witness or not witness.strip():
        return False
    return witness.strip().lower() not in _ASSISTANT_NAME_DENYLIST | {"you"}


def validate_witness(witness: str, *, refusal: str) -> None:
    """Refuse a witness that is blank, the bare word "you", or an assistant name.

    Shared by both write paths (``_apply_change`` and ``add_entry``) so the
    refusal list lives in one place. Does not pick a default witness itself —
    that is ``resolve_witness``'s job; this still refuses a blank/"you"
    string whether it arrived as-is or as an unmarked default.
    """
    if not _is_valid_witness_name(witness):
        raise EditRefusal(refusal)


_WITNESS_CONFIG_ENV = "LOCKWIRE_WITNESS_CONFIG"
_WITNESS_DEFAULT_MARK = " (default)"
# Control characters, line/paragraph separators, lone surrogates: never in a saved name.
_UNSAFE_NAME_CATEGORIES = {"Cc", "Cs", "Zl", "Zp"}


def default_witness_config_path() -> Path:
    """Where the per-machine witness default lives.

    ``LOCKWIRE_WITNESS_CONFIG`` overrides the path outright — tests use this so
    a run never reads or depends on a real file of the user's (a test never
    touches a real file by default). Otherwise the app
    and CLI share one XDG-style location.
    """
    override = os.environ.get(_WITNESS_CONFIG_ENV)
    if override:
        return Path(override)
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "lockwire" / "config.toml"


def read_default_witness() -> str | None:
    """The per-machine config's ``witness`` value, or ``None`` if unset.

    Missing file, missing key, a blank value, or a file that fails to parse
    all read the same way: no default, so the caller falls back to the
    existing refusal. A default mechanism failing open into "no default" can
    never itself cause a bad write; it can only cause the same refusal that
    already existed before the default.
    """
    path = default_witness_config_path()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    value = data.get("witness")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def write_default_witness(name: str) -> str:
    """Save the per-machine default witness (other config keys are kept); refuses a bad name.

    Every refusal happens before the file is touched. A name with a control
    character, line or paragraph separator, or lone surrogate is refused: TOML
    can't hold most of them raw, and a line break inside the value would split
    the line on the next rewrite. The new file is checked to read back the exact
    name before it replaces the old one.
    """
    stripped = name.strip()
    validate_witness(stripped, refusal=f"Use your real name, not {name!r}.")
    if any(unicodedata.category(c) in _UNSAFE_NAME_CATEGORIES for c in stripped):
        raise EditRefusal("A name can't contain line breaks or control characters.")
    path = default_witness_config_path()
    quoted = '"' + stripped.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        # Split only on "\n" (read_text turns "\r\n" into it), so no character inside a value splits a line.
        old = path.read_text(encoding="utf-8").removesuffix("\n") if path.is_file() else ""
        lines = old.split("\n") if old else []
        head = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
        kept = [ln for ln in lines[:head] if not re.match(r"\s*witness\s*=", ln)] + lines[head:]
        text = "\n".join([f"witness = {quoted}", *kept]) + "\n"
        reads_back = tomllib.loads(text).get("witness") == stripped
    except ValueError:  # not UTF-8, or not TOML
        reads_back = False
    if not reads_back:
        raise EditRefusal(f"Fix or delete the settings file {path}, then save your name again.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return stripped


def resolve_witness(witness: str | None) -> str:
    """An explicit ``--witness`` wins; an absent/blank one falls back to the
    per-machine config default, marked "(default)".

    The mark is written straight into the value that ends up stored — the
    same string lands in the file's ``witness:`` field, in ``describe``'s
    per-device page, and (once merged) in ``blame``'s row for this record —
    so a defaulted witness can never render identically to a hand-entered
    one on any surface that just echoes ``provenance.witness``. Does not
    itself refuse: a resolved value that is still blank, or a config value
    that is itself blank/"you"/an assistant name, reaches ``validate_witness``
    unmarked and is refused exactly as before.

    Only OMISSION (``witness is None``) taking the default. An
    explicit blank — ``--witness ""`` or all-whitespace — is a different act:
    the caller deliberately passed nothing, so silently attributing a
    default witness to them would be wrong even though the string looks the
    same as an omission once ``.strip``-ed. That case is refused here,
    before the default is ever consulted, with the same wording
    used for a hand-typed blank/"you"/assistant name.
    """
    if witness is not None and not witness.strip():
        raise EditRefusal(f"--witness must name a real person, not {witness!r}")
    if witness and witness.strip():
        return witness
    default = read_default_witness()
    if default and _is_valid_witness_name(default):
        return f"{default}{_WITNESS_DEFAULT_MARK}"
    return witness or ""


_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")


def source_is_absolute(source: str) -> bool:
    """True when ``source`` names a location only one machine has.

    Lexical, no filesystem touched: a POSIX absolute path, a ``~`` home path, a
    backslash-rooted or UNC Windows path, or a drive path (``C:``, ``C:/``,
    ``C:\\``). A URL is *not* absolute in this sense — it travels with the
    contract — and the scheme length is what separates the two: ``https:`` is a
    URL, ``C:`` is a drive letter.
    """
    text = source.strip()
    if text.startswith(("/", "~", "\\")):
        return True
    return len(text) >= 2 and text[0].isalpha() and text[1] == ":" and text[2:3] in ("", "/", "\\")


def validate_source(source: str, *, prefix: str = "") -> None:
    """Refuse a blank source, and refuse an absolute path.

    Shared by every write door — ``_apply_change`` (for ``--source`` and, via
    ``iter_written_sources``, for every source the change's *value* lands),
    ``add_entry`` (which every ``_build_*`` and ``_add_battery_entry`` runs
    through) and ``scene_import.write_change_file`` — so the rule lives in one
    place, the way ``validate_witness`` does. A source that survives this is relative, and a
    relative source resolves from the contract's own directory
    (``resolve_source``), so the evidence travels with the contract.
    """
    if not source or not source.strip():
        raise EditRefusal(f"{prefix}--source is required")
    if source_is_absolute(source):
        raise EditRefusal(
            f"{prefix}--source must be a path relative to the contract, a URL or a cited "
            f"title, not the absolute path {source.strip()!r} — an absolute path names a "
            f"file only this machine has, so the evidence cannot travel with the "
            f"contract"
        )


def iter_written_sources(path: str, value: Any) -> Iterator[tuple[str, str]]:
    """Every source string a change would land, with the path it lands at.

    ``--source`` is the witness's citation for the edit; this is the other half —
    the value side. ``declare devices/x/provenance/source /Users/me/ds.pdf`` and an
    appended row carrying its own ``provenance.source`` both write a source field
    without ever touching ``--source``, so both are walked here and validated by the
    same guard.
    """
    if isinstance(value, str):
        if path.rsplit("/", 1)[-1] == "source":
            yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_written_sources(f"{path}/{key}", item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_written_sources(path, item)


def relative_source(contract_path: Path, file_path: Path) -> str:
    """The portable spelling of a file a write path was handed as a path.

    ``lockwire start /home/me/robot.urdf`` must not stamp ``/home/me`` into the
    contract — nobody else has that directory. The path is re-read from the
    contract's own directory instead, so the model file travels with the
    contract. Falls back to the bare filename when no relative spelling exists
    (a different Windows drive).
    """
    try:
        return os.path.relpath(file_path.resolve(), contract_path.resolve().parent)
    except ValueError:
        return file_path.name


def resolve_source(contract_path: Path, source: str) -> Path | None:
    """Where a relative file source points, read from the contract's own directory.

    A source is a file beside the contract, a URL or a cited title. Only
    the first kind resolves — a URL, a cited title and (refused on write, but
    still found in older contracts) an absolute path return ``None``. The
    contract's directory is the anchor, never the working directory, so the same
    contract resolves the same way from anywhere.
    """
    text = source.strip()
    if not text or source_is_absolute(text) or _URL_SCHEME.match(text) or " " in text:
        return None
    return contract_path.resolve().parent / text


def _apply_change(doc: Any, index: int, change: Change) -> str:
    if change.witness is not None or change.source:  # an anonymous edit may give no source
        validate_source(change.source, prefix=f"entry {index} ({change.path}): ")
    for where, written in iter_written_sources(change.path, change.value):
        validate_source(written, prefix=f"entry {index} ({where}): ")
    if change.witness is not None:
        validate_witness(
            change.witness,
            refusal=f"entry {index} ({change.path}): --witness must name a real person, not {change.witness!r}",
        )
    segments = split_path(change.path)
    if len(segments) == 1 and segments[0] in _COLLECTION_APPEND_KINDS:
        return _apply_collection_append(doc, index, change, segments[0])
    try:
        parent = _navigate(doc, segments[:-1], create=True)
    except EditRefusal as error:
        raise EditRefusal(f"entry {index} ({change.path}): {error}") from error
    if not isinstance(parent, dict):
        raise EditRefusal(f"entry {index} ({change.path}): not a container")
    key = segments[-1]
    try:
        value = (
            parse_value(change.value, field_name=key)
            if isinstance(change.value, str)
            else change.value
        )
        if value is None:
            raise EditRefusal("no value given; declare a value like '20.6 kg'")
        if change.unit is not None:
            value = _attach_unit(value, change.unit)
    except EditRefusal as error:
        raise EditRefusal(f"entry {index} ({change.path}): {error}") from error
    parent[key] = value
    locked_at = change.locked_at or dt.date.today()
    owner_segments = provenance_owner_path(segments)
    try:
        _stamp_provenance(
            doc,
            owner_segments,
            field_leaf=key,
            source=change.source,
            witness=change.witness,
            note=change.note,
            locked_at=locked_at,
        )
    except EditRefusal as error:
        raise EditRefusal(f"entry {index} ({change.path}): {error}") from error
    return f"declared {change.path} = {format_value(value)} ({_att(change.witness, change.source)})"


def propose_changes_from_bytes(
    contract_path: Path, raw_bytes: bytes, changes: list[Change]
) -> EditProposal:
    """Build a proposal from caller-bound raw bytes without reading the path."""
    if not changes:
        raise EditRefusal("no changes given")
    original_text, doc, style = _load_doc_bytes_guarded(raw_bytes)

    # a declare that clears a violation by RAISING the limit rather
    # than lowering the draw leaves a standing mark. Reading the contract as it
    # stands is the only way to know which side moved; a file that does not
    # validate today has no violations to clear, so no mark can be owed.
    limit_changes = _budget_limit_changes(changes)
    before: Any | None = None
    if limit_changes:
        try:
            declared = doc.get("schema_version", SCHEMA_VERSION) if isinstance(doc, dict) else None
            if declared not in SUPPORTED_SCHEMA_VERSIONS:
                raise UnsupportedSchemaVersion(declared, SUPPORTED_SCHEMA_VERSIONS)
            before = Contract.model_validate(doc)
        except Exception:  # noqa: BLE001 — an unloadable "before" owes no mark
            before = None

    lines: list[str] = []
    for index, change in enumerate(changes, start=1):
        lines.append(_apply_change(doc, index, change))
    _bump_anonymous_model(doc, lines)

    new_text = _dump(doc, style)
    # The file already declared an unsupported ``schema_version`` before this
    # edit touched it, so the edit is not the cause: let the typed refusal
    # through as itself rather than letting the correlator blame an entry.
    try:
        declared = doc.get("schema_version", SCHEMA_VERSION) if isinstance(doc, dict) else None
        if declared not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(declared, SUPPORTED_SCHEMA_VERSIONS)
        after = Contract.model_validate(doc)
    except UnsupportedSchemaVersion:
        raise
    except Exception as error:
        culprit = _correlate_failure(doc, changes, error)
        raise EditRefusal(culprit) from error

    if before is not None:
        marked = False
        for budget_id in sorted(_cleared_by_raising(before, after)):
            change = limit_changes[budget_id]
            if _stamp_limit_raised(
                doc,
                budget_id,
                witness=change.witness,
                locked_at=change.locked_at or dt.date.today(),
            ):
                marked = True
                lines.append(f"marked budgets/{budget_id}: the limit was raised past a violation")
        if marked:
            new_text = _dump(doc, style)
            after = Contract.model_validate(doc)

    diff = "".join(
        difflib.unified_diff(
            original_text.replace("\r\n", "\n").splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(contract_path),
            tofile=str(contract_path),
        )
    )
    return EditProposal(EditResult(diff=diff, lines=lines), after, new_text, original_text)


def propose_changes(contract_path: Path, changes: list[Change]) -> EditProposal:
    """Build and validate an edit proposal without creating any files."""
    _guard_contract_raw(contract_path)
    return propose_changes_from_bytes(contract_path, contract_path.read_bytes(), changes)


@_locked_write
def apply_changes(
    contract_path: Path,
    changes: list[Change],
    *,
    dry_run: bool = False,
    expected_revision: str | None = None,
    journal: bool = True,
) -> EditResult:
    """Apply every change to the contract, validate once, write atomically.

    One call is one undo step however many changes it holds (the batch is one write).
    ``journal=False`` writes no undo journal: for a copy the user never sees.

    Nothing is written when any entry is malformed or the resulting document
    fails to validate — the raised ``EditRefusal`` names the failing entry.
    """
    proposal = propose_changes(contract_path, changes)
    if dry_run:
        return proposal.result
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, proposal.original_text) as handle:
            handle.write(proposal.text)
        _replace(tmp_path, contract_path, journal=journal)
        proposal.result.written = True
        return proposal.result
    finally:
        tmp_path.unlink(missing_ok=True)


@_locked_write
def apply_batch(
    contract_path: Path,
    steps: list[tuple[str, Callable[[Path], EditResult]]],
    *,
    dry_run: bool = False,
    expected_revision: str | None = None,
    journal: bool = True,
) -> EditResult:
    """Run mixed edits (declare/add/place/move...) as ONE write and ONE undo step.

    Each step runs on a staged copy beside the lockfile; the real file is replaced
    once, with one journal entry, only if every step succeeded. A refusal names the
    failing item (1-based) and leaves the real file and its journal untouched.
    """
    if not steps:
        raise EditRefusal("no changes given")
    original = contract_path.read_bytes()
    stage = contract_path.with_name(f".{contract_path.name}.batch-{os.getpid()}.stage")
    tmp_path = contract_path.with_name(f"{stage.name}.tmp")
    lines: list[str] = []
    token = _journal_off.set(True)
    try:
        stage.write_bytes(original)
        shutil.copymode(contract_path, stage)
        for index, (label, run) in enumerate(steps, start=1):
            try:
                lines.extend(run(stage).lines)
            except EditRefusal as error:
                raise EditRefusal(f"item {index} ({label}): {error}") from error
        final = stage.read_bytes()
        diff = "".join(
            difflib.unified_diff(
                original.decode().replace("\r\n", "\n").splitlines(keepends=True),
                final.decode().replace("\r\n", "\n").splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        if dry_run:
            return EditResult(diff=diff, lines=lines, written=False)
        _journal_off.reset(token)
        token = None
        tmp_path.write_bytes(final)
        _replace(tmp_path, contract_path, journal=journal)
        return EditResult(diff=diff, lines=lines, written=True)
    finally:
        if token is not None:
            _journal_off.reset(token)
        for leftover in (stage, tmp_path, _journal_path(stage)):
            leftover.unlink(missing_ok=True)


@_locked_write
def apply_review(
    contract_path: Path,
    path: str,
    state: str,
    *,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Approve or question one already-declared value, appending a review note
    to the owning block's provenance, atomically.

    Same load -> mutate -> validate -> write shape as ``apply_changes`` — one
    writer, not a second one. The witness-blank/assistant-name refusal
    and the "questioned needs a note" refusal are not duplicated here: they
    are ``schema.ReviewNote``'s own validators, reached through the reload
    below exactly the way ``apply_changes`` reaches ``Contract``'s. That is
    the one gate every review door (CLI, MCP, library) shares.
    """
    original_text, doc, style = _load_doc_guarded(contract_path)
    segments = split_path(path)
    owner_segments = provenance_owner_path(segments)
    owner = _navigate(doc, owner_segments, create=False)
    if not isinstance(owner, dict) or not isinstance(owner.get("provenance"), dict):
        raise EditRefusal(f"cannot review {path}: nothing declared there yet (no provenance)")
    prov = owner["provenance"]
    entry = CommentedMap()
    entry["state"] = state
    entry["witness"] = witness
    entry["at"] = locked_at or dt.date.today()
    if note:
        entry["note"] = note
    reviews = prov.get("reviews")
    if reviews is None:
        reviews = []
        prov["reviews"] = reviews
    reviews.append(entry)

    new_text = _dump(doc, style)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, original_text) as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:
            raise
        except Exception as error:
            raise EditRefusal(
                f"review of {path}: {_named(error, tmp_path, contract_path)}"
            ) from error
        diff = "".join(
            difflib.unified_diff(
                original_text.replace("\r\n", "\n").splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        line = f"reviewed {path}: {state} (witness {witness})"
        if dry_run:
            return EditResult(diff=diff, lines=[line], written=False)
        _replace(tmp_path, contract_path)
        return EditResult(diff=diff, lines=[line], written=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _correlate_failure(doc: Any, changes: list[Change], error: Exception) -> str:
    """Turn a validation failure into "entry N (path): message" when possible."""
    errors = getattr(error, "errors", None)
    if callable(errors):
        for pydantic_error in errors():
            loc_path = _loc_to_path(doc, pydantic_error.get("loc", ()))
            for index, change in enumerate(changes, start=1):
                if (
                    loc_path == change.path
                    or loc_path.startswith(change.path + "/")
                    or (change.path.startswith(loc_path + "/") if loc_path else False)
                ):
                    return f"entry {index} ({change.path}): {plain_message(pydantic_error)}"
    lines = (
        [".".join(map(str, e["loc"])) + ": " + plain_message(e) for e in errors()]
        if callable(errors)
        else [str(error)]
    )
    return f"contract invalid after {len(changes)} change(s): " + "; ".join(lines)


# ------------------------------------------------- add device|rail|bus|family|model|budget|gearbox

_ADD_KINDS = (
    "device",
    "rail",
    "bus",
    "family",
    "model",
    "budget",
    "battery",
    "gearbox",
    "wire",
    "belt",
    "connector",
    "protection",
    "link",
    "joint",
    "param",
    "pattern",
    "duplicate",
    "mirror",
    "attach",
    "shape",
    "holes",
    "promote",
    "stock",
    "variant",
    "select",
    "group",
)
_COLLECTION_APPEND_KINDS = ("families", "device_models", "budgets", "accepted")


@_locked_write
def add_entry(
    contract_path: Path,
    kind: str,
    fields: dict[str, Any],
    *,
    source: str,
    witness: str | None,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Create a new device/rail/bus block, with provenance, atomically."""
    if kind not in _ADD_KINDS:
        raise EditRefusal(f"unknown add kind {kind!r}; expected one of {_ADD_KINDS}")
    if witness is not None or source:  # an anonymous edit may give no source
        validate_source(source)
    if witness is not None:
        validate_witness(witness, refusal=f"--witness must name a real person, not {witness!r}")

    original_text, doc, style = _load_doc_guarded(contract_path)
    locked_at = locked_at or dt.date.today()

    if kind == "group":
        name, members = fields.get("name"), fields.get("members")
        if not name or not isinstance(members, (list, tuple)):
            raise EditRefusal("group requires a name and members")
        groups = _ensure_list(doc, ["groups"])
        if any(isinstance(g, dict) and g.get("name") == name for g in groups):
            raise EditRefusal(f"group {name!r} already exists")
        groups.append(CommentedMap(name=name, members=list(members)))
        line = f"added group {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind == "connector":
        entry, device_id = _build_connector(doc, fields, source, witness, note, locked_at)
        return _write_add(
            contract_path,
            doc,
            style,
            original_text,
            f"{device_id}/{entry['id']}",
            kind,
            [f"added connector {entry['id']} on {device_id} ({_att(witness, source)})"],
            dry_run,
        )

    if kind == "protection":  # a fuse, breaker, TVS or reverse-polarity guard
        entry = _build_protection(fields)
        guards = _ensure_list(doc, ["topology", "protection"])
        _refuse_duplicate(guards, entry["id"], kind)
        _stamp_comment_provenance(
            entry, field_leaf="kind", source=source, witness=witness, note=note, locked_at=locked_at
        )
        guards.append(entry)
        line = f"added protection {entry['id']} ({_att(witness, source)})"
        return _write_add(
            contract_path, doc, style, original_text, entry["id"], kind, [line], dry_run
        )

    if kind in ("link", "joint"):
        extra: list[str] = []
        if kind == "link" and fields.get("mesh") is not None:
            fields, extra = _mesh_link_fields(contract_path, fields)
        name = _add_link_or_joint(doc, kind, fields, source, witness, note, locked_at)
        line = f"added {kind} {name} ({_att(witness, source)})"
        return _write_add(
            contract_path, doc, style, original_text, name, kind, [*extra, line], dry_run
        )

    if kind == "shape":  # one more primitive on an existing link (the twin of delete shape)
        name, shape = fields.get("link"), fields.get("shape")
        model = _lockfile_model(doc, create=False)
        row = next((x for x in model.get("links") or [] if x.get("name") == name), None)
        if row is None:
            raise EditRefusal(f"There is no link {name!r} to add a shape to.")
        if not isinstance(shape, dict):
            raise EditRefusal("shape requires a shape: {kind: box|cylinder|sphere|mesh, ...}")
        row["geometry"] = [*(row.get("geometry") or []), CommentedMap(shape)]
        _rehash(model, source, witness, locked_at)
        line = f"added a {shape.get('kind')} shape to link {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind == "holes":  # a bolt pattern on one shape's face (z = 0 there)
        name, entries = _build_holes(doc, fields)
        row = next(x for x in _lockfile_model(doc, create=False)["links"] if x["name"] == name)
        shape = row["geometry"][int(fields.get("shape_index") or 0)]
        shape["holes"] = [*(shape.get("holes") or []), *entries]
        _rehash(_lockfile_model(doc, create=False), source, witness, locked_at)
        line = (
            f"added {len(entries)} holes to link {name} ({_att(witness, source)}); "
            "holes do not reduce the derived mass"
        )
        lines = [line]
        if fields.get("fastener") is not None:
            from lockwire.schema import Hole

            h = Hole(xyz=(0, 0, 0), fastener=fields["fastener"], fit=fields["fit"])
            size = (
                f"{h.resolved_diameter_m * 1000:g} mm ({h.resolved_fit_class}, {h.resolved_source})"
                if h.resolved_diameter_m is not None
                else h.diameter_note
            )
            lines.append(f"{len(entries)} {h.fastener} {h.fit} holes: {size}")
        return _write_add(contract_path, doc, style, original_text, name, kind, lines, dry_run)

    if kind == "attach":
        name = _attach_part(doc, fields, source, witness, note, locked_at)
        line = f"attached {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind == "promote":
        name, said = _promote_device(contract_path, doc, fields, source, witness, note, locked_at)
        line = f"promoted {fields['device']} to link {name} ({_att(witness, source)})"
        return _write_add(
            contract_path, doc, style, original_text, name, kind, [*said, line], dry_run
        )

    if kind == "stock":
        name = _attach_part(
            doc, fields, source, witness, note, locked_at, stock=_stock_link(doc, fields)
        )
        line = f"added stock {name} (witness {witness}, source {source})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind in ("pattern", "duplicate"):
        lines = _add_pattern_or_duplicate(doc, kind, fields, source, witness, note, locked_at)
        return _write_add(
            contract_path, doc, style, original_text, fields.get("of", ""), kind, lines, dry_run
        )

    if kind == "mirror":
        lines = _mirror_branch(doc, fields, source, witness, note, locked_at)
        return _write_add(
            contract_path, doc, style, original_text, fields.get("of", ""), kind, lines, dry_run
        )

    if kind == "param":
        name = fields.get("name")
        if not name:
            raise EditRefusal("param requires --name")
        params = _ensure_list(doc, ["params"])
        if any(isinstance(p, dict) and p.get("name") == name for p in params):
            raise EditRefusal(f"param {name!r} already exists")
        entry = CommentedMap(name=name)
        _copy_optional(entry, fields, ("value", "min", "max", "step"))
        if fields.get("unit"):
            entry["unit"] = fields["unit"]
        _stamp_add_provenance(entry, source=source, witness=witness, locked_at=locked_at, note=note)
        params.append(entry)
        line = f"added param {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind == "variant":
        name = fields.get("name")
        if not name:
            raise EditRefusal("variant requires --name")
        variants = _ensure_list(doc, ["variants"])
        if any(isinstance(v, dict) and v.get("name") == name for v in variants):
            raise EditRefusal(f"variant {name!r} already exists")
        entry = CommentedMap(name=name, params=CommentedMap(fields.get("params") or {}))
        if note:
            entry["note"] = note
        variants.append(entry)
        line = f"added variant {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if (
        kind == "select"
    ):  # choose the active variant (declare cannot stamp a top-level scalar)
        name = fields.get("variant")
        if not name:
            raise EditRefusal("select requires --variant")
        doc["active_variant"] = name
        line = f"selected variant {name} ({_att(witness, source)})"
        return _write_add(contract_path, doc, style, original_text, name, kind, [line], dry_run)

    if kind == "battery":
        return _add_battery_entry(
            contract_path,
            doc,
            fields,
            original_text=original_text,
            style=style,
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            dry_run=dry_run,
        )

    new_id = fields.get("id")
    if not new_id:
        raise EditRefusal("--id is required")

    # "one datasheet, one submit" — a device whose --family/--model
    # name records that do not exist yet gets them created alongside it, in
    # this same in-memory doc, so the eventual disk write is one transaction
    # (nothing lands unless the whole thing validates — see the tail below).
    # A device whose family/model already exist behaves exactly as before.
    created_with_device: list[tuple[str, CommentedMap]] = []
    if kind == "device":
        family_ref = fields.get("family")
        model_ref = fields.get("model")
        if model_ref and family_ref:
            existing_model = next(
                (
                    m
                    for m in doc.get("device_models", []) or []
                    if isinstance(m, dict) and m.get("id") == model_ref
                ),
                None,
            )
            if existing_model is not None and existing_model.get("family") != family_ref:
                raise EditRefusal(
                    f"model {model_ref!r} belongs to family {existing_model.get('family')!r}, "
                    f"not {family_ref!r}"
                )
            families = _ensure_list(doc, ["families"])
            if not any(isinstance(f, dict) and f.get("id") == family_ref for f in families):
                family_entry = _build_family(
                    {
                        "id": family_ref,
                        "name": fields.get("name") or _default_family_name(family_ref),
                    }
                )
                families.append(family_entry)
                created_with_device.append(("family", family_entry))
            models = _ensure_list(doc, ["device_models"])
            if not any(isinstance(m, dict) and m.get("id") == model_ref for m in models):
                model_entry = _build_model(
                    {
                        "id": model_ref,
                        "family": family_ref,
                        "input_voltage_min": fields.get("input_voltage_min"),
                        "input_voltage_max": fields.get("input_voltage_max"),
                    },
                    doc,
                )
                models.append(model_entry)
                created_with_device.append(("model", model_entry))
        collection = _ensure_list(doc, ["devices"])
        _refuse_duplicate(collection, new_id, "device")
        entry = _build_device(fields)
    elif kind in ("rail", "bus", "wire", "belt"):
        list_key = {"rail": "rails", "bus": "buses", "wire": "wires", "belt": "belts"}[kind]
        collection = _ensure_list(doc, ["topology", list_key])
        _refuse_duplicate(collection, new_id, kind)
        build = {"rail": _build_rail, "bus": _build_bus, "wire": _build_wire, "belt": _build_belt}
        entry = build[kind](fields)
    elif kind == "family":
        collection = _ensure_list(doc, ["families"])
        _refuse_duplicate(collection, new_id, "family")
        entry = _build_family(fields)
    elif kind == "model":
        collection = _ensure_list(doc, ["device_models"])
        _refuse_duplicate(collection, new_id, "model")
        entry = _build_model(fields, doc)
    elif kind == "gearbox":
        collection = _ensure_list(doc, ["gearboxes"])
        _refuse_duplicate(collection, new_id, "gearbox")
        entry = _build_gearbox(fields, doc)
    else:  # budget
        collection = _ensure_list(doc, ["budgets"])
        _refuse_duplicate(collection, new_id, "budget")
        entry = _build_budget(fields, doc)

    if kind == "device":
        rail_ref = fields.get("power_rail")
        if rail_ref is not None:
            rails = (
                doc.get("topology", {}).get("rails", [])
                if isinstance(doc.get("topology"), dict)
                else []
            )
            if not any(isinstance(r, dict) and r.get("id") == rail_ref for r in rails):
                raise EditRefusal(f"device references unknown rail {rail_ref!r}")
        bus_ref = fields.get("identity_bus_ref")
        if bus_ref is not None:
            buses = (
                doc.get("topology", {}).get("buses", [])
                if isinstance(doc.get("topology"), dict)
                else []
            )
            if not any(isinstance(b, dict) and b.get("id") == bus_ref for b in buses):
                raise EditRefusal(f"device references unknown bus {bus_ref!r}")
        model_ref = fields.get("model")
        models = doc.get("device_models", []) or []
        if not any(isinstance(m, dict) and m.get("id") == model_ref for m in models):
            existing = ", ".join(
                sorted(m["id"] for m in models if isinstance(m, dict) and m.get("id"))
            )
            raise EditRefusal(
                f"model {model_ref!r} is not declared — declare it first: "
                f"lockwire add model {contract_path} --id {model_ref} --family <fam.id> "
                "--source ... --witness ... "
                f"(or use an existing id: {existing or 'none declared yet'})"
            )

    # One shared stamper for every record this call writes ("one
    # witness line" for the three records) — a family/model created *because*
    # this device named them is not itself witnessed a second time; its
    # provenance points at the device that brought it in instead of repeating
    # the name. A family/model that already existed is untouched, unstamped.
    lines: list[str] = []
    for supporting_kind, supporting_entry in created_with_device:
        pointer = f"see devices/{new_id} (added together)"
        _stamp_add_provenance(supporting_entry, source=source, witness=pointer, locked_at=locked_at)
        lines.append(
            f"added {supporting_kind} {supporting_entry['id']} (witness {pointer}, source {source})"
        )
    _stamp_add_provenance(entry, source=source, witness=witness, locked_at=locked_at, note=note)
    collection.append(entry)
    lines.append(f"added {kind} {new_id} ({_att(witness, source)})")

    new_text = _dump(doc, style)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, original_text) as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:  # in the file before the edit; see apply_changes
            raise
        except Exception as error:
            raise EditRefusal(
                f"add {kind} {new_id!r} invalid: {_named(error, tmp_path, contract_path)}"
            ) from error

        diff = "".join(
            difflib.unified_diff(
                original_text.replace("\r\n", "\n").splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        if dry_run:
            return EditResult(diff=diff, lines=lines, written=False)
        _replace(tmp_path, contract_path)
        return EditResult(diff=diff, lines=lines, written=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _build_holes(doc: Any, fields: dict[str, Any]) -> tuple[str, list[CommentedMap]]:
    """The hole rows for `add holes`, or one plain refusal."""
    name, index = fields.get("link"), fields.get("shape_index") or 0
    model = _lockfile_model(doc, create=False)
    row = next((x for x in model.get("links") or [] if x.get("name") == name), None)
    if row is None:
        raise EditRefusal(f"There is no link {name!r} to add holes to.")
    shapes = row.get("geometry") or []
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(shapes):
        raise EditRefusal(f"Link {name!r} has no shape number {index}.")
    if shapes[index].get("kind") == "mesh":
        raise EditRefusal("Holes go on a box or cylinder, not a mesh.")
    diameter, fastener, fit = (fields.get(k) for k in ("diameter_m", "fastener", "fit"))
    if (diameter is None) == (fastener is None) or (diameter is None and fit is None):
        raise EditRefusal("Give a hole either a diameter_m or a fastener with a fit, not both.")
    depth = fields.get("depth_m")
    for size in (diameter, depth):
        if size is not None and (isinstance(size, bool) or not isinstance(size, (int, float)) or not size > 0):
            raise EditRefusal("Hole sizes must be positive numbers.")
    positions, grid = fields.get("positions"), fields.get("grid")
    if (positions is None) == (grid is None):
        raise EditRefusal("Give the holes either positions or a grid, not both.")
    if grid is not None:
        pitch, nx, ny = (grid.get(k) if isinstance(grid, dict) else None for k in ("pitch_m", "nx", "ny"))
        if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in (nx, ny)) or not (
            isinstance(pitch, (int, float)) and pitch > 0
        ):
            raise EditRefusal("A hole grid needs a positive pitch_m and whole-number nx and ny.")
        positions = [
            [round((i - (nx - 1) / 2) * pitch, 9), round((j - (ny - 1) / 2) * pitch, 9)]
            for j in range(ny)
            for i in range(nx)
        ]
    if not positions or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in positions):
        raise EditRefusal("Each hole position is [x, y].")
    size = {"diameter_m": diameter} if diameter is not None else {"fastener": fastener, "fit": fit}
    if depth is not None:
        size["depth_m"] = depth
    return name, [CommentedMap(xyz=[p[0], p[1], 0.0], **size) for p in positions]


def _write_add(
    contract_path: Path,
    doc: Any,
    style: tuple[int, int],
    original_text: str,
    label: str,
    kind: str,
    lines: list[str],
    dry_run: bool,
    verb: str = "add",
) -> EditResult:
    """The validate-then-replace tail for an add that needs no supporting records."""
    _bump_anonymous_model(doc, lines)
    _bump_mesh_scale(doc, lines)
    _bump_holes(doc, lines)
    _bump_groups(doc, lines)
    new_text = _dump(doc, style)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, original_text) as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:
            raise
        except Exception as error:
            raise EditRefusal(
                f"{verb} {kind} {label!r} invalid: {_named(error, tmp_path, contract_path)}"
            ) from error
        diff = "".join(
            difflib.unified_diff(
                original_text.replace("\r\n", "\n").splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        if dry_run:
            return EditResult(diff=diff, lines=lines, written=False)
        _replace(tmp_path, contract_path)
        return EditResult(diff=diff, lines=lines, written=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _lockfile_model(doc: Any, *, create: bool) -> CommentedMap:
    """The model whose links and joints live in this file; an imported one is refused."""
    model = doc.get("model")
    if model is None:
        if not create:
            raise EditRefusal("This robot has no model yet; add a link first.")
        # ponytail: source_sha256 names no file for a lockfile model; sha256 of empty bytes.
        model = CommentedMap(source="lockfile", source_sha256=hashlib.sha256(b"").hexdigest())
        model["structure_hash"], model["links"], model["joints"] = "", [], []
        doc["model"] = model
    elif model.get("source") != "lockfile":
        raise EditRefusal(
            f"This robot's model is {model.get('source')}; edit that file and run "
            "lockwire start on it again, since that file is the truth."
        )
    return model


def _rehash(model: CommentedMap, source: str, witness: str | None, locked_at: dt.date) -> None:
    """Write the structure hash the loader would derive, and stamp who changed the tree."""
    try:
        links = [schema.Link.model_validate(dict(x)) for x in model.get("links") or []]
        joints = [schema.Joint.model_validate(dict(x)) for x in model.get("joints") or []]
    except ValueError as error:
        raise EditRefusal(f"the model would be invalid: {error}") from error
    model["structure_hash"] = _model_structure_hash(links, joints)
    _stamp_add_provenance(model, source=source, witness=witness, locked_at=locked_at)


def _resolve_material(value: Any) -> dict[str, Any]:
    """A material dict {name, density_kg_m3, source}, or a catalog material name."""
    if isinstance(value, dict):
        return schema.Material(**value).model_dump()
    from lockwire import catalog

    found = catalog.material(str(value))
    if found is None:
        near = difflib.get_close_matches(str(value), [m.name for m in catalog.materials()], n=3)
        hint = f" Did you mean {', '.join(near)}?" if near else ""
        raise EditRefusal(f"No catalog material named {value!r}.{hint}")
    return found.model_dump()


def _add_link_or_joint(
    doc: Any,
    kind: str,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> str:
    """Append a link or joint to a lockfile-stored model, keeping the links a tree."""
    name = fields.get("name")
    if not name:
        raise EditRefusal(f"{kind} requires --name")
    model = _lockfile_model(doc, create=kind == "link")
    links = model.get("links") or []
    joints = model.get("joints") or []
    names = {x.get("name") for x in links}
    if any(x.get("name") == name for x in (links if kind == "link" else joints)):
        raise EditRefusal(f"{kind} {name!r} already exists")
    entry = CommentedMap(name=name)
    if kind == "link":
        if fields.get("mass"):
            entry["mass"] = parse_value(str(fields["mass"]), field_name="mass", flag="--mass")
        if fields.get("geometry"):
            entry["geometry"] = [CommentedMap(shape) for shape in fields["geometry"]]
        if fields.get("material"):
            entry["material"] = CommentedMap(_resolve_material(fields["material"]))
    else:
        joint_type, parent, child = fields.get("type"), fields.get("parent"), fields.get("child")
        types = typing.get_args(schema.Joint.model_fields["type"].annotation)
        if joint_type not in types:
            raise EditRefusal(f"joint type must be one of {', '.join(types)}, not {joint_type!r}")
        for end in (parent, child):
            if end not in names:
                raise EditRefusal(f"This robot's model has no link {end!r}; add it first.")
        if parent == child:
            raise EditRefusal(f"A joint cannot join link {child!r} to itself.")
        parent_of = {j.get("child"): j.get("parent") for j in joints}
        if child in parent_of:
            raise EditRefusal(
                f"Link {child!r} already hangs from {parent_of[child]!r}; a link has one parent."
            )
        chain = _chain_to_root(parent_of, parent)
        if child in chain:
            raise EditRefusal(
                f"Joint {name!r} would close a loop ({' -> '.join([*reversed(chain), child])}); "
                "a robot's links must form a tree."
            )
        entry["type"], entry["parent"], entry["child"] = joint_type, parent, child
        for key in ("origin", "axis", "limits"):
            if fields.get(key) is not None:
                entry[key] = fields[key]
    _stamp_comment_provenance(
        entry, field_leaf="name", source=source, witness=witness, note=note, locked_at=locked_at
    )
    model["links" if kind == "link" else "joints"] = [
        *(links if kind == "link" else joints),
        entry,
    ]
    _rehash(model, source, witness, locked_at)
    return name


def _attach_part(
    doc: Any,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
    stock: tuple[dict[str, Any], str] | None = None,
) -> str:
    """A catalog part becomes a child link, joined to ``parent`` at a frame the caller
    gave (``frame`` xyz/rpy, or ``frame_id`` on the part). Never invents an offset or a limit.
    ``stock`` is (link fields, label) from _stock_link, in place of a catalog part."""
    from lockwire import catalog

    parent, part_id = fields.get("parent"), fields.get("part")
    if stock is not None:
        part_id = fields.get("stock")
        part = types.SimpleNamespace(numbers={}, frames=[])
    else:
        parts = {p.id: p for p in catalog.load_catalog().parts}
        if part_id not in parts:
            near = difflib.get_close_matches(str(part_id), list(parts), n=3)
            hint = f" Did you mean {', '.join(near)}?" if near else ""
            raise EditRefusal(f"No catalog part {part_id!r}.{hint}")
        part = parts[part_id]
    model = _lockfile_model(doc, create=True)
    if parent not in {x.get("name") for x in model.get("links") or []}:
        raise EditRefusal(f"This robot's model has no link {parent!r}; add it first.")
    name = fields.get("name") or part_id.rsplit(".", 1)[-1]

    frame, frame_id = fields.get("frame") or {}, fields.get("frame_id")
    xyz, rpy = list(frame.get("xyz") or []), frame.get("rpy")
    if frame_id is not None:
        found = next((f for f in part.frames if f.id == frame_id), None)
        if found is None:
            ids = ", ".join(f.id for f in part.frames) or "none"
            raise EditRefusal(f"{part_id} has no frame {frame_id!r}; its frames: {ids}.")
        given = xyz or [None] * 3  # the caller may only fill axes the drawing leaves open
        xyz = [d if d is not None else g for d, g in zip(found.xyz, given, strict=True)]
        rpy = list(found.rpy) if found.rpy else rpy
        if None in xyz:
            raise EditRefusal(
                f"Frame {frame_id!r} on {part_id} is undimensioned on an axis; give frame.xyz "
                "for it, since the offset is never guessed."
            )
    elif not xyz:
        raise EditRefusal("attach needs a frame (xyz and rpy) or a frame_id; no offset is guessed.")
    rpy = rpy if rpy is not None else [0.0, 0.0, 0.0]
    for label, triple in (("frame xyz", xyz), ("frame rpy", rpy)):
        ok = isinstance(triple, (list, tuple)) and len(triple) == 3
        if not ok or not all(isinstance(v, (int, float, str)) for v in triple):
            raise EditRefusal(f"{label} must be three numbers or param names")

    spec = fields.get("joint") or {}
    joint_type, axis = spec.get("type"), spec.get("axis")
    if joint_type in (None, "floating", "planar", "ball"):
        raise EditRefusal("joint type must be one of revolute, continuous, prismatic, fixed")
    if joint_type != "fixed":
        if axis is None or not any(_num3(axis, "axis")):
            raise EditRefusal(
                f"A {joint_type} joint needs a non-zero axis; the axis is undimensioned."
            )
    else:
        axis = None

    notes = [note] if note else []
    link: dict[str, Any] = {"name": name}
    if stock is not None:
        link.update(stock[0])
        notes.append(stock[1])
    mass = part.numbers.get("mass")
    if mass is not None:
        link["mass"] = f"{mass.value} {mass.unit}"
        notes.append(f"mass from catalog {part_id}: {mass.source}")
    limits = dict(spec.get("limits") or {})
    torque = part.numbers.get("stall_torque")
    if "effort" not in limits and torque is not None and joint_type in ("revolute", "continuous"):
        limits["effort"] = units.to_base(torque.value, torque.unit)
        notes.append(f"effort from catalog {part_id} stall_torque: {torque.source}")
    joint = {
        "name": spec.get("name") or f"{parent}-{name}",
        "type": joint_type,
        "parent": parent,
        "child": name,
        "origin": {"xyz": list(xyz), "rpy": list(rpy)},
        "axis": axis,
        "limits": limits or None,
    }
    joined = "; ".join(notes) or None
    _add_link_or_joint(doc, "link", link, source, witness, joined, locked_at)
    _add_link_or_joint(doc, "joint", joint, source, witness, joined, locked_at)
    return name


def _promote_device(
    contract_path: Path,
    doc: Any,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> tuple[str, list[str]]:
    """A device already placed on a link becomes a child link + joint whose origin is
    exactly that placement; the device then mounts on the new link at zero (same world pose)."""
    dev_id = fields.get("device")
    device = next((d for d in load_contract(contract_path).devices if d.id == dev_id), None)
    if device is None:
        raise EditRefusal(f"No device {dev_id!r}.")
    phys = device.physical
    parent = phys.mount_frame if phys else None
    if parent is None:
        raise EditRefusal(f"Device {dev_id!r} is not placed; place it first.")
    model = _lockfile_model(doc, create=False)
    if parent not in {x.get("name") for x in model.get("links") or []}:
        raise EditRefusal(
            f"Device {dev_id!r} mounts on {parent!r}, which is not a link of this model."
        )
    spec = fields.get("joint") or {}
    if spec.get("type") not in ("revolute", "continuous", "prismatic", "fixed"):
        raise EditRefusal("joint type must be one of revolute, continuous, prismatic, fixed")
    axis = spec.get("axis")
    if spec["type"] != "fixed" and (axis is None or not any(_num3(axis, "axis"))):
        raise EditRefusal(
            f"A {spec['type']} joint needs a non-zero axis; the axis is undimensioned."
        )
    name = fields.get("name") or dev_id.rsplit(".", 1)[-1]
    xyz = [units.to_base(q.value, q.unit) for q in phys.mount_xyz or ()] or [0.0, 0.0, 0.0]
    rpy = list(phys.mount_rpy or (0.0, 0.0, 0.0))
    said = [f"the device's mass stays on the device, mounted on {name}"]
    if not phys.mount_rpy:
        said.append(f"{dev_id} has no mount_rpy; the joint origin rpy is zeros")
    notes = [note] if note else []
    link: dict[str, Any] = {"name": name}
    joint = {
        "name": spec.get("name") or f"{parent}-{name}",
        "type": spec["type"],
        "parent": parent,
        "child": name,
        "origin": {"xyz": xyz, "rpy": rpy},
        "axis": None if spec["type"] == "fixed" else axis,
        "limits": spec.get("limits"),
    }
    joined = "; ".join(notes) or None
    _add_link_or_joint(doc, "link", link, source, witness, joined, locked_at)
    _add_link_or_joint(doc, "joint", joint, source, witness, joined, locked_at)
    row = next(d for d in doc["devices"] if d.get("id") == dev_id)["physical"]
    row["mount_frame"] = name
    if "mount_xyz" in row:
        row["mount_xyz"] = [{"value": 0.0, "unit": "m"} for _ in range(3)]
    if "mount_rpy" in row:
        row["mount_rpy"] = [0.0, 0.0, 0.0]
    return name, said


def _stock_dim(label: str, value: Any, params: dict[str, Any]) -> float:
    """A stock dimension's metres: a number, or a param name (leading "-" negates it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EditRefusal(f"stock dimension {label} must be a number or a param name")
    if isinstance(value, str):
        if value.removeprefix("-") not in params:
            raise EditRefusal(f"stock dimension {label} names no param {value.removeprefix('-')!r}")
        number = float(params[value.removeprefix("-")]) * (-1 if value.startswith("-") else 1)
    else:
        number = float(value)
    if not number > 0:
        raise EditRefusal(f"stock dimension {label} must be positive, got {value}.")
    return number


def _stock_link(doc: Any, fields: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The link fields and mass label for a stock recipe sized by ``dimensions``.
    Plate and rod are solid, so the mass is derived from shape x material. A hollow shape (tube,
    angle) has no primitive: it is drawn as its outer solid and its mass is declared from
    density x true cross-section x length. A T-slot declares maker's mass per metre x length."""
    from lockwire import catalog

    stock_id = fields.get("stock")
    rows = {r.id: r for r in catalog.stock()}
    if stock_id not in rows:
        near = difflib.get_close_matches(str(stock_id), list(rows), n=3)
        hint = f" Did you mean {', '.join(near)}?" if near else ""
        raise EditRefusal(f"No stock shape {stock_id!r}.{hint}")
    row, given = rows[stock_id], dict(fields.get("dimensions") or {})
    if extra := sorted(set(given) - set(row.dimensions)):
        raise EditRefusal(f"{stock_id} has no dimension {extra[0]!r}; it takes {row.dimensions}.")
    if missing := [d for d in row.dimensions if d not in given]:
        raise EditRefusal(f"{stock_id} needs dimension {missing[0]!r}.")
    params = {p.get("name"): p.get("value") for p in doc.get("params") or [] if isinstance(p, dict)}
    raw = {d: given[d] for d in row.dimensions}
    if (
        row.shape != "plate"
        and row.shape != "round_rod"
        and any(isinstance(x, str) for x in raw.values())
    ):
        raise EditRefusal(
            f"{stock_id} takes numbers for its dimensions: its mass is computed once from them, "
            "so a named dimension would go stale."
        )
    v = {d: _stock_dim(d, raw[d], params) for d in row.dimensions}

    def half(label: str) -> float:  # a cylinder wants a radius, which a named diameter cannot give
        if isinstance(raw[label], str):
            raise EditRefusal(f"{label} must be a number: a param cannot be halved into a radius.")
        return v[label] / 2

    shape, length, area = row.shape, raw.get("length"), None
    if shape == "plate":
        geometry = {"kind": "box", "size": [raw["length"], raw["width"], raw["thickness"]]}
    elif shape == "round_rod":
        geometry = {"kind": "cylinder", "radius": half("diameter"), "length": length}
    elif shape == "round_tube":
        r, w = half("outer_diameter"), v["wall"]
        geometry = {"kind": "cylinder", "radius": r, "length": length}
        area = math.pi * (r * r - (r - w) ** 2) if w <= r else None
    elif shape == "angle":
        a, b, t = v["leg_a"], v["leg_b"], v["thickness"]
        geometry = {"kind": "box", "size": [raw["leg_a"], raw["leg_b"], length]}
        area = t * (a + b - t) if t <= min(a, b) else None
    elif shape == "tslot":
        geometry = {"kind": "box", "size": [*row.outer, length]}  # type: ignore[misc]
    else:  # square_tube, rect_tube
        w_, h_ = (
            v.get("outer_width", v.get("outer_size")),
            v.get("outer_height", v.get("outer_size")),
        )
        side = [
            raw.get("outer_width", raw.get("outer_size")),
            raw.get("outer_height", raw.get("outer_size")),
        ]
        geometry = {"kind": "box", "size": [*side, length]}
        area = (
            w_ * h_ - (w_ - 2 * v["wall"]) * (h_ - 2 * v["wall"])
            if 2 * v["wall"] <= min(w_, h_)
            else None
        )
    if shape in ("round_tube", "square_tube", "rect_tube", "angle") and area is None:
        raise EditRefusal(f"{stock_id}: the wall or thickness is thicker than half the outer size.")

    if shape == "tslot":
        mpl = row.mass_per_length
        if mpl.unit != "kg/m":  # type: ignore[union-attr]
            raise EditRefusal(f"{stock_id}: mass per length is in {mpl.unit}, not kg/m.")  # type: ignore[union-attr]
        mass = mpl.value * v["length"]  # type: ignore[union-attr]
        label = (
            f"mass from maker's mass per metre ({mpl.value} kg/m x {v['length']} m): {mpl.source}"  # type: ignore[union-attr]
        )
        return {"geometry": [geometry], "mass": f"{mass:.6g} kg"}, label
    material = fields.get("material")
    if not material:
        raise EditRefusal(f"{stock_id} needs a material.")
    found = _resolve_material(material)
    out: dict[str, Any] = {"geometry": [geometry], "material": material}
    if area is None:
        return out, f"mass derived from {stock_id} shape x material {found['name']}"
    mass = area * v["length"] * found["density_kg_m3"]
    out["mass"] = f"{mass:.6g} kg"
    return out, (
        f"mass from {found['name']} density x true cross-section x length (density source: "
        f"{found['source']}); drawn as outline only, the hollow shape has no primitive"
    )


def _num3(value: Any, what: str) -> list[float]:
    ok = isinstance(value, (list, tuple)) and len(value) == 3
    if not ok or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        raise EditRefusal(f"{what} must be three numbers (a param name cannot be patterned)")
    return [float(v) for v in value]


def _rot(axis: list[float], angle: float) -> list[list[float]]:
    """Rodrigues rotation matrix about the unit axis."""
    n = math.sqrt(sum(a * a for a in axis))
    x, y, z = (a / n for a in axis)
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def _mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rpy_matrix(rpy: list[float]) -> list[list[float]]:
    rx, ry, rz = (_rot(axis, a) for axis, a in zip(([1, 0, 0], [0, 1, 0], [0, 0, 1]), rpy))
    return _mul(rz, _mul(ry, rx))  # URDF fixed-axis roll, pitch, yaw


def _matrix_rpy(m: list[list[float]]) -> list[float]:
    pitch = math.asin(max(-1.0, min(1.0, -m[2][0])))
    if abs(m[2][0]) > 1 - 1e-9:  # gimbal lock: roll 0, all in yaw
        return [0.0, pitch, math.atan2(-m[0][1], m[1][1])]
    return [math.atan2(m[2][1], m[2][2]), pitch, math.atan2(m[1][0], m[0][0])]


def _copy_branch(
    model: CommentedMap, seed: str, new_name: str, origin: dict[str, list[float]]
) -> tuple[CommentedMap, CommentedMap]:
    """A new link (the seed's body under ``new_name``) and a copy of the seed's parent joint."""
    links, joints = model.get("links") or [], model.get("joints") or []
    link = next((x for x in links if x.get("name") == seed), None)
    if link is None:
        raise EditRefusal(f"This robot's model has no link {seed!r}.")
    joint = next((j for j in joints if j.get("child") == seed), None)
    if joint is None:
        raise EditRefusal(f"Link {seed!r} has no parent joint to copy; it is the root.")
    joint_name = f"{joint['name']}-{new_name.removeprefix(seed + '-')}"
    if new_name in {x.get("name") for x in links} or joint_name in {j.get("name") for j in joints}:
        raise EditRefusal(f"link {new_name!r} (or its joint) already exists")
    new_link = CommentedMap(name=new_name)
    for key, value in link.items():
        if key != "name":
            new_link[key] = copy.deepcopy(value)
    new_joint = CommentedMap(name=joint_name)
    for key, value in joint.items():
        if key != "name":
            new_joint[key] = copy.deepcopy(value)
    new_joint["child"] = new_name
    new_joint["origin"] = CommentedMap({k: list(v) for k, v in origin.items()})  # no YAML aliases
    return new_link, new_joint


def _add_pattern_or_duplicate(
    doc: Any,
    kind: str,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> list[str]:
    """Copy a link and its parent joint (duplicate) or n times around/along an axis."""
    seed = fields.get("of")
    if not seed:
        raise EditRefusal(f"{kind} requires 'of', the link to copy")
    model = _lockfile_model(doc, create=False)
    if seed not in {x.get("name") for x in model.get("links") or []}:
        raise EditRefusal(f"This robot's model has no link {seed!r}.")
    joint = next((j for j in model.get("joints") or [] if j.get("child") == seed), None)
    if joint is None:
        raise EditRefusal(f"Link {seed!r} has no parent joint to copy; it is the root.")
    base = joint.get("origin") or {}
    xyz = _num3(base.get("xyz", [0, 0, 0]), "the joint origin xyz")
    rpy = _num3(base.get("rpy", [0, 0, 0]), "the joint origin rpy")
    copies: list[tuple[str, dict[str, list[float]]]] = []
    if kind == "duplicate":
        offset = _num3(fields.get("offset_xyz", [0, 0, 0]), "offset_xyz")
        name = fields.get("name") or f"{seed}-2"
        copies.append((name, {"xyz": [a + b for a, b in zip(xyz, offset)], "rpy": rpy}))
        record = None
    else:
        pid, pkind = fields.get("id") or f"{seed}-{fields.get('kind')}", fields.get("kind")
        existing = doc.get("patterns") or []
        if any(p.get("id") == pid for p in existing):
            raise EditRefusal(f"pattern {pid!r} already exists")
        record = {
            k: fields[k]
            for k in ("kind", "of", "count", "axis", "angle_deg", "pitch_m")
            if fields.get(k) is not None
        }
        record = {"id": pid, **record}
        if fields.get("joint_type"):
            record["joint_type"] = fields["joint_type"]
        try:
            pattern = schema.Pattern.model_validate(record)
        except ValueError as error:
            raise EditRefusal(f"pattern {pid!r} invalid: {error}") from error
        axis = list(pattern.axis)
        for i in range(1, pattern.count):
            if pkind == "circular":
                step = pattern.angle_deg if pattern.angle_deg is not None else 360 / pattern.count
                r = _rot(axis, math.radians(i * step))
                moved = [sum(r[row][k] * xyz[k] for k in range(3)) for row in range(3)]
                turned = _matrix_rpy(_mul(r, _rpy_matrix(rpy)))
                origin = {"xyz": moved, "rpy": turned}
            else:
                n = math.sqrt(sum(a * a for a in axis))
                shift = [i * pattern.pitch_m * a / n for a in axis]
                origin = {"xyz": [a + b for a, b in zip(xyz, shift)], "rpy": rpy}
            copies.append((f"{seed}-{i + 1}", origin))
    lines = []
    for name, origin in copies:  # all refusals above/inside land before any write is kept
        link, new_joint = _copy_branch(model, seed, name, origin)
        for owner in (link, new_joint):
            _stamp_comment_provenance(
                owner,
                field_leaf="name",
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            )
        model["links"] = [*model["links"], link]
        model["joints"] = [*model["joints"], new_joint]
        lines.append(f"added link {name} and joint {new_joint['name']} (copy of {seed})")
    if record is not None:
        _ensure_list(doc, ["patterns"]).append(CommentedMap(record))
    _rehash(model, source, witness, locked_at)
    return lines


_MIRROR_NORMAL = {"xz": 1, "yz": 0, "xy": 2}


def _neg(value: Any) -> Any:
    """Minus a number or a param dimension ("name" <-> "-name", PARAM-NEG); never -0.0."""
    if isinstance(value, str):
        return value[1:] if value.startswith("-") else f"-{value}"
    return 0.0 if value == 0 else -value


def _mirror_origin(origin: Any, n: int) -> CommentedMap:
    """Mirror a pose across the plane with normal axis ``n``. M = I - 2 n nT; R' = M R M.
    M Rot(e_i, a) M = Rot(M e_i, -a), so each rpy angle flips unless it is about the normal
    (Rz Ry Rx conjugates factor by factor); xyz flips only its normal component."""
    out = CommentedMap(origin)
    if "xyz" in out:
        out["xyz"] = [_neg(v) if i == n else v for i, v in enumerate(out["xyz"])]
    if "rpy" in out:
        out["rpy"] = [v if i == n else _neg(v) for i, v in enumerate(out["rpy"])]
    return out


def _mirror_link(link: CommentedMap, new_name: str, n: int) -> CommentedMap:
    new = CommentedMap(name=new_name)
    for key, value in link.items():
        if key != "name":
            new[key] = copy.deepcopy(value)
    if new.get("com") is not None:
        new["com"] = [_neg(v) if i == n else v for i, v in enumerate(new["com"])]
    inertia = new.get("inertia")
    if inertia is not None:  # axes rows are eigenvectors: M v; third row flipped to stay right-handed
        rows = [[_neg(v) if i == n else v for i, v in enumerate(r)] for r in inertia["axes"]]
        inertia["axes"] = [*rows[:2], [_neg(v) for v in rows[2]]]
    for key in ("geometry", "collision"):
        for shape in new.get(key) or []:
            if shape.get("origin") is not None:
                shape["origin"] = _mirror_origin(shape["origin"], n)
    return new


def _mirror_branch(
    doc: Any,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> list[str]:
    """Mirror a link (and by default its descendants, with their joints) across the
    plane through the seed's parent link frame origin. CALLs: a revolute/continuous axis is a
    pseudo-vector so it becomes -(M a); a prismatic axis M a; limits are unchanged, so the
    mirrored joint moved by +q lands at the mirror image of the original moved by +q. Devices
    are not copied; mimic joints inside the copy follow the new names."""
    seed, plane = fields.get("of"), fields.get("plane")
    if not seed:
        raise EditRefusal("mirror requires 'of', the link to mirror")
    if plane not in _MIRROR_NORMAL:
        raise EditRefusal("mirror plane must be xz, yz or xy")
    n = _MIRROR_NORMAL[plane]
    model = _lockfile_model(doc, create=False)
    links, joints = model.get("links") or [], model.get("joints") or []
    if seed not in {x.get("name") for x in links}:
        raise EditRefusal(f"This robot's model has no link {seed!r}.")
    if not any(j.get("child") == seed for j in joints):
        raise EditRefusal(f"Link {seed!r} has no parent joint to copy; it is the root.")
    members = {seed}
    while fields.get("subtree", True):  # joints are not ordered parent-first
        more = {j["child"] for j in joints if j["parent"] in members} - members
        if not more:
            break
        members |= more
    rename = fields.get("rename") or {}

    def renamed(name: str) -> str:
        old, new = rename.get("from"), rename.get("to")
        out = name.replace(old, new) if old and new is not None else name
        return out if out != name else f"{name}-mirror"

    mine = [x for x in links if x.get("name") in members]
    for x in mine:
        if any(s.get("kind") == "mesh" for k in ("geometry", "collision") for s in x.get(k) or []):
            raise EditRefusal(f"link {x['name']!r} has a mesh shape; a mesh cannot be mirrored")
    mjoints = [j for j in joints if j.get("child") in members]
    fresh = [renamed(x["name"]) for x in mine] + [renamed(j["name"]) for j in mjoints]
    taken = {x.get("name") for x in links} | {j.get("name") for j in joints}
    for name in fresh:
        if name in taken or fresh.count(name) > 1:
            raise EditRefusal(f"link {name!r} (or its joint) already exists")
    out_links = [_mirror_link(x, renamed(x["name"]), n) for x in mine]
    out_joints = []
    for j in mjoints:
        new = CommentedMap(name=renamed(j["name"]))
        for key, value in j.items():
            if key != "name":
                new[key] = copy.deepcopy(value)
        new["child"] = renamed(j["child"])
        if j["parent"] in members:
            new["parent"] = renamed(j["parent"])
        new["origin"] = _mirror_origin(j.get("origin") or CommentedMap(), n)
        if j.get("axis") is not None:
            a = [-v if i == n else v for i, v in enumerate(j["axis"])]  # M a
            new["axis"] = a if j.get("type") == "prismatic" else [_neg(v) for v in a]
        if new.get("mimic") is not None and new["mimic"]["joint"] in {x["name"] for x in mjoints}:
            new["mimic"]["joint"] = renamed(new["mimic"]["joint"])
        out_joints.append(new)
    for owner in (*out_links, *out_joints):
        _stamp_comment_provenance(
            owner, field_leaf="name", source=source, witness=witness, note=note,
            locked_at=locked_at,
        )  # fmt: skip
    model["links"] = [*links, *out_links]
    model["joints"] = [*joints, *out_joints]
    _rehash(model, source, witness, locked_at)
    return [f"mirrored {seed} across {plane}: added {len(out_links)} link(s) and joint(s)"]


def _paths_naming(node: Any, value: str, *, skip: Any, path: str = "") -> Iterator[str]:
    """Every path whose string equals ``value`` or embeds it as a ``devices/<value>`` contract
    path: catches each field that holds a device id, present and future, without a list of
    them to keep in step with schema.py."""
    if node is skip:
        return
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _paths_naming(child, value, skip=skip, path=f"{path}/{key}".lstrip("/"))
    elif isinstance(node, list):
        for i, child in enumerate(node):
            label = child.get("id", child.get("name", i)) if isinstance(child, dict) else i
            yield from _paths_naming(child, value, skip=skip, path=f"{path}/{label}")
    elif isinstance(node, str) and (
        node == value or f"devices/{value}/" in node or node == f"devices/{value}"
    ):
        yield path


@_locked_write
def delete_entry(
    contract_path: Path,
    kind: str,
    name: str,
    *,
    source: str,
    witness: str | None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
    index: int | None = None,
) -> EditResult:
    """Delete a link, joint, device, group (ungroup), or one geometry shape (kind "shape": name the link, pass index)."""
    if kind not in ("link", "joint", "device", "shape", "group"):
        raise EditRefusal(
            f"unknown delete kind {kind!r}; expected link, joint, device, group or shape"
        )
    if witness is not None or source:  # an anonymous edit may give no source
        validate_source(source)
    if witness is not None:
        validate_witness(witness, refusal=f"--witness must name a real person, not {witness!r}")
    original_text, doc, style = _load_doc_guarded(contract_path)
    locked_at = locked_at or dt.date.today()
    if kind == "group":
        rows, key = doc.get("groups") or [], "name"
    elif kind == "device":
        rows, key = doc.get("devices") or [], "id"
    else:
        model = _lockfile_model(doc, create=False)
        rows, key = model.get("links" if kind == "shape" else f"{kind}s") or [], "name"
    row = next((x for x in rows if isinstance(x, dict) and x.get(key) == name), None)
    if row is None:
        raise EditRefusal(f"There is no {'link' if kind == 'shape' else kind} {name!r} to delete.")
    if kind == "shape":
        shapes = row.get("geometry") or []
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(shapes):
            raise EditRefusal(
                f"Link {name!r} has no shape at index {index!r} ({len(shapes)} shapes)."
            )
        del shapes[index]
        if not shapes:
            del row["geometry"]  # None, not empty
        _rehash(model, source, witness, locked_at)
        line = f"deleted shape {index} of link {name} ({_att(witness, source)})"
        return _write_add(
            contract_path, doc, style, original_text, name, kind, [line], dry_run, verb="delete"
        )
    if kind == "group":
        rows.remove(row)
        if not rows:
            del doc["groups"]  # None, not empty
        line = f"deleted group {name} (its members stay; {_att(witness, source)})"
        return _write_add(
            contract_path, doc, style, original_text, name, kind, [line], dry_run, verb="delete"
        )
    if kind == "device":
        named = [p for p in _paths_naming(doc, name, skip=row) if not p.startswith("groups/")]
        if named:
            raise EditRefusal(
                f"Device {name!r} is still named by {', '.join(named[:3])}"
                f"{f' and {len(named) - 3} more' if len(named) > 3 else ''}; change those first."
            )
    extra: list[str] = []
    if kind == "link":
        joints = model.get("joints") or []
        kids = [f"{j.get('child')} by joint {j.get('name')}" for j in joints if j.get("parent") == name]
        if kids:
            n = len(kids)
            raise EditRefusal(
                f"{name} holds {n} part{'s' if n > 1 else ''} ({', '.join(kids[:3])}"
                f"{', ...' if n > 3 else ''}); delete or re-attach "
                f"{'it' if n == 1 else 'them'} first"
            )
        mounted = [
            d.get("id")
            for d in doc.get("devices") or []
            if (d.get("physical") or {}).get("mount_frame") == name
        ]
        if mounted:
            raise EditRefusal(
                f"{name} carries {', '.join(mounted)}; re-mount or delete "
                f"{'it' if len(mounted) == 1 else 'them'} first."
            )
        for j in [j for j in joints if j.get("child") == name]:
            joints.remove(j)
            extra.append(f"deleted joint {j.get('name')} (it only attached {name})")
            _drop_mimics(joints, j.get("name"))
        extra += _drop_link_refs(doc, name)
    if kind == "joint":
        child = row.get("child")
        others = [j for j in model.get("joints") or [] if j is not row]
        if not any(j.get("child") == child for j in others) and _link_mass(contract_path, child):
            raise EditRefusal(
                f"deleting {name} would leave {child} floating with its weight; "
                f"delete the part {child} instead"
            )
        _drop_mimics(others, name)
    rows.remove(row)
    if kind != "device":
        _rehash(model, source, witness, locked_at)
    line = f"deleted {kind} {name} ({_att(witness, source)})"
    lines = [line, *extra, *_leave_groups(doc, name)]
    return _write_add(
        contract_path, doc, style, original_text, name, kind, lines, dry_run, verb="delete"
    )


def _link_mass(contract_path: Path, link: str) -> bool:
    """True if the link has a mass, declared or derived from its shapes and material."""
    found = next((k for k in load_contract(contract_path).model.links if k.name == link), None)
    return bool(found and found.mass and found.mass.base() > 0)


def _drop_mimics(joints: Any, joint: str | None) -> None:
    """A joint that copied a deleted joint's motion stops copying it."""
    for j in joints:
        if (j.get("mimic") or {}).get("joint") == joint:
            del j["mimic"]


def _drop_link_refs(doc: Any, link: str) -> list[str]:
    """Remove the entries that only existed for a deleted link; an emptied list is dropped (None)."""
    out: list[str] = []
    for key, test in (
        ("collision_pairs", lambda r: link in (r.get("link1"), r.get("link2"))),
        ("contacts", lambda r: r.get("link") == link),
        ("patterns", lambda r: r.get("of") == link),
    ):
        rows = doc.get(key) or []
        gone = [r for r in rows if test(r)]
        for r in gone:
            rows.remove(r)
        if gone:
            out.append(f"removed {len(gone)} {key.replace('_', ' ')} entry(s) naming {link}")
        if key in doc and not rows:
            del doc[key]
    if link in (doc.get("printed") or {}):
        del doc["printed"][link]
        if not doc["printed"]:
            del doc["printed"]
    return out


def _leave_groups(doc: Any, part: str) -> list[str]:
    """Take a deleted link or device out of its group; an emptied group is dropped (and said so)."""
    out: list[str] = []
    for g in list(doc.get("groups") or []):
        if part in g.get("members", ()):
            g["members"].remove(part)
            out.append(f"removed {part} from group {g['name']}")
            if not g["members"]:
                doc["groups"].remove(g)
                out.append(f"group {g['name']} had no members left and was deleted")
    if "groups" in doc and not doc["groups"]:
        del doc["groups"]
    return out


# ---- one id and every reference to it, in one write.
# Each kind lists EVERY place the schema holds that id: a reference missing here is a visible
# omission, not a silent dangling name. Spec: "/" walks keys, "[]" every list element, a final
# "{}" renames the keys of that mapping. Modes: id (exact), dim (also "-name", PARAM-NEG),
# path (a contract path segment after its collection name, the matcher idea).
# Not covered (names only in free-form structural/simulation inputs): see the docs.
_PATHS = (
    ("edges[]/quantity", "path"),
    ("accepted[]/id", "path"),
    ("fitted_band_allocations[]/fitted_bands[]", "path"),
)
_DIMS = ("size[]", "radius", "length", "origin/xyz[]", "origin/rpy[]")
_RENAME_ROWS = {
    "link": "model/links[]/name",
    "joint": "model/joints[]/name",
    "device": "devices[]/id",
    "rail": "topology/rails[]/id",
    "wire": "topology/wires[]/id",
    "param": "params[]/name",
    "group": "groups[]/name",
}
_PATH_COLLECTION = {
    "link": "links",
    "joint": "joints",
    "device": "devices",
    "rail": "rails",
    "wire": "wires",
}
_PROTECTS = ("topology/protection[]/protects[]", "id")  # one namespace for rail, wire, device
_RENAME_REFS: dict[str, tuple[tuple[str, str], ...]] = {
    "link": (
        ("model/links[]/frame", "id"),
        ("model/joints[]/parent", "id"),
        ("model/joints[]/child", "id"),
        ("devices[]/physical/mount_frame", "id"),
        ("patterns[]/of", "id"),
        ("collision_pairs[]/link1", "id"),
        ("collision_pairs[]/link2", "id"),
        ("contacts[]/link", "id"),
        ("printed{}", "id"),
        ("model/mesh_refs[]/link", "id"),
        ("model/zero_mass_links[]", "id"),
        ("groups[]/members[]", "id"),
        *_PATHS,
    ),
    "joint": (
        ("model/joints[]/mimic/joint", "id"),
        ("controllers[]/joint", "id"),
        ("devices[]/drives_joint", "id"),
        ("gearboxes[]/input_joint", "id"),
        ("gearboxes[]/output_joint", "id"),
        ("actuator_cases[]/joint", "id"),
        ("actuator_cases[]/bindings/independent_load/joint", "id"),
        ("servo{}", "id"),
        *_PATHS,
    ),
    "device": (
        ("topology/wires[]/from/device", "id"),
        ("topology/wires[]/to/device", "id"),
        ("topology/rails[]/charged_by", "id"),
        ("topology/rails[]/source_device", "id"),
        ("topology/hosts[]/id", "id"),
        ("topology/buses[]/host", "id"),
        ("devices[]/identity/host", "id"),
        ("topology/buses[]/can_segment/nodes[]", "id"),
        ("topology/buses[]/can_segment/termination_nodes[]", "id"),
        ("assertions[]/instance", "id"),
        ("safety/channels[]/device", "id"),
        ("actuator_cases[]/actuator_device", "id"),
        ("actuator_cases[]/transient_dc/part", "id"),
        ("groups[]/members[]", "id"),
        _PROTECTS,
        *_PATHS,
    ),
    "rail": (
        ("topology/rails[]/upstream", "id"),
        ("topology/wires[]/rail", "id"),
        ("devices[]/power/rail", "id"),
        ("budgets[]/rail", "id"),
        ("actuator_cases[]/rail", "id"),
        ("actuator_cases[]/transient_dc/supply_rail", "id"),
        _PROTECTS,
        *_PATHS,
    ),
    "wire": (_PROTECTS, *_PATHS),
    "group": (),
    "param": (
        *((f"model/links[]/{g}[]/{d}", "dim") for g in ("geometry", "collision") for d in _DIMS),
        ("model/joints[]/origin/xyz[]", "dim"),
        ("model/joints[]/origin/rpy[]", "dim"),
        ("variants[]/params{}", "id"),
    ),
}


def _renamed(value: str, mode: str, old: str, new: str, kind: str) -> str:
    if mode == "path":
        parts = value.split("/")
        for i in range(1, len(parts)):
            if parts[i] == old and parts[i - 1] == _PATH_COLLECTION.get(kind):
                parts[i] = new
        return "/".join(parts)
    if value == old:
        return new
    return "-" + new if mode == "dim" and value == "-" + old else value


def _rewrite(node: Any, spec: list[str], mode: str, old: str, new: str, kind: str) -> int:
    """Apply one reference spec under ``node``; the number of values changed."""
    head, rest = spec[0], spec[1:]
    if not isinstance(node, dict):
        return 0
    if head.endswith("{}"):
        found = node.get(head[:-2])
        if not isinstance(found, dict) or old not in found:
            return 0
        items = list(found.items())  # ponytail: rebuilt to keep order; comments on keys are lost
        found.clear()
        for key, value in items:
            found[new if key == old else key] = value
        return 1
    each = head.endswith("[]")
    name = head[:-2] if each else head
    child = node.get(name)
    if rest:
        children = child if each else [child]
        if not isinstance(children, list):
            return 0
        return sum(_rewrite(c, rest, mode, old, new, kind) for c in children)
    if each and isinstance(child, list):
        slots = [(child, i) for i in range(len(child))]
    elif not each and name in node:
        slots = [(node, name)]
    else:
        return 0
    changed = 0
    for holder, slot in slots:
        value = holder[slot]
        if isinstance(value, str) and (out := _renamed(value, mode, old, new, kind)) != value:
            holder[slot] = out
            changed += 1
    return changed


@_locked_write
def rename_entry(
    contract_path: Path,
    kind: str,
    old: str,
    new: str,
    *,
    source: str,
    witness: str | None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Rename a link, joint, device, rail, wire or param and every reference to it, in one write."""
    if kind not in _RENAME_ROWS:
        raise EditRefusal(
            f"unknown rename kind {kind!r}; expected link, joint, device, rail, wire, param or group"
        )
    if witness is not None or source:  # an anonymous edit may give no source
        validate_source(source)
    if witness is not None:
        validate_witness(witness, refusal=f"--witness must name a real person, not {witness!r}")
    original_text, doc, style = _load_doc_guarded(contract_path)
    locked_at = locked_at or dt.date.today()
    model = doc.get("model") if kind in ("link", "joint") else None
    if model is not None:
        model = _lockfile_model(doc, create=False)  # an imported model's names are its file's
    if not isinstance(new, str) or not new.strip() or "/" in new or new.startswith("-"):
        raise EditRefusal(
            f"{new!r} is not a valid {kind} name: it must be non-empty, "
            "contain no '/' and not start with '-'."
        )
    *rows_spec, key = _RENAME_ROWS[kind].split("/")
    node: Any = doc
    for segment in rows_spec:
        node = node.get(segment.removesuffix("[]")) if isinstance(node, dict) else None
    rows = node if isinstance(node, list) else []
    if not any(isinstance(r, dict) and r.get(key) == old for r in rows):
        raise EditRefusal(f"There is no {kind} {old!r} to rename.")
    if any(isinstance(r, dict) and r.get(key) == new for r in rows):
        raise EditRefusal(f"There is already a {kind} named {new!r}.")
    changed = _rewrite(doc, _RENAME_ROWS[kind].split("/"), "id", old, new, kind)
    for spec, mode in _RENAME_REFS[kind]:
        changed += _rewrite(doc, spec.split("/"), mode, old, new, kind)
    if model is not None:
        _rehash(model, source, witness, locked_at)
    line = (
        f"renamed {kind} {old} to {new}: {changed} places updated "
        f"({_att(witness, source)})"
    )
    return _write_add(
        contract_path, doc, style, original_text, new, kind, [line], dry_run, verb="rename"
    )


def _build_connector(
    doc: Any,
    fields: dict[str, Any],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
) -> tuple[CommentedMap, str]:
    """Attach a connector (pins 1..pin_count, named from the type) to a device; returns it and the device id.
    Given `id` and `pins` (each a schema Pin, e.g. copied from a pin card), those pins are written as-is."""
    device_id, type_id = fields.get("device"), fields.get("type")
    device = next(
        (d for d in doc.get("devices") or [] if isinstance(d, dict) and d.get("id") == device_id),
        None,
    )
    if not device_id or not (type_id or fields.get("pins")):
        raise EditRefusal("connector requires --device and --type")
    if device is None:
        raise EditRefusal(f"unknown device {device_id!r}")
    if fields.get("pins"):
        return _connector_with_pins(device, fields, source, witness, note, locked_at), device_id
    types = _ensure_list(doc, ["connector_types"])
    ctype = next((t for t in types if isinstance(t, dict) and t.get("id") == type_id), None)
    cited = None
    if ctype is None:
        from lockwire.catalog import load_catalog

        known = {t.id: t for t in load_catalog().connector_types}
        if type_id not in known:
            close = difflib.get_close_matches(type_id, [*(t.get("id") for t in types), *known], n=3)
            hint = f" (did you mean {', '.join(close)}?)" if close else ""
            raise EditRefusal(f"unknown connector type {type_id!r}{hint}")
        cat = known[type_id]
        ctype = CommentedMap(cat.model_dump(mode="json", exclude_none=True, exclude={"sources"}))
        cited = "; ".join(sorted({v.split(" (")[0] for v in cat.sources.values()}))
    count, labels = ctype.get("pin_count"), ctype.get("pin_labels") or []
    if not count:
        raise EditRefusal(f"connector type {type_id!r} has no pin_count")
    existing = device.get("connectors") or []
    taken = {c.get("id") for c in existing if isinstance(c, dict)}
    cid = fields.get("id") or next(
        c for c in (type_id, *(f"{type_id}-{n}" for n in range(2, 99))) if c not in taken
    )
    if cid in taken:
        raise EditRefusal(f"connector {cid!r} already exists on {device_id}")
    entry = CommentedMap(id=cid, type=type_id)
    if fields.get("placement"):
        entry["placement"] = fields["placement"]
    entry["pins"] = [
        CommentedMap(number=n, **({"name": labels[n - 1]} if n <= len(labels) else {}))
        for n in range(1, count + 1)
    ]
    if cited:
        types.append(ctype)
        ctype.yaml_set_start_comment(f"from the parts catalog: {cited}", indent=4)
    _stamp_comment_provenance(
        entry, field_leaf="type", source=source, witness=witness, note=note, locked_at=locked_at
    )
    device["connectors"] = [*existing, entry]
    return entry, device_id


def _connector_with_pins(device, fields, source, witness, note, locked_at) -> CommentedMap:
    from lockwire.schema import Pin

    if not fields.get("id"):
        raise EditRefusal("a connector with pins requires id")
    if fields["id"] in {c.get("id") for c in device.get("connectors") or [] if isinstance(c, dict)}:
        raise EditRefusal(f"connector {fields['id']!r} already exists on {device.get('id')}")
    try:
        pins = [
            Pin.model_validate(p).model_dump(mode="json", exclude_none=True) for p in fields["pins"]
        ]
    except ValueError as error:
        raise EditRefusal(f"connector {fields['id']!r}: {error}") from error
    entry = CommentedMap(
        id=fields["id"], **({"type": fields["type"]} if fields.get("type") else {})
    )
    entry["pins"] = [CommentedMap(p) for p in pins]
    _stamp_comment_provenance(
        entry, field_leaf="id", source=source, witness=witness, note=note, locked_at=locked_at
    )
    device["connectors"] = [*(device.get("connectors") or []), entry]
    return entry


def _add_battery_entry(
    contract_path: Path,
    doc: Any,
    fields: dict[str, Any],
    *,
    original_text: str,
    style: tuple[int, int],
    source: str,
    witness: str | None,
    note: str | None,
    locked_at: dt.date,
    dry_run: bool,
) -> EditResult:
    """Create or update `topology/battery` — a singleton, not an id'd collection.

    `Battery` (schema.py) has no `provenance` field and forbids extras
    (schema fields are added by a schema change) — source/witness/chemistry
    are recorded as a YAML comment on the `battery:` key instead of a
    structured block. Unstructured: `check`/`describe` never read it back.
    """
    capacity = fields.get("capacity")
    if not capacity:
        raise EditRefusal("battery requires --capacity")
    topology = _navigate(doc, ["topology"], create=True)
    battery = topology.get("battery")
    if not isinstance(battery, dict):
        battery = CommentedMap()
        topology["battery"] = battery
    battery["capacity"] = parse_value(capacity, field_name="capacity", flag="--capacity")
    if fields.get("nominal_voltage"):
        battery["voltage"] = parse_value(
            fields["nominal_voltage"], field_name="voltage", flag="--nominal-voltage"
        )
    _copy_optional(battery, fields, _BATTERY_F1A)

    stamp = f"{locked_at.isoformat()}: battery declared{_by(witness)}, source {source}"
    if fields.get("chemistry"):
        stamp += f"; chemistry {fields['chemistry']}"
    if note:
        stamp += f"; {note}"
    topology.yaml_set_comment_before_after_key("battery", before=stamp, indent=2)

    new_text = _dump(doc, style)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, original_text) as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:  # in the file before the edit; see apply_changes
            raise
        except Exception as error:
            raise EditRefusal(
                f"battery invalid: {_named(error, tmp_path, contract_path)}"
            ) from error

        diff = "".join(
            difflib.unified_diff(
                original_text.replace("\r\n", "\n").splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        line = f"declared battery capacity ({_att(witness, source)})"
        if dry_run:
            return EditResult(diff=diff, lines=[line], written=False)
        _replace(tmp_path, contract_path)
        return EditResult(diff=diff, lines=[line], written=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _ensure_list(doc: Any, segments: list[str]) -> list:
    parent = _navigate(doc, segments[:-1], create=True)
    key = segments[-1]
    if key not in parent or parent[key] is None:
        parent[key] = []
    node = parent[key]
    if not isinstance(node, list):
        raise EditRefusal(f"{'/'.join(segments)} is not a list")
    return node


def _refuse_duplicate(collection: list, new_id: str, kind: str) -> None:
    if any(isinstance(x, dict) and x.get("id") == new_id for x in collection):
        raise EditRefusal(f"{kind} {new_id!r} already exists")


def _stamp_add_provenance(
    entry: CommentedMap,
    *,
    source: str,
    witness: str | None,
    locked_at: dt.date,
    note: str | None = None,
) -> None:
    """The one shared provenance stamp every ``add``-written record goes
    through (mutation bite: stamping per record with duplicated inline code
    instead of this one function is exactly the bug its test catches).

    A one-submit device add calls this 2-3 times with the SAME ``locked_at``
    object; ruamel anchors/aliases a repeated node by identity, and this repo
    refuses anchors on read (``_reject_yaml_anchors_aliases``) — so each call
    gets its own fresh ``date``, never the caller's shared one.
    """
    if witness is None and not (source and source.strip()):
        entry.pop("provenance", None)  # anonymous and no source: record nothing, no stale name stays
        return
    prov = CommentedMap()
    if witness is not None:
        prov["witness"] = witness
    prov["source"] = source
    prov["locked_at"] = dt.date.fromordinal(locked_at.toordinal())
    if note:
        prov["note"] = f"{locked_at.isoformat()}: {note}"
    entry["provenance"] = prov


def _default_family_name(family_id: str) -> str:
    """A readable ``DeviceFamily.name`` when ``add device``'s one-submit path
    creates a family from only an id and no explicit ``--name``."""
    tail = family_id.removeprefix("fam.")
    readable = tail.replace(".", " ").replace("-", " ").replace("_", " ").strip()
    return readable.title() if readable else family_id


def _build_device(fields: dict[str, Any]) -> CommentedMap:
    for required in ("id", "model", "owner"):
        if not fields.get(required):
            raise EditRefusal(f"device requires --{required.replace('_', '-')}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["model"] = fields["model"]
    if fields.get("identity_bus_ref") or fields.get("identity_host"):
        if not (fields.get("identity_bus_ref") and fields.get("identity_host")):
            raise EditRefusal("device identity needs both --bus-ref and --host")
        entry["identity"] = CommentedMap(
            [("bus_ref", fields["identity_bus_ref"]), ("host", fields["identity_host"])]
        )
    if fields.get("physical_mass") or fields.get("physical_mount_frame"):
        # mass and mount_frame are independently optional — declare either alone.
        physical = CommentedMap()
        if fields.get("physical_mass"):
            physical["mass"] = parse_value(
                fields["physical_mass"], field_name="mass", flag="--mass"
            )
        if fields.get("physical_mount_frame"):
            physical["mount_frame"] = fields["physical_mount_frame"]
        entry["physical"] = physical
    if fields.get("power_rail") or fields.get("power_draw"):
        if fields.get("power_draw") and not fields.get("power_rail"):
            raise EditRefusal("device power draw needs --rail")
        power = CommentedMap()
        power["rail"] = fields["power_rail"]
        # draw is optional — a device may declare a rail with unknown draw.
        if fields.get("power_draw"):
            power["draw"] = parse_value(fields["power_draw"], field_name="draw", flag="--draw")
        entry["power"] = power
    entry["owner"] = fields["owner"]
    return entry


# electrical optional scalars that `add` copies through as given (None -> absent).
_RAIL_F1A = (
    "source_device",
    "regulator_kind",
    "dropout_v",
    "min_input_v",
    "capacitance_f",
    "accepts_regen",
    "thermal_resistance_k_per_w",
)
_BUS_F1A = ("phy", "length_m", "protocol", "twisted")
_BATTERY_F1A = ("cells_parallel", "reserve_pct", "r1_mohm", "c1_f", "regen_current_max_a")


def _copy_optional(entry: Any, fields: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if fields.get(key) is not None:
            entry[key] = parse_value(str(fields[key]), field_name=key)


def _build_rail(fields: dict[str, Any]) -> CommentedMap:
    for required in ("id", "capacity"):
        if not fields.get(required):
            raise EditRefusal(f"rail requires --{required}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    # voltage is optional — capacity alone still describes a rail.
    if fields.get("voltage"):
        entry["voltage"] = parse_value(fields["voltage"], field_name="voltage", flag="--voltage")
    entry["capacity"] = parse_value(fields["capacity"], field_name="capacity", flag="--capacity")
    if fields.get("upstream"):
        entry["upstream"] = fields["upstream"]
    if fields.get("efficiency"):
        entry["efficiency"] = parse_value(
            fields["efficiency"], field_name="efficiency", flag="--efficiency"
        )
    _copy_optional(entry, fields, _RAIL_F1A)
    return entry


def _build_bus(fields: dict[str, Any]) -> CommentedMap:
    for required in ("id", "kind", "host"):
        if not fields.get(required):
            raise EditRefusal(f"bus requires --{required}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["kind"] = fields["kind"]
    entry["host"] = fields["host"]
    if fields.get("bitrate"):
        entry["bitrate"] = parse_value(fields["bitrate"], field_name="bitrate", flag="--bitrate")
    _copy_optional(entry, fields, _BUS_F1A)
    return entry


def _build_wire(fields: dict[str, Any]) -> CommentedMap:
    # awg/installation are needed only for a power-rail wire; a pin-to-pin signal wire may omit them.
    for required in ("id", "owner") + (("awg", "installation") if fields.get("rail") else ()):
        if not fields.get(required):
            raise EditRefusal(f"wire requires --{required}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    if fields.get("awg"):
        try:
            entry["awg"] = int(fields["awg"])
        except (TypeError, ValueError):
            raise EditRefusal(f"wire --awg must be a whole number, not {fields['awg']!r}") from None
    if fields.get("installation"):
        entry["installation"] = fields["installation"]
    for key in ("rail", "net_class", "connector"):
        if fields.get(key):
            entry[key] = fields[key]
    _copy_optional(entry, fields, ("length_m",))
    for end in ("from", "to"):
        if fields.get(end):
            entry[end] = CommentedMap(fields[end])
    entry["owner"] = fields["owner"]
    return entry


def _build_protection(fields: dict[str, Any]) -> CommentedMap:
    # `kind` is taken by the add kind itself, so the guard's own kind (fuse, breaker, tvs, reverse_polarity) rides as `guard`.
    for required in ("id", "guard"):
        if not fields.get(required):
            raise EditRefusal(f"protection requires {required}")
    entry = CommentedMap(id=fields["id"], kind=fields["guard"])
    _copy_optional(entry, fields, ("rating_a", "curve", "i2t_a2s", "v_standoff_v", "v_clamp_v"))
    if fields.get("protects"):
        entry["protects"] = list(fields["protects"])
    return entry


def _build_belt(fields: dict[str, Any]) -> CommentedMap:
    for required in ("driver", "driven", "profile", "center_distance_m"):
        if fields.get(required) in (None, ""):
            raise EditRefusal(f"belt requires --{required.replace('_', '-')}")
    entry = CommentedMap(id=fields["id"])
    for end in ("driver", "driven"):
        entry[end] = CommentedMap(fields[end])
    entry["profile"] = fields["profile"]
    entry["center_distance_m"] = fields["center_distance_m"]
    entry.update({k: fields[k] for k in ("length_m", "teeth") if fields.get(k) is not None})
    return entry


def _build_family(fields: dict[str, Any]) -> CommentedMap:
    for required in ("id", "name"):
        if not fields.get(required):
            raise EditRefusal(f"family requires --{required}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["name"] = fields["name"]
    if fields.get("min_capture_window_s"):
        entry["min_capture_window_s"] = parse_value(
            fields["min_capture_window_s"],
            field_name="min_capture_window_s",
            flag="--min-capture-window-s",
        )
    if fields.get("notes"):
        entry["notes"] = fields["notes"]
    return entry


def _build_model(fields: dict[str, Any], doc: Any) -> CommentedMap:
    for required in ("id", "family"):
        if not fields.get(required):
            raise EditRefusal(f"model requires --{required}")
    family_ref = fields["family"]
    families = doc.get("families", []) or []
    if not any(isinstance(f, dict) and f.get("id") == family_ref for f in families):
        existing = ", ".join(
            sorted(f["id"] for f in families if isinstance(f, dict) and f.get("id"))
        )
        raise EditRefusal(
            f"family {family_ref!r} is not declared — declare it first: "
            "lockwire add family ... --id <fam.id> --name ... --source ... --witness ... "
            f"(or use an existing id: {existing or 'none declared yet'})"
        )
    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["family"] = family_ref
    input_min = fields.get("input_voltage_min")
    input_max = fields.get("input_voltage_max")
    if input_min or input_max:
        if not (input_min and input_max):
            raise EditRefusal(
                "model input voltage range needs both --input-voltage-min and --input-voltage-max"
            )
        entry["input_voltage_range"] = CommentedMap(
            [
                ("min", parse_value(input_min, field_name="min", flag="--input-voltage-min")),
                ("max", parse_value(input_max, field_name="max", flag="--input-voltage-max")),
            ]
        )
    return entry


def _build_budget(fields: dict[str, Any], doc: Any) -> CommentedMap:
    for required in ("id", "budget_kind", "limit", "owner"):
        if not fields.get(required):
            raise EditRefusal(f"budget requires --{required.replace('budget_', '')}")
    kind = fields["budget_kind"]
    if kind not in ("rail_power", "endurance", "mass_total"):
        raise EditRefusal(
            f"budget --kind must be one of rail_power|endurance|mass_total, got {kind!r}"
        )
    rail_ref = fields.get("rail")
    if kind == "rail_power":
        if not rail_ref:
            raise EditRefusal("budget --kind rail_power requires --rail")
        rails = (
            doc.get("topology", {}).get("rails", [])
            if isinstance(doc.get("topology"), dict)
            else []
        )
        if not any(isinstance(r, dict) and r.get("id") == rail_ref for r in rails):
            raise EditRefusal(f"budget references unknown rail {rail_ref!r}")
    elif rail_ref:
        raise EditRefusal(f"budget --kind {kind} takes no --rail")

    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["kind"] = kind
    entry["limit"] = parse_value(fields["limit"], field_name="limit", flag="--limit")
    if rail_ref:
        entry["rail"] = rail_ref
    if fields.get("draw_mode"):
        entry["draw_mode"] = fields["draw_mode"]
    if fields.get("sources"):
        entry["sources"] = fields["sources"]
    if fields.get("nominal"):
        entry["nominal"] = parse_value(fields["nominal"], field_name="nominal", flag="--nominal")
    if fields.get("tolerance_pct"):
        entry["tolerance_pct"] = float(fields["tolerance_pct"])
    entry["owner"] = fields["owner"]
    return entry


def _build_gearbox(fields: dict[str, Any], doc: Any) -> CommentedMap:
    for required in ("id", "ratio", "input_joint", "output_joint"):
        if fields.get(required) in (None, ""):
            raise EditRefusal(f"gearbox requires --{required.replace('_', '-')}")
    joints = doc.get("model", {}).get("joints", []) if isinstance(doc.get("model"), dict) else []
    joint_names = {j.get("name") for j in joints if isinstance(j, dict)}
    for role in ("input_joint", "output_joint"):
        ref = fields[role]
        if ref not in joint_names:
            raise EditRefusal(f"gearbox {role} references unknown joint {ref!r}")
    try:
        ratio = float(fields["ratio"])
    except (TypeError, ValueError) as error:
        raise EditRefusal(f"gearbox --ratio must be a number, got {fields['ratio']!r}") from error
    if ratio <= 0:
        raise EditRefusal("gearbox --ratio must be > 0")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    entry["ratio"] = ratio
    entry["input_joint"] = fields["input_joint"]
    entry["output_joint"] = fields["output_joint"]
    return entry


# ------------------------------------------------------- pose edits (move/rotate/reparent)
#
# The "editable minor CAD changes". Each one is a FILE operation on
# the contract, never a mutation of a live scene object: `scene.ScenePart` is a
# frozen dataclass of baked world-space vertices with no pose, no parent and no
# mesh reference (measured, `scene.py:38-48`), so the scene is a render product
# and the contract is the thing that can be edited. The scene is rebuilt from the
# contract afterwards by the code that already builds it.
#
# All three go through `apply_changes` — the same writer `lockwire declare` calls
# — so source, witness and provenance are stamped by one gate, and a
# refusal raised here happens before `apply_changes` opens the file at all, which
# is what leaves a refused edit byte-identical.

#: Accepted rotation units and their factor to radians. `JointOrigin.rpy` is a
#: bare radian triple (URDF-native), so radians is what gets written.
#: `units.py` is deliberately NOT the table here: its `angle` dimension is based
#: on degrees and carries no `rad` entry, and adding one would change every angle
#: comparison in the codebase for the sake of a field that stores no unit at all.
_ROTATION_UNITS = {"rad": 1.0, "deg": math.pi / 180.0}


def _require_unit(unit: str | None, accepted: tuple[str, ...], *, what: str) -> str:
    """Refuse a missing or wrong-dimension unit rather than guessing one.

    The joint-limit rule read across to pose: a number without its unit is
    refused, never defaulted. Defaulting is the failure this exists to prevent —
    ``--rpy 0,0,90`` meant in degrees and silently read as radians is a 90-radian
    rotation that no later check can tell from an intended one.
    """
    if unit is None or not unit.strip():
        raise EditRefusal(
            f"--unit is required for {what}: give one of {', '.join(accepted)}. "
            "A bare number has no meaning without it and lockwire does not guess."
        )
    if unit.strip() not in accepted:
        raise EditRefusal(
            f"--unit {unit.strip()!r} is not a {what} unit; give one of {', '.join(accepted)}"
        )
    return unit.strip()


def _length_units() -> tuple[str, ...]:
    """Every length unit `units.py` knows, so the two never drift apart."""
    return tuple(u for u in ("m", "cm", "mm") if units.known(u))


def _link_tree(contract: Any) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """ONE walk of the recorded model: child->parent, child->joint name, link names.

    Every pose refusal below reads this one map. A second tree walk that
    disagreed with this one is exactly how a cycle would get into the file,
    and a cycle in the file makes `checks._fixed_frame_transforms` return no
    transform for the whole subtree — the CG composition would go quiet rather
    than wrong, and nothing else in the repo would notice.

    ponytail: a link named as the child of two joints keeps the last one seen.
    The schema forbids duplicate joint *names*, not duplicate children, so that
    model is already outside the tree these operations claim to edit; the
    root/orphan check below refuses it in practice by failing to reach the root.
    """
    if contract.model is None:
        raise EditRefusal(
            "this contract records no model, so there is no link tree to edit — "
            "run `lockwire start <urdf>` or `lockwire init` against a model first"
        )
    parent_of: dict[str, str] = {}
    joint_of: dict[str, str] = {}
    for joint in contract.model.joints:
        parent_of[joint.child] = joint.parent
        joint_of[joint.child] = joint.name
    return parent_of, joint_of, {link.name for link in contract.model.links}


def _chain_to_root(parent_of: dict[str, str], link: str) -> list[str]:
    """``link`` then every ancestor above it, nearest first, ending at its root.

    Stops if it meets a link twice, so a cycle already present in the file makes
    this terminate rather than hang.
    """
    chain, seen = [link], {link}
    while link in parent_of:
        link = parent_of[link]
        if link in seen:
            break
        chain.append(link)
        seen.add(link)
    return chain


def _resolve_pose_target(contract: Any, target: str) -> tuple[str, str]:
    """``("device", id)`` or ``("link", name)`` — what ``target`` names, or a refusal."""
    if any(device.id == target for device in contract.devices):
        return "device", target
    if contract.model is not None and any(link.name == target for link in contract.model.links):
        return "link", target
    raise EditRefusal(
        f"{target!r} is neither a declared device id nor a link in the recorded model; "
        "a pose edit addresses one of those two"
    )


def _joint_placing(contract: Any, link: str, *, verb: str) -> str:
    """The joint whose child is ``link`` — the joint that places it in its parent."""
    _parent_of, joint_of, _links = _link_tree(contract)
    if link not in joint_of:
        raise EditRefusal(
            f"link {link!r} is the model root: no joint makes it a child, so there is "
            f"no recorded pose to {verb}. Its frame is the model's own origin."
        )
    return joint_of[link]


def _pose_change(
    path: str,
    value: Any,
    *,
    source: str,
    witness: str,
    note: str | None,
    locked_at: dt.date | None,
    unit: str | None = None,
) -> Change:
    return Change(
        path=path,
        value=value,
        source=source,
        witness=witness,
        note=note,
        locked_at=locked_at,
        unit=unit,
    )


@_locked_write
def place(
    contract_path: Path,
    target: str,
    on: str,
    offset: tuple[float, float, float],
    *,
    unit: str | None,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Place an existing device on a model link in one atomic contract write."""
    contract = load_contract(contract_path)
    kind, name = _resolve_pose_target(contract, target)
    if kind != "device":
        raise EditRefusal(
            f"{name!r} is a model link and already has a placing joint; "
            "use move, rotate or reparent to edit it"
        )
    _parent_of, _joint_of, links = _link_tree(contract)
    if on not in links:
        raise EditRefusal(
            f"placement parent {on!r} does not resolve: the recorded model has no link by that name"
        )
    unit = _require_unit(unit, _length_units(), what="an offset")
    return apply_changes(
        contract_path,
        [
            _pose_change(
                f"devices/{name}/physical/mount_frame",
                f'"{on}"',
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            ),
            _pose_change(
                f"devices/{name}/physical/mount_xyz",
                [float(v) for v in offset],
                unit=unit,
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            ),
        ],
        dry_run=dry_run,
        expected_revision=expected_revision,
    )


@_locked_write
def set_mount_shape(
    contract_path: Path,
    target: str,
    shape: dict[str, Any],
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Write one device mount shape through the same atomic gate as declare."""
    contract = load_contract(contract_path)
    kind, name = _resolve_pose_target(contract, target)
    if kind != "device":
        raise EditRefusal(f"{name!r} is a model link; mount_shape belongs to device physical")
    return apply_changes(
        contract_path,
        [
            _pose_change(
                f"devices/{name}/physical/mount_shape",
                shape,
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            )
        ],
        dry_run=dry_run,
        expected_revision=expected_revision,
    )


@_locked_write
def move(
    contract_path: Path,
    target: str,
    offset: tuple[float, float, float],
    *,
    unit: str | None,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Set where ``target`` sits in the frame that already holds it.

    A device moves in its ``mount_frame`` (``devices/<id>/physical/mount_xyz``);
    a link moves in its parent link's frame, which is its placing joint's origin
    (``model/joints/<joint>/origin/xyz``, meters, URDF-native).
    """
    contract = load_contract(contract_path)
    kind, name = _resolve_pose_target(contract, target)
    unit = _require_unit(unit, _length_units(), what="an offset")
    if kind == "device":
        change = _pose_change(
            f"devices/{name}/physical/mount_xyz",
            [float(v) for v in offset],
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
            unit=unit,
        )
    else:
        joint = _joint_placing(contract, name, verb="move")
        change = _pose_change(
            f"model/joints/{joint}/origin/xyz",
            [units.convert(float(v), unit, "m") for v in offset],
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
        )
    return apply_changes(
        contract_path, [change], dry_run=dry_run, expected_revision=expected_revision
    )


@_locked_write
def rotate(
    contract_path: Path,
    target: str,
    rpy: tuple[float, float, float],
    *,
    unit: str | None,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Set ``target``'s orientation in the frame that already holds it.

    Worth stating plainly: **a device has no recorded orientation.**
    ``schema.Physical`` carries mass, placement and mount shape, but no orientation. The only rotation
    the contract records is a joint's ``origin.rpy``, so ``rotate`` addresses a
    link, and a device target is refused in words that say why rather than
    inventing a field.
    """
    contract = load_contract(contract_path)
    kind, name = _resolve_pose_target(contract, target)
    unit = _require_unit(unit, tuple(_ROTATION_UNITS), what="a rotation")
    if kind == "device":
        raise EditRefusal(
            f"{name!r} is a device, and the contract records no orientation for a device: "
            "`physical` carries mass, placement and mount shape, but no orientation. "
            "Rotate the link it is mounted to instead, and if a device really "
            "needs its own orientation that is a schema change (open an issue to propose it)."
        )
    joint = _joint_placing(contract, name, verb="rotate")
    factor = _ROTATION_UNITS[unit]
    change = _pose_change(
        f"model/joints/{joint}/origin/rpy",
        [float(v) * factor for v in rpy],
        source=source,
        witness=witness,
        note=note,
        locked_at=locked_at,
    )
    return apply_changes(
        contract_path, [change], dry_run=dry_run, expected_revision=expected_revision
    )


@_locked_write
def reparent(
    contract_path: Path,
    target: str,
    to: str,
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Hang ``target`` off ``to`` instead of whatever holds it now.

    A device changes the link it is mounted to (``physical/mount_frame``); a link
    changes its placing joint's ``parent``, which moves the link tree and
    therefore ``structure_hash`` — that move is PRINTED, never silent, because a
    silent one makes every later model import look like drift
    (``urdf.describe_structure_drift``).

    Three refusals, each naming which one it is rather than failing generically:
    a parent that does not resolve, a re-parent that closes a CYCLE (``to`` is
    ``target`` or sits below it), and one that would ORPHAN ``target``'s subtree
    (``to`` is not connected to the root ``target`` currently hangs from).
    """
    contract = load_contract(contract_path)
    kind, name = _resolve_pose_target(contract, target)
    parent_of, joint_of, links = _link_tree(contract)
    if to not in links:
        raise EditRefusal(
            f"parent {to!r} does not resolve: the recorded model has no link by that name "
            f"(it has {', '.join(sorted(links)) or 'no links'})"
        )
    if kind == "device":
        change = _pose_change(
            f"devices/{name}/physical/mount_frame",
            f'"{to}"',
            source=source,
            witness=witness,
            note=note,
            locked_at=locked_at,
        )
        return apply_changes(
            contract_path, [change], dry_run=dry_run, expected_revision=expected_revision
        )

    if to == name:
        raise EditRefusal(
            f"re-parenting link {name!r} onto itself closes a CYCLE in the link tree: "
            "a link cannot be its own parent"
        )
    chain = _chain_to_root(parent_of, to)
    if name in chain:
        below = " -> ".join(reversed(chain[: chain.index(name) + 1]))
        raise EditRefusal(
            f"re-parenting link {name!r} onto {to!r} closes a CYCLE in the link tree: "
            f"{to!r} already sits below {name!r} ({below}). A cycle in the link tree makes "
            "every later tree walk in the contract either stop early or report no transform "
            "at all, so it is refused rather than written"
        )
    anchor = _chain_to_root(parent_of, name)[-1]
    if chain[-1] != anchor:
        raise EditRefusal(
            f"re-parenting link {name!r} onto {to!r} would ORPHAN {name!r} and everything "
            f"below it: {to!r} is not connected to the model root {anchor!r} (it reaches "
            f"{chain[-1]!r} instead), so the moved subtree would hang off nothing"
        )

    joint = _joint_placing(contract, name, verb="re-parent")
    model = contract.model
    assert model is not None  # _link_tree already refused a contract with no model
    old_hash = model.structure_hash
    new_hash = _model_structure_hash(
        model.links,
        [j.model_copy(update={"parent": to}) if j.name == joint else j for j in model.joints],
    )
    result = apply_changes(
        contract_path,
        [
            _pose_change(
                f"model/joints/{joint}/parent",
                f'"{to}"',
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            ),
            _pose_change(
                "model/structure_hash",
                f'"{new_hash}"',
                source=source,
                witness=witness,
                note=note,
                locked_at=locked_at,
            ),
        ],
        dry_run=dry_run,
        expected_revision=expected_revision,
    )
    result.lines.append(
        f"structure hash moved: {old_hash} -> {new_hash} "
        f"(the link tree changed, so every later model import compares against the new one)"
    )
    return result
