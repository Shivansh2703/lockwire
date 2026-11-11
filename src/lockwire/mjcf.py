"""A MuJoCo MJCF file as the same `ModelRecord` the URDF and SDF readers make, for `lockwire check`.

The values come from `check_sim._read_mjcf` (masses, COMs, inertia moments, joint ranges,
default classes); this adds the body tree, joint types and axes. Files read: the model and
the `<include file>` entries directly under `<mujoco>`, each inside the model's own folder
(resolved, so a symlink or `..` out of it is refused). Mesh files are never opened.
"""

import datetime as dt
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

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
from lockwire.urdf import _principal, _refuse_doctype_entity

_ROTATIONS = ("quat", "euler", "axisangle", "xyaxes", "zaxis")
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


def parse_mjcf(path: Path, warnings: list[str] | None = None) -> ModelRecord:
    """`warnings` gets one line naming what the file has that `check` does not compare."""
    raw = path.read_bytes()
    root = _read_root(path)
    _splice_includes(root, path.resolve().parent)
    world = root.find("worldbody")
    if world is None:
        raise ValueError(f"{path}: no <worldbody> element found")
    _name_all(world, "world")
    bodies, joint_values, _ = _read_mjcf(root)
    classes = _classes(root)
    links: dict[str, Link] = {}
    joints: list[Joint] = []

    def walk(el: ET.Element, parent: str, childclass: str | None) -> None:
        for body in el.findall("body"):
            name = body.get("name", "")
            read = bodies[name]
            link = Link(name=name)
            if read.mass:
                inertia = read.moments and _principal(
                    read.moments[0], 0, 0, read.moments[1], 0, read.moments[2]
                )
                link = Link(
                    name=name,
                    mass=Quantity(value=read.mass, unit="kg"),
                    com=read.com,
                    frame=name,
                    inertia=inertia or None,
                )
            links[name] = link
            cc = body.get("childclass", childclass)
            origin = JointOrigin(xyz=tuple(_floats(body.get("pos")) or (0.0, 0.0, 0.0)))
            made, free = [], False
            for raw_joint in body.findall("joint"):
                joint = _resolve(classes, raw_joint, cc)
                kind = joint.get("type", "hinge")
                free = free or kind == "free"
                if kind == "free":
                    continue  # a floating base: no joint to the world
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
            if not made and not free and parent != "world":
                made.append(
                    Joint(
                        name=f"{name}_fixed", type="fixed", parent=parent, child=name, origin=origin
                    )
                )
            if made and parent == "world":
                links.setdefault("world", Link(name="world"))
            joints.extend(made)
            walk(body, name, cc)

    walk(world, "world", None)
    if warnings is not None:
        every = list(root.iter("body"))
        has = {
            "actuators and gear": root.find("actuator") is not None,
            "tendons": root.find("tendon") is not None,
            "body rotations": any(b.get(a) for b in every for a in _ROTATIONS),
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
