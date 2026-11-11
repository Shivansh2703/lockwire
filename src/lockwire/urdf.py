"""URDF -> Tier A extraction (`lockwire init`). Witness W2: in-repo model files.

Traps handled here (spec v3):
- URDF inertia is about the COM in the inertial frame -> compare principal
  moments + axes, never raw tensor elements.
- Zero-mass / missing-inertial links are flagged, not silently accepted.
- Structure hash covers the kinematic topology (joints + links), so a moved
  mount or re-parented joint changes it; cosmetic reordering does not.
- Unresolved xacro / ROS substitutions are refused — never treated as link names.
"""

import datetime as dt
import hashlib
import re
import typing
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from lockwire.schema import (
    Joint,
    JointLimits,
    JointOrigin,
    Link,
    MeshRef,
    ModelRecord,
    PrincipalInertia,
    Provenance,
    Quantity,
    _model_structure_hash,
)

# `${var}` in a name is xacro property substitution (TurtleBot3 ships this unexpanded).
# A bare `$` (e.g. link `cost_$`) is NOT matched — that would be a false positive.
_XACRO_PROP = re.compile(r"\$\{[^}]+\}")
# ROS command substitutions that survive only when xacro was never run.
_ROS_SUBST = re.compile(r"\$\((?:arg|find)\b")
_REMEDY = "Expand the xacro first, then re-run: xacro model.urdf.xacro > model.urdf"


class UnresolvedXacroError(ValueError):
    """Input still contains xacro/ROS substitutions; refuse rather than invent names."""


class ModelDefect(ValueError):
    """The model names a link it lacks, or a name twice: a real defect, not a tool failure."""


class NotANumber(ValueError):
    """A numeric attribute that does not parse; the message names the element and attribute."""


def _num(raw: str, where: str) -> float:
    try:
        return float(raw)
    except ValueError:
        hint = " (use a dot for decimals)" if "," in raw else ""
        raise NotANumber(f"{where} is {raw!r}, not a number{hint}") from None


# A plain URDF that pulls one file from a ROS package: skipped, the rest is still read.
_FIND_INCLUDE = re.compile(
    rb"""<xacro:include\b[^>]*?filename=["']([^"']*\$\(find[^"']*)["'][^>]*?(?:/>|>.*?</xacro:include>)""",
    re.S,
)


def _find_in_checkout(model: Path, pkg: str) -> Path | None:
    """`$(find pkg)` from the model's own checkout: the model's folder and its ancestors up
    to the repo root (the first holding a git folder), searched 3 deep. With no git folder it
    stops one level above the model's own package (its workspace), or at the model's folder when
    it is in no package, so a stray folder in a shared parent (tmp, home) is never found.
    Symlinked folders are not followed, so nothing outside is read."""
    from lockwire.scene_urdf import _find_package_dir  # lazy: scene_urdf imports this module

    here = model.resolve().parent
    ups = [here, *here.parents][:4]
    top = next((d.parent for d in ups if (d / "package.xml").is_file()), here)
    if any((d / ".git").exists() for d in ups):
        top = None
    for root in ups:
        found = _find_package_dir(root, pkg, max_depth=3, follow_links=False)
        if found is not None:
            return found
        if (root / ".git").exists() or root == top:
            break
    return None


