"""The sample ROS 2 bag: generated, never committed as a binary.

`lockwire bag inspect --example` and `lockwire bag check --example` write it
into a temp folder, so a fresh `pip install 'lockwire[ros2bag]'` can walk the
whole bag-first path with no robot and no ROS. Three real sensor_msgs topics
(field names exactly as a real driver publishes them) at fixed log times, so
the same library version writes the same bytes. `ros2bag_example.yaml` beside
this file is the contract that reads it, and it passes GO.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

CONTRACT = Path(__file__).with_name("ros2bag_example.yaml")

_HEADER = """\
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
"""
_VECTOR = "float64 x\nfloat64 y\nfloat64 z\n"

#: Message definitions as rosbag2 stores them (constants omitted; they carry no data).
MSGDEFS = {
    "sensor_msgs/msg/JointState": "std_msgs/Header header\nstring[] name\nfloat64[] position\n"
    "float64[] velocity\nfloat64[] effort\n" + _HEADER,
    "sensor_msgs/msg/Imu": "std_msgs/Header header\ngeometry_msgs/Quaternion orientation\n"
    "float64[9] orientation_covariance\ngeometry_msgs/Vector3 angular_velocity\n"
    "float64[9] angular_velocity_covariance\ngeometry_msgs/Vector3 linear_acceleration\n"
    "float64[9] linear_acceleration_covariance\n"
    + _HEADER
    + "================================================================================\n"
    "MSG: geometry_msgs/Quaternion\n" + _VECTOR + "float64 w\n"
    "================================================================================\n"
    "MSG: geometry_msgs/Vector3\n" + _VECTOR,
    "sensor_msgs/msg/BatteryState": "std_msgs/Header header\nfloat32 voltage\n"
    "float32 temperature\nfloat32 current\nfloat32 charge\nfloat32 capacity\n"
    "float32 design_capacity\nfloat32 percentage\nuint8 power_supply_status\n"
    "uint8 power_supply_health\nuint8 power_supply_technology\nbool present\n"
    "float32[] cell_voltage\nfloat32[] cell_temperature\nstring location\n"
    "string serial_number\n" + _HEADER,
}

_START_NS = 1_700_000_000_000_000_000
_JOINTS = ["hip_joint", "knee_joint", "ankle_joint"]


def write_bag(path: Path, streams: Iterable[tuple[str, str, Iterable[tuple[int, dict]]]]) -> Path:
    """Write (topic, msg_type, [(log_time_ns, message dict)]) streams as one real MCAP file."""
    from mcap_ros2.writer import Writer

    from lockwire_pro.ros2bag import _decoder

    _decoder()  # the plain "pip install 'lockwire[ros2bag]'" error if the extra is missing
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        for topic, msg_type, messages in streams:
            schema = writer.register_msgdef(msg_type, MSGDEFS[msg_type])
            for log_ns, message in messages:
                writer.write_message(topic, schema, message, log_time=log_ns, publish_time=log_ns)
        writer.finish()
    return path


def _header(i: int, period_ns: int, frame: str) -> dict[str, Any]:
    t = _START_NS + i * period_ns
    return {"stamp": {"sec": t // 1_000_000_000, "nanosec": t % 1_000_000_000}, "frame_id": frame}


def _ticks(hz: int, seconds: int = 3) -> Iterable[tuple[int, int]]:
    period = 1_000_000_000 // hz
    return ((i, period) for i in range(hz * seconds))


def write_example_bag(folder: Path) -> Path:
    """`folder/run.mcap`: /joint_states 50 Hz, /imu/data 100 Hz, /battery_state 10 Hz, 3 s."""
    zeros = [0.0] * 9
    joints = [
        (
            _START_NS + i * p,
            {
                "header": _header(i, p, "base_link"),
                "name": _JOINTS,
                "position": [0.1 * math.sin(i / 10 + k) for k in range(3)],
                "velocity": [0.0, 0.0, 0.0],
                "effort": [1.5, 4.0, 2.0],
            },
        )
        for i, p in _ticks(50)
    ]
    imu = [
        (
            _START_NS + i * p,
            {
                "header": _header(i, p, "imu_link"),
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                "orientation_covariance": zeros,
                "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.01},
                "angular_velocity_covariance": zeros,
                "linear_acceleration": {"x": 0.0, "y": 0.0, "z": -9.81},
                "linear_acceleration_covariance": zeros,
            },
        )
        for i, p in _ticks(100)
    ]
    battery = [
        (
            _START_NS + i * p,
            {
                "header": _header(i, p, "battery"),
                "voltage": 24.5,
                "temperature": 31.0,
                "current": -3.2,
                "charge": 4.1,
                "capacity": 5.0,
                "design_capacity": 5.0,
                "percentage": 0.82,
                "power_supply_status": 2,
                "power_supply_health": 1,
                "power_supply_technology": 3,
                "present": True,
                "cell_voltage": [4.08] * 6,
                "cell_temperature": [31.0] * 6,
                "location": "base",
                "serial_number": "",
            },
        )
        for i, p in _ticks(10)
    ]
    return write_bag(
        Path(folder) / "run.mcap",
        [
            ("/joint_states", "sensor_msgs/msg/JointState", joints),
            ("/imu/data", "sensor_msgs/msg/Imu", imu),
            ("/battery_state", "sensor_msgs/msg/BatteryState", battery),
        ],
    )
