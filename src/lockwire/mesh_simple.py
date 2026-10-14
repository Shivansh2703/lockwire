"""Stdlib STL and OBJ mesh readers (`lockwire.scene_urdf`'s `<mesh>` door).

Both readers flatten every triangle corner into its own vertex (no index
sharing) — the simplest thing that is still correct, and it sidesteps OBJ's
independent position/normal index streams without a join table.  Not a
memory-optimal representation for huge meshes; the largest mesh surveyed was
~326k triangles (~1M floats), well inside what this project ships pages
with already.

``MeshRefusal`` mirrors ``SceneRefusal``'s posture (message names the defect
and the remedy); ``scene_urdf.py`` re-wraps it as a ``SceneRefusal``.
"""

import struct
from pathlib import Path

_STL_HEADER = 80
_STL_RECORD = 50  # 12 floats (normal + 3 verts) + uint16 attribute byte count


class MeshRefusal(ValueError):
    """The mesh file is malformed or truncated; message names the defect."""


def _looks_ascii_stl(raw: bytes) -> bool:
    try:
        head = raw[:512].decode("ascii")
    except UnicodeDecodeError:
        return False
    return head.lstrip().lower().startswith("solid")


def read_stl(path: Path) -> tuple[tuple[float, ...], tuple[float, ...], tuple[int, ...]]:
    """Binary or ASCII STL -> (positions, normals, indices), flat per-facet."""
    raw = path.read_bytes()
    if not raw:
        raise MeshRefusal(f"{path}: STL file is empty. Re-export it.")
    if len(raw) >= _STL_HEADER + 4:
        (count,) = struct.unpack_from("<I", raw, _STL_HEADER)
        expected = _STL_HEADER + 4 + _STL_RECORD * count
        if len(raw) == expected:
            return _read_stl_binary(raw, count, path)
        if not _looks_ascii_stl(raw):
            raise MeshRefusal(
                f"{path}: STL is {len(raw)} bytes but declares {count} triangles "
                f"(needs {expected}); the file is truncated or corrupt. Re-export it."
            )
    return _read_stl_ascii(raw, path)


