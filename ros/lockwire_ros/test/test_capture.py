"""Capture's judgement, tested off-robot.

`lockwire_ros.capture` imports nothing from ROS at module level, so the parts
that decide things — walking a field path, refusing an empty capture, and
writing native evidence — run under Humble without importing Lockwire core.
The core round-trip test owns `load_recording` proof in its supported Python
environment.

The claim throughout: **capture never invents a number.** A missing field, a
non-numeric field, or a topic that produced nothing is an error, not a default.
A capture that quietly wrote 0.0 would hand the gate a healthy-looking value
with no evidence behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lockwire_ros.capture import (
    CaptureError,
    DeviceCapture,
    captures_from_contract,
    resolve_field,
    write_recording,
)

IMU_PAYLOAD = {
    "header": {"stamp": {"sec": 12, "nanosec": 500}, "frame_id": "imu_link"},
    "linear_acceleration": {"x": 0.01, "y": -0.02, "z": -9.81},
    "angular_velocity": {"x": 0.001, "y": 0.0, "z": 0.0},
    "orientation_covariance": [0.0, 0.0],
}


# ---------------------------------------------------------------------------
# field resolution
# ---------------------------------------------------------------------------


def test_resolves_a_nested_field():
    assert resolve_field(IMU_PAYLOAD, "linear_acceleration.z") == -9.81


def test_resolves_a_deeper_nested_field():
    assert resolve_field(IMU_PAYLOAD, "header.stamp.sec") == 12.0


JOINTS = {"name": ["hip", "knee"], "position": [0.1, 0.2], "effort": []}


def test_resolves_array_fields_by_index_and_by_joint_name():
    assert resolve_field(JOINTS, "position[1]") == 0.2
    assert resolve_field(JOINTS, "position[name=knee]") == 0.2


def test_an_empty_effort_array_is_refused_not_defaulted():
    with pytest.raises(CaptureError, match="effort has 0 entries"):
        resolve_field(JOINTS, "effort[name=knee]")


def test_a_missing_field_raises_rather_than_defaulting():
    """The whole point: no silent 0.0."""
    with pytest.raises(CaptureError, match="does not resolve"):
        resolve_field(IMU_PAYLOAD, "linear_acceleration.w")


def test_a_missing_intermediate_names_where_the_walk_stopped():
    with pytest.raises(CaptureError, match="does not resolve"):
        resolve_field(IMU_PAYLOAD, "no_such_block.z")


def test_a_non_numeric_field_is_refused():
    with pytest.raises(CaptureError, match="not a number"):
        resolve_field(IMU_PAYLOAD, "header.frame_id")


def test_a_sub_message_is_refused_rather_than_stringified():
    with pytest.raises(CaptureError, match="not a number"):
        resolve_field(IMU_PAYLOAD, "linear_acceleration")


# ---------------------------------------------------------------------------
# reading the plan out of a contract
# ---------------------------------------------------------------------------


CONTRACT = {
    "devices": [
        {
            "id": "dev.imu",
            "driver": {
                "topic": "/imu/data",
                "msg_type": "sensor_msgs/msg/Imu",
                "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
            },
        },
        {"id": "dev.gnss", "driver": {"topic": "/fix", "nominal_rate_hz": 5.0}},
        {"id": "dev.nodriver"},
    ]
}


def test_only_devices_declaring_a_mapping_are_captured():
    """Devices without a capture mapping say nothing about capture and are skipped, not failed."""
    plan = captures_from_contract(CONTRACT)
    assert [c.device_id for c in plan] == ["dev.imu"]
    assert plan[0].topic == "/imu/data"
    assert plan[0].signals == {"accel_z": ("linear_acceleration.z", "m/s^2")}


@pytest.mark.parametrize(
    ("kind", "procedure_id", "signal", "code"),
    [
        ("reset", "reset-v1", "reset_code", 3),
        ("watchdog", "watchdog-kick-v1", "watchdog_code", 7),
        ("update", "update-v1", "update_code", 9),
    ],
)
def test_declared_procedure_is_bound_to_its_declared_capture_signal(
    kind, procedure_id, signal, code
):
    contract = {
        "devices": [{
            "id": f"dev.{kind}",
            "driver": {
                "topic": f"/{kind}/status",
                "msg_type": "std_msgs/msg/UInt8",
                "signals": {signal: {"field": "data", "unit": "1"}},
                "procedures": [{
                    "id": procedure_id,
                    "kind": kind,
                    "signal": signal,
                    "code": code,
                }],
            },
        }],
    }
    plan = captures_from_contract(contract)

    assert plan[0].procedures == {procedure_id: (signal, code, kind)}

    contract["devices"][0]["driver"]["procedures"][0]["signal"] = "missing"
    with pytest.raises(CaptureError, match="declared signal"):
        captures_from_contract(contract)


def test_a_hand_edited_signal_missing_its_unit_is_refused():
    """Schema validation makes this unreachable; caught anyway, loudly."""
    contract = {
        "devices": [
            {
                "id": "dev.imu",
                "driver": {
                    "topic": "/imu/data",
                    "msg_type": "sensor_msgs/msg/Imu",
                    "signals": {"accel_z": {"field": "linear_acceleration.z"}},
                },
            }
        ]
    }
    with pytest.raises(CaptureError, match="missing field or unit"):
        captures_from_contract(contract)


def test_a_contract_with_no_mappings_yields_an_empty_plan():
    assert captures_from_contract({"devices": [{"id": "dev.x"}]}) == []


def _x500_gnss_capture_contract(name):
    """Bind the ROS-only capture plan to exact fixture declarations without a YAML dependency."""
    root = Path(__file__).resolve().parents[3]
    text = (root / "tests" / "fixtures" / "x500" / name).read_text(encoding="utf-8")
    for declaration in (
        "topic: /fmu/out/vehicle_gps_position",
        "msg_type: px4_msgs/msg/SensorGps",
        "latitude_deg: {field: latitude_deg, unit: deg}",
        "longitude_deg: {field: longitude_deg, unit: deg}",
        "gnss_altitude_m: {field: altitude_msl_m, unit: m}",
        "PX4-Autopilot v1.16.2@54f0455ffcd755534539a7cf33a09a20bf71d29d",
        "PX4/px4_msgs v1.16.2@392e831c1f659429ca83902e66820d7094591410",
        "This source-derived descriptor does not prove live topic emission.",
    ):
        assert declaration in text
    return {
        "devices": [{
            "id": "dev.gnss.here3-plus",
            "driver": {
                "topic": "/fmu/out/vehicle_gps_position",
                "msg_type": "px4_msgs/msg/SensorGps",
                "signals": {
                    "latitude_deg": {"field": "latitude_deg", "unit": "deg"},
                    "longitude_deg": {"field": "longitude_deg", "unit": "deg"},
                    "gnss_altitude_m": {"field": "altitude_msl_m", "unit": "m"},
                },
            },
        }],
    }


@pytest.mark.parametrize("name", ["contract.yaml", "contract_corrected.yaml"])
def test_x500_gnss_capture_is_pinned_to_the_declared_px4_sensor_gps_interface(name):
    """The contract records a source-derived PX4 DDS map, not live traffic."""
    contract = _x500_gnss_capture_contract(name)
    plan = captures_from_contract(contract)
    gnss = next(capture for capture in plan if capture.device_id == "dev.gnss.here3-plus")

    assert gnss.topic == "/fmu/out/vehicle_gps_position"
    assert gnss.msg_type == "px4_msgs/msg/SensorGps"
    assert gnss.signals == {
        "latitude_deg": ("latitude_deg", "deg"),
        "longitude_deg": ("longitude_deg", "deg"),
        "gnss_altitude_m": ("altitude_msl_m", "m"),
    }


def test_x500_gnss_capture_refuses_a_missing_declared_px4_field():
    """A source-map field absent from the received message never becomes a value."""
    contract = _x500_gnss_capture_contract("contract.yaml")
    plan = captures_from_contract(contract)
    gnss = next(capture for capture in plan if capture.device_id == "dev.gnss.here3-plus")

    with pytest.raises(CaptureError, match="altitude_msl_m.*does not resolve"):
        resolve_field(
            {"latitude_deg": 43.0, "longitude_deg": -79.0},
            gnss.signals["gnss_altitude_m"][0],
        )


def test_x500_gnss_capture_refuses_a_hand_edited_missing_unit():
    """The descriptor must retain the unit explicitly declared by SensorGps."""
    contract = _x500_gnss_capture_contract("contract.yaml")
    del contract["devices"][0]["driver"]["signals"]["latitude_deg"]["unit"]

    with pytest.raises(CaptureError, match="missing field or unit"):
        captures_from_contract(contract)


def test_dds_deadline_plan_requires_an_integral_nanosecond_count():
    driver = {
        "topic": "/imu/data",
        "msg_type": "sensor_msgs/msg/Imu",
        "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
    }
    plan = captures_from_contract({"devices": [{"id": "dev.imu", "driver": driver | {
        "dds_deadline_s": 3e-9,
    }}]})
    assert plan[0].dds_deadline_s == 3e-9

    with pytest.raises(CaptureError, match="integral number of nanoseconds"):
        captures_from_contract({"devices": [{"id": "dev.imu", "driver": driver | {
            "dds_deadline_s": 1.5e-9,
        }}]})


# ---------------------------------------------------------------------------
# writing a recording
# ---------------------------------------------------------------------------


def _imu_capture(rows):
    capture = DeviceCapture(
        "dev.imu",
        "/imu/data",
        "sensor_msgs/msg/Imu",
        {"accel_z": ("linear_acceleration.z", "m/s^2")},
    )
    capture.rows = rows
    return capture


def test_writes_a_manifest_and_samples(tmp_path):
    capture = _imu_capture([(100.0, {"accel_z": -9.81}), (100.2, {"accel_z": -9.80})])

    out = write_recording(str(tmp_path / "rec"), [capture], "bench-idle")

    manifest = (Path(out) / "manifest.yaml").read_text()
    assert "context: bench-idle" in manifest
    assert "dev.imu:" in manifest
    # Units must be quoted — `m/s^2` is not a plain YAML scalar.
    assert 'accel_z: "m/s^2"' in manifest

    rows = [json.loads(line) for line in (Path(out) / "samples.jsonl").read_text().splitlines()]
    assert [r["dev"] for r in rows] == ["dev.imu", "dev.imu"]
    assert rows[0]["sig"]["accel_z"] == -9.81


def test_times_are_rebased_so_the_first_sample_is_zero(tmp_path):
    """recv times come from time.monotonic(); the absolute epoch is meaningless."""
    capture = _imu_capture([(5000.0, {"accel_z": -9.8}), (5000.5, {"accel_z": -9.8})])

    out = write_recording(str(tmp_path / "rec"), [capture], "bench-idle")

    rows = [json.loads(line) for line in (Path(out) / "samples.jsonl").read_text().splitlines()]
    assert rows[0]["t"] == 0.0
    assert rows[1]["t"] == pytest.approx(0.5)


def test_rows_come_out_in_nondecreasing_time_order(tmp_path):
    """load_recording rejects out-of-order recv times, so the writer must sort."""
    imu = _imu_capture([(10.0, {"accel_z": -9.8}), (10.4, {"accel_z": -9.8})])
    other = DeviceCapture(
        "dev.baro",
        "/baro",
        "sensor_msgs/msg/FluidPressure",
        {"pressure_pa": ("fluid_pressure", "Pa")},
    )
    other.rows = [(10.2, {"pressure_pa": 101325.0})]

    out = write_recording(str(tmp_path / "rec"), [imu, other], "bench-idle")

    rows = [json.loads(line) for line in (Path(out) / "samples.jsonl").read_text().splitlines()]
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)
    assert [r["dev"] for r in rows] == ["dev.imu", "dev.baro", "dev.imu"]


def test_an_empty_capture_is_refused_not_written(tmp_path):
    """An empty recording would sail through the gate as 'nothing scored'.

    Refusing here is what turns "the driver was dead" into a refusal instead of
    a vacuous pass.
    """
    silent = _imu_capture([])

    with pytest.raises(CaptureError, match="no messages captured"):
        write_recording(str(tmp_path / "rec"), [silent], "bench-idle")

    assert not (tmp_path / "rec" / "samples.jsonl").exists()


def test_the_refusal_names_the_topics_that_were_silent(tmp_path):
    with pytest.raises(CaptureError, match="/imu/data"):
        write_recording(str(tmp_path / "rec"), [_imu_capture([])], "bench-idle")


def test_an_unknown_context_is_refused(tmp_path):
    """CONTEXTS is restated in this package; a drift must fail here, not at the gate."""
    with pytest.raises(CaptureError, match="unknown context"):
        write_recording(
            str(tmp_path / "rec"), [_imu_capture([(1.0, {"accel_z": -9.8})])], "in-flight"
        )


def test_a_device_that_produced_nothing_is_left_out_of_the_manifest(tmp_path):
    """Declaring units for a stream with no rows would claim evidence we lack."""
    live = _imu_capture([(1.0, {"accel_z": -9.8})])
    silent = DeviceCapture(
        "dev.baro",
        "/baro",
        "sensor_msgs/msg/FluidPressure",
        {"pressure_pa": ("fluid_pressure", "Pa")},
    )

    out = write_recording(str(tmp_path / "rec"), [live, silent], "bench-idle")

    manifest = (Path(out) / "manifest.yaml").read_text()
    assert "dev.imu:" in manifest
    assert "dev.baro" not in manifest


def test_dds_deadline_capture_writes_event_without_changing_message_rows(tmp_path):
    capture = _imu_capture([(100.0, {"accel_z": -9.81}),
                            (100.3, {"accel_z": -9.80})])
    capture.dds_deadline_s = 0.1
    capture.deadline_misses.append((100.15, 1))
    out = Path(write_recording(str(tmp_path / "rec"), [capture], "bench-idle"))
    assert "dds_deadlines:\n  dev.imu: 0.1" in (out / "manifest.yaml").read_text()
    rows = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
    assert [row["t"] for row in rows] == [0.0, 0.15, 0.3]
    assert rows[1] == {"t": 0.15, "dev": "dev.imu", "dds_deadline_missed": 1}
    assert [row["sig"]["accel_z"] for row in (rows[0], rows[2])] == [-9.81, -9.8]


def test_declared_watchdog_marker_is_written_as_an_event_row(tmp_path):
    """A selected numeric marker is observed; capture never sends a command."""
    capture = DeviceCapture(
        "dev.imu",
        "/imu/data",
        "sensor_msgs/msg/Imu",
        {"watchdog_code": ("status.code", "1")},
        procedures={"watchdog-kick-v1": ("watchdog_code", 7, "watchdog")},
    )
    capture.rows = [(100.0, {"watchdog_code": 7.0}), (100.2, {"watchdog_code": 0.0})]
    # `take()` normally appends this only after resolving the declared message
    # field. This pure writer test supplies its already-observed event so it
    # remains runnable outside the Humble container.
    capture.procedure_events = [(100.0, "watchdog-kick-v1")]

    out = Path(write_recording(str(tmp_path / "rec"), [capture], "bench-idle"))
    rows = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]

    assert "procedures:" in (out / "manifest.yaml").read_text()
    evidence = {
        "id": "watchdog-kick-v1", "kind": "watchdog", "signal": "watchdog_code", "code": 7,
    }
    assert "signal: \"watchdog_code\"" in (out / "manifest.yaml").read_text()
    assert "code: 7" in (out / "manifest.yaml").read_text()
    assert rows[1] == {"t": 0.0, "dev": "dev.imu", "procedure_event": evidence}
