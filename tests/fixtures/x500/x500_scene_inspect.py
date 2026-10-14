"""Inspect the X500 fixture with stdlib only: python3 x500_scene_inspect.py x500.glb."""

import hashlib
import json
import math
from pathlib import Path
import struct
import sys

EXPECTED_IDS = {
    "x500_frame",
    "dev.flight-controller.pixhawk-6c",
    "dev.power-module.pm02-v3",
    "dev.companion.jetson-orin-nano",
    "dev.camera.oak-d-lite",
    "dev.radio.rfd900x",
    "dev.gnss.here3-plus",
    "dev.magnetometer.ist8310",
    *(f"dev.motor.m{i}" for i in range(1, 5)),
    *(f"dev.esc.esc{i}" for i in range(1, 5)),
    "rail.pm02-5v",
    "rail.pixhawk-telem1-5v",
    "rail.servo-motor",
    "bus.uart.telemetry",
    "bus.uart.companion",
    "bus.usb.companion",
    "bus.can.dronecan",
    "bus.i2c.external",
}


def inspect(path):
    raw = Path(path).read_bytes()
    magic, version, length = struct.unpack_from("<4sII", raw)
    assert (magic, version, length) == (b"glTF", 2, len(raw)), "Invalid GLB header"
    chunks = []
    offset = 12
    while offset < len(raw):
        size, kind = struct.unpack_from("<I4s", raw, offset)
        assert size % 4 == 0 and offset + 8 + size <= len(raw), "Invalid chunk size"
        chunks.append((kind, raw[offset + 8 : offset + 8 + size]))
        offset += 8 + size
    assert offset == len(raw)
    assert [kind for kind, _ in chunks] == [b"JSON", b"BIN\0"], "Expected JSON then BIN"
    doc = json.loads(chunks[0][1])
    assert len(doc["buffers"]) == 1
    assert 0 <= len(chunks[1][1]) - doc["buffers"][0]["byteLength"] <= 3

    def no_uri(value):
        if isinstance(value, dict):
            assert "uri" not in value, "URI key in GLB"
            for child in value.values():
                no_uri(child)
        elif isinstance(value, list):
            for child in value:
                no_uri(child)

    no_uri(doc)
    assert not doc.get("textures") and not doc.get("images"), "Expected flat materials"
    nodes = doc["nodes"]
    owners = {}
    print("NODES (translations are local glTF Y-up metres; omitted = [0, 0, 0])")
    for index, node in enumerate(nodes):
        print(
            json.dumps(
                {
                    "index": index,
                    "name": node.get("name"),
                    "extras": node.get("extras", {}),
                    "translation": node.get("translation", [0, 0, 0]),
                },
                sort_keys=True,
            )
        )
        contract_id = node.get("extras", {}).get("lockwire", {}).get("id")
        if contract_id is not None:
            assert contract_id in EXPECTED_IDS, f"Foreign ID: {contract_id}"
            assert contract_id not in owners, f"Duplicate ID: {contract_id}"
            assert node["name"] == contract_id, "ID and node name differ"
            assert "mesh" not in node and "matrix" not in node
            assert node.get("rotation", [0, 0, 0, 1]) == [0, 0, 0, 1]
            assert node.get("scale", [1, 1, 1]) == [1, 1, 1]
            owners[contract_id] = index
    assert set(owners) == EXPECTED_IDS, f"Missing IDs: {EXPECTED_IDS - owners.keys()}"
    assert len(doc["scenes"]) == 1
    roots = doc["scenes"][doc.get("scene", 0)]["nodes"]
    assert set(roots) == set(owners.values()), "Every ID must be a scene root"
    visited = set()
    mesh_indices = []
    by_id = {key: [] for key in sorted(owners)}

    def walk(index, inherited=()):
        assert index not in visited, "Cycle or multiply parented node"
        visited.add(index)
        node = nodes[index]
        own_id = node.get("extras", {}).get("lockwire", {}).get("id")
        lineage = inherited + (own_id,) if own_id else inherited
        if "mesh" in node:
            assert len(lineage) == 1, f"Mesh must have exactly one ID ancestor: {node['name']}"
            by_id[lineage[0]].append(node["name"])
            mesh_indices.append(node["mesh"])
        for child in node.get("children", []):
            walk(child, lineage)

    for root in roots:
        walk(root)
    assert visited == set(range(len(nodes))), "Unreachable nodes"
    assert set(mesh_indices) == set(range(len(doc["meshes"]))), "Unreachable meshes"
    assert all(by_id.values()), "ID without geometry"
    vertices = triangles = 0
    for mesh_index in mesh_indices:
        for primitive in doc["meshes"][mesh_index]["primitives"]:
            assert primitive.get("mode", 4) == 4, "Expected triangles"
            assert "material" in primitive and "NORMAL" in primitive["attributes"]
            material = doc["materials"][primitive["material"]]
            assert len(material["pbrMetallicRoughness"]["baseColorFactor"]) == 4
            count = doc["accessors"][primitive["attributes"]["POSITION"]]["count"]
            vertices += count
            index_count = doc["accessors"][primitive["indices"]]["count"]
            assert index_count % 3 == 0
            triangles += index_count // 3
    assert len(raw) <= 600_000, "Scene exceeds size target"

    # Inverse of export_yup: glTF (x, y, z) -> Blender (x, -z, y).
    def centre(contract_id):
        x, y, z = nodes[owners[contract_id]].get("translation", [0, 0, 0])
        return x, -z, y

    assert centre("x500_frame") == (0, 0, 0)
    signs = [(1, -1), (-1, 1), (-1, -1), (1, 1)]
    for i, (sx, sy) in enumerate(signs, 1):
        x, y, z = centre(f"dev.motor.m{i}")
        assert math.isclose(math.hypot(x, y), 0.25, abs_tol=1e-7)
        assert x * sx > 0 and y * sy > 0, "Wrong PX4 motor quadrant"
        ex, ey, ez = centre(f"dev.esc.esc{i}")
        assert math.isclose(ex, x * 0.78, abs_tol=1e-7)
        assert math.isclose(ey, y * 0.78, abs_tol=1e-7) and ez < z
    print("\nID TABLE (node name = contract ID; exact descendant mesh-node names)")
    for contract_id, meshes in by_id.items():
        print(f"{contract_id} -> {contract_id} -> {', '.join(meshes)}")
    summary = {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "nodes": len(nodes),
        "ids": len(owners),
        "mesh_nodes": len(mesh_indices),
        "vertices": vertices,
        "triangles": triangles,
        "uri_keys": 0,
    }
    print("\nSUMMARY " + json.dumps(summary, sort_keys=True))
    print(
        "PASS: IDs, ownership, embedded BIN, no URI/textures, materials/normals, size, motor/ESC placement"
    )
    return doc, by_id, summary


if __name__ == "__main__":
    inspect(sys.argv[1])
