"""Write a ModelRecord out as URDF, SDF or MJCF on demand: `write(model, fmt, out)` is the one call.

The lockfile holds no geometry, so the files carry links, inertials and joints only. A joint a
format cannot hold raises one plain sentence naming it; nothing is guessed.
"""

import math
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from lockwire import plugins
from lockwire.scene_urdf import _IDENTITY_POSE, _compose
from lockwire.schema import Joint, Link, ModelRecord
from lockwire.mesh_simple import read_stl
from lockwire.sdf import _matrix_to_rpy
from lockwire.statics import _rpy, link_frames, primitive_inertia

SUFFIX = {"urdf": ".urdf", "sdf": ".sdf", "mjcf": ".xml"}
STEP_REFUSAL = "STEP export is not built (it would need a new dependency)"
SEGMENTS = (
    32  # default tessellation of a cylinder or sphere; a 32-gon prism holds 99.4% of the volume
)
UNITS = {"m": 1.0, "mm": 1000.0}
_NOTE = " Exported by lockwire from the lockfile model; no visual or collision geometry. "
_NO = {
    "urdf": ("ball",),
    "sdf": ("floating", "planar"),
    "mjcf": ("planar",),
}


def _v(values) -> str:
    return " ".join(repr(float(x)) for x in values)


def _tensor(link: Link) -> list[float] | None:
    """ixx ixy ixz iyy iyz izz, rebuilt as A^T diag(moments) A from the principal form."""
    if link.inertia is None:
        return None
    axes = np.array(link.inertia.axes)
    t = axes.T @ np.diag(link.inertia.moments_kgm2) @ axes
    return [t[0, 0], t[0, 1], t[0, 2], t[1, 1], t[1, 2], t[2, 2]]


def _shape_tensor(link: Link) -> list[float] | None:
    """ixx ixy ixz iyy iyz izz about the link's CoM, from its primitive shapes sharing the link's
    mass by volume (uniform density); None when a shape is a mesh or a size is unresolved."""
    if link.mass is None or not link.geometry:
        return None
    parts = []
    for sh in link.geometry:
        dims = [*(sh.size or ()), sh.radius, sh.length]
        if sh.kind not in ("box", "cylinder", "sphere") or any(isinstance(d, str) for d in dims):
            return None
        if sh.kind == "box":
            vol, own = math.prod(sh.size), sh.size
        elif sh.kind == "cylinder":
            vol, own = math.pi * sh.radius**2 * sh.length, (sh.radius, sh.length)
        else:
            vol, own = 4 / 3 * math.pi * sh.radius**3, (sh.radius,)
        o = sh.origin
        xyz = np.array(o.xyz if o else (0.0, 0.0, 0.0), float)
        parts.append((vol, own, sh.kind, xyz, _rpy(*(o.rpy if o else (0.0, 0.0, 0.0)))))
    total = sum(p[0] for p in parts)
    if not total > 0:
        return None
    com = np.array(link.com) if link.com is not None else sum(p[0] * p[3] for p in parts) / total
    inertia = np.zeros((3, 3))
    for vol, own, kind, xyz, rot in parts:
        m = link.mass.base() * vol / total
        d = xyz - com
        inertia += rot @ primitive_inertia(kind, tuple(own), m) @ rot.T
        inertia += m * (d @ d * np.eye(3) - np.outer(d, d))
    return [inertia[0, 0], inertia[0, 1], inertia[0, 2], inertia[1, 1], inertia[1, 2], inertia[2, 2]]


def _check(model: ModelRecord, fmt: str) -> None:
    for joint in model.joints:
        if joint.type in _NO[fmt]:
            raise ValueError(
                f"joint {joint.name!r} is a {joint.type} joint; {fmt.upper()} has none"
            )
    for link in model.links:
        if link.mass is not None and link.frame not in (None, link.name):
            raise ValueError(f"link {link.name!r} gives its center of mass in frame {link.frame!r}")
        if (
            fmt == "mjcf"
            and link.mass is not None
            and link.inertia is None
            and _shape_tensor(link) is None
        ):
            raise ValueError(
                f"link {link.name!r} has a mass but no inertia; MJCF needs both "
                "(declare the link's inertia, or give it shapes and a material)"
            )


