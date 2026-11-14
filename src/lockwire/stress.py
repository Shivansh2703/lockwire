"""The stress list, every part's load as a percent of its rating, hot ones first.

Ratings come only from catalog rows (lane F, cited) reached through ``provenance.compared_from``
on a device or its model, or from the connector rating fields. A part with no rating on
record, or an operating value that is missing, adds nothing. Over 100 % of a part's own rating
is a sourced warning (cites the catalog source); the 80 % line is an unsourced convention, so it
is advice only. Nothing here reads a field a file written before 0.5.0 can carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lockwire.catalog import CatalogPart, SourcedNumber, load_catalog
from lockwire.report import Finding
from lockwire.schema import Contract, DeviceInstance

DERATE_PCT = 80.0  # UNSOURCED convention: advice only, never a verdict


@dataclass(frozen=True)
class StressRow:
    part: str  # device id
    quantity: str  # e.g. "output current"
    value: float
    rating: float
    unit: str
    source: str  # where the rating came from
    path: str  # contract path

    @property
    def pct(self) -> float:
        return 100.0 * self.value / self.rating


def _rating(part: CatalogPart | None, key: str, units: tuple[str, ...]):
    n = part.numbers.get(key) if part else None
    return n if n is not None and n.unit in units else None


def _draw_w(d: DeviceInstance) -> float | None:
    """Worst declared draw of a device, W; None when it states none."""
    if d.power is None or d.power.draw is None:
        return None
    return max([d.power.draw.base(), *(m.draw.base() for m in d.power.modes)])


def _rail_v(c: Contract, rail_id: str | None) -> float | None:
    rail = c.rail(rail_id) if rail_id else None
    return rail.voltage.base() if rail and rail.voltage is not None else None


def _in_volts(c: Contract, rail) -> float | None:
    if rail.upstream == "battery":
        b = c.topology.battery if c.topology else None
        return b.voltage.base() if b and b.voltage is not None else None
    return _rail_v(c, rail.upstream)


def stress_list(contract: Contract) -> list[StressRow]:
    """Every part-against-rating row, sorted by percent of rating, highest first."""
    from lockwire.checks import _worst_rail_power  # read-only; checks loads this module lazily

    c = contract
    catalog = {p.id: p for p in load_catalog().parts}
    models = {m.id: m for m in c.device_models}
    rows: list[StressRow] = []

    def add(d, quantity, value, n, label=None):
        if n is not None and n.value > 0:
            src = f"{label or ''} {n.source}".strip()
            rows.append(StressRow(d.id, quantity, value, n.value, n.unit, src, f"devices/{d.id}"))

    def rail_amps(rail_id):
        v = _rail_v(c, rail_id)
        if v is None or v <= 0:
            return None
        w, _, _, unknown = _worst_rail_power(c, rail_id)
        return None if unknown or w <= 0 else w / v

    for d in c.devices:
        model = models.get(d.model)
        prov = d.provenance or (model.provenance if model else None)
        part = catalog.get(prov.compared_from) if prov and prov.compared_from else None
        if part is not None:
            label = f"catalog {part.id}:"
            for rail in (
                r for r in (c.topology.rails if c.topology else []) if r.source_device == d.id
            ):
                amps = rail_amps(rail.id)
                if amps is not None:
                    add(
                        d,
                        f"output current ({rail.id})",
                        amps,
                        _rating(part, "output_current_max", ("A",)),
                        label,
                    )
                vin = _in_volts(c, rail)
                if vin is not None:
                    add(
                        d,
                        f"input voltage ({rail.id})",
                        vin,
                        _rating(part, "input_voltage_max", ("V",)),
                        label,
                    )
                _junction(rows, c, d, model, part, rail, label)
            v = _rail_v(c, d.power.rail) if d.power else None
            w = _draw_w(d)
            if v and w:  # a driver, ESC or fuse sits on the rail it carries
                amps = w / v
                add(
                    d,
                    "current",
                    amps,
                    _rating(part, "current_continuous", ("A",))
                    or _rating(part, "hold_current", ("A",)),
                    label,
                )
                add(
                    d,
                    "voltage",
                    v,
                    _rating(part, "input_voltage_max", ("V",))
                    or _rating(part, "rated_voltage", ("V",)),
                    label,
                )
        for conn in d.connectors or []:
            w = _draw_w(d)
            v = _rail_v(c, d.power.rail) if d.power else None
            if not (w and v and conn.id in (d.power_inputs or [])):
                continue
            ctype = next((t for t in c.connector_types or [] if t.id == conn.type), None)
            limit = conn.max_current_a or (ctype.rated_current_a if ctype else None)
            if limit:
                n = SourcedNumber(
                    value=limit,
                    unit="A",
                    kind="source_file",
                    source=f"connector {conn.id} rating in the lockfile",
                )
                add(d, f"connector {conn.id} current", w / v, n)
    return sorted(rows, key=lambda r: r.pct, reverse=True)


def _junction(rows, c, d, model, part, rail, label):
    """Tj = Ta + P_loss * theta_JA, as a share of the rise allowed by the model's thermal limit."""
    from lockwire.checks import _worst_rail_power

    theta = rail.thermal_resistance_k_per_w
    n = _rating(part, "thermal_resistance_ja", ("C/W",))
    if theta is None and n is not None:
        theta = n.value
    lim = model.thermal_limit if model else None
    amb = d.thermal.ambient if d.thermal else None
    w, _, _, unknown = _worst_rail_power(c, rail.id)
    if theta is None or lim is None or amb is None or unknown or not w:
        return  # theta_JA, ambient or the limit is not on record: no row
    kind = (rail.regulator_kind or part.family.rsplit(".", 1)[-1]).lower()
    if kind == "ldo":  # exact for a linear regulator: all of (Vin - Vout) x Iout is heat
        vin, vout = _in_volts(c, rail), _rail_v(c, rail.id)
        if vin is None or vout is None or vin <= vout:
            return
        iq = _rating(part, "quiescent_current", ("mA", "uA"))
        iq_a = iq.value * (1e-3 if iq.unit == "mA" else 1e-6) if iq else 0.0
        loss, how = (
            (vin - vout) * w / vout + vin * iq_a,
            "linear regulator loss (Vin - Vout) x Iout",
        )
    elif kind in ("buck", "boost", "buck-boost") and rail.efficiency < 1.0:
        loss, how = (
            w * (1 / rail.efficiency - 1),
            "switching regulator loss (1/efficiency - 1) x Pout",
        )
    else:
        return  # unknown kind, or switching with no efficiency on record: no formula to stand behind
    rise = loss * theta
    room = lim.temperature.base() - amb.base()
    if room > 0:
        src = SourcedNumber(value=room, unit="K", kind="datasheet", source=lim.provenance.source)
        rows.append(
            StressRow(
                d.id,
                f"junction rise ({rail.id})",
                rise,
                room,
                "K",
                f"{label} {src.source} [{how}]",
                f"devices/{d.id}",
            )
        )


