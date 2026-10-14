"""Baked into the E5.10 sim-jazzy image at /opt/lockwire.

Flies the X500 through real PX4 SITL over MAVLink (arm / NAV_TAKEOFF / hover /
NAV_LAND), the same sequence the retired run_e5_10.py's fly_x500_with_px4 used
-- reused here, trimmed of the trace/commands.json bookkeeping that script
wrote for itself. This script only flies the mission, trims PX4's own .ulg to
the topics the judge needs, and copies it to /output flat; it does not decide
GO/NO-GO. That verdict is Lockwire's own (jobs._x500_evidence_reason, reading
the .ulg through ulog.read_ulog_rows).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

_MAGIC = b"ULog\x01\x12\x35"
_HEADER_SIZE = 16

# The judge's decision topics (jobs._X500_DECISION_TOPICS) -- trimmed here so
# the evidence .ulg stays well under Lockwire's 4 MiB single-file cap
# (jobs._MOTION_MAX_BYTES); PX4's full default topic set is far larger.
_KEEP_TOPICS = {"vehicle_local_position", "vehicle_land_detected", "actuator_motors", "vehicle_status"}

POSE_PUBLISHER_XML = """
<gazebo>
  <plugin filename="gz-sim-pose-publisher-system" name="gz::sim::systems::PosePublisher">
    <publish_link_pose>false</publish_link_pose>
    <publish_model_pose>true</publish_model_pose>
    <use_pose_vector_msg>false</use_pose_vector_msg>
    <static_publisher>true</static_publisher>
    <static_update_frequency>10</static_update_frequency>
  </plugin>