def _read_stl_binary(
    raw: bytes, count: int, path: Path
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[int, ...]]:
    positions: list[float] = []
    normals: list[float] = []
    offset = _STL_HEADER + 4
    try:
        for _ in range(count):
            nx, ny, nz, v1x, v1y, v1z, v2x, v2y, v2z, v3x, v3y, v3z, _attr = struct.unpack_from(
                "<12fH", raw, offset
            )
            positions.extend((v1x, v1y, v1z, v2x, v2y, v2z, v3x, v3y, v3z))
            normals.extend((nx, ny, nz) * 3)
            offset += _STL_RECORD
    except struct.error as exc:
        raise MeshRefusal(f"{path}: binary STL is truncated mid-triangle. Re-export it.") from exc
    indices = tuple(range(len(positions) // 3))
    return tuple(positions), tuple(normals), indices


def _read_stl_ascii(
    raw: bytes, path: Path
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[int, ...]]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MeshRefusal(
            f"{path}: not a recognizable STL — not binary (size mismatch) and not "
            f"ASCII (non-ASCII bytes). Re-export as binary or ASCII STL."
        ) from exc
    stripped = text.strip()
    if not stripped.lower().startswith("solid"):
        raise MeshRefusal(
            f"{path}: not a recognizable STL — no binary header match and no leading "
            "'solid' line. Re-export as binary or ASCII STL."
        )
    positions: list[float] = []
    normals: list[float] = []
    cur_normal: tuple[float, float, float] = (0.0, 0.0, 0.0)
    verts_in_facet: list[tuple[float, float, float]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        words = line.split()
        if not words:
            continue
        if words[0] == "facet" and len(words) >= 5 and words[1] == "normal":
            try:
                cur_normal = tuple(float(x) for x in words[2:5])  # type: ignore[assignment]
            except ValueError as exc:
                raise MeshRefusal(
                    f"{path}:{lineno}: 'facet normal' line has a non-numeric value."
                ) from exc
            verts_in_facet = []
        elif words[0] == "vertex" and len(words) >= 4:
            try:
                verts_in_facet.append(tuple(float(x) for x in words[1:4]))  # type: ignore[arg-type]
            except ValueError as exc:
                raise MeshRefusal(
                    f"{path}:{lineno}: 'vertex' line has a non-numeric value."
                ) from exc
        elif words[0] == "endfacet":
            if len(verts_in_facet) != 3:
                raise MeshRefusal(
                    f"{path}: ASCII STL facet has {len(verts_in_facet)} vertices, expected 3."
                )
            for v in verts_in_facet:
                positions.extend(v)
                normals.extend(cur_normal)
            verts_in_facet = []
    if not positions:
        raise MeshRefusal(f"{path}: ASCII STL has no facets.")
    indices = tuple(range(len(positions) // 3))
    return tuple(positions), tuple(normals), indices


def read_obj(path: Path) -> tuple[tuple[float, ...], tuple[float, ...] | None, tuple[int, ...]]:
    """OBJ (v, vn, f) -> (positions, normals or None, indices), fan-triangulated,
    flat per-corner (no shared vertex/normal index join)."""
    verts: list[tuple[float, float, float]] = []
    vnormals: list[tuple[float, float, float]] = []
    positions: list[float] = []
    normals: list[float] = []
    have_normals = False
    face_count = 0

    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        words = line.split()
        if not words:
            continue
        tag = words[0]
        if tag == "v" and len(words) >= 4:
            try:
                verts.append((float(words[1]), float(words[2]), float(words[3])))
            except ValueError as exc:
                raise MeshRefusal(f"{path}:{lineno}: 'v' line has a non-numeric value.") from exc
        elif tag == "vn" and len(words) >= 4:
            try:
                vnormals.append((float(words[1]), float(words[2]), float(words[3])))
            except ValueError as exc:
                raise MeshRefusal(f"{path}:{lineno}: 'vn' line has a non-numeric value.") from exc
        elif tag == "f" and len(words) >= 4:
            corners = words[1:]
            resolved: list[tuple[int, int | None]] = []
            for corner in corners:
                parts = corner.split("/")
                try:
                    v_idx = int(parts[0])
                except ValueError as exc:
                    raise MeshRefusal(
                        f"{path}:{lineno}: face corner {corner!r} has no vertex index."
                    ) from exc
                v_idx = v_idx - 1 if v_idx > 0 else len(verts) + v_idx
                n_idx = None
                if len(parts) == 3 and parts[2]:
                    try:
                        n_idx = int(parts[2])
                    except ValueError as exc:
                        raise MeshRefusal(
                            f"{path}:{lineno}: face corner {corner!r} has a "
                            "non-numeric normal index."
                        ) from exc
                    n_idx = n_idx - 1 if n_idx > 0 else len(vnormals) + n_idx
                if v_idx < 0 or v_idx >= len(verts):
                    raise MeshRefusal(
                        f"{path}:{lineno}: face vertex index {parts[0]} is out of range "
                        f"({len(verts)} vertices read so far)."
                    )
                resolved.append((v_idx, n_idx))
            # Fan-triangulate polygons with >3 corners.
            for i in range(1, len(resolved) - 1):
                for v_idx, n_idx in (resolved[0], resolved[i], resolved[i + 1]):
                    positions.extend(verts[v_idx])
                    if n_idx is not None:
                        if n_idx < 0 or n_idx >= len(vnormals):
                            raise MeshRefusal(
                                f"{path}:{lineno}: face normal index out of range "
                                f"({len(vnormals)} normals read so far)."
                            )
                        normals.extend(vnormals[n_idx])
                        have_normals = True
                    else:
                        normals.extend((0.0, 0.0, 0.0))
                face_count += 1

    if not positions:
        raise MeshRefusal(f"{path}: OBJ has no triangulable faces.")
    indices = tuple(range(len(positions) // 3))
    normals_out = tuple(normals) if have_normals else None
    return tuple(positions), normals_out, indices
