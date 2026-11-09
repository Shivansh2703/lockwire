"""`ros2 launch` must report a refusal, not just act on it.

This file exists because of a measured upstream behaviour, not a suspected one:
``LaunchService.run()`` never returns nonzero because a child exited nonzero
(ros2/launch#586, open since 2022-01-19). A gate that refuses while
`ros2 launch` reports success is the bypassed-gate failure lockwire is built to
prevent, and it is invisible from inside the launch file.

So this runs the real `ros2 launch` binary twice against a refusing gate:

- ``test_the_naive_shutdown_shape_exits_zero`` is the **negative control** — the
  bug reinstated. It pins the upstream behaviour, so if #586 is ever fixed this
  test goes red and tells us the workaround can be reconsidered, rather than the
  workaround quietly living forever.
- ``test_gate_bringup_exits_nonzero_on_a_refusal`` is the claim.

Container-only: needs a ROS environment with `ros2 launch`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("rclpy", reason="no ROS environment; run this in the Humble container")

ROS2 = shutil.which("ros2")
requires_ros2 = pytest.mark.skipif(ROS2 is None, reason="ros2 CLI not on PATH")

NAIVE_LAUNCH = """
from launch import LaunchDescription
from launch.actions import EmitEvent, ExecuteProcess, LogInfo, RegisterEventHandler
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown


def generate_launch_description():
    gate = ExecuteProcess(
        cmd=["lockwire", "preflight", "c.yaml", "--from", "r"], output="screen", shell=False
    )

    def _on_exit(event, context):
        if event.returncode:
            return [LogInfo(msg="refused"), EmitEvent(event=Shutdown(reason="refused"))]
        return []

    return LaunchDescription(
        [gate, RegisterEventHandler(OnProcessExit(target_action=gate, on_exit=_on_exit))]
    )
"""

OURS_LAUNCH = """
from launch import LaunchDescription
from launch.actions import LogInfo

from lockwire_ros.launch_gate import gate_bringup


def generate_launch_description():
    return LaunchDescription(
        gate_bringup(
            on_pass=[LogInfo(msg="DOWNSTREAM-STARTED")], contract="c.yaml", recording="r"
        )
    )
"""


@pytest.fixture
def refusing_lockwire(tmp_path):
    """A `lockwire` on PATH that always exits 1, so the gate always refuses."""
    script = tmp_path / "lockwire"
    script.write_text(
        "#!"
        + sys.executable
        + textwrap.dedent(
            """
            import sys
            print("NO-GO — 1 violation(s), 0 warning(s)")
            sys.exit(1)
            """
        )
    )
    script.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    return env


def _launch(path, source: str, env):
    path.write_text(source)
    return subprocess.run(
        [ROS2, "launch", str(path)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )


@requires_ros2
def test_the_naive_shutdown_shape_exits_zero(tmp_path, refusing_lockwire):
    """NEGATIVE CONTROL — the bug, reinstated and observed.

    EmitEvent(Shutdown) stops the launch but leaves the exit code at 0. If this
    ever starts returning nonzero, #586 was fixed upstream and
    `lockwire_ros.launch_gate`'s raise can be revisited.
    """
    result = _launch(tmp_path / "naive.launch.py", NAIVE_LAUNCH, refusing_lockwire)

    assert result.returncode == 0, (
        "ros2 launch now propagates child exit codes — recheck ros2/launch#586\n"
        + result.stdout
        + result.stderr
    )


@requires_ros2
def test_gate_bringup_exits_nonzero_on_a_refusal(tmp_path, refusing_lockwire):
    result = _launch(tmp_path / "ours.launch.py", OURS_LAUNCH, refusing_lockwire)

    assert result.returncode != 0, result.stdout + result.stderr


@requires_ros2
def test_a_refusal_starts_nothing_downstream(tmp_path, refusing_lockwire):
    """The exit code is only half of it: the gated actions must never be emitted."""
    result = _launch(tmp_path / "ours2.launch.py", OURS_LAUNCH, refusing_lockwire)

    assert "DOWNSTREAM-STARTED" not in (result.stdout + result.stderr)
    assert "bringup refused" in (result.stdout + result.stderr)


@requires_ros2
def test_a_passing_gate_lets_bringup_through(tmp_path):
    """The positive control: without it, a gate that refuses everything would pass above."""
    script = tmp_path / "lockwire"
    script.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(0)\n")
    script.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]

    result = _launch(tmp_path / "pass.launch.py", OURS_LAUNCH, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DOWNSTREAM-STARTED" in (result.stdout + result.stderr)
