"""Drives Gazebo Harmonic inside the sim-jazzy CI container for E5.9/E5.10.

Not run or importable outside that container: it shells out to `gz`/`ros2`/the
built PX4 SITL binary, which only exist there. tests/test_gazebo_runner.py and
tests/test_e5_10_traces.py never import this module; they read the files it
writes to /out, so the assertions can run on the host too (LOCKWIRE_SIM=1
skips them everywhere else).

X500: flown by real PX4 SITL (packaging/sim_jazzy/Dockerfile builds
px4_sitl_default from the pinned v1.16.2 release), commanded over MAVLink
vehicle_command the same way scenarios.json's px4_x500_takeoff_hover_land
declares (arm / NAV_TAKEOFF / hover / NAV_LAND), read back from PX4's own
uorb-backed telemetry (LOCAL_POSITION_NED, EXTENDED_SYS_STATE), not command
echo. No hand-written motor-speed control loop.

ANYmal: controller-driven squat trace (effort controller active) plus the
controller-disabled negative scenarios.json requires (effort controller
deactivated, hold_joints:false, gravity must move the legs).
"""

from __future__ import annotations

import copy
import json
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

WS = Path("/ws")
OUT = Path("/out")
FIXTURES = WS / "tests/fixtures/e5_10_real_scenarios"
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())
LEG_JOINTS = [f"{leg}_{joint}" for leg in ("LF", "RF", "LH", "RH") for joint in ("HAA", "HFE", "KFE")]

PX4_ROOT = Path("/opt/px4-autopilot")
PX4_BIN = PX4_ROOT / "build/px4_sitl_default/bin/px4"

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


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def write_json(name: str, payload: dict) -> None:
    (OUT / name).write_text(json.dumps(payload, sort_keys=True) + "\n")


def check_network_isolation() -> None:
    """E5.9 part 1: inside --network none, an external connect must fail."""
    ok = False
    error = None
    try:
        socket.setdefaulttimeout(3)
        socket.create_connection(("1.1.1.1", 53))
    except OSError as exc:
        ok = True
        error = str(exc)
    write_json("network.json", {"connect_attempted": True, "connect_failed": ok, "error": error})


