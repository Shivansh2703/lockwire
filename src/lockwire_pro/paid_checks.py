"""Paid record checks: actuator-case conformance and runtime condition envelopes.

Moved out of ``checks.py`` (open-core split) so the free ``checks`` module
imports nothing paid. Importing this module fills ``checks.PAID_CHECKS``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

from lockwire import units
from lockwire_pro.actuator_case import (
    ActuatorCaseEvidence,
    ActuatorCaseReport,
    ClockMapping,
    DCDeliveryObservation,
    PredictionSample,
    SignalObservation,
    evaluate_actuator_case,
    find_event,
    find_signal,
    unadmitted_actuator_case_report,
)
from lockwire.checks import PAID_CHECKS, _with_enforcement_provenance, unusable_effort_finding
from lockwire.report import Finding, ScoredRecord, format_magnitude
from lockwire.schema import ActuatorCase, ArtifactRef, Contract, Provenance, Quantity


# ---------------------------------------------------------------- condition envelopes


def check_condition_envelopes(c: Contract, current_conditions: dict[str, float]) -> list[Finding]:
    """Refuse to extrapolate beyond the conditions supported by healthy baselines.

    Interpolation is defined independently per axis for each device and context.
    Convert every baseline band for an axis to the first band's unit, then take
    the closed span from the smallest lower endpoint to the largest upper
    endpoint. A current point is interpolatable exactly when every declared
    coordinate lies in its axis's closed span; equivalently, the supported
    region is the Cartesian product of those per-axis spans. A gap between two
    baseline bands is therefore interpolation, even when no baseline reproduced
    that exact point. A coordinate below or above its span is extrapolation.
    With one baseline, its own closed band is the span.

    ``current_conditions`` values use the unit declared by the first baseline
    band for that axis. Missing declared axes are unsupported rather than
    silently assumed healthy. Extra current axes are ignored because the
    contract has not declared them as condition dependencies.

    When two or more condition axes are declared for a context, emit
    ``not_scored`` naming those axes — independent-axis verification is stated;
    cross-axis interaction is unverified. Correlated/diagonal region modeling
    is deferred. A single-axis envelope needs no such disclosure.

    ScoreBook: intentionally omitted. CLI ``check`` never passes conditions, so
    this path is not the newcomer denominator surface. Callers that do pass
    ``current_conditions`` into ``run_record_checks`` get real findings without
    those comparisons inflating ``scored`` — a deliberate API-path gap, not a
    silent pass claim on the CLI.
    """
    out: list[Finding] = []

    for device in c.devices:
        by_context: dict[str, list] = {}
        for envelope in device.envelopes:
            if envelope.conditions:
                by_context.setdefault(envelope.context, []).append(envelope)

        for context, envelopes in by_context.items():
            spans: dict[str, tuple[float, float, str]] = {}
            for envelope in envelopes:
                for axis, band in envelope.conditions.items():
                    if axis not in spans:
                        spans[axis] = (band.lo, band.hi, band.unit)
                        continue
                    lo, hi, unit = spans[axis]
                    band_lo = units.convert(band.lo, band.unit, unit)
                    band_hi = units.convert(band.hi, band.unit, unit)
                    spans[axis] = (min(lo, band_lo), max(hi, band_hi), unit)

            if len(spans) >= 2:
                axis_names = ", ".join(spans)
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=f"devices/{device.id}/envelopes/{context}/conditions",
                        observed=(
                            f"axes {axis_names} verified independently; "
                            "interaction between them unverified"
                        ),
                        expected=(
                            "correlated multi-axis envelope modeling "
                            "(deferred until real envelope data exists)"
                        ),
                        dependent=", ".join(device.depends) or None,
                        owner=device.owner,
                        action=(
                            "treat the supported region as a Cartesian product of "
                            "per-axis spans, or wait for correlated-region modeling"
                        ),
                    )
                )

            outside = [
                axis
                for axis, (lo, hi, _) in spans.items()
                if axis in current_conditions and not lo <= current_conditions[axis] <= hi
            ]
            missing = [axis for axis in spans if axis not in current_conditions]
            unsupported = outside + missing
            if not unsupported:
                continue

            # Outside axes: name the measured value so an operator can see how
            # far outside they are (B36). Missing axes have no measured value —
            # do not invent one. Typed magnitudes populate only when a single
            # outside axis supplies an honest scalar; multi-axis outside cannot
            # fit in one Quantity, and missing-only has nothing to type
            # (voltage-compat / B88 cycle-2 precedent: leave unset).
            outside_parts = [
                f"{axis}: {format_magnitude(current_conditions[axis], spans[axis][2])}"
                for axis in outside
            ]
            if outside and missing:
                observed = (
                    f"outside baselined conditions on {', '.join(outside_parts)}; "
                    f"current conditions unavailable on {', '.join(missing)} "
                    "- no data supports healthy here"
                )
            elif outside:
                observed = (
                    f"outside baselined conditions on {', '.join(outside_parts)} "
                    "- no data supports healthy here"
                )
            else:
                observed = (
                    f"current conditions unavailable on {', '.join(missing)} "
                    "- no data supports healthy here"
                )
            expected = ", ".join(
                f"{axis} in [{lo:g}, {hi:g}] {unit}" for axis, (lo, hi, unit) in spans.items()
            )

            observed_quantity: Quantity | None = None
            expected_quantity: Quantity | None = None
            if len(outside) == 1:
                axis = outside[0]
                lo, hi, unit = spans[axis]
                measured = current_conditions[axis]
                observed_quantity = Quantity(value=measured, unit=unit)
                bound = lo if measured < lo else hi
                expected_quantity = Quantity(value=bound, unit=unit)

            out.append(
                Finding(
                    severity="violation",
                    quantity=f"devices/{device.id}/envelopes/{context}/conditions",
                    observed=observed,
                    expected=expected,
                    observed_quantity=observed_quantity,
                    expected_quantity=expected_quantity,
                    dependent=", ".join(device.depends) or None,
                    owner=device.owner,
                    action=(
                        "operate within the baselined condition spans or record and review "
                        "a healthy baseline that extends them"
                    ),
                )
            )

    return out


def _check_actuator_cases(
    contract: Contract,
    *,
    actuator_evidence: Mapping[str, ArtifactRef] | None,
    evidence_root: Path | None,
    contract_sha256: str | None,
) -> tuple[list[Finding], list[ActuatorCaseReport]]:
    """Run each declared case only when its immutable evidence inputs exist."""
    if not contract.actuator_cases:
        return [], []
    missing = (
        actuator_evidence is None or evidence_root is None or not _is_raw_sha256(contract_sha256)
    )
    findings: list[Finding] = []
    reports: list[ActuatorCaseReport] = []
    for case in contract.actuator_cases:
        if missing:
            report = unadmitted_actuator_case_report(
                case,
                "evidence mapping, evidence root, or full raw contract SHA-256 is missing",
            )
            findings.extend(report.findings)
            reports.append(report)
            continue
        evidence_ref = actuator_evidence.get(case.id)
        if evidence_ref is None:
            report = unadmitted_actuator_case_report(
                case, "no evidence artifact is mapped to this case"
            )
            findings.extend(report.findings)
            reports.append(report)
            continue
        report = evaluate_actuator_case(
            contract,
            case.id,
            evidence_ref,
            evidence_root=evidence_root,
            contract_sha256=contract_sha256,
        )
        findings.extend(report.findings)
        reports.append(report)
    return findings, reports


def _is_raw_sha256(value: str | None) -> bool:
    return bool(value) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


# ---------------------------------------------------------------- ownership


def _actuator_gap(
    case: ActuatorCase,
    name: str,
    observed: str,
    expected: str,
    *,
    provenance: Provenance | None = None,
) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"actuator_cases/{case.id}/{name}",
        observed=observed,
        expected=expected,
        action="supply the declared, independently admissible evidence; do not estimate it",
        source=provenance.source if provenance is not None else None,
        witness=provenance.witness if provenance is not None else None,
    )


def _actuator_violation(
    case: ActuatorCase,
    name: str,
    observed: str,
    expected: str,
    *,
    observed_quantity: Quantity | None = None,
    expected_quantity: Quantity | None = None,
    provenance: Provenance | None = None,
) -> Finding:
    return Finding(
        severity="violation",
        quantity=f"actuator_cases/{case.id}/{name}",
        observed=observed,
        expected=expected,
        observed_quantity=observed_quantity,
        expected_quantity=expected_quantity,
        action="correct the declared command, hardware condition, or model before rerunning",
        source=provenance.source if provenance is not None else None,
        witness=provenance.witness if provenance is not None else None,
    )


def _bound_observation(
    case: ActuatorCase, evidence: ActuatorCaseEvidence, binding_name: str, *, physical: bool
) -> tuple[object | None, Finding | None]:
    binding = getattr(case.bindings, binding_name)
    if binding is None:
        return None, _actuator_gap(case, binding_name, "binding missing", "declared signal binding")
    observation = find_signal(evidence.signals, binding.signal)
    if observation is None:
        return None, _actuator_gap(case, binding_name, "signal missing", binding.signal)
    if (
        observation.value.unit != binding.unit
        or observation.provenance_class != binding.provenance_class
        or (physical and observation.synthetic)
        or (binding.frame is not None and observation.frame != binding.frame)
        or (
            observation.value.frame is not None
            and observation.frame is not None
            and observation.value.frame != observation.frame
        )
    ):
        return None, _actuator_gap(
            case,
            binding_name,
            f"signal {observation.signal!r}: unit={observation.value.unit!r}, "
            f"frame={observation.frame!r}, value_frame={observation.value.frame!r}, "
            f"provenance={observation.provenance_class!r}, synthetic={observation.synthetic}",
            f"binding {binding.signal!r}: unit={binding.unit!r}, frame={binding.frame!r}, "
            f"provenance={binding.provenance_class!r}, "
            f"synthetic={'False' if physical else 'allowed'}",
            provenance=observation.provenance,
        )
    if physical and (
        observation.provenance is None
        or not observation.provenance.source.strip()
        or not observation.provenance.witness.strip()
    ):
        return None, _actuator_gap(
            case,
            binding_name,
            "measurement provenance unknown: source or witness is not recorded",
            "explicit recorded measurement source and witness",
        )
    return observation, None


def _clock_pair(
    evidence: ActuatorCaseEvidence,
    left_ref: ArtifactRef | None,
    right_ref: ArtifactRef | None,
) -> tuple[ClockMapping | None, ClockMapping | None]:
    return (
        evidence.resolved_clock_mapping(left_ref),
        evidence.resolved_clock_mapping(right_ref),
    )


def _clock_pair_is_comparable(left: ClockMapping, right: ClockMapping) -> bool:
    return (
        left.target_clock_domain == right.target_clock_domain
        and left.target_epoch == right.target_epoch
    )


def _sample_index(
    samples: list[PredictionSample],
) -> dict[tuple[str, int], PredictionSample] | None:
    indexed: dict[tuple[str, int], PredictionSample] = {}
    completed_epochs: set[str] = set()
    current_epoch: str | None = None
    previous_sequence = -1
    for sample in samples:
        if sample.sequence_epoch != current_epoch:
            if sample.sequence_epoch in completed_epochs:
                return None
            if current_epoch is not None:
                completed_epochs.add(current_epoch)
            current_epoch = sample.sequence_epoch
            previous_sequence = -1
        key = (sample.sequence_epoch, sample.sequence)
        if sample.sequence <= previous_sequence or key in indexed:
            return None
        indexed[key] = sample
        previous_sequence = sample.sequence
    return indexed


def _dc_observation(evidence: ActuatorCaseEvidence, role: str) -> DCDeliveryObservation | None:
    matches = [row for row in evidence.dc_delivery if row.role == role]
    return matches[0] if len(matches) == 1 else None


def _check_dc_delivery(
    model: object, case: ActuatorCase, evidence: ActuatorCaseEvidence
) -> list[Finding]:
    target = case.dc_delivery
    if target is None:
        return []

    voltage_range = getattr(model, "input_voltage_range", None)
    if voltage_range is None:
        return [
            _actuator_gap(
                case,
                "dc_delivery/receiver_model",
                "receiver input minimum is absent",
                "declared receiver model input minimum",
            )
        ]

    resistance = sum(
        getattr(target, name).base()
        for name in (
            "source_resistance_max",
            "outbound_resistance_max",
            "return_resistance_max",
            "contact_resistance_max",
        )
    )
    calculated = target.source_voltage_min.base() - target.peak_current_max.base() * resistance
    findings: list[Finding] = []
    if calculated < voltage_range.min.base():
        findings.append(
            _actuator_violation(
                case,
                "dc_delivery/source_path",
                f"{target.source_voltage_min.base():g} V - "
                f"{target.peak_current_max.base():g} A * {resistance:g} ohm = "
                f"{calculated:g} V at the receiver",
                f">= {voltage_range.min.base():g} V receiver input minimum",
            )
        )

    bindings = {
        "source_voltage": case.bindings.source_voltage,
        "receiver_voltage": case.bindings.receiver_voltage,
        "load_current": case.bindings.load_current,
    }
    if any(binding is None for binding in bindings.values()):
        findings.append(
            _actuator_gap(
                case,
                "dc_delivery/bindings",
                "source, receiver, or current binding is missing",
                "all three explicitly named DC observation bindings",
            )
        )
        return findings

    observations = {role: _dc_observation(evidence, role) for role in bindings}
    if any(row is None for row in observations.values()):
        findings.append(
            _actuator_gap(
                case,
                "dc_delivery/evidence",
                "exactly one source, receiver, and current observation is required",
                "three synchronized role-labelled observations",
            )
        )
        return findings

    for role, row in observations.items():
        binding = bindings[role]
        assert row is not None and binding is not None
        if (
            row.signal != binding.signal
            or row.value.unit != binding.unit
            or row.uncertainty.unit != binding.unit
            or row.provenance_class != binding.provenance_class
            or row.provenance_class != "measured"
            or row.synthetic
        ):
            findings.append(
                _actuator_gap(
                    case,
                    f"dc_delivery/{role}",
                    "binding, unit, uncertainty, provenance, or physical origin mismatches",
                    "the declared binding with measured non-synthetic evidence",
                )
            )
            return findings
        if (
            row.applicability != target.applicability
            or row.condition_id != target.condition_id
            or row.source_node != target.source_node
            or row.receiver_node != target.receiver_node
            or row.return_node != target.return_node
        ):
            findings.append(
                _actuator_gap(
                    case,
                    "dc_delivery/condition",
                    "non-series, transient, or endpoint/condition-mismatched evidence",
                    "the declared steady single-series path, condition, and endpoints",
                )
            )
            return findings

    rows = [observations[role] for role in bindings]
    assert all(row is not None for row in rows)
    if (
        len({row.source_time for row in rows}) != 1
        or len({row.bandwidth.base() for row in rows}) != 1
    ):
        findings.append(
            _actuator_gap(
                case,
                "dc_delivery/synchronization",
                "source time or measurement bandwidth differs across observations",
                "one synchronized source/receiver/current observation set",
            )
        )
        return findings

    source = observations["source_voltage"]
    receiver = observations["receiver_voltage"]
    current = observations["load_current"]
    assert source is not None and receiver is not None and current is not None
    source_lower = source.value.base() - source.uncertainty.base()
    current_upper = abs(current.value.base()) + current.uncertainty.base()
    if source_lower < target.source_voltage_min.base():
        findings.append(
            _actuator_violation(
                case,
                "dc_delivery/source_observation",
                f"{source_lower:g} V measured lower bound",
                f">= {target.source_voltage_min.base():g} V declared source minimum",
            )
        )
    if current_upper > target.peak_current_max.base():
        findings.append(
            _actuator_gap(
                case,
                "dc_delivery/current_condition",
                f"{current_upper:g} A measured upper bound exceeds the declared case",
                f"<= {target.peak_current_max.base():g} A peak-current condition",
            )
        )
        return findings
    receiver_lower = receiver.value.base() - receiver.uncertainty.base()
    if receiver_lower < voltage_range.min.base():
        findings.append(
            _actuator_violation(
                case,
                "dc_delivery/receiver_observation",
                f"{receiver_lower:g} V measured receiver lower bound "
                f"at {source_lower:g} V source and {current_upper:g} A load bounds",
                f">= {voltage_range.min.base():g} V receiver input minimum",
            )
        )
    return findings


def _check_transient_dc_binding(
    case: ActuatorCase, evidence: ActuatorCaseEvidence
) -> list[Finding]:
    """Join one explicitly-bound load sample to its command/feedback response.

    The opt-in binding is deliberately observational: it compares only declared
    limits and mapped evidence identities, and does not apply the steady-path
    resistance model to a transient.
    """
    target = case.transient_dc
    if case.transient_dc_agreement_required is not True:
        return []
    if target is None:
        return [
            _actuator_gap(
                case,
                "transient_dc/binding",
                "binding absent",
                "declared transient evidence binding",
            )
        ]
    declaration = (
        target.applicability,
        target.channel,
        target.part,
        target.supply_rail,
        target.source_node,
        target.receiver_node,
        target.return_node,
        target.time_basis_clock_domain,
        target.time_basis_epoch,
        target.voltage_min,
        target.current_max,
        target.response_latency_max,
        target.evidence_artifact,
    )
    if any(item is None for item in declaration) or not target.provenance_artifacts:
        return [
            _actuator_gap(
                case,
                "transient_dc/declaration",
                "binding identity, limit, evidence artifact, or provenance is absent",
                "complete declared transient binding and limits",
            )
        ]
    rows = {
        role: _dc_observation(evidence, role)
        for role in ("source_voltage", "receiver_voltage", "load_current")
    }
    if any(row is None for row in rows.values()):
        return [
            _actuator_gap(
                case,
                "transient_dc/evidence",
                "exactly one source, receiver, and current observation is required",
                "three hash-bound transient observations",
            )
        ]
    observations = list(rows.values())
    assert all(row is not None for row in observations)
    first = observations[0]
    assert first is not None
    expected_identity = (
        target.applicability,
        target.channel,
        target.part,
        target.supply_rail,
        target.source_node,
        target.receiver_node,
        target.return_node,
    )
    for row in observations:
        assert row is not None
        identity = (
            row.applicability,
            row.channel,
            row.part,
            row.supply_rail,
            row.source_node,
            row.receiver_node,
            row.return_node,
        )
        if (
            identity != expected_identity
            or row.sequence is None
            or row.sequence_epoch is None
            or row.receive_time is None
            or row.clock_mapping is None
            or (row.artifact.path, row.artifact.sha256)
            != (target.evidence_artifact.path, target.evidence_artifact.sha256)
            or row.provenance_class != "measured"
            or row.synthetic
        ):
            return [
                _actuator_gap(
                    case,
                    "transient_dc/identity",
                    "channel, part, supply, timing identity, hash, or provenance mismatches",
                    "the declared measured transient binding",
                )
            ]
    if any(
        (row.sequence, row.sequence_epoch) != (first.sequence, first.sequence_epoch)
        for row in observations
    ):
        return [
            _actuator_gap(
                case,
                "transient_dc/sequence",
                "DC observations do not share one sequence and epoch",
                "one command/feedback sequence and epoch",
            )
        ]
    command = next(
        (
            row
            for row in evidence.events
            if (
                row.event == case.bindings.command_issued_event.event
                and row.sequence == first.sequence
                and row.sequence_epoch == first.sequence_epoch
            )
        ),
        None,
    )
    feedback = next(
        (
            row
            for row in evidence.events
            if (
                row.event == case.bindings.feedback_event.event
                and row.sequence == first.sequence
                and row.sequence_epoch == first.sequence_epoch
            )
        ),
        None,
    )
    if (
        command is None
        or feedback is None
        or command.source_time is None
        or feedback.source_time is None
        or command.receive_time is None
        or feedback.receive_time is None
        or command.clock_mapping is None
        or feedback.clock_mapping is None
        or command.provenance_class != "measured"
        or feedback.provenance_class != "measured"
        or command.synthetic
        or feedback.synthetic
        or (command.artifact.path, command.artifact.sha256)
        != (target.evidence_artifact.path, target.evidence_artifact.sha256)
        or (feedback.artifact.path, feedback.artifact.sha256)
        != (target.evidence_artifact.path, target.evidence_artifact.sha256)
    ):
        return [
            _actuator_gap(
                case,
                "transient_dc/timing",
                "joined command or feedback lacks measured hash-bound source/receipt timing or mapping",
                "measured hash-bound command and feedback for the DC observation sequence",
            )
        ]
    command_clock, feedback_clock = _clock_pair(
        evidence, command.clock_mapping, feedback.clock_mapping
    )
    dc_clock = evidence.resolved_clock_mapping(first.clock_mapping)
    if (
        command_clock is None
        or feedback_clock is None
        or dc_clock is None
        or any(
            (clock.target_clock_domain, clock.target_epoch)
            != (target.time_basis_clock_domain, target.time_basis_epoch)
            for clock in (command_clock, feedback_clock, dc_clock)
        )
        or command_clock.max_error.base() + feedback_clock.max_error.base()
        > case.clock_alignment_max_error.base()
        or command_clock.max_error.base() + dc_clock.max_error.base()
        > case.clock_alignment_max_error.base()
        or feedback_clock.max_error.base() + dc_clock.max_error.base()
        > case.clock_alignment_max_error.base()
    ):
        return [
            _actuator_gap(
                case,
                "transient_dc/clock_mapping",
                "clock mappings are missing, incompatible, or exceed the declared alignment budget",
                "declared mappings into the transient binding time basis",
            )
        ]
    command_time = command_clock.map_source_time(command.source_time)
    feedback_time = feedback_clock.map_source_time(feedback.source_time)
    dc_time = dc_clock.map_source_time(first.source_time)
    if not all(math.isfinite(value) for value in (command_time, feedback_time, dc_time)):
        return [
            _actuator_gap(
                case,
                "transient_dc/clock_mapping",
                "mapped source time is non-finite",
                "finite source times under declared mappings",
            )
        ]
    if (
        dc_time + dc_clock.max_error.base() < command_time - command_clock.max_error.base()
        or dc_time - dc_clock.max_error.base() > feedback_time + feedback_clock.max_error.base()
    ):
        return [
            _actuator_gap(
                case,
                "transient_dc/association",
                "DC sample is outside the command-to-feedback interval",
                "a mapped sample within the declared response interval",
            )
        ]
    receiver = rows["receiver_voltage"]
    current = rows["load_current"]
    assert receiver is not None and current is not None
    findings: list[Finding] = []
    receiver_lower = receiver.value.base() - receiver.uncertainty.base()
    if receiver_lower < target.voltage_min.base():
        findings.append(
            _actuator_violation(
                case,
                "transient_dc/receiver_voltage",
                f"{receiver_lower:g} V measured receiver lower bound",
                f">= {target.voltage_min}",
            )
        )
    current_upper = abs(current.value.base()) + current.uncertainty.base()
    if current_upper > target.current_max.base():
        findings.append(
            _actuator_violation(
                case,
                "transient_dc/load_current",
                f"{current_upper:g} A measured current upper bound",
                f"<= {target.current_max}",
            )
        )
    elapsed = feedback_time - command_time
    uncertainty = command_clock.max_error.base() + feedback_clock.max_error.base()
    if elapsed < 0 or elapsed + uncertainty > target.response_latency_max.base():
        findings.append(
            _actuator_violation(
                case,
                "transient_dc/response_latency",
                f"{elapsed:g} s plus {uncertainty:g} s declared mapping error",
                f"<= {target.response_latency_max}",
            )
        )
    return findings


def _event_stream_index(rows: list[object], event: str) -> dict[tuple[str, int], object] | None:
    indexed: dict[tuple[str, int], object] = {}
    completed_epochs: set[str] = set()
    current_epoch: str | None = None
    previous_sequence = -1
    for row in (item for item in rows if getattr(item, "event", None) == event):
        epoch = row.sequence_epoch
        if epoch != current_epoch:
            if epoch in completed_epochs:
                return None
            if current_epoch is not None:
                completed_epochs.add(current_epoch)
            current_epoch = epoch
            previous_sequence = -1
        key = (epoch, row.sequence)
        if row.sequence <= previous_sequence or key in indexed:
            return None
        indexed[key] = row
        previous_sequence = row.sequence
    return indexed


def _check_event_timeline(case: ActuatorCase, evidence: ActuatorCaseEvidence) -> list[Finding]:
    command_binding = case.bindings.command_issued_event
    feedback_binding = case.bindings.feedback_event
    commands = _event_stream_index(evidence.events, command_binding.event)
    feedback = _event_stream_index(evidence.events, feedback_binding.event)
    if commands is None or feedback is None or not commands:
        return [
            _actuator_gap(
                case,
                "feedback_timeline",
                "duplicate, nonmonotonic, reused-epoch, or absent command timing",
                "unique monotonic command and feedback sequences per declared epoch",
            )
        ]

    captures: dict[tuple[str, str], object] = {}
    for capture in evidence.event_captures:
        key = (capture.event, capture.epoch)
        expected_class = (
            command_binding.provenance_class
            if capture.event == command_binding.event
            else feedback_binding.provenance_class
            if capture.event == feedback_binding.event
            else None
        )
        if (
            expected_class is None
            or key in captures
            or not capture.complete
            or capture.provenance_class != expected_class
            or capture.provenance_class != "measured"
            or capture.synthetic
        ):
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/coverage",
                    "capture coverage is absent, duplicate, incomplete, synthetic, or mismatched",
                    "one complete measured capture for each event and sequence epoch",
                )
            ]
        captures[key] = capture

    for rows, binding in ((commands, command_binding), (feedback, feedback_binding)):
        for (epoch, sequence), row in rows.items():
            capture = captures.get((binding.event, epoch))
            if (
                capture is None
                or not capture.first_sequence <= sequence <= capture.last_sequence
                or row.provenance_class != binding.provenance_class
                or row.provenance_class != "measured"
                or row.synthetic
                or row.source_time is None
                or row.receive_time is None
                or row.clock_mapping is None
            ):
                return [
                    _actuator_gap(
                        case,
                        "feedback_timeline/evidence",
                        "event is outside complete coverage or lacks measured source/receive time",
                        "measured non-synthetic raw source and receive times within declared capture coverage",
                    )
                ]

    command_epochs = {epoch for event, epoch in captures if event == command_binding.event}
    feedback_epochs = {epoch for event, epoch in captures if event == feedback_binding.event}
    if command_epochs != feedback_epochs:
        return [
            _actuator_gap(
                case,
                "feedback_timeline/coverage",
                "command and feedback captures do not name the same sequence epochs",
                "one matched complete capture pair for every declared sequence epoch",
            )
        ]
    for epoch in command_epochs:
        command_capture = captures.get((command_binding.event, epoch))
        feedback_capture = captures.get((feedback_binding.event, epoch))
        if (
            command_capture is None
            or feedback_capture is None
            or command_capture.first_sequence != feedback_capture.first_sequence
            or command_capture.last_sequence != feedback_capture.last_sequence
        ):
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/coverage",
                    "command and feedback coverage windows do not match",
                    "complete measured coverage of the same declared epochs and sequence windows",
                )
            ]
        present_sequences = {
            sequence for row_epoch, sequence in set(commands).union(feedback) if row_epoch == epoch
        }
        declared_count = command_capture.last_sequence - command_capture.first_sequence + 1
        if len(present_sequences) != declared_count:
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/coverage",
                    f"{len(present_sequences)} sequence position(s) are present in a "
                    f"{declared_count}-position complete capture",
                    "at least one command or feedback event at every sequence position",
                )
            ]

    if set(feedback) - set(commands):
        return [
            _actuator_gap(
                case,
                "feedback_timeline",
                "feedback sequence has no command in the complete command capture",
                "feedback joined only to a command with the same epoch and sequence",
            )
        ]

    mapping_errors: dict[tuple[str, int], tuple[float, float]] = {}
    for key in set(commands).intersection(feedback):
        command = commands[key]
        response = feedback[key]
        command_clock, feedback_clock = _clock_pair(
            evidence, command.clock_mapping, response.clock_mapping
        )
        if (
            command_clock is None
            or feedback_clock is None
            or not _clock_pair_is_comparable(command_clock, feedback_clock)
            or command_clock.max_error.base() + feedback_clock.max_error.base()
            > case.clock_alignment_max_error.base()
        ):
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/clock_mapping",
                    "clock mapping is missing, incompatible, or exceeds the alignment budget; "
                    f"command mapping: {command_clock.model_dump_json() if command_clock else 'missing'}; "
                    f"feedback mapping: {feedback_clock.model_dump_json() if feedback_clock else 'missing'}",
                    "compatible declared mappings for both raw source times; "
                    f"combined mapping error <= {case.clock_alignment_max_error}",
                )
            ]
        command_time = command_clock.map_source_time(command.source_time)
        feedback_time = feedback_clock.map_source_time(response.source_time)
        if not math.isfinite(command_time) or not math.isfinite(feedback_time):
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/clock_mapping",
                    "mapped source time is non-finite",
                    "finite raw source times under declared mappings",
                )
            ]
        elapsed = feedback_time - command_time
        if elapsed < 0:
            return [
                _actuator_gap(
                    case,
                    "feedback_timeline/clock_mapping",
                    "mapped feedback precedes its command",
                    "nonnegative elapsed time for every joined event pair",
                )
            ]
        mapping_errors[key] = (
            elapsed,
            command_clock.max_error.base() + feedback_clock.max_error.base(),
        )

    findings: list[Finding] = []
    missing = set(commands) - set(feedback)
    if len(missing) > case.feedback_loss_max:
        findings.append(
            _actuator_violation(
                case,
                "feedback_loss",
                f"{len(missing)} command(s) lack feedback in complete capture",
                f"<= {case.feedback_loss_max} lost feedback event(s)",
            )
        )
    for key, (elapsed, mapping_error) in mapping_errors.items():
        if elapsed + mapping_error > case.feedback_deadline_max.base():
            findings.append(
                _actuator_violation(
                    case,
                    "feedback_deadline",
                    f"epoch {key[0]!r} sequence {key[1]}: {elapsed:g} s plus "
                    f"{mapping_error:g} s declared mapping error",
                    f"<= {case.feedback_deadline_max}",
                )
            )
    return findings


def check_actuator_case_conformance(
    contract: Contract,
    case: ActuatorCase,
    evidence: ActuatorCaseEvidence,
    *,
    comparisons: list[ScoredRecord] | None = None,
) -> list[Finding]:
    """Check one selected case.  It is intentionally not a global record check."""

    findings: list[Finding] = []

    def passed(name: str, observation: SignalObservation, expected: str, margin: str) -> None:
        if comparisons is not None:
            comparisons.append(
                ScoredRecord(
                    quantity=f"actuator_cases/{case.id}/{name}",
                    observed=str(observation.value),
                    expected=expected,
                    margin=margin,
                    observed_quantity=Quantity(
                        value=observation.value.value,
                        unit=observation.value.unit,
                        frame=(
                            observation.frame
                            if observation.frame is not None
                            else observation.value.frame
                        ),
                    ),
                    provenance=observation.provenance,
                    uncertainty=observation.uncertainty,
                )
            )

    joint = (
        next((item for item in contract.model.joints if item.name == case.joint), None)
        if contract.model
        else None
    )
    device = contract.device(case.actuator_device)
    model = contract.device_model(device.model) if device else None
    rail = contract.rail(case.rail)
    if joint is None or device is None or model is None or rail is None:
        return [
            _actuator_gap(
                case, "identity", "case references are unresolved", "declared device, joint, rail"
            )
        ]

    command_position, gap = _bound_observation(case, evidence, "command_position", physical=False)
    if gap:
        findings.append(gap)
    elif joint.limits is None or joint.limits.lower is None or joint.limits.upper is None:
        findings.append(
            _actuator_gap(
                case,
                "command_conformance/position",
                "joint bounds absent",
                "declared joint position bounds",
            )
        )
    elif not joint.limits.lower <= command_position.value.base() <= joint.limits.upper:
        findings.append(
            _actuator_violation(
                case,
                "command_conformance/position",
                str(command_position.value),
                _with_enforcement_provenance(
                    "within declared joint bounds",
                    ("lower", joint.limits.lower_enforced_by),
                    ("upper", joint.limits.upper_enforced_by),
                ),
            )
        )

    command_velocity, gap = _bound_observation(case, evidence, "command_velocity", physical=False)
    if gap:
        findings.append(gap)
    elif joint.limits is None or joint.limits.velocity is None:
        findings.append(
            _actuator_gap(
                case,
                "command_conformance/velocity",
                "joint velocity bound absent",
                "declared joint velocity bound",
            )
        )
    elif abs(command_velocity.value.base()) > joint.limits.velocity:
        findings.append(
            _actuator_violation(
                case,
                "command_conformance/velocity",
                str(command_velocity.value),
                _with_enforcement_provenance(
                    "within declared joint velocity bound",
                    ("velocity", joint.limits.velocity_enforced_by),
                ),
            )
        )

    command_torque, gap = _bound_observation(case, evidence, "command_torque", physical=False)
    if gap:
        findings.append(gap)
    elif joint.limits is None or joint.limits.effort is None:
        findings.append(
            _actuator_gap(
                case,
                "command_conformance/torque",
                "joint effort bound absent",
                "declared joint effort bound",
            )
        )
    elif joint.limits.effort_limit is None:
        findings.append(
            unusable_effort_finding(
                f"actuator_cases/{case.id}/command_conformance/torque", joint.limits.effort
            )
        )
    elif abs(command_torque.value.base()) > joint.limits.effort_limit:
        findings.append(
            _actuator_violation(
                case,
                "command_conformance/torque",
                str(command_torque.value),
                _with_enforcement_provenance(
                    "within declared joint effort bound",
                    ("effort", joint.limits.effort_enforced_by),
                ),
            )
        )

    measured_position, position_gap = _bound_observation(
        case, evidence, "measured_position", physical=True
    )
    measured_torque, torque_gap = _bound_observation(
        case, evidence, "measured_joint_torque", physical=True
    )
    receiver_voltage, voltage_gap = _bound_observation(
        case, evidence, "receiver_voltage", physical=True
    )
    findings.extend(gap for gap in (position_gap, torque_gap, voltage_gap) if gap is not None)
    load = evidence.independent_load
    binding = case.bindings.independent_load
    if (
        load is None
        or load.signal != binding.signal
        or load.value.unit != binding.unit
        or load.provenance_class != "measured"
        or load.synthetic
        or load.joint != binding.joint
        or load.frame != binding.frame
        or load.sign != binding.sign
        or load.application_point != binding.application_point
        or load.axis != binding.axis
        or load.applicability != binding.applicability
        or load.calibration is None
        or load.uncertainty is None
        or load.uncertainty.unit != "N*m"
    ):
        findings.append(
            _actuator_gap(
                case,
                "physical_load",
                "independent calibrated load is missing or mismatched",
                "declared joint/frame/sign/point/axis/context with uncertainty",
            )
        )

    if measured_position is not None and (
        measured_position.uncertainty is None or measured_position.uncertainty.unit != "rad"
    ):
        findings.append(
            _actuator_gap(
                case,
                "physical_position",
                "position uncertainty missing or mismatched",
                "declared rad uncertainty",
            )
        )
        measured_position = None
    if measured_torque is not None and (
        measured_torque.joint != case.joint
        or measured_torque.uncertainty is None
        or measured_torque.uncertainty.unit != "N*m"
    ):
        findings.append(
            _actuator_gap(
                case,
                "physical_torque",
                "joint torque identity or uncertainty missing",
                "declared joint and N*m uncertainty",
            )
        )
        measured_torque = None
    if measured_position is not None and command_position is not None:
        error = abs(measured_position.value.base() - command_position.value.base())
        uncertainty = measured_position.uncertainty.base()
        if error + uncertainty > case.position_tracking_max_error.base():
            findings.append(
                _actuator_violation(
                    case,
                    "physical_position",
                    f"{error:g} rad plus uncertainty",
                    f"<= {case.position_tracking_max_error}",
                    observed_quantity=Quantity(value=error, unit="rad"),
                    expected_quantity=case.position_tracking_max_error,
                    provenance=measured_position.provenance,
                )
            )
        else:
            passed(
                "physical_position",
                measured_position,
                str(command_position.value),
                f"tracking error {error:g} rad plus declared uncertainty <= {case.position_tracking_max_error}",
            )
    if measured_torque is not None:
        uncertainty = measured_torque.uncertainty.base()
        if joint.limits is None or joint.limits.effort is None:
            findings.append(
                _actuator_gap(
                    case,
                    "physical_torque",
                    "joint effort bound absent",
                    "declared joint effort bound",
                )
            )
        elif joint.limits.effort_limit is None:
            findings.append(
                unusable_effort_finding(
                    f"actuator_cases/{case.id}/physical_torque", joint.limits.effort
                )
            )
        elif abs(measured_torque.value.base()) + uncertainty > joint.limits.effort_limit:
            findings.append(
                _actuator_violation(
                    case,
                    "physical_torque",
                    str(measured_torque.value),
                    _with_enforcement_provenance(
                        f"<= {joint.limits.effort:g} N*m",
                        ("effort", joint.limits.effort_enforced_by),
                    ),
                    provenance=measured_torque.provenance,
                )
            )
        else:
            passed(
                "physical_torque",
                measured_torque,
                f"<= {joint.limits.effort:g} N*m",
                "within declared effort bound including uncertainty",
            )
    if receiver_voltage is not None:
        voltage_range = model.input_voltage_range
        if voltage_range is None:
            findings.append(
                _actuator_gap(
                    case,
                    "receiver_voltage",
                    "receiver voltage range absent",
                    "declared receiver voltage range",
                )
            )
        elif (
            not voltage_range.min.base()
            <= receiver_voltage.value.base()
            <= voltage_range.max.base()
        ):
            expected_bound = (
                voltage_range.min
                if receiver_voltage.value.base() < voltage_range.min.base()
                else voltage_range.max
            )
            findings.append(
                _actuator_violation(
                    case,
                    "receiver_voltage",
                    str(receiver_voltage.value),
                    _with_enforcement_provenance(
                        f"within {voltage_range.min}-{voltage_range.max}",
                        ("min", voltage_range.min_enforced_by),
                        ("max", voltage_range.max_enforced_by),
                    ),
                    observed_quantity=receiver_voltage.value,
                    expected_quantity=expected_bound,
                    provenance=receiver_voltage.provenance,
                )
            )
        else:
            passed(
                "receiver_voltage",
                receiver_voltage,
                f"within {voltage_range.min}-{voltage_range.max}",
                "within declared receiver voltage range",
            )

    findings.extend(_check_dc_delivery(model, case, evidence))
    findings.extend(_check_transient_dc_binding(case, evidence))
    if case.feedback_loss_max is not None:
        findings.extend(_check_event_timeline(case, evidence))
        return findings

    command_event = find_event(evidence.events, case.bindings.command_issued_event.event)
    feedback_event = find_event(evidence.events, case.bindings.feedback_event.event)
    command_clock, feedback_clock = (
        _clock_pair(evidence, command_event.clock_mapping, feedback_event.clock_mapping)
        if command_event is not None and feedback_event is not None
        else (None, None)
    )
    if (
        command_event is None
        or feedback_event is None
        or command_event.synthetic
        or feedback_event.synthetic
        or command_event.provenance_class != case.bindings.command_issued_event.provenance_class
        or feedback_event.provenance_class != case.bindings.feedback_event.provenance_class
        or command_event.source_time is None
        or feedback_event.source_time is None
        or command_clock is None
        or feedback_clock is None
        or command_event.sequence != feedback_event.sequence
        or command_event.sequence_epoch != feedback_event.sequence_epoch
        or not _clock_pair_is_comparable(command_clock, feedback_clock)
        or command_clock.max_error.base() + feedback_clock.max_error.base()
        > case.clock_alignment_max_error.base()
    ):
        findings.append(
            _actuator_gap(
                case,
                "feedback_deadline",
                "receive-time-only or unaligned timing evidence; "
                f"command mapping: {command_clock.model_dump_json() if command_clock else 'missing'}; "
                f"feedback mapping: {feedback_clock.model_dump_json() if feedback_clock else 'missing'}; "
                f"command source_time={command_event.source_time!r}, sequence={command_event.sequence!r}, "
                f"epoch={command_event.sequence_epoch!r}; feedback source_time={feedback_event.source_time!r}, "
                f"sequence={feedback_event.sequence!r}, epoch={feedback_event.sequence_epoch!r}",
                "declared mapped source times with matching sequence and epoch; "
                f"combined mapping error <= {case.clock_alignment_max_error}",
            )
        )
    else:
        command_time = command_clock.map_source_time(command_event.source_time)
        feedback_time = feedback_clock.map_source_time(feedback_event.source_time)
        if not math.isfinite(command_time) or not math.isfinite(feedback_time):
            findings.append(
                _actuator_gap(
                    case,
                    "feedback_deadline",
                    "mapped source time is non-finite",
                    "finite source times under the declared clock mappings",
                )
            )
            return findings
        elapsed = feedback_time - command_time
        mapping_error = command_clock.max_error.base() + feedback_clock.max_error.base()
        if elapsed < 0 or elapsed + mapping_error > case.feedback_deadline_max.base():
            findings.append(
                _actuator_violation(
                    case,
                    "feedback_deadline",
                    f"{elapsed:g} s plus {mapping_error:g} s declared mapping error",
                    f"<= {case.feedback_deadline_max}",
                )
            )
    return findings


def check_actuator_case_simulation_validity(
    case: ActuatorCase,
    evidence: ActuatorCaseEvidence,
    *,
    contract_sha256: str,
    comparisons: list[ScoredRecord] | None = None,
) -> list[Finding]:
    """Compare only a bound prediction sidecar with held-out physical reference data."""

    prediction, reference = evidence.prediction, evidence.validation_reference
    if prediction is None or reference is None:
        return [
            _actuator_gap(
                case,
                "simulation_validity",
                "prediction or reference missing",
                "bound prediction and held-out physical reference",
            )
        ]
    expected_parameters = {item.sha256 for item in case.parameter_artifacts}
    expected_calibrations = {item.sha256 for item in case.calibration_artifacts}
    if (
        prediction.contract_sha256 != contract_sha256
        or prediction.case_id != case.id
        or prediction.model_input_manifest_sha256 != case.model_input_manifest.sha256
        or set(prediction.parameter_hashes) != expected_parameters
        or set(prediction.calibration_hashes) != expected_calibrations
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/identity",
                "prediction identity is not bound to this contract and case; "
                f"contract_sha256={prediction.contract_sha256!r}, case_id={prediction.case_id!r}, "
                f"manifest_sha256={prediction.model_input_manifest_sha256!r}, "
                f"parameter_hashes={sorted(prediction.parameter_hashes)!r}, "
                f"calibration_hashes={sorted(prediction.calibration_hashes)!r}",
                f"contract_sha256={contract_sha256!r}, case_id={case.id!r}, "
                f"manifest_sha256={case.model_input_manifest.sha256!r}, "
                f"parameter_hashes={sorted(expected_parameters)!r}, "
                f"calibration_hashes={sorted(expected_calibrations)!r}",
            )
        ]
    if (
        reference.provenance_class != "measured"
        or reference.synthetic
        or not reference.held_out
        or reference.artifact.sha256 == prediction.artifact.sha256
        or reference.artifact.sha256 in expected_calibrations
        or set(reference.calibration_hashes).intersection(expected_calibrations)
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/reference",
                "reference is synthetic, fitted, or unaligned",
                "distinct held-out measured reference with valid clock mapping",
            )
        ]
    prediction_clock, reference_clock = _clock_pair(
        evidence, prediction.clock_mapping, reference.clock_mapping
    )
    if (
        prediction_clock is None
        or reference_clock is None
        or not _clock_pair_is_comparable(prediction_clock, reference_clock)
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/clock_mapping",
                "clock mapping is missing or targets a different clock domain or epoch; "
                f"prediction mapping: {prediction_clock.model_dump_json() if prediction_clock else 'missing'}; "
                f"reference mapping: {reference_clock.model_dump_json() if reference_clock else 'missing'}",
                "contract-declared mappings into one exact target clock domain and epoch"
                + (
                    f"; match prediction target domain={prediction_clock.target_clock_domain!r}, "
                    f"epoch={prediction_clock.target_epoch!r}"
                    if prediction_clock
                    else ""
                ),
            )
        ]
    predicted = _sample_index(prediction.samples)
    observed = _sample_index(reference.samples)
    if (
        not predicted
        or not observed
        or set(predicted) != set(observed)
        or any(sample.source_time is None for sample in predicted.values())
        or any(sample.source_time is None for sample in observed.values())
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/alignment",
                "prediction/reference sequence, epoch, or source time is missing or ambiguous",
                "one unique monotonic sequence in an explicitly named epoch",
            )
        ]
    mapping_error = prediction_clock.max_error.base() + reference_clock.max_error.base()
    mapped_pairs = [
        (
            prediction_clock.map_source_time(predicted[key].source_time),
            reference_clock.map_source_time(observed[key].source_time),
        )
        for key in predicted
    ]
    if any(
        not math.isfinite(predicted_time)
        or not math.isfinite(observed_time)
        or abs(predicted_time - observed_time) + mapping_error
        > case.clock_alignment_max_error.base()
        for predicted_time, observed_time in mapped_pairs
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/source_time_alignment",
                "mapped source times exceed the declared alignment budget",
                "mapped source-time residual plus both mapping errors within the case limit",
            )
        ]
    errors = [
        abs(predicted[key].position.base() - observed[key].position.base()) for key in predicted
    ]
    if any(
        predicted[key].position.unit != "rad" or observed[key].position.unit != "rad"
        for key in predicted
    ):
        return [
            _actuator_gap(
                case,
                "simulation_validity/position",
                "position unit mismatch",
                "rad position samples",
            )
        ]
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    peak = max(errors)
    if comparisons is not None:
        for name, value, limit in (
            ("rmse", rmse, case.prediction_position_rmse_max),
            ("peak", peak, case.prediction_position_peak_max),
        ):
            if value <= limit.base():
                comparisons.append(
                    ScoredRecord(
                        quantity=f"actuator_cases/{case.id}/simulation_validity/{name}",
                        observed=f"{value:g} rad",
                        expected=f"<= {limit}",
                        margin="within declared prediction error; position uncertainty unknown",
                        observed_quantity=Quantity(value=value, unit="rad"),
                        expected_quantity=limit,
                    )
                )
    findings: list[Finding] = []
    if rmse > case.prediction_position_rmse_max.base():
        findings.append(
            _actuator_violation(
                case,
                "simulation_validity/rmse",
                f"{rmse:g} rad",
                f"<= {case.prediction_position_rmse_max}",
            )
        )
    if peak > case.prediction_position_peak_max.base():
        findings.append(
            _actuator_violation(
                case,
                "simulation_validity/peak",
                f"{peak:g} rad",
                f"<= {case.prediction_position_peak_max}",
            )
        )
    return findings


PAID_CHECKS["actuator_cases"] = _check_actuator_cases
PAID_CHECKS["condition_envelopes"] = check_condition_envelopes
