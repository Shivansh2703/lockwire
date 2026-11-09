"""One ROS 2 command from a running robot to a verdict.

    ros2 run lockwire_ros preflight lockwire.yaml --seconds 10

Subscribes to every topic the contract's drivers declare, captures for that
window, writes a lockwire recording, runs `lockwire preflight --from` on it and
exits with the verdict's code: 0 GO (with or without notes), 1 NO-GO, 2 the
gate could not run (nothing captured, core missing, bad contract), 3 nothing
scored. Everything nonzero is a refusal, so a shell `&&` or a launch file can
gate on it directly.

Not a lifecycle node on purpose: a person at a terminal and a CI step both want
a process that runs once and exits, not a node to drive through transitions.

Python 3.10 compatible; imports rclpy only inside ``main`` so the decision path
is testable without ROS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Callable, List, Optional

from lockwire_ros import core
from lockwire_ros.capture import CONTEXTS


def live_argv(contract, seconds, context, executable="lockwire") -> List[object]:
    """The argv a launch file runs for a live gate (items may be launch substitutions).

    ``python -m`` rather than ``ros2 run``: it works both from a colcon install
    and from a source checkout on PYTHONPATH, and keeps the child's exit code
    exactly the verdict's.
    """
    return [sys.executable, "-m", "lockwire_ros.preflight_cli", contract,
            "--seconds", seconds, "--context", context, "--lockwire-executable", executable]


def parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="preflight",
        description="Capture the contract's declared topics live, then gate on them.",
    )
    parser.add_argument("contract", help="the lockfile to gate against")
    parser.add_argument("--seconds", type=float, default=10.0, help="capture window")
    parser.add_argument("--context", choices=CONTEXTS, default="bench-idle")
    parser.add_argument("--out", default="", help="keep the recording here (default: a temp dir)")
    parser.add_argument("--profile", default="analysis", help="analysis or powered-release")
    parser.add_argument("--require-evidence", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="print the core's JSON document")
    parser.add_argument("--no-ledger", action="store_true",
                        help="do not append this run to the robot's health ledger")
    parser.add_argument("--lockwire-executable", default="lockwire")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    # `ros2 run ... --ros-args -r ...` belongs to rclpy, not to us.
    if "--ros-args" in argv:
        argv = argv[: argv.index("--ros-args")]
    return parser.parse_args(argv)


def _capture_live(args: argparse.Namespace, out_dir: str) -> str:
    import rclpy

    from lockwire_ros.capture_node import capture_to

    rclpy.init()
    try:
        return capture_to(args.contract, out_dir, args.context, args.seconds,
                          node_name="lockwire_preflight")
    finally:
        if rclpy.ok():
            rclpy.shutdown()


def main(argv: Optional[List[str]] = None,
         capture: Callable[[argparse.Namespace, str], str] = _capture_live) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    say = sys.stderr if args.json else sys.stdout
    try:
        recording = capture(args, args.out or tempfile.mkdtemp(prefix="lockwire-capture-"))
    except Exception as error:  # capture is part of the gate: no evidence is a refusal
        print("lockwire preflight REFUSED: capture failed: {0}".format(error), file=sys.stderr)
        if args.json:
            print(json.dumps({"verdict": "REFUSED", "exit_code": 2, "reason": str(error)}))
        return core.EXIT_TOOL_FAILED
    print("captured {0:g}s of live topics -> {1}".format(args.seconds, recording), file=say, flush=True)

    # The JSON lands beside the recording, so the core's "wrote ..." line names a file that stays.
    result = core.run_gate(args.contract, recording, args.lockwire_executable, args.timeout_s,
                           profile=args.profile, required_evidence=args.require_evidence,
                           no_ledger=args.no_ledger,
                           json_out=os.path.join(recording, "lockwire-preflight.json"))
    if args.json:
        print(json.dumps(result.document or {"verdict": "REFUSED", "exit_code": 2,
                                             "reason": result.reason}, indent=2), flush=True)
    else:
        print(result.output, end="", flush=True)  # before the summary on stderr
    print("lockwire {0}: {1}".format(result.outcome, result.reason), file=sys.stderr)
    if result.outcome == core.REFUSED or result.exit_code is None:
        return core.EXIT_TOOL_FAILED
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
