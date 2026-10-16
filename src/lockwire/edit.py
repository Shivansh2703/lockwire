"""``lockwire declare`` / ``lockwire apply`` / ``lockwire add`` — the edit primitive.

The YAML is the truth. Every edit lands as one declared fact carrying a
source and a witness, written through a ruamel round-trip load/modify/dump
so existing comments, key order and quoting style survive untouched outside the
edited lines. Nothing is written unless the edited document validates through
``Contract`` first — the same byte cap and anchor/alias refusal the rest of the
codebase applies to a lockfile apply here too.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import io
import json
import math
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, CommentedMap
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarfloat import ScalarFloat

from lockwire.schema import (
    Contract,
    MAX_CONTRACT_BYTES,
    _guard_contract_raw,
    _load_yaml_guarded,
    _model_structure_hash,
    _reject_yaml_anchors_aliases,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    UnsupportedSchemaVersion,
    load_contract,
)
from lockwire import _filelock, units
from lockwire.report import LIMIT_RAISED_MARK


class EditRefusal(ValueError):
    """A declare/apply/add refused before writing anything."""


class EditConflict(EditRefusal):
    """The bytes a caller edited are no longer the bytes on disk."""


class EditRecoveryError(OSError):
    """A failed edit left recovery bytes in a named artifact."""

    def __init__(
        self,
        recovery_artifact: Path,
        replace_error: BaseException,
        recovery_error: BaseException,
        *,
        contract_state: str | None = None,
        backup_state: str | None = None,
        recovery_action: str = "rollback",
    ):
        self.recovery_artifact = recovery_artifact
        self.contract_state = contract_state
        self.backup_state = backup_state
        self.recovery_action = recovery_action
        if contract_state is None:
            message = (
                f"contract replacement failed ({replace_error}); backup rollback failed "
                f"({recovery_error}); prior undo bytes are at {recovery_artifact}"
            )
        else:
            message = (
                f"contract replacement failed ({replace_error}); backup {recovery_action} failed "
                f"({recovery_error}); contract is {contract_state}; {backup_state} at "
                f"{recovery_artifact}"
            )
        super().__init__(message)


_locked_contracts: ContextVar[frozenset[Path]] = ContextVar("locked_contracts", default=frozenset())


def contract_revision(path: Path | str) -> str:
    """Opaque revision for the exact contract bytes, not its semantic hash."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def _contract_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating writers with a sibling advisory lock."""
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _filelock.lock(fd)
    except TimeoutError:
        os.close(fd)
        raise EditConflict("contract is locked by another lockwire process; retry") from None
    try:
        yield
    finally:
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
_NUM = r"-?\d+(?:\.\d+)?"
_SINGLE_RE = re.compile(rf"^({_NUM})\s*({_UNIT_CHARS})$")
_TRIPLE_RE = re.compile(rf"^\[?\s*({_NUM})[,\s]+({_NUM})[,\s]+({_NUM})\s*\]?\s+({_UNIT_CHARS})$")


def _flow_quantity(value: float, unit: str) -> CommentedMap:
    """A `{value, unit}` quantity written flow-style, matching every one in the fixtures."""
    q = CommentedMap([("value", value), ("unit", unit)])
    q.fa.set_flow_style()
    return q


def parse_value(raw: str, *, field_name: str) -> Any:
    """Parse a `declare`/`apply` VALUE string into a YAML-writable python value.

    Grammar: a bare number -> number; "25 W" -> {value, unit}; a triple
    ("0.12 0 0.05 m" or "[0.12, 0, 0.05] m") -> a 3-tuple of quantities, only
    for ``mount_xyz``; true/false -> bool; a quoted string -> string; anything
    else is kept as a plain string.
    """
    text = raw.strip()
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
        return float(text)
    except ValueError:
        pass
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


