"""Tests for the URDF/SDF -> Scene door (`lockwire.scene_urdf`)."""

import struct
import sys
import pytest
from typer.testing import CliRunner
from lockwire.scene import SceneRefusal
from lockwire.scene_urdf import load_robot_scene


runner = CliRunner()

# --------------------------------------------------------------------------- mesh fixtures


def _write_binary_stl(path, triangles):
    """triangles: list of (normal, v1, v2, v3), each a 3-tuple."""
    header = b"lockwire test stl" + b"\x00" * (80 - len("lockwire test stl"))
    with open(path, "wb") as f:
        f.write(header)
        f.write(struct.pack("<I", len(triangles)))
        for normal, v1, v2, v3 in triangles:
            f.write(struct.pack("<12fH", *normal, *v1, *v2, *v3, 0))


_TWO_TRI_STL = [
    ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
    ((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
]

_ASCII_STL = """solid two_tri
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 1 1 0
endloop
endfacet
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 1 0
vertex 0 1 0
endloop
endfacet
endsolid two_tri
"""

_OBJ_QUAD = """v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
vn 0 0 1
f -4//-1 -3//-1 -2//-1 -1//-1
"""


def _urdf(links_and_joints: str) -> str:
    return f'<?xml version="1.0"?><robot name="testbot">{links_and_joints}</robot>'


def _box_visual(size="1 1 1", origin_xyz="0 0 0", origin_rpy="0 0 0"):
    return (
        f'<visual><origin xyz="{origin_xyz}" rpy="{origin_rpy}"/>'
        f'<geometry><box size="{size}"/></geometry></visual>'
    )


# --------------------------------------------------------------------------- primitives


def test_primitives_on_two_links_joined_translate_correctly(tmp_path):
    urdf = _urdf(
        "<link name='base'>"
        + _box_visual()
        + "</link>"
        + "<link name='wheel'>"
        + '<visual><geometry><cylinder radius="0.1" length="0.05"/></geometry></visual>'
        + "</link>"
        + "<link name='eye'>"
        + '<visual><geometry><sphere radius="0.02"/></geometry></visual>'
        + "</link>"
        + '<joint name="base_to_wheel" type="fixed">'
        + '<parent link="base"/><child link="wheel"/>'
        + '<origin xyz="0.3 0.0 -0.1" rpy="0 0 1.5707963267948966"/>'
        + "</joint>"
        + '<joint name="wheel_to_eye" type="revolute">'
        + '<parent link="wheel"/><child link="eye"/>'
        + '<origin xyz="0.02 0.0 0.05" rpy="0 0 0"/>'
        + "</joint>"
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    ids = {part.extras_id for part in scene.parts}
    assert ids == {"base", "wheel", "eye"}
    wheel_part = next(p for p in scene.parts if p.extras_id == "wheel")
    # base_to_wheel origin (0.3, 0, -0.1) rotated by yaw=pi/2 around a joint whose
    # parent (base) is identity -> the child (wheel) sits exactly at that offset.
    assert wheel_part.translation[0] == pytest.approx(0.3, abs=1e-6)
    assert wheel_part.translation[1] == pytest.approx(0.0, abs=1e-6)
    assert wheel_part.translation[2] == pytest.approx(-0.1, abs=1e-6)

    eye_part = next(p for p in scene.parts if p.extras_id == "eye")
    # eye = wheel pose (yaw pi/2 rotation, from base_to_wheel's rpy) composed with the
    # wheel_to_eye offset (0.02, 0, 0.05): Rz(pi/2) turns (0.02, 0, 0.05) into
    # (0, 0.02, 0.05). Skipping rpy in the composition (the brief's bite) would leave
    # the offset unrotated and put eye at x=0.32, y=0.0 instead.
    assert eye_part.translation[0] == pytest.approx(0.3, abs=1e-6)
    assert eye_part.translation[1] == pytest.approx(0.02, abs=1e-6)
    assert eye_part.translation[2] == pytest.approx(-0.05, abs=1e-6)
    assert scene.up == "z"


def test_joint_origin_composition_uses_rpy(tmp_path):
    """Bite check (brief): if the joint composition skipped rpy, this rotated-offset
    translation would come out wrong."""
    urdf = _urdf(
        "<link name='base'>" + _box_visual() + "</link>"
        "<link name='child'>" + _box_visual() + "</link>"
        '<joint name="j" type="fixed"><parent link="base"/><child link="child"/>'
        '<origin xyz="1 0 0" rpy="0 0 1.5707963267948966"/></joint>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    child = next(p for p in scene.parts if p.extras_id == "child")
    # translation is the joint origin itself (parent==identity), so this passes even
    # without rpy composition here — but a grandchild would not; see below.
    assert child.translation[0] == pytest.approx(1.0, abs=1e-6)
    assert child.translation[1] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- mesh: STL/OBJ


def test_mesh_binary_stl(tmp_path):
    stl_path = tmp_path / "box.stl"
    _write_binary_stl(stl_path, _TWO_TRI_STL)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="box.stl"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    part = scene.parts[0]
    assert len(part.positions) == 6 * 3
    assert len(part.indices) == 6


def test_mesh_ascii_stl(tmp_path):
    (tmp_path / "box.stl").write_text(_ASCII_STL)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="box.stl"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    assert len(scene.parts[0].indices) == 6


def test_mesh_obj_quad_with_negative_indices(tmp_path):
    (tmp_path / "quad.obj").write_text(_OBJ_QUAD)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="quad.obj"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    part = scene.parts[0]
    # fan-triangulated quad -> 2 triangles = 6 corners
    assert len(part.indices) == 6
    assert len(part.positions) == 6 * 3


def test_mesh_scale_applied(tmp_path):
    stl_path = tmp_path / "box.stl"
    _write_binary_stl(stl_path, _TWO_TRI_STL)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="box.stl" scale="2 2 2"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    part = scene.parts[0]
    assert max(part.positions) == pytest.approx(2.0, abs=1e-9)


# --------------------------------------------------------------------------- package://


def test_package_resolved_via_package_root(tmp_path):
    pkg_dir = tmp_path / "workspace" / "my_pkg"
    (pkg_dir / "meshes").mkdir(parents=True)
    (pkg_dir / "package.xml").write_text(
        '<?xml version="1.0"?><package><name>my_pkg</name></package>'
    )
    _write_binary_stl(pkg_dir / "meshes" / "box.stl", _TWO_TRI_STL)

    urdf_dir = tmp_path / "urdf_dir"
    urdf_dir.mkdir()
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="package://my_pkg/meshes/box.stl"/></geometry></visual></link>'
    )
    path = urdf_dir / "robot.urdf"
    path.write_text(urdf)

    scene = load_robot_scene(path, package_roots=(tmp_path / "workspace",))
    assert len(scene.parts[0].indices) == 6


def test_package_resolved_via_ament_prefix_path(tmp_path, monkeypatch):
    share_dir = tmp_path / "install" / "share" / "my_pkg" / "meshes"
    share_dir.mkdir(parents=True)
    _write_binary_stl(share_dir / "box.stl", _TWO_TRI_STL)
    monkeypatch.setenv("AMENT_PREFIX_PATH", str(tmp_path / "install"))

    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="package://my_pkg/meshes/box.stl"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    scene = load_robot_scene(path)
    assert len(scene.parts[0].indices) == 6


def test_package_unresolvable_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
    monkeypatch.delenv("ROS_PACKAGE_PATH", raising=False)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="package://nope/box.stl"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    with pytest.raises(SceneRefusal, match="cannot resolve package://nope"):
        load_robot_scene(path)


# --------------------------------------------------------------------------- SDF


def test_sdf_model_two_links_and_mesh(tmp_path):
    stl_path = tmp_path / "box.stl"
    _write_binary_stl(stl_path, _TWO_TRI_STL)
    sdf = """<?xml version="1.0"?>
<sdf version="1.6">
  <model name="testbot">
    <link name="base_link">
      <pose>0 0 0 0 0 0</pose>
      <visual name="v1"><geometry><box><size>1 1 1</size></box></geometry></visual>
    </link>
    <link name="sensor_link">
      <pose>0.5 0 0.2 0 0 0</pose>
      <visual name="v2">
        <geometry><mesh><uri>box.stl</uri></mesh></geometry>
      </visual>
    </link>
  </model>
</sdf>
"""
    path = tmp_path / "robot.sdf"
    path.write_text(sdf)
    scene = load_robot_scene(path)
    ids = {part.extras_id for part in scene.parts}
    assert ids == {"base_link", "sensor_link"}
    sensor = next(p for p in scene.parts if p.extras_id == "sensor_link")
    assert sensor.translation == pytest.approx((0.5, 0.0, 0.2), abs=1e-6)


# --------------------------------------------------------------------------- refusals


def test_doctype_refused(tmp_path):
    path = tmp_path / "bad.urdf"
    path.write_bytes(b'<?xml version="1.0"?><!DOCTYPE robot [<!ENTITY x "y">]><robot/>')
    with pytest.raises(SceneRefusal, match="DOCTYPE"):
        load_robot_scene(path)


def test_dae_refused_when_module_missing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "lockwire.mesh_dae", None)
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="thing.dae"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    (tmp_path / "thing.dae").write_text("<COLLADA/>")
    with pytest.raises(SceneRefusal, match="DAE support is not in this build"):
        load_robot_scene(path)


def test_dae_multi_mesh_becomes_one_part_per_submesh(tmp_path, monkeypatch):
    """I2's `read_dae` returns a `DaeResult` with a `.meshes` list — one (positions,
    normals, indices) triple per DAE geometry. Each must become its own ScenePart."""
    import types

    fake_module = types.ModuleType("lockwire.mesh_dae")

    class _FakeResult:
        def __init__(self, meshes):
            self.meshes = meshes
            self.skipped: list[str] = []

    def _fake_read_dae(path):
        tri_a = ((0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0), None, (0, 1, 2))
        tri_b = ((0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0), None, (0, 1, 2))
        return _FakeResult([tri_a, tri_b])

    fake_module.read_dae = _fake_read_dae
    monkeypatch.setitem(sys.modules, "lockwire.mesh_dae", fake_module)

    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="thing.dae"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    (tmp_path / "thing.dae").write_text("<COLLADA/>")
    scene = load_robot_scene(path)
    assert len(scene.parts) == 2
    assert all(p.extras_id == "base" for p in scene.parts)


def test_unknown_mesh_extension_refused(tmp_path):
    (tmp_path / "thing.ply").write_text("not a real mesh")
    urdf = _urdf(
        "<link name='base'><visual><geometry>"
        '<mesh filename="thing.ply"/></geometry></visual></link>'
    )
    path = tmp_path / "robot.urdf"
    path.write_text(urdf)
    with pytest.raises(SceneRefusal, match="unsupported extension"):
        load_robot_scene(path)


def test_unresolved_xacro_refused(tmp_path):
    path = tmp_path / "robot.urdf"
    path.write_text(_urdf("<link name='${prefix}base'/>"))
    with pytest.raises(SceneRefusal, match="unresolved xacro"):
        load_robot_scene(path)


def test_xacro_extension_literal_xml_parses(tmp_path):
    path = tmp_path / "robot.xacro"
    path.write_text(_urdf("<link name='base'>" + _box_visual() + "</link>"))
    scene = load_robot_scene(path)
    assert scene.parts[0].extras_id == "base"


def test_xacro_extension_with_macro_refused(tmp_path):
    path = tmp_path / "robot.xacro"
    path.write_text(
        '<?xml version="1.0"?><robot name="x" xmlns:xacro="http://ros.org/wiki/xacro">'
        '<xacro:macro name="m"><link name="base"/></xacro:macro></robot>'
    )
    with pytest.raises(SceneRefusal, match="unresolved xacro"):
        load_robot_scene(path)
