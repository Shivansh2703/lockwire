from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lockwire import urdf
from lockwire.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "gripper_mimic.urdf"


def run(*args: object) -> tuple[int, str]:
    res = CliRunner().invoke(app, [str(a) for a in args])
    return res.exit_code, " ".join(res.output.split())


def lock(tmp_path: Path, old: bool = False) -> tuple[Path, Path]:
    model, out = tmp_path / "g.urdf", tmp_path / "lockwire.yaml"
    model.write_text(FIXTURE.read_text())
    run("start", model, "--out", out)
    doc = yaml.safe_load(out.read_text())
    if old:  # what start wrote before 0.5.2: the follower without limits and mimic
        follower = next(j for j in doc["model"]["joints"] if j["name"] == "k")
        del follower["limits"], follower["mimic"]
        out.write_text(yaml.safe_dump(doc))
    return model, out


def test_new_lock_records_the_follower_and_checks_clean_without_a_note(tmp_path):
    _, out = lock(tmp_path)
    code, text = run("check", out)
    assert "mimic:\n        joint: j" in out.read_text() and "upper: 1.0" in out.read_text()
    assert code == 0 and "written before follower" not in text


def test_old_lock_checks_clean_and_says_to_re_lock(tmp_path):
    model, out = lock(tmp_path, old=True)
    code, text = run("check", out)
    assert code == 0 and "written before follower joints kept their limits" in text
    assert f"lockwire start {model} --out {out} --force" in text


@pytest.mark.parametrize("old, joint", [(True, "j"), (False, "j"), (False, "k")])
def test_a_real_limit_change_after_locking_is_drift(tmp_path, old, joint):
    model, out = lock(tmp_path, old=old)
    line = next(x for x in model.read_text().splitlines() if f'joint name="{joint}"' in x)
    model.write_text(model.read_text().replace(line, line.replace('upper="1"', 'upper="2"')))
    assert run("check", out)[0] == 1


def test_export_writes_the_follower_limit_and_mimic_without_a_warning(tmp_path):
    _, out = lock(tmp_path)
    exported = tmp_path / "o.urdf"
    code, text = run("export", out, "--format", "urdf", "--out", exported)
    k = urdf.parse_urdf(exported).joints[1]
    assert code == 0 and "no effort or velocity limit" not in text
    assert (k.limits, k.mimic.joint) == (urdf.parse_urdf(FIXTURE).joints[1].limits, "j")


def test_a_robot_without_a_mimic_joint_pays_for_one_hash(tmp_path, monkeypatch):
    plain = tmp_path / "plain.urdf"
    plain.write_text(FIXTURE.read_text().replace('<mimic joint="j"/>', ""))
    monkeypatch.setattr(urdf, "locked_record_hash", lambda *a: pytest.fail("second hash"))
    assert urdf.legacy_follower_hash(None, urdf.read_urdf_topology(plain), False) is None
