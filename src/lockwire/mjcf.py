"""A MuJoCo MJCF file as the same `ModelRecord` the URDF and SDF readers make, for `lockwire check`.

The values come from `check_sim._read_mjcf` (masses, COMs, inertia moments, joint ranges,
default classes); this adds the body tree, joint types and axes. Files read: the model and
the `<include file>` entries directly under `<mujoco>`, each inside the model's own folder
(resolved, so a symlink or `..` out of it is refused). Mesh files are never opened.
"""

import datetime as dt
import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from lockwire.check_sim import _classes, _floats, _read_mjcf, _resolve
from lockwire.schema import (
    Joint,
    JointLimits,
    JointOrigin,
    Link,
    ModelRecord,
    Provenance,
    Quantity,
    _model_structure_hash,
)
from lockwire.urdf import UrdfTopology, _principal, _refuse_doctype_entity

_UNREAD = ("axisangle", "xyaxes", "zaxis")  # rotations named in the not-compared note


def _rotation(el: ET.Element, degrees: bool, seq: str) -> np.ndarray | None:
    """`quat` (w x y z, normalised) or `euler` (`eulerseq`: lowercase turns with the frame)."""
    if q := _floats(el.get("quat")):
        w, x, y, z = np.array(q) / np.linalg.norm(q)
        return np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ]
        )
    if not (angles := _floats(el.get("euler"))):
        return None
    out = np.eye(3)
    for axis, angle in zip(seq, angles):
        a, i = math.radians(angle) if degrees else angle, "xyz".index(axis.lower())
        j, k = (i + 1) % 3, (i + 2) % 3
        turn = np.eye(3)
        turn[j, j] = turn[k, k] = math.cos(a)
        turn[k, j], turn[j, k] = math.sin(a), -math.sin(a)
        out = out @ turn if axis.islower() else turn @ out
    return out


def _rpy(r: np.ndarray | None) -> tuple[float, float, float]:
    """URDF roll-pitch-yaw (fixed axes x, y, z) of `r`, rounded so a re-read hashes alike."""
    if r is None:
        return (0.0, 0.0, 0.0)
    pitch = math.asin(max(-1.0, min(1.0, -r[2, 0])))
    rpy = (math.atan2(r[2, 1], r[2, 2]), pitch, math.atan2(r[1, 0], r[0, 0]))
    return tuple(round(v, 12) + 0.0 for v in rpy)


def _num(values) -> str:
    """Composed numbers, rounded as `_rpy` does, so a frame's float noise never reaches the lock."""
    return " ".join(repr(round(float(v), 12) + 0.0) for v in values)


_TYPES = {"hinge": "revolute", "slide": "prismatic", "ball": "ball"}


def _read_root(path: Path) -> ET.Element:
    raw = path.read_bytes()
    _refuse_doctype_entity(raw, path)
    root = ET.fromstring(raw)
    if root.tag != "mujoco":
        raise ValueError(f"{path}: root element is <{root.tag}>, expected <mujoco>")
    return root


def _splice_includes(root: ET.Element, folder: Path) -> None:
    for inc in root.findall("include"):
        target = (folder / inc.get("file", "")).resolve()
        if not target.is_relative_to(folder) or not target.is_file():
            raise ValueError(f"<include file={inc.get('file')!r}> is not a file inside {folder}")
        at = list(root).index(inc)
        root.remove(inc)
        for i, el in enumerate(_read_root(target)):
            root.insert(at + i, el)
    if root.find(".//include") is not None:
        raise ValueError("an <include> inside an included file or a body is not read")


def _name_all(el: ET.Element, parent: str) -> None:
    """Give unnamed bodies and joints a stable name, so each can be told apart."""
    for i, body in enumerate(el.findall("body")):
        name = body.get("name") or f"{parent}_body{i}"
        body.set("name", name)
        for j, joint in enumerate(body.findall("joint")):
            joint.set("name", joint.get("name") or f"{name}_joint{j}")
        _name_all(body, name)


