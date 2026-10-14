"""Tests for `lockwire.sdf`: init/start bootstrap from an SDF.

Fixture `px4vision.sdf` is a trimmed copy of the PX4 Gazebo model, kept for the
5-link/4-joint shape only (sensors/plugins/visuals stripped).
SOURCES: PX4-gazebo-models, BSD-3, https://github.com/PX4/PX4-gazebo-models
(models/px4vision/model.sdf), fetched 2026-09-06.
"""

import math

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.sdf import parse_sdf, read_sdf_topology
from lockwire.urdf import read_urdf_topology

from conftest import FIXTURES

runner = CliRunner()

PX4VISION_SDF = FIXTURES / "px4vision.sdf"


def test_px4vision_links_and_masses():
    model = parse_sdf(PX4VISION_SDF)
    assert len(model.links) == 5
    masses = {link.name: link.mass.value for link in model.links}
    assert masses == {
        "base_link": 1.5,
        "rotor_0": 0.005,
        "rotor_1": 0.005,
        "rotor_2": 0.005,
        "rotor_3": 0.005,
    }
    assert model.zero_mass_links == []


def test_px4vision_joints():
    model = parse_sdf(PX4VISION_SDF)
    assert len(model.joints) == 4
    names = {joint.name for joint in model.joints}
    assert names == {"rotor_0_joint", "rotor_1_joint", "rotor_2_joint", "rotor_3_joint"}
    j0 = next(j for j in model.joints if j.name == "rotor_0_joint")
    assert j0.parent == "base_link"
    assert j0.child == "rotor_0"
    assert j0.type == "revolute"


def test_px4vision_structure_hash_stable():
    a = parse_sdf(PX4VISION_SDF)
    b = parse_sdf(PX4VISION_SDF)
    assert a.structure_hash == b.structure_hash


def test_no_sdf_root_refused(tmp_path):
    path = tmp_path / "bad.sdf"
    path.write_text('<?xml version="1.0"?><robot name="nope"/>')
    with pytest.raises(ValueError, match="expected <sdf>"):
        parse_sdf(path)


def test_no_links_refused(tmp_path):
    path = tmp_path / "empty.sdf"
    path.write_text('<?xml version="1.0"?><sdf version="1.9"><model name="empty"/></sdf>')
    with pytest.raises(ValueError, match="no <link>"):
        parse_sdf(path)


def test_doctype_refused(tmp_path):
    path = tmp_path / "bomb.sdf"
    path.write_bytes(b'<?xml version="1.0"?><!DOCTYPE sdf [<!ENTITY x "y">]><sdf/>')
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_sdf(path)


def _write(path, text):
    path.write_text(text)
    return path


def test_joint_origin_is_child_in_parent_frame(tmp_path):
    """SDF gives every link a model-relative pose; the record's joint origin
    must be the CHILD's pose expressed in the PARENT's frame (what URDF's
    <joint><origin> means), not the raw model-relative child pose.

    Parent sits rotated 90deg (yaw) about Z at (1,0,0); child sits at (2,0,0)
    with no rotation of its own, both model-relative. Hand-computed
    child-in-parent: xyz = R_parent^T @ (t_child - t_parent) = (0, -1, 0);
    rpy = R_parent^T @ R_child -> yaw = -pi/2.
    """
    sdf_text = """<?xml version="1.0"?>
<sdf version="1.9">
  <model name="two_link">
    <link name="parent_link">
      <pose>1 0 0 0 0 1.5707963268</pose>
      <inertial><mass>1.0</mass><inertia><ixx>0.01</ixx><iyy>0.01</iyy><izz>0.01</izz></inertia></inertial>
    </link>
    <link name="child_link">
      <pose>2 0 0 0 0 0</pose>
      <inertial><mass>0.5</mass><inertia><ixx>0.001</ixx><iyy>0.001</iyy><izz>0.001</izz></inertia></inertial>
    </link>
    <joint name="hinge" type="revolute">
      <parent>parent_link</parent>
      <child>child_link</child>
      <axis><xyz>0 0 1</xyz></axis>
    </joint>
  </model>
</sdf>
"""
    sdf_path = _write(tmp_path / "two_link.sdf", sdf_text)
    model = parse_sdf(sdf_path)
    joint = next(j for j in model.joints if j.name == "hinge")

    assert joint.origin.xyz == pytest.approx((0.0, -1.0, 0.0), abs=1e-6)
    assert joint.origin.rpy == pytest.approx((0.0, 0.0, -math.pi / 2), abs=1e-6)

    # And prove it against the equivalent URDF, which encodes the same
    # kinematics directly (child-in-parent is exactly the URDF <origin>).
    urdf_text = """<?xml version="1.0"?>
<robot name="two_link">
  <link name="parent_link">
    <inertial><origin xyz="0 0 0"/><mass value="1.0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <link name="child_link">
    <inertial><origin xyz="0 0 0"/><mass value="0.5"/>
      <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial>
  </link>
  <joint name="hinge" type="revolute">
    <parent link="parent_link"/><child link="child_link"/>
    <origin xyz="0 -1 0" rpy="0 0 -1.5707963268"/>
    <axis xyz="0 0 1"/>
  </joint>
</robot>
"""
    urdf_path = _write(tmp_path / "two_link.urdf", urdf_text)
    urdf_topology = read_urdf_topology(urdf_path)
    urdf_hinge = next(j for j in urdf_topology.joints if j.name == "hinge")
    assert joint.origin.xyz == pytest.approx(urdf_hinge.origin.xyz, abs=1e-6)
    assert joint.origin.rpy == pytest.approx(urdf_hinge.origin.rpy, abs=1e-6)


