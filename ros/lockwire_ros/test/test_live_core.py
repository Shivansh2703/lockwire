"""The live path: the 3.10 node driving the REAL lockwire core on 3.12.

Every other test in this package runs the gate against a stand-in `lockwire`
executable. That proves the node's logic and proves nothing about the actual
arrangement, and a suite that only ever exercises the stand-in is the failure
mode the house law names by name ("where a protocol has a mock and a live
implementation, the suite almost certainly exercises only the mock").

So this file runs the whole thing for real: a `LockwireGate` on the distro's
Python 3.10 invoking a genuine lockwire install on Python 3.12, against a
committed contract and committed bags, and asserts the lifecycle state machine
lands where the verdict says it should.

**The skip is loud on purpose.** ``LOCKWIRE_REAL_EXECUTABLE`` must point at a
real lockwire binary; the Humble container harness sets it after installing the
core into its own 3.12 venv. If it is unset these cases skip with
``LIVE_CORE_UNTESTED`` in the reason so the gap is countable in the run summary
rather than invisible — the same convention as ``tests/test_live_source.py``.
"""

from __future__ import annotations

import os
import subprocess
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
#: Repo root inside the container harness; the committed fixtures live under it.
REPO = Path(os.environ.get("LOCKWIRE_REPO_ROOT", "/work"))

requires_real_core = pytest.mark.skipif(
    not REAL or not Path(REAL).exists(),
    reason="LIVE_CORE_UNTESTED: LOCKWIRE_REAL_EXECUTABLE unset or missing",
)


@pytest.fixture(scope="module", autouse=True)
def _ros():
    rclpy.init()
    yield
    if rclpy.ok():
        rclpy.shutdown()


def _gate(node_name, bag, **overrides):
    node = LockwireGate(
        node_name,
        parameter_overrides=[
            rclpy.parameter.Parameter(
                "contract", value=str(REPO / "tests/fixtures/contract_clean.yaml")
            ),
            rclpy.parameter.Parameter("recording", value=str(REPO / "tests/fixtures/bags" / bag)),
            rclpy.parameter.Parameter("lockwire_executable", value=REAL),
            rclpy.parameter.Parameter("publish_diagnostics", value=False),
            *[rclpy.parameter.Parameter(k, value=v) for k, v in overrides.items()],
        ],
    )
    return node


@requires_real_core
def test_the_two_interpreters_really_are_different():
    """The premise of the whole package, asserted rather than assumed.

    If these ever match, the split has collapsed and the Humble claim is no
    longer being tested by anything below.
    """
    import sys

    core_version = subprocess.run(
        [REAL, "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert core_version.strip(), "the real core did not report a version"
    assert sys.version_info[:2] == (3, 10), "this test only means something on Humble's 3.10"


@requires_real_core
def test_real_core_clean_bag_configures_the_node():
    node = _gate("live_go", "clean")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS, node.result.reason
        assert node._state_machine.current_state[0] == PRIMARY_STATE_INACTIVE
        assert node.result.outcome == core.GO
        # Presence floor: a GO with a zero denominator proves nothing.
        assert node.result.scored > 0
    finally:
        node.destroy_node()


@requires_real_core
def test_real_core_planted_fault_leaves_the_node_unconfigured():
    """A real gate, a real refusal, and a door that really stays shut."""
    node = _gate("live_nogo", "rate_collapse")
    try:
        assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert node._state_machine.current_state[0] == PRIMARY_STATE_UNCONFIGURED
        assert node.result.outcome == core.NO_GO
        assert any(f["quantity"] == "devices/dev.gnss/rate_hz" for f in node.result.violations()), [
            f["quantity"] for f in node.result.violations()
        ]
    finally:
        node.destroy_node()


@requires_real_core
def test_real_core_discloses_what_the_go_did_not_cover():
    """The not_scored lines must survive the whole trip: core -> JSON -> node."""
    node = _gate("live_disclose", "clean")
    try:
        node.trigger_configure()
        assert node.result.counts.get("not_scored", 0) > 0
        disclosures = [f for f in node.result.findings if f["severity"] == "not_scored"]
        assert any("live hardware" in f["expected"] + f["observed"] for f in disclosures)
    finally:
        node.destroy_node()
