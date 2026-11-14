"""Where a link's mass comes from, best source first.

weighed (measured) > slicer (calculated) > mesh volume x density (estimated).
Slicer import reads PrusaSlicer's G-code footer and Bambu/Orca 3MF ``used_g``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from lockwire.edit import Change
from lockwire.mesh_simple import read_stl
from lockwire.schema import MassKind
from lockwire.urdf import _refuse_doctype_entity

# `[g]` only: the `[cm3]` line sits right beside it and is a volume, never grams.
_GRAMS = re.compile(r"^;\s*(?:total )?filament used \[g\]\s*=\s*([0-9.,\s]+?)\s*$", re.M)
_TAIL = 262144  # footer lives at the end of the file
MAX_3MF_MEMBER_BYTES = 16 * 1024 * 1024  # a slice_info.config is kilobytes
MAX_3MF_MEMBERS = 10_000


class SlicerRefusal(ValueError):
    pass


def _numbers(text: str, path: Path) -> float:
    try:
        return sum(float(v) for v in text.split(",") if v.strip())
    except ValueError:
        raise SlicerRefusal(f"{path}: filament weight is not a number.") from None


def read_slicer_grams(path: Path) -> float:
    """Grams of filament the slicer says this print uses. Raises SlicerRefusal."""
    try:
        if path.suffix.lower() == ".3mf":
            with zipfile.ZipFile(path) as z:
                if len(z.infolist()) > MAX_3MF_MEMBERS:
                    raise SlicerRefusal(f"{path}: the 3MF file has too many parts to read safely.")
                info = z.getinfo("Metadata/slice_info.config")
                too_big = SlicerRefusal(f"{path}: the 3MF slice info is too large to read safely.")
                if info.file_size > MAX_3MF_MEMBER_BYTES:
                    raise too_big
                with z.open(info) as m:
                    raw = m.read(MAX_3MF_MEMBER_BYTES + 1)  # a lying header cannot bypass the cap
                if len(raw) > MAX_3MF_MEMBER_BYTES:
                    raise too_big
            _refuse_doctype_entity(raw, path)
            used = [f.get("used_g") for f in ET.fromstring(raw).iter("filament")]
            grams = sum(float(u) for u in used if u)
        else:
            with path.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - _TAIL))
                tail = f.read().decode("utf-8", "replace")
            m = _GRAMS.findall(tail)
            if not m:
                raise SlicerRefusal(f"{path}: no 'filament used [g]' line in the footer.")
            grams = _numbers(m[-1], path)
    except SlicerRefusal:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError, ValueError) as e:
        raise SlicerRefusal(f"{path}: not a readable slicer file ({type(e).__name__}).") from None
    if grams <= 0:
        raise SlicerRefusal(f"{path}: the slicer file states no filament weight.")
    return grams


def mesh_volume_m3(path: Path) -> float:
    """Closed STL volume by the signed-tetrahedron sum (STL units assumed meters)."""
    pos, _normals, idx = read_stl(path)
    v = [pos[i : i + 3] for i in range(0, len(pos), 3)]
    tris = [(v[idx[i]], v[idx[i + 1]], v[idx[i + 2]]) for i in range(0, len(idx), 3)]
    return abs(
        sum(
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
            for a, b, c in tris
        )
        / 6
    )


def estimate_kg(mesh: Path, density_kg_m3: float) -> float:
    return mesh_volume_m3(mesh) * density_kg_m3


def pick_mass(
    weighed_kg: float | None = None,
    slicer_kg: float | None = None,
    estimate_kg: float | None = None,
) -> tuple[float, MassKind] | None:
    """The mass to use and its kind; None when no source exists (nothing is invented)."""
    for kg, kind in (
        (weighed_kg, "measured"),
        (slicer_kg, "calculated"),
        (estimate_kg, "estimated"),
    ):
        if kg is not None:
            return kg, kind  # type: ignore[return-value]
    return None


def mass_changes(link: str, kg: float, kind: MassKind, source: str, witness: str) -> list[Change]:
    """Edit-path changes that write the mass and its kind; apply with edit.apply_changes."""
    base = f"model/links/{link}"
    return [
        Change(path=f"{base}/mass", value=f"{kg:.6g} kg", source=source, witness=witness),
        Change(path=f"{base}/mass_kind", value=kind, source=source, witness=witness),
    ]
