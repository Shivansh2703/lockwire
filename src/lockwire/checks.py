"""Record-level checks (rung 1): do the records agree with each other?

No robot required — these run in CI on every change to the contract or its
witnesses. Every check emits the one grammar (report.Finding). The
false-positive budget is a first-class requirement: a clean contract must
produce zero findings.
"""

import functools
import hashlib
import importlib
import inspect
import itertools
import fnmatch
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel

from lockwire import plugins, statics, units
from lockwire.advice import build_rail_alternatives
from lockwire.catalog import SourcedNumber, belt_pitch, load_catalog, recognise
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
from lockwire.presets import NOT_SOURCED, choose_preset
from lockwire.schema import (
    ArtifactRef,
    Band,
    Contract,
    DeviceInstance,
    JointLimits,
    SCHEMA_VERSION,
    ModelRecord,
    Quantity,
    Rail,
    Provenance,
    Tolerance,
)
from lockwire.sdf import loop_cuts, parse_sdf, read_sdf_root, read_sdf_topology, urdf_view
from lockwire.urdf import (
    _has_doctype_entity,
    describe_structure_drift,
    legacy_axis_joints,
    locked_record_hash,
    mesh_refs,
    parse_urdf,
    read_model_bytes,
    read_urdf_topology,
    UnresolvedXacroError,
    UrdfTopology,
)
from lockwire.webots import parse_webots

if TYPE_CHECKING:
    from lockwire.plan import Plan

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
        self._n = 0
        self.records: list[ScoredRecord] = []

    def record(self, key: str) -> None:
        self._keys.add(key)
        self._n += 1  # two checks may score one key; each comparison counts

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
        return self._n


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


def requires(*fields: str):
    """Decorate a check: any named contract input absent (None) -> return [] untouched.

    Dotted names walk attributes (``"model.source"``). The body never runs, so no
    finding and no score-book entry: a missing input is silence, never NOT_SCORED.
    """

    def deco(check):
        @functools.wraps(check)
        def wrapper(contract, *args, **kwargs):
            for field in fields:
                value = contract
                for part in field.split("."):
                    value = getattr(value, part, None)
                if value is None:
                    return []
            return check(contract, *args, **kwargs)

        return wrapper

    return deco


# Engine-module registry (lane A2 implements against this; checks.py is not theirs).
# Each module below, if it exists, defines a module-level ``ENGINE_CHECKS`` tuple of
# callables run as ``check(contract, contract_path=contract_path)`` -> list[Finding].
# A function that also accepts a ``plan`` keyword gets ``plan=`` too (None when run_record_checks
# has no plan; such a function must then emit nothing, that is its contract).
# A module that does not exist yet, or exists without ENGINE_CHECKS, is skipped; any other
# import error propagates. Names: controls, pins, harness, sensitivity, webots, battery, motor_card, stress, power_budget.
ENGINE_MODULES = (
    "controls",
    "pins",
    "harness",
    "sensitivity",
    "webots",
    "battery",
    "motor_card",
    "stress",
    "power_budget",
)


def _engine_checks() -> Iterator[Callable[..., list[Finding]]]:
    for name in ENGINE_MODULES:
        try:
            module = importlib.import_module("lockwire." + name)
        except ModuleNotFoundError as error:
            if error.name != "lockwire." + name:
                raise
            continue
        yield from getattr(module, "ENGINE_CHECKS", ())


def _with_mjcf_effort(c: Contract, contract_path: Path | None) -> Contract:
    """An MJCF model keeps its joint effort in <actuator>; give the checks that read
    `joint.limits.effort` the same number a URDF's <limit effort> gives them, in memory only."""
    named = _urdf_path_candidate(c.model) if c.model and c.model.joints else None
    path = named and _model_on_disk(contract_path, named)
    from lockwire.mjcf import is_mjcf, parse_mjcf  # mjcf -> check_sim -> checks

    efforts: dict[str, float] = {}
    try:
        if path and is_mjcf(path):
            parse_mjcf(path, efforts=efforts)
    except (OSError, ET.ParseError, ValueError, TypeError):
        return c
    joints = (
        [
            j.model_copy(
                update={
                    "limits": (j.limits or JointLimits()).model_copy(
                        update={"effort": efforts[j.name]}
                    )
                }
            )
            if j.name in efforts and not (j.limits and j.limits.effort)
            else j
            for j in c.model.joints
        ]
        if efforts
        else None
    )
    return (
        c.model_copy(update={"model": c.model.model_copy(update={"joints": joints})})
        if joints
        else c
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
    plan: "Plan | None" = None,
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
    contract = _with_mjcf_effort(contract, contract_path)
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
    findings += check_budgets(contract, scores=scores, contract_path=contract_path)
    findings += check_ampacity(contract, scores=scores)
    findings += check_rail_current(contract, scores=scores)
    findings += check_rail_source_rules(contract)
    findings += check_torque_budgets(contract, scores=scores)
    findings += check_gravity_reach(contract, scores=scores, contract_path=contract_path)
    findings += check_tip_over(contract)
    findings += check_standing_load(contract)
    findings += check_hold_torque(contract, plan)
    findings += check_bus_bandwidth(contract, scores=scores)
    findings += check_zero_mass(contract, contract_path=contract_path)
    findings += check_floating_mass(contract)
    findings += check_unusable_effort(contract)
    findings += check_servo_stall(
        contract, servo or contract.servo, scores=scores, contract_path=contract_path
    )
    findings += check_servo_brownout(contract)
    findings += check_bulk_capacitance(contract)
    findings += check_mechanisms(contract, scores=scores)
    findings += check_urdf_drift(contract, scores=scores, contract_path=contract_path)
    findings += check_model_values(contract, scores=scores, contract_path=contract_path)
    findings += check_mesh_drift(contract, scores=scores, contract_path=contract_path)
    findings += check_datasheet_agreement(
        contract, scores=scores, model=model, contract_path=contract_path
    )
    findings += check_cross_file_agreement(contract, contract_path=contract_path)
    for pack_check in (
        *CHECK_PACK_1,
        *CHECK_PACK_2,
        *CHECK_PACK_3,
        *CHECK_PACK_4,
        *CHECK_PACK_5,
    ):
        findings += pack_check(contract, contract_path=contract_path)
    for engine_check in _engine_checks():
        extra = {"plan": plan} if "plan" in inspect.signature(engine_check).parameters else {}
        findings += engine_check(contract, contract_path=contract_path, **extra)
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
    if contract.modes is not None:
        # Pro mode-graph check; empty slot (free) or absent input -> silence.
        if run := PAID_CHECKS.get("modes"):
            findings += run(contract, contract_path=contract_path)
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
            f"accepted ({row.reason}, witness {row.provenance.witness or "no name on record"}, {row.provenance.locked_at})"
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
            if wire.rail is not None and c.rail(wire.rail) is None:
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
            or (pair.provenance.witness is not None and not pair.provenance.witness.strip())
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
                or (segment.provenance.witness is not None and not segment.provenance.witness.strip())
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
                or (segment.provenance.witness is not None and not segment.provenance.witness.strip())
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


def mass_sum(
    c: Contract, sources: str, frames: frozenset[str] = frozenset()
) -> tuple[float, list[str], list[str], list[str]]:
    """(kg, contributors, devices with no mass, links with no mass) for a mass budget's
    `sources` ("devices", "links" or "links+devices"). A missing mass is a gap, never 0 kg:
    no physical block and a physical block with no mass are disclosed alike. ``frames`` are
    links with no <inertial> in the model file: pure frames, 0 kg and not a gap."""
    total_kg, contributors, unknown_devices, unknown_links = 0.0, [], [], []
    if sources in ("devices", "links+devices"):
        for d in c.devices:
            if d.physical is None or d.physical.mass is None:
                unknown_devices.append(d.id)
            else:
                total_kg += d.physical.mass.base()
                contributors.append(d.id)
    if sources in ("links", "links+devices") and c.model is not None:
        for link in c.model.links:
            if link.mass is None:
                if link.name not in frames:
                    unknown_links.append(link.name)
            else:
                total_kg += link.mass.base()
                contributors.append(link.name)
    return total_kg, contributors, unknown_devices, unknown_links


def _frame_links(c: Contract, contract_path: Path | None) -> frozenset[str]:
    """Links with no <inertial> element at all in the model file: frames, not missing masses."""
    found = _urdf_root(c, contract_path) if c.model else None
    if found is None:
        return frozenset()
    bare = {k.get("name") for k in found[1].findall("link") if k.find("inertial") is None}
    return frozenset(k.name for k in c.model.links if k.mass is None and k.name in bare)


def check_budgets(
    c: Contract, *, scores: _ScoreBook | None = None, contract_path: Path | None = None
) -> list[Finding]:
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
            frames = _frame_links(c, contract_path) if "links" in b.sources else frozenset()
            total_kg, contributors, unknown_devices, unknown_links = mass_sum(c, b.sources, frames)
            framed = (
                f"; {len(frames)} links with no <inertial> counted as 0 kg frames: "
                + ", ".join(sorted(frames)[:3])
                + (", ..." if len(frames) > 3 else "")
                if frames
                else ""
            )
            for device_id in unknown_devices:
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"devices/{device_id}/physical/mass",
                        observed="mass not declared",
                        expected="a positive mass with units",
                        owner=b.owner,
                        action="declare the device's mass, or accept it excluded from this budget",
                    )
                )
            unknown_mass = unknown_devices + unknown_links
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
                        + (f"; mass unknown for {', '.join(unknown_mass)}" if unknown_mass else "")
                        + framed,
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
                    observed=format_magnitude(total_kg, "kg") + framed,
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
                            observed=format_magnitude(total_kg, "kg") + framed,
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
        if wire.rail is None:
            continue  # a pin-to-pin signal wire carries no rail current
        limit_a = _AMPACITY_A.get((wire.awg, wire.installation))
        limit_src, advisory = AMPACITY_SOURCE, False
        if limit_a is None and (lim := _wire_limits(wire)) is not None:
            # No NEC row: the cited chassis column (via the fuse rule's table) gives a warning only.
            limit_a, limit_src, advisory = lim[0], lim[1], True
        if limit_a is None:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=(
                        f"{wire.awg} AWG {wire.installation}: not scored, no ampacity "
                        "for this gauge in the NEC tables or the PowerStream chassis table"
                    ),
                    expected="a gauge listed in the NEC tables or the PowerStream chassis table",
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
                    severity="warning" if advisory else "violation",
                    quantity=f"topology/wires/{wire.id}/ampacity",
                    observed=observed,
                    expected=f"<= {format_magnitude(limit_a, 'A')} published ampacity"
                    + (" (PowerStream chassis column, rule of thumb, wiring in air)" if advisory else ""),
                    observed_quantity=Quantity(value=draw_a, unit="A"),
                    expected_quantity=Quantity(value=limit_a, unit="A"),
                    delta=(
                        f"+{format_magnitude(draw_a - limit_a, 'A')} (+{(draw_a / limit_a - 1) * 100:.0f}%)"
                    ),
                    dependent=", ".join(contributors) or None,
                    owner=wire.owner,
                    source=limit_src,
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

        limit_a = rail.current_limit.base()
        if limit_a <= 0.0:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"topology/rails/{rail.id}/current_limit",
                    observed=f"rail {rail.id!r} declares a current_limit of {format_magnitude(limit_a, 'A')}",
                    expected="a positive current_limit",
                    action="set the rail's current_limit to its fuse or supply rating in amps, above 0",
                )
            )
            continue
        draw_a = load_w / voltage_v
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
        observed_quantity=Quantity(value=peak_nm, unit="N*m"),
        expected_quantity=Quantity(value=required, unit="N*m"),
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
_LOOP_GRID = 7  # poses per driven joint across its range when a closed loop is solved
_LOOP_MAX_POSES = 2500  # four driven joints on one loop; more is refused, not ground through
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


def gravity_loads(c: Contract) -> dict[str, float] | None:
    """Worst-pose static gravity load (N*m) per rotary joint, exactly as check_gravity_reach
    scores it but for every rotary joint, limit or not; None when it cannot score."""
    got = _gravity_worst(c)
    if isinstance(got, list):
        return None
    worst, by_name = got[0], got[1]
    return {n: row[0].torque_nm for n, row in worst.items() if by_name[n].type in _ROTARY}


