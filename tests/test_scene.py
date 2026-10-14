"""Tests for the stdlib glTF 2.0 scene reader (`lockwire.scene`)."""

import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from lockwire.scene import Scene, SceneRefusal, load_scene

GLTF_FLOAT = 5126
GLTF_USHORT = 5123
GLTF_UINT = 5125

# A unit cube, 8 verts / 12 tris (as triangle list, non-indexed-friendly too).
_CUBE_POS = [
    (-0.5, -0.5, -0.5),
    (0.5, -0.5, -0.5),
    (0.5, 0.5, -0.5),
    (-0.5, 0.5, -0.5),
    (-0.5, -0.5, 0.5),
    (0.5, -0.5, 0.5),
    (0.5, 0.5, 0.5),
    (-0.5, 0.5, 0.5),
]
_CUBE_TRIS = [
    (0, 1, 2),
    (0, 2, 3),  # bottom
    (4, 6, 5),
    (4, 7, 6),  # top
]


def _pad4(b: bytes) -> bytes:
    pad = (-len(b)) % 4
    return b + b"\x00" * pad


def _build_gltf_json(
    *,
    node_name: str = "Cube",
    extras: dict | None = None,
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
    matrix: list[float] | None = None,
    index_type: int = GLTF_USHORT,
    indexed: bool = True,
    mode: int = 4,
    base_color: tuple[float, float, float, float] | None = None,
    up_extras: str | None = None,
    positions: list[tuple[float, float, float]] | None = None,
    tris: list[tuple[int, int, int]] | None = None,
    buffer_uri: str | None = None,
    buffer_byte_length: int | None = None,
    parent_extras: dict | None = None,
    parent_name: str = "Parent",
    with_parent: bool = False,
    parent_translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> tuple[dict, bytes]:
    """Build a minimal glTF JSON + a single interleaved-free binary buffer.

    Layout: positions (float32 x3), then indices (u16 or u32).
    """
    positions = positions if positions is not None else _CUBE_POS
    tris = tris if tris is not None else _CUBE_TRIS

    pos_bytes = b"".join(struct.pack("<3f", *p) for p in positions)
    flat_idx = [i for tri in tris for i in tri]
    if index_type == GLTF_UINT:
        idx_bytes = b"".join(struct.pack("<I", i) for i in flat_idx)
    else:
        idx_bytes = b"".join(struct.pack("<H", i) for i in flat_idx)

    buf = _pad4(pos_bytes) + _pad4(idx_bytes)
    pos_view_len = len(pos_bytes)
    idx_offset = len(_pad4(pos_bytes))
    idx_view_len = len(idx_bytes)

    buffer_views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": pos_view_len, "target": 34962},
        {"buffer": 0, "byteOffset": idx_offset, "byteLength": idx_view_len, "target": 34963},
    ]

    mins = [min(p[i] for p in positions) for i in range(3)]
    maxs = [max(p[i] for p in positions) for i in range(3)]

    accessors = [
        {
            "bufferView": 0,
            "componentType": GLTF_FLOAT,
            "count": len(positions),
            "type": "VEC3",
            "min": mins,
            "max": maxs,
        },
    ]
    attributes = {"POSITION": 0}

    primitive: dict = {"attributes": attributes, "mode": mode}
    if indexed:
        accessors.append(
            {
                "bufferView": 1,
                "componentType": index_type,
                "count": len(flat_idx),
                "type": "SCALAR",
            }
        )
        primitive["indices"] = 1

    materials = []
    if base_color is not None:
        primitive["material"] = 0
        materials.append({"pbrMetallicRoughness": {"baseColorFactor": list(base_color)}})

    node: dict = {"name": node_name, "mesh": 0}
    if extras is not None:
        node["extras"] = extras
    if matrix is not None:
        node["matrix"] = matrix
    else:
        if translation != (0.0, 0.0, 0.0):
            node["translation"] = list(translation)
        if rotation != (0.0, 0.0, 0.0, 1.0):
            node["rotation"] = list(rotation)
        if scale != (1.0, 1.0, 1.0):
            node["scale"] = list(scale)

    nodes = []
    scene_roots = []
    if with_parent:
        parent_node: dict = {"name": parent_name, "children": [1]}
        if parent_extras is not None:
            parent_node["extras"] = parent_extras
        if parent_translation != (0.0, 0.0, 0.0):
            parent_node["translation"] = list(parent_translation)
        nodes = [parent_node, node]
        scene_roots = [0]
    else:
        nodes = [node]
        scene_roots = [0]

    asset: dict = {"version": "2.0"}
    if up_extras is not None:
        asset["extras"] = {"lockwire": {"up": up_extras}}

    doc: dict = {
        "asset": asset,
        "scene": 0,
        "scenes": [{"nodes": scene_roots}],
        "nodes": nodes,
        "meshes": [{"primitives": [primitive]}],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "materials": materials,
    }

    if buffer_uri == "data":
        b64 = base64.b64encode(buf).decode("ascii")
        doc["buffers"] = [
            {"byteLength": len(buf), "uri": f"data:application/octet-stream;base64,{b64}"}
        ]
    elif buffer_uri is not None:
        doc["buffers"] = [{"byteLength": len(buf), "uri": buffer_uri}]
    else:
        doc["buffers"] = [{"byteLength": len(buf)}]

    return doc, buf


