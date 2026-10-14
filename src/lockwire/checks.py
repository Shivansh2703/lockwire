"""Record-level checks (rung 1): do the records agree with each other?

No robot required — these run in CI on every change to the contract or its
witnesses. Every check emits the one grammar (report.Finding). The
false-positive budget is a first-class requirement: a clean contract must
produce zero findings.
"""

import math
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from lockwire import plugins, units
from lockwire.advice import build_rail_alternatives
from lockwire.catalog import SourcedNumber, load_catalog
from lockwire.edit import resolve_source, source_is_absolute
from lockwire.report import (
    CGContributor,
    CGReport,
    Finding,
    RecordReport,
    ScoredRecord,
    format_magnitude,
)
from lockwire.mechanism import NAMED_SET, Mechanism, analyse_model
from lockwire.scene import Scene
from lockwire.scene_view import resolve_scene
from lockwire.schema import (
    ArtifactRef,
    Band,
    Contract,
    ModelRecord,
    Quantity,
    Provenance,
    Tolerance,
)
from lockwire.sdf import read_sdf_topology
from lockwire.urdf import describe_structure_drift, read_urdf_topology

# the YAML is the truth. This is only how far the model in the scene is
# allowed to disagree with it before the disagreement is a finding, not a
# silent picture.
PLACEMENT_TOLERANCE_M = 0.005

# Raw bus bitrate is not usable payload throughput: arbitration, framing, retries,
# and latency collapse make sustained operation unsafe well before 100% load.
BUS_UTILIZATION_CEILING = 0.60


def unknown_draw_message(device_id: str, rail_id: str) -> str:
    """one phrasing for "this consumer's draw is undeclared", reused by
    every surface (terminal, page, engineer report, describe) so the same gap
    reads the same way everywhere."""
    return f"{device_id} draws an unknown amount from {rail_id} — declare its draw"


# Presence-floor disclosure when no record-check comparison had both operands
# Severity-neutral: stub contracts stay exit 0, never a pass.
EMPTY_SCORE_OBSERVED = "nothing was scored: no record-check comparison had both operands populated"
EMPTY_SCORE_QUANTITY = "check/scored"


class _ScoreBook:
    """Declarations for which a check compared two independent populated operands.

    The presence-floor denominator is ``len(self)`` — reported by the checks
    themselves at the moment both operands are confirmed, not inferred from
    how many devices/rails/budgets the contract declares.
    """

    def __init__(self) -> None:
        self._keys: set[str] = set()
        self.records: list[ScoredRecord] = []

    def record(self, key: str) -> None:
        self._keys.add(key)

    def passed(
        self,
        quantity: str,
        observed: str,
        expected: str,
        margin: str,
        *,
        observed_quantity: Quantity | None = None,
        expected_quantity: Quantity | None = None,
        provenance: Provenance | None = None,
    ) -> None:
        """Record a PASSED comparison alongside ``record`` (same key, same call site).

        Only call this on the branch where the comparison did NOT produce a
        violation/warning finding — one scored comparison yields exactly one
        of {violation, warning, ScoredRecord} so the presence-floor identity
        (``len(scored_records) + violations + warnings == scored``) holds.

        ``observed_quantity``/``expected_quantity`` are optional typed magnitudes
        (mirrors ``Finding``): a caller with real compared numbers (a preflight
        band/limit check) can pass them so a reader can show the numbers without
        re-deriving them; record checks leave them unset.
        """
        self.records.append(
            ScoredRecord(
                quantity=quantity,
                observed=observed,
                expected=expected,
                margin=margin,
                observed_quantity=observed_quantity,
                expected_quantity=expected_quantity,
                provenance=provenance,
            )
        )

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def _empty_score_disclosure() -> Finding:
    return Finding(
        severity="not_scored",
        quantity=EMPTY_SCORE_QUANTITY,
        observed=EMPTY_SCORE_OBSERVED,
        expected="at least one record-check comparison with both operands populated",
        action=(
            "add a comparable pair (e.g. envelope vs hard limit, budget limit vs "
            "witness sum, rail voltage vs upstream, joint effort vs actuator) "
            "so check has something to compare"
        ),
    )


# Check slots filled by the paid package (open-core split). Keys:
# "actuator_cases", "condition_envelopes". An empty slot never passes silently.
PAID_CHECKS: dict[str, Callable[..., Any]] = {}


def _paid_check_missing(quantity: str, what: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=quantity,
        observed="not run: needs Lockwire Pro",
        expected=f"{what} checked by Lockwire Pro",
        action=f"install Lockwire Pro to check {what}, or remove them from the contract",
    )


def run_record_checks(
    contract: Contract,
    current_conditions: dict[str, float] | None = None,
    *,
    contract_path: Path | None = None,
    current_edge_values: dict[str, float] | None = None,
    actuator_evidence: Mapping[str, ArtifactRef] | None = None,
    evidence_root: Path | None = None,
    contract_sha256: str | None = None,
) -> RecordReport:
    """Return judgements and severity-free derived information in separate lanes.

    ``contract_path`` is the contract's own file, when the caller has it: it
    anchors a relative source to the contract's directory, so without it a
    relative URDF path stays unresolvable and drift stays ``not_scored``.

    ``current_edge_values`` is the same API-path gap as ``current_conditions``
    below: an edge's tolerance needs a live reading to score drift against,
    the CLI ``check`` command does not supply one today, and callers that do
    (a recording, a live sample) get real findings without inflating the
    ``_ScoreBook`` denominator for a run that never had one.
    """
    plugins.ensure()
    scores = _ScoreBook()
    findings: list[Finding] = []
    findings += check_device_owners(contract)
    findings += check_compared_from(contract)
    findings += check_source_portability(contract)
    findings += check_reference_integrity(contract)
    findings += check_edge_tolerance(contract, current_edge_values, scores=scores)
    findings += check_bus_identities(contract)
    findings += check_electrical_segments(contract, scores=scores)
    findings += check_rail_topology(contract, scores=scores)
    findings += check_voltage_compatibility(contract, scores=scores)
    findings += check_budgets(contract, scores=scores)
    findings += check_ampacity(contract, scores=scores)
    findings += check_rail_current(contract, scores=scores)
    findings += check_torque_budgets(contract, scores=scores)
    findings += check_bus_bandwidth(contract, scores=scores)
    findings += check_zero_mass(contract)
    findings += check_unusable_effort(contract)
    findings += check_mechanisms(contract, scores=scores)
    findings += check_urdf_drift(contract, scores=scores, contract_path=contract_path)
    findings += check_envelopes_vs_hard_limits(contract, scores=scores)
    if current_conditions is not None:
        # No caller of the free path passes conditions today, so an empty slot
        # here cannot be reached from `lockwire check`; skip silently.
        if run := PAID_CHECKS.get("condition_envelopes"):
            findings += run(contract, current_conditions)
    findings += check_thermal_budget(contract, scores=scores)
    findings += check_link_budget(contract, scores=scores)
    actuator_case_reports: list[Any] = []
    if contract.actuator_cases:
        if run := PAID_CHECKS.get("actuator_cases"):
            actuator_findings, actuator_case_reports = run(
                contract,
                actuator_evidence=actuator_evidence,
                evidence_root=evidence_root,
                contract_sha256=contract_sha256,
            )
            findings += actuator_findings
        else:
            findings.append(_paid_check_missing("actuator_cases/conformance", "actuator cases"))
    scored = len(scores)
    if scored == 0:
        findings.append(_empty_score_disclosure())
    return RecordReport(
        findings,
        compute_system_cg(contract),
        scored=scored,
        scored_keys=scores.keys,
        scored_records=scores.records,
        actuator_case_reports=actuator_case_reports,
    )


def _is_accountable_owner(owner: str) -> bool:
    normalized = owner.strip()
    return normalized.casefold() != "unresolved" and any(
        character.isalnum() for character in normalized
    )


def check_device_owners(c: Contract) -> list[Finding]:
    """A device contract must name who is accountable for resolving its assertions."""
    return [
        Finding(
            severity="violation",
            quantity=f"devices/{device.id}/owner",
            observed=repr(device.owner),
            expected="an accountable owner",
            action="name the human or team accountable for this device",
        )
        for device in c.devices
        if not _is_accountable_owner(device.owner)
    ]


# ---------------------------------------------------------------- compared-from


def _catalog_part_mass(ref: str) -> tuple[str, SourcedNumber] | None:
    """Return a concrete catalog mass record with its identity.

    This narrow path intentionally accepts only a concrete catalog-part id.
    A family id has no single source mass, so it remains an explicit unresolved
    reference rather than choosing or inventing a representative value.
    """
    for part in load_catalog().parts:
        if part.id != ref:
            continue
        mass = part.numbers.get("mass")
        if mass is None:
            return None
        return f"catalog part {part.id}", mass
    return None


def check_compared_from(c: Contract) -> list[Finding]:
    """Check a device mass copied from one concrete seeded catalog part.

    ``Provenance.compared_from`` records the identity of the copied source; it
    never substitutes a catalog value for the device's own declaration. Only
    a catalog part with a declared mass is resolvable here. Device and family
    references are deliberately outside this catalog-part slice.
    """
    findings: list[Finding] = []
    for device in c.devices:
        provenance = device.provenance
        if provenance is None or provenance.compared_from is None:
            continue
        ref = provenance.compared_from
        quantity = f"devices/{device.id}/provenance/compared_from"
        source = _catalog_part_mass(ref)
        if source is None:
            findings.append(
                Finding(
                    severity="warning",
                    quantity=quantity,
                    observed=f"compared_from {ref!r}",
                    expected="a seeded catalog part id with a declared mass",
                    action=(
                        f"replace {ref!r} with the concrete catalog part whose mass was copied; "
                        "family references are not resolved by this check"
                    ),
                )
            )
            continue
        source_label, catalog_mass = source
        source_kind = catalog_mass.kind
        source_citation = catalog_mass.source
        if source_kind == "assumed":
            findings.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=(
                        f"{source_label} mass is kind={source_kind}; source={source_citation}"
                    ),
                    expected="a catalog mass marked datasheet or source_file",
                    action=(
                        "replace the assumed catalog mass with a sourced value; this check refuses "
                        "to compare a declared device mass against a seed assumption"
                    ),
                )
            )
            continue
        declared_mass = device.physical.mass if device.physical is not None else None
        if declared_mass is None:
            findings.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=f"copied from {source_label} (kind={source_kind}; source={source_citation})",
                    expected="a declared device physical mass to compare",
                    action="declare the device's physical mass; this check will not copy one for it",
                )
            )
            continue
        source_mass = Quantity(value=catalog_mass.value, unit=catalog_mass.unit)
        if math.isclose(declared_mass.base(), source_mass.base(), rel_tol=1e-9):
            continue
        findings.append(
            Finding(
                severity="warning",
                quantity=quantity,
                observed=f"{declared_mass} (declared device mass)",
                expected=(
                    f"{source_mass} ({source_label}'s current mass; "
                    f"kind={source_kind}; source={source_citation})"
                ),
                action=(
                    f"re-confirm {device.id}'s mass against {source_label} or update the declared copy"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------- source portability


def _iter_source_fields(node: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every ``source`` string in the contract tree, with its '/'-separated path.

    Generic on purpose: ``Provenance.source`` and ``ModelRecord.source`` are the
    two today, and a third one added later is covered without another walker.
    """
    if isinstance(node, BaseModel):
        value = getattr(node, "source", None)
        if isinstance(value, str):
            yield (f"{path}/source" if path else "source"), value
        for name in type(node).model_fields:
            yield from _iter_source_fields(getattr(node, name), f"{path}/{name}" if path else name)
    elif isinstance(node, list):
        for position, item in enumerate(node):
            key = getattr(item, "id", None) or getattr(item, "name", None) or position
            yield from _iter_source_fields(item, f"{path}/{key}")
    elif isinstance(node, dict):
        for key, item in node.items():
            yield from _iter_source_fields(item, f"{path}/{key}")


def check_source_portability(c: Contract) -> list[Finding]:
    """a source is a file beside the contract, a URL or a cited title.

    Write paths refuse an absolute path outright (``edit.validate_source``); a
    contract that already carries one predates that gate, so it is reported here
    rather than crashing or passing silently. A warning, not a violation: the
    physics still agrees, it is the evidence that cannot travel.
    """
    return [
        Finding(
            severity="warning",
            quantity=quantity,
            observed=repr(source),
            expected="a path relative to the contract, a URL or a cited title",
            source=source,
            action="check the evidence in beside the contract and re-declare this "
            "fact with the relative path",
        )
        for quantity, source in _iter_source_fields(c, "")
        if source_is_absolute(source)
    ]


# ---------------------------------------------------------------- references


def check_reference_integrity(c: Contract) -> list[Finding]:
    """Every cross-entry reference must resolve. A dangling ref is a violation:
    it means the contract describes a robot that doesn't exist in the records."""
    out: list[Finding] = []
    hosts = {host.id for host in c.topology.hosts} if c.topology is not None else set()
    buses = {bus.id for bus in c.topology.buses} if c.topology is not None else set()
    links = {link.name for link in c.model.links} if c.model is not None else set()
    joints = {joint.name for joint in c.model.joints} if c.model is not None else set()

    for d in c.devices:
        if c.device_model(d.model) is None:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{d.id}/model",
                    observed=repr(d.model),
                    expected="an id in device_models",
                    dependent=", ".join(d.depends) or None,
                    owner=d.owner,
                    action=f"add device model {d.model!r} or fix the reference",
                )
            )
        if d.power is not None and c.rail(d.power.rail) is None:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{d.id}/power/rail",
                    observed=repr(d.power.rail),
                    expected="a rail id in topology",
                    owner=d.owner,
                    action=f"declare rail {d.power.rail!r} in topology or fix the reference",
                )
            )
        if d.identity is not None:
            if d.identity.bus_ref not in buses:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"devices/{d.id}/identity/bus_ref",
                        observed=repr(d.identity.bus_ref),
                        expected="a bus id in topology",
                        owner=d.owner,
                        action=(
                            f"declare bus {d.identity.bus_ref!r} in topology or fix the reference"
                        ),
                    )
                )
            if d.identity.host not in hosts:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"devices/{d.id}/identity/host",
                        observed=repr(d.identity.host),
                        expected="a host id in topology",
                        owner=d.owner,
                        action=f"declare host {d.identity.host!r} in topology or fix the reference",
                    )
                )
        if d.physical is not None and d.physical.mount_frame is not None:
            if c.model is None:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"devices/{d.id}/physical/mount_frame",
                        observed=repr(d.physical.mount_frame),
                        expected="a model link once a model is recorded",
                        owner=d.owner,
                        action="record the model before relying on mount-frame integrity",
                    )
                )
            elif d.physical.mount_frame not in links:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"devices/{d.id}/physical/mount_frame",
                        observed=repr(d.physical.mount_frame),
                        expected="a link name in model/links",
                        owner=d.owner,
                        action="fix the mount frame or add the missing link to the model record",
                    )
                )
        if d.drives_joint is not None and d.drives_joint not in joints:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{d.id}/drives_joint",
                    observed=repr(d.drives_joint),
                    expected="a joint name in model/joints",
                    owner=d.owner,
                    action="fix the driven-joint reference or record the missing joint",
                )
            )

    for m in c.device_models:
        if c.family(m.family) is None:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"device_models/{m.id}/family",
                    observed=repr(m.family),
                    expected="an id in families",
                    action=f"add family {m.family!r} or fix the reference",
                )
            )

    if c.topology is not None:
        for bus in c.topology.buses:
            if bus.host not in hosts:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/buses/{bus.id}/host",
                        observed=repr(bus.host),
                        expected="a host id in topology",
                        action=f"declare host {bus.host!r} in topology or fix the bus reference",
                    )
                )
        for rail in c.topology.rails:
            if rail.charged_by is not None and c.device(rail.charged_by) is None:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/rails/{rail.id}/charged_by",
                        observed=repr(rail.charged_by),
                        expected="a device id in devices",
                        action=(
                            f"declare device {rail.charged_by!r} or fix the charged_by reference"
                        ),
                    )
                )
            if rail.upstream is None:
                continue
            if rail.upstream == "battery":
                if c.topology.battery is None:
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=f"topology/rails/{rail.id}/upstream",
                            observed="'battery'",
                            expected="a declared topology battery",
                            action="declare the battery or fix the rail's upstream reference",
                        )
                    )
            elif c.rail(rail.upstream) is None:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/rails/{rail.id}/upstream",
                        observed=repr(rail.upstream),
                        expected="a rail id in topology or 'battery'",
                        action=f"declare rail {rail.upstream!r} or fix the upstream reference",
                    )
                )

    for e in c.edges:
        try:
            c.resolve(e.quantity)
        except KeyError:
            out.append(
                Finding(
                    severity="violation",
                    quantity=e.quantity,
                    observed="unresolvable path",
                    expected="a contract path that resolves",
                    dependent=", ".join(e.code),
                    owner=e.owner,
                    action="fix the edge's quantity path — the code it guards is now unguarded",
                )
            )

    for b in c.budgets:
        if b.kind == "rail_power" and b.rail is not None and c.rail(b.rail) is None:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"budgets/{b.id}/rail",
                    observed=repr(b.rail),
                    expected="a rail id in topology",
                    owner=b.owner,
                    action=f"declare rail {b.rail!r} in topology or fix the budget",
                )
            )

    if c.topology is not None:
        for wire in c.topology.wires:
            if c.rail(wire.rail) is None:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/wires/{wire.id}/rail",
                        observed=repr(wire.rail),
                        expected="a rail id in topology",
                        owner=wire.owner,
                        action=f"declare rail {wire.rail!r} in topology or fix the wire",
                    )
                )

    return out


