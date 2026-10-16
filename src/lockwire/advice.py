"""The alternatives half of the advice sentence.

``check_budgets`` (``checks.py``) already knows a rail_power budget is over its
limit; this module answers the second half of the sentence — "what else could
carry it" — headroom on every OTHER rail_power budget, plus a projected
regulator suggestion. It computes nothing about rail membership itself:
Reuse means ``checks.power_rollup``'s own return values (rail totals, the
per-rail unknown-consumer sets) are passed in as plain data, never re-walked.
This module never imports ``checks`` — the call only ever goes the other way,
so there is no cycle.

Every number carries its ``kind`` (a catalog or projected
figure is shown only with its own visual mark and word, never in the same
style as a measured one): headroom is ``derived-by-<check>`` — the same
rollup ``check_budgets`` itself scores against; a suggested regulator rating
is ``projected``, a number this product is choosing, not measuring.

A move is offered only onto a rail whose declared voltage suits a load on the
over rail (its model's input range, else the over rail's nominal ±5%) and has
room for that load, and never onto a rail fed through the over rail itself. The
move names the largest such load and says whether it clears the overload.

**Raising the over-budget rail's own declared capacity is never offered**
: that clears the finding by moving the limit rather than
the draw, which leaves a standing mark. Offering it as
advice would make the product recommend the exact thing that mark exists to flag.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from lockwire.schema import Contract, Rail


class Alternative(BaseModel):
    """One remedy suggestion, its number, and the kind that number carries."""

    model_config = ConfigDict(extra="forbid")

    text: str
    value: float
    unit: str
    kind: str  # "derived-by-<check>" | "projected" | "none"
    #: The word for a ``projected`` figure. Required when ``kind ==
    #: "projected"`` — a projected figure reaching a door carries its mark and
    #: its word, not a corrected sentence saying it is unenforced.
    mark: str | None = None
    #: The rail this remedy points at (a move: the rail to move to; a regulator: the
    #: overloaded rail it relieves), and the device a move moves. Typed, so a reader
    #: never has to parse ``text`` for them.
    rail: str | None = None
    device: str | None = None

    @model_validator(mode="after")
    def _carries_its_word(self) -> Alternative:
        if self.kind == "projected" and not self.mark:
            raise ValueError("a 'projected' number must carry its own word, e.g. mark='projected'")
        return self


def build_rail_alternatives(
    contract: Contract,
    *,
    over_rail_id: str,
    deficit_w: float,
    rail_totals: dict[str, float],
    rail_unknown: dict[str, set[str]],
    check_name: str = "check_budgets",
    draw_mode: str = "nominal",
) -> list[Alternative]:
    """Alternatives for a rail_power budget over its limit on ``over_rail_id``.

    ``rail_totals``/``rail_unknown`` must come from ``checks.power_rollup`` run
    against the same contract and draw mode as the violation being explained —
    the shared builder that every call site (and every mutation bite) goes
    through, so a broken kind anywhere breaks every caller alike.
    """
    alternatives: list[Alternative] = []
    loads = _movable_loads(contract, over_rail_id, draw_mode)
    for budget in contract.budgets:
        if budget.kind != "rail_power" or budget.rail in (None, over_rail_id):
            continue
        target = contract.rail(budget.rail)
        if target is None or target.voltage is None or _fed_by(contract, target, over_rail_id):
            # unknown voltage can't be called safe; a rail fed from the over rail
            # still draws through it, so moving a load there relieves nothing.
            continue
        if rail_unknown.get(budget.rail):
            # a rail with any consumer of unknown draw cannot be summed,
            # so it cannot be claimed to have headroom either.
            continue
        used = rail_totals.get(budget.rail, 0.0)
        headroom_w = budget.limit.base() - used
        fits = [
            (draw_w, dev)
            for dev, lo, hi, draw_w in loads
            if rail_fits(target, lo, hi) and draw_w <= headroom_w
        ]
        if fits:
            draw_w, device_id = max(fits)
            relief = (
                "clears the overload"
                if draw_w >= deficit_w
                else f"frees {draw_w:.2f} W of the {deficit_w:.2f} W over"
            )
            alternatives.append(
                Alternative(
                    text=(
                        f"move {device_id!r} to rail {budget.rail!r} "
                        f"({headroom_w:.2f} W free); {relief}"
                    ),
                    value=headroom_w,
                    unit="W",
                    kind=f"derived-by-{check_name}",
                    rail=budget.rail,
                    device=device_id,
                )
            )

    over_rail = contract.rail(over_rail_id)
    source_rail_id = "battery"
    if over_rail is not None and over_rail.upstream is not None:
        source_rail_id = over_rail.upstream
    rating_w = max(deficit_w, 0.0)
    if rating_w > 0.0:
        alternatives.append(
            Alternative(
                text=f"add a {rating_w:.2f} W regulator fed from rail {source_rail_id!r}",
                value=rating_w,
                unit="W",
                kind="projected",
                mark="projected",
                rail=over_rail_id,
            )
        )

    if not alternatives:
        alternatives.append(
            Alternative(
                text=(
                    f"no other rail has headroom for {over_rail_id!r} and no regulator "
                    "rating could be projected"
                ),
                value=0.0,
                unit="W",
                kind="none",
            )
        )
    return alternatives


#: Accepted band around the over rail's nominal voltage for a load with no declared input range.
_VOLTAGE_TOLERANCE = 0.05


def rail_fits(rail: Rail, lo: float, hi: float) -> bool:
    """True when ``rail``'s nominal voltage sits in [lo, hi]; a rail with no voltage never fits."""
    return rail.voltage is not None and lo <= rail.voltage.base() <= hi


