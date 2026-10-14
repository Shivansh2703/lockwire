"""Baked into the E5.10 sim-jazzy image at /opt/lockwire.

The ANYmal-only parts of the old run_e5_10.py scaffold (prepare_anymal,
build_squat_trajectory_yaml), turned into a small CLI so run-admission.sh can
call them without importing a module that reads /ws at import time. No PX4
here (cycle 1); the X500 half stays in run_e5_10.py for cycle 2.
"""

from __future__ import annotations

import copy
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LEG_JOINTS = [f"{leg}_{joint}" for leg in ("LF", "RF", "LH", "RH") for joint in ("HAA", "HFE", "KFE")]


def prepare_anymal(fixtures: Path, merged_out: Path) -> None:
    anymal_root = fixtures / "official/anymal-d"
    robot = ET.parse(anymal_root / "urdf/anymal.urdf").getroot()
    overlay = ET.parse(fixtures / "anymal_control_overlay.xml").getroot()
    for child in overlay:
        robot.append(copy.deepcopy(child))
    text = ET.tostring(robot, encoding="unicode")
    text = text.replace("package://anymal_d_simple_description/", f"file://{anymal_root}/")
    text = text.replace(">controllers.yaml<", f">{fixtures / 'controllers.yaml'}<")
    merged_out.write_text(text)


def _nominal(scenarios_path: Path) -> dict:
    return json.loads(scenarios_path.read_text())["scenarios"]["anymal_d_effort_squat"]["test_trajectory"]["nominal_rad"]


def build_hold_trajectory(scenarios_path: Path, yaml_out: Path) -> None:
    """A single nominal-pose point: settles the collapsed spawn pose before
    either scenario's evidence capture starts, so the transient never lands
    in the judged trace."""
    nominal = _nominal(scenarios_path)
    vals = ", ".join(str(nominal[j]) for j in LEG_JOINTS)
    yaml_out.write_text(
        "joint_names: [" + ", ".join(LEG_JOINTS) + "]\n"
        "points:\n"
        f"- positions: [{vals}]\n"
        "  time_from_start: {sec: 3, nanosec: 0}\n"
    )


def build_squat_trajectory(scenarios_path: Path, yaml_out: Path) -> None:
    scenario = json.loads(scenarios_path.read_text())["scenarios"]["anymal_d_effort_squat"]["test_trajectory"]
    nominal, delta, cycle_s, cycles = scenario["nominal_rad"], scenario["squat_delta_rad"], scenario["cycle_duration_s"], scenario["cycles"]

    def squat(joints: dict) -> dict:
        out = dict(joints)
        for name, value in joints.items():
            for key, amount in delta.items():
                if name.endswith(key):
                    out[name] = value + (amount if value >= 0 else -amount)
        return out

    squatted = squat(nominal)
    points, t = [], 0.0
    for _ in range(cycles):
        points.append((t, nominal))
        points.append((t + cycle_s / 2, squatted))
        t += cycle_s
    points.append((t, nominal))

    lines = ["joint_names: [" + ", ".join(LEG_JOINTS) + "]", "points:"]
    for when, positions in points:
        vals = ", ".join(str(positions[j]) for j in LEG_JOINTS)
        sec, nsec = int(when), int(round((when - int(when)) * 1e9))
        lines.append(f"- positions: [{vals}]")
        lines.append(f"  time_from_start: {{sec: {sec}, nanosec: {nsec}}}")
    yaml_out.write_text("\n".join(lines))


def extract_tracking() -> None:
    """Reduce a full ``ros2 topic echo .../controller_state`` stream to the
    header/joint_names/error triple the judge (jobs._anymal_evidence_reason)
    needs, one flat YAML document per frame. A simplification of the real
    message shape (skips reference/feedback point sub-fields); revisit if a
    later cycle needs the full nested shape."""
    import yaml  # provided by ROS's python3-yaml dependency

    for document in yaml.safe_load_all(sys.stdin):
        if not isinstance(document, dict):
            continue
        header = document.get("header", {})
        print("header: {stamp: {sec: %d, nanosec: %d}, frame_id: ''}" % (
            header.get("stamp", {}).get("sec", 0), header.get("stamp", {}).get("nanosec", 0)))
        print("joint_names: [" + ", ".join(document.get("joint_names", [])) + "]")
        for field in ("reference", "feedback", "error"):
            values = document.get(field, {}).get("positions", [])
            print(f"{field}:")
            for value in values:
                print(f"- {value}")
        print("---")
        sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare_anymal(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "hold-trajectory":
        build_hold_trajectory(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "trajectory":
        build_squat_trajectory(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "extract-tracking":
        extract_tracking()
    else:
        raise SystemExit(f"unknown anymal_runner command {sys.argv[1]!r}")