def read_model_bytes(
    path: Path, xacro_args: dict[str, str] | None = None, warnings: list[str] | None = None
) -> bytes:
    """The model's URDF/SDF bytes; xacro (a ``.xacro`` file, or any file with ``${``,
    ``$(`` or ``<xacro:``) is expanded in memory (no ROS needed).

    A failed expansion (including ``$(find pkg)``, which needs ROS) raises
    ``UnresolvedXacroError`` carrying xacro's own message plus the manual remedy.
    ``xacro_args`` are the ``name:=value`` arguments.
    """
    raw = path.read_bytes()
    stripped = _FIND_INCLUDE.sub(b"", raw)  # unchanged when there is no $(find) include
    if path.suffix.lower() != ".xacro" and not any(
        t in stripped for t in (b"${", b"$(", b"<xacro:")
    ):
        for name in _FIND_INCLUDE.findall(raw) if warnings is not None else ():
            warnings.append(
                f"skipped <xacro:include filename={name.decode(errors='replace')!r}>: it needs "
                "a ROS environment; checked the links and joints in this file only"
            )
        return stripped
    _refuse_doctype_entity(raw, path)
    import xacro

    from xacro import substitution_args as sub

    ros_find = sub._eval_find

    def find(pkg):  # the ROS environment first, then the checkout; else the ROS error
        try:
            return ros_find(pkg)
        except Exception:
            if (d := _find_in_checkout(path, pkg)) is None:
                raise
            return str(d)

    mappings, wanted = dict(xacro_args or {}), {}
    while True:
        sub._eval_find = find
        try:
            out = xacro.process_file(str(path), mappings=mappings).toxml().encode()
            failure = None
        except Exception as exc:  # xacro raises XacroException, ParseError, even SystemExit-ish
            out, failure = b"", exc
        finally:
            sub._eval_find = ros_find
        undefined = re.fullmatch(r"Undefined substitution argument (\S+)", str(failure))
        if undefined and undefined[1] not in mappings:
            # ponytail: declarations in this file only; an <xacro:include>d file's defaults are not read
            decl = re.search(
                rf"""<xacro:arg\s+name=["']{re.escape(undefined[1])}["']\s+default=["']([^"']*)""",
                raw.decode(errors="replace"),
            )
            wanted[undefined[1]] = mappings[undefined[1]] = decl[1] if decl else ""
            continue
        break
    if wanted:  # the file works only with arguments the user never gave
        args = " ".join(f"--xacro-arg {k}={v or '<value>'}" for k, v in wanted.items())
        raise UnresolvedXacroError(
            f"{path}: this xacro file needs arguments you did not give. Run: "
            f"lockwire start {path.name} {args}"
        )
    if failure is None:
        return out
    if "ament_index_python" in str(failure):
        raise UnresolvedXacroError(
            f"{path}: xacro expansion failed — $(find …) needs a ROS environment. Expand it "
            f"there (xacro model.urdf.xacro > model.urdf), then re-run. ({failure})"
        ) from failure
    raise UnresolvedXacroError(
        f"{path}: unresolved xacro — xacro expansion failed: {failure}. {_REMEDY}"
    ) from failure


def _local_tag(tag: str) -> str:
    """Strip Clark notation `{ns}local` → `local`; leave un-namespaced tags alone."""
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def robot_name(path: Path) -> str:
    """The URDF's own ``<robot name>``; the file stem when it has none."""
    try:
        return ET.fromstring(read_model_bytes(path)).get("name") or path.stem
    except (OSError, ET.ParseError, UnresolvedXacroError):
        return path.stem


def _refuse_unresolved_xacro(root: ET.Element, path: Path) -> None:
    """Refuse unexpanded xacro rather than treat template tokens as robot links.

    Detection order (most → least specific to the TurtleBot3 miss):
    1. `${…}` surviving in a link or joint *name* (or parent/child link ref)
    2. a `<xacro:…>` element or the xacro XML namespace still in the tree
    3. `$(arg …)` / `$(find …)` in any attribute value

    False-positive shape we deliberately do *not* match: a bare `$` in a name
    (`link_$1`, `cost_usd`) — only braced `${…}` property syntax counts.
    """
    for el in root.findall("link"):
        name = el.get("name") or ""
        if _XACRO_PROP.search(name):
            raise UnresolvedXacroError(
                f"{path}: unresolved xacro — link name {name!r} still contains "
                f"${{…}} substitutions. This is a xacro template, not a finished URDF. "
                f"{_REMEDY}"
            )
    for el in root.findall("joint"):
        name = el.get("name") or ""
        if _XACRO_PROP.search(name):
            raise UnresolvedXacroError(
                f"{path}: unresolved xacro — joint name {name!r} still contains "
                f"${{…}} substitutions. This is a xacro template, not a finished URDF. "
                f"{_REMEDY}"
            )
        for end in ("parent", "child"):
            ref = el.find(end)
            link_name = ref.get("link", "") if ref is not None else ""
            if _XACRO_PROP.search(link_name):
                raise UnresolvedXacroError(
                    f"{path}: unresolved xacro — joint {name!r} {end} link "
                    f"{link_name!r} still contains ${{…}} substitutions. "
                    f"This is a xacro template, not a finished URDF. {_REMEDY}"
                )

    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        local = _local_tag(tag)
        # Un-namespaced `xacro:foo` (common in shipped .xacro) or `{…xacro…}foo`.
        if local.startswith("xacro:") or (
            tag.startswith("{") and "xacro" in tag[1:].split("}", 1)[0]
        ):
            raise UnresolvedXacroError(
                f"{path}: unresolved xacro — <{local}> element remains in the document. "
                f"This is a xacro template, not a finished URDF. {_REMEDY}"
            )
        for attr_val in el.attrib.values():
            if _ROS_SUBST.search(attr_val):
                raise UnresolvedXacroError(
                    f"{path}: unresolved xacro — $(arg …)/$(find …) substitutions remain "
                    f"in attribute values. This is a xacro template, not a finished URDF. "
                    f"{_REMEDY}"
                )


