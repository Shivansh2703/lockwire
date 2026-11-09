"""Run ``preflight`` or ``scan`` as a job that can be stopped.

The CLI's ``preflight`` and ``scan`` run to completion in the calling process:
a long run (v8 measured one preflight over a 741,975-row recording) cannot be
interrupted, which is the one thing an app driving them needs. This module adds
the missing half and **nothing else** — the work itself is the same CLI
invocation, started as a child process, so the document and the exit code are
the ones ``lockwire preflight`` produces today rather than a second
implementation of the pipeline.

A child process, not a thread, because a Python thread cannot be stopped: a
``cancel()`` that sets a flag and lets the work finish is the failure this box
exists to avoid — the caller believes the run stopped and it has not.

**Never a shell line.** Every invocation is the argv list :func:`build_argv`
returns, handed straight to ``subprocess.Popen``; no ``shell=True``, no
``os.system``, no string ever concatenated into a command. E0-01 fixed one place
where a user-supplied ``--ros-distro`` reached ``bash -lc``; this module is
written so the same class of bug has nowhere to live, and
``test_no_command_is_ever_built_from_text`` greps it.

**Cancelling is not actuating**: lockwire never moves hardware. Stopping its own
scoring stops lockwire; nothing is ever sent to a device — the same is true of stopping its
own container-side recorder (:func:`kill_container_process`, E3-QC). The live
capture path (``preflight --live``) is reachable through this same job: the
child CLI process installs a SIGTERM/SIGINT handler for exactly the ``--live``
branch (``cli.py``) that calls ``LiveSource.cancel()`` — which uses
:func:`kill_container_process` to prove the container-side recorder is gone —
before letting the signal finish the process. ``Job.cancel`` signals
the live CLI leader first, leaving its SSH/docker client available for that
handler's corpse proof and bag fetch. If cleanup does not finish within 20 s,
it kills the owned process group; after a normal exit it also removes any
remaining group members. See :func:`start_job` for the one ceiling that remains.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from lockwire_pro.analysis import AnalysisRefused, _reject_duplicate_json_keys, build_analysis_argv
from lockwire_pro.health import sidecar_path_for
from lockwire.scan import CommandRunner
from lockwire_pro.ulog import read_ulog_rows

__all__ = [
    "GRACE_SECONDS",
    "KINDS",
    "Job",
    "JobCancelled",
    "build_argv",
    "build_motion_argv",
    "kill_container_process",
    "motion_evidence_is_genuine",
    "start_analysis_job",
    "start_job",
    "start_motion_job",
]

#: The commands that can be run as a job. Both are long: ``preflight`` scores a
#: whole recording, ``scan`` walks the machine. Everything else in the CLI
#: returns immediately and needs no handle.
KINDS = ("preflight", "scan")

#: Seconds a cancelled child gets to close its files after ``SIGTERM`` before
#: ``SIGKILL``. TERM first, so a run that is mid-write to the health ledger can
#: finish the line it started; KILL after, so a child that ignores TERM cannot
#: outlive the job that reports it stopped. TERM alone hangs; KILL alone
#: truncates — this is both, in that order.
GRACE_SECONDS = 2.0

#: Wall-clock seconds a job may run before it is stopped, so a child nobody is
#: watching cannot keep appending to the ledger forever. Callers can pass their own.
# ponytail: one flat ceiling (4 h), no heartbeat; add a heartbeat if runs must exceed it.
JOB_TIMEOUT_SECONDS = 4 * 60 * 60.0

MOTION_LABEL = "lockwire.synthetic_runtime_admission"
MOTION_IMAGE = "lockwire-motion@sha256:" + "0" * 64
_MOTION_DIGEST = r"[0-9a-f]{64}"
_MOTION_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_MOTION_PROFILE_PATH = Path(__file__).with_name("motion-admission.json")
_MOTION_RUNNER = "/opt/lockwire/run-admission.sh"
_MOTION_INPUTS = {
    "runner": _MOTION_RUNNER,
    "packages": "/opt/lockwire/packages.lock",
    "robot": "/workspace/robot.urdf.xacro",
    "world": "/workspace/world.sdf",
    "controllers": "/workspace/controllers.yaml",
}
_MOTION_FILES = (
    "joint_states.raw",
    "effort.raw",
    "tracking.raw",
    "gz.raw",
    "spawn.raw",
    "controllers.raw",
    "command.raw",
    "output.txt",
    "errors.txt",
    "document.json",
    "scenarios.json",
    "x500.ulg",
)
_MOTION_MAX_BYTES = 4 * 1024 * 1024
#: The fixed leg-joint order every ANYmal evidence array is checked against.
_LEG_JOINTS = tuple(
    f"{leg}_{joint}" for leg in ("LF", "RF", "LH", "RH") for joint in ("HAA", "HFE", "KFE")
)


class _MotionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    engine: Literal["gazebo_harmonic"]
    ros: Literal["jazzy"]
    architecture: Literal["linux/arm64"]
    control_msgs_version: Literal["5.9.0-1noble.20260902.141921"]
    controller_version: Literal["4.42.1-1noble.20260904.012550"]
    message_shape: Literal["reference-feedback-error"]
    baked_inputs: dict[str, str]
    # These are the existing hash-bound controllers.yaml and trajectory command,
    # not general-purpose sim-to-real thresholds.
    trajectory_tolerance: Literal[0.10]
    goal_tolerance: Literal[0.05]
    goal_position: Literal[0.8]
    entrypoint: str = "/opt/lockwire/run-admission.sh"
    commands: list[Literal["enabled", "disabled"]] = ["enabled", "disabled"]


class _SimJazzyProfile(BaseModel):
    """The amd64 ANYmal sim profile CI records."""

    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    architecture: Literal["linux/amd64"]
    entrypoint: str
    commands: list[
        Literal[
            "anymal_d_effort_squat",
            "anymal_d_controller_disabled_negative",
            "px4_x500_takeoff_hover_land",
        ]
    ]
    baked_inputs: dict[str, str]


class _MotionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest_version: Literal["1"]
    resolution_status: Literal["unadmitted", "admitted"]
    profiles: dict[str, _MotionProfile | _SimJazzyProfile]


def _motion_profile(
    image: str, *, profile_path: Path | None = None
) -> _MotionProfile | _SimJazzyProfile:
    path = profile_path if profile_path is not None else _MOTION_PROFILE_PATH
    try:
        raw = _motion_read(path, max_bytes=256 * 1024)
        manifest = _MotionManifest.model_validate(
            json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        )
        if manifest.resolution_status != "admitted":
            raise ValueError(
                "motion image/profile is unadmitted; fresh image evidence must be recorded"
            )
        matches = [profile for profile in manifest.profiles.values() if profile.image == image]
        if not matches:
            raise ValueError("motion image differs from the recorded immutable profile")
        profile = matches[0]
        expected_inputs = (
            _MOTION_INPUTS if isinstance(profile, _MotionProfile) else {"scenarios": ""}
        )
        if set(profile.baked_inputs) != set(expected_inputs) or any(
            not re.fullmatch(_MOTION_DIGEST, value) for value in profile.baked_inputs.values()
        ):
            raise ValueError("motion profile has incomplete baked input identities")
        return profile
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"invalid trusted motion profile: {error}") from error


def _motion_read(path: Path, *, max_bytes: int = _MOTION_MAX_BYTES) -> bytes:
    # Unlike resolve(), O_NOFOLLOW does not admit even a contained symlink.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"motion artifact {path.name!r} is not a regular file")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"motion artifact {path.name!r} exceeds its size limit")
    return data


class JobCancelled(RuntimeError):
    """Raised by ``Job.result`` for a cancelled job — there is no document, on purpose."""


def _motion_identity(run_id: str) -> tuple[str, str]:
    """Return the one container name and label value owned by a motion run."""
    try:
        canonical = str(uuid.UUID(run_id))
    except (AttributeError, ValueError) as error:
        raise ValueError("motion run_id must be a UUID") from error
    return f"lockwire-motion-{canonical}", canonical


def _motion_frames(raw: str, *, max_lines: int = 256) -> list[tuple[datetime, dict[str, Any]]]:
    """Consume every complete stamped ROS YAML frame; never harvest substrings."""
    yaml = YAML(typ="safe", pure=True)
    frames = []
    lines: list[str] = []
    stamp = None
    for line in raw.splitlines():
        match = re.fullmatch(rf"({_MOTION_TIMESTAMP}) (.*)", line)
        if match is None:
            raise ValueError("motion feedback has an unstamped or malformed row")
        current = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if stamp is not None and current != stamp:
            raise ValueError("motion frame header/value UTC timestamps differ")
        stamp = current
        if match[2] == "---":
            if not lines:
                raise ValueError("motion feedback has an empty frame")
            document = yaml.load("\n".join(lines))
            if not isinstance(document, dict):
                raise ValueError("motion feedback frame is not a mapping")
            if frames and current < frames[-1][0]:
                raise ValueError("motion feedback UTC time decreases")
            frames.append((current, document))
            lines, stamp = [], None
        else:
            lines.append(match[2])
            if len(lines) > max_lines:
                raise ValueError("motion feedback frame is too large")
    if lines or not frames:
        raise ValueError("motion feedback has missing or incomplete frames")
    return frames


def _motion_scalar(document: dict[str, Any], field: str) -> float:
    values = document.get(field)
    if not isinstance(values, list) or len(values) != 1 or type(values[0]) not in (int, float):
        raise ValueError(f"motion feedback {field!r} must contain one numeric scalar")
    value = float(values[0])
    if not math.isfinite(value):
        raise ValueError(f"motion feedback {field!r} is nonfinite")
    return value


def _motion_duration(document: Any) -> tuple[int, int]:
    if not isinstance(document, dict) or set(document) != {"sec", "nanosec"}:
        raise ValueError("motion ROS time shape is unknown")
    sec, nanosec = document["sec"], document["nanosec"]
    if type(sec) is not int or type(nanosec) is not int or not 0 <= nanosec < 1_000_000_000:
        raise ValueError("motion ROS time is malformed")
    return sec, nanosec


def _motion_header(document: dict[str, Any]) -> tuple[int, int]:
    header = document.get("header")
    if (
        not isinstance(header, dict)
        or set(header) != {"stamp", "frame_id"}
        or not isinstance(header["frame_id"], str)
    ):
        raise ValueError("motion ROS header is missing or malformed")
    stamp = _motion_duration(header["stamp"])
    if stamp[0] < 0:
        raise ValueError("motion ROS header stamp is negative")
    return stamp


def _motion_point(document: Any) -> None:
    if not isinstance(document, dict) or set(document) != {
        "positions",
        "velocities",
        "accelerations",
        "effort",
        "time_from_start",
    }:
        raise ValueError("motion trajectory-point shape is unknown")
    _motion_duration(document["time_from_start"])
    for field in ("positions", "velocities", "accelerations", "effort"):
        values = document[field]
        if not isinstance(values, list) or len(values) > 1:
            raise ValueError("motion point arrays are not empty or single-joint")
        if values:
            _motion_scalar(document, field)


def _motion_evidence_reason(
    mode: str,
    *,
    state: str,
    effort: str,
    tracking: str,
    trajectory_tolerance: float = 0.10,
    goal_tolerance: float = 0.05,
    goal_position: float = 0.8,
) -> str | None:
    try:
        states, efforts = _motion_frames(state), _motion_frames(effort)
        positions = [_motion_scalar(frame, "position") for _, frame in states]
        state_stamps = []
        for _, frame in states:
            if set(frame) != {"header", "name", "position", "velocity", "effort"} or frame.get(
                "name"
            ) != ["pendulum_joint"]:
                raise ValueError("motion joint-state shape/identity is unknown")
            state_stamps.append(_motion_header(frame))
            _motion_scalar(frame, "velocity")
            _motion_scalar(frame, "effort")
        if len(set(state_stamps)) < 2 or state_stamps != sorted(state_stamps):
            raise ValueError("motion joint-state ROS header time has no monotonic evolution")
        if any(set(frame) != {"effort"} for _, frame in efforts):
            raise ValueError("motion extracted effort shape is unknown")
        effort_values = [_motion_scalar(frame, "effort") for _, frame in efforts]
        if len({stamp for stamp, _ in states}) < 2 or len(set(positions)) < 2:
            raise ValueError("motion feedback has no temporal physical evolution")
        if len({stamp for stamp, _ in efforts}) < 2:
            raise ValueError("motion effort has no temporal feedback")
        if mode == "disabled":
            if (
                tracking.strip()
                or any(effort_values)
                or any(_motion_scalar(frame, "effort") for _, frame in states)
            ):
                raise ValueError("disabled motion has measured actuation or tracking")
            return None
        if mode != "enabled":
            raise ValueError("motion mode is unknown")
        if not any(effort_values) or not any(
            _motion_scalar(frame, "effort") for _, frame in states
        ):
            raise ValueError("enabled motion has no nonzero measured effort")
        tracks = _motion_frames(tracking)
        actual, desired, errors, track_stamps = [], [], [], []
        for _, frame in tracks:
            if set(frame) != {
                "header",
                "joint_names",
                "reference",
                "feedback",
                "error",
                "output",
                "multi_dof_joint_names",
                "multi_dof_reference",
                "multi_dof_feedback",
                "multi_dof_error",
                "multi_dof_output",
                "speed_scaling_factor",
            } or frame.get("joint_names") != ["pendulum_joint"]:
                raise ValueError(
                    "motion controller-state shape is unknown; expected Jazzy reference/feedback/error"
                )
            track_stamps.append(_motion_header(frame))
            if frame["multi_dof_joint_names"] != []:
                raise ValueError("motion multi-DOF joints are unsupported")
            for field in (
                "multi_dof_reference",
                "multi_dof_feedback",
                "multi_dof_error",
                "multi_dof_output",
            ):
                point = frame[field]
                if (
                    not isinstance(point, dict)
                    or set(point)
                    != {"transforms", "velocities", "accelerations", "time_from_start"}
                    or any(
                        point[key] != [] for key in ("transforms", "velocities", "accelerations")
                    )
                ):
                    raise ValueError("motion multi-DOF point shape is unsupported")
                _motion_duration(point["time_from_start"])
            scaling = frame["speed_scaling_factor"]
            if type(scaling) not in (int, float) or not math.isfinite(scaling):
                raise ValueError("motion speed scaling is nonfinite or malformed")
            for field in ("feedback", "reference", "error", "output"):
                _motion_point(frame[field])
            actual.append(_motion_scalar(frame["feedback"], "positions"))
            desired.append(_motion_scalar(frame["reference"], "positions"))
            errors.append(_motion_scalar(frame["error"], "positions"))
            for field in ("feedback", "reference", "error"):
                _motion_scalar(frame[field], "velocities")
            if not math.isclose(desired[-1] - actual[-1], errors[-1], abs_tol=1e-6):
                raise ValueError(
                    "motion controller error disagrees with measured reference/feedback"
                )
        if len(set(track_stamps)) < 2 or track_stamps != sorted(track_stamps):
            raise ValueError("motion controller ROS header time has no monotonic evolution")
        if len({stamp for stamp, _ in tracks}) < 2 or len(set(actual)) < 2 or len(set(desired)) < 2:
            raise ValueError("motion controller has no temporal measured/reference evolution")
        if any(abs(value) > trajectory_tolerance for value in errors):
            raise ValueError("motion tracking exceeds the baked trajectory tolerance")
        if (
            abs(actual[-1] - goal_position) > goal_tolerance
            or abs(desired[-1] - goal_position) > goal_tolerance
        ):
            raise ValueError("motion final tracking misses the baked goal tolerance")
        return None
    except (ValueError, TypeError, OverflowError, RecursionError, YAMLError) as error:
        return str(error)


def motion_evidence_is_genuine(
    mode: str,
    *,
    state: str,
    effort: str,
    tracking: str,
    trajectory_tolerance: float = 0.10,
    goal_tolerance: float = 0.05,
    goal_position: float = 0.8,
) -> bool:
    """The production gate for this one hash-bound synthetic fixture only."""
    return (
        _motion_evidence_reason(
            mode,
            state=state,
            effort=effort,
            tracking=tracking,
            trajectory_tolerance=trajectory_tolerance,
            goal_tolerance=goal_tolerance,
            goal_position=goal_position,
        )
        is None
    )


def _motion_named_frame(
    frame: dict[str, Any], *, fields: tuple[str, ...] = ("position", "velocity", "effort")
) -> dict[str, dict[str, float]]:
    """Read a 12-joint ANYmal ``/joint_states`` frame by joint name.

    ``joint_state_broadcaster`` does not promise the overlay's joint order
    (a long-standing ros2_control complaint), so this checks set equality
    against the fixed leg-joint names, not positional order.
    """
    names = frame.get("name")
    if not isinstance(names, list) or sorted(names) != sorted(_LEG_JOINTS):
        raise ValueError("motion ANYmal joint-state names do not match the fixed leg-joint set")
    out: dict[str, dict[str, float]] = {}
    for field in fields:
        values = frame.get(field)
        if not isinstance(values, list) or len(values) != len(names):
            raise ValueError(f"motion ANYmal joint-state {field!r} array length mismatch")
        scalars = []
        for value in values:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"motion ANYmal joint-state {field!r} has a nonfinite value")
            scalars.append(float(value))
        out[field] = dict(zip(names, scalars))
    return out


def _anymal_evidence_reason(
    scenario: str, *, state: str, tracking: str, controllers: str, criteria: dict[str, Any]
) -> str | None:
    """The ANYmal judge: squat GO, controller-off NO-GO."""
    try:
        frames = _motion_frames(state, max_lines=512)
        stamps, parsed = [], []
        for _, frame in frames:
            if set(frame) != {"header", "name", "position", "velocity", "effort"}:
                raise ValueError("motion ANYmal joint-state shape is unknown")
            stamps.append(_motion_header(frame))
            parsed.append(_motion_named_frame(frame))
        if len(set(stamps)) < 2 or stamps != sorted(stamps):
            raise ValueError("motion ANYmal joint-state time has no monotonic evolution")

        if scenario == "anymal_d_controller_disabled_negative":
            # ros2controlcli colorizes the state column without checking isatty
            # (AGENTS.md's "suspect colour" trap); a load/set-state command echo
            # also names the controller and ends in a state word, which
            # scenarios.json's command_echo_may_not_satisfy forbids as proof --
            # only a genuine `list_controllers` row (its first token names the
            # controller, unlike a "Successfully ..." echo) counts, and only
            # its last occurrence (state can change mid-log).
            plain = re.sub(r"\x1b\[[0-9;]*m", "", controllers)
            rows = [
                tokens
                for line in plain.splitlines()
                if (tokens := line.split())
                and (
                    tokens[0] == "anymal_effort_controller"
                    or tokens[0].startswith("anymal_effort_controller[")
                )
            ]
            if not rows or rows[-1][-1] != "inactive":
                raise ValueError("motion ANYmal controller is not shown inactive")
            threshold = criteria["minimum_actual_position_change_rad"]
            speed_threshold = criteria["or_minimum_actual_speed_rad_s"]
            moved = any(
                max(f["position"][joint] for f in parsed)
                - min(f["position"][joint] for f in parsed)
                >= threshold
                or max(abs(f["velocity"][joint]) for f in parsed) >= speed_threshold
                for joint in _LEG_JOINTS
            )
            if not moved:
                raise ValueError("motion ANYmal joints did not move under released gravity")
            return None
        if scenario != "anymal_d_effort_squat":
            raise ValueError("motion ANYmal scenario is unknown")

        nominal = criteria["nominal_rad"]
        effort_limit = criteria["absolute_effort_limit_nm"]
        if any(
            abs(frame["effort"][joint]) > effort_limit for frame in parsed for joint in _LEG_JOINTS
        ):
            raise ValueError("motion ANYmal effort exceeds the baked limit")
        for group, key in (
            ("HFE", "hfe_peak_displacement_rad"),
            ("KFE", "kfe_peak_displacement_rad"),
        ):
            target, tolerance = criteria[key]["target"], criteria[key]["tolerance"]
            joints = [j for j in _LEG_JOINTS if j.endswith(group)]
            peak, excursions, above = 0.0, 0, False
            for frame in parsed:
                displacement = max(abs(frame["position"][j] - nominal[j]) for j in joints)
                peak = max(peak, displacement)
                if displacement >= target / 2:
                    excursions += not above
                    above = True
                else:
                    above = False
            if abs(peak - target) > tolerance:
                raise ValueError(f"motion ANYmal {group} peak displacement misses the baked target")
            if excursions != criteria["cycles_required"]:
                raise ValueError(
                    f"motion ANYmal {group} excursion count does not match the baked cycle count"
                )
        tail = parsed[-3:] if len(parsed) >= 3 else parsed
        for joint in _LEG_JOINTS:
            error = abs(sum(f["position"][joint] for f in tail) / len(tail) - nominal[joint])
            if error > criteria["return_position_error_rad"]["maximum"]:
                raise ValueError("motion ANYmal return position misses its baked tolerance")
            if (
                max(abs(f["velocity"][joint]) for f in tail)
                > criteria["return_speed_rad_s"]["maximum"]
            ):
                raise ValueError("motion ANYmal return speed misses its baked tolerance")

        track_frames = _motion_frames(tracking, max_lines=512)
        errors = []
        for _, frame in track_frames:
            if set(frame) != {"header", "joint_names", "reference", "feedback", "error"} or sorted(
                frame.get("joint_names") or []
            ) != sorted(_LEG_JOINTS):
                raise ValueError(
                    "motion ANYmal tracking shape is unknown; expected reference/feedback/error"
                )
            values = frame["error"]
            if (
                not isinstance(values, list)
                or len(values) != len(_LEG_JOINTS)
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in values
                )
            ):
                raise ValueError("motion ANYmal tracking error array is malformed or nonfinite")
            errors.append(tuple(float(value) for value in values))
        if not errors or not any(any(row) for row in errors) or len(set(errors)) < 2:
            raise ValueError("motion ANYmal tracking error is not genuine (zero or unchanging)")
        return None
    except (
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        YAMLError,
        KeyError,
        ZeroDivisionError,
    ) as error:
        return str(error)


_X500_DECISION_TOPICS = (
    "vehicle_local_position",
    "vehicle_land_detected",
    "actuator_motors",
    "vehicle_status",
)


def _x500_evidence_reason(criteria: dict[str, Any], *, ulog_bytes: bytes) -> str | None:
    """The X500 judge: PX4's own .ulg, read through ulog.read_ulog_rows.

    Applies scenarios.json's px4_x500_takeoff_hover_land acceptance as written:
    reach the takeoff altitude within tolerance, then hold a hover window
    within tolerance/speed, then land (vehicle_land_detected) at a low
    altitude, all before the baked deadline. Altitude is -z in NED.
    """
    try:
        rows = read_ulog_rows(ulog_bytes, _X500_DECISION_TOPICS)
        position = sorted(rows["vehicle_local_position"], key=lambda row: row["timestamp"])
        altitudes = [-float(row["z"]) for row in position]
        if len(set(altitudes)) < 2:
            raise ValueError("motion X500 altitude trace is constant; not genuine")
        start = position[0]["timestamp"]
        takeoff, hover, landed_max = (
            criteria["takeoff_altitude_m"],
            criteria["hover_duration_s"],
            criteria["landed_altitude_m"],
        )
        if not any(abs(alt - takeoff["target"]) <= takeoff["tolerance"] for alt in altitudes):
            raise ValueError("motion X500 never reached the baked takeoff altitude")

        window_start, held = None, 0.0
        for row in position:
            alt = -float(row["z"])
            speed = math.hypot(float(row.get("vx", 0.0)), float(row.get("vy", 0.0)))
            steady = (
                abs(alt - takeoff["target"]) <= hover["vertical_tolerance_m"]
                and speed <= hover["max_horizontal_speed_m_s"]
            )
            if steady:
                window_start = row["timestamp"] if window_start is None else window_start
                held = max(held, (row["timestamp"] - window_start) / 1e6)
            else:
                window_start = None
        if held < hover["minimum"]:
            raise ValueError("motion X500 hover window misses the baked duration/tolerance/speed")

        # A grounded vehicle reports landed=True before it ever lifts off, so
        # the judged "landed" transition must be the first True *after* the
        # first recorded liftoff (landed=False) -- otherwise a flight that
        # never lands would still pass by reading its own starting state.
        landed_rows = sorted(rows["vehicle_land_detected"], key=lambda row: row["timestamp"])
        liftoff_at = next((row["timestamp"] for row in landed_rows if not row.get("landed")), None)
        if liftoff_at is None:
            raise ValueError("motion X500 vehicle_land_detected never reports airborne")
        landed_at = next(
            (
                row["timestamp"]
                for row in landed_rows
                if row["timestamp"] > liftoff_at and row.get("landed")
            ),
            None,
        )
        if landed_at is None:
            raise ValueError("motion X500 vehicle_land_detected never reports landed")
        if (landed_at - start) / 1e6 > criteria["deadline_s"]:
            raise ValueError("motion X500 landing exceeded the baked deadline")
        at_landing = [-float(row["z"]) for row in position if row["timestamp"] <= landed_at]
        if not at_landing or at_landing[-1] > landed_max["maximum"]:
            raise ValueError("motion X500 landed altitude exceeds the baked maximum")
        if not rows["actuator_motors"] or not rows["vehicle_status"]:
            raise ValueError("motion X500 actuator/status evidence is missing")
        return None
    except (ValueError, TypeError, KeyError) as error:
        return str(error)


def build_motion_argv(
    run_id: str,
    *,
    output_dir: Path,
    source_mounts: Mapping[Path | str, str] | None = None,
    command: Sequence[str] = (),
    plugins: Sequence[str] = (),
    image: str = MOTION_IMAGE,
    entrypoint: str | None = None,
    profile: Path | None = None,
) -> list[str]:
    """Build the fixed, isolated Docker argv for synthetic runtime admission.

    Caller values may only select the recorded profile's fixed supported mode.
    ``profile`` names the trusted-profile manifest to validate against;
    CI records the image digest it just built there. ``None`` uses the
    checked-in path, which stays unadmitted.
    """
    name, canonical_id = _motion_identity(run_id)
    if source_mounts:
        raise ValueError(
            "motion source mounts are refused; admission assets are baked into the image"
        )
    matched = _motion_profile(image, profile_path=profile)
    resolved_entrypoint = entrypoint if entrypoint is not None else matched.entrypoint
    if (
        resolved_entrypoint != matched.entrypoint
        or plugins
        or tuple(command) not in {(c,) for c in matched.commands}
    ):
        raise ValueError(
            "motion only supports the baked runner, plugins and enabled/disabled fixture argv"
        )
    output_text = str(output_dir)
    if not output_dir.is_absolute() or "," in output_text:
        raise ValueError("motion output must be an absolute, comma-free path")
    argv = [
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        name,
        "--label",
        f"{MOTION_LABEL}=true",
        "--label",
        f"lockwire.motion.run_id={canonical_id}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop=ALL",
        # run 36525875812 (measured): "cp: cannot stat '/output/scenarios.json':
        # Permission denied" -- /output is a bind mount of tempfile.mkdtemp()'s
        # host directory (mode 0700, owned by the CI runner's uid), and
        # --cap-drop=ALL strips DAC_OVERRIDE/DAC_READ_SEARCH from the
        # container's root too, so root loses the usual free pass through a
        # directory it does not own. Add back only DAC_OVERRIDE (not the
        # other 36 capabilities) so the container can read/write its own
        # bind-mounted output directory; --network none/--read-only/the rest
        # of --cap-drop=ALL are unchanged.
        "--cap-add=DAC_OVERRIDE",
        "--security-opt=no-new-privileges",
        # 1024, not the earlier 512: the ANYmal profile alone stayed under 512,
        # but PX4 SITL + gz sim + ROS 2 together (E5.10 X500 cycle 2) start
        # noticeably more threads/processes; raised for both profiles rather
        # than forking build_motion_argv per profile.
        "--pids-limit=1024",
        "--cpus=2",
        "--memory=4g",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=1g",
        "--tmpfs",
        "/root:rw,nosuid,nodev,size=512m",
        "--mount",
        f"type=bind,src={output_text},dst=/output",
        "--env",
        f"LOCKWIRE_MOTION_PLUGINS={','.join(plugins)}",
        "--entrypoint",
        resolved_entrypoint,
        image,
        *command,
    ]
    return argv


def kill_container_process(
    runner: CommandRunner, pid: int, *, grace: float = GRACE_SECONDS
) -> bool:
    """Stop process ``pid`` as seen through ``runner``, and prove it is gone.

    ``Job.cancel`` proves a local child's death by ``os.kill(pid, 0)`` and
    ``Popen.wait`` — this is its container-side twin, for a process this Python
    process does not own and cannot ``waitpid``: a ``ros2 bag record`` behind
    ``docker exec`` (E3-QC). ``kill -0`` through the same ``runner`` used to
    reach the process in the first place is the corpse check: it signals
    nothing and its exit code alone says whether ``pid`` still resolves inside
    that target. Same TERM-then-KILL order as ``Job.cancel`` and for the same
    reason: TERM first so a run mid-write to the bag gets ``grace`` seconds to
    close its files, KILL after so a recorder that ignores TERM cannot outlive
    a caller that believes it stopped.

    **Cancelling is not actuating:** this stops lockwire's own recorder
    process; nothing is ever sent to a device.

    Returns ``True`` only once the corpse is confirmed (including "already
    gone" before anything was sent) — never on a signal merely having been
    sent. ``False`` means TERM and KILL both went out and ``pid`` still
    resolves, so the caller must not report the recorder stopped.
    """

    def _alive() -> bool:
        return runner(("kill", "-0", str(pid))).returncode == 0

    if not _alive():
        return True
    runner(("kill", "-TERM", str(pid)))
    time.sleep(grace)
    if _alive():
        runner(("kill", "-KILL", str(pid)))
        time.sleep(grace)
    return not _alive()


def _ledger_rows(contract: Path | None) -> int:
    """Non-empty line count of the health sidecar — a partial line counts, on purpose."""
    if contract is None:
        return 0
    path = sidecar_path_for(contract)
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def build_argv(
    kind: str,
    *,
    contract: Path | str | None = None,
    recording: Path | str | None = None,
    context: str = "nominal-op",
    profile: str = "analysis",
    required_evidence: list[str] | None = None,
    robot: str = "scanned-robot",
    json_path: Path | None = None,
    live: bool = False,
    container: str | None = None,
    local: bool = False,
    seconds: float = 10.0,
    ssh_host: str | None = None,
    ssh_port: int = 22,
    ssh_user: str = "root",
    ssh_identity_file: Path | str | None = None,
    ssh_known_hosts: Path | str | None = None,
    ssh_target: str | None = None,
    ssh_image: str | None = None,
    keep_bag: Path | str | None = None,
) -> list[str]:
    """The argv list for ``lockwire <kind>`` — a list, never a string.

    Every caller-supplied value is its own element, so a path or a robot name
    containing a space, a quote, a newline or a ``;`` is one argument and can
    never become a second command. There is no quoting step here because there
    is nothing to quote: no shell ever sees these bytes.

    ``live=True`` builds ``preflight --live`` instead of ``--from RECORDING``
    (E3-QC); only ``kind="preflight"`` supports it — ``scan`` has no ``--live``.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}: expected one of {', '.join(KINDS)}")
    # A frozen sidecar is itself the lockwire CLI, so it cannot interpret
    # Python's ``-m lockwire`` launcher form.
    base = [sys.executable]
    if not getattr(sys, "frozen", False):
        base += ["-m", "lockwire"]
    base += [kind]
    if kind == "scan":
        if (
            live
            or ssh_host is not None
            or any((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image, keep_bag))
            or ssh_port != 22
            or ssh_user != "root"
        ):
            raise ValueError("scan has no --live; only preflight does")
        return [*base, "--robot", robot]
    if contract is None:
        raise ValueError("preflight needs contract=")
    from lockwire_pro.preflight.report import preflight_policy

    profile, required = preflight_policy(profile, required_evidence)
    if live:
        if recording is not None:
            raise ValueError("preflight --live is exclusive with recording=")
        if sum((container is not None, local, ssh_host is not None)) != 1:
            raise ValueError(
                "preflight --live needs exactly one of container=, local=True, or ssh_host="
            )
        argv = [*base, str(contract), "--live", "--seconds", str(seconds), "--context", context]
        if container is not None:
            argv += ["--container", container]
        if local:
            argv += ["--local"]
        if ssh_host is not None:
            if not all((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image)):
                raise ValueError(
                    "SSH live capture needs identity file, known hosts, target, and image"
                )
            argv += [
                "--ssh-host",
                ssh_host,
                "--ssh-port",
                str(ssh_port),
                "--ssh-user",
                ssh_user,
                "--ssh-identity-file",
                str(ssh_identity_file),
                "--ssh-known-hosts",
                str(ssh_known_hosts),
                "--ssh-target",
                ssh_target,
                "--ssh-image",
                ssh_image,
            ]
        elif (
            any((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image))
            or ssh_port != 22
            or ssh_user != "root"
        ):
            raise ValueError("SSH options need ssh_host=")
        if keep_bag is not None:
            argv += ["--keep-bag", str(keep_bag)]
    else:
        if (
            ssh_host is not None
            or any((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image, keep_bag))
            or ssh_port != 22
            or ssh_user != "root"
        ):
            raise ValueError("SSH and keep_bag options need live=True")
        if recording is None:
            raise ValueError("preflight needs recording= (or live=True)")
        argv = [*base, str(contract), "--from", str(recording), "--context", context]
    if json_path is not None:
        argv += ["--json", str(json_path)]
    if profile != "analysis" or required:
        argv += ["--profile", profile]
        for evidence in required:
            argv += ["--require-evidence", evidence]
    return argv


