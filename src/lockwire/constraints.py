"""the constraints each part participates in, for the app's badges and margin bars.

One row per constraint instance the engine evaluates. Numbers are the checks' own:
``report_html._budget_rows`` for budgets (itself ``power_rollup``), ``power_rollup`` for rail
current, the same drivers x bit/s sum for buses, the same torque/thermal arithmetic. A row with
no used or limit value is ``unset`` rather than a guess. No second scorer, no schema change.

Domain mapping (the graph lens ids stay as they are): lens ``mechanical`` -> "mechanical",
lens ``power`` -> "electrical", lens ``data`` -> "data", lens ``health`` -> "health";
thermal has no lens and gets its own "thermal".
"""

from __future__ import annotations

from typing import Any

from lockwire.checks import BUS_UTILIZATION_CEILING, _device_worst_draw_w, power_rollup
from lockwire.scene_view import _bus_consumers, _rail_consumers
from lockwire.schema import Contract

#: Under this much headroom (percent of the limit) an in-limits constraint is "warn".
WARN_MARGIN_PCT = 10.0

_HEALTH_STATUS = {
    "healthy": "ok",
    "degrading": "warn",
    "stale": "warn",
    "faulty": "bad",
    "watched": "unset",
    "not_watched": "unset",
}


def _row(
    id_: str,
    label: str,
    domain: str,
    *,
    used: float | None,
    limit: float | None,
    unit: str,
    used_kind: str | None,
    limit_kind: str | None,
    components: list[str],
    direction: str = "max",
    over: bool | None = None,
    scale: float | None = None,
) -> dict[str, Any]:
    """``over`` overrides the used-vs-limit comparison (a budget's own verdict);
    ``scale`` is the margin's denominator (default: the limit)."""
    margin: float | None = None
    if used is not None and limit is not None and (scale or limit) > 0:
        gap = limit - used if direction == "max" else used - limit
        margin = round(gap / (scale or limit) * 100.0, 1)
    if margin is None and over is None:
        status = "unset"
    elif over or (over is None and margin is not None and margin < 0):
        status = "bad"
    elif margin is not None and margin < WARN_MARGIN_PCT:
        status = "warn"
    else:
        status = "ok" if margin is not None else "unset"
    return {
        "id": id_,
        "label": label,
        "domain": domain,
        "used": used,
        "limit": limit,
        "unit": unit,
        "margin_pct": margin,
        "status": status,
        "used_kind": used_kind if used is not None else None,
        "limit_kind": limit_kind if limit is not None else None,
        "parts": [],
        "components": list(dict.fromkeys(components)),
    }