def check_asset_hashes() -> None:
    """E5.9 part 2: reuse verify.py's own asset-hash check (D... assets.lock)."""
    sys.path.insert(0, str(FIXTURES))
    import verify  # type: ignore

    try:
        verify.verify_assets()
        verify.verify_official_models()
        verify.verify_control_inputs()
        write_json("assets_verify.json", {"ok": True, "error": None})
    except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
        write_json("assets_verify.json", {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def start_gz_server(world: Path, resource_path: str) -> subprocess.Popen:
    env = {"GZ_SIM_RESOURCE_PATH": resource_path}
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    log = (OUT / "gz_server.log").open("w")
    # -s: server only (headless, no GUI/rendering client); -r: run immediately.
    proc = subprocess.Popen(
        ["gz", "sim", "-s", "-r", "-v", "1", str(world)],
        stdout=log, stderr=subprocess.STDOUT, env=full_env,
    )
    time.sleep(5)
    if proc.poll() is not None:
        log.flush()
        tail = (OUT / "gz_server.log").read_text()[-4000:]
        raise RuntimeError(
            f"gz sim server exited during startup (code {proc.returncode}); tail of gz_server.log:\n{tail}"
        )
    return proc


def read_pose(model: str) -> dict | None:
    result = run(["gz", "topic", "-e", "-t", f"/model/{model}/pose", "-n", "1", "--json-output"], timeout=5)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        return None


def prepare_x500(scratch: Path) -> tuple[Path, str]:
    px4_models = FIXTURES / "official/px4-gazebo-models/models"
    dst = scratch / "models"
    shutil.copytree(px4_models, dst)
    model_sdf = dst / "x500/model.sdf"
    text = model_sdf.read_text()
    # Give the runtime-spawned model a pose topic; the official upstream file
    # ships without one (nothing spawns it standalone like this normally does).
    text = text.replace("</model>", POSE_PUBLISHER_XML + "</model>", 1)
    model_sdf.write_text(text)
    return dst / "x500/model.sdf", str(dst)


def prepare_anymal(scratch: Path) -> Path:
    anymal_root = FIXTURES / "official/anymal-d"
    urdf_path = anymal_root / "urdf/anymal.urdf"
    overlay_path = FIXTURES / "anymal_control_overlay.xml"

    robot = ET.parse(urdf_path).getroot()
    overlay = ET.parse(overlay_path).getroot()
    for child in overlay:
        robot.append(copy.deepcopy(child))

    text = ET.tostring(robot, encoding="unicode")
    # xacro is not in this image's package set; the URDF has no xacro macros to
    # expand (checked in tests/fixtures/e5_10_real_scenarios/README.md scope),
    # so ElementTree merge + a plain path rewrite is the whole job.
    prefix = "package://anymal_d_simple_description/"
    text = text.replace(prefix, f"file://{anymal_root}/")
    merged = scratch / "anymal_merged.urdf"
    merged.write_text(text)

    # <parameters>controllers.yaml</parameters> in the overlay is a relative
    # path ros2_control resolves via CWD/ROS package share, neither of which
    # applies to a bare urdf file, so point it at the fixture directly.
    fixed = merged.read_text().replace(">controllers.yaml<", f">{FIXTURES / 'controllers.yaml'}<")
    merged.write_text(fixed)
    return merged


def spawn(name: str, file: Path, x: float, y: float, z: float) -> None:
    result = run(["ros2", "run", "ros_gz_sim", "create", "-name", name, "-file", str(file),
                  "-x", str(x), "-y", str(y), "-z", str(z)], timeout=30)
    (OUT / f"spawn_{name}.log").write_text(result.stdout + result.stderr)


def x500_idle_check(duration_s: float = 5.0) -> None:
    """E5.9 extra: with PX4 not yet attached (no controller command at all),
    a drone at rest on the ground under gravity+contact should not move."""
    start = time.time()
    trace = []
    while time.time() - start < duration_s:
        pose = read_pose("x500")
        if pose is not None:
            trace.append({"t": time.time() - start, **pose.get("position", {})})
        time.sleep(0.5)
    with (OUT / "x500_idle_pose.jsonl").open("w") as handle:
        for row in trace:
            handle.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------
# E5.10 X500: real PX4 SITL, MAVLink vehicle_command, uorb-backed telemetry.
# --------------------------------------------------------------------------

def fly_x500_with_px4(scenario: dict) -> None:
    import os

    from pymavlink import mavutil

    acceptance = scenario["acceptance"]
    deadline_s = acceptance["deadline_s"]
    takeoff_alt = acceptance["takeoff_altitude_m"]["target"]
    takeoff_tol = acceptance["takeoff_altitude_m"]["tolerance"]
    hover_s = acceptance["hover_duration_s"]["minimum"]
    hover_vtol = acceptance["hover_duration_s"]["vertical_tolerance_m"]
    hover_max_hspeed = acceptance["hover_duration_s"]["max_horizontal_speed_m_s"]

    px4_wd = Path("/tmp/px4_wd")
    px4_wd.mkdir(parents=True, exist_ok=True)
    px4_log = (OUT / "px4.log").open("w")

    env = dict(os.environ)
    env.update({
        "PX4_SIM_MODEL": "gz_x500",
        "PX4_GZ_WORLD": "default",       # matches scenario_world.sdf's <world name="default">
        "PX4_GZ_MODEL_NAME": "x500",     # attach to the model we already spawned, don't spawn our own
        "PX4_GZ_STANDALONE": "1",        # we own gz sim's lifecycle, not PX4
        "HOME": os.environ.get("HOME", "/tmp/home"),
    })

    px4 = subprocess.Popen(
        [str(PX4_BIN), str(PX4_ROOT / "ROMFS/px4fmu_common"), "-s", "etc/init.d-posix/rcS", "-w", str(px4_wd), "-i", "0"],
        stdout=px4_log, stderr=subprocess.STDOUT, env=env, cwd=str(px4_wd),
    )

    trace: list[dict] = []
    commands: dict = {
        "deadline_s": deadline_s, "aborted": False, "abort_reason": None,
        "arm_t": None, "takeoff_cmd_t": None, "hover_reached_t": None,
        "hover_ok_through_t": None, "land_cmd_t": None, "landed_t": None,
        "landed_altitude_m": None, "sensors_seen": False,
    }

    def finish(reason: str | None = None) -> None:
        commands["aborted"] = reason is not None
        commands["abort_reason"] = reason
        with (OUT / "x500_px4_trace.jsonl").open("w") as handle:
            for row in trace:
                handle.write(json.dumps(row) + "\n")
        write_json("x500_px4_commands.json", commands)
        px4.terminate()
        try:
            px4.wait(timeout=10)
        except subprocess.TimeoutExpired:
            px4.kill()

    try:
        if px4.poll() is not None:
            finish(f"px4 exited immediately (code {px4.returncode})")
            return

        start = time.time()

        def elapsed() -> float:
            return time.time() - start

        try:
            master = mavutil.mavlink_connection("udpin:0.0.0.0:14540")
            master.wait_heartbeat(timeout=60)
        except Exception as exc:  # noqa: BLE001
            finish(f"no MAVLink heartbeat from PX4 within 60s: {exc}")
            return

        def send_command(command: int, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0, tries: int = 5) -> bool:
            for _ in range(tries):
                master.mav.command_long_send(
                    master.target_system, master.target_component, command, 0, p1, p2, p3, p4, p5, p6, p7,
                )
                ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
                if ack is not None and ack.command == command and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    return True
            return False

        def sample(phase: str) -> tuple[float, float]:
            """Drain pending mavlink messages, record telemetry, return (altitude_m, horizontal_speed)."""
            alt = None
            hspeed = None
            landed_state = None
            deadline = time.time() + 0.5
            while time.time() < deadline:
                msg = master.recv_match(blocking=True, timeout=0.2)
                if msg is None:
                    break
                mtype = msg.get_type()
                if mtype == "LOCAL_POSITION_NED":
                    alt = -msg.z
                    hspeed = (msg.vx ** 2 + msg.vy ** 2) ** 0.5
                    trace.append({
                        "t": elapsed(), "phase": phase, "x": msg.x, "y": msg.y, "z": msg.z,
                        "vx": msg.vx, "vy": msg.vy, "vz": msg.vz,
                    })
                elif mtype == "EXTENDED_SYS_STATE":
                    landed_state = msg.landed_state
                elif mtype in ("HIGHRES_IMU", "SCALED_IMU2", "ATTITUDE"):
                    commands["sensors_seen"] = True
            if landed_state is not None:
                trace.append({"t": elapsed(), "phase": phase, "landed_state": landed_state})
            return alt, hspeed

        # Let the EKF settle (GPS/baro/mag fusion) before arming.
        settle_deadline = elapsed() + 20
        while elapsed() < settle_deadline:
            sample("settle")
            time.sleep(0.2)

        master.mav.param_set_send(
            master.target_system, master.target_component,
            b"MIS_TAKEOFF_ALT", float(takeoff_alt), mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
        )
        time.sleep(0.5)

        # Force-arm (param2 magic number): headless CI has no RC/safety switch,
        # and prearm gating here is not what this scenario is proving.
        if not send_command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196):
            finish("arm command not accepted")
            return
        commands["arm_t"] = elapsed()

        if not send_command(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, float("nan"),
                             float("nan"), float("nan"), float(takeoff_alt)):
            finish("takeoff command not accepted")
            return
        commands["takeoff_cmd_t"] = elapsed()

        # Wait for the climb to reach target altitude.
        reached = False
        while elapsed() < deadline_s:
            alt, _ = sample("climb")
            if alt is not None and abs(alt - takeoff_alt) <= takeoff_tol:
                reached = True
                break
            time.sleep(0.2)
        if not reached:
            finish(f"never reached takeoff altitude within tolerance by t={elapsed():.1f}s")
            return
        commands["hover_reached_t"] = elapsed()

        # Hover window: altitude within tolerance and horizontal speed bounded
        # for hover_s continuously.
        hover_start = elapsed()
        hover_ok = True
        while elapsed() - hover_start < hover_s:
            if elapsed() >= deadline_s:
                finish("deadline exceeded during hover window")
                return
            alt, hspeed = sample("hover")
            if alt is not None and abs(alt - takeoff_alt) > hover_vtol:
                hover_ok = False
            if hspeed is not None and hspeed > hover_max_hspeed:
                hover_ok = False
            time.sleep(0.2)
        commands["hover_ok_through_t"] = elapsed()
        if not hover_ok:
            commands["abort_reason"] = "hover tolerance breached (recorded in trace; not fatal to the run)"

        if not send_command(mavutil.mavlink.MAV_CMD_NAV_LAND, 0, 0, 0, float("nan"),
                             float("nan"), float("nan"), float("nan")):
            finish("land command not accepted")
            return
        commands["land_cmd_t"] = elapsed()

        landed = False
        last_alt = None
        while elapsed() < deadline_s:
            alt, _ = sample("land")
            if alt is not None:
                last_alt = alt
            msg = None
            for row in reversed(trace):
                if row.get("phase") == "land" and "landed_state" in row:
                    msg = row
                    break
            if msg is not None and msg["landed_state"] == mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND:
                landed = True
                break
            time.sleep(0.2)

        commands["landed_t"] = elapsed() if landed else None
        commands["landed_altitude_m"] = last_alt
        if not landed:
            finish("never confirmed landed (EXTENDED_SYS_STATE) before deadline")
            return

        finish(None)
    except Exception as exc:  # noqa: BLE001 - always leave evidence behind
        finish(f"{type(exc).__name__}: {exc}")
        raise


def build_squat_trajectory_yaml(anymal_scenario: dict) -> str:
    nominal = anymal_scenario["test_trajectory"]["nominal_rad"]
    delta = anymal_scenario["test_trajectory"]["squat_delta_rad"]
    cycle_s = anymal_scenario["test_trajectory"]["cycle_duration_s"]
    cycles = anymal_scenario["test_trajectory"]["cycles"]

    def squat(joints: dict) -> dict:
        out = dict(joints)
        for name, value in joints.items():
            for key, amount in delta.items():
                if name.endswith(key):
                    out[name] = value + (amount if value >= 0 else -amount)
        return out

    squatted = squat(nominal)
    points = []
    t = 0.0
    for _ in range(cycles):
        points.append((t, nominal))
        points.append((t + cycle_s / 2, squatted))
        t += cycle_s
    points.append((t, nominal))

    lines = ["joint_names: [" + ", ".join(LEG_JOINTS) + "]", "points:"]
    for when, positions in points:
        vals = ", ".join(str(positions[j]) for j in LEG_JOINTS)
        sec = int(when)
        nsec = int(round((when - sec) * 1e9))
        lines.append(f"- positions: [{vals}]")
        lines.append(f"  time_from_start: {{sec: {sec}, nanosec: {nsec}}}")
    return "\n".join(lines), t


def anymal_squat_run() -> None:
    """E5.10 ANYmal motion: activate the effort controller and run the
    scenarios.json test_trajectory (nominal/squat/nominal, 2 cycles), same
    ros2 topic echo capture pattern as packaging/motion/run-admission.sh."""
    run(["ros2", "control", "load_controller", "--set-state", "active", "joint_state_broadcaster"], timeout=15)
    run(["ros2", "control", "load_controller", "--set-state", "active", "anymal_effort_controller"], timeout=15)

    yaml_body, total_s = build_squat_trajectory_yaml(SCENARIOS["scenarios"]["anymal_d_effort_squat"])
    traj_file = OUT / "squat_trajectory.yaml"
    traj_file.write_text(yaml_body)

    window = int(total_s) + 5
    state_out = (OUT / "anymal_joint_states.raw").open("w")
    ctrl_out = (OUT / "anymal_controller_state.raw").open("w")
    state_p = subprocess.Popen(["timeout", str(window), "ros2", "topic", "echo", "/joint_states"], stdout=state_out)
    ctrl_p = subprocess.Popen(
        ["timeout", str(window), "ros2", "topic", "echo", "/anymal_effort_controller/controller_state"],
        stdout=ctrl_out,
    )
    time.sleep(1)
    run(["ros2", "topic", "pub", "--once", "/anymal_effort_controller/joint_trajectory",
         "trajectory_msgs/msg/JointTrajectory", "-f", str(traj_file)], timeout=15)
    state_p.wait()
    ctrl_p.wait()


def anymal_disabled_negative_run(scenario: dict) -> None:
    """E5.9: scenarios.json's anymal_d_controller_disabled_negative. Deactivate
    the effort controller (hold_joints:false is already set in the overlay),
    confirm it's inactive, then capture /joint_states through the gravity
    window with no controller commands sent."""
    run(["ros2", "control", "load_controller", "--set-state", "inactive", "anymal_effort_controller"], timeout=15)

    listed = run(["ros2", "control", "list_controllers"], timeout=15)
    (OUT / "anymal_controller_list_after_deactivate.txt").write_text(listed.stdout + listed.stderr)

    window = int(scenario["actions"][1]["duration_s"]) + 3
    state_out = (OUT / "anymal_negative_joint_states.raw").open("w")
    state_p = subprocess.Popen(["timeout", str(window), "ros2", "topic", "echo", "/joint_states"], stdout=state_out)
    state_p.wait()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    scratch = Path("/tmp/sim_jazzy")
    scratch.mkdir(parents=True, exist_ok=True)

    check_network_isolation()
    check_asset_hashes()

    x500_sdf, x500_resource_path = prepare_x500(scratch)
    anymal_urdf = prepare_anymal(scratch)

    world = FIXTURES / "scenario_world.sdf"
    server = start_gz_server(world, x500_resource_path)
    try:
        spawn("x500", x500_sdf, 0.0, 0.0, 0.1)
        x500_idle_check()
        fly_x500_with_px4(SCENARIOS["scenarios"]["px4_x500_takeoff_hover_land"])

        spawn("anymal", anymal_urdf, 3.0, 0.0, 0.6)
        time.sleep(3)
        anymal_squat_run()
        anymal_disabled_negative_run(SCENARIOS["scenarios"]["anymal_d_controller_disabled_negative"])
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
