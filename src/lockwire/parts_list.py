"""A pasted parts list matched to the catalog, and the draft lockfile it implies.

One line per part, free text. Matching is stdlib only: a part-number substring first, then
normalised-token overlap. The draft lockfile goes through ``start_blank`` + ``add_entry``,
the same door ``lockwire start`` / ``lockwire add`` use, never a second lockfile builder.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lockwire.catalog import CatalogPart, load_catalog
from lockwire.units import UnitError, convert, dimension

_QTY = (
    re.compile(r"^\s*(\d+)\s*[x×]\s*"),  # 12x ...
    re.compile(r"^\s*[x×]\s*(\d+)\s+"),  # x12 ...
    re.compile(r"\s*\((\d+)\)\s*"),  # ... (12)
    re.compile(r"\s+[x×]\s*(\d+)\s*$"),  # ... x12
)
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(kg|mg|g|mV|V|mW|kW|W|mA|A)\b")
_STOP = {"the", "and", "for", "with", "placeholder", "generic", "module", "kit"}
_TOLERANCE = 0.05  # a written number within 5% of the catalog's counts as agreeing


def _tokens(text: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if len(t) > 1} - _STOP


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _quantity(line: str) -> tuple[int, str]:
    for pattern in _QTY:
        found = pattern.search(line)
        if found and int(found.group(1)) <= 64:  # bigger is a model number or capacity
            return int(found.group(1)), (line[: found.start()] + " " + line[found.end() :]).strip()
    return 1, line.strip()


def _keys(part: CatalogPart) -> list[str]:
    """Part-number-like keys: the id's last segment and every digit-bearing name token."""
    keys = [_compact(part.id.rsplit(".", 1)[-1])]
    keys += [_compact(t) for t in part.name.split() if len(t) >= 4 and re.search(r"\d", t)]
    return [k for k in keys if len(k) >= 3]


def _match(text: str, parts: list[CatalogPart]) -> tuple[CatalogPart, float] | None:
    flat = _compact(text)
    # a written id that is only the front of a longer key ("Jetson Orin Nano" vs the devkit's
    # "jetsonorinnanodevkit") also hits, so the longer key (the plain device) outranks a kit part
    hits = [
        (len(k), p)
        for p in parts
        for k in _keys(p)
        if k in flat or (len(flat) >= 6 and k.startswith(flat))
    ]
    if hits:
        return max(hits, key=lambda h: h[0])[1], 0.95
    words = _tokens(text)
    best: tuple[float, CatalogPart] | None = None
    for part in parts:
        theirs = _tokens(part.id.replace(".", " ") + " " + part.name)
        shared = len(words & theirs)
        if shared < 2:
            continue
        score = shared / min(len(words), len(theirs)) - len(theirs) * 0.001
        if best is None or score > best[0]:
            best = (score, part)
    if best is None or best[0] < 0.5:
        return None
    return best[1], round(min(best[0], 1.0) * 0.8, 2)


def _mismatches(text: str, part: CatalogPart) -> list[dict[str, Any]]:
    """Numbers with units written in the line that disagree with the catalog (mass, voltage)."""
    found = []
    for raw, unit in _NUMBER.findall(text):
        value, written = float(raw), f"{raw} {unit}"
        try:
            kind = dimension(unit)
        except UnitError:
            continue
        if kind == "mass" and "mass" in part.numbers:
            ref = part.numbers["mass"]
            want = convert(ref.value, ref.unit, unit)
            ok, said = abs(value - want) <= _TOLERANCE * want, f"{ref.value:g} {ref.unit}"
            field = "mass"
        elif kind == "voltage":
            lo, hi, nom = (
                part.numbers.get(k)
                for k in ("input_voltage_min", "input_voltage_max", "nominal_voltage")
            )
            volts = convert(value, unit, "V")
            if lo and hi:
                ok, said = (
                    lo.value * (1 - _TOLERANCE) <= volts <= hi.value * (1 + _TOLERANCE),
                    f"{lo.value:g} to {hi.value:g} V",
                )
            elif nom:
                ok, said = abs(volts - nom.value) <= _TOLERANCE * nom.value, f"{nom.value:g} V"
            else:
                continue
            field = "voltage"
        else:
            continue
        if not ok:
            found.append({"field": field, "written": written, "catalog": said})
    return found


def match_parts(text: str, catalog: Path | str | None = None) -> dict[str, Any]:
    """Match each non-blank, non-# line of ``text`` to the catalog."""
    parts = load_catalog(catalog).parts
    rows, mismatches = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        quantity, rest = _quantity(line)
        row: dict[str, Any] = {"line": number, "text": line.strip(), "quantity": quantity}
        hit = _match(rest, parts)
        if hit is None:
            rows.append({**row, "status": "NO MATCH", "id": None, "confidence": None})
            continue
        part, confidence = hit
        rows.append({**row, "status": "MATCHED", "id": part.id, "confidence": confidence})
        mismatches += [{"line": number, "id": part.id, **m} for m in _mismatches(rest, part)]
    return {"lines": rows, "mismatches": mismatches}


def write_draft(document: dict[str, Any], out: Path, catalog: Path | str | None = None) -> None:
    """Write the matched parts into a new lockfile at ``out`` through start_blank + add_entry."""
    from lockwire.edit import add_entry
    from lockwire.start_operation import start_blank

    by_id = {p.id: p for p in load_catalog(catalog).parts}
    start_blank(out)
    seen: dict[str, int] = {}
    for row in document["lines"]:
        if row["status"] != "MATCHED":
            continue
        part = by_id[row["id"]]
        slug = part.id.replace(".", "-")
        mass = part.numbers.get("mass") or part.numbers.get("frame_mass")
        for _ in range(row["quantity"]):
            seen[part.id] = seen.get(part.id, 0) + 1
            fields = {
                "id": f"dev.{slug}-{seen[part.id]}",
                "model": f"mdl.{slug}",
                "family": part.family,
                "name": part.name,
                "owner": "parts-list",
                "physical_mass": f"{mass.value:g} {mass.unit}" if mass else None,
            }
            add_entry(
                out,
                "device",
                fields,
                source=f"parts list line {row['line']}, matched to catalog part {part.id}",
                witness="from-parts draft (unreviewed)",
            )
