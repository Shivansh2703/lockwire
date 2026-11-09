"""`ros2 run lockwire_ros monitor`: a verdict on the last window, published every period.

    ros2 run lockwire_ros monitor --ros-args -p contract:=lockwire.yaml \\
        [-p window_s:=10.0 -p period_s:=10.0 -p context:=nominal-op \\
         -p status_topic:=/lockwire/status -p lockwire_executable:=lockwire]

Subscribes once to every topic the contract's drivers declare (the capture
node's machinery; its rows are the rolling buffer). Every ``period_s`` it gates
the last ``window_s`` seconds and publishes:

- ``/lockwire/status``: one ``diagnostic_msgs/DiagnosticStatus``, latched
  (RELIABLE + TRANSIENT_LOCAL, depth 1) so a node that subscribes late still
  gets the last verdict;
- ``/diagnostics``: the same verdict as a ``DiagnosticArray`` (the lifecycle
  gate's mapping).

The gate runs on a worker thread, not in the timer callback: ``run_gate`` blocks
on a subprocess, and blocking the executor would stop the subscriptions and
punch a gap into the next window, which the core would then score as a dropped
rate. One gate runs at a time; a tick that finds one still running is skipped
and logged, and ``timeout_s`` defaults to ``period_s`` so a hung tool is
reported as ERROR within a period rather than going quiet.

The start-time gate is still ``gate_node`` (it refuses to configure). This node
gates nothing itself; a dependent node subscribes to ``/lockwire/status``.

Python 3.10 compatible.
"""

from __future__ import annotations

import tempfile
import threading
import time
from typing import List, Optional

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from lockwire_ros import diagnostics
from lockwire_ros.capture import CaptureError
from lockwire_ros.capture_node import CaptureNode
from lockwire_ros.monitor import Monitor, default_gate, level_name, status_row

#: The latched-status QoS. Subscribers must ask for the same durability to get the last verdict.
STATUS_QOS = QoSProfile(
    depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
)


class MonitorNode(CaptureNode):
    """The capture node's subscriptions, gated in windows, forever."""

    def __init__(self, node_name: str = "lockwire_monitor", **node_kwargs) -> None:
        # A monitor watches a robot that is running, so nominal-op, not bench-idle.
        super().__init__(node_name, context="nominal-op", **node_kwargs)
        self.declare_parameter("window_s", 10.0)
        self.declare_parameter("period_s", 10.0)
        self.declare_parameter("timeout_s", 0.0)  # 0 = period_s
        self.declare_parameter("status_topic", "/lockwire/status")
        self.declare_parameter("diagnostics_topic", "/diagnostics")
        self.declare_parameter("lockwire_executable", "lockwire")
        self.declare_parameter("keep_last", 0)  # recordings to keep on disk; 0 = none

        from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus

        self._status_pub = self.create_publisher(
            DiagnosticStatus, self._text("status_topic") or "/lockwire/status", STATUS_QOS
        )
        self._diag_pub = self.create_publisher(
            DiagnosticArray, self._text("diagnostics_topic") or "/diagnostics", 10
        )
        self.monitor: Optional[Monitor] = None
        self._worker: Optional[threading.Thread] = None

    def start(self) -> None:
        """Plan, subscribe, start the period timer. Raises CaptureError on a contract with nothing to watch."""
        self.plan()
        period_s = float(self.get_parameter("period_s").value)
        timeout_s = float(self.get_parameter("timeout_s").value) or period_s
        self.monitor = Monitor(
            captures=self._captures,
            window_s=float(self.get_parameter("window_s").value),
            context=self._text("context") or "nominal-op",
            gate=default_gate(
                self._text("contract"), self._text("lockwire_executable") or "lockwire", timeout_s
            ),
            report=self._publish,
            out_dir=tempfile.mkdtemp(prefix="lockwire-monitor-"),
            keep_last=int(self.get_parameter("keep_last").value),
        )
        self.subscribe()  # after the monitor: the handlers feed it
        self.create_timer(period_s, self._tick)

    def _make_handler(self, capture):
        def handle(message) -> None:
            self.monitor.take(capture, message, time.monotonic())

        return handle

    def _tick(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self.get_logger().warning("lockwire monitor: previous window still gating; skipping this one")
            return
        window = self.monitor.snapshot(time.monotonic())
        faults = self.monitor.take_faults()
        self._worker = threading.Thread(target=self._gate_window, args=(window, faults), daemon=True)
        self._worker.start()

    def _gate_window(self, window, faults) -> None:
        try:
            self.monitor.run_window(window, faults)
        except Exception as error:  # a worker that dies quietly reads like a healthy robot
            self.get_logger().error("lockwire monitor: could not publish this window: {0!r}".format(error))

    def _publish(self, rows: List[diagnostics.DiagnosticRow]) -> None:
        from diagnostic_msgs.msg import DiagnosticStatus, KeyValue

        stamp = self.get_clock().now().to_msg()
        contract = self._text("contract")
        row = status_row(rows)
        self._status_pub.publish(
            DiagnosticStatus(
                level=bytes([row.level]), name=row.name, message=row.message, hardware_id=contract,
                values=[KeyValue(key=key, value=value) for key, value in row.values],
            )
        )
        self._diag_pub.publish(diagnostics.to_diagnostic_array(rows, stamp, hardware_id=contract))
        text = "lockwire monitor {0}: {1}".format(level_name(row.level), row.message)
        # Two call sites on purpose: rclpy pins one severity per call site and raises otherwise.
        if row.level == diagnostics.OK:
            self.get_logger().info(text)
        else:
            self.get_logger().warning(text)

    def close(self) -> None:
        if self._worker is not None:
            self._worker.join(timeout=5.0)
        if self.monitor is not None:
            self.monitor.close()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = MonitorNode()
    try:
        node.start()
        rclpy.spin(node)
    except CaptureError as error:
        node.get_logger().error(str(error))
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # rclpy's own SIGINT handling raises ExternalShutdownException (rclpy.executors);
        # anything else is a real failure and keeps its traceback.
        if type(error).__name__ != "ExternalShutdownException":
            raise
    finally:
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