def _transmissions(c: Contract, contract_path: Path | None) -> tuple[set[str], dict[str, float]]:
    """(joints a <transmission> or <ros2_control> block names, joint -> mechanicalReduction),
    read from the model file; both empty when the file cannot be read."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return set(), {}
    named: set[str] = set()
    reduction: dict[str, float] = {}
    for block in (*found[1].iter("transmission"), *found[1].iter("ros2_control")):
        joints = [j.get("name", "") for j in block.findall("joint")]
        named.update(joints)
        ratio = _floats(next((r.text for r in block.iter("mechanicalReduction")), None))
        for name in joints if block.tag == "transmission" and ratio else ():
            reduction[name] = ratio[0]
    return named, reduction


_CONTACT_NAME = re.compile(r"wheel|foot(?!print)|feet|toe|paw|caster|skid", re.I)
_CONTACT_BAND_M = 0.05  # ponytail: a fixed band under the lowest candidate; declared contacts replace it


def _tip_not_scored(why: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity="model/tip_margin",
        observed=f"tip margin not computed: {why}",
        expected="one root link and at least 3 non-collinear ground contacts",
        action="use one root link and 3 contacts not in a line",
    )


@requires("model")
def check_tip_over(c: Contract, *, contacts: list[str] | None = None) -> list[Finding]:
    """NO-GO when the robot would tip over: its centre of mass sits outside the area its wheels or feet touch.

    ``contacts`` names the ground-touching links (declared contacts will pass them). Left None, they are the
    links named wheel/foot/feet/toe/paw/caster/skid within 5 cm of the lowest such link, at the zero
    pose, gravity along the root -Z. The polygon is the convex hull of their origins; that is
    geometry and needs no sourced number. Silent with no contacts or no centre of mass; not scored
    (with the reason) for a second root or fewer than three non-collinear contacts.

    The margin percent below is a convention, not a cited one: the distance from the CoM
    to the nearest polygon edge as a percent of that distance for the polygon's own vertex mean
    (100 = as centred as the polygon allows). It has no source, so it is advice only and never fails.
    """
    frames = statics.link_frames(c.model)
    named = [lk.name for lk in c.model.links if _CONTACT_NAME.search(lk.name)]
    if frames is None:
        if not (contacts or named):
            return []
        return [_tip_not_scored("the links do not form one tree from a single root link")]
    com = statics.system_com(c.model, frames)
    if com is None:
        return []
    if contacts is None:
        low = min((frames[n][0][2] for n in named), default=0.0)
        contacts = [n for n in named if frames[n][0][2] <= low + _CONTACT_BAND_M]
    hull = statics.support_polygon([frames[n][0][:2] for n in contacts if n in frames])
    if hull is None:
        if not contacts:
            return []
        return [_tip_not_scored("the contacts do not span an area (fewer than 3 non-collinear points)")]
    dist = statics.tip_distance(com[:2], hull)
    where = f"CoM at ({com[0]:.3f}, {com[1]:.3f}) m over {len(hull)} contact points ({', '.join(contacts)})"
    if dist < 0:
        return [
            Finding(
                severity="violation",
                quantity="model/tip_over",
                observed=f"{where} falls {-dist * 1000:.0f} mm outside the support polygon: it tips",
                expected="CoM ground projection inside the support polygon",
                action="move mass toward the support polygon or widen the contacts",
            )
        ]
    centred = statics.tip_distance(
        (sum(p[0] for p in hull) / len(hull), sum(p[1] for p in hull) / len(hull)), hull
    )
    return [
        Finding(
            severity="warning",
            quantity="model/tip_margin",
            observed=f"{where} is {dist * 1000:.0f} mm inside the edge: tip margin {100 * dist / centred:.0f}%",
            expected="no cited margin threshold",
            action="none needed unless the robot will tilt or accelerate hard",
            advice_unsourced=True,  # margin convention from memory, no source yet
        )
    ]


_LEG_NAME = re.compile(r"foot(?!print)|feet|toe|tibia|leg", re.I)


def _standing_rows(
    c: Contract,
) -> tuple[float, int, list[tuple[str, float, float | None, bool]]] | None:
    """(body weight N, contact count, [(joint, standing torque N*m, effort limit or None)]) for a
    robot on its declared support contacts; None without contacts, a single-root tree or link masses.

    Static, level ground, the weight (all link masses x g) shared equally over the contacts, the
    zero pose. Each contact pushes up with that share; a joint on the root-to-contact chain
    carries |(r x F) . axis|, r from the joint origin to the contact point. A joint on several
    contact chains carries the sum of the signed torques (one force vector per contact).
    ponytail: equal share, not the CoM-weighted share; upgrade by solving the support reactions.
    """
    if c.model is None or not c.contacts:
        return None
    frames = statics.link_frames(c.model)
    mass = sum(lk.mass.base() for lk in c.model.links if lk.mass is not None and lk.mass.base() > 0)
    if frames is None or mass <= 0:
        return None
    by_child = {j.child: j for j in c.model.joints}
    load = mass * statics.STANDARD_G / len(c.contacts)
    signed: dict[str, float] = {}
    for sc in c.contacts:
        if sc.link not in frames:
            continue
        pos, rot = frames[sc.link]
        foot = pos + rot @ np.array(sc.xyz)
        j, seen = by_child.get(sc.link), set()
        while j is not None and j.name not in seen:
            seen.add(j.name)
            if j.type in _ROTARY and j.axis is not None:
                jpos, jrot = frames[j.child]
                axis = jrot @ np.array(j.axis)
                signed[j.name] = signed.get(j.name, 0.0) + float(
                    np.dot(np.cross(foot - jpos, [0.0, 0.0, load]), axis)
                )
            j = by_child.get(j.parent)
    limits = {j.name: j.limits.effort_limit if j.limits else None for j in c.model.joints}
    cited = {n for n, v in limits.items() if v is not None}  # the model's own declared effort
    for d in c.devices:  # no joint effort: the driving servo's catalogued peak torque is the limit
        m = c.device_model(d.model)
        peak = m.actuator.torque_peak if m is not None and m.actuator is not None else None
        if d.drives_joint in limits and limits[d.drives_joint] is None:
            limits[d.drives_joint] = peak
            # cited only when that device model carries a provenance with a non-blank source
            if peak is not None and m.provenance is not None and m.provenance.source.strip():
                cited.add(d.drives_joint)
    return (
        mass * statics.STANDARD_G,
        len(c.contacts),
        [(n, abs(t), limits[n], n in cited) for n, t in signed.items()],
    )


def check_standing_load(c: Contract) -> list[Finding]:
    """Warns when a joint cannot carry the robot standing on its declared support contacts.

    check_gravity_reach hangs each leg from a fixed body; a legged robot on its feet loads the
    leg joints with the whole weight. Runs only when the lockfile declares ``contacts`` (see
    ``_standing_rows`` for the model). A joint over its own declared effort limit is a warning
    (that limit is the model's own); the per-joint margins are one advice line (the equal share is
    an unsourced convention). Never a violation. A legged robot with no contacts declared gets one
    advice note saying standing load was not checked; "legged" is two or more leaf links
    named foot/feet/toe/tibia/leg.
    """
    if c.model is None:
        return []
    got = _standing_rows(c)
    if got is None:
        parents = {j.parent for j in c.model.joints}
        legs = [k.name for k in c.model.links if _LEG_NAME.search(k.name) and k.name not in parents]
        if c.contacts or len(legs) < 2:
            return []
        return [
            Finding(
                severity="warning",
                quantity="model/standing_load",
                observed=f"{len(legs)} leg ends ({legs[0]}, ...) and no support contacts declared: the load on the leg joints while standing was not checked",
                expected="support contacts declared in the lockfile",
                action="declare the foot contacts to check standing torque",
                advice_unsourced=True,
            )
        ]
    weight, n, rows = got
    assumed = (
        f"static stance, level ground, equal share across {n} declared contacts, zero pose, "
        "no dynamics: a pass here does not mean the robot can walk"
    )
    out = [
        Finding(
            severity="warning",
            quantity=f"model/joints/{name}/standing_torque",
            observed=f"joint cannot carry the robot standing: {tq:.2f} N*m standing torque ({assumed})",
            expected=f"<= {lim:.2f} N*m joint effort limit (or driving servo peak torque when none is declared)",
            delta=f"+{tq - lim:.2f} N*m",
            action="raise its effort limit, add contacts or lighten the robot",
            advice_unsourced=not cited,  # an uncited servo peak is advice, never a plain warning
        )
        for name, tq, lim, cited in rows
        if lim is not None and tq > lim
    ]
    margins = "; ".join(
        f"{name} {tq:.2f}" + (f"/{lim:.2f} N*m ({lim - tq:+.2f})" if lim is not None else " N*m")
        for name, tq, lim, _ in rows
    )
    out.append(
        Finding(
            severity="warning",
            quantity="model/standing_load",
            observed=(
                f"standing margins (torque/limit, margin): {margins}. Assumes static, level ground, "
                f"the {weight:.1f} N weight shared equally over {n} contacts, zero pose, no dynamics"
            ),
            expected="no cited standing-load rule",
            action="none needed unless the robot will walk; walking loads are higher",
            advice_unsourced=True,
        )
    )
    return out


def check_hold_torque(c: Contract, plan: "Plan | None") -> list[Finding]:
    """Warns when holding a plan pose against gravity needs more torque than the joint can give.

    One finding per segment and joint, from the same torque code check_gravity_reach uses.
    Silent with no plan, no segment pose, or a model gravity_plan cannot walk.
    """
    if plan is None or c.model is None:
        return []
    try:
        gp = statics.gravity_plan(c.model)
    except statics.ChainError:
        return []
    if gp is None:
        return []
    out = []
    for seg in plan.segments:
        if not seg.pose:
            continue
        names = list(seg.pose)
        rows = statics.plan_rows(gp, statics.plan_torques(gp, names, [list(seg.pose.values())])[0])
        for row in rows:
            if row.margin_nm is not None and row.margin_nm < 0:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"plan/segments/{seg.name}/hold_torque/{row.joint}",
                        observed=f"{row.torque_nm:.2f} N*m holding torque at this pose",
                        expected=f"<= {row.limit_nm:.2f} N*m joint effort limit",
                        source="the joint effort limit declared in the model",
                        action=f"lower the load or change the pose of {seg.name}",
                    )
                )
    return out


def check_gravity_reach(
    c: Contract, *, scores: _ScoreBook | None = None, contract_path: Path | None = None
) -> list[Finding]:
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

    A closed loop in an SDF (a five-bar leg) is solved rather than walked: the loop is closed at
    each pose of a grid over its driven joints' ranges (driven: a positive effort limit) and each
    driven joint holds its share by virtual work, a passive pin holding nothing. That is still
    the base held with no ground contact, not a standing load. ``not_scored`` for the loops it
    does not solve: not planar, driven joints that do not exactly fix the loop, a joint moving
    above or below the loop, or no SDF file on disk to read the pins. A grid pose the linkage
    locks at or cannot reach is left out of the peak and counted in the output.

    From the model file: when it names its driven joints in <transmission>/<ros2_control>
    blocks, a slide none of them names is passive (a suspension), so its load is not compared.
    A joint over its effort whose transmission has mechanicalReduction > 1 is a warning, not a
    violation: URDF effort is joint-side, but some files (Cassie) write the motor side.
    """
    got = _gravity_worst(c, contract_path)
    if isinstance(got, list):
        return got
    worst, by_name, note, out = got
    driven, reduction = _transmissions(c, contract_path) if worst else (set(), {})
    for name, (r, pose, how, dev) in worst.items():
        if r.limit_nm is None:
            continue
        quantity = f"model/joints/{name}/limits/effort"
        if driven and name not in driven and by_name[name].type == "prismatic":
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=(
                        f"passive slide: no <transmission> or <ros2_control> names {name} while "
                        f"{len(driven)} other joints have one; its {r.torque_nm:.2f} N load is not "
                        f"compared with its {r.limit_nm:.2f} N effort"
                    ),
                    expected="an actuated joint for the effort limit to be a holding limit",
                    action=f"if {name} is driven, add its transmission to the model",
                )
            )
            continue
        payload = f"with {r.payload_kg:g} kg payload" if r.payload_kg else "no payload declared"
        looped = how.startswith("closed loop")
        unit, what = (
            ("N", "N weight along the slide axis")
            if by_name[name].type == "prismatic"
            else ("N*m", "N*m static gravity torque")
        )
        observed = (
            f"{r.torque_nm:.2f} {what} at pose "
            + ", ".join(
                f"{k}={round(d) if math.isfinite(d := math.degrees(v)) else d}deg"
                for k, v in pose.items()
            )
            + f" ({'' if looped else 'peak over its own range, other joints at '}{how}; "
            f"{payload}; {dev}{note})"
        )
        expected = f"<= {r.limit_nm:.2f} {unit} joint effort limit"
        if scores is not None:
            scores.record(quantity)
        if r.torque_nm <= r.limit_nm:
            if scores is not None:
                scores.passed(
                    quantity,
                    f"{r.torque_nm:.2f} {unit}"
                    + (f" {payload}" if r.payload_kg else "")
                    + (f" ({how.split(', servos')[0]}; base held)" if looped else ""),
                    expected,
                    f"{r.margin_nm:.2f} {unit} headroom",
                    detail=observed,
                )
            continue
        gear = reduction.get(name, 1.0)
        motor_side = gear > 1 and r.torque_nm <= r.limit_nm * gear
        if motor_side:
            expected += (
                f", or <= {r.limit_nm * gear:.2f} {unit} if that effort is motor-side "
                f"(x {gear:g} mechanicalReduction in its <transmission>); not compared: the "
                "model does not say which side"
            )
        out.append(
            Finding(
                severity="warning" if motor_side else "violation",
                quantity=quantity,
                observed=observed,
                expected=expected,
                delta=f"+{r.torque_nm - r.limit_nm:.2f} {unit}",
                dependent=quantity,
                action=(
                    f"if {name}'s effort is joint-side, as URDF intends, the joint cannot hold "
                    "this pose; if motor-side, write the joint-side effort into the model"
                    if motor_side
                    else "the joint cannot hold this pose against gravity: raise the effort limit "
                    "with a stronger actuator, shorten the reach, or lighten the links"
                ),
            )
        )
    return out


def _loop_worst(
    c: Contract, contract_path: Path | None, mounted: dict, worst: dict, solved: set[str]
) -> str | None:
    """Score each closed loop's driven joints into ``worst`` with the loop solved at every pose of
    a grid over their ranges, and name every joint of a solved loop in ``solved``; returns why
    not, when it could not. The pins come from the SDF on disk: a record alone does not say
    where a looped link's frame sits (sdf.loop_cuts)."""
    names = (c.model.provenance.source, c.model.source)
    named = next((n for n in names if n.lower().endswith(".sdf")), None)
    if named is None:
        return None  # not an SDF: the walk's own reason stands
    path = _model_on_disk(contract_path, named)
    if not path:
        return "closed chain: the loop's pins are read from the SDF file, which is not on disk"
    try:
        lp = statics.loop_plan(c.model, loop_cuts(path), mounted)
        for loop in lp.loops if lp is not None else ():
            driven, passive, pins = loop
            sides = []
            for j in driven:  # ponytail: an unlimited servo sweeps +/-180 deg
                lo = j.limits.lower if j.limits.lower is not None else -math.pi
                hi = j.limits.upper if j.limits.upper is not None else math.pi
                sides.append(np.linspace(lo, hi, _LOOP_GRID))
            if _LOOP_GRID ** len(driven) > _LOOP_MAX_POSES:
                return f"closed chain: {len(driven)} driven joints on one loop are too many to grid"
            poses = np.array(list(itertools.product(*sides)))
            held = np.abs(statics.loop_torques(lp, loop, poses))
            closed = int(np.isfinite(held).all(axis=1).sum())
            if not closed:
                return "closed chain: the loop locks or stays open at every pose tried"
            # the poses left out are the ones nearest locking, where the torque is largest: say so
            near = round(math.degrees(math.acos(1 - statics._LOOP_SINGULAR**2)))
            short = f"closed loop solved at {closed} of {len(poses)} grid poses" + (
                ""
                if closed == len(poses)
                else f"; peak is over those only, the other {len(poses) - closed} lock, sit within "
                f"{near} deg of locking or cannot be reached, and torque rises toward locking"
            )
            how = (
                f"{short}, servos "
                f"{', '.join(j.name for j in driven)}, free pins {', '.join(j.name for j in passive)}"
                "; base held, no ground contact, so not a standing load"
            )
            for k, j in enumerate(driven):
                i = int(np.nanargmax(held[:, k]))
                lim = j.limits.effort_limit
                kg = float((lp.plan.carries @ lp.plan.payload)[lp.plan.joints.index(j)])
                row = statics.JointTorque(
                    j.name, float(held[i, k]), lim, lim - float(held[i, k]), kg
                )
                pose = {d.name: float(v) for d, v in zip(driven, poses[i])}
                dev = "device masses included" if mounted else "no device masses"
                worst[j.name] = (row, pose, how, dev)
            ends = {(a, b) for a, _, b, _ in pins}  # the cut pins, by the links they join
            solved |= {j.name for j in (*driven, *passive)}
            solved |= {j.name for j in c.model.joints if (j.parent, j.child) in ends}
    except (OSError, ET.ParseError, ValueError) as e:  # ChainError is a ValueError
        return str(e)
    return None


def _gravity_worst(
    c: Contract, contract_path: Path | None = None
) -> list[Finding] | tuple[dict, dict, str, list[Finding]]:
    """check_gravity_reach's worst-pose walk: findings when it cannot score, else (worst row,
    pose, how, devices per joint; joints by name; note; not-scored findings)."""
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
    unwalked: list[tuple[str, str]] = []  # (joint, why gravity_plan refused it)
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
        try:
            plan = statics.gravity_plan(c.model, under=j.name, devices=mounted)
        except statics.ChainError as e:
            unwalked.append((j.name, str(e)))  # this joint only: an open chain beside it scores
            continue
        done.append(key)
        if plan is None:
            return []  # no single root or an off-frame CoM: silent, as before
        for kk, joint in enumerate(plan.joints):
            lim = joint.limits.effort_limit if joint.limits is not None else None
            if (joint.type == "prismatic") != (j.type == "prismatic"):
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
                margin = None if lim is None else lim - float(best[i])
                row = statics.JointTorque(joint.name, float(best[i]), lim, margin, kg)
                nd = int((plan.carries @ (plan.device > 0))[kk])
                dkg = float((plan.carries @ plan.device)[kk])
                dev = f"includes {nd} device masses ({dkg:g} kg)" if nd else "no device masses"
                pose = {n: v for n, v in zip(names, poses[i]) if by_name[n].type in _ROTARY}
                if joint.type in _ROTARY:
                    pose[joint.name] = float(q[i])
                worst[joint.name] = (row, pose, how, dev)
    out: list[Finding] = []
    solved: set[str] = set()
    looped = any(w.startswith("closed chain") for _, w in unwalked)
    why = _loop_worst(c, contract_path, mounted, worst, solved) if looped else None
    left = [(n, w) for n, w in unwalked if n not in solved]
    if left:
        names, first = [n for n, _ in left], left[0][1]
        out += _reach_not_scored(
            f"{', '.join(names[:3])}{', ...' if len(names) > 3 else ''}: "
            + (why if why and first.startswith("closed chain") else first)
        )
    skipped = sorted(n for n, (_, k) in sets.items() if k in over and n not in worst)
    if skipped:
        out += _reach_not_scored(
            f"{', '.join(skipped[:3])}{', ...' if len(skipped) > 3 else ''} carry too many "
            f"rotary joints to keep the +/-90 deg poses within the {_REACH_MAX_POSES} pose grid"
        )
    return worst, by_name, note, out


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
                    observed_quantity=Quantity(value=available, unit="N*m"),
                    expected_quantity=Quantity(value=required, unit="N*m"),
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


def _bad_masses(root: ET.Element) -> dict[str, str]:
    """Link name -> its mass text, for each mass that is NaN or negative."""
    bad: dict[str, str] = {}
    for el in root.findall("link"):
        raw = el.find("inertial/mass")
        raw = raw.get("value", "") if raw is not None else ""
        v = _floats(raw)
        if v and not v[0] >= 0.0:  # NaN fails >= too
            bad[el.get("name", "")] = raw
    return bad


