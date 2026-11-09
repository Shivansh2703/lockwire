"""Record-level checks (rung 1): do the records agree with each other?

No robot required — these run in CI on every change to the contract or its
witnesses. Every check emits the one grammar (report.Finding). The
false-positive budget is a first-class requirement: a clean contract must
produce zero findings.
"""

import hashlib
import itertools
import fnmatch
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from lockwire import plugins, statics, units
from lockwire.advice import build_rail_alternatives
from lockwire.catalog import SourcedNumber, load_catalog, recognise
from lockwire.cli_line import cli_line
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
from lockwire.scene_urdf import mesh_root
from lockwire.scene_view import resolve_scene
from lockwire.schema import (
    ArtifactRef,
    Band,
    Contract,
    SCHEMA_VERSION,
    ModelRecord,
    Quantity,
    Provenance,
    Tolerance,
)
from lockwire.sdf import parse_sdf, read_sdf_root, read_sdf_topology, urdf_view
from lockwire.urdf import (
    _has_doctype_entity,
    describe_structure_drift,
    legacy_axis_joints,
    locked_record_hash,
    mesh_refs,
    parse_urdf,
    read_model_bytes,
    read_urdf_topology,
)

# the YAML is the truth. This is only how far the model in the scene is
# allowed to disagree with it before the disagreement is a finding, not a
# silent picture.
PLACEMENT_TOLERANCE_M = 0.005

# Raw bus bitrate is not usable payload throughput: arbitration, framing, retries,
# and latency collapse make sustained operation unsafe well before 100% load.
BUS_UTILIZATION_CEILING = 0.60


def _can_wire_bits(payload_bytes: int) -> int:
    """Worst-case classical-CAN bits on the wire for one message.

    Per frame of n data bytes: 47 framing bits (SOF 1, 11-bit ID, RTR, IDE, r0,
    DLC 4, CRC 15, CRC delimiter, ACK 2, EOF 7, interframe 3) + 8n data bits +
    worst-case bit stuffing floor((34 + 8n - 1) / 4). An 8-byte frame is
    47 + 64 + 24 = 135 bits for 64 payload bits (2.1x): without framing the
    60% payload ceiling was ~104% of the wire bitrate.
    """
    frames, tail = divmod(payload_bytes, 8)
    sizes = [8] * frames + ([tail] if tail else [])
    return sum(47 + 8 * n + (33 + 8 * n) // 4 for n in sizes)


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
        detail: str | None = None,
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
                detail=detail,
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


# Check slots filled by the recording plugin (module boundary). Keys:
# "actuator_cases", "condition_envelopes". An empty slot never passes silently.
PAID_CHECKS: dict[str, Callable[..., Any]] = {}


def _paid_check_missing(quantity: str, what: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=quantity,
        observed="not run: the recording checks did not load",
        expected=f"{what} checked",
        action=f"reinstall lockwire to check {what}, or remove them from the contract",
    )


def run_record_checks(
    contract: Contract,
    current_conditions: dict[str, float] | None = None,
    *,
    contract_path: Path | None = None,
    current_edge_values: dict[str, float] | None = None,
    servo: str | None = None,
    actuator_evidence: Mapping[str, ArtifactRef] | None = None,
    evidence_root: Path | None = None,
    contract_sha256: str | None = None,
    model: str | None = None,
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
    findings += check_gravity_reach(contract, scores=scores)
    findings += check_bus_bandwidth(contract, scores=scores)
    findings += check_zero_mass(contract, contract_path=contract_path)
    findings += check_unusable_effort(contract)
    findings += check_servo_stall(contract, servo or contract.servo, scores=scores)
    findings += check_mechanisms(contract, scores=scores)
    findings += check_urdf_drift(contract, scores=scores, contract_path=contract_path)
    findings += check_model_values(contract, scores=scores, contract_path=contract_path)
    findings += check_mesh_drift(contract, scores=scores, contract_path=contract_path)
    findings += check_datasheet_agreement(
        contract, scores=scores, model=model, contract_path=contract_path
    )
    for pack_check in (*CHECK_PACK_1, *CHECK_PACK_2):
        findings += pack_check(contract, contract_path=contract_path)
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
    findings, accepted = _set_aside_accepted(contract, findings)
    return RecordReport(
        findings,
        compute_system_cg(contract),
        scored=scored,
        scored_keys=scores.keys,
        scored_records=scores.records,
        actuator_case_reports=actuator_case_reports,
        accepted=accepted,
    )


_LOCK_DRIFT = re.compile(
    r"model/structure_hash|model/links/[^/]+/(mass|com|inertia|mesh)|model/external_assets/.+"
)


def is_lock_drift(finding: Finding) -> bool:
    """The model no longer matches its lock (edited, restructured or unparseable since lock).

    Only re-locking clears it: never accepted, adopted or baselined. The producers are
    check_urdf_drift and check_model_values (and check-sim's sim-vs-lock rows); a
    warning with the same id (e.g. an impossible inertia) is not drift.
    """
    return finding.severity == "violation" and bool(_LOCK_DRIFT.fullmatch(finding.quantity))


def _set_aside_accepted(
    contract: Contract, findings: list[Finding]
) -> tuple[list[Finding], list[Finding]]:
    """Split off findings the lockfile accepted at exactly their observed value."""
    pins = {(a.id, a.observed): a for a in contract.accepted or []}
    kept, accepted = [], []
    for finding in findings:
        row = pins.get((finding.quantity, finding.observed))
        if row is None or is_lock_drift(finding):
            kept.append(finding)
            continue
        how = (
            f"accepted ({row.reason}, witness {row.provenance.witness}, {row.provenance.locked_at})"
        )
        accepted.append(finding.model_copy(update={"action": how, "next_command": None}))
    return kept, accepted


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
    fraction_warned: set[str] = set()
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
            tol = rail.output_tolerance_pct
            if tol is not None and 0 < tol < 0.5 and rail.id not in fraction_warned:
                # 0.05 meant as 5% passes the schema and checks as 0.05%.
                fraction_warned.add(rail.id)
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"topology/rails/{rail.id}/output_tolerance_pct",
                        observed=f"{tol:g} (percent) looks like a fraction",
                        expected="percent, e.g. 5 for +/-5%",
                        owner=device.owner,
                        action="if this was meant as a fraction, multiply it by 100",
                    )
                )
            # the worst case is checked on both sides.
            for bound, bound_v, worst_v in (
                ("max", maximum_v, rail_v * (1 + (tol or 0) / 100)),
                ("min", minimum_v, rail_v * (1 - (tol or 0) / 100)),
            ):
                if tol is None or (worst_v <= bound_v if bound == "max" else worst_v >= bound_v):
                    continue
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"devices/{device.id}/power/rail",
                        observed=(
                            f"{format_magnitude(rail_v, 'V')} nominal, +/-{tol:g}% -> "
                            f"{format_magnitude(worst_v, 'V')}"
                        ),
                        expected=(
                            f"{'at most' if bound == 'max' else 'at least'} device model "
                            f"{model.id!r}'s {format_magnitude(bound_v, 'V')} {bound} input"
                        ),
                        delta=f"{'+' if bound == 'max' else '-'}"
                        f"{format_magnitude(abs(worst_v - bound_v), 'V')}",
                        dependent=(
                            f"topology/rails/{rail.id}/output_tolerance_pct, "
                            f"device_models/{model.id}/input_voltage_range"
                        ),
                        owner=device.owner,
                        action=(
                            "set the converter closer to the window or use a tighter one, or power "
                            f"this device from a rail whose worst-case output stays inside its {bound}"
                        ),
                    )
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


