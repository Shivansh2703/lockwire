"""Part Swap: rank catalog servos for one joint by its real gravity load, list what
the swap does to every joint above it and write an accepted swap as one edit.

Loads are `checks.gravity_loads`, the same worst-pose numbers `check` scores. A candidate's
mass change rides on the joint's child link at its origin (the joint itself), as the same
device the accepted edit writes, so preview and check cannot disagree.
"""

from __future__ import annotations

import datetime as dt
import difflib
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from lockwire import units
from lockwire import catalog as _catalog
from lockwire.catalog import CatalogPart, load_catalog
from lockwire.checks import _ROTARY, gravity_loads, servo_for
from lockwire.edit import (
    EditRefusal,
    EditResult,
    _dump,
    _fdopen_text,
    _flow_quantity,
    _load_doc_guarded,
    _locked_write,
    _replace,
)
from lockwire.schema import (
    Contract,
    DeviceInstance,
    Physical,
    Quantity,
    load_contract,
)

RANK = {"fit": 0, "tight": 1, "too weak": 2}


def status(stall: float, load: float) -> tuple[str, float]:
    """Fit at stall >= 2x load, tight at >= 1x, too weak below; and stall/load."""
    ratio = stall / load if load > 0 else math.inf
    return ("fit" if ratio >= 2 else "tight" if ratio >= 1 else "too weak"), ratio


def shown_ratio(r: float) -> float:
    """stall/load as printed: capped at 100 (ample) and rounded DOWN to 0.1, so 1.97 never
    reads "2.0x" next to "tight"."""
    return math.floor(min(r, 100.0) * 10 + 1e-9) / 10


@dataclass
class Candidate:
    id: str
    label: str
    stall: float
    mass_g: float
    load: float
    status: str
    ratio: float
    flags: list[str]
    name: str = ""
    drops: bool = False  # its knock-on drops some joint above it a status


@dataclass
class KnockOn:
    joint: str
    before: float | None  # None: the gravity walk did not score this joint
    after: float | None
    status: str | None = None  # None: no catalog servo with a stall torque on this joint
    ratio: float | None = None
    was: str | None = None

    @property
    def dropped(self) -> bool:
        return self.was is not None and RANK[self.status] > RANK[self.was]


@dataclass
class Preview:
    joint: str
    current: str | None
    load: float
    candidates: list[Candidate]
    chosen: Candidate | None = None
    knock_on: list[KnockOn] = field(default_factory=list)
    delta_g: float = 0.0
    notes: list[str] = field(default_factory=list)


def _grams(part: CatalogPart | None) -> float | None:
    mass = part.numbers.get("mass") if part else None
    return units.to_base(mass.value, mass.unit) * 1000 if mass else None


def device_id(joint: str) -> str:
    return f"dev.{joint}-servo-swap"


def _with_delta(c: Contract, joint, grams: float) -> Contract:
    """``c`` with this joint's swap device carrying ``grams`` (none at <= 0)."""
    devices = [d for d in c.devices if d.id != device_id(joint.name)]
    if grams > 0:
        mass = Quantity(value=grams, unit="g")
        physical = Physical(mass=mass, mount_frame=joint.child)
        devices.append(
            DeviceInstance(id=device_id(joint.name), model="", owner="", physical=physical)
        )
    return c.model_copy(update={"devices": devices})


def _loads(c: Contract) -> dict[str, float]:
    loads = gravity_loads(c)
    if loads is None:
        raise ValueError("gravity load cannot be computed for this model (see `lockwire check`)")
    return loads


def _knock_on(c: Contract, j, parts: dict, before: dict, after: dict) -> list[KnockOn]:
    """One line per rotary joint above ``j``, load before -> after and its status."""
    rows, by_child = [], {x.child: x for x in c.model.joints}
    up = by_child.get(j.parent)
    while up is not None:
        if up.type in _ROTARY:
            row = KnockOn(up.name, before.get(up.name), after.get(up.name))
            stall = parts.get(servo_for(c.servo, up.name))
            stall = stall.numbers.get("stall_torque") if stall else None
            if stall is not None and None not in (row.before, row.after):
                row.was = status(stall.value, row.before)[0]
                row.status, row.ratio = status(stall.value, row.after)
            rows.append(row)
        up = by_child.get(up.parent)
    return rows


def _declared_supply(c: Contract, joint: str) -> tuple[float | None, str | None]:
    """The joint's supply from the contract: the rail of the device that drives it, else the
    only rail with a voltage; (None, None) when neither is declared."""
    rails = {r.id: r for r in (c.topology.rails if c.topology else []) if r.voltage is not None}
    dev = next((d for d in c.devices if d.drives_joint == joint and d.power), None)
    rail = rails.get(dev.power.rail) if dev else None
    if rail is None and len(rails) == 1:
        rail = next(iter(rails.values()))
    return (rail.voltage.base(), rail.id) if rail else (None, None)