def parse_mjcf(
    path: Path, warnings: list[str] | None = None, efforts: dict[str, float] | None = None
) -> ModelRecord:
    """`warnings` gets one line naming what the file has that `check` does not compare.

    `efforts` gets joint -> N*m from the actuators (forcerange x gear). The record never
    holds them: an effort in a joint's limits is part of the structure hash, which would
    drift every MJCF lock written before.
    """
    raw = path.read_bytes()
    root = _read_root(path)
    _splice_includes(root, path.resolve().parent)
    world = root.find("worldbody")
    if world is None:
        raise ValueError(f"{path}: no <worldbody> element found")
    compiler = root.find("compiler")
    compiler = {} if compiler is None else compiler.attrib
    degrees, seq = compiler.get("angle", "degree") == "degree", compiler.get("eulerseq", "xyz")
    turned: dict[ET.Element, np.ndarray | None] = {}  # a body's rotation once a <frame> folds in

    def rot(el: ET.Element) -> np.ndarray | None:
        return turned[el] if el in turned else _rotation(el, degrees, seq)

    classes = _classes(root)

    def fold_frames(el: ET.Element, cc: str | None) -> None:
        """A <frame> is transparent, as in MuJoCo: its bodies, inertials and joints become the
        parent's, posed through the frame. Geoms, sites, cameras and lights in it are not read."""
        cc = el.get("childclass", cc)
        for child in list(el):
            fold_frames(child, cc)
        for fr in el.findall("frame"):
            if any(fr.get(a) for a in _UNREAD):  # never lock a value posed through a wrong turn
                raise ValueError(
                    "a <frame> rotated by axisangle, xyaxes or zaxis: a form lockwire does not read; write it as quat or euler"
                )
            at, rf, fcc = list(el).index(fr), rot(fr), fr.get("childclass", cc)
            pf = np.array(_floats(fr.get("pos")) or (0.0, 0.0, 0.0))
            el.remove(fr)
            held = [c for c in fr if c.tag in ("body", "inertial", "joint", "freejoint")]
            for i, c in enumerate(held):
                pc, rc = np.array(_floats(c.get("pos")) or (0.0, 0.0, 0.0)), rot(c)
                c.set("pos", _num(pf + (pc if rf is None else rf @ pc)))
                turned[c] = rc if rf is None else rf if rc is None else rf @ rc
                if fcc and not c.get("class" if c.tag == "joint" else "childclass"):
                    c.set("class" if c.tag == "joint" else "childclass", fcc)
                if c.tag == "joint" and rf is not None:
                    axis = _floats(_resolve(classes, c, fcc).get("axis")) or (0.0, 0.0, 1.0)
                    c.set("axis", _num(rf @ np.array(axis)))
                el.insert(at + i, c)
        for extra in el.findall("inertial")[:-1]:  # MuJoCo keeps the last one written
            el.remove(extra)

    fold_frames(world, None)
    _name_all(world, "world")
    bodies, joint_values, _ = _read_mjcf(root)
    if efforts is not None:
        efforts.update({n: v.effort for n, v in joint_values.items() if v.effort})
    links: dict[str, Link] = {}
    joints: list[Joint] = []

    def walk(el: ET.Element, parent: str, childclass: str | None) -> None:
        for body in el.findall("body"):
            name = body.get("name", "")
            read = bodies[name]
            link = Link(name=name)
            if read.mass:
                inertia = None
                if read.moments:  # the body-frame tensor, turned by the inertial's own rotation
                    el = body.find("inertial")
                    d, f = _floats(el.get("diaginertia")), _floats(el.get("fullinertia"))
                    t = np.diag(d) if d else np.array(f)[[[0, 3, 4], [3, 1, 5], [4, 5, 2]]]
                    r = rot(el)
                    t = t if r is None else r @ t @ r.T
                    inertia = _principal(t[0, 0], t[0, 1], t[0, 2], t[1, 1], t[1, 2], t[2, 2])
                link = Link(
                    name=name,
                    mass=Quantity(value=read.mass, unit="kg"),
                    com=read.com,
                    frame=name,
                    inertia=inertia or None,
                )
            links[name] = link
            cc = body.get("childclass", childclass)
            xyz = tuple(_floats(body.get("pos")) or (0.0, 0.0, 0.0))
            origin = JointOrigin(xyz=xyz, rpy=_rpy(rot(body)))
            made, free = [], body.find("freejoint") is not None
            for raw_joint in body.findall("joint"):
                joint = _resolve(classes, raw_joint, cc)
                kind = joint.get("type", "hinge")
                free = free or kind == "free"
                if kind == "free":
                    continue  # a floating base: one floating joint to the world, below
                lim = joint_values[joint.get("name", "")]
                limits = None
                if lim.lower is not None and kind != "ball":
                    limits = JointLimits(lower=lim.lower, upper=lim.upper)
                axis = None
                if kind != "ball":
                    axis = tuple(_floats(joint.get("axis")) or (0.0, 0.0, 1.0))
                made.append(
                    Joint(
                        name=joint.get("name", ""),
                        type="continuous" if kind == "hinge" and not limits else _TYPES[kind],
                        parent=parent,
                        child=name,
                        origin=origin if not made else JointOrigin(),
                        axis=axis,
                        limits=limits,
                    )
                )
            if not made:  # welded to its parent, or a free body floating on the world
                jtype = "floating" if free else "fixed"
                made.append(
                    Joint(
                        name=f"{name}_{jtype}", type=jtype, parent=parent, child=name, origin=origin
                    )
                )
            if parent == "world":  # every top-level body hangs off one world link: one tree
                links.setdefault("world", Link(name="world"))
            joints.extend(made)
            walk(body, name, cc)

    walk(world, "world", None)
    if warnings is not None:
        every = list(root.iter("body"))
        has = {
            "actuators and gear": root.find("actuator") is not None,
            "tendons": root.find("tendon") is not None,
            "joint damping, armature and friction": any(j.extras for j in joint_values.values()),
            "rotations as axisangle, xyaxes or zaxis": any(
                e.get(a)
                for e in root.iter()
                if e.tag in ("body", "inertial", "frame")
                for a in _UNREAD
            ),
            "masses MuJoCo computes from geoms": any(not bodies[b.get("name")].mass for b in every),
            "mesh files": root.find("asset/mesh") is not None,
        }
        if skipped := [k for k, v in has.items() if v]:
            warnings.append(f"MJCF file: not compared: {', '.join(skipped)}.")
    rows = list(links.values())
    return ModelRecord(
        source=str(path),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        structure_hash=_model_structure_hash(rows, joints),
        links=rows,
        joints=joints,
        zero_mass_links=sorted(k.name for k in rows if k.mass is None),
        provenance=Provenance(witness="w2", source=str(path), locked_at=dt.date.today()),
    )