def _resolved_magnitude(value: Any) -> tuple[float, str] | None:
    """A resolved contract path as ``(numeric value, unit-or-'')``, or ``None``
    when it resolved to something a tolerance cannot bound (a string, a list,
    a bare dict with no numeric ``value``). Never guessed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value), ""
    if (
        isinstance(value, dict)
        and isinstance(value.get("value"), (int, float))
        and not isinstance(value.get("value"), bool)
        and isinstance(value.get("unit"), str)
    ):
        return float(value["value"]), value["unit"]
    return None


def _edge_tolerance_str(tolerance: Tolerance) -> str:
    if tolerance.rel_pct is not None:
        return f"{tolerance.rel_pct:g}%"
    assert tolerance.abs is not None  # Tolerance's own validator requires exactly one
    return format_magnitude(tolerance.abs.value, tolerance.abs.unit)


def check_edge_tolerance(
    c: Contract,
    current_edge_values: dict[str, float] | None = None,
    *,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """Score an edge's declared tolerance against a live current reading.

    ``DependencyEdge.tolerance`` bounds how far ``e.quantity`` may drift from
    the contract's own declared value before the code in ``e.code`` should
    stop trusting it. Scoring that drift needs two independently populated
    operands — the contract's declared value (``c.resolve(e.quantity)``) and
    a live/current reading — and ``DependencyEdge`` carries no second,
    distinct field to diff against (its fields are exactly
    quantity/tolerance/code/owner). Inventing one would mean touching
    ``schema.py``, which stays frozen; this
    check follows ``check_condition_envelopes``'s own precedent instead — a
    caller-supplied mapping of fresh readings keyed by the same path,
    threaded through ``run_record_checks`` exactly like ``current_conditions``.
    The CLI ``check`` command does not populate it today (the same
    API-path gap that check documents for itself), so absent a supplied
    reading this stays a disclosed ``not_scored`` coverage gap — never a
    silent pass and never a guessed comparison.

    A dangling quantity path stays ``check_reference_integrity``'s violation,
    not this check's (skipped here to avoid a duplicate finding on the same
    edge). A declared value this check cannot bind a number to (absent,
    non-numeric) is ``not_scored``, generalizing the existing idiom for
    ``Physical.mass``.

    Both operands present and the drift falls inside the tolerance band:
    recorded via ``scores`` and silent (the false-positive budget) — this is
    a real two-operand comparison, unlike a structural presence check, so it
    now belongs in the ``_ScoreBook`` denominator.
    """
    out: list[Finding] = []
    for e in c.edges:
        try:
            declared = c.resolve(e.quantity)
        except KeyError:
            continue

        declared_magnitude = _resolved_magnitude(declared)
        if declared_magnitude is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=e.quantity,
                    observed=(
                        "no declared value at this path"
                        if declared is None
                        else f"declared value {declared!r} is not a magnitude a tolerance can bound"
                    ),
                    expected=(
                        f"a numeric declared value to score the "
                        f"{_edge_tolerance_str(e.tolerance)} tolerance against"
                    ),
                    dependent=", ".join(e.code),
                    owner=e.owner,
                    action=(
                        f"declare a value at {e.quantity}, or drop the edge if "
                        f"{', '.join(e.code)} no longer depends on it"
                    ),
                )
            )
            continue

        declared_value, unit = declared_magnitude
        if current_edge_values is None or e.quantity not in current_edge_values:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=e.quantity,
                    observed="no current reading supplied to score against the declared value",
                    expected=(
                        f"a current reading for {e.quantity} to score the "
                        f"{_edge_tolerance_str(e.tolerance)} tolerance against"
                    ),
                    dependent=", ".join(e.code),
                    owner=e.owner,
                    action=(
                        "supply a current reading (a recording or a live sample) "
                        "via current_edge_values, or the tolerance stays unhonoured"
                    ),
                )
            )
            continue

        current_value = current_edge_values[e.quantity]
        if not math.isfinite(current_value):  # NaN/inf is never a pass
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=e.quantity,
                    observed=f"current reading {current_value} is not a finite number",
                    expected="a finite current reading",
                    dependent=", ".join(e.code),
                    owner=e.owner,
                    action="supply a finite current reading",
                )
            )
            continue
        if e.tolerance.rel_pct is not None:
            allowed = abs(declared_value) * (e.tolerance.rel_pct / 100.0)
        else:
            assert e.tolerance.abs is not None
            try:
                allowed = (
                    units.convert(e.tolerance.abs.value, e.tolerance.abs.unit, unit)
                    if unit
                    else e.tolerance.abs.value
                )
            except (KeyError, ValueError):
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=e.quantity,
                        observed=(
                            f"declared unit {unit!r} is not compatible with the "
                            f"tolerance's unit {e.tolerance.abs.unit!r}"
                        ),
                        expected="a tolerance unit convertible to the declared value's unit",
                        dependent=", ".join(e.code),
                        owner=e.owner,
                        action="fix the tolerance's unit or the declared value's unit",
                    )
                )
                continue

        delta = abs(current_value - declared_value)
        if delta > allowed:
            out.append(
                Finding(
                    severity="violation",
                    quantity=e.quantity,
                    observed=(
                        f"current {format_magnitude(current_value, unit)} vs "
                        f"declared {format_magnitude(declared_value, unit)}"
                    ),
                    expected=f"within {_edge_tolerance_str(e.tolerance)} of the declared value",
                    observed_quantity=Quantity(value=current_value, unit=unit) if unit else None,
                    expected_quantity=Quantity(value=declared_value, unit=unit) if unit else None,
                    delta=format_magnitude(delta, unit),
                    dependent=", ".join(e.code),
                    owner=e.owner,
                    action=(
                        f"re-verify {', '.join(e.code)} against the drifted value, "
                        "or update the declared value"
                    ),
                )
            )
        elif scores is not None:
            scores.record(e.quantity)

    return out


# ---------------------------------------------------------------- identities


def check_electrical_segments(c: Contract, *, scores: _ScoreBook) -> list[Finding]:
    """Bound only explicitly sourced logic, one unbuffered I2C segment and CAN wiring."""
    if c.topology is None:
        return []
    out: list[Finding] = []

    def finding(
        severity: str,
        quantity: str,
        observed: str,
        expected: str,
        provenance: Provenance | None = None,
    ) -> None:
        out.append(
            Finding(
                severity=severity,
                quantity=quantity,
                observed=observed,
                expected=expected,
                source=provenance.source if provenance else None,
                witness=provenance.witness if provenance else None,
                action="supply sourced electrical operands or correct the declared segment",
            )
        )

    for pair in c.topology.logic_pairs:
        prefix = f"topology/logic_pairs/{pair.id}"
        values = (
            pair.operating_supply_min,
            pair.operating_supply_max,
            pair.voh_min,
            pair.vol_max,
            pair.vih_min,
            pair.vil_max,
        )
        if (
            pair.provenance is None
            or not pair.provenance.source.strip()
            or not pair.provenance.witness.strip()
            or any(value is None for value in values)
        ):
            finding(
                "not_scored",
                prefix,
                "source or operating-envelope logic limit missing",
                "sourced supply and driver/receiver VOH, VOL, VIH, VIL limits",
            )
            continue
        assert all(value is not None for value in values)
        supply_min, supply_max, voh, vol, vih, vil = values
        if (
            any(not math.isfinite(value.base()) for value in values)
            or supply_min.base() <= 0
            or supply_min.base() > supply_max.base()
        ):
            finding(
                "not_scored",
                prefix,
                "invalid operating supply envelope",
                "positive ordered minimum and maximum supply",
                pair.provenance,
            )
            continue
        for name, margin, observed, expected in (
            ("high_margin", voh.base() - vih.base(), voh, vih),
            ("low_margin", vil.base() - vol.base(), vil, vol),
        ):
            quantity = f"{prefix}/{name}"
            scores.record(quantity)
            if margin < 0:
                finding(
                    "violation", quantity, f"{margin:g} V margin", ">= 0 V margin", pair.provenance
                )
            else:
                scores.passed(
                    quantity,
                    str(observed),
                    str(expected),
                    f"{margin:g} V margin at declared supply envelope",
                    provenance=pair.provenance,
                )

    for bus in c.topology.buses:
        if bus.i2c_segment is not None:
            segment = bus.i2c_segment
            prefix = f"topology/buses/{bus.id}/i2c_segment"
            values = (
                segment.vdd_min_v,
                segment.vdd_max_v,
                segment.pullup_min_ohm,
                segment.pullup_max_ohm,
                segment.capacitance_max_pf,
                segment.rise_time_max_ns,
                segment.sink_current_min_ma,
                segment.vol_max_v,
            )
            if (
                bus.kind != "i2c"
                or segment.unbuffered is not True
                or segment.mode not in {"Standard", "Fast", "FastPlus"}
                or segment.provenance is None
                or not segment.provenance.source.strip()
                or not segment.provenance.witness.strip()
                or any(value is None for value in values)
            ):
                finding(
                    "not_scored",
                    prefix,
                    "buffered, unsupported or missing segment operands",
                    "sourced unbuffered Standard/Fast/FastPlus segment with all bounds",
                )
                continue
            assert all(value is not None for value in values)
            vmin, vmax, rmin, rmax, capacitance, rise_max, sink_ma, vol_max = values
            if (
                any(not math.isfinite(value) for value in values)
                or vmin <= 0
                or vmax < vmin
                or rmin <= 0
                or rmax < rmin
                or capacitance < 0
                or rise_max <= 0
                or sink_ma <= 0
                or vol_max < 0
                or vol_max >= vmin
            ):
                finding(
                    "not_scored",
                    prefix,
                    "invalid segment operating envelope",
                    "positive ordered sourced limits",
                    segment.provenance,
                )
                continue
            rise_ns = 0.8473 * rmax * capacitance / 1000
            mode_max_ns = {"Standard": 1000, "Fast": 300, "FastPlus": 120}[segment.mode]
            mode_max_pf = {"Standard": 400, "Fast": 400, "FastPlus": 550}[segment.mode]
            rise_ceiling_ns = min(rise_max, mode_max_ns)
            required_rmin = (vmax - vol_max) * 1000 / sink_ma
            for name, observed, expected, good in (
                ("rise_time", rise_ns, rise_ceiling_ns, rise_ns <= rise_ceiling_ns),
                ("capacitance", capacitance, mode_max_pf, capacitance <= mode_max_pf),
                ("pullup_sink", rmin, required_rmin, rmin >= required_rmin),
            ):
                quantity = f"{prefix}/{name}"
                scores.record(quantity)
                if good:
                    scores.passed(
                        quantity,
                        f"{observed:g}",
                        f"{expected:g}",
                        "bounded unbuffered segment only",
                        provenance=segment.provenance,
                    )
                else:
                    finding(
                        "violation", quantity, f"{observed:g}", f"{expected:g}", segment.provenance
                    )
        if bus.can_segment is not None:
            segment = bus.can_segment
            prefix = f"topology/buses/{bus.id}/can_segment"
            if (
                bus.kind != "can"
                or segment.linear is not True
                or segment.provenance is None
                or not segment.provenance.source.strip()
                or not segment.provenance.witness.strip()
                or len(segment.nodes) < 2
                or any(not node.strip() for node in segment.nodes)
                or len(segment.nodes) != len(set(segment.nodes))
                or len(segment.termination_nodes) != 2
                or len(segment.termination_ohm) != 2
                or segment.cable_impedance_ohm is None
                or segment.impedance_tolerance_ohm is None
            ):
                finding(
                    "not_scored",
                    prefix,
                    "unsourced, nonlinear or incomplete CAN segment",
                    "sourced linear ordered nodes and two end terminations",
                )
                continue
            impedance = segment.cable_impedance_ohm
            tolerance = segment.impedance_tolerance_ohm
            if (
                any(
                    not math.isfinite(value)
                    for value in (impedance, tolerance, *segment.termination_ohm)
                )
                or impedance <= 0
                or tolerance < 0
                or any(value <= 0 for value in segment.termination_ohm)
            ):
                finding(
                    "not_scored",
                    prefix,
                    "invalid impedance envelope",
                    "positive cable/termination resistance and nonnegative tolerance",
                    segment.provenance,
                )
                continue
            quantity = f"{prefix}/termination"
            scores.record(quantity)
            ends = [segment.nodes[0], segment.nodes[-1]]
            good = segment.termination_nodes == ends and all(
                abs(value - impedance) <= tolerance for value in segment.termination_ohm
            )
            if good:
                scores.passed(
                    quantity,
                    f"two ends at {segment.termination_ohm} ohm",
                    f"{impedance:g} +/- {tolerance:g} ohm at {ends}",
                    "linear termination only; CAN timing unscored",
                    provenance=segment.provenance,
                )
            else:
                finding(
                    "violation",
                    quantity,
                    f"terminations {segment.termination_nodes} at {segment.termination_ohm} ohm",
                    f"ends {ends} at {impedance:g} +/- {tolerance:g} ohm",
                    segment.provenance,
                )
            finding(
                "not_scored",
                f"{prefix}/timing",
                "CAN timing not evaluated",
                "sourced propagation and transceiver timing evidence",
                segment.provenance,
            )
    return out


def check_bus_identities(c: Contract) -> list[Finding]:
    """Bus-local IDs must be unique, and duplicate serials cannot prove instance
    identity. These checks prevent addressing collisions and clone USB devices
    from being mistaken for distinct physical units."""
    out: list[Finding] = []
    devices_with_identity = [device for device in c.devices if device.identity is not None]

    for field in ("can_id", "address"):
        groups: dict[tuple[str, str], list] = {}
        for device in devices_with_identity:
            value = getattr(device.identity, field)
            if value is not None:
                groups.setdefault((device.identity.bus_ref, value), []).append(device)
        for (bus_ref, value), devices in groups.items():
            if len(devices) < 2:
                continue
            ids = ", ".join(device.id for device in devices)
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/buses/{bus_ref}/identity/{field}",
                    observed=f"value {value!r} shared by {len(devices)} devices: {ids}",
                    expected=f"one device per {field} on a bus",
                    dependent=ids,
                    owner=", ".join(sorted({device.owner for device in devices})),
                    action=f"assign unique {field} values or correct the bus wiring record",
                )
            )

    serials: dict[str, list] = {}
    for device in devices_with_identity:
        if device.identity.serial_number is not None:
            serials.setdefault(device.identity.serial_number, []).append(device)
    for serial, devices in serials.items():
        if len(devices) < 2:
            continue
        for device in devices:
            if device.identity.effective_strength() == "instance":
                device.identity.strength = "model"
        ids = ", ".join(device.id for device in devices)
        out.append(
            Finding(
                severity="warning",
                quantity="devices/identity/serial_number",
                observed=f"serial {serial!r} duplicated on {ids}",
                expected="a unique per-unit serial for instance-strength identity",
                delta="identity strength downgraded to model",
                dependent=ids,
                owner=", ".join(sorted({device.owner for device in devices})),
                action="replace clone serials or add a genuinely per-unit identity descriptor",
            )
        )

    return out


# ---------------------------------------------------------------- rail topology


def check_rail_topology(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Rail ancestry must be acyclic and voltage cannot rise through an
    unmodeled converter. Otherwise rollups can hang or bless a wiring typo."""
    if c.topology is None:
        return []

    out: list[Finding] = []
    reported_cycles: set[frozenset[str]] = set()
    for start in c.topology.rails:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while True:
            if current.id in positions:
                cycle = path[positions[current.id] :] + [current.id]
                key = frozenset(cycle)
                if key not in reported_cycles:
                    reported_cycles.add(key)
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=f"topology/rails/{current.id}/upstream",
                            observed=" -> ".join(cycle),
                            expected="an acyclic rail path ending at battery or no upstream",
                            dependent=", ".join(cycle[:-1]),
                            action="break the rail upstream cycle so power rollup terminates",
                        )
                    )
                break
            positions[current.id] = len(path)
            path.append(current.id)
            if current.upstream in (None, "battery"):
                break
            parent = c.rail(current.upstream)
            if parent is None:
                break  # reference integrity reports the missing rail
            current = parent

    for rail in c.topology.rails:
        if rail.voltage is None:
            # no declared voltage means no upstream/derived comparison is
            # possible — never treat it as 0 V.
            if rail.voltage_range is not None:
                observed = f"{rail.id} declares a voltage range but no nominal voltage"
                expected = "a declared nominal rail voltage for the upstream comparison"
            else:
                observed = f"{rail.id} has no declared voltage"
                expected = "a declared rail voltage"
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/voltage",
                    observed=observed,
                    expected=expected,
                    action=(
                        "declare the rail's voltage, or accept upstream/derived voltage "
                        "checks as not scored"
                    ),
                )
            )
            continue
        upstream_voltage = None
        upstream_name = rail.upstream
        if rail.upstream == "battery" and c.topology.battery is not None:
            upstream_voltage = c.topology.battery.voltage
        elif rail.upstream is not None:
            parent = c.rail(rail.upstream)
            if parent is not None:
                upstream_voltage = parent.voltage
        if upstream_voltage is None:
            continue
        child_v = rail.voltage.base()
        parent_v = upstream_voltage.base()

        # A rail declaring a charging source is not bounded by its battery: an
        # alternator holds the bus ABOVE the pack in order to charge it.
        # Such a rail is scored against the charging source's rated output
        # instead — still two independently authored records.
        charge_rating: Band | None = None
        charger = c.device(rail.charged_by) if rail.charged_by is not None else None
        if charger is not None:
            charger_model = c.device_model(charger.model)
            if charger_model is not None:
                charge_rating = charger_model.hard_limits.get("regulated_voltage")

        if rail.charged_by is not None:
            if charger is None:
                continue  # reference integrity reports the dangling charged_by
            if charge_rating is None:
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"topology/rails/{rail.id}/voltage",
                        observed=(
                            f"charged by {rail.charged_by!r}, which declares no "
                            f"regulated_voltage rating"
                        ),
                        expected="a regulated_voltage hard limit on the charging device's model",
                        dependent=rail.charged_by,
                        action=(
                            f"record the rated output of {rail.charged_by!r} as a "
                            f"'regulated_voltage' hard limit so the rail can be scored"
                        ),
                    )
                )
                continue
            lo = units.to_base(charge_rating.lo, charge_rating.unit)
            hi = units.to_base(charge_rating.hi, charge_rating.unit)
            if scores is not None:
                scores.record(f"rails/{rail.id}")
            if not (lo <= child_v <= hi):
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/rails/{rail.id}/voltage",
                        observed=format_magnitude(child_v, "V"),
                        expected=(
                            f"within {format_magnitude(lo, 'V')}..{format_magnitude(hi, 'V')} "
                            f"rated output of {rail.charged_by}"
                        ),
                        observed_quantity=Quantity(value=child_v, unit="V"),
                        delta=f"{'+' if child_v > hi else '-'}"
                        f"{format_magnitude(abs(child_v - (hi if child_v > hi else lo)), 'V')}",
                        dependent=rail.charged_by,
                        action=(
                            "fix the rail voltage record or the charging source's rated output"
                        ),
                    )
                )
            elif scores is not None:
                headroom_v = min(child_v - lo, hi - child_v)
                scores.passed(
                    f"topology/rails/{rail.id}/voltage",
                    observed=format_magnitude(child_v, "V"),
                    expected=f"within {format_magnitude(lo, 'V')}..{format_magnitude(hi, 'V')}",
                    margin=(
                        f"{format_magnitude(headroom_v, 'V')} inside the rated output "
                        f"of {rail.charged_by}"
                    ),
                )
            continue

        # Both rail voltage and upstream voltage populated — the two-source comparison.
        if scores is not None:
            scores.record(f"rails/{rail.id}")
        efficiency_warning = (
            not math.isclose(child_v, parent_v, rel_tol=1e-9)
            and "efficiency" not in rail.model_fields_set
        )
        if efficiency_warning:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"topology/rails/{rail.id}/efficiency",
                    observed="default 1.0 on a voltage-converting rail",
                    expected="an explicitly sourced converter efficiency",
                    dependent=upstream_name,
                    action="record the converter efficiency so parent load is not understated",
                )
            )
        if child_v > parent_v:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/rails/{rail.id}/voltage",
                    observed=format_magnitude(child_v, "V"),
                    expected=f"<= {format_magnitude(parent_v, 'V')} from {upstream_name}",
                    observed_quantity=Quantity(value=child_v, unit="V"),
                    expected_quantity=Quantity(value=parent_v, unit="V"),
                    delta=f"+{format_magnitude(child_v - parent_v, 'V')}",
                    dependent=upstream_name,
                    action=(
                        "fix the voltage record or model the missing step-up converter explicitly"
                    ),
                )
            )
        elif scores is not None and not efficiency_warning:
            # Only when nothing else fired for this scored unit (identity):
            # an efficiency-default warning on the SAME comparison would otherwise
            # double-count against len(scored_records)+violations+warnings==scored.
            headroom_v = parent_v - child_v
            scores.passed(
                f"topology/rails/{rail.id}/voltage",
                observed=format_magnitude(child_v, "V"),
                expected=f"<= {format_magnitude(parent_v, 'V')} from {upstream_name}",
                margin=f"{format_magnitude(headroom_v, 'V')} below {upstream_name}",
            )

    return out


