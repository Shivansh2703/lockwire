"""Searched fixes: for one finding, try candidate changes in memory and keep only the ones the
check itself proves: after the change that finding is gone, nothing new appears and no finding
that was already there has a number further past its bound.

Three kinds are searched. A joint whose effort is over its servo's stall torque: stronger catalog
servos, in Part Swap's own order. A fuse above its wire's ampacity or a wire that drops too much:
thicker gauges, and a fuse at the wire's ampacity. A brown-out, a pack over its C-rate or an
overloaded rail: thicker wires on that rail, catalog regulators and catalog packs with a higher
rating (pack sag has none yet: no catalog pack states an internal resistance). Every number comes from the existing checks and the cited catalog; nothing is written.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lockwire import checks, swap, units
from lockwire.catalog import CatalogPart, is_cited, load_catalog
from lockwire.edit import Change, EditRefusal, propose_changes
from lockwire.report import verdict, verdict_word
from lockwire.schema import Contract, load_contract
from lockwire.whatif import _assert_unchanged, _bytes, _check

NO_KIND = "no searched fix for this kind yet"
#: Stands in for the person who signs the real edit (the app's own dry-run stand-in).
WITNESS = "your name"
#: One call's wall-clock budget, from entry: past it the search returns what it has proved so far.
BUDGET_S = 9.0
_OPEN = ("violation", "warning")
_BAD = (*_OPEN, "not_scored")


@dataclass
class _Candidate:
    label: str
    size: float  # how far the changed number moves from today's; smallest first
    scope: str  # the lock entry it changes: no violation or warning may stay under this path
    changes: list[dict[str, str]] = field(default_factory=list)  # what-if path, value, source
    swap: dict[str, str] | None = None  # Part Swap: joint and part_id
    pv: Any = None  # the Part Swap preview for this part
    part: CatalogPart | None = None
    cite: tuple[str, ...] = ()  # the part's numbers this fix rests on
    notes: list[str] = field(default_factory=list)


def _servos(c: Contract, path: Path, m: re.Match) -> list[_Candidate]:
    """Part Swap's ranked servos for the joint, less any that cannot be shown to work here: too
    weak for the gravity load, tips a joint above it, or not rated for the declared supply."""
    joint = m["joint"]
    try:
        pv = swap.preview(c, joint, contract_path=path)
    except ValueError:  # the gravity load is not scored for this joint, so nothing is ranked
        return []
    parts, old = {p.id: p for p in _on_sale()}, {p.id: p for p in load_catalog().parts}.get(pv.current)
    out = []
    for rank, x in enumerate(pv.candidates):
        # ponytail: Part Swap marks every supply mismatch with the word "supply" in its flag
        if x.id not in parts or x.status == "too weak" or x.drops or any("supply" in f for f in x.flags):
            continue
        notes = [f"Part Swap: {x.status} for the {x.load:.2f} N*m gravity load", *x.flags]
        if "different bus" in notes:  # Part Swap's two words, said so a person sees what else to buy
            bus = parts[x.id].bus
            notes[notes.index("different bus")] = (
                f"different bus: {old.id} speaks {old.bus} and {x.id} speaks {bus}, so an adapter or "
                f"controller for {bus} is needed"
            )
        notes += [
            f"the declared draw of {d.id} is still the old servo's ({d.power.draw}); update "
            f"devices/{d.id}/power/draw to what {x.id} draws"
            for d in c.devices
            if d.drives_joint == joint and d.power is not None and d.power.draw is not None
        ]  # Part Swap changes the servo entry and the weight, never a declared draw
        # the mass a swap adds is the part's mass less one offset, the same for every part
        chosen = replace(pv, chosen=x, delta_g=x.mass_g + pv.delta_g - pv.chosen.mass_g, notes=[])
        out.append(
            _Candidate(
                f"{joint}: swap {pv.current or 'the servo'} for {x.id} ({x.name})",
                rank,
                f"model/joints/{joint}/",
                swap={"joint": joint, "part_id": x.id},
                pv=chosen,
                part=parts[x.id],
                cite=("stall_torque", "stall_torque_voltage", "input_voltage_min", "input_voltage_max", "mass"),
                notes=notes,
            )
        )  # fmt: skip
    return out


def _gauges(c: Contract, wire: Any) -> list[_Candidate]:
    """Thicker gauges for one wire, nearest first. Even sizes only: the table also lists odd
    gauges, which are rarely sold."""
    table = load_catalog().wire_table
    rows = {r.awg: r for r in table.rows} if table else {}
    if wire.awg not in rows:
        return []
    now = rows[wire.awg].ohm_per_m
    return [
        _Candidate(
            f"wire {wire.id}: {wire.awg} AWG to {awg} AWG",
            (now - rows[awg].ohm_per_m) / now,
            f"topology/wires/{wire.id}/",
            [
                {
                    "path": f"topology/wires/{wire.id}/awg",
                    "value": str(awg),
                    "source": f"design choice: {awg} AWG, thicker than the {wire.awg} AWG it replaces",
                }
            ],
        )
        for awg in sorted((a for a in rows if a < wire.awg and a % 2 == 0), reverse=True)
    ]


def _wire(c: Contract, path: Path, m: re.Match) -> list[_Candidate]:
    wire = next(w for w in c.topology.wires if w.id == m["wire"])
    out = _gauges(c, wire)
    rail = c.rail(wire.rail) if wire.rail else None
    limits = checks._wire_limits(wire)
    fuse = rail.current_limit.base() if rail is not None and rail.current_limit is not None else 0
    if m["what"] == "fuse" and limits and fuse > limits[2]:
        _, _, amps, source = limits
        out.append(
            _Candidate(
                f"fuse on {rail.id}: {fuse:g} A to {amps:g} A, what {wire.awg} AWG wire {wire.id} can carry",
                (fuse - amps) / fuse,
                f"topology/rails/{rail.id}/",
                [
                    {
                        "path": f"topology/rails/{rail.id}/current_limit",
                        "value": f"{amps:g} A",
                        "source": f"design choice: a fuse at the wire's ampacity ({source})",
                    }
                ],
            )
        )
    return out


def _on_sale() -> list[CatalogPart]:
    """Catalog parts, less the ones the catalog marks discontinued."""
    catalog = load_catalog()
    gone = {a.part for a in catalog.availability if a.status == "discontinued"}
    return [p for p in catalog.parts if p.id not in gone]


def _number(part: CatalogPart, key: str) -> float | None:
    n = part.numbers.get(key)
    return None if n is None else n.value / 100 if n.unit == "%" else n.value


def _regulators(c: Contract, rail: Any, budget: Any = None) -> list[_Candidate]:
    """Catalog regulators with this rail's output voltage, a higher output power and an input
    range that holds what feeds the rail. A regulator fact the lock declares must come from the
    new part too, or the part is skipped: a number of the old part never stays standing."""
    if rail.voltage is None or rail.dropout_v or rail.output_tolerance_pct or rail.thermal_resistance_k_per_w:
        return []  # fmt: skip
    volts, old = rail.voltage.base(), rail.capacity.base()
    up = c.topology.battery if rail.upstream == "battery" else c.rail(rail.upstream) if rail.upstream else None
    feed = up.voltage.base() if up is not None and up.voltage is not None else None  # fmt: skip
    base = f"topology/rails/{rail.id}"
    out = []
    for p in _on_sale():
        amps, vmin, vmax = (_number(p, k) for k in ("output_current_max", "input_voltage_min", "input_voltage_max"))  # fmt: skip
        eff = _number(p, "efficiency_min")
        if not p.family.startswith("fam.power.") or _number(p, "output_voltage") != volts or not amps:
            continue  # fmt: skip
        if amps * volts <= old or (feed is not None and not (vmin or 0) <= feed <= (vmax or feed)):
            continue
        if (rail.min_input_v and vmin is None) or (rail.efficiency != 1.0 and eff is None):
            continue
        cited = p.numbers["output_current_max"].source
        watts = f"{amps * volts:g} W"
        changes = [{"path": f"{base}/capacity", "value": watts, "source": cited}]
        if rail.min_input_v:
            changes.append({"path": f"{base}/min_input_v", "value": f"{vmin:g}", "source": cited})
        if rail.efficiency != 1.0:
            changes.append({"path": f"{base}/efficiency", "value": f"{eff:g}", "source": cited})
        if budget is not None and budget.limit.base() == old:  # the limit is the regulator's rating
            changes.append({"path": f"budgets/{budget.id}/limit", "value": watts, "source": cited})
        out.append(
            _Candidate(
                f"regulator for {rail.id}: {p.id} ({p.name}), {old:g} W to {watts}",
                (amps * volts - old) / old,
                f"{base}/",
                changes,
                part=p,
                cite=("output_voltage", "output_current_max", "input_voltage_min", "input_voltage_max", "efficiency_min"),
            )
        )  # fmt: skip
    return out


def _brownout(c: Contract, path: Path, m: re.Match) -> list[_Candidate]:
    rail = c.rail(m["rail"])
    wires = [w for w in c.topology.wires if w.rail == rail.id and w.length_m]
    return [x for w in wires for x in _gauges(c, w)] + _regulators(c, rail)


def _budget(c: Contract, path: Path, m: re.Match) -> list[_Candidate] | None:
    budget = next((b for b in c.budgets if b.id == m["budget"]), None)
    rail = c.rail(budget.rail) if budget is not None and budget.kind == "rail_power" and c.topology else None
    return None if rail is None else _regulators(c, rail, budget)  # fmt: skip


def _packs(c: Contract, path: Path, m: re.Match) -> list[_Candidate]:
    """Catalog packs with the same cell count, no less capacity and more current. Every pack fact the lock
    declares (C-rates, mass, internal resistance) must come from the new pack, or it is skipped;
    no catalog pack states an internal resistance today, so pack sag has no searched fix yet."""
    found = checks._pack(c)
    if found is None or found[2] is None or found[0].cells_series is None:
        return []
    battery, _, nominal = found
    rate = m["field"]
    declared = [k for k in ("c_rate_continuous", "c_rate_burst", "mass", "internal_resistance_mohm") if getattr(battery, k) is not None]  # fmt: skip
    if rate not in declared:
        return []
    old = battery.capacity.base() / nominal * getattr(battery, rate)
    # the pack as a device: the one that makes a rail fed by nothing but the battery, and draws
    # from no rail itself (a regulator on the battery draws from it)
    makers = {r.source_device for r in c.topology.rails if r.upstream in (None, "battery")}
    pack = [d for d in c.devices if d.id in makers and (d.power is None or d.power.rail is None)]
    weighed = pack[0] if len(pack) == 1 and pack[0].physical and pack[0].physical.mass else None
    if weighed is None:
        why = f"{len(pack)} devices ({', '.join(d.id for d in pack)}) could be the pack" if len(pack) > 1 else "no device in the lock is the pack with a mass"
        weight = [f"the pack's weight is not on the robot model ({why}), so the weight checks do not see the new pack"]  # fmt: skip
    out = []
    for p in _on_sale():
        n = p.numbers
        if not p.family.startswith("fam.battery.pack") or any(k not in n for k in ("cells_series", "capacity_mah", *declared)):
            continue  # fmt: skip
        amp_hours = n["capacity_mah"].value / 1000
        amps = amp_hours * n[rate].value
        smaller = amp_hours * nominal < battery.capacity.base() * (1 - 1e-9)
        if n["cells_series"].value != battery.cells_series or amps <= old or smaller:
            continue
        changes = [
            {
                "path": "topology/battery/capacity",
                "value": f"{amp_hours * nominal:g} Wh",
                "source": n["capacity_mah"].source,
            }
        ]
        for k in declared:
            value = f"{n[k].value:g} {n[k].unit}" if k == "mass" else f"{n[k].value:g}"
            changes.append({"path": f"topology/battery/{k}", "value": value, "source": n[k].source})
        if weighed is not None:  # the device carries the new pack's catalog mass, so gravity and standing see it
            if "mass" not in n:
                continue
            mass = f"{n['mass'].value:g} {n['mass'].unit}"
            changes.append({"path": f"devices/{weighed.id}/physical/mass", "value": mass, "source": n["mass"].source})
        out.append(
            _Candidate(
                f"pack: {p.id} ({p.name}), {old:.3g} A to {amps:.3g} A",
                (amps - old) / old,
                "topology/battery/",
                changes,
                part=p,
                cite=("cells_series", "capacity_mah", "c_rate_continuous", "c_rate_burst", "mass"),
                notes=["a rail capacity worked out from the old pack is left as it is", *([] if weighed else weight)],
            )
        )
    return out


#: quantity pattern, kind, the one check that emits the finding (the cheap first look), candidates
_KINDS = (
    (r"model/joints/(?P<joint>[^/]+)/limits/effort", "joint too weak", lambda c: checks.check_servo_stall(c, c.servo), _servos),
    (r"topology/wires/(?P<wire>[^/]+)/(?P<what>fuse)", "wire and fuse", checks.check_fuse_wire, _wire),
    (r"topology/wires/(?P<wire>[^/]+)/(?P<what>drop)", "wire and fuse", checks.check_wire_drop, _wire),
    (r"topology/rails/(?P<rail>[^/]+)/brownout", "power", checks.check_servo_brownout, _brownout),
    (r"topology/battery/(?P<field>c_rate_burst|c_rate_continuous)", "power", checks.check_battery_c_rate, _packs),
    (r"budgets/(?P<budget>[^/]+)", "power", checks.check_budgets, _budget),
)  # fmt: skip


def _proposed(path: Path, cand: _Candidate) -> Contract:
    """The contract after this candidate, built by the door that would write it."""
    if cand.swap:
        return swap.swap_text(path, cand.swap["joint"], cand.swap["part_id"], pv=cand.pv)[4]
    changes = [Change(x["path"], x["value"], x["source"], WITNESS) for x in cand.changes]
    return propose_changes(path, changes).contract


def _unverified(part: CatalogPart | None) -> str | None:
    """The catalog's "unverified" label in plain words, naming the figures behind it."""
    if part is None or part.label == "cited":
        return None
    names = [k.replace("_", " ") for k, n in part.numbers.items() if not is_cited(n.kind, n.source)]
    return (
        f"this part's datasheet figures are not fully verified: {', '.join(names)} "
        f"{'is' if len(names) == 1 else 'are'} not from a maker's datasheet page"
    )


