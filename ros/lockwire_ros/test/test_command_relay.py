"""Humble fixture: a typed TwistStamped payload crosses the raw relay unchanged."""

from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from pathlib import Path

import pytest

pytest.importorskip("rclpy", reason="run inside the Humble container")

import rclpy
from geometry_msgs.msg import TwistStamped
from lockwire_ros.relay_node import CommandRelay
from rclpy.qos import QoSProfile


@pytest.fixture(scope="module", autouse=True)
def _ros():
    rclpy.init()
    yield
    if rclpy.ok():
        rclpy.shutdown()


def test_typed_command_is_forwarded_byte_for_byte_and_metadata_stays_unknown(tmp_path):
    core_python = os.environ.get("LOCKWIRE_CORE_PYTHON", "")
    assert core_python and Path(core_python).exists(), (
        "Humble fixture must provide Python 3.12 core"
    )
    repo = Path(__file__).resolve().parents[3]
    contract = repo / "tests" / "fixtures" / "contract_clean.yaml"
    record = tmp_path / "commands.jsonl"
    source_topic = "/lockwire_test/cmd_vel_in"
    destination_topic = "/lockwire_test/cmd_vel_out"
    relay = CommandRelay(
        parameter_overrides=[
            rclpy.parameter.Parameter("input_topic", value=source_topic),
            rclpy.parameter.Parameter("output_topic", value=destination_topic),
            rclpy.parameter.Parameter("msg_type", value="geometry_msgs/msg/TwistStamped"),
            rclpy.parameter.Parameter("contract", value=str(contract)),
            rclpy.parameter.Parameter("record", value=str(record)),
            rclpy.parameter.Parameter("device", value="dev.test_controller"),
            rclpy.parameter.Parameter("core_python", value=core_python),
        ]
    )
    source = rclpy.create_node("lockwire_relay_source")
    destination = rclpy.create_node("lockwire_relay_destination")
    qos = QoSProfile(depth=10)
    # Keep this literal so the text-level publisher guard can verify the
    # synthetic fixture's topic instead of merely pinning an identifier.
    publisher = source.create_publisher(TwistStamped, "/lockwire_test/cmd_vel_in", qos)
    # A typed publish serializes independently; CDR padding in another
    # serialize_message() call is not the relay's ingress.  Tap that ingress
    # passively so the equality below stays a byte-for-byte transport check.
    submitted: list[bytes] = []
    source.create_subscription(TwistStamped, source_topic, submitted.append, qos, raw=True)
    received: list[bytes] = []
    destination.create_subscription(TwistStamped, destination_topic, received.append, qos, raw=True)
    relay_closed = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (
            publisher.get_subscription_count() < 2 or relay._publisher.get_subscription_count() < 1
        ):
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
            rclpy.spin_once(destination, timeout_sec=0.01)
        assert publisher.get_subscription_count() >= 2
        assert relay._publisher.get_subscription_count() >= 1

        for index in range(3):
            message = TwistStamped()
            message.header.frame_id = "fixture"
            message.header.stamp.sec = 500 + index
            message.header.stamp.nanosec = 125_000_000
            message.twist.linear.x = 0.25 + index
            message.twist.angular.z = -0.5 - index
            publisher.publish(message)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (len(submitted) < 3 or len(received) < 3):
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
            rclpy.spin_once(destination, timeout_sec=0.01)
        assert len(submitted) == 3
        assert len(received) == 3
        assert received == submitted, "relay changed or reordered raw command payloads"

        # The writer consumes the observation queue asynchronously. Closing the
        # relay FIFO-drains it before the sidecar assertions below.
        relay.destroy_node()
        relay_closed = True
        rows = [json.loads(line) for line in record.read_text().splitlines()]
        assert len(rows) == 6
        assert [row["event"] for row in rows] == ["received", "forwarded"] * 3
        received_rows = rows[::2]
        forwarded_rows = rows[1::2]
        assert [row["raw_hex"] for row in received_rows] == [payload.hex() for payload in submitted]
        assert all(row["raw_hex"] is None for row in forwarded_rows)
        assert [row["raw_sha256"] for row in forwarded_rows] == [
            sha256(payload).hexdigest() for payload in submitted
        ]
        assert all(row["overall_verdict"] is None for row in forwarded_rows)
        assert [row["receipt_order"] for row in received_rows] == [1, 2, 3]
        assert all(row["source_sequence"] is None for row in received_rows)
        assert [row["source_time"] for row in received_rows] == [500.125, 501.125, 502.125]
        assert all(row["overall_verdict"] == "NOT_SCORED" for row in received_rows)
        assert all("unknown" in row["transport_issue"] for row in received_rows)
        assert [row["command_id"] for row in received_rows] == [
            row["command_id"] for row in forwarded_rows
        ]
    finally:
        source.destroy_node()
        destination.destroy_node()
        if not relay_closed:
            relay.destroy_node()


