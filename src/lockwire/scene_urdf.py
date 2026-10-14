"""URDF/SDF -> Scene. Stdlib only: xml.etree, struct, math, pathlib.

A link's visuals belong to the link (`extras_id = name_chain = (link)`); link poses
compose joint `<origin xyz rpy>` down the tree from the root with every joint at zero
(fixed/revolute/prismatic/continuous alike — no joint *type* changes the pose used here).
URDF and SDF are Z-up metres already, same as the contract: no axis conversion (`up = "z"`).

SDF link poses are used directly (`<link><pose>`), model-relative — this build does not
walk SDF `<joint>` chains or honour `relative_to`; a link's own `<pose>` is taken as its
final pose under the (identity) model frame.

`package://pkg/rel` and `model://name/rel` resolve, in order: `AMENT_PREFIX_PATH`
(`<dir>/share/pkg/rel`) and `ROS_PACKAGE_PATH` (`<dir>/pkg/rel`); each `package_roots` entry,
walking up to 3 levels for a `package.xml` naming `pkg` or a directory named `pkg`; then the
URDF/SDF's own directory. Anything else is a `SceneRefusal` naming the remedy.
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import replace
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from lockwire.mesh_simple import MeshRefusal, read_obj, read_stl
from lockwire.scene import Scene, ScenePart, SceneRefusal
from lockwire.urdf import UnresolvedXacroError, _refuse_doctype_entity, _refuse_unresolved_xacro
from lockwire.urdf import _xyz_rpy as _urdf_xyz_rpy

_CYL_SEGMENTS = 24
_SPHERE_LAT = 12
_SPHERE_LONG = 24
_DEFAULT_COLOR = (0.8, 0.8, 0.8, 1.0)

# (rotation-3x3, translation) pose pairs, composed like URDF joint chains.
_Pose = tuple[tuple[tuple[float, float, float], ...], tuple[float, float, float]]
_IDENTITY_POSE: _Pose = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.0, 0.0, 0.0))


# --------------------------------------------------------------------------- pose algebra


def _rpy_matrix(r: float, p: float, y: float) -> tuple[tuple[float, float, float], ...]:
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _mat3_mul(
    a: tuple[tuple[float, float, float], ...], b: tuple[tuple[float, float, float], ...]
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3)
    )


def _apply_r(
    r_mat: tuple[tuple[float, float, float], ...], v: tuple[float, float, float]
) -> tuple[float, float, float]:
    x, y, z = v
    return (
        r_mat[0][0] * x + r_mat[0][1] * y + r_mat[0][2] * z,
        r_mat[1][0] * x + r_mat[1][1] * y + r_mat[1][2] * z,
        r_mat[2][0] * x + r_mat[2][1] * y + r_mat[2][2] * z,
    )


def _compose(
    parent: _Pose, xyz: tuple[float, float, float], rpy: tuple[float, float, float]
) -> _Pose:
    r_parent, t_parent = parent
    r_local = _rpy_matrix(*rpy)
    r_new = _mat3_mul(r_parent, r_local)
    dx, dy, dz = _apply_r(r_parent, xyz)
    t_new = (t_parent[0] + dx, t_parent[1] + dy, t_parent[2] + dz)
    return r_new, t_new


def _apply_pose(pose: _Pose, point: tuple[float, float, float]) -> tuple[float, float, float]:
    r_mat, t = pose
    rx, ry, rz = _apply_r(r_mat, point)
    return (rx + t[0], ry + t[1], rz + t[2])


# --------------------------------------------------------------------------- geometry


def _box_mesh(sx: float, sy: float, sz: float):
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    # 6 faces, 4 verts each (flat-shaded), CCW looking from outside.
    faces = [
        ((-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz), (0, 0, 1)),  # +Z
        ((-hx, hy, -hz), (hx, hy, -hz), (hx, -hy, -hz), (-hx, -hy, -hz), (0, 0, -1)),  # -Z
        ((-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz), (-hx, -hy, hz), (0, -1, 0)),  # -Y
        ((hx, hy, -hz), (-hx, hy, -hz), (-hx, hy, hz), (hx, hy, hz), (0, 1, 0)),  # +Y
        ((-hx, hy, -hz), (-hx, -hy, -hz), (-hx, -hy, hz), (-hx, hy, hz), (-1, 0, 0)),  # -X
        ((hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz), (hx, -hy, hz), (1, 0, 0)),  # +X
    ]
    positions: list[float] = []
    normals: list[float] = []
    for a, b, c, d, n in faces:
        for v in (a, b, c, a, c, d):
            positions.extend(v)
            normals.extend(n)
    indices = tuple(range(len(positions) // 3))
    return tuple(positions), tuple(normals), indices


def _cylinder_mesh(radius: float, length: float, segments: int = _CYL_SEGMENTS):
    half = length / 2
    positions: list[float] = []
    normals: list[float] = []
    for i in range(segments):
        a0 = 2 * math.pi * i / segments
        a1 = 2 * math.pi * (i + 1) / segments
        x0, y0 = radius * math.cos(a0), radius * math.sin(a0)
        x1, y1 = radius * math.cos(a1), radius * math.sin(a1)
        n0 = (math.cos(a0), math.sin(a0), 0.0)
        n1 = (math.cos(a1), math.sin(a1), 0.0)
        # side wall, two triangles
        for v, n in (
            ((x0, y0, -half), n0),
            ((x1, y1, -half), n1),
            ((x1, y1, half), n1),
            ((x0, y0, -half), n0),
            ((x1, y1, half), n1),
            ((x0, y0, half), n0),
        ):
            positions.extend(v)
            normals.extend(n)
        # top cap
        for v in ((0.0, 0.0, half), (x0, y0, half), (x1, y1, half)):
            positions.extend(v)
            normals.extend((0.0, 0.0, 1.0))
        # bottom cap
        for v in ((0.0, 0.0, -half), (x1, y1, -half), (x0, y0, -half)):
            positions.extend(v)
            normals.extend((0.0, 0.0, -1.0))
    indices = tuple(range(len(positions) // 3))
    return tuple(positions), tuple(normals), indices


def _sphere_mesh(radius: float, lat_segments: int = _SPHERE_LAT, lon_segments: int = _SPHERE_LONG):
    def point(lat_i: int, lon_i: int) -> tuple[float, float, float]:
        theta = math.pi * lat_i / lat_segments  # 0..pi (pole to pole)
        phi = 2 * math.pi * lon_i / lon_segments
        x = math.sin(theta) * math.cos(phi)
        y = math.sin(theta) * math.sin(phi)
        z = math.cos(theta)
        return x, y, z

    positions: list[float] = []
    normals: list[float] = []
    for lat_i in range(lat_segments):
        for lon_i in range(lon_segments):
            p00 = point(lat_i, lon_i)
            p01 = point(lat_i, lon_i + 1)
            p10 = point(lat_i + 1, lon_i)
            p11 = point(lat_i + 1, lon_i + 1)
            quad = (p00, p10, p11, p00, p11, p01)
            for n in quad:
                positions.extend((n[0] * radius, n[1] * radius, n[2] * radius))
                normals.extend(n)
    indices = tuple(range(len(positions) // 3))
    return tuple(positions), tuple(normals), indices


# --------------------------------------------------------------------------- mesh files


def _load_mesh_file(resolved: Path, source: Path) -> list[tuple]:
    """-> a list of (positions, normals_or_None, indices) submeshes — one for every
    reader except DAE, which may carry several geometries in one file."""
    suffix = resolved.suffix.lower()
    if suffix == ".stl":
        try:
            return [read_stl(resolved)]
        except MeshRefusal as exc:
            raise SceneRefusal(str(exc)) from exc
    if suffix == ".obj":
        try:
            return [read_obj(resolved)]
        except MeshRefusal as exc:
            raise SceneRefusal(str(exc)) from exc
    if suffix == ".dae":
        try:
            from lockwire.mesh_dae import read_dae  # I2 lands this in parallel
        except ImportError as exc:
            raise SceneRefusal(
                f"{source}: mesh {resolved.name} is DAE, and DAE support is not in this build."
            ) from exc
        return list(read_dae(resolved).meshes)
    raise SceneRefusal(
        f"{source}: mesh {resolved.name} has unsupported extension {resolved.suffix!r} "
        "(convert to STL, OBJ, DAE or glTF)."
    )


def _find_package_dir(root: Path, pkg: str, max_depth: int) -> Path | None:
    def walk(d: Path, depth: int) -> Path | None:
        if depth > max_depth or not d.is_dir():
            return None
        if d.name == pkg:
            return d
        pkg_xml = d / "package.xml"
        if pkg_xml.is_file():
            try:
                xml_root = ET.fromstring(pkg_xml.read_bytes())
            except ET.ParseError:
                xml_root = None
            if xml_root is not None:
                name_el = xml_root.find("name")
                if name_el is not None and (name_el.text or "").strip() == pkg:
                    return d
        if depth == max_depth:
            return None
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return None
        for child in children:
            found = walk(child, depth + 1)
            if found is not None:
                return found
        return None

    return walk(root, 0)


def package_root(source: Path) -> Path | None:
    """The robot's package folder: the nearest ancestor of the model file holding package.xml."""
    source = source.resolve()
    return next((d for d in source.parents if (d / "package.xml").is_file()), None)


