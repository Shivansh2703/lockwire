"""`.xacro` opens directly; `package://` meshes resolve without ROS."""

import shutil
from pathlib import Path

import pytest

from lockwire.scene import SceneRefusal
from lockwire.scene_urdf import load_robot_scene
from lockwire.start_operation import start_contract
from lockwire.urdf import UnresolvedXacroError, parse_urdf

FIXTURE = Path(__file__).parent / "fixtures" / "xacro" / "robot_description"
STL = "solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid s\n"
ROBOT = '<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="r">%s</robot>'


def _model(root, uri, mesh=None, at="r.urdf"):
    """root/at references `uri`; the STL (if any) sits at root/mesh."""
    link = f'<link name="b"><visual><geometry><mesh filename="{uri}"/></geometry></visual></link>'
    for rel, text in ((mesh, STL), (at, ROBOT % link)):
        if rel:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    return root / at


def test_start_expands_xacro_and_finds_package_mesh(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "pkg")
    (tmp_path / "pkg" / "meshes").mkdir()
    (tmp_path / "pkg" / "meshes" / "base.stl").write_text(STL)
    text = Path(start_contract(tmp_path / "pkg" / "urdf" / "arm.urdf.xacro")["path"]).read_text()
    assert "meshes/base.stl" in text and "2.5" in text


def test_xacro_hash_covers_included_file(tmp_path):
    top = tmp_path / "r.urdf.xacro"
    top.write_text(ROBOT % '<xacro:include filename="i.xacro"/>')
    (tmp_path / "i.xacro").write_text(ROBOT % '<link name="a"/>')
    first = parse_urdf(top).source_sha256
    assert parse_urdf(top).source_sha256 == first
    (tmp_path / "i.xacro").write_text(ROBOT % '<link name="c"/>')
    assert parse_urdf(top).source_sha256 != first


@pytest.mark.parametrize(
    ("body", "msg"),
    [("<xacro:nope/>", r"unknown macro name.*xacro model\.urdf\.xacro"),
     ('<a b="$(find x)"/>', r"\$\(find …\) needs a ROS environment")],
)  # fmt: skip
def test_broken_xacro_shows_plain_error(tmp_path, body, msg):
    (tmp_path / "bad.urdf.xacro").write_text(ROBOT % body)
    with pytest.raises(UnresolvedXacroError, match=msg):
        parse_urdf(tmp_path / "bad.urdf.xacro")


def test_package_mesh_resolves_from_sibling_folder(tmp_path):
    src = _model(tmp_path, "package://other_pkg/m.stl", "other_pkg/m.stl", "bot/urdf/r.urdf")
    assert load_robot_scene(src).parts


def test_start_records_mesh_in_sibling_package_folder(tmp_path):
    (tmp_path / "package.xml").write_text("<package><name>bot</name></package>")
    # the mesh is deeper than the package folder's own search reaches; only a parent finds it
    src = _model(tmp_path, "package://o/m.stl", "u/v/p/q/o/m.stl", "u/v/w/r.urdf")
    assert "u/v/p/q/o/m.stl" in Path(start_contract(src)["path"]).read_text()


def test_unresolvable_package_mesh_still_refused(tmp_path):
    with pytest.raises(SceneRefusal, match="cannot resolve package://ghost"):
        load_robot_scene(_model(tmp_path, "package://ghost/m.stl"))
