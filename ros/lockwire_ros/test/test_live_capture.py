"""End to end: a real IMU publishing, captured live, gated by the real core.

This is the demo the owner asked for, as a test. A genuine
`sensor_msgs/msg/Imu` publisher runs in its own process over real DDS; the gate
node subscribes to the topic its contract declares, writes a lockwire recording
from what actually arrived, and runs `lockwire preflight` on it.

Two cases, and the pair is the point:

- healthy publisher at 100 Hz -> **GO**, node reaches `inactive`
- faulted publisher at 5 Hz   -> **NO-GO**, node stays `unconfigured`

Without the second, a gate that refuses everything would pass the first. Without
the first, a gate that clears everything would pass the second.

Container-only, and needs the real core.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="no ROS environment; run this in the Humble container")

import rclpy  # noqa: E402
from rclpy.lifecycle import TransitionCallbackReturn  # noqa: E402

from lockwire_ros import core  # noqa: E402
from lockwire_ros.gate_node import LockwireGate  # noqa: E402

PRIMARY_STATE_UNCONFIGURED = 1
PRIMARY_STATE_INACTIVE = 2

REAL = os.environ.get("LOCKWIRE_REAL_EXECUTABLE", "")
PUBLISHER = str(Path(__file__).parent / "imu_publisher.py")

requires_real_core = pytest.mark.skipif(
    not REAL or not Path(REAL).exists(),
    reason="LIVE_CORE_UNTESTED: LOCKWIRE_REAL_EXECUTABLE unset or missing",
)

NOMINAL_HZ = 100.0
# 8 s, not 4: liveness divides by the capture window, so a slow DDS discovery under load
# eats into a short window and a healthy 100 Hz IMU can read NO-GO.
CAPTURE_S = 8.0

CONTRACT = """\
schema_version: "0.3"
robot:
  name: capturebot
families:
  - id: fam.imu.mems
    name: MEMS IMU
device_models:
  - id: mdl.imu.bmi088
    family: fam.imu.mems
devices:
  - id: dev.imu
    model: mdl.imu.bmi088
    owner: shivansh
    driver:
      node: /imu_driver
      topic: /imu/data
      nominal_rate_hz: {nominal}
      msg_type: sensor_msgs/msg/Imu
      signals:
        accel_z: {{field: linear_acceleration.z, unit: "m/s^2"}}
topology:
  hosts:
    - id: host.local