def check_link_values(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Link values no simulator accepts: a NaN or negative mass, a mass with no inertia, a
    mesh scale that is NaN, zero or not three numbers."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    for name, raw in _bad_masses(root).items():
        warn(
            f"{name}/mass_value",
            f"link '{name}' has mass {raw}; a mass must be a positive number",
            "a positive mass, or no mass for a pure frame",
            f"fix link '{name}' mass in the source model",
            severity="violation",
        )
    for el in root.findall("link"):
        name = el.get("name", "")
        mass = (
            _floats(el.find("inertial/mass").get("value"))
            if el.find("inertial/mass") is not None
            else []
        )
        if mass and mass[0] > 0 and el.find("inertial/inertia") is None:
            warn(
                f"{name}/no_inertia",
                f"link '{name}' has mass {mass[0]:g} kg but no inertia; simulators treat it as zero",
                "an <inertia> with the mass",
                f"add link '{name}' inertia from CAD",
            )
        for mesh in el.iter("mesh"):
            raw = mesh.get("scale")
            v = _floats(raw)
            if raw is not None and (len(v) != 3 or not all(math.isfinite(x) and x for x in v)):
                warn(
                    f"{name}/mesh_scale_value",
                    f"link '{name}' mesh {mesh.get('filename', '?')} has scale '{raw}'",
                    "three finite non-zero numbers (a negative one mirrors the mesh)",
                    f"fix link '{name}' mesh scale",
                )
    return out


def check_zero_mass(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Missing or non-positive link mass is derived from the canonical link
    records so a stale zero_mass_links cache cannot hide incomplete inertia."""
    if c.model is None:
        return []
    found = _urdf_root(c, contract_path)
    bad = _bad_masses(found[1]) if found else {}  # check_link_values says these, not "a frame"
    from lockwire.mjcf import is_mjcf  # mjcf -> check_sim -> checks

    synthetic = {"world"} if found and is_mjcf(found[0]) else set()  # the user wrote no such link
    massless = [
        k.name
        for k in c.model.links
        if (k.mass is None or k.mass.base() <= 0.0)
        and k.name not in bad
        and k.name not in synthetic
    ]
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


@requires("model")
def check_floating_mass(c: Contract) -> list[Finding]:
    """Warns about a link with weight that no joint ties to the robot's main body.

    The main body is the root whose joints reach the most links; a weightless frame left
    loose is fine, and a model with no joints says nothing.
    """
    joints = c.model.joints
    if not joints:
        return []
    kids: dict[str, list[str]] = {}
    for j in joints:
        kids.setdefault(j.parent, []).append(j.child)
    children = {j.child for j in joints}

    def reach(root: str) -> set[str]:
        seen, stack = {root}, [root]
        while stack:
            for k in kids.get(stack.pop(), []):
                if k not in seen:
                    seen.add(k)
                    stack.append(k)
        return seen

    roots = [k.name for k in c.model.links if k.name not in children]
    if not roots:
        return []
    main = max((reach(r) for r in roots), key=len)
    return [
        Finding(
            severity="warning",
            quantity=f"model/links/{k.name}/mass",
            observed=f"link '{k.name}' weighs {k.mass.base():.3g} kg but hangs off no joint",
            expected="every link with weight attached to the robot by a joint",
            action="attach it with a joint or delete it",
        )
        for k in c.model.links
        if k.name not in main and k.mass is not None and k.mass.base() > 0.0
    ]


@requires("model")
def check_no_material(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Notes a part that has shape but no mass and no material, so there is nothing to weigh it by.

    check_zero_mass already warns on every massless link that carries mass below it; those
    are skipped here, so a part is never reported twice.
    """
    found = _urdf_root(c, contract_path)
    if found is None:
        return []
    said = {f.quantity for f in check_zero_mass(c, contract_path=contract_path)}
    massless = {k.name for k in c.model.links if k.mass is None or k.mass.base() <= 0.0}
    return [
        Finding(
            severity="warning",
            quantity=f"model/links/{name}/material",
            observed=f"link '{name}' has shapes but no mass and no material",
            expected="a mass, or a material to weigh the shapes by",
            action=f"declare {name}'s mass, or note it as a pure visual",
            advice_unsourced=True,  # a massless sensor frame is normal; never a verdict
        )
        for el in found[1].findall("link")
        if (name := el.get("name")) in massless
        and f"model/links/{name}/mass" not in said
        and (el.find("visual/geometry") is not None or el.find("collision/geometry") is not None)
        and el.find("visual/material") is None
    ]


def _shape_parts(link: ET.Element, mass: float) -> list[tuple] | None:
    """Collision (else visual) primitives of a link as (mass share, COM, own inertia) with the
    mass split by volume, or None when a mesh, rotation or odd shape makes it unsafe to say."""
    kind = "collision" if link.find("collision/geometry") is not None else "visual"
    raw = []
    for el in link.findall(kind):
        shape = el.find("geometry")[0]
        origin = el.find("origin")
        if origin is not None and any(_floats(origin.get("rpy", "0 0 0"))):
            return None  # ponytail: rotated shapes unhandled; add a rotation if one matters
        xyz = tuple(_floats(origin.get("xyz", "0 0 0"))) if origin is not None else (0.0,) * 3
        r, h = ((_floats(shape.get(k)) or [0.0])[0] for k in ("radius", "length"))
        if shape.tag == "box" and len(_floats(shape.get("size"))) == 3:
            dims, vol = tuple(_floats(shape.get("size"))), math.prod(_floats(shape.get("size")))
        elif shape.tag == "cylinder":
            dims, vol = (r, h), math.pi * r * r * h
        elif shape.tag == "sphere":
            dims, vol = (r,), 4 / 3 * math.pi * r**3
        else:
            return None
        raw.append((shape.tag, dims, xyz, vol))
    total = sum(v for *_, v in raw)
    if not raw or total <= 0:
        return None
    return [
        (mass * v / total, xyz, statics.primitive_inertia(k, d, mass * v / total))
        for k, d, xyz, v in raw
    ]


@requires("model")
def check_inertia_sanity(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Warns when a link's declared inertia is far from what its own shapes would give at its mass."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return []
    out = []
    for el in found[1].findall("link"):
        link = next((k for k in c.model.links if k.name == el.get("name")), None)
        if link is None or link.inertia is None or link.mass is None or link.mass.base() <= 0:
            continue
        parts = _shape_parts(el, link.mass.base())
        if parts is None:
            continue
        computed = float(np.trace(statics.merge_inertia(parts)[2]))
        declared = sum(link.inertia.moments_kgm2)
        if computed > 0 and not 0.1 <= declared / computed <= 10:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/links/{link.name}/inertia_sanity",
                    observed=f"declared inertia sums to {declared:.4g} kg*m^2; "
                    f"its shapes at {link.mass.base():g} kg give {computed:.4g} kg*m^2",
                    expected="inertia of the same order as the shapes give",
                    action=f"recompute {link.name}'s inertia from CAD",
                    # ponytail: the 10x band has no cited source; it is advice only
                    advice_unsourced=True,
                )
            )
    return out


def servo_stall_torque(servo: str) -> SourcedNumber:
    """The stall torque (N*m) of catalog part ``servo``; ValueError names the parts that have one."""
    parts = [p for p in load_catalog().parts if "stall_torque" in p.numbers]
    for part in parts:
        if part.id == servo:
            return part.numbers["stall_torque"]
    known = ", ".join(sorted(p.id for p in parts))
    raise ValueError(f"no catalog servo {servo!r} with a stall torque (known: {known})")


def servo_for(servo: str | dict[str, str] | None, joint: str) -> str | None:
    """The catalog servo id a `servo:` line names for ``joint``: the one id, or the first
    matching joint-name pattern of a map; None when nothing matches."""
    rules = servo if isinstance(servo, dict) else {"*": servo} if servo else {}
    return next((s for pat, s in rules.items() if fnmatch.fnmatchcase(joint, pat)), None)


def shown_path(path: Path | None) -> str:
    """The lockfile as a command can name it from where the user is: as typed when relative,
    cwd-relative when absolute under cwd, else absolute (always runnable)."""
    if path is None:
        return "lockwire.yaml"
    if path.is_absolute() and path.is_relative_to(Path.cwd()):
        return str(path.relative_to(Path.cwd()))
    return str(path)


def _effort_edit(c: Contract, joint: str, effort: float, stall: float) -> str:
    """The exact model edit that brings ``joint``'s effort to the stall, rounded down to
    0.001 (the precision the finding prints the stall at) so it never exceeds it. Lockwire
    shows this line; it never writes the model file."""
    to = f"{math.floor(stall * 1000 + 1e-9) / 1000:.3f}"
    name = c.model.source.rsplit("/", 1)[-1]
    if name.endswith((".urdf", ".xacro")):
        return f'in {name}: joint {joint} <limit effort="{effort:g}"> → "{to}"'
    if name.endswith(".sdf"):
        return f"in {name}: joint {joint} <effort>{effort:g}</effort> → {to}"
    return f"in this lockfile's model: joints {joint} limits effort {effort:g} → {to}"


def check_servo_stall(
    c: Contract,
    servo: str | dict[str, str] | None,
    *,
    scores: _ScoreBook | None = None,
    contract_path: Path | None = None,
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
                    action=(
                        "if this is a private part, add it to ~/.lockwire/parts.yaml "
                        "(see the parts guide); otherwise fix the servo: line to a known id"
                    ),
                )
            )
    for joint in c.model.joints:
        effort = joint.limits.effort if joint.limits else None
        sid = servo_for(rules, joint.name)
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
                expected=f"<= {stall.value:.3f} N*m, the {sid} stall torque",
                delta=f"{effort / stall.value:.1f}x the stall torque",
                source=stall.source,
                action=(
                    f"a simulator will let {joint.name} push {effort:g} N*m, which a {sid} cannot; "
                    f"{_effort_edit(c, joint.name, effort, stall.value)}, then re-lock "
                    "(ignore if another motor drives it); or find a stronger servo: "
                    f"{cli_line('lockwire', 'swap', shown_path(contract_path), joint.name)}"
                ),
            )
        )
    return out


def _servo_rail_volts(c: Contract, rail: Rail, loads: list, fraction: float) -> float:
    """Voltage at the servos on ``rail`` when each draws ``fraction`` of its stall current.

    ``loads`` is [(stall_a, rated_v)]. Chain: pack resistance -> regulator (holds only while
    input >= min_input_v or Vout + dropout_v) -> wire run; every term that cannot be computed
    is left out (shrinks the sag). Current at V is I_rated * min(1, V / V_rated) * fraction;
    V solves g(V) = V, g falling in V, by bisection on [0, rail voltage], upper end kept
    (at or above the root: the sag is only ever understated).
    """
    battery = c.topology.battery
    vout = rail.voltage.base()

    def sag(amps: float) -> float:
        v = vout
        if rail.upstream == "battery" and battery is not None and battery.voltage is not None:
            vin = battery.voltage.base()
            if battery.internal_resistance_mohm is not None:
                # input current: lower bound (efficiency 1; a buck draws Vout/Vin of the output)
                i_in = amps if rail.regulator_kind == "ldo" else amps * min(1.0, vout / vin)
                vin -= i_in * battery.internal_resistance_mohm / 1000.0
            hold = rail.min_input_v or (vout + rail.dropout_v if rail.dropout_v else None)
            if hold is not None and vin < hold:
                v = vin - (rail.dropout_v or 0.0)
        return v - sum(
            wire_drop_v(c, w, amps) or 0.0 for w in c.topology.wires if w.rail == rail.id
        )

    lo, v = 0.0, vout  # g(v) <= v at the top of the bracket, g(lo) > lo
    for _ in range(40):
        mid = (lo + v) / 2
        amps = fraction * sum(i * min(1.0, mid / vr) for i, vr in loads)
        lo, v = (lo, mid) if sag(amps) <= mid else (mid, v)
    return v


def rail_sweep(
    c: Contract, rail_id: str, fractions: tuple[float, ...] = (0, 0.25, 0.5, 0.75, 1.0)
) -> list[tuple[float, float]] | None:
    """DC sweep: [(load fraction, volts at the servos)] for rail ``rail_id``, one point per
    entry of ``fractions`` in the order given; fraction 1.0 is every servo stalled (the
    brownout point), 0 is no load. Non-increasing in the fraction. None when the rail has no
    voltage or no servo with catalog stall data (a missing input is silence).
    Data only: it emits no finding, brownout and operating point already report the minimums.
    """
    rail = c.rail(rail_id) if c.topology else None
    if rail is None or rail.voltage is None:
        return None
    parts = {
        p.id: (p.numbers["stall_current"].value, p.numbers["stall_current_voltage"].value)
        for p in load_catalog().parts
        if "stall_current" in p.numbers and "stall_current_voltage" in p.numbers
    }
    loads = [
        n
        for d in c.devices
        if d.power is not None and d.power.rail == rail.id and d.drives_joint
        and (n := parts.get(servo_for(c.servo, d.drives_joint) or "")) is not None
    ]  # fmt: skip
    if not loads:
        return None
    return [(f, _servo_rail_volts(c, rail, loads, f)) for f in fractions]


# Bulk-capacitance rule of thumb: 470 uF per servo on the rail. No maker application note was
# read for this number, so it ships as advice (advice_unsourced), never a verdict.
_BULK_UF_PER_SERVO = 470.0


@requires("topology")
def check_bulk_capacitance(c: Contract) -> list[Finding]:
    """A servo rail whose declared Rail.capacitance_f is below 470 uF per servo (advice, unsourced).

    A rail with no capacitance_f, or with no servo on it, emits nothing.
    """
    out: list[Finding] = []
    for rail in c.topology.rails:
        n = sum(1 for d in c.devices if d.power and d.power.rail == rail.id and d.drives_joint)
        if rail.capacitance_f is None or not n:
            continue
        need = n * _BULK_UF_PER_SERVO * 1e-6
        if rail.capacitance_f >= need:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"topology/rails/{rail.id}/capacitance_f",
                observed=f"{rail.capacitance_f * 1e6:g} uF on servo rail {rail.id} ({n} servos)",
                expected=f">= {need * 1e6:g} uF ({_BULK_UF_PER_SERVO:g} uF per servo)",
                action=f"add bulk capacitance near the servos on rail {rail.id!r}",
                advice_unsourced=True,  # rule of thumb, no maker note read
            )
        )
    return out


def check_servo_brownout(c: Contract) -> list[Finding]:
    """NO-GO: every servo on a rail at stall at once pulls a device it feeds
    below that device's declared minimum input.

    Stall current is the catalog ``stall_current`` of the servo the lockfile's ``servo:`` line
    names for the device's ``drives_joint`` (same source as check_wire_drop); a servo not in
    the catalog has none (or no ``stall_current_voltage``) and adds nothing. The catalog
    stall current is rated at ``stall_current_voltage``; a stalled winding is resistive, so at
    the voltage V the servo actually sees it draws I_rated * min(1, V / V_rated). Never scaled
    up above the rating, so the current is never overstated. V is the sag chain's own output:
    the servo voltage solves g(V) = V, where g (current at V, then the sag chain) falls as V
    rises, so it is found by bisection on [0, rail voltage] and the upper end of the bracket
    is kept: at or above the true root, so the sag can only be understated and a NO-GO stays
    true.
    Chain: pack resistance -> regulator (holds only while input >= min_input_v or
    Vout + dropout_v) -> wire run. Every term that cannot be computed (no length, no pack
    resistance, no catalog stall current) is LEFT OUT, which can only shrink the sag, so a NO-GO stays
    true. No stall data, rail voltage or device minimum: nothing is emitted.
    """
    if c.topology is None:
        return []
    out: list[Finding] = []
    parts = {
        p.id: (p.numbers["stall_current"].value, p.numbers["stall_current_voltage"].value)
        for p in load_catalog().parts
        if "stall_current" in p.numbers and "stall_current_voltage" in p.numbers
    }
    for rail in c.topology.rails:
        if rail.voltage is None:
            continue
        on_rail = [d for d in c.devices if d.power is not None and d.power.rail == rail.id]
        loads = [
            n
            for d in on_rail
            if d.drives_joint
            and (n := parts.get(servo_for(c.servo, d.drives_joint) or "")) is not None
        ]
        floors = [
            (d.id, m.input_voltage_range.min.base())
            for d in on_rail
            if (m := c.device_model(d.model)) is not None and m.input_voltage_range is not None
        ]
        if not loads or not floors:
            continue
        vout = rail.voltage.base()
        v = _servo_rail_volts(c, rail, loads, 1.0)
        amps = sum(i * min(1.0, v / vr) for i, vr in loads)
        who, floor = max(floors, key=lambda f: f[1])
        if v >= vout or v >= floor:
            continue
        out.append(
            Finding(
                severity="violation",
                quantity=f"topology/rails/{rail.id}/brownout",
                observed=f"{rail.id} sags to {v:.3g} V with every servo stalled ({amps:.3g} A)",
                expected=f">= {floor:g} V, the declared minimum input of {who}",
                observed_quantity=Quantity(value=v, unit="V"),
                expected_quantity=Quantity(value=floor, unit="V"),
                action=(
                    "shorten or thicken the wire, cut the pack resistance, "
                    "or move the servos to their own rail"
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


_MODEL_SUFFIXES = (".urdf", ".xacro", ".sdf", ".xml", ".mjcf", ".wbt", ".proto")
_WEBOTS = (".wbt", ".proto")


def _webots_topology(path: Path) -> UrdfTopology:
    """Webots twin of `read_sdf_topology`, for the drift and value checks."""
    model = parse_webots(path)
    return UrdfTopology(
        link_names=frozenset(k.name for k in model.links),
        joints=tuple(model.joints),
        topology_hash=model.structure_hash,
        record_hash=model.structure_hash,
    )


def _webots_view(path: Path) -> ET.Element:
    """The Webots model as a URDF-shaped `<robot>`, for the checks that read URDF XML."""
    model = parse_webots(path)
    robot = ET.Element("robot", name=model.links[0].name)
    for link in model.links:
        out = ET.SubElement(robot, "link", name=link.name)
        inertial = ET.SubElement(out, "inertial")
        mass = link.mass.value if link.mass else 0.0
        ET.SubElement(inertial, "mass", value=str(mass))
        if link.com:
            ET.SubElement(inertial, "origin", xyz=" ".join(map(str, link.com)))
        if link.inertia:
            t = np.array(link.inertia.axes).T @ np.diag(link.inertia.moments_kgm2) @ np.array(link.inertia.axes)  # fmt: skip
            names = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
            vals = (t[0, 0], t[0, 1], t[0, 2], t[1, 1], t[1, 2], t[2, 2])
            ET.SubElement(inertial, "inertia", {n: repr(float(v)) for n, v in zip(names, vals)})
    for joint in model.joints:
        out = ET.SubElement(robot, "joint", name=joint.name, type=joint.type)
        ET.SubElement(out, "parent", link=joint.parent)
        ET.SubElement(out, "child", link=joint.child)
        ET.SubElement(out, "origin", xyz=" ".join(map(str, joint.origin.xyz)))
        if joint.axis:
            ET.SubElement(out, "axis", xyz=" ".join(map(str, joint.axis)))
        lim = joint.limits
        if lim:
            attrs = {k: str(getattr(lim, k)) for k in ("lower", "upper", "effort", "velocity") if getattr(lim, k) is not None}  # fmt: skip
            ET.SubElement(out, "limit", attrs)
    return robot


def _urdf_path_candidate(model: ModelRecord) -> str | None:
    """Return the URDF/xacro/SDF path named by provenance (preferred) or model.source."""
    for candidate in (
        (model.provenance.source if model.provenance else "", model.source)
        if model.source != "lockfile"
        else ()
    ):
        if candidate.lower().endswith(_MODEL_SUFFIXES):
            return candidate
    return None


def _model_on_disk(contract_path: Path | None, named: str) -> Path | None:
    """The model file a lock names: an absolute path as written, a relative one beside the lock.

    A relative path with no lock to anchor it is refused: it would be read from the
    working directory, which may hold a same-named file that is not this robot's.
    """
    absolute = Path(named).expanduser()
    if absolute.is_absolute():
        return absolute
    return resolve_source(contract_path, named) if contract_path else None


def _check_self_structure(model: ModelRecord, scores: _ScoreBook | None) -> list[Finding]:
    """A lockfile-sourced model is scored on its own structure: every joint joins two links of
    the lock and no link has two parents. Silent without links (nothing to compare)."""
    if not model.links:
        return []
    names = {link.name for link in model.links}
    children = [j.child for j in model.joints]
    bad = [
        j.name
        for j in model.joints
        if j.parent not in names or j.child not in names or children.count(j.child) > 1
    ]
    if scores is not None:
        scores.record("model/structure_hash")
    if not bad:
        return []
    return [
        Finding(
            severity="warning",
            quantity="model/structure_hash",
            observed=f"joint {', '.join(bad)} does not join two links of this lockfile's model",
            expected="every joint joins two links of the model, one parent per link",
            action="fix the joint's parent or child in the lockfile",
        )
    ]


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
    if c.model.source == "lockfile":  # the lock is the model: no file to drift from
        return _check_self_structure(c.model, scores)

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
                # absent, not unreadable: visible on GO (report.verdict caps only unreadable)
                expected="the URDF on disk at the provenance path",
                source=named,
                action=(
                    "download the URDF next to the lockfile to re-check it"
                    if named.lower().startswith(("http://", "https://"))
                    else "restore the URDF or correct model provenance to its on-disk path"
                ),
                next_command=cli_line("lockwire", "start") + " <urdf>",
            )
        ]

    from lockwire.mjcf import is_mjcf, read_mjcf_topology  # mjcf -> check_sim -> api -> checks

    try:
        topology = (
            _webots_topology(path)
            if path.suffix.lower() in _WEBOTS
            else read_sdf_topology(path)
            if path.suffix.lower() == ".sdf"
            else read_mjcf_topology(path)
            if is_mjcf(path)
            else read_urdf_topology(path, c.model.xacro_args)
        )
    except (OSError, ET.ParseError, ValueError, TypeError) as error:
        try:
            needs_args = "Undefined substitution argument" in str(
                error
            ) or "<xacro:arg" in path.read_text(encoding="utf-8", errors="replace")
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
        if not isinstance(error, (OSError, UnresolvedXacroError)) and (
            c.model.joints or c.model.links
        ):
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
    # A lock written before 0.5 read an <axis/> with no xyz as 0 0 1; 0.5 and later read 1 0 0.
    legacy = c.schema_version in {"0.2", "0.3", "0.4"}
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
                next_command=_relock_command(path, contract_path, c.model.xacro_args),
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
            next_command=_relock_command(path, contract_path, c.model.xacro_args),
        )
    ]