# ---------------------------------------------------------------- budgets


_Matrix3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]
_Transform = tuple[_Matrix3, tuple[float, float, float]]
_IDENTITY: _Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _matmul(left: _Matrix3, right: _Matrix3) -> _Matrix3:
    """Compose rotations explicitly so CG needs no robotics/math dependency."""
    return tuple(
        tuple(sum(left[row][k] * right[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _matvec(matrix: _Matrix3, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply a recorded frame rotation to a point or translation."""
    return tuple(sum(matrix[row][col] * vector[col] for col in range(3)) for row in range(3))  # type: ignore[return-value]


def _add(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Keep vector arithmetic named at the frame-composition call sites."""
    return tuple(left[i] + right[i] for i in range(3))  # type: ignore[return-value]


def _rpy_rotation(rpy: tuple[float, float, float]) -> _Matrix3:
    """Convert URDF fixed-joint roll/pitch/yaw to its parent-frame rotation."""
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _fixed_frame_transforms(
    c: Contract,
) -> tuple[str | None, dict[str, _Transform], list[str]]:
    """Resolve only configuration-independent paths to a single model root.

    An articulated joint intentionally stops traversal: using its recorded
    origin as a reference configuration would manufacture a configuration that
    the contract does not declare.
    """
    if c.model is None:
        return None, {}, ["no model is recorded, so mount frames cannot be composed"]

    links = {link.name for link in c.model.links}
    child_counts: dict[str, int] = {}
    joints_by_child = {}
    for joint in c.model.joints:
        child_counts[joint.child] = child_counts.get(joint.child, 0) + 1
        joints_by_child[joint.child] = joint
    roots = sorted(links - set(joints_by_child))
    if len(roots) != 1:
        return (
            None,
            {},
            [f"model has {len(roots)} roots; no single frame exists for a system CG"],
        )

    root = roots[0]
    transforms: dict[str, _Transform] = {root: (_IDENTITY, (0.0, 0.0, 0.0))}
    resolving: set[str] = set()

    def resolve(frame: str) -> _Transform | None:
        if frame in transforms:
            return transforms[frame]
        joint = joints_by_child.get(frame)
        if (
            joint is None
            or joint.type != "fixed"
            or child_counts.get(frame) != 1
            or frame in resolving
        ):
            return None
        resolving.add(frame)
        parent = resolve(joint.parent)
        resolving.discard(frame)
        if parent is None:
            return None
        parent_rotation, parent_translation = parent
        joint_rotation = _rpy_rotation(joint.origin.rpy)
        transform = (
            _matmul(parent_rotation, joint_rotation),
            _add(parent_translation, _matvec(parent_rotation, joint.origin.xyz)),
        )
        transforms[frame] = transform
        return transform

    for link in links:
        resolve(link)
    limitations = []
    if any(joint.type != "fixed" for joint in c.model.joints):
        limitations.append(
            "articulated joint paths are configuration-dependent and are not composed "
            "without a recorded reference configuration"
        )
    return root, transforms, limitations


def _point_in_root(
    frame: str,
    point: tuple[float, float, float],
    transforms: dict[str, _Transform],
) -> tuple[float, float, float] | None:
    """Place a local point in the root only when its rigid transform is known."""
    transform = transforms.get(frame)
    if transform is None:
        return None
    rotation, translation = transform
    return _add(translation, _matvec(rotation, point))


def compute_system_cg(c: Contract) -> list[CGReport]:
    """Compute severity-free CG reports using each mass budget's source policy.

    Reusing ``Budget.sources`` makes inclusion (and intentional double counting)
    exactly match the established mass-total convention. A device without a
    recorded ``Physical.mount_xyz`` remains honestly unpositioned.
    """
    root, transforms, frame_limitations = _fixed_frame_transforms(c)
    reports: list[CGReport] = []

    for budget in c.budgets:
        if budget.kind != "mass_total":
            continue
        contributors: list[CGContributor] = []
        limitations = list(frame_limitations)

        if budget.sources in ("devices", "links+devices"):
            mounted_devices = [device for device in c.devices if device.physical is not None]
            for device in mounted_devices:
                physical = device.physical
                assert physical is not None
                if physical.mass is None:
                    # undeclared mass is a coverage gap, never a 0 kg contributor.
                    limitations.append(f"device {device.id!r} has no declared mass")
                    continue
                mass = physical.mass.base()
                if physical.mount_frame is None:
                    # a mass with no mount frame still counts toward the
                    # total (it is a mass) but is excluded from CG, disclosed.
                    position = None
                    limitations.append(
                        f"device {device.id!r} has a mass but no mount frame — CG excludes it"
                    )
                elif physical.mount_xyz is None:
                    position = None
                    limitations.append(
                        f"device {device.id!r} has no mount_xyz offset in {physical.mount_frame!r}"
                    )
                else:
                    offset_m = tuple(component.base() for component in physical.mount_xyz)
                    position = _point_in_root(physical.mount_frame, offset_m, transforms)
                moment = (
                    tuple(mass * coordinate for coordinate in position)
                    if position is not None
                    else None
                )
                contributors.append(
                    CGContributor(
                        quantity=f"devices/{device.id}/physical/mass",
                        mass_kg=mass,
                        position_m=position,
                        moment_kgm=moment,
                    )
                )

        if budget.sources in ("links", "links+devices") and c.model is not None:
            for link in c.model.links:
                if link.mass is None:
                    continue
                mass = link.mass.base()
                position = None
                if link.com is None:
                    limitations.append(f"link {link.name!r} has mass but no COM")
                else:
                    position = _point_in_root(link.frame or link.name, link.com, transforms)
                moment = (
                    tuple(mass * coordinate for coordinate in position)
                    if position is not None
                    else None
                )
                contributors.append(
                    CGContributor(
                        quantity=f"model/links/{link.name}/mass",
                        mass_kg=mass,
                        position_m=position,
                        moment_kgm=moment,
                    )
                )

        total_mass = sum(item.mass_kg for item in contributors)
        positioned = [item for item in contributors if item.position_m is not None]
        positioned_mass = sum(item.mass_kg for item in positioned)
        cg = None
        if positioned_mass > 0.0:
            cg = tuple(
                sum((item.moment_kgm or (0.0, 0.0, 0.0))[axis] for item in positioned)
                / positioned_mass
                for axis in range(3)
            )
        complete = bool(contributors) and len(positioned) == len(contributors)
        if not contributors:
            limitations.append("the selected source policy has no mass contributors")
        elif not complete:
            limitations.append(
                "system CG is incomplete because one or more contributor positions "
                "cannot be resolved"
            )

        def dominance(item: CGContributor) -> tuple[bool, float, float]:
            moment = item.moment_kgm
            magnitude = (
                math.sqrt(sum(component * component for component in moment))
                if moment is not None
                else 0.0
            )
            return item.position_m is None, magnitude, item.mass_kg

        contributors.sort(key=dominance, reverse=True)
        reports.append(
            CGReport(
                quantity=f"budgets/{budget.id}/cg",
                sources=budget.sources,
                frame=root,
                cg_m=cg,
                complete=complete,
                total_mass_kg=total_mass,
                positioned_mass_kg=positioned_mass,
                contributors=contributors,
                limitation="; ".join(dict.fromkeys(limitations)) or None,
                budget_limit_kg=budget.limit.base(),
            )
        )

    return reports


def check_voltage_compatibility(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Refuse a device supplied outside its model's declared input range.

    The connection is the same direct device ``power.rail`` edge consumed by
    power budgets. Reference integrity owns missing models and rails; an absent
    input range is an optional declaration and intentionally produces nothing.
    The comparison is inclusive: a rail voltage exactly equal to a declared min or max is compatible — a datasheet min/max includes its endpoints.
    """
    out: list[Finding] = []
    for device in c.devices:
        if device.power is None:
            continue
        model = c.device_model(device.model)
        rail = c.rail(device.power.rail)
        if model is None or rail is None or model.input_voltage_range is None:
            continue
        if rail.voltage_range is not None:
            # the rail publishes a whole range — compatible iff it is
            # fully inside the device's declared input range (inclusive).
            if scores is not None:
                scores.record(f"devices/{device.id}/power/rail")
            rail_min_v = rail.voltage_range.min.base()
            rail_max_v = rail.voltage_range.max.base()
            dev_min_v = model.input_voltage_range.min.base()
            dev_max_v = model.input_voltage_range.max.base()
            rail_range_str = (
                f"[{format_magnitude(rail_min_v, 'V')}, {format_magnitude(rail_max_v, 'V')}]"
            )
            dev_range_str = (
                f"[{format_magnitude(dev_min_v, 'V')}, {format_magnitude(dev_max_v, 'V')}]"
            )
            if rail_min_v >= dev_min_v and rail_max_v <= dev_max_v:
                if scores is not None:
                    headroom_v = min(rail_min_v - dev_min_v, dev_max_v - rail_max_v)
                    scores.passed(
                        f"devices/{device.id}/power/rail",
                        observed=rail_range_str,
                        expected=_with_enforcement_provenance(
                            dev_range_str,
                            ("min", model.input_voltage_range.min_enforced_by),
                            ("max", model.input_voltage_range.max_enforced_by),
                        ),
                        margin=f"{format_magnitude(headroom_v, 'V')} inside the {dev_range_str} window",
                    )
                continue

            sources = []
            if rail.provenance is not None:
                sources.append(f"rail {rail.id} voltage_range: {rail.provenance.source}")
            if model.provenance is not None:
                sources.append(f"device model {model.id} input range: {model.provenance.source}")
            if rail_min_v < dev_min_v:
                delta = f"-{format_magnitude(dev_min_v - rail_min_v, 'V')}"
            else:
                delta = f"+{format_magnitude(rail_max_v - dev_max_v, 'V')}"
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{device.id}/power/rail",
                    observed=(
                        f"rail {rail.id!r} publishes {rail_range_str} to device {device.id!r}"
                    ),
                    expected=_with_enforcement_provenance(
                        f"device model {model.id!r} input range {dev_range_str}",
                        ("min", model.input_voltage_range.min_enforced_by),
                        ("max", model.input_voltage_range.max_enforced_by),
                    ),
                    delta=delta,
                    dependent=(
                        f"topology/rails/{rail.id}/voltage_range, "
                        f"device_models/{model.id}/input_voltage_range"
                    ),
                    owner=device.owner,
                    source="; ".join(sources) or None,
                    action=(
                        "supply the device from a compatible rail or correct the sourced "
                        "rail voltage_range and device input range"
                    ),
                )
            )
            continue
        if rail.voltage is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"devices/{device.id}/power/rail",
                    observed=f"{rail.id} has no declared voltage",
                    expected="a declared rail voltage",
                    owner=device.owner,
                    action=(
                        "declare the rail's voltage, or accept voltage compatibility as not scored"
                    ),
                )
            )
            continue

        # Rail voltage vs datasheet input range — both sides present.
        if scores is not None:
            scores.record(f"devices/{device.id}/power/rail")
        rail_v = rail.voltage.base()
        minimum_v = model.input_voltage_range.min.base()
        maximum_v = model.input_voltage_range.max.base()
        if minimum_v <= rail_v <= maximum_v:
            if scores is not None:
                headroom_v = min(rail_v - minimum_v, maximum_v - rail_v)
                scores.passed(
                    f"devices/{device.id}/power/rail",
                    observed=format_magnitude(rail_v, "V"),
                    expected=_with_enforcement_provenance(
                        f"[{format_magnitude(minimum_v, 'V')}, {format_magnitude(maximum_v, 'V')}]",
                        ("min", model.input_voltage_range.min_enforced_by),
                        ("max", model.input_voltage_range.max_enforced_by),
                    ),
                    margin=(
                        f"{format_magnitude(headroom_v, 'V')} inside the "
                        f"{format_magnitude(minimum_v, 'V')}-{format_magnitude(maximum_v, 'V')} window"
                    ),
                )
            continue

        sources: list[str] = []
        if rail.provenance is not None:
            sources.append(f"rail {rail.id} voltage: {rail.provenance.source}")
        if model.provenance is not None:
            sources.append(f"device model {model.id} input range: {model.provenance.source}")
        if rail_v < minimum_v:
            delta = f"-{format_magnitude(minimum_v - rail_v, 'V')}"
        else:
            delta = f"+{format_magnitude(rail_v - maximum_v, 'V')}"

        out.append(
            Finding(
                severity="violation",
                quantity=f"devices/{device.id}/power/rail",
                observed=(
                    f"rail {rail.id!r} supplies {format_magnitude(rail_v, 'V')} "
                    f"to device {device.id!r}"
                ),
                expected=_with_enforcement_provenance(
                    (
                        f"device model {model.id!r} input range "
                        f"[{format_magnitude(minimum_v, 'V')}, {format_magnitude(maximum_v, 'V')}]"
                    ),
                    ("min", model.input_voltage_range.min_enforced_by),
                    ("max", model.input_voltage_range.max_enforced_by),
                ),
                observed_quantity=Quantity(value=rail_v, unit="V"),
                delta=delta,
                dependent=(
                    f"topology/rails/{rail.id}/voltage, "
                    f"device_models/{model.id}/input_voltage_range"
                ),
                owner=device.owner,
                source="; ".join(sources) or None,
                action=(
                    "supply the device from a compatible rail or correct the sourced "
                    "rail voltage and device input range"
                ),
            )
        )
    return out


def power_rollup(
    c: Contract, draw_mode: str
) -> tuple[dict[str, float], dict[str, set[str]], float, set[str], dict[str, set[str]], set[str]]:
    """Roll the selected draw mode toward the battery through each converter.

    ``draw`` is the nominal/default value. A named mode overrides it when
    present; devices with no such override retain their compatible single draw.

    A device with no declared ``draw`` (and, by the schema validator, no
    modes either — modes require a fallback draw) is an UNKNOWN consumer: it
    is never summed as 0 W. Instead its id is recorded, keyed by every rail on
    its path to the battery, in the returned ``rail_unknown``/``battery_unknown``
    — a caller that depends on any of those totals must not_score itself
    rather than silently sum around the gap.

    Decision: a budget checking nominal stays SILENT even when the same tree would breach at peak - the contract explicitly selected its mode, and no near-miss warning policy exists. The narrower future enhancement, if ever wanted: warn only when draw_mode is non-nominal and zero devices declare any named mode. Deliberate, disclosed asymmetry: budgets score the contract's selected draw mode, while check_ampacity scores the worst declared mode. A budget is a design target the author owns; ampacity is a fire limit that holds no matter which mode the author had in mind.
    """
    rail_totals: dict[str, float] = {}
    rail_witnesses: dict[str, set[str]] = {}
    rail_unknown: dict[str, set[str]] = {}
    battery_w = 0.0
    battery_witnesses: set[str] = set()
    battery_unknown: set[str] = set()

    for device in c.devices:
        if device.power is None:
            continue
        rail = c.rail(device.power.rail)
        if rail is None:
            continue
        selected = next(
            (mode.draw for mode in device.power.modes if mode.name == draw_mode),
            device.power.draw,
        )
        if selected is None:
            rail_unknown.setdefault(rail.id, set()).add(device.id)
            seen = {rail.id}
            current = rail
            while True:
                if current.upstream in (None, "battery"):
                    battery_unknown.add(device.id)
                    break
                parent = c.rail(current.upstream)
                if parent is None or parent.id in seen:
                    break
                rail_unknown.setdefault(parent.id, set()).add(device.id)
                seen.add(parent.id)
                current = parent
            continue
        load_w = selected.base()
        rail_totals[rail.id] = rail_totals.get(rail.id, 0.0) + load_w
        rail_witnesses.setdefault(rail.id, set()).add(device.id)
        seen = {rail.id}
        current = rail
        while True:
            if current.upstream == "battery":
                battery_w += load_w / current.efficiency
                battery_witnesses.add(device.id)
                break
            if current.upstream is None:
                battery_w += load_w
                battery_witnesses.add(device.id)
                break
            parent = c.rail(current.upstream)
            if parent is None or parent.id in seen:
                break
            load_w /= current.efficiency
            rail_totals[parent.id] = rail_totals.get(parent.id, 0.0) + load_w
            rail_witnesses.setdefault(parent.id, set()).add(device.id)
            seen.add(parent.id)
            current = parent

    return rail_totals, rail_witnesses, battery_w, battery_witnesses, rail_unknown, battery_unknown


#: Retained so nothing outside this module has to rename its call sites.
_power_rollup = power_rollup


def _format_hours(hours: float) -> str:
    """Display a duration in the unit a human reads fastest: minutes under an
    hour, hours at or above it. Display only — the contract and schema stay in
    hours; only these rendered strings change."""
    if abs(hours) < 1.0:
        return f"{hours * 60:.1f} min"
    return f"{hours:.2f} h"


def unusable_effort_finding(quantity: str, effort: float, **extra: Any) -> Finding:
    """Warning for a joint effort that is set but not a usable limit (0 = unset, <0 = invalid)."""
    label = "unset (0)" if effort == 0 else f"invalid (negative) {effort:g}"
    return Finding(
        severity="warning",
        quantity=quantity,
        observed=f"joint torque limit is {label} in the model; torque is not checked",
        expected="a positive joint effort limit",
        action="set the joint effort limit from the datasheet",
        **extra,
    )


def _with_enforcement_provenance(expected: str, *limits: tuple[str, str | None]) -> str:
    """Annotate an existing checked limit without changing check scoring."""
    declarations = [f"{value} ({name})" for name, value in limits if value is not None]
    if not declarations:
        return expected
    return (
        f"{expected} (declared enforcement provenance: {', '.join(declarations)}; "
        "metadata only; does not prove physical protection)"
    )


def check_budgets(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Budgets are derived entries: current value = sum over witnesses, here the
    contract's own device and model entries. Power follows the efficiency-aware
    rail tree so one child load constrains its converter, ancestors, and endurance."""
    out: list[Finding] = []
    for b in c.budgets:
        limit = b.limit.base()
        if limit <= 0.0:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"budgets/{b.id}/limit",
                    observed=str(b.limit),
                    observed_quantity=b.limit,
                    expected=_with_enforcement_provenance(
                        "a positive budget limit", ("limit", b.limit_enforced_by)
                    ),
                    owner=b.owner,
                    source=b.provenance.source if b.provenance is not None else None,
                    witness=b.provenance.witness if b.provenance is not None else None,
                    locked_at=b.provenance.locked_at if b.provenance is not None else None,
                    action="replace the non-positive limit with a sourced physical constraint",
                )
            )
            continue

        if b.kind == "rail_power":
            rail_totals, rail_witnesses, _, _, rail_unknown, _ = _power_rollup(c, b.draw_mode)
            contributors = sorted(rail_witnesses.get(b.rail or "", set()))
            unknown_consumers = sorted(rail_unknown.get(b.rail or "", set()))
            has_named_modes = any(c.device(device_id).power.modes for device_id in contributors)
            total_w = rail_totals.get(b.rail or "", 0.0)
            limit_w = limit
            rail = c.rail(b.rail) if b.rail is not None else None
            if rail is not None and limit_w > rail.capacity.base():
                # This two-number sentence accuses both values, but its quantity and
                # observed clause name the budget limit, so the one source slot cites
                # Budget and never falls back to Rail.
                # Readers now exist for Budget, Rail, Bus; Host.provenance
                # (schema.py:489) and DeviceFamily.provenance (schema.py:294) remain UNREAD.
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}/limit",
                        observed=format_magnitude(limit_w, "W"),
                        expected=_with_enforcement_provenance(
                            f"<= rail capacity {format_magnitude(rail.capacity.base(), 'W')}",
                            ("capacity", rail.capacity_enforced_by),
                        ),
                        observed_quantity=Quantity(value=limit_w, unit="W"),
                        expected_quantity=Quantity(value=rail.capacity.base(), unit="W"),
                        delta=f"+{format_magnitude(limit_w - rail.capacity.base(), 'W')}",
                        dependent=f"topology/rails/{rail.id}/capacity",
                        owner=b.owner,
                        source=b.provenance.source if b.provenance is not None else None,
                        witness=b.provenance.witness if b.provenance is not None else None,
                        locked_at=b.provenance.locked_at if b.provenance is not None else None,
                        action=(
                            "lower the budget limit or correct the rail capacity with provenance"
                        ),
                    )
                )
            if unknown_consumers:
                # a rail with any consumer of unknown draw cannot be safely
                # summed — never sum an unknown draw as 0 W.
                device_id = unknown_consumers[0]
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"budgets/{b.id}",
                        observed=unknown_draw_message(device_id, c.device(device_id).power.rail),
                        expected="every rail consumer to declare a draw",
                        dependent=", ".join(unknown_consumers),
                        owner=b.owner,
                        action=(
                            f"declare {device_id}'s power draw, or accept the budget as not scored"
                        ),
                    )
                )
                continue
            if not contributors:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"budgets/{b.id}",
                        observed="zero contributing power declarations",
                        expected="at least one device on the rail or a descendant rail",
                        owner=b.owner,
                        action="add device power witnesses or remove the empty budget",
                    )
                )
                # No witness draw — limit vs .get(..., 0.0) is vacuous; do not score.
            else:
                # Score on the rolled-up MAGNITUDE, not on the contributor list.
                # A non-empty list whose every draw is 0.0 W sums to nothing, and
                # limit-vs-0.0 passes trivially. Draws are
                # non-negative, so > 0 separates "a real load was weighed" from
                # "a list of zeros"; one 0 W device among positive siblings still
                # leaves the sum positive and still scores.
                if scores is not None and total_w > 0.0:
                    scores.record(f"budgets/{b.id}")
            if total_w > limit_w:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}",
                        observed=(
                            f"{format_magnitude(total_w, 'W')} (draw mode: {b.draw_mode})"
                            if has_named_modes
                            else format_magnitude(total_w, "W")
                        ),
                        expected=_with_enforcement_provenance(
                            f"<= {format_magnitude(limit_w, 'W')}",
                            ("limit", b.limit_enforced_by),
                        ),
                        observed_quantity=Quantity(value=total_w, unit="W"),
                        expected_quantity=Quantity(value=limit_w, unit="W"),
                        delta=(
                            f"+{format_magnitude(total_w - limit_w, 'W')} "
                            f"(+{(total_w / limit_w - 1) * 100:.0f}%)"
                        ),
                        dependent=", ".join(contributors),
                        owner=b.owner,
                        source=(
                            rail.provenance.source
                            if rail is not None and rail.provenance is not None
                            else b.provenance.source
                            if b.provenance is not None
                            else None
                        ),
                        action=(
                            f"shed load on rail {b.rail!r} or raise its capacity with provenance"
                        ),
                        alternatives=build_rail_alternatives(
                            c,
                            over_rail_id=b.rail or "",
                            deficit_w=total_w - limit_w,
                            rail_totals=rail_totals,
                            rail_unknown=rail_unknown,
                            check_name="check_budgets",
                        ),
                    )
                )
            elif scores is not None and total_w > 0.0:
                headroom_w = limit_w - total_w
                scores.passed(
                    f"budgets/{b.id}",
                    observed=format_magnitude(total_w, "W"),
                    expected=_with_enforcement_provenance(
                        f"<= {format_magnitude(limit_w, 'W')}",
                        ("limit", b.limit_enforced_by),
                    ),
                    margin=(
                        f"{format_magnitude(headroom_w, 'W')} headroom "
                        f"({headroom_w / limit_w * 100:.0f}%)"
                    ),
                )

        elif b.kind == "mass_total":
            total_kg = 0.0
            contributors: list[str] = []
            if b.sources in ("devices", "links+devices"):
                for d in c.devices:
                    if d.physical is None or d.physical.mass is None:
                        # no physical block and a physical block with no
                        # declared mass are the same coverage gap — never summed
                        # as a 0 kg contributor, always disclosed alike.
                        out.append(
                            Finding(
                                severity="not_scored",
                                quantity=f"devices/{d.id}/physical/mass",
                                observed="mass not declared",
                                expected="a positive mass with units",
                                owner=b.owner,
                                action="declare the device's mass, or accept it excluded from this budget",
                            )
                        )
                        continue
                    total_kg += d.physical.mass.base()
                    contributors.append(d.id)
            if b.sources in ("links", "links+devices") and c.model is not None:
                for link in c.model.links:
                    if link.mass is not None:
                        total_kg += link.mass.base()
                        contributors.append(link.name)
            limit_kg = limit
            # Score on the summed MAGNITUDE, not on the contributor list: a
            # non-empty list of zero-mass witnesses sums to 0.0 kg and would
            # vacuous-pass the limit. Masses are non-negative.
            if scores is not None and total_kg > 0.0:
                scores.record(f"budgets/{b.id}")
            if total_kg > limit_kg:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}",
                        observed=format_magnitude(total_kg, "kg"),
                        expected=_with_enforcement_provenance(
                            f"<= {format_magnitude(limit_kg, 'kg')}",
                            ("limit", b.limit_enforced_by),
                        ),
                        observed_quantity=Quantity(value=total_kg, unit="kg"),
                        expected_quantity=Quantity(value=limit_kg, unit="kg"),
                        delta=f"+{format_magnitude(total_kg - limit_kg, 'kg')}",
                        dependent=", ".join(contributors),
                        owner=b.owner,
                        source=b.provenance.source if b.provenance is not None else None,
                        witness=b.provenance.witness if b.provenance is not None else None,
                        locked_at=b.provenance.locked_at if b.provenance is not None else None,
                        action="remove mass or re-lock the budget limit with provenance",
                    )
                )
            elif scores is not None and total_kg > 0.0:
                headroom_kg = limit_kg - total_kg
                scores.passed(
                    f"budgets/{b.id}",
                    observed=format_magnitude(total_kg, "kg"),
                    expected=_with_enforcement_provenance(
                        f"<= {format_magnitude(limit_kg, 'kg')}",
                        ("limit", b.limit_enforced_by),
                    ),
                    margin=(
                        f"{format_magnitude(headroom_kg, 'kg')} headroom "
                        f"({headroom_kg / limit_kg * 100:.0f}%)"
                    ),
                )

        elif b.kind == "endurance":
            _, _, battery_w, battery_witnesses, _, battery_unknown = _power_rollup(c, b.draw_mode)
            has_named_modes = any(
                c.device(device_id).power.modes for device_id in battery_witnesses
            )
            if c.topology is None or c.topology.battery is None:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}",
                        observed="no battery declared in topology",
                        expected="topology.battery with an energy capacity",
                        owner=b.owner,
                        action="declare the battery so endurance can be computed",
                    )
                )
                continue
            if battery_unknown:
                # any device on the way to the battery with unknown draw
                # makes endurance unsafe to compute — never sum it as 0 W.
                device_id = sorted(battery_unknown)[0]
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"budgets/{b.id}",
                        observed=unknown_draw_message(device_id, c.device(device_id).power.rail),
                        expected="every device reaching the battery to declare a draw",
                        dependent=", ".join(sorted(battery_unknown)),
                        owner=b.owner,
                        action=(
                            f"declare {device_id}'s power draw, or accept endurance as not scored"
                        ),
                    )
                )
                continue
            total_w = battery_w
            if total_w == 0.0:
                # zero declared draw is an unmeasured
                # budget, not a warning-worthy anomaly — never score it, and
                # never let it read as "0.00 min" against the required minutes.
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"budgets/{b.id}",
                        observed="no draw declared",
                        observed_quantity=Quantity(value=0.0, unit="W"),
                        expected="at least one device with a power entry",
                        owner=b.owner,
                        action="add power draws to devices — endurance is uncheckable until then",
                    )
                )
                continue
            capacity_wh = c.topology.battery.capacity.base()
            hours = capacity_wh / total_w
            required_h = units.convert(b.limit.value, b.limit.unit, "h")
            # Battery capacity/draw vs required endurance — both sides present.
            if scores is not None:
                scores.record(f"budgets/{b.id}")
            if hours < required_h:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}",
                        observed=(
                            f"{_format_hours(hours)} expected endurance (draw mode: {b.draw_mode})"
                            if has_named_modes
                            else f"{_format_hours(hours)} expected endurance"
                        ),
                        expected=_with_enforcement_provenance(
                            f">= {_format_hours(required_h)}", ("limit", b.limit_enforced_by)
                        ),
                        observed_quantity=Quantity(value=hours, unit="h"),
                        expected_quantity=Quantity(value=required_h, unit="h"),
                        delta=f"-{_format_hours(required_h - hours)}",
                        dependent=", ".join(sorted(battery_witnesses)),
                        owner=b.owner,
                        source=b.provenance.source if b.provenance is not None else None,
                        witness=b.provenance.witness if b.provenance is not None else None,
                        locked_at=b.provenance.locked_at if b.provenance is not None else None,
                        action=(
                            "shed power draw, grow the battery, or lower the mission requirement"
                        ),
                    )
                )
                continue
            # Nominal passes: check declared cold-capacity deratings. A failure here
            # is a go-with-note (MEL-style) — the mission works, but not below the
            # named pack temperature.
            derated_warning = False
            for row in sorted(c.topology.battery.derating, key=lambda r: -r.below_c):
                derated_h = capacity_wh * row.capacity_factor / total_w
                if derated_h < required_h:
                    derated_warning = True
                    out.append(
                        Finding(
                            severity="warning",
                            quantity=f"budgets/{b.id}",
                            observed=(
                                f"{_format_hours(derated_h)} expected endurance at "
                                f"pack temp <= {row.below_c:g} C"
                                + (f" (draw mode: {b.draw_mode})" if has_named_modes else "")
                            ),
                            expected=_with_enforcement_provenance(
                                f">= {_format_hours(required_h)}",
                                ("limit", b.limit_enforced_by),
                            ),
                            observed_quantity=Quantity(value=derated_h, unit="h"),
                            expected_quantity=Quantity(value=required_h, unit="h"),
                            delta=f"-{_format_hours(required_h - derated_h)}",
                            dependent=", ".join(sorted(battery_witnesses)),
                            owner=b.owner,
                            source=b.provenance.source if b.provenance is not None else None,
                            witness=b.provenance.witness if b.provenance is not None else None,
                            locked_at=b.provenance.locked_at if b.provenance is not None else None,
                            action=(
                                f"cold ops below {row.below_c:g} C unsupported "
                                "— restrict conditions, preheat pack, or grow "
                                "the battery"
                            ),
                        )
                    )
                    break  # coldest failing row already implies all colder rows fail
            if scores is not None and not derated_warning:
                headroom_h = hours - required_h
                scores.passed(
                    f"budgets/{b.id}",
                    observed=f"{_format_hours(hours)}",
                    expected=_with_enforcement_provenance(
                        f">= {_format_hours(required_h)}", ("limit", b.limit_enforced_by)
                    ),
                    margin=f"{_format_hours(headroom_h)} to spare",
                )

    return out