@dataclass
class _Row:
    """One number the check holds unrounded: ``value`` against ``bound``; ``gap`` is how far it is
    past the bound (negative inside). ``head`` and ``says`` pick the finding it belongs to when
    two share a quantity (the start of its observed text, a word of its expected text)."""

    quantity: str
    passed: bool
    gap: float
    value: float
    bound: float
    unit: str
    label: str
    head: str = ""
    says: str = ""


def _rows(report: Any, c: Contract) -> list[_Row]:
    """Every unrounded number the check holds for its findings and passed rows: the typed
    pairs of passed rows, the gravity rows, the standing rows and balance margin, the tip
    distance, each servo's stall torque against its joint's effort, and each servo rail's
    brown-out point. A typed finding carries its own pair (`_gap`)."""
    out = [
        _Row(r.quantity, True, -abs(o.base() - e.base()), units.convert(o.value, o.unit, e.unit), e.value, e.unit, r.quantity)  # fmt: skip
        for r in report.scored_records
        if (o := r.observed_quantity) is not None and (e := r.expected_quantity) is not None and o.dimension() == e.dimension()  # fmt: skip
    ]
    rules = c.servo if isinstance(c.servo, dict) else {"*": c.servo} if c.servo else {}
    for j in c.model.joints if c.model and rules else ():
        effort, sid = j.limits.effort if j.limits else None, checks.servo_for(rules, j.name)
        try:
            stall = checks.servo_stall_torque(sid).value if sid and effort and effort > 0 else None
        except ValueError:
            stall = None
        if stall is not None:
            out.append(_Row(f"model/joints/{j.name}/limits/effort", effort <= stall, effort - stall, effort, stall, "N*m", f"{j.name} effort against the {sid} stall torque", f"{effort:.2f} N*m joint effort limit", "stall torque"))  # fmt: skip
    for t in report.joint_torques:
        q = f"model/joints/{t['joint']}/limits/effort"
        out.append(_Row(q, t["passed"], t["peak"] - t["limit"], t["peak"], t["limit"], t["unit"], f"{t['joint']} gravity load", f"{t['peak']:.2f} "))  # fmt: skip
    stance = (report.standing or {}).get("stance") or {}
    for x in (report.standing or {}).get("rows", []):
        at, peak, limit = x["at_file_nm"], x["peak_nm"], x["limit_nm"]
        still = x["case"] == "all_feet" and at is not None and at > limit
        value, head = (at, f"{x['joint']} carries ") if still else (peak, f"{x['joint']} would carry ")
        out.append(_Row(f"model/joints/{x['joint']}/standing/{x['case']}", not still and peak <= limit, value - limit, value, limit, "N*m", f"{x['joint']} standing load ({x['case']})", head))  # fmt: skip
    if stance.get("balance_margin_m") is not None:
        m = stance["balance_margin_m"]
        out.append(_Row("model/standing/tips", m >= 0, -m, m, 0.0, "m", "centre of mass to the support edge"))
    if (d := checks.tip_distance_m(c)) is not None:
        out.append(_Row("model/tip_over" if d < 0 else "model/tip_margin", False, -d, d, 0.0, "m", "centre of mass to the tip edge"))  # fmt: skip
    for rail in c.topology.rails if c.topology else ():
        floors = [m.input_voltage_range.min.base() for d in c.devices if d.power is not None and d.power.rail == rail.id and (m := c.device_model(d.model)) is not None and m.input_voltage_range is not None]  # fmt: skip
        points = checks.rail_sweep(c, rail.id, (1.0,)) if floors else None
        if points:
            v = points[0][1]
            out.append(_Row(f"topology/rails/{rail.id}/brownout", v >= max(floors), max(floors) - v, v, max(floors), "V", f"{rail.id} brown-out point"))  # fmt: skip
    return out


