"""Battery cell model: OCV curve + R0 + one RC branch, cold and aged corners.

Free engine code; ``checks.py`` loads it through ``ENGINE_MODULES``. The
reserve check and pack builder sit below the model section.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

from lockwire.report import Finding, Quantity

LABEL = "tested on recorded files, not on hardware"


# ---- model ---------------------------------------------------------------------------------


def ocv_at(curve, soc_pct: float) -> float | None:
    """Open-circuit voltage by linear interpolation over (soc_pct, voltage_v) points; clamped."""
    pts = sorted((p.soc_pct, p.voltage_v) for p in curve or [])
    if not pts:
        return None
    if soc_pct <= pts[0][0]:
        return pts[0][1]
    for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
        if soc_pct <= s1:
            return v0 + (v1 - v0) * (soc_pct - s0) / (s1 - s0) if s1 > s0 else v1
    return pts[-1][1]


def pack_from_cell(
    ocv_cell_v: float, r_cell_mohm: float, series: int, parallel: int
) -> tuple[float, float]:
    """Pack (OCV V, resistance mOhm) from one cell: voltages add in series, resistances divide in parallel."""
    return ocv_cell_v * series, r_cell_mohm * series / parallel


def corner(
    r0_mohm: float,
    r1_mohm: float | None = None,
    *,
    capacity_factor: float = 1.0,
    resistance_growth: float = 1.0,
) -> tuple[float, float | None]:
    """Resistances at a corner. A cold or aged cell holding ``capacity_factor`` of its capacity
    also has 1/factor times the resistance (one factor for both), times ``resistance_growth`` for age."""
    k = resistance_growth / capacity_factor
    return r0_mohm * k, None if r1_mohm is None else r1_mohm * k


def sag_v(
    amps: float,
    r0_mohm: float,
    r1_mohm: float | None = None,
    c1_f: float | None = None,
    t_s: float | None = None,
) -> float:
    """Voltage drop under a current step: I*R0 plus the RC branch (settled when ``t_s`` is None)."""
    drop = amps * r0_mohm / 1000.0
    if r1_mohm is None:
        return drop
    settle = 1.0
    if t_s is not None and c1_f is not None:
        settle = 1.0 - math.exp(-t_s / (r1_mohm / 1000.0 * c1_f))
    return drop + amps * r1_mohm / 1000.0 * settle


class Cell(NamedTuple):
    """One cell's datasheet numbers (the cell library supplies these; fixtures until it lands)."""

    nominal_v: float
    capacity_ah: float
    mass_kg: float
    max_a: float  # max continuous discharge
    r_mohm: float


class Pack(NamedTuple):
    voltage_v: float
    capacity_ah: float
    energy_wh: float
    mass_kg: float  # cells only: no wiring, holder or BMS
    max_a: float
    r_mohm: float


def build_pack(cell: Cell, series: int, parallel: int) -> Pack:
    """Series adds voltage; parallel adds capacity and current. Mass is every cell."""
    volts, r_mohm = pack_from_cell(cell.nominal_v, cell.r_mohm, series, parallel)
    ah = cell.capacity_ah * parallel
    return Pack(
        volts, ah, volts * ah, cell.mass_kg * series * parallel, cell.max_a * parallel, r_mohm
    )


def plan_energy_wh(contract, plan) -> float | None:
    """Wh the timed plan draws: sum of duty x seconds x declared draw. None when any segment has
    no duration or any listed device no declared draw (a guess would pass a pack that is short)."""
    from lockwire.plan import plan_segments, total_time

    if isinstance(total_time(plan), str):
        return None
    joules = 0.0
    for seg in plan_segments(plan):
        for dev_id, duty in seg.device_modes.items():
            dev = contract.device(dev_id)
            if dev is None or dev.power is None or dev.power.draw is None:
                return None
            joules += duty * seg.duration_s * dev.power.draw.base()
    return joules / 3600.0


# ---- checks --------------------------------------------------------------------------------


def check_cell_sag(contract, contract_path: Path | None = None) -> list[Finding]:
    """Pack sag at the end of usable charge, nominal and at the coldest declared derating,
    against the lowest pack-fed rail minimum. Warning only; silent on any missing input."""
    from lockwire.checks import _bus_rails, _pack_amps

    battery = contract.topology.battery if contract.topology else None
    r0 = battery.internal_resistance_mohm if battery else None
    if battery is None or r0 is None or not battery.ocv_curve:
        return []
    soc = battery.reserve_pct or 0.0
    ocv = ocv_at(battery.ocv_curve, soc)
    nominal = battery.voltage.base() if battery.voltage else ocv_at(battery.ocv_curve, 50.0)
    rails = _bus_rails(contract, nominal)
    if not rails:
        return []
    amps, mode, unknown = _pack_amps(contract, nominal)
    if unknown:
        return []
    rail = max(rails, key=lambda r: r.voltage_range.min.base())
    floor = rail.voltage_range.min.base()
    cold = min((d.capacity_factor for d in battery.derating), default=1.0)
    for name, factor in (("nominal", 1.0), ("cold", cold)):
        r0c, r1c = corner(r0, battery.r1_mohm, capacity_factor=factor)
        volts = ocv - sag_v(amps, r0c, r1c, battery.c1_f)
        if volts >= floor:
            continue
        prov = rail.provenance  # the rail minimum's own citation; without one it is advice
        return [
            Finding(
                severity="warning",
                quantity="topology/battery/ocv_curve",
                observed=f"pack sags to {volts:.3g} V at {amps:.3g} A, {soc:g}% charge, "
                f"{name} corner ({mode})",
                expected=f">= {floor:g} V, {rail.id}'s minimum",
                observed_quantity=Quantity(value=volts, unit="V"),
                expected_quantity=Quantity(value=floor, unit="V"),
                action=f"{rail.id} may brown out near empty; lower the peak or the pack resistance"
                f" ({LABEL})",
                source=prov.source if prov else None,
                witness=prov.witness if prov else None,
                locked_at=prov.locked_at if prov else None,
                advice_unsourced=prov is None,
            )
        ]
    return []


def check_reserve(contract, contract_path: Path | None = None, plan=None) -> list[Finding]:
    """Energy the timed plan needs against the pack's energy after the reserve (taken once).
    Silent with no plan, no reserve, no battery, or any input the plan energy needs.
    ``plan`` is a keyword: the engine registry passes none, so only a plan-aware caller fires it."""
    battery = contract.topology.battery if contract.topology else None
    if plan is None or battery is None or battery.reserve_pct is None:
        return []
    need = plan_energy_wh(contract, plan)
    if need is None:
        return []
    usable = battery.capacity.base() * (1 - battery.reserve_pct / 100.0)
    if need <= usable:
        return []
    return [
        Finding(
            severity="warning",
            quantity="topology/battery/reserve_pct",
            observed=f"plan needs {need:.4g} Wh",
            expected=f"<= {usable:.4g} Wh usable ({battery.capacity.base():g} Wh less "
            f"{battery.reserve_pct:g}% reserve)",
            observed_quantity=Quantity(value=need, unit="Wh"),
            expected_quantity=Quantity(value=usable, unit="Wh"),
            action=f"the plan eats into the reserve; shorten it or grow the pack ({LABEL})",
            advice_unsourced=True,  # the reserve and capacity carry no citation of their own
        )
    ]


ENGINE_CHECKS = (check_cell_sag, check_reserve)