# ---------------------------------------------------------------- ampacity


# Published copper ampacity (amperes), 90 °C insulation column.
# Source: NFPA 70 National Electrical Code (NEC) 2020 —
#   free_air  -> Table 310.17 (single-insulated conductors in free air)
#   enclosed  -> Table 310.16 (≤3 current-carrying conductors in raceway,
#               cable, or earth) — the same standard's enclosed column, not a
#               hand-typed derating multiplier (derating honesty).
# Gauges absent from these tables are not scored (emit not_scored).
#
# 18/16 AWG free-air corroboration: checked
# against NFPA 70 (NEC) 2020 Table 310.17 copper 90 °C — 18 AWG = 18 A,
# 16 AWG = 24 A. Values stand; no correction.
AMPACITY_SOURCE = (
    "NFPA 70 (NEC) 2020 Table 310.17 (free air) / Table 310.16 "
    "(≤3 CCC in raceway/cable/earth), copper, 90 °C column"
)
_AMPACITY_A: dict[tuple[int, str], float] = {
    # (awg, installation) -> amperes
    (18, "free_air"): 18.0,  # NEC 2020 Table 310.17 Cu 90 °C — corroborated
    (16, "free_air"): 24.0,  # NEC 2020 Table 310.17 Cu 90 °C — corroborated
    (14, "free_air"): 35.0,
    (12, "free_air"): 40.0,
    (10, "free_air"): 55.0,
    (8, "free_air"): 80.0,
    (6, "free_air"): 105.0,
    (4, "free_air"): 140.0,
    (3, "free_air"): 165.0,
    (2, "free_air"): 190.0,
    (1, "free_air"): 220.0,
    (18, "enclosed"): 14.0,
    (16, "enclosed"): 18.0,
    (14, "enclosed"): 25.0,
    (12, "enclosed"): 30.0,
    (10, "enclosed"): 40.0,
    (8, "enclosed"): 55.0,
    (6, "enclosed"): 75.0,
    (4, "enclosed"): 95.0,
    (3, "enclosed"): 115.0,
    (2, "enclosed"): 130.0,
    (1, "enclosed"): 145.0,
}