def _pad4_json(b: bytes) -> bytes:
    pad = (-len(b)) % 4
    return b + b" " * pad  # glTF spec: JSON chunk padded with spaces, not NUL


def _write_glb(path: Path, doc: dict, bin_data: bytes) -> None:
    json_bytes = json.dumps(doc).encode("utf-8")
    json_bytes = _pad4_json(json_bytes)
    bin_bytes = _pad4(bin_data)
    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    header = struct.pack("<4sII", b"glTF", 2, total_len)
    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    bin_chunk = struct.pack("<II", len(bin_bytes), 0x004E4942) + bin_bytes
    path.write_bytes(header + json_chunk + bin_chunk)


def _write_gltf_databuri(path: Path, **kwargs) -> None:
    doc, buf = _build_gltf_json(buffer_uri="data", **kwargs)
    path.write_text(json.dumps(doc))


def _write_gltf_sidecar(path: Path, bin_name: str = "scene.bin", **kwargs) -> None:
    doc, buf = _build_gltf_json(buffer_uri=bin_name, **kwargs)
    path.write_text(json.dumps(doc))
    (path.parent / bin_name).write_bytes(buf)


# ---------------------------------------------------------------------------


def test_glb_parse_basic(tmp_path: Path):
    doc, buf = _build_gltf_json(node_name="Cube", extras={"lockwire": {"id": "dev.motor.m1"}})
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)

    scene = load_scene(p)
    assert isinstance(scene, Scene)
    assert scene.asset_path == str(p.resolve())
    assert scene.asset_sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    assert scene.triangles == 4
    assert scene.vertices == 8
    assert len(scene.parts) == 1
    part = scene.parts[0]
    assert part.node == "Cube"
    assert part.extras_id == "dev.motor.m1"
    assert part.name_chain == ("Cube",)
    assert len(part.indices) == 12
    assert scene.nodes_with_ids == {"dev.motor.m1": "Cube"}


def test_gltf_data_uri(tmp_path: Path):
    p = tmp_path / "scene.gltf"
    _write_gltf_databuri(p, extras={"lockwire": {"id": "dev.motor.m1"}})
    scene = load_scene(p)
    assert len(scene.parts) == 1
    assert scene.parts[0].extras_id == "dev.motor.m1"


def test_gltf_sidecar_bin(tmp_path: Path):
    p = tmp_path / "scene.gltf"
    _write_gltf_sidecar(p, extras={"lockwire": {"id": "dev.motor.m1"}})
    scene = load_scene(p)
    assert len(scene.parts) == 1
    assert scene.parts[0].extras_id == "dev.motor.m1"


def test_uint32_indices(tmp_path: Path):
    p = tmp_path / "scene.glb"
    doc, buf = _build_gltf_json(index_type=GLTF_UINT)
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    assert scene.parts[0].indices == tuple(i for tri in _CUBE_TRIS for i in tri)