def preview(
    c: Contract, joint: str, part_id: str | None = None, catalog: Path | str | None = None
) -> Preview:
    """Every catalog servo with a stall torque and a mass, ranked for ``joint``, and the
    knock-on above it for ``part_id`` (default: the top candidate). Offline, writes nothing."""
    j = next((x for x in c.model.joints if x.name == joint), None) if c.model else None
    if j is None or j.type not in _ROTARY:
        names = ", ".join(x.name for x in c.model.joints if x.type in _ROTARY) if c.model else ""
        raise ValueError(f"no revolute or continuous joint {joint!r} in the model; joints: {names}")
    parts = {p.id: p for p in load_catalog(catalog).parts}
    # a part from ~/.lockwire/parts.yaml (not in the shipped seed) is private
    public = {p.id for p in load_catalog(_catalog._SEED_PATH).parts} if catalog is None else parts
    current = servo_for(c.servo, joint)
    old = parts.get(current)
    old_g = _grams(old)
    swap_dev = next((d for d in c.devices if d.id == device_id(joint)), None)
    placed = swap_dev.physical.mass.base() * 1000 if swap_dev and swap_dev.physical else 0.0
    before = _loads(c)
    if joint not in before:
        raise ValueError(f"{joint}'s gravity load is not scored (see `lockwire check`)")
    pv = Preview(joint, current, before[joint], [])
    runs: dict[str, dict[str, float]] = {}
    higher: set[str] = set()
    volts: set[str] = set()  # voltage range does not cover the supply
    bus: set[str] = set()
    top = old.numbers.get("input_voltage_max") if old else None
    sup, rail_id = _declared_supply(c, joint)
    if sup is None and top is not None:
        sup = top.value
    for p in parts.values():
        stall, grams = p.numbers.get("stall_torque"), _grams(p)
        if p.id == current or stall is None or grams is None:
            continue
        delta = grams - (old_g - placed) if old_g is not None else grams
        runs[p.id] = loads = _loads(_with_delta(c, j, delta))
        flags = []
        if old and old.bus and p.bus and old.bus != p.bus:
            flags.append("different bus")
            bus.add(p.id)
        plo, phi = (p.numbers.get(k) for k in ("input_voltage_min", "input_voltage_max"))
        # the joint's supply is at most the current servo's max input voltage: a stall figure
        # rated above that is not what this joint gets, so it ranks after the same-supply parts
        # of its own status
        rated = p.numbers.get("stall_torque_voltage")
        if sup and phi and phi.value < sup:
            volts.add(p.id)
            flags.append(f"max {phi.value:g} V, below your supply's {sup:g} V (needs new supply)")
        elif sup and plo and plo.value > sup:
            volts.add(p.id)
            flags.append(f"needs at least {plo.value:g} V, above your supply's {sup:g} V")
        elif sup and rated and rated.value > sup:
            higher.add(p.id)
            flags.append(
                f"rated at {rated.value:g} V; your supply is {'' if rail_id else 'at most '}"
                f"{sup:g} V, expect less (needs new supply)"
            )
        if p.id not in public:
            flags.append("private")
        st, ratio = status(stall.value, loads.get(joint, 0.0))
        load = loads.get(joint, 0.0)
        drops = any(k.dropped for k in _knock_on(c, j, parts, before, loads))
        cand = Candidate(p.id, p.label, stall.value, grams, load, st, ratio, flags, p.name, drops)
        pv.candidates.append(cand)
    # strong enough, then a voltage range that covers the supply (over-volting can damage a
    # servo), then the same bus, then fit before tight, then same supply, then a candidate that
    # tips no joint above it, then cited, then lightest: the top row needs the fewest changes
    pv.candidates.sort(
        key=lambda x: (
            x.status == "too weak",
            x.id in volts,
            x.id in bus,
            RANK[x.status],
            x.id in higher,
            x.drops,
            x.label != "cited",
            x.mass_g,
        )
    )
    if part_id is not None and part_id not in runs:
        raise ValueError(f"{part_id!r} is not a catalog servo with a stall torque and a mass")
    pv.chosen = next((x for x in pv.candidates if x.id == part_id), None) or (
        pv.candidates[0] if pv.candidates else None
    )
    if pv.chosen is None:
        return pv
    after = runs[pv.chosen.id]
    if rail_id:
        pv.notes.append(f"supply {sup:g} V, as declared for its power rail")
    elif sup is not None:
        pv.notes.append(
            f"supply taken as at most {sup:g} V, {current}'s max input voltage"
            " (declare a rail voltage to use the real one)"
        )
    else:
        who = "no current servo" if old is None else f"{current} has no input voltage"
        pv.notes.append(
            f"{who}, so supply voltage is unknown — stall figures are at each servo's rated voltage"
        )
    if sup and all(x.id in volts for x in pv.candidates):
        pv.notes.append(
            f"none of these servos runs on your {sup:g} V supply; each needs a new supply"
        )
    pv.delta_g = pv.chosen.mass_g - (old_g - placed) if old_g is not None else pv.chosen.mass_g
    if old_g is None:
        pv.notes.append(
            f"the current servo's mass is unknown, so the swap adds the full {pv.chosen.mass_g:g} g"
        )
    elif pv.delta_g < 0:
        pv.notes.append(
            "the new servo is lighter; no mass is removed, so loads are slightly overstated"
        )
    pv.knock_on = _knock_on(c, j, parts, before, after)
    return pv


