"""Raw-byte ROS 2 relay with queued, out-of-process command observation."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile


class CommandRelay(Node):
    def __init__(self, *, parameter_overrides=None) -> None:
        super().__init__("lockwire_command_relay", parameter_overrides=parameter_overrides)
        self.declare_parameter("input_topic", "")
        self.declare_parameter("output_topic", "")
        self.declare_parameter("msg_type", "")
        self.declare_parameter("contract", "")
        self.declare_parameter("record", "")
        self.declare_parameter("device", "")
        self.declare_parameter("core_python", os.environ.get("LOCKWIRE_CORE_PYTHON", ""))

        self.input_topic = str(self.get_parameter("input_topic").value or "")
        self.output_topic = str(self.get_parameter("output_topic").value or "")
        msg_type = str(self.get_parameter("msg_type").value or "")
        contract = str(self.get_parameter("contract").value or "")
        record = str(self.get_parameter("record").value or "")
        device = str(self.get_parameter("device").value or "")
        core_python = str(self.get_parameter("core_python").value or "")
        if not self.input_topic or not self.output_topic or not msg_type:
            raise ValueError("input_topic, output_topic, and msg_type are required")
        if self.input_topic == self.output_topic:
            raise ValueError("input_topic and output_topic must differ to prevent relay loops")
        if not contract or not record or not device or not core_python:
            raise ValueError("contract, record, device, and core_python are required")

        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message

        try:
            message_type = get_message(msg_type)
        except (ValueError, ImportError, AttributeError) as error:
            raise ValueError(f"cannot resolve configured msg_type {msg_type!r}: {error}")

        writer = Path(__file__).with_name("relay_writer.py")
        self._writer: subprocess.Popen | None = subprocess.Popen(
            [core_python, str(writer), contract, record, self.input_topic, device],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._observer_queue: queue.Queue[dict | None] = queue.Queue(maxsize=256)
        self._continuity_gap_pending = False
        self._observer_thread = threading.Thread(
            target=self._observe_queue, name="lockwire-command-observer", daemon=True
        )
        self._observer_thread.start()
        self._message_type = message_type
        self._deserialize_message = deserialize_message

        qos = QoSProfile(depth=50)
        self._publisher = self.create_publisher(message_type, self.output_topic, qos)
        self._subscription = self.create_subscription(
            message_type, self.input_topic, self._receive, qos, raw=True
        )
        self.get_logger().info(
            f"observing and forwarding raw {msg_type} payloads: {self.input_topic} -> {self.output_topic}"
        )

    def _receive(self, raw: bytes) -> None:
        if not isinstance(raw, bytes):
            self.get_logger().error("rclpy raw subscription returned a non-bytes message")
            return

        received_at = time.time()
        publish_error = None
        try:
            self._publish_raw(raw)
            forwarded_at = time.time()
        except Exception as error:  # noqa: BLE001
            publish_error = str(error)
            forwarded_at = None
            self.get_logger().error(f"raw command publish failed: {error}")

        source_time = None
        try:
            message = self._deserialize_message(raw, self._message_type)
            stamp = getattr(getattr(message, "header", None), "stamp", None)
            if stamp is not None:
                source_time = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000
        except Exception as error:  # noqa: BLE001
            self.get_logger().warning(f"source timestamp unavailable: {error}")

        item = {
            "raw_hex": raw.hex(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "source_time": source_time,
            "receipt_time": received_at,
            "forwarded_time": forwarded_at,
            "publish_error": publish_error,
        }
        if self._continuity_gap_pending:
            item["continuity_break"] = True
        try:
            self._observer_queue.put_nowait(item)
            self._continuity_gap_pending = False
        except queue.Full:
            self._continuity_gap_pending = True
            self.get_logger().error(
                "observer queue overflow; command forwarding continued, evidence gap recorded for next observation"
            )

    def _publish_raw(self, raw: bytes) -> None:
        self._publisher.publish(raw)

    def _observe_queue(self) -> None:
        writer = self._writer
        if writer is None or writer.stdin is None or writer.stdout is None:
            self.get_logger().error("command observer pipes are unavailable")
            return
        try:
            ready = json.loads(writer.stdout.readline())
        except (json.JSONDecodeError, OSError) as error:
            self.get_logger().error(f"command observer failed to start: {error}")
            return
        if not ready.get("ready"):
            self.get_logger().error(
                "command observer failed to start: {}".format(ready.get("error", "unknown error"))
            )
            return

        while True:
            item = self._observer_queue.get()
            if item is None:
                return
            if item.get("continuity_break"):
                self._send_observer_request(writer, {"operation": "continuity_break"})
            result = self._send_observer_request(
                writer,
                {
                    "operation": "observe",
                    "raw_hex": item["raw_hex"],
                    "source_time": item["source_time"],
                    "receipt_time": item["receipt_time"],
                },
            )
            if not result or result.get("error"):
                continue
            if result.get("status") != "OK":
                self.get_logger().warning(
                    "command observation {}: {}".format(
                        result.get("status"), result.get("issue") or "no score"
                    )
                )
            command_id = result.get("command_id")
            if not command_id:
                self.get_logger().error("command observer omitted command identity")
                continue
            if item["publish_error"] is None:
                operation = "forwarded"
                extra = {"event_time": item["forwarded_time"]}
            else:
                operation = "delivery_failed"
                extra = {
                    "event_time": time.time(),
                    "issue": item["publish_error"],
                }
            self._send_observer_request(
                writer,
                {
                    "operation": operation,
                    "command_id": command_id,
                    "raw_sha256": item["raw_sha256"],
                    **extra,
                },
            )

    def _send_observer_request(self, writer: subprocess.Popen, request: dict) -> dict | None:
        try:
            if writer.poll() is not None or writer.stdin is None or writer.stdout is None:
                raise BrokenPipeError("Lockwire observer process is unavailable")
            writer.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            writer.stdin.flush()
            response = json.loads(writer.stdout.readline())
            if response.get("error"):
                self.get_logger().error(f"command observation failed: {response['error']}")
                return None
            return response
        except Exception as error:  # noqa: BLE001
            self.get_logger().error(f"command observation failed: {error}")
            return None

    def _stop_writer(self) -> None:
        try:
            self._observer_queue.put_nowait(None)
        except queue.Full:
            try:
                self._observer_queue.get_nowait()
            except queue.Empty:
                pass
            self.get_logger().error(
                "observer shutdown dropped queued evidence; forwarding had already completed"
            )
            try:
                self._observer_queue.put_nowait(None)
            except queue.Full:
                pass
        self._observer_thread.join(timeout=2)
        writer, self._writer = self._writer, None
        if writer is None:
            return
        if writer.stdin:
            writer.stdin.close()
        try:
            writer.wait(timeout=2)
        except subprocess.TimeoutExpired:
            writer.terminate()
            writer.wait(timeout=2)
        self._observer_thread.join(timeout=1)

    def destroy_node(self) -> bool:
        self._stop_writer()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = CommandRelay()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