def _budget_constraints(contract: Contract, budgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    by_id = {b.id: b for b in contract.budgets}
    for row in budgets:
        budget = by_id[row["id"]]
        components: list[str] = []
        if budget.kind == "rail_power" and budget.rail:
            components = [budget.rail, *sorted(_rail_consumers(contract, budget.rail))]
        elif budget.kind == "mass_total":
            if budget.sources in ("devices", "links+devices"):
                components += [
                    d.id
                    for d in contract.devices
                    if d.physical is not None and d.physical.mass is not None
                ]
            if budget.sources in ("links", "links+devices") and contract.model is not None:
                components += [ln.name for ln in contract.model.links if ln.mass is not None]
        label = {"rail_power": "Rail power", "mass_total": "Mass", "endurance": "Endurance"}[
            budget.kind
        ]
        out.append(
            _row(
                f"budget.{row['id']}",
                label,
                "mechanical" if budget.kind == "mass_total" else "electrical",
                used=row["value"],
                limit=row["limit"],
                unit=row["unit"],
                used_kind="derived",
                limit_kind="declared",
                components=components,
                direction=row["direction"],
                over=True if row["state"] == "breach" else None if row["value"] is None else False,
            )
        )
    return out


def _rail_current_constraints(contract: Contract) -> list[dict[str, Any]]:
    if contract.topology is None:
        return []
    totals, witnesses, _, _, unknown, _ = power_rollup(contract, "nominal")
    out = []
    for rail in contract.topology.rails:
        if rail.current_limit is None:
            continue
        volts = (
            rail.voltage_range.min.base()
            if rail.voltage_range is not None
            else rail.voltage.base()
            if rail.voltage is not None
            else None
        )
        summable = volts and witnesses.get(rail.id) and not unknown.get(rail.id)
        out.append(
            _row(
                f"rail-current.{rail.id}",
                "Rail current",
                "electrical",
                used=totals.get(rail.id, 0.0) / volts if summable else None,
                limit=rail.current_limit.base(),
                unit="A",
                used_kind="derived",
                limit_kind="declared",
                components=[rail.id, *sorted(_rail_consumers(contract, rail.id))],
            )
        )
    return out


def _bus_constraints(contract: Contract) -> list[dict[str, Any]]:
    out = []
    for bus in contract.topology.buses if contract.topology is not None else []:
        drivers = [
            d
            for d in contract.devices
            if d.identity is not None
            and d.identity.bus_ref == bus.id
            and d.driver is not None
            and d.driver.nominal_rate_hz is not None
            and d.driver.payload_bytes is not None
        ]
        used = sum(d.driver.nominal_rate_hz * d.driver.payload_bytes * 8 for d in drivers)
        out.append(
            _row(
                f"bus.{bus.id}",
                "Bus bandwidth",
                "data",
                used=used if used > 0 else None,
                limit=bus.bitrate * BUS_UTILIZATION_CEILING if bus.bitrate else None,
                unit="bit/s",
                used_kind="derived",
                limit_kind="derived",
                components=[bus.id, *sorted(_bus_consumers(contract, bus.id))],
            )
        )
    return out


def _joint_constraints(contract: Contract) -> list[dict[str, Any]]:
    if contract.model is None:
        return []
    out = []
    driver = {d.drives_joint: d for d in contract.devices if d.drives_joint is not None}
    for joint in contract.model.joints:
        lim = joint.limits
        if lim is None or joint.type == "fixed":
            continue
        rot = joint.type in ("revolute", "continuous")
        dev = driver.get(joint.name)
        model = contract.device_model(dev.model) if dev is not None else None
        act = model.actuator if model is not None else None
        comps = [joint.name, joint.child, *([dev.id] if dev is not None else [])]
        if lim.effort is not None:  # <= 0 is URDF's "unset": still a row, used=None
            have = act is not None and act.torque_continuous and act.gear_ratio
            out.append(
                _row(
                    f"joint-torque.{joint.name}",
                    "Joint torque",
                    "mechanical",
                    used=lim.effort_limit,
                    limit=act.torque_continuous * act.gear_ratio if have else None,
                    unit="N*m" if rot else "N",
                    used_kind="declared",
                    limit_kind="derived",
                    components=comps,
                )
            )
        if lim.velocity is not None:
            out.append(
                _row(
                    f"joint-speed.{joint.name}",
                    "Joint speed",
                    "mechanical",
                    used=None,
                    limit=lim.velocity if lim.velocity > 0 else None,  # 0 = unset
                    unit="rad/s" if rot else "m/s",
                    used_kind=None,
                    limit_kind="declared",
                    components=comps,
                )
            )
        if lim.lower is not None and lim.upper is not None and joint.type != "continuous":
            out.append(
                _row(
                    f"joint-travel.{joint.name}",
                    "Joint travel",
                    "mechanical",
                    used=None,
                    limit=lim.upper - lim.lower,
                    unit="rad" if rot else "m",
                    used_kind=None,
                    limit_kind="derived",
                    components=comps,
                )
            )
    return out


def _thermal_constraints(contract: Contract) -> list[dict[str, Any]]:
    out = []
    for device in contract.devices:
        model = contract.device_model(device.model)
        limit = model.thermal_limit if model is not None else None
        if limit is None:
            continue
        t, draw = device.thermal, _device_worst_draw_w(device)
        used = None
        if (
            limit.kind == "throttle"
            and t is not None
            and t.resistance is not None
            and t.ambient is not None
            and draw is not None
        ):
            used = t.ambient.base() + draw[0] * t.resistance.base()
        limit_c = limit.temperature.base()
        out.append(
            _row(
                f"thermal.{device.id}",
                "Junction temp",
                "thermal",
                used=used,
                limit=limit_c,
                unit="C",
                used_kind="derived",
                limit_kind="declared",
                components=[device.id],
                # the check fires at estimate >= limit, so the cap is "over" at equality
                over=None if used is None else used >= limit_c,
                scale=limit_c + 273.15,  # percent of absolute temperature, not of a Celsius figure
            )
        )
    return out


def _health_constraints(health_devices: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            **_row(
                f"health.{h['id']}",
                "Health",
                "health",
                used=None,
                limit=None,
                unit="",
                used_kind=None,
                limit_kind=None,
                components=[h["id"]],
            ),
            "status": _HEALTH_STATUS.get(h["state"], "unset"),
        }
        for h in health_devices or []
    ]


def constraint_rows(
    contract: Contract,
    budgets: list[dict[str, Any]],
    *,
    health_devices: list[dict[str, Any]] | None = None,
    scene_part_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """``parts`` is ``components`` narrowed to ids that have a scene part."""
    rows = [
        *_budget_constraints(contract, budgets),
        *_rail_current_constraints(contract),
        *_bus_constraints(contract),
        *_joint_constraints(contract),
        *_thermal_constraints(contract),
        *_health_constraints(health_devices),
    ]
    for row in rows:
        row["parts"] = [c for c in row["components"] if c in (scene_part_ids or ())]
    return rows