def _gap(f: Any, rows: list[_Row]) -> _Row | None:
    """The unrounded number behind a finding: its own typed pair (a finding is past its bound),
    else the check's row for it; None when the engine holds no number for it."""
    o, e = f.observed_quantity, f.expected_quantity
    if o is not None and e is not None and o.dimension() == e.dimension():
        return _Row(f.quantity, False, abs(o.base() - e.base()), units.convert(o.value, o.unit, e.unit), e.value, e.unit, f.quantity)  # fmt: skip
    return next((r for r in rows if not r.passed and r.quantity == f.quantity and f.observed.startswith(r.head) and r.says in f.expected), None)  # fmt: skip


def _num(r: _Row) -> str:
    return f"{r.value:.7g} {r.unit}"


#: a number with a unit in a finding's text: a measured value whose print can round a change away
_MEASURED = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\s?(?:N\*m|Nm|kW|mW|W|mV|V|mAh|Ah|mA|A|mWh|kWh|Wh|kg|mg|g|mm|cm|m|rad/s|rpm|deg|%|C|kHz|Hz|ms|s|ohm|N)(?![\w*/^])")  # fmt: skip


def _depends(quantity: str) -> set[str]:
    """What a finding can depend on, judged from where it sits in the lock, erring wide: the
    model's findings read masses (and a servo's voltage), the power tree's read voltage and
    current, anything else may read any of the three. Three read none of them: a link's own
    sanity lines (its mass, inertia and shapes; a swap or a pack adds a device, never a link), a
    wire's pin lines (a pin card's levels and currents) and a joint's range against its servo's
    datasheet travel (a swap on that joint changes the servo named in the text)."""
    if quantity.startswith("model/links/") or re.fullmatch(r"topology/wires/[^/]+|model/joints/[^/]+/limits/datasheet_range", quantity):
        return set()
    if quantity.startswith(("model/", "plan/")):
        return {"mass", "voltage"}
    if quantity.startswith(("topology/", "budgets/")):
        return {"voltage", "current"}
    return {"mass", "voltage", "current"}


