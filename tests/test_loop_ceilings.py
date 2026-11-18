"""What a closed loop must look like to be scored: each case below breaks one rule on the
five-bar of fivebar_wide.sdf and must give no torque, one plain not-scored line, no exception."""

from pathlib import Path

import pytest

from lockwire import checks
from lockwire.checks import run_record_checks
from lockwire.schema import Contract, RobotInfo
from lockwire.sdf import parse_sdf

WIDE = (Path(__file__).parent / "fixtures/fivebar_wide.sdf").read_text()
CLOSURE_END = "<axis><xyz>0 1 0</xyz></axis>\n    </joint>\n  </model>"
ELBOW_R = '<joint name="elbow_r" type="revolute">'
YAW = (
    '<link name="root"/><joint name="hip_yaw" type="revolute"><parent>root</parent><child>base</child>'
    '<axis><xyz>0 0 1</xyz><limit><lower>-1</lower><upper>1</upper><effort>9</effort></limit></axis></joint>'
)
FOOT = (
    '<link name="foot"><pose>-0.06 0 -0.2 0 0 0</pose></link><joint name="ankle" type="revolute">'
    '<parent>lower_l</parent><child>foot</child><axis><xyz>0 1 0</xyz><limit><lower>-1</lower>'
    "<upper>1</upper><effort>3</effort></limit></axis></joint></model>"
)
CASES = {  # name -> (edits to the file, words the line must hold)
    "above": ([('<link name="base"/>', '<link name="base"/>' + YAW)], ["moving joint", "hip_yaw", "fixed joint"]),
    "below": ([("</model>", FOOT)], ["ankle", "nothing moves below", "fixed joint"]),
    "not_flat": ([("<parent>crank_r</parent><child>lower_r</child>\n      <axis><xyz>0 1 0", "<parent>crank_r</parent><child>lower_r</child>\n      <axis><xyz>1 0 0")], ["not flat", "parallel"]),
    "slide_in_loop": ([(ELBOW_R, ELBOW_R.replace("revolute", "prismatic"))], ["prismatic", "elbow_r", "revolute pin"]),
    "closure_ball": ([('name="closure" type="revolute"', 'name="closure" type="ball"'), (CLOSURE_END, "</joint>\n  </model>")], ["ball joint", "revolute pin"]),
    "closure_driven": ([(CLOSURE_END, "<axis><xyz>0 1 0</xyz><limit><lower>-3</lower><upper>3</upper><effort>2</effort></limit></axis>\n    </joint>\n  </model>")], ["closure", "effort 0"]),
    "posed_joint": ([('<joint name="a" type="revolute">', '<joint name="a" type="revolute"><pose>0 0 0.01 0 0 0</pose>')], ["'a'", "<pose>", "move the link"]),
    "two_pose_less_parents": ([("<pose>0.03125 0 -0.09499177596 0 0 0</pose>\n      <axis>", "<axis>")], ["lower_l", "several joints end", "no <pose>"]),
    "no_motor": ([("<effort>5</effort>", "")], ["no joint of the loop", "effort limit"]),
    "extra_motor": ([("<upper>3.1</upper><velocity>20", "<upper>3.1</upper><effort>1</effort><velocity>20")], ["exactly 2", "4 have an effort limit"]),
    "never_closes": ([("<lower>-1.5</lower><upper>1.5</upper>", "<lower>0.5</lower><upper>1.5</upper>"), ("<lower>-3.1</lower><upper>3.1</upper>", "<lower>-0.01</lower><upper>0.01</upper>")], ["cannot close at any pose", "joint limits"]),
}  # fmt: skip


def run(tmp_path, text):
    (tmp_path / "m.sdf").write_text(text)
    model = parse_sdf(tmp_path / "m.sdf")
    return run_record_checks(
        Contract(robot=RobotInfo(name="p"), model=model), contract_path=tmp_path / "lockwire.yaml"
    )


def refused(tmp_path, text):
    got = run(tmp_path, text)
    assert not [r for r in got.scored_records if "/joints/" in r.quantity]  # no torque number
    return " ".join(f.observed for f in got if f.severity == "not_scored")


@pytest.mark.parametrize("name", CASES)
def test_loop_ceiling_gives_no_number_and_a_plain_reason(tmp_path, name):
    edits, words = CASES[name]
    text = WIDE
    for old, new in edits:
        assert old in text
        text = text.replace(old, new)
    said = " ".join(refused(tmp_path, text).split())
    assert "gravity reach not scored" in said and "closed chain" in said
    assert all(w in said for w in words), said


def test_loop_with_too_many_driven_joints_to_sweep_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "_LOOP_MAX_POSES", 10)  # two driven joints are 49 poses
    assert "more than the 1 Lockwire can sweep" in refused(tmp_path, WIDE)


def test_a_loop_beside_an_idle_loop_scores_and_names_the_idle_one(tmp_path):
    idle = WIDE[WIDE.index('<link name="crank_r">') : WIDE.index("</model>")]
    for n in ("crank_r", "crank_l", "lower_r", "lower_l", "a", "b", "elbow_r", "elbow_l", "closure"):
        idle = idle.replace(f'"{n}"', f'"{n}2"').replace(f">{n}<", f">{n}2<")
    got = run(tmp_path, WIDE.replace("</model>", idle.replace("<effort>5</effort>", "") + "</model>"))
    said = " ".join(f.observed for f in got if f.severity == "not_scored")
    assert {r.quantity for r in got.scored_records} >= {"model/joints/a/limits/effort"}
    assert "no joint of the loop (a2, b2, elbow_r2, elbow_l2)" in said


