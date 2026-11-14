"""SDF -> Tier A extraction (`lockwire init`/`start`). Stdlib only for XML.

Reuses `urdf._principal` (inertia tensor -> principal moments/axes, same trap
as URDF: inertia is about the COM in the inertial frame) and `urdf._model_structure_hash`
via `schema.py`'s validator, plus `scene_urdf`'s pure-python pose algebra (already
used to place SDF/URDF visuals) so the joint-origin conversion below and the
scene renderer agree on what a pose means.

Pose conversion (the one trap this module exists to handle):

SDF gives every `<link>` a `<pose>` that is model-relative (this build does not
walk `relative_to` chains — same assumption `scene_urdf._scene_from_sdf` makes).
A `<joint>`'s own `<pose>` is relative to the CHILD link frame unless a
`relative_to` attribute says otherwise (SDF spec v1.9,
http://sdformat.org/spec?ver=1.9&elem=joint, `pose` element description).
URDF's `<joint><origin>`, by contrast, *is* the parent-frame pose of the child
link frame — it's how URDF places the child at all.

So to store a joint origin here that means what URDF's `<origin>` means, this
composes: joint_frame_in_model = child_link_pose ∘ joint.pose (child-relative
offset), then converts to the parent's frame:

    origin_in_parent = inverse(parent_link_pose) ∘ joint_frame_in_model

When (as in nearly every real SDF, including PX4 Vector's) the joint carries no
`<pose>` of its own, this reduces to `inverse(parent) ∘ child` — the child's
pose expressed in the parent's frame, which is exactly the URDF-equivalence a
mixed-format drift check needs.
"""

import datetime as dt
import hashlib
import xml.etree.ElementTree as ET
from math import asin, atan2, cos
from pathlib import Path

from lockwire.schema import (
    Joint,
    JointLimits,
    JointOrigin,
    Link,
    ModelRecord,
    PrincipalInertia,
    Provenance,
    Quantity,
    _model_structure_hash,
)
from lockwire.scene_urdf import (
    _IDENTITY_POSE,
    _apply_r,
    _compose,
    _first_model,
    _mat3_mul,
    _Pose,
    env_package_file,
)
from lockwire.urdf import UrdfTopology, _principal, _refuse_doctype_entity, mesh_refs