def _moves(cand: _Candidate) -> set[str]:
    """What a candidate changes: a swap moves weight and the servo's current and voltage; a pack
    the same; a fuse only the current a rail may carry; a wire or a regulator a rail's voltage
    and current, and weight when a step sets a mass."""
    if cand.swap or cand.scope == "topology/battery/":
        return {"mass", "voltage", "current"}
    paths = [x["path"] for x in cand.changes]
    moved = {"current"} if all(p.endswith("/current_limit") for p in paths) else {"voltage", "current"}
    return moved | ({"mass"} if any(p.endswith("/mass") for p in paths) else set())


def _worse(before: Any, after: Any, rows_was: list[_Row], rows_now: list[_Row], moves: set[str] | None = None) -> str | None:
    """The one comparator, for every kind: why the check ``after`` is worse than ``before``, in
    plain words, or None. Worse is a violation, warning or not-scored line that is new, or one
    that was there whose unrounded number (`_rows`) is now further past its bound. A finding the
    engine holds no number for may be worse when its text changed; a violation or warning that
    prints a measured number it cannot compare may be worse when the candidate ``moves`` what it
    depends on (`_depends`), even with its text unchanged: a print can round a change away."""

    def text(f: Any) -> tuple:
        return f.severity, f.quantity, f.observed, f.expected

    def words(f: Any) -> tuple:  # one check's finding keeps its words when its numbers move
        return f.severity, f.quantity, re.sub(r"\d+(\.\d+)?", "#", f.expected)

    def short(f: Any) -> str:  # the finding's number without its bracketed detail
        head = f.observed.split(" (")[0]
        return head if re.search(r"\d", head) else f.observed

    old, pairs = [f for f in before if f.severity in _BAD], []
    for same in (text, words):  # unchanged findings pair off first, then the ones whose numbers moved
        for f in [f for f in after if f.severity in _BAD and all(f is not x for _, x in pairs)]:
            was = next((x for x in old if same(x) == same(f)), None)
            if was is not None:
                old.remove(was)
                pairs.append((was, f))
    for f in after:
        if f.severity in _BAD and all(f is not x for _, x in pairs):
            return f"it would add a new problem: {f.quantity} ({short(f)})"
    blind = []
    for was, f in sorted(pairs, key=lambda p: p[1].severity != "violation"):  # a violation is named first
        a, b = _gap(was, rows_was), _gap(f, rows_now)
        if a is not None and b is not None:
            if b.gap > a.gap + 1e-9 * max(1.0, abs(a.gap)):
                return f"it would deepen an existing problem: {f.quantity}, from {_num(a)} to {_num(b)} against {b.bound:.7g} {b.unit}"  # fmt: skip
        elif text(was) != text(f):
            blind.append(f"it changes an existing problem that has no number to compare, so it may be worse: {f.quantity}, from {short(was)} to {short(f)}")  # fmt: skip
        elif f.severity in _OPEN and _MEASURED.search(f"{f.observed} {f.expected}") and (hit := sorted(_depends(f.quantity) & (moves or set()))):  # fmt: skip
            blind.append(f"it may deepen an existing problem the engine holds no exact number for: {f.quantity} ({short(f)}) can depend on the {' and '.join(hit)} this change moves")  # fmt: skip
    return blind[0] if blind else None