def _inertial(link: Link, parent: ET.Element, sdf: bool = False) -> None:
    if link.mass is None:
        return
    el = ET.SubElement(parent, "inertial")
    com = link.com or (0.0, 0.0, 0.0)
    if sdf:
        ET.SubElement(el, "pose").text = _v((*com, 0, 0, 0))
        ET.SubElement(el, "mass").text = repr(link.mass.base())
    else:
        ET.SubElement(el, "origin", xyz=_v(com), rpy="0 0 0")
        ET.SubElement(el, "mass", value=repr(link.mass.base()))
    if (t := _tensor(link)) is not None:
        names = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
        if sdf:
            inertia = ET.SubElement(el, "inertia")
            for key, x in zip(names, t):
                ET.SubElement(inertia, key).text = repr(float(x))
        else:
            ET.SubElement(el, "inertia", {k: repr(float(x)) for k, x in zip(names, t)})


def _urdf(model: ModelRecord, name: str) -> ET.Element:
    root = ET.Element("robot", name=name)
    root.append(ET.Comment(_NOTE))
    for link in model.links:
        _inertial(link, ET.SubElement(root, "link", name=link.name))
    for j in model.joints:
        el = ET.SubElement(root, "joint", name=j.name, type=j.type)
        ET.SubElement(el, "parent", link=j.parent)
        ET.SubElement(el, "child", link=j.child)
        ET.SubElement(el, "origin", xyz=_v(j.origin.xyz), rpy=_v(j.origin.rpy))
        if j.axis is not None:
            ET.SubElement(el, "axis", xyz=_v(j.axis))
        if j.limits is not None:
            lim = j.limits
            given = {"lower": lim.lower, "upper": lim.upper, "effort": lim.effort}
            given["velocity"] = lim.velocity
            ET.SubElement(el, "limit", {k: repr(v) for k, v in given.items() if v is not None})
    return root


def _link_poses(model: ModelRecord) -> dict:
    """Each link's pose in the model frame: roots at the origin, children through the joints."""
    children = {j.child for j in model.joints}
    poses = {k.name: _IDENTITY_POSE for k in model.links if k.name not in children}
    todo = list(model.joints)
    while todo:
        ready = [j for j in todo if j.parent in poses]
        if not ready:
            raise ValueError(f"joint {todo[0].name!r} hangs off a link no root reaches")
        for j in ready:
            poses.setdefault(j.child, _compose(poses[j.parent], j.origin.xyz, j.origin.rpy))
        todo = [j for j in todo if j not in ready]
    return poses


def _sdf(model: ModelRecord, name: str) -> ET.Element:
    root = ET.Element("sdf", version="1.7")
    el = ET.SubElement(root, "model", name=name)
    el.append(ET.Comment(_NOTE))
    poses = _link_poses(model)
    for link in model.links:
        rot, xyz = poses[link.name]
        link_el = ET.SubElement(el, "link", name=link.name)
        ET.SubElement(link_el, "pose").text = _v((*xyz, *_matrix_to_rpy(rot)))
        _inertial(link, link_el, sdf=True)
    for j in model.joints:
        # SDFormat's joint type list has "continuous" (sdformat.org/spec, joint type; recalled,
        # the spec page was not fetchable here). A limitless "revolute" reads back as a
        # follower, not a moving joint (sdf.py/checks.py), so write the type itself.
        je = ET.SubElement(el, "joint", name=j.name, type=j.type)
        ET.SubElement(je, "parent").text = j.parent
        ET.SubElement(je, "child").text = j.child
        if j.axis is None and j.limits is None:
            continue
        axis = ET.SubElement(je, "axis")
        if j.axis is not None:
            ET.SubElement(axis, "xyz").text = _v(j.axis)
        if j.limits is not None:
            lim = ET.SubElement(axis, "limit")
            for key in ("lower", "upper", "effort", "velocity"):
                if (x := getattr(j.limits, key)) is not None:
                    ET.SubElement(lim, key).text = repr(x)
    return root


