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
_GLUED = re.compile(r"\d+(?:\.\d+)?(?:kg|mg|g|mv|v|mw|kw|w|ma|a)")
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
    return _resolve(text, parts)[0]


def _stated(words: set[str]) -> set[str]:
    """Part numbers a line states (3+ chars, digits, not "80g"/"6v")."""
    return {t for t in words if len(t) >= 3 and re.search(r"\d", t) and not _GLUED.fullmatch(t)}


def _settle(text: str, tied: list[CatalogPart]) -> tuple[CatalogPart | None, list[CatalogPart]]:
    """(the tied part the line means, the best candidates): an exact name, else the one best overlap."""
    flat, words = _compact(text), _tokens(text)
    exact = [p for p in tied if _compact(p.name) == flat]
    if len(exact) == 1:
        return exact[0], exact
    stated = _stated(words)
    theirs = {p.id: _tokens(p.id.replace(".", " ") + " " + p.name) for p in tied}
    score = {i: len(words & t) for i, t in theirs.items()}
    top = max(score.values())
    best = [p for p in tied if score[p.id] == top]
    ok = [p for p in best if not stated - theirs[p.id]]
    return (ok[0] if len(best) == 1 and ok and top >= 2 else None), best


def _resolve(
    text: str, parts: list[CatalogPart]
) -> tuple[tuple[CatalogPart, float] | None, list[str]]:
    """((part, confidence) or None, ids a key tie could not choose between)."""
    flat = _compact(text)
    # a written id that is only the front of a longer key ("Jetson Orin Nano" vs the devkit's
    # "jetsonorinnanodevkit") also hits, so the longer key (the plain device) outranks a kit part
    hits = [
        (len(k), p, k)
        for p in parts
        for k in _keys(p)
        if k in flat or (len(flat) >= 6 and k.startswith(flat))
    ]
    if hits:
        top = max(n for n, _, _ in hits)
        tied = list({p.id: p for n, p, _ in hits if n == top}.values())
        if len(tied) > 1:  # a tie is never settled by catalog order
            words = _tokens(text)
            # 1. the line names a part's family ("connector", "servo", "adapter")
            fam = [p for p in tied if words & (set(re.split(r"[.-]", p.family)) - {"fam"})]
            tied = fam or tied
            # 2a. a maker the line names that only one candidate carries ("Pololu TB6612FNG")
            makers = words & {w for p in tied for w in _tokens(p.maker or "")}
            owns = {p.id: _tokens(f"{p.id.replace('.', ' ')} {p.name} {p.maker}") for p in tied}
            named = [
                p
                for p in tied
                if sum(bool(makers & o) for o in owns.values()) == 1 and makers & owns[p.id]
            ]
            tied = named or tied
            # 2. the matched key is that part's own id, not just a word in its name
            keys = {k for n, _, k in hits if n == top}
            tied = [p for p in tied if _compact(p.id.rsplit(".", 1)[-1]) in keys] or tied
            if len(tied) != 1:
                one, best = _settle(text, tied)
                if one is None:
                    return None, sorted(p.id for p in best)[:3]
                tied = [one]
        return (tied[0], 0.95), []
    words = _tokens(text)
    # a part number the line states that a candidate lacks rules it out
    stated = _stated(words)
    best: tuple[float, CatalogPart] | None = None
    for part in parts:
        theirs = _tokens(part.id.replace(".", " ") + " " + part.name)
        shared = len(words & theirs)
        if shared < 2 or stated - theirs:
            continue
        score = shared / min(len(words), len(theirs)) - len(theirs) * 0.001
        if best is None or score > best[0]:
            best = (score, part)
    if best is None or best[0] < 0.5:
        return None, []
    return (best[1], round(min(best[0], 1.0) * 0.8, 2)), []


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
        hit, tied = _resolve(rest, parts)
        if hit is None:
            miss = {**row, "status": "NO MATCH", "id": None, "confidence": None, "label": None}
            if tied:  # the cli table has no column for it; api consumers can show it
                miss["note"] = f"could be: {', '.join(tied)}"
            rows.append(miss)
            continue
        part, confidence = hit
        rows.append(
            {
                **row,
                "status": "MATCHED",
                "id": part.id,
                "confidence": confidence,
                "label": part.label,
            }
        )
        mismatches += [{"line": number, "id": part.id, **m} for m in _mismatches(rest, part)]
    return {"lines": rows, "mismatches": mismatches}


def device_fields(part: CatalogPart, device_id: str) -> dict[str, Any]:
    """The `add device` fields a catalog part fills in: the one catalog-to-device mapping."""
    mass = part.numbers.get("mass") or part.numbers.get("frame_mass")
    return {
        "id": device_id,
        "model": f"mdl.{part.id.replace('.', '-')}",
        "family": part.family,
        "name": part.name,
        "physical_mass": f"{mass.value:g} {mass.unit}" if mass else None,
    }


