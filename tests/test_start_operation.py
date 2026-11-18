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
from lockwire import edit, scene_urdf, schema, start_operation


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
    replacement = api.start(
        source, overwrite=True, expected_revision=hashlib.sha256(changed).hexdigest()
    )
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
        assert (
            client.post("/v1/run", headers=headers, json=body).json()["payload"]["written"] is False
        )
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
        fixtures / "real_scenarios/official/anymal-d/meshes",
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
    gap = result["mesh_gap"]
    assert gap["missing"] == gap["total"] > 0 and gap["example"].startswith("package://")


def test_start_into_the_chosen_package_folder_pins_meshes_and_check_finds_them(tmp_path: Path):
    # The app's "Choose folder" fixer: a repo laid out as pkg/urdf + pkg/meshes, no package.xml.
    (tmp_path / "arm_pkg/urdf").mkdir(parents=True)
    (tmp_path / "arm_pkg/meshes").mkdir()
    (tmp_path / "arm_pkg/meshes/m.stl").write_text(_STL)
    urdf = tmp_path / "arm_pkg/urdf/arm.urdf"
    urdf.write_text(
        _ROS_URDF.format(a="package://arm_pkg/meshes/m.stl", b="package://arm_pkg/meshes/m.stl")
    )
    assert api.start(urdf)["mesh_gap"]["missing"] == 1
    body = {
        "command": "start",
        "args": {"urdf": str(urdf), "out": str(tmp_path / "arm_pkg/lockwire.yaml")},
    }
    with TestClient(create_app("token"), base_url="http://127.0.0.1") as client:
        run = lambda b: client.post("/v1/run", headers={"X-Lockwire-Token": "token"}, json=b).json()  # noqa: E731
        start = run(body)["payload"]
        assert start["written"] and "mesh_gap" not in start
        checked = run({"command": "check", "args": {"contract": start["path"]}})
    assert checked["refusal"] is None and checked["payload"]["scene"]["parts"]


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


@pytest.mark.parametrize(
    "a,b",
    [
        ("package://pkg/meshes/a.stl", "package://pkg/meshes/b.stl"),
        ("../meshes/a.stl", "../meshes/b.stl"),
    ],
)
def test_start_pins_meshes_in_a_sibling_folder_of_the_package(tmp_path: Path, a: str, b: str):
    urdf = _ros_package(tmp_path / "pkg", a, b)
    result = api.start(urdf)
    assert "note" not in result
    assert sorted(x.path for x in load_contract(Path(result["path"])).model.external_assets) == [
        "meshes/a.stl",
        "meshes/b.stl",
    ]
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


def test_start_takes_robot_name_from_the_urdf(tmp_path: Path):
    source = tmp_path / "file_stem.urdf"
    source.write_text('<robot name="anymal"><link name="base"/></robot>')
    api.start(source)
    assert load_contract(tmp_path / "lockwire.yaml").robot.name == "anymal"  # bites the file stem


def test_starters_list_and_blank_start_checks_not_scored(tmp_path: Path):
    headers = {"X-Lockwire-Token": "token"}
    with TestClient(create_app("token"), base_url="http://127.0.0.1") as client:
        run = lambda body: client.post("/v1/run", headers=headers, json=body).json()  # noqa: E731
        listed = run({"command": "starters", "args": {}})["payload"]["starters"]
        assert {"drone": "Drone", "rover": "Rover", "quadruped": "Quadruped"}.items() <= {
            s["name"]: s["title"] for s in listed
        }.items()
        # The bench is a starter, and wherever it shows it is labelled built to fail.
        titles = {s["name"]: s["title"] for s in listed}
        assert all(s["based_on"] for s in listed) and "built to fail" in titles["servo-bench"]
        out = tmp_path / "blank" / "lockwire.yaml"
        started = run({"command": "start", "args": {"blank": True, "out": str(out)}})["payload"]
        assert started["written"] is True and load_contract(out).robot.name == "New robot"
        assert (
            run({"command": "start", "args": {"blank": True, "out": str(out)}})["payload"][
                "written"
            ]
            is False
        )
        checked = run({"command": "check", "args": {"contract": str(out)}})
        # No model and no parts: nothing to compare, so honestly NOT SCORED (GO needs a model start).
        assert (
            checked["refusal"] is None
            and checked["exit_code"] == 3
            and checked["verdict"] == "NOT_SCORED"
        )