def _principal(
    ixx: float, ixy: float, ixz: float, iyy: float, iyz: float, izz: float
) -> PrincipalInertia:
    tensor = np.array([[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]])
    moments, axes = np.linalg.eigh(tensor)  # ascending; columns = eigenvectors
    return PrincipalInertia(
        moments_kgm2=tuple(float(m) for m in moments),
        axes=tuple(tuple(float(v) for v in axes[:, i]) for i in range(3)),
    )


def _parse_link(el: ET.Element, notes: list[str] | None = None) -> tuple[Link, bool]:
    """Returns (link, is_zero_mass). A NaN or negative mass counts as zero, with a note."""
    name = el.get("name", "?")
    inertial = el.find("inertial")
    if inertial is None:
        return Link(name=name), True

    mass_el = inertial.find("mass")
    mass_val = (
        _num(mass_el.get("value", "0"), f"link {name!r} mass value") if mass_el is not None else 0.0
    )
    if not mass_val > 0.0:
        if mass_val != 0.0 and notes is not None:
            notes.append(f"link {name!r} has mass {mass_el.get('value')}; treated as zero mass.")
        return Link(name=name), True

    origin = inertial.find("origin")
    xyz = tuple(
        _num(x, f"link {name!r} inertial origin xyz")
        for x in (origin.get("xyz", "0 0 0") if origin is not None else "0 0 0").split()
    )

    inertia_el = inertial.find("inertia")
    inertia = None
    if inertia_el is not None:
        inertia = _principal(
            *(
                _num(inertia_el.get(k, "0"), f"link {name!r} inertia {k}")
                for k in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
            )
        )

    link = Link(
        name=name,
        mass=Quantity(value=mass_val, unit="kg"),
        com=xyz,
        frame=name,  # URDF inertial origin is expressed in the link frame
        inertia=inertia,
    )
    return link, False


