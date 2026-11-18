"""Machine output (JSON, csv, GitHub annotations) never carries the folder the lockfile sits in."""

import json
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app

URDF = """<robot name="r"><link name="base"><inertial><origin xyz="{com} 0 0"/><mass value="1"/>
<inertia ixx="0.01" iyy="0.02" izz="0.03" ixy="0" ixz="0" iyz="0"/></inertial>{mesh}</link></robot>"""


def _locked(tmp_path: Path, monkeypatch, mesh: str = "") -> Path:
    folder = tmp_path / "robot dir"
    folder.mkdir()
    monkeypatch.chdir(folder)
    (folder / "robot.urdf").write_text(URDF.format(com=0, mesh=mesh))
    assert CliRunner().invoke(app, ["start", "robot.urdf"]).exit_code == 0
    return folder


def _outputs() -> tuple[list[str], dict]:
    runner = CliRunner()
    runs = [runner.invoke(app, ["check", *fmt]) for fmt in (["--json", "-"], ["--format", "csv"], ["--format", "github"])]
    return [r.output for r in runs], json.loads(runs[0].stdout)


def test_drifted_model_output_has_no_folder(tmp_path, monkeypatch):
    folder = _locked(tmp_path, monkeypatch)
    (folder / "robot.urdf").write_text(URDF.format(com=0.1, mesh=""))
    out, doc = _outputs()
    assert all(str(tmp_path) not in o and str(tmp_path.resolve()) not in o for o in out)
    com = next(f for f in doc["findings"] if f["quantity"] == "model/links/base/com")
    assert (com["source"], com["next_command"]) == ("robot.urdf", "lockwire start robot.urdf --force")
    assert "file=robot.urdf::" in out[2]


def test_missing_mesh_output_has_no_folder(tmp_path, monkeypatch):
    _locked(tmp_path, monkeypatch, '<visual><geometry><mesh filename="meshes/gone.stl"/></geometry></visual>')
    out, doc = _outputs()
    assert all(str(tmp_path) not in o and str(tmp_path.resolve()) not in o for o in out)
    assert any(f["quantity"] == "model/meshes" and f["source"] == "robot.urdf" for f in doc["findings"])