def test_blank_robot_page_says_what_to_do(tmp_path: Path):
    from lockwire.page import build_payload

    out = tmp_path / "lockwire.yaml"
    start_operation.start_blank(out)
    cards = build_payload(load_contract(out), contract_path=out)["cards"]
    assert "Nothing to check yet. Add a part to start checking." in [c["body"] for c in cards]


def test_example_start_copies_its_plan_beside_the_lockfile_and_keeps_an_existing_one(
    tmp_path: Path,
):
    from lockwire.start_operation import start_example

    kept = tmp_path / "mine.plan.yaml"
    kept.write_text("mine", encoding="utf-8")
    start_example("drone", tmp_path / "mine.yaml")
    start_example("drone", tmp_path / "bot.yaml")
    assert kept.read_text() == "mine"
    assert api.plan(tmp_path / "bot.yaml", tmp_path / "bot.plan.yaml")["steps"]


def test_start_example_keeps_a_users_glb_beside_the_output(tmp_path: Path):
    """a 9-byte user .glb became the example's 367,800-byte scene."""
    (tmp_path / "lockwire.glb").write_bytes(b"USER GLB\n")
    result = start_operation.start_example("drone", tmp_path / "lockwire.yaml")
    assert (tmp_path / "lockwire.glb").read_bytes() == b"USER GLB\n"
    assert result["written"] and "lockwire.glb" in result["note"]


def test_start_overwrite_keeps_file_mode(tmp_path: Path):
    """An overwrite keeps the contract's file mode. The lock file beside the contract is kept on
    purpose: it is the shared inode other editors wait on."""
    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    result = api.start(source)
    target = tmp_path / "lockwire.yaml"
    target.chmod(0o644)
    api.start(source, overwrite=True, expected_revision=result["revision"])
    assert target.stat().st_mode & 0o777 == 0o644


def _mass_lock(tmp_path: Path, monkeypatch, **options) -> str:
    monkeypatch.setattr("getpass.getuser", lambda: "zz-login")
    model = tmp_path / "robot.urdf"
    model.write_text('<robot name="robot"><link name="base"/></robot>')
    lock = start_operation.model_lock(model, tmp_path, mass="1 kg", **options)
    return lock.read_text()


def test_the_cli_mass_check_still_names_the_login_as_witness(tmp_path: Path, monkeypatch):
    assert "zz-login" in _mass_lock(tmp_path, monkeypatch)


def test_an_anonymous_mass_check_never_asks_for_the_login(tmp_path: Path, monkeypatch):
    assert "zz-login" not in _mass_lock(tmp_path, monkeypatch, anonymous=True)


@pytest.mark.parametrize("how", ["no_link", "eperm"])
def test_start_without_hard_links_still_never_replaces(tmp_path: Path, monkeypatch, how):
    import errno
    import os

    if how == "no_link":
        monkeypatch.delattr(os, "link")
    else:
        monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError(errno.EPERM, "")))
    source = tmp_path / "robot.urdf"
    source.write_text('<robot name="robot"><link name="base"/></robot>')
    api.start(source)
    target = tmp_path / "lockwire.yaml"
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        edit.link_new(source, target)
    assert target.read_bytes() == before


def _workspace(root: Path, mesh: str = "package://pkg_b/meshes/m.stl") -> Path:
    for pkg in ("pkg_a", "pkg_b"):
        (root / pkg).mkdir(parents=True)
        (root / pkg / "package.xml").write_text(f"<package><name>{pkg}</name></package>")
    (root / "pkg_b/meshes").mkdir()
    (root / "pkg_b/meshes/m.stl").write_text(_STL)
    (root / "pkg_a/robot.urdf").write_text(_ROS_URDF.format(a=mesh, b=mesh))
    return root / "pkg_a/robot.urdf"


def _check_text(lock: str) -> str:
    return CliRunner().invoke(app, ["check", lock, "--all"]).output


