"""GAP-meshimport: `add link` takes an STL or DAE mesh with an explicit scale."""

import struct

import pytest

from lockwire.edit import EditRefusal, add_entry
from lockwire.schema import Contract, load_contract, save_contract

CUBE_V = [(x, y, z) for x in (0, 2) for y in (0, 3) for z in (0, 4)]
CUBE_T = [
    (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
    (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),
]  # fmt: skip
HEAD = 'schema_version: "0.7"\nrobot:\n  name: r\n'


def _binary(count=None, drop=0):
    body = b"".join(
        struct.pack("<3f", 0, 0, 1) + b"".join(struct.pack("<3f", *CUBE_V[i]) for i in t) + b"\0\0"
        for t in CUBE_T
    )
    return b"x" * 80 + struct.pack("<I", len(CUBE_T) if count is None else count) + body[: len(body) - drop]


def _ascii():
    facets = "".join(
        "facet normal 0 0 1\nouter loop\n"
        + "".join("vertex %g %g %g\n" % CUBE_V[i] for i in t)
        + "endloop\nendfacet\n"
        for t in CUBE_T
    )
    return ("solid c\n" + facets + "endsolid c\n").encode()


def _dae():
    pos = " ".join("%g %g %g" % v for v in CUBE_V)
    idx = " ".join(str(i) for t in CUBE_T for i in t)
    return f"""<?xml version="1.0"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema">
<asset><unit meter="1"/><up_axis>Z_UP</up_axis></asset><library_geometries>
<geometry id="g"><mesh><source id="p"><float_array id="pa" count="24">{pos}</float_array>
<technique_common><accessor source="#pa" count="8" stride="3"><param name="X" type="float"/>
<param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<vertices id="v"><input semantic="POSITION" source="#p"/></vertices>
<triangles count="12"><input semantic="VERTEX" source="#v" offset="0"/><p>{idx}</p></triangles>
</mesh></geometry></library_geometries></COLLADA>""".encode()


def _add(tmp_path, name, data, scale, head=HEAD, **extra):
    (tmp_path / name).write_bytes(data)
    lock = tmp_path / "lock.yaml"
    lock.write_text(head)
    fields = {"name": "part", "mesh": tmp_path / name, "scale": scale, **extra}
    return lock, add_entry(lock, "link", fields, source="cad", witness="Maya Chen")


@pytest.mark.parametrize(
    ("name", "data"), [("c.stl", _ascii()), ("b.stl", _binary()), ("d.dae", _dae())]
)
def test_each_format_imports_12_triangles_into_a_valid_lockfile(tmp_path, name, data):
    lock, result = _add(tmp_path, name, data, "m")
    assert f"imported {name}: 2.000 x 3.000 x 4.000 m (12 triangles)" in result.lines
    shape = load_contract(lock).model.links[0].geometry[0]
    assert (shape.kind, shape.mesh, shape.scale) == ("mesh", name, 1.0)


def test_scale_shrinks_the_bounds_by_a_thousand(tmp_path):
    _, mm = _add(tmp_path, "b.stl", _binary(), "mm")
    _, one = _add(tmp_path, "b.stl", _binary(), 1)
    assert "0.002 x 0.003 x 0.004 m" in mm.lines[0]
    assert "2.000 x 3.000 x 4.000 m" in one.lines[0]


def test_scale_number_is_stored_as_given(tmp_path):
    lock, _ = _add(tmp_path, "b.stl", _binary(), "0.001")
    assert load_contract(lock).model.links[0].geometry[0].scale == 0.001


@pytest.mark.parametrize("bad", [None, "", "feet", "0", "-1", "nan", True])
def test_a_missing_or_bad_scale_is_refused_in_one_line(tmp_path, bad):
    with pytest.raises(EditRefusal) as error:
        _add(tmp_path, "b.stl", _binary(), bad)
    assert str(error.value) == (
        "A mesh needs its scale: say mm, m or a number such as 0.001. Lockwire does not guess the unit."
    )


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (_binary(drop=10), "header count does not match its length"),
        (_binary(count=13), "header count does not match its length"),
        (_binary(count=5), "header count does not match its length"),
        (b"solid empty\nendsolid empty\n", "not a readable STL"),
        (b"", "not a readable STL"),
        (b"solid x\nfacet\nvertex 0 0 0\nendfacet\n", "empty or broken ASCII STL"),
    ],
)
def test_a_lying_truncated_or_empty_stl_is_refused(tmp_path, data, why):
    with pytest.raises(EditRefusal, match=why):
        _add(tmp_path, "b.stl", data, "m")