@_locked_write
def apply_swap(
    contract_path: Path,
    joint: str,
    part_id: str,
    *,
    catalog: Path | str | None = None,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> tuple[Preview, EditResult]:
    """Write the swap as one edit: the joint's exact `servo:` entry and one device on the child
    link with the new-minus-old servo mass (replacing an earlier swap's device for this joint;
    none when the new servo is lighter). The version line is never touched (an old file moves up only through `lockwire upgrade`)."""
    pv = preview(load_contract(contract_path), joint, part_id, catalog)
    original_text, doc, style = _load_doc_guarded(contract_path)
    contract = Contract.model_validate(doc)
    j = next(x for x in contract.model.joints if x.name == joint)
    servo = CommentedMap([(joint, part_id)])
    old = doc.get("servo")
    if isinstance(old, dict):
        servo.update((k, v) for k, v in old.items() if k != joint)
    elif old:
        servo["*"] = old
    doc["servo"] = servo
    devices = doc.get("devices") or []
    at = next((i for i, d in enumerate(devices) if d.get("id") == device_id(joint)), None)
    if at is not None:
        del devices[at]
    if pv.delta_g > 0:
        # the model of the device driving this joint only when that device IS the servo being
        # replaced (its model id ends in the catalog id's name, e.g. mdl.servo.sts3215 for
        # feetech.sts3215); else the generic mass-only model: never an unrelated part's model
        host = next(
            (d for d in contract.devices if d.id != device_id(joint) and d.drives_joint == joint),
            None,
        )
        same = host and pv.current and host.model.rsplit(".", 1)[-1] == pv.current.rsplit(".")[-1]
        model = host.model if same else "mdl.part-swap.servo-mass"
        if not same and model not in {m.id for m in contract.device_models}:
            fam = "fam.part-swap.servo-mass"
            doc["families"] = doc.get("families") or []
            if fam not in {f.id for f in contract.families}:
                doc["families"].append(CommentedMap([("id", fam), ("name", "Servo mass")]))
            doc["device_models"] = doc.get("device_models") or []
            doc["device_models"].append(CommentedMap([("id", model), ("family", fam)]))
            pv.notes.append(
                "added a mass-only part so the new servo's weight is counted (no limits or signals)"
            )
        parts = {p.id: p for p in load_catalog(catalog).parts}
        # the difference when the old servo's mass is known, else the full new mass
        what = "mass difference" if _grams(parts.get(pv.current)) is not None else "full mass"
        pv.notes.append(f"the {j.child} link now carries the servo's {what}")
        owner = host.owner if host else next((d.owner for d in contract.devices), "you")
        sources = [
            p.numbers["mass"].source
            for p in (parts[part_id], parts.get(pv.current))
            if p and "mass" in p.numbers
        ]
        entry = CommentedMap()
        entry["id"] = device_id(joint)
        entry["model"] = model
        entry["physical"] = CommentedMap(
            [("mass", _flow_quantity(round(pv.delta_g, 3), "g")), ("mount_frame", j.child)]
        )
        entry["owner"] = owner
        entry["provenance"] = CommentedMap(
            [
                ("witness", "w0"),
                ("source", "; ".join(sources)),
                ("locked_at", dt.date.today()),
                (
                    "note",
                    f"Part Swap: {part_id} replaces {pv.current or 'an unknown servo'} on "
                    f"{joint}; this device is the servo {what}, not a part.",
                ),
            ]
        )
        devices.insert(len(devices) if at is None else at, entry)
        if doc.get("devices") is None:
            doc["devices"] = devices
    new_text = _dump(doc, style)
    try:
        Contract.model_validate(doc)
    except Exception as error:  # noqa: BLE001 — refuse, never write an invalid file
        raise EditRefusal(f"swap of {joint}: {error}") from error
    diff = "".join(
        difflib.unified_diff(
            original_text.replace("\r\n", "\n").splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(contract_path),
            tofile=str(contract_path),
        )
    )
    if "private" in pv.chosen.flags:
        pv.notes.append(
            f"{pv.chosen.name or part_id} is a private part: teammates without it in their parts file will see "
            "'no catalog servo'"
        )
    result = EditResult(
        diff=diff, lines=[f"swapped {joint} to {pv.chosen.name or part_id}", *pv.notes]
    )
    if dry_run:
        return pv, result
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{contract_path.name}.", suffix=".tmp", dir=str(contract_path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with _fdopen_text(fd, original_text) as handle:
            handle.write(new_text)
        _replace(tmp_path, contract_path)
        result.written = True
        return pv, result
    finally:
        tmp_path.unlink(missing_ok=True)
