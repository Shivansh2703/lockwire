"""`ros2 run lockwire_ros preflight`: args -> capture -> gate -> exit code, off-robot.

The capture is a stand-in that writes real native evidence from fake rows; the
core is a real subprocess (a stand-in script), so the exit-code path crosses the
same process boundary it does on a robot. The live versions of these cases run
in the Humble container (test_live_capture.py).
"""

from __future__ import annotations

import json
import sys

import pytest

from lockwire_ros import preflight_cli
from lockwire_ros.capture import DeviceCapture, write_recording

PRINTED = "PREFLIGHT TEXT FROM THE CORE"


def _core(tmp_path, verdict, code):
    """A `lockwire` that prints text, writes a canonical document, exits `code`."""
    document = {"verdict": verdict, "exit_code": code, "scored": 0 if code == 3 else 4,
                "counts": {"violation": int(code == 1)}, "findings": []}
    script = tmp_path / "lockwire"
    script.write_text("#!{0}\nimport json, sys\nprint({1!r})\nout = sys.argv[sys.argv.index("
                      "'--json') + 1]\njson.dump({2}, open(out, 'w'))\nprint('wrote', out)\n"
                      "json.dump(sys.argv, open({4!r}, 'w'))\nsys.exit({3})\n".format(
                          sys.executable, PRINTED, json.dumps(document), code,
                          str(tmp_path / "argv.json")))
    script.chmod(0o755)
    return str(script)


def _capture(rows):
    seen = {}

    def capture(args, out_dir):
        seen.update(vars(args))
        device = DeviceCapture("dev.imu", "/imu/data", "sensor_msgs/msg/Imu",
                               {"accel_z": ("linear_acceleration.z", "m/s^2")})
        device.rows = rows
        return write_recording(out_dir, [device], args.context)

    return capture, seen


@pytest.mark.parametrize("verdict,code", [("GO", 0), ("GO_WITH_NOTES", 0), ("NO-GO", 1),
                                          ("REFUSED", 2), ("NOT_SCORED", 3)])
def test_exit_code_is_the_verdicts_and_the_core_text_is_printed(tmp_path, capsys, verdict, code):
    capture, seen = _capture([(0.0, {"accel_z": -9.81}), (0.01, {"accel_z": -9.8})])
    argv = ["lockwire.yaml", "--seconds", "3", "--context", "armed-idle", "--out",
            str(tmp_path / "rec"), "--lockwire-executable", _core(tmp_path, verdict, code),
            "--ros-args", "-r", "__node:=x"]

    assert preflight_cli.main(argv, capture) == code
    assert PRINTED in capsys.readouterr().out
    assert (seen["seconds"], seen["context"]) == (3.0, "armed-idle")


def test_json_passes_the_core_document_through(tmp_path, capsys):
    capture, _ = _capture([(0.0, {"accel_z": -9.81})])
    argv = ["c.yaml", "--json", "--lockwire-executable", _core(tmp_path, "NO-GO", 1)]

    assert preflight_cli.main(argv, capture) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "NO-GO"


def test_a_silent_topic_refuses_with_exit_two_and_names_the_topic(tmp_path, capsys):
    capture, _ = _capture([])
    argv = ["c.yaml", "--lockwire-executable", _core(tmp_path, "GO", 0)]

    assert preflight_cli.main(argv, capture) == 2
    assert "/imu/data" in capsys.readouterr().err


def test_launch_live_argv_runs_this_module():
    argv = preflight_cli.live_argv("c.yaml", "4.0", "bench-idle", "/opt/lockwire")
    assert argv[1:] == ["-m", "lockwire_ros.preflight_cli", "c.yaml", "--seconds", "4.0",
                        "--context", "bench-idle", "--lockwire-executable", "/opt/lockwire"]


def test_no_ledger_reaches_the_core_and_the_printed_json_path_exists(tmp_path, capsys):
    capture, _ = _capture([(0.0, {"accel_z": -9.81})])
    argv = ["c.yaml", "--no-ledger", "--lockwire-executable", _core(tmp_path, "GO", 0)]

    assert preflight_cli.main(argv, capture) == 0
    assert "--no-ledger" in json.loads((tmp_path / "argv.json").read_text())
    wrote = [line for line in capsys.readouterr().out.splitlines() if line.startswith("wrote ")]
    assert wrote and json.loads(open(wrote[0][len("wrote "):]).read())["verdict"] == "GO"