def test_wrong_extension_and_outside_the_project_are_refused(tmp_path):
    with pytest.raises(EditRefusal, match="not an STL or DAE"):
        _add(tmp_path, "b.obj", _binary(), "m")
    outside, inside = tmp_path / "out", tmp_path / "in"
    outside.mkdir(), inside.mkdir()
    (outside / "b.stl").write_bytes(_binary())
    (inside / "lock.yaml").write_text(HEAD)
    fields = {"name": "p", "mesh": outside / "b.stl", "scale": "m"}
    with pytest.raises(EditRefusal, match="inside the lockfile's folder"):
        add_entry(inside / "lock.yaml", "link", fields, source="cad", witness="Maya Chen")


def test_a_dae_with_a_doctype_entity_is_refused(tmp_path):
    bad = _dae().replace(b"<COLLADA", b'<!DOCTYPE c [<!ENTITY e "x">]><COLLADA', 1)
    with pytest.raises(EditRefusal, match="not a readable DAE"):
        _add(tmp_path, "d.dae", bad, "m")


def test_a_mesh_link_gets_no_derived_mass_and_says_how_to_declare_one(tmp_path):
    from lockwire.schema import mass_derivation

    lock, result = _add(tmp_path, "b.stl", _binary(), "m", material="Aluminium 6061-T6")
    assert any(
        line.startswith("mass not derived for a mesh; declare one with: lockwire declare")
        and "model/links/part/mass" in line
        for line in result.lines
    )
    assert "no known volume" in mass_derivation(load_contract(lock).model.links[0])


def test_mesh_scale_moves_a_06_file_to_07_and_says_so(tmp_path):
    old = 'schema_version: "0.6"\nrobot:\n  name: r\n'
    lock, result = _add(tmp_path, "b.stl", _binary(), "mm", head=old)
    assert lock.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records a mesh scale" in result.lines


def test_a_06_file_that_uses_a_mesh_scale_is_refused_by_the_schema(tmp_path):
    lock, _ = _add(tmp_path, "b.stl", _binary(), "mm")
    lock.write_text(lock.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="a mesh scale needs lockfile format 0.7"):
        load_contract(lock)


def test_round_trip_and_unknown_key_refusal(tmp_path):
    lock, _ = _add(tmp_path, "b.stl", _binary(), "mm")
    first = load_contract(lock)
    out = tmp_path / "saved.yaml"
    save_contract(first, out)
    assert load_contract(out).model_dump() == first.model_dump()
    lock.write_text(lock.read_text().replace("scale:", "mesh_scale:", 1))
    with pytest.raises(ValueError, match="mesh_scale"):
        load_contract(lock)
    assert Contract


@pytest.mark.parametrize(("name", "data"), [("c.stl", _ascii()), ("b.stl", _binary()), ("d.dae", _dae())])
def test_a_mesh_over_the_triangle_cap_is_refused(tmp_path, monkeypatch, name, data):
    from lockwire import meshread

    monkeypatch.setattr(meshread, "MAX_TRIANGLES", 11)
    with pytest.raises(EditRefusal, match="triangles; (the )?limit|over 11 triangles"):
        _add(tmp_path, name, data, "m")


def test_a_mesh_over_the_size_cap_is_refused(tmp_path, monkeypatch):
    from lockwire import meshread

    monkeypatch.setattr(meshread, "MAX_BYTES", 100)
    with pytest.raises(EditRefusal, match="MiB; use a smaller mesh"):
        _add(tmp_path, "b.stl", _binary(), "m")


def test_a_dotdot_or_symlinked_mesh_outside_the_folder_is_refused(tmp_path):
    inside = tmp_path / "in"
    inside.mkdir()
    (tmp_path / "x.stl").write_bytes(_binary())
    (inside / "link.stl").symlink_to(tmp_path / "x.stl")
    (inside / "lock.yaml").write_text(HEAD)
    for mesh in (inside / ".." / "x.stl", inside / "link.stl"):
        fields = {"name": "p", "mesh": mesh, "scale": "m"}
        with pytest.raises(EditRefusal, match="inside the lockfile's folder"):
            add_entry(inside / "lock.yaml", "link", fields, source="cad", witness="Maya Chen")
