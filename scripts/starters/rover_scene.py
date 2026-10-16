"""One-off: write src/lockwire/examples/rover/scene.glb.

Run: uv run --with trimesh --with numpy --with fast-simplification --with scipy python scripts/starters/rover_scene.py [stl-dir]
(trimesh is a one-off tool here, never a package dependency).

The body is the real ROBOTIS TurtleBot3 Burger visual meshes (burger_base, left/right tire, lds;
Apache-2.0, turtlebot3_description at ROBOTIS-GIT/turtlebot3@90a68bd, humble), downloaded from that
commit into [stl-dir] (default /tmp/tb3-stl) and quadric-decimated to a few thousand triangles each
(about 1 MB glb instead of 7.7 MB of STL). Placement follows the URDF: visual origins, base_link
0.010 above base_footprint, wheel joints at (0, +-0.08, 0.023), scan joint at (-0.032, 0, 0.172).
Z-up, metres, x forward. The base mesh is the node "base_link"; wheels and lidar carry the
contract's device ids so the app can join them. The OpenCR and Jetson boxes are ILLUSTRATIVE (the
URDF has no link for them; sizes and heights are author-chosen, not contract numbers).
"""
import json
import struct
import sys
import urllib.request
from pathlib import Path

import numpy as np
import trimesh

OUT = Path(__file__).resolve().parents[2] / "src/lockwire/examples/rover/scene.glb"
REV = "90a68bd2e3c61c12966779da89d8eeaec82730e9"
RAW = f"https://raw.githubusercontent.com/ROBOTIS-GIT/turtlebot3/{REV}/turtlebot3_description/meshes/"
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tb3-stl")
BASE_Z = 0.010


def real(name, faces, shift):
    """Decimated ROBOTIS STL (mm -> m), moved by `shift`, welded, smooth vertex normals."""
    f = SRC / Path(name).name
    if not f.exists():
        SRC.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(RAW + name, f)
    m = trimesh.load(f)
    m.apply_scale(0.001)
    m.merge_vertices()
    m = m.simplify_quadric_decimation(face_count=faces)
    m.apply_translation(shift)
    return m


def box(sx, sy, sz, shift):
    m = trimesh.creation.box((sx, sy, sz))
    m.apply_translation(shift)
    t = m.triangles.reshape(-1, 3)  # unshared corners, so each face keeps its flat normal
    return trimesh.Trimesh(t, np.arange(len(t)).reshape(-1, 3), process=False)


LDS = "sensors/lds.stl"
# (node name, extras id or None, mesh, rgba). Wheel visuals: rpy 1.57 under a joint rpy -1.57 = identity.
P = [
    ("base_link", None, real("bases/burger_base.stl", 50000, (-0.032, 0, BASE_Z)), (0.30, 0.30, 0.33, 1)),
    ("dev.motor-controller", "dev.motor-controller", box(0.105, 0.075, 0.012, (-0.032, 0, 0.066)), (0.1, 0.45, 0.2, 1)),
    ("dev.jetson-nano-computer", "dev.jetson-nano-computer", box(0.100, 0.080, 0.026, (-0.032, 0, 0.112)), (0.2, 0.2, 0.22, 1)),
    ("dev.2d-lidar", "dev.2d-lidar", real(LDS, 6000, (-0.032, 0, BASE_Z + 0.172)), (0.15, 0.15, 0.17, 1)),
    ("dev.left-wheel-motor", "dev.left-wheel-motor", real("wheels/left_tire.stl", 6000, (0, 0.08, BASE_Z + 0.023)), (0.08, 0.08, 0.09, 1)),
    ("dev.right-wheel-motor", "dev.right-wheel-motor", real("wheels/right_tire.stl", 6000, (0, -0.08, BASE_Z + 0.023)), (0.08, 0.08, 0.09, 1)),
]

bin_, views, accs, meshes, nodes, mats = bytearray(), [], [], [], [], []


def add(arr, dtype, typ, target, mm=False):
    off = len(bin_)
    bin_.extend(arr.astype(dtype).tobytes())
    bin_.extend(b"\0" * (-len(bin_) % 4))
    views.append({"buffer": 0, "byteOffset": off, "byteLength": arr.size * np.dtype(dtype).itemsize, "target": target})
    a = {"bufferView": len(views) - 1, "componentType": {"<f4": 5126, "<u2": 5123, "<u4": 5125}[dtype], "count": len(arr), "type": typ}
    if mm:
        a["min"], a["max"] = arr.min(0).tolist(), arr.max(0).tolist()
    accs.append(a)
    return len(accs) - 1


for name, ident, m, rgba in P:
    p = add(np.asarray(m.vertices), "<f4", "VEC3", 34962, True)
    q = add(np.asarray(m.vertex_normals), "<f4", "VEC3", 34962)
    r = add(np.asarray(m.faces).flatten(), "<u2" if len(m.vertices) < 65535 else "<u4", "SCALAR", 34963)
    mats.append({"pbrMetallicRoughness": {"baseColorFactor": list(rgba), "metallicFactor": 0.1, "roughnessFactor": 0.6}})
    meshes.append({"primitives": [{"attributes": {"POSITION": p, "NORMAL": q}, "indices": r, "material": len(mats) - 1}]})
    node = {"name": name, "mesh": len(meshes) - 1}
    if ident:
        node["extras"] = {"lockwire": {"id": ident}}
    nodes.append(node)

doc = {"asset": {"version": "2.0", "generator": "scripts/starters/rover_scene.py", "extras": {"lockwire": {"up": "z"}}},
       "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes,
       "materials": mats, "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(bin_)}]}
js = json.dumps(doc, separators=(",", ":")).encode()
js += b" " * (-len(js) % 4)
total = 12 + 8 + len(js) + 8 + len(bin_)
OUT.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
                + struct.pack("<I4s", len(bin_), b"BIN\0") + bytes(bin_))
print(f"wrote {OUT} ({total} bytes)", file=sys.stderr)
