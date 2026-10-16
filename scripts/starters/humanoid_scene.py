"""One-off: build the humanoid starter's scene.glb from ToddlerBot 2xm's visual STL meshes.

    curl -sLo /tmp/toddlerbot_2xm.urdf <URL below>
    # the STLs: <URL's folder>/assets/<link>_visual.stl, saved into one folder
    uv run --with trimesh --with numpy --with scipy --with fast-simplification python scripts/starters/humanoid_scene.py \\
        /tmp/toddlerbot_2xm.urdf /path/to/stl_folder

Source: github.com/hshi74/toddlerbot (MIT), URDF at the commit below. Its meshes are 65 MB, so each is
decimated (quadric) to a share of ~FACES triangles by its size; trimesh is for this script only, never a
dependency of the package. One node per drawn link, named as the link, at its pose with all joints at 0.
Links that carry a contract device get that device's id, so the app can join part and device.
"""

import json
import math
import struct

import numpy as np
import fast_simplification
import trimesh
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

COMMIT = "e337f3b177b4b53abff70b31d1695a7b66cc6d2e"
URL = (
    f"https://raw.githubusercontent.com/hshi74/toddlerbot/{COMMIT}"
    "/toddlerbot/descriptions/toddlerbot_2xm/toddlerbot_2xm.urdf"
)
OUT = Path(__file__).resolve().parents[2] / "src/lockwire/examples/humanoid/scene.glb"

# link -> contract device id. Paper Table 2 says which servo drives which joint; putting the id on
# the link that joint moves is an authoring choice. Servos whose link has no collision box have no part.
IDS = {
    "torso": "dev.jetson-orin",
    "head": "dev.servo.neck-pitch",
    "pelvis_link": "dev.servo.waist-roll",
}
for side in ("left", "right"):
    IDS.update(
        {
            f"{side}_hip_pitch_link": f"dev.servo.{side}-hip-pitch",
            f"{side}_hip_yaw_link": f"dev.servo.{side}-hip-yaw",
            f"{side}_knee_link": f"dev.servo.{side}-knee",
            f"{side}_ankle_pitch_link": f"dev.servo.{side}-ankle-pitch",
            f"{side}_ankle_roll_link": f"dev.servo.{side}-ankle-roll",
            f"{side}_shoulder_roll_link": f"dev.servo.{side}-shoulder-roll-yaw",
            f"{side}_elbow_roll_link": f"dev.servo.{side}-elbow-roll-yaw",
            f"{side}_wrist_pitch_link": f"dev.servo.{side}-wrist-pitch-roll",
        }
    )
FACES = 70000  # total triangle budget across all links


def floats(s):
    return [float(v) for v in s.split()]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def pose(origin):
    x, y, z = floats(origin.get("xyz", "0 0 0")) if origin is not None else (0, 0, 0)
    r, p, w = floats(origin.get("rpy", "0 0 0")) if origin is not None else (0, 0, 0)
    cr, sr, cp, sp, cy, sy = (
        math.cos(r),
        math.sin(r),
        math.cos(p),
        math.sin(p),
        math.cos(w),
        math.sin(w),
    )
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y],
        [-sp, cp * sr, cp * cr, z],
        [0, 0, 0, 1],
    ]


def colmajor(m):
    return [m[r][c] for c in range(4) for r in range(4)]


def decimate(m, keep):
    """Quadric decimation, then vertex clustering when the STL's many loose parts stop it short."""
    m.merge_vertices()
    v, f = np.asarray(m.vertices), np.asarray(m.faces)
    if len(f) > keep:
        v, f = fast_simplification.simplify(v, f, target_count=keep, agg=7)
    best = (v, f)
    lo, hi = 1e-5, float(np.ptp(v, axis=0).max())
    while len(f) > keep * 1.1 and hi - lo > 1e-6:  # bisect the cluster cell size
        h = (lo + hi) / 2
        _, first, inv, cnt = np.unique(
            np.floor(v / h).astype(np.int64),
            axis=0,
            return_index=True,
            return_inverse=True,
            return_counts=True,
        )
        inv = inv.ravel()
        nv = np.zeros((len(cnt), 3))
        np.add.at(nv, inv, v)
        nf = inv[f]
        nf = nf[(nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 0] != nf[:, 2])]
        nf = nf[
            np.unique(np.sort(nf, 1), axis=0, return_index=True)[1]
        ]  # one copy of each face, winding kept
        if len(nf) > keep * 1.1:
            lo = h
        else:
            hi = h
            best = (nv / cnt[:, None], nf)
    return trimesh.Trimesh(*best, process=False)


