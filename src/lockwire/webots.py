"""Webots `.wbt` world / `.proto` robot -> `ModelRecord`. Stdlib tokenizer.

Reads the first `Robot` node: `Solid`/`Robot` `name` + `physics Physics` (mass, centerOfMass,
inertiaMatrix) become links; each `HingeJoint` becomes a revolute (or continuous, when
`minPosition`/`maxPosition` are not a range) joint with axis/anchor from `HingeJointParameters`
and limits from its `RotationalMotor` (radians, maxTorque, maxVelocity). Anything else it
cannot read is refused with ONE plain sentence (`ValueError`), never a traceback.
"""

import datetime as dt
import hashlib
import re
from pathlib import Path

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
from lockwire.urdf import _principal

_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|([{}\[\]])|([^\s{}\[\]"#]+))')
# Non-physical nodes that may sit in a children list; any other capitalised type there is a
# PROTO instance this reader cannot resolve. ponytail: list is by hand, grow it on a real miss.
_IGNORED = {
    "Shape", "Transform", "Group", "Pose", "Camera", "RangeFinder", "Lidar", "DistanceSensor",
    "Accelerometer", "Gyro", "GPS", "Compass", "InertialUnit", "Display", "LED", "Emitter",
    "Receiver", "Connector", "TouchSensor", "LightSensor", "Radar", "Speaker", "Microphone",
    "Altimeter", "Brake", "Charger", "PointLight", "SpotLight", "DirectionalLight", "Track",
    "Propeller", "Skin", "Fluid", "Slot",
}  # fmt: skip


class _Node:
    def __init__(self, kind: str):
        self.kind = kind
        self.fields: dict[str, object] = {}


def _tokens(text: str) -> list[str]:
    if "%<" in text:
        raise ValueError("uses a PROTO template (%< >%)")
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError("has text it cannot tokenise")
            break
        pos = m.end()
        if m.group(1) is None:
            out.append(m.group(0).strip())
    return out


class _Parser:
    def __init__(self, toks: list[str]):
        self.t, self.i = toks, 0

    def peek(self, k: int = 0) -> str | None:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self) -> str:
        if self.i >= len(self.t):
            raise ValueError("ends in the middle of a node")
        self.i += 1
        return self.t[self.i - 1]

    def expect(self, tok: str) -> None:
        if self.take() != tok:
            raise ValueError(f"has {self.t[self.i - 1]!r} where {tok!r} was expected")

    def node(self) -> _Node:
        kind = self.take()
        if kind in ("DEF", "USE"):
            raise ValueError("uses DEF/USE")
        if not kind[:1].isalpha() or self.peek() != "{":
            raise ValueError(f"has {kind!r} where a node was expected")
        self.take()
        node = _Node(kind)
        while self.peek() != "}":
            if self.peek() is None:
                raise ValueError("ends in the middle of a node")
            name = self.take()
            if name == "IS" or self.peek() == "IS":
                raise ValueError("uses PROTO parameters (IS)")
            node.fields[name] = self.value()
        self.take()
        return node

    def value(self) -> object:
        tok = self.peek()
        if tok in ("DEF", "USE"):
            raise ValueError("uses DEF/USE")
        if tok == "[":
            self.take()
            items: list[object] = []
            while self.peek() != "]":
                if self.peek() is None:
                    raise ValueError("ends inside a list")
                if self.peek() in ("DEF", "USE"):
                    raise ValueError("uses DEF/USE")
                items.append(self.value() if self.peek(1) == "{" else self.take())
            self.take()
            return items
        if tok is not None and self.peek(1) == "{":
            return self.node()
        if tok is None or tok in ("}", "]"):
            raise ValueError("has a field with no value")
        vals = [self.take()]
        while (nxt := self.peek()) is not None and re.fullmatch(r"[-+.\d][-+.\deE]*", nxt):
            vals.append(self.take())
        return vals[0] if len(vals) == 1 else vals


def _nums(raw: object, what: str, count: tuple[int, ...] | None = None) -> list[float]:
    flat = raw if isinstance(raw, list) else [raw]
    try:
        vals = [float(x) for x in flat]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"has a {what} that is not numbers") from None
    if count and len(vals) not in count:
        raise ValueError(f"has a {what} with {len(vals)} values")
    return vals


def _num(node: _Node, field: str, default: float) -> float:
    return _nums(node.fields[field], field)[0] if field in node.fields else default


def _string(raw: object) -> str:
    return raw.strip('"') if isinstance(raw, str) else ""