MJCF = """<mujoco><worldbody>
<body name="crank" pos="0 0 1"><inertial pos="0.05 0 0" mass="1" diaginertia="1 1 1"/>
 <joint name="a" axis="0 1 0" range="-60 60"/>
 <body name="coupler" pos="0.1 0 0"><inertial pos="0.1 0 0" mass="1" diaginertia="1 1 1"/><joint name="p" axis="0 1 0"/></body></body>
<body name="rocker" pos="0.2 0 1"><inertial pos="0 0 -0.1" mass="1" diaginertia="1 1 1"/><joint name="q" axis="0 1 0"/></body>
<body name="arm" pos="1 0 1"><inertial pos="0.1 0 0" mass="1" diaginertia="1 1 1"/><joint name="s" axis="0 1 0" range="-60 60"/></body>
</worldbody>%s<actuator><motor joint="a" forcerange="-5 5"/><motor joint="s" forcerange="-5 5"/></actuator></mujoco>"""


def test_mjcf_equality_loop_gets_no_torque_but_a_free_arm_beside_it_does(tmp_path):
    """An MJCF four-bar closed by <equality> used to score its crank as an open tree (2.45 N*m for
    this one, ignoring the closure). The arm 1 m away is no part of the loop and keeps its number."""
    plain, tied = (MJCF % x for x in ("", '<equality><connect body1="coupler" body2="rocker" anchor="0.2 0 0"/></equality>'))
    for text, loop in ((plain, True), (tied, False)):
        (tmp_path / "m.xml").write_text(text)
        got = checks.gravity_loads(
            Contract(robot=RobotInfo(name="p"), model=__import__("lockwire.mjcf").mjcf.parse_mjcf(tmp_path / "m.xml")),
            tmp_path / "lockwire.yaml",
        )
        assert ("a" in got) is loop and "s" in got


TIES = {
    "connect": '<equality><connect body1="coupler" body2="rocker" anchor="0.2 0 0"/></equality>',
    "weld": '<equality><weld body1="coupler" body2="rocker"/></equality>',
    "joint": '<equality><joint joint1="a" joint2="p"/></equality>',
}


def tied_contract(tmp_path, tie):
    """The four-bar (a, p, q) with a free arm (s), every moving joint limited to 0.2 N*m and
    given a speed; tie "plain" is the same file with no <equality>."""
    from lockwire.mjcf import parse_mjcf
    from lockwire.schema import JointLimits

    (tmp_path / f"{tie}.xml").write_text(MJCF % TIES.get(tie, ""))
    m = parse_mjcf(tmp_path / f"{tie}.xml")
    lim = JointLimits(lower=-1, upper=1, effort=0.2, velocity=2, acceleration=5)
    joints = [j.model_copy(update={"limits": lim}) if j.type != "fixed" else j for j in m.joints]
    return Contract(robot=RobotInfo(name="p"), model=m.model_copy(update={"joints": joints}))


@pytest.mark.parametrize("tie", ["connect", "weld", "joint"])
def test_hold_torque_gives_the_tied_joints_no_number_and_the_free_arm_its_own(tmp_path, tie):
    from lockwire.plan import Plan, Segment

    plan = Plan(segments=[Segment(name="r", pose={"a": 0.5, "s": 0.5})])
    plain = checks.check_hold_torque(tied_contract(tmp_path, "plain"), plan)
    got = checks.check_hold_torque(tied_contract(tmp_path, tie), plan)
    by = lambda fs: {f.quantity: f.observed for f in fs if f.severity == "warning"}  # noqa: E731
    assert "plan/segments/r/hold_torque/a" in by(plain)
    assert set(by(got)) == {"plan/segments/r/hold_torque/s"} and by(got).items() <= by(plain).items()
    assert "hold torque not scored: a: the MJCF" in " ".join(f.observed for f in got)


NAMES = {"a": "crankj", "p": "couplerj", "q": "rockerj", "s": "armj"}


def real_lock(tmp_path, monkeypatch):
    # A REAL lock: `lockwire start robot.xml` beside the tied four-bar (crankj, couplerj, rockerj and
    # the free armj), so the lock names its model by the relative path `robot.xml`.
    import re

    from typer.testing import CliRunner

    from lockwire.cli import app

    (tmp_path / "robot.xml").write_text(re.sub('="([apqs])"', lambda m: f'="{NAMES[m[1]]}"', MJCF % TIES["connect"]))
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["start", "robot.xml", "--no-check"]).exit_code == 0
    return CliRunner(), app, tmp_path / "lockwire.yaml"


def test_check_strict_on_a_tied_mjcf_lock_gives_no_warning_about_its_relative_path(tmp_path, monkeypatch):
    runner, app, lock = real_lock(tmp_path, monkeypatch)
    out = runner.invoke(app, ["check", str(lock), "--strict", "--json", "-"]).output
    assert "gravity reach not scored: couplerj, crankj, rockerj" in out and "provenance" not in out


def test_an_mjcf_tie_removed_in_place_within_one_clock_tick_is_not_served_from_the_cache(tmp_path, monkeypatch):
    import os

    from lockwire.schema import load_contract
    from lockwire.statics import tied_joints

    _runner, _app, lock = real_lock(tmp_path, monkeypatch)
    xml = tmp_path / "robot.xml"
    assert tied_joints(load_contract(lock).model)
    old, text = xml.stat(), xml.read_text()
    untied = text.replace(TIES["connect"], " " * len(TIES["connect"]))  # same size, no tie
    xml.write_text(untied)
    os.utime(xml, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert (xml.stat().st_mtime_ns, xml.stat().st_size) == (old.st_mtime_ns, old.st_size)
    assert tied_joints(load_contract(lock).model) == frozenset()
