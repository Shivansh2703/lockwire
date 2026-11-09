"""The monitor live: a real IMU over DDS, a real core, `/lockwire/status` read like a robot would.

Healthy at 100 Hz -> repeated OK statuses; a late subscriber still gets the last
one (latched); the IMU collapses to 5 Hz -> the status flips to ERROR (NO-GO).
Container-only, and needs the real core.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="no ROS environment; run this in the Humble container")

import rclpy  # noqa: E402
from diagnostic_msgs.msg import DiagnosticStatus  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402

from lockwire_ros.monitor_node import STATUS_QOS, MonitorNode  # noqa: E402

REAL = os.environ.get("LOCKWIRE_REAL_EXECUTABLE", "")
PUBLISHER = str(Path(__file__).parent / "imu_publisher.py")
CONTRACT = """\
schema_version: "0.3"
robot: {name: monitorbot}
families: [{id: fam.imu.mems, name: MEMS IMU}]
device_models: [{id: mdl.imu.bmi088, family: fam.imu.mems}]
devices:
  - id: dev.imu
    model: mdl.imu.bmi088
    owner: shivansh
    driver:
      node: /imu_driver
      topic: /imu/data
      nominal_rate_hz: 100.0
      msg_type: sensor_msgs/msg/Imu
      signals: {accel_z: {field: linear_acceleration.z, unit: "m/s^2"}}
topology: {hosts: [{id: host.local}]}
"""


@pytest.mark.skipif(not REAL or not Path(REAL).exists(),
                    reason="LIVE_CORE_UNTESTED: LOCKWIRE_REAL_EXECUTABLE unset or missing")
def test_the_status_repeats_is_latched_and_flips_to_error_on_rate_collapse(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text(CONTRACT)
    rclpy.init()
    publisher = subprocess.Popen([sys.executable, PUBLISHER, "--rate", "100"])
    node = MonitorNode("lockwire_monitor_live", parameter_overrides=[
        rclpy.parameter.Parameter("contract", value=str(contract)),
        rclpy.parameter.Parameter("lockwire_executable", value=REAL),
        rclpy.parameter.Parameter("context", value="bench-idle"),
        rclpy.parameter.Parameter("window_s", value=3.0),
        rclpy.parameter.Parameter("period_s", value=3.0),
        rclpy.parameter.Parameter("timeout_s", value=60.0),
    ])
    listener = rclpy.create_node("status_listener")
    executor = SingleThreadedExecutor()
    seen, late = [], []
    listener.create_subscription(DiagnosticStatus, "/lockwire/status", seen.append, STATUS_QOS)

    def spin_until(done, limit=60.0):
        deadline = time.monotonic() + limit
        while not done() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.1)
        assert done(), [(s.level, s.message) for s in seen]

    try:
        node.start()
        executor.add_node(node)
        executor.add_node(listener)
        spin_until(lambda: sum(s.level == b"\x00" for s in seen) >= 2)
        assert "GO" in seen[-1].message

        listener.create_subscription(DiagnosticStatus, "/lockwire/status", late.append, STATUS_QOS)
        spin_until(lambda: late, 10.0)

        publisher.kill()
        publisher.wait()
        publisher = subprocess.Popen([sys.executable, PUBLISHER, "--rate", "5"])
        spin_until(lambda: "NO-GO" in seen[-1].message)
        assert seen[-1].level == b"\x02"
    finally:
        publisher.kill()
        node.close()
        executor.shutdown()
        node.destroy_node()
        listener.destroy_node()
        rclpy.shutdown()


def test_a_field_that_does_not_resolve_latches_error_and_the_node_keeps_running(tmp_path):
    # No core needed: a window with a fault is refused before the gate runs.
    contract = tmp_path / "lockwire.yaml"
    contract.write_text(CONTRACT.replace("linear_acceleration.z", "linear_acceleration.w"))
    rclpy.init()
    publisher = subprocess.Popen([sys.executable, PUBLISHER, "--rate", "100"])
    node = MonitorNode("lockwire_monitor_fault", parameter_overrides=[
        rclpy.parameter.Parameter("contract", value=str(contract)),
        rclpy.parameter.Parameter("window_s", value=2.0),
        rclpy.parameter.Parameter("period_s", value=2.0),
    ])
    listener = rclpy.create_node("fault_listener")
    executor = SingleThreadedExecutor()
    seen = []
    listener.create_subscription(DiagnosticStatus, "/lockwire/status", seen.append, STATUS_QOS)
    try:
        node.start()
        executor.add_node(node)
        executor.add_node(listener)
        deadline = time.monotonic() + 30.0
        while len(seen) < 2 and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.1)  # a CaptureError in a handler would raise here
        assert len(seen) >= 2, [(s.level, s.message) for s in seen]
        assert all(s.level == b"\x02" and "linear_acceleration.w" in s.message for s in seen)
    finally:
        publisher.kill()
        node.close()
        executor.shutdown()
        node.destroy_node()
        listener.destroy_node()
        rclpy.shutdown()