#: what `_rows` reads for passed rows, and what it does not: the sentence every fix carries
COMPARED = (
    "Compared for lost margin: joint gravity loads, standing loads, the standing balance margin, tip distance, "
    "stall torque against effort, brown-out points and any passing row the check gives typed numbers. Not compared: "
    "current limits, wire ampacity, power budgets and the check's other passing rows; re-run the check after "
    "applying to see them."
)


def _margins(rows_was: list[_Row], rows_now: list[_Row]) -> list[str]:
    """Up to three passed rows that lose the largest share of their margin, with both numbers."""
    was = {(r.quantity, r.label): r for r in rows_was if r.passed}
    lost = [(r, w) for r in rows_now if r.passed and (w := was.get((r.quantity, r.label))) and r.gap > w.gap + 1e-9 * max(1.0, abs(w.gap))]  # fmt: skip
    # margin = -gap; share lost = (before - after) / before, so rows in different units rank alike
    lost.sort(key=lambda p: (p[0].gap - p[1].gap) / -p[1].gap if p[1].gap < 0 else float("inf"), reverse=True)
    return [
        f"{r.label} {w.value:.4g} to {r.value:.4g} {r.unit}, " + (f"of {r.bound:.4g} allowed" if r.value <= r.bound else f"above the {r.bound:.4g} minimum")  # fmt: skip
        for r, w in lost[:3]
    ]