_RELOCK_ACTION = (
    "fix the model file; if the structural change is on purpose, a person reviews it and "
    "re-locks (the command below discards locked entries)"
)


def _describe_hint(contract_path: Path | str | None) -> str:
    return cli_line("lockwire", "describe", contract_path or "lockwire.yaml")


def _relock_command(
    urdf: Path | str, contract_path: Path | None, xacro_args: dict[str, str] | None = None
) -> str | None:
    if str(urdf) == "lockfile":  # a robot designed in Lockwire has no model file to re-lock
        return None
    default = (
        contract_path is None or Path(contract_path).resolve() == Path("lockwire.yaml").resolve()
    )
    out = [] if default else ["--out", contract_path]  # --out lockwire.yaml is the default
    args = [a for k, v in (xacro_args or {}).items() for a in ("--xacro-arg", f"{k}={v}")]
    return cli_line("lockwire", "start", urdf, *out, *args, "--force")


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
    path = _model_on_disk(contract_path, named)
    if not path:
        return []
    from lockwire.mjcf import is_mjcf, parse_mjcf, read_mjcf_topology  # mjcf -> check_sim -> checks

    mjcf = is_mjcf(path)
    try:
        sdf = path.suffix.lower() == ".sdf"
        wbt = path.suffix.lower() in _WEBOTS
        topology = (
            _webots_topology(path)
            if wbt
            else read_sdf_topology(path)
            if sdf
            else read_mjcf_topology(path)
            if mjcf
            else read_urdf_topology(path, c.model.xacro_args)
        )
        parsed = (
            parse_webots(path)
            if wbt
            else parse_sdf(path)
            if sdf
            else parse_mjcf(path)
            if mjcf
            else parse_urdf(path, c.model.xacro_args)
        )
        disk = {link.name: link for link in parsed.links}
    except (OSError, ET.ParseError, ValueError, TypeError):
        return []
    same = (topology.record_hash if c.model.joints else topology.topology_hash) == (
        c.model.structure_hash
    ) or (
        c.model.joints
        and locked_record_hash(c.model, topology, c.schema_version in {"0.2", "0.3", "0.4"})
        == c.model.structure_hash
    )
    if not same:
        return []

    def differs(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        return not all(math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-9) for x, y in zip(a, b))

    def show(v: tuple[float, ...]) -> str:
        return " ".join(f"{x:.9g}" for x in v)

    def tensor(i) -> tuple[float, ...]:  # MJCF: the inertial's rotation counts, axes too
        return tuple((np.array(i.axes).T @ np.diag(i.moments_kgm2) @ np.array(i.axes)).flat)

    findings = []
    if mjcf:  # never a bare GO over what an MJCF lock leaves out
        bare = [k.name for k in c.model.links if k.name != "world" and not k.mass]
        findings.append(
            Finding(
                severity="not_scored",
                quantity="model/mjcf",
                observed="an MJCF lock does not compare joint damping, armature or friction; "
                "actuators (gear, ctrlrange, forcerange); tendons; joint anchor positions (pos); "
                "geoms, sites, cameras, lights, contacts and meshes; "
                "rotations written as axisangle, xyaxes or zaxis"
                + (f"; MuJoCo computes the mass of {', '.join(bare)} from geoms" if bare else ""),
                expected="every model quantity compared",
                action="review edits to these by hand; give each body an <inertial> with a mass",
            )
        )
    for link in c.model.links:
        now = disk[link.name]
        zero = (0.0, 0.0, 0.0)
        values = {
            "mass": (
                (link.mass.base() if link.mass else 0.0,),
                (now.mass.base() if now.mass else 0.0,),
            ),
            "com": (link.com or zero, now.com or zero),
            "inertia": tuple(
                (tensor(i) if mjcf else i.moments_kgm2) if i else zero
                for i in (link.inertia, now.inertia)
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
                            f"the model file's {what} of {link.name} changed after the lock; fix the model "
                            "file; if the edit is on purpose, a person reviews it and re-locks "
                            "(the command below discards locked entries)"
                        ),
                        next_command=_relock_command(path, contract_path, c.model.xacro_args),
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
    path = named and _model_on_disk(contract_path, named)
    try:
        if not path:
            now = []
        elif path.suffix.lower() == ".sdf":  # includes spliced in, as the lock read it
            now = mesh_refs(read_sdf_root(path)[0])
        else:
            now = mesh_refs(ET.fromstring(read_model_bytes(path, c.model.xacro_args)))
    except (OSError, ET.ParseError, ValueError, TypeError):
        return []  # the drift check already says why the model can't be read
    locked = c.model.mesh_refs if c.model else None
    if not path or (not now and not locked):
        return []
    relock = _relock_command(path, contract_path, c.model.xacro_args)

    def finding(
        severity: str,
        quantity: str,
        observed: str,
        expected: str,
        action: str = "a mesh changed after the lock; fix the mesh file; if the edit is on purpose, a person "
        "reviews it and re-locks (the command below discards locked entries)",
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
                "a person can re-lock to record them (the command below discards locked entries)",
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
_SHEET_UNITS |= {"deg/s": math.pi / 180, "rpm": math.pi / 30, "N*m": 1.0}
_SCOPE_WORDS = {
    "with_battery": "includes the battery, computer and sensors",
    "with_cable": "includes the cable",
    "with_gripper": "includes the gripper",
    "arm_only": "is the arm alone, no controller or tool",
    "dry": "is dry, no battery or fluids",
}


def _model_file_links(c: Contract, contract_path: Path | None) -> list:
    """The links parsed from the model file on disk now; empty when it is missing or unreadable."""
    named = _urdf_path_candidate(c.model)
    if named is None:
        return []
    path = _model_on_disk(contract_path, named)
    if not path:
        return []
    try:
        if path.suffix.lower() in _WEBOTS:
            return list(parse_webots(path).links)
        if path.suffix.lower() == ".sdf":
            return list(parse_sdf(path).links)
        from lockwire.mjcf import is_mjcf, parse_mjcf  # mjcf -> check_sim -> checks

        if is_mjcf(path):
            return list(parse_mjcf(path).links)
        return list(parse_urdf(path, c.model.xacro_args).links)
    except (OSError, ET.ParseError, ValueError, TypeError):
        return []


_TORQUE_TOL = 0.10  # the catalog's joint_torque_max and `check --torque` share it


def check_torque_claims(
    c: Contract, claims: dict[str, float], source: str, scores: _ScoreBook
) -> list[Finding]:
    """Each joint's URDF effort limit vs the published torque (N*m) the user typed."""
    by_name = {j.name: j for j in (c.model.joints if c.model else [])}
    found = []
    for name, sheet in claims.items():
        limit = by_name[name].limits
        quantity = f"datasheet/{name}/joint_torque"
        expected = f"published {sheet:g} N*m ({source})"
        if not (limit and limit.effort_limit):
            found.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed="no effort limit in the model",
                    expected=expected,
                    action="add the joint's effort limit to the URDF to compare it",
                )
            )
            continue
        urdf = limit.effort_limit
        delta = (urdf - sheet) / sheet
        scores.record(quantity)
        if abs(delta) <= _TORQUE_TOL + 1e-9:
            scores.passed(quantity, f"URDF effort: {urdf:.4g}", expected, f"{delta:+.1%}")
            continue
        found.append(
            Finding(
                severity="warning",
                quantity=quantity,
                observed=f"URDF effort: {urdf:.4g}",
                expected=expected,
                delta=f"{delta:+.1%}",
                source=source,
                action="check the URDF effort limit against the published figure",
            )
        )
    return found


def _file_joint_limits(
    path: Path, xacro_args: dict[str, str] | None
) -> dict[str, dict[str, float]]:
    """Joint name -> {"effort": .., "velocity": ..} a model file declares; {} when unreadable."""
    from lockwire.check_sim import read_sim

    try:
        if path.suffix.lower() in (".urdf", ".xacro"):
            raise ValueError  # URDF: read_sim has no reader for it
        _bodies, joints, _fmt = read_sim(path)
        return {
            n: {k: v for k, v in (("effort", j.effort), ("velocity", j.velocity)) if v}
            for n, j in joints.items()
        }
    except (OSError, ET.ParseError, ValueError, TypeError):
        pass
    try:
        return {
            j.name: {
                k: v
                for k, v in (("effort", j.limits.effort_limit), ("velocity", j.limits.velocity))
                if v
            }
            for j in parse_urdf(path, xacro_args).joints
            if j.limits
        }
    except (OSError, ET.ParseError, ValueError, TypeError):
        return {}


def check_cross_file_agreement(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Warns when the same joint limit is written differently in two files the lock names.

    Files: the model source and its provenance source, plus urdf/sdf artifacts of the sim
    inputs. A joint in one file only, or a file that is missing, says nothing.
    """
    if c.model is None or contract_path is None:
        return []
    named = {c.model.provenance.source, c.model.source}
    sim = c.simulation
    refs = [i.artifact.ref.path for i in [*sim.worlds, *sim.plants]] if sim else []
    names = [n for n in (*named, *refs) if n.lower().endswith(_MODEL_SUFFIXES)]
    paths = []
    for n in sorted(names):  # sorted: the same lock reports the same way every run
        p = _model_on_disk(contract_path, n)
        if p is not None and p.is_file() and p not in paths:
            paths.append(p)
    limits = [(p.name, _file_joint_limits(p, c.model.xacro_args)) for p in paths]
    kinds = {j.name: j.type for j in c.model.joints}
    unit = {"effort": "N*m", "velocity": "rad/s"}
    out = []
    for joint in dict.fromkeys(n for _f, lim in limits for n in lim):
        for field in ("effort", "velocity"):
            seen = [(f, lim[joint][field]) for f, lim in limits if field in lim.get(joint, {})]
            if len(seen) < 2 or all(math.isclose(v, seen[0][1], rel_tol=1e-6) for _f, v in seen):
                continue
            u = unit[field]
            if kinds.get(joint) == "prismatic":
                u = "N" if field == "effort" else "m/s"
            values = ", ".join(f"{f} {float(v)!r} {u}" for f, v in seen)
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/joints/{joint}/{field}",
                    observed=f"{joint} {field} differs: {values}",
                    expected=f"one {field} for {joint} in every file the lock names",
                    action=f"make the {field} of {joint} the same in these files",
                )
            )
    return out


def check_datasheet_agreement(
    c: Contract,
    *,
    scores: _ScoreBook | None = None,
    model: str | None = None,
    contract_path: Path | None = None,
) -> list[Finding]:
    """A recognised robot's URDF-derived numbers vs its vendor datasheet (catalog_robots.yaml).

    Mass +-5 % and dof exact are violations; payload and joint speed (the URDF allowing more
    than the sheet) and largest joint torque (+-10 %) are warnings; reach (+-3 %) is passed when it agrees, else a not_scored note. A mass_total budget (`--mass`) wins over the
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
    mass_sum = sum(link.mass.base() for link in c.model.links if link.mass)
    if live := _model_file_links(c, contract_path):  # the file as it is now, not as it was locked
        mass_sum = sum(link.mass.base() for link in live if link.mass)
    speeds = [
        j.limits.velocity
        for j in moving
        if j.type != "prismatic" and j.limits and j.limits.velocity
    ]
    efforts = [
        j.limits.effort_limit
        for j in joints
        if j.type in ("revolute", "continuous") and j.limits and j.limits.effort_limit
    ]
    derived = {
        "mass_total": (
            mass_sum,
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
        "joint_torque_max": (
            max(efforts, default=None),
            _TORQUE_TOL,
            "largest revolute joint effort, N*m",
        ),
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
                "check the URDF counts what the sheet counts; if the sheet is right, fix the "
                f"URDF; to compare with another figure, run {cli_line('lockwire', 'check')} "
                "<robot.urdf> --mass"
            ),
            next_command=None if key in who else rerun,  # never swap a typed mass for the sheet's
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
    named = next((n for n in names if n.lower().endswith(_MODEL_SUFFIXES)), None)
    if named is None:
        return None
    path = _model_on_disk(contract_path, named)
    if not path:
        return None
    from lockwire.mjcf import (
        is_mjcf,
        urdf_view as mjcf_urdf_view,
    )  # mjcf -> check_sim -> api -> checks

    if path.suffix.lower() in _WEBOTS:
        try:
            return path, _webots_view(path)
        except (OSError, ValueError):
            return None
    if path.suffix.lower() == ".sdf" or is_mjcf(path):
        try:
            return path, (urdf_view if path.suffix.lower() == ".sdf" else mjcf_urdf_view)(path)
        except (OSError, ET.ParseError, ValueError):
            return None
    try:
        raw = read_model_bytes(path, c.model.xacro_args)  # a .xacro is expanded here
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
    if (
        c.model is None
        or c.model.source == "lockfile"
        or any(n.lower().endswith(_MODEL_SUFFIXES) for n in names)
    ):
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
    fix = _relock_command(path, contract_path, c.model.xacro_args)
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
                    f"joint {name} has lower {lo[0]:g} {'>' if lo[0] > hi[0] else '>='} "
                    f"upper {hi[0]:g}; locked as unlimited",
                    "lower < upper",
                    f"swap or fix {name}'s limits",
                    severity="violation" if lo[0] > hi[0] else "warning",
                )
            elif lo and hi:
                limits[name] = (lo[0], hi[0])
            # urdfdom joint.cpp: revolute/prismatic need <limit>, and a <limit> needs velocity
            # (https://raw.githubusercontent.com/ros/urdfdom/master/urdf_parser/src/joint.cpp).
            mimic = el.find("mimic")  # a follower takes its target's motion (urdf.py:371)
            if mimic is not None and mimic.get("joint") in {j.get("name") for j in joints}:
                pass
            elif lim is None or lim.get("velocity") is None:
                warn(
                    f"{name}/velocity",
                    f"moving joint {name} has no velocity limit",
                    "a <limit velocity=...> (ROS urdfdom refuses the file without it)",
                    f"add {name}'s velocity limit from its datasheet",
                )
            elif _floats(lim.get("velocity"))[:1] == [0.0]:
                warn(
                    f"{name}/velocity",
                    f"moving {jtype} joint {name} has velocity limit 0: it may not move at all",
                    "a positive <limit velocity=...> (0 is an exporter placeholder)",
                    f"set {name}'s velocity limit from its datasheet",
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
    # a continuous joint between limited ones: climb to a limited revolute, descend to a mover
    ends = [(j, j.find("parent"), j.find("child")) for j in joints]
    ends = [
        (j, a.get("link"), b.get("link")) for j, a, b in ends if a is not None and b is not None
    ]
    above = {b: (j, a) for j, a, b in ends}  # child link -> its joint and parent link

    def _moves_below(link: str, seen: set[str]) -> bool:
        seen.add(link)
        return any(
            j.get("type", "fixed") != "fixed" or (b not in seen and _moves_below(b, seen))
            for j, a, b in ends
            if a == link
        )

    for el, top, bottom in ends:
        if el.get("type") != "continuous" or not _moves_below(bottom, set()):
            continue
        link, seen, limited = top, set(), False
        while link in above and link not in seen and not limited:
            seen.add(link)
            j, link = above[link]
            limited = j.get("type") == "revolute" and j.get("name") in limits
        if limited:
            name = el.get("name", "?")
            warn(
                f"{name}/continuous",
                f"joint {name} is continuous (no position limit) inside a chain of limited joints",
                "a position limit like its neighbours (or a deliberate continuous joint)",
                f"give {name} lower/upper limits, or keep it if the maker meant it",
            )
    two_parents = sorted(link for link, js in parents.items() if len(js) > 1)
    # urdfdom initTree writes parent_link_tree[child] with no check for an existing parent,
    # so ROS silently keeps one of two parent joints
    # (https://raw.githubusercontent.com/ros/urdfdom_headers/master/include/urdf_model/model.h).
    # SDF may close a loop (a five-bar leg), which URDF cannot say
    # (https://classic.gazebosim.org/tutorials?tut=ros_urdf): there it is no fault, and
    # check_gravity_reach says what it could and could not score through it.
    if two_parents and path.suffix.lower() != ".sdf":
        warn(
            "loops",
            "links with more than one parent joint (a kinematic loop): " + ", ".join(two_parents),
            "a tree: one parent joint per link",
            "give each link one parent joint",
            severity="violation",
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
        # A body with mass spreads it over a volume, so its moments cannot all be zero. ponytail:
        # 1 mg floor, below it a zero tensor is a massless-frame stand-in (SO-101's 1e-9 kg frame).
        heavy = link.mass is not None and link.mass.base() >= 1e-6
        bad = (
            "a principal moment is negative"
            if a < -1e-12
            else f"all moments are zero on a {link.mass.base():g} kg link"
            if heavy and big == 0
            else "the largest moment exceeds the sum of the other two"
            if big > (a + b) * (1 + 1e-6)
            else None
        )
        if bad:
            out.append(
                Finding(
                    severity="violation" if a < -1e-12 or big == 0 else "warning",
                    quantity=f"model/links/{link.name}/inertia",
                    observed=f"principal moments {a:.4g}, {b:.4g}, {big:.4g} kg*m^2: {bad}",
                    expected="non-negative moments with the largest <= the sum of the other two",
                    action=(f"no real body has this inertia; recompute {link.name}'s from CAD"),
                    next_command=_relock_command(
                        (_urdf_path_candidate(c.model) or c.model.source),
                        contract_path,
                        c.model.xacro_args,
                    ),
                )
            )
    return out


def check_duplicate_inertial(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A link carrying the same inertial as the link it is fixed to double-counts its mass."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return []
    path, root = found
    key = {}
    for link in root.findall("link"):
        i = link.find("inertial")
        if i is None:
            continue
        mass, org, ten = i.find("mass"), i.find("origin"), i.find("inertia")
        key[link.get("name")] = [
            *_floats(mass.get("value") if mass is not None else None),
            *_floats(org.get("xyz") if org is not None else None),
            *_floats(org.get("rpy") if org is not None else None),
            *(float((ten.get(k) if ten is not None else None) or 0) for k in _INERTIA_KEYS),
        ]
    out = []
    for j in root.findall("joint"):
        p_, c_ = j.find("parent"), j.find("child")
        a, b = (key.get(e.get("link")) if e is not None else None for e in (p_, c_))
        if j.get("type") != "fixed" or not a or a[0] <= 0 or len(a) != len(b or ()):
            continue
        if all(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12) for x, y in zip(a, b)):
            child, parent = c_.get("link"), p_.get("link")
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/links/{child}/duplicate_inertial",
                    observed=f"{child} has the same inertial as {parent}, fixed to it: "
                    f"{a[0]:g} kg counted twice",
                    expected="one inertial per rigid body",
                    source=str(path),
                    action=f"remove the inertial from {child} (or give it its own), then re-lock",
                    next_command=_relock_command(path, contract_path, c.model.xacro_args),
                )
            )
    return out


def check_sibling_effort(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Consecutive moving joints of one chain named alike whose effort differs 20x: a note."""
    if c.model is None:
        return []
    # ponytail: 20x keeps G1 knee 139 vs ankle 50 quiet; only joints sharing a first name token
    # and chained child->parent are compared, so shoulder-to-wrist taper is never flagged.
    stem = lambda j: re.sub(r"_joint$", "", j.name).split("_")[0]  # noqa: E731
    by_parent = {j.parent: j for j in c.model.joints if j.limits and j.limits.effort_limit}
    out = []
    for a in c.model.joints:
        b = by_parent.get(a.child)
        if b is None or not (a.limits and a.limits.effort_limit) or stem(a) != stem(b):
            continue
        lo, hi = sorted((a, b), key=lambda j: j.limits.effort_limit)
        if hi.limits.effort_limit >= 20 * lo.limits.effort_limit:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=f"model/sibling/{lo.name}",
                    observed=f"{lo.name} effort {lo.limits.effort_limit:g} vs {hi.name} "
                    f"{hi.limits.effort_limit:g} in one chain",
                    expected="neighbouring joints of one limb within 20x of each other",
                    action=f"check {lo.name}'s effort against the datasheet; it may be a typo",
                )
            )
    return out


SIDE = r"(?:^|_)(left|right|l|r|fl|fr|rl|rr)(?=_|$)"  # a left/right name token, shared with diff
_OTHER_SIDE = {"left": "right", "l": "r", "fl": "fr", "rl": "rr"}


def _mirror_values(x: Any) -> dict[str, float]:
    if hasattr(x, "limits"):
        lim = x.limits
        return {"effort": lim and lim.effort or 0, "velocity": lim and lim.velocity or 0}
    moments = x.inertia.moments_kgm2 if x.inertia else (0, 0, 0)
    return {
        "mass": x.mass.base() if x.mass else 0,
        **{f"moment {i}": m for i, m in enumerate(moments, 1)},
    }


def check_mirror(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Joints and links named as left/right twins must agree on effort, velocity, mass, inertia."""
    if c.model is None:
        return []
    # ponytail: 10% passes rounding; real asymmetries (B2 hip ixx 0.0033 vs 0.0026) are 25%.
    off = lambda x, y: abs(x - y) > 0.1 * max(x, y) and max(x, y) > 1e-6  # noqa: E731
    out: list[Finding] = []
    for items in (c.model.joints, c.model.links):
        table = {}
        for i in items:
            t = re.search(SIDE, i.name.lower())
            if t:
                table[re.sub(SIDE, "_~", i.name.lower()), t.group(1)] = i
        for (key, tok), a in table.items():
            b = table.get((key, _OTHER_SIDE.get(tok, "")))
            if b is None or (hasattr(a, "limits") and not (a.limits and b.limits)):
                continue  # a mimic follower or fixed joint has no limits of its own
            va, vb = _mirror_values(a), _mirror_values(b)
            bad = [f"{k} {va[k]:g} vs {vb[k]:g}" for k in va if off(va[k], vb[k])]
            if bad:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"model/mirror/{a.name}",
                        observed=f"{a.name} and {b.name} are left/right twins but differ: "
                        + "; ".join(bad),
                        expected="mirrored sides carry the same values (within 10%)",
                        action=f"check {a.name} and {b.name} against the datasheet or CAD",
                        next_command=_relock_command(
                            (_urdf_path_candidate(c.model) or c.model.source),
                            contract_path,
                            c.model.xacro_args,
                        ),
                    )
                )
    if len(out) > 5:  # one count instead of a wall of pairs
        out[5:] = [
            out[5].model_copy(
                update={
                    "quantity": "model/mirror/more",
                    "observed": f"{len(out) - 5} more left/right pairs differ",
                    "action": "fix the pairs above, then re-run",
                }
            )
        ]
    return out