def test_a_sibling_package_mesh_is_recorded_checked_and_drift_is_caught(tmp_path: Path):
    result = api.start(_workspace(tmp_path))
    model = load_contract(Path(result["path"])).model
    assert model.asset_root == "workspace"
    assert [a.path for a in model.external_assets] == ["pkg_b/meshes/m.stl"]
    assert "were not found" not in _check_text(result["path"])
    (tmp_path / "pkg_b/meshes/m.stl").write_text(_STL.replace("vertex 1 0 0", "vertex 2 0 0"))
    assert "pkg_b/meshes/m.stl changed since lock" in _check_text(result["path"])


def test_a_single_package_lock_has_no_asset_root(tmp_path: Path):
    urdf = _ros_package(tmp_path / "pkg", "../meshes/a.stl", "../meshes/b.stl")
    assert "asset_root" not in Path(api.start(urdf)["path"]).read_text()


@pytest.mark.parametrize("mesh", ["package://pkg_b/../../out/m.stl", "package://pkg_b/meshes/link.stl"])
def test_a_mesh_leaving_the_sibling_folder_is_refused(tmp_path: Path, mesh: str):
    (tmp_path / "out").mkdir()
    (tmp_path / "out/m.stl").write_text(_STL)
    urdf = _workspace(tmp_path / "ws", mesh)
    (tmp_path / "ws/pkg_b/meshes/link.stl").symlink_to(tmp_path / "out/m.stl")
    result = api.start(urdf)
    assert "not found inside" in result["note"]
    assert load_contract(Path(result["path"])).model.external_assets == []


def test_a_missing_sibling_package_is_named_in_the_hint(tmp_path: Path):
    urdf = _workspace(tmp_path, "package://pkg_c/m.stl")
    assert "Looked for a folder of package 'pkg_c' in" in api.start(urdf)["note"]


def test_asset_root_round_trips(tmp_path: Path):
    lock = Path(api.start(_workspace(tmp_path))["path"])
    first = lock.read_bytes()
    schema.save_contract(load_contract(lock), lock)
    assert lock.read_bytes() == first


def _pin(lock: str, *paths: str, base: Path | None = None, root: str | None = None) -> None:
    import yaml

    d = yaml.safe_load(Path(lock).read_text())
    d["model"]["asset_root"] = root or d["model"].get("asset_root")
    d["model"]["external_assets"] += [{"path": p, "sha256": hashlib.sha256((base / p).read_bytes()).hexdigest() if base else "0" * 64} for p in paths]
    Path(lock).write_text(yaml.safe_dump(d))


def _reads(monkeypatch) -> list[str]:
    seen, real = [], Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: seen.append(self.name) or real(self))
    return seen


@pytest.mark.parametrize("asset", ["secrets.txt", "notpkg/.git/config"])
def test_a_hand_edited_workspace_asset_outside_the_packages_is_refused_unread(tmp_path, monkeypatch, asset):
    lock = api.start(_workspace(tmp_path))["path"]
    (tmp_path / "notpkg/.git").mkdir(parents=True)
    (tmp_path / asset).write_text("TOPSECRET")
    _pin(lock, asset, base=tmp_path)
    seen, out = _reads(monkeypatch), _check_text(lock)
    assert f"mesh {asset} is outside the robot's package folders" in out
    assert Path(asset).name not in seen and "TOPSECRET" not in out


def test_a_link_leaving_the_package_is_refused_but_one_staying_inside_is_compared(tmp_path: Path):
    lock = api.start(_workspace(tmp_path))["path"]
    (tmp_path / "pkg_b/meshes/inside.stl").symlink_to(tmp_path / "pkg_b/meshes/m.stl")
    (tmp_path / "pkg_b/meshes/out.stl").symlink_to(tmp_path / "pkg_a/robot.urdf")
    _pin(lock, "pkg_b/meshes/inside.stl", "pkg_b/meshes/out.stl", base=tmp_path)
    assert "mesh pkg_b/meshes/out.stl is a link that leaves the package folder" in _check_text(lock)
    (tmp_path / "pkg_b/meshes/m.stl").write_text(_STL.replace("vertex 1 0 0", "vertex 2 0 0"))
    assert "inside.stl changed since lock" in _check_text(lock)


