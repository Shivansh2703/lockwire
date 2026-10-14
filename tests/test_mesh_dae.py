"""Tests for the stdlib Collada (DAE) mesh reader (`lockwire.mesh_dae`)."""

import math
from pathlib import Path

import pytest

from lockwire.mesh_dae import DaeResult, read_dae
from lockwire.scene import SceneRefusal

_CUBE_POS = (
    "-0.5 -0.5 -0.5  0.5 -0.5 -0.5  0.5 0.5 -0.5  -0.5 0.5 -0.5 "
    "-0.5 -0.5 0.5  0.5 -0.5 0.5  0.5 0.5 0.5  -0.5 0.5 0.5"
)
_CUBE_NORMALS = "0 0 -1  0 0 1"
_CUBE_TRI_P = "0 0 1 0 2 0  0 0 2 0 3 0  4 1 6 1 5 1  4 1 7 1 6 1"


def _write(tmp_path: Path, xml: str, name: str = "mesh.dae") -> Path:
    path = tmp_path / name
    path.write_text(xml)
    return path


def _wrap(body: str, *, up_axis: str = "Y_UP", unit_meter: float = 1.0) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <unit name="meter" meter="{unit_meter}"/>
    <up_axis>{up_axis}</up_axis>
  </asset>
  {body}
</COLLADA>
"""


def _triangles_geometry(geom_id: str = "cube-mesh") -> str:
    return f"""
  <library_geometries>
    <geometry id="{geom_id}" name="cube">
      <mesh>
        <source id="{geom_id}-positions">
          <float_array id="{geom_id}-positions-array" count="24">{_CUBE_POS}</float_array>
          <technique_common>
            <accessor source="#{geom_id}-positions-array" count="8" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{geom_id}-normals">
          <float_array id="{geom_id}-normals-array" count="6">{_CUBE_NORMALS}</float_array>
          <technique_common>
            <accessor source="#{geom_id}-normals-array" count="2" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{geom_id}-vertices">
          <input semantic="POSITION" source="#{geom_id}-positions"/>
        </vertices>
        <triangles count="4">
          <input semantic="VERTEX" source="#{geom_id}-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{geom_id}-normals" offset="1"/>
          <p>{_CUBE_TRI_P}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
"""


def _polylist_geometry(geom_id: str = "cube-mesh") -> str:
    # Two quads (vcount 4 4), fan-triangulated -> 4 triangles total.
    p = "0 1 2 3  4 5 6 7"
    return f"""
  <library_geometries>
    <geometry id="{geom_id}" name="cube">
      <mesh>
        <source id="{geom_id}-positions">
          <float_array id="{geom_id}-positions-array" count="24">{_CUBE_POS}</float_array>
          <technique_common>
            <accessor source="#{geom_id}-positions-array" count="8" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{geom_id}-vertices">
          <input semantic="POSITION" source="#{geom_id}-positions"/>
        </vertices>
        <polylist count="2">
          <input semantic="VERTEX" source="#{geom_id}-vertices" offset="0"/>
          <vcount>4 4</vcount>
          <p>{p}</p>
        </polylist>
      </mesh>
    </geometry>
  </library_geometries>
"""


def _visual_scene(instance_geom_xml: str, scene_id: str = "Scene") -> str:
    return f"""
  <library_visual_scenes>
    <visual_scene id="{scene_id}" name="{scene_id}">
      {instance_geom_xml}
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#{scene_id}"/>
  </scene>
"""


def test_triangles_y_up_and_unit_scale_to_metres_zup(tmp_path):
    xml = _wrap(
        _triangles_geometry()
        + _visual_scene('<node id="n1"><instance_geometry url="#cube-mesh"/></node>'),
        up_axis="Y_UP",
        unit_meter=0.001,
    )
    result = read_dae(_write(tmp_path, xml))
    assert isinstance(result, DaeResult)
    assert len(result.meshes) == 1
    positions, normals, indices = result.meshes[0]
    assert len(indices) == 12  # 4 triangles

    # First vertex (-0.5,-0.5,-0.5) in file units -> scale 0.001 -> metres -> Y_UP to Z-up
    # (x, y, z) -> (x, -z, y)
    x, y, z = -0.5 * 0.001, -0.5 * 0.001, -0.5 * 0.001
    expected = (x, -z, y)
    got = positions[0:3]
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, abs=1e-9)

    assert normals is not None
    nx, ny, nz = 0.0, 0.0, -1.0
    expected_n = (nx, -nz, ny)
    got_n = normals[0:3]
    for g, e in zip(got_n, expected_n):
        assert g == pytest.approx(e, abs=1e-9)


def test_polylist_quads_fan_triangulated(tmp_path):
    xml = _wrap(
        _polylist_geometry()
        + _visual_scene('<node id="n1"><instance_geometry url="#cube-mesh"/></node>'),
        up_axis="Z_UP",
        unit_meter=1.0,
    )
    result = read_dae(_write(tmp_path, xml))
    assert len(result.meshes) == 1
    positions, normals, indices = result.meshes[0]
    # 2 quads fan-triangulated -> 4 triangles -> 12 indices
    assert len(indices) == 12
    assert normals is None


def test_visual_scene_transform_translate_rotate_scale(tmp_path):
    node = (
        '<node id="n1">'
        "<translate>1 2 3</translate>"
        "<rotate>0 0 1 90</rotate>"
        "<scale>2 2 2</scale>"
        '<instance_geometry url="#cube-mesh"/>'
        "</node>"
    )
    xml = _wrap(
        _triangles_geometry() + _visual_scene(node),
        up_axis="Z_UP",
        unit_meter=1.0,
    )
    result = read_dae(_write(tmp_path, xml))
    positions, _normals, _indices = result.meshes[0]
    # local (-0.5,-0.5,-0.5): scale*2 -> (-1,-1,-1); rotate 90 about Z: (x,y)->(-y,x)
    # -> (1,-1,-1); translate +(1,2,3) -> (2,1,2)
    got = positions[0:3]
    expected = (2.0, 1.0, 2.0)
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, abs=1e-6)


def test_scaled_node_normals_stay_unit_length(tmp_path):
    node = '<node id="n1"><scale>10 10 10</scale><instance_geometry url="#cube-mesh"/></node>'
    xml = _wrap(_triangles_geometry() + _visual_scene(node), up_axis="Z_UP")
    _positions, normals, _indices = read_dae(_write(tmp_path, xml)).meshes[0]
    assert normals
    for i in range(0, len(normals), 3):
        assert abs(math.sqrt(sum(c * c for c in normals[i : i + 3])) - 1) < 1e-9


def test_instance_node_indirection(tmp_path):
    xml = _wrap(
        _triangles_geometry()
        + """
  <library_nodes>
    <node id="referenced"><instance_geometry url="#cube-mesh"/></node>
  </library_nodes>
