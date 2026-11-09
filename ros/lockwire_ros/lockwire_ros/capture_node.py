"""Subscribe to the contract's declared topics for a window, write a recording.

Used two ways: standalone (`ros2 run lockwire_ros capture_node`) to produce a
recording you can gate later, and from inside `LockwireGate.on_configure` so the
gate checks what the robot is doing right now.

Python 3.10 compatible.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.qos_event import SubscriptionEventCallbacks, UnsupportedEventTypeError

from lockwire_ros.capture import CaptureError, DeviceCapture, captures_from_contract, deadline_nanoseconds
from lockwire_ros.capture import write_recording


def load_contract_yaml(path: str) -> Dict[str, Any]:
    """Read a lockfile without importing lockwire (which needs Python >= 3.12).

    Uses PyYAML, which every ROS 2 distro pulls in already (ament and the ros2
    CLI both depend on it), so this stays inside the "nothing outside ros_core"
    promise. If it is somehow absent, this raises rather than falling back to a
    hand-rolled parser — a lockfile misread by an approximate YAML reader would
    hand the gate the wrong topics, and a clear error beats a quiet mis-parse.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        raise CaptureError(
            "no YAML reader available: install python3-yaml, or run the capture "
            "node with a contract already parsed"
        )
    with open(path, "r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream)
    if not isinstance(loaded, dict):
        raise CaptureError("{0}: contract is not a mapping".format(path))
    return loaded


class CaptureNode(Node):
    """Collects messages from the contract's topics for a bounded window."""

    def __init__(
        self, node_name: str = "lockwire_capture", context: str = "bench-idle", **node_kwargs
    ) -> None:
        super().__init__(node_name, **node_kwargs)
        self.declare_parameter("contract", "lockwire.yaml")
        self.declare_parameter("out", "")
        self.declare_parameter("context", context)
        self.declare_parameter("duration_s", 30.0)
        # Sensor data is usually BEST_EFFORT; a RELIABLE subscription silently
        # matches nothing against such a publisher, and "no messages" would
        # otherwise look like a dead driver rather than a QoS mismatch.
        self.declare_parameter("best_effort", True)
        self._captures: List[DeviceCapture] = []
        self._window_s: Optional[float] = None

    def _text(self, name: str) -> str:
        return str(self.get_parameter(name).value or "")

    def plan(self, contract: Optional[Dict[str, Any]] = None) -> List[DeviceCapture]:
        if contract is None:
            contract = load_contract_yaml(self._text("contract"))
        self._captures = captures_from_contract(contract)
        if not self._captures:
            raise CaptureError(
                "no device in this contract declares driver.msg_type + driver.signals, "
                "so there is nothing to capture"
            )
        return self._captures

    def subscribe(self) -> None:
        from rosidl_runtime_py.utilities import get_message

        best_effort = bool(self.get_parameter("best_effort").value)
        for capture in self._captures:
            qos = QoSProfile(depth=50)
            if best_effort:
                qos.reliability = ReliabilityPolicy.BEST_EFFORT
            callbacks = None
            if capture.dds_deadline_s is not None:
                qos.deadline = Duration(nanoseconds=deadline_nanoseconds(capture.dds_deadline_s))
                callbacks = SubscriptionEventCallbacks(
                    deadline=self._make_deadline_handler(capture)
                )
            try:
                message_class = get_message(capture.msg_type)
            except (ValueError, ImportError, AttributeError) as error:
                raise CaptureError(
                    "device {0!r}: cannot resolve msg_type {1!r}: {2}".format(
                        capture.device_id, capture.msg_type, error
                    )
                )
            try:
                self.create_subscription(
                    message_class, capture.topic, self._make_handler(capture), qos,
                    event_callbacks=callbacks,
                )
            except UnsupportedEventTypeError as error:
                raise CaptureError(
                    "device {0!r}: DDS requested-deadline event unsupported: {1}".format(
                        capture.device_id, error
                    )
                ) from error
            self.get_logger().info(
                "capturing {0} <- {1} ({2})".format(
                    capture.device_id, capture.topic, capture.msg_type
                )
            )

    def _make_handler(self, capture: DeviceCapture):
        def handle(message) -> None:
            # Receive time from one monotonic source, per recording.py's
            # recv-time contract. Never a stamp from inside the payload.
            capture.take(message, time.monotonic())

        return handle

    def _make_deadline_handler(self, capture: DeviceCapture):
        def handle(event) -> None:
            count = int(event.total_count_change)
            if count > 0:
                capture.deadline_misses.append((time.monotonic(), count))

        return handle

    def collect(self, duration_s: Optional[float] = None) -> None:
        if duration_s is None:
            duration_s = float(self.get_parameter("duration_s").value)
        start = time.monotonic()
        deadline = start + duration_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
        # How long we actually listened; the core's liveness divides by it, not by
        # the receive span, which a delivery burst can collapse.
        self._window_s = time.monotonic() - start

    def write(self, out_dir: Optional[str] = None) -> str:
        out_dir = out_dir or self._text("out")
        if not out_dir:
            raise CaptureError("no output directory given")
        for capture in self._captures:
            if not capture.rows:
                # Name the silent topic: "nothing scored" for a device says less.
                self.get_logger().warning("no messages on {0} (device {1}) in the capture "
                                          "window".format(capture.topic, capture.device_id))
        path = write_recording(out_dir, self._captures, self._text("context") or "bench-idle",
                               capture_window_s=self._window_s)
        total = sum(len(c.rows) for c in self._captures)
        self.get_logger().info("wrote {0} ({1} message(s))".format(path, total))
        return path


def capture_to(
    contract_path: str,
    out_dir: str,
    context: str = "bench-idle",
    duration_s: float = 30.0,
    node_name: str = "lockwire_capture",
    best_effort: bool = True,
) -> str:
    """One capture, start to finish. Assumes rclpy is already initialised."""
    node = CaptureNode(node_name)
    node.set_parameters(
        [
            rclpy.parameter.Parameter("contract", value=contract_path),
            rclpy.parameter.Parameter("out", value=out_dir),
            rclpy.parameter.Parameter("context", value=context),
            rclpy.parameter.Parameter("duration_s", value=float(duration_s)),
            rclpy.parameter.Parameter("best_effort", value=best_effort),
        ]
    )
    try:
        node.plan()
        node.subscribe()
        node.collect()
        return node.write()
    finally:
        node.destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = CaptureNode()
    try:
        node.plan()
        node.subscribe()
        node.collect()
        node.write()
    except CaptureError as error:
        node.get_logger().error(str(error))
        raise SystemExit(1)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