def _missing_rail(c: Contract, rail_id: str) -> str | None:
    """The first rail id on the way up from ``rail_id`` that is not declared, else None."""
    seen: set[str] = set()
    while rail_id not in (None, "battery") and rail_id not in seen:
        seen.add(rail_id)
        rail = c.rail(rail_id)
        if rail is None:
            return rail_id
        rail_id = rail.upstream
    return None


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
            battery_unknown.add(
                device.id
            )  # its rail is undeclared: the path to the battery is unknown
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
            if parent is None:
                battery_unknown.add(device.id)  # upstream names a missing rail: never drop its draw
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
                        cluster_root=b.rail,
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
                            draw_mode=b.draw_mode,
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
            unknown_mass: list[str] = []
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
                        unknown_mass.append(d.id)
                        continue
                    total_kg += d.physical.mass.base()
                    contributors.append(d.id)
            if b.sources in ("links", "links+devices") and c.model is not None:
                for link in c.model.links:
                    if link.mass is None:
                        unknown_mass.append(link.name)  # same gap as a massless device
                        continue
                    total_kg += link.mass.base()
                    contributors.append(link.name)
            if unknown_mass and not total_kg > limit:
                # a partial sum is not a total; it can still violate, never pass.
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"budgets/{b.id}",
                        observed=f"mass unknown for {', '.join(unknown_mass)}; total not scored",
                        expected="every device and link in this budget to declare a mass",
                        owner=b.owner,
                        action="declare the missing masses, or accept this budget unchecked",
                    )
                )
            limit_kg = limit
            # Score on the summed MAGNITUDE, not on the contributor list: a
            # non-empty list of zero-mass witnesses sums to 0.0 kg and would
            # vacuous-pass the limit. Masses are non-negative.
            # masses only add: a known partial sum over the limit is a definite, counted violation.
            if scores is not None and total_kg > 0.0 and (not unknown_mass or total_kg > limit_kg):
                scores.record(f"budgets/{b.id}")
            if total_kg > limit_kg:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"budgets/{b.id}",
                        observed=format_magnitude(total_kg, "kg")
                        + (f"; mass unknown for {', '.join(unknown_mass)}" if unknown_mass else ""),
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
            elif scores is not None and total_kg > 0.0 and not unknown_mass:
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
            if b.nominal is not None and total_kg > 0.0:
                nominal_kg = b.nominal.base()
                gap_pct = (total_kg - nominal_kg) / nominal_kg * 100
                # a partial sum (unknown masses) can only prove "too heavy", never "too light"
                # 1e-9 relative slack: 0.1 kg + 0.2 kg is 0.30000000000000004 kg, not off nominal
                tol_kg = max((b.tolerance_pct or 0.0) / 100 * nominal_kg, 1e-9 * nominal_kg)
                if abs(total_kg - nominal_kg) > tol_kg and (not unknown_mass or gap_pct > 0):
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=f"budgets/{b.id}/nominal",
                            observed=format_magnitude(total_kg, "kg"),
                            expected=f"{format_magnitude(nominal_kg, 'kg')} +/- {b.tolerance_pct or 0:g}%",
                            observed_quantity=Quantity(value=total_kg, unit="kg"),
                            expected_quantity=Quantity(value=nominal_kg, unit="kg"),
                            delta=f"{gap_pct:+.0f}% ({total_kg - nominal_kg:+.2f} kg)",
                            dependent=", ".join(contributors),
                            owner=b.owner,
                            source=b.provenance.source if b.provenance is not None else None,
                            witness=b.provenance.witness if b.provenance is not None else None,
                            locked_at=b.provenance.locked_at if b.provenance is not None else None,
                            action="the summed link masses disagree with the declared nominal mass; fix the model or the declared value",
                        )
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
                        observed=(
                            f"{device_id}'s path to the battery names rail '{gap}', which is not declared"
                            if (gap := _missing_rail(c, c.device(device_id).power.rail))
                            else unknown_draw_message(device_id, c.device(device_id).power.rail)
                        ),
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

    Draw side: the worst rolled-up watts across nominal and every declared
    PowerMode (``_worst_rail_power``), divided by
    the rail's worst-case voltage: its declared ``voltage_range.min`` when
    present (the conservative bound — lower voltage means higher current for
    the same watts), else its single ``voltage``. A rail with no voltage fact
    (neither) or an unsummable draw (any consumer of unknown draw) is
    ``not_scored`` rather than guessing.
    """
    if c.topology is None:
        return []
    out: list[Finding] = []
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
        if voltage_v <= 0.0:
            # not_scored like the neighbouring "no voltage fact" case, never a divide by zero.
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed=f"rail {rail.id!r} declares a non-positive voltage",
                    expected="a positive rail voltage",
                    action="correct the rail's voltage, or accept its current_limit as not scored",
                )
            )
            continue

        # every declared mode is scored, the worst one wins (same rule as check_ampacity).
        load_w, worst_mode, witnesses, unknown_consumers = _worst_rail_power(c, rail.id)
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

        contributors = sorted(witnesses)
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
                    observed=(
                        f"{format_magnitude(draw_a, 'A')} computed draw on rail {rail.id!r} "
                        f"(mode: {worst_mode})"
                    ),
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
                    # one fact with the rail's rail_power budget: one card per rail
                    cluster_root=rail.id,
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


def _peak_shortfall(device, joint, peak_nm: float) -> Finding:
    required = joint.limits.effort
    return Finding(
        severity="violation",
        quantity=f"devices/{device.id}/drives_joint",
        observed=f"{peak_nm:.2f} N*m peak (stall) geared torque",
        expected=_with_enforcement_provenance(
            f">= {required:.2f} N*m joint effort limit",
            ("effort", joint.limits.effort_enforced_by),
        ),
        delta=f"-{required - peak_nm:.2f} N*m",
        dependent=f"model/joints/{joint.name}/limits/effort",
        owner=device.owner,
        action=(
            "the joint can never reach its limit: select a stronger actuator, "
            "change gearing, or correct the sourced limits"
        ),
    )


_REACH_ANGLES = (0.0, math.pi / 2, -math.pi / 2)
# ponytail: per-joint cap of 20k poses (about 2 s); past it the grid thins to {-90, 0, +90}
# (limit points go first), and a joint still over (it carries ~10+ rotary joints, e.g. a humanoid
# waist) is not_scored. Upgrade path: a real search over the other joints instead of a grid.
_REACH_MAX_POSES = 20_000
_ROTARY = ("revolute", "continuous")


def _reach_not_scored(why: str) -> list[Finding]:
    return [
        Finding(
            severity="not_scored",
            quantity="model/joints/limits/effort",
            observed=f"gravity reach not scored: {why}",
            expected="a kinematic tree of revolute/continuous joints with axes, masses and CoMs",
            action="gravity reach is unchecked for this model; check the effort limits by hand",
        )
    ]


def _sinusoid_peak(a, b, limits) -> tuple[np.ndarray, np.ndarray]:
    """Max of |a cos q + b sin q| over the joint's [lower, upper], and the angle that gives it.

    ``a``/``b`` are the signed torque at q=0 and q=pi/2. The peak sqrt(a^2+b^2) sits at
    q = atan2(b, a) + n*pi; if no such angle is inside the limits the larger limit end wins.
    """
    lo = limits.lower if limits is not None and limits.lower is not None else -math.inf
    hi = limits.upper if limits is not None and limits.upper is not None else math.inf
    phi = np.arctan2(b, a)
    if math.isfinite(lo):
        peak_q = phi + np.ceil((lo - phi) / math.pi - 1e-12) * math.pi
        ok = peak_q <= hi + 1e-9
    else:
        peak_q = phi + (
            np.floor((hi - phi) / math.pi + 1e-12) * math.pi if math.isfinite(hi) else 0
        )
        ok = np.ones(len(a), dtype=bool)
    best, at = np.where(ok, np.hypot(a, b), 0.0), peak_q
    for end in (lo, hi):
        if math.isfinite(end):
            t = np.abs(a * math.cos(end) + b * math.sin(end))
            best, at = np.where(t > best, t, best), np.where(t > best, end, at)
    return best, at


def check_gravity_reach(c: Contract, *, scores: _ScoreBook | None = None) -> list[Finding]:
    """Static gravity torque at the worst pose must fit each joint's effort limit.

    Each joint's own angle is solved exactly: its torque is a*cos(q) + b*sin(q), so the peak over
    its limits is sqrt(a^2 + b^2) when the peak angle is allowed, else the larger limit end. The
    other joints take the grid below. A prismatic joint scores its weight along its axis (N).

    Works on a kinematic tree, so the arm starter scores with its gripper: the torque at joint J depends
    only on the rotary joints on the root-to-J path and in J's subtree, so each J gets its own
    pose grid over that set: 0 and +/-90 deg plus the declared lower and upper limit (within
    limits). Prismatic, planar and floating joints are held at zero. A link's declared
    ``payload`` is a point mass at its origin (payload at the tool tip); a device whose
    ``mount_frame`` names a link adds its mass at ``mount_xyz`` (origin when unset). Silent when the model has no rotary
    joint, no link mass or no single root; ``not_scored`` says why when it cannot score
    (closed chain, zero or missing axis, a massive link with no CoM, a joint over the pose
    cap). Silent for joints with no effort limit.
    """
    if c.model is None:
        return []
    joints = c.model.joints
    rotary = [j for j in joints if j.type in _ROTARY]
    if not rotary:
        return []
    if any(j.axis is None for j in rotary):
        return _reach_not_scored("a rotary joint has no axis")
    no_com = [k.name for k in c.model.links if k.mass and k.mass.base() > 0 and k.com is None]
    if no_com:
        return _reach_not_scored(f"link '{no_com[0]}' has mass but no centre of mass (CoM)")
    by_child = {j.child: j for j in joints}
    kids: dict[str, list[Any]] = {}
    for j in joints:
        kids.setdefault(j.parent, []).append(j)

    def reach_set(j) -> tuple[int, frozenset[str]]:
        """(rotary joints above j, rotary joints on root->j plus j's subtree); loop-safe."""
        names, seen, up = (
            {j.name},
            set(),
            by_child.get(j.parent),
        )  # a slide's weight ignores its subtree's angles
        while up is not None and up.name not in seen:
            seen.add(up.name)
            names |= {up.name} if up.type in _ROTARY else set()
            up = by_child.get(up.parent)
        depth, stack, seen = len(names) - 1, ([j.child] if j.type in _ROTARY else []), set()
        while stack:
            name = stack.pop()
            if name not in seen:
                seen.add(name)
                names |= {k.name for k in kids.get(name, []) if k.type in _ROTARY}
                stack += [k.child for k in kids.get(name, [])]
        return depth, frozenset(names)

    def grid(names: list[str], angles: tuple[float, ...], ends: bool = True) -> list[list[float]]:
        out = []
        for n in names:
            if by_name[n].type not in _ROTARY:
                out.append([0.0])  # a slide is held at zero
                continue
            lim = by_name[n].limits
            lo = lim.lower if lim and lim.lower is not None else -math.inf
            hi = lim.upper if lim and lim.upper is not None else math.inf
            pts = {
                a
                for a in (*angles, *((lo, hi) if ends else ()))
                if lo <= a <= hi and math.isfinite(a)
            }
            out.append(sorted(pts) or [min(max(0.0, lo), hi)])
        return out

    slides = [
        j for j in joints if j.type == "prismatic" and j.axis and any(j.axis)
        and j.limits is not None and j.limits.effort_limit is not None
    ]  # fmt: skip
    by_name = {j.name: j for j in (*rotary, *slides)}
    sets = {j.name: reach_set(j) for j in by_name.values()}
    held = [j.name for j in joints if j.type not in ("fixed", *_ROTARY)]
    note = f"; {', '.join(held[:2])}{', ...' if len(held) > 2 else ''} held at zero" if held else ""
    link_names = {lk.name for lk in c.model.links}
    mounted: dict[str, list[tuple]] = {}
    uncounted: list[str] = []
    for d in c.devices:
        p = d.physical
        if p is None or (p.mass is None and p.mount_frame is None):
            continue  # nothing declared about where or how heavy: not a mounted device
        if p.mass is None or p.mount_frame not in link_names:
            uncounted.append(
                f"{d.id} (no mass)"
                if p.mass is None
                else f"{d.id} (frame {p.mount_frame!r} is not a link)"
            )
            continue
        xyz = tuple(v.base() for v in p.mount_xyz) if p.mount_xyz else (0.0, 0.0, 0.0)
        mounted.setdefault(p.mount_frame, []).append((xyz, p.mass.base()))
    if uncounted:
        more = f", +{len(uncounted) - 3} more" if len(uncounted) > 3 else ""
        note += f"; {len(uncounted)} device masses not counted: {', '.join(uncounted[:3])}{more}"
    worst: dict[str, tuple[Any, dict[str, float], str, str]] = {}
    done: list[frozenset[str]] = []
    over: set[frozenset[str]] = set()
    for j in sorted(by_name.values(), key=lambda j: sets[j.name][0]):  # top of each run first
        key = sets[j.name][1]
        if any(key <= d for d in done) or key in over:
            continue  # a superset grid under an ancestor already covered this joint
        names = sorted(key)
        choices, how = grid(names, _REACH_ANGLES), "0/+-90 deg and limit points"
        if math.prod(map(len, choices)) > _REACH_MAX_POSES:
            choices, how = (
                grid(names, _REACH_ANGLES, False),
                "0/+-90 deg only (limit points dropped)",
            )
        if math.prod(map(len, choices)) > _REACH_MAX_POSES:
            over.add(key)
            continue
        done.append(key)
        try:
            plan = statics.gravity_plan(c.model, under=j.name, devices=mounted)
        except statics.ChainError as e:
            return _reach_not_scored(str(e))
        if plan is None:
            return []  # no single root or an off-frame CoM: silent, as before
        for kk, joint in enumerate(plan.joints):
            lim = joint.limits.effort_limit if joint.limits is not None else None
            if lim is None or (joint.type == "prismatic") != (j.type == "prismatic"):
                continue
            if joint.type == "prismatic" and joint.name != j.name:
                continue
            # the joint's own angle is solved exactly below, so only the other joints take the grid
            poses = list(
                itertools.product(
                    *[[0.0] if n == joint.name else c for n, c in zip(names, choices)]
                )
            )
            ang = np.array(poses, dtype=float).reshape(len(poses), len(names))
            t0 = statics.plan_torques(plan, names, ang, signed=True)[:, kk]
            best, q = t0, np.zeros(len(poses))
            if joint.type in _ROTARY:
                ang[:, names.index(joint.name)] = math.pi / 2
                t1 = statics.plan_torques(plan, names, ang, signed=True)[:, kk]
                best, q = _sinusoid_peak(t0, t1, joint.limits)
            i = int(best.argmax())
            if joint.name not in worst or best[i] > worst[joint.name][0].torque_nm:
                kg = float((plan.carries @ plan.payload)[kk])
                row = statics.JointTorque(joint.name, float(best[i]), lim, lim - float(best[i]), kg)
                nd = int((plan.carries @ (plan.device > 0))[kk])
                dkg = float((plan.carries @ plan.device)[kk])
                dev = f"includes {nd} device masses ({dkg:g} kg)" if nd else "no device masses"
                pose = {n: v for n, v in zip(names, poses[i]) if by_name[n].type in _ROTARY}
                if joint.type in _ROTARY:
                    pose[joint.name] = float(q[i])
                worst[joint.name] = (row, pose, how, dev)
    out: list[Finding] = []
    skipped = sorted(n for n, (_, k) in sets.items() if k in over and n not in worst)
    if skipped:
        out += _reach_not_scored(
            f"{', '.join(skipped[:3])}{', ...' if len(skipped) > 3 else ''} carry too many "
            f"rotary joints to keep the +/-90 deg poses within the {_REACH_MAX_POSES} pose grid"
        )
    for name, (r, pose, how, dev) in worst.items():
        quantity = f"model/joints/{name}/limits/effort"
        payload = f"with {r.payload_kg:g} kg payload" if r.payload_kg else "no payload declared"
        unit, what = (
            ("N", "N weight along the slide axis")
            if by_name[name].type == "prismatic"
            else ("N*m", "N*m static gravity torque")
        )
        observed = (
            f"{r.torque_nm:.2f} {what} at pose "
            + ", ".join(f"{k}={round(math.degrees(v))}deg" for k, v in pose.items())
            + f" (peak over its own range, other joints at {how}; {payload}; "
            f"{dev}{note})"
        )
        expected = f"<= {r.limit_nm:.2f} {unit} joint effort limit"
        if scores is not None:
            scores.record(quantity)
        if r.torque_nm <= r.limit_nm:
            if scores is not None:
                scores.passed(
                    quantity,
                    f"{r.torque_nm:.2f} {unit}" + (f" {payload}" if r.payload_kg else ""),
                    expected,
                    f"{r.margin_nm:.2f} {unit} headroom",
                    detail=observed,
                )
            continue
        out.append(
            Finding(
                severity="violation",
                quantity=quantity,
                observed=observed,
                expected=expected,
                delta=f"+{r.torque_nm - r.limit_nm:.2f} {unit}",
                dependent=quantity,
                action=(
                    "the joint cannot hold this pose against gravity: raise the effort limit "
                    "with a stronger actuator, shorten the reach, or lighten the links"
                ),
            )
        )
    return out


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
        if (
            actuator is None
            or (actuator.torque_continuous is None and actuator.torque_peak is None)
            or actuator.gear_ratio is None
        ):
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
        required = joint.limits.effort
        if actuator.torque_continuous is None:
            # Peak-only (e.g. a hobby servo's stall torque): it cannot hold the limit if even
            # the peak falls short. When continuous is also declared, the check below runs
            # first; schema forces peak >= continuous, so continuous failing covers peak.
            peak_nm = actuator.torque_peak * actuator.gear_ratio
            if required > peak_nm:
                out.append(_peak_shortfall(device, joint, peak_nm))
            elif scores is not None:
                scores.passed(
                    f"devices/{device.id}/drives_joint",
                    observed=f"{required:.2f} N*m joint effort limit",
                    expected=_with_enforcement_provenance(
                        f"<= {peak_nm:.2f} N*m peak (stall) geared torque",
                        ("effort", joint.limits.effort_enforced_by),
                    ),
                    margin=f"{peak_nm - required:.2f} N*m headroom",
                )
            continue
        available = actuator.torque_continuous * actuator.gear_ratio
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
        silent = [
            device.id
            for device in c.devices
            if device.identity is not None
            and device.identity.bus_ref == bus.id
            and device not in contributors
        ]
        # CAN and the byte-framed buses are scored on wire bits; USB, Ethernet and SPI stay on
        # payload bits (their framing is not modelled here). Framing rules: UART 8N1 sends 10
        # bits per byte (start + 8 data + stop); I2C sends 9 bits per byte (8 + ACK) and we
        # assume 2 extra bytes per message for the start/address.
        bits, unit = {
            "can": (_can_wire_bits, "bit/s on the wire"),
            "serial": (lambda n: n * 10, "bit/s on the wire (8N1 framing)"),
            "i2c": (
                lambda n: (n + 2) * 9,
                "bit/s on the wire (assumes 2 address/start bytes per message)",
            ),
        }.get(bus.kind, (lambda n: n * 8, "bit/s payload (framing overhead not modelled)"))
        used_bps = sum(
            device.driver.nominal_rate_hz * bits(device.driver.payload_bytes)
            for device in contributors
        )
        ceiling_bps = bus.bitrate * BUS_UTILIZATION_CEILING
        # Bitrate ceiling vs declared traffic — score on the summed TRAFFIC, not
        # on the contributor list. A device declaring nominal_rate_hz = 0 is a
        # contributor that carries nothing: 0 bps against the ceiling passes for
        # no reason.
        if scores is not None and used_bps > 0 and not silent:
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
        if silent:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/buses/{bus.id}/bandwidth",
                    observed=f"{', '.join(silent)} declare no rate or payload; their traffic is unweighed",
                    expected="every device on the bus to declare nominal_rate_hz and payload_bytes",
                    dependent=", ".join(silent),
                    action="declare each device's traffic, or accept this bus as not fully scored",
                )
            )
        if used_bps > ceiling_bps:
            out.append(
                Finding(
                    severity="violation",
                    quantity=f"topology/buses/{bus.id}/bandwidth",
                    observed=f"{used_bps:.0f} {unit}",
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
        elif scores is not None and used_bps > 0 and not silent:
            headroom_bps = ceiling_bps - used_bps
            scores.passed(
                f"topology/buses/{bus.id}/bandwidth",
                observed=f"{used_bps:.0f} {unit}",
                expected=(
                    f"<= {ceiling_bps:.0f} bit/s ({BUS_UTILIZATION_CEILING:.0%} of raw bitrate)"
                ),
                margin=(
                    f"{headroom_bps:.0f} bit/s headroom ({headroom_bps / ceiling_bps * 100:.0f}%)"
                ),
            )
    return out


# ---------------------------------------------------------------- model sanity


# A leaf link named like this is a real tool, not a mount frame.
# "tool0" / "tcp" / "tcp_frame" are frames on UR/ABB arms, so they are not matched.
_TOOL_LINK = re.compile(
    r"(?<![a-z])(tool(?![0-9])|gripper|hand|finger|end.?effector|ee|wrist)(?![a-z])", re.I
)


def check_zero_mass(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Missing or non-positive link mass is derived from the canonical link
    records so a stale zero_mass_links cache cannot hide incomplete inertia."""
    if c.model is None:
        return []
    massless = [k.name for k in c.model.links if k.mass is None or k.mass.base() <= 0.0]
    # A massless link with no massive link below it is a sensor/mount frame: one summary line.
    kids: dict[str, list[str]] = {}
    for j in c.model.joints:
        kids.setdefault(j.parent, []).append(j.child)
    bare = set(massless)

    def carries_mass(name: str, seen: frozenset[str] = frozenset()) -> bool:
        return name not in seen and (
            name not in bare or any(carries_mass(k, seen | {name}) for k in kids.get(name, []))
        )

    # The contract keeps no geometry, so a pure frame (base_footprint, world) is the narrowest
    # lookalike: a massless root named like a frame whose every joint is fixed or floating and that no device mounts on.
    mounted = {d.physical.mount_frame for d in c.devices if d.physical}
    children = {j.child for j in c.model.joints}
    fixed_root = {
        n
        for n in massless
        if n
        in ("world", "map", "odom", "base_footprint")  # a named frame; base_link may be a real body
        and n not in children
        and n not in mounted
        and kids.get(n)
        and all(j.type in ("fixed", "floating") for j in c.model.joints if j.parent == n)
    }
    leaves = [
        n for n in massless if n in fixed_root or not any(carries_mass(k) for k in kids.get(n, []))
    ]
    tools = [n for n in leaves if _TOOL_LINK.search(n)]
    leaves = [n for n in leaves if n not in tools]
    findings = [
        Finding(
            severity="warning",
            quantity=f"model/links/{name}/mass",
            observed="zero or missing mass"
            + (" on a tool-like leaf link (the frame fold would hide it)" if name in tools else ""),
            expected="a positive mass with units",
            action=(
                "fill in the link's inertial in the source model, then run "
                f"`{cli_line('lockwire', 'declare', contract_path or 'lockwire.yaml')} model/links/{name}/mass "
                '"<mass> kg" --source "datasheet" --witness "you"` '
                "(replace <mass>), or accept as massless with a Tier C assumption"
            ),
        )
        for name in massless
        if name not in leaves
    ]
    # ponytail: name heuristic only; the schema has no frame mark to opt out, so declare a mass.
    # A pure root frame (world, odom, ...) is no body at all: not even the "normal" note.
    leaves = [n for n in leaves if n not in fixed_root]
    if leaves:
        shown = ", ".join(leaves[:2]) + (", ..." if len(leaves) > 2 else "")
        findings.append(
            Finding(
                severity="warning",
                quantity="model/links/massless_frames",
                observed=f"{len(leaves)} massless frames (sensor/mount frames, normal): {shown}",
                expected="mass only on real bodies",
                action="none for pure frames",
            )
        )
    return findings


def servo_stall_torque(servo: str) -> SourcedNumber:
    """The stall torque (N*m) of catalog part ``servo``; ValueError names the parts that have one."""
    parts = [p for p in load_catalog().parts if "stall_torque" in p.numbers]
    for part in parts:
        if part.id == servo:
            return part.numbers["stall_torque"]
    known = ", ".join(sorted(p.id for p in parts))
    raise ValueError(f"no catalog servo {servo!r} with a stall torque (known: {known})")


def check_servo_stall(
    c: Contract, servo: str | dict[str, str] | None, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Each revolute/continuous joint's effort limit vs the stall torque of the servo that
    drives it, named by `check --servo <id>` or the lockfile's `servo:` line.

    `servo:` is one catalog id for every joint, or a map of joint-name pattern -> id
    (`{"arm_*": feetech.sts3215}`); the first matching pattern wins and an unmatched joint is
    not compared. `check --servo` overrides the lockfile line.
    """
    if servo is None or c.model is None:
        return []
    rules = servo if isinstance(servo, dict) else {"*": servo}
    stalls, out = {}, []
    for sid in dict.fromkeys(rules.values()):
        try:
            stalls[sid] = servo_stall_torque(sid)
        except ValueError as unknown:
            out.append(
                Finding(
                    severity="warning",
                    quantity="servo",
                    observed=str(unknown),
                    expected="a catalog servo id with a stall torque",
                    action="fix the lockfile's servo: line to one of the known ids",
                )
            )
    for joint in c.model.joints:
        effort = joint.limits.effort if joint.limits else None
        sid = next((s for pat, s in rules.items() if fnmatch.fnmatchcase(joint.name, pat)), None)
        if joint.type not in _ROTARY or effort is None or effort <= 0 or sid not in stalls:
            continue
        stall = stalls[sid]
        quantity = f"model/joints/{joint.name}/limits/effort"
        if scores is not None:
            scores.record(quantity)
        if effort <= stall.value:
            if scores is not None:
                scores.passed(
                    quantity,
                    observed=f"{effort:.2f} N*m joint effort limit",
                    expected=f"<= {stall.value:.2f} N*m {sid} stall torque",
                    margin=f"{stall.value - effort:.2f} N*m headroom",
                )
            continue
        out.append(
            Finding(
                severity="violation",
                quantity=quantity,
                observed=f"{effort:.2f} N*m joint effort limit",
                expected=f"<= {stall.value:.2f} N*m, the {sid} stall torque",
                delta=f"{effort / stall.value:.1f}x the stall torque",
                source=stall.source,
                action=(
                    f"a simulator will let {joint.name} push {effort:g} N*m, which a {sid} cannot; "
                    f"set its effort to {stall.value:.2f} or less (ignore if another motor drives it)"
                ),
            )
        )
    return out


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
        and (m.actuator.torque_continuous is not None or m.actuator.torque_peak is not None)
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
        if m.verdict != "locking":
            rockers = [j for j in m.declared_fully_rotating if j not in m.rotating_joints]
            if rockers:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=quantity,
                        observed=f"{', '.join(rockers)} only swings between two dead centres; "
                        f"only {' and '.join(m.rotating_joints)} turn all the way round. {m.text}",
                        expected=f"{', '.join(rockers)} declared to turn all the way round",
                        action=(
                            f"re-declare {', '.join(rockers)} as a revolute joint with the "
                            "limits it actually reaches"
                        ),
                    )
                )
            declared = ", ".join(j for j in m.declared_fully_rotating if j in m.rotating_joints)
            if not declared:
                continue
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
                next_command=cli_line("lockwire", "start") + " <urdf>",
            )
        ]

    path = Path(named)
    if contract_path is not None:
        path = resolve_source(contract_path, named) or path
    if not path.is_file():
        if named.lower().startswith(("http://", "https://")):
            observed = "URDF is a web link; not re-checked offline"
        elif path.is_absolute():
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
                action=(
                    "download the URDF next to the lockfile to re-check it"
                    if named.lower().startswith(("http://", "https://"))
                    else "restore the URDF or correct model provenance to its on-disk path"
                ),
                next_command=cli_line("lockwire", "start") + " <urdf>",
            )
        ]

    try:
        topology = (
            read_sdf_topology(path) if path.suffix.lower() == ".sdf" else read_urdf_topology(path)
        )
    except (OSError, ET.ParseError, ValueError, TypeError) as error:
        try:
            needs_args = "Undefined substitution argument" in str(
                error
            ) or "<xacro:arg" in path.read_text(errors="replace")
        except OSError:
            needs_args = False
        if needs_args:  # xacro args are not kept in the lock, so this is a coverage gap
            return [
                Finding(
                    severity="not_scored",
                    quantity="model/structure_hash",
                    observed=(
                        f"the model needs xacro arguments ({error}); structure drift not scored"
                    ),
                    expected="a readable URDF at the provenance path",
                    source=str(path),
                    action=f"run {cli_line('lockwire', 'start', path)} --xacro-arg name=value to re-check it",
                )
            ]
        if not isinstance(error, OSError) and (c.model.joints or c.model.links):
            # it parsed when the lock was written (the lock holds its links/joints)
            return [
                Finding(
                    severity="violation",
                    quantity="model/structure_hash",
                    observed=f"the model file no longer parses since it was locked ({error})",
                    expected="a URDF that parses and matches the locked structure",
                    source=str(path),
                    action="fix the URDF, or re-lock the model from a file that parses",
                )
            ]
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
    # A match leaves no record: it says "unchanged since lock", not "adds up";
    # report.render shows it as its own "model check" bucket.
    if disk_hash == c.model.structure_hash:
        return []
    # A lock written before the default axis was filled in (or before 0.5) reads the same file.
    legacy = c.schema_version != SCHEMA_VERSION
    if c.model.joints and locked_record_hash(c.model, topology, legacy) == c.model.structure_hash:
        old = legacy_axis_joints(c.model, topology) if legacy else []
        if not old:
            return []
        return [
            Finding(
                severity="warning",
                quantity="model/structure_hash",
                observed=(
                    f"this schema {c.schema_version} lock read <axis/> with no xyz as 0 0 1; the "
                    f"URDF spec (and schema {SCHEMA_VERSION}) reads 1 0 0: joint {', '.join(old)}"
                ),
                expected="each locked joint axis read the way the URDF spec defines it",
                source=str(path),
                action="confirm those joints turn about X (or write their xyz), then re-lock",
                next_command=_relock_command(path, contract_path),
            )
        ]

    return [
        Finding(
            severity="violation",
            quantity="model/structure_hash",
            observed=describe_structure_drift(c.model, topology),
            expected="structure hash matching the on-disk URDF",
            delta=(f"contract {c.model.structure_hash[:12]}... != urdf {disk_hash[:12]}..."),
            source=str(path),
            action=_RELOCK_ACTION,
            next_command=_relock_command(path, contract_path),
        )
    ]