_INERTIA_KEYS = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")


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
    fix = _relock_command(path, contract_path, c.model.xacro_args)
    shapes = _link_primitives(root)
    out = []
    for link in c.model.links:
        if link.mass is None or link.mass.base() <= 0:
            continue
        mass, com = link.mass.base(), link.com or (0.0, 0.0, 0.0)
        problems = []
        sets = shapes.get(link.name)
        if sets:
            prims = [p for shapes_ in sets for p in shapes_]  # collision and visual together
            volumes = [sum(p[2] for p in shapes_) for shapes_ in sets]
            # Fetched https://en.wikipedia.org/wiki/Density 2026-11-04: osmium, "the densest known
            # element", 22,570 kg/m^3; aerographite, the lightest solid in its table, 0.2 kg/m^3.
            # Outside that, units slipped. The roomier set judges "too dense", the tighter "too light".
            if min(volumes) > 0 and not (
                mass / max(volumes) <= 22570 and mass / min(volumes) >= 0.2
            ):
                density = (
                    mass / max(volumes) if mass / max(volumes) > 22570 else mass / min(volumes)
                )
                problems.append(
                    f"density {density:.4g} kg/m^3 from {mass:g} kg (g vs kg or mm vs m?)"
                )
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
            if link.inertia and math.sqrt(max(link.inertia.moments_kgm2) / mass) > extent * (
                1 + 1e-6
            ):
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
        # No shape needed (mesh links too): at most 22,570 kg/m^3 (osmium, above), the least
        # spread-out body of this mass is a solid ball of radius r, and I1 + I2 + I3 =
        # 2 * sum(r^2 dm) >= 1.2 m r^2. Zero inertia is judged by check_inertia_valid
        # instead, so it is skipped; so is a link the shape checks above already flagged.
        trace = sum(link.inertia.moments_kgm2) if link.inertia else 0.0
        floor = 1.2 * mass * (3 * mass / (4 * math.pi * 22570)) ** (2 / 3)
        if not problems and 0 < trace < floor * (1 - 1e-6):
            problems.append(
                f"inertia {trace:.4g} kg*m^2 (sum of principal moments) is below {floor:.4g}, the "
                f"least any {mass:g} kg solid can have: the mass or the inertia is wrong"
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
                (_urdf_path_candidate(c.model) or c.model.source),
                contract_path,
                c.model.xacro_args,
            ),
        )
    ]


def check_limit_unit_traps(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Degrees typed as radians, and joint numbers too large for a robot (mm typed as m)."""
    if c.model is None:
        return []
    fix = _relock_command(
        (_urdf_path_candidate(c.model) or c.model.source), contract_path, c.model.xacro_args
    )
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


def check_datasheet_limit(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A revolute joint's limit span vs the travel its linked catalog servo's datasheet gives
    (catalog number `rotation_range`, an angle in deg or rad). No servo, no cited range, no
    both limits: nothing."""
    if c.model is None or not c.servo:
        return []
    ranges = {
        p.id: p.numbers["rotation_range"]
        for p in load_catalog().parts
        if "rotation_range" in p.numbers
        and p.numbers["rotation_range"].kind == "datasheet"
        and units.known(p.numbers["rotation_range"].unit)
        and units.dimension(p.numbers["rotation_range"].unit) == "angle"
    }
    out = []
    for j in c.model.joints:
        sid, lim = servo_for(c.servo, j.name), j.limits
        if j.type != "revolute" or sid not in ranges or not lim:
            continue
        if lim.lower is None or lim.upper is None:
            continue
        rng = ranges[sid]
        travel = units.to_base(rng.value, rng.unit)  # radians, whatever the catalog wrote
        span = lim.upper - lim.lower
        if span <= travel * (1 + 1e-6):
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"model/joints/{j.name}/limits/datasheet_range",
                observed=f"limit span {math.degrees(span):.0f} deg",
                expected=f"<= {math.degrees(travel):.0f} deg, the {sid} datasheet rotation range",
                source=rng.source,
                action=(
                    f"narrow {j.name}'s limits to the servo's {math.degrees(travel):.0f} deg, "
                    "or drive it with a servo that turns further, then re-lock"
                ),
            )
        )
    return out