def _solid(node: _Node, links: list[Link], joints: list[Joint], zero: list[str]) -> str:
    name = _string(node.fields.get("name"))
    if not name:
        raise ValueError(f"has a {node.kind} with no name")
    mass = 0.0
    phys = node.fields.get("physics")
    link = Link(name=name)
    if isinstance(phys, _Node):
        mass = _num(phys, "mass", 0.0)
    if mass > 0:  # Webots mass -1 means "use density": no mass the record can state
        com = _nums(phys.fields["centerOfMass"], "centerOfMass", (3,)) if "centerOfMass" in phys.fields else [0.0] * 3  # fmt: skip
        inertia = None
        if "inertiaMatrix" in phys.fields:
            m = _nums(phys.fields["inertiaMatrix"], "inertiaMatrix", (3, 6))
            m += [0.0] * (6 - len(m))  # Webots: Ixx Iyy Izz Ixy Ixz Iyz
            inertia = _principal(m[0], m[3], m[4], m[1], m[5], m[2])
        link = Link(name=name, mass=Quantity(value=mass, unit="kg"), com=tuple(com), frame=name, inertia=inertia)  # type: ignore[arg-type]  # fmt: skip
    else:
        zero.append(name)
    if any(link.name == x.name for x in links):
        raise ValueError(f"names the link {name!r} twice")
    links.append(link)
    kids = node.fields.get("children", [])
    for kid in kids if isinstance(kids, list) else []:
        if not isinstance(kid, _Node):
            raise ValueError(f"has something in {name!r} children that is not a node")
        if kid.kind == "HingeJoint":
            _joint(kid, name, links, joints, zero)
        elif kid.kind not in _IGNORED | {"Solid", "Robot", "Physics"}:
            raise ValueError(f"has {kid.kind}, a node it cannot resolve")
    return name


def _joint(node: _Node, parent: str, links: list[Link], joints: list[Joint], zero: list[str]) -> None:  # fmt: skip
    end = node.fields.get("endPoint")
    if not isinstance(end, _Node) or end.kind not in ("Solid", "Robot"):
        raise ValueError(f"has a HingeJoint under {parent!r} with no Solid endPoint")
    if "rotation" in end.fields and _nums(end.fields["rotation"], "rotation", (4,))[3] != 0:
        raise ValueError(f"has a rotated endPoint under {parent!r}, which it cannot read")
    params = node.fields.get("jointParameters")
    params = params if isinstance(params, _Node) else _Node("HingeJointParameters")
    axis = _nums(params.fields["axis"], "axis", (3,)) if "axis" in params.fields else [0.0, 0.0, 1.0]  # fmt: skip
    anchor = _nums(params.fields["anchor"], "anchor", (3,)) if "anchor" in params.fields else [0.0] * 3  # fmt: skip
    motor = next((d for d in _as_list(node.fields.get("device")) if isinstance(d, _Node) and d.kind == "RotationalMotor"), None)  # fmt: skip
    slot = len(joints)  # parent joint first, then the joints below its endPoint
    child = _solid(end, links, joints, zero)
    lower = upper = None
    effort = velocity = None
    if motor is not None:
        lo, hi = _num(motor, "minPosition", 0.0), _num(motor, "maxPosition", 0.0)
        if lo < hi:  # Webots: min >= max means unlimited
            lower, upper = lo, hi
        effort, velocity = _num(motor, "maxTorque", 10.0), _num(motor, "maxVelocity", 10.0)
    name = _string(motor.fields.get("name")) if motor is not None else f"{child}_joint"
    origin = _nums(end.fields["translation"], "translation", (3,)) if "translation" in end.fields else anchor  # fmt: skip
    joints.insert(
        slot,
        Joint(
            name=name or f"{child}_joint",
            type="revolute" if lower is not None else "continuous",
            parent=parent,
            child=child,
            origin=JointOrigin(xyz=tuple(origin)),  # type: ignore[arg-type]
            axis=tuple(axis),  # type: ignore[arg-type]
            limits=JointLimits(lower=lower, upper=upper, effort=effort, velocity=velocity),
        ),
    )


def _as_list(raw: object) -> list:
    return raw if isinstance(raw, list) else [] if raw is None else [raw]


def _first_robot(p: _Parser) -> _Node:
    """The first top-level Robot node; other world nodes (arena, viewpoint) are skipped."""
    while p.peek() is not None:
        tok = p.take()
        if tok == "EXTERNPROTO":
            raise ValueError("uses EXTERNPROTO")
        if tok == "PROTO":  # `PROTO Name [ interface ] { body }`: read the body
            p.take()
            p.expect("[")
            while p.peek() != "]":
                p.take()
            p.take()
            p.expect("{")
            continue
        if tok in ("DEF", "USE"):
            raise ValueError("uses DEF/USE")
        if p.peek() == "{":
            p.i -= 1
            node = p.node()
            if node.kind == "Robot":
                return node
    raise ValueError("has no Robot node")


def parse_webots(path: Path) -> ModelRecord:
    """First Robot in a `.wbt`/`.proto` -> `ModelRecord`; ValueError with one plain sentence."""
    try:
        raw = path.read_bytes()
        robot = _first_robot(_Parser(_tokens(raw.decode("utf-8"))))
        links: list[Link] = []
        joints: list[Joint] = []
        zero: list[str] = []
        _solid(robot, links, joints, zero)
        names = [j.name for j in joints]
        if len(set(names)) != len(names):
            raise ValueError("names a joint twice")
        return ModelRecord(
            source=str(path),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            structure_hash=_model_structure_hash(links, joints),
            links=links,
            joints=joints,
            zero_mass_links=sorted(zero),
            provenance=Provenance(witness="w2", source=str(path), locked_at=dt.date.today()),
        )
    except (ValueError, RecursionError, OSError) as error:  # UnicodeDecodeError is a ValueError
        why = "could not be opened" if isinstance(error, OSError) else str(error)
        if isinstance(error, UnicodeDecodeError):
            why = "is not text"
        raise ValueError(f"{path}: lockwire cannot read this Webots file, it {why}.") from None


ENGINE_CHECKS: tuple = ()  # checks.py loads every ENGINE_MODULES entry; the reader adds no check
