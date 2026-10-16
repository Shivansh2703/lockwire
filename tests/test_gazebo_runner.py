"""OBSOLETE: this file checked Gazebo/ROS output directly,
never Lockwire code, and is superseded by tests/test_sim_jazzy_admission.py
(jobs.start_motion_job). Kept until cycle 3 deletes it.

E5.9: the sim-jazzy CI job proves container isolation, asset identity, and
that a disabled controller produces no motion.

These never run Gazebo themselves -- the sim-jazzy job in .github/workflows/ci.yml
does that (packaging/sim_jazzy/run_e5_10.py), writing its evidence to
LOCKWIRE_SIM_OUT. This file only reads and checks that evidence, so it can
also be pointed at a local run for debugging. Skipped everywhere else: local
Docker stays paused, and there is nothing to read without LOCKWIRE_SIM=1.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
from ruamel.yaml import YAML

_YAML = YAML(typ="safe")

FIXTURES = Path(__file__).parent / "fixtures" / "e5_10_real_scenarios"
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())
LEG_JOINTS = [
    f"{leg}_{joint}" for leg in ("LF", "RF", "LH", "RH") for joint in ("HAA", "HFE", "KFE")
]

pytestmark = pytest.mark.skipif(
    os.environ.get("LOCKWIRE_SIM") != "1",
    reason="E5.9 runs only in the sim-jazzy CI job (LOCKWIRE_SIM=1); local Docker stays paused",
)


@pytest.fixture
def sim_out() -> Path:
    out = os.environ.get("LOCKWIRE_SIM_OUT")
    assert out, "LOCKWIRE_SIM=1 requires LOCKWIRE_SIM_OUT to point at the run's evidence directory"
    path = Path(out)
    assert path.is_dir(), f"LOCKWIRE_SIM_OUT does not exist: {path}"
    return path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_container_has_no_network(sim_out: Path) -> None:
    document = _read_json(sim_out / "network.json")
    assert document["connect_attempted"] is True
    assert document["connect_failed"] is True, (
        "a socket connect to an external address succeeded -- --network none is not in effect"
    )


def test_pinned_assets_match_lock(sim_out: Path) -> None:
    document = _read_json(sim_out / "assets_verify.json")
    assert document["ok"] is True, document["error"]


def test_controller_disabled_gives_no_motion(sim_out: Path) -> None:
    """X500 sits on the ground with no motor command; base pose barely moves.

    Not ANYmal: scenarios.json's own anymal_d_controller_disabled_negative
    requires >=0.01 rad of gravity motion once its effort controller goes
    inactive (hold_joints: false) -- the opposite property. See the worker
    report for why E5.9's generic "controller off -> no motion" check uses
    X500 instead.
    """
    path = sim_out / "x500_idle_pose.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) >= 2, "not enough idle-pose samples to judge motion"
    x0, y0, z0 = rows[0].get("x", 0.0), rows[0].get("y", 0.0), rows[0].get("z", 0.0)
    epsilon = 0.02  # metres; ground contact settling noise, not real motion
    for row in rows[1:]:
        displacement = math.dist(
            (x0, y0, z0), (row.get("x", 0.0), row.get("y", 0.0), row.get("z", 0.0))
        )
        assert displacement < epsilon, (
            f"X500 base moved {displacement:.4f} m with no controller command"
        )


def test_anymal_controller_disabled_negative(sim_out: Path) -> None:
    """B4/E5.9's real controller-disabled negative: scenarios.json's
    anymal_d_controller_disabled_negative. With anymal_effort_controller
    deactivated (hold_joints:false in the overlay) and the 2s gravity window
    captured, the legs must visibly NOT hold -- moving at least the fixture's
    stated threshold, in the actual joint state, under gravity alone.
    """
    scenario = SCENARIOS["scenarios"]["anymal_d_controller_disabled_negative"]
    acceptance = scenario["acceptance"]

    listing = (sim_out / "anymal_controller_list_after_deactivate.txt").read_text()
    assert "anymal_effort_controller" in listing, (
        "controller_manager listing missing anymal_effort_controller"
    )
    for line in listing.splitlines():
        if "anymal_effort_controller" in line:
            assert "inactive" in line, (
                f"anymal_effort_controller not reported inactive during capture: {line!r}"
            )

    path = sim_out / "anymal_negative_joint_states.raw"
    text = path.read_text()
    messages = [doc for doc in _YAML.load_all(text) if doc]
    assert messages, "no /joint_states messages captured during the gravity-release window"

    series: dict[str, list[tuple[float, float]]] = {name: [] for name in LEG_JOINTS}
    for message in messages:
        names = message.get("name", [])
        positions = message.get("position", [])
        velocities = message.get("velocity", [])
        for idx, name in enumerate(names):
            if name in series:
                pos = positions[idx] if idx < len(positions) else 0.0
                vel = velocities[idx] if idx < len(velocities) else 0.0
                series[name].append((float(pos), float(vel)))

    pos_threshold = acceptance["minimum_actual_position_change_rad"]
    speed_threshold = acceptance["or_minimum_actual_speed_rad_s"]

    moved = False
    for name in LEG_JOINTS:
        samples = series[name]
        if not samples:
            continue
        start_pos = samples[0][0]
        max_delta = max(abs(pos - start_pos) for pos, _ in samples)
        max_speed = max(abs(vel) for _, vel in samples)
        if max_delta >= pos_threshold or max_speed >= speed_threshold:
            moved = True
            break

    assert moved, (
        f"no leg joint moved >= {pos_threshold} rad or >= {speed_threshold} rad/s under gravity "
        "with the controller inactive and hold_joints disabled"
    )