def _stamp_provenance(
    doc: Any,
    owner_segments: list[str],
    *,
    field_leaf: str,
    source: str,
    witness: str,
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
    stamp = f"{locked_at.isoformat()}: {field_leaf} declared, witness {witness}"
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
    witness: str,
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
    stamp = f"{locked_at.isoformat()}: {field_leaf} declared, witness {witness}, source {source}"
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


def _stamp_limit_raised(doc: Any, budget_id: str, *, witness: str, locked_at: dt.date) -> bool:
    """Append the standing mark to the budget's provenance note. No new field:
    `Provenance.note` is a schema field and the mark is text in it."""
    for entry in doc.get("budgets") or []:
        if not isinstance(entry, dict) or entry.get("id") != budget_id:
            continue
        prov = entry.get("provenance")
        if not isinstance(prov, dict):
            return False
        mark = f"{LIMIT_RAISED_MARK}{locked_at.isoformat()}, witness {witness}"
        old_note = prov.get("note")
        prov["note"] = f"{old_note}\n{mark}" if old_note else mark
        return True
    return False


@dataclass
class Change:
    path: str
    value: Any  # raw VALUE string, or an already-structured python value
    source: str
    witness: str
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
    style = _sniff_sequence_style(text)
    doc = _load_yaml_guarded(_yaml(*style), io.StringIO(text))
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
            prov = CommentedMap()
            prov["witness"] = change.witness
            prov["source"] = change.source
            prov["locked_at"] = locked_at
            if change.note:
                prov["note"] = f"{locked_at.isoformat()}: {change.note}"
            entry["provenance"] = prov
        collection.append(entry)
        added.append(str(entry["id"]))
    return f"added {collection_key} {', '.join(added)} (witness {change.witness}, source {change.source})"


_ASSISTANT_NAME_DENYLIST = frozenset(
    {"claude", "gpt", "chatgpt", "codex", "copilot", "gemini", "assistant", "ai"}
)


def _is_inside_git_repo(path: Path) -> bool:
    """Walk up from ``path``'s parent looking for a ``.git`` dir or file.

    No subprocess (cheaper than ``git rev-parse``); a ``.git`` file covers
    worktrees and submodules, a ``.git`` dir covers the ordinary case.
    """
    for parent in (path.resolve().parent, *path.resolve().parent.parents):
        if (parent / ".git").exists():
            return True
    return False


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


def _replace_then_backup(tmp_path: Path, contract_path: Path, original_text: str) -> None:
    """Pair a contract replacement with its non-git undo baseline."""
    if _is_inside_git_repo(contract_path):
        os.replace(tmp_path, contract_path)
        return

    backup_path = contract_path.with_name(contract_path.name + ".bak")
    previous_backup = backup_path.read_bytes() if backup_path.exists() else None
    staged_backup = _backup_temp(backup_path, original_text.encode("utf-8"))
    try:
        # Keep the old undo bytes on disk before changing either destination.
        restore_backup = (
            _backup_temp(backup_path, previous_backup) if previous_backup is not None else None
        )
        retain_restore = False
        try:
            # Install a complete prospective baseline before the contract replacement.
            # If that replacement fails, restore the exact prior backup under the same lock.
            os.replace(staged_backup, backup_path)
            try:
                os.replace(tmp_path, contract_path)
            except BaseException as replace_error:
                try:
                    if restore_backup is None:
                        backup_path.unlink(missing_ok=True)
                    else:
                        os.replace(restore_backup, backup_path)
                except BaseException as recovery_error:
                    # The prior backup remains a complete sibling file even
                    # when the destination cannot be restored right now.
                    if restore_backup is not None:
                        retain_restore = True
                        raise EditRecoveryError(
                            restore_backup, replace_error, recovery_error
                        ) from recovery_error
                    raise EditRecoveryError(
                        backup_path,
                        replace_error,
                        recovery_error,
                        contract_state="unchanged",
                        backup_state=(
                            "the prospective .bak contains the contract's pre-write bytes"
                        ),
                        recovery_action="cleanup",
                    ) from recovery_error
                raise
        finally:
            if restore_backup is not None and not retain_restore:
                restore_backup.unlink(missing_ok=True)
    finally:
        staged_backup.unlink(missing_ok=True)


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
        import tomllib

        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    value = data.get("witness")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


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
    validate_source(change.source, prefix=f"entry {index} ({change.path}): ")
    for where, written in iter_written_sources(change.path, change.value):
        validate_source(written, prefix=f"entry {index} ({where}): ")
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
    return f"declared {change.path} = {format_value(value)} (witness {change.witness}, source {change.source})"


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
            original_text.splitlines(keepends=True),
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
) -> EditResult:
    """Apply every change to the contract, validate once, write atomically.

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
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(proposal.text)
        _replace_then_backup(tmp_path, contract_path, proposal.original_text)
        proposal.result.written = True
        return proposal.result
    finally:
        tmp_path.unlink(missing_ok=True)


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
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:
            raise
        except Exception as error:
            raise EditRefusal(f"review of {path}: {error}") from error
        diff = "".join(
            difflib.unified_diff(
                original_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        line = f"reviewed {path}: {state} (witness {witness})"
        if dry_run:
            return EditResult(diff=diff, lines=[line], written=False)
        _replace_then_backup(tmp_path, contract_path, original_text)
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
                    return f"entry {index} ({change.path}): {pydantic_error.get('msg', error)}"
    return f"contract invalid after {len(changes)} change(s): {error}"


# ------------------------------------------------- add device|rail|bus|family|model|budget|gearbox

_ADD_KINDS = ("device", "rail", "bus", "family", "model", "budget", "battery", "gearbox")
_COLLECTION_APPEND_KINDS = ("families", "device_models", "budgets")


@_locked_write
def add_entry(
    contract_path: Path,
    kind: str,
    fields: dict[str, Any],
    *,
    source: str,
    witness: str,
    note: str | None = None,
    locked_at: dt.date | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> EditResult:
    """Create a new device/rail/bus block, with provenance, atomically."""
    if kind not in _ADD_KINDS:
        raise EditRefusal(f"unknown add kind {kind!r}; expected one of {_ADD_KINDS}")
    validate_source(source)
    validate_witness(witness, refusal=f"--witness must name a real person, not {witness!r}")

    original_text, doc, style = _load_doc_guarded(contract_path)
    locked_at = locked_at or dt.date.today()

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
                model_entry = _build_model({"id": model_ref, "family": family_ref}, doc)
                models.append(model_entry)
                created_with_device.append(("model", model_entry))
        collection = _ensure_list(doc, ["devices"])
        _refuse_duplicate(collection, new_id, "device")
        entry = _build_device(fields)
    elif kind in ("rail", "bus"):
        list_key = "rails" if kind == "rail" else "buses"
        collection = _ensure_list(doc, ["topology", list_key])
        _refuse_duplicate(collection, new_id, kind)
        entry = _build_rail(fields) if kind == "rail" else _build_bus(fields)
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
    lines.append(f"added {kind} {new_id} (witness {witness}, source {source})")

    new_text = _dump(doc, style)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:  # in the file before the edit; see apply_changes
            raise
        except Exception as error:
            raise EditRefusal(f"add {kind} {new_id!r} invalid: {error}") from error

        diff = "".join(
            difflib.unified_diff(
                original_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        if dry_run:
            return EditResult(diff=diff, lines=lines, written=False)
        _replace_then_backup(tmp_path, contract_path, original_text)
        return EditResult(diff=diff, lines=lines, written=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _add_battery_entry(
    contract_path: Path,
    doc: Any,
    fields: dict[str, Any],
    *,
    original_text: str,
    style: tuple[int, int],
    source: str,
    witness: str,
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
    battery["capacity"] = parse_value(capacity, field_name="capacity")
    if fields.get("nominal_voltage"):
        battery["voltage"] = parse_value(fields["nominal_voltage"], field_name="voltage")

    stamp = f"{locked_at.isoformat()}: battery declared, witness {witness}, source {source}"
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
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            load_contract(tmp_path)
        except UnsupportedSchemaVersion:  # in the file before the edit; see apply_changes
            raise
        except Exception as error:
            raise EditRefusal(f"battery invalid: {error}") from error

        diff = "".join(
            difflib.unified_diff(
                original_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(contract_path),
                tofile=str(contract_path),
            )
        )
        line = f"declared battery capacity (witness {witness}, source {source})"
        if dry_run:
            return EditResult(diff=diff, lines=[line], written=False)
        _replace_then_backup(tmp_path, contract_path, original_text)
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
    entry: CommentedMap, *, source: str, witness: str, locked_at: dt.date, note: str | None = None
) -> None:
    """The one shared provenance stamp every ``add``-written record goes
    through (mutation bite: stamping per record with duplicated inline code
    instead of this one function is exactly the bug its test catches).

    A one-submit device add calls this 2-3 times with the SAME ``locked_at``
    object; ruamel anchors/aliases a repeated node by identity, and this repo
    refuses anchors on read (``_reject_yaml_anchors_aliases``) — so each call
    gets its own fresh ``date``, never the caller's shared one.
    """
    prov = CommentedMap()
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
            physical["mass"] = parse_value(fields["physical_mass"], field_name="mass")
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
            power["draw"] = parse_value(fields["power_draw"], field_name="draw")
        entry["power"] = power
    entry["owner"] = fields["owner"]
    return entry


def _build_rail(fields: dict[str, Any]) -> CommentedMap:
    for required in ("id", "capacity"):
        if not fields.get(required):
            raise EditRefusal(f"rail requires --{required}")
    entry = CommentedMap()
    entry["id"] = fields["id"]
    # voltage is optional — capacity alone still describes a rail.
    if fields.get("voltage"):
        entry["voltage"] = parse_value(fields["voltage"], field_name="voltage")
    entry["capacity"] = parse_value(fields["capacity"], field_name="capacity")
    if fields.get("upstream"):
        entry["upstream"] = fields["upstream"]
    if fields.get("efficiency"):
        entry["efficiency"] = parse_value(fields["efficiency"], field_name="efficiency")
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
        entry["bitrate"] = parse_value(fields["bitrate"], field_name="bitrate")
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
            fields["min_capture_window_s"], field_name="min_capture_window_s"
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
                ("min", parse_value(input_min, field_name="min")),
                ("max", parse_value(input_max, field_name="max")),
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
    entry["limit"] = parse_value(fields["limit"], field_name="limit")
    if rail_ref:
        entry["rail"] = rail_ref
    if fields.get("draw_mode"):
        entry["draw_mode"] = fields["draw_mode"]
    if fields.get("sources"):
        entry["sources"] = fields["sources"]
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