def mesh_root(source: Path, contract_dir: Path) -> Path:
    """The one folder a declared mesh must sit inside: the robot's
    package, else the lockfile's folder. `external_assets` paths are relative to it."""
    return package_root(source) or contract_dir.resolve()


def _resolve_package(
    pkg: str, rel: str, base_dir: Path, package_roots: tuple[Path, ...]
) -> Path | None:
    ament = os.environ.get("AMENT_PREFIX_PATH")
    if ament:
        for d in ament.split(os.pathsep):
            if not d:
                continue
            candidate = Path(d) / "share" / pkg / rel
            if candidate.is_file():
                return candidate
    ros_pkg_path = os.environ.get("ROS_PACKAGE_PATH")
    if ros_pkg_path:
        for d in ros_pkg_path.split(os.pathsep):
            if not d:
                continue
            candidate = Path(d) / pkg / rel
            if candidate.is_file():
                return candidate
    for root in package_roots:
        found = _find_package_dir(Path(root), pkg, max_depth=3)
        if found is not None:
            candidate = found / rel
            if candidate.is_file():
                return candidate
    for candidate in (base_dir / pkg / rel, base_dir / rel):
        if candidate.is_file():
            return candidate
    return None


def _resolve_mesh_path(
    uri: str, base_dir: Path, source: Path, package_roots: tuple[Path, ...]
) -> Path:
    if uri.startswith(("http://", "https://")):
        raise SceneRefusal(
            f"{source}: mesh URI {uri!r} is a network URL; only local files are accepted."
        )
    if uri.startswith("package://") or uri.startswith("model://"):
        scheme, _, rest = uri.partition("://")
        pkg, sep, rel = rest.partition("/")
        if not sep:
            raise SceneRefusal(f"{source}: {scheme}:// URI {uri!r} has no path after the package.")
        resolved = _resolve_package(pkg, rel, base_dir, package_roots)
        if resolved is None:
            raise SceneRefusal(
                f"{source}: cannot resolve {scheme}://{pkg} — set --package-root DIR "
                "(a folder containing the package) or source your ROS workspace."
            )
        return resolved
    p = Path(uri)
    if p.is_absolute():
        raise SceneRefusal(
            f"{source}: mesh path {uri!r} is absolute; use a relative path or package://."
        )
    resolved = base_dir / uri
    if not resolved.is_file():
        raise SceneRefusal(f"{source}: mesh file {uri} does not exist next to {source.name}.")
    return resolved


