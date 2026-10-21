"""Stdlib Collada (DAE) mesh reader.

Reads `<library_geometries>` (`<triangles>` and `<polylist>`, fan-triangulated) composed
through the `<library_visual_scenes>` node tree (`<matrix>`/`<translate>`/`<rotate>`/`<scale>`,
`<instance_node>` indirection), converting the file's declared up-axis and unit into metres,
Z-up. Namespace-agnostic (Collada documents are commonly namespaced,
`http://www.collada.org/2005/11/COLLADASchema`); tags are matched after stripping `{...}`.

stdlib only: `xml.etree.ElementTree`, `math`, `pathlib`. Refusal posture mirrors
`urdf.py`'s `_refuse_doctype_entity` (checked on raw bytes before any XML parser sees them)
and reuses `scene.py`'s `SceneRefusal` — plain words, never a raw exception.
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from lockwire.scene import SceneRefusal
from lockwire.urdf import _has_doctype_entity

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_INSTANCE_NODE_DEPTH = 32

Mesh = tuple[tuple[float, ...], tuple[float, ...] | None, tuple[int, ...]]


@dataclass(frozen=True)
class DaeResult:
    meshes: tuple[Mesh, ...]
    skipped: tuple[str, ...]


def _tag(el: ET.Element) -> str:
    t = el.tag
    return t.split("}", 1)[1] if t.startswith("{") else t


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _tag(c) == name]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _refuse_doctype_entity(raw: bytes, path: Path) -> None:
    """Same posture as `urdf._refuse_doctype_entity`: refuse on raw bytes before parsing —
    an internal `<!ENTITY>` still expands under stdlib `xml.etree`, so refusing before the
    parser ever sees it is the only safe posture."""
    if _has_doctype_entity(raw):
        raise SceneRefusal(
            f"{path}: DOCTYPE/ENTITY declarations are refused (XML entity expansion risk). "
            "Re-export the file without an internal DTD."
        )


def read_dae(path: Path) -> DaeResult:
    """Read a Collada (.dae) file's visual-scene geometry as metres, Z-up.

    Raises `SceneRefusal` on anything malformed or unsupported, naming the defect and the
    remedy; never propagates a raw parser exception.
    """
    raw = path.read_bytes()
    if len(raw) > _MAX_FILE_BYTES:
        raise SceneRefusal(f"{path}: file is over the 64 MiB size guard. Re-export a smaller mesh.")
    _refuse_doctype_entity(raw, path)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SceneRefusal(f"{path}: not valid XML ({exc}). Re-export the file.") from exc
    if _tag(root) != "COLLADA":
        raise SceneRefusal(
            f"{path}: root element is <{_tag(root)}>, expected <COLLADA>. "
            "This is not a Collada (.dae) file."
        )

    unit_scale = 1.0
    up_axis = "Y_UP"
    asset = _child(root, "asset")
    if asset is not None:
        unit_el = _child(asset, "unit")
        if unit_el is not None and unit_el.get("meter") is not None:
            unit_scale = float(unit_el.get("meter"))
        up_el = _child(asset, "up_axis")
        if up_el is not None and up_el.text:
            up_axis = up_el.text.strip()

    geometries = _parse_geometries(root, path)
    nodes_by_id = _index_nodes(root, path)

    visual_scene = _find_visual_scene(root, path)
    skipped: list[str] = []
    meshes: list[Mesh]
    if visual_scene is None:
        # No visual scene at all -> every geometry once, identity transform.
        meshes = [
            _apply_transform(geom, _IDENTITY, unit_scale, up_axis) for geom in geometries.values()
        ]
    else:
        meshes = []
        for node in _children(visual_scene, "node"):
            _walk_node(
                node,
                _IDENTITY,
                geometries,
                nodes_by_id,
                unit_scale,
                up_axis,
                meshes,
                skipped,
                path,
                depth=0,
            )
    return DaeResult(meshes=tuple(meshes), skipped=tuple(skipped))


# --------------------------------------------------------------------------- geometry parsing

_GeomData = tuple[
    list[tuple[float, float, float]], list[tuple[float, float, float]] | None, list[int]
]


def _parse_float_array(source: ET.Element, path: Path) -> list[float]:
    arr = _child(source, "float_array")
    if arr is None or not arr.text:
        return []
    return [float(x) for x in arr.text.split()]


def _source_stride(source: ET.Element, path: Path) -> tuple[list[float], int]:
    values = _parse_float_array(source, path)
    tc = _child(source, "technique_common")
    stride = 3
    if tc is not None:
        accessor = _child(tc, "accessor")
        if accessor is not None:
            stride = int(accessor.get("stride", "3"))
    if stride <= 0:
        raise SceneRefusal(
            f"{path}: source {source.get('id')!r} has stride {stride} (must be > 0)."
        )
    return values, stride


def _parse_geometries(root: ET.Element, path: Path) -> dict[str, _GeomData]:
    result: dict[str, _GeomData] = {}
    for lib in _children(root, "library_geometries"):
        for geom in _children(lib, "geometry"):
            geom_id = geom.get("id")
            mesh_el = _child(geom, "mesh")
            if geom_id is None or mesh_el is None:
                continue
            result[geom_id] = _parse_mesh(mesh_el, path, geom_id)
    return result


def _sources_by_id(mesh_el: ET.Element) -> dict[str, ET.Element]:
    return {s.get("id"): s for s in _children(mesh_el, "source") if s.get("id")}


def _vertices_position_source(mesh_el: ET.Element, sources: dict[str, ET.Element]) -> str | None:
    vertices = _child(mesh_el, "vertices")
    if vertices is None:
        return None
    for inp in _children(vertices, "input"):
        if inp.get("semantic") == "POSITION":
            return inp.get("source", "").lstrip("#")
    return None


def _parse_mesh(mesh_el: ET.Element, path: Path, geom_id: str) -> _GeomData:
    if _child(mesh_el, "polygons") is not None:
        raise SceneRefusal(
            f"{path}: geometry {geom_id!r} uses the <polygons> form, which is not "
            "supported. Re-export as triangles (or polylist)."
        )

    sources = _sources_by_id(mesh_el)
    vertices_id = "vertices-of-" + geom_id
    vertices_el = _child(mesh_el, "vertices")
    if vertices_el is not None and vertices_el.get("id"):
        vertices_id = vertices_el.get("id")

    positions_out: list[tuple[float, float, float]] = []
    normals_out: list[tuple[float, float, float]] | None = None
    indices_out: list[int] = []

    for prim_name in ("triangles", "polylist"):
        for prim in _children(mesh_el, prim_name):
            inputs = _children(prim, "input")
            offsets: dict[str, tuple[int, str]] = {}
            max_offset = 0
            for inp in inputs:
                semantic = inp.get("semantic")
                offset = int(inp.get("offset", "0"))
                src = inp.get("source", "").lstrip("#")
                if semantic == "VERTEX" and src == vertices_id:
                    src = _vertices_position_source(mesh_el, sources) or src
                if semantic in ("VERTEX", "NORMAL"):
                    offsets[semantic] = (offset, src)
                max_offset = max(max_offset, offset)
            stride = max_offset + 1

            if "VERTEX" not in offsets:
                continue
            pos_offset, pos_src_id = offsets["VERTEX"]
            pos_source = sources.get(pos_src_id)
            if pos_source is None:
                raise SceneRefusal(
                    f"{path}: geometry {geom_id!r} references missing source {pos_src_id!r}."
                )
            pos_values, pos_stride = _source_stride(pos_source, path)
            pos_count = len(pos_values) // pos_stride

            norm_offset = norm_values = norm_stride = norm_count = None
            if "NORMAL" in offsets:
                n_offset, n_src_id = offsets["NORMAL"]
                norm_source = sources.get(n_src_id)
                if norm_source is None:
                    raise SceneRefusal(
                        f"{path}: geometry {geom_id!r} references missing source {n_src_id!r}."
                    )
                norm_values, norm_stride = _source_stride(norm_source, path)
                norm_count = len(norm_values) // norm_stride
                norm_offset = n_offset
                if normals_out is None:
                    normals_out = []

            p_el = _child(prim, "p")
            indices_flat = [int(x) for x in (p_el.text or "").split()] if p_el is not None else []

            def emit_vertex(base: int) -> int:
                pi = indices_flat[base + pos_offset]
                if pi < 0 or pi >= pos_count:
                    raise SceneRefusal(
                        f"{path}: geometry {geom_id!r} <p> index {pi} is out of range for "
                        f"source with {pos_count} entries."
                    )
                positions_out.append(
                    tuple(pos_values[pi * pos_stride : pi * pos_stride + 3])  # type: ignore[arg-type]
                )
                if norm_offset is not None and normals_out is not None:
                    ni = indices_flat[base + norm_offset]
                    if ni < 0 or ni >= norm_count:
                        raise SceneRefusal(
                            f"{path}: geometry {geom_id!r} <p> normal index {ni} is out of "
                            f"range for source with {norm_count} entries."
                        )
                    normals_out.append(
                        tuple(norm_values[ni * norm_stride : ni * norm_stride + 3])  # type: ignore[arg-type]
                    )
                idx = len(positions_out) - 1
                return idx

            if prim_name == "triangles":
                n_verts = len(indices_flat) // stride
                for v in range(0, n_verts, 3):
                    a = emit_vertex((v) * stride)
                    b = emit_vertex((v + 1) * stride)
                    c = emit_vertex((v + 2) * stride)
                    indices_out.extend((a, b, c))
            else:  # polylist
                vcount_el = _child(prim, "vcount")
                vcounts = (
                    [int(x) for x in (vcount_el.text or "").split()]
                    if vcount_el is not None
                    else []
                )
                cursor = 0
                for vcount in vcounts:
                    verts = [emit_vertex((cursor + k) * stride) for k in range(vcount)]
                    cursor += vcount
                    # Fan-triangulate.
                    for k in range(1, vcount - 1):
                        indices_out.extend((verts[0], verts[k], verts[k + 1]))

    return positions_out, normals_out, indices_out


# --------------------------------------------------------------------------- visual scenes

_Matrix = tuple[float, ...]  # 16 row-major
_IDENTITY: _Matrix = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


def _mat_mul(a: _Matrix, b: _Matrix) -> _Matrix:
    out = [0.0] * 16
    for r in range(4):
        for c in range(4):
            out[r * 4 + c] = sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
    return tuple(out)


def _translate_matrix(x: float, y: float, z: float) -> _Matrix:
    return (
        1.0,
        0.0,
        0.0,
        x,
        0.0,
        1.0,
        0.0,
        y,
        0.0,
        0.0,
        1.0,
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _scale_matrix(x: float, y: float, z: float) -> _Matrix:
    return (
        x,
        0.0,
        0.0,
        0.0,
        0.0,
        y,
        0.0,
        0.0,
        0.0,
        0.0,
        z,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _rotate_matrix(x: float, y: float, z: float, degrees: float) -> _Matrix:
    length = math.sqrt(x * x + y * y + z * z)
    if length == 0:
        return _IDENTITY
    x, y, z = x / length, y / length, z / length
    theta = math.radians(degrees)
    c = math.cos(theta)
    s = math.sin(theta)
    t = 1 - c
    return (
        t * x * x + c,
        t * x * y - s * z,
        t * x * z + s * y,
        0.0,
        t * x * y + s * z,
        t * y * y + c,
        t * y * z - s * x,
        0.0,
        t * x * z - s * y,
        t * y * z + s * x,
        t * z * z + c,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _node_local_transform(node: ET.Element) -> _Matrix:
    """Compose a node's transform elements in document order, as COLLADA requires."""
    m = _IDENTITY
    for child in node:
        name = _tag(child)
        if name == "matrix" and child.text:
            vals = [float(x) for x in child.text.split()]
            if len(vals) == 16:
                m = _mat_mul(m, tuple(vals))
        elif name == "translate" and child.text:
            x, y, z = (float(v) for v in child.text.split())
            m = _mat_mul(m, _translate_matrix(x, y, z))
        elif name == "rotate" and child.text:
            x, y, z, deg = (float(v) for v in child.text.split())
            m = _mat_mul(m, _rotate_matrix(x, y, z, deg))
        elif name == "scale" and child.text:
            x, y, z = (float(v) for v in child.text.split())
            m = _mat_mul(m, _scale_matrix(x, y, z))
    return m


