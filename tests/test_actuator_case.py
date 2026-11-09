"""Bounded v0.4 actuator-case evidence admission tests."""

import hashlib
import json

import pytest
from pydantic import ValidationError

import lockwire_pro.paid_checks as paid_checks
from lockwire_pro.actuator_case import ActuatorCaseEvidence, ClockMapping, evaluate_actuator_case
from lockwire.checks import run_record_checks
from lockwire_pro.paid_checks import check_actuator_case_conformance
from lockwire.describe import describe_system
from lockwire.factdiff import fact_diff
from lockwire.schema import ArtifactRef, Contract, Provenance, read_artifact


def _ref(path: str, marker: str) -> dict[str, str]:
    return {"path": path, "sha256": marker * 64}


def _contract() -> Contract:
    return Contract.model_validate(
        {
            "schema_version": "0.4",
            "robot": {"name": "actuator test"},
            "model": {
                "source": "robot.urdf",
                "source_sha256": "f" * 64,
                "structure_hash": "ignored",
                "links": [{"name": "base"}, {"name": "arm"}],
                "joints": [
                    {
                        "name": "elbow",
                        "type": "revolute",
                        "parent": "base",
                        "child": "arm",
                        "limits": {"lower": -1.0, "upper": 1.0, "velocity": 2.0, "effort": 3.0},
                    }
                ],
                "provenance": {
                    "witness": "human",
                    "source": "robot.urdf",
                    "locked_at": "2026-09-13",
                },
            },
            "device_models": [
                {
                    "id": "mdl.motor",
                    "family": "motors",
                    "actuator": {"gear_ratio": 1.0},
                    "input_voltage_range": {
                        "min": {"value": 10.0, "unit": "V"},
                        "max": {"value": 14.0, "unit": "V"},
                    },
                }
            ],
            "devices": [
                {
                    "id": "motor.elbow",
                    "model": "mdl.motor",
                    "drives_joint": "elbow",
                    "power": {"rail": "rail.12v", "draw": {"value": 1.0, "unit": "W"}},
                    "owner": "test",
                }
            ],
            "topology": {"rails": [{"id": "rail.12v", "capacity": {"value": 20.0, "unit": "W"}}]},
            "actuator_cases": [
                {
                    "id": "elbow.nominal",
                    "actuator_device": "motor.elbow",
                    "joint": "elbow",
                    "rail": "rail.12v",
                    "load_torque": {"value": 0.5, "unit": "N*m"},
                    "load_applicability": "static",
                    "position_tracking_max_error": {"value": 0.05, "unit": "rad"},
                    "feedback_deadline_max": {"value": 0.05, "unit": "s"},
                    "clock_alignment_max_error": {"value": 0.01, "unit": "s"},
                    "prediction_position_rmse_max": {"value": 0.03, "unit": "rad"},
                    "prediction_position_peak_max": {"value": 0.04, "unit": "rad"},
                    "model_input_manifest": _ref("model/manifest.json", "a"),
                    "parameter_artifacts": [_ref("model/parameters.json", "b")],
                    "calibration_artifacts": [_ref("calibration/torque.json", "c")],
                    "clock_mapping_artifacts": [
                        _ref("clock/controller.json", "6"),
                        _ref("clock/simulation.json", "7"),
                        _ref("clock/validation.json", "8"),
                    ],
                    "bindings": {
                        "command_position": {
                            "signal": "cmd.position",
                            "scope": "command",
                            "unit": "rad",
                            "provenance_class": "measured",
                        },
                        "command_velocity": {
                            "signal": "cmd.velocity",
                            "scope": "command",
                            "unit": "rad/s",
                            "provenance_class": "measured",
                        },
                        "command_torque": {
                            "signal": "cmd.torque",
                            "scope": "command",
                            "unit": "N*m",
                            "provenance_class": "measured",
                        },
                        "measured_position": {
                            "signal": "state.position",
                            "scope": "observation",
                            "unit": "rad",
                            "frame": "elbow",
                            "provenance_class": "measured",
                        },
                        "measured_joint_torque": {
                            "signal": "state.torque",
                            "scope": "observation",
                            "unit": "N*m",
                            "frame": "elbow",
                            "provenance_class": "measured",
                        },
                        "receiver_voltage": {
                            "signal": "state.voltage",
                            "scope": "observation",
                            "unit": "V",
                            "provenance_class": "measured",
                        },
                        "command_issued_event": {
                            "event": "command",
                            "scope": "command",
                            "provenance_class": "measured",
                        },
                        "feedback_event": {
                            "event": "feedback",
                            "scope": "observation",
                            "provenance_class": "measured",
                        },
                        "independent_load": {
                            "signal": "load.torque",
                            "scope": "observation",
                            "unit": "N*m",
                            "joint": "elbow",
                            "frame": "elbow",
                            "sign": 1,
                            "application_point": [0.0, 0.0, 0.0],
                            "axis": [0.0, 0.0, 1.0],
                            "applicability": "static",
                            "provenance_class": "measured",
                        },
                    },
                }
            ],
        }
    )