_RELOCK_ACTION = (
    "review the structural change, then re-lock from the URDF with the command below "
    "(this discards locked entries)"
)


def _describe_hint(contract_path: Path | str | None) -> str:
    return cli_line("lockwire", "describe", contract_path or "lockwire.yaml")


def _relock_command(urdf: Path, contract_path: Path | None) -> str:
    default = (
        contract_path is None or Path(contract_path).resolve() == Path("lockwire.yaml").resolve()
    )
    out = [] if default else ["--out", contract_path]  # --out lockwire.yaml is the default
    return cli_line("lockwire", "start", urdf, *out, "--force")


def check_model_values(
    c: Contract, *, scores: _ScoreBook | None = None, contract_path: Path | None = None
) -> list[Finding]:
    """Locked link mass / COM / principal inertia vs the on-disk URDF or SDF.

    `structure_hash` cannot see a value edit, so with the structure unchanged each
    link's values are compared (relative 1e-6, absolute 1e-9). Anything unreadable
    stays the drift check's `not_scored`; this check is then silent.
    """
    named = _urdf_path_candidate(c.model) if c.model else None
    if named is None:
        return []
    path = (contract_path and resolve_source(contract_path, named)) or Path(named)
    try:
        sdf = path.suffix.lower() == ".sdf"
        topology = read_sdf_topology(path) if sdf else read_urdf_topology(path)
        disk = {link.name: link for link in (parse_sdf(path) if sdf else parse_urdf(path)).links}
    except (OSError, ET.ParseError, ValueError, TypeError):
        return []
    same = (topology.record_hash if c.model.joints else topology.topology_hash) == (
        c.model.structure_hash
    ) or (
        c.model.joints
        and locked_record_hash(c.model, topology, c.schema_version != SCHEMA_VERSION)
        == c.model.structure_hash
    )
    if not same:
        return []

    def differs(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        return not all(math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-9) for x, y in zip(a, b))

    def show(v: tuple[float, ...]) -> str:
        return " ".join(f"{x:.9g}" for x in v)

    findings = []
    for link in c.model.links:
        now = disk[link.name]
        zero = (0.0, 0.0, 0.0)
        values = {
            "mass": (
                (link.mass.base() if link.mass else 0.0,),
                (now.mass.base() if now.mass else 0.0,),
            ),
            "com": (link.com or zero, now.com or zero),
            "inertia": (
                link.inertia.moments_kgm2 if link.inertia else zero,
                now.inertia.moments_kgm2 if now.inertia else zero,
            ),
        }
        for what, (locked, current) in values.items():
            if differs(locked, current):
                quantity = f"model/links/{link.name}/{what}"
                findings.append(
                    Finding(
                        severity="violation",
                        quantity=quantity,
                        observed=f"model file has {what} {show(current)}",
                        expected=f"locked {what} {show(locked)}",
                        source=str(path),
                        action=(
                            f"the model file's {what} of {link.name} changed after the lock; if the "
                            "edit is real, re-lock from the model with the command below "
                            "(this discards locked entries)"
                        ),
                        next_command=_relock_command(path, contract_path),
                    )
                )
                if scores is not None:
                    scores.record(quantity)
    return findings