def _transform_point(m: _Matrix, p: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = p
    return (
        m[0] * x + m[1] * y + m[2] * z + m[3],
        m[4] * x + m[5] * y + m[6] * z + m[7],
        m[8] * x + m[9] * y + m[10] * z + m[11],
    )


def _transform_normal(m: _Matrix, n: tuple[float, float, float]) -> tuple[float, float, float]:
    # Rotation/scale only, no translation, for direction vectors.
    # ponytail: renormalize only; non-uniform scale needs the inverse-transpose.
    x, y, z = n
    nx, ny, nz = (
        m[0] * x + m[1] * y + m[2] * z,
        m[4] * x + m[5] * y + m[6] * z,
        m[8] * x + m[9] * y + m[10] * z,
    )
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / length, ny / length, nz / length) if length else (nx, ny, nz)


def _to_zup(p: tuple[float, float, float], up_axis: str) -> tuple[float, float, float]:
    x, y, z = p
    if up_axis == "Y_UP":
        return (x, -z, y)
    if up_axis == "X_UP":
        return (y, z, x)
    return (x, y, z)  # Z_UP as-is


def _apply_transform(geom: _GeomData, m: _Matrix, unit_scale: float, up_axis: str) -> Mesh:
    positions, normals, indices = geom
    out_positions: list[float] = []
    for p in positions:
        scaled = (p[0] * unit_scale, p[1] * unit_scale, p[2] * unit_scale)
        world = _transform_point(m, scaled)
        out_positions.extend(_to_zup(world, up_axis))
    out_normals: list[float] | None = None
    if normals is not None:
        out_normals = []
        for n in normals:
            world_n = _transform_normal(m, n)
            out_normals.extend(_to_zup(world_n, up_axis))
    return (
        tuple(out_positions),
        tuple(out_normals) if out_normals is not None else None,
        tuple(indices),
    )