def _ampacity_draw_modes(c: Contract) -> list[str]:
    """Nominal plus every named PowerMode declared on any powered device."""
    names: set[str] = {"nominal"}
    for device in c.devices:
        if device.power is None:
            continue
        for mode in device.power.modes:
            names.add(mode.name)
    return ["nominal", *sorted(n for n in names if n != "nominal")]


def _worst_rail_power(c: Contract, rail_id: str) -> tuple[float, str, set[str], set[str]]:
    """Maximize rolled-up watts on ``rail_id`` across declared draw modes.

    Ampacity must not silently score a defaulted ``draw_mode="nominal"`` when
    contributing devices declare higher named modes: that
    path reports a confident wrong pass on a fire-safety check. Returns
    ``(load_w, mode_name, witnesses, unknown_consumers)`` for the worst mode
    (nominal first on ties so a true peak is what gets named when it wins).
    ``unknown_consumers`` is invariant across draw_mode — a device with no
    declared draw has no modes either (the schema requires a fallback), so it
    is unknown under every mode; a non-empty set means the caller cannot
    safely compute a peak and must not_score instead.
    """
    worst_w = -1.0
    worst_mode = "nominal"
    worst_witnesses: set[str] = set()
    unknown: set[str] = set()
    for mode_name in _ampacity_draw_modes(c):
        rail_totals, rail_witnesses, _, _, rail_unknown, _ = _power_rollup(c, mode_name)
        unknown |= rail_unknown.get(rail_id, set())
        load_w = rail_totals.get(rail_id, 0.0)
        if load_w > worst_w:
            worst_w = load_w
            worst_mode = mode_name
            worst_witnesses = set(rail_witnesses.get(rail_id, set()))
    return max(worst_w, 0.0), worst_mode, worst_witnesses, unknown