def check_mesh_drift(
    c: Contract, *, scores: _ScoreBook | None = None, contract_path: Path | None = None
) -> list[Finding]:
    """Mesh file/scale per link, then recorded mesh bytes, vs the on-disk model.

    structure_hash holds no geometry, so a scale or file edit after the lock needs this.
    What the lock never recorded is `not_scored`, never a silent pass.
    """
    named = _urdf_path_candidate(c.model) if c.model else None
    path = contract_path and named and resolve_source(contract_path, named)
    try:
        if not path:
            now = []
        elif path.suffix.lower() == ".sdf":  # includes spliced in, as the lock read it
            now = mesh_refs(read_sdf_root(path)[0])
        else:
            now = mesh_refs(ET.fromstring(read_model_bytes(path)))
    except (OSError, ET.ParseError, ValueError, TypeError):
        return []  # the drift check already says why the model can't be read
    locked = c.model.mesh_refs if c.model else None
    if not path or (not now and not locked):
        return []
    relock = _relock_command(path, contract_path)

    def finding(
        severity: str,
        quantity: str,
        observed: str,
        expected: str,
        action: str = "a mesh changed after the lock; if the edit is real, re-lock from the URDF "
        "with the command below (this discards locked entries)",
    ) -> Finding:
        if scores is not None and severity == "violation":
            scores.record(quantity)
        return Finding(
            severity=severity,
            quantity=quantity,
            observed=observed,
            expected=expected,
            source=str(path),
            action=action,
            next_command=relock,
        )

    if locked is None:
        return [
            finding(
                "not_scored",
                "model/meshes",
                "this lock predates recorded mesh files and scales; a mesh edit is not compared",
                "mesh files and scales recorded at lock",
                "re-lock with the command below to record them (this discards locked entries)",
            )
        ]
    if locked != now:
        found = []
        for link in sorted({m.link for m in locked + now}):
            before, after = (
                ", ".join(f"{m.uri} scale {m.scale}" for m in refs if m.link == link) or "no mesh"
                for refs in (locked, now)
            )
            if before != after:
                found.append(
                    finding(
                        "violation",
                        f"model/links/{link}/mesh",
                        f"model has {after}",
                        f"locked {before}",
                    )
                )
        return found
    if not c.model.external_assets:
        return [
            finding(
                "not_scored",
                "model/meshes",
                f"{len({m.uri for m in now})} mesh files were not found at lock; their bytes are not compared",
                "mesh files inside the robot's package folder, pinned at lock",
                "make the meshes resolvable inside the robot's package folder, then re-lock",
            )
        ]
    root = mesh_root(path, contract_path.parent)
    found = []
    for asset in c.model.external_assets:
        file = root / asset.path
        digest = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
        if digest != asset.sha256:
            found.append(
                finding(
                    "violation",
                    f"model/external_assets/{asset.path}",
                    f"mesh {asset.path} {'changed' if digest else 'is missing'} since lock",
                    f"mesh bytes sha256 {asset.sha256[:12]}...",
                )
            )
    return found


_SHEET_UNITS = {"kg": 1.0, "g": 1e-3, "m": 1.0, "mm": 1e-3, "count": 1.0, "rad/s": 1.0}
_SHEET_UNITS |= {"deg/s": math.pi / 180, "rpm": math.pi / 30}
_SCOPE_WORDS = {
    "with_battery": "includes the battery, computer and sensors",
    "with_cable": "includes the cable",
    "with_gripper": "includes the gripper",
    "arm_only": "is the arm alone, no controller or tool",
    "dry": "is dry, no battery or fluids",
}


