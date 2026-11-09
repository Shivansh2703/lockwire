"""Gate a bringup on `lockwire preflight`: nothing downstream starts until it passes.

Lives in the package (not in ``launch/``) so it can be imported and tested;
``launch/preflight.launch.py`` is a thin entry point over it.

**The sharp edge this module exists to handle.** `ros2 launch` does not
propagate a child process's exit code. ``LaunchService.run()`` returns 1 only
when an exception reaches it, when the run task is cancelled, or when its
asyncio exception handler fires — *never* because an ``ExecuteProcess`` exited
nonzero. That is ros2/launch#586, open since 2022-01-19 and still open.

Measured on ROS 2 Humble, 2026-08-14, both halves in one run:

- a launch file that reacts to the refusal with ``EmitEvent(Shutdown(...))`` —
  the shape almost everyone writes — shut the launch down and **`ros2 launch`
  still exited 0**;
- ``gate_bringup`` below exited **1**, with zero downstream actions started.

For a product whose death condition is a gate that gets bypassed, "the gate
refused and the launch reported success" is not a footnote. So two things here
are deliberate:

1. Downstream actions are registered **inside** the zero-returncode branch, so a
   refusal means they are never emitted at all.
2. A refusal **raises** from inside the launch event loop, which is the one route
   that makes ``LaunchService.run()`` actually return nonzero. Shutdown alone
   stops the launch but leaves the code at 0 — stopping is not reporting.

Both halves are pinned by ``test/test_launch_exit_code.py``, which reinstates the
naive shape as a negative control. If #586 is ever fixed upstream the raise
becomes redundant, and harmless.

Python 3.10 compatible.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
    LogInfo,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import AnyLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration

from lockwire_ros.preflight_cli import live_argv


class LockwirePreflightFailed(RuntimeError):
    """Raised inside the launch event loop so `ros2 launch` exits nonzero.

    Named rather than a bare RuntimeError so a supervisor reading the log can
    tell "the gate refused" from "launch broke".
    """


def preflight_process(
    contract: Optional[str] = None,
    recording: Optional[str] = None,
    executable: str = "lockwire",
    live: bool = False,
    capture_s: float = 10.0,
    context: str = "bench-idle",
) -> ExecuteProcess:
    """The gate itself, as a launch action.

    ``live=True`` captures the contract's declared topics for ``capture_s`` and
    gates on that (``lockwire_ros.preflight_cli``); otherwise it gates a
    recording. ``shell=False`` is load-bearing: ros2/launch#757 reports
    ``ExecuteProcess`` with ``shell=True`` reading back the wrong exit code, and
    this whole design rests on that number being right.
    """
    contract = contract if contract is not None else LaunchConfiguration("contract")
    if live:
        cmd = live_argv(contract, str(capture_s), context, executable)
    else:
        cmd = [executable, "preflight", contract, "--from",
               recording if recording is not None else LaunchConfiguration("recording")]
    return ExecuteProcess(cmd=cmd, name="lockwire_preflight", output="screen", shell=False)


def gate_bringup(
    on_pass: Sequence[object] = (),
    contract: Optional[str] = None,
    recording: Optional[str] = None,
    executable: str = "lockwire",
    live: bool = False,
    capture_s: float = 10.0,
    context: str = "bench-idle",
) -> List[object]:
    """Actions that run ``on_pass`` only after the gate returns exit 0."""
    gate = preflight_process(contract, recording, executable, live, capture_s, context)

    def _on_exit(event, context):
        if event.returncode == 0:
            return [LogInfo(msg="lockwire GO — continuing bringup"), *on_pass]
        raise LockwirePreflightFailed(
            "lockwire preflight exited {0} — bringup refused, nothing downstream "
            "was started".format(event.returncode)
        )

    return [gate, RegisterEventHandler(OnProcessExit(target_action=gate, on_exit=_on_exit))]


def gate_launch_description() -> LaunchDescription:
    """What ``preflight.launch.py`` and ``lockwire_gate.launch.py`` both serve.

    ``bringup:=<launch file>`` is included only after a GO. Without it, an
    include of this file still fails the launch on a refusal, but sibling
    actions in the including file start alongside the gate, not after it.
    """

    def _resolve(launch_context):
        def arg(name):
            return LaunchConfiguration(name).perform(launch_context)

        live = arg("live").lower() in ("true", "1", "yes")
        if not live and not arg("recording"):
            raise LockwirePreflightFailed("pass recording:=<dir> or live:=true")
        on_pass = [IncludeLaunchDescription(AnyLaunchDescriptionSource(arg("bringup")))
                   ] if arg("bringup") else []
        return gate_bringup(on_pass, arg("contract"), arg("recording"),
                            arg("lockwire_executable"), live, float(arg("capture_s")),
                            arg("context"))

    return LaunchDescription([
        DeclareLaunchArgument("contract", default_value="lockwire.yaml"),
        DeclareLaunchArgument("recording", default_value="",
                              description="lockwire recording dir or .ulg (offline gate)"),
        DeclareLaunchArgument("live", default_value="false",
                              description="capture the contract's topics instead"),
        DeclareLaunchArgument("capture_s", default_value="10.0"),
        DeclareLaunchArgument("context", default_value="bench-idle"),
        DeclareLaunchArgument("bringup", default_value="",
                              description="launch file to start only after a GO"),
        DeclareLaunchArgument("lockwire_executable", default_value="lockwire"),
        OpaqueFunction(function=_resolve),
    ])
