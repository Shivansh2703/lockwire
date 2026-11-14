"""A model stored in the lockfile (model.source == "lockfile") opens no model file.

An imported URDF still drifts when its file changes:
tests/test_checks.py::test_urdf_drift_added_link_names_the_link.
"""

import json

import yaml
from typer.testing import CliRunner

from lockwire.cli import app

runner = CliRunner()
URDF = """<robot name="r">
  <link name="base"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
  <link name="arm"><inertial><mass value="{arm}"/>
    <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.02" iyz="0" izz="0.025"/></inertial></link>
  <joint name="elbow" type="revolute"><parent link="base"/><child link="arm"/>
    <axis xyz="0 0 1"/><limit lower="-1" upper="1" effort="3" velocity="2"/></joint>
</robot>
"""


def _check(tmp_path, monkeypatch, provenance="lockfile"):
    """Lock a two-link one-joint robot, move its model into the lockfile, then check it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.urdf").write_text(URDF.format(arm=2))
    assert runner.invoke(app, ["start", "r.urdf", "--force"]).exit_code == 0
    data = yaml.safe_load((tmp_path / "lockwire.yaml").read_text())
    data["model"]["source"], data["model"]["provenance"]["source"] = "lockfile", provenance
    (tmp_path / "lockwire.yaml").write_text(yaml.safe_dump(data))
    # the old file moves on; the lock must not care
    (tmp_path / "r.urdf").write_text(URDF.format(arm=3))
    result = runner.invoke(app, ["check", "lockwire.yaml", "--json", "out.json"])
    assert result.exit_code != 2 and isinstance(result.exception, SystemExit | None), result.output
    return json.loads((tmp_path / "out.json").read_text())["findings"]


def test_lockfile_model_has_no_structure_drift_finding(tmp_path, monkeypatch):
    assert not [f for f in _check(tmp_path, monkeypatch) if f["quantity"] == "model/structure_hash"]


def test_lockfile_model_is_not_told_it_names_no_model_file(tmp_path, monkeypatch):
    assert not [f for f in _check(tmp_path, monkeypatch) if f["quantity"] == "model/sanity"]


def test_lockfile_model_is_never_told_to_relock_a_file_named_lockfile(tmp_path, monkeypatch):
    found = [f for f in _check(tmp_path, monkeypatch) if "placeholder_inertia" in f["quantity"]]
    assert found and "lockfile" not in (found[0].get("next_command") or "")


def test_lockfile_model_ignores_the_urdf_its_provenance_still_names(tmp_path, monkeypatch):
    findings = _check(tmp_path, monkeypatch, provenance="r.urdf")
    assert not [f for f in findings if f["quantity"].startswith("model/links/arm")]