def check_datasheet_agreement(
    c: Contract,
    *,
    scores: _ScoreBook | None = None,
    model: str | None = None,
    contract_path: Path | None = None,
) -> list[Finding]:
    """A recognised robot's URDF-derived numbers vs its vendor datasheet (catalog_robots.yaml).

    Mass +-5 % and dof exact are violations; payload and joint speed (the URDF allowing more
    than the sheet) are warnings; reach (+-3 %) is passed when it agrees, else a not_scored note. A mass_total budget (`--mass`) wins over the
    row. Silent for an unrecognised robot. At most the 3 largest deltas are reported.
    """
    if c.model is None:
        return []
    robot = recognise(c.robot.name, [link.name for link in c.model.links], model)
    if robot is None:
        return []
    joints = c.model.joints
    # urdf.py drops a mimic follower's limits, so a limitless revolute/prismatic is a follower.
    moving = [
        j
        for j in joints
        if j.type == "continuous" or (j.type in ("revolute", "prismatic") and j.limits)
    ]
    by_child = {j.child: j for j in joints}
    poses: dict[str, _Transform] = {}

    def pose(link: str) -> _Transform:  # the link's frame at the zero pose, in the base frame
        joint = by_child.get(link)
        if joint is None or link in poses:
            return poses.get(link, (_IDENTITY, (0.0, 0.0, 0.0)))
        poses[link] = (_IDENTITY, (0.0, 0.0, 0.0))  # a cycle ends at the origin, not a crash
        rot, pos = pose(joint.parent)
        poses[link] = (
            _matmul(rot, _rpy_rotation(joint.origin.rpy)),
            _add(pos, _matvec(rot, joint.origin.xyz)),
        )
        return poses[link]

    payloads = [link.payload.base() for link in c.model.links if link.payload]
    speeds = [
        j.limits.velocity
        for j in moving
        if j.type != "prismatic" and j.limits and j.limits.velocity
    ]
    derived = {
        "mass_total": (
            sum(link.mass.base() for link in c.model.links if link.mass),
            0.05,
            "sum of link masses",
        ),
        "dof": (len(moving), 0.0, "moving joints, fixed and mimic not counted"),
        "reach_m": (
            max((math.dist(pose(j.child)[1], (0.0, 0.0, 0.0)) for j in moving), default=None),
            0.03,
            "straight-line base to last joint at zero pose; vendor reach may include the flange",
        ),
        "payload_kg": (sum(payloads) if payloads else None, 0.0, "payload carried on links"),
        "joint_speed_max": (max(speeds, default=None), 0.05, "fastest joint velocity limit, rad/s"),
    }
    rows, who = dict(robot.numbers), {}
    declared = next((b for b in c.budgets if b.kind == "mass_total"), None)
    if declared is not None:  # a declared mass (--mass) beats the row
        sum_kg, nominal = derived["mass_total"][0], declared.nominal
        if nominal is None or all(link.mass for link in c.model.links) or sum_kg > nominal.base():
            del derived["mass_total"]  # check_budgets judges it
        else:  # check_budgets can't call a partial sum too light; to a simulator a gap is 0 kg
            source = declared.provenance.source if declared.provenance else "--mass"
            update = {"value": nominal.base(), "unit": "kg", "source": source, "scope": None}
            rows["mass_total"] = rows["mass_total"].model_copy(update=update)
            derived["mass_total"] = (
                sum_kg,
                (declared.tolerance_pct or 0) / 100,
                "sum of link masses",
            )
            who["mass_total"] = "declared mass"
    named = _urdf_path_candidate(c.model)
    path = named and ((contract_path and resolve_source(contract_path, named)) or Path(named))
    if path and path.resolve().is_relative_to(Path.cwd()):
        path = path.resolve().relative_to(Path.cwd())  # the spelling the user typed, usually
    mass = robot.numbers["mass_total"]
    rerun = cli_line(
        "lockwire",
        "check",
        path or "<model>",
        "--mass",
        f"{mass.value:g} {mass.unit}",
        "--source",
        mass.url,
    )
    suppressed = dict(d.split(":", 1) for d in robot.known_urdf_deviations if ":" in d)
    ranked: list[tuple[tuple[int, float], Finding]] = []
    for key, (urdf, tolerance, how) in derived.items():
        row = rows.get(key)
        if row is None or urdf is None or row.unit not in _SHEET_UNITS:
            continue
        quantity = f"datasheet/{robot.id}/{key}"
        sheet = row.value * _SHEET_UNITS[row.unit]
        scope = f"; the sheet's figure {_SCOPE_WORDS[row.scope]}" if row.scope else ""
        expected = f"{who.get(key, robot.name + ' datasheet')}: {row.value:g} {row.unit}{scope}"
        observed = f"URDF {how}: {urdf:.4g}"
        if key in suppressed:
            note = f"not compared: known URDF deviation —{suppressed[key]}"
            finding = Finding(
                severity="not_scored",
                quantity=quantity,
                observed=note,
                expected=expected,
                source=row.url,
                action="none; the catalog row names why",
                next_command=rerun,
            )
            ranked.append(((2, 0.0), finding))
            continue
        delta = (urdf - sheet) / sheet
        one_sided = key in ("payload_kg", "joint_speed_max")
        inside = (delta if one_sided else abs(delta)) <= tolerance + 1e-9
        if key == "reach_m" and not inside:
            # Vendors measure reach differently (flange, tool); a miss is a note, never a warning.
            finding = Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"not compared: {observed} ({delta:+.1%} vs the sheet)",
                expected=expected,
                source=row.url,
                action="none; vendor reach is measured differently, so this is only a note",
                next_command=rerun,
            )
            ranked.append(((2, 0.0), finding))
            continue
        if scores is not None:
            scores.record(quantity)
        if inside:
            if scores is not None:
                scores.passed(quantity, observed, expected, f"{delta:+.1%}", provenance=None)
            continue
        severity = "violation" if key in ("mass_total", "dof") else "warning"
        finding = Finding(
            severity=severity,
            quantity=quantity,
            observed=observed,
            expected=expected,
            delta=f"{delta:+.1%}",
            source=row.url,
            action=(
                f"check the URDF counts what the sheet counts ({row.scope or key}); if the sheet "
                "is right, fix the URDF; to compare with another figure, re-run with --mass"
            ),
            next_command=rerun,
        )
        ranked.append(((0 if severity == "violation" else 1, -abs(delta)), finding))
    return [finding for _, finding in sorted(ranked, key=lambda pair: pair[0])[:3]]


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
                        f"score this bound with `{cli_line('lockwire', 'plan')}`, which compares consecutive "
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

        if estimated_c <= limit_c:  # at the limit passes, as `expected` says
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
            offset = tuple(
                a - b for a, b in zip(part.translation, frame_part.translation, strict=True)
            )
            # mount_xyz is in the mount frame, so rotate the world offset into it
            # (transpose of the frame's root rotation).
            rotation = _transforms.get(physical.mount_frame, (_IDENTITY, None))[0]
            observed_m = _matvec(tuple(zip(*rotation)), offset)  # type: ignore[arg-type]
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


# ---------------------------------------------------------------- check pack 1
# Zero-setup checks: they read only what the lock, the on-disk URDF and the
# contract already hold. A heuristic without a published threshold is a
# `not_scored` disclosure, never a warning.


def _urdf_root(c: Contract, contract_path: Path | None) -> tuple[Path, ET.Element] | None:
    """The on-disk URDF the lock names, re-read the way check_urdf_drift does."""
    names = (c.model.provenance.source, c.model.source) if c.model else ()
    named = next((n for n in names if n.lower().endswith((".urdf", ".xacro", ".sdf"))), None)
    if named is None:
        return None
    path = (contract_path and resolve_source(contract_path, named)) or Path(named)
    if path.suffix.lower() == ".sdf":
        try:
            return path, urdf_view(path)
        except (OSError, ET.ParseError, ValueError):
            return None
    try:
        raw = read_model_bytes(path)  # a .xacro is expanded here
        if _has_doctype_entity(raw):
            return None
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError, ValueError):
        return None
    return (path, root) if root.tag == "robot" else None


def _floats(raw: str | None) -> list[float]:
    try:
        return [float(x) for x in (raw or "").split()]
    except ValueError:
        return []


