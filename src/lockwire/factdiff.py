"""The fact diff: what changed in the facts, not in the text.

Two loaded contracts in, a diff document out. Nothing here reads git (that is
``gitref.py``) and nothing here prints (``cli.py`` does that) — this module
only compares.

The comparison is over the *validated* contract, not the YAML text, so a
reordered device list, a re-indented block or a rewritten comment is not a
change. Every declared scalar is flattened to a ``/``-separated path
(``topology/rails/rail.5v/capacity/value``), list rows keyed by their own
``id``/``name``/``context`` rather than by position.
"""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from lockwire.checks import run_record_checks
from lockwire.schema import Contract, load_contract

# Path segments whose value *is* a declared ceiling. A number growing under one
# of these is a limit being raised — the edit that makes a finding go away
# without the robot changing, so it is called out rather than listed as one
# more field change.
LIMIT_SEGMENTS = frozenset(
    {
        "capacity",
        "limit",
        "hard_limits",
        "current_limit",
        "effort",
        "velocity",
        "torque_peak",
        "torque_continuous",
        "fade_margin_required",
        "position_tracking_max_error",
        "feedback_deadline_max",
        "feedback_loss_max",
        "clock_alignment_max_error",
        "prediction_position_rmse_max",
        "prediction_position_peak_max",
    }
)

# The entity collections a person thinks in. An added or removed row here is
# reported once, by id — not as forty field changes underneath it.
ENTITY_KINDS = (
    "devices",
    "device_models",
    "families",
    "budgets",
    "assumptions",
    "rails",
    "buses",
    "wires",
    "links",
    "actuator_cases",
)


@dataclass(frozen=True)
class FieldChange:
    path: str
    old: Any  # None when the field was not declared before
    new: Any  # None when the field is no longer declared
    witness: str | None = None
    source: str | None = None
    limit_raised: bool = False


@dataclass(frozen=True)
class EntityChange:
    kind: str  # one of ENTITY_KINDS
    id: str


@dataclass(frozen=True)
class FindingRef:
    severity: str
    quantity: str


@dataclass
class FactDiff:
    changes: list[FieldChange] = field(default_factory=list)
    added: list[EntityChange] = field(default_factory=list)
    removed: list[EntityChange] = field(default_factory=list)
    findings_new: list[FindingRef] = field(default_factory=list)
    findings_gone: list[FindingRef] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes or self.added or self.removed)

    def as_json(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "changes": [
                {
                    "path": c.path,
                    "old": c.old,
                    "new": c.new,
                    "witness": c.witness,
                    "source": c.source,
                    "limit_raised": c.limit_raised,
                }
                for c in self.changes
            ],
            "added": [{"kind": e.kind, "id": e.id} for e in self.added],
            "removed": [{"kind": e.kind, "id": e.id} for e in self.removed],
            "findings_new": [
                {"severity": f.severity, "quantity": f.quantity} for f in self.findings_new
            ],
            "findings_gone": [
                {"severity": f.severity, "quantity": f.quantity} for f in self.findings_gone
            ],
        }


def _key_for(row: Any, index: int) -> str:
    if isinstance(row, dict):
        for field_name in ("id", "name", "context"):
            value = row.get(field_name)
            if isinstance(value, str) and value:
                return value
    return str(index)


