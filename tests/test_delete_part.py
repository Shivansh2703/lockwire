"""Deleting a part takes its body, weight and joint together; a floating body warns."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lockwire import export_model
from lockwire.checks import check_floating_mass
from lockwire.cli import app
from lockwire.edit import EditRefusal, add_entry, contract_revision, delete_entry, undo_edit
from lockwire.schema import load_contract
from lockwire.serve import create_app

ROVER = Path(__file__).resolve().parent.parent / "src/lockwire/examples/rover-param/contract.yaml"
WHEEL = "wheel-front-left"
TOKEN = "t"
_BLANK = 'schema_version: "0.7"\nrobot:\n  name: r\n'


def _rover(tmp_path: Path) -> Path:
    path = tmp_path / "lockwire.yaml"
    shutil.copy(ROVER, path)
    return path


def _total(path: Path) -> float:
    return sum(k.mass.base() for k in load_contract(path).model.links if k.mass)


def _check(path: Path):
    return CliRunner().invoke(app, ["check", str(path)], terminal_width=200)


def _serve_delete(path: Path, kind: str, name: str) -> dict:
    client = TestClient(create_app(TOKEN), base_url="http://127.0.0.1")
    args = {"contract": str(path), "kind": kind, "name": name, "source": "s", "witness": "w1"}
    args["expected_revision"] = contract_revision(path)
    body = {"command": "delete", "args": args}
    return client.post("/v1/run", headers={"X-Lockwire-Token": TOKEN}, json=body).json()


def test_leaf_wheel_delete_drops_its_weight_joint_and_keeps_export_and_verdict(tmp_path):
    path = _rover(tmp_path)
    before_mass, before = _total(path), _check(path)
    wheel = next(k for k in load_contract(path).model.links if k.name == WHEEL).mass.base()
    reply = _serve_delete(path, "link", WHEEL)
    assert reply["refusal"] is None and reply["payload"]["written"] is True
    model = load_contract(path).model
    assert WHEEL not in {k.name for k in model.links}
    assert WHEEL not in {j.child for j in model.joints}
    assert _total(path) == pytest.approx(before_mass - wheel)
    after = _check(path)
    assert after.exit_code == before.exit_code and "hangs off no joint" not in after.output
    files, _ = export_model.write_stl(model, tmp_path / "stl")
    assert files


def test_engine_delete_then_undo_restores_the_bytes(tmp_path):
    path = _rover(tmp_path)
    original = path.read_bytes()
    delete_entry(path, "link", WHEEL, source="s", witness="w1")
    assert path.read_bytes() != original
    undo_edit(path)
    assert path.read_bytes() == original


def test_link_with_children_is_refused_and_file_unchanged(tmp_path):
    path = _rover(tmp_path)
    add_entry(path, "link", {"name": "hub", "mass": "1 kg"}, source="s", witness="w1")
    joint = {"name": "hub_joint", "type": "fixed", "parent": WHEEL, "child": "hub"}
    add_entry(path, "joint", joint, source="s", witness="w1")
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match=rf"{WHEEL} holds 1 part \(hub by joint hub_joint\); delete or re-attach it first"):
        delete_entry(path, "link", WHEEL, source="s", witness="w1")
    assert path.read_bytes() == before


def test_deleting_only_the_joint_of_a_weighty_link_is_refused(tmp_path):
    path = _rover(tmp_path)
    joint = next(j.name for j in load_contract(path).model.joints if j.child == WHEEL)
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match=rf"deleting {joint} would leave {WHEEL} floating"):
        delete_entry(path, "joint", joint, source="s", witness="w1")
    assert path.read_bytes() == before


def test_a_device_mounted_on_the_link_blocks_the_delete(tmp_path):
    path = _rover(tmp_path)
    device = f"devices:\n- id: dev.cam\n  model: m\n  owner: Pat\n  physical:\n    mount_frame: {WHEEL}\n"
    path.write_text(path.read_text().replace("devices: []\n", device))
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match=r"re-mount or delete it first"):
        delete_entry(path, "link", WHEEL, source="s", witness="w1")
    assert path.read_bytes() == before


def test_references_to_the_deleted_link_leave_with_it(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text(_BLANK)
    for name in ("base", "arm"):
        add_entry(path, "link", {"name": name, "mass": "1 kg"}, source="s", witness="w1")
    joint = {"name": "elbow", "type": "fixed", "parent": "base", "child": "arm"}
    add_entry(path, "joint", joint, source="s", witness="w1")
    path.write_text(
        path.read_text()
        + "collision_pairs:\n- {link1: base, link2: arm, reason: touch}\ncontacts:\n- {link: arm, xyz: [0, 0, 0]}\n"
    )
    delete_entry(path, "link", "arm", source="s", witness="w1")
    text = path.read_text()
    assert "collision_pairs" not in text and "contacts" not in text and "elbow" not in text


def test_floating_mass_warns_on_a_detached_massive_link_and_is_silent_otherwise(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text(_BLANK)
    for name, mass in (("base", "1 kg"), ("arm", "1 kg"), ("loose", "2 kg"), ("frame", "0 kg")):
        add_entry(path, "link", {"name": name, "mass": mass}, source="s", witness="w1")
    joint = {"name": "elbow", "type": "fixed", "parent": "base", "child": "arm"}
    add_entry(path, "joint", joint, source="s", witness="w1")
    found = check_floating_mass(load_contract(path))
    assert [f.quantity for f in found] == ["model/links/loose/mass"] and "2 kg" in found[0].observed
    shown = CliRunner().invoke(app, ["check", str(path), "--all"], terminal_width=200).output
    assert "loose" in shown and "hangs off no joint" in shown
    assert found[0].severity == "warning" and found[0].action == "attach it with a joint or delete it"