def check_model_checks_skipped(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """One line, not silence, when the URDF-reading checks cannot run on this lock."""
    names = (c.model.provenance.source, c.model.source) if c.model else ()
    if c.model is None or any(n.lower().endswith((".urdf", ".xacro", ".sdf")) for n in names):
        return []
    return [
        Finding(
            severity="not_scored",
            quantity="model/sanity",
            observed="model sanity checks not run: the lock names no .urdf, .xacro or .sdf file",
            expected="a .urdf the checks can read",
            action=f"run {cli_line('lockwire', 'start')} on the expanded model to check them",
            next_command=cli_line("lockwire", "start") + " <expanded.urdf>",
        )
    ]


def check_urdf_sanity(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Structural mistakes a tolerant lock accepted: inverted or missing limits,
    non-unit axes, loops, mimic ranges past the follower's limits, mesh name clashes."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return []
    path, root = found
    fix = _relock_command(path, contract_path)
    out: list[Finding] = []

    def warn(quantity: str, observed: str, expected: str, action: str, severity="warning"):
        out.append(
            Finding(
                severity=severity,
                quantity=f"model/sanity/{quantity}",
                observed=observed,
                expected=expected,
                source=str(path),
                action=action + ", then re-lock",
                next_command=fix,
            )
        )

    limits: dict[str, tuple[float, float]] = {}
    parents: dict[str, list[str]] = {}
    joints = root.findall("joint")
    for el in joints:
        name, jtype, lim = el.get("name", "?"), el.get("type", "fixed"), el.find("limit")
        child = el.find("child")
        parents.setdefault(child.get("link", "?") if child is not None else "?", []).append(name)
        if jtype in ("revolute", "prismatic"):
            lo, hi = (_floats(lim.get(k)) if lim is not None else [] for k in ("lower", "upper"))
            if lo and hi and lo[0] >= hi[0] and el.find("mimic") is None:
                # Joint._ordered rejects this; urdf.py drops the limit and locks it unlimited.
                warn(
                    f"{name}/limits",
                    f"joint {name} has lower {lo[0]:g} >= upper {hi[0]:g}; locked as unlimited",
                    "lower < upper",
                    f"swap or fix {name}'s limits",
                )
            elif lo and hi:
                limits[name] = (lo[0], hi[0])
            # urdfdom joint.cpp: revolute/prismatic need <limit>, and a <limit> needs velocity
            # (https://raw.githubusercontent.com/ros/urdfdom/master/urdf_parser/src/joint.cpp).
            if lim is None or lim.get("velocity") is None:
                warn(
                    f"{name}/velocity",
                    f"moving joint {name} has no velocity limit",
                    "a <limit velocity=...> (ROS urdfdom refuses the file without it)",
                    f"add {name}'s velocity limit from its datasheet",
                )
        axis = _floats(el.find("axis").get("xyz")) if el.find("axis") is not None else []
        norm = math.sqrt(sum(v * v for v in axis)) if len(axis) == 3 else 1.0
        # ponytail: no fetched spec says unit axis (urdfdom does not normalize), so a
        # disclosure; 1e-3 is rounding slack for hand-typed axes.
        if jtype in ("revolute", "continuous", "prismatic") and abs(norm - 1.0) > 1e-3:
            warn(
                f"{name}/axis",
                f"joint {name} axis has length {norm:.4g}",
                "a unit axis",
                f"make {name}'s axis length 1",
                severity="not_scored",
            )
    two_parents = sorted(link for link, js in parents.items() if len(js) > 1)
    # urdfdom initTree writes parent_link_tree[child] with no check for an existing parent,
    # so ROS silently keeps one of two parent joints
    # (https://raw.githubusercontent.com/ros/urdfdom_headers/master/include/urdf_model/model.h).
    if two_parents:
        warn(
            "loops",
            "links with more than one parent joint (a kinematic loop): " + ", ".join(two_parents),
            "a tree: one parent joint per link",
            "give each link one parent joint",
        )
    for el in joints:
        mimic, name = el.find("mimic"), el.get("name", "?")
        lim = el.find("limit")
        lead = limits.get(mimic.get("joint", "")) if mimic is not None else None
        own = (
            (_floats(lim.get("lower")), _floats(lim.get("upper"))) if lim is not None else ([], [])
        )
        if lead is None or not own[0] or not own[1] or own[0][0] >= own[1][0]:
            continue
        m, o = (
            (_floats(mimic.get("multiplier")) or [1.0])[0],
            (_floats(mimic.get("offset")) or [0.0])[0],
        )
        # follower = multiplier * leader + offset; urdfdom defaults 1 and 0 (joint.cpp above).
        reach = sorted((m * lead[0] + o, m * lead[1] + o))
        tol = 1e-6 * max(1.0, abs(own[0][0]), abs(own[1][0]))
        if reach[0] < own[0][0] - tol or reach[1] > own[1][0] + tol:
            warn(
                f"{name}/mimic",
                f"{name} follows {mimic.get('joint')} over {reach[0]:.4g}..{reach[1]:.4g}",
                f"inside {name}'s own limits {own[0][0]:.4g}..{own[1][0]:.4g}",
                f"fix {name}'s mimic multiplier, offset or limits",
            )
    meshes: dict[str, set[str]] = {}
    for mesh in root.iter("mesh"):
        fname = mesh.get("filename", "")
        meshes.setdefault(fname.replace("\\", "/").rsplit("/", 1)[-1].lower(), set()).add(fname)
    clash = sorted(base for base, names in meshes.items() if len(names) > 1)
    if clash:  # no published rule: a disclosure, since tools that flatten mesh folders collide
        warn(
            "meshes",
            "different mesh files share a file name: " + ", ".join(clash),
            "one file per mesh name",
            "rename one file of each pair",
            severity="not_scored",
        )
    return out


def check_inertia_valid(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Principal moments must be non-negative and obey a <= b + c."""
    # Drake RotationalInertia::CouldBePhysicallyValid: principal moments non-negative and
    # satisfying the triangle inequality
    # (https://drake.mit.edu/doxygen_cxx/classdrake_1_1multibody_1_1_rotational_inertia.html).
    out = []
    for link in c.model.links if c.model else []:
        if link.inertia is None:
            continue
        a, b, big = sorted(link.inertia.moments_kgm2)
        # ponytail: the slacks absorb the eigen-solve's float noise only. Zero is allowed
        # (a point mass, e.g. SO-101's 1e-9 kg gripper frame).
        bad = (
            "a principal moment is negative"
            if a < -1e-12
            else "the largest moment exceeds the sum of the other two"
            if big > (a + b) * (1 + 1e-6)
            else None
        )
        if bad:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/links/{link.name}/inertia",
                    observed=f"principal moments {a:.4g}, {b:.4g}, {big:.4g} kg*m^2: {bad}",
                    expected="non-negative moments with the largest <= the sum of the other two",
                    action=(f"no real body has this inertia; recompute {link.name}'s from CAD"),
                    next_command=_relock_command(
                        Path(_urdf_path_candidate(c.model) or c.model.source), contract_path
                    ),
                )
            )
    return out


def _link_primitives(
    root: ET.Element,
) -> dict[str, list[list[tuple[tuple[float, ...], float, float]]]]:
    """Per link, one list per collision/visual set: (origin xyz, bounding radius, volume,
    axis radius). The axis radius is the shape's farthest point from its own thinnest axis
    through its centre (a cylinder's radius, a box's half cross-section diagonal).

    A link with a mesh anywhere (a primitive may only simplify it) is left out:
    no mesh dependency.
    """
    out = {}
    for link in root.findall("link"):
        if link.find(".//mesh") is not None:
            continue
        sets = []
        for kind in ("collision", "visual"):
            shapes = []
            for el in link.findall(kind):
                geometry = el.find("geometry")
                shape = geometry[0] if geometry is not None and len(geometry) else None
                if shape is None:
                    continue
                xyz = _floats(el.find("origin").get("xyz")) if el.find("origin") is not None else []
                size = _floats(shape.get("size"))
                r, length = ((_floats(shape.get(k)) or [0.0])[0] for k in ("radius", "length"))
                if shape.tag == "box" and len(size) == 3:
                    radius, volume = 0.5 * math.hypot(*size), size[0] * size[1] * size[2]
                    axis_r = 0.5 * math.hypot(*sorted(size)[:2])
                elif shape.tag == "cylinder":
                    radius, volume, axis_r = math.hypot(r, length / 2), math.pi * r**2 * length, r
                elif shape.tag == "sphere":
                    radius, volume, axis_r = r, 4 / 3 * math.pi * r**3, r
                else:
                    continue
                origin = tuple(xyz) if len(xyz) == 3 else (0.0, 0.0, 0.0)
                shapes.append((origin, radius, volume, axis_r))
            if shapes:
                sets.append(shapes)
        if sets:
            out[link.get("name", "?")] = sets
    return out


def check_mass_vs_size(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Density, CoM and radius of gyration against the link's primitive shapes."""
    found = _urdf_root(c, contract_path)
    if found is None or c.model is None:
        return []
    path, root = found
    fix = _relock_command(path, contract_path)
    shapes = _link_primitives(root)
    out = []
    for link in c.model.links:
        sets = shapes.get(link.name)
        if not sets or link.mass is None or link.mass.base() <= 0:
            continue
        mass, com = link.mass.base(), link.com or (0.0, 0.0, 0.0)
        prims = [p for shapes_ in sets for p in shapes_]  # collision and visual together
        volumes = [sum(p[2] for p in shapes_) for shapes_ in sets]
        problems = []
        # Fetched https://en.wikipedia.org/wiki/Density 2026-11-04: osmium, "the densest known
        # element", 22,570 kg/m^3; aerographite, the lightest solid in its table, 0.2 kg/m^3.
        # Outside that, units slipped. The roomier set judges "too dense", the tighter "too light".
        if min(volumes) > 0 and not (mass / max(volumes) <= 22570 and mass / min(volumes) >= 0.2):
            density = mass / max(volumes) if mass / max(volumes) > 22570 else mass / min(volumes)
            problems.append(f"density {density:.4g} kg/m^3 from {mass:g} kg (g vs kg or mm vs m?)")
        # The CoM is a mass-weighted average, a convex combination of the body's points, so it
        # lies in their convex hull (https://en.wikipedia.org/wiki/Convex_combination), which
        # the box around the shapes' bounding spheres contains.
        box = [
            (min(o[i] - r for o, r, *_ in prims), max(o[i] + r for o, r, *_ in prims))
            for i in range(3)
        ]
        if any(not lo - 1e-6 <= x <= hi + 1e-6 for x, (lo, hi) in zip(com, box)):
            problems.append(f"CoM {_fmt_point(com)} m lies outside the link's shapes")
        # I = sum(r^2 dm) <= m R^2: the radius of gyration cannot exceed the body's extent
        # from the CoM (https://en.wikipedia.org/wiki/Radius_of_gyration).
        extent = max(math.dist(com, o) + r for o, r, *_ in prims)
        if link.inertia and math.sqrt(max(link.inertia.moments_kgm2) / mass) > extent * (1 + 1e-6):
            problems.append(
                f"radius of gyration {math.sqrt(max(link.inertia.moments_kgm2) / mass):.4g} m "
                f"exceeds the shapes' extent {extent:.4g} m from the CoM"
            )
        # About the shape's own thinnest axis, every point lies within axis_r of it, so no body
        # inside can exceed m (axis_r + CoM offset)^2 there; the smallest principal moment is the
        # minimum over all axes, so it is bounded too. ponytail: single-shape sets only; a
        # multi-shape link has no one axis, so it keeps the looser extent bound above.
        if link.inertia and not problems and all(len(shapes_) == 1 for shapes_ in sets):
            cap = max(mass * (a_r + math.dist(com, o)) ** 2 for ((o, _, _, a_r),) in sets)
            low = min(link.inertia.moments_kgm2)
            # ponytail: 2x slack, since a collision primitive often simplifies the part a little.
            if cap > 0 and low > 2 * cap:
                problems.append(
                    f"smallest moment {low:.4g} kg*m^2 is {low / cap:.0f}x the most a "
                    f"{mass:g} kg body inside its shape can have ({cap:.3g}): placeholder inertia?"
                )
        for problem in problems:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/links/{link.name}/mass_vs_size",
                    observed=problem,
                    expected="mass, CoM and inertia a solid of this link's shapes can have",
                    source=str(path),
                    action=(
                        f"compute {link.name}'s inertia from its mass and shape, then re-lock"
                        if problem.endswith("placeholder inertia?")
                        else f"check the units of {link.name}'s mass and shape, then re-lock"
                    ),
                    next_command=fix,
                )
            )
    return out


def check_inertia_scale(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Exactly equal round-number principal moments (1, 1e-4 ...): a placeholder?"""
    # ponytail: no published rule names placeholder inertia, so this is a disclosure.
    flat = []
    for link in c.model.links if c.model else []:
        m = link.inertia.moments_kgm2 if link.inertia else ()
        if m and m[0] > 0 and all(math.isclose(v, m[0], rel_tol=1e-9) for v in m):
            exponent = math.log10(m[0])
            if math.isclose(exponent, round(exponent), abs_tol=1e-9):
                flat.append(f"{link.name} ({m[0]:g})")
    if not flat:
        return []
    return [
        Finding(
            severity="not_scored",
            quantity="model/links/placeholder_inertia",
            observed=f"{len(flat)} link(s) with three equal round-number moments: "
            + ", ".join(flat[:3])
            + (", ..." if len(flat) > 3 else ""),
            expected="inertia computed from each part (placeholder inertia?)",
            action="if placeholders, compute the inertia from CAD, then re-lock",
            next_command=_relock_command(
                Path(_urdf_path_candidate(c.model) or c.model.source), contract_path
            ),
        )
    ]


def check_limit_unit_traps(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Degrees typed as radians, and joint numbers too large for a robot (mm typed as m)."""
    if c.model is None:
        return []
    fix = _relock_command(Path(_urdf_path_candidate(c.model) or c.model.source), contract_path)
    out = []
    for j in c.model.joints:
        lim = j.limits
        bounds = [abs(v) for v in (lim.lower, lim.upper) if v is not None] if lim else []
        # ponytail: no fetched source states the URDF unit (wiki.ros.org/urdf/XML/joint was
        # access-denied 2026-11-04; urdfdom joint.cpp and urdf.xsd say nothing on units), so
        # "past three turns is degrees" is our own heuristic and only discloses. ANYmal D ships
        # +/-3*pi (tests/fixtures/anymal_d.urdf); huge values are "unbounded" sentinels.
        if j.type == "revolute" and bounds and 6 * math.pi * (1 + 1e-3) < max(bounds) <= 720:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"model/joints/{j.name}/limits",
                    observed=f"revolute limit {max(bounds):g} rad is more than three full turns",
                    expected="radians within one turn (degrees typed as radians?)",
                    action=f"convert {j.name}'s limits to radians, then re-lock",
                    next_command=fix,
                )
            )
        # ponytail: 100 rad/s and 5 m are unsourced sanity bounds, so they only disclose.
        if j.type == "revolute" and lim and lim.velocity and lim.velocity > 100:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"model/joints/{j.name}/limits/velocity",
                    observed=f"velocity limit {lim.velocity:g} rad/s",
                    expected="a joint speed in rad/s (degrees/s typed as rad/s?)",
                    action=f"confirm {j.name}'s velocity limit is in rad/s",
                    next_command=fix,
                )
            )
        if math.hypot(*j.origin.xyz) > 5.0:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"model/joints/{j.name}/origin",
                    observed=f"joint origin {math.hypot(*j.origin.xyz):.4g} m from its parent",
                    expected="metres (millimetres typed as metres?)",
                    action=f"confirm {j.name}'s origin is in metres",
                    next_command=fix,
                )
            )
    return out