def part_to_add(
    contract_path: Path, part_id: str, rail: str | None = None
) -> tuple[dict[str, Any], str, str | None]:
    """(fields, source, note) for `add device --part`; refuses when the rail cannot be picked."""
    from lockwire.advice import _VOLTAGE_TOLERANCE, input_range, rail_fits, rails_for_voltage
    from lockwire.cli_line import cli_line
    from lockwire.edit import EditRefusal
    from lockwire.schema import load_contract

    part = next((p for p in load_catalog().parts if p.id == part_id), None)
    if part is None:
        raise EditRefusal(f"no catalog part {part_id!r}; lockwire catalog parts lists them")
    try:
        contract = load_contract(contract_path)
    except ValueError as error:
        raise EditRefusal(str(error)) from error
    taken = {d.id for d in contract.devices}
    slug, n = part.id.replace(".", "-"), 1
    while f"dev.{slug}-{n}" in taken:
        n += 1
    fields = device_fields(part, f"dev.{slug}-{n}")
    fields["owner"] = next((d.owner for d in contract.devices), "you")
    nums = part.numbers
    lo, hi, nom = (
        nums.get(k) for k in ("input_voltage_min", "input_voltage_max", "nominal_voltage")
    )
    used = [nums.get("mass") or nums.get("frame_mass"), nums.get("draw_max")]
    if lo and hi:
        fields["input_voltage_min"] = f"{lo.value:g} {lo.unit}"
        fields["input_voltage_max"] = f"{hi.value:g} {hi.unit}"
        used += [lo, hi]
    elif nom:
        used.append(nom)
    note = None
    draw = nums.get("draw_max")
    if draw:
        fields["power_draw"] = f"{draw.value:g} {draw.unit}"
        rails = contract.topology.rails if contract.topology else []
        pct = f"{_VOLTAGE_TOLERANCE * 100:g}"
        names = ", ".join(f"{r.id} ({r.voltage.base():g} V)" for r in rails if r.voltage) or "none"
        band = input_range(
            lo.value if lo else None, hi.value if hi else None, nom.value if nom else None
        )
        want = (
            f"is {nom.value:g} V nominal (lockwire allows ±{pct}%: {band[0]:g} to {band[1]:g} V)"
            if band and nom and not (lo and hi)
            else f"needs {band[0]:g} to {band[1]:g} V"
            if band
            else ""
        )
        if rail:
            fields["power_rail"] = rail
            given = contract.rail(rail)
            if given and given.voltage is None:
                note = f"{rail} has no voltage declared, so its fit to {part_id} was not checked"
            elif given and band and not rail_fits(given, *band):
                fit = [r.id for r in rails_for_voltage(contract, *band)]
                retry = (
                    cli_line(
                        "lockwire",
                        "add",
                        "device",
                        contract_path,
                        "--part",
                        part_id,
                        "--rail",
                        fit[0],
                    )
                    if fit
                    else "start from a robot with a matching rail"
                )
                raise EditRefusal(
                    f"{part_id} {want} but {rail} is "
                    f"{given.voltage.base():g} V; "
                    + (
                        f"rails that fit: {', '.join(fit)}; try: {retry}"
                        if fit
                        else f"no rail here fits; {retry}"
                    )
                )
        elif band is None:
            raise EditRefusal(
                f"{part_id} has no voltage in the catalog, so no rail can be picked; "
                f"pass --rail <one of: {names}>"
            )
        else:
            fit = [r.id for r in rails_for_voltage(contract, *band)]
            if not fit:
                raise EditRefusal(
                    f"{part_id} {want} and no rail here fits "
                    f"(rails: {names}); start from a robot with a matching rail"
                )
            if len(fit) > 1:
                again = cli_line(
                    "lockwire", "add", "device", contract_path, "--part", part_id, "--rail", fit[0]
                )
                raise EditRefusal(
                    f"{part_id} fits {', '.join(fit)}; pass --rail <one>, for example: {again}"
                )
            fields["power_rail"] = fit[0]
            note = f"rail: {fit[0]} (the only rail at {band[0]:g} to {band[1]:g} V)"
    source = "; ".join(dict.fromkeys(u.source for u in used if u))
    if not source:
        raise EditRefusal(
            f"{part_id} has no draw or mass in the catalog, so there is nothing to fill in; "
            "add it with --source and --witness and your own numbers"
        )
    if part.label == "unverified":
        source = f"unverified catalog entry: {source}"
        note = "\n".join(
            filter(
                None,
                [
                    note,
                    f"{part_id} is marked unverified in the catalog; check its numbers against the datasheet.",
                ],
            )
        )
    return fields, source, note


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
        for _ in range(row["quantity"]):
            seen[part.id] = seen.get(part.id, 0) + 1
            fields = {**device_fields(part, f"dev.{slug}-{seen[part.id]}"), "owner": "parts-list"}
            add_entry(
                out,
                "device",
                fields,
                source=f"parts list line {row['line']}, matched to catalog part {part.id}",
                witness="from-parts draft (unreviewed)",
            )