def check_ampacity(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Published AWG ampacity vs computed rail current (two-source).

    Limit side: embedded NEC tables cited in ``AMPACITY_SOURCE``. Draw side:
    the **worst** rolled-up device watts on the wire's rail across nominal and
    every declared ``PowerMode`` (``_worst_rail_power``), divided by rail
    voltage — never the wire's defaulted ``draw_mode`` alone. A gauge or
    installation absent from the published table emits ``not_scored`` rather
    than inventing a derating.

    Counterpart to check_budgets, which stays pinned to the contract's selected draw mode; see _power_rollup for that side of the asymmetry.
    """
    if c.topology is None:
        return []

    out: list[Finding] = []
    for wire in c.topology.wires:
        limit_a = _AMPACITY_A.get((wire.awg, wire.installation))
        if limit_a is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=(
                        f"{wire.awg} AWG {wire.installation}: no published "
                        "ampacity in the cited table; ampacity not scored"
                    ),
                    expected="a gauge+installation pair present in the cited NEC tables",
                    owner=wire.owner,
                    source=AMPACITY_SOURCE,
                    action=(
                        "choose a gauge listed in the cited NEC tables, or accept "
                        "that this wire is excluded from the ampacity gate"
                    ),
                )
            )
            continue

        rail = c.rail(wire.rail)
        if rail is None:
            continue  # reference integrity owns the dangling rail

        load_w, draw_mode, witnesses, unknown_consumers = _worst_rail_power(c, wire.rail)
        if unknown_consumers:
            device_id = sorted(unknown_consumers)[0]
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=unknown_draw_message(device_id, wire.rail),
                    expected="every consumer on this rail to declare a draw",
                    dependent=", ".join(sorted(unknown_consumers)),
                    owner=wire.owner,
                    source=AMPACITY_SOURCE,
                    action=(
                        f"declare {device_id}'s power draw, or accept ampacity on "
                        "this wire as not scored"
                    ),
                )
            )
            continue
        if rail.voltage is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=f"rail {wire.rail!r} has no declared voltage",
                    expected="a declared rail voltage",
                    owner=wire.owner,
                    source=AMPACITY_SOURCE,
                    action="declare the rail's voltage, or accept ampacity on this wire as not scored",
                )
            )
            continue
        voltage_v = rail.voltage.base()
        if voltage_v <= 0.0:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=f"rail voltage <= 0 ({format_magnitude(voltage_v, 'V')})",
                    expected="a positive rail voltage",
                    owner=wire.owner,
                    source=AMPACITY_SOURCE,
                    action=("record the rail's actual voltage so computed draw can be scored"),
                )
            )
            continue
        draw_a = load_w / voltage_v
        contributors = sorted(witnesses)
        # NEC ampacity vs computed draw — score on the computed CURRENT, not on
        # the witness list. This comment previously named the vacuous-pass defect
        # while the guard only checked list non-emptiness: a witness declaring
        # 0 W yields 0 A against a published ampacity limit, which passes for no
        # reason. Draws are non-negative, so > 0 means a real load was weighed.
        if scores is not None and draw_a > 0.0:
            scores.record(f"wires/{wire.id}")
        has_named_modes = any(c.device(device_id).power.modes for device_id in contributors)

        if draw_a > limit_a:
            observed = (
                f"{format_magnitude(draw_a, 'A')} computed draw on {wire.awg} AWG "
                f"{wire.installation} (draw mode: {draw_mode})"
                if has_named_modes
                else (
                    f"{format_magnitude(draw_a, 'A')} computed draw on {wire.awg} AWG {wire.installation}"
                )
            )
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=observed,
                    expected=f"<= {format_magnitude(limit_a, 'A')} published ampacity",
                    observed_quantity=Quantity(value=draw_a, unit="A"),
                    expected_quantity=Quantity(value=limit_a, unit="A"),
                    delta=(
                        f"+{format_magnitude(draw_a - limit_a, 'A')} (+{(draw_a / limit_a - 1) * 100:.0f}%)"
                    ),
                    dependent=", ".join(contributors) or None,
                    owner=wire.owner,
                    source=AMPACITY_SOURCE,
                    action=(
                        f"upsize wire {wire.id!r}, shorten/re-route toward free-air, "
                        "or shed load on its rail"
                    ),
                )
            )
        elif scores is not None and draw_a > 0.0:
            headroom_a = limit_a - draw_a
            scores.passed(
                f"topology/wires/{wire.id}/ampacity",
                observed=format_magnitude(draw_a, "A"),
                expected=f"<= {format_magnitude(limit_a, 'A')}",
                margin=(
                    f"{format_magnitude(headroom_a, 'A')} headroom "
                    f"({headroom_a / limit_a * 100:.0f}%)"
                ),
            )

    return out


def check_rail_current(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Published rail current_limit vs computed draw (sibling to check_ampacity).

    Draw side: the same nominal-mode rolled-up watts a ``rail_power`` budget
    would sum for this rail (``power_rollup`` — not re-added here), divided by
    the rail's worst-case voltage: its declared ``voltage_range.min`` when
    present (the conservative bound — lower voltage means higher current for
    the same watts), else its single ``voltage``. A rail with no voltage fact
    (neither) or an unsummable draw (any consumer of unknown draw) is
    ``not_scored`` rather than guessing.
    """
    if c.topology is None:
        return []
    out: list[Finding] = []
    rail_totals, rail_witnesses, _, _, rail_unknown, _ = power_rollup(c, "nominal")
    for rail in c.topology.rails:
        if rail.current_limit is None:
            continue
        if rail.voltage_range is not None:
            voltage_v = rail.voltage_range.min.base()
        elif rail.voltage is not None:
            voltage_v = rail.voltage.base()
        else:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed=f"rail {rail.id!r} has no declared voltage or voltage_range",
                    expected="a declared rail voltage or voltage_range",
                    action=(
                        "declare the rail's voltage or voltage_range, or accept its "
                        "current_limit as not scored"
                    ),
                )
            )
            continue

        unknown_consumers = rail_unknown.get(rail.id, set())
        if unknown_consumers:
            device_id = sorted(unknown_consumers)[0]
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed=unknown_draw_message(device_id, rail.id),
                    expected="every consumer on this rail to declare a draw",
                    dependent=", ".join(sorted(unknown_consumers)),
                    action=(
                        f"declare {device_id}'s power draw, or accept this rail's "
                        "current_limit as not scored"
                    ),
                )
            )
            continue

        load_w = rail_totals.get(rail.id, 0.0)
        contributors = sorted(rail_witnesses.get(rail.id, set()))
        if not contributors:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed="no summable power declarations on this rail",
                    expected="at least one device on the rail or a descendant rail",
                    action="add device power witnesses, or accept this rail's current_limit as not scored",
                )
            )
            continue

        draw_a = load_w / voltage_v
        limit_a = rail.current_limit.base()
        # Score on the computed CURRENT, not on the witness list — a witness
        # declaring 0 W yields 0 A and would vacuous-pass for no reason.
        if scores is not None and draw_a > 0.0:
            scores.record(f"topology/rails/{rail.id}/current_limit")
        if draw_a > limit_a:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed=f"{format_magnitude(draw_a, 'A')} computed draw on rail {rail.id!r}",
                    expected=_with_enforcement_provenance(
                        f"<= {format_magnitude(limit_a, 'A')} published current_limit",
                        ("current_limit", rail.current_limit_enforced_by),
                    ),
                    observed_quantity=Quantity(value=draw_a, unit="A"),
                    expected_quantity=Quantity(value=limit_a, unit="A"),
                    delta=(
                        f"+{format_magnitude(draw_a - limit_a, 'A')} "
                        f"(+{(draw_a / limit_a - 1) * 100:.0f}%)"
                    ),
                    dependent=", ".join(contributors),
                    action=(
                        f"shed load on rail {rail.id!r} or raise its published current_limit "
                        "with provenance"
                    ),
                )
            )
        elif scores is not None and draw_a > 0.0:
            headroom_a = limit_a - draw_a
            scores.passed(
                f"topology/rails/{rail.id}/current_limit",
                observed=format_magnitude(draw_a, "A"),
                expected=_with_enforcement_provenance(
                    f"<= {format_magnitude(limit_a, 'A')}",
                    ("current_limit", rail.current_limit_enforced_by),
                ),
                margin=(
                    f"{format_magnitude(headroom_a, 'A')} headroom "
                    f"({headroom_a / limit_a * 100:.0f}%)"
                ),
            )
    return out


# ---------------------------------------------------------------- actuator and bus budgets


def check_torque_budgets(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """A joint's modeled effort demand must not exceed the paired actuator's
    continuous geared output; peak torque is not a sustainable design budget."""
    if c.model is None:
        return []
    joints = {joint.name: joint for joint in c.model.joints}
    out: list[Finding] = []
    for device in c.devices:
        if device.drives_joint is None:
            continue
        joint = joints.get(device.drives_joint)
        model = c.device_model(device.model)
        if (
            joint is None
            or joint.type not in ("revolute", "continuous")
            or joint.limits is None
            or joint.limits.effort is None
        ):
            continue
        actuator = model.actuator if model is not None else None
        if actuator is None or actuator.torque_continuous is None or actuator.gear_ratio is None:
            continue
        if joint.limits.effort_limit is None:
            out.append(
                unusable_effort_finding(
                    f"devices/{device.id}/drives_joint",
                    joint.limits.effort,
                    dependent=f"model/joints/{joint.name}/limits/effort",
                    owner=device.owner,
                )
            )
            continue
        if scores is not None:
            scores.record(f"devices/{device.id}/drives_joint")
        available = actuator.torque_continuous * actuator.gear_ratio
        required = joint.limits.effort
        if required > available:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{device.id}/drives_joint",
                    observed=f"{available:.2f} N*m continuous geared torque",
                    expected=_with_enforcement_provenance(
                        f">= {required:.2f} N*m joint effort limit",
                        ("effort", joint.limits.effort_enforced_by),
                    ),
                    delta=f"-{required - available:.2f} N*m",
                    dependent=f"model/joints/{joint.name}/limits/effort",
                    owner=device.owner,
                    action=(
                        "select a stronger actuator, change gearing, or correct the sourced limits"
                    ),
                )
            )
        elif scores is not None:
            headroom_nm = available - required
            scores.passed(
                f"devices/{device.id}/drives_joint",
                observed=f"{required:.2f} N*m joint effort limit",
                expected=_with_enforcement_provenance(
                    f"<= {available:.2f} N*m continuous geared torque",
                    ("effort", joint.limits.effort_enforced_by),
                ),
                margin=(
                    f"{headroom_nm:.2f} N*m headroom ({headroom_nm / available * 100:.0f}%)"
                    if available > 0.0
                    else f"{headroom_nm:.2f} N*m headroom"
                ),
            )
    return out