def rails_for_voltage(contract: Contract, lo: float, hi: float) -> list[Rail]:
    """The contract's rails whose voltage fits [lo, hi] V: the one voltage test for moves and new parts."""
    rails = contract.topology.rails if contract.topology else []
    return [rail for rail in rails if rail_fits(rail, lo, hi)]


def input_range(
    low: float | None, high: float | None, nominal: float | None
) -> tuple[float, float] | None:
    """A part's accepted input band: its published min/max, else nominal +-5%, else unknown."""
    if low is not None and high is not None:
        return low, high
    if nominal is not None:
        return nominal * (1 - _VOLTAGE_TOLERANCE), nominal * (1 + _VOLTAGE_TOLERANCE)
    return None


def _movable_loads(
    contract: Contract, over_rail_id: str, draw_mode: str
) -> list[tuple[str, float, float, float]]:
    """(device id, min V, max V, draw W) for each load sitting directly on the over rail.

    A load's range is its model's ``input_voltage_range``, else the over rail's
    nominal ±5%; a load with neither is left out, so it is never offered a move."""
    over_rail = contract.rail(over_rail_id)
    nominal = over_rail.voltage.base() if over_rail and over_rail.voltage else None
    loads = []
    for device in contract.devices:
        if device.power is None or device.power.rail != over_rail_id:
            continue
        draw = next((m.draw for m in device.power.modes if m.name == draw_mode), device.power.draw)
        model = contract.device_model(device.model)
        if draw is None:
            continue
        if model is not None and model.input_voltage_range is not None:
            r = model.input_voltage_range
            loads.append((device.id, r.min.base(), r.max.base(), draw.base()))
        elif nominal is not None:
            band = nominal * _VOLTAGE_TOLERANCE
            loads.append((device.id, nominal - band, nominal + band, draw.base()))
    return loads


def _fed_by(contract: Contract, rail: Rail | None, over_rail_id: str) -> bool:
    """True when ``rail``'s upstream chain passes through ``over_rail_id``."""
    seen = {rail.id}
    while rail is not None and rail.upstream not in (None, "battery"):
        if rail.upstream == over_rail_id:
            return True
        if rail.upstream in seen:
            return False
        seen.add(rail.upstream)
        rail = contract.rail(rail.upstream)
    return False
