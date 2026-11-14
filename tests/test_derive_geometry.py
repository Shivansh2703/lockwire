"""A named dimension (a param) moves the model; mass is derived only when undeclared.

The arm is stored in the lockfile (model.source "lockfile"); its upper arm is a cylinder as long
as the param ``arm_length``, and the elbow sits that far out.
"""

import json
from io import StringIO

import pytest
import yaml
from ruamel.yaml import YAML
from typer.testing import CliRunner

from lockwire import schema
from lockwire.cli import app
from lockwire.schema import ContractError, load_contract, save_contract

runner = CliRunner()
URDF = """<robot name="r">
  <link name="base"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
  <link name="arm"><inertial><mass value="2"/>
    <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.02" iyz="0" izz="0.025"/></inertial></link>
  <link name="fore"><inertial><mass value="1.5"/><origin xyz="0 0 0.1"/>
    <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.02" iyz="0" izz="0.025"/></inertial></link>
  <joint name="shoulder" type="revolute"><parent link="base"/><child link="arm"/>
    <axis xyz="0 1 0"/><limit lower="-1" upper="1" effort="0.1" velocity="2"/></joint>
  <joint name="elbow" type="revolute"><parent link="arm"/><child link="fore"/>
    <origin xyz="0 0 0.2"/><axis xyz="0 1 0"/><limit lower="-1" upper="1" effort="0.1" velocity="2"/>
  </joint>
</robot>
"""
CYLINDER = {"kind": "cylinder", "radius": 0.02, "length": "arm_length"}


def _arm_text(
    tmp_path,
    monkeypatch,
    length=0.2,
    density=True,
    shape=None,
    unit=None,
    elbow="arm_length",
    arm_extra=None,
):
    """A lockfile text, in save order: the arm has a param-length cylinder and no mass."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.urdf").write_text(URDF)
    assert runner.invoke(app, ["start", "r.urdf", "--force"]).exit_code == 0
    yml = YAML()
    data = yml.load((tmp_path / "lockwire.yaml").read_text())
    data["model"]["source"] = "lockfile"
    arm = data["model"]["links"][1]
    for key in ("mass", "com", "inertia"):
        del arm[key]
    if density:
        arm["material"] = {"name": "aluminium", "density_kg_m3": 2700.0, "source": "alloy sheet"}
    arm.update(arm_extra or {})
    arm["geometry"] = [shape or {**CYLINDER, "origin": {"xyz": [0.0, 0.0, "arm_length"]}}]
    data["model"]["joints"][1]["origin"]["xyz"] = [0.0, 0.0, elbow]
    data["model"]["zero_mass_links"] = []
    param = {"name": "arm_length", "value": length}
    data["params"] = [{**param, "unit": unit} if unit else param]
    out = StringIO()
    yml.dump(data, out)
    return out.getvalue()


def _write(tmp_path, monkeypatch, **kw):
    path = tmp_path / "arm.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, **kw))
    return path


def _gravity(tmp_path, path):
    """The shoulder's gravity demand as `lockwire check` prints it, N*m."""
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["check", str(path), "--json", str(out)])
    assert result.exit_code != 2, result.output
    found = [
        f
        for f in json.loads(out.read_text())["findings"]
        if f["quantity"] == "model/joints/shoulder/limits/effort"
    ]
    assert found, result.output
    return float(found[0]["observed"].split()[0])


def test_a_param_name_resolves_to_its_value_in_shapes_and_joint_origins(tmp_path, monkeypatch):
    c = load_contract(_write(tmp_path, monkeypatch, length=0.3))
    assert c.model.joints[1].origin.xyz == (0.0, 0.0, 0.3)
    assert c.model.links[1].geometry[0].length == 0.3


def test_a_param_unit_converts_to_metres_and_a_wrong_unit_is_refused(tmp_path, monkeypatch):
    c = load_contract(_write(tmp_path, monkeypatch, length=300.0, unit="mm"))
    assert c.model.joints[1].origin.xyz[2] == pytest.approx(0.3)
    bad = _write(tmp_path, monkeypatch, unit="kg")
    with pytest.raises(ContractError, match="unit 'kg' is not a length"):
        load_contract(bad)