def _verify_mesh_identity(
    resolved: Path, source: Path, external_assets: Mapping[Path, str] | None
) -> None:
    """Refuse a selected model's mesh unless its declared bytes match."""

    if external_assets is None:
        return
    expected = external_assets.get(resolved.resolve())
    if expected is None:
        raise SceneRefusal(f"{source}: mesh {resolved} has no declared sha256.")
    observed = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if observed != expected:
        raise SceneRefusal(
            f"{source}: mesh {resolved} sha256 does not match declared asset: "
            f"observed {observed}, expected {expected}."
        )


def _scale_positions(
    positions: tuple[float, ...], scale: tuple[float, float, float]
) -> tuple[float, ...]:
    if scale == (1.0, 1.0, 1.0):
        return positions
    # ponytail: normals are not re-scaled by 1/scale (wrong under non-uniform scale) —
    # upgrade if a non-uniform-scaled mesh's shading visibly breaks.
    return tuple(positions[i] * scale[i % 3] for i in range(len(positions)))


# --------------------------------------------------------------------------- URDF


def _urdf_material_colors(root: ET.Element) -> dict[str, tuple[float, float, float, float]]:
    colors: dict[str, tuple[float, float, float, float]] = {}
    for mat in root.findall("material"):
        name = mat.get("name")
        color_el = mat.find("color")
        if name and color_el is not None:
            colors[name] = tuple(  # type: ignore[assignment]
                float(x) for x in color_el.get("rgba", "0.8 0.8 0.8 1").split()
            )
    return colors