def test_byte_stride_honoured(tmp_path: Path):
    # Build positions interleaved with a dummy 4-byte pad per vertex, non-default byteStride.
    positions = _CUBE_POS
    stride = 16  # 12 bytes float3 + 4 bytes padding
    pos_bytes = b"".join(struct.pack("<3f", *p) + b"\x00\x00\x00\x00" for p in positions)
    flat_idx = [i for tri in _CUBE_TRIS for i in tri]
    idx_bytes = b"".join(struct.pack("<H", i) for i in flat_idx)
    buf = _pad4(pos_bytes) + _pad4(idx_bytes)
    idx_offset = len(_pad4(pos_bytes))

    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Cube", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": GLTF_FLOAT,
                "count": len(positions),
                "type": "VEC3",
            },
            {
                "bufferView": 1,
                "componentType": GLTF_USHORT,
                "count": len(flat_idx),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "byteStride": stride},
            {"buffer": 0, "byteOffset": idx_offset, "byteLength": len(idx_bytes)},
        ],
        "buffers": [{"byteLength": len(buf)}],
        "materials": [],
    }
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    got = [
        tuple(round(v, 6) for v in part.positions[i : i + 3])
        for i in range(0, len(part.positions), 3)
    ]
    expected = [(x, -z, y) for x, y, z in positions]  # default Y-up file -> Z-up frame
    assert got == [tuple(round(v, 6) for v in p_) for p_ in expected]


def test_hierarchy_trs_world_position(tmp_path: Path):
    p = tmp_path / "scene.glb"
    doc, buf = _build_gltf_json(
        with_parent=True,
        parent_translation=(10.0, 0.0, 0.0),
        parent_extras={"lockwire": {"id": "dev.motor.m1"}},
        translation=(1.0, 2.0, 3.0),
    )
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    # Child inherits parent's id (nearest ancestor-or-self with an id).
    assert part.extras_id == "dev.motor.m1"
    assert part.name_chain == ("Parent", "Cube")
    # translation is the OWNING id node's (the parent's) world translation, —
    # not the child mesh node's own position.
    px, py, pz = 10.0, 0.0, 0.0
    gx, gy, gz = px, -pz, py
    assert part.translation == pytest.approx((gx, gy, gz), abs=1e-6)
    # Check a vertex position lands at world-transform-applied Z-up frame too.
    wx, wy, wz = 10.0 + 1.0, 2.0, 3.0
    v0 = part.positions[0:3]
    expected_x = _CUBE_POS[0][0] + wx
    expected_y_gltf = _CUBE_POS[0][1] + wy
    expected_z_gltf = _CUBE_POS[0][2] + wz
    assert v0 == pytest.approx((expected_x, -expected_z_gltf, expected_y_gltf), abs=1e-6)


def test_matrix_node(tmp_path: Path):
    # Pure translation matrix (column-major), translate by (5, 0, 0).
    matrix = [
        1,
        0,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        1,
        0,
        5,
        0,
        0,
        1,
    ]
    doc, buf = _build_gltf_json(matrix=[float(x) for x in matrix])
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    assert part.translation == pytest.approx((5.0, 0.0, 0.0), abs=1e-6)


def test_yup_to_zup_conversion(tmp_path: Path):
    doc, buf = _build_gltf_json(translation=(0.0, 1.0, 0.0))
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    assert part.translation == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)


def test_explicit_zup_left_alone(tmp_path: Path):
    doc, buf = _build_gltf_json(translation=(0.0, 1.0, 0.0), up_extras="z")
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    assert scene.up == "z"
    assert part.translation == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)


def test_base_color_factor(tmp_path: Path):
    doc, buf = _build_gltf_json(base_color=(0.5, 0.25, 0.1, 1.0))
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    assert scene.parts[0].color == pytest.approx((0.5, 0.25, 0.1, 1.0))


def test_default_color(tmp_path: Path):
    doc, buf = _build_gltf_json()
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    assert scene.parts[0].color == pytest.approx((0.8, 0.8, 0.8, 1.0))


