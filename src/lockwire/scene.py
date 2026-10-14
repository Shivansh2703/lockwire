"""Stdlib glTF 2.0 reader for the scene door (a subset; ids in the extras join key).

Accepts `.glb` (binary container) and `.gltf` with either a relative sidecar `.bin` or a
`data:` base64 URI buffer. No third-party glTF library (no tool-specific dependency) —
`json`, `struct`, `base64`, `math`, `pathlib`, `dataclasses` only.

Refusal posture mirrors `urdf.py`'s DOCTYPE/xacro refusals: refuse on raw bytes/structure
before any geometry is trusted, name the specific defect, and say the remedy.
"""

import base64
import hashlib
import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024 * 1024

_COMPONENT_FORMATS = {
    5121: ("B", 1),  # UNSIGNED_BYTE
    5123: ("H", 2),  # UNSIGNED_SHORT
    5125: ("I", 4),  # UNSIGNED_INT
    5126: ("f", 4),  # FLOAT
}
_TYPE_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_TRIANGLES_MODE = 4


class SceneRefusal(ValueError):
    """The scene file is malformed or uses an unsupported feature; message names the
    defect and the remedy. The CLI maps this to exit 2."""


@dataclass(frozen=True)
class ScenePart:
    node: str
    extras_id: str | None
    name_chain: tuple[str, ...]
    positions: tuple[float, ...]
    normals: tuple[float, ...] | None
    indices: tuple[int, ...]
    color: tuple[float, float, float, float]
    translation: tuple[float, float, float]


@dataclass(frozen=True)
class Scene:
    parts: tuple[ScenePart, ...]
    up: Literal["y", "z"]
    nodes_with_ids: dict[str, str]
    source: str
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]
    triangles: int
    vertices: int
    skipped: tuple[str, ...] = field(default_factory=tuple)
    asset_path: str | None = None
    asset_sha256: str | None = None


# --------------------------------------------------------------------------- containers


def _read_glb(raw: bytes, path: Path) -> tuple[dict, bytes | None]:
    if len(raw) < 12 or raw[0:4] != b"glTF":
        raise SceneRefusal(
            f"{path}: not a valid GLB — bad magic header (expected b'glTF'). "
            "Re-export as glTF 2.0 binary (.glb)."
        )
    magic, version, length = struct.unpack_from("<4sII", raw, 0)
    if version != 2:
        raise SceneRefusal(
            f"{path}: unsupported glTF binary version {version} (only version 2 is supported)."
        )
    if length > len(raw):
        raise SceneRefusal(
            f"{path}: truncated GLB — header declares {length} bytes but the file has "
            f"{len(raw)}. Re-export the file."
        )
    offset = 12
    json_chunk: bytes | None = None
    bin_chunk: bytes | None = None
    while offset + 8 <= length:
        chunk_len, chunk_type = struct.unpack_from("<II", raw, offset)
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_len
        if chunk_end > length:
            raise SceneRefusal(f"{path}: truncated GLB — a chunk overruns the declared length.")
        chunk_data = raw[chunk_start:chunk_end]
        if chunk_type == 0x4E4F534A:  # 'JSON'
            if len(chunk_data) > _MAX_JSON_BYTES:
                raise SceneRefusal(
                    f"{path}: GLB JSON chunk is {len(chunk_data)} bytes, over the "
                    f"{_MAX_JSON_BYTES}-byte guard. Refusing rather than parse an "
                    "unexpectedly large document."
                )
            json_chunk = chunk_data
        elif chunk_type == 0x004E4942:  # 'BIN\0'
            bin_chunk = chunk_data
        offset = chunk_end
    if json_chunk is None:
        raise SceneRefusal(f"{path}: GLB has no JSON chunk.")
    try:
        doc = json.loads(json_chunk)
    except json.JSONDecodeError as exc:
        raise SceneRefusal(f"{path}: GLB JSON chunk is not valid JSON ({exc}).") from exc
    return doc, bin_chunk


def _refuse_buffer_uri(uri: str, base_dir: Path, path: Path) -> None:
    if uri.startswith("data:"):
        return
    if uri.startswith("http://") or uri.startswith("https://") or uri.startswith("file:"):
        raise SceneRefusal(
            f"{path}: buffer URI {uri!r} is a network/file URI; only a relative sidecar "
            "path or a data: URI is accepted. Re-export with a local relative buffer."
        )
    if Path(uri).is_absolute():
        raise SceneRefusal(
            f"{path}: buffer URI {uri!r} is an absolute path; only a relative sidecar "
            "path is accepted. Re-export with a relative buffer path."
        )
    resolved = (base_dir / uri).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError as exc:
        raise SceneRefusal(
            f"{path}: buffer URI {uri!r} escapes the scene file's directory. "
            "Keep buffer files alongside the .gltf."
        ) from exc


