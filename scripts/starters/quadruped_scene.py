"""One-off: build src/lockwire/examples/quadruped/scene.glb from ODRI's Solo-12 URDF + STL meshes.

    git clone https://github.com/open-dynamic-robot-initiative/robot_properties_solo /tmp/rps
    uv run --with trimesh --with numpy --with xacro --with scipy python scripts/starters/quadruped_scene.py /tmp/rps

Meshes are BSD-3 (see NOTICE next to the output). Joints at zero angle, z-up, metres.
Each actuator node (dev.motor.<front-left>-<hip-abduction|hip-flexion|knee>) carries the mesh of the link its joint drives;
the base mesh hangs on the `base_link` node (the URDF link name). The package itself never imports this script.
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np
import trimesh
import xacro

root = Path(sys.argv[1]) / "src/robot_properties_solo/resources"
out = Path(__file__).resolve().parents[2] / "src/lockwire/examples/quadruped/scene.glb"
urdf = xacro.process_file(str(root / "xacro/solo12.urdf.xacro")).toxml()
from xml.etree import ElementTree as ET  # noqa: E402

robot = ET.fromstring(urdf)
xyz = lambda e: np.array([float(v) for v in e.get("xyz").split()])  # noqa: E731
parent_joint = {j.find("child").get("link"): j for j in robot.iter("joint")}


def world(link):  # every joint origin in this URDF has rpy 0, so poses are plain sums
    j = parent_joint.get(link)
    return np.zeros(3) if j is None else world(j.find("parent").get("link")) + xyz(j.find("origin"))


meshes, mesh_index, nodes, accessors, views, blob = [], {}, [], [], [], bytearray()


def add(data, target, ctype, kind, minmax=False):
    while len(blob) % 4:
        blob.append(0)
    views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes, "target": target})
    blob.extend(data.tobytes())
    acc = {"bufferView": len(views) - 1, "componentType": ctype, "count": len(data) if kind == "SCALAR" else len(data), "type": kind}
    if minmax:
        acc["min"], acc["max"] = data.min(0).tolist(), data.max(0).tolist()
    accessors.append(acc)
    return len(accessors) - 1


def mesh_of(filename):
    if filename not in mesh_index:
        m = trimesh.load(root / filename.split("resources/")[1], force="mesh")
        m.merge_vertices()
        v = np.asarray(m.vertices, np.float32)
        n = np.asarray(m.vertex_normals, np.float32)
        f = np.asarray(m.faces, np.uint32).reshape(-1)
        attrs = {"POSITION": add(v, 34962, 5126, "VEC3", True), "NORMAL": add(n, 34962, 5126, "VEC3")}
        i = add(f, 34963, 5125, "SCALAR")
        meshes.append({"name": Path(filename).stem, "primitives": [{"attributes": attrs, "indices": i, "material": 0}]})
        mesh_index[filename] = len(meshes) - 1
    return mesh_index[filename]


roots = []
for link in robot.iter("link"):
    vis = link.find("visual")
    if vis is None:
        continue
    pos = world(link.get("name")) + xyz(vis.find("origin"))
    child = {"name": "body" if link.get("name") == "base_link" else link.get("name").lower().replace("_", "-"), "mesh": mesh_of(vis.find("geometry/mesh").get("filename"))}
    if link.get("name") == "base_link":
        child["translation"] = pos.tolist()
        nodes.append(child)
        nodes.append({"name": "base_link", "children": [len(nodes) - 1], "extras": {"lockwire": {"id": "base_link"}}})  # URDF link name
        roots.append(len(nodes) - 1)
        continue
    leg, joint = parent_joint[link.get("name")].get("name").split("_")  # FL_HAA -> dev.motor.front-left-hip-abduction
    leg = {"FL": "front-left", "FR": "front-right", "HL": "hind-left", "HR": "hind-right"}[leg]
    joint = {"HAA": "hip-abduction", "HFE": "hip-flexion", "KFE": "knee"}[joint]
    child["name"] = f"{leg}-{ {'hip-abduction': 'shoulder', 'hip-flexion': 'upper-leg', 'knee': 'lower-leg'}[joint] }"
    jpos = world(link.get("name"))
    child["translation"] = (pos - jpos).tolist()
    nodes.append(child)
    nodes.append({"name": f"dev.motor.{leg}-{joint}", "translation": jpos.tolist(), "children": [len(nodes) - 1],
                  "extras": {"lockwire": {"id": f"dev.motor.{leg}-{joint}"}}})
    roots.append(len(nodes) - 1)

doc = {
    "asset": {"version": "2.0", "generator": "scripts/starters/quadruped_scene.py", "extras": {"lockwire": {"up": "z"}}},
    "scene": 0, "scenes": [{"nodes": roots}], "nodes": nodes, "meshes": meshes, "accessors": accessors, "bufferViews": views,
    "buffers": [{"byteLength": len(blob)}],
    "materials": [{"name": "Solo12 grey", "pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1], "metallicFactor": 0.1, "roughnessFactor": 0.6}}],
}
js = json.dumps(doc, separators=(",", ":")).encode()
js += b" " * (-len(js) % 4)
blob.extend(b"\0" * (-len(blob) % 4))
doc_len = 12 + 8 + len(js) + 8 + len(blob)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(struct.pack("<III", 0x46546C67, 2, doc_len) + struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
print(out, out.stat().st_size, "bytes", len(nodes), "nodes")