def test_urdf_and_sdf_doors_hash_the_same_small_robot(tmp_path):
    """The URDF door (`parse_urdf`) and the SDF door (`parse_sdf`)
    must agree on `structure_hash` for the same kinematic robot, once both
    record joints. Same two-link/one-hinge robot as
    `test_joint_origin_is_child_in_parent_frame`, just compared end-to-end."""
    from lockwire.urdf import parse_urdf

    sdf_text = """<?xml version="1.0"?>
<sdf version="1.9">
  <model name="two_link">
    <link name="parent_link">
      <pose>0 0 0 0 0 0</pose>
      <inertial><mass>1.0</mass><inertia><ixx>0.01</ixx><iyy>0.01</iyy><izz>0.01</izz></inertia></inertial>
    </link>
    <link name="child_link">
      <pose>1 0 0 0 0 0</pose>
      <inertial><mass>0.5</mass><inertia><ixx>0.001</ixx><iyy>0.001</iyy><izz>0.001</izz></inertia></inertial>
    </link>
    <joint name="hinge" type="revolute">
      <parent>parent_link</parent>
      <child>child_link</child>
      <axis><xyz>0 0 1</xyz></axis>
    </joint>
  </model>
</sdf>
"""
    urdf_text = """<?xml version="1.0"?>
<robot name="two_link">
  <link name="parent_link">
    <inertial><origin xyz="0 0 0"/><mass value="1.0"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial>
  </link>
  <link name="child_link">
    <inertial><origin xyz="0 0 0"/><mass value="0.5"/>
      <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial>
  </link>
  <joint name="hinge" type="revolute">
    <parent link="parent_link"/><child link="child_link"/>
    <origin xyz="1 0 0" rpy="0 0 0"/>
    <axis xyz="0 0 1"/>
  </joint>
</robot>
"""
    sdf_path = _write(tmp_path / "two_link.sdf", sdf_text)
    urdf_path = _write(tmp_path / "two_link.urdf", urdf_text)

    sdf_model = parse_sdf(sdf_path)
    urdf_model = parse_urdf(urdf_path)
    assert sdf_model.structure_hash == urdf_model.structure_hash


def test_read_sdf_topology_matches_parse():
    model = parse_sdf(PX4VISION_SDF)
    topology = read_sdf_topology(PX4VISION_SDF)
    assert topology.link_names == {link.name for link in model.links}
    assert topology.record_hash == model.structure_hash


def test_cli_init_from_sdf(tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    result = runner.invoke(app, ["init", str(PX4VISION_SDF), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert contract_path.exists()

    check = runner.invoke(app, ["check", str(contract_path)])
    assert check.exit_code == 0, check.output


def test_cli_start_from_sdf(tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    result = runner.invoke(
        app,
        ["start", str(PX4VISION_SDF), "--out", str(contract_path)],
    )
    assert result.exit_code == 0, result.output
    assert contract_path.exists()