def main(urdf, stl):
    root = ET.fromstring(Path(urdf).read_bytes())
    world = {"world": pose(None)}
    for j in root.iter("joint"):  # joints are listed parent-first; every joint angle is 0
        world[j.find("child").get("link")] = mul(
            world[j.find("parent").get("link")], pose(j.find("origin"))
        )
    nodes, meshes, accessors, views, blob, mats = (
        [],
        [],
        [],
        [],
        bytearray(),
        [
            {
                "name": "body",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.62, 0.65, 0.7, 1],
                    "metallicFactor": 0.1,
                    "roughnessFactor": 0.6,
                },
            },
            {
                "name": "servo link",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.2, 0.45, 0.8, 1],
                    "metallicFactor": 0.1,
                    "roughnessFactor": 0.5,
                },
            },
        ],
    )

    def add(data, fmt, ctype, atype, minmax=None):
        while len(blob) % 4:
            blob.append(0)
        views.append(
            {
                "buffer": 0,
                "byteOffset": len(blob),
                "byteLength": struct.calcsize("<%d%s" % (len(data), fmt)),
            }
        )
        blob.extend(struct.pack("<%d%s" % (len(data), fmt), *data))
        a = {
            "bufferView": len(views) - 1,
            "componentType": ctype,
            "count": len(data) // (3 if atype == "VEC3" else 1),
            "type": atype,
        }
        if minmax:
            a["min"], a["max"] = minmax
        accessors.append(a)
        return len(accessors) - 1

    links = []  # (link name, visual origin, mesh)
    for link in root.iter("link"):
        v = link.find("visual")
        if v is not None:
            m = trimesh.load(Path(stl) / Path(v.find("geometry/mesh").get("filename")).name)
            links.append((link.get("name"), v.find("origin"), m))
    total = sum(m.area for _, _, m in links)
    top = []
    for name, o, m in links:
        m = decimate(m, max(300, int(FACES * m.area / total)))
        pos = np.asarray(m.vertices, "<f4")
        idx = np.asarray(m.faces, "<u2").ravel()
        nor = np.asarray(m.vertex_normals, "<f4")
        mesh = {
            "name": name,
            "primitives": [
                {
                    "attributes": {
                        "POSITION": add(
                            pos.ravel().tolist(),
                            "f",
                            5126,
                            "VEC3",
                            (pos.min(0).tolist(), pos.max(0).tolist()),
                        ),
                        "NORMAL": add(nor.ravel().tolist(), "f", 5126, "VEC3"),
                    },
                    "indices": add(idx.tolist(), "H", 5123, "SCALAR"),
                    "material": 1 if name in IDS else 0,
                }
            ],
        }
        meshes.append(mesh)
        node = {
            "name": name,
            "mesh": len(meshes) - 1,
            "matrix": colmajor(mul(world[name], pose(o))),
        }
        if name in IDS:
            node["extras"] = {"lockwire": {"id": IDS[name]}}
        nodes.append(node)
        top.append(len(nodes) - 1)
    doc = {
        "asset": {
            "version": "2.0",
            "generator": "scripts/starters/humanoid_scene.py",
            "extras": {"lockwire": {"up": "z"}, "source": URL},
        },
        "scene": 0,
        "scenes": [{"nodes": top}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": mats,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    blob.extend(b"\0" * (-len(blob) % 4))
    doc_len = 12 + 8 + len(js) + 8 + len(blob)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(
        struct.pack("<4sII", b"glTF", 2, doc_len)
        + struct.pack("<I4s", len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(blob), b"BIN\0")
        + bytes(blob)
    )
    print(f"{OUT}: {doc_len} bytes, {len(top)} links, {len(meshes)} meshes")


main(sys.argv[1], sys.argv[2])
