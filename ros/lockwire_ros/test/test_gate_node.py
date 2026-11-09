"""The lifecycle gate, exercised against a real rclpy node.

Skipped off-robot, executed inside the Humble container. What it proves is the
one claim the whole package rests on: **a refusal actually leaves the node
Unconfigured**, so anything a bringup registered behind its activation never
starts. Asserting on the returned enum alone would not prove that — the
lifecycle state machine is the thing that has to agree, so these read the state
back from the node after the transition.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

rclpy = pytest.importorskip("rclpy", reason="no ROS environment; run this in the Humble container")

from rclpy.lifecycle import TransitionCallbackReturn  # noqa: E402

from lockwire_ros import core  # noqa: E402
from lockwire_ros.gate_node import LockwireGate  # noqa: E402

# lifecycle_msgs/msg/State primary-state ids, restated so this file needs no extra import.
PRIMARY_STATE_UNCONFIGURED = 1
PRIMARY_STATE_INACTIVE = 2

CLEAN_DOCUMENT = {
    "verdict": "GO",
    "exit_code": 0,
    "counts": {"violation": 0, "warning": 0, "not_scored": 2},
    "scored": 31,
    "findings": [
        {
            "severity": "not_scored",
            "quantity": "preflight/coverage/live_recon",
            "observed": "permanently out of scope offline (requires live hardware)",
            "expected": "a powered-on robot",
            "action": "check it on the powered-on robot",
        }
    ],
}
NO_GO_DOCUMENT = {
    "verdict": "NO-GO",
    "exit_code": 1,
    "counts": {"violation": 1, "warning": 0, "not_scored": 0},
    "scored": 31,
    "findings": [
        {
            "severity": "violation",
            "quantity": "devices/dev.gnss/rate_hz",
            "observed": "0.20 Hz",
            "expected": "in [4.5, 5.5] Hz",
            "action": "check the GNSS driver",
        }
    ],
}


@pytest.fixture(scope="module", autouse=True)
def _ros():
    rclpy.init()
    yield
    if rclpy.ok():
        rclpy.shutdown()


def _fake_lockwire(tmp_path, document=None, exit_code=0, name="lockwire"):
    """A real executable on disk — the process boundary under test is not mocked."""
    if document is None:
        body = "import sys\nsys.exit({0})\n".format(exit_code)
    else:
        body = textwrap.dedent(
            """
            import json, sys
            json.dump({0}, open(sys.argv[sys.argv.index("--json") + 1], "w"))
            sys.exit({1})
            """
        ).format(json.dumps(document), exit_code)
    script = tmp_path / name
    script.write_text("#!" + sys.executable + "\n" + body)
    script.chmod(0o755)
    return str(script)


def _gate(tmp_path, executable, node_name, **overrides):
    node = LockwireGate(
        node_name,
        parameter_overrides=[
            rclpy.parameter.Parameter("contract", value="lockwire.yaml"),
            rclpy.parameter.Parameter("recording", value=str(tmp_path)),
            rclpy.parameter.Parameter("lockwire_executable", value=executable),
            rclpy.parameter.Parameter("publish_diagnostics", value=False),
            *[rclpy.parameter.Parameter(k, value=v) for k, v in overrides.items()],
        ],
    )
    return node


def _state_id(node):
    """The node's own view of where the state machine landed."""
    return node._state_machine.current_state[0]


def test_a_go_configures_the_node(tmp_path):
    node = _gate(tmp_path, _fake_lockwire(tmp_path, CLEAN_DOCUMENT, 0), "gate_go")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
        assert _state_id(node) == PRIMARY_STATE_INACTIVE
        assert node.result.outcome == core.GO
    finally:
        node.destroy_node()


def test_a_no_go_leaves_the_node_unconfigured(tmp_path):
    """The claim of record: refusing is not just a return value, it holds the door."""
    node = _gate(tmp_path, _fake_lockwire(tmp_path, NO_GO_DOCUMENT, 1), "gate_nogo")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert _state_id(node) == PRIMARY_STATE_UNCONFIGURED
        assert node.result.outcome == core.NO_GO
    finally:
        node.destroy_node()


def test_a_missing_tool_also_leaves_the_node_unconfigured(tmp_path):
    """The gate could not run. That is not a pass, and the state machine must show it."""
    node = _gate(tmp_path, "lockwire-does-not-exist-anywhere", "gate_missing")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert _state_id(node) == PRIMARY_STATE_UNCONFIGURED
        assert node.result.outcome == core.REFUSED
    finally:
        node.destroy_node()


def test_exit_2_tool_failure_leaves_the_node_unconfigured(tmp_path):
    node = _gate(tmp_path, _fake_lockwire(tmp_path, None, 2), "gate_exit2")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert _state_id(node) == PRIMARY_STATE_UNCONFIGURED
        assert node.result.outcome == core.REFUSED
    finally:
        node.destroy_node()


def test_a_vacuous_pass_is_refused_by_default(tmp_path):
    """`NOT_SCORED` is the producer's zero-denominator verdict and exits 3."""
    document = dict(CLEAN_DOCUMENT, verdict="NOT_SCORED", exit_code=3, scored=0)
    node = _gate(tmp_path, _fake_lockwire(tmp_path, document, 3), "gate_vacuous")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert _state_id(node) == PRIMARY_STATE_UNCONFIGURED
    finally:
        node.destroy_node()


def test_a_vacuous_pass_can_be_opted_into_deliberately(tmp_path):
    document = dict(CLEAN_DOCUMENT, verdict="NOT_SCORED", exit_code=3, scored=0)
    node = _gate(
        tmp_path,
        _fake_lockwire(tmp_path, document, 3),
        "gate_vacuous_ok",
        refuse_when_nothing_scored=False,
    )
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
        assert _state_id(node) == PRIMARY_STATE_INACTIVE
    finally:
        node.destroy_node()


def test_no_recording_is_a_refusal_not_a_pass(tmp_path):
    node = LockwireGate(
        "gate_norec",
        parameter_overrides=[rclpy.parameter.Parameter("publish_diagnostics", value=False)],
    )
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert _state_id(node) == PRIMARY_STATE_UNCONFIGURED
    finally:
        node.destroy_node()


def test_diagnostics_publish_on_a_real_node(tmp_path):
    """The /diagnostics path is exercised, not just the pure mapping."""
    from diagnostic_msgs.msg import DiagnosticArray

    received = []
    node = _gate(
        tmp_path,
        _fake_lockwire(tmp_path, NO_GO_DOCUMENT, 1),
        "gate_diag",
        publish_diagnostics=True,
    )
    listener = rclpy.create_node("gate_diag_listener")
    listener.create_subscription(DiagnosticArray, "/diagnostics", received.append, 10)
    try:
        node.trigger_configure()
        deadline = 40
        while not received and deadline:
            rclpy.spin_once(listener, timeout_sec=0.1)
            deadline -= 1
        assert received, "no DiagnosticArray arrived on /diagnostics"
        names = [status.name for status in received[0].status]
        assert "lockwire: preflight" in names
        assert "lockwire: devices/dev.gnss/rate_hz" in names
    finally:
        listener.destroy_node()
        node.destroy_node()