def _load_buffer(uri: str, base_dir: Path, path: Path) -> bytes:
    _refuse_buffer_uri(uri, base_dir, path)
    if uri.startswith("data:"):
        _, _, data_part = uri.partition(",")
        try:
            return base64.b64decode(data_part)
        except Exception as exc:  # noqa: BLE001 - report as a scene refusal, not a crash
            raise SceneRefusal(f"{path}: buffer data: URI is not valid base64.") from exc
    sidecar = base_dir / uri
    if not sidecar.is_file():
        raise SceneRefusal(f"{path}: buffer sidecar {uri!r} does not exist next to the scene file.")
    return sidecar.read_bytes()


def _read_gltf_json(path: Path, raw: bytes) -> dict:
    if len(raw) > _MAX_JSON_BYTES:
        raise SceneRefusal(
            f"{path}: .gltf JSON is {len(raw)} bytes, over the {_MAX_JSON_BYTES}-byte guard."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SceneRefusal(f"{path}: not valid JSON ({exc}).") from exc


# --------------------------------------------------------------------------- accessors


def _accessor_data(doc: dict, index: int, buffers: list[bytes], path: Path) -> list[float | int]:
    accessors = doc.get("accessors", [])
    if index < 0 or index >= len(accessors):
        raise SceneRefusal(f"{path}: accessor index {index} out of range.")
    acc = accessors[index]
    if "sparse" in acc:
        raise SceneRefusal(
            f"{path}: accessor {index} is sparse; sparse accessors are not supported. "
            "Re-export without sparse accessors."
        )
    if acc.get("normalized"):
        raise SceneRefusal(
            f"{path}: accessor {index} sets `normalized`, which is not supported. "
            "Re-export with un-normalized accessors."
        )
    component_type = acc["componentType"]
    if component_type not in _COMPONENT_FORMATS:
        raise SceneRefusal(
            f"{path}: accessor {index} has unsupported componentType {component_type}."
        )
    fmt_char, comp_size = _COMPONENT_FORMATS[component_type]
    acc_type = acc["type"]
    if acc_type not in _TYPE_COUNTS:
        raise SceneRefusal(f"{path}: accessor {index} has unsupported type {acc_type!r}.")
    n_components = _TYPE_COUNTS[acc_type]
    count = acc["count"]

    bv_index = acc.get("bufferView")
    if bv_index is None:
        # Accessor with no bufferView is legal (implies zero-filled); not needed for our subset.
        return [0.0] * (count * n_components)
    buffer_views = doc.get("bufferViews", [])
    if bv_index < 0 or bv_index >= len(buffer_views):
        raise SceneRefusal(f"{path}: accessor {index} references missing bufferView {bv_index}.")
    bv = buffer_views[bv_index]
    buf_index = bv.get("buffer", 0)
    if buf_index < 0 or buf_index >= len(buffers):
        raise SceneRefusal(f"{path}: bufferView {bv_index} references missing buffer {buf_index}.")
    buf = buffers[buf_index]
    base_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    default_stride = n_components * comp_size
    stride = bv.get("byteStride", default_stride)

    out: list[float | int] = []
    struct_fmt = "<" + fmt_char * n_components
    elem_size = n_components * comp_size
    for i in range(count):
        offset = base_offset + i * stride
        if offset + elem_size > len(buf):
            raise SceneRefusal(
                f"{path}: accessor {index} reads past the end of its buffer (truncated file)."
            )
        out.extend(struct.unpack_from(struct_fmt, buf, offset))
    return out


# --------------------------------------------------------------------------- transforms


def _quat_to_matrix(x: float, y: float, z: float, w: float) -> list[list[float]]:
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        [1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)],
        [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)],
        [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)],
    ]


def _mat4_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _identity4() -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _node_local_matrix(node: dict) -> list[list[float]]:
    if "matrix" in node:
        m = node["matrix"]  # column-major, 16 floats
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    t = node.get("translation", [0.0, 0.0, 0.0])
    r = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    s = node.get("scale", [1.0, 1.0, 1.0])
    rot3 = _quat_to_matrix(*r)
    mat = _identity4()
    for i in range(3):
        for j in range(3):
            mat[i][j] = rot3[i][j] * s[j]
        mat[i][3] = t[i]
    return mat


def _apply_matrix(mat: list[list[float]], point: tuple[float, float, float], w: float = 1.0):
    x, y, z = point
    out = []
    for i in range(3):
        out.append(mat[i][0] * x + mat[i][1] * y + mat[i][2] * z + mat[i][3] * w)
    return tuple(out)


def _yup_to_zup(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x, -z, y)


# --------------------------------------------------------------------------- ids / hierarchy