class Job:
    """A running ``lockwire`` command, with a way to stop it.

    Normally built by :func:`start_job`; the constructor is also the seam the
    cancellation tests use, with a plain uncooperative child in place of a run.
    """

    def __init__(
        self,
        kind: str,
        process: subprocess.Popen,
        work_dir: Path,
        contract: Path | None = None,
        rows_before: int = 0,
        live: bool = False,
        timeout: float | None = JOB_TIMEOUT_SECONDS,
    ):
        self.kind = kind
        self.process = process
        self.work_dir = work_dir
        self.contract = contract
        self.cancelled = False
        self.live = live
        self._rows_before = rows_before
        self._document: dict[str, Any] | None = None
        self._failure: Exception | None = None
        self._output = ""
        self._errors = ""
        self.timed_out = False
        self._timer: threading.Timer | None = None
        if timeout is not None:
            self._timer = threading.Timer(timeout, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self) -> None:
        """The time limit passed: stop the child (TERM, then KILL) and mark the job failed."""
        if self.process.poll() is not None:
            return
        self.timed_out = True
        self._signal_group(signal.SIGTERM)
        try:
            self.process.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)

    @property
    def exit_code(self) -> int | None:
        """The child's exit code, or ``None`` while it is still running."""
        return self.process.poll()

    @property
    def rows_written(self) -> int:
        """Health-ledger rows this job added. Zero for a run cancelled before its append."""
        return _ledger_rows(self.contract) - self._rows_before

    def cancel(self, grace: float = GRACE_SECONDS) -> None:
        """Stop the run, then prove the child is gone before returning.

        A non-live child runs in its own process group (``start_new_session``),
        so TERM reaches its grandchildren too. A live child's SIGTERM handler
        must keep its SSH/docker client alive while it stops the remote recorder
        and fetches the partial bag. Signal that child alone first; after its
        bounded cleanup, KILL any remaining members of its owned process group.

        **Reach, for a live job (E3-QC):** killing the local transport alone
        would detach it and leave the remote recorder running. The CLI handler
        calls ``LiveSource.cancel()`` to prove the recorder corpse before it
        fetches the partial bag. The live wait is bounded; if the handler hangs,
        group KILL remains the fallback. A child killed before it installs the
        handler cannot provide that remote proof.
        """
        self.cancelled = True
        if self._timer is not None:
            self._timer.cancel()
        if self.process.poll() is None:
            if self.live:
                try:
                    os.kill(self.process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                self._signal_group(signal.SIGTERM)
            # Live remote corpse proof and a verified bag fetch can outlast the
            # recorder's TERM grace; a structural child also needs time to stop
            # and reap its separately grouped solver before we escalate.
            first_wait = (
                max(grace, 20.0)
                if self.live
                else max(grace, 2 * GRACE_SECONDS + 1.0)
                if self.kind == "analyze"
                else grace
            )
            try:
                self.process.wait(timeout=first_wait)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                self.process.wait(timeout=grace)
        if self.live:
            self._signal_group(signal.SIGKILL)
        self._clean()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or no longer ours — the wait() above settles it

    def result(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the run and return its document — the CLI's own ``--json``.

        Raises :class:`JobCancelled` for a cancelled job and
        ``subprocess.TimeoutExpired`` if the run outlives ``timeout``.
        """
        if self.cancelled:
            raise JobCancelled(f"{self.kind} job was cancelled — no document was produced")
        if self._document is None and self._failure is None:
            self.process.wait(timeout=timeout)
            self._finish()
        if self._failure is not None:
            raise self._failure
        assert self._document is not None
        return self._document

    def snapshot(self) -> dict[str, Any]:
        """Return the current run state without waiting for a running child."""
        exit_code = self.exit_code
        if (
            exit_code is not None
            and not self.cancelled
            and self._document is None
            and self._failure is None
        ):
            self._finish()
        if self.cancelled:
            status = "cancelled"
        elif exit_code is None:
            status = "running"
        elif self._document is not None:
            status = "completed"
        else:
            status = "failed"
        return {
            "status": status,
            "exit_code": exit_code,
            "rows_written": self.rows_written,
            "document": self._document,
            "output": self._output if not self.work_dir.exists() else self._read("output.txt"),
            "errors": self._errors if not self.work_dir.exists() else self._read("errors.txt"),
            "error": str(self._failure) if self._failure is not None else None,
        }

    def _finish(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        try:
            if self.timed_out:
                raise RuntimeError(f"{self.kind} job was stopped: it ran past its time limit")
            self._document = self._read_document()
        except (RuntimeError, json.JSONDecodeError, AnalysisRefused, OSError) as error:
            self._failure = error
        finally:
            self._clean()

    def _read_document(self) -> dict[str, Any]:
        if self.kind == "analyze":
            path = self.work_dir / "document.json"
            if self.exit_code != 0 or not path.exists():
                raise RuntimeError(
                    f"analysis job failed (exit {self.exit_code}): {self._read('errors.txt').strip()}"
                )
            result = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys
            )
            if not isinstance(result, dict) or (
                (result.get("kind"), result.get("status"), result.get("solver_started"))
                not in {
                    ("analysis_preparation", "PREPARED", False),
                    ("analysis_result", "COMPLETED", True),
                }
            ):
                raise RuntimeError("analysis job returned an unsupported document")
            if result["kind"] == "analysis_result":
                output = result.get("output_text")
                if (
                    not isinstance(output, str)
                    or len(output.encode("utf-8"))
                    > (
                        64 * 1024 * 1024
                        if result.get("deck_template") == "frictionless-impact-v1"
                        else 8 * 1024 * 1024
                    )
                    or hashlib.sha256(output.encode("utf-8")).hexdigest()
                    != result.get("output_sha256")
                ):
                    raise RuntimeError("analysis result output bytes do not match their hash")
            return result
        if self.kind == "preflight":
            document = self.work_dir / "document.json"
            if not document.exists():
                raise RuntimeError(
                    f"preflight wrote no document (exit {self.exit_code}): "
                    f"{self._read('errors.txt').strip() or 'no output'}"
                )
            if self.exit_code is not None and self.exit_code < 0:
                # A NO-GO exits 1 with its document; a signal death is not a verdict.
                raise RuntimeError(f"preflight was killed (signal {-self.exit_code})")
            return json.loads(document.read_text(encoding="utf-8"))
        # `scan` has no --json: its document is the draft YAML it prints, which is
        # why stdout is captured apart from stderr rather than merged into it.
        if self.exit_code != 0:
            raise RuntimeError(f"scan failed (exit {self.exit_code})")
        return {"draft": self._read("output.txt")}

    def _read(self, name: str) -> str:
        path = self.work_dir / name
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def _clean(self) -> None:
        """Delete the job's scratch directory — with it, any half-written document.

        A cancelled ``preflight`` was writing its ``--json`` here, so the partial
        file goes with the directory and no later run can find it. Idempotent.
        """
        if self.work_dir.exists():
            self._output = self._read("output.txt")
            self._errors = self._read("errors.txt")
        shutil.rmtree(self.work_dir, ignore_errors=True)


class MotionJob(Job):
    """A :class:`Job` whose Docker container is checked by its exact identity."""

    def __init__(
        self,
        *args: Any,
        container_name: str,
        run_id: str,
        mode: str,
        profile: _MotionProfile | _SimJazzyProfile,
        evidence_dir: Path | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.container_name = container_name
        self.run_id = run_id
        self.mode = mode
        self.profile = profile
        self.evidence_dir = evidence_dir
        self._evidence_inode = evidence_dir.stat().st_ino if evidence_dir is not None else None

    def _read(self, name: str) -> str:
        try:
            return _motion_read(self.work_dir / name).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except (OSError, ValueError) as error:
            return f"motion output refused: {error}"

    def _read_document(self) -> dict[str, Any]:
        if isinstance(self.profile, _SimJazzyProfile):
            return self._read_sim_jazzy_document()
        return self._read_pendulum_document()

    def _read_sim_jazzy_document(self) -> dict[str, Any]:
        """The ANYmal branch: dispatched per profile, judged by :func:`_anymal_evidence_reason`."""
        try:
            document = json.loads(
                _motion_read(self.work_dir / "document.json"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            if not isinstance(document, dict) or set(document) != {
                "label",
                "scenario",
                "exit_code",
                "evidence",
                "verdict",
                "fixture_admission",
                "baked_inputs",
            }:
                raise ValueError("motion document shape is unknown")
            if (
                document["label"] != "e5_10_sim_jazzy_admission"
                or document["scenario"] != self.mode
            ):
                raise ValueError("motion document identity/scenario differs from the launch")
            if type(document["exit_code"]) is not int or document["exit_code"] != self.exit_code:
                raise ValueError("motion document exit differs from the direct process exit")
            is_x500 = self.mode == "px4_x500_takeoff_hover_land"
            expected_evidence = (
                {"ulog": "x500.ulg"}
                if is_x500
                else {
                    "joint_state": "joint_states.raw",
                    "tracking": "tracking.raw",
                    "controllers": "controllers.raw",
                }
            )
            if document["evidence"] != expected_evidence:
                raise ValueError("motion evidence paths differ from the fixed contained artifacts")
            scenarios_raw = _motion_read(self.work_dir / "scenarios.json")
            scenarios_hash = hashlib.sha256(scenarios_raw).hexdigest()
            if (
                document["baked_inputs"] != {"scenarios": scenarios_hash}
                or self.profile.baked_inputs.get("scenarios") != scenarios_hash
            ):
                raise ValueError(
                    "motion baked scenarios identity differs from the recorded image profile"
                )
            scenarios = json.loads(scenarios_raw, object_pairs_hook=_reject_duplicate_json_keys)
            scenario = scenarios["scenarios"][self.mode]
            criteria = dict(scenario["acceptance"])
            if self.mode == "anymal_d_effort_squat":
                criteria["nominal_rad"] = scenario["test_trajectory"]["nominal_rad"]
            if is_x500:
                reason = _x500_evidence_reason(
                    criteria, ulog_bytes=_motion_read(self.work_dir / "x500.ulg")
                )
            else:
                raw = {
                    field: _motion_read(self.work_dir / name).decode("utf-8")
                    for field, name in {
                        "state": "joint_states.raw",
                        "tracking": "tracking.raw",
                        "controllers": "controllers.raw",
                    }.items()
                }
                reason = _anymal_evidence_reason(
                    self.mode,
                    state=raw["state"],
                    tracking=raw["tracking"],
                    controllers=raw["controllers"],
                    criteria=criteria,
                )
            passed = self.exit_code == 0 and reason is None
            document["fixture_admission"] = "PASSED" if passed else "REFUSED"
            document["verdict"] = (
                "GO"
                if passed and self.mode in ("anymal_d_effort_squat", "px4_x500_takeoff_hover_land")
                else "NO-GO"
            )
            document["physical_conformance"] = "NOT_SCORED"
            document["simulation_validity"] = "NOT_SCORED"
            document["admission_reason"] = (
                None
                if passed
                else (reason or f"motion fixture process failed with exit {self.exit_code}")
            )
            return document
        except (OSError, UnicodeError, ValueError, KeyError) as error:
            raise RuntimeError(f"motion admission refused: {error}") from error

    def _read_pendulum_document(self) -> dict[str, Any]:
        """Never return the container's claimed GO without host raw-byte admission."""
        try:
            document = json.loads(
                _motion_read(self.work_dir / "document.json"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            if not isinstance(document, dict) or set(document) != {
                "label",
                "mode",
                "exit_code",
                "evidence",
                "verdict",
                "fixture_admission",
                "baked_inputs",
                "control_msgs_version",
                "controller_version",
            }:
                raise ValueError("motion document shape is unknown")
            if document["label"] != "synthetic_runtime_admission" or document["mode"] != self.mode:
                raise ValueError("motion document identity/mode differs from the launch")
            if type(document["exit_code"]) is not int or document["exit_code"] != self.exit_code:
                raise ValueError("motion document exit differs from the direct process exit")
            if document["baked_inputs"] != self.profile.baked_inputs or (
                document["control_msgs_version"] != self.profile.control_msgs_version
                or document["controller_version"] != self.profile.controller_version
            ):
                raise ValueError(
                    "motion baked input/package identity differs from the recorded image profile"
                )
            if document["evidence"] != {
                "joint_state": "joint_states.raw",
                "effort": "effort.raw",
                "tracking": "tracking.raw",
            }:
                raise ValueError("motion evidence paths differ from the fixed contained artifacts")
            raw = {
                field: _motion_read(self.work_dir / name).decode("utf-8")
                for field, name in {
                    "state": "joint_states.raw",
                    "effort": "effort.raw",
                    "tracking": "tracking.raw",
                }.items()
            }
            constraints = {
                field: getattr(self.profile, field)
                for field in ("trajectory_tolerance", "goal_tolerance", "goal_position")
            }
            genuine = motion_evidence_is_genuine(self.mode, **raw, **constraints)
            passed = self.exit_code == 0 and genuine
            document["fixture_admission"] = "PASSED" if passed else "REFUSED"
            document["verdict"] = "GO" if passed and self.mode == "enabled" else "NO-GO"
            document["physical_conformance"] = "NOT_SCORED"
            document["simulation_validity"] = "NOT_SCORED"
            document["admission_reason"] = (
                None
                if passed
                else (
                    _motion_evidence_reason(self.mode, **raw, **constraints)
                    or f"motion fixture process failed with exit {self.exit_code}"
                )
            )
            return document
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeError(f"motion admission refused: {error}") from error

    def _owned_container_ids(self) -> list[str]:
        result = subprocess.run(  # noqa: S603 - fixed Docker argv, never a shell
            [
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                f"name=^/{self.container_name}$",
                "--filter",
                f"label={MOTION_LABEL}=true",
                "--filter",
                f"label=lockwire.motion.run_id={self.run_id}",
            ],
            capture_output=True,
            text=True,
            timeout=GRACE_SECONDS,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"could not inspect motion container {self.container_name}")
        return result.stdout.split()

    def _clean(self) -> None:
        """Bounded TERM/KILL/remove, then prove this run owns no survivor."""
        failure = None
        try:
            if self._owned_container_ids():
                subprocess.run(  # noqa: S603 - exact owned container name
                    [
                        "docker",
                        "container",
                        "stop",
                        "--time",
                        str(int(GRACE_SECONDS)),
                        self.container_name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=GRACE_SECONDS + 1,
                    check=False,
                )
            if self._owned_container_ids():
                subprocess.run(  # noqa: S603 - exact owned container name
                    ["docker", "container", "kill", self.container_name],
                    capture_output=True,
                    text=True,
                    timeout=GRACE_SECONDS,
                    check=False,
                )
                subprocess.run(  # noqa: S603 - exact owned container name
                    ["docker", "container", "rm", "--force", self.container_name],
                    capture_output=True,
                    text=True,
                    timeout=GRACE_SECONDS,
                    check=False,
                )
            if self._owned_container_ids():
                raise RuntimeError(f"motion container {self.container_name} survived cleanup")
        except Exception as error:
            failure = error
            self._failure, self._document = error, None
        finally:
            try:
                self._preserve_evidence()
            except Exception as error:
                failure = error
                self._failure, self._document = error, None
            finally:
                super()._clean()
        if failure is not None:
            raise failure

    def _preserve_evidence(self) -> None:
        if self.evidence_dir is None or not self.work_dir.exists():
            return
        if (
            self.evidence_dir.is_symlink()
            or self.evidence_dir.stat().st_ino != self._evidence_inode
        ):
            raise RuntimeError("motion evidence output target was replaced")
        hashes = {}
        refusals = {}
        directory_fd = os.open(self.evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(directory_fd).st_ino != self._evidence_inode or os.listdir(directory_fd):
                raise RuntimeError("motion evidence output target is replaced or foreign-occupied")
            for name in _MOTION_FILES:
                path = self.work_dir / name
                if not path.exists() and not path.is_symlink():
                    continue
                try:
                    data = _motion_read(path)
                except (OSError, ValueError) as error:
                    refusals[name] = str(error)
                    continue
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "wb") as handle:
                    if handle.write(data) != len(data):
                        raise OSError("short write preserving motion evidence")
                    handle.flush()
                    os.fsync(handle.fileno())
                hashes[name] = hashlib.sha256(data).hexdigest()
            encoded = (
                json.dumps(
                    {
                        "label": "synthetic_runtime_admission",
                        "run_id": self.run_id,
                        "mode": self.mode,
                        "image": self.profile.image,
                        "baked_inputs": self.profile.baked_inputs,
                        "exit_code": self.exit_code,
                        "cancelled": self.cancelled,
                        "document": self._document,
                        "failure": str(self._failure) if self._failure else None,
                        "sha256": hashes,
                        "preservation_refusals": refusals,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            temporary_name = f".receipt-{uuid.uuid4()}.tmp"
            # Same flush/fsync/atomic-publication pattern as analysis's writer,
            # but anchored to the owned directory fd and link-without-overwrite:
            # its path-based replace would overwrite a foreign occupied target.
            fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    if handle.write(encoded) != len(encoded):
                        raise OSError("short write preserving motion receipt")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(
                    temporary_name,
                    "receipt.json",
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.fsync(directory_fd)
            finally:
                os.unlink(temporary_name, dir_fd=directory_fd)
            if (
                self.evidence_dir.is_symlink()
                or self.evidence_dir.stat().st_ino != self._evidence_inode
            ):
                raise RuntimeError("motion evidence output target was replaced during preservation")
        finally:
            os.close(directory_fd)
        if refusals:
            raise RuntimeError(f"motion evidence preservation refused unsafe outputs: {refusals}")


def start_motion_job(
    run_id: str,
    *,
    source_mounts: Mapping[Path | str, str] | None = None,
    command: Sequence[str] = (),
    plugins: Sequence[str] = (),
    image: str = MOTION_IMAGE,
    entrypoint: str | None = None,
    evidence_dir: Path | None = None,
    profile: Path | None = None,
) -> MotionJob:
    """Start the isolated synthetic motion admission in the existing Job lifecycle.

    The per-run directory is the sole writable host bind. The container writes
    ``document.json`` and its timestamped evidence there; :class:`Job` reads the
    document and removes the directory after completion or cancellation.
    ``profile`` is the trusted-profile manifest path CI records.
    """
    name, canonical_id = _motion_identity(run_id)
    if image == MOTION_IMAGE:
        raise RuntimeError("motion admission has no measured image pin; build and record it first")
    matched_profile = _motion_profile(image, profile_path=profile)
    resolved_entrypoint = entrypoint if entrypoint is not None else matched_profile.entrypoint
    if (
        source_mounts
        or resolved_entrypoint != matched_profile.entrypoint
        or plugins
        or tuple(command) not in {(c,) for c in matched_profile.commands}
    ):
        raise ValueError(
            "motion only supports baked inputs/runner/plugins and enabled/disabled fixture argv"
        )
    if evidence_dir is not None:
        if not evidence_dir.is_absolute() or evidence_dir.is_symlink() or evidence_dir.exists():
            raise ValueError(
                "motion evidence output must be a fresh absolute non-symlink directory"
            )
        evidence_dir = evidence_dir.parent.resolve(strict=True) / evidence_dir.name
    work_dir = Path(tempfile.mkdtemp(prefix="lockwire-motion-"))
    try:
        argv = build_motion_argv(
            canonical_id,
            output_dir=work_dir,
            source_mounts=source_mounts,
            command=command,
            plugins=plugins,
            image=image,
            entrypoint=entrypoint,
            profile=profile,
        )
        # Retain the exact trusted profile selected by the builder, not an earlier
        # read if package bytes were concurrently replaced.
        if _motion_profile(image, profile_path=profile) != matched_profile:
            raise ValueError("trusted motion profile changed during launch preparation")
        if evidence_dir is not None:
            evidence_dir.mkdir(mode=0o700)
        with (
            (work_dir / "output.txt").open("w", encoding="utf-8") as out,
            (work_dir / "errors.txt").open("w", encoding="utf-8") as err,
        ):
            process = subprocess.Popen(  # noqa: S603 - fixed argv from build_motion_argv
                argv,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return MotionJob(
        "preflight",
        process,
        work_dir,
        container_name=name,
        run_id=canonical_id,
        mode=command[0],
        profile=matched_profile,
        evidence_dir=evidence_dir,
    )


def start_job(
    kind: str,
    *,
    contract: Path | str | None = None,
    recording: Path | str | None = None,
    context: str = "nominal-op",
    profile: str = "analysis",
    required_evidence: list[str] | None = None,
    robot: str = "scanned-robot",
    live: bool = False,
    container: str | None = None,
    local: bool = False,
    seconds: float = 10.0,
    ssh_host: str | None = None,
    ssh_port: int = 22,
    ssh_user: str = "root",
    ssh_identity_file: Path | str | None = None,
    ssh_known_hosts: Path | str | None = None,
    ssh_target: str | None = None,
    ssh_image: str | None = None,
    keep_bag: Path | str | None = None,
) -> Job:
    """Start ``lockwire <kind>`` as a child process and hand back a handle.

    ``preflight`` needs ``contract`` and ``recording``; ``scan`` takes neither.
    ``live=True`` needs ``container=`` (docker), ``local=True``, or a declared
    SSH target instead of ``recording`` — only ``kind="preflight"`` supports it.

    **Presence and reachability (E3-QC).** ``preflight --live`` runs ``ros2 bag
    record`` on the target, inside a container via ``docker exec`` or on this
    machine; killing this job's process group on its own would only kill the
    local client and leave that recorder running. What closes the gap is not
    here — it is the CLI's own ``--live`` branch (``cli.py``), which installs a
    SIGTERM/SIGINT handler that calls ``LiveSource.cancel()`` (backed by
    :func:`kill_container_process`, proving the corpse) before letting the
    signal finish the process. ``Job.cancel`` signals the live CLI leader first
    so its SSH/docker transport remains available for cleanup, then kills any
    surviving members of the owned group after a bounded wait.

    E3-QC's remaining piece was learning the recorder's container-side pid
    while the capture's single blocking ``docker exec`` is still in flight:
    ``LiveSource`` now does that with a background thread plus a concurrent
    poll through the same runner (``cancellable=True``, set unconditionally by
    the CLI's ``--live`` branch, not exposed here — this function has no direct
    handle on the child's ``LiveSource`` instance to toggle it itself).
    """
    if kind == "preflight" and live:
        if container is None and not local and ssh_host is None:
            raise ValueError(
                "start_job needs container=, local=True, or ssh_host= for a live preflight"
            )
    elif live:
        raise ValueError(f"only preflight can run live, not {kind!r}")
    work_dir = Path(tempfile.mkdtemp(prefix="lockwire-job-"))
    contract_path = Path(contract) if kind == "preflight" and contract is not None else None
    try:
        argv = build_argv(
            kind,
            contract=contract,
            recording=recording,
            context=context,
            profile=profile,
            required_evidence=required_evidence,
            robot=robot,
            json_path=work_dir / "document.json" if kind == "preflight" else None,
            live=live,
            container=container,
            local=local,
            seconds=seconds,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_identity_file=ssh_identity_file,
            ssh_known_hosts=ssh_known_hosts,
            ssh_target=ssh_target,
            ssh_image=ssh_image,
            keep_bag=keep_bag,
        )
        # Counted before the child can append: the number must be a fact about this job.
        rows_before = _ledger_rows(contract_path)
        with (
            (work_dir / "output.txt").open("w", encoding="utf-8") as out,
            (work_dir / "errors.txt").open("w", encoding="utf-8") as err,
        ):
            process = subprocess.Popen(  # noqa: S603 - argv list from build_argv, never a shell
                argv,
                stdout=out,
                stderr=err,
                start_new_session=True,  # its own process group, so cancel reaches grandchildren
            )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return Job(kind, process, work_dir, contract_path, rows_before, live=live)


def start_analysis_job(
    contract: Path,
    *,
    expected_revision: str,
    procedure_id: str,
    evidence_root: Path,
    package_manifest_path: Path,
) -> Job:
    """Prepare a bound structural deck under the existing cancellable Job owner."""
    work_dir = Path(tempfile.mkdtemp(prefix="lockwire-analysis-"))
    argv = build_analysis_argv(
        contract,
        expected_revision=expected_revision,
        procedure_id=procedure_id,
        evidence_root=evidence_root,
        package_manifest_path=package_manifest_path,
        document_path=work_dir / "document.json",
    )
    try:
        with (
            (work_dir / "output.txt").open("w", encoding="utf-8") as out,
            (work_dir / "errors.txt").open("w", encoding="utf-8") as err,
        ):
            process = subprocess.Popen(argv, stdout=out, stderr=err, start_new_session=True)  # noqa: S603
    except OSError:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return Job("analyze", process, work_dir)