def _after(c: Contract, report: Any, quantity: str, observed: str, expected: str) -> dict[str, str]:
    """The finding's numbers once it is gone: the check's own passed row (of two rows on one
    quantity, the one that reads like the finding), or the check's own helper; "passes" with the
    finding's bound where the check has neither."""
    rows = [r for r in report.scored_records if r.quantity == quantity]
    if rows:
        row = next((r for r in rows if r.observed == observed), rows[0])
        return {"observed": row.observed, "expected": row.expected}
    if m := re.fullmatch(r"topology/rails/([^/]+)/brownout", quantity):
        points = checks.rail_sweep(c, m[1], (1.0,))
        if points:
            return {"observed": f"{m[1]} holds {points[0][1]:.3g} V with every servo stalled", "expected": expected}  # fmt: skip
    if m := re.fullmatch(r"topology/wires/([^/]+)/fuse", quantity):
        wire = next(w for w in c.topology.wires if w.id == m[1])
        fuse, limits = c.rail(wire.rail).current_limit.base(), checks._wire_limits(wire)
        return {
            "observed": f"{fuse:g} A fuse on {wire.awg} AWG wire {wire.id}",
            "expected": f"fuse <= {limits[2]:g} A wire ampacity",
        }
    if m := re.fullmatch(r"topology/wires/([^/]+)/drop", quantity):
        wire = next(w for w in c.topology.wires if w.id == m[1])
        amps, part = checks._wire_load(c, wire)
        volts, drop = c.rail(wire.rail).voltage.base(), checks.wire_drop_v(c, wire, amps)
        return {
            "observed": f"servo wire drops {drop:.2f} V at stall ({volts:.1f} V -> {volts - drop:.2f} V)",
            "expected": f">= {part.numbers['input_voltage_min'].value:g} V ({part.id} minimum input voltage)",
        }
    if m := re.fullmatch(r"topology/battery/(c_rate_\w+)", quantity):
        battery, _, nominal = checks._pack(c)
        amps, mode, _ = checks._pack_amps(c, nominal)
        amp_hours, rate = battery.capacity.base() / nominal, getattr(battery, m[1])
        return {
            "observed": f"{amps:.3g} A peak ({mode})",
            "expected": f"<= {amp_hours * rate:.3g} A ({amp_hours:.3g} Ah x {rate:g}C)",
        }
    return {"observed": "passes", "expected": expected}  # the check prints no row once it passes


#: bounds a fix's own steps can change: the path, how a person names it, where it sits in a contract
def _plain(q: Any) -> tuple[float, str] | None:
    return None if q is None else (q.base(), f"{q.value:g} {q.unit}")