def _extras_id(extras: dict | None) -> str | None:
    if not extras:
        return None
    lockwire = extras.get("lockwire")
    if isinstance(lockwire, dict) and isinstance(lockwire.get("id"), str):
        return lockwire["id"]
    flat = extras.get("lockwire_id")
    if isinstance(flat, str):
        return flat
    return None


@dataclass
class _Walked:
    node_index: int
    name_chain: tuple[str, ...]
    owning_id: str | None  # nearest ancestor-or-self extras id
    owning_id_node_index: int | None
    world: list[list[float]]


def _walk(
    doc: dict,
    node_indices: list[int],
    parent_world: list[list[float]],
    parent_names: tuple[str, ...],
    parent_id: str | None,
    parent_id_node_index: int | None,
    out: list[_Walked],
    ids_seen: dict[str, list[int]],
) -> None:
    nodes = doc.get("nodes", [])
    for idx in node_indices:
        node = nodes[idx]
        local = _node_local_matrix(node)
        world = _mat4_mul(parent_world, local)
        name = node.get("name", "")
        name_chain = parent_names + (name,)
        extras = node.get("extras")
        own_id = _extras_id(extras)
        if own_id is not None:
            ids_seen.setdefault(own_id, []).append(idx)
            owning_id, owning_id_node_index = own_id, idx
        else:
            # Name-fallback join (a node named after a contract id) is resolved by
            # the caller against the contract; the reader only records name_chain.
            owning_id, owning_id_node_index = parent_id, parent_id_node_index
        out.append(_Walked(idx, name_chain, owning_id, owning_id_node_index, world))
        children = node.get("children", [])
        if children:
            _walk(doc, children, world, name_chain, owning_id, owning_id_node_index, out, ids_seen)


def _check_duplicate_ids(ids_seen: dict[str, list[int]], doc: dict, path: Path) -> None:
    nodes = doc.get("nodes", [])
    # Build a parent map to test ancestor relationships.
    parent_of: dict[int, int] = {}
    for i, node in enumerate(nodes):
        for c in node.get("children", []):
            parent_of[c] = i

    def is_ancestor(a: int, b: int) -> bool:
        cur = b
        while cur in parent_of:
            cur = parent_of[cur]
            if cur == a:
                return True
        return False

    for lw_id, indices in ids_seen.items():
        if len(indices) < 2:
            continue
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                a, b = indices[i], indices[j]
                if a == b or is_ancestor(a, b) or is_ancestor(b, a):
                    continue
                raise SceneRefusal(
                    f"{path}: two parts in the scene both claim `{lw_id}`; give each part one id."
                )


# --------------------------------------------------------------------------- top level