def _index_nodes(root: ET.Element, path: Path) -> dict[str, ET.Element]:
    nodes: dict[str, ET.Element] = {}

    def collect(el: ET.Element) -> None:
        for child in el:
            if _tag(child) == "node" and child.get("id"):
                nodes[child.get("id")] = child
            collect(child)

    for lib_name in ("library_nodes", "library_visual_scenes"):
        for lib in _children(root, lib_name):
            collect(lib)
    return nodes


def _find_visual_scene(root: ET.Element, path: Path) -> ET.Element | None:
    scene_el = _child(root, "scene")
    if scene_el is None:
        return None
    inst = _child(scene_el, "instance_visual_scene")
    if inst is None:
        return None
    url = inst.get("url", "")
    scene_id = url.lstrip("#")
    for lib in _children(root, "library_visual_scenes"):
        for vs in _children(lib, "visual_scene"):
            if vs.get("id") == scene_id:
                return vs
    raise SceneRefusal(f"{path}: <instance_visual_scene> targets missing url {url!r}.")


def _walk_node(
    node: ET.Element,
    parent_m: _Matrix,
    geometries: dict[str, _GeomData],
    nodes_by_id: dict[str, ET.Element],
    unit_scale: float,
    up_axis: str,
    meshes: list[Mesh],
    skipped: list[str],
    path: Path,
    depth: int,
    visited: frozenset[str] = frozenset(),
) -> None:
    if depth > _MAX_INSTANCE_NODE_DEPTH:
        raise SceneRefusal(
            f"{path}: <instance_node> nesting exceeds depth {_MAX_INSTANCE_NODE_DEPTH}."
        )
    m = _mat_mul(parent_m, _node_local_transform(node))

    for inst in _children(node, "instance_geometry"):
        url = inst.get("url", "")
        geom_id = url.lstrip("#")
        geom = geometries.get(geom_id)
        if geom is None:
            raise SceneRefusal(f"{path}: <instance_geometry> targets missing url {url!r}.")
        meshes.append(_apply_transform(geom, m, unit_scale, up_axis))

    for inst in _children(node, "instance_controller"):
        url = inst.get("url", "")
        skipped.append(f"{url.lstrip('#')}: <instance_controller> (skin/morph) not read")

    for inst in _children(node, "instance_node"):
        url = inst.get("url", "")
        ref_id = url.lstrip("#")
        target = nodes_by_id.get(ref_id)
        if target is None:
            raise SceneRefusal(f"{path}: <instance_node> targets missing url {url!r}.")
        if ref_id in visited:
            raise SceneRefusal(f"{path}: <instance_node> cycle detected at {url!r}.")
        _walk_node(
            target,
            m,
            geometries,
            nodes_by_id,
            unit_scale,
            up_axis,
            meshes,
            skipped,
            path,
            depth + 1,
            visited | {ref_id},
        )

    for child in _children(node, "node"):
        _walk_node(
            child,
            m,
            geometries,
            nodes_by_id,
            unit_scale,
            up_axis,
            meshes,
            skipped,
            path,
            depth + 1,
            visited,
        )