def _pack_limit(c: Contract, field: str) -> tuple[float, str] | None:
    """check_battery_c_rate's bound: capacity (Ah, at the pack's nominal voltage) x the C-rate."""
    found = checks._pack(c)
    rate = getattr(c.topology.battery, field) if c.topology and c.topology.battery else None
    if found is None or found[2] is None or rate is None:
        return None
    amp_hours = found[0].capacity.base() / found[2]
    return amp_hours * rate, f"{amp_hours * rate:.3g} A ({amp_hours:.3g} Ah x {rate:g}C)"


#: bounds a fix's own steps can change, each as the check that scores against it reads it:
#: the path, how a person names it, its (number, text) in a contract
_BOUNDS = (
    (r"topology/rails/([^/]+)/current_limit", "the fuse limit on {}", lambda c, m: _plain(c.rail(m[1]).current_limit)),  # check_rail_current
    (r"topology/rails/([^/]+)/capacity", "the power rating of {}", lambda c, m: _plain(c.rail(m[1]).capacity)),  # check_budgets (a budget limit against it), check_servo_bus
    (r"budgets/([^/]+)/limit", "the power budget {}", lambda c, m: _plain(next(b.limit for b in c.budgets if b.id == m[1]))),  # check_budgets
    (r"topology/battery/c_rate_(burst|continuous)", "the pack's {} limit", lambda c, m: _pack_limit(c, f"c_rate_{m[1]}")),  # check_battery_c_rate
)  # fmt: skip


def _bounds_moved(cand: _Candidate, was: Contract, now: Contract) -> list[str]:
    """Each bound the fix itself changes, as its check computes it, from the two contracts:
    the direction word comes from that number, never from one field of it."""
    out = []
    for step in cand.changes:
        for pattern, name, get in _BOUNDS:
            if (m := re.fullmatch(pattern, step["path"])) and (a := get(was, m)) and (b := get(now, m)) and a[0] != b[0]:  # fmt: skip
                out.append(f"{name.format(m[1])} goes from {a[1]} to {b[1]}; rows scored against it have {'less' if b[0] < a[0] else 'more'} headroom")  # fmt: skip
    return out


def _fix(cand: _Candidate, c: Contract, report: Any, target: Any, moved: list[str], bounds: list[str] = ()) -> dict[str, Any]:
    # One signed card: a swap is the assistant's `swap` tool; declared values are the app's batch
    # steps (`recordBatch`), each one op of the engine's `batch-edit`. Proved together: sign together.
    steps = [{"command": "swap", "args": cand.swap}] if cand.swap else [{"command": "declare", "args": x} for x in cand.changes]  # fmt: skip
    unverified = _unverified(cand.part)
    part = cand.part and {
        "id": cand.part.id,
        "name": cand.part.name,
        "numbers": {
            k: {"value": n.value, "unit": n.unit, "source": n.source}
            for k in cand.cite
            if (n := cand.part.numbers.get(k)) is not None
        },
    }
    return {
        "label": cand.label,
        "steps": steps,
        "after": _after(c, report, target.quantity, target.observed, target.expected),
        "verdict_after": verdict_word(verdict(report).code),
        "part": part,
        "notes": [*cand.notes, *([unverified] if unverified else []), *bounds, *(f"it moves a passing row closer to its bound: {m}" for m in moved), COMPARED],
    }


def suggest(contract_path: Path | str, finding: str, limit: int = 3) -> dict[str, Any]:
    """Proved fixes for one finding of ``contract_path``, smallest change first, at most ``limit``.

    ``finding`` is the finding's quantity path as `check` prints it (or its last segments when
    only one finding ends that way). A fix is returned only when the full check on the changed
    contract no longer has that quantity as a violation, a warning or a not-scored line, has no
    violation or warning left on the entry the fix changes, has no violation, warning or
    not-scored line the current contract lacks, and has no finding that was already there with a
    number further past its bound (`_worse`). The file is byte-checked on the way out.
    """
    path = Path(contract_path).resolve()
    before, start = _bytes(path), time.monotonic()
    try:
        return _suggest(path, finding, limit, start)
    finally:
        _assert_unchanged(path, before, name="contract")


