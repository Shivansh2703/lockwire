"""Tests for `lockwire.mesh_simple` garbage must refuse, not crash."""

import struct

import pytest

from lockwire.mesh_simple import MeshRefusal, read_obj, read_stl


def _binary_stl_bytes(n_declared: int, n_actual: int) -> bytes:
    """A binary STL header declaring `n_declared` triangles but carrying `n_actual`
    triangle records — `n_declared != n_actual` builds a truncated/corrupt file."""
    header = b"lockwire test stl" + b"\x00" * (80 - len("lockwire test stl"))
    body = header + struct.pack("<I", n_declared)
    record = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 0)
    return body + record * n_actual


def test_truncated_binary_stl_refuses(tmp_path):
    path = tmp_path / "robot.stl"
    path.write_bytes(_binary_stl_bytes(n_declared=40, n_actual=2))
    with pytest.raises(MeshRefusal, match="truncated"):
        read_stl(path)


def test_triangle_count_mismatch_refuses(tmp_path):
    path = tmp_path / "robot.stl"
    # declares 2 but ships 3 records worth of bytes -- also a size mismatch.
    path.write_bytes(_binary_stl_bytes(n_declared=2, n_actual=3))
    with pytest.raises(MeshRefusal, match="declares 2 triangles"):
        read_stl(path)


def test_empty_stl_file_refuses(tmp_path):
    path = tmp_path / "empty.stl"
    path.write_bytes(b"")
    with pytest.raises(MeshRefusal):
        read_stl(path)


def test_ascii_stl_bad_vertex_line_refuses(tmp_path):
    path = tmp_path / "bad.stl"
    path.write_text(
        "solid bad\n"
        "facet normal 0 0 1\n"
        "outer loop\n"
        "vertex not a number\n"
        "vertex 1 0 0\n"
        "vertex 1 1 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid bad\n"
    )
    with pytest.raises(MeshRefusal):
        read_stl(path)


def test_ascii_stl_bad_facet_normal_refuses(tmp_path):
    path = tmp_path / "bad_normal.stl"
    path.write_text(
        "solid bad\n"
        "facet normal x y z\n"
        "outer loop\n"
        "vertex 0 0 0\n"
        "vertex 1 0 0\n"
        "vertex 1 1 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid bad\n"
    )
    with pytest.raises(MeshRefusal):
        read_stl(path)


def test_obj_face_index_past_vertex_count_refuses(tmp_path):
    path = tmp_path / "bad.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 5\n")
    with pytest.raises(MeshRefusal, match="out of range"):
        read_obj(path)


def test_obj_bad_vertex_line_refuses(tmp_path):
    path = tmp_path / "bad_vertex.obj"
    path.write_text("v 0 0 notanumber\nv 1 0 0\nv 1 1 0\nf 1 2 3\n")
    with pytest.raises(MeshRefusal):
        read_obj(path)


def test_obj_face_normal_index_bad_token_refuses(tmp_path):
    path = tmp_path / "bad_normal_idx.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nvn 0 0 1\nf 1//x 2//1 3//1\n")
    with pytest.raises(MeshRefusal):
        read_obj(path)
