"""Light / typical / max load per rail, and the budget as a CSV.

Operating points, per device, in watts at the device, from data already in the lockfile:

- light:   the lowest draw the device declares (nominal ``draw`` or any named mode, so an
           ``idle``/``standby`` mode wins), and never above ``typical``.
- typical: the nominal ``draw``; with a Plan, the duty-weighted draw
           (``plan.duty_weighted_current`` times rail voltage) for devices the plan names.
- max:     the highest draw the device declares (nominal or any mode, e.g. a stall mode).

A device with no declared draw is left out of every number and shown as ``unknown`` in the
CSV, never as 0. Rails are rolled up toward the battery by ``checks.power_rollup``.
No findings: this module only reports columns.
"""

import csv
import io

from lockwire.checks import power_rollup
from lockwire.plan import Plan, duty_weighted_current
from lockwire.schema import Contract, DeviceInstance, PowerDraw, PowerQuantity

ENGINE_CHECKS = ()
POINTS = ("light", "typical", "max")


def device_points(contract: Contract, plan: Plan | None = None) -> dict[str, dict[str, float]]:
    """{device id: {light, typical, max}} in watts, for devices with a declared draw and rail."""
    amps = duty_weighted_current(contract, plan) if plan is not None else {}
    out: dict[str, dict[str, float]] = {}
    for dev in contract.devices:
        power = dev.power
        if power is None or power.draw is None:
            continue
        draws = [power.draw.base(), *(m.draw.base() for m in power.modes)]
        rail = contract.rail(power.rail)
        typical = power.draw.base()
        if dev.id in amps and rail is not None and rail.voltage is not None:
            typical = amps[dev.id] * rail.voltage.base()
        out[dev.id] = {"light": min(*draws, typical), "typical": typical, "max": max(draws)}
    return out


def rail_columns(contract: Contract, plan: Plan | None = None) -> dict[str, dict[str, float]]:
    """{rail id: {light, typical, max}} in watts, each load carried up through its converters."""
    points = device_points(contract, plan)
    out: dict[str, dict[str, float]] = {}
    for point in POINTS:
        synthetic = [
            DeviceInstance(
                id=dev_id,
                model="budget",
                owner="lockwire budget",
                power=PowerDraw(
                    rail=contract.device(dev_id).power.rail,
                    draw=PowerQuantity(value=w[point], unit="W"),
                ),
            )
            for dev_id, w in points.items()
        ]
        totals, *_ = power_rollup(contract.model_copy(update={"devices": synthetic}), "nominal")
        for rail_id, watts in totals.items():
            out.setdefault(rail_id, {})[point] = watts
    return out


def budget_csv(contract: Contract, plan: Plan | None = None) -> str:
    """One row per device per rail: light/typical/max watts and the plan duty (blank if none)."""
    points = device_points(contract, plan)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["device", "rail", "light_w", "typical_w", "max_w", "duty_derived_typical_over_nominal"]
    )
    for dev in contract.devices:
        if dev.power is None:
            continue
        w = points.get(dev.id)
        if w is None:
            writer.writerow([dev.id, dev.power.rail, "unknown", "unknown", "unknown", ""])
            continue
        nominal = dev.power.draw.base()
        duty = f"{w['typical'] / nominal:.4g}" if plan is not None and nominal else ""
        writer.writerow([dev.id, dev.power.rail, *(f"{w[p]:.6g}" for p in POINTS), duty])
    return buf.getvalue()