def _flatten(node: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(value, f"{prefix}/{key}" if prefix else str(key), out)
    elif isinstance(node, list):
        for index, row in enumerate(node):
            _flatten(row, f"{prefix}/{_key_for(row, index)}", out)
    else:
        out[prefix] = node


def flatten(contract: Contract) -> dict[str, Any]:
    """Every declared scalar in the contract, by path."""
    out: dict[str, Any] = {}
    _flatten(contract.model_dump(mode="json", exclude_none=True), "", out)
    return out


def _entities(contract: Contract) -> dict[str, set[str]]:
    topology = contract.topology
    model = contract.model
    return {
        "devices": {row.id for row in contract.devices},
        "device_models": {row.id for row in contract.device_models},
        "families": {row.id for row in contract.families},
        "budgets": {row.id for row in contract.budgets},
        "assumptions": {row.id for row in contract.assumptions},
        "rails": {row.id for row in topology.rails} if topology else set(),
        "buses": {row.id for row in topology.buses} if topology else set(),
        "wires": {row.id for row in topology.wires} if topology else set(),
        "links": {row.name for row in model.links} if model else set(),
        "actuator_cases": {row.id for row in contract.actuator_cases},
    }


def _limit_raised(path: str, old: Any, new: Any) -> bool:
    segments = path.split("/")
    if segments[-1] == "lo":  # raising a floor narrows the band, it does not relax it
        return False
    if not LIMIT_SEGMENTS.intersection(segments):
        return False
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return False
    return bool(new > old)


def _provenance_for(path: str, flat: dict[str, Any]) -> tuple[str | None, str | None]:
    """Witness and source of the nearest enclosing entry that declares provenance."""
    segments = path.split("/")
    for cut in range(len(segments) - 1, 0, -1):
        stem = "/".join(segments[:cut])
        witness = flat.get(f"{stem}/provenance/witness")
        if witness is not None:
            return witness, flat.get(f"{stem}/provenance/source")
    return None, None


def _findings(contract: Contract) -> set[FindingRef]:
    return {FindingRef(f.severity, f.quantity) for f in run_record_checks(contract)}


def fact_diff(before: Contract, after: Contract) -> FactDiff:
    """Compare two contracts: declared values, entries, and the verdict delta."""
    diff = FactDiff()

    old_entities, new_entities = _entities(before), _entities(after)
    touched: set[str] = set()
    for kind in ENTITY_KINDS:
        for entity_id in sorted(new_entities[kind] - old_entities[kind]):
            diff.added.append(EntityChange(kind, entity_id))
            touched.add(entity_id)
        for entity_id in sorted(old_entities[kind] - new_entities[kind]):
            diff.removed.append(EntityChange(kind, entity_id))
            touched.add(entity_id)

    flat_old, flat_new = flatten(before), flatten(after)
    for path in sorted(set(flat_old) | set(flat_new)):
        old, new = flat_old.get(path), flat_new.get(path)
        if old == new:
            continue
        if touched.intersection(path.split("/")):
            continue  # already reported once, as an added/removed entry
        witness, source = _provenance_for(path, flat_new if new is not None else flat_old)
        diff.changes.append(
            FieldChange(path, old, new, witness, source, _limit_raised(path, old, new))
        )

    old_findings, new_findings = _findings(before), _findings(after)
    key = lambda finding: (finding.severity, finding.quantity)  # noqa: E731
    diff.findings_new = sorted(new_findings - old_findings, key=key)
    diff.findings_gone = sorted(old_findings - new_findings, key=key)
    return diff


def render_lines(diff: FactDiff) -> list[str]:
    """The fact diff as plain lines, in the order a person reads them."""
    if not diff.changed and not diff.findings_new and not diff.findings_gone:
        return ["no fact changes"]
    lines: list[str] = []
    for change in diff.changes:
        provenance = ""
        if change.witness or change.source:
            provenance = f"  [{change.witness or '?'} · {change.source or '?'}]"
        flag = "  limit raised" if change.limit_raised else ""
        lines.append(f"~ {change.path}: {change.old} -> {change.new}{provenance}{flag}")
    for entry in diff.added:
        lines.append(f"+ {entry.kind}/{entry.id}")
    for entry in diff.removed:
        lines.append(f"- {entry.kind}/{entry.id}")
    for finding in diff.findings_new:
        lines.append(f"! new {finding.severity}: {finding.quantity}")
    for finding in diff.findings_gone:
        lines.append(f"o gone {finding.severity}: {finding.quantity}")
    return lines


def load_contract_text(text: str) -> Contract:
    """Load a contract held in memory (the version git handed back)."""
    with NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        temp = Path(handle.name)
    try:
        return load_contract(temp)
    finally:
        temp.unlink(missing_ok=True)