def test_hung_observer_does_not_withhold_original_raw_command(tmp_path):
    core_python = os.environ.get("LOCKWIRE_CORE_PYTHON", "")
    assert core_python and Path(core_python).exists()
    repo = Path(__file__).resolve().parents[3]
    contract = repo / "tests" / "fixtures" / "contract_clean.yaml"
    record = tmp_path / "hung-observer.jsonl"
    hung_writer = tmp_path / "hung-python"
    hung_writer.write_text(f"#!{core_python}\nimport time\ntime.sleep(60)\n")
    hung_writer.chmod(0o755)
    source_topic = "/lockwire_test/hung_in"
    destination_topic = "/lockwire_test/hung_out"
    relay = CommandRelay(
        parameter_overrides=[
            rclpy.parameter.Parameter("input_topic", value=source_topic),
            rclpy.parameter.Parameter("output_topic", value=destination_topic),
            rclpy.parameter.Parameter("msg_type", value="geometry_msgs/msg/TwistStamped"),
            rclpy.parameter.Parameter("contract", value=str(contract)),
            rclpy.parameter.Parameter("record", value=str(record)),
            rclpy.parameter.Parameter("device", value="dev.test_controller"),
            rclpy.parameter.Parameter("core_python", value=str(hung_writer)),
        ]
    )
    source = rclpy.create_node("lockwire_hung_source")
    destination = rclpy.create_node("lockwire_hung_destination")
    qos = QoSProfile(depth=10)
    publisher = source.create_publisher(TwistStamped, "/lockwire_test/hung_in", qos)
    submitted: list[bytes] = []
    source.create_subscription(TwistStamped, source_topic, submitted.append, qos, raw=True)
    received: list[bytes] = []
    destination.create_subscription(TwistStamped, destination_topic, received.append, qos, raw=True)
    try:
        message = TwistStamped()
        message.header.stamp.sec = 900
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (
            publisher.get_subscription_count() < 2 or relay._publisher.get_subscription_count() < 1
        ):
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
            rclpy.spin_once(destination, timeout_sec=0.01)
        assert publisher.get_subscription_count() >= 2
        assert relay._publisher.get_subscription_count() >= 1
        publisher.publish(message)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not submitted or not received):
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
            rclpy.spin_once(destination, timeout_sec=0.01)
        assert len(submitted) == 1
        assert len(received) == 1
        assert received == submitted
    finally:
        source.destroy_node()
        destination.destroy_node()
        relay.destroy_node()


def test_publish_failure_is_recorded_without_claiming_forwarded(tmp_path, monkeypatch):
    core_python = os.environ.get("LOCKWIRE_CORE_PYTHON", "")
    assert core_python and Path(core_python).exists()
    repo = Path(__file__).resolve().parents[3]
    contract = repo / "tests" / "fixtures" / "contract_clean.yaml"
    record = tmp_path / "publish-failure.jsonl"
    source_topic = "/lockwire_test/failure_in"
    destination_topic = "/lockwire_test/failure_out"
    relay = CommandRelay(
        parameter_overrides=[
            rclpy.parameter.Parameter("input_topic", value=source_topic),
            rclpy.parameter.Parameter("output_topic", value=destination_topic),
            rclpy.parameter.Parameter("msg_type", value="geometry_msgs/msg/TwistStamped"),
            rclpy.parameter.Parameter("contract", value=str(contract)),
            rclpy.parameter.Parameter("record", value=str(record)),
            rclpy.parameter.Parameter("device", value="dev.test_controller"),
            rclpy.parameter.Parameter("core_python", value=core_python),
        ]
    )
    source = rclpy.create_node("lockwire_failure_source")
    destination = rclpy.create_node("lockwire_failure_destination")
    qos = QoSProfile(depth=10)
    publisher = source.create_publisher(TwistStamped, "/lockwire_test/failure_in", qos)
    received: list[bytes] = []
    destination.create_subscription(TwistStamped, destination_topic, received.append, qos, raw=True)
    publish_attempts: list[bytes] = []

    def fail_publish(raw: bytes) -> None:
        publish_attempts.append(raw)
        raise OSError("local publish failure")

    monkeypatch.setattr(
        relay, "_publish_raw", fail_publish
    )
    relay_closed = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and publisher.get_subscription_count() < 1:
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
        message = TwistStamped()
        publisher.publish(message)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not publish_attempts:
            rclpy.spin_once(source, timeout_sec=0.01)
            rclpy.spin_once(relay, timeout_sec=0.01)
            rclpy.spin_once(destination, timeout_sec=0.01)
        assert publish_attempts

        relay.destroy_node()
        relay_closed = True
        rows = [json.loads(line) for line in record.read_text().splitlines()]
        assert [row["event"] for row in rows] == ["received", "delivery_failed"]
        assert rows[1]["command_id"] == rows[0]["command_id"]
        assert rows[1]["transport_issue"] == "local publish failure"
        assert rows[1]["overall_verdict"] == "REFUSED"
        assert not received
    finally:
        source.destroy_node()
        destination.destroy_node()
        if not relay_closed:
            relay.destroy_node()