def _evidence(*, synthetic: bool = False, receive_time_only: bool = False) -> ActuatorCaseEvidence:
    artifact = _ref("evidence/capture.json", "d")
    measured = "synthetic" if synthetic else "measured"
    source = None if receive_time_only else 1.0
    evidence = ActuatorCaseEvidence.model_validate(
        {
            "version": "1",
            "case_id": "elbow.nominal",
            "signals": [
                {
                    "signal": "cmd.position",
                    "value": {"value": 0.10, "unit": "rad"},
                    "provenance_class": "measured",
                    "artifact": artifact,
                },
                {
                    "signal": "cmd.velocity",
                    "value": {"value": 0.20, "unit": "rad/s"},
                    "provenance_class": "measured",
                    "artifact": artifact,
                },
                {
                    "signal": "cmd.torque",
                    "value": {"value": 0.50, "unit": "N*m"},
                    "provenance_class": "measured",
                    "artifact": artifact,
                },
                {
                    "signal": "state.position",
                    "value": {"value": 0.11, "unit": "rad"},
                    "frame": "elbow",
                    "uncertainty": {"value": 0.005, "unit": "rad"},
                    "provenance_class": measured,
                    "synthetic": synthetic,
                    "artifact": artifact,
                },
                {
                    "signal": "state.torque",
                    "value": {"value": 0.50, "unit": "N*m"},
                    "frame": "elbow",
                    "joint": "elbow",
                    "uncertainty": {"value": 0.01, "unit": "N*m"},
                    "provenance_class": measured,
                    "synthetic": synthetic,
                    "artifact": artifact,
                },
                {
                    "signal": "state.voltage",
                    "value": {"value": 12.0, "unit": "V"},
                    "provenance_class": measured,
                    "synthetic": synthetic,
                    "artifact": artifact,
                },
            ],
            "events": [
                {
                    "event": "command",
                    "sequence": 7,
                    "sequence_epoch": "case-run-1",
                    "source_time": source,
                    "receive_time": 4.0,
                    "clock_mapping": _ref("clock/controller.json", "6"),
                    "provenance_class": measured,
                    "synthetic": synthetic,
                    "artifact": artifact,
                },
                {
                    "event": "feedback",
                    "sequence": 7,
                    "sequence_epoch": "case-run-1",
                    "source_time": None if receive_time_only else 1.02,
                    "receive_time": 4.001,
                    "clock_mapping": _ref("clock/controller.json", "6"),
                    "provenance_class": measured,
                    "synthetic": synthetic,
                    "artifact": artifact,
                },
            ],
            "independent_load": {
                "signal": "load.torque",
                "value": {"value": 0.5, "unit": "N*m"},
                "frame": "elbow",
                "joint": "elbow",
                "uncertainty": {"value": 0.01, "unit": "N*m"},
                "provenance_class": measured,
                "synthetic": synthetic,
                "artifact": artifact,
                "sign": 1,
                "application_point": [0.0, 0.0, 0.0],
                "axis": [0.0, 0.0, 1.0],
                "applicability": "static",
                "calibration": _ref("calibration/load.json", "e"),
            },
            "prediction": {
                "contract_sha256": "0" * 64,
                "case_id": "elbow.nominal",
                "solver_version": "solver-1",
                "model_version": "model-1",
                "model_input_manifest_sha256": "a" * 64,
                "parameter_hashes": ["b" * 64],
                "calibration_hashes": ["c" * 64],
                "artifact": _ref("prediction/run.json", "f"),
                "clock_mapping": _ref("clock/simulation.json", "7"),
                "samples": [
                    {
                        "sequence": 7,
                        "sequence_epoch": "case-run-1",
                        "source_time": 10.02,
                        "position": {"value": 0.10, "unit": "rad"},
                    }
                ],
            },
            "validation_reference": {
                "artifact": _ref("validation/held-out.json", "9"),
                "provenance_class": measured,
                "synthetic": synthetic,
                "held_out": True,
                "clock_mapping": _ref("clock/validation.json", "8"),
                "samples": [
                    {
                        "sequence": 7,
                        "sequence_epoch": "case-run-1",
                        "source_time": 100.02,
                        "position": {"value": 0.11, "unit": "rad"},
                    }
                ],
            },
        }
    )

    for row in evidence.signals:
        if row.signal in {"state.position", "state.torque", "state.voltage"}:
            row.provenance = Provenance(
                source="measurement-log/run-1", witness="bench operator", locked_at="2026-09-27"
            )
    return evidence


def _with_dc_delivery(
    contract: Contract,
    evidence: ActuatorCaseEvidence,
    *,
    receiver_voltage: float = 4.7,
    receiver_uncertainty: float = 0.05,
) -> None:
    case = contract.actuator_cases[0]
    contract.device_models[0].input_voltage_range.min.value = 4.5
    contract.device_models[0].input_voltage_range.max.value = 5.5
    evidence.signals[5].value.value = receiver_voltage
    case.bindings.source_voltage = {
        "signal": "power.source_voltage",
        "scope": "observation",
        "unit": "V",
        "provenance_class": "measured",
    }
    case.bindings.load_current = {
        "signal": "power.load_current",
        "scope": "observation",
        "unit": "A",
        "provenance_class": "measured",
    }
    case.dc_delivery = {
        "applicability": "single_series_dc_steady",
        "condition_id": "peak-load",
        "source_node": "supply.5v",
        "receiver_node": "motor.input",
        "return_node": "supply.return",
        "source_voltage_min": {"value": 5.0, "unit": "V"},
        "source_resistance_max": {"value": 0.1, "unit": "ohm"},
        "outbound_resistance_max": {"value": 0.1, "unit": "ohm"},
        "return_resistance_max": {"value": 0.1, "unit": "ohm"},
        "contact_resistance_max": {"value": 0.1, "unit": "ohm"},
        "peak_current_max": {"value": 1.0, "unit": "A"},
        "provenance_artifacts": [_ref("electrical/dc-path.json", "1")],
    }
    shared = {
        "applicability": "single_series_dc_steady",
        "condition_id": "peak-load",
        "source_node": "supply.5v",
        "receiver_node": "motor.input",
        "return_node": "supply.return",
        "source_time": 2.0,
        "bandwidth": {"value": 100.0, "unit": "Hz"},
        "provenance_class": "measured",
        "artifact": _ref("evidence/capture.json", "d"),
    }
    evidence.dc_delivery = [
        {
            **shared,
            "role": "source_voltage",
            "signal": "power.source_voltage",
            "value": {"value": 5.1, "unit": "V"},
            "uncertainty": {"value": 0.05, "unit": "V"},
        },
        {
            **shared,
            "role": "receiver_voltage",
            "signal": "state.voltage",
            "value": {"value": receiver_voltage, "unit": "V"},
            "uncertainty": {"value": receiver_uncertainty, "unit": "V"},
        },
        {
            **shared,
            "role": "load_current",
            "signal": "power.load_current",
            "value": {"value": 0.9, "unit": "A"},
            "uncertainty": {"value": 0.05, "unit": "A"},
        },
    ]