def check_stress(contract: Contract, contract_path: Path | None = None) -> list[Finding]:
    out = []
    for r in stress_list(contract):
        if r.pct < DERATE_PCT:
            break  # sorted: nothing below here either
        over = r.pct > 100.0
        out.append(
            Finding(
                severity="warning",
                quantity=r.path,
                observed=f"{r.part} {r.quantity}: {r.value:g} {r.unit}, {r.pct:.0f}% of its rating",
                expected=f"<= {r.rating:g} {r.unit} (rating: {r.source})"
                if over
                else f"under {DERATE_PCT:.0f}% of {r.rating:g} {r.unit} (advice: not yet sourced)",
                action="use a part rated higher, or cut the load"
                if over
                else "leave margin for heat and ageing",
                source=r.source if over else None,
                advice_unsourced=not over,
            )
        )
    return out


def check_driver_motor_match(
    contract: Contract, contract_path: Path | None = None
) -> list[Finding]:
    """A driver/ESC's continuous current must cover its motor's continuous current (not its peak)."""
    c = contract
    catalog = {p.id: p for p in load_catalog().parts}
    models = {m.id: m for m in c.device_models}
    out = []
    for drv in c.devices:
        dm = models.get(drv.model)
        prov = drv.provenance or (dm.provenance if dm else None)
        part = (
            catalog.get(prov.compared_from)
            if prov and prov.compared_from and drv.drives_joint
            else None
        )
        n = _rating(part, "current_continuous", ("A",))
        if n is None:
            continue
        for mot in c.devices:
            a = models[mot.model].actuator if mot.model in models else None
            if (
                mot is drv
                or mot.drives_joint != drv.drives_joint
                or a is None
                or a.continuous_current_a is None
            ):
                continue
            if a.continuous_current_a > n.value:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"devices/{drv.id}",
                        observed=f"{drv.id} is rated {n.value:g} A continuous; {mot.id} draws {a.continuous_current_a:g} A continuous",
                        expected=f">= {a.continuous_current_a:g} A continuous (rating: catalog {part.id}: {n.source})",
                        action="pick a driver or ESC rated for the motor's continuous current",
                        source=n.source,
                    )
                )
    return out


ENGINE_CHECKS = (check_stress, check_driver_motor_match)
