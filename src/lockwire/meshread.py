"""Hand-read STL (ASCII and binary) and DAE meshes for `add link`: a triangle count and bounds.

The file is untrusted: size and triangle caps, and a binary header must match the file length.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

MAX_BYTES = 32 * 1024 * 1024
MAX_TRIANGLES = 200_000


class MeshRefusal(ValueError):
    """A plain one-line reason the mesh cannot be imported."""


def _bounds(points: np.ndarray, what: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if not np.isfinite(points).all():
        raise MeshRefusal(f"{what} has a vertex that is not a finite number.")
    return tuple(points.min(axis=0)), tuple(points.max(axis=0))


def _read_stl(raw: bytes, name: str) -> tuple[int, np.ndarray]:
    if len(raw) >= 84:
        count = int.from_bytes(raw[80:84], "little")
        if len(raw) == 84 + 50 * count:
            if count == 0:
                raise MeshRefusal(f"{name} has no triangles.")
            if count > MAX_TRIANGLES:
                raise MeshRefusal(f"{name} has {count} triangles; the limit is {MAX_TRIANGLES}.")
            rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
            return count, np.frombuffer(raw, rec, count, 84)["v"].reshape(-1, 3).astype(float)
    if raw[:5].lower() == b"solid" and b"facet" in raw[:2048] + raw[-2048:]:
        nums = []
        for hit in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", raw):
            nums.append(hit.groups())
            if len(nums) > 3 * MAX_TRIANGLES:
                raise MeshRefusal(f"{name} has over {MAX_TRIANGLES} triangles; that is the limit.")
        try:
            pts = np.array(nums, dtype=float).reshape(-1, 3)
        except ValueError:
            raise MeshRefusal(f"{name} has a vertex that is not a number.") from None
        if len(pts) == 0 or len(pts) % 3:
            raise MeshRefusal(f"{name} is an empty or broken ASCII STL.")
        return len(pts) // 3, pts
    if len(raw) >= 84:
        raise MeshRefusal(f"{name} is a binary STL whose header count does not match its length.")
    raise MeshRefusal(f"{name} is not a readable STL.")


def read_mesh(path: Path) -> tuple[int, tuple[float, ...], tuple[float, ...]]:
    """(triangle count, min xyz, max xyz) in the file's own units (a DAE's own unit is applied)."""
    name = path.name
    suffix = path.suffix.lower()
    if suffix not in (".stl", ".dae"):
        raise MeshRefusal(f"{name} is not an STL or DAE mesh.")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise MeshRefusal(f"{name} is over {MAX_BYTES // 2**20} MiB; use a smaller mesh.")
        raw = path.read_bytes()
    except OSError:
        raise MeshRefusal(f"{name} cannot be read.") from None
    if suffix == ".stl":
        count, pts = _read_stl(raw, name)
    else:
        from lockwire.mesh_dae import read_dae
        from lockwire.scene import SceneRefusal

        try:
            meshes = read_dae(path).meshes
        except SceneRefusal:
            raise MeshRefusal(f"{name} is not a readable DAE mesh.") from None
        count = sum(len(m[2]) // 3 for m in meshes)
        if count == 0:
            raise MeshRefusal(f"{name} has no triangles.")
        if count > MAX_TRIANGLES:
            raise MeshRefusal(f"{name} has {count} triangles; the limit is {MAX_TRIANGLES}.")
        pts = np.array([v for m in meshes for v in zip(*[iter(m[0])] * 3)], dtype=float)
    lo, hi = _bounds(pts, name)
    return count, lo, hi