</gazebo>
"""


def prepare_x500(fixtures: Path, scratch: Path) -> None:
    dst = scratch / "models"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(fixtures / "official/px4-gazebo-models/models", dst)
    model_sdf = dst / "x500/model.sdf"
    text = model_sdf.read_text().replace("</model>", POSE_PUBLISHER_XML + "</model>", 1)
    model_sdf.write_text(text)


def trim_ulog(raw: bytes, keep_topics: set[str] = _KEEP_TOPICS) -> bytes:
    """Keep only Format/Subscription/Data records for ``keep_topics``.

    ponytail: a standalone byte-level filter (duplicates the parsing shape of
    the main package's ULog record loop) rather than installing the lockwire
    package into this ROS/PX4 image just to reuse it; keep the two in sync if
    the ULog record format ever changes.
    """
    if len(raw) < _HEADER_SIZE or raw[:7] != _MAGIC:
        raise ValueError("not a ULog file")
    out = bytearray(raw[:_HEADER_SIZE])
    dropped_ids: set[int] = set()
    position = _HEADER_SIZE
    while position < len(raw):
        size, message_type = struct.unpack_from("<HB", raw, position)
        header_end = position + 3
        payload = raw[header_end : header_end + size]
        record = raw[position : header_end + size]
        position = header_end + size
        char = chr(message_type)
        if char == "F":
            name = payload.split(b":", 1)[0].decode("utf-8", "replace")
            if name in keep_topics:
                out += record
        elif char == "A":
            message_id = struct.unpack_from("<H", payload, 1)[0]
            name = payload[3:].decode("utf-8", "replace").rstrip("\0")
            if name in keep_topics:
                out += record
            else:
                dropped_ids.add(message_id)
        elif char in ("R", "D"):
            message_id = struct.unpack_from("<H", payload)[0]
            if message_id not in dropped_ids:
                out += record
        else:
            out += record
    return bytes(out)


def fly(fixtures: Path, px4_root: Path, px4_wd: Path, out_dir: Path) -> int:
    from pymavlink import mavutil

    scenario = json.loads((fixtures / "scenarios.json").read_text())["scenarios"]["px4_x500_takeoff_hover_land"]
    acceptance = scenario["acceptance"]
    takeoff_alt = acceptance["takeoff_altitude_m"]["target"]
    hover_s = acceptance["hover_duration_s"]["minimum"]
    deadline_s = acceptance["deadline_s"]

    px4_wd.mkdir(parents=True, exist_ok=True)
    px4_bin = px4_root / "build/px4_sitl_default/bin/px4"
    env = dict(os.environ)
    env.update({
        "PX4_SIM_MODEL": "gz_x500",
        "PX4_GZ_WORLD": "default",     # matches scenario_world.sdf's <world name="default">
        "PX4_GZ_MODEL_NAME": "x500",   # attach to the model already spawned, don't spawn our own
        "PX4_GZ_STANDALONE": "1",      # we own gz sim's lifecycle, not PX4
        "HOME": os.environ.get("HOME", "/tmp/home"),
    })
    px4_log = (out_dir / "px4.log").open("w")
    px4 = subprocess.Popen(
        [str(px4_bin), str(px4_root / "ROMFS/px4fmu_common"), "-s", "etc/init.d-posix/rcS", "-w", str(px4_wd), "-i", "0"],
        stdout=px4_log, stderr=subprocess.STDOUT, env=env, cwd=str(px4_wd),
    )

    start = time.time()
    landed_ok = False

    def elapsed() -> float:
        return time.time() - start

    def sample() -> tuple[float | None, int | None]:
        alt, landed_state = None, None
        deadline = time.time() + 0.5
        while time.time() < deadline:
            msg = master.recv_match(blocking=True, timeout=0.2)
            if msg is None:
                break
            mtype = msg.get_type()
            if mtype == "LOCAL_POSITION_NED":
                alt = -msg.z
            elif mtype == "EXTENDED_SYS_STATE":
                landed_state = msg.landed_state
        return alt, landed_state

    def send_command(command: int, *params: float, tries: int = 5) -> bool:
        padded = list(params) + [0] * (7 - len(params))
        for _ in range(tries):
            master.mav.command_long_send(master.target_system, master.target_component, command, 0, *padded)
            ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
            if ack is not None and ack.command == command and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                return True
        return False

    try:
        if px4.poll() is None:
            master = mavutil.mavlink_connection("udpin:0.0.0.0:14540")
            try:
                master.wait_heartbeat(timeout=60)
            except Exception:
                master = None
            if master is not None:
                while elapsed() < 20:  # let the EKF settle before arming
                    sample()
                master.mav.param_set_send(
                    master.target_system, master.target_component,
                    b"MIS_TAKEOFF_ALT", float(takeoff_alt), mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
                )
                time.sleep(0.5)
                # Force-arm (param2 magic number): headless CI has no RC/safety
                # switch, and prearm gating here is not what this scenario proves.
                if send_command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196) and send_command(
                    mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, float("nan"), float("nan"), float("nan"), takeoff_alt
                ):
                    reached = elapsed() + deadline_s
                    while elapsed() < reached:
                        alt, _ = sample()
                        if alt is not None and abs(alt - takeoff_alt) <= acceptance["takeoff_altitude_m"]["tolerance"]:
                            break
                        time.sleep(0.2)
                    hover_until = elapsed() + hover_s
                    while elapsed() < hover_until and elapsed() < deadline_s:
                        sample()
                        time.sleep(0.2)
                    if send_command(mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0, float("nan"), float("nan"), float("nan"), float("nan")):
                        while elapsed() < deadline_s:
                            _, landed_state = sample()
                            if landed_state == mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND:
                                landed_ok = True
                                break
                            time.sleep(0.2)
    finally:
        px4.terminate()
        try:
            px4.wait(timeout=10)
        except subprocess.TimeoutExpired:
            px4.kill()
            px4.wait(timeout=10)
        px4_log.flush()

        logs = sorted(glob.glob(str(px4_wd / "log" / "**" / "*.ulg"), recursive=True))
        if logs:
            trimmed = trim_ulog(Path(logs[-1]).read_bytes())
            (out_dir / "x500.ulg").write_bytes(trimmed)

    return 0 if landed_ok else 1


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare_x500(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "fly":
        raise SystemExit(fly(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])))
    else:
        raise SystemExit(f"unknown x500_runner command {sys.argv[1]!r}")