def _with_event_timeline(contract: Contract, evidence: ActuatorCaseEvidence) -> None:
    case = contract.actuator_cases[0]
    case.feedback_loss_max = 0
    artifact = _ref("evidence/capture.json", "d")
    mapping = _ref("clock/controller.json", "6")
    events = []
    for sequence in (1, 2, 3):
        events.extend(
            [
                {
                    "event": "command",
                    "sequence": sequence,
                    "sequence_epoch": "case-run-1",
                    "source_time": float(sequence),
                    "receive_time": 100.0 + sequence,
                    "clock_mapping": mapping,
                    "provenance_class": "measured",
                    "artifact": artifact,
                },
                {
                    "event": "feedback",
                    "sequence": sequence,
                    "sequence_epoch": "case-run-1",
                    "source_time": sequence + 0.02,
                    "receive_time": 100.01 + sequence,
                    "clock_mapping": mapping,
                    "provenance_class": "measured",
                    "artifact": artifact,
                },
            ]
        )
    evidence.events = events
    evidence.event_captures = [
        {
            "event": event,
            "epoch": "case-run-1",
            "first_sequence": 1,
            "last_sequence": 3,
            "complete": True,
            "provenance_class": "measured",
            "artifact": artifact,
        }
        for event in ("command", "feedback")
    ]


def _with_transient_dc_binding(contract: Contract, evidence: ActuatorCaseEvidence) -> None:
    """Opt into the joined path without changing the steady-path fixture."""
    _with_dc_delivery(contract, evidence)
    case = contract.actuator_cases[0]
    case.dc_delivery = None
    case.transient_dc_agreement_required = True
    case.transient_dc = {
        "applicability": "actuator_command_response_transient",
        "channel": "motor-power",
        "part": "motor.elbow",
        "supply_rail": "rail.12v",
        "source_node": "supply.5v",
        "receiver_node": "motor.input",
        "return_node": "supply.return",
        "time_basis_clock_domain": "actuator-case",
        "time_basis_epoch": "case-run-1",
        "voltage_min": {"value": 4.5, "unit": "V"},
        "current_max": {"value": 1.0, "unit": "A"},
        "response_latency_max": {"value": 0.05, "unit": "s"},
        "evidence_artifact": _ref("evidence/capture.json", "d"),
        "provenance_artifacts": [_ref("electrical/transient-source.json", "2")],
    }
    evidence.dc_delivery = [
        type(row).model_validate(
            {
                **row.model_dump(mode="python"),
                "applicability": "actuator_command_response_transient",
                "source_time": 1.01,
                "receive_time": 4.0005,
                "sequence": 7,
                "sequence_epoch": "case-run-1",
                "clock_mapping": _ref("clock/controller.json", "6"),
                "channel": "motor-power",
                "part": "motor.elbow",
                "supply_rail": "rail.12v",
            }
        )
        for row in evidence.dc_delivery
    ]


def _evidence_refs(evidence: ActuatorCaseEvidence) -> list[ArtifactRef]:
    refs = [row.artifact for row in evidence.signals]
    refs.extend(row.artifact for row in evidence.dc_delivery)
    refs.extend(row.artifact for row in evidence.events)
    refs.extend(row.artifact for row in evidence.event_captures)
    if evidence.independent_load is not None:
        refs.extend((evidence.independent_load.artifact, evidence.independent_load.calibration))
    if evidence.prediction is not None:
        refs.append(evidence.prediction.artifact)
    if evidence.validation_reference is not None:
        refs.append(evidence.validation_reference.artifact)
    return refs


def _write_artifact(root, ref: ArtifactRef) -> None:
    path = root / ref.path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"artifact:{ref.path}".encode()
    path.write_bytes(payload)
    ref.sha256 = hashlib.sha256(payload).hexdigest()


def _mapping_payload(path: str) -> dict[str, object]:
    source_domain, source_epoch, offset, max_error = {
        "clock/controller.json": ("controller", "boot-42", 0.0, 0.001),
        "clock/simulation.json": ("simulator", "run-17", -9.0, 0.001),
        "clock/validation.json": ("sensor", "boot-81", -99.0, 0.001),
        "clock/mismatch.json": ("controller", "boot-42", 0.0, 0.001),
        "clock/skewed.json": ("controller", "boot-42", 0.0, 0.02),
    }[path]
    return {
        "version": "1",
        "id": path,
        "source_clock_domain": source_domain,
        "source_epoch": source_epoch,
        "target_clock_domain": "actuator-case",
        "target_epoch": "wrong-run" if path == "clock/mismatch.json" else "case-run-1",
        "scale": 1.0,
        "offset": {"value": offset, "unit": "s"},
        "max_error": {"value": max_error, "unit": "s"},
        "provenance_class": "declared",
    }


def _write_mapping(root, ref: ArtifactRef, payload: dict[str, object] | None = None) -> None:
    path = root / ref.path
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload or _mapping_payload(ref.path), separators=(",", ":")).encode()
    path.write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()


def _replace_mapping_refs(evidence: ActuatorCaseEvidence, case) -> None:
    declared = {ref.path: ref for ref in case.clock_mapping_artifacts}
    for row in evidence.dc_delivery:
        if row.clock_mapping is not None:
            row.clock_mapping = declared[row.clock_mapping.path]
    for event in evidence.events:
        if event.clock_mapping is not None:
            event.clock_mapping = declared[event.clock_mapping.path]
    if evidence.prediction is not None and evidence.prediction.clock_mapping is not None:
        evidence.prediction.clock_mapping = declared[evidence.prediction.clock_mapping.path]
    if (
        evidence.validation_reference is not None
        and evidence.validation_reference.clock_mapping is not None
    ):
        evidence.validation_reference.clock_mapping = declared[
            evidence.validation_reference.clock_mapping.path
        ]


def _admitted_evidence(
    tmp_path, contract: Contract, evidence: ActuatorCaseEvidence
) -> tuple[ArtifactRef, object]:
    root = tmp_path / "evidence-root"
    root.mkdir(parents=True, exist_ok=True)
    case = contract.actuator_cases[0]
    dc_artifacts = case.dc_delivery.provenance_artifacts if case.dc_delivery is not None else []
    transient_artifacts = (
        (
            *(
                (case.transient_dc.evidence_artifact,)
                if case.transient_dc.evidence_artifact is not None
                else ()
            ),
            *case.transient_dc.provenance_artifacts,
        )
        if (case.transient_dc_agreement_required is True and case.transient_dc is not None)
        else []
    )
    for ref in (
        case.model_input_manifest,
        *case.parameter_artifacts,
        *case.calibration_artifacts,
        *dc_artifacts,
        *transient_artifacts,
    ):
        _write_artifact(root, ref)
    for ref in case.clock_mapping_artifacts:
        _write_mapping(root, ref)
    _replace_mapping_refs(evidence, case)
    if evidence.prediction is not None:
        evidence.prediction.model_input_manifest_sha256 = case.model_input_manifest.sha256
        evidence.prediction.parameter_hashes = [item.sha256 for item in case.parameter_artifacts]
        evidence.prediction.calibration_hashes = [
            item.sha256 for item in case.calibration_artifacts
        ]
    for ref in _evidence_refs(evidence):
        _write_artifact(root, ref)
    payload = evidence.model_dump_json().encode()
    ref = ArtifactRef(path="evidence/payload.json", sha256=hashlib.sha256(payload).hexdigest())
    path = root / ref.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ref, root


