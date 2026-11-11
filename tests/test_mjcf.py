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
    (joint,) = model.joints  # the free base joint is not a joint
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
    assert {k.name for k in parse_mjcf(inside).links} == {"base", "arm"}
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
