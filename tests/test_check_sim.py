"""`lockwire check-sim`: a sim file (MJCF/SDF) read beside the lock, value by value.

Fixtures in fixtures/so101/: `lockwire.yaml` (three links cut from so101.urdf plus a
battery), `so101_drift.xml` (MJCF, shoulder mass +15 %, shoulder_pan range narrowed),
`so101.sdf` (agrees with the lock), `so101_density.xml` (mass from geom density).
"""

import json

from conftest import FIXTURES
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.mcp_server import _with_cli_line
from test_mcp_server import _call

SO101 = FIXTURES / "so101"
LOCK = str(SO101 / "lockwire.yaml")


def _run(sim: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, ["check-sim", str(SO101 / sim), "--lock", LOCK, "--json", "-"])
    return result.exit_code, json.loads(result.stdout)


def test_mass_drift_is_a_violation_naming_the_body_and_both_values():
    code, doc = _run("so101_drift.xml")
    assert code == 1 and doc["verdict"] == "NO-GO"
    by = {f["quantity"]: f for f in doc["findings"] if f["severity"] == "violation"}
    mass = by["model/links/shoulder_link/mass"]  # sim "shoulder" matched by suffix
    assert "<body name='shoulder'>" in mass["observed"] and "0.115007 kg" in mass["observed"]
    assert mass["expected"] == "lock says 0.100006 kg"
    assert (mass["next_command"], mass["next_tool"]) == (
        f"lockwire check-sim {SO101 / 'so101_drift.xml'} --lock {LOCK}",
        "check_sim_config",
    )
    # range is in degrees by MJCF default: -90 deg read as -1.5708 rad, not -90 rad
    assert "-1.5708" in by["model/joints/shoulder_pan/lower"]["observed"]
    assert len(by) == 3  # mass, lower, upper; matching inertia/COM/effort stay quiet


def test_matching_sim_is_go_and_lists_what_the_format_cannot_express():
    code, doc = _run("so101.sdf")
    assert code == 0 and doc["verdict"] == "GO" and doc["scored"] > 0
    assert [r["quantity"] for r in doc["cannot_express"]] == ["topology/battery"]


def test_density_mass_is_not_scored_and_unmatched_names_are_listed():
    _code, doc = _run("so101_density.xml")
    rows = {r["quantity"]: r["reason"] for r in doc["not_compared"]}
    assert rows["model/links/base_link/mass"] == "not compared: mass from density"
    assert "sim/unmatched/sim_body/camera_mount" in rows
    assert "sim/unmatched/locked_link/upper_arm_link" in rows


def test_mcp_tool_returns_the_cli_document():
    sim = str(SO101 / "so101_drift.xml")
    sc = _call("check_sim_config", {"path": sim, "lock": LOCK}).structured_content
    _code, doc = _run("so101_drift.xml")
    assert sc.pop("cli_line") == f"lockwire check-sim {sim} --lock {LOCK}"
    assert sc == {k: v for k, v in _with_cli_line(doc, "").items() if k != "cli_line"}


def _joint_doc(tmp_path, defaults="", angle="", body="", joint="", actuator=""):
    xml = (
        f'<mujoco>{angle}{defaults}<worldbody><body name="shoulder"{body}>'
        f'<joint name="shoulder_pan" {joint}/></body></worldbody>'
        f"<actuator>{actuator}</actuator></mujoco>"
    )
    (tmp_path / "s.xml").write_text(xml)
    return _run(str(tmp_path / "s.xml"))[1]


def _violations(doc):
    return {f["quantity"] for f in doc["findings"] if f["severity"] == "violation"}


def test_forcerange_is_multiplied_by_gear_to_joint_effort(tmp_path):
    # MuJoCo XML reference (actuator/general: gear, forcerange), read 2026-11-05
    act = '<motor joint="shoulder_pan" gear="2 0 0 0 0 0" forcerange="-5 5"/>'
    assert "model/joints/shoulder_pan/effort" not in _violations(_joint_doc(tmp_path, actuator=act))


def test_joint_range_from_a_default_class_is_compared(tmp_path):
    # MuJoCo XML reference (default; body/childclass), read 2026-11-05: 110 deg is the lock's
    d = '<default><default class="arm"><joint range="-90 90"/></default></default>'
    doc = _joint_doc(tmp_path, defaults=d, body=' childclass="arm"')
    assert "model/joints/shoulder_pan/lower" in _violations(doc)
    doc = _joint_doc(tmp_path, defaults=d, joint='class="arm"')
    assert "model/joints/shoulder_pan/lower" in _violations(doc)


def test_radian_compiler_reads_ranges_unchanged(tmp_path):
    # MuJoCo XML reference (compiler/angle), read 2026-11-05
    doc = _joint_doc(tmp_path, angle='<compiler angle="radian"/>', joint='range="-1.91986 1.91986"')
    assert "model/joints/shoulder_pan/lower" not in _violations(doc)
    doc = _joint_doc(tmp_path, angle='<compiler angle="radian"/>', joint='range="-1.5 1.5"')
    assert (
        "sim file says -1.5"
        in next(f for f in doc["findings"] if f["quantity"].endswith("/lower"))["observed"]
    )


def test_agreeing_sim_prints_one_line_and_the_lock_is_found_beside_it(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no lockwire.yaml here: the one beside the sim file is used
    result = CliRunner().invoke(app, ["check-sim", str(SO101 / "so101.sdf")])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "lockwire: so101.sdf agrees with lockwire.yaml (23 values compared)"
    ]


def test_drifted_sim_still_names_each_mismatch():
    result = CliRunner().invoke(app, ["check-sim", str(SO101 / "so101_drift.xml"), "--lock", LOCK])
    assert result.exit_code == 1 and "agrees with" not in result.output
    assert "VIOLATION model/links/shoulder_link/mass" in result.output