def _evaluate(tmp_path, contract: Contract, evidence: ActuatorCaseEvidence):
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    return evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )


def _record_checks(tmp_path, contract: Contract, evidence: ActuatorCaseEvidence):
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    return (
        run_record_checks(
            contract,
            actuator_evidence={"elbow.nominal": ref},
            evidence_root=root,
            contract_sha256="0" * 64,
        ),
        ref,
        root,
    )


def _findings(contract: Contract, evidence: ActuatorCaseEvidence):
    case = contract.actuator_cases[0]
    for ref in case.clock_mapping_artifacts:
        evidence.register_clock_mapping(
            ref, ClockMapping.model_validate(_mapping_payload(ref.path))
        )
    return check_actuator_case_conformance(contract, case, evidence)


def test_actuator_case_has_two_independent_admitted_claims(tmp_path):
    contract, evidence = _contract(), _evidence()
    report = _evaluate(tmp_path, contract, evidence)
    assert report.conformance.code == "GO"
    assert report.simulation_validity.code == "GO"


def test_required_transient_dc_binding_is_named_not_scored_when_absent(tmp_path):
    contract, evidence = _contract(), _evidence()
    contract.actuator_cases[0].transient_dc_agreement_required = True

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("NOT_SCORED", "GO")
    assert any(item.quantity.endswith("transient_dc/binding") for item in report.findings)


