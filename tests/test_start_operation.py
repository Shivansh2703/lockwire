"""The desktop's start route shares the CLI writer and refuses stale replacement."""

import hashlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lockwire import api
from lockwire.api_payload import StartPayload
from lockwire.cli import app
from lockwire.scene import SceneRefusal
from lockwire.schema import load_contract
from lockwire.serve import create_app
from lockwire import start_operation


def test_start_does_not_replace_a_newly_appearing_lockfile(tmp_path: Path, monkeypatch):
    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    target = tmp_path / "lockwire.yaml"
    original_link = start_operation.os.link

    def racing_link(temporary, destination):
        target.write_bytes(b"concurrent writer\n")
        original_link(temporary, destination)

    monkeypatch.setattr(start_operation.os, "link", racing_link)
    with pytest.raises(ValueError, match="appeared during"):
        api.start(source)
    assert target.read_bytes() == b"concurrent writer\n"


def test_start_shared_writer_adjacent_existing_and_stale(tmp_path: Path):
    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    result = StartPayload.model_validate(api.start(source))
    target = tmp_path / "lockwire.yaml"
    assert result.path == str(target) and result.written
    assert load_contract(target).model.source == "robot.urdf"
    assert not (tmp_path / "lockwire-gui.html").exists()
    original = target.read_bytes()
    reused = api.start(source)
    assert reused == {**result.model_dump(exclude_none=True), "written": False}
    assert target.read_bytes() == original
    target.write_bytes(original + b"\n# another writer\n")
    changed = target.read_bytes()
    with pytest.raises(ValueError, match="changed since"):
        api.start(source, overwrite=True, expected_revision=result.revision)
    assert target.read_bytes() == changed
    with pytest.raises(ValueError, match="changed since"):
        api.start(source, overwrite=True)
    replacement = api.start(source, overwrite=True, expected_revision=hashlib.sha256(changed).hexdigest())
    assert replacement["written"] is True
    cli = CliRunner().invoke(app, ["start", str(source), "--out", str(target)])
    assert cli.exit_code == 0, cli.output
    assert "already exists" in cli.output and target.read_bytes() == original


def test_native_start_sidecar_refuses_stale_and_robot_write(tmp_path: Path):
    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    headers = {"X-Lockwire-Token": "token"}
    body = {"command": "start", "args": {"urdf": str(source)}}
    with TestClient(create_app("token"), base_url="http://127.0.0.1") as client:
        result = client.post("/v1/run", headers=headers, json=body).json()
        start = StartPayload.model_validate(result["payload"])
        assert start.written and result["refusal"] is None
        assert client.post("/v1/run", headers=headers, json=body).json()["payload"]["written"] is False
        target = Path(start.path)
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
        before = target.read_bytes()
        body["args"].update(overwrite=True, expected_revision=start.revision)
        refusal = client.post("/v1/run", headers=headers, json=body).json()
        assert refusal["exit_code"] == 2 and "changed since" in refusal["refusal"]
        assert target.read_bytes() == before
    with TestClient(create_app("token", robot=True), base_url="http://127.0.0.1") as client:
        refused = client.post("/v1/run", headers=headers, json=body).json()
        assert refused["refusal"] and target.read_bytes() == before


def test_start_pins_the_meshes_so_the_design_page_finds_the_robot(tmp_path: Path):
    fixtures = Path(__file__).parent / "fixtures"
    shutil.copy(fixtures / "anymal_d.urdf", tmp_path / "anymal_d.urdf")
    shutil.copytree(
        fixtures / "e5_10_real_scenarios/official/anymal-d/meshes",
        tmp_path / "anymal_d_simple_description/meshes",
    )
    result = api.start(tmp_path / "anymal_d.urdf")
    contract = load_contract(Path(result["path"]))
    assert contract.model.external_assets
    scene, source = api.load_scene(result["path"], None)
    assert scene.parts and "anymal_d.urdf" in source


def test_start_with_a_missing_mesh_says_so_and_still_writes(tmp_path: Path):
    shutil.copy(Path(__file__).parent / "fixtures/anymal_d.urdf", tmp_path / "anymal_d.urdf")
    result = api.start(tmp_path / "anymal_d.urdf")
    assert result["written"] and "mesh files not found" in result["note"]
    assert load_contract(Path(result["path"])).model.external_assets == []


_STL = "solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid t\n"
_ROS_URDF = (
    '<robot name="r"><link name="a"><visual><geometry><mesh filename="{a}"/></geometry></visual></link>'
    '<link name="b"><visual><geometry><mesh filename="{b}"/></geometry></visual></link>'
    '<joint name="j" type="fixed"><parent link="a"/><child link="b"/></joint></robot>'
)


def _ros_package(root: Path, a: str, b: str) -> Path:
    (root / "urdf").mkdir(parents=True)
    (root / "meshes").mkdir()
    (root / "package.xml").write_text("<package><name>pkg</name></package>")
    (root / "meshes/a.stl").write_text(_STL)
    (root / "meshes/b.stl").write_text(_STL)
    (root / "urdf/robot.urdf").write_text(_ROS_URDF.format(a=a, b=b))
    return root / "urdf/robot.urdf"


@pytest.mark.parametrize("a,b", [("package://pkg/meshes/a.stl", "package://pkg/meshes/b.stl"),
                                 ("../meshes/a.stl", "../meshes/b.stl")])
def test_start_pins_meshes_in_a_sibling_folder_of_the_package(tmp_path: Path, a: str, b: str):
    urdf = _ros_package(tmp_path / "pkg", a, b)
    result = api.start(urdf)
    assert "note" not in result
    assert sorted(x.path for x in load_contract(Path(result["path"])).model.external_assets) == [
        "meshes/a.stl", "meshes/b.stl"]
    scene, _ = api.load_scene(result["path"], None)
    assert scene.parts
    (tmp_path / "pkg/meshes/b.stl").write_text(_STL.replace("vertex 1 0 0", "vertex 2 0 0"))
    with pytest.raises(SceneRefusal, match="sha256"):
        api.load_scene(result["path"], None)


def test_start_does_not_pin_a_mesh_outside_the_package(tmp_path: Path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere/b.stl").write_text(_STL)
    urdf = _ros_package(tmp_path / "pkg", "../meshes/a.stl", "../../elsewhere/b.stl")
    result = api.start(urdf)
    assert "not found inside the robot's package" in result["note"]
    assert load_contract(Path(result["path"])).model.external_assets == []