def _urdf_visual_color(
    vis_el: ET.Element, materials: dict[str, tuple[float, float, float, float]]
) -> tuple[float, float, float, float]:
    mat_el = vis_el.find("material")
    if mat_el is not None:
        color_el = mat_el.find("color")
        if color_el is not None:
            return tuple(  # type: ignore[return-value]
                float(x) for x in color_el.get("rgba", "0.8 0.8 0.8 1").split()
            )
        name = mat_el.get("name")
        if name and name in materials:
            return materials[name]
    return _DEFAULT_COLOR


def _urdf_geometry_to_meshes(
    geom: ET.Element,
    base_dir: Path,
    source: Path,
    package_roots: tuple[Path, ...],
    external_assets: Mapping[Path, str] | None,
) -> list[tuple]:
    box = geom.find("box")
    if box is not None:
        sx, sy, sz = (float(x) for x in box.get("size", "1 1 1").split())
        return [_box_mesh(sx, sy, sz)]
    cyl = geom.find("cylinder")
    if cyl is not None:
        return [_cylinder_mesh(float(cyl.get("radius", "1")), float(cyl.get("length", "1")))]
    sph = geom.find("sphere")
    if sph is not None:
        return [_sphere_mesh(float(sph.get("radius", "1")))]
    mesh = geom.find("mesh")
    if mesh is not None:
        filename = mesh.get("filename")
        if not filename:
            raise SceneRefusal(f"{source}: <mesh> has no filename.")
        scale_raw = mesh.get("scale", "1 1 1")
        scale = tuple(float(x) for x in scale_raw.split())  # type: ignore[assignment]
        resolved = _resolve_mesh_path(filename, base_dir, source, package_roots)
        _verify_mesh_identity(resolved, source, external_assets)
        submeshes = _load_mesh_file(resolved, source)
        return [
            (_scale_positions(positions, scale), normals, indices)  # type: ignore[arg-type]
            for positions, normals, indices in submeshes
        ]
    raise SceneRefusal(f"{source}: <geometry> has no box/cylinder/sphere/mesh child.")