def check_collision_visual(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A link whose collision primitives' bounding box is far from its visual ones'. Only
    primitive shapes are read (`_link_primitives` leaves mesh links out); both needed."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return []
    out = []
    for name, sets in _link_primitives(found[1]).items():
        if len(sets) != 2:  # collision then visual: both present
            continue
        sizes = []
        for shapes in sets:
            lo = [min(o[i] - r for o, r, *_ in shapes) for i in range(3)]
            hi = [max(o[i] + r for o, r, *_ in shapes) for i in range(3)]
            sizes.append(math.dist(lo, hi))
        # ponytail: 2x between the bounding-sphere boxes' diagonals is our own number, no source.
        if min(sizes) > 0 and max(sizes) / min(sizes) > 2:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"model/links/{name}/collision_visual",
                    observed=f"collision box diagonal {sizes[0]:.3g} m vs visual {sizes[1]:.3g} m",
                    expected="no cited threshold",
                    source=str(found[0]),
                    action=f"check {name}'s collision shape matches the part it stands for",
                    advice_unsourced=True,
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
        data = YAML(typ="safe").load(yaml_path.read_text(encoding="utf-8"))
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
        nominal = battery.voltage.base() if battery.voltage.base() > 0 else None
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
    if battery.voltage is not None and nominal is None:
        out.append(
            _not_scored(
                "topology/battery/voltage",
                f"battery voltage is {battery.voltage.base():g} V; pack checks not compared",
                "the pack's nominal voltage in volts, above 0",
                _declare(contract_path, "voltage", "11.1"),
            )
        )
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
    if battery.mass.base() <= 0:
        return [
            _not_scored(
                "topology/battery/mass",
                f"battery mass is {battery.mass.base():g} kg; energy density not compared",
                "the pack's mass in kg, above 0",
                _declare(contract_path, "mass", "0.143"),
            )
        ]
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


# --- Check Pack 3: failures outside physics (charging, connectors, chemistry, firmware, law) ---


def _pack3(
    severity: str, quantity: str, observed: str, expected: str, action: str, source: str, path: Any
) -> Finding:
    return Finding(
        severity=severity,
        quantity=quantity,
        observed=observed,
        expected=expected,
        action=action,
        source=source,
        next_command=_describe_hint(path),
    )


_BU = "https://batteryuniversity.com/article/"


def check_charger(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Declared charger setpoint, float mode and current against the pack's chemistry."""
    pack = _pack(c)
    if pack is None:
        return []
    battery, row, nominal = pack
    lithium = battery.chemistry in ("li-ion", "lipo", "lifepo4")
    out: list[Finding] = []
    if battery.charger_voltage is not None and battery.cells_series and row[1] is not None:
        # 0.05 V: Samsung SDI INR18650-30Q spec "CCCV, 1.50A, 4.20 +/- 0.05 V" (catalog_seed.yaml,
        # cell.samsung.inr18650-30q charge_cutoff_voltage); LFP has no such tolerance on record.
        tolerance = 0.05 if battery.chemistry in ("li-ion", "lipo") else 0.0
        limit = battery.cells_series * (row[1] + tolerance)
        volts = battery.charger_voltage.base()
        if volts > limit + 1e-9:
            out.append(
                _pack3(
                    "violation",
                    "topology/battery/charger_voltage",
                    f"charger set to {volts:g} V",
                    f"<= {limit:g} V ({battery.cells_series} cells x {row[1] + tolerance:g} V)",
                    "overcharging these cells can start a fire; lower the charger's voltage",
                    _BU + "bu-409-charging-lithium-ion",
                    contract_path,
                )
            )
    if battery.charger_float and lithium:
        out.append(
            _pack3(
                "violation",
                "topology/battery/charger_float",
                f"float charge on a {battery.chemistry} pack",
                "a charger that stops when the pack is full",
                "lithium cells cannot take a float charge: it plates lithium and is unsafe",
                _BU + "bu-409-charging-lithium-ion",
                contract_path,
            )
        )
    if battery.charger_current is not None and lithium and nominal:
        amp_hours = battery.capacity.base() / nominal
        amps = battery.charger_current.base()
        if amps > amp_hours * (1 + 1e-9):  # exactly 1C must not warn on float rounding
            out.append(
                _pack3(
                    "warning",
                    "topology/battery/charger_current",
                    f"charging at {amps:.3g} A ({amps / amp_hours:.2g}C)",
                    f"<= {amp_hours:.3g} A (1C), unless the cell datasheet allows more",
                    "fast charging wears the pack and heats it; lower the charge current",
                    _BU + "bu-808-how-to-prolong-lithium-based-batteries",
                    contract_path,
                )
            )
    return out


def check_battery_cutoff(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Declared low-voltage cutoff against the chemistry's lowest safe cell voltage."""
    pack = _pack(c)
    if pack is None or pack[0].cutoff_voltage is None or not pack[0].cells_series:
        return []
    battery, row, _ = pack
    per_cell = battery.cutoff_voltage.base() / battery.cells_series
    if per_cell >= row[2]:
        return []
    return [
        _pack3(
            "warning",
            "topology/battery/cutoff_voltage",
            f"cutoff at {per_cell:.3g} V per cell",
            f">= {row[2]:g} V per cell for {battery.chemistry}",
            "the pack is drained past its safe minimum before the cutoff acts; raise the cutoff",
            "chemistry cell minimum (same table as the cell-window check)",
            contract_path,
        )
    ]


# Maker-rated continuous current. XT60: Amass XT60 spec V1.2 (rated 30 A, 60 A instantaneous).
# JST XH: JST eXH series sheet (3 A). ponytail: two connectors read at source; add a row per
# connector as its maker sheet is read.
_CONNECTOR_A = {"xt60": 30.0, "jst-xh": 3.0}


def check_connector_rating(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Worst rail current through a wire against its connector's maker-rated current."""
    out: list[Finding] = []
    for wire in c.topology.wires if c.topology else []:
        rail = c.rail(wire.rail)
        if wire.connector is None or rail is None or rail.voltage is None:
            continue  # check_ampacity already reports a missing rail or voltage
        load_w, mode, _, unknown = _worst_rail_power(c, wire.rail)
        volts = rail.voltage.base()
        if unknown or volts <= 0.0:
            continue  # likewise reported by check_ampacity
        amps, rated = load_w / volts, _CONNECTOR_A[wire.connector]
        if amps > rated:
            out.append(
                _pack3(
                    "violation",
                    f"topology/wires/{wire.id}/connector",
                    f"{amps:.3g} A through the {wire.connector} connector ({mode})",
                    f"<= {rated:g} A (maker's rated current)",
                    "the connector overheats at this current; use a larger connector",
                    "Amass XT60 spec V1.2; JST XH series sheet",
                    contract_path,
                )
            )
    return out


def check_threadlocker_on_print(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Anaerobic threadlocker declared on a 3D-printed (thermoplastic) part."""
    return [
        _pack3(
            "warning",
            f"printed/{name}/threadlocker",
            f"threadlocker on printed {link.material} part {name}",
            "no threadlocker touching the plastic",
            "threadlocker can stress-crack plastic; use a plastic-safe grade or lock nuts",
            "Henkel Loctite 243 technical data sheet",
            contract_path,
        )
        for name, link in (c.printed or {}).items()
        if link.threadlocker
    ]


# Printer name (lower case) -> (bed X, Y, Z in mm, source). Only a bed READ on the maker's own
# page 2026-11-13 is listed; Bambu Lab (403/402 to our fetch) and Creality pages could not be read.
_BEDS = {
    "prusa mk4s": (
        (250.0, 210.0, 220.0),
        "Prusa Research product page, build volume 250 x 210 x 220 mm: "
        "https://www.prusa3d.com/product/original-prusa-mk4s-3d-printer-5/",
    ),
}


def _single_primitive_dims_mm(root: ET.Element, name: str) -> list[float] | None:
    """Sorted extents (mm) of a link whose geometry is one box/cylinder/sphere, else None.
    Meshes, several shapes, and tilts that are not multiples of 90 degrees are not sized."""
    link = next((el for el in root.findall("link") if el.get("name") == name), None)
    if link is None:
        return None
    for kind in ("collision", "visual"):
        els = link.findall(kind)
        if not els:
            continue
        geometry = els[0].find("geometry")
        shape = geometry[0] if geometry is not None and len(geometry) else None
        if len(els) != 1 or shape is None:
            return None
        origin = els[0].find("origin")
        rpy = _floats(origin.get("rpy")) if origin is not None else []
        if any(abs(a / (math.pi / 2) - round(a / (math.pi / 2))) > 1e-6 for a in rpy):
            return None
        size = _floats(shape.get("size"))
        r, length = ((_floats(shape.get(k)) or [0.0])[0] for k in ("radius", "length"))
        if shape.tag == "box" and len(size) == 3:
            dims = size
        elif shape.tag == "cylinder" and r > 0 and length > 0:
            dims = [2 * r, 2 * r, length]
        elif shape.tag == "sphere" and r > 0:
            dims = [2 * r] * 3
        else:
            return None
        return sorted(d * 1000 for d in dims)
    return None


def check_print_bed_fit(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A printed part longer than its printer's bed on any axis (one finding per part).

    Reads `printed:` links that name a `printer` (a name in the bed table) and the part's size
    from its single URDF primitive. Sorted dims are compared with sorted bed dims, so turning
    the part is allowed but never its diagonal. No printer, a printer not in the table (its bed is not on record: nothing is emitted, because
    not_scored would trip --strict and the advice flag does not fit), mesh: nothing.
    """
    found = _urdf_root(c, contract_path) if c.printed else None
    if found is None:
        return []
    out = []
    for name, link in c.printed.items():
        bed = _BEDS.get((link.printer or "").lower())
        dims = _single_primitive_dims_mm(found[1], name) if bed else None
        if bed is None or dims is None:
            continue
        bed_dims = sorted(bed[0])
        if any(d > b + 1e-6 for d, b in zip(dims, bed_dims, strict=True)):
            out.append(
                _pack3(
                    "warning",
                    f"printed/{name}/bed_fit",
                    f"part {' x '.join(f'{d:g}' for d in dims)} mm",
                    f"each side within the bed, {' x '.join(f'{b:g}' for b in bed_dims)} mm",
                    "split the part, or print it on a larger bed",
                    bed[1],
                    contract_path,
                )
            )
    return out


def belt_lengths(c: Contract) -> list[tuple[str, float, int]]:
    """(belt id, computed length m, computed teeth) per belt with a known profile.

    Open-belt geometry, two pulleys: pitch diameter d = teeth * pitch / pi and
    L = 2C + pi (D + d) / 2 + (D - d)^2 / (4 C) (standard open-belt length, e.g. Shigley's
    Mechanical Engineering Design, flat-belt drives). Teeth = L / pitch to the nearest tooth.
    """
    out = []
    for belt in (c.topology.belts if c.topology else None) or []:
        row = belt_pitch(belt.profile)
        if row is None:
            continue
        d, big = sorted(
            t * row.pitch / math.pi for t in (belt.driver.pulley_teeth, belt.driven.pulley_teeth)
        )
        cd = belt.center_distance_m
        length = 2 * cd + math.pi * (big + d) / 2 + (big - d) ** 2 / (4 * cd)
        out.append((belt.id, length, round(length / row.pitch)))
    return out


def check_belt_length(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A belt's declared teeth or length that disagrees with its pulleys and centre distance.

    Teeth must equal the computed length over the pitch, rounded to a whole tooth; a declared
    length may differ by half a pitch (the rounding of a real belt). Unknown profile, no belts:
    nothing is emitted.
    """
    out = []
    belts = {b.id: b for b in (c.topology.belts if c.topology else None) or []}
    for bid, length, teeth in belt_lengths(c):
        belt, row = belts[bid], belt_pitch(belts[bid].profile)
        if belt.teeth is not None and belt.teeth != teeth:
            observed, declared = f"{belt.teeth} teeth", f"{teeth} teeth"
        elif belt.length_m is not None and abs(belt.length_m - length) > row.pitch / 2:
            observed, declared = f"{belt.length_m * 1000:g} mm", f"{length * 1000:g} mm"
        else:
            continue
        out.append(
            _pack3(
                "warning",
                f"belts/{bid}/length",
                f"belt declares {observed}",
                f"{declared}, from the pulleys and centre distance ({row.profile}, {row.pitch * 1000:g} mm pitch)",
                "fix the declared belt size, or the pulley teeth or centre distance",
                row.source,
                contract_path,
            )
        )
    return out


_ARDU = "https://ardupilot.org/copter/docs/"
_PX4 = "https://docs.px4.io/main/en/advanced_config/parameter_reference.html"
# (firmware, parameter) -> (severity, what a value of 0 means, source)
_FAILSAFE_OFF = {
    ("ardupilot", "FS_THR_ENABLE"): (
        "violation",
        "the radio failsafe is switched off",
        _ARDU + "radio-failsafe.html",
    ),
    ("ardupilot", "BATT_FS_LOW_ACT"): (
        "warning",
        "a low battery triggers no action",
        _ARDU + "failsafe-battery.html",
    ),
    ("px4", "COM_LOW_BAT_ACT"): ("warning", "a low battery only warns", _PX4),
}


def check_autopilot_failsafe(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Declared flight-controller parameters that leave a failsafe off or set below the pack."""
    if c.autopilot is None:
        return []
    params, out = c.autopilot.params, []
    for (firmware, name), (severity, meaning, source) in _FAILSAFE_OFF.items():
        if firmware == c.autopilot.firmware and params.get(name) == 0:
            out.append(
                _pack3(
                    severity,
                    f"autopilot/params/{name}",
                    f"{name} = 0: {meaning}",
                    "a failsafe that lands or returns the vehicle",
                    f"set {name} to an action; with 0 the vehicle keeps going",
                    source,
                    contract_path,
                )
            )
    low_v = params.get("BATT_LOW_VOLT") if c.autopilot.firmware == "ardupilot" else None
    pack = _pack(c)
    floor = pack[0].cells_series * pack[1][2] if pack and pack[0].cells_series else None
    if low_v == 0 and not params.get("BATT_LOW_MAH"):
        observed, expected = "BATT_LOW_VOLT = 0: battery failsafe is switched off", "> 0 V"
    elif low_v and floor and low_v < floor:
        observed = f"BATT_LOW_VOLT = {low_v:g} V, below the pack's safe minimum"
        expected = f">= {floor:g} V ({pack[0].cells_series} cells x {pack[1][2]:g} V)"
    else:
        return out
    return [
        *out,
        _pack3(
            "warning",
            "autopilot/params/BATT_LOW_VOLT",
            observed,
            expected,
            "set the low-battery voltage above the pack's minimum so the failsafe acts in time",
            _ARDU + "failsafe-battery.html",
            contract_path,
        ),
    ]


def check_radio_legal_power(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """2.4 GHz transmit power against the declared region's licence-free limit.

    ponytail: 2.4 GHz only and cable loss is not subtracted (so it can over-warn by the
    feedline loss); other bands join as their rule text is read at source.
    """
    out: list[Finding] = []
    for device in c.devices if c.robot.region else []:
        budget, model = device.link_budget, c.device_model(device.model)
        if budget is None or model is None or model.radio is None or budget.tx_antenna is None:
            continue  # check_link_budget already reports the gap
        hz = budget.carrier_frequency.base() if budget.carrier_frequency else 0.0
        if not 2.4e9 <= hz <= 2.4835e9:
            continue
        tx, gain = model.radio.tx_power.base(), budget.tx_antenna.gain.base()
        eirp = tx + gain
        if c.robot.region == "eu":  # ETSI EN 300 328 V2.2.2 cl. 4.3.2.2.3
            over, limit, rule = eirp > 20, "20 dBm radiated", "ETSI EN 300 328"
        elif c.robot.region == "us":  # 47 CFR 15.247(b)(3) and (b)(4)
            cap = 30 - max(0.0, gain - 6)
            over, limit, rule = tx > cap, f"{cap:g} dBm at the antenna port", "47 CFR 15.247"
        else:  # India, G.S.R. 45(E) 2005: 1 W output, 4 W radiated
            over = tx > 30 or eirp > 36
            limit, rule = "30 dBm output and 36 dBm radiated", "India G.S.R. 45(E)"
        if over:
            out.append(
                _pack3(
                    "warning",
                    f"devices/{device.id}/link_budget/tx_power",
                    f"{tx:g} dBm into a {gain:g} dBi antenna ({eirp:g} dBm radiated)",
                    f"<= {limit} in region {c.robot.region}",
                    "over the licence-free limit; lower transmit power or antenna gain",
                    rule,
                    contract_path,
                )
            )
    return out


# ---------------------------------------------------------------- fuse, wire and bus

# Chassis-wiring column (wiring in air, not bundled; a rule of thumb, not NEC). Source:
# PowerStream, Wire Gauge and Current Limits (https://www.powerstream.com/Wire_Size.htm, page
# dated 2023-04-18), following the Handbook of Electronic Tables and Formulas.
_CHASSIS_SOURCE = (
    "PowerStream, Wire Gauge and Current Limits (powerstream.com/Wire_Size.htm, 2023-04-18) "
    "chassis-wiring column, in air, not bundled; rule of thumb, not NEC"
)
_CHASSIS_A: dict[int, float] = {
    8: 73.0, 9: 64.0, 10: 55.0, 11: 47.0, 12: 41.0, 13: 35.0, 14: 32.0, 15: 28.0, 16: 22.0,
    17: 19.0, 18: 16.0, 19: 14.0, 20: 11.0, 21: 9.0, 22: 7.0, 23: 4.7, 24: 3.5, 25: 2.7,
    26: 2.2, 27: 1.7, 28: 1.4, 29: 1.2, 30: 0.86,
}  # fmt: skip
_NEC_FREE_SOURCE = "NFPA 70 (NEC) 2020 Table 310.17 (free air), copper, 90 °C column"
_NEC_ENCLOSED_SOURCE = (
    "NFPA 70 (NEC) 2020 Table 310.16 (≤3 CCC in raceway/cable/earth), copper, 90 °C column"
)
_LENIENT = "; this is the lenient in-air figure"


def _wire_limits(wire) -> tuple[float, str, float, str] | None:
    """(fail limit, its source, stricter figure, its source) for a wire's gauge.

    The fail limit is the most permissive cited in-air figure (NEC 310.17 free air or the
    PowerStream chassis column); the stricter figure is the lowest cited figure (adds NEC
    310.16 enclosed). A fuse is a violation only above the fail limit.
    """
    lenient = [
        (v, src)
        for v, src in (
            (_AMPACITY_A.get((wire.awg, "free_air")), _NEC_FREE_SOURCE),
            (_CHASSIS_A.get(wire.awg), _CHASSIS_SOURCE),
        )
        if v is not None
    ]
    if not lenient:
        return None
    figures = lenient + (
        [(v, _NEC_ENCLOSED_SOURCE)]
        if (v := _AMPACITY_A.get((wire.awg, "enclosed"))) is not None
        else []
    )
    fail = max(lenient, key=lambda f: f[0])
    strict = min(figures, key=lambda f: f[0])
    return fail[0], fail[1], strict[0], strict[1]


def _fused_upstream(rails: dict, rail_id: str | None) -> bool:
    seen: set[str] = set()
    while rail_id in rails and rail_id not in seen:
        seen.add(rail_id)
        if rails[rail_id].current_limit_enforced_by == "fuse":
            return True
        rail_id = rails[rail_id].upstream
    return False


@requires("topology")
def check_fuse_wire(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A fuse rated above what its wire can carry cannot protect that wire (NO-GO).

    Compares the fuse rating (rail current_limit enforced by a fuse) to the WIRE's published
    ampacity, never to the load. Also: a wire with no fuse upstream while another rail is
    fused (note), and a driver/firmware current limit above the wire's ampacity (advice).
    """
    rails = {r.id: r for r in c.topology.rails}
    any_fuse = any(r.current_limit_enforced_by == "fuse" for r in rails.values())
    out: list[Finding] = []
    for wire in c.topology.wires:
        rail = rails.get(wire.rail)
        if rail is None:
            continue
        found = _wire_limits(wire)
        quantity = f"topology/wires/{wire.id}/fuse"
        if found is None and (preset := choose_preset(c)) and rail.current_limit is not None:
            cap = preset.fuse_max_a_by_awg.get(wire.awg)
            if (
                cap is not None
                and rail.current_limit_enforced_by == "fuse"
                and rail.current_limit.base() > cap
            ):
                out.append(
                    Finding(
                        severity="warning",
                        advice_unsourced=preset.source is None,
                        quantity=quantity,
                        observed=f"{format_magnitude(rail.current_limit.base(), 'A')} fuse on {wire.awg} AWG wire {wire.id}",
                        expected=f"fuse <= {format_magnitude(cap, 'A')} ({preset.name} preset, {preset.source or NOT_SOURCED})",
                        owner=wire.owner,
                        source=preset.source,
                        action=f"fit a fuse at or below {format_magnitude(cap, 'A')} or upsize wire {wire.id!r}",
                    )
                )
        if rail.current_limit_enforced_by == "fuse" and rail.current_limit is not None and found:
            fail_a, fail_src, strict_a, strict_src = found
            fuse_a = rail.current_limit.base()
            if fuse_a > strict_a:
                over = fuse_a > fail_a
                limit_a, source = (fail_a, fail_src + _LENIENT) if over else (strict_a, strict_src)
                out.append(
                    Finding(
                        severity="violation" if over else "warning",
                        advice_unsourced=not over,
                        quantity=quantity,
                        observed=f"{format_magnitude(fuse_a, 'A')} fuse on {wire.awg} AWG wire {wire.id}",
                        expected=f"fuse <= {format_magnitude(limit_a, 'A')} wire ampacity",
                        observed_quantity=Quantity(value=fuse_a, unit="A"),
                        expected_quantity=Quantity(value=limit_a, unit="A"),
                        owner=wire.owner,
                        source=source,
                        action=f"fit a fuse at or below {format_magnitude(limit_a, 'A')} or upsize wire {wire.id!r}",
                    )
                )
        elif (
            rail.current_limit_enforced_by in ("driver", "firmware")
            and rail.current_limit is not None
            and found
            and rail.current_limit.base() > found[0]
        ):
            out.append(
                Finding(
                    severity="warning",
                    quantity=quantity,
                    observed=(
                        f"{rail.current_limit_enforced_by} limit "
                        f"{format_magnitude(rail.current_limit.base(), 'A')} is above wire {wire.id}'s "
                        f"{format_magnitude(found[0], 'A')} ampacity"
                    ),
                    expected="a driver limit the wire can carry",
                    owner=wire.owner,
                    action=f"set the {rail.current_limit_enforced_by} current limit below the wire's ampacity",
                    advice_unsourced=True,
                )
            )
        elif (
            any_fuse
            and rail.current_limit_enforced_by not in ("driver", "firmware")
            and not _fused_upstream(rails, wire.rail)
        ):
            out.append(
                Finding(
                    severity="warning",
                    quantity=quantity,
                    observed=f"wire {wire.id} on rail {rail.id} has no fuse on it or upstream",
                    expected="a fuse on every powered branch",
                    owner=wire.owner,
                    action=f"declare a fuse on rail {rail.id!r}",
                )
            )
    return out


# ---------------------------------------------------------------- wire drop and heat


def wire_drop_v(contract: Contract, wire, current_a: float | None) -> float | None:
    """Volts lost along ``wire`` carrying ``current_a``: catalog wire_table ohm_per_m for its AWG
    x ``wire.length_m`` x current. None when the current, the length or the gauge's table row
    is missing (a missing input is silence, never a guess).

    One conductor, not a supply+return pair: the schema's Wire is one "power conductor" and
    says nothing about a return leg, so the resistance is not doubled. ``contract`` is unused
    today; it is in the signature so a caller can pass it once wires carry a return path.
    """
    table = load_catalog().wire_table
    row = next((r for r in table.rows if r.awg == wire.awg), None) if table else None
    if row is None or wire.length_m is None or current_a is None:
        return None
    return current_a * row.ohm_per_m * wire.length_m


def _wire_load(c: Contract, wire):
    """(amps, servo part or None) a wire carries, or None. A wire ending at a servo carries
    that servo's catalog stall current (a servo with none in the catalog gives None, never a
    guess); any other wire carries its rail's nominal draw."""
    parts = {p.id: p for p in load_catalog().parts}
    for end in (wire.from_, wire.to):
        d = c.device(end.device) if end else None
        if d is None or not d.drives_joint:
            continue
        part = parts.get(servo_for(c.servo, d.drives_joint))
        stall = part.numbers.get("stall_current") if part else None
        return (stall.value, part) if stall else None
    amps = _rail_amps(c, rail) if wire.rail and (rail := c.rail(wire.rail)) else None
    return (amps, None) if amps else None


@requires("topology")
def check_wire_drop(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A servo's wire drops its supply below the servo's catalog minimum input voltage at stall.

    Drop is the catalog AWG table's ohm/m (a rule of thumb) x the wire's length x the servo's
    catalog stall current. Needs a wire length, a rail voltage, a catalog stall current and a
    catalog minimum input voltage; any missing, nothing is said.
    """
    source = load_catalog().wire_table.source if load_catalog().wire_table else None
    out: list[Finding] = []
    for wire in c.topology.wires:
        rail = c.rail(wire.rail) if wire.rail else None
        load = _wire_load(c, wire)
        if rail is None or rail.voltage is None or load is None or load[1] is None:
            continue
        (amps, part), vmin = load, load[1].numbers.get("input_voltage_min")
        drop = wire_drop_v(c, wire, amps)
        if drop is None or vmin is None or source is None:
            continue
        volts = rail.voltage.base()
        if volts - drop >= vmin.value:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"topology/wires/{wire.id}/drop",
                observed=(
                    f"servo wire drops {drop:.2f} V at stall ({volts:.1f} V -> "
                    f"{volts - drop:.2f} V), {amps * drop:.2f} W lost in the wire"
                ),
                expected=f">= {vmin.value:g} V ({part.id} minimum input voltage)",
                observed_quantity=Quantity(value=volts - drop, unit="V"),
                expected_quantity=Quantity(value=vmin.value, unit="V"),
                owner=wire.owner,
                source=f"{source}; stall current and minimum voltage from catalog part {part.id}",
                action=f"shorten wire {wire.id!r}, use a thicker gauge, or raise the rail voltage",
            )
        )
    return out


@requires("topology")
def check_connector_heat(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A catalog connector carrying more than its rated current per contact, with the I^2*R heat.

    The connector is the catalog part its installed Connector.type names; rating and contact
    resistance are that part's catalog numbers. A part without both says nothing.
    """
    parts = {p.id: p for p in load_catalog().parts}
    out: list[Finding] = []
    for wire in c.topology.wires:
        load = _wire_load(c, wire)
        if load is None:
            continue
        for end in (wire.from_, wire.to):
            d = c.device(end.device) if end and end.connector else None
            conn = (
                next((k for k in (d.connectors or []) if k.id == end.connector), None)
                if d
                else None
            )
            part = parts.get(conn.type) if conn and conn.type else None
            n = part.numbers if part and part.family.startswith("fam.connector") else {}
            resistance, rated = n.get("contact_resistance_mohm"), n.get("current_per_contact")
            if resistance is None or rated is None or load[0] <= rated.value:
                continue
            heat_w = load[0] ** 2 * resistance.value / 1000
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"topology/wires/{wire.id}/connector_heat/{end.device}",
                    observed=(
                        f"{load[0]:.3g} A through {part.id} on {end.device}, heating "
                        f"{heat_w:.2f} W in one contact ({resistance.value:g} mOhm)"
                    ),
                    expected=f"<= {rated.value:g} A per contact",
                    observed_quantity=Quantity(value=load[0], unit="A"),
                    expected_quantity=Quantity(value=rated.value, unit="A"),
                    owner=wire.owner,
                    source=f"catalog part {part.id}: current_per_contact and contact_resistance_mohm",
                    action="use a connector rated for this current or split it over more contacts",
                )
            )
    return out


@requires("topology")
def check_rail_operating_point(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A rail's loaded voltage (its voltage minus the worst wire drop at nominal draw) below the
    rail's own declared voltage_range minimum.

    Needs the rail's voltage and voltage_range, its nominal draw and a wire with a length on it;
    any missing, nothing is said. Gauge per net class is not checked: no sourced rule exists.
    """
    source = load_catalog().wire_table.source if load_catalog().wire_table else None
    out: list[Finding] = []
    for rail in c.topology.rails:
        if rail.voltage is None or rail.voltage_range is None or source is None:
            continue
        amps = _rail_amps(c, rail)
        drops = [
            d for w in c.topology.wires if w.rail == rail.id
            if (d := wire_drop_v(c, w, amps)) is not None
        ]  # fmt: skip
        if not drops:
            continue
        loaded, vmin = rail.voltage.base() - max(drops), rail.voltage_range.min.base()
        if loaded >= vmin:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"topology/rails/{rail.id}/operating_point",
                observed=(
                    f"rail {rail.id} loads to {loaded:.2f} V at {amps:.3g} A "
                    f"({rail.voltage.base():g} V less a {max(drops):.2f} V wire drop)"
                ),
                expected=f">= {vmin:g} V (the rail's declared minimum voltage)",
                observed_quantity=Quantity(value=loaded, unit="V"),
                expected_quantity=Quantity(value=vmin, unit="V"),
                source=source,
                action=f"shorten or thicken the wires on rail {rail.id!r}",
            )
        )
    return out


# PX4 "Pixhawk 4 Wiring Quick Start", https://docs.px4.io/main/en/assembly/quick_start_pixhawk4.html :
# the PM board does not supply power to the servos via the FMU PWM-OUT pins; the servo power
# rail "needs to be connected to a BEC equipped ESC or a standalone 5V BEC".
_PX4_SERVO_PAGE = "PX4 docs: https://docs.px4.io/main/en/assembly/quick_start_pixhawk4.html"


def _is_fc(c: Contract, device_id: str | None) -> bool:
    d = c.device(device_id) if device_id else None
    m = c.device_model(d.model) if d else None
    text = f"{m.id} {m.family}".lower() if m else ""
    return any(w in text for w in ("flight", "autopilot", "fcu"))


def _servos_on(c: Contract, rail_id: str):
    for d in c.devices:
        m = c.device_model(d.model)
        if d.power and d.power.rail == rail_id and d.drives_joint and m and m.actuator:
            yield d


def _rail_amps(c: Contract, rail: Rail) -> float | None:
    if rail.voltage is None or rail.voltage.base() <= 0:
        return None
    watts = sum(
        d.power.draw.base()
        for d in c.devices
        if d.power and d.power.rail == rail.id and d.power.draw
    )
    return watts / rail.voltage.base() if watts else None


@requires("topology")
def check_rail_source_rules(c: Contract) -> list[Finding]:
    """Warns about a rail with no power source, servos powered from the flight controller, an LDO that runs too hot, and supplies with no shared ground.

    Each rule reads optional electrical fields; a missing input emits nothing.
    """
    rails = c.topology.rails
    out: list[Finding] = []
    # A source_device that is not a declared device id is an unknown source: it steers nothing
    # (free text must never move a verdict), so it counts as no source modelling.
    known = {r.id: c.device(r.source_device) is not None for r in rails if r.source_device}
    opted_in = any(known.values())  # no source modelling anywhere -> silent
    for r in rails:
        q = f"topology/rails/{r.id}"
        src = r.source_device if known.get(r.id) else None
        if opted_in and not src and r.upstream is None and r.charged_by is None:
            out.append(
                Finding(
                    severity="warning",
                    quantity=q + "/source_device",
                    observed=f"rail {r.id} has no source device, upstream or charger",
                    expected="every rail fed by a declared source",
                    action=f"name the source device (or upstream) of rail {r.id!r}",
                )
            )
        servos = list(_servos_on(c, r.id))
        if servos and _is_fc(c, src):
            out.append(
                Finding(
                    severity="warning",
                    quantity=q + "/servo_source",
                    observed=f"servo rail {r.id} fed by the flight controller {r.source_device}",
                    expected="servos on their own BEC",
                    dependent=", ".join(d.id for d in servos),
                    source=_PX4_SERVO_PAGE,
                    action="servo rail fed by the flight controller: give servos their own BEC (PX4 docs)",
                )
            )
        elif servos and src and any(_is_fc(c, o.source_device) for o in rails):
            out.append(
                Finding(
                    severity="warning",
                    quantity=q + "/ground",
                    observed=f"servo rail {r.id} ({r.source_device}) and the flight controller are separate supplies",
                    expected="one common ground between them",
                    dependent=", ".join(d.id for d in servos),
                    action="join the BEC ground to the flight controller ground",
                    advice_unsourced=True,  # prior-pass rule, no source yet
                )
            )
        up = c.rail(r.upstream) if r.upstream else None
        vin = (
            up.voltage
            if up and up.voltage
            else (
                c.topology.battery.voltage
                if r.upstream == "battery" and c.topology.battery
                else None
            )
        )
        amps = _rail_amps(c, r)
        if (
            r.regulator_kind == "ldo"
            and r.thermal_resistance_k_per_w
            and vin is not None
            and r.voltage is not None
            and amps is not None
        ):
            heat = (vin.base() - r.voltage.base()) * amps
            rise = heat * r.thermal_resistance_k_per_w
            if rise > _LDO_MAX_RISE_K:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=q + "/ldo_heat",
                        observed=f"LDO {r.id} dissipates {heat:.2f} W, {rise:.0f} K over ambient",
                        expected=f"<= {_LDO_MAX_RISE_K:.0f} K rise",
                        action="use a buck converter or cut the dropout voltage",
                        advice_unsourced=True,  # 100 K rise is a convention (125 C - 25 C), uncited
                    )
                )
    return out


_LDO_MAX_RISE_K = 100.0

# Dynamixel Protocol 1.0/2.0 broadcast ID is 0xFE (254): ROBOTIS e-Manual, Protocol 2.0
# "Broadcast ID"; Feetech SCS/STS use the same 0xFE broadcast.
SERVO_BROADCAST_ID = 254


def _address_int(text: str | None) -> int | None:
    try:
        return None if text is None else int(text, 0)
    except ValueError:
        return None


def bus_address_map(c: Contract) -> list[tuple[str, str, str, str]]:
    """Every declared bus address as (bus id, bus kind, address, device id), sorted."""
    if c.topology is None:
        return []
    kinds = {b.id: b.kind for b in c.topology.buses}
    rows = [
        (d.identity.bus_ref, kinds.get(d.identity.bus_ref, "?"), d.identity.address, d.id)
        for d in c.devices
        if d.identity is not None and d.identity.address is not None
    ]
    return sorted(rows)


def _servo_ids(c: Contract) -> list[tuple[str, int, DeviceInstance]]:
    out = []
    for d in c.devices:
        model = c.device_model(d.model)
        text = (d.model + " " + (model.family if model else "")).lower()
        sid = _address_int(d.identity.address) if d.identity else None
        if sid is not None and ("dynamixel" in text or "feetech" in text):
            out.append((d.identity.bus_ref, sid, d))
    return out


@requires("topology")
def check_servo_bus(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Warns about two servos sharing an ID, a servo set to the broadcast ID, and a servo power adapter too small for the servos on it."""
    servos = _servo_ids(c)
    out: list[Finding] = []
    for bus_ref, sid, d in servos:
        if sid == SERVO_BROADCAST_ID:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"devices/{d.id}/identity/address",
                    observed=f"servo {d.id} uses ID {SERVO_BROADCAST_ID}, the protocol's broadcast ID",
                    expected=f"an ID other than {SERVO_BROADCAST_ID} (every servo answers a broadcast)",
                    owner=d.owner,
                    action=f"give {d.id} a unique ID below {SERVO_BROADCAST_ID}",
                )
            )
    groups: dict[tuple[str, int], list[DeviceInstance]] = {}
    for bus_ref, sid, d in servos:
        groups.setdefault((bus_ref, sid), []).append(d)
    for (bus_ref, sid), devs in groups.items():
        # check_bus_identities already flags devices sharing the identical address string
        if len(devs) > 1 and len({d.identity.address for d in devs}) > 1:
            names = " and ".join(d.id for d in devs)
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"topology/buses/{bus_ref}/servo_id/{sid}",
                    observed=f"{names} share servo ID {sid} on {bus_ref}",
                    expected="one servo per ID on a bus",
                    owner=", ".join(sorted({d.owner for d in devs})),
                    action="give each servo its own ID",
                )
            )
    rails = {r.id: r for r in c.topology.rails}
    for d in c.devices:
        model = c.device_model(d.model)
        text = (d.model + " " + (model.family if model else "")).lower()
        if d.identity is None or d.power is None or not ("adapter" in text or "u2d2" in text):
            continue
        rail = rails.get(d.power.rail)
        on_bus = [
            s
            for b, _, s in servos
            if b == d.identity.bus_ref and s.power and s.power.rail == d.power.rail
        ]
        if rail is None or not on_bus:
            continue
        load_w, _, _, unknown = _worst_rail_power(c, rail.id)
        if not unknown and load_w > rail.capacity.base():
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"devices/{d.id}/power",
                    observed=(
                        f"servos on adapter {d.id} draw {format_magnitude(load_w, 'W')}, "
                        f"its rail {rail.id} supplies {format_magnitude(rail.capacity.base(), 'W')}"
                    ),
                    expected="servos powered from a supply that can carry them",
                    owner=d.owner,
                    action="power the servos from their own supply, not the adapter",
                )
            )
    return out


_WIKI_CAN = "https://en.wikipedia.org/wiki/CAN_bus"  # read 2026-11-13: <40 m at 1 Mbit/s, 500 m at 125 kbit/s
_PX4_DRONECAN = (
    "https://docs.px4.io/main/en/dronecan/"  # read 2026-11-13: no allocation server on CAN2
)


@requires("topology")
def check_bus_wiring_rules(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Warns about bus cabling risks, only for buses that declare phy / length_m.

    Warnings only, never a violation. Sourced: CAN length limits (Wikipedia, two points read) and
    the PX4 CAN2 node-ID note. Advice (numbers with no read source): TTL length, RS-485
    termination at 2 m+, untwisted-cable noise. ROBOTIS e-manual pages were read and give no
    termination or length numbers. CAN termination stays with check_bus_bandwidth (can_segment).
    """
    out: list[Finding] = []
    for bus in c.topology.buses:
        phy = (bus.phy or "").lower()
        q = f"topology/buses/{bus.id}"

        def warn(sub, observed, expected, action, source=None, owner=bus.host, q=q):
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"{q}/{sub}",
                    observed=observed,
                    expected=expected,
                    owner=owner,
                    source=source,
                    action=action,
                    advice_unsourced=source is None,
                )
            )

        length = bus.length_m
        if phy == "ttl" and length is not None and length > 3:
            warn(
                "length_m",
                f"TTL bus {bus.id} is {length:g} m long",
                "a TTL servo bus under about 3 m; longer runs belong on RS-485",
                "move the servos to an RS-485 bus or shorten the run",
            )
        if (
            phy == "rs485"
            and length is not None
            and length >= 2
            and bus.bitrate is not None
            and bus.bitrate >= 1e6
            and bus.can_segment is not None
            and not any(abs(r - 120) <= 12 for r in bus.can_segment.termination_ohm)
        ):
            warn(
                "termination",
                f"{length:g} m RS-485 bus {bus.id} at {bus.bitrate / 1e6:g} Mbps declares no 120 ohm termination",
                "a 120 ohm terminator at each end of a long fast RS-485 bus",
                "add 120 ohm terminators at both ends and declare them",
            )
        if phy.startswith("can") and length is not None and bus.bitrate is not None:
            if (bus.bitrate >= 1e6 and length > 40) or (bus.bitrate <= 125e3 and length > 500):
                limit = "40 m at 1 Mbit/s" if bus.bitrate >= 1e6 else "500 m at 125 kbit/s"
                warn(
                    "length_m",
                    f"CAN bus {bus.id} is {length:g} m at {bus.bitrate / 1e3:g} kbit/s",
                    f"no longer than about {limit}",
                    "shorten the bus or lower the bitrate",
                    source=_WIKI_CAN,
                )
        if (bus.protocol or "").lower() == "dronecan" and "can2" in bus.id.lower():
            warn(
                "node_id",
                f"DroneCAN bus {bus.id} looks like PX4's CAN2 port",
                "node IDs set by hand for devices only on CAN2",
                "PX4 runs no node-ID allocation server on CAN2: set each node ID manually",
                source=_PX4_DRONECAN,
            )
        if bus.twisted is False and (phy == "rs485" or phy.startswith("can")):
            warn(
                "twisted",
                f"{bus.id} ({phy}) runs on untwisted cable",
                "twisted-pair cable on a differential bus",
                "use twisted pair to cut noise pickup",
            )
    return out


def check_estop_wiring(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A dual-channel safe-torque-off driver needs both channels on the e-stop chain, on
    separate contacts. Warnings only; nothing is emitted without connector safety data.

    Source for the channel rule: the driver's own declared STO1/STO2 channels (structural).
    Stop categories: https://www.lenze.com/es-us/go/akb/200502242/1 (read: defines 0/1/2, does
    not say which an e-stop may use); the "e-stop is 0 or 1" rule is unsourced, so advice.
    """
    safety = c.safety
    if safety is None:
        return []
    out: list[Finding] = []
    ends = {  # (device, kind) -> far ends of the signal wires touching that channel's pin
        (ch.device, ch.kind): set() for ch in safety.channels or ()
    }
    for ch in safety.channels or ():
        mine = (ch.device, ch.connector, str(ch.pin))
        for w in c.topology.wires:
            pair = [(e.device, e.connector, str(e.pin)) for e in (w.from_, w.to) if e is not None]
            if len(pair) == 2 and mine in pair:
                ends[(ch.device, ch.kind)].add(pair[1] if pair[0] == mine else pair[0])
    for device in sorted({d for d, _ in ends}):
        one, two = ends.get((device, "STO1")), ends.get((device, "STO2"))
        if one is None or two is None or not (one or two):
            continue  # not dual-channel, or no wiring recorded: nothing to judge
        quantity = f"safety/channels/{device}/sto"
        if bool(one) != bool(two):
            missing = "STO2" if one else "STO1"
            observed = (
                f"{device} declares STO1 and STO2 but only {'STO1' if one else 'STO2'} is wired"
            )
            action = f"wire {missing} of {device} to the e-stop chain as well"
        elif one & two:
            observed = f"STO1 and STO2 of {device} both land on one contact"
            action = f"give STO1 and STO2 of {device} separate e-stop contacts"
        else:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=quantity,
                observed=observed,
                expected="both STO channels wired, each to its own e-stop contact",
                owner=device,
                source=f"{device}'s own declared STO1/STO2 channels",
                action=action,
            )
        )
    if safety.estop_category == 2:
        out.append(
            Finding(
                severity="warning",
                advice_unsourced=True,  # "an e-stop is category 0 or 1" read from summaries only
                quantity="safety/estop_category",
                observed="emergency stop declared as stop category 2 (power stays on)",
                expected="stop category 0 or 1 for an emergency stop",
                owner="safety",
                action="declare the e-stop as category 0 (cut power) or 1 (ramp down, then cut)",
            )
        )
    return out


