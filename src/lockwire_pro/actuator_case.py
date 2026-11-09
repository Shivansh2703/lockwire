"""Typed evidence sidecars for the bounded actuator case.

This module deliberately does not decide a claim.  ``checks.py`` consumes these
records through the existing Finding/Verdict machinery.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PrivateAttr, ValidationError, field_validator, model_validator

from lockwire.schema import (
    ActuatorCase,
    ArtifactRef,
    Base,
    Contract,
    Quantity,
    Provenance,
    TimeQuantity,
    read_artifact,
)
from lockwire.report import Finding, RecordReport, ScoredRecord, Verdict, verdict


class ClockMapping(Base):
    version: Literal["1"]
    id: str
    source_clock_domain: str
    source_epoch: str
    target_clock_domain: str
    target_epoch: str
    scale: Annotated[float, Field(strict=True)]
    offset: TimeQuantity
    max_error: TimeQuantity
    provenance_class: Literal["declared"]

    @field_validator(
        "id", "source_clock_domain", "source_epoch", "target_clock_domain", "target_epoch"
    )
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clock mapping identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def _finite_affine_mapping(self) -> "ClockMapping":
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("clock mapping scale must be finite and positive")
        if not math.isfinite(self.offset.base()):
            raise ValueError("clock mapping offset must be finite")
        if not math.isfinite(self.max_error.base()) or self.max_error.base() <= 0:
            raise ValueError("clock mapping max_error must be finite and positive")
        return self

    def map_source_time(self, source_time: float) -> float:
        return source_time * self.scale + self.offset.base()


class SignalObservation(Base):
    signal: str
    value: Quantity
    provenance_class: Literal["measured", "synthetic"]
    synthetic: bool = False
    frame: str | None = None
    joint: str | None = None
    uncertainty: Quantity | None = None
    artifact: ArtifactRef
    provenance: Provenance | None = None


class DCDeliveryObservation(Base):
    """One synchronized observation used to witness a declared DC path."""

    role: Literal["source_voltage", "receiver_voltage", "load_current"]
    signal: str
    value: Quantity
    uncertainty: Quantity
    applicability: str
    condition_id: str
    source_node: str
    receiver_node: str
    return_node: str
    source_time: float
    receive_time: float | None = None
    sequence: Annotated[int, Field(strict=True, ge=0)] | None = None
    sequence_epoch: str | None = None
    clock_mapping: ArtifactRef | None = None
    channel: str | None = None
    part: str | None = None
    supply_rail: str | None = None
    bandwidth: Quantity
    provenance_class: Literal["measured", "synthetic"]
    synthetic: bool = False
    artifact: ArtifactRef

    @field_validator(
        "signal", "applicability", "condition_id", "source_node", "receiver_node", "return_node"
    )
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DC observation identity fields must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_measurement(self) -> "DCDeliveryObservation":
        expected = "current" if self.role == "load_current" else "voltage"
        if self.value.dimension() != expected or self.uncertainty.dimension() != expected:
            raise ValueError(f"{self.role} value and uncertainty must have {expected} units")
        if self.uncertainty.base() < 0:
            raise ValueError("DC observation uncertainty must be nonnegative")
        if not math.isfinite(self.source_time):
            raise ValueError("DC observation source_time must be finite")
        if self.receive_time is not None and not math.isfinite(self.receive_time):
            raise ValueError("DC observation receive_time must be finite")
        if (self.sequence is None) != (self.sequence_epoch is None):
            raise ValueError("DC observation sequence and sequence_epoch must be supplied together")
        for value in (self.sequence_epoch, self.channel, self.part, self.supply_rail):
            if value is not None and not value.strip():
                raise ValueError("DC observation optional identity fields must not be blank")
        if self.bandwidth.dimension() != "rate" or self.bandwidth.base() <= 0:
            raise ValueError("DC observation bandwidth must be positive and use rate units")
        return self


class TimedEvent(Base):
    event: str
    sequence: Annotated[int, Field(strict=True, ge=0)]
    sequence_epoch: str
    source_time: float | None = None
    receive_time: float | None = None
    clock_mapping: ArtifactRef | None = None
    provenance_class: Literal["measured", "synthetic"]
    synthetic: bool = False
    artifact: ArtifactRef

    @model_validator(mode="after")
    def _valid_timeline(self) -> "TimedEvent":
        if not self.sequence_epoch.strip():
            raise ValueError("event sequence and sequence_epoch must name one monotonic epoch")
        for value in (self.source_time, self.receive_time):
            if value is not None and not math.isfinite(value):
                raise ValueError("event timestamps must be finite")
        return self


class EventCapture(Base):
    """Explicit completeness claim for one event stream and sequence epoch."""

    event: str
    epoch: str
    first_sequence: Annotated[int, Field(strict=True, ge=0)]
    last_sequence: Annotated[int, Field(strict=True, ge=0)]
    complete: bool
    provenance_class: Literal["measured", "synthetic"]
    synthetic: bool = False
    artifact: ArtifactRef

    @field_validator("event", "epoch")
    @classmethod
    def _nonempty_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event capture event and epoch must not be blank")
        return value

    @model_validator(mode="after")
    def _ordered_range(self) -> "EventCapture":
        if self.first_sequence > self.last_sequence:
            raise ValueError("event capture first_sequence must not exceed last_sequence")
        return self


class IndependentLoadObservation(SignalObservation):
    joint: str
    frame: str
    sign: Literal[-1, 1]
    application_point: tuple[float, float, float]
    axis: tuple[float, float, float]
    applicability: Literal["static", "dynamic"]
    calibration: ArtifactRef


class PredictionSample(Base):
    sequence: Annotated[int, Field(strict=True, ge=0)]
    sequence_epoch: str
    source_time: float | None = None
    position: Quantity

    @model_validator(mode="after")
    def _valid_timeline(self) -> "PredictionSample":
        if not self.sequence_epoch.strip():
            raise ValueError("sample sequence and sequence_epoch must name one monotonic epoch")
        if self.source_time is not None and not math.isfinite(self.source_time):
            raise ValueError("sample source_time must be finite")
        return self


class PredictionSidecar(Base):
    contract_sha256: str
    case_id: str
    solver_version: str
    model_version: str
    model_input_manifest_sha256: str
    parameter_hashes: list[str]
    calibration_hashes: list[str]
    artifact: ArtifactRef
    samples: list[PredictionSample]
    clock_mapping: ArtifactRef | None = None

    @field_validator(
        "contract_sha256",
        "model_input_manifest_sha256",
        "parameter_hashes",
        "calibration_hashes",
    )
    @classmethod
    def _sha256(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if any(
            len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
            for item in values
        ):
            raise ValueError("sidecar hashes must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("solver_version", "model_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prediction solver and model versions must not be blank")
        return value


class ValidationReference(Base):
    artifact: ArtifactRef
    provenance_class: Literal["measured", "synthetic"]
    synthetic: bool = False
    held_out: bool
    calibration_hashes: list[str] = []
    samples: list[PredictionSample]
    clock_mapping: ArtifactRef | None = None


class ActuatorCaseEvidence(Base):
    """Versioned evidence parsed only from a hash-verified artifact payload."""

    version: Literal["1"]
    case_id: str
    signals: list[SignalObservation] = []
    dc_delivery: list[DCDeliveryObservation] = []
    events: list[TimedEvent] = []
    event_captures: list[EventCapture] = []
    independent_load: IndependentLoadObservation | None = None
    prediction: PredictionSidecar | None = None
    validation_reference: ValidationReference | None = None

    _clock_mappings: dict[tuple[str, str], ClockMapping] = PrivateAttr(default_factory=dict)

    def register_clock_mapping(self, ref: ArtifactRef, mapping: ClockMapping) -> None:
        self._clock_mappings[(ref.path, ref.sha256)] = mapping

    def resolved_clock_mapping(self, ref: ArtifactRef | None) -> ClockMapping | None:
        if ref is None:
            return None
        return self._clock_mappings.get((ref.path, ref.sha256))


def find_signal(rows: list[SignalObservation], name: str) -> SignalObservation | None:
    matches = [row for row in rows if row.signal == name]
    return matches[0] if len(matches) == 1 else None


def find_event(rows: list[TimedEvent], name: str) -> TimedEvent | None:
    matches = [row for row in rows if row.event == name]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class ActuatorCaseReport:
    """The two independent product claim results for one selected case."""

    case_id: str
    conformance: Verdict
    simulation_validity: Verdict
    findings: tuple[Finding, ...] = ()
    comparisons: tuple[ScoredRecord, ...] = ()
    physical_evidence: ActuatorCaseEvidence | None = None
    simulation_evidence: ActuatorCaseEvidence | None = None
    physical_artifacts: tuple[ArtifactRef, ...] = ()
    simulation_artifacts: tuple[ArtifactRef, ...] = ()
    simulation_calibration_artifacts: tuple[ArtifactRef, ...] = ()


def evaluate_actuator_case(
    contract: Contract,
    case_id: str,
    evidence_ref: ArtifactRef,
    *,
    evidence_root: Path,
    contract_sha256: str,
) -> ActuatorCaseReport:
    """Score one declared case only from a hash-verified versioned evidence payload."""

    from lockwire_pro.paid_checks import (
        check_actuator_case_conformance,
        check_actuator_case_simulation_validity,
    )

    case = next((item for item in contract.actuator_cases if item.id == case_id), None)
    if case is None:
        return _unadmitted_case_report(case_id, "case is not declared by the contract")
    if not isinstance(evidence_ref, ArtifactRef):
        return unadmitted_actuator_case_report(case, "payload is not an ArtifactRef")
    try:
        payload = json.loads(read_artifact(evidence_ref, evidence_root))
        if not isinstance(payload, dict):
            raise ValueError("actuator evidence payload must be a JSON object")
        allowed = {
            "version",
            "case_id",
            "signals",
            "dc_delivery",
            "events",
            "event_captures",
            "independent_load",
            "prediction",
            "validation_reference",
        }
        if set(payload) - allowed:
            raise ValueError("actuator evidence payload has unknown top-level fields")
        if payload.get("version") != "1" or payload.get("case_id") != case.id:
            raise ValueError("payload version or case identity does not match the declared case")
    except (OSError, ValidationError, ValueError) as error:
        return unadmitted_actuator_case_report(case, str(error))

    physical, physical_error = _admit_lane(
        payload,
        ("signals", "dc_delivery", "events", "event_captures", "independent_load"),
        case,
        evidence_root,
        include_case_artifacts=False,
    )
    simulation, simulation_error = _admit_lane(
        payload,
        ("prediction", "validation_reference"),
        case,
        evidence_root,
        include_case_artifacts=True,
    )
    comparisons: list[ScoredRecord] = []
    conformance_findings = (
        [_unadmitted_claim(case, "conformance", physical_error)]
        if physical is None
        else check_actuator_case_conformance(contract, case, physical, comparisons=comparisons)
    )
    simulation_findings = (
        [_unadmitted_claim(case, "simulation_validity", simulation_error)]
        if simulation is None
        else check_actuator_case_simulation_validity(
            case, simulation, contract_sha256=contract_sha256, comparisons=comparisons
        )
    )
    return ActuatorCaseReport(
        case_id=case.id,
        conformance=_actuator_claim(conformance_findings),
        simulation_validity=_actuator_claim(simulation_findings),
        findings=tuple(conformance_findings + simulation_findings),
        comparisons=tuple(comparisons),
        physical_evidence=physical,
        simulation_evidence=simulation,
        physical_artifacts=tuple(
            _physical_artifacts(case, physical) + _clock_mapping_refs(case, physical)
        )
        if physical
        else (),
        simulation_artifacts=tuple(
            _simulation_artifacts(case, simulation) + _clock_mapping_refs(case, simulation)
        )
        if simulation
        else (),
        simulation_calibration_artifacts=tuple(case.calibration_artifacts) if simulation else (),
    )


def _admit_lane(
    payload: dict[str, object],
    fields: tuple[str, ...],
    case: ActuatorCase,
    evidence_root: Path,
    *,
    include_case_artifacts: bool,
) -> tuple[ActuatorCaseEvidence | None, str]:
    try:
        evidence = ActuatorCaseEvidence.model_validate(
            {"version": payload["version"], "case_id": payload["case_id"]}
            | {field: payload[field] for field in fields if field in payload}
        )
        refs = (
            _simulation_artifacts(case, evidence)
            if include_case_artifacts
            else _physical_artifacts(case, evidence)
        )
        for ref in refs:
            read_artifact(ref, evidence_root)
        declared = {(ref.path, ref.sha256) for ref in case.clock_mapping_artifacts}
        for ref in _clock_mapping_refs(case, evidence):
            if (ref.path, ref.sha256) not in declared:
                raise ValueError(f"clock mapping {ref.path!r} is not declared by the actuator case")
            mapping = ClockMapping.model_validate_json(read_artifact(ref, evidence_root))
            evidence.register_clock_mapping(ref, mapping)
        return evidence, ""
    except (KeyError, OSError, ValidationError, ValueError) as error:
        return None, str(error)


def _physical_artifacts(case: ActuatorCase, evidence: ActuatorCaseEvidence) -> list[ArtifactRef]:
    refs = [row.artifact for row in evidence.signals]
    if case.dc_delivery is not None or case.transient_dc_agreement_required is True:
        refs.extend(row.artifact for row in evidence.dc_delivery)
    refs.extend(row.artifact for row in evidence.events)
    refs.extend(row.artifact for row in evidence.event_captures)
    if evidence.independent_load is not None:
        refs.extend((evidence.independent_load.artifact, evidence.independent_load.calibration))
    if case.dc_delivery is not None:
        refs.extend(case.dc_delivery.provenance_artifacts)
    if case.transient_dc_agreement_required is True and case.transient_dc is not None:
        if case.transient_dc.evidence_artifact is not None:
            refs.append(case.transient_dc.evidence_artifact)
        refs.extend(case.transient_dc.provenance_artifacts)
    return refs


def _simulation_artifacts(case: ActuatorCase, evidence: ActuatorCaseEvidence) -> list[ArtifactRef]:
    refs = [case.model_input_manifest, *case.parameter_artifacts, *case.calibration_artifacts]
    if evidence.prediction is not None:
        refs.append(evidence.prediction.artifact)
    if evidence.validation_reference is not None:
        refs.append(evidence.validation_reference.artifact)
    return refs


def _clock_mapping_refs(case: ActuatorCase, evidence: ActuatorCaseEvidence) -> list[ArtifactRef]:
    refs = []
    if case.dc_delivery is not None or case.transient_dc_agreement_required is True:
        refs.extend(
            row.clock_mapping for row in evidence.dc_delivery if row.clock_mapping is not None
        )
    refs.extend(row.clock_mapping for row in evidence.events if row.clock_mapping is not None)
    if evidence.prediction is not None and evidence.prediction.clock_mapping is not None:
        refs.append(evidence.prediction.clock_mapping)
    if (
        evidence.validation_reference is not None
        and evidence.validation_reference.clock_mapping is not None
    ):
        refs.append(evidence.validation_reference.clock_mapping)
    return refs


def _unadmitted_claim(case: ActuatorCase, lane: str, reason: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"actuator_cases/{case.id}/{lane}/evidence",
        observed=f"unverified evidence: {reason}",
        expected="contained hash-verified artifacts consumed by this claim",
        action=(
            "supply the declared artifacts below the evidence root; do not substitute another lane"
        ),
    )


def unadmitted_actuator_case_report(case: ActuatorCase, reason: str) -> ActuatorCaseReport:
    """Return the two named zero-denominator claims when case inputs are absent."""
    findings = [
        _unadmitted_claim(case, lane, reason) for lane in ("conformance", "simulation_validity")
    ]
    return ActuatorCaseReport(
        case_id=case.id,
        conformance=_actuator_claim([findings[0]]),
        simulation_validity=_actuator_claim([findings[1]]),
        findings=tuple(findings),
    )


def _actuator_claim(findings: list[Finding]) -> Verdict:
    # A selected actuator claim is not a global record run: absence of any
    # required operand is a zero-denominator claim, even if an envelope
    # comparison happened to pass alongside it.
    typed = list(findings)
    return verdict(
        RecordReport(
            typed,
            scored=(0 if any(item.severity == "not_scored" for item in typed) else 1),
        )
    )


def _unadmitted_case_report(case_id: str, reason: str) -> ActuatorCaseReport:
    finding = Finding(
        severity="not_scored",
        quantity=f"actuator_cases/{case_id}/evidence",
        observed=f"unverified evidence: {reason}",
        expected="a contained hash-verified versioned actuator evidence payload",
        action="supply the declared evidence artifacts below the evidence root",
    )
    rejected = _actuator_claim([finding])
    return ActuatorCaseReport(
        case_id=case_id,
        conformance=rejected,
        simulation_validity=rejected,
        findings=(finding,),
    )