def check_bus_bandwidth(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Declared traffic must fit below a conservative share of raw bitrate;
    protocol overhead and contention make 100% raw utilization unusable.

    A bus with ``bitrate is None`` is not scored: emit a ``not_scored`` finding so
    GO cannot be read as "bandwidth verified" when utilization was never checked.
    A bus with a bitrate but no driver declaring both ``nominal_rate_hz`` and
    ``payload_bytes`` is the same disclosure the other way round: there is
    nothing to weigh against the known bitrate, so it is also ``not_scored``
    rather than silently emitting nothing.
    ``not_scored`` is neither a violation nor a warning (false-positive budget;
    must not obscure headline defects with warning noise).
    """
    if c.topology is None:
        return []
    out: list[Finding] = []
    for bus in c.topology.buses:
        if bus.bitrate is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/buses/{bus.id}/bandwidth",
                    observed="bitrate unrated; bandwidth not scored",
                    expected="a positive bitrate so utilization can be scored",
                    source=bus.provenance.source if bus.provenance is not None else None,
                    witness=bus.provenance.witness if bus.provenance is not None else None,
                    locked_at=bus.provenance.locked_at if bus.provenance is not None else None,
                    action=(
                        "lock a cited bitrate, or accept that this bus is excluded "
                        "from the bandwidth gate"
                    ),
                )
            )
            continue
        contributors = [
            device
            for device in c.devices
            if device.identity is not None
            and device.identity.bus_ref == bus.id
            and device.driver is not None
            and device.driver.nominal_rate_hz is not None
            and device.driver.payload_bytes is not None
        ]
        used_bps = sum(
            device.driver.nominal_rate_hz * device.driver.payload_bytes * 8
            for device in contributors
        )
        ceiling_bps = bus.bitrate * BUS_UTILIZATION_CEILING
        # Bitrate ceiling vs declared traffic — score on the summed TRAFFIC, not
        # on the contributor list. A device declaring nominal_rate_hz = 0 is a
        # contributor that carries nothing: 0 bps against the ceiling passes for
        # no reason.
        if scores is not None and used_bps > 0:
            scores.record(f"buses/{bus.id}")
        if used_bps == 0:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/buses/{bus.id}/bandwidth",
                    observed="no declared traffic on this bus",
                    expected=(
                        "at least one driver on the bus declaring nominal_rate_hz and payload_bytes"
                    ),
                    source=bus.provenance.source if bus.provenance is not None else None,
                    witness=bus.provenance.witness if bus.provenance is not None else None,
                    locked_at=bus.provenance.locked_at if bus.provenance is not None else None,
                    action=(
                        "declare the traffic each driver puts on this bus, or "
                        "accept that its bandwidth is unchecked"
                    ),
                )
            )
            continue
        if used_bps > ceiling_bps:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/buses/{bus.id}/bandwidth",
                    observed=f"{used_bps:.0f} bit/s payload",
                    expected=(
                        f"<= {ceiling_bps:.0f} bit/s ({BUS_UTILIZATION_CEILING:.0%} of raw bitrate)"
                    ),
                    delta=f"+{used_bps - ceiling_bps:.0f} bit/s",
                    dependent=", ".join(device.id for device in contributors),
                    source=bus.provenance.source if bus.provenance is not None else None,
                    witness=bus.provenance.witness if bus.provenance is not None else None,
                    locked_at=bus.provenance.locked_at if bus.provenance is not None else None,
                    action="reduce rates or payloads, split the bus, or increase its bitrate",
                )
            )
        elif scores is not None and used_bps > 0:
            headroom_bps = ceiling_bps - used_bps
            scores.passed(
                f"topology/buses/{bus.id}/bandwidth",
                observed=f"{used_bps:.0f} bit/s payload",
                expected=(
                    f"<= {ceiling_bps:.0f} bit/s ({BUS_UTILIZATION_CEILING:.0%} of raw bitrate)"
                ),
                margin=(
                    f"{headroom_bps:.0f} bit/s headroom ({headroom_bps / ceiling_bps * 100:.0f}%)"
                ),
            )
    return out


# ---------------------------------------------------------------- model sanity


def check_zero_mass(c: Contract) -> list[Finding]:
    """Missing or non-positive link mass is derived from the canonical link
    records so a stale zero_mass_links cache cannot hide incomplete inertia."""
    if c.model is None:
        return []
    return [
        Finding(
            severity="warning",
            quantity=f"model/links/{name}/mass",
            observed="zero or missing mass",
            expected="a positive mass with units",
            action=(
                "fill in the link's inertial in the source model, or accept "
                "as massless with a Tier C assumption"
            ),
        )
        for link in c.model.links
        if link.mass is None or link.mass.base() <= 0.0
        for name in [link.name]
    ]


def check_unusable_effort(c: Contract) -> list[Finding]:
    """a model joint effort <= 0 is unset, not a limit; warn on a fresh check."""
    if c.model is None:
        return []
    # check_torque_budgets already warns for a joint with a usable actuator; don't double up.
    budgeted = {
        d.drives_joint
        for d in c.devices
        if d.drives_joint is not None
        and (m := c.device_model(d.model)) is not None
        and m.actuator is not None
        and m.actuator.torque_continuous is not None
        and m.actuator.gear_ratio is not None
    }
    return [
        unusable_effort_finding(f"model/joints/{j.name}/limits/effort", j.limits.effort)
        for j in c.model.joints
        if j.limits is not None
        and j.limits.effort is not None
        and j.limits.effort_limit is None
        and not (j.type in ("revolute", "continuous") and j.name in budgeted)
    ]


def _mechanism_quantity(m: Mechanism) -> str:
    return "model/mechanisms/" + "+".join(m.links)


def check_mechanisms(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Does a joint declared to spin all the way round actually have the geometry to?

    The comparison is *declared vs measured* like every other check here: the
    declaration is a loop joint of type ``continuous``, and the second operand is the
    closed-form Grashof answer computed from the loop's four measured link lengths
    (``mechanism.py``). No solver is involved and no tolerance is argued — Grashof is
    an exact inequality on four numbers.

    Two silences are deliberate. A loop where **nothing** is declared ``continuous``
    produces no finding at all: a four-bar of limited revolute joints is a
    double-rocker, which is a legitimate machine and not a defect, and a check that
    spoke there would spend the false-positive budget on every parallel linkage in
    every model. And a loop that IS declared continuous but is not a four-bar — a
    five-bar, a coupled linkage, a loop whose axes are not recorded — is
    ``not_scored`` **naming the kind it found**, never scored as a four-bar built from
    four of its links. That silent-approximation path is the one this check exists to
    close: it would produce a confident, plausible, wrong answer.
    """
    if c.model is None:
        return []
    out: list[Finding] = []
    for m in analyse_model(c.model):
        if not m.declared_fully_rotating:
            continue
        declared = ", ".join(m.declared_fully_rotating)
        quantity = _mechanism_quantity(m)
        if m.verdict == "not-scored":
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=m.text,
                    expected=(
                        f"a mechanism in the named set ({', '.join(NAMED_SET)}) with the "
                        "geometry recorded to answer it"
                    ),
                    action=(
                        f"{declared} is declared to turn all the way round and nothing here "
                        "checked whether it can — verify this loop by hand, or record the "
                        "geometry named above so the check can answer it"
                    ),
                )
            )
            continue
        if scores is not None:
            scores.record(quantity)
        if m.verdict == "rotates":
            if scores is not None:
                scores.passed(
                    quantity=quantity,
                    observed=m.text,
                    expected=f"{declared} declared to turn all the way round",
                    margin=(
                        f"p + q exceeds s + l by {format_magnitude(m.p_plus_q - m.s_plus_l, 'm')}"
                    ),
                )
            continue
        out.append(
            Finding(
                severity="violation" if m.verdict == "locking" else "warning",
                quantity=quantity,
                observed=m.text,
                expected=f"{declared} declared to turn all the way round (type continuous)",
                action=(
                    f"change the link lengths so s + l <= p + q, or re-declare {declared} as a "
                    "revolute joint with the limits it actually reaches"
                ),
            )
        )
    return out


def _urdf_path_candidate(model: ModelRecord) -> str | None:
    """Return the URDF/SDF path named by provenance (preferred) or model.source."""
    for candidate in (model.provenance.source, model.source):
        if candidate.lower().endswith((".urdf", ".sdf")):
            return candidate
    return None


def check_urdf_drift(
    c: Contract, *, scores: _ScoreBook | None = None, contract_path: Path | None = None
) -> list[Finding]:
    """Compare the contract's structure_hash against the on-disk URDF.

    Tier A promises the locked model matches the URDF named in provenance. A
    hash mismatch is a violation that names the structural delta (links/joints
    added, removed, or moved). Absence / unreadability / missing URDF
    provenance is ``not_scored``: visible on GO, never a silent
    pass and never a false NO-GO.

    A relative URDF path resolves from the contract's own directory when the
    caller passed ``contract_path`` — never from the working directory, so
    the same contract scores the same from anywhere. Without that anchor it stays
    ``not_scored``: a coverage gap is strictly better than an unearned GO.
    Absolute paths that are missing are likewise ``not_scored``.
    """
    if c.model is None:
        return []

    named = _urdf_path_candidate(c.model)
    if named is None:
        return [
            Finding(
                severity="not_scored",
                quantity="model/structure_hash",
                observed="no URDF provenance; structure drift not scored",
                expected="a URDF path in model provenance so structure can be scored",
                source=c.model.provenance.source,
                witness=c.model.provenance.witness,
                locked_at=c.model.provenance.locked_at,
                action=(
                    "lock model provenance to the on-disk URDF, or accept that "
                    "URDF drift is unchecked"
                ),
            )
        ]

    path = Path(named)
    if contract_path is not None:
        path = resolve_source(contract_path, named) or path
    if not path.is_file():
        if path.is_absolute():
            observed = f"URDF {path} absent; structure drift not scored"
        else:
            observed = (
                f"URDF {path}: relative path, no contract base dir — "
                "not resolvable; structure drift not scored"
            )
        return [
            Finding(
                severity="not_scored",
                quantity="model/structure_hash",
                observed=observed,
                expected="a readable URDF at the provenance path",
                source=named,
                action="restore the URDF or correct model provenance to its on-disk path",
            )
        ]

    try:
        topology = (
            read_sdf_topology(path) if path.suffix.lower() == ".sdf" else read_urdf_topology(path)
        )
    except (OSError, ET.ParseError, ValueError, TypeError) as error:
        return [
            Finding(
                severity="not_scored",
                quantity="model/structure_hash",
                observed=f"URDF unreadable ({error}); structure drift not scored",
                expected="a readable URDF at the provenance path",
                source=str(path),
                action="fix or replace the URDF so structure drift can be scored",
            )
        ]

    # v0.2 records with joints derive structure_hash from Link/Joint rows; v0.1
    # init records keep the URDF kinematic topology hash.
    disk_hash = topology.record_hash if c.model.joints else topology.topology_hash
    # Contract structure_hash vs on-disk URDF hash — both sides present.
    if scores is not None:
        scores.record("model/structure_hash")
    if disk_hash == c.model.structure_hash:
        return []

    return [
        Finding(
            severity="violation",
            quantity="model/structure_hash",
            observed=describe_structure_drift(c.model, topology),
            expected="structure hash matching the on-disk URDF",
            delta=(f"contract {c.model.structure_hash[:12]}... != urdf {disk_hash[:12]}..."),
            source=str(path),
            action="re-lock the model from the URDF after reviewing the structural change",
        )
    ]


# ---------------------------------------------------------------- envelopes


def check_envelopes_vs_hard_limits(
    c: Contract, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """A locked envelope outside its model's hard limits means the baseline was
    almost certainly captured on a sick sensor (spec: baseline-on-broken-sensor
    trap). Hard limits win; the envelope must be re-derived."""
    out: list[Finding] = []
    for d in c.devices:
        model = c.device_model(d.model)
        if model is None:
            continue  # reference check already flagged it
        # `Band.max_rate` is a rate of change, and a rate
        # needs two samples and the time between them. `check` is a static
        # comparison of one contract against itself — no time series reaches
        # it, and an envelope is a range, not a trajectory — so there is no
        # previous value here to compute a rate from. That decision is said out
        # loud, per declared bound, rather than left silent: a rate limit that
        # quietly does not apply is a rate limit that has stopped biting.
        # It is scored where the operands exist: `lockwire plan`
        # (plan._score_max_rate): compare now, predict
        # eventually, never actuate — scored, never enforced on a device.
        # Deliberately not recorded in the ScoreBook: nothing was compared.
        for channel, limit in sorted(model.hard_limits.items()):
            if limit.max_rate is None:
                continue
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"devices/{d.id}/hard_limits/{channel}/max_rate",
                    observed=f"declared max_rate {limit.max_rate:g} {limit.unit}/s",
                    expected="two timed samples of this signal, which a contract check never has",
                    owner=d.owner,
                    action=(
                        "score this bound with `lockwire plan`, which compares consecutive "
                        "published values; `check` compares declarations, not trajectories"
                    ),
                )
            )
        for env in d.envelopes:
            for channel, sig in env.signals.items():
                limit: Band | None = model.hard_limits.get(channel)
                if limit is None:
                    continue
                # Envelope band vs datasheet hard limit — both sides present.
                quantity = f"devices/{d.id}/envelopes/{env.context}/{channel}"
                if scores is not None:
                    scores.record(quantity)
                if units.dimension(sig.band.unit) != units.dimension(limit.unit):
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=quantity,
                            observed=f"unit {sig.band.unit!r}",
                            expected=f"same dimension as hard limit unit {limit.unit!r}",
                            owner=d.owner,
                            action=(
                                "fix the channel's units — envelope and hard limit are incomparable"
                            ),
                        )
                    )
                    continue
                if not limit.contains(sig.band):
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=quantity,
                            observed=str(sig.band),
                            expected=f"inside hard limit {limit}",
                            dependent=", ".join(d.depends) or None,
                            owner=d.owner,
                            action=(
                                "re-baseline — envelope exceeds absolute ratings, "
                                "baseline likely captured on a sick sensor"
                            ),
                        )
                    )
                elif scores is not None:
                    band_lo = units.convert(sig.band.lo, sig.band.unit, limit.unit)
                    band_hi = units.convert(sig.band.hi, sig.band.unit, limit.unit)
                    headroom = min(band_lo - limit.lo, limit.hi - band_hi)
                    scores.passed(
                        quantity,
                        observed=str(sig.band),
                        expected=f"inside hard limit {limit}",
                        margin=f"{format_magnitude(headroom, limit.unit)} headroom",
                    )
    return out


# ---------------------------------------------------------------- thermal budget


# Published T.SoC junction trip points for Jetson Orin-class modules.
# Source: NVIDIA Thermal Design Guide TDG-11127-001_v1.5 (May 2025) —
#   software throttle 99 °C, hardware shutdown 105 °C, both the composite
#   equal-ratio average of CPU / GPU / SoC zones. NVIDIA publishes no
#   discrete per-zone trip points; the check's operand is that composite.
# The actionable gate is throttle; shutdown is finding context only.
THERMAL_TRIP_SOURCE = (
    "NVIDIA Thermal Design Guide TDG-11127-001_v1.5 (May 2025), "
    "T.SoC junction (equal-ratio CPU/GPU/SoC): software throttle 99 °C, "
    "hardware shutdown 105 °C"
)
_THERMAL_SHUTDOWN_C = 105.0


def _device_worst_draw_w(device) -> tuple[float, str] | None:
    """Maximize device watts across nominal and every declared PowerMode.

    Thermal trip is a hard physical limit (sibling to ampacity): score the
    worst declared mode rather than a silently-defaulted nominal. Returns
    ``(watts, mode_name)`` or ``None`` when the device declares no power.
    """
    if device.power is None or device.power.draw is None:
        # an undeclared draw is unknown, never 0 W — same "no power" gap
        # this already reported before draw could be optional.
        return None
    worst_w = device.power.draw.base()
    worst_mode = "nominal"
    for mode in device.power.modes:
        load_w = mode.draw.base()
        if load_w > worst_w:
            worst_w = load_w
            worst_mode = mode.name
    return worst_w, worst_mode