def _body(model: ModelRecord, link: Link, joints: list[Joint], parent: ET.Element) -> None:
    by_parent: dict[str, list[Joint]] = {}
    for j in model.joints:
        by_parent.setdefault(j.parent, []).append(j)
    first = joints[0].origin if joints else None
    attrs = {"name": link.name}
    if first is not None:
        attrs["pos"] = _v(first.xyz)
        if any(first.rpy):
            r, p, y = first.rpy  # eulerseq zyx turns yaw, pitch, roll: URDF's fixed-axis rpy
            attrs["euler"] = _v((y, p, r))
    el = ET.SubElement(parent, "body", attrs)
    for j in joints:
        if j.type == "floating":
            ET.SubElement(el, "freejoint")
        elif j.type != "fixed":
            kind = {"revolute": "hinge", "continuous": "hinge", "prismatic": "slide"}
            je = ET.SubElement(el, "joint", name=j.name, type=kind.get(j.type, j.type))
            if j.axis is not None:
                je.set("axis", _v(j.axis))
            if j.limits is not None and None not in (j.limits.lower, j.limits.upper):
                je.set("range", _v((j.limits.lower, j.limits.upper)))
    if link.mass is not None:
        t = _tensor(link)
        if t is None:
            t = _shape_tensor(link)
            el.append(
                ET.Comment(
                    " inertia derived from the link's shapes and material (not declared) "
                )
            )
        full = _v((t[0], t[3], t[5], t[1], t[2], t[4]))  # MJCF order M11 M22 M33 M12 M13 M23
        ET.SubElement(
            el,
            "inertial",
            pos=_v(link.com or (0.0, 0.0, 0.0)),
            mass=repr(link.mass.base()),
            fullinertia=full,
        )
    _children(model, link.name, el, by_parent)


def _children(model: ModelRecord, parent: str, el: ET.Element, by_parent: dict) -> None:
    links = {k.name: k for k in model.links}
    grouped: dict[str, list[Joint]] = {}
    for j in by_parent.get(parent, []):
        grouped.setdefault(j.child, []).append(j)
    for child, joints in grouped.items():
        _body(model, links[child], joints, el)


def _mjcf(model: ModelRecord, name: str) -> ET.Element:
    root = ET.Element("mujoco", model=name)
    root.append(ET.Comment(_NOTE))
    ET.SubElement(root, "compiler", angle="radian", eulerseq="zyx")
    world = ET.SubElement(root, "worldbody")
    children = {j.child for j in model.joints}
    by_parent: dict[str, list[Joint]] = {}
    for j in model.joints:
        by_parent.setdefault(j.parent, []).append(j)
    for link in model.links:
        if link.name in children:
            continue
        if link.name == "world" and link.mass is None:  # MJCF's own world: bodies hang off it
            _children(model, "world", world, by_parent)
        else:
            _body(model, link, [], world)
    motors = [j for j in model.joints if j.limits and (j.limits.effort or 0) > 0]
    if motors := [j for j in motors if j.type in ("revolute", "continuous", "prismatic")]:
        act = ET.SubElement(root, "actuator")
        for j in motors:
            e = j.limits.effort
            ET.SubElement(act, "motor", joint=j.name, forcerange=_v((-e, e)))
    return root


def render(model: ModelRecord, fmt: str, name: str) -> str:
    if fmt == "step":
        raise ValueError(STEP_REFUSAL)
    if fmt not in SUFFIX:
        raise ValueError(f"format must be urdf, sdf or mjcf, not {fmt!r}")
    _check(model, fmt)
    root = {"urdf": _urdf, "sdf": _sdf, "mjcf": _mjcf}[fmt](model, name)
    ET.indent(root)
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def default_out(contract: Path, fmt: str) -> Path:
    """Beside the lockfile: lockwire.yaml -> lockwire.urdf / .sdf / .xml."""
    if fmt == "step":
        raise ValueError(STEP_REFUSAL)
    return contract.with_suffix(SUFFIX[fmt])


