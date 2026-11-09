"""The lifecycle gate: a node that refuses to configure when the robot disagrees with its records.

``on_configure`` runs `lockwire preflight` and returns
``TransitionCallbackReturn.FAILURE`` on anything short of a GO. Per the ROS 2
managed-node contract that leaves this node **Unconfigured** — it never reaches
Inactive, never activates, and anything a launch file registered behind its
activation never starts.

Why the gate lives in ``on_configure`` and not ``on_activate``: configure is the
transition a bringup sequence runs first and is allowed to fail cheaply. A
failed activate is recoverable-looking and invites a retry loop against a robot
that is physically wrong.

**Refusal is not the same as NO-GO, and both hold the door.** If the tool is
missing, times out, crashes, or writes a document this node cannot read, the
outcome is ``REFUSED`` and configuring still fails. The one thing that must
never happen is a robot cleared because the gate could not run.

Python 3.10 compatible: plain ``LifecycleNode.__init__(node_name)``, no
``LifecycleNodeArgs``/``Unpack`` (those are newer-distro typing sugar and would
break the Humble floor this package exists to hold).
"""

from __future__ import annotations

import rclpy
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from rclpy.lifecycle import State
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.parameter import Parameter

from lockwire_ros import core, diagnostics

DEFAULT_CONTRACT = "lockwire.yaml"


