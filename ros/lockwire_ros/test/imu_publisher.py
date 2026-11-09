"""A real `sensor_msgs/msg/Imu` publisher, healthy or faulted, for the live capture test.

Run as a subprocess by `test_live_capture.py`. Publishes genuine ROS messages
over a real DDS transport — the capture path is exercised end to end rather
than by handing the node a constructed message.

    python3 imu_publisher.py --rate 100     # healthy
    python3 imu_publisher.py --rate 5       # rate collapse: below the 50%-of-nominal floor

Deliberately publishes BEST_EFFORT, which is what real sensor drivers do and
what the capture node must match to see anything at all.
"""

from __future__ import annotations

import argparse
import math

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Imu


class ImuPublisher(Node):
    def __init__(
        self, rate_hz: float, topic: str, node_name: str,
        dds_deadline_s: float | None = None,
    ) -> None:
        super().__init__(node_name)
        qos = QoSProfile(depth=10)
        qos.reliability = ReliabilityPolicy.BEST_EFFORT
        if dds_deadline_s is not None:
            qos.deadline = Duration(nanoseconds=round(dds_deadline_s * 1e9))
        self._pub = self.create_publisher(Imu, topic, qos)
        self._tick = 0
        self.create_timer(1.0 / rate_hz, self._publish)

    def _publish(self) -> None:
        message = Imu()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = "imu_link"
        # A bench-idle IMU: gravity on z, small bounded noise. Deterministic
        # rather than random so a failure is reproducible.
        self._tick += 1
        wobble = 0.01 * math.sin(self._tick / 7.0)
        message.linear_acceleration.x = wobble
        message.linear_acceleration.y = -wobble
        message.linear_acceleration.z = -9.81 + wobble
        message.angular_velocity.x = 0.001 * wobble
        self._pub.publish(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=100.0)
    parser.add_argument("--topic", default="/imu/data")
    parser.add_argument("--name", default="imu_driver")
    parser.add_argument("--dds-deadline-s", type=float)
    args = parser.parse_args()

    rclpy.init()
    node = ImuPublisher(
        args.rate, args.topic, args.name, args.dds_deadline_s,
    )
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
