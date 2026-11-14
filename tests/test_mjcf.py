"""`lockwire check` reads MJCF: Tier A model from <mujoco>, bounded includes, DOCTYPE refused."""

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.mjcf import parse_mjcf

from conftest import FIXTURES

TWO_LINK = FIXTURES / "unitree_like" / "two_link.xml"
runner = CliRunner()


def test_two_link_model():
    notes: list[str] = []
    model = parse_mjcf(TWO_LINK, notes)
    assert {k.name: k.mass.base() for k in model.links if k.mass} == {"base": 1.5, "arm": 0.5}
    _, joint = model.joints  # the free base floats on the world
    assert (joint.name, joint.type, joint.parent, joint.axis) == (
        "shoulder",
        "revolute",
        "base",
        (0.0, 1.0, 0.0),
    )
    assert (joint.limits.lower, joint.limits.upper) == (-1.0, 1.0)
    assert "actuators and gear" in notes[0]


@pytest.mark.parametrize(("mass", "code"), [("2 kg", 0), ("1 kg", 1)])
def test_check_mjcf_goes_or_not(mass, code):
    result = runner.invoke(app, ["check", str(TWO_LINK), "--mass", mass, "--source", "test"])
    assert result.exit_code == code, result.output


def test_include_stays_inside_the_model_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "outside.xml").write_text(TWO_LINK.read_text())
    (tmp_path / "sub" / "robot.xml").write_text(TWO_LINK.read_text())
    inside = tmp_path / "sub" / "scene.xml"
    inside.write_text('<mujoco><include file="robot.xml"/></mujoco>')
    assert {k.name for k in parse_mjcf(inside).links} == {"world", "base", "arm"}
    inside.write_text('<mujoco><include file="../outside.xml"/></mujoco>')
    with pytest.raises(ValueError, match="not a file inside"):
        parse_mjcf(inside)