def test_a_package_root_link_to_a_file_outside_is_refused(tmp_path: Path):
    lock = api.start(_ros_package(tmp_path / "pkg", "../meshes/a.stl", "../meshes/b.stl"))["path"]
    (tmp_path / "pkg/meshes/evil.stl").symlink_to("/etc/hosts")
    _pin(lock, "meshes/evil.stl", base=tmp_path / "pkg")
    assert "mesh meshes/evil.stl is a link that leaves the package folder" in _check_text(lock)


def test_a_folder_the_model_names_is_not_a_package_without_package_xml_or_model_config(tmp_path, monkeypatch):
    urdf = _workspace(tmp_path)
    (tmp_path / "notpkg").mkdir()
    (tmp_path / "notpkg/data.bin").write_text("TOPSECRET")
    urdf.write_text(_ROS_URDF.format(a="package://pkg_b/meshes/m.stl", b="package://notpkg/data.bin"))
    lock = api.start(urdf)
    assert "That folder needs a package.xml or model.config" in lock["note"]
    _pin(lock["path"], "notpkg/data.bin", base=tmp_path, root="workspace")  # a hand-made lock lists it
    seen = _reads(monkeypatch)
    assert "notpkg/data.bin is outside the robot's package folders" in _check_text(lock["path"])
    assert "data.bin" not in seen


def test_a_model_config_folder_is_a_package_and_a_linked_package_folder_is_accepted(tmp_path: Path):
    (tmp_path / "gz/meshes").mkdir(parents=True)
    (tmp_path / "gz/meshes/m.stl").write_text(_STL)
    (tmp_path / "gz/model.config").write_text("<model/>")
    urdf = _workspace(tmp_path / "ws", "package://gz/meshes/m.stl")
    shutil.copytree(tmp_path / "gz", tmp_path / "ws/gz")
    result = api.start(urdf)
    assert "note" not in result and "were not found" not in _check_text(result["path"])
    shutil.rmtree(tmp_path / "ws/gz")
    (tmp_path / "ws/gz").symlink_to(tmp_path / "gz")  # a link planted in the workspace already controls it
    assert "outside the robot's" not in _check_text(result["path"])


@pytest.mark.parametrize("bad", ["a\0b", "x" * 5000], ids=["nul", "long"])
@pytest.mark.parametrize("ws", [True, False], ids=["ws", "pkg"])
def test_an_unresolvable_asset_path_is_a_clean_refusal_in_check_and_scene(tmp_path, bad, ws):
    lock = api.start(_workspace(tmp_path) if ws else _ros_package(tmp_path / "pkg", "../meshes/a.stl", "../meshes/b.stl"))["path"]
    _pin(lock, ("pkg_b/meshes/" if ws else "meshes/") + bad)
    run = CliRunner().invoke(app, ["check", lock, "--all"])
    assert "cannot be resolved" in run.output and "bug in lockwire" not in run.output and run.exit_code == 1
    with pytest.raises(SceneRefusal, match="cannot be resolved"):
        api.load_scene(lock, None)


def test_a_missing_package_or_model_mesh_prints_the_layout_once_per_name(tmp_path: Path):
    pkg = tmp_path / "ws" / "arm_pkg"
    (pkg / "urdf").mkdir(parents=True)
    (pkg / "package.xml").write_text("<package><name>arm_pkg</name></package>")
    urdf = pkg / "urdf" / "arm.urdf"
    uris = ["package://hand/meshes/a.stl", "package://hand/meshes/b.stl", "model://box/m/c.dae"]
    hint = scene_urdf.sibling_hint(urdf, uris)
    ws = (tmp_path / "ws").resolve()
    assert hint.count("package://hand/meshes/a.stl expects") == 1 and "hand/meshes/b.stl" not in hint
    assert f"{ws}/hand/package.xml and {ws}/hand/meshes/a.stl" in hint
    assert f"{ws}/box/model.config and {ws}/box/m/c.dae" in hint
    (ws / "hand").mkdir()
    (ws / "hand" / "package.xml").write_text("<package/>")
    assert "hand" not in scene_urdf.sibling_hint(urdf, uris)  # resolves now: nothing to teach


def test_the_arm_power_label_is_no_longer_than_the_servo_bench_one():
    from lockwire.start_operation import example_label

    arm, bench = example_label("arm-power"), example_label("servo-bench")
    assert arm.startswith("arm-power (built to fail: ") and len(arm) <= len(bench)
