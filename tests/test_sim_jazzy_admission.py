"""jobs.start_motion_job launches the real
sim-jazzy image and Lockwire's own motion evidence admission judges it.

The CI-gated test (skipped everywhere but the sim-jazzy job, LOCKWIRE_SIM=1;
local Docker stays paused) exercises jobs.start_motion_job end to
end. The rest exercise jobs._anymal_evidence_reason directly with synthetic
evidence, same pattern as tests/test_motion_admission.py's pendulum fixtures:
no Docker, no sim, just Lockwire's own judge fed hand-built frames.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pytest

from lockwire_pro import jobs

ROOT = Path(__file__).parent
SCENARIOS = json.loads((ROOT / "fixtures" / "e5_10_real_scenarios" / "scenarios.json").read_text())
SQUAT = SCENARIOS["scenarios"]["anymal_d_effort_squat"]
NOMINAL = SQUAT["test_trajectory"]["nominal_rad"]
JOINTS = list(NOMINAL)  # LF_HAA, LF_HFE, LF_KFE, RF_HAA, ... — same order as jobs._LEG_JOINTS
SQUAT_CRITERIA = dict(SQUAT["acceptance"], nominal_rad=NOMINAL)
DISABLED_CRITERIA = SCENARIOS["scenarios"]["anymal_d_controller_disabled_negative"]["acceptance"]
X500_CRITERIA = SCENARIOS["scenarios"]["px4_x500_takeoff_hover_land"]["acceptance"]


def _x500_rows(*, genuine: bool, lands: bool) -> dict:
    """Synthetic vehicle_local_position/vehicle_land_detected/actuator_motors/
    vehicle_status rows shaped like jobs._x500_evidence_reason expects (row
    dicts with a microsecond ``timestamp``), not real ULog bytes -- this feeds
    the judge at the row level (jobs.read_ulog_rows is monkeypatched)."""
    target = X500_CRITERIA["takeoff_altitude_m"]["target"]
    position = [{"timestamp": 0, "z": 0.0, "vx": 0.0, "vy": 0.0}]
    if genuine:
        position.append({"timestamp": 5_000_000, "z": -target, "vx": 0.0, "vy": 0.0})
        for t in range(7, 20, 2):  # a 12s steady hover window at the target altitude
            position.append({"timestamp": t * 1_000_000, "z": -target, "vx": 0.0, "vy": 0.0})
        position.append({"timestamp": 25_000_000, "z": -0.05, "vx": 0.0, "vy": 0.0})
    else:
        position.append({"timestamp": 25_000_000, "z": 0.0, "vx": 0.0, "vy": 0.0})
    landed = [{"timestamp": 0, "landed": True}, {"timestamp": 4_000_000, "landed": False}]
    if lands:
        landed.append({"timestamp": 26_000_000, "landed": True})
    return {
        "vehicle_local_position": position,
        "vehicle_land_detected": landed,
        "actuator_motors": [{"timestamp": 1_000_000, "control": [0.5, 0.5, 0.5, 0.5]}],
        "vehicle_status": [{"timestamp": 1_000_000}],
    }


@pytest.mark.parametrize(
    "genuine,lands,expected_snippet",
    [
        (True, True, None),
        (False, True, "constant"),
        (True, False, "never reports landed"),
    ],
)
def test_x500_evidence_reason_row_level(monkeypatch, genuine, lands, expected_snippet):
    monkeypatch.setattr(
        jobs, "read_ulog_rows", lambda raw, topics: _x500_rows(genuine=genuine, lands=lands)
    )
    reason = jobs._x500_evidence_reason(X500_CRITERIA, ulog_bytes=b"unused")
    if expected_snippet is None:
        assert reason is None
    else:
        assert reason is not None and expected_snippet in reason


def test_x500_evidence_reason_refuses_a_missing_decision_topic(monkeypatch):
    def _raise(raw, topics):
        raise ValueError("ULog is missing required topic(s): vehicle_status")

    monkeypatch.setattr(jobs, "read_ulog_rows", _raise)
    reason = jobs._x500_evidence_reason(X500_CRITERIA, ulog_bytes=b"unused")
    assert reason is not None and "missing required topic" in reason


def _state_frame(t: int, position: dict, velocity: float = 0.0, effort: float = 5.0) -> str:
    timestamp = f"2026-09-29T00:00:{t:02d}Z"
    lines = [f"header: {{stamp: {{sec: {t}, nanosec: 0}}, frame_id: ''}}", "name:"]
    lines += [f"- {j}" for j in JOINTS]
    lines += ["position:"] + [f"- {position[j]}" for j in JOINTS]
    lines += ["velocity:"] + [f"- {velocity}" for _ in JOINTS]
    lines += ["effort:"] + [f"- {effort}" for _ in JOINTS]
    lines.append("---")
    return "".join(f"{timestamp} {line}\n" for line in lines)


def _tracking_frame(t: int, error: float) -> str:
    timestamp = f"2026-09-29T00:00:{t:02d}Z"
    lines = (
        [
            f"header: {{stamp: {{sec: {t}, nanosec: 0}}, frame_id: ''}}",
            "joint_names: [" + ", ".join(JOINTS) + "]",
            "reference:",
            "feedback:",
            "error:",
        ]
        + [f"- {error}" for _ in JOINTS]
        + ["---"]
    )
    # reference/feedback are unread by the judge; kept empty-bodied here on purpose.
    return "".join(f"{timestamp} {line}\n" for line in lines)


def _squat_state(genuine: bool) -> str:
    """8s of two squat cycles (target 0.15 rad on every HFE/KFE joint) plus a
    2s return-to-nominal tail, or (genuine=False) a flat, unmoving trace."""
    frames = []
    for t in range(11):
        if not genuine:
            frames.append(_state_frame(t, NOMINAL))
            continue
        magnitude = 0.15 * abs(math.sin(math.pi * t / 4)) if t <= 8 else 0.0
        position = {
            joint: value + (magnitude if value >= 0 else -magnitude)
            if joint.endswith(("HFE", "KFE"))
            else value
            for joint, value in NOMINAL.items()
        }
        frames.append(_state_frame(t, position))
    return "".join(frames)


def _squat_tracking(genuine: bool) -> str:
    if not genuine:
        return "".join(_tracking_frame(t, 0.0) for t in range(11))
    return "".join(_tracking_frame(t, 0.01 * math.sin(t)) for t in range(11))


def _disabled_state(falling: bool) -> str:
    """A gravity-released trace (legs drift from nominal) or (falling=False)
    a held, unmoving one."""
    frames = []
    for t in range(6):
        if not falling:
            frames.append(_state_frame(t, NOMINAL))
            continue
        position = {
            joint: value + (0.03 * t if joint.endswith(("HFE", "KFE")) else 0.0)
            for joint, value in NOMINAL.items()
        }
        frames.append(_state_frame(t, position, velocity=0.03))
    return "".join(frames)


def test_good_squat_evidence_is_go(monkeypatch):
    reason = jobs._anymal_evidence_reason(
        "anymal_d_effort_squat",
        state=_squat_state(True),
        tracking=_squat_tracking(True),
        controllers="",
        criteria=SQUAT_CRITERIA,
    )
    assert reason is None


def test_constant_squat_trace_is_refused_as_not_genuine():
    reason = jobs._anymal_evidence_reason(
        "anymal_d_effort_squat",
        state=_squat_state(False),
        tracking=_squat_tracking(True),
        controllers="",
        criteria=SQUAT_CRITERIA,
    )
    assert reason is not None and "peak displacement" in reason


def test_zero_tracking_error_is_refused():
    reason = jobs._anymal_evidence_reason(
        "anymal_d_effort_squat",
        state=_squat_state(True),
        tracking=_squat_tracking(False),
        controllers="",
        criteria=SQUAT_CRITERIA,
    )
    assert reason is not None and "tracking error is not genuine" in reason


def test_disabled_with_holding_is_refused():
    controllers = "anymal_effort_controller  ros2_control_components  active  \n"
    reason = jobs._anymal_evidence_reason(
        "anymal_d_controller_disabled_negative",
        state=_disabled_state(True),
        tracking="",
        controllers=controllers,
        criteria=DISABLED_CRITERIA,
    )
    assert reason is not None and "not shown inactive" in reason
    controllers_inactive = "anymal_effort_controller  ros2_control_components  inactive  \n"
    still_held = jobs._anymal_evidence_reason(
        "anymal_d_controller_disabled_negative",
        state=_disabled_state(False),
        tracking="",
        controllers=controllers_inactive,
        criteria=DISABLED_CRITERIA,
    )
    assert still_held is not None and "did not move" in still_held


def test_disabled_command_echo_and_coloured_row_do_not_satisfy_the_judge():
    # A load/set-state echo names the controller and ends in a state word too
    # (scenarios.json forbids command_echo as proof); a real list_controllers
    # row can be ANSI-coloured. Only the coloured row, correctly parsed, may
    # count -- and it names the controller first, unlike the echo sentence.
    controllers = (
        "Successfully switched off controller anymal_effort_controller: inactive\n"
        "anymal_effort_controller[joint_trajectory_controller/JointTrajectoryController] \x1b[32mactive\x1b[0m\n"
        "anymal_effort_controller[joint_trajectory_controller/JointTrajectoryController] \x1b[31minactive\x1b[0m\n"
    )
    reason = jobs._anymal_evidence_reason(
        "anymal_d_controller_disabled_negative",
        state=_disabled_state(True),
        tracking="",
        controllers=controllers,
        criteria=DISABLED_CRITERIA,
    )
    assert reason is None


RUN_ID = "38cb5cc3-868b-4f24-8b39-583cc2e1b678"
LOCAL_IMAGE_ID = "sha256:" + "c" * 64


class FinishedProcess:
    def __init__(self, exit_code=0):
        self.returncode = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def sim_jazzy_profile(tmp_path):
    """The exact manifest shape .github/workflows/ci.yml's sim-jazzy job
    writes: a JSON list for ``commands`` (not a tuple), the ``docker image
    inspect --format '{{.Id}}'`` digest form, and the scenarios.json sha256."""
    scenarios_sha = hashlib.sha256(
        (ROOT / "fixtures" / "e5_10_real_scenarios" / "scenarios.json").read_bytes()
    ).hexdigest()
    manifest = {
        "manifest_version": "1",
        "resolution_status": "admitted",
        "profiles": {
            "e5_10_sim_jazzy": {
                "image": LOCAL_IMAGE_ID,
                "architecture": "linux/amd64",
                "entrypoint": "/opt/lockwire/run-e5-10-admission.sh",
                "commands": [
                    "anymal_d_effort_squat",
                    "anymal_d_controller_disabled_negative",
                    "px4_x500_takeoff_hover_land",
                ],
                "baked_inputs": {"scenarios": scenarios_sha},
            }
        },
    }
    path = tmp_path / "sim-jazzy-profile.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, scenarios_sha


@pytest.mark.parametrize(
    "scenario,expected_verdict,genuine",
    [
        ("anymal_d_effort_squat", "GO", True),
        ("anymal_d_controller_disabled_negative", "NO-GO", True),
    ],
)
def test_start_motion_job_admits_real_manifest_shape(
    tmp_path, monkeypatch, sim_jazzy_profile, scenario, expected_verdict, genuine
):
    """Exercises jobs.start_motion_job end to end against the manifest CI
    actually writes (list ``commands``, not a tuple) -- this is the coverage
    gap that let a strict-tuple field ship broken."""
    profile_path, scenarios_sha = sim_jazzy_profile
    monkeypatch.setattr(jobs.MotionJob, "_owned_container_ids", lambda self: [])

    def start(argv, **kwargs):
        path = Path(argv[argv.index("--mount") + 1].split("src=", 1)[1].split(",dst=", 1)[0])
        (path / "scenarios.json").write_bytes(
            (ROOT / "fixtures" / "e5_10_real_scenarios" / "scenarios.json").read_bytes()
        )
        if scenario == "anymal_d_effort_squat":
            state, tracking, controllers = _squat_state(genuine), _squat_tracking(genuine), ""
        else:
            state = _disabled_state(genuine)
            tracking = ""
            controllers = "anymal_effort_controller[joint_trajectory_controller/JointTrajectoryController] inactive\n"
        (path / "joint_states.raw").write_text(state, encoding="utf-8")
        (path / "tracking.raw").write_text(tracking, encoding="utf-8")
        (path / "controllers.raw").write_text(controllers, encoding="utf-8")
        document = {
            "label": "e5_10_sim_jazzy_admission",
            "scenario": scenario,
            "exit_code": 0,
            "evidence": {
                "joint_state": "joint_states.raw",
                "tracking": "tracking.raw",
                "controllers": "controllers.raw",
            },
            "verdict": "NO-GO",
            "fixture_admission": "UNVERIFIED",
            "baked_inputs": {"scenarios": scenarios_sha},
        }
        (path / "document.json").write_text(json.dumps(document), encoding="utf-8")
        return FinishedProcess()

    monkeypatch.setattr(jobs.subprocess, "Popen", start)
    document = jobs.start_motion_job(
        RUN_ID, image=LOCAL_IMAGE_ID, command=(scenario,), profile=profile_path
    ).result()
    assert document["fixture_admission"] == "PASSED", document.get("admission_reason")
    assert document["verdict"] == expected_verdict


EVIDENCE_ROOT = Path("/tmp/lockwire-sim-jazzy-evidence")


@pytest.mark.skipif(
    os.environ.get("LOCKWIRE_SIM") != "1",
    reason="sim-jazzy admission runs only in CI (LOCKWIRE_SIM=1); skipped locally",
)
@pytest.mark.parametrize(
    "scenario,expected_verdict,run_suffix,timeout",
    [
        ("anymal_d_effort_squat", "GO", "678", 600),
        ("anymal_d_controller_disabled_negative", "NO-GO", "679", 600),
        ("px4_x500_takeoff_hover_land", "GO", "680", 900),
    ],
)
def test_start_motion_job_launches_and_judges_sim(scenario, expected_verdict, run_suffix, timeout):
    image = os.environ["LOCKWIRE_SIM_IMAGE"]
    profile = Path(os.environ["LOCKWIRE_SIM_PROFILE"])
    # start_motion_job's evidence_dir must not exist yet, but its *parent* must
    # (it's resolved with resolve(strict=True) before the fresh-directory
    # check) -- don't rely solely on the CI step's own mkdir (run 36521132724
    # died here when nothing had created it).
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    evidence_dir = EVIDENCE_ROOT / scenario
    job = jobs.start_motion_job(
        f"38cb5cc3-868b-4f24-8b39-583cc2e1b{run_suffix}",
        command=(scenario,),
        image=image,
        profile=profile,
        evidence_dir=evidence_dir,
    )
    document = job.result(timeout=timeout)
    assert document["fixture_admission"] == "PASSED", document.get("admission_reason")
    assert document["exit_code"] == 0
    assert document["verdict"] == expected_verdict
