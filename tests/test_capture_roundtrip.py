"""What `lockwire_ros.capture` writes, `lockwire_pro.recording.load_recording` must read.

This is the seam that matters and the one place both sides are importable at
once. `capture.py` deliberately depends on nothing outside ros_core, so it
hand-rolls its manifest YAML rather than importing lockwire's writer — which
means the two halves could drift and every test on either side would stay green.
Only a real round trip catches that.

Then the recording goes through the actual gate, because a file that loads is
not the same claim as a file the gate can score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording
from lockwire.report import verdict
from lockwire.schema import Contract

from lockwire_ros.capture import DeviceCapture, captures_from_contract, write_recording


def _capture(device_id, topic, signals, rows):
    capture = DeviceCapture(device_id, topic, "sensor_msgs/msg/Imu", signals)
    capture.rows = rows
    return capture


def test_a_captured_recording_loads(tmp_path):
    capture = _capture(
        "dev.imu",
        "/imu/data",
        {"accel_z": ("linear_acceleration.z", "m/s^2")},
        [(100.0 + i * 0.005, {"accel_z": -9.81 + i * 0.001}) for i in range(200)],
    )

    out = write_recording(str(tmp_path / "rec"), [capture], "bench-idle")
    recording = load_recording(out)

    assert recording.context == "bench-idle"
    assert "dev.imu" in recording.devices
    assert len(recording.devices["dev.imu"].recv_times) == 200
    assert recording.unit_of("accel_z", device="dev.imu") == "m/s^2"
    assert recording.sha256


@pytest.mark.parametrize(
    ("kind", "procedure_id", "signal", "code"),
    [
        ("reset", "reset-v1", "reset_code", 3),
        ("watchdog", "watchdog-kick-v1", "watchdog_code", 7),
        ("update", "update-v1", "update_code", 9),
    ],
)
def test_procedure_event_round_trip_requires_its_full_declared_binding(
    tmp_path, kind, procedure_id, signal, code
):
    """Core owns loader validation; Humble only needs the raw evidence shape."""
    capture = DeviceCapture(
        "dev.imu",
        "/imu/data",
        "sensor_msgs/msg/Imu",
        {signal: ("status.code", "1")},
        procedures={procedure_id: (signal, code, kind)},
    )
    capture.rows = [(100.0, {signal: float(code)}), (100.2, {signal: 0.0})]
    capture.procedure_events = [(100.0, procedure_id)]
    out = Path(write_recording(str(tmp_path / "rec"), [capture], "bench-idle"))

    recording = load_recording(out)
    binding = (kind, signal, code)
    assert recording.procedures == {"dev.imu": {procedure_id: binding}}
    assert recording.procedure_events == {"dev.imu": {procedure_id: [(0.0, *binding)]}}

    rows = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
    wrong_kind = "watchdog" if kind == "reset" else "reset"
    for field, altered in (("kind", wrong_kind), ("signal", "other_signal"), ("code", code + 1)):
        altered_rows = json.loads(json.dumps(rows))
        altered_rows[1]["procedure_event"][field] = altered
        (out / "samples.jsonl").write_text(
            "\n".join(json.dumps(row) for row in altered_rows) + "\n"
        )
        with pytest.raises(ValueError, match="does not match its declaration"):
            load_recording(out)


def test_multi_device_capture_loads_with_interleaved_rows(tmp_path):
    """Interleaving is where a naive writer breaks load_recording's ordering rule."""
    imu = _capture(
        "dev.imu",
        "/imu/data",
        {"accel_z": ("linear_acceleration.z", "m/s^2")},
        [(100.0 + i * 0.01, {"accel_z": -9.81}) for i in range(50)],
    )
    baro = _capture(
        "dev.baro",
        "/baro",
        {"pressure_pa": ("fluid_pressure", "Pa")},
        [(100.005 + i * 0.05, {"pressure_pa": 101325.0}) for i in range(10)],
    )

    recording = load_recording(write_recording(str(tmp_path / "rec"), [imu, baro], "bench-idle"))

    assert set(recording.devices) == {"dev.imu", "dev.baro"}
    for stream in recording.devices.values():
        assert stream.recv_times == sorted(stream.recv_times)


def test_units_with_awkward_characters_survive_the_manifest(tmp_path):
    """`m/s^2` and `rad/s` are not plain YAML scalars; a bad quote breaks loading."""
    capture = _capture(
        "dev.imu",
        "/imu/data",
        {
            "accel_z": ("linear_acceleration.z", "m/s^2"),
            "gyro_x": ("angular_velocity.x", "rad/s"),
        },
        [(1.0, {"accel_z": -9.81, "gyro_x": 0.001})],
    )

    recording = load_recording(write_recording(str(tmp_path / "rec"), [capture], "bench-idle"))

    assert recording.unit_of("accel_z", device="dev.imu") == "m/s^2"
    assert recording.unit_of("gyro_x", device="dev.imu") == "rad/s"


def _imu_contract() -> Contract:
    return Contract.model_validate(
        {
            "schema_version": "0.3",
            "robot": {"name": "capturebot"},
            "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
            "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
            "devices": [
                {
                    "id": "dev.imu",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {
                        "node": "/imu_driver",
                        "topic": "/imu/data",
                        "msg_type": "sensor_msgs/msg/Imu",
                        "nominal_rate_hz": 100.0,
                        "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
                    },
                }
            ],
        }
    )


