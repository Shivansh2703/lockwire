"""One-off generator for src/lockwire/examples/arm/scene.glb (not a package dependency).

Input: the SO-101 URDF and its STL assets from github.com/TheRobotStudio/SO-ARM100
(Apache-2.0), Simulation/SO101/. Each STL becomes its convex hull (keeps the real
outline, ~1% of the size), posed by the URDF's joint chain at zero angles.
Servo meshes join the contract's servo devices by glTF extras id; the rest is structure.

    uv run --with trimesh --with numpy --with scipy python scripts/starters/arm_scene.py \
        so101_new_calib.urdf assets/ src/lockwire/examples/arm/scene.glb
"""

import json
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

# link -> (servo mesh file, contract device id); the plate carries the Waveshare board.
SERVO = {
    "base_link": "dev.shoulder-pan-servo",
    "shoulder_link": "dev.shoulder-lift-servo",
    "upper_arm_link": "dev.elbow-flex-servo",
    "lower_arm_link": "dev.wrist-flex-servo",
    "wrist_link": "dev.wrist-roll-servo",
    "gripper_link": "dev.gripper-servo",
}
BOARD = ("waveshare_mounting_plate_so101_v2", "dev.waveshare-servo-adapter")


def pose(origin):
    xyz = [float(v) for v in (origin.get("xyz") or "0 0 0").split()]
    r, p, y = [float(v) for v in (origin.get("rpy") or "0 0 0").split()]
    m = trimesh.transformations.euler_matrix(r, p, y, "sxyz")  # URDF rpy = fixed-axis xyz
    m[:3, 3] = xyz
    return m


def main(urdf, assets, out):
    root = ET.parse(urdf).getroot()
    world = {"base_link": np.eye(4)}
    joints = {j.find("child").get("link"): (j.find("parent").get("link"), pose(j.find("origin")))
              for j in root.findall("joint")}  # file lists children before parents

    def link_world(name):
        if name not in world:
            parent, m = joints[name]
            world[name] = link_world(parent) @ m
        return world[name]

    groups: dict[str, list[trimesh.Trimesh]] = {}  # node name -> hulls
    for link in root.iter("link"):
        for vis in link.findall("visual"):
            mesh_el = vis.find("geometry/mesh")
            if mesh_el is None:
                continue
            stem = Path(mesh_el.get("filename")).stem
            mesh = trimesh.load(Path(assets) / f"{stem}.stl", force="mesh")
            mesh.apply_transform(link_world(link.get("name")) @ pose(vis.find("origin")))
            hull = mesh.convex_hull
            lname = link.get("name")
            if stem.startswith("sts3215") and lname in SERVO:
                key = SERVO[lname]
            elif stem == BOARD[0]:
                key = BOARD[1]
            else:
                key = lname  # node name = URDF link name, so a drop on structure hits a model link
            groups.setdefault(key, []).append(hull)

    blob, views, accs, meshes, nodes = bytearray(), [], [], [], []
    for key, hulls in sorted(groups.items()):
        h = trimesh.util.concatenate(hulls)
        v = np.asarray(h.vertices, "<f4")
        f = np.asarray(h.faces, "<u4")
        n = np.asarray(h.vertex_normals, "<f4")
        accessors = []
        for arr, target, kind in ((v, 34962, "VEC3"), (n, 34962, "VEC3"), (f.ravel(), 34963, "SCALAR")):
            blob.extend(b"\0" * (-len(blob) % 4))
            views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes, "target": target})
            acc = {"bufferView": len(views) - 1, "count": len(arr), "type": kind,
                   "componentType": 5126 if arr.dtype == "<f4" else 5125}
            if arr is v:
                acc.update(min=v.min(0).tolist(), max=v.max(0).tolist())
            accs.append(acc)
            accessors.append(len(accs) - 1)
            blob.extend(arr.tobytes())
        meshes.append({"primitives": [{"attributes": {"POSITION": accessors[0], "NORMAL": accessors[1]},
                                       "indices": accessors[2], "material": 0 if key.startswith("dev.") else 1}]})
        node = {"name": key, "mesh": len(meshes) - 1}
        if key.startswith("dev."):
            node["extras"] = {"lockwire": {"id": key}}
        nodes.append(node)

    def mat(name, rgb):
        return {"name": name, "doubleSided": True,
                "pbrMetallicRoughness": {"baseColorFactor": [*rgb, 1], "metallicFactor": 0.1, "roughnessFactor": 0.6}}

    doc = {
        "asset": {"version": "2.0", "generator": "scripts/starters/arm_scene.py",
                  "extras": {"lockwire": {"up": "z"}}},  # URDF is z-up, meters
        "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes,
        "materials": [mat("servo / board", (0.12, 0.14, 0.16)), mat("printed structure", (0.85, 0.82, 0.75))],
        "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(blob)}],
    }
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    blob.extend(b"\0" * (-len(blob) % 4))
    total = 12 + 8 + len(js) + 8 + len(blob)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(
        struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
        + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob))
    print(out, total, "bytes;", len(nodes), "nodes")


if __name__ == "__main__":
    main(*sys.argv[1:4])