def _suggest(path: Path, finding: str, limit: int, start: float) -> dict[str, Any]:
    contract = load_contract(path)
    base = _check(contract, path).report
    doc: dict[str, Any] = {
        "finding": finding,
        "kind": None,
        "robot_verdict": verdict_word(verdict(base).code),  # not "verdict": a payload "verdict" is a run verdict to the app (ipcKit validateRunReplySemantics)
        "before": None,
        "fixes": [],
        "tried": 0,
        "turned_down": [],  # each change tried and not returned, with why
        "reason": None,
        "stopped_early": None,  # set when the time budget cut the search short
    }
    standing = sorted({f.quantity for f in base if f.severity in _OPEN})
    named = [q for q in standing if q == finding or q.endswith("/" + finding)]
    if len(named) != 1:
        doc["reason"] = (
            f"no single finding is named {finding!r}; this robot's findings: {', '.join(standing)}"
            if standing
            else "this robot has no violation or warning to fix"
        )
        return doc
    quantity = named[0]
    doc["finding"] = quantity

    def mine(findings: Any) -> Any:
        return next((f for f in findings if f.quantity == quantity and f.severity in _OPEN), None)

    target = mine(base)
    for pattern, kind, emits, candidates in _KINDS:
        # A kind is searched only when its own check emits the finding today: a joint's gravity
        # load over its effort limit shares the stall finding's path, and no servo swap moves it.
        m = re.fullmatch(pattern, quantity)
        found = candidates(contract, path, m) if m and mine(emits(contract)) else None
        if found is not None:
            target = mine(emits(contract))
            break
    else:
        kind, found = None, []
    doc["kind"] = kind
    doc["before"] = {"severity": target.severity, "observed": target.observed, "expected": target.expected}  # fmt: skip
    if kind is None:
        doc["reason"] = NO_KIND
        return doc
    checked, longest, rows_base = 0, 0.0, _rows(base, contract)
    # verified parts first, then the catalog's "unverified" ones; in each, the smallest change first
    for cand in sorted(found, key=lambda x: (_unverified(x.part) is not None, x.size)):
        # ponytail: at most limit + 2 full checks, so a search stays a few seconds; a longer
        # candidate list is cut there and `tried` says how far it got
        if len(doc["fixes"]) >= limit or checked >= limit + 2:
            break
        if time.monotonic() - start + longest > BUDGET_S:  # the next candidate would not fit the budget
            doc["stopped_early"] = (
                f"the search stopped early, after {time.monotonic() - start:.1f} s and {doc['tried']} of "
                f"{len(found)} changes, to answer in time; the fixes listed are proved, others may exist"
            )
            break
        began = time.monotonic()
        doc["tried"] += 1
        try:
            after = _proposed(path, cand)
        except (EditRefusal, ValueError) as error:
            why = f"the engine refuses it: {error}"
        else:
            why = "the finding is still there"
            # the one check that emits the finding still does: no full check needed
            if not any(f.quantity == quantity and f.severity in _BAD for f in emits(after)):
                report, checked = _check(after, path).report, checked + 1
                rows = _rows(report, after)
                left = [f.quantity for f in report if f.severity in _OPEN and f.quantity.startswith(cand.scope)]  # fmt: skip
                if not any(f.quantity == quantity and f.severity in _BAD for f in report):
                    why = f"a problem is left on the part it changes: {left[0]}" if left else _worse(base, report, rows_base, rows, _moves(cand))  # fmt: skip
                if why is None:
                    doc["fixes"].append(_fix(cand, after, report, target, _margins(rows_base, rows), _bounds_moved(cand, contract, after)))
                    longest = max(longest, time.monotonic() - began)
                    continue
        doc["turned_down"].append({"label": cand.label, "why": why})
        longest = max(longest, time.monotonic() - began)
    if not doc["fixes"]:
        doc["reason"] = doc["stopped_early"] or (
            f"tried {doc['tried']} of {len(found)} changes; none cleared the finding without a new "
            "problem or a deeper one (turned_down says why for each)"
            if found
            else "nothing in the catalog or the lock to try for this finding"
        )
    return doc


class SuggestFixesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: str = Field(description="The lockfile to search fixes for.")
    finding: str = Field(description="The finding to clear: its quantity path as check prints it, such as topology/wires/w.pack/fuse.")  # fmt: skip
    # at most 5: a search runs up to limit + 2 full checks, and a check can take over a second
    limit: int = Field(3, ge=1, le=5, description="The most fixes to return.")


class SuggestFixesCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["suggest-fixes"] = "suggest-fixes"
    args: SuggestFixesArgs


def handle(args: SuggestFixesArgs) -> Any:
    """The served `suggest-fixes`: reads only."""
    from lockwire import serve  # noqa: PLC0415 - serve imports this module for its command table

    cli_line = serve._build_cli_line("lockwire", "check", args.contract)
    try:
        payload = suggest(args.contract, args.finding, args.limit)
    except serve._LOAD_ERRORS as error:
        return serve._refusal(cli_line, 2, serve._no_contract(Path(args.contract)) or str(error))
    return serve._ok(cli_line, exit_code=0, verdict=None, payload=payload)