def _sdf_pose(
    pose_el: ET.Element | None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    text = pose_el.text if pose_el is not None and pose_el.text else "0 0 0 0 0 0"
    vals = [float(x) for x in text.split()]
    vals += [0.0] * (6 - len(vals))
    return (vals[0], vals[1], vals[2]), (vals[3], vals[4], vals[5])


def _pose_mul(a: _Pose, b: _Pose) -> _Pose:
    ra, ta = a
    rb, tb = b
    r_new = _mat3_mul(ra, rb)
    dt_ = _apply_r(ra, tb)
    t_new = (ta[0] + dt_[0], ta[1] + dt_[1], ta[2] + dt_[2])
    return r_new, t_new


def _pose_inverse(pose: _Pose) -> _Pose:
    r_mat, t = pose
    r_t = tuple(tuple(r_mat[j][i] for j in range(3)) for i in range(3))
    t_inv = tuple(-x for x in _apply_r(r_t, t))
    return r_t, t_inv


def _matrix_to_rpy(r_mat: tuple[tuple[float, float, float], ...]) -> tuple[float, float, float]:
    """Inverse of `scene_urdf._rpy_matrix` (URDF/SDF's Rz(yaw)*Ry(pitch)*Rx(roll))."""
    sp = max(-1.0, min(1.0, -r_mat[2][0]))
    pitch = asin(sp)
    cp = cos(pitch)
    if abs(cp) > 1e-8:
        roll = atan2(r_mat[2][1], r_mat[2][2])
        yaw = atan2(r_mat[1][0], r_mat[0][0])
    else:
        # Gimbal lock: roll/yaw are not independently recoverable; pin roll=0.
        roll = 0.0
        yaw = atan2(-r_mat[0][1], r_mat[1][1])
    # `-0.0 + 0.0 == 0.0`: normalize sign so an unrotated axis hashes identically
    # to the URDF door's literal `0.0`, instead of `json.dumps` emitting "-0.0".
    return roll + 0.0, pitch + 0.0, yaw + 0.0


def _link_poses(model_el: ET.Element) -> dict[str, _Pose]:
    poses: dict[str, _Pose] = {}
    for link in model_el.findall("link"):
        name = link.get("name", "?")
        xyz, rpy = _sdf_pose(link.find("pose"))
        poses[name] = _compose(_IDENTITY_POSE, xyz, rpy)
    return poses


def _parse_link(link_el: ET.Element) -> tuple[Link, bool]:
    """Returns (link, is_zero_mass). Mirrors `urdf._parse_link`."""
    name = link_el.get("name", "?")
    inertial = link_el.find("inertial")
    if inertial is None:
        return Link(name=name), True

    mass_text = inertial.findtext("mass")
    mass_val = float(mass_text) if mass_text is not None else 0.0
    if mass_val == 0.0:
        return Link(name=name), True

    xyz, _rpy = _sdf_pose(inertial.find("pose"))

    inertia_el = inertial.find("inertia")
    inertia: PrincipalInertia | None = None
    if inertia_el is not None:
        inertia = _principal(
            *(
                float(inertia_el.findtext(k) or "0")
                for k in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
            )
        )

    link = Link(
        name=name, mass=Quantity(value=mass_val, unit="kg"), com=xyz, frame=name, inertia=inertia
    )
    return link, False


def _parse_joints(model_el: ET.Element, link_poses: dict[str, _Pose]) -> list[Joint]:
    joints: list[Joint] = []
    for joint_el in model_el.findall("joint"):
        name = joint_el.get("name", "?")
        parent = joint_el.findtext("parent") or "?"
        child = joint_el.findtext("child") or "?"

        j_xyz, j_rpy = _sdf_pose(joint_el.find("pose"))
        child_pose = link_poses.get(child, _IDENTITY_POSE)
        joint_frame_in_model = _compose(child_pose, j_xyz, j_rpy)
        parent_pose = link_poses.get(parent, _IDENTITY_POSE)
        origin_in_parent = _pose_mul(_pose_inverse(parent_pose), joint_frame_in_model)
        r_origin, t_origin = origin_in_parent
        origin = JointOrigin(xyz=t_origin, rpy=_matrix_to_rpy(r_origin))  # type: ignore[arg-type]

        axis_el = joint_el.find("axis")
        axis = None
        limits = None
        if axis_el is not None:
            axis_text = axis_el.findtext("xyz")
            if axis_text:
                axis = tuple(float(x) for x in axis_text.split())  # type: ignore[assignment]
            limit_el = axis_el.find("limit")
            if limit_el is not None:
                lower_raw = limit_el.findtext("lower")
                upper_raw = limit_el.findtext("upper")
                effort_raw = limit_el.findtext("effort")
                velocity_raw = limit_el.findtext("velocity")
                limits = JointLimits(
                    lower=float(lower_raw) if lower_raw is not None else None,
                    upper=float(upper_raw) if upper_raw is not None else None,
                    effort=float(effort_raw) if effort_raw is not None else None,
                    velocity=float(velocity_raw) if velocity_raw is not None else None,
                )

        joints.append(
            Joint(
                name=name,
                type=joint_el.get("type", "fixed"),  # type: ignore[arg-type]
                parent=parent,
                child=child,
                origin=origin,
                axis=axis,
                limits=limits,
            )
        )
    return joints


def _model_element(root: ET.Element, path: Path) -> ET.Element:
    if root.tag != "sdf":
        raise ValueError(f"{path}: root element is <{root.tag}>, expected <sdf>")
    model_el = _first_model(root)
    if model_el is None:
        raise ValueError(f"{path}: no <model> element found")
    return model_el


def _flatten_includes(model_el: ET.Element, model_dir: Path, seen: tuple[Path, ...]) -> None:
    """Splice each `<include><uri>model://X</uri>` into `model_el` as `Name::link`.

    X is looked up as the sibling folder `model_dir/../X/model.sdf` (Gazebo's model-folder
    layout), then on the Gazebo model path the scene uses (`scene_urdf.env_package_file`). The include's `<pose>` (else the included model's own) places its links.
    """
    for inc in model_el.findall("include"):
        uri = (inc.findtext("uri") or "").strip()
        name = uri.removeprefix("model://")
        sub_path = model_dir.parent / name / "model.sdf"
        if uri.startswith("model://") and not sub_path.is_file():
            sub_path = env_package_file(name, "model.sdf") or sub_path
        if not uri.startswith("model://") or not sub_path.is_file():
            raise ValueError(
                f"could not find {uri or '<include> uri'}, looked in {model_dir.parent} and "
                "the Gazebo model path (GZ_SIM_RESOURCE_PATH, GAZEBO_MODEL_PATH)"
            )
        sub_dir = sub_path.resolve().parent
        if sub_dir in seen:
            raise ValueError(f"{sub_path}: <include> loop through {uri}")
        raw = sub_path.read_bytes()
        _refuse_doctype_entity(raw, sub_path)
        sub = _model_element(ET.fromstring(raw), sub_path)
        _flatten_includes(sub, sub_dir, (*seen, sub_dir))
        prefix = (inc.findtext("name") or sub.get("name", "?")) + "::"
        inc_pose = inc.find("pose")
        place = _compose(
            _IDENTITY_POSE, *_sdf_pose(sub.find("pose") if inc_pose is None else inc_pose)
        )
        for link in sub.findall("link"):
            link.set("name", prefix + link.get("name", "?"))
            r_mat, t = _compose(place, *_sdf_pose(link.find("pose")))
            pose_el = link.find("pose")
            if pose_el is None:
                pose_el = ET.SubElement(link, "pose")
            pose_el.text = " ".join(str(v) for v in (*t, *_matrix_to_rpy(r_mat)))
            model_el.append(link)
        for joint in sub.findall("joint"):
            joint.set("name", prefix + joint.get("name", "?"))
            for end in (joint.find("parent"), joint.find("child")):
                if end is not None and end.text and end.text.strip() != "world":
                    end.text = prefix + end.text.strip()
            model_el.append(joint)
        model_el.remove(inc)


def read_sdf_root(path: Path) -> tuple[ET.Element, ET.Element]:
    """(root, model) of `path` with every `<include>` spliced in."""
    raw = path.read_bytes()
    _refuse_doctype_entity(raw, path)
    root = ET.fromstring(raw)
    model_el = _model_element(root, path)
    model_dir = path.resolve().parent
    _flatten_includes(model_el, model_dir, (model_dir,))
    return root, model_el


def loop_cuts(path: Path) -> dict[str, tuple[float, float, float]]:
    """Each joint that closes a kinematic loop -> its pin, a point in its child link's frame.

    A record places a link at the joint above it, which is only true for a joint with no
    ``<pose>`` of its own. So into a link with several parent joints exactly one must sit on the
    link's frame (it is kept) and the others are the cuts, wherever the file declares them.
    ValueError for a file the torque walk would misplace: a looped link with no such joint, or a
    kept joint anywhere that carries a ``<pose>``.
    """
    _root, model_el = read_sdf_root(path)
    into: dict[str, list[ET.Element]] = {}
    for el in model_el.findall("joint"):
        into.setdefault(el.findtext("child") or "?", []).append(el)
    here = _sdf_pose(None)
    cuts: dict[str, tuple[float, float, float]] = {}
    for link, els in into.items():
        off = [e for e in els if _sdf_pose(e.find("pose")) != here]
        if len(els) > 1 and len(off) != len(els) - 1:
            raise ValueError(f"closed chain: link '{link}' needs one parent joint with no <pose>")
        if len(els) == 1 and off:
            raise ValueError(
                f"closed chain: joint '{off[0].get('name', '?')}' has a <pose> off its child link"
            )
        cuts.update({e.get("name", "?"): _sdf_pose(e.find("pose"))[0] for e in off if len(els) > 1})
    return cuts


def _urdf_geometry(geometry: ET.Element | None) -> ET.Element | None:
    shape = geometry[0] if geometry is not None and len(geometry) else None
    if shape is None:
        return None
    out = ET.Element("geometry")
    if shape.tag == "box":
        ET.SubElement(out, "box", size=shape.findtext("size") or "0 0 0")
    elif shape.tag in ("cylinder", "sphere"):
        attrs = {k: shape.findtext(k) for k in ("radius", "length") if shape.findtext(k)}
        ET.SubElement(out, shape.tag, attrs)
    elif shape.tag == "mesh":
        ET.SubElement(
            out,
            "mesh",
            filename=shape.findtext("uri") or "",
            scale=shape.findtext("scale") or "1 1 1",
        )
    else:
        return None
    return out


def urdf_view(path: Path) -> ET.Element:
    """The SDF (includes spliced in) as a URDF-shaped `<robot>`, for the checks that read URDF XML.

    Links keep their collision/visual shapes with each one's link-relative `<pose>` as `<origin>`;
    joints keep type, parent, child, axis and limits. A missing SDF `<velocity>` means unlimited,
    so it is written as SDF's own default -1, never as URDF's required-but-missing attribute.
    """
    _root, model_el = read_sdf_root(path)
    robot = ET.Element("robot", name=model_el.get("name", "?"))
    for link in model_el.findall("link"):
        out = ET.SubElement(robot, "link", name=link.get("name", "?"))
        if (mass := link.findtext("inertial/mass")) is not None:
            inertial = ET.SubElement(out, "inertial")
            ET.SubElement(inertial, "mass", value=mass)
            if (tensor := link.find("inertial/inertia")) is not None:
                ET.SubElement(
                    inertial,
                    "inertia",
                    {
                        k: tensor.findtext(k) or "0"
                        for k in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
                    },
                )
        for kind in ("collision", "visual"):
            for el in link.findall(kind):
                geometry = _urdf_geometry(el.find("geometry"))
                if geometry is None:
                    continue
                part = ET.SubElement(out, kind)
                xyz, rpy = _sdf_pose(el.find("pose"))
                ET.SubElement(
                    part, "origin", xyz=" ".join(map(str, xyz)), rpy=" ".join(map(str, rpy))
                )
                part.append(geometry)
    for joint in model_el.findall("joint"):
        out = ET.SubElement(
            robot, "joint", name=joint.get("name", "?"), type=joint.get("type", "fixed")
        )
        ET.SubElement(out, "parent", link=joint.findtext("parent") or "?")
        ET.SubElement(out, "child", link=joint.findtext("child") or "?")
        axis = joint.find("axis")
        if axis is not None and axis.findtext("xyz"):
            ET.SubElement(out, "axis", xyz=axis.findtext("xyz"))
        limit = axis.find("limit") if axis is not None else None
        limit = ET.Element("limit") if limit is None else limit  # SDF: no <limit> = unlimited
        attrs = {k: limit.findtext(k) for k in ("lower", "upper", "effort") if limit.findtext(k)}
        ET.SubElement(out, "limit", attrs, velocity=limit.findtext("velocity") or "-1")
    return robot


def _parse(path: Path) -> tuple[ET.Element, list[Link], list[str], list[Joint]]:
    root, model_el = read_sdf_root(path)

    link_els = model_el.findall("link")
    if not link_els:
        raise ValueError(f"{path}: no <link> elements found")

    links: list[Link] = []
    zero_mass: list[str] = []
    for link_el in link_els:
        link, is_zero = _parse_link(link_el)
        links.append(link)
        if is_zero:
            zero_mass.append(link.name)

    link_poses = _link_poses(model_el)
    joints = _parse_joints(model_el, link_poses)
    return root, links, sorted(zero_mass), joints


def parse_sdf(path: Path) -> ModelRecord:
    """First `<model>` in `path` (or the first inside a `<world>`) -> `ModelRecord`."""
    raw = path.read_bytes()
    root, links, zero_mass, joints = _parse(path)
    return ModelRecord(
        source=str(path),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        structure_hash=_model_structure_hash(links, joints),  # re-derived by the
        # schema validator when joints is non-empty; kept here for the (rare)
        # zero-joint single-link model where the validator leaves it as-is.
        mesh_refs=mesh_refs(root),
        links=links,
        joints=joints,
        zero_mass_links=zero_mass,
        provenance=Provenance(witness="w2", source=str(path), locked_at=dt.date.today()),
    )


def read_sdf_topology(path: Path) -> UrdfTopology:
    """SDF twin of `urdf.read_urdf_topology`, for `checks.check_urdf_drift`."""
    _root, links, _zero_mass, joints = _parse(path)
    record_hash = _model_structure_hash(links, joints)
    return UrdfTopology(
        link_names=frozenset(link.name for link in links),
        joints=tuple(joints),
        topology_hash=record_hash,
        record_hash=record_hash,
    )
