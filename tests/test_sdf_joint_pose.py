"""A tree joint's own <pose> (SDF: child-relative unless relative_to) places the child's CoM right.

Hand answer for arm.sdf: the joint frame sits at (0.2, 0, 1.1) turned 90 deg about z; the arm's CoM
is at (0.3, 0, 1.0) in the world, so (0.1, 0, -0.1) from the joint. Gravity (2 kg, g 9.81) about
the joint axis (world +y) is 2 * 9.81 * 0.1 = 1.962 N*m; the system CoG is (0.2, 0, 2/3).
"""

import pytest

from lockwire import statics
from lockwire.sdf import _frame_shifts, _link_poses, parse_sdf, read_sdf_root
from lockwire.urdf import parse_urdf

from conftest import FIXTURES

D = FIXTURES / "sdf_joint_pose"


@pytest.mark.parametrize("name", ["arm.sdf", "arm_relative.sdf", "arm.urdf"])
def test_posed_joint_matches_hand_answer(name):
    path = D / name
    model = parse_urdf(path) if name.endswith(".urdf") else parse_sdf(path)
    torque = statics.joint_gravity_torques(model)[0].torque_nm
    assert torque == pytest.approx(1.962, rel=1e-3)
    assert statics.system_com(model) == pytest.approx([0.2, 0.0, 2 / 3], abs=1e-4)


@pytest.mark.parametrize("name", ["fivebar.sdf", "fivebar_wide.sdf"])
def test_closed_loop_legs_are_never_shifted(name):
    model_el = read_sdf_root(FIXTURES / name)[1]
    assert _frame_shifts(model_el, _link_poses(model_el)) == {}


def _locked(tmp_path, com_of, mass_of=None):
    """A lock of arm.sdf whose links carry the given com (`{link: [x, y, z]}`) and mass, as an old lock would."""
    from ruamel.yaml import YAML
    from typer.testing import CliRunner

    from lockwire.cli import app

    model = tmp_path / "arm.sdf"
    model.write_bytes((D / "arm.sdf").read_bytes())
    lock = tmp_path / "lock.yaml"
    run = CliRunner().invoke(app, ["start", str(model), "--out", str(lock)])
    assert run.exit_code in (0, 1), run.output
    yaml = YAML()
    doc = yaml.load(lock.read_text())
    for link in doc["model"]["links"]:
        if link["name"] in com_of:
            link["com"] = com_of[link["name"]]
        if link["name"] in (mass_of or {}):
            link["mass"]["value"] = mass_of[link["name"]]
    with lock.open("w") as f:
        yaml.dump(doc, f)
    return model, lock, CliRunner().invoke(app, ["check", str(lock)])


def test_old_lock_below_posed_joint_says_relock(tmp_path):
    from typer.testing import CliRunner

    from lockwire.cli import app

    model, lock, result = _locked(tmp_path, {"arm": [0.3, 0.0, 0.0]})  # the old reader's wrong com
    text = " ".join(result.output.split())
    assert result.exit_code == 1 and "VIOLATION model/links/arm/com" in text
    assert "this lock may come from Lockwire before 0.5.3" in text
    assert "If you did edit it, review the change first." in text
    assert "fix the model file" not in text
    relock = f"lockwire start {model} --out {lock} --force"
    assert text.count(f"To re-lock: {relock}.") == 1
    assert CliRunner().invoke(app, relock.split()[1:]).exit_code in (0, 1)
    clean = " ".join(CliRunner().invoke(app, ["check", str(lock)]).output.split())
    assert "model/links/arm/com" not in clean and "before 0.5.3" not in clean


def test_drift_on_unposed_link_keeps_ordinary_message(tmp_path):
    _model, _lock, result = _locked(tmp_path, {"base": [0.5, 0.0, 0.0]})
    text = " ".join(result.output.split())
    assert "VIOLATION model/links/base/com" in text and "fix the model file" in text
    assert "before 0.5.3" not in text


def test_mass_drift_below_posed_joint_has_no_pose_note(tmp_path):
    _model, _lock, result = _locked(tmp_path, {}, {"arm": 5.0})  # the old reader never misread mass
    text = " ".join(result.output.split())
    assert "VIOLATION model/links/arm/mass" in text and "fix the model file" in text
    assert "before 0.5.3" not in text


def test_moved_joint_origin_below_posed_link_says_relock(tmp_path):
    from typer.testing import CliRunner

    from lockwire.cli import app

    model, lock, _ = _locked(tmp_path, {})
    model.write_text(model.read_text().replace("0.2 0 0.1 0 0", "0.2 0 0.5 0 0"))
    text = " ".join(CliRunner().invoke(app, ["check", str(lock)]).output.split())
    assert "model/structure_hash" in text and "this lock may come from Lockwire before 0.5.3" in text
    assert text.count("before 0.5.3") == 2  # the action and the one note