def _urdf_visual_to_parts(
    vis_el: ET.Element,
    link_name: str,
    link_pose: _Pose,
    base_dir: Path,
    source: Path,
    package_roots: tuple[Path, ...],
    external_assets: Mapping[Path, str] | None,
    materials: dict[str, tuple[float, float, float, float]],
) -> list[ScenePart]:
    xyz, rpy = _urdf_xyz_rpy(vis_el.find("origin"))
    visual_pose = _compose(link_pose, xyz, rpy)
    geom = vis_el.find("geometry")
    if geom is None:
        raise SceneRefusal(f"{source}: link {link_name!r} visual has no <geometry>.")
    submeshes = _urdf_geometry_to_meshes(
        geom, base_dir, source, package_roots, external_assets
    )
    color = _urdf_visual_color(vis_el, materials)
    return [
        _make_part(link_name, link_pose, visual_pose, positions, normals, indices, color)
        for positions, normals, indices in submeshes
    ]


def _make_part(
    link_name: str,
    link_pose: _Pose,
    visual_pose: _Pose,
    positions: tuple[float, ...],
    normals: tuple[float, ...] | None,
    indices: tuple[int, ...],
    color: tuple[float, float, float, float],
) -> ScenePart:
    world_pos: list[float] = []
    for i in range(0, len(positions), 3):
        x, y, z = _apply_pose(visual_pose, (positions[i], positions[i + 1], positions[i + 2]))
        world_pos.extend((x, y, z))
    world_norm: tuple[float, ...] | None = None
    if normals is not None:
        r_mat, _ = visual_pose
        norm_out: list[float] = []
        for i in range(0, len(normals), 3):
            norm_out.extend(_apply_r(r_mat, (normals[i], normals[i + 1], normals[i + 2])))
        world_norm = tuple(norm_out)
    return ScenePart(
        node=link_name,
        extras_id=link_name,
        name_chain=(link_name,),
        positions=tuple(world_pos),
        normals=world_norm,
        indices=indices,
        color=color,
        translation=link_pose[1],
    )


def _scene_from_urdf(
    root: ET.Element,
    source: Path,
    package_roots: tuple[Path, ...],
    external_assets: Mapping[Path, str] | None,
) -> Scene:
    if root.tag != "robot":
        raise SceneRefusal(f"{source}: root element is <{root.tag}>, expected <robot>.")
    base_dir = source.parent
    links = {el.get("name", "?"): el for el in root.findall("link")}
    materials = _urdf_material_colors(root)

    children_of: dict[str, list[ET.Element]] = {}
    child_links: set[str] = set()
    for joint in root.findall("joint"):
        parent = joint.find("parent")
        child = joint.find("child")
        pname = parent.get("link") if parent is not None else None
        cname = child.get("link") if child is not None else None
        if pname and cname:
            children_of.setdefault(pname, []).append(joint)
            child_links.add(cname)
    roots = [name for name in links if name not in child_links]

    parts: list[ScenePart] = []
    visited: set[str] = set()

    def walk(link_name: str, pose: _Pose) -> None:
        if link_name in visited:
            raise SceneRefusal(f"{source}: link {link_name!r} reached twice — cyclic joint tree.")
        visited.add(link_name)
        link_el = links.get(link_name)
        if link_el is not None:
            for vis in link_el.findall("visual"):
                parts.extend(
                    _urdf_visual_to_parts(
                        vis,
                        link_name,
                        pose,
                        base_dir,
                        source,
                        package_roots,
                        external_assets,
                        materials,
                    )
                )
        for joint in children_of.get(link_name, []):
            xyz, rpy = _urdf_xyz_rpy(joint.find("origin"))
            child = joint.find("child")
            cname = child.get("link") if child is not None else None
            if cname is None or cname not in links:
                continue
            walk(cname, _compose(pose, xyz, rpy))

    for root_link in roots:
        walk(root_link, _IDENTITY_POSE)

    return _build_scene(parts, source)


# --------------------------------------------------------------------------- SDF