def test_record_checks_register_verified_actuator_evidence_without_scorebook_inflation(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.signals[2].value.value = 4.0
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    direct = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    baseline = run_record_checks(contract)
    registered = run_record_checks(
        contract,
        actuator_evidence={"elbow.nominal": ref},
        evidence_root=root,
        contract_sha256="0" * 64,
    )

    assert direct.conformance.code == "NO_GO"
    assert [
        item.quantity for item in registered if item.quantity.startswith("actuator_cases/")
    ] == [item.quantity for item in direct.findings]
    assert len(registered.actuator_case_reports) == 1
    retained = registered.actuator_case_reports[0]
    assert retained.case_id == "elbow.nominal"
    assert (retained.conformance.code, retained.simulation_validity.code) == (
        "NO_GO",
        "GO",
    )
    assert [item.quantity for item in retained.findings] == [
        item.quantity for item in direct.findings
    ]
    assert registered.scored == baseline.scored
    assert registered.scored_keys == baseline.scored_keys
    assert registered.scored_records == baseline.scored_records
    assert registered.scored_keys is not None
    assert registered.scored == len(registered.scored_records) + sum(
        item.severity == "violation" and item.quantity in registered.scored_keys
        for item in registered
    )


def test_record_checks_keeps_the_evaluator_case_report_object(tmp_path, monkeypatch):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    evaluated = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    monkeypatch.setattr(paid_checks, "evaluate_actuator_case", lambda *_args, **_kwargs: evaluated)

    registered = run_record_checks(
        contract,
        actuator_evidence={"elbow.nominal": ref},
        evidence_root=root,
        contract_sha256="0" * 64,
    )

    assert registered.actuator_case_reports == [evaluated]
    assert registered.actuator_case_reports[0] is evaluated
    assert registered.actuator_case_reports[0].case_id == "elbow.nominal"


def test_record_checks_keeps_multi_case_reports_labeled_in_contract_order(tmp_path):
    contract, evidence = _contract(), _evidence()
    second = contract.actuator_cases[0].model_copy(update={"id": "elbow.second"})
    contract.actuator_cases.append(second)
    first_ref, root = _admitted_evidence(tmp_path, contract, evidence)
    second_evidence = evidence.model_copy(update={"case_id": second.id})
    payload = second_evidence.model_dump_json().encode()
    second_ref = ArtifactRef(
        path="evidence/second-payload.json", sha256=hashlib.sha256(payload).hexdigest()
    )
    (root / second_ref.path).write_bytes(payload)

    report = run_record_checks(
        contract,
        actuator_evidence={second.id: second_ref, contract.actuator_cases[0].id: first_ref},
        evidence_root=root,
        contract_sha256="0" * 64,
    )

    assert [item.case_id for item in report.actuator_case_reports] == [
        "elbow.nominal",
        "elbow.second",
    ]


@pytest.mark.parametrize(
    "kwargs",
    (
        {},
        {
            "actuator_evidence": {
                "other.case": ArtifactRef(path="evidence/payload.json", sha256="0" * 64)
            }
        },
        {"evidence_root": None},
        {"contract_sha256": "0" * 12},
    ),
)
def test_record_checks_missing_actuator_inputs_emit_both_named_claim_gaps(tmp_path, kwargs):
    contract = _contract()
    options = {
        "actuator_evidence": {
            "elbow.nominal": ArtifactRef(path="evidence/payload.json", sha256="0" * 64)
        },
        "evidence_root": tmp_path,
        "contract_sha256": "0" * 64,
    }
    options.update(kwargs)
    report = run_record_checks(contract, **options)
    assert [item.quantity for item in report if item.quantity.startswith("actuator_cases/")] == [
        "actuator_cases/elbow.nominal/conformance/evidence",
        "actuator_cases/elbow.nominal/simulation_validity/evidence",
    ]
    retained = report.actuator_case_reports[0]
    assert retained.case_id == "elbow.nominal"
    assert (retained.conformance.code, retained.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )


def test_record_checks_keep_actuator_claim_lanes_independent(tmp_path):
    contract, evidence = _contract(), _evidence()
    report, evidence_ref, root = _record_checks(tmp_path, contract, evidence)
    assert not [item for item in report if item.quantity.startswith("actuator_cases/")]
    assert [
        (item.conformance.code, item.simulation_validity.code)
        for item in report.actuator_case_reports
    ] == [("GO", "GO")]

    (root / evidence.signals[0].artifact.path).unlink()
    report = run_record_checks(
        contract,
        actuator_evidence={"elbow.nominal": evidence_ref},
        evidence_root=root,
        contract_sha256="0" * 64,
    )
    assert [item.quantity for item in report if item.quantity.startswith("actuator_cases/")] == [
        "actuator_cases/elbow.nominal/conformance/evidence"
    ]
    assert [
        (item.conformance.code, item.simulation_validity.code)
        for item in report.actuator_case_reports
    ] == [("NOT_SCORED", "GO")]


def test_record_checks_reject_wrong_full_contract_sha_in_simulation_lane(tmp_path):
    contract, evidence = _contract(), _evidence()
    _, evidence_ref, root = _record_checks(tmp_path, contract, evidence)
    report = run_record_checks(
        contract,
        actuator_evidence={"elbow.nominal": evidence_ref},
        evidence_root=root,
        contract_sha256="1" * 64,
    )
    assert [item.quantity for item in report if item.quantity.startswith("actuator_cases/")] == [
        "actuator_cases/elbow.nominal/simulation_validity/identity",
    ]
    assert [
        (item.conformance.code, item.simulation_validity.code)
        for item in report.actuator_case_reports
    ] == [("GO", "NOT_SCORED")]


def test_command_torque_is_envelope_only_and_never_combined_with_load(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.signals[2].value.value = 4.0
    report = _evaluate(tmp_path, contract, evidence)
    assert report.conformance.code == "NO_GO"
    assert report.simulation_validity.code == "GO"


@pytest.mark.parametrize("effort", [0.0, -1.0])
def test_unusable_joint_effort_warns_instead_of_violating(effort):
    contract = _contract()
    contract.model.joints[0].limits.effort = effort
    findings = _findings(contract, _evidence())
    assert not [f for f in findings if f.severity == "violation"]  # bites 0 as a hard limit
    warned = {f.quantity.rsplit("/", 1)[-1] for f in findings if f.severity == "warning"}
    assert {"torque", "physical_torque"} <= warned


def test_synthetic_command_can_prove_an_envelope_no_go_but_not_a_physical_go(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.signals[2].synthetic = True  # even if its claimed provenance says measured
    evidence.signals[2].value.value = 4.0
    report = _evaluate(tmp_path, contract, evidence)
    assert report.conformance.code == "NO_GO"


def test_missing_or_synthetic_physical_operands_never_clear_conformance(tmp_path):
    contract = _contract()
    synthetic = _evaluate(tmp_path, contract, _evidence(synthetic=True))
    assert synthetic.conformance.code == "NOT_SCORED"
    assert synthetic.simulation_validity.code == "NOT_SCORED"


def test_synthetic_flag_overrides_a_spoofed_measured_physical_origin(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.signals[3].synthetic = True
    report = _evaluate(tmp_path, contract, evidence)
    assert report.conformance.code == "NOT_SCORED"


def test_v02_and_v03_reject_v04_only_actuator_cases():
    dumped = _contract().model_dump(mode="python")
    for version in ("0.2", "0.3"):
        dumped["schema_version"] = version
        with pytest.raises(ValidationError, match="actuator_cases require schema_version '0.4'"):
            Contract.model_validate(dumped)


def test_actuator_case_requires_declared_clock_mapping_artifacts():
    dumped = _contract().model_dump(mode="python")
    dumped["actuator_cases"][0]["clock_mapping_artifacts"] = []
    with pytest.raises(ValidationError, match="at least one declared clock mapping"):
        Contract.model_validate(dumped)


def test_actuator_clock_mapping_contract_is_described_and_diffed():
    before = _contract()
    rendered = describe_system(before)
    for ref in before.actuator_cases[0].clock_mapping_artifacts:
        assert ref.path in rendered
        assert ref.sha256 in rendered

    after = before.model_copy(deep=True)
    after.actuator_cases[0].clock_alignment_max_error.value = 0.02
    change = next(
        item
        for item in fact_diff(before, after).changes
        if item.path.endswith("clock_alignment_max_error/value")
    )
    assert change.limit_raised is True


def test_receive_time_only_and_calibration_overlap_are_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence(receive_time_only=True)
    report = _evaluate(tmp_path, contract, evidence)
    assert report.conformance.code == "NOT_SCORED"
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    evidence.validation_reference.calibration_hashes = [
        contract.actuator_cases[0].calibration_artifacts[0].sha256
    ]
    payload = evidence.model_dump_json().encode()
    (root / ref.path).write_bytes(payload)
    ref = ArtifactRef(path=ref.path, sha256=hashlib.sha256(payload).hexdigest())
    report = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert report.simulation_validity.code == "NOT_SCORED"


def test_artifact_refs_are_contained_hashed_and_bounded(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    payload = root / "capture.bin"
    payload.write_bytes(b"proof")
    digest = hashlib.sha256(b"proof").hexdigest()
    assert read_artifact(ArtifactRef(path="capture.bin", sha256=digest), root) == b"proof"
    with pytest.raises(ValidationError):
        ArtifactRef(path="../escape", sha256=digest)
    with pytest.raises(ValidationError):
        ArtifactRef(path="https://example.test/evidence", sha256=digest)
    with pytest.raises(ValueError, match="sha256"):
        read_artifact(ArtifactRef(path="capture.bin", sha256="0" * 64), root)
    escape = tmp_path / "escape.bin"
    escape.write_bytes(b"escape")
    (root / "link.bin").symlink_to(escape)
    with pytest.raises(ValueError, match="escapes"):
        read_artifact(
            ArtifactRef(path="link.bin", sha256=hashlib.sha256(b"escape").hexdigest()), root
        )
    with pytest.raises(ValueError, match="exceeds"):
        read_artifact(ArtifactRef(path="capture.bin", sha256=digest), root, max_bytes=1)


def test_evaluator_scores_only_the_verified_payload_bytes(tmp_path):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    baseline = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    evidence.signals[2].value.value = 4.0  # This unverified caller-side array is not an input.
    unchanged = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert (baseline.conformance.code, baseline.simulation_validity.code) == ("GO", "GO")
    assert (unchanged.conformance.code, unchanged.simulation_validity.code) == ("GO", "GO")

    missing = evaluate_actuator_case(
        contract,
        "elbow.nominal",
        ArtifactRef(path="evidence/does-not-exist.json", sha256="0" * 64),
        evidence_root=root,
        contract_sha256="0" * 64,
    )
    assert (missing.conformance.code, missing.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )

    source = root / evidence.signals[0].artifact.path
    original = source.read_bytes()
    source.write_bytes(b"tampered")
    tampered_source = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    source.write_bytes(original)
    assert (tampered_source.conformance.code, tampered_source.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )

    payload = root / ref.path
    payload.write_bytes(payload.read_bytes().replace(b'"value":0.5', b'"value":4.0', 1))
    changed_without_hash = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert (
        changed_without_hash.conformance.code,
        changed_without_hash.simulation_validity.code,
    ) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )

    changed_ref = ArtifactRef(
        path=ref.path, sha256=hashlib.sha256(payload.read_bytes()).hexdigest()
    )
    changed_with_hash = evaluate_actuator_case(
        contract, "elbow.nominal", changed_ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert changed_with_hash.conformance.code == "NO_GO"


def test_evaluator_rejects_a_caller_side_evidence_model(tmp_path):
    contract, evidence = _contract(), _evidence()
    report = evaluate_actuator_case(
        contract, "elbow.nominal", evidence, evidence_root=tmp_path, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )


def test_missing_or_malformed_evidence_payload_never_scores(tmp_path):
    contract, evidence = _contract(), _evidence()
    root = tmp_path / "evidence-root"
    root.mkdir()
    missing = ArtifactRef(path="evidence/missing.json", sha256="0" * 64)
    report = evaluate_actuator_case(
        contract, "elbow.nominal", missing, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )

    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    malformed = b'{"version":"2"}'
    path = root / ref.path
    path.write_bytes(malformed)
    report = evaluate_actuator_case(
        contract,
        "elbow.nominal",
        ArtifactRef(path=ref.path, sha256=hashlib.sha256(malformed).hexdigest()),
        evidence_root=root,
        contract_sha256="0" * 64,
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )


def test_self_referential_or_missing_held_out_evidence_is_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.validation_reference.artifact = evidence.prediction.artifact
    report = _evaluate(tmp_path, contract, evidence)
    assert report.simulation_validity.code == "NOT_SCORED"

    contract, evidence = _contract(), _evidence()
    evidence.validation_reference = None
    report = _evaluate(tmp_path, contract, evidence)
    assert report.simulation_validity.code == "NOT_SCORED"


def test_distinct_declared_clock_domains_align_only_after_mapping(tmp_path):
    report = _evaluate(tmp_path, _contract(), _evidence())
    assert (report.conformance.code, report.simulation_validity.code) == ("GO", "GO")


def test_rehashed_source_time_drift_only_rejects_simulation_lane(tmp_path):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    payload = json.loads((root / ref.path).read_bytes())
    payload["validation_reference"]["samples"][0]["source_time"] = 9876.5
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    rehashed = ArtifactRef(path=ref.path, sha256=hashlib.sha256(encoded).hexdigest())
    report = evaluate_actuator_case(
        contract, "elbow.nominal", rehashed, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )


def test_prediction_and_physical_capture_artifacts_are_lane_local(tmp_path):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    (root / evidence.prediction.artifact.path).unlink()
    report = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )

    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    (root / evidence.signals[0].artifact.path).unlink()
    report = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_shared_artifact_corruption_rejects_only_actual_consumers(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.validation_reference.artifact = evidence.signals[0].artifact
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    (root / evidence.signals[0].artifact.path).unlink()
    report = evaluate_actuator_case(
        contract, "elbow.nominal", ref, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "NOT_SCORED",
    )


def test_malformed_simulation_clock_mapping_does_not_suppress_physical(tmp_path):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    mapping_ref = contract.actuator_cases[0].clock_mapping_artifacts[1]
    _write_mapping(root, mapping_ref, {"version": "1"})
    payload = json.loads((root / ref.path).read_bytes())
    payload["prediction"]["clock_mapping"] = mapping_ref.model_dump(mode="json")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    rehashed = ArtifactRef(path=ref.path, sha256=hashlib.sha256(encoded).hexdigest())
    report = evaluate_actuator_case(
        contract, "elbow.nominal", rehashed, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )


@pytest.mark.parametrize("failure", ["missing_error", "invalid_scale", "clock_mismatch"])
def test_invalid_simulation_clock_mapping_is_lane_local(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    mapping_ref = contract.actuator_cases[0].clock_mapping_artifacts[2]
    mapping = _mapping_payload(mapping_ref.path)
    if failure == "missing_error":
        mapping.pop("max_error")
    elif failure == "invalid_scale":
        mapping["scale"] = -1.0
    else:
        mapping["target_epoch"] = "different-case-run"
    _write_mapping(root, mapping_ref, mapping)
    payload = json.loads((root / ref.path).read_bytes())
    payload["validation_reference"]["clock_mapping"] = mapping_ref.model_dump(mode="json")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    rehashed = ArtifactRef(path=ref.path, sha256=hashlib.sha256(encoded).hexdigest())
    report = evaluate_actuator_case(
        contract, "elbow.nominal", rehashed, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )


def test_missing_simulation_mapping_or_sequence_epoch_is_lane_local(tmp_path):
    contract, evidence = _contract(), _evidence()
    evidence.prediction.clock_mapping = None
    report = _evaluate(tmp_path / "mapping", contract, evidence)
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )

    contract, evidence = _contract(), _evidence()
    ref, root = _admitted_evidence(tmp_path / "epoch", contract, evidence)
    payload = json.loads((root / ref.path).read_bytes())
    payload["validation_reference"]["samples"][0].pop("sequence_epoch")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    rehashed = ArtifactRef(path=ref.path, sha256=hashlib.sha256(encoded).hexdigest())
    report = evaluate_actuator_case(
        contract, "elbow.nominal", rehashed, evidence_root=root, contract_sha256="0" * 64
    )
    assert (report.conformance.code, report.simulation_validity.code) == (
        "GO",
        "NOT_SCORED",
    )


def test_duplicate_or_wrapped_sequence_is_not_scored(tmp_path):
    for wrapped in (False, True):
        contract, evidence = _contract(), _evidence()
        predicted = evidence.prediction.samples[0].model_copy(deep=True)
        observed = evidence.validation_reference.samples[0].model_copy(deep=True)
        if wrapped:
            predicted.sequence = 0
            observed.sequence = 0
        evidence.prediction.samples.append(predicted)
        evidence.validation_reference.samples.append(observed)
        report = _evaluate(tmp_path / str(wrapped), contract, evidence)
        assert report.conformance.code == "GO"
        assert report.simulation_validity.code == "NOT_SCORED"


def test_dc_delivery_calculates_the_full_series_path_and_observed_lower_bound(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("GO", "GO")
    assert "5 V" in describe_system(contract)
    assert "0.1 ohm/0.1 ohm/0.1 ohm/0.1 ohm" in describe_system(contract)


def test_dc_receiver_lower_bound_can_fail_while_declared_source_path_passes(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence, receiver_voltage=4.75, receiver_uncertainty=0.3)

    findings = _findings(contract, evidence)
    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "NO_GO"
    assert not any(row.quantity.endswith("dc_delivery/source_path") for row in findings)
    receiver = next(
        row for row in findings if row.quantity.endswith("dc_delivery/receiver_observation")
    )
    assert "4.45 V measured receiver lower bound" in receiver.observed
    assert "4.5 V receiver input minimum" in receiver.expected


def test_dc_source_path_uses_return_resistance_in_the_worst_case_sum(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)
    contract.device_models[0].input_voltage_range.min.value = 4.65

    findings = _findings(contract, evidence)

    source = next(row for row in findings if row.quantity.endswith("dc_delivery/source_path"))
    assert "1 A * 0.4 ohm = 4.6 V" in source.observed


@pytest.mark.parametrize("failure", ["binding", "current", "condition", "nonseries"])
def test_dc_missing_or_inapplicable_evidence_is_not_scored(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)
    if failure == "binding":
        contract.actuator_cases[0].bindings.load_current = None
    elif failure == "current":
        evidence.dc_delivery.pop()
    elif failure == "condition":
        evidence.dc_delivery[1].condition_id = "different-load"
    else:
        evidence.dc_delivery[1].applicability = "transient"

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_dc_missing_return_identity_is_not_scored_without_affecting_simulation(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    payload = json.loads((root / ref.path).read_bytes())
    payload["dc_delivery"][1].pop("return_node")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)

    report = evaluate_actuator_case(
        contract,
        "elbow.nominal",
        ArtifactRef(path=ref.path, sha256=hashlib.sha256(encoded).hexdigest()),
        evidence_root=root,
        contract_sha256="0" * 64,
    )

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_synthetic_dc_observation_never_yields_physical_go(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)
    evidence.dc_delivery[0].synthetic = True

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_dc_observation_time_or_bandwidth_mismatch_is_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_dc_delivery(contract, evidence)
    evidence.dc_delivery[1].source_time += 0.001

    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "NOT_SCORED"


def test_transient_dc_binding_joins_power_and_response_without_scoring_simulation(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_transient_dc_binding(contract, evidence)

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("GO", "GO")


@pytest.mark.parametrize(
    "failure", ["missing_receipt", "foreign_hash", "outside_interval", "clock_skew"]
)
def test_transient_dc_binding_refuses_unbound_or_stale_association(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    _with_transient_dc_binding(contract, evidence)
    if failure == "missing_receipt":
        evidence.dc_delivery[0].receive_time = None
    elif failure == "foreign_hash":
        evidence.dc_delivery[0].artifact = _ref("evidence/foreign.json", "f")
    elif failure == "outside_interval":
        for row in evidence.dc_delivery:
            row.source_time = 2.0
    else:
        skewed = ArtifactRef.model_validate(_ref("clock/skewed.json", "3"))
        contract.actuator_cases[0].clock_mapping_artifacts.append(skewed)
        for row in evidence.dc_delivery:
            row.clock_mapping = skewed

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


@pytest.mark.parametrize("failure", ["voltage", "current", "latency"])
def test_transient_dc_binding_applies_only_declared_limits(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    _with_transient_dc_binding(contract, evidence)
    if failure == "voltage":
        evidence.dc_delivery[1].value.value = 4.0
    elif failure == "current":
        evidence.dc_delivery[2].value.value = 1.1
    else:
        evidence.events[1].source_time = 1.2

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("NO_GO", "GO")


@pytest.mark.parametrize("scope", [None, False], ids=["omitted", "false"])
@pytest.mark.parametrize("failure", ["missing", "foreign", "partial"])
def test_unrequired_populated_transient_binding_does_not_admit_or_consume_artifacts(
    tmp_path, scope, failure
):
    contract, evidence = _contract(), _evidence()
    _with_transient_dc_binding(contract, evidence)
    case = contract.actuator_cases[0]
    case.transient_dc_agreement_required = scope
    if failure in {"missing", "partial"}:
        absent = ArtifactRef.model_validate(_ref("evidence/unrequired-transient.json", "f"))
        assert case.transient_dc is not None
        case.transient_dc.evidence_artifact = absent
        if failure == "partial":
            case.transient_dc.provenance_artifacts = []
        for row in evidence.dc_delivery:
            row.artifact = absent
        payload_ref, root = _admitted_evidence(tmp_path, contract, evidence)
        (root / absent.path).unlink()
    else:
        payload_ref, root = _admitted_evidence(tmp_path, contract, evidence)
        payload = json.loads((root / payload_ref.path).read_bytes())
        payload["dc_delivery"][0]["clock_mapping"] = _ref("clock/foreign.json", "f")
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        (root / payload_ref.path).write_bytes(encoded)
        payload_ref = ArtifactRef(path=payload_ref.path, sha256=hashlib.sha256(encoded).hexdigest())

    report = evaluate_actuator_case(
        contract, "elbow.nominal", payload_ref, evidence_root=root, contract_sha256="0" * 64
    )

    assert (report.conformance.code, report.simulation_validity.code) == ("GO", "GO")


def test_transient_dc_binding_requires_contract_identity_and_limits():
    contract = _contract().model_dump(mode="python")
    contract["actuator_cases"][0]["transient_dc"] = {
        "applicability": "actuator_command_response_transient",
        "channel": "motor-power",
        "part": "wrong-part",
        "supply_rail": "rail.12v",
        "source_node": "supply.5v",
        "receiver_node": "motor.input",
        "return_node": "supply.return",
        "time_basis_clock_domain": "actuator-case",
        "time_basis_epoch": "case-run-1",
        "voltage_min": {"value": 4.5, "unit": "V"},
        "current_max": {"value": 1.0, "unit": "A"},
        "response_latency_max": {"value": 0.05, "unit": "s"},
        "evidence_artifact": _ref("evidence/capture.json", "d"),
        "provenance_artifacts": [_ref("electrical/transient-source.json", "2")],
    }

    with pytest.raises(ValidationError, match="part must match actuator_device"):
        Contract.model_validate(contract)

    incomplete = _contract().model_dump(mode="python")
    incomplete["actuator_cases"][0]["transient_dc"] = {
        "applicability": "actuator_command_response_transient",
        "voltage_min": {"value": 4.5, "unit": "V"},
    }
    binding = Contract.model_validate(incomplete).actuator_cases[0].transient_dc
    assert binding is not None
    assert binding.response_latency_max is None


def test_transient_dc_missing_limit_is_named_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    contract.actuator_cases[0].transient_dc_agreement_required = True
    contract.actuator_cases[0].transient_dc = {
        "applicability": "actuator_command_response_transient",
        "voltage_min": {"value": 4.5, "unit": "V"},
    }

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("NOT_SCORED", "GO")
    assert any(item.quantity.endswith("transient_dc/declaration") for item in report.findings)


def test_three_event_pairs_are_joined_and_checked_individually(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == ("GO", "GO")


def test_missing_receive_time_in_complete_event_timeline_is_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    evidence.events[3].receive_time = None

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_one_late_event_cannot_be_hidden_by_two_timely_events(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    evidence.events[3].source_time = 2.2

    findings = _findings(contract, evidence)
    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "NO_GO"
    late = [row for row in findings if row.quantity.endswith("feedback_deadline")]
    assert len(late) == 1
    assert "sequence 2" in late[0].observed


def test_missing_feedback_in_explicit_complete_coverage_is_a_violation(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    evidence.events.pop(3)

    findings = _findings(contract, evidence)
    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "NO_GO"
    loss = next(row for row in findings if row.quantity.endswith("feedback_loss"))
    assert "1 command(s) lack feedback" in loss.observed


def test_jointly_missing_sequence_makes_complete_coverage_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    evidence.events = [row for row in evidence.events if row.sequence != 2]

    findings = _findings(contract, evidence)
    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )
    coverage = next(row for row in findings if row.quantity.endswith("feedback_timeline/coverage"))
    assert (
        "2 sequence position(s) are present in a 3-position complete capture" in coverage.observed
    )


def test_declared_feedback_loss_allowance_is_applied_to_complete_coverage(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    contract.actuator_cases[0].feedback_loss_max = 1
    evidence.events.pop(3)

    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "GO"


@pytest.mark.parametrize("failure", ["incomplete", "synthetic"])
def test_event_coverage_must_be_complete_measured_and_nonsynthetic(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    if failure == "incomplete":
        evidence.event_captures[1].complete = False
    else:
        evidence.event_captures[1].provenance_class = "synthetic"
        evidence.event_captures[1].synthetic = True

    report = _evaluate(tmp_path, contract, evidence)

    assert report.conformance.code == "NOT_SCORED"


@pytest.mark.parametrize("failure", ["duplicate", "reused_epoch", "wrap", "receive_only"])
def test_ambiguous_or_receipt_only_event_timeline_is_not_scored(tmp_path, failure):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    if failure == "duplicate":
        evidence.events.append(evidence.events[2].model_copy(deep=True))
    elif failure == "reused_epoch":
        evidence.events[2].sequence_epoch = "case-run-2"
        evidence.event_captures.append(
            evidence.event_captures[0].model_copy(
                update={"epoch": "case-run-2", "first_sequence": 2, "last_sequence": 2},
                deep=True,
            )
        )
    elif failure == "wrap":
        evidence.events[4].sequence = 0
    else:
        evidence.events[3].source_time = None

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_event_clock_mismatch_is_not_scored(tmp_path):
    contract, evidence = _contract(), _evidence()
    _with_event_timeline(contract, evidence)
    mismatch = ArtifactRef.model_validate(_ref("clock/mismatch.json", "2"))
    contract.actuator_cases[0].clock_mapping_artifacts.append(mismatch)
    evidence.events[3].clock_mapping = mismatch

    report = _evaluate(tmp_path, contract, evidence)

    assert (report.conformance.code, report.simulation_validity.code) == (
        "NOT_SCORED",
        "GO",
    )


def test_new_actuator_capabilities_are_optional_and_legacy_versions_still_load():
    for version in ("0.2", "0.3"):
        contract = Contract.model_validate({"schema_version": version, "robot": {"name": "legacy"}})
        assert contract.actuator_cases == []
    case = _contract().actuator_cases[0]
    assert case.dc_delivery is None
    assert case.transient_dc is None
    assert case.transient_dc_agreement_required is None
    assert case.feedback_loss_max is None
    assert "transient_dc" not in _contract().model_dump(exclude_none=True)["actuator_cases"][0]
    dumped = _contract().model_dump(mode="python")
    dumped["actuator_cases"][0]["bindings"].pop("receiver_voltage")
    assert Contract.model_validate(dumped).actuator_cases[0].bindings.receiver_voltage is None


def test_dc_and_feedback_limits_are_fact_diffed():
    before = _contract()
    before.actuator_cases[0].feedback_loss_max = 0
    after = before.model_copy(deep=True)
    _with_dc_delivery(after, _evidence())
    after.actuator_cases[0].feedback_loss_max = 1

    changes = {row.path: row for row in fact_diff(before, after).changes}

    loss = changes["actuator_cases/elbow.nominal/feedback_loss_max"]
    assert loss.limit_raised is True
    assert changes["actuator_cases/elbow.nominal/dc_delivery/return_node"].new == "supply.return"
