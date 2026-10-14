"""OBSOLETE: this file checked Gazebo/ROS output directly,
never Lockwire code, and is superseded by tests/test_sim_jazzy_admission.py
(jobs.start_motion_job). Kept until cycle 3 deletes it.

E5.10: controller-driven traces from the sim-jazzy CI job.

Reads the evidence packaging/sim_jazzy/run_e5_10.py wrote to LOCKWIRE_SIM_OUT
(X500 pose while climbing, ANYmal /joint_states and controller_state while
squatting) and checks it against the acceptance numbers already recorded in
tests/fixtures/e5_10_real_scenarios/scenarios.json, not invented thresholds.
Skipped everywhere but the sim-jazzy job (LOCKWIRE_SIM=1); local Docker stays
paused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from ruamel.yaml import YAML

_YAML = YAML(typ="safe")

FIXTURES = Path(__file__).parent / "fixtures" / "e5_10_real_scenarios"
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())
LEG_JOINTS = [f"{leg}_{joint}" for leg in ("LF", "RF", "LH", "RH") for joint in ("HAA", "HFE", "KFE")]

pytestmark = pytest.mark.skipif(
    os.environ.get("LOCKWIRE_SIM") != "1",
    reason="E5.10 runs only in the sim-jazzy CI job (LOCKWIRE_SIM=1); local Docker stays paused",
)


@pytest.fixture
def sim_out() -> Path:
    out = os.environ.get("LOCKWIRE_SIM_OUT")
    assert out, "LOCKWIRE_SIM=1 requires LOCKWIRE_SIM_OUT to point at the run's evidence directory"
    path = Path(out)
    assert path.is_dir(), f"LOCKWIRE_SIM_OUT does not exist: {path}"
    return path


def _load_yaml_stream(path: Path) -> list[dict]:
    text = path.read_text()
    return [doc for doc in _YAML.load_all(text) if doc]


def test_x500_px4_takeoff_hover_land(sim_out: Path) -> None:
    """X500 flies on PX4 SITL, judged against scenarios.json's own
    px4_x500_takeoff_hover_land acceptance block -- not an invented threshold,
    and not command echo (decision_sources are position/velocity/landed/
    sensors, all read back from PX4's telemetry below)."""
    scenario = SCENARIOS["scenarios"]["px4_x500_takeoff_hover_land"]
    acceptance = scenario["acceptance"]

    commands = json.loads((sim_out / "x500_px4_commands.json").read_text())
    rows = [json.loads(line) for line in (sim_out / "x500_px4_trace.jsonl").read_text().splitlines() if line.strip()]
    assert rows, "no X500 PX4 telemetry samples recorded"

    assert commands["abort_reason"] in (None, "hover tolerance breached (recorded in trace; not fatal to the run)"), (
        f"flight aborted: {commands['abort_reason']}"
    )
    assert commands["arm_t"] is not None, "PX4 never accepted the arm command"
    assert commands["takeoff_cmd_t"] is not None, "PX4 never accepted the takeoff command"
    assert commands["hover_reached_t"] is not None, "X500 never reached takeoff altitude within tolerance"
    assert commands["land_cmd_t"] is not None, "PX4 never accepted the land command"
    assert commands["landed_t"] is not None, acceptance["requires_landed_feedback"] and (
        "no vehicle_land_detected/EXTENDED_SYS_STATE landed feedback before the deadline"
    )
    assert commands["sensors_seen"], "no simulated sensor telemetry (IMU/attitude) observed during the flight"

    deadline_s = acceptance["deadline_s"]
    assert commands["landed_t"] <= deadline_s, f"landed at t={commands['landed_t']:.1f}s, deadline is {deadline_s}s"

    target = acceptance["takeoff_altitude_m"]["target"]
    hover_rows = [row for row in rows if row.get("phase") == "hover" and "z" in row]
    assert hover_rows, "no altitude samples recorded during the hover window"
    for row in hover_rows:
        altitude = -row["z"]
        assert abs(altitude - target) <= acceptance["hover_duration_s"]["vertical_tolerance_m"], (
            f"hover altitude {altitude:.2f} m outside {target}+/-{acceptance['hover_duration_s']['vertical_tolerance_m']} m"
        )
        hspeed = (row["vx"] ** 2 + row["vy"] ** 2) ** 0.5
        assert hspeed <= acceptance["hover_duration_s"]["max_horizontal_speed_m_s"], (
            f"hover horizontal speed {hspeed:.2f} m/s exceeds {acceptance['hover_duration_s']['max_horizontal_speed_m_s']} m/s"
        )
    hover_span = commands["hover_ok_through_t"] - commands["hover_reached_t"]
    assert hover_span >= acceptance["hover_duration_s"]["minimum"] - 0.5, (
        f"hover window only {hover_span:.1f}s (need >= {acceptance['hover_duration_s']['minimum']}s)"
    )

    landed_altitude = commands["landed_altitude_m"]
    assert landed_altitude is not None and landed_altitude <= acceptance["landed_altitude_m"]["maximum"], (
        f"landed altitude {landed_altitude} m exceeds {acceptance['landed_altitude_m']['maximum']} m"
    )

    # Not scripted/constant: successive altitude samples must actually differ.
    unique_z = {round(row["z"], 3) for row in rows if "z" in row}
    assert len(unique_z) > 1, "X500 altitude trace is constant -- looks scripted, not simulated"


def test_anymal_joints_squat_at_least_twice(sim_out: Path) -> None:
    trajectory = SCENARIOS["scenarios"]["anymal_d_effort_squat"]["test_trajectory"]
    acceptance = SCENARIOS["scenarios"]["anymal_d_effort_squat"]["acceptance"]
    nominal = trajectory["nominal_rad"]
    lower = {
        "HFE": acceptance["hfe_peak_displacement_rad"]["target"] - acceptance["hfe_peak_displacement_rad"]["tolerance"],
        "KFE": acceptance["kfe_peak_displacement_rad"]["target"] - acceptance["kfe_peak_displacement_rad"]["tolerance"],
    }
    upper = {
        "HFE": acceptance["hfe_peak_displacement_rad"]["target"] + acceptance["hfe_peak_displacement_rad"]["tolerance"],
        "KFE": acceptance["kfe_peak_displacement_rad"]["target"] + acceptance["kfe_peak_displacement_rad"]["tolerance"],
    }
    thresholds = lower

    messages = _load_yaml_stream(sim_out / "anymal_joint_states.raw")
    assert messages, "no /joint_states messages captured during the squat run"

    series: dict[str, list[float]] = {name: [] for name in LEG_JOINTS}
    for message in messages:
        names = message.get("name", [])
        positions = message.get("position", [])
        for name, position in zip(names, positions):
            if name in series:
                series[name].append(float(position))

    for name in LEG_JOINTS:
        joint_kind = "HFE" if name.endswith("HFE") else ("KFE" if name.endswith("KFE") else None)
        if joint_kind is None:
            continue  # HAA joints carry no squat_delta in this fixture
        deviations = [abs(value - nominal[name]) for value in series[name]]
        assert deviations, f"no samples recorded for {name}"
        above = [d >= thresholds[joint_kind] for d in deviations]
        crossings = sum(1 for prev, cur in zip(above, above[1:]) if cur and not prev)
        assert crossings >= 2, (
            f"{name} exceeded {thresholds[joint_kind]:.3f} rad only {crossings} time(s) (need >= 2, two squat cycles)"
        )
        peak = max(deviations)
        assert peak <= upper[joint_kind], (
            f"{name} peak displacement {peak:.3f} rad exceeds {upper[joint_kind]:.3f} rad "
            f"(target {upper[joint_kind] - (upper[joint_kind] - lower[joint_kind]) / 2:.3f} +/- tolerance)"
        )


def test_anymal_trace_is_not_constant_or_scripted(sim_out: Path) -> None:
    """command_echo_may_not_satisfy (scenarios.json): a scripted/echoed trace
    reports zero tracking error throughout. A genuinely simulated controller
    does not."""
    messages = _load_yaml_stream(sim_out / "anymal_controller_state.raw")
    assert messages, "no controller_state messages captured during the squat run"

    errors = []
    for message in messages:
        for key in ("actual", "desired"):
            assert key in message, f"controller_state message missing '{key}'"
        actual = message["actual"].get("positions", [])
        desired = message["desired"].get("positions", [])
        errors.extend(abs(a - d) for a, d in zip(actual, desired))

    assert errors, "no position samples in controller_state stream"
    assert any(error > 1e-4 for error in errors), (
        "tracking error is identically zero throughout -- looks like a command echo, not simulated tracking"
    )
    assert len({round(e, 5) for e in errors}) > 1, "tracking error trace is constant -- looks scripted"