def _connector_of(c: Contract, end):
    d = c.device(end.device) if end is not None and end.connector else None
    return d, next((k for k in (d.connectors or []) if k.id == end.connector), None) if d else None


def _family(c: Contract, conn) -> str:
    """Declared family of an installed connector's type (lockfile types first, then catalog)."""
    tid = conn.type if conn is not None else None
    t = next((t for t in c.connector_types or [] if t.id == tid), None) or next(
        (t for t in load_catalog().connector_types if t.id == tid), None
    )
    return re.sub(r"[^a-z0-9]", "", (t.family or "").lower()) if t else ""


def check_connector_mating(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A wire whose two connector ends are different declared families cannot mate.

    Source: the two connector types' own declared families. Missing type or family: nothing.
    """
    out: list[Finding] = []
    for w in c.topology.wires if c.topology else []:
        (da, ka), (db, kb) = _connector_of(c, w.from_), _connector_of(c, w.to)
        fa, fb = _family(c, ka), _family(c, kb)
        if fa and fb and fa != fb:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"topology/wires/{w.id}/mating",
                    observed=f"{ka.type} on {da.id} wired to {kb.type} on {db.id}",
                    expected="two connectors of one family",
                    owner=w.owner,
                    source="the two connector types' declared families",
                    action="use matching connectors or an adapter cable",
                )
            )
    return out


def _protected_ids(c: Contract, kind: str | None = None) -> set[str]:
    """Ids named in `protects` by protection[] entries (of one kind, or any)."""
    return {
        t
        for p in (c.topology.protection if c.topology else None) or []
        if kind is None or p.kind == kind
        for t in p.protects or []
    }


def check_back_power(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A board with two declared power inputs and no reverse-polarity/ideal-diode guard on it.

    The Raspberry Pi back-power / ideal-diode note was not read at source, so advice only.
    A `reverse_polarity` protection whose `protects` names the device counts as the guard.
    """
    guarded = _protected_ids(c)
    return [
        Finding(
            severity="warning",
            advice_unsourced=True,
            quantity=f"devices/{d.id}/power_inputs",
            observed=f"{d.id} is powered from {len(d.power_inputs)} inputs with no diode between",
            expected="one supply, or an ideal diode between the sources",
            owner=d.owner,
            action=f"declare an ideal diode protecting {d.id}, or power it from one source",
        )
        for d in c.devices
        if len(d.power_inputs or []) > 1 and d.id not in guarded
    ]


def check_reverse_polarity(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A file that uses protection[] with a multi-cell pack feeding a rail and no
    reverse-polarity guard on that path gets one warning (advice only: no maker source read).

    A guard is a `reverse_polarity` entry naming "battery", a battery-fed rail or a device on
    one, or naming nothing (a declared pack-level guard). A TVS never counts. When
    check_back_power already flags a device on a battery-fed rail, it already says the path
    has no diode, so this stays quiet rather than say it twice.
    """
    t = c.topology
    b = t.battery if t else None
    if t is None or t.protection is None or b is None or (b.cells_series or 0) < 2:
        return []
    fed = {r.id for r in t.rails if r.upstream == "battery"}
    if not fed:
        return []
    on_path = fed | {"battery"} | {d.id for d in c.devices if d.power and d.power.rail in fed}
    rp = [p for p in t.protection if p.kind == "reverse_polarity"]
    if any(not p.protects for p in rp) or on_path & _protected_ids(c, "reverse_polarity"):
        return []
    said = {f.quantity.split("/")[1] for f in check_back_power(c)}
    if any(d.id in said and d.power and d.power.rail in fed for d in c.devices):
        return []
    return [
        Finding(
            severity="warning",
            advice_unsourced=True,
            quantity="topology/battery/reverse_polarity",
            observed=f"{b.cells_series}S pack feeds {', '.join(sorted(fed))} with no reverse-polarity guard",
            expected="a reverse-polarity guard (diode, ideal diode or P-FET) on the pack path",
            action="declare a reverse-polarity protection that protects the battery or its rail",
        )
    ]


def check_tvs_rating(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A rail with a motor on it that is declared unable to absorb regen
    (accepts_regen false) needs a TVS naming it, whose standoff is not below the rail's
    highest voltage. Only a file that uses protection[] is read. Advice only: no maker
    source read, so no number here is sourced.
    """
    t = c.topology
    if t is None or t.protection is None:
        return []
    tvs = [p for p in t.protection if p.kind == "tvs"]
    out: list[Finding] = []
    for r in t.rails:
        motors = [d for d in c.devices if d.drives_joint and d.power and d.power.rail == r.id]
        top = r.voltage_range.max if r.voltage_range else r.voltage
        if r.accepts_regen is not False or not motors or top is None:
            continue
        mine = [p for p in tvs if r.id in (p.protects or [])]
        low = [p for p in mine if p.v_standoff_v is not None and p.v_standoff_v < top.base()]
        if mine and not low:
            continue
        out.append(
            Finding(
                severity="warning",
                advice_unsourced=True,
                quantity=f"topology/{r.id}/tvs",
                observed=(
                    f"{low[0].id} stands off {low[0].v_standoff_v:g} V on {r.id}, up to {top.base():g} V"
                    if low
                    else f"{r.id} carries motors that cannot absorb regen and has no TVS"
                ),
                expected=f"a TVS naming {r.id} with standoff at or above {top.base():g} V",
                action=f"declare a TVS on {r.id} rated at or above its highest voltage",
            )
        )
    return out


def _chain_amps(c: Contract, host, conn_id: str, joins) -> float | None:
    """Total current of every device downstream of host's connector (whole chain, not per
    cable), or None when any of them lacks a draw or rail voltage."""
    from lockwire.stress import _draw_w, _rail_v

    wires = [w for w in c.topology.wires if w.from_ and w.to and joins(w)]
    seen, todo = {host.id}, [(host.id, conn_id)]
    while todo:
        dev, only = todo.pop()
        for w in wires:
            ends = [(e.device, e.connector) for e in (w.from_, w.to)]
            for me, far in (ends, ends[::-1]):
                if me[0] == dev and (only is None or me[1] == only) and far[0] not in seen:
                    seen.add(far[0])
                    todo.append((far[0], None))
    amps = 0.0
    for d in map(c.device, seen - {host.id}):
        w, v = (_draw_w(d), _rail_v(c, d.power.rail)) if d and d.power else (None, None)
        if not (w and v):
            return None
        amps += w / v
    return amps if len(seen) > 1 else None


# SparkFun "Qwiic Connect System" FAQ, read at https://www.sparkfun.com/qwiic: "The very
# conservative max current on a Qwiic cable is 226mA".
_QWIIC_A = 0.226


def _chain_finding(c, host, conn, limit, amps, name, source):
    return Finding(
        severity="warning",
        quantity=f"devices/{host.id}/{conn.id}/{name}",
        observed=f"{amps * 1000:.0f} mA drawn through {conn.id} on {host.id}",
        expected=f"<= {limit * 1000:.0f} mA over the whole chain",
        observed_quantity=Quantity(value=amps * 1000, unit="mA"),
        expected_quantity=Quantity(value=limit * 1000, unit="mA"),
        owner=host.owner,
        source=source,
        action="power the downstream boards from their own supply or split the chain",
    )


def check_qwiic_budget(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """The summed current of a whole Qwiic chain against SparkFun's 226 mA cable figure.

    The chain starts at a Qwiic connector on a device that has power_inputs (the host).
    """
    out: list[Finding] = []
    for host in c.devices if c.topology else []:
        for conn in host.connectors or [] if host.power_inputs else []:
            if _family(c, conn) != "qwiic":
                continue
            amps = _chain_amps(
                c,
                host,
                conn.id,
                lambda w: all(
                    _family(c, _connector_of(c, e)[1]) == "qwiic" for e in (w.from_, w.to)
                ),
            )
            if amps is not None and amps > _QWIIC_A:
                out.append(
                    _chain_finding(
                        c,
                        host,
                        conn,
                        _QWIIC_A,
                        amps,
                        "qwiic",
                        "https://www.sparkfun.com/qwiic (FAQ: very conservative max current on a Qwiic cable is 226mA)",
                    )
                )
    return out


def check_port_budget(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """The summed current of everything downstream of a connector against the max_current_a
    declared on that connector (e.g. a flight-controller port). Nothing without the rating."""
    out: list[Finding] = []
    for host in c.devices if c.topology else []:
        for conn in host.connectors or []:
            if conn.max_current_a is None:
                continue
            amps = _chain_amps(c, host, conn.id, lambda w: True)
            if amps is not None and amps > conn.max_current_a:
                out.append(
                    _chain_finding(
                        c,
                        host,
                        conn,
                        conn.max_current_a,
                        amps,
                        "port_budget",
                        f"connector {conn.id} max_current_a declared in the lockfile",
                    )
                )
    return out


CHECK_PACK_1 = (
    check_model_checks_skipped,
    check_urdf_sanity,
    check_link_values,
    check_inertia_valid,
    check_duplicate_inertial,
    check_mirror,
    check_sibling_effort,
    check_inertia_scale,
    check_mass_vs_size,
    check_no_material,
    check_inertia_sanity,
    check_limit_unit_traps,
    check_datasheet_limit,
    check_collision_visual,
    check_battery_shipping_class,
    check_cold_derating_coverage,
    check_i2c_reserved_address,
    check_controller_limits,
    check_driver_watchdog_marker,
    check_fuse_wire,
    check_servo_bus,
    check_bus_wiring_rules,
    check_wire_drop,
    check_connector_heat,
    check_rail_operating_point,
)

CHECK_PACK_2 = (
    check_battery_cell_window,
    check_battery_c_rate,
    check_battery_sag,
    check_battery_temperature,
    check_battery_energy_density,
)

CHECK_PACK_3 = (
    check_charger,
    check_battery_cutoff,
    check_connector_rating,
    check_threadlocker_on_print,
    check_print_bed_fit,
    check_belt_length,
    check_autopilot_failsafe,
    check_radio_legal_power,
)


# ---------------------------------------------------------------- check pack 4
# Structure of the model file itself: control blocks, frames, mimics. No lockfile lines needed.


def _model_warner(c: Contract, contract_path: Path | None):
    """(root, warn, findings) for a model-structure check, or None with no readable URDF."""
    found = _urdf_root(c, contract_path)
    if found is None:
        return None
    path, root = found
    fix = _relock_command(path, contract_path, c.model.xacro_args)
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

    return root, warn, out


def check_control_joint_names(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """ros2_control and transmission blocks naming a joint the model lacks, or commanding
    a fixed or mimic joint."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    joints = {el.get("name"): el for el in root.findall("joint")}
    seen: set[str] = set()
    # ros2_control component_parser.cpp refuses all three when the hardware loads
    # (https://raw.githubusercontent.com/ros-controls/ros2_control/master/hardware_interface/src/component_parser.cpp).
    for block in (*root.findall("ros2_control"), *root.findall("transmission")):
        for ref in block.findall("joint"):
            name = ref.get("name", "?")
            real = joints.get(name)
            kind = (
                None
                if real is None or not ref.findall("command_interface")
                else "fixed"
                if real.get("type", "fixed") == "fixed"
                else "mimic"
                if real.find("mimic") is not None
                else None
            )
            if name in seen or (real is not None and kind is None):
                continue
            seen.add(name)
            if real is None:
                warn(
                    f"control/{name}",
                    f"the {block.tag} block names joint {name}, which the model does not have",
                    "a joint the model declares",
                    f"rename {name} in the {block.tag} block or add the joint",
                )
            else:
                warn(
                    f"control/{name}",
                    f"the {block.tag} block commands {name}, a {kind} joint",
                    "command interfaces only on joints that move on their own",
                    f"remove the command interface from {name}",
                )
    return out


# REP-103: an optical frame has z forward, x right, y down, so from a body frame
# (x forward, y left, z up) it is rpy (-pi/2, 0, -pi/2)
# (https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0103.rst).
_OPTICAL_ROTATION: _Matrix3 = ((0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0))
_OPTICAL_TOLERANCE = 5e-3  # ours, no published figure: loose enough for a rounded -1.57


def check_optical_frames(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A camera's optical frame not rotated to z forward, x right, y down (REP-103)."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx

    def rotation(joint: ET.Element) -> _Matrix3:
        origin = joint.find("origin")
        rpy = _floats(origin.get("rpy")) if origin is not None else []
        return _rpy_rotation((rpy[0], rpy[1], rpy[2]) if len(rpy) == 3 else (0.0, 0.0, 0.0))

    def off(rot: _Matrix3) -> float:
        return max(abs(rot[i][j] - _OPTICAL_ROTATION[i][j]) for i in range(3) for j in range(3))

    above = {
        j.find("child").get("link"): rotation(j)
        for j in root.findall("joint")
        if j.get("type", "fixed") == "fixed" and j.find("child") is not None
    }
    for el in root.findall("joint"):
        parent, child, origin = el.find("parent"), el.find("child"), el.find("origin")
        link = child.get("link", "") if child is not None else ""
        if (
            el.get("type", "fixed") != "fixed"
            or not link.endswith(("_optical", "_optical_frame"))
            or parent is None
            or "optical" in parent.get("link", "")
        ):
            continue
        rpy = _floats(origin.get("rpy")) if origin is not None else []
        rot, up = rotation(el), above.get(parent.get("link"))
        # the turn may be split over the two fixed joints above the optical frame
        both = up and tuple(
            tuple(sum(up[i][k] * rot[k][j] for k in range(3)) for j in range(3)) for i in range(3)
        )
        if off(rot) > _OPTICAL_TOLERANCE and (not both or off(both) > _OPTICAL_TOLERANCE):
            shown = " ".join(f"{v:.4g}" for v in rpy) or "0 0 0"
            warn(
                f"frames/{link}/optical",
                f"{link} is rotated rpy {shown} from {parent.get('link')}",
                "rpy -1.5708 0 -1.5708 (z forward, x right, y down)",
                f"set the rpy of joint {el.get('name', '?')} to -1.5708 0 -1.5708",
            )
    return out


def check_frame_tree(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """Frames that do not form one tree: more or fewer than one root, a link called map or
    odom, a floating or planar joint below the root. (The parser already refuses a joint
    that names a missing link.)"""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    links = [el.get("name", "") for el in root.findall("link")]
    children: set[str] = set()
    up: dict[str, str] = {}
    loose: list[tuple[str, str]] = []
    for el in root.findall("joint"):
        ends = [
            (e.get("link", "") if e is not None else "") for e in map(el.find, ("parent", "child"))
        ]
        children.add(ends[1])
        up.setdefault(ends[1], ends[0])
        if el.get("type") in ("floating", "planar"):
            loose.append((el.get("name", "?"), ends[0]))
    roots = [n for n in links if n not in children]
    if links and len(roots) != 1:
        walk = [links[0]]  # no root: every link has a parent, so walking up ends in a loop
        while not roots and up[walk[-1]] not in walk:
            walk.append(up[walk[-1]])
        cycle = " -> ".join([*walk[walk.index(up[walk[-1]]) :], up[walk[-1]]]) if not roots else ""
        warn(
            "tree/roots",
            f"{len(roots)} root links"
            + (": " + ", ".join(sorted(roots)) if roots else f"; kinematic loop {cycle}"),
            "exactly one link with no parent joint",
            "join every part to one root link",
            severity="violation",
        )
    # REP-105: map and odom are world frames a localiser publishes; each frame has one parent
    # (https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0105.rst).
    world = sorted(n for n in links if n in ("map", "odom"))
    if world:
        warn(
            "tree/map_odom",
            "the model declares a link named " + " and ".join(world),
            "map and odom published by localisation, not declared in the model",
            "rename or remove the link",
        )
    # kdl_parser: "Converting unknown joint type of joint '%s' into a fixed joint"
    # (https://raw.githubusercontent.com/ros/kdl_parser/ros2/kdl_parser/src/kdl_parser.cpp).
    below = sorted(name for name, parent in loose if parent not in roots)
    if below:
        warn(
            "tree/floating",
            "floating or planar joints below the root: " + ", ".join(below),
            "floating and planar joints only at the root (KDL turns others into fixed joints)",
            "replace them with revolute and prismatic joints",
            severity="warning" if root.find("ros2_control") is not None else "not_scored",
        )
    return out


def check_mimic_targets(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A mimic joint that follows a joint the model lacks, follows another mimic, or
    follows itself in a circle."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    joints = {el.get("name", "?") for el in root.findall("joint")}
    mimics = {
        el.get("name", "?"): el.find("mimic").get("joint", "")
        for el in root.findall("joint")
        if el.find("mimic") is not None
    }
    for name, target in mimics.items():
        # urdfdom never checks that the mimicked joint exists
        # (https://raw.githubusercontent.com/ros/urdfdom/master/urdf_parser/src/joint.cpp).
        if target not in joints:
            warn(
                f"{name}/mimic_target",
                f"{name} mimics {target or '(no joint)'}, which the model does not have",
                "a joint the model declares",
                f"point {name}'s mimic at a real joint",
                severity="violation",
            )
        elif target in mimics:
            seen, cur = {name}, target
            while cur in mimics and cur not in seen:
                seen.add(cur)
                cur = mimics[cur]
            circle = cur in seen
            warn(
                f"{name}/mimic_target",
                f"{name} mimics {target}, itself a mimic" + (", in a circle" if circle else ""),
                "a mimic that follows a driven joint",
                f"point {name}'s mimic at the joint that is driven",
                # A chain is reported as unsupported only by old user reports: a disclosure.
                severity="warning" if circle else "not_scored",
            )
    return out


CHECK_PACK_4 = (
    check_control_joint_names,
    check_optical_frames,
    check_frame_tree,
    check_mimic_targets,
)


def _float(el: ET.Element | None, attr: str) -> float | None:
    try:
        return float(el.get(attr)) if el is not None and el.get(attr) is not None else None
    except ValueError:
        return None


def check_safety_limits(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A joint's safety controller whose soft limits sit outside the joint's hard limits,
    or are left at their default of zero so the joint cannot move."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    for joint in root.findall("joint"):
        safety, limit, name = (
            joint.find("safety_controller"),
            joint.find("limit"),
            joint.get("name", "?"),
        )
        if safety is None or joint.get("type") not in ("revolute", "prismatic"):
            continue
        # urdfdom: soft_lower_limit and soft_upper_limit default to 0 when absent
        # (https://raw.githubusercontent.com/ros/urdfdom/master/urdf_parser/src/joint.cpp).
        attrs = ("soft_lower_limit", "soft_upper_limit")
        unset = [a for a in attrs if _float(safety, a) is None]
        soft = [_float(safety, a) or 0.0 for a in attrs]
        hard = [_float(limit, a) for a in ("lower", "upper")]
        if unset or soft[0] >= soft[1]:
            warn(
                f"{name}/soft_limits",
                f"{name} has a safety controller without {' and '.join(unset)}, which then is 0"
                if unset
                else f"{name} soft limits {soft[0]:g} to {soft[1]:g} leave it no room to move",
                "soft limits set inside the joint's range",
                f"set soft_lower_limit and soft_upper_limit on {name}",
            )
        elif None not in hard and (soft[0] < hard[0] or soft[1] > hard[1]):
            warn(
                f"{name}/soft_limits",
                f"{name} soft limits {soft[0]:g} to {soft[1]:g} reach past its hard limits "
                f"{hard[0]:g} to {hard[1]:g}",
                "soft limits inside the hard limits, so they act first",
                f"move {name}'s soft limits inside its hard limits",
            )
    return out


def check_gazebo_references(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """A ``<gazebo reference=...>`` block naming a link or joint the model lacks (the
    simulator drops the block without a word), and two sensors sharing one name."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    known = {el.get("name") for el in (*root.findall("link"), *root.findall("joint"))}
    for ref in sorted({g.get("reference") for g in root.findall("gazebo")} - known - {None, ""}):
        warn(
            f"gazebo/{ref}",
            f"a gazebo block refers to {ref}, which the model does not have",
            "a link or joint the model declares",
            f"rename the gazebo block's reference from {ref} to a real link or joint",
        )
    # only the simulator's sensors: a ros2_control <sensor> has to repeat the gazebo name
    names = [s.get("name") for g in root.findall("gazebo") for s in g.iter("sensor")]
    names = [n for n in names if n]
    for name in sorted({n for n in names if names.count(n) > 1}):
        # sdformat sensor.sdf: a sensor name is required and must be unique in its model
        # (https://github.com/gazebosim/sdformat/blob/main/sdf/1.12/sensor.sdf).
        warn(
            f"gazebo/sensors/{name}",
            f"{names.count(name)} sensors are named {name}",
            "one name per sensor",
            f"give each sensor named {name} its own name",
        )
    return out


def check_mesh_scale_agreement(c: Contract, *, contract_path: Path | None = None) -> list[Finding]:
    """One link using the same mesh file for its visual and its collision at two different
    scales, so what you see is not what collides."""
    ctx = _model_warner(c, contract_path)
    if ctx is None:
        return []
    root, warn, out = ctx
    for link in root.findall("link"):
        scales: dict[str, set[str]] = {}
        for mesh in (
            *link.findall("visual/geometry/mesh"),
            *link.findall("collision/geometry/mesh"),
        ):
            scale = _floats(mesh.get("scale") or "1 1 1")
            if len(scale) == 3:
                scales.setdefault(mesh.get("filename", ""), set()).add(
                    " ".join(f"{v:g}" for v in scale)
                )
        for filename, seen in scales.items():
            if len(seen) > 1:
                warn(
                    f"{link.get('name', '?')}/mesh_scale",
                    f"{Path(filename).name} is used at scales {' and '.join(sorted(seen))}",
                    "one scale for the visual and the collision mesh",
                    f"give {link.get('name', '?')}'s visual and collision mesh the same scale",
                )
    return out


CHECK_PACK_5 = (
    check_safety_limits,
    check_gazebo_references,
    check_mesh_scale_agreement,
    check_estop_wiring,
    check_connector_mating,
    check_back_power,
    check_reverse_polarity,
    check_tvs_rating,
    check_qwiic_budget,
    check_port_budget,
)