def test_non_indexed_primitive(tmp_path: Path):
    # Non-indexed primitives must carry a per-triangle (unwelded) vertex list.
    unwelded = [_CUBE_POS[i] for tri in _CUBE_TRIS for i in tri]
    doc, buf = _build_gltf_json(indexed=False, positions=unwelded, tris=[])
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    part = scene.parts[0]
    assert part.indices == tuple(range(len(unwelded)))


def test_refuse_index_past_vertex_count(tmp_path: Path):
    # 3 vertices (one triangle), but the index accessor claims vertex 99 exists.
    positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    doc, buf = _build_gltf_json(positions=positions, tris=[(0, 1, 99)], node_name="cube")
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    with pytest.raises(SceneRefusal, match="99"):
        load_scene(p)


def test_mode_not_triangles_skipped(tmp_path: Path):
    doc, buf = _build_gltf_json(mode=1)  # LINES
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    assert scene.parts == ()
    assert "Cube" in scene.skipped


def test_refuse_bad_magic(tmp_path: Path):
    p = tmp_path / "scene.glb"
    p.write_bytes(b"NOPE" + b"\x00" * 20)
    with pytest.raises(SceneRefusal):
        load_scene(p)


def test_refuse_sparse_accessor(tmp_path: Path):
    doc, buf = _build_gltf_json()
    doc["accessors"][0]["sparse"] = {
        "count": 1,
        "indices": {"bufferView": 1, "componentType": GLTF_USHORT},
        "values": {"bufferView": 0},
    }
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    with pytest.raises(SceneRefusal):
        load_scene(p)


def test_refuse_extensions_required(tmp_path: Path):
    doc, buf = _build_gltf_json()
    doc["extensionsRequired"] = ["KHR_draco_mesh_compression"]
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    with pytest.raises(SceneRefusal, match="KHR_draco_mesh_compression"):
        load_scene(p)


def test_refuse_absolute_buffer_uri(tmp_path: Path):
    p = tmp_path / "scene.gltf"
    _write_gltf_sidecar(p)
    doc = json.loads(p.read_text())
    doc["buffers"][0]["uri"] = "/etc/passwd"
    p.write_text(json.dumps(doc))
    with pytest.raises(SceneRefusal):
        load_scene(p)


def test_refuse_dotdot_buffer_uri(tmp_path: Path):
    p = tmp_path / "scene.gltf"
    _write_gltf_sidecar(p)
    doc = json.loads(p.read_text())
    doc["buffers"][0]["uri"] = "../scene.bin"
    p.write_text(json.dumps(doc))
    with pytest.raises(SceneRefusal):
        load_scene(p)


def test_refuse_http_buffer_uri(tmp_path: Path):
    p = tmp_path / "scene.gltf"
    _write_gltf_sidecar(p)
    doc = json.loads(p.read_text())
    doc["buffers"][0]["uri"] = "http://example.com/scene.bin"
    p.write_text(json.dumps(doc))
    with pytest.raises(SceneRefusal):
        load_scene(p)


def test_refuse_duplicate_id(tmp_path: Path):
    doc, buf = _build_gltf_json(extras={"lockwire": {"id": "dev.motor.m1"}})
    # Add a second, unrelated root node with the same id.
    second_node = {"name": "Other", "extras": {"lockwire": {"id": "dev.motor.m1"}}}
    doc["nodes"].append(second_node)
    doc["scenes"][0]["nodes"].append(1)
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    with pytest.raises(SceneRefusal, match="dev.motor.m1"):
        load_scene(p)


def test_flat_lockwire_id_extras(tmp_path: Path):
    doc, buf = _build_gltf_json(extras={"lockwire_id": "rail.pm02-5v"})
    p = tmp_path / "scene.glb"
    _write_glb(p, doc, buf)
    scene = load_scene(p)
    assert scene.parts[0].extras_id == "rail.pm02-5v"


X500_GLB = Path(__file__).parent / "fixtures" / "x500" / "x500.glb"


@pytest.mark.skipif(not X500_GLB.exists(), reason="x500.glb fixture not landed yet")
def test_x500_fixture():
    scene = load_scene(X500_GLB)
    assert len(scene.parts) >= 20
    ids = set(scene.nodes_with_ids)
    assert "dev.motor.m1" in ids
    assert "rail.pm02-5v" in ids