def _refuse_own_files(model: ModelRecord, source_root: Path | None, targets) -> None:
    """Never write over the model's own source, assets or meshes, even with force (symlinks followed)."""
    refs = [model.source, *(a.path for a in model.external_assets), *(r.uri for r in model.mesh_refs or [])]
    refs += [sh.mesh for ln in model.links for sh in ln.geometry or [] if sh.kind == "mesh" and sh.mesh]
    src_dir = Path(model.source).parent
    own = set()
    for ref in refs:
        p = Path(ref.removeprefix("file://"))
        roots = [None] if p.is_absolute() else [None, source_root, source_root and source_root / src_dir, src_dir]
        for root in roots:
            c = p if root is None else root / p
            if c.is_file():
                own.add(c.resolve())
    for path in targets:
        if path.resolve() in own:
            raise ValueError(f"{path} is one of your model's own files; pick another folder")


def write(
    model: ModelRecord, fmt: str, out: Path, force: bool = False, source_root: Path | None = None
) -> Path:
    """The call the CLI makes. Never overwrites an existing file unless `force`.

    `source_root` is the contract's folder: a relative source resolves against it, as the repo's
    readers do, and `out` may never be that file. A relative source with no `source_root`, parsed
    under some other cwd, cannot be resolved by anything; that case is out of scope.
    """
    text = render(model, fmt, Path(model.source).stem or "robot")
    plugins.ensure()
    # a plugin may add collision shapes: it returns the new text and sidecar files for beside `out`
    text, sidecars = plugins.pick("collision_shapes", lambda m, f, t, o, r: (t, {}))(
        model, fmt, text, out, source_root
    )
    source = Path(model.source)
    # the contract-folder rule (cli/api: contract.parent / source), plus cwd and beside-`out`
    candidates = [source, out.parent / source]
    if source_root is not None:
        candidates.append(source_root / source)
    if out.is_file() and any(c.is_file() and c.samefile(out) for c in candidates):
        raise ValueError(f"{out} is the source model itself; export to a different file")
    _refuse_own_files(model, source_root, (out, *sidecars))
    if not force:  # check every file first, so a refusal leaves no half-written export
        for path in (out, *sidecars):
            if path.exists():
                raise FileExistsError(f"{path} already exists; pass force to overwrite it")
    try:
        with out.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(text)
        for path, data in sidecars.items():
            with path.open("wb" if force else "xb") as handle:
                handle.write(data)
    except FileExistsError as error:
        raise FileExistsError(
            f"{error.filename} already exists; pass force to overwrite it"
        ) from None
    return out


def default_stl_dir(contract: Path, fmt: str = "stl") -> Path:
    """Beside the lockfile: lockwire.yaml -> lockwire-stl/ (or -obj, -dae)."""
    return contract.with_name(f"{contract.stem}-{fmt}")


def _quads(verts, quads) -> np.ndarray:
    faces = [t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))]
    return np.array(verts, dtype=float)[np.array(faces)]