class LockwireGate(LifecycleNode):
    """Runs the preflight gate as a lifecycle transition."""

    def __init__(self, node_name: str = "lockwire_gate", **node_kwargs) -> None:
        super().__init__(node_name, **node_kwargs)
        # Read-only: what the gate runs and may skip must not be settable over the wire.
        fixed = ParameterDescriptor(read_only=True)
        self.declare_parameter("contract", DEFAULT_CONTRACT, fixed)
        self.declare_parameter("recording", "", fixed)
        self.declare_parameter("lockwire_executable", "lockwire", fixed)
        self.declare_parameter("timeout_s", 120.0)
        self.declare_parameter("profile", "analysis")
        self.declare_parameter("required_evidence", Parameter.Type.STRING_ARRAY)
        self.declare_parameter("publish_diagnostics", True)
        self.declare_parameter("diagnostics_topic", "/diagnostics")
        # A result that scored nothing exits 3 upstream, but on a robot it almost
        # always means a misconfigured contract. Default to refusing it here and
        # let a team opt out deliberately rather than discover it after an
        # incident (presence floor: a GO that scored nothing proves nothing).
        self.declare_parameter("refuse_when_nothing_scored", True, fixed)
        # Live capture. With `capture_s` > 0 and no `recording`, the gate
        # subscribes to the contract's declared topics for that window and gates
        # what the robot is doing NOW. An explicit `recording` always wins, so
        # the offline path is unchanged.
        self.declare_parameter("capture_s", 0.0)
        self.declare_parameter("capture_context", "bench-idle")
        self.declare_parameter("capture_dir", "")
        self._result = None
        self._captured_to = None

    # -- accessors -------------------------------------------------------

    def _text(self, name: str) -> str:
        return str(self.get_parameter(name).value or "")

    @property
    def result(self):
        """The last GateResult, for tests and for a later activate."""
        return self._result

    # -- the gate --------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        contract = self._text("contract")
        recording = self._text("recording")
        capture_s = float(self.get_parameter("capture_s").value)

        if not recording and capture_s > 0:
            try:
                recording = self._capture(contract, capture_s)
            except Exception as error:
                # Capture is part of the gate. If it cannot produce evidence —
                # no messages, an unresolvable msg_type, a bad field path — that
                # is a refusal, never a pass on missing data.
                self.get_logger().error("lockwire capture failed: {0}".format(error))
                return TransitionCallbackReturn.FAILURE

        if not recording:
            self.get_logger().error(
                "lockwire gate has no 'recording' and no 'capture_s' — nothing to check "
                "against, which is a refusal, not a pass"
            )
            return TransitionCallbackReturn.FAILURE

        self.get_logger().info(
            "lockwire preflight: contract={0} recording={1}".format(contract, recording)
        )
        result = core.run_gate(
            contract=contract,
            recording=recording,
            executable=self._text("lockwire_executable") or "lockwire",
            timeout_s=float(self.get_parameter("timeout_s").value),
            profile=self._text("profile"),
            required_evidence=list(self.get_parameter_or("required_evidence").value or []),
        )
        self._result = result

        if self.get_parameter("publish_diagnostics").value:
            self._publish(result)

        return self._decide(result)

    def _capture(self, contract: str, capture_s: float) -> str:
        """Subscribe to the contract's topics for a window and write a recording.

        Imported here rather than at module load so the gate still works on a
        box without the capture path's YAML reader — the offline path must not
        acquire a dependency it never uses.
        """
        import tempfile

        from lockwire_ros.capture_node import capture_to

        out_dir = self._text("capture_dir") or tempfile.mkdtemp(prefix="lockwire-capture-")
        self.get_logger().info(
            "capturing {0:g}s from the topics declared in {1}".format(capture_s, contract)
        )
        path = capture_to(
            contract_path=contract,
            out_dir=out_dir,
            context=self._text("capture_context") or "bench-idle",
            duration_s=capture_s,
            node_name="{0}_capture".format(self.get_name()),
        )
        self._captured_to = path
        return path

    @property
    def captured_to(self):
        """Where the live capture landed, or None if a recording was supplied."""
        return self._captured_to

    def _decide(self, result) -> TransitionCallbackReturn:
        """One place decides; every branch that is not an affirmative GO fails."""
        if result.outcome == core.REFUSED:
            self.get_logger().error("lockwire gate REFUSED: {0}".format(result.reason))
            return TransitionCallbackReturn.FAILURE

        if result.outcome == core.NO_GO:
            self.get_logger().error("lockwire NO-GO: {0}".format(result.reason))
            for finding in result.violations():
                self.get_logger().error(
                    "  {0}: observed {1}, expected {2} | action: {3}".format(
                        finding.get("quantity"),
                        finding.get("observed"),
                        finding.get("expected"),
                        finding.get("action"),
                    )
                )
            return TransitionCallbackReturn.FAILURE

        if result.scored_nothing and self.get_parameter("refuse_when_nothing_scored").value:
            self.get_logger().error(
                "lockwire scored NOTHING and refuse_when_nothing_scored is set: {0}".format(
                    result.reason
                )
            )
            return TransitionCallbackReturn.FAILURE

        not_scored = result.counts.get("not_scored", 0)
        if not_scored:
            # Always say what the GO did not cover. Silence here would be the
            # overclaim docs/gate_coverage.md exists to prevent.
            self.get_logger().warning(
                "lockwire GO does NOT cover {0} check(s); see the coverage diagnostic".format(
                    not_scored
                )
            )
        self.get_logger().info("lockwire {0}".format(result.reason))
        return TransitionCallbackReturn.SUCCESS

    def _publish(self, result) -> None:
        try:
            from diagnostic_msgs.msg import DiagnosticArray
        except ImportError:  # pragma: no cover - diagnostic_msgs is in ros_core
            self.get_logger().warning("diagnostic_msgs unavailable; not publishing /diagnostics")
            return
        topic = self._text("diagnostics_topic") or "/diagnostics"
        publisher = self.create_publisher(DiagnosticArray, topic, 10)
        rows = diagnostics.diagnostic_rows(result)
        publisher.publish(
            diagnostics.to_diagnostic_array(
                rows, self.get_clock().now().to_msg(), hardware_id=self._text("contract")
            )
        )

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        self._result = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        return TransitionCallbackReturn.SUCCESS


def main(args=None) -> None:
    rclpy.init(args=args)
    node = LockwireGate()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