def is_mjcf(path: Path) -> bool:
    """A `.xml`/`.mjcf` file whose root is `<mujoco>`; a `.xml` URDF stays on the URDF path."""
    try:
        return path.suffix.lower() in (".xml", ".mjcf") and bool(_read_root(path) is not None)
    except (OSError, ET.ParseError, ValueError):
        return False


def read_mjcf_topology(path: Path) -> UrdfTopology:
    """MJCF twin of `sdf.read_sdf_topology`, for `checks.check_urdf_drift`."""
    model = parse_mjcf(path)
    return UrdfTopology(
        link_names=frozenset(k.name for k in model.links),
        joints=tuple(model.joints),
        topology_hash=model.structure_hash,
        record_hash=model.structure_hash,
    )


def urdf_view(path: Path) -> ET.Element:
    """The MJCF as a URDF-shaped `<robot>`, for the checks that read URDF XML.

    Inertial only where the MJCF declares a mass. Axes are written unit length (MuJoCo
    normalises them) and a missing velocity limit is -1, unlimited, as the SDF view does.
    A body's second joint gets its own child name so it never reads as a kinematic loop.
    """
    model = parse_mjcf(path)
    robot = ET.Element("robot", name=path.stem)
    for k in model.links:
        link = ET.SubElement(robot, "link", name=k.name)
        if k.mass:
            inertial = ET.SubElement(link, "inertial")
            ET.SubElement(inertial, "mass", value=str(k.mass.base()))
            ET.SubElement(inertial, "origin", xyz=" ".join(map(str, k.com or (0, 0, 0))))
            if k.inertia:
                xx, yy, zz = k.inertia.moments_kgm2
                tensor = dict(ixx=xx, iyy=yy, izz=zz, ixy=0, ixz=0, iyz=0)
                ET.SubElement(inertial, "inertia", {a: str(b) for a, b in tensor.items()})
    seen: set[str] = set()
    for j in model.joints:
        out = ET.SubElement(robot, "joint", name=j.name, type=j.type)
        ET.SubElement(out, "parent", link=j.parent)
        ET.SubElement(out, "child", link=f"{j.child}/{j.name}" if j.child in seen else j.child)
        seen.add(j.child)
        if j.axis:
            norm = sum(v * v for v in j.axis) ** 0.5 or 1.0
            ET.SubElement(out, "axis", xyz=" ".join(str(v / norm) for v in j.axis))
        lim = {
            k: str(v)
            for k, v in (j.limits or JointLimits()).model_dump().items()
            if k in ("lower", "upper") and v is not None
        }
        ET.SubElement(out, "limit", lim, velocity="-1")
    return robot