def _sdf_pose(
    pose_el: ET.Element | None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    text = pose_el.text if pose_el is not None and pose_el.text else "0 0 0 0 0 0"
    vals = tuple(float(x) for x in text.split())
    if len(vals) < 6:
        vals = vals + (0.0,) * (6 - len(vals))
    xyz = (vals[0], vals[1], vals[2])
    rpy = (vals[3], vals[4], vals[5])
    return xyz, rpy


def _sdf_geometry_to_meshes(
    geom: ET.Element,
    base_dir: Path,
    source: Path,
    package_roots: tuple[Path, ...],
    external_assets: Mapping[Path, str] | None,
) -> list[tuple]:
    box = geom.find("box")
    if box is not None:
        size_text = box.findtext("size") or "1 1 1"
        sx, sy, sz = (float(x) for x in size_text.split())
        return [_box_mesh(sx, sy, sz)]
    cyl = geom.find("cylinder")
    if cyl is not None:
        radius = float(cyl.findtext("radius") or "1")
        length = float(cyl.findtext("length") or "1")
        return [_cylinder_mesh(radius, length)]
    sph = geom.find("sphere")
    if sph is not None:
        return [_sphere_mesh(float(sph.findtext("radius") or "1"))]
    mesh = geom.find("mesh")
    if mesh is not None:
        uri = mesh.findtext("uri")
        if not uri:
            raise SceneRefusal(f"{source}: <mesh> has no <uri>.")
        scale_text = mesh.findtext("scale")
        scale = tuple(float(x) for x in scale_text.split()) if scale_text else (1.0, 1.0, 1.0)
        resolved = _resolve_mesh_path(uri, base_dir, source, package_roots)
        _verify_mesh_identity(resolved, source, external_assets)
        submeshes = _load_mesh_file(resolved, source)
        return [
            (_scale_positions(positions, scale), normals, indices)  # type: ignore[arg-type]
            for positions, normals, indices in submeshes
        ]
    raise SceneRefusal(f"{source}: <geometry> has no box/cylinder/sphere/mesh child.")


def _first_model(root: ET.Element) -> ET.Element | None:
    if root.tag == "model":
        return root
    for el in root.iter("model"):
        return el
    return None


def _scene_from_sdf(
    root: ET.Element,
    source: Path,
    package_roots: tuple[Path, ...],
    external_assets: Mapping[Path, str] | None,
) -> Scene:
    base_dir = source.parent
    if root.tag != "sdf":
        raise SceneRefusal(f"{source}: root element is <{root.tag}>, expected <sdf>.")
    model = _first_model(root)
    if model is None:
        raise SceneRefusal(f"{source}: no <model> element found.")

    parts: list[ScenePart] = []
    for link in model.findall("link"):
        link_name = link.get("name", "?")
        link_xyz, link_rpy = _sdf_pose(link.find("pose"))
        link_pose = _compose(_IDENTITY_POSE, link_xyz, link_rpy)
        for vis in link.findall("visual"):
            vis_xyz, vis_rpy = _sdf_pose(vis.find("pose"))
            visual_pose = _compose(link_pose, vis_xyz, vis_rpy)
            geom = vis.find("geometry")
            if geom is None:
                raise SceneRefusal(f"{source}: link {link_name!r} visual has no <geometry>.")
            submeshes = _sdf_geometry_to_meshes(
                geom, base_dir, source, package_roots, external_assets
            )
            for positions, normals, indices in submeshes:
                parts.append(
                    _make_part(
                        link_name,
                        link_pose,
                        visual_pose,
                        positions,
                        normals,
                        indices,
                        _DEFAULT_COLOR,
                    )
                )

    return _build_scene(parts, source)


# --------------------------------------------------------------------------- shared / top level


def _build_scene(parts: list[ScenePart], source: Path) -> Scene:
    if parts:
        min_b = [math.inf, math.inf, math.inf]
        max_b = [-math.inf, -math.inf, -math.inf]
        for part in parts:
            for i in range(0, len(part.positions), 3):
                for k in range(3):
                    v = part.positions[i + k]
                    min_b[k] = min(min_b[k], v)
                    max_b[k] = max(max_b[k], v)
    else:
        min_b = [0.0, 0.0, 0.0]
        max_b = [0.0, 0.0, 0.0]

    nodes_with_ids = {part.extras_id: part.node for part in parts if part.extras_id}
    triangles = sum(len(part.indices) // 3 for part in parts)
    vertices = sum(len(part.positions) // 3 for part in parts)
    return Scene(
        parts=tuple(parts),
        up="z",
        nodes_with_ids=nodes_with_ids,
        source=source.name,
        bounds=(tuple(min_b), tuple(max_b)),  # type: ignore[arg-type]
        triangles=triangles,
        vertices=vertices,
    )


def _refuse_doctype(raw: bytes, source: Path) -> None:
    try:
        _refuse_doctype_entity(raw, source)
    except ValueError as exc:
        raise SceneRefusal(str(exc)) from exc


def urdf_visual_parents(path: Path) -> dict[str, str]:
    """Link with no visual (inertial-only) -> its nearest ancestor link that has one, so a
    value on the inertial link can light the part the robot actually draws."""
    raw = Path(path).read_bytes()
    _refuse_doctype(raw, Path(path))
    root = ET.fromstring(raw)
    drawn = {el.get("name") for el in root.findall("link") if el.find("visual") is not None}
    parent = {
        c.get("link"): p.get("link")
        for j in root.findall("joint")
        if (p := j.find("parent")) is not None and (c := j.find("child")) is not None
    }
    out: dict[str, str] = {}
    for link in parent:
        if link in drawn:
            continue
        up = parent.get(link)
        while up is not None and up not in drawn:
            up = parent.get(up)
        if up is not None:
            out[link] = up
    return out


def _refuse_xacro(root: ET.Element, source: Path) -> None:
    try:
        _refuse_unresolved_xacro(root, source)
    except UnresolvedXacroError as exc:
        raise SceneRefusal(str(exc)) from exc


def load_robot_scene(
    path: Path,
    *,
    package_roots: tuple[Path, ...] = (),
    external_assets: Mapping[Path, str] | None = None,
) -> Scene:
    """A `.urdf`, `.sdf` or literal-XML `.xacro` file -> `Scene`."""
    path = Path(path)
    if not path.is_file():
        raise SceneRefusal(f"{path}: file does not exist.")
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if (own := package_root(path)) is not None:
        package_roots = (*package_roots, own)

    if suffix in (".urdf", ".xacro"):
        _refuse_doctype(raw, path)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SceneRefusal(f"{path}: not valid XML ({exc}).") from exc
        _refuse_xacro(root, path)
        return replace(_scene_from_urdf(root, path, package_roots, external_assets),
                       asset_path=str(path.resolve()), asset_sha256=hashlib.sha256(raw).hexdigest())

    if suffix == ".sdf":
        _refuse_doctype(raw, path)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SceneRefusal(f"{path}: not valid XML ({exc}).") from exc
        return replace(_scene_from_sdf(root, path, package_roots, external_assets),
                       asset_path=str(path.resolve()), asset_sha256=hashlib.sha256(raw).hexdigest())

    raise SceneRefusal(
        f"{path}: unsupported file extension {suffix!r} (expected .urdf, .sdf or .xacro)."
    )


def load_scene_any(
    path: Path,
    *,
    package_roots: tuple[Path, ...] = (),
    external_assets: Mapping[Path, str] | None = None,
) -> Scene:
    """Dispatch a `--scene` path to the glTF reader or this URDF/SDF reader by suffix."""
    from lockwire.scene import load_scene

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".glb", ".gltf"):
        return load_scene(path)
    if suffix in (".urdf", ".sdf", ".xacro"):
        return load_robot_scene(
            path, package_roots=package_roots, external_assets=external_assets
        )
    raise SceneRefusal(
        f"{path}: unsupported scene file extension {suffix!r} "
        "(expected .glb, .gltf, .urdf, .sdf or .xacro)."
    )
