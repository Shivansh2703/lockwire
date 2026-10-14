"""Independent provisioner for the live-target fixture.

Plants an i2c sysfs tree **inside the container** under ``CONTAINER_FS_ROOT``.
``LiveSource`` reads that tree through ``docker_runner`` (``fs_access="runner"``),
never via a host ``Path``. Constants here are the provisioner's own —
``contract_live.yaml`` must NOT import or derive its declared identity from
them (house-bug guard).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Provisioner's planted device — authored here, not read from the contract.
PLANTED_I2C_BUS = 1
PLANTED_I2C_ADDRESS_HEX = "0068"  # sysfs entry name suffix; scan emits address "0x68"
PLANTED_I2C_NAME = "bmi088"

# Container-absolute root for runner-routed filesystem sources.
CONTAINER_FS_ROOT = "/var/lockwire"

# A second address the provisioner knows how to plant — used when a test wants
# the target to expose the device the absent-case contract is missing.
PRESENT_I2C_ADDRESS_HEX = "0068"


def plant_i2c_device(
    fs_root: Path,
    *,
    bus: int = PLANTED_I2C_BUS,
    address_hex: str = PLANTED_I2C_ADDRESS_HEX,
    name: str = PLANTED_I2C_NAME,
) -> Path:
    """Create ``{fs_root}/sys/bus/i2c/devices/{bus}-{address}/name`` on the host.

    Host-side helper for scan unit tests only. LiveSource must not score this.
    """
    entry = fs_root / "sys" / "bus" / "i2c" / "devices" / f"{bus}-{address_hex}"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "name").write_text(f"{name}\n")
    return entry


def plant_i2c_device_in_container(
    container_id: str,
    *,
    bus: int = PLANTED_I2C_BUS,
    address_hex: str = PLANTED_I2C_ADDRESS_HEX,
    name: str = PLANTED_I2C_NAME,
    fs_root: str = CONTAINER_FS_ROOT,
) -> str:
    """Plant an i2c sysfs entry inside ``container_id``; return the entry path."""
    entry = f"{fs_root.rstrip('/')}/sys/bus/i2c/devices/{bus}-{address_hex}"
    script = f"mkdir -p {entry} && printf '%s\\n' '{name}' > {entry}/name"
    completed = subprocess.run(
        ["docker", "exec", container_id, "bash", "-lc", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    return entry


def clear_i2c_devices_in_container(container_id: str, *, fs_root: str = CONTAINER_FS_ROOT) -> None:
    """Remove planted i2c devices under ``fs_root`` inside the container."""
    devices = f"{fs_root.rstrip('/')}/sys/bus/i2c/devices"
    subprocess.run(
        ["docker", "exec", container_id, "bash", "-lc", f"rm -rf {devices}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


# Publishes a real sensor_msgs/msg/Imu at 5 Hz — the live-capture path reads
# this through `ros2 bag record` + `load_ros2_bag`, so the message needs a real
# numeric field for `contract_live.yaml`'s driver.signals to decode.
IMU_DRIVER_SCRIPT = r"""
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu

class ImuDriver(Node):
    def __init__(self) -> None:
        super().__init__("imu_driver")
        self._pub = self.create_publisher(Imu, "/imu/data", 10)
        self.create_timer(0.2, self._tick)

    def _tick(self) -> None:
        msg = Imu()
        msg.linear_acceleration.z = -9.81
        self._pub.publish(msg)

def main() -> None:
    rclpy.init()
    node = ImuDriver()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == "__main__":
    main()
"""