def _primitive(shape, n: int) -> np.ndarray:
    """(triangles, 3, 3) of a box, cylinder (axis Z) or sphere centred on its own origin, outward."""
    if shape.kind == "box":
        corners = [(x, y, z) for z in (-0.5, 0.5) for y in (-0.5, 0.5) for x in (-0.5, 0.5)]
        verts = np.array(corners) * np.array(shape.size, dtype=float)
        quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
        tris = _quads(verts, quads)
    elif shape.kind == "cylinder":
        r, h = shape.radius, shape.length / 2
        a = 2 * np.pi * np.arange(n) / n
        ring = np.c_[r * np.cos(a), r * np.sin(a)]
        verts = [(*p, -h) for p in ring] + [(*p, h) for p in ring] + [(0, 0, -h), (0, 0, h)]
        faces = []
        for i in range(n):
            j = (i + 1) % n
            faces += [(i, j, n + j), (i, n + j, n + i), (2 * n, j, i), (2 * n + 1, n + i, n + j)]
        tris = np.array(verts, dtype=float)[np.array(faces)]
    else:  # sphere: n segments round, n // 2 bands pole to pole
        r, m = shape.radius, max(n // 2, 2)
        a = 2 * np.pi * np.arange(n) / n
        verts = [(0, 0, r), (0, 0, -r)]
        for k in range(1, m):
            t = np.pi * k / m
            verts += [
                (r * np.sin(t) * np.cos(x), r * np.sin(t) * np.sin(x), r * np.cos(t)) for x in a
            ]
        ring = lambda k: [2 + (k - 1) * n + i for i in range(n)]  # noqa: E731
        faces = []
        for i in range(n):
            j = (i + 1) % n
            faces += [(0, ring(1)[i], ring(1)[j]), (1, ring(m - 1)[j], ring(m - 1)[i])]
            for k in range(1, m - 1):
                a0, a1, b0, b1 = ring(k)[i], ring(k)[j], ring(k + 1)[i], ring(k + 1)[j]
                faces += [(a0, a1, b1), (a0, b1, b0)]
        tris = np.array(verts, dtype=float)[np.array(faces)]
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    flip = (normal * tris.mean(axis=1)).sum(axis=1) < 0  # convex and centred: outward points away
    tris[flip] = tris[flip][:, ::-1]
    return tris


def _place(tris: np.ndarray, rot: np.ndarray, pos) -> np.ndarray:
    return tris @ rot.T + np.asarray(pos, dtype=float)


def _stl_bytes(tris: np.ndarray, scale: float) -> bytes:
    tris = tris * scale
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n = n / np.where((m := np.linalg.norm(n, axis=1, keepdims=True)) > 0, m, 1)
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tris
    return b"lockwire".ljust(80, b" ") + struct.pack("<I", len(tris)) + rec.tobytes()


def _obj_bytes(tris: np.ndarray, scale: float, name: str = "", units: str = "m") -> bytes:
    v = (tris * scale).reshape(-1, 3)
    lines = [f"# lockwire, units {units}", f"o {name or 'assembly'}"]
    lines += [f"v {x:.9g} {y:.9g} {z:.9g}" for x, y, z in v]
    lines += [f"f {i} {i + 1} {i + 2}" for i in range(1, len(v), 3)]  # unwelded: 3 vertices a face
    return ("\n".join(lines) + "\n").encode()


def _dae_bytes(tris: np.ndarray, scale: float, name: str = "", units: str = "m") -> bytes:
    """Hand-written COLLADA 1.4.1: one geometry, one visual-scene node."""
    ns = "http://www.collada.org/2005/11/COLLADASchema"
    name = name or "assembly"
    v = (tris * scale).reshape(-1, 3)
    col = lambda tag, parent=None, **a: ET.SubElement(parent, tag, a) if parent is not None else ET.Element(tag, a)  # noqa: E731
    root = col("COLLADA", xmlns=ns, version="1.4.1")
    asset = col("asset", root)
    col("unit", asset, name={"m": "meter", "mm": "millimeter"}[units], meter=repr(1 / UNITS[units]))
    col("up_axis", asset).text = "Z_UP"
    geom = col("geometry", col("library_geometries", root), id="g", name=name)
    mesh = col("mesh", geom)
    src = col("source", mesh, id="g-pos")
    arr = col("float_array", src, id="g-pos-array", count=str(v.size))
    arr.text = " ".join(f"{x:.9g}" for x in v.ravel())
    acc = col("accessor", col("technique_common", src), source="#g-pos-array", count=str(len(v)), stride="3")
    for axis in "XYZ":
        col("param", acc, name=axis, type="float")
    col("input", col("vertices", mesh, id="g-vtx"), semantic="POSITION", source="#g-pos")
    t = col("triangles", mesh, count=str(len(v) // 3))
    col("input", t, semantic="VERTEX", source="#g-vtx", offset="0")
    col("p", t).text = " ".join(map(str, range(len(v))))
    node = col("node", col("visual_scene", col("library_visual_scenes", root), id="s"), id="n", name=name)
    col("instance_geometry", node, url="#g")
    col("instance_visual_scene", col("scene", root), url="#s")
    return ET.tostring(root, xml_declaration=True, encoding="utf-8")


_ENCODERS = {"stl": lambda t, s, n, u: _stl_bytes(t, s), "obj": _obj_bytes, "dae": _dae_bytes}


def _link_tris(link: Link, n: int, source_root: Path | None, notes: list[str]) -> np.ndarray | None:
    """The link's triangles in its own frame, or None; a shape that cannot be written is noted."""
    parts = []
    for shape in link.geometry or []:
        if shape.kind == "mesh":
            file = Path(shape.mesh)
            file = file if file.is_absolute() or source_root is None else source_root / file
            if file.suffix.lower() != ".stl" or not file.is_file():
                notes.append(
                    f"not exported: {shape.mesh}" + ("" if file.is_file() else " (not found)")
                )
                continue
            tris = np.array(read_stl(file)[0], dtype=float).reshape(-1, 3, 3)
        else:
            tris = _primitive(shape, n)
        o = shape.origin
        parts.append(_place(tris, _rpy(*o.rpy), o.xyz) if o else tris)
    return np.concatenate(parts) if parts else None


def write_stl(model: ModelRecord, out: Path, *args, **kw) -> tuple[list[Path], list[str]]:
    return write_mesh(model, out, "stl", *args, **kw)


def write_mesh(
    model: ModelRecord,
    out: Path,
    fmt: str = "stl",
    units: str = "m",
    segments: int = SEGMENTS,
    force: bool = False,
    source_root: Path | None = None,
) -> tuple[list[Path], list[str]]:
    """One STL, OBJ or DAE per shaped link in its own frame, plus assembly.<fmt> at the zero pose, in `out`.

    Returns (files written, one plain line per link or mesh that was not exported). Never
    overwrites unless `force`, and never writes into the folder the source model sits in.
    """
    if fmt not in _ENCODERS:
        raise ValueError(f"mesh format must be stl, obj or dae, not {fmt!r}")
    if units not in UNITS:
        raise ValueError(f"units must be m or mm, not {units!r}")
    if segments < 3:
        raise ValueError("segments must be at least 3")
    frames = link_frames(model)
    if frames is None:
        raise ValueError("the links do not hang off one root, so the assembly cannot be placed")
    source = Path(model.source)
    if any(
        out.resolve() == c.resolve()
        for c in (
            source_root,
            source.parent,
            *([source_root / source.parent] if source_root else []),
        )
        if c is not None
    ):
        raise ValueError(f"{out} holds the source model; export into a different folder")
    notes: list[str] = []
    shaped: dict[str, np.ndarray] = {}
    for link in model.links:
        if not link.geometry:
            notes.append(f"{link.name}: has no shape")
        elif (tris := _link_tris(link, segments, source_root, notes)) is not None:
            shaped[link.name] = tris
    if not shaped:
        raise ValueError("no link has a shape to export")
    names = {name: re.sub(r"[^\w.-]", "_", name) + "." + fmt for name in shaped}
    if len({*names.values(), f"assembly.{fmt}"}) != len(names) + 1:
        raise ValueError("two links would share a file name, or one is named 'assembly'")
    files = {names[k]: t for k, t in shaped.items()}
    files[f"assembly.{fmt}"] = np.concatenate(
        [_place(t, frames[k][1], frames[k][0]) for k, t in shaped.items()]
    )
    enc = _ENCODERS[fmt]
    targets = {
        out / name: enc(t, UNITS[units], name.removesuffix("." + fmt), units)
        for name, t in files.items()
    }
    _refuse_own_files(model, source_root, targets)
    if not force and (taken := [p for p in targets if p.exists()]):
        raise FileExistsError(f"{taken[0]} already exists; pass force to overwrite it")
    out.mkdir(parents=True, exist_ok=True)
    for path, data in targets.items():
        path.write_bytes(data)
    return list(targets), notes
