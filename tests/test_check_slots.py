"""Paid check slots: an empty slot says so, it never passes silently.

Runs unchanged in the free repo (no paid module exists) and with the paid plugin
installed (the slot is emptied here, on free-side names only)."""

from lockwire import checks, plugins
from lockwire.checks import run_record_checks
from lockwire.report import verdict
from lockwire.schema import Contract


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


def test_empty_actuator_slot_reports_not_run(monkeypatch):
    monkeypatch.setattr(plugins, "ensure", lambda: None)
    monkeypatch.setattr(checks, "PAID_CHECKS", {})
    report = run_record_checks(_contract())
    ours = [f for f in report if f.quantity == "actuator_cases/conformance"]
    assert len(ours) == 1
    assert ours[0].severity == "not_scored"
    assert ours[0].observed == "not run: needs Lockwire Pro"
    assert not [f for f in report if f.quantity.startswith("actuator_cases/elbow")]
    rest = [f for f in report if f is not ours[0]]
    assert verdict(report).code == verdict(rest).code
    assert verdict([ours[0]]).code == "GO"