def check_battery_shipping_class(
    c: Contract, *, contract_path: Path | None = None
) -> list[Finding]:
    """Pack energy against the IATA lithium-ion watt-hour lines."""
    battery = c.topology.battery if c.topology else None
    if battery is None:
        return []
    wh = battery.capacity.base()  # energy base unit is Wh
    # IATA "Passengers travelling with lithium batteries": >100 to <=160 Wh needs operator
    # approval; >160 Wh must be carried as cargo under the DGR
    # (https://www.iata.org/contentassets/6fea26dd84d24b26a7a1fd5788561d6e/passenger-lithium-battery.pdf).
    # Cargo over 100 Wh is fully regulated, UN 38.3 tested (IATA Lithium Battery Guidance,
    # https://www.iata.org/contentassets/05e6d8742b0047259bf3a700bc9d42b9/lithium-battery-guidance-document.pdf).
    if wh <= 100:
        return []
    over = wh > 160
    return [
        Finding(
            severity="warning" if over else "not_scored",
            quantity="topology/battery/capacity",
            observed=f"{wh:g} Wh lithium pack",
            expected="<= 160 Wh to fly with a passenger" if over else "<= 100 Wh to fly freely",
            observed_quantity=Quantity(value=wh, unit="Wh"),
            expected_quantity=Quantity(value=160.0 if over else 100.0, unit="Wh"),
            action=(
                "too big to fly with a passenger; ship it as dangerous-goods cargo"
                if over
                else "needs airline approval to fly; ships as regulated cargo (UN 38.3)"
            ),
            next_command=_describe_hint(contract_path),
        )
    ]


def _declared_ambients(c: Contract) -> list[float]:
    """Every declared ambient in C: device thermal ambients and C/degC envelope bounds."""
    return [
        d.thermal.ambient.base() for d in c.devices if d.thermal and d.thermal.ambient is not None
    ] + [
        bound
        for d in c.devices
        for env in d.envelopes
        for band in env.conditions.values()
        if band.unit in ("C", "degC")
        for bound in (band.lo, band.hi)
    ]


def check_cold_derating_coverage(
    c: Contract, *, contract_path: Path | None = None
) -> list[Finding]:
    """A below-freezing declared ambient, an endurance budget, and no derating row for it."""
    battery = c.topology.battery if c.topology else None
    if battery is None or not any(b.kind == "endurance" for b in c.budgets):
        return []
    coldest = min(_declared_ambients(c), default=None)
    # Battery University BU-502: a battery giving 100% at 27 C typically gives only 50% at
    # -18 C (https://batteryuniversity.com/article/bu-502-discharging-at-high-and-low-temperatures).
    if coldest is None or coldest >= 0 or any(row.below_c >= coldest for row in battery.derating):
        return []
    return [
        Finding(
            severity="warning",
            quantity="topology/battery/derating",
            observed=f"declared ambient down to {coldest:g} C, no capacity derating covers it",
            expected=f"a derating row with below_c >= {coldest:g} (endurance assumes room temperature)",
            action=("cold halves capacity by -18 C; declare a derating row from the datasheet"),
            next_command=_describe_hint(contract_path),
        )
    ]


def check_i2c_reserved_address(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A 7-bit I2C address in a reserved group (0x00-0x07, 0x78-0x7F)."""
    i2c = {b.id for b in c.topology.buses if b.kind == "i2c"} if c.topology else set()
    out = []
    for d in c.devices:
        ident = d.identity
        if ident is None or ident.bus_ref not in i2c or ident.address is None:
            continue
        try:
            address = int(ident.address, 0)
        except ValueError:
            continue
        # NXP UM10204 section 3.1.12, Table 4: addresses 0000 XXX and 1111 XXX are reserved
        # (https://www.nxp.com/docs/en/user-guide/UM10204.pdf).
        if address <= 0x07 or 0x78 <= address <= 0x7F:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"devices/{d.id}/identity/address",
                    observed=f"I2C address {address:#04x}",
                    expected="0x08..0x77 (0x00-0x07 and 0x78-0x7F are reserved, UM10204 Table 4)",
                    action=f"check {d.id}'s address straps: this address is reserved",
                    next_command=_describe_hint(contract_path),
                )
            )
    return out


def check_controller_limits(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A ros2_control/MoveIt joint_limits.yaml wider than the URDF limits.

    Found by one rule: ``joint_limits.yaml`` beside the URDF, else in a ``config/``
    folder beside the URDF's folder (the ROS package layout). Otherwise silent.
    """
    found = _urdf_root(c, contract_path)
    if found is None or c.model is None:
        return []
    path = found[0]
    yaml_path = next(
        (
            p
            for p in (
                path.parent / "joint_limits.yaml",
                path.parent.parent / "config" / "joint_limits.yaml",
            )
            if p.is_file()
        ),
        None,
    )
    if yaml_path is None:
        return []
    from ruamel.yaml import YAML, YAMLError

    try:
        data = YAML(typ="safe").load(yaml_path.read_text())
    except (OSError, YAMLError):
        return []
    rows = data.get("joint_limits", data) if isinstance(data, dict) else {}
    # ros2_control joint_limits_rosparam.hpp: "Limits specified in the parameter server will
    # overwrite existing values" (https://raw.githubusercontent.com/ros-controls/ros2_control/
    # master/joint_limits/include/joint_limits/joint_limits_rosparam.hpp), so a wider yaml
    # limit silently widens what the URDF promised.
    out = []
    for j in c.model.joints:
        row, lim = rows.get(j.name) if isinstance(rows, dict) else None, j.limits
        if not isinstance(row, dict) or lim is None:
            continue
        pairs = []
        if row.get("has_position_limits"):
            pairs += [
                ("min_position", row.get("min_position"), lim.lower, -1),
                ("max_position", row.get("max_position"), lim.upper, 1),
            ]
        if row.get("has_velocity_limits"):
            pairs.append(("max_velocity", row.get("max_velocity"), lim.velocity, 1))
        if row.get("has_effort_limits"):
            pairs.append(("max_effort", row.get("max_effort"), lim.effort_limit, 1))
        for key, yaml_value, urdf_value, sign in pairs:
            if (
                isinstance(yaml_value, (int, float))
                and urdf_value is not None
                and sign * (yaml_value - urdf_value) > 1e-9 * max(1.0, abs(urdf_value))
            ):
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"model/joints/{j.name}/limits/{key}",
                        observed=f"{yaml_path.name} {key} {yaml_value:g}",
                        expected=f"within the URDF limit {urdf_value:g}",
                        source=str(yaml_path),
                        action=(
                            f"the controller lets {j.name} pass its model limit; tighten {key}"
                        ),
                        next_command=cli_line("lockwire", "check", contract_path or path),
                    )
                )
    return out


def check_driver_watchdog_marker(
    c: Contract, *, contract_path: Path | None = None
) -> list[Finding]:
    """Actuators with no observed reset/watchdog marker: a disclosure, never a pass or fail.

    Only drivers that map signals: a procedure marker names a declared signal, so a
    driver without signals could not declare one and saying so would be noise.
    """
    bare = sorted(
        d.id
        for d in c.devices
        if d.drives_joint is not None
        and d.driver is not None
        and d.driver.signals
        and not any(p.kind in ("reset", "watchdog") for p in d.driver.procedures)
    )
    if not bare:
        return []
    return [
        Finding(
            severity="not_scored",
            quantity="devices/watchdog",
            observed=(
                f"no watchdog marker found for {len(bare)} actuator(s): "
                + ", ".join(bare[:3])
                + (", ..." if len(bare) > 3 else "")
                + "; absence proves nothing"
            ),
            expected="a reset/watchdog procedure marker on each actuator's driver",
            action="declare the driver's reset or watchdog marker, if it has one",
            next_command=_describe_hint(contract_path),
        )
    ]


# Per-cell volts (nominal, full charge, end of discharge) and default (charge, discharge)
# windows in C. Every number fetched 2026-11-05; None = no source found, so not scored.
# Li-ion: 3.6 V nominal, 4.2 V max (BU-303 https://batteryuniversity.com/article/bu-303-confusion-with-voltages);
#   3.0 V end of discharge, "most Li-ion to 3.0V/cell" (BU-501 https://batteryuniversity.com/article/bu-501-basics-about-discharging).
# LiPo: "Charge and discharge characteristics of Li-polymer are identical to other Li-ion
#   systems" (BU-206 https://batteryuniversity.com/article/bu-206-lithium-polymer-substance-or-hype).
# LiFePO4: 3.2 V nominal, charges to 3.65 V, 2.50 V cut-off (BU-205
#   https://batteryuniversity.com/article/bu-205-types-of-lithium-ion); no temperature source found.
# NiMH: 1.20 V nominal (BU-303), 1.00 V end of discharge (BU-501); no max-voltage source found
#   (BU-408 gives only the -dV full-charge signal).
# Lead acid: 2.00 V nominal, 1.75 V end of discharge (BU-501); 2.45 V/cell, the top of the
#   2.30-2.45 V charge-voltage limit (BU-403 https://batteryuniversity.com/article/bu-403-charging-lead-acid).
# Temperatures: BU-410 Table 1 (https://batteryuniversity.com/article/bu-410-charging-at-high-and-low-temperatures):
#   Li-ion charge 0..45, discharge -20..60; NiMH 0..45, -20..65; lead acid -20..50 both.
_CHEMISTRY: dict[
    str, tuple[float, float | None, float, tuple[float, float] | None, tuple[float, float] | None]
] = {
    "li-ion": (3.6, 4.2, 3.0, (0.0, 45.0), (-20.0, 60.0)),
    "lipo": (3.6, 4.2, 3.0, (0.0, 45.0), (-20.0, 60.0)),
    "lifepo4": (3.2, 3.65, 2.5, None, None),
    "nimh": (1.2, None, 1.0, (0.0, 45.0), (-20.0, 65.0)),
    "lead-acid": (2.0, 2.45, 1.75, (-20.0, 50.0), (-20.0, 50.0)),
}