"""


@pytest.fixture(scope="module", autouse=True)
def _ros():
    rclpy.init()
    yield
    if rclpy.ok():
        rclpy.shutdown()


@pytest.fixture
def contract(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text(CONTRACT.format(nominal=NOMINAL_HZ))
    return str(path)


def _publisher(rate_hz: float, dds_deadline_s: float | None = None):
    """Start a real publisher process and wait until its topic is advertised."""
    args = [sys.executable, PUBLISHER, "--rate", str(rate_hz)]
    if dds_deadline_s is not None:
        args.extend(["--dds-deadline-s", str(dds_deadline_s)])
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = rclpy.create_node("imu_probe_{0}".format(int(rate_hz)))
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(topic == "/imu/data" for topic, _ in probe.get_topic_names_and_types()):
                return process
            time.sleep(0.2)
    finally:
        probe.destroy_node()
    process.kill()
    raise AssertionError("imu_publisher never advertised /imu/data")


def _gate(contract_path, tmp_path, node_name, capture_s=CAPTURE_S):
    node = LockwireGate(
        node_name,
        parameter_overrides=[
            rclpy.parameter.Parameter("contract", value=contract_path),
            rclpy.parameter.Parameter("lockwire_executable", value=REAL),
            rclpy.parameter.Parameter("capture_s", value=capture_s),
            rclpy.parameter.Parameter("capture_context", value="bench-idle"),
            rclpy.parameter.Parameter("capture_dir", value=str(tmp_path / node_name)),
            rclpy.parameter.Parameter("publish_diagnostics", value=False),
        ],
    )
    return node


@requires_real_core
def test_a_healthy_imu_captured_live_clears_the_gate(contract, tmp_path):
    publisher = _publisher(NOMINAL_HZ)
    node = _gate(contract, tmp_path, "gate_live_go")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS, node.result.reason
        assert node._state_machine.current_state[0] == PRIMARY_STATE_INACTIVE
        assert node.result.outcome == core.GO
        # Presence floor: a GO that scored nothing proves nothing.
        assert node.result.scored > 0
        # The recording came from live traffic, not from a file someone staged.
        samples = Path(node.captured_to) / "samples.jsonl"
        assert samples.exists()
        assert len(samples.read_text().splitlines()) > 100
    finally:
        node.destroy_node()
        publisher.kill()


@requires_real_core
def test_a_rate_collapsed_imu_captured_live_is_refused(contract, tmp_path):
    """5 Hz against a 100 Hz nominal is below the 50% gross-liveness floor."""
    publisher = _publisher(5.0)
    node = _gate(contract, tmp_path, "gate_live_nogo")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        # Under the core's floor (10 samples over 1 s) nothing is scored; say so, not "wrong verdict".
        times = [json.loads(line)["t"] for line in
                 (Path(node.captured_to) / "samples.jsonl").read_text().splitlines()]
        assert len(times) >= 10 and times[-1] - times[0] >= 1.0, "capture starved: {0}".format(times)
        assert node._state_machine.current_state[0] == PRIMARY_STATE_UNCONFIGURED
        assert node.result.outcome == core.NO_GO
        assert any("sustained_rate_liveness" in f["quantity"] for f in node.result.violations()), [
            f["quantity"] for f in node.result.violations()
        ]
    finally:
        node.destroy_node()
        publisher.kill()


@requires_real_core
def test_dds_requested_deadline_miss_is_retained_and_refused(tmp_path):
    """A real middleware event, followed by more IMU traffic, causes NO-GO."""
    declared_s = 0.1
    contract_path = tmp_path / "deadline.yaml"
    contract_path.write_text(
        CONTRACT.replace("nominal_rate_hz: {nominal}",
                         "nominal_rate_hz: {nominal}\n      dds_deadline_s: {deadline}")
        .format(nominal=5.0, deadline=declared_s)
    )
    publisher = _publisher(5.0, dds_deadline_s=declared_s)
    node = _gate(str(contract_path), tmp_path, "gate_dds_deadline")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert node.result.outcome == core.NO_GO
        assert any(f["quantity"] == "devices/dev.imu/dds_requested_deadline"
                   for f in node.result.violations()), node.result.violations()
        rows = [json.loads(line) for line in
                (Path(node.captured_to) / "samples.jsonl").read_text().splitlines()]
        misses = [i for i, row in enumerate(rows) if row.get("dds_deadline_missed")]
        assert misses and any(row.get("sig") for row in rows[misses[0] + 1:])
    finally:
        node.destroy_node()
        publisher.kill()


@requires_real_core
def test_a_silent_topic_is_refused_rather_than_passed(contract, tmp_path):
    """No publisher at all. Capture produces nothing, and nothing is not a pass.

    This is the case a naive capture gets wrong: writing an empty recording and
    letting the gate return "nothing scored", which exits 0.
    """
    node = _gate(contract, tmp_path, "gate_live_silent")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert node._state_machine.current_state[0] == PRIMARY_STATE_UNCONFIGURED
    finally:
        node.destroy_node()


@requires_real_core
def test_the_gate_works_when_driven_through_the_real_lifecycle_service(contract, tmp_path):
    """The production path, which `trigger_configure()` does not exercise.

    In real bringup nobody calls `trigger_configure()`. A launch file or an
    operator sends a `ChangeState` service request, and the callback then runs
    inside the node's executor — where a multi-second capture blocks the very
    executor that has to answer the service call.

    That divergence is invisible to every other case in this file: they call the
    transition directly on the test thread. If capture-inside-a-service-callback
    deadlocks or times out, this is the only test that says so.
    """
    from lifecycle_msgs.msg import State as StateMsg
    from lifecycle_msgs.srv import ChangeState, GetState
    from rclpy.executors import SingleThreadedExecutor

    publisher = _publisher(NOMINAL_HZ)
    node = _gate(contract, tmp_path, "gate_service_go")
    client_node = rclpy.create_node("gate_service_client")
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    executor.add_node(client_node)

    change = client_node.create_client(ChangeState, "/gate_service_go/change_state")
    get = client_node.create_client(GetState, "/gate_service_go/get_state")
    try:
        deadline = time.monotonic() + 20
        while not (change.service_is_ready() and get.service_is_ready()):
            executor.spin_once(timeout_sec=0.1)
            assert time.monotonic() < deadline, "lifecycle services never came up"

        request = ChangeState.Request()
        request.transition.id = 1  # TRANSITION_CONFIGURE
        future = change.call_async(request)

        # Generous: the capture window itself is CAPTURE_S, and the whole point
        # is that the executor is busy for that entire time.
        deadline = time.monotonic() + CAPTURE_S + 60
        while not future.done():
            executor.spin_once(timeout_sec=0.1)
            assert time.monotonic() < deadline, "configure never returned through the service"

        assert future.result().success, node.result.reason if node.result else "no result"

        state_future = get.call_async(GetState.Request())
        deadline = time.monotonic() + 20
        while not state_future.done():
            executor.spin_once(timeout_sec=0.1)
            assert time.monotonic() < deadline, "get_state never returned"

        assert state_future.result().current_state.id == StateMsg.PRIMARY_STATE_INACTIVE
        assert node.result.outcome == core.GO
    finally:
        executor.remove_node(node)
        executor.remove_node(client_node)
        client_node.destroy_node()
        node.destroy_node()
        publisher.kill()


# ---------------------------------------------------------------------------
# the one-command entry point and the includable launch file, on live traffic
# ---------------------------------------------------------------------------

GATE_LAUNCH = Path(__file__).parents[1] / "launch" / "lockwire_gate.launch.py"
ROBOT_LAUNCH = """
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource

def generate_launch_description():
    return LaunchDescription([IncludeLaunchDescription(
        PythonLaunchDescriptionSource({gate!r}),
        launch_arguments={{"contract": {contract!r}, "live": "true", "capture_s": "{seconds}",
                          "lockwire_executable": {real!r}, "bringup": {bringup!r}}}.items())])
"""
BRINGUP_LAUNCH = """
from launch import LaunchDescription
from launch.actions import LogInfo

def generate_launch_description():
    return LaunchDescription([LogInfo(msg="DOWNSTREAM-STARTED")])
"""


@requires_real_core
@pytest.mark.parametrize("rate_hz,code", [(NOMINAL_HZ, 0), (5.0, 1)])
def test_the_one_command_preflight_exits_with_the_live_verdict(contract, rate_hz, code):
    publisher = _publisher(rate_hz)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "lockwire_ros.preflight_cli", contract,
             "--seconds", str(CAPTURE_S), "--lockwire-executable", REAL],
            capture_output=True, text=True, timeout=180, check=False)
    finally:
        publisher.kill()
    assert result.returncode == code, result.stdout + result.stderr


@requires_real_core
@pytest.mark.parametrize("rate_hz,passes", [(NOMINAL_HZ, True), (5.0, False)])
def test_the_included_gate_holds_the_bringup_until_a_live_go(contract, tmp_path, rate_hz, passes):
    bringup = tmp_path / "bringup.launch.py"
    bringup.write_text(BRINGUP_LAUNCH)
    robot = tmp_path / "robot.launch.py"
    robot.write_text(ROBOT_LAUNCH.format(gate=str(GATE_LAUNCH), contract=contract,
                                         seconds=CAPTURE_S, real=REAL, bringup=str(bringup)))
    publisher = _publisher(rate_hz)
    try:
        result = subprocess.run(["ros2", "launch", str(robot)], capture_output=True, text=True,
                                timeout=180, check=False)
    finally:
        publisher.kill()
    output = result.stdout + result.stderr
    assert (result.returncode == 0, "DOWNSTREAM-STARTED" in output) == (passes, passes), output
    assert passes or "NO_GO" in output, output