def test_doctype_refused_and_other_files_named(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text('<!DOCTYPE x [<!ENTITY a "b">]><mujoco/>')
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_mjcf(bad)
    other = tmp_path / "notes.txt"
    other.write_text("hi")
    result = runner.invoke(app, ["check", str(other), "--mass", "1 kg"])
    assert result.exit_code == 2 and "not a model lockwire reads" in result.output


def test_locked_mjcf_catches_an_added_body(tmp_path):
    model, lock = tmp_path / "robot.xml", tmp_path / "lockwire.yaml"
    model.write_text(TWO_LINK.read_text())
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    assert runner.invoke(app, ["check", str(lock)]).exit_code == 0
    model.write_text(model.read_text().replace("</worldbody>", '<body name="tail"/></worldbody>'))
    result = runner.invoke(app, ["check", str(lock)])
    assert result.exit_code == 1 and "model/structure_hash" in result.output, result.output
    assert "tail" in result.output


@pytest.mark.parametrize(
    ("old", "new", "link", "values"),
    [
        ('pos="0 0 0" mass="1.5"', 'pos="0 0 0" mass="9.5"', "links/base/mass", ("9.5", "1.5")),
        ('pos="0 0 0.05" mass', 'pos="0 0 0.07" mass', "links/arm/com", ("0.07", "0.05")),
        ('diaginertia="0.01 ', 'diaginertia="0.09 ', "links/base/inertia", ("0.09", "0.01")),
        ('0" mass="1.5"', '0" quat="0.707 0.707 0 0" mass="1.5"', "links/base/inertia", ("0.03",)),
        ('0" mass="1.5"', '0" euler="0 0 90" mass="1.5"', "links/base/inertia", ("0.03",)),
        (
            '"arm" pos="0.1 0 0"',
            '"arm" pos="0.1 0 0" quat="0 0 0 1"',
            "structure_hash",
            ("shoulder",),
        ),
    ],
)
def test_locked_mjcf_catches_a_value_edit(tmp_path, old, new, link, values):
    model, lock = tmp_path / "robot.xml", tmp_path / "lockwire.yaml"
    model.write_text(TWO_LINK.read_text())
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    model.write_text(model.read_text().replace(old, new))
    result = runner.invoke(app, ["check", str(lock), "--all"])
    assert result.exit_code == 1 and f"model/{link}" in result.output, result.output
    assert all(v in result.output for v in values), result.output


TABLE = '<body name="table"><inertial pos="0 0 0" mass="5" diaginertia="0.5 0.6 0.7"/></body>'
MOVER = '<inertial pos="0 0 0" mass="1" diaginertia="0.01 0.02 0.03"/></body>'


@pytest.mark.parametrize("joint", ['<joint type="free"/>', '<joint axis="0 0 1" range="-1 1"/>'])
def test_unchanged_mjcf_lock_goes_and_names_what_it_skips(tmp_path, joint):
    model, lock = tmp_path / "scene.xml", tmp_path / "lockwire.yaml"
    model.write_text(
        f'<mujoco><worldbody>{TABLE}<body name="b">{joint}{MOVER}</worldbody></mujoco>'
    )
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    result = runner.invoke(app, ["check", str(lock), "--all"])
    assert result.exit_code == 0 and "model/mjcf: an MJCF lock does not" in result.output, (
        result.output
    )


FRAMED = (
    '<mujoco><worldbody><frame pos="0 0 0.5" {frame}><body name="b" pos="0.1 0 0">'
    '<joint axis="0 0 1" range="-1 1"/>' + MOVER + "</frame></worldbody></mujoco>"
)


def test_frame_wrapper_is_read_and_a_frame_edit_is_caught(tmp_path):
    model, lock = tmp_path / "robot.xml", tmp_path / "lockwire.yaml"
    model.write_text(FRAMED.format(frame=""))
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    assert runner.invoke(app, ["check", str(lock), "--all"]).exit_code == 0
    joint = next(j for j in parse_mjcf(model).joints if j.child == "b")
    assert joint.origin.xyz == pytest.approx((0.1, 0, 0.5))
    model.write_text(FRAMED.format(frame='euler="0 0 90"'))
    assert runner.invoke(app, ["check", str(lock), "--all"]).exit_code == 1
    rotated = next(j for j in parse_mjcf(model).joints if j.child == "b")
    assert (
        rotated.origin.xyz == pytest.approx((0, 0.1, 0.5), abs=1e-9) and rotated.origin.rpy[2] > 1.5
    )


def test_hinge_arm_on_a_table_has_no_world_mass_warning(tmp_path):
    model, lock = tmp_path / "scene.xml", tmp_path / "lockwire.yaml"
    model.write_text(
        f'<mujoco><worldbody>{TABLE}<body name="b"><joint axis="0 0 1" range="-1 1"/>'
        f"{MOVER}</worldbody></mujoco>"
    )
    runner.invoke(app, ["start", str(model), "--out", str(lock)])
    result = runner.invoke(app, ["check", str(lock), "--all"])
    assert result.exit_code == 0 and "links/world/mass" not in result.output, result.output


IN_FRAME = (
    '<mujoco><worldbody><body name="base" pos="0 0 1"><inertial pos="0 0 0" mass="2" '
    'diaginertia="1 1 1"/><frame pos="0.1 0.2 0.3" euler="10 20 30"><inertial pos="0.01 0 0" '
    'mass="3" diaginertia="1 2 3"/><joint name="j" axis="0 0 1" range="-1 1"/></frame></body>'
    "</worldbody></mujoco>"
)


@pytest.mark.parametrize(("old", "new"), [('mass="3"', 'mass="9"'), ('"-1 1"', '"-1 2"')])
def test_inertial_and_joint_inside_a_frame_are_locked(tmp_path, old, new):
    model, lock = tmp_path / "robot.xml", tmp_path / "lockwire.yaml"
    model.write_text(IN_FRAME)
    parsed = parse_mjcf(model)
    assert [k.mass.base() for k in parsed.links if k.mass] == [3.0], parsed.links
    assert [j.name for j in parsed.joints] == ["j"]
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    model.write_text(IN_FRAME.replace(old, new))
    assert runner.invoke(app, ["check", str(lock), "--all"]).exit_code == 1


@pytest.mark.parametrize("case", ["c", "d"])
def test_frame_float_noise_is_not_a_change(tmp_path, case):
    model, lock = tmp_path / "robot.xml", tmp_path / "lockwire.yaml"
    model.write_bytes((FIXTURES / f"mjcf_frame_{case}_framed.xml").read_bytes())
    assert runner.invoke(app, ["start", str(model), "--out", str(lock)]).exit_code == 0
    model.write_bytes((FIXTURES / f"mjcf_frame_{case}_flat.xml").read_bytes())
    assert runner.invoke(app, ["check", str(lock), "--all"]).exit_code == 0