def test_a_joint_origin_naming_an_undeclared_param_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "bad.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, elbow="nope"))
    with pytest.raises(ContractError, match="joint 'elbow' uses param 'nope'"):
        load_contract(path)


def test_changing_a_param_leaves_the_structure_hash_and_is_not_a_hash_mismatch(
    tmp_path, monkeypatch
):
    path = _write(tmp_path, monkeypatch)
    before = load_contract(path).model.structure_hash
    path.write_text(path.read_text().replace("value: 0.2", "value: 0.5"))
    after = load_contract(path)
    assert after.model.structure_hash == before
    assert after.model.joints[1].origin.xyz[2] == 0.5


def test_declaring_a_param_value_then_loading_is_not_refused(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    declared = runner.invoke(
        app,
        [
            "declare",
            str(path),
            "params/arm_length/value",
            "0.4",
            "--source",
            "bench",
            "--witness",
            "me",
        ],
    )
    assert declared.exit_code == 0, declared.output
    assert load_contract(path).model.joints[1].origin.xyz[2] == 0.4


def test_save_and_revalidate_write_the_names_back_byte_for_byte(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    save_contract(load_contract(path), path)  # the canonical spelling
    text = path.read_text()
    c = load_contract(path)
    save_contract(c, path)
    assert path.read_text() == text
    save_contract(c.revalidate(), path)
    assert path.read_text() == text
    assert c.revalidate().model.joints[1].origin.xyz[2] == 0.2  # still resolved after the trip


def test_the_resolved_values_are_not_what_save_wrote(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    save_contract(load_contract(path), path)
    saved = yaml.safe_load(path.read_text())
    assert saved["model"]["joints"][1]["origin"]["xyz"][2] == "arm_length"
    assert saved["model"]["links"][1]["geometry"][0]["length"] == "arm_length"
    assert schema.SCHEMA_VERSION  # the version is untouched


# ---- derived mass


def _grow(path, value):
    path.write_text(path.read_text().replace("value: 0.2\n", f"value: {value}\n"))


def test_a_longer_arm_raises_the_shoulder_demand_and_a_declared_mass_stays(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    short = _gravity(tmp_path, path)
    assert load_contract(path).model.links[2].mass.value == 1.5
    _grow(path, 0.6)
    assert _gravity(tmp_path, path) > short * 1.5
    assert load_contract(path).model.links[2].mass.value == 1.5  # declared: never touched


def test_the_derived_mass_is_volume_times_density_and_the_com_is_the_centroid(
    tmp_path, monkeypatch
):
    arm = load_contract(_write(tmp_path, monkeypatch)).model.links[1]
    assert arm.mass.value == pytest.approx(3.141592653589793 * 0.02**2 * 0.2 * 2700.0)
    assert arm.com == pytest.approx((0.0, 0.0, 0.2))


def test_a_declared_mass_beats_geometry(tmp_path, monkeypatch):
    extra = {"mass": {"value": 9.0, "unit": "kg"}}
    path = tmp_path / "declared.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, arm_extra=extra))
    assert load_contract(path).model.links[1].mass.value == 9.0


@pytest.mark.parametrize("kind", ["calculated", "measured"])
def test_a_calculated_or_measured_link_is_never_derived(tmp_path, monkeypatch, kind):
    path = tmp_path / "kind.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, arm_extra={"mass_kind": kind}))
    assert load_contract(path).model.links[1].mass is None


def test_no_density_or_a_mesh_derives_nothing_and_describe_says_why(tmp_path, monkeypatch):
    from lockwire.describe import describe_system

    arm = load_contract(_write(tmp_path, monkeypatch, density=False))
    assert arm.model.links[1].mass is None
    assert "arm: mass not derived: no material density" in describe_system(arm)
    mesh = {"kind": "mesh", "mesh": "arm.stl"}
    c = load_contract(_write(tmp_path, monkeypatch, shape=mesh))
    assert c.model.links[1].mass is None
    assert "arm: mass not derived: a mesh shape has no known volume" in describe_system(c)


def test_a_derived_mass_does_not_leave_a_zero_mass_finding(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    out = tmp_path / "o.json"
    runner.invoke(app, ["check", str(path), "--json", str(out)])
    assert not [f for f in json.loads(out.read_text())["findings"] if "arm/mass" in f["quantity"]]
    no_density = _write(tmp_path, monkeypatch, density=False)
    runner.invoke(app, ["check", str(no_density), "--json", str(out)])
    assert [f for f in json.loads(out.read_text())["findings"] if "arm/mass" in f["quantity"]]


def test_save_never_writes_a_derived_mass_and_the_zero_mass_list_survives(tmp_path, monkeypatch):
    text = _arm_text(tmp_path, monkeypatch).replace(
        "zero_mass_links: []", "zero_mass_links:\n    - arm", 1
    )
    path = tmp_path / "z.yaml"
    path.write_text(text)
    c = load_contract(path)
    assert c.model.zero_mass_links == []  # derived, so no longer massless in memory
    save_contract(c, path)
    saved = yaml.safe_load(path.read_text())["model"]
    assert "mass" not in saved["links"][1] and "com" not in saved["links"][1]
    assert saved["zero_mass_links"] == ["arm"]
    save_contract(c.revalidate(), path)
    assert "mass" not in yaml.safe_load(path.read_text())["model"]["links"][1]


def test_a_derived_mass_carries_the_marker_and_the_density_source_a_declared_one_does_not(
    tmp_path, monkeypatch
):
    links = load_contract(_write(tmp_path, monkeypatch)).model.links
    assert "derived from its shapes and material" in links[1].mass_note
    assert "alloy sheet" in links[1].mass_note
    assert links[2].mass_note is None  # declared 1.5 kg
    declared = {"mass": {"value": 9.0, "unit": "kg"}}
    path = tmp_path / "declared.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, arm_extra=declared))
    assert load_contract(path).model.links[1].mass_note is None


def test_describe_shows_the_marker_for_a_derived_mass_only_and_save_never_writes_it(
    tmp_path, monkeypatch
):
    from lockwire.describe import describe_system

    path = _write(tmp_path, monkeypatch)
    text = describe_system(load_contract(path))
    assert "arm: 0.6786 kg, derived from its shapes and material" in text
    assert "alloy sheet" in text and "fore: 1.5 kg" not in text
    c = load_contract(path)
    save_contract(c, path)
    assert "derived from its shapes" not in path.read_text()
    declared = {"mass": {"value": 9.0, "unit": "kg"}}
    path.write_text(_arm_text(tmp_path, monkeypatch, arm_extra=declared))
    assert "derived from its shapes" not in describe_system(load_contract(path))


# ---- PARAM-NEG: "-name" is minus the param


def _neg(tmp_path, monkeypatch, **kw):
    shape = {**schema_cyl(), "origin": {"xyz": [0.0, 0.0, "-arm_length"]}}
    return _write(tmp_path, monkeypatch, shape=shape, elbow="-arm_length", **kw)


def schema_cyl():
    return {"kind": "cylinder", "radius": 0.02, "length": 0.2}


def test_a_negated_name_resolves_to_minus_the_value_in_a_shape_and_a_joint_origin(
    tmp_path, monkeypatch
):
    c = load_contract(_neg(tmp_path, monkeypatch, length=300.0, unit="mm"))
    assert c.model.joints[1].origin.xyz[2] == pytest.approx(-0.3)
    assert c.model.links[1].geometry[0].origin.xyz[2] == pytest.approx(-0.3)


def test_a_negated_undeclared_name_is_refused_naming_the_param(tmp_path, monkeypatch):
    path = tmp_path / "bad.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, elbow="-nope"))
    with pytest.raises(ContractError, match="joint 'elbow' uses param 'nope'"):
        load_contract(path)


def test_save_writes_a_negated_name_back_and_one_param_moves_both_signs(tmp_path, monkeypatch):
    path = _neg(tmp_path, monkeypatch)
    save_contract(load_contract(path), path)
    text = path.read_text()
    c = load_contract(path)
    save_contract(c.revalidate(), path)
    assert path.read_text() == text and "- -arm_length" in text
    path.write_text(text.replace("value: 0.2\n", "value: 0.5\n"))
    c = load_contract(path)
    assert c.model.joints[1].origin.xyz[2] == -0.5
    assert c.model.links[1].geometry[0].origin.xyz[2] == -0.5