def test_a_captured_recording_runs_through_the_real_gate(tmp_path):
    """Loading is not scoring. This puts the captured file through run_preflight."""
    contract = _imu_contract()
    capture = captures_from_contract(contract.model_dump(mode="python"))
    assert [c.device_id for c in capture] == ["dev.imu"], "the plan came out of the real schema"
    capture[0].rows = [(50.0 + i * 0.01, {"accel_z": -9.81}) for i in range(300)]

    out = write_recording(str(tmp_path / "rec"), capture, "bench-idle")
    report = run_preflight(contract, RecordedSource(load_recording(out)))

    # The gate ran on captured data and reached a verdict rather than erroring.
    assert verdict(report).exit_code in (0, 1)
    assert report.scored is not None


@pytest.mark.parametrize(("window_s", "exit_code"), [(4.0, 1), (None, 0)])
def test_a_burst_inside_a_long_capture_window_is_not_live(tmp_path, window_s, exit_code):
    """64 messages delivered in ~1 s of a 4 s window: 16 Hz, not the 62 Hz the span says."""
    contract = _imu_contract()
    capture = captures_from_contract(contract.model_dump(mode="python"))
    capture[0].rows = [(50.0 + i * 0.016, {"accel_z": -9.81}) for i in range(64)]
    out = write_recording(str(tmp_path / "rec"), capture, "bench-idle", capture_window_s=window_s)
    recording = load_recording(out)
    assert recording.capture_window_s == window_s
    report = run_preflight(contract, RecordedSource(recording))
    assert verdict(report).exit_code == exit_code, [f.observed for f in report]


def test_the_plan_is_read_from_a_schema_validated_contract(tmp_path):
    """captures_from_contract must agree with what the schema (driver topic, msg_type, signals) actually emits.

    Building the plan from a hand-written dict everywhere else would let the
    reader and the schema drift apart silently.
    """
    contract = Contract.model_validate(
        {
            "schema_version": "0.3",
            "robot": {"name": "capturebot"},
            "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
            "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
            "devices": [
                {
                    "id": "dev.imu",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {
                        "topic": "/imu/data",
                        "msg_type": "sensor_msgs/msg/Imu",
                        "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
                    },
                },
                {
                    "id": "dev.plain",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {"topic": "/x"},
                },
            ],
        }
    )

    plan = captures_from_contract(contract.model_dump(mode="python"))

    assert [c.device_id for c in plan] == ["dev.imu"]
    assert plan[0].msg_type == "sensor_msgs/msg/Imu"
    assert plan[0].signals["accel_z"] == ("linear_acceleration.z", "m/s^2")


@pytest.mark.parametrize("context", ["bench-idle", "armed-idle", "nominal-op"])
def test_every_context_this_package_allows_is_one_lockwire_allows(context, tmp_path):
    """capture.py restates CONTEXTS; this pins the restatement against the real list."""
    capture = _capture(
        "dev.imu",
        "/imu/data",
        {"accel_z": ("linear_acceleration.z", "m/s^2")},
        [(1.0, {"accel_z": -9.81})],
    )
    recording = load_recording(write_recording(str(tmp_path / context), [capture], context))
    assert recording.context == context


def test_the_two_context_lists_have_not_drifted():
    from lockwire.schema import CONTEXTS as LOCKWIRE_CONTEXTS

    from lockwire_ros.capture import CONTEXTS as ROS_CONTEXTS

    assert tuple(ROS_CONTEXTS) == tuple(LOCKWIRE_CONTEXTS)


def test_the_vendored_field_path_resolver_is_byte_identical():
    """lockwire_ros must not import lockwire, so it carries a copy of fieldpath.py."""
    import lockwire.fieldpath as core
    import lockwire_ros.fieldpath as ros

    assert Path(ros.__file__).read_bytes() == Path(core.__file__).read_bytes()


def test_a_live_captured_go_still_discloses_the_m1b_boundary(tmp_path):
    """The correction of record, pinned.

    A capture is fresh robot data, but it still reaches the gate through
    `RecordedSource`, so it closes none of the permanent offline boundary. If a
    future change starts suppressing these disclosures for captured recordings,
    a live-captured GO would silently claim more than it checked — and this
    goes red.
    """
    from lockwire_pro.preflight.stages.coverage import M1B_CAPABILITIES

    contract = Contract.model_validate(
        {
            "schema_version": "0.3",
            "robot": {"name": "capturebot"},
            "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
            "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
            "devices": [
                {
                    "id": "dev.imu",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {
                        "topic": "/imu/data",
                        "msg_type": "sensor_msgs/msg/Imu",
                        "nominal_rate_hz": 100.0,
                        "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
                    },
                }
            ],
        }
    )
    capture = captures_from_contract(contract.model_dump(mode="python"))
    capture[0].rows = [(1.0 + i * 0.01, {"accel_z": -9.81}) for i in range(300)]

    out = write_recording(str(tmp_path / "rec"), capture, "bench-idle")
    report = run_preflight(contract, RecordedSource(load_recording(out)))

    disclosed = " ".join(f.observed + f.expected for f in report if f.severity == "not_scored")
    for capability in M1B_CAPABILITIES:
        assert capability in disclosed, f"a captured run stopped disclosing {capability!r}"


def test_the_coverage_doc_says_live_capture_does_not_move_the_boundary(tmp_path):
    """Copy pin: the doc must keep saying the live-hardware list does not move."""
    from pathlib import Path as _Path

    doc = _Path(__file__).resolve().parents[1] / "docs" / "gate_coverage.md"
    text = doc.read_text(encoding="utf-8")

    assert "Live capture (`lockwire_ros`)" in text
    assert "the live-hardware list above does not move" in text