def _declare(contract_path: Path | None, field: str, example: str) -> str:
    return cli_line(
        "lockwire",
        "declare",
        contract_path or "lockwire.yaml",
        f"topology/battery/{field}",
        example,
        "--source",
        "<pack datasheet>",
    )


def _pack(c: Contract) -> tuple[Any, tuple, float | None] | None:
    """(battery, chemistry row, pack nominal V) when a chemistry is declared, else None."""
    battery = c.topology.battery if c.topology else None
    if battery is None or battery.chemistry is None:
        return None
    row = _CHEMISTRY[battery.chemistry]
    if battery.voltage is not None:
        nominal = battery.voltage.base()
    else:
        nominal = battery.cells_series * row[0] if battery.cells_series else None
    return battery, row, nominal


def _bus_rails(c: Contract, nominal_v: float | None) -> list[Any]:
    """Rails fed straight from the pack: upstream battery, a voltage_range holding pack nominal.

    ponytail: a converter rail's voltage_range is its output, so a range that does not hold
    the pack's nominal voltage is read as converted and skipped; a converter input field
    would let those be compared too.
    """
    if nominal_v is None or c.topology is None:
        return []
    return [
        r
        for r in c.topology.rails
        if r.upstream == "battery"
        and r.voltage_range is not None
        and r.voltage_range.min.base() <= nominal_v <= r.voltage_range.max.base()
    ]


def _not_scored(quantity: str, observed: str, expected: str, cmd: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=quantity,
        observed=observed,
        expected=expected,
        action="declare it from the pack datasheet",
        next_command=cmd,
    )


def check_battery_cell_window(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """The pack's cell window (cells x cell min/max for its chemistry) against each pack-fed rail's range.

    Also the group's door: a battery with no chemistry gets one "Not compared yet" line.
    """
    battery = c.topology.battery if c.topology else None
    if battery is None:
        return []
    if battery.chemistry is None:
        cmd = _declare(contract_path, "chemistry", "li-ion")
        return [
            _not_scored(
                "topology/battery/chemistry",
                "battery chemistry not declared",
                "one of li-ion, lipo, lifepo4, nimh, lead-acid",
                cmd,
            )
        ]
    _, (_, cell_max, cell_min, _, _), nominal = _pack(c)
    out = []
    if battery.chemistry == "lead-acid":
        out.append(
            _not_scored(
                "topology/battery/enclosure",
                "lead-acid charging in a sealed box not compared (no enclosure field)",
                "a vented enclosure while charging (hydrogen)",
                _describe_hint(contract_path),
            )
        )
    if battery.cells_series is None:
        return out + [
            _not_scored(
                "topology/battery/cells_series",
                "cells in series not declared; pack voltage window unknown",
                "the pack's series cell count",
                _declare(contract_path, "cells_series", "4"),
            )
        ]
    n = battery.cells_series
    if cell_max is None:
        out.append(
            _not_scored(
                "topology/battery/chemistry",
                f"no sourced full-charge cell voltage for {battery.chemistry}; pack max not compared",
                "a sourced full-charge cell voltage",
                _describe_hint(contract_path),
            )
        )
    for rail in _bus_rails(c, nominal):
        lo, hi = rail.voltage_range.min.base(), rail.voltage_range.max.base()
        for side, pack_v, bad in (
            ("min", n * cell_min, n * cell_min < lo),
            ("max", n * cell_max if cell_max else None, cell_max is not None and n * cell_max > hi),
        ):
            if bad:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=f"topology/rails/{rail.id}/voltage_range/{side}",
                        observed=f"{n}S {battery.chemistry} pack reaches {pack_v:.3g} V",
                        expected=f"within {rail.id}'s {lo:g}-{hi:g} V",
                        observed_quantity=Quantity(value=pack_v, unit="V"),
                        expected_quantity=Quantity(value=lo if side == "min" else hi, unit="V"),
                        action=(
                            f"{rail.id} browns out before the pack is empty"
                            if side == "min"
                            else f"a full pack is above what {rail.id} accepts"
                        ),
                        next_command=_describe_hint(contract_path),
                    )
                )
    return out


def _pack_amps(c: Contract, nominal_v: float) -> tuple[float, str, set[str]]:
    """Worst pack current over nominal and every declared power mode, its mode, unknown draws."""
    worst, worst_mode, unknown = -1.0, "nominal", set()
    for mode in _ampacity_draw_modes(c):
        _, _, watts, _, _, gaps = _power_rollup(c, mode)
        unknown |= gaps
        if watts > worst:
            worst, worst_mode = watts, mode
    return max(worst, 0.0) / nominal_v, worst_mode, unknown


def check_battery_c_rate(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Peak pack current (power rollup, worst mode) against capacity x continuous and burst C-rate."""
    pack = _pack(c)
    if pack is None or pack[2] is None:
        return []
    battery, _, nominal = pack
    if battery.c_rate_continuous is None and battery.c_rate_burst is None:
        return [
            _not_scored(
                "topology/battery/c_rate_continuous",
                "pack C-rate not declared; peak draw not compared",
                "the datasheet's continuous and burst C",
                _declare(contract_path, "c_rate_continuous", "20"),
            )
        ]
    amps, mode, unknown = _pack_amps(c, nominal)
    if unknown:
        return [
            _not_scored(
                "topology/battery/c_rate_continuous",
                f"draw unknown for {', '.join(sorted(unknown)[:3])}; peak current not summed",
                "every device reaching the battery to declare a draw",
                _describe_hint(contract_path),
            )
        ]
    amp_hours = battery.capacity.base() / nominal
    # Burst past its rating is a violation; past continuous but inside burst is a warning,
    # since a mode may or may not be short enough to count as a burst.
    for field, rate, severity in (
        ("c_rate_burst", battery.c_rate_burst, "violation"),
        ("c_rate_continuous", battery.c_rate_continuous, "warning"),
    ):
        if rate is not None and amps > amp_hours * rate:
            return [
                Finding(
                    severity=severity,
                    quantity=f"topology/battery/{field}",
                    observed=f"{amps:.3g} A peak ({mode})",
                    expected=f"<= {amp_hours * rate:.3g} A ({amp_hours:.3g} Ah x {rate:g}C)",
                    observed_quantity=Quantity(value=amps, unit="A"),
                    expected_quantity=Quantity(value=amp_hours * rate, unit="A"),
                    action="the pack cannot deliver this peak; grow the pack or a higher-C cell",
                    next_command=_describe_hint(contract_path),
                )
            ]
    return []


def check_battery_sag(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Pack nominal minus peak current x pack internal resistance, against the lowest pack-fed rail minimum."""
    pack = _pack(c)
    if pack is None or pack[2] is None:
        return []
    battery, _, nominal = pack
    rails = _bus_rails(c, nominal)
    if not rails:
        return []
    if battery.internal_resistance_mohm is None:
        return [
            _not_scored(
                "topology/battery/internal_resistance_mohm",
                "pack internal resistance not declared; sag under peak not compared",
                "the pack's DC internal resistance in mOhm",
                _declare(contract_path, "internal_resistance_mohm", "30"),
            )
        ]
    amps, mode, unknown = _pack_amps(c, nominal)
    if unknown:
        return []  # check_battery_c_rate already names the unknown draws
    sagged = nominal - amps * battery.internal_resistance_mohm / 1000.0
    rail = max(rails, key=lambda r: r.voltage_range.min.base())
    floor = rail.voltage_range.min.base()
    if sagged >= floor:
        return []
    return [
        Finding(
            severity="warning",
            quantity="topology/battery/internal_resistance_mohm",
            observed=f"pack sags to {sagged:.3g} V at {amps:.3g} A ({mode})",
            expected=f">= {floor:g} V, {rail.id}'s minimum",
            observed_quantity=Quantity(value=sagged, unit="V"),
            expected_quantity=Quantity(value=floor, unit="V"),
            action=f"{rail.id} may brown out under peak load; lower the peak or the pack resistance",
            next_command=_describe_hint(contract_path),
        )
    ]


def check_battery_temperature(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """The declared ambient range against the pack's charge and discharge temperature windows."""
    pack = _pack(c)
    if pack is None:
        return []
    battery, (_, _, _, charge, discharge), _ = pack
    ambients = _declared_ambients(c)
    if not ambients:
        return [
            _not_scored(
                "topology/battery/discharge_temp_c",
                "no declared ambient; pack temperature window not compared",
                "a device thermal ambient or a C envelope",
                _describe_hint(contract_path),
            )
        ]
    cold, hot = min(ambients), max(ambients)
    out = []
    for field, declared, default, verb in (
        ("charge_temp_c", battery.charge_temp_c, charge, "charge"),
        ("discharge_temp_c", battery.discharge_temp_c, discharge, "run"),
    ):
        if declared is not None:
            window = (
                units.to_base(declared.lo, declared.unit),
                units.to_base(declared.hi, declared.unit),
            )
        elif default is not None:
            window = default
        else:
            out.append(
                _not_scored(
                    f"topology/battery/{field}",
                    f"no sourced {verb} window for {battery.chemistry}; not compared",
                    "the datasheet's temperature window",
                    _describe_hint(contract_path),
                )
            )
            continue
        if cold < window[0] or hot > window[1]:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"topology/battery/{field}",
                    observed=f"declared ambient {cold:g}..{hot:g} C",
                    expected=f"inside the pack's {verb} window {window[0]:g}..{window[1]:g} C",
                    action=f"the pack is outside its {verb} window at the declared ambient",
                    next_command=_describe_hint(contract_path),
                )
            )
    return out


def check_battery_energy_density(
    c: Contract, *, contract_path: Path | None = None
) -> list[Finding]:
    """Pack Wh per kg above 300, more than any shipping cell: the capacity or mass is wrong."""
    pack = _pack(c)
    if pack is None or pack[0].mass is None:
        return []
    battery = pack[0]
    density = battery.capacity.base() / battery.mass.base()
    # BU-205 (fetched 2026-11-05): the densest family, NCA, is "200-260Wh/kg; 300Wh/kg
    # predictable" at cell level (https://batteryuniversity.com/article/bu-205-types-of-lithium-ion);
    # a whole pack carries casing and wiring on top.
    if density <= 300:
        return []
    return [
        Finding(
            severity="warning",
            quantity="topology/battery/mass",
            observed=f"{density:.0f} Wh/kg",
            expected="<= 300 Wh/kg (denser than any shipping cell)",
            action="check the pack's capacity and mass against its datasheet",
            next_command=_describe_hint(contract_path),
        )
    ]


CHECK_PACK_1 = (
    check_model_checks_skipped,
    check_urdf_sanity,
    check_inertia_valid,
    check_inertia_scale,
    check_mass_vs_size,
    check_limit_unit_traps,
    check_battery_shipping_class,
    check_cold_derating_coverage,
    check_i2c_reserved_address,
    check_controller_limits,
    check_driver_watchdog_marker,
)

CHECK_PACK_2 = (
    check_battery_cell_window,
    check_battery_c_rate,
    check_battery_sag,
    check_battery_temperature,
    check_battery_energy_density,
)