def _xyz_rpy(
    origin: ET.Element | None, where: str = "origin"
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    xyz = tuple(
        _num(x, f"{where} xyz")
        for x in (origin.get("xyz", "0 0 0") if origin is not None else "0 0 0").split()
    )
    rpy = tuple(
        _num(x, f"{where} rpy")
        for x in (origin.get("rpy", "0 0 0") if origin is not None else "0 0 0").split()
    )
    return xyz, rpy  # type: ignore[return-value]


def _structure_hash(root: ET.Element) -> str:
    """Hash of the kinematic topology: sorted joints (name, type, parent, child,
    origin) + sorted link names. Rounded so float noise doesn't churn the hash."""
    lines: list[str] = []
    for joint in root.findall("joint"):
        parent = joint.find("parent")
        child = joint.find("child")
        xyz, rpy = _xyz_rpy(joint.find("origin"))
        nums = " ".join(f"{v:.6f}" for v in xyz + rpy)
        lines.append(
            "|".join(
                [
                    joint.get("name", "?"),
                    joint.get("type", "?"),
                    parent.get("link", "?") if parent is not None else "?",
                    child.get("link", "?") if child is not None else "?",
                    nums,
                ]
            )
        )
    lines.sort()
    lines += sorted(link.get("name", "?") for link in root.findall("link"))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


_AXIS_TYPES = ("revolute", "continuous", "prismatic", "planar")


def _same_axis(locked: Joint, disk: Joint) -> bool:
    """Equal axes; an unset locked axis equals the default on axis-bearing joints."""
    if locked.type in ("fixed", "floating") and disk.type == locked.type:
        return True
    if locked.axis is None and disk.type in _AXIS_TYPES:
        return disk.axis == (1.0, 0.0, 0.0)
    return disk.axis == locked.axis


def legacy_axis_joints(model: ModelRecord, topology: "UrdfTopology") -> list[str]:
    """Joints a pre-0.5 lock recorded as (0, 0, 1) from an ``<axis/>`` with no xyz."""
    locked = {j.name: j.axis for j in model.joints}
    return sorted(n for n in topology.empty_axis if locked.get(n) == (0.0, 0.0, 1.0))


def locked_record_hash(model: ModelRecord, topology: "UrdfTopology", legacy: bool = False) -> str:
    """The on-disk record hash, reading axes the way the lock was written.

    ``legacy``: the lock predates schema 0.5, which read an ``<axis/>`` with no xyz as (0, 0, 1).
    """
    locked = {j.name: j for j in model.joints}
    old = set(legacy_axis_joints(model, topology)) if legacy else set()
    joints = [
        j.model_copy(update={"axis": None})
        if j.name in locked and locked[j.name].axis is None and _same_axis(locked[j.name], j)
        else j.model_copy(update={"axis": (0.0, 0.0, 1.0)})
        if j.name in old
        else j
        for j in topology.joints
    ]
    return _model_structure_hash([Link(name=n) for n in topology.link_names], joints)


_JOINT_TYPES = typing.get_args(Joint.model_fields["type"].annotation)


def _parse_joint(
    el: ET.Element, notes: list[str] | None = None, warnings: list[str] | None = None
) -> Joint:
    """URDF joint -> schema Joint; consumed by URDF drift.

    ``warnings`` collects lines the check reports: an infinite, DBL_MAX or negative-velocity
    limit is read as not stated, an unknown joint type as ball (spherical) or floating."""
    name = el.get("name", "?")
    warn = warnings.append if warnings is not None else lambda _line: None
    parent = el.find("parent")
    child = el.find("child")
    xyz, rpy = _xyz_rpy(el.find("origin"), f"joint {name!r} origin")
    axis_el = el.find("axis")
    jtype = el.get("type", "fixed")
    if jtype not in _JOINT_TYPES:
        given, jtype = jtype, "ball" if jtype == "spherical" else "floating"
        warn(f"unsupported joint type {given!r} on joint {name!r}; checked as {jtype}")
    unbounded: list[str] = []

    def limit(key: str) -> float | None:
        raw = limit_el.get(key)
        value = None if raw is None else _num(raw, f"joint {name!r} limit {key}")
        if value is not None and not abs(value) < 1e300:  # inf, DBL_MAX or nan
            unbounded.append(f"{key} {raw}")
            return None
        if value is not None and key == "velocity" and value < 0:  # -1: "unknown" by convention
            warn(f"joint {name!r} velocity limit is {raw}; treated as not stated")
            return None
        return value

    # URDF spec: absent <axis> or <axis/> without xyz means (1, 0, 0), only on joints with an axis
    axis = None
    if axis_el is not None or jtype in _AXIS_TYPES:
        axis = tuple(
            _num(x, f"joint {name!r} axis xyz")
            for x in ((axis_el.get("xyz") if axis_el is not None else None) or "1 0 0").split()
        )
    limit_el = el.find("limit")
    limits = None
    # A mimic joint follows its parent and has no limits of its own (OnRobot's RG6 ships 0/0/effort 0).
    if limit_el is not None and el.find("mimic") is None:
        lo, hi = limit("lower"), limit("upper")
        if jtype in ("revolute", "prismatic") and None not in (lo, hi) and lo >= hi:
            # A reversed range would abort the whole lock: treat as unlimited and say so.
            if notes is not None:
                notes.append(f"joint {el.get('name')!r} has lower {lo} >= upper {hi}; unlimited.")
        else:
            limits = JointLimits(
                lower=lo, upper=hi, effort=limit("effort"), velocity=limit("velocity")
            )
    if unbounded:
        warn(f"joint {name!r} limit {', '.join(unbounded)}; treated as unbounded")
    return Joint(
        name=name,
        type=jtype,  # type: ignore[arg-type]
        parent=parent.get("link", "?") if parent is not None else "?",
        child=child.get("link", "?") if child is not None else "?",
        origin=JointOrigin(xyz=xyz, rpy=rpy),
        axis=axis,
        limits=limits,
    )


@dataclass(frozen=True)
class UrdfTopology:
    """Link/joint sets extracted for structure-hash drift comparison."""

    link_names: frozenset[str]
    joints: tuple[Joint, ...]
    topology_hash: str  # v0.1 URDF kinematic hash (links + joint poses)
    record_hash: str  # v0.2 hash over Link/Joint records (axis + limits load-bearing)
    empty_axis: frozenset[str] = frozenset()  # joints whose <axis> has no xyz


_DOCTYPE_ENTITY_FORBIDDEN = (
    "XML DOCTYPE/ENTITY declarations are not permitted (billion-laughs risk)"
)


def _xml_text(raw: bytes) -> str:
    """Decode XML bytes the way a parser would: BOM, else NUL-pattern, else declared encoding."""
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        enc = "utf-32"
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    elif raw[:2] == b"<\x00":
        enc = "utf-16-le"
    elif raw[:2] == b"\x00<":
        enc = "utf-16-be"
    elif raw[:4] in (b"<\x00\x00\x00", b"\x00\x00\x00<"):
        enc = "utf-32-le" if raw[:1] == b"<" else "utf-32-be"
    else:
        m = re.match(rb"""\s*<\?xml[^>]*encoding\s*=\s*["']([\w.-]+)""", raw)
        enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("latin-1")


def _has_doctype_entity(raw: bytes) -> bool:
    # raw bytes too: a lying declaration must not hide an ASCII DOCTYPE
    txt = _xml_text(raw)
    return any(t in txt or t.encode() in raw for t in ("<!DOCTYPE", "<!ENTITY"))


def _refuse_doctype_entity(raw: bytes, path: Path) -> None:
    """Refuse a DOCTYPE/ENTITY-bearing URDF before ``ET.fromstring`` ever sees it.

    stdlib ``xml.etree`` does not resolve *external* entities, but an internal
    entity declared and referenced within the same document still expands —
    a 336-byte billion-laughs file grows to a multi-megabyte attribute with no
    error. Refusing on the raw bytes, the same shape ``schema.py`` uses for
    YAML anchors/aliases, means the parser never has a chance to expand it.
    """
    if _has_doctype_entity(raw):
        raise ValueError(f"{path}: {_DOCTYPE_ENTITY_FORBIDDEN}")


def read_urdf_topology(path: Path, xacro_args: dict[str, str] | None = None) -> UrdfTopology:
    """Read on-disk URDF topology for drift checks. Raises on I/O or parse errors."""
    raw = read_model_bytes(path, xacro_args)
    _refuse_doctype_entity(raw, path)
    root = ET.fromstring(raw)
    if root.tag != "robot":
        raise ValueError(f"{path}: root element is <{root.tag}>, expected <robot>")
    _refuse_unresolved_xacro(root, path)
    links = [Link(name=el.get("name", "?")) for el in root.findall("link")]
    joints = tuple(_parse_joint(el) for el in root.findall("joint"))
    return UrdfTopology(
        link_names=frozenset(link.name for link in links),
        joints=joints,
        topology_hash=_structure_hash(root),
        record_hash=_model_structure_hash(links, list(joints)),
        empty_axis=frozenset(
            el.get("name", "?")
            for el in root.findall("joint")
            if el.find("axis") is not None and el.find("axis").get("xyz") is None
        ),
    )


def describe_structure_drift(model: ModelRecord, topology: UrdfTopology) -> str:
    """Name added/removed/moved link and joint elements; not merely 'hash differs'."""
    contract_links = {link.name for link in model.links}
    parts: list[str] = []
    added_links = sorted(topology.link_names - contract_links)
    removed_links = sorted(contract_links - topology.link_names)
    if added_links:
        parts.append("link added: " + ", ".join(added_links))
    if removed_links:
        parts.append("link removed: " + ", ".join(removed_links))

    # v0.1 init records omit joints[]; only diff joints when the contract locked them.
    if model.joints:
        disk_joints = {joint.name: joint for joint in topology.joints}
        contract_joints = {joint.name: joint for joint in model.joints}
        added_joints = sorted(disk_joints.keys() - contract_joints.keys())
        removed_joints = sorted(contract_joints.keys() - disk_joints.keys())
        if added_joints:
            parts.append("joint added: " + ", ".join(added_joints))
        if removed_joints:
            parts.append("joint removed: " + ", ".join(removed_joints))

        moved: list[str] = []
        for name in sorted(disk_joints.keys() & contract_joints.keys()):
            disk = disk_joints[name]
            locked = contract_joints[name]
            changes: list[str] = []
            if (disk.parent, disk.child) != (locked.parent, locked.child):
                changes.append(
                    f"parent/child {locked.parent}->{locked.child} -> {disk.parent}->{disk.child}"
                )
            if disk.type != locked.type:
                changes.append(f"type {locked.type} -> {disk.type}")
            if disk.origin.xyz != locked.origin.xyz or disk.origin.rpy != locked.origin.rpy:
                changes.append("origin moved")
            if not _same_axis(locked, disk):
                changes.append("axis changed")
            if disk.limits != locked.limits:
                changes.append("limits changed")
            if changes:
                moved.append(f"{name} ({'; '.join(changes)})")
        if moved:
            parts.append("joint moved: " + ", ".join(moved))
    elif topology.joints:
        # Contract predates this fix (or was locked from a joint-less source): the
        # hash may still be moving on joint changes the link diff above can't see.
        parts.append("joints not recorded in this contract — re-run init to record them")

    if not parts:
        parts.append("kinematic topology changed")
    return "; ".join(parts)


def mesh_refs(root: ET.Element) -> list[MeshRef]:
    """Each link's mesh file and scale (URDF attributes or SDF children), sorted."""
    rows = {
        (
            link.get("name", ""),
            uri,
            " ".join((m.get("scale") or m.findtext("scale") or "1 1 1").split()),
        )
        for link in root.iter("link")
        for m in link.iter("mesh")
        if (uri := m.get("filename") or m.findtext("uri"))
    }
    return [MeshRef(link=name, uri=uri, scale=scale) for name, uri, scale in sorted(rows)]


def parse_urdf(
    path: Path,
    xacro_args: dict[str, str] | None = None,
    notes: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ModelRecord:
    """``notes`` collects plain-words lines about tolerated oddities; ``warnings`` the ones a
    check reports as findings. A missing or duplicate name raises ``ModelDefect``."""
    raw = read_model_bytes(
        path, xacro_args, warnings
    )  # for .xacro: the expanded bytes, so an include edit is drift
    _refuse_doctype_entity(raw, path)
    root = ET.fromstring(raw)
    if root.tag != "robot":
        raise ValueError(f"{path}: root element is <{root.tag}>, expected <robot>")
    _refuse_unresolved_xacro(root, path)
    names = [el.get("name", "") for el in root.findall("link")]
    if not names:
        raise ValueError(f"{path} has no <link>; this is not a robot model")
    if "" in names:
        raise ValueError(f"{path} has a <link> with an empty name; give every link a name")

    links: list[Link] = []
    zero_mass: list[str] = []
    try:
        for el in root.findall("link"):
            link, is_zero = _parse_link(el, notes)
            links.append(link)
            if is_zero:
                zero_mass.append(link.name)
        joints = [_parse_joint(el, notes, warnings) for el in root.findall("joint")]
    except NotANumber as error:
        raise NotANumber(f"{path}: {error}") from None

    names = [link.name for link in links]
    for kind, rows in (("link", names), ("joint", [joint.name for joint in joints])):
        if twice := sorted({n for n in rows if rows.count(n) > 1}):
            raise ModelDefect(f"{path}: {kind} name {twice[0]!r} is used more than once")
    for joint in joints:
        for end, link_name in (("parent", joint.parent), ("child", joint.child)):
            if link_name not in names:
                raise ModelDefect(
                    f"{path}: joint {joint.name!r} names {end} link {link_name!r}, "
                    "which the model does not have"
                )

    return ModelRecord(
        source=str(path),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        # v0.1 hash; overridden by the schema validator once joints (below) are non-empty.
        structure_hash=_structure_hash(root),
        mesh_refs=mesh_refs(root),
        links=links,
        joints=joints,
        zero_mass_links=sorted(zero_mass),
        provenance=Provenance(witness="w2", source=str(path), locked_at=dt.date.today()),
    )