def check_thermal_budget(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Junction-temperature estimate vs published module thermal limit.

    Estimate: device dissipated power × published enclosure/heatsink RθJA
    (junction-to-ambient) + declared ambient. Limit side: the model's
    declared ``thermal_limit`` with ``kind="throttle"`` and required
    provenance. Resistance side: the instance's ``thermal.resistance`` from
    the author's heatsink/enclosure datasheet. A declared limit with missing
    resistance, ambient, or draw emits ``not_scored`` naming the gap — never
    a guessed RθJA (two-source).

    Dissipation uses the worst declared device power mode (ampacity sibling):
    a throttle limit must hold regardless of which named mode the author had
    in mind. ``kind="shutdown"`` alone is ``not_scored`` — shutdown is
    context (see ``_THERMAL_SHUTDOWN_C``), not the fire threshold.
    """
    out: list[Finding] = []
    for device in c.devices:
        model = c.device_model(device.model)
        if model is None or model.thermal_limit is None:
            continue

        limit = model.thermal_limit
        limit_source = limit.provenance.source
        sources = [THERMAL_TRIP_SOURCE, f"device model {model.id} thermal limit: {limit_source}"]

        if limit.kind != "throttle":
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"devices/{device.id}/thermal",
                    observed=(
                        f"device model {model.id!r} declares a {limit.kind} trip at "
                        f"{limit.temperature}; thermal budget scores the software-throttle "
                        "trip point only"
                    ),
                    expected=(
                        "a thermal_limit with kind 'throttle' (shutdown may be cited as "
                        f"context; published hardware shutdown is "
                        f"{format_magnitude(_THERMAL_SHUTDOWN_C, 'C')})"
                    ),
                    owner=device.owner,
                    source="; ".join(sources),
                    action=(
                        "declare the module's software-throttle trip point with provenance, "
                        "or accept that this device is excluded from the thermal-budget gate"
                    ),
                )
            )
            continue

        thermal = device.thermal
        missing: list[str] = []
        if thermal is None or thermal.resistance is None:
            missing.append("published thermal resistance (RθJA)")
        if thermal is None or thermal.ambient is None:
            missing.append("declared ambient")
        draw = _device_worst_draw_w(device)
        if draw is None:
            missing.append("device power draw")

        if missing:
            gap = ", ".join(missing)
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"devices/{device.id}/thermal",
                    observed=(
                        f"device model {model.id!r} declares throttle limit "
                        f"{limit.temperature}, but {gap} "
                        f"{'is' if len(missing) == 1 else 'are'} missing; "
                        "thermal budget not scored"
                    ),
                    expected=(
                        "published enclosure/heatsink RθJA with provenance, declared "
                        "ambient, and device power draw so Tj = P·RθJA + Tamb can be scored"
                    ),
                    owner=device.owner,
                    source="; ".join(sources),
                    action=(
                        "lock the missing thermal operands with datasheet provenance, "
                        "or accept that this device is excluded from the thermal-budget gate"
                    ),
                )
            )
            continue

        assert thermal is not None and thermal.resistance is not None
        assert thermal.ambient is not None and draw is not None
        # Tj estimate vs published throttle limit — score on the DISSIPATION, not
        # merely on the operands being present. With a declared draw of 0 W,
        # Tj = 0·RθJA + Tamb collapses to ambient and "passes" a throttle limit
        # that was never tested against a load (the same vacuous-pass shape as the
        # other scored checks).
        if scores is not None and draw[0] > 0.0:
            scores.record(f"devices/{device.id}")
        if thermal.provenance is not None:
            sources.append(f"device {device.id} thermal resistance: {thermal.provenance.source}")

        power_w, draw_mode = draw
        r_th = thermal.resistance.base()
        ambient_c = thermal.ambient.base()
        estimated_c = ambient_c + power_w * r_th
        limit_c = limit.temperature.base()

        if estimated_c < limit_c:
            if scores is not None:
                headroom_c = limit_c - estimated_c
                scores.passed(
                    f"devices/{device.id}/thermal",
                    observed=f"estimated junction {format_magnitude(estimated_c, 'C')}",
                    expected=_with_enforcement_provenance(
                        f"<= {format_magnitude(limit_c, 'C')} throttle limit",
                        ("temperature", limit.temperature_enforced_by),
                    ),
                    margin=f"{format_magnitude(headroom_c, 'C')} headroom",
                )
            continue

        mode_note = f" (draw mode: {draw_mode})" if draw_mode != "nominal" else ""
        margin_c = estimated_c - limit_c
        out.append(
            Finding(
                severity="violation",
                quantity=f"devices/{device.id}/thermal",
                observed=(
                    f"estimated junction {format_magnitude(estimated_c, 'C')} "
                    f"(P={format_magnitude(power_w, 'W')} × "
                    f"RθJA={format_magnitude(r_th, 'C/W')} + "
                    f"Tamb={format_magnitude(ambient_c, 'C')}){mode_note}"
                ),
                expected=_with_enforcement_provenance(
                    (
                        f"<= {format_magnitude(limit_c, 'C')} published software-throttle "
                        f"limit (hardware shutdown context: "
                        f"{format_magnitude(_THERMAL_SHUTDOWN_C, 'C')})"
                    ),
                    ("temperature", limit.temperature_enforced_by),
                ),
                observed_quantity=Quantity(value=estimated_c, unit="C"),
                expected_quantity=Quantity(value=limit_c, unit="C"),
                delta=(
                    f"+{format_magnitude(margin_c, 'C')} above throttle "
                    f"({format_magnitude(limit_c, 'C')})"
                ),
                dependent=", ".join(device.depends) or None,
                owner=device.owner,
                source="; ".join(sources),
                action=(
                    "reduce dissipation, improve the published thermal path (lower RθJA), "
                    "lower ambient, or correct the sourced throttle limit"
                ),
            )
        )

    return out


# ---------------------------------------------------------------- RF / telemetry link budget


# Friis free-space path loss uses SI metres and hertz; c is exact SI definition.
_SPEED_OF_LIGHT_M_S = 299_792_458.0


def free_space_path_loss_db(range_m: float, frequency_hz: float) -> float:
    """Free-space path loss (dB) from Friis: 20·log10(4π·d·f/c).

    Operates on linear SI operands already extracted from Quantity; the result
    is a log-space dB figure for the link-budget sum. Not a units.py conversion.
    """
    if range_m <= 0.0 or frequency_hz <= 0.0:
        raise ValueError(
            f"FSPL requires positive range and frequency, got range_m={range_m}, "
            f"frequency_hz={frequency_hz}"
        )
    return 20.0 * math.log10(4.0 * math.pi * range_m * frequency_hz / _SPEED_OF_LIGHT_M_S)


def check_link_budget(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Compare RF link fade margin at a declared range against the requirement.

    A LINK BUDGET: transmit power + TX/RX antenna gains − free-space path loss
    − installation loss (feedline / connector / misc) versus receiver
    sensitivity, at the declared required range, leaving a fade margin. Fires
    when that margin falls below the contract's declared requirement (field
    design goal 20–30 dB). Under-specified fade margin is the leading
    documented cause of intermittent link loss.

    This is NOT the RF band-overlap check, which was rejected: band
    overlap has no true-positive class (2.4 GHz RC and WiFi coexist by
    design). A link budget has a real failure mode and a true-positive class.

    Engagement: only devices that declare ``link_budget`` are considered; an
    absent block is silent (optional, like an undeclared input-voltage range).
    ``installation_loss`` is mandatory on that block: omitting it fails
    schema validation so no operator ever sees an unqualified margin that
    silently ignored feedline loss. Missing radio datasheet operands, carrier
    frequency, or either antenna gain emit ``not_scored`` — never a bundled
    2 dBi monopole and never an assumed mid-band channel (e.g. 915 MHz).
    Arithmetic stays in log space (dBm + dBi − dB = dBm); dimensions are
    validated via Quantity.
    """
    out: list[Finding] = []
    for device in c.devices:
        budget = device.link_budget
        if budget is None:
            continue

        quantity = f"devices/{device.id}/link_budget"
        model = c.device_model(device.model)
        radio = model.radio if model is not None else None

        gaps: list[str] = []
        if radio is None:
            gaps.append(
                "device model radio datasheet operands (tx_power, rx_sensitivity, air_rate)"
            )
        if budget.carrier_frequency is None:
            gaps.append("carrier_frequency")
        if budget.tx_antenna is None:
            gaps.append("tx_antenna gain")
        if budget.rx_antenna is None:
            gaps.append("rx_antenna gain")

        if gaps:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=(
                        "link budget incomplete (" + ", ".join(gaps) + "); link budget not scored"
                    ),
                    expected=(
                        "radio datasheet operands, carrier frequency, and TX/RX "
                        "antenna gains with antenna-datasheet provenance"
                    ),
                    owner=device.owner,
                    source=(
                        model.provenance.source
                        if model is not None and model.provenance is not None
                        else None
                    ),
                    action=(
                        "declare the missing RF operands with provenance, or accept "
                        "that this radio's link budget is unchecked"
                    ),
                )
            )
            continue

        assert radio is not None
        assert budget.carrier_frequency is not None
        assert budget.tx_antenna is not None
        assert budget.rx_antenna is not None

        # Fade margin vs required — all RF operands populated.
        if scores is not None:
            scores.record(quantity)

        # Log-space sum: Pt + Gt + Gr − FSPL − L_install − Sr
        # (all dBm / dBi / dB). Quantity.base yields the dimension base
        # (dBm, dB, m, Hz) — no dBm↔W conversion; that belongs nowhere in
        # units.py.
        pt_dbm = radio.tx_power.base()
        sr_dbm = radio.rx_sensitivity.base()
        gt_dbi = budget.tx_antenna.gain.base()
        gr_dbi = budget.rx_antenna.gain.base()
        range_m = budget.required_range.base()
        freq_hz = budget.carrier_frequency.base()
        required_db = budget.fade_margin_required.base()
        install_loss_db = budget.installation_loss.base()

        fspl_db = free_space_path_loss_db(range_m, freq_hz)
        margin_db = pt_dbm + gt_dbi + gr_dbi - fspl_db - install_loss_db - sr_dbm

        if margin_db >= required_db:
            if scores is not None:
                headroom_db = margin_db - required_db
                scores.passed(
                    quantity,
                    observed=f"{format_magnitude(margin_db, 'dB')} fade margin",
                    expected=_with_enforcement_provenance(
                        f">= {format_magnitude(required_db, 'dB')} required",
                        ("fade_margin_required", budget.fade_margin_required_enforced_by),
                    ),
                    margin=f"{format_magnitude(headroom_db, 'dB')} to spare",
                )
            continue

        sources: list[str] = []
        if model is not None and model.provenance is not None:
            sources.append(f"device model {model.id} radio: {model.provenance.source}")
        sources.append(f"tx antenna: {budget.tx_antenna.provenance.source}")
        sources.append(f"rx antenna: {budget.rx_antenna.provenance.source}")
        shortfall_db = required_db - margin_db

        out.append(
            Finding(
                severity="violation",
                quantity=quantity,
                observed=(
                    f"fade margin {format_magnitude(margin_db, 'dB')} at "
                    f"{format_magnitude(range_m, 'm')} "
                    f"(FSPL {format_magnitude(fspl_db, 'dB')}, "
                    f"installation loss {format_magnitude(install_loss_db, 'dB')}, "
                    f"air rate {radio.air_rate})"
                ),
                expected=_with_enforcement_provenance(
                    f">= {format_magnitude(required_db, 'dB')} required fade margin",
                    ("fade_margin_required", budget.fade_margin_required_enforced_by),
                ),
                observed_quantity=Quantity(value=margin_db, unit="dB"),
                expected_quantity=Quantity(value=required_db, unit="dB"),
                delta=f"-{format_magnitude(shortfall_db, 'dB')}",
                dependent=(f"device_models/{device.model}/radio, devices/{device.id}/link_budget"),
                owner=device.owner,
                source="; ".join(sources),
                action=(
                    "raise TX power, improve antennas, reduce required range, "
                    "cut feedline/connector loss, or lower the air rate to a "
                    "more sensitive row — or correct the sourced operands"
                ),
            )
        )
    return out


# ---------------------------------------------------------------- scene placement


def _fmt_point(point: tuple[float, float, float]) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in point) + ")"


def check_scene_placement(
    c: Contract, scene: Scene, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Compare a device's declared ``mount_xyz`` to where the scene actually put it.

    Never run by ``lockwire check`` (the terminal never draws); this is a
    page-only check called from ``gui.build_payload`` when a scene is given.
    The YAML is the truth: this only scores whether the model agrees,
    within ``PLACEMENT_TOLERANCE_M``, in the model's single root-link frame.
    """
    resolved = resolve_scene(c, scene)
    parts_by_id: dict[str, "object"] = {}
    for part, part_id in zip(scene.parts, resolved.part_ids, strict=True):
        if part_id is not None:
            parts_by_id.setdefault(part_id, part)  # one id, first mesh's origin

    root, _transforms, _limitations = _fixed_frame_transforms(c)

    out: list[Finding] = []
    for device in c.devices:
        quantity = f"devices/{device.id}/placement"
        part = parts_by_id.get(device.id)
        if part is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed="not in the model",
                    expected="a part in the scene named by this device's id",
                    action=(
                        "add a node with extras.lockwire.id set to this device's id, "
                        "or name the node the id"
                    ),
                )
            )
            continue

        physical = device.physical
        if physical is None or physical.mount_frame is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed="no mount frame",
                    expected="a declared mount frame",
                    action="declare mount_frame (and mount_xyz) with a source and witness",
                )
            )
            continue
        if physical.mount_xyz is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed="drawn where the model put it",
                    expected="a declared mount point",
                    action="declare mount_xyz with a source and witness",
                )
            )
            continue

        expected_m = tuple(component.base() for component in physical.mount_xyz)
        frame_part = parts_by_id.get(physical.mount_frame)
        if physical.mount_frame != root and frame_part is not None:
            observed_m = tuple(
                a - b for a, b in zip(part.translation, frame_part.translation, strict=True)
            )
        else:
            observed_m = part.translation
        distance_m = math.sqrt(
            sum((a - b) ** 2 for a, b in zip(expected_m, observed_m, strict=True))
        )

        if scores is not None:
            scores.record(quantity)
        if distance_m <= PLACEMENT_TOLERANCE_M:
            if scores is not None:
                scores.passed(
                    quantity,
                    observed=f"{_fmt_point(observed_m)} m",
                    expected=f"{_fmt_point(expected_m)} m",
                    margin=f"{distance_m * 1000:.1f} mm off",
                )
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=quantity,
                observed=f"{_fmt_point(observed_m)} m in the model",
                expected=f"within 5 mm of {_fmt_point(expected_m)} m declared",
                delta=f"{distance_m * 1000:.1f} mm",
                action="move the part in the model, or declare the measured position with a witness",
            )
        )

    for lw_id in resolved.scene_only:
        out.append(
            Finding(
                severity="not_scored",
                quantity=f"scene/{lw_id}/unclaimed",
                observed="the model has a part the contract doesn't name",
                expected="a matching device, rail, bus or link in the contract",
                action="add it to the contract, or remove the id from the scene",
            )
        )
    return out