"""
        + _visual_scene('<node id="n1"><instance_node url="#referenced"/></node>'),
        up_axis="Z_UP",
        unit_meter=1.0,
    )
    result = read_dae(_write(tmp_path, xml))
    assert len(result.meshes) == 1


def test_instance_controller_skipped_and_reported(tmp_path):
    xml = _wrap(
        _triangles_geometry()
        + _visual_scene(
            '<node id="n1"><instance_controller url="#some-controller">'
            "<skeleton>#root</skeleton></instance_controller></node>"
        ),
        up_axis="Z_UP",
        unit_meter=1.0,
    )
    result = read_dae(_write(tmp_path, xml))
    assert len(result.meshes) == 0
    assert len(result.skipped) == 1
    assert "some-controller" in result.skipped[0]


def test_no_visual_scene_reads_every_geometry_once(tmp_path):
    xml = _wrap(_triangles_geometry(), up_axis="Z_UP", unit_meter=1.0)
    result = read_dae(_write(tmp_path, xml))
    assert len(result.meshes) == 1


def test_refuses_polygons_form(tmp_path):
    body = _triangles_geometry().replace(
        '<triangles count="4">\n'
        '          <input semantic="VERTEX" source="#cube-mesh-vertices" offset="0"/>\n'
        '          <input semantic="NORMAL" source="#cube-mesh-normals" offset="1"/>\n'
        f"          <p>{_CUBE_TRI_P}</p>\n"
        "        </triangles>",
        '<polygons count="1"><input semantic="VERTEX" source="#cube-mesh-vertices" '
        'offset="0"/><p>0 1 2 3</p></polygons>',
    )
    xml = _wrap(body + _visual_scene('<node id="n1"><instance_geometry url="#cube-mesh"/></node>'))
    with pytest.raises(SceneRefusal, match="polygons"):
        read_dae(_write(tmp_path, xml))


def test_refuses_index_past_source(tmp_path):
    bad_p = "0 0 1 0 99 0  0 0 2 0 3 0  4 1 6 1 5 1  4 1 7 1 6 1"
    body = _triangles_geometry().replace(_CUBE_TRI_P, bad_p)
    xml = _wrap(body + _visual_scene('<node id="n1"><instance_geometry url="#cube-mesh"/></node>'))
    with pytest.raises(SceneRefusal):
        read_dae(_write(tmp_path, xml))


def test_refuses_missing_url_target(tmp_path):
    xml = _wrap(
        _triangles_geometry()
        + _visual_scene('<node id="n1"><instance_geometry url="#does-not-exist"/></node>'),
    )
    with pytest.raises(SceneRefusal, match="does-not-exist"):
        read_dae(_write(tmp_path, xml))


def test_refuses_doctype(tmp_path):
    xml = '<?xml version="1.0"?>\n<!DOCTYPE COLLADA [<!ENTITY x "y">]>\n' + _wrap(
        _triangles_geometry()
    )
    with pytest.raises(SceneRefusal, match="DOCTYPE"):
        read_dae(_write(tmp_path, xml))


def test_refuses_non_collada_root(tmp_path):
    xml = '<?xml version="1.0"?><notcollada/>'
    with pytest.raises(SceneRefusal, match="COLLADA"):
        read_dae(_write(tmp_path, xml))


def test_real_world_fixture_l_finger_tip():
    fixture = Path(__file__).parent / "fixtures" / "mesh" / "l_finger_tip.dae"
    if not fixture.exists():
        pytest.skip("no permissively-licensed real-world DAE fixture available")
    result = read_dae(fixture)
    assert len(result.meshes) > 0
    total_tris = sum(len(m[2]) // 3 for m in result.meshes)
    assert total_tris > 0
    for positions, _normals, _indices in result.meshes:
        for v in positions:
            assert math.isfinite(v)