def load_scene(path: Path) -> Scene:
    path = Path(path)
    if not path.is_file():
        raise SceneRefusal(f"{path}: file does not exist.")
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise SceneRefusal(
            f"{path}: file is {size} bytes, over the {_MAX_FILE_BYTES}-byte guard. "
            "Re-export a smaller scene or split it."
        )
    raw = path.read_bytes()

    buffers: list[bytes]
    if path.suffix.lower() == ".glb":
        doc, bin_chunk = _read_glb(raw, path)
        buffers = [bin_chunk if bin_chunk is not None else b""]
        for i in range(1, len(doc.get("buffers", []))):
            raise SceneRefusal(f"{path}: GLB with more than one external buffer is not supported.")
    elif path.suffix.lower() == ".gltf":
        doc = _read_gltf_json(path, raw)
        buffers = []
        for buf_def in doc.get("buffers", []):
            uri = buf_def.get("uri")
            if uri is None:
                raise SceneRefusal(f"{path}: .gltf buffer has no uri and this is not a .glb.")
            buffers.append(_load_buffer(uri, path.parent, path))
    else:
        raise SceneRefusal(
            f"{path}: unsupported file extension {path.suffix!r} (expected .glb or .gltf)."
        )

    ext_required = doc.get("extensionsRequired") or []
    if ext_required:
        names = ", ".join(ext_required)
        raise SceneRefusal(
            f"{path}: scene requires extensions this reader does not implement: {names}. "
            "Re-export without them."
        )

    asset_up = ((doc.get("asset") or {}).get("extras") or {}).get("lockwire", {}).get("up")
    scene_index = doc.get("scene", 0)
    scenes = doc.get("scenes", [])
    if scene_index < 0 or scene_index >= len(scenes):
        raise SceneRefusal(f"{path}: default scene index {scene_index} out of range.")
    root_indices = scenes[scene_index].get("nodes", [])

    up: Literal["y", "z"] = "z" if asset_up == "z" else "y"

    walked: list[_Walked] = []
    ids_seen: dict[str, list[int]] = {}
    _walk(doc, root_indices, _identity4(), (), None, None, walked, ids_seen)
    _check_duplicate_ids(ids_seen, doc, path)

    nodes_with_ids = {
        lw_id: doc["nodes"][idxs[0]].get("name", "") for lw_id, idxs in ids_seen.items()
    }

    materials = doc.get("materials", [])
    meshes = doc.get("meshes", [])
    nodes = doc.get("nodes", [])

    parts: list[ScenePart] = []
    skipped: list[str] = []
    total_tris = 0
    total_verts = 0
    min_b = [math.inf, math.inf, math.inf]
    max_b = [-math.inf, -math.inf, -math.inf]

    def convert(p: tuple[float, float, float]) -> tuple[float, float, float]:
        return p if up == "z" else _yup_to_zup(*p)

    for w in walked:
        node = nodes[w.node_index]
        mesh_index = node.get("mesh")
        if mesh_index is None:
            continue
        mesh = meshes[mesh_index]
        for prim in mesh.get("primitives", []):
            mode = prim.get("mode", _TRIANGLES_MODE)
            if mode != _TRIANGLES_MODE:
                skipped.append(node.get("name", ""))
                continue
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                raise SceneRefusal(
                    f"{path}: primitive on node {node.get('name', '')!r} has no POSITION accessor."
                )
            raw_pos = _accessor_data(doc, attrs["POSITION"], buffers, path)
            n_verts = len(raw_pos) // 3
            world_positions: list[float] = []
            for i in range(n_verts):
                local_pt = (raw_pos[3 * i], raw_pos[3 * i + 1], raw_pos[3 * i + 2])
                world_pt = _apply_matrix(w.world, local_pt, 1.0)
                gx, gy, gz = convert(world_pt)
                world_positions.extend((gx, gy, gz))
                min_b[0] = min(min_b[0], gx)
                min_b[1] = min(min_b[1], gy)
                min_b[2] = min(min_b[2], gz)
                max_b[0] = max(max_b[0], gx)
                max_b[1] = max(max_b[1], gy)
                max_b[2] = max(max_b[2], gz)

            normals_tuple: tuple[float, ...] | None = None
            if "NORMAL" in attrs:
                raw_norm = _accessor_data(doc, attrs["NORMAL"], buffers, path)
                world_normals: list[float] = []
                for i in range(len(raw_norm) // 3):
                    local_n = (raw_norm[3 * i], raw_norm[3 * i + 1], raw_norm[3 * i + 2])
                    world_n = _apply_matrix(w.world, local_n, 0.0)
                    world_normals.extend(convert(world_n))
                normals_tuple = tuple(world_normals)

            indices_index = prim.get("indices")
            if indices_index is not None:
                idx_data = _accessor_data(doc, indices_index, buffers, path)
                indices = tuple(int(i) for i in idx_data)
            else:
                indices = tuple(range(n_verts))
            if len(indices) % 3 != 0:
                raise SceneRefusal(
                    f"{path}: primitive on node {node.get('name', '')!r} has "
                    f"{len(indices)} indices, not a multiple of 3 (mode 4 requires triangles)."
                )
            if indices and max(indices) >= n_verts:
                raise SceneRefusal(
                    f"{path}: index {max(indices)} points past the {n_verts} vertices of "
                    f"node {node.get('name', '')!r}; re-export the mesh."
                )

            mat_index = prim.get("material")
            color = (0.8, 0.8, 0.8, 1.0)
            if mat_index is not None and 0 <= mat_index < len(materials):
                bcf = (materials[mat_index].get("pbrMetallicRoughness") or {}).get(
                    "baseColorFactor"
                )
                if bcf is not None:
                    color = tuple(float(c) for c in bcf)  # type: ignore[assignment]

            if w.owning_id_node_index is not None:
                owning_world = next(
                    ww.world for ww in walked if ww.node_index == w.owning_id_node_index
                )
                ox, oy, oz = _apply_matrix(owning_world, (0.0, 0.0, 0.0), 1.0)
            else:
                ox, oy, oz = _apply_matrix(w.world, (0.0, 0.0, 0.0), 1.0)
            translation = convert((ox, oy, oz))

            parts.append(
                ScenePart(
                    node=node.get("name", ""),
                    extras_id=w.owning_id,
                    name_chain=w.name_chain,
                    positions=tuple(world_positions),
                    normals=normals_tuple,
                    indices=indices,
                    color=color,  # type: ignore[arg-type]
                    translation=translation,
                )
            )
            total_tris += len(indices) // 3
            total_verts += n_verts

    if not parts:
        min_b = [0.0, 0.0, 0.0]
        max_b = [0.0, 0.0, 0.0]

    return Scene(
        parts=tuple(parts),
        up=up,
        nodes_with_ids=nodes_with_ids,
        source=path.name,
        asset_path=str(path.resolve()),
        asset_sha256=hashlib.sha256(raw).hexdigest(),
        bounds=(tuple(min_b), tuple(max_b)),  # type: ignore[arg-type]
        triangles=total_tris,
        vertices=total_verts,
        skipped=tuple(skipped),
    )
