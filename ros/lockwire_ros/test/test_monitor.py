"""The continuous monitor's judgement, off-robot: windows in, one published verdict per window.

The check-that-fails is the first test: a monitor that published its verdict
once (as `gate_node` does at configure) publishes one row list over two
periods, not two. The live half, with a real IMU over DDS, is in
`test_live_monitor.py` and runs in the Humble container.
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from lockwire_ros import core, diagnostics
from lockwire_ros.capture import DeviceCapture
from lockwire_ros.monitor import Monitor, status_row

DOCS = {
    "GO": {"verdict": "GO", "exit_code": 0, "scored": 4, "counts": {}},
    "GO_WITH_NOTES": {"verdict": "GO_WITH_NOTES", "exit_code": 0, "scored": 4,
                      "counts": {"warning": 1}},
    "NOT_SCORED": {"verdict": "NOT_SCORED", "exit_code": 3, "scored": 0, "counts": {}},
    "NO-GO": {"verdict": "NO-GO", "exit_code": 1, "scored": 4, "counts": {"violation": 1},
              "findings": [{"severity": "violation", "quantity": "devices/dev.imu/rate_hz",
                            "observed": "5.0 Hz", "expected": "in [50, 150] Hz"}]},
}


def _imu():
    return DeviceCapture("dev.imu", "/imu/data", "sensor_msgs/msg/Imu",
                         {"accel_z": ("linear_acceleration.z", "m/s^2")})


def _feed(capture, start, stop, rate_hz=100.0):
    capture.rows += [(start + i / rate_hz, {"accel_z": -9.81})
                     for i in range(int((stop - start) * rate_hz))]


def _monitor(tmp_path, verdict="GO", gate=None, keep_last=0):
    published, seen = [], []

    def fake_gate(recording):
        with open(os.path.join(recording, "samples.jsonl")) as stream:
            seen.append([json.loads(line)["t"] for line in stream])
        return core.interpret(DOCS[verdict], DOCS[verdict]["exit_code"])

    capture = _imu()
    monitor = Monitor([capture], 10.0, "nominal-op", gate or fake_gate, published.append,
                      str(tmp_path / "out"), keep_last)
    return monitor, capture, published, seen


def test_every_period_publishes_and_each_window_holds_only_its_own_traffic(tmp_path):
    monitor, capture, published, seen = _monitor(tmp_path)
    _feed(capture, 0.0, 10.0)
    monitor.run_window(monitor.snapshot(10.0))
    _feed(capture, 10.0, 20.0)
    monitor.run_window(monitor.snapshot(20.0))

    assert len(published) >= 2
    assert [len(times) for times in seen] == [1000, 1000]
    assert len(capture.rows) == 1000  # the buffer is trimmed, not grown forever
    assert not os.path.exists(tmp_path / "out" / "window-000001")  # keep_last=0 cleans up


@pytest.mark.parametrize(
    ("verdict", "level"),
    [("GO", diagnostics.OK), ("GO_WITH_NOTES", diagnostics.WARN),
     ("NOT_SCORED", diagnostics.WARN), ("NO-GO", diagnostics.ERROR)],
)
def test_the_status_level_follows_the_verdict(tmp_path, verdict, level):
    monitor, capture, published, _ = _monitor(tmp_path, verdict)
    _feed(capture, 0.0, 10.0)
    status = status_row(monitor.run_window(monitor.snapshot(10.0)))
    assert status.level == level
    assert status.message == published[0][0].message
    if verdict == "NO-GO":
        assert ("lockwire: devices/dev.imu/rate_hz",
                "ERROR: observed 5.0 Hz, expected in [50, 150] Hz") in status.values


def test_a_gate_that_crashes_publishes_error_with_the_reason(tmp_path):
    def broken(recording):
        raise RuntimeError("lockwire binary vanished")

    monitor, capture, published, _ = _monitor(tmp_path, gate=broken)
    _feed(capture, 0.0, 10.0)
    monitor.run_window(monitor.snapshot(10.0))
    assert published[0][0].level == diagnostics.ERROR
    assert "lockwire binary vanished" in published[0][0].message


def test_a_window_with_no_traffic_publishes_error_not_silence(tmp_path):
    monitor, capture, published, seen = _monitor(tmp_path)
    _feed(capture, 0.0, 5.0)
    monitor.run_window(monitor.snapshot(20.0))  # all traffic is older than the window
    assert seen == []
    assert published[0][0].level == diagnostics.ERROR
    assert "no messages captured" in published[0][0].message


def test_keep_last_keeps_only_the_newest_recordings(tmp_path):
    monitor, capture, _, _ = _monitor(tmp_path, keep_last=1)
    for start in (0.0, 10.0):
        _feed(capture, start, start + 10.0)
        monitor.run_window(monitor.snapshot(start + 10.0))
    assert sorted(os.listdir(tmp_path / "out")) == ["window-000002"]
    monitor.close()
    assert os.path.exists(tmp_path / "out")


def test_a_field_that_does_not_resolve_publishes_error_naming_it_every_window(tmp_path, monkeypatch):
    # The handler path, ROS-free: message_to_ordereddict is the identity on a dict.
    monkeypatch.setitem(sys.modules, "rosidl_runtime_py",
                        types.SimpleNamespace(message_to_ordereddict=dict))
    monitor, capture, published, seen = _monitor(tmp_path)
    capture.signals = {"accel_w": ("linear_acceleration.w", "m/s^2")}
    for window_end in (10.0, 20.0):
        monitor.take(capture, {"linear_acceleration": {"x": 0.0, "z": -9.81}}, window_end - 1.0)
        monitor.run_window(monitor.snapshot(window_end), monitor.take_faults())
        assert published[-1][0].level == diagnostics.ERROR
        assert "linear_acceleration.w" in published[-1][0].message
        assert "/imu/data" in published[-1][0].message
    assert len(published) == 2 and seen == []
