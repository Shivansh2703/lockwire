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

**Raising the over-budget rail's own declared capacity is never offered**
: that clears the finding by moving the limit rather than
the draw, which leaves a standing mark. Offering it as
advice would make the product recommend the exact thing that mark exists to flag.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from lockwire.schema import Contract


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
) -> list[Alternative]:
    """Alternatives for a rail_power budget over its limit on ``over_rail_id``.

    ``rail_totals``/``rail_unknown`` must come from ``checks.power_rollup`` run
    against the same contract and draw mode as the violation being explained —
    the shared builder that every call site (and every mutation bite) goes
    through, so a broken kind anywhere breaks every caller alike.
    """
    alternatives: list[Alternative] = []
    for budget in contract.budgets:
        if budget.kind != "rail_power" or budget.rail in (None, over_rail_id):
            continue
        if rail_unknown.get(budget.rail):
            # a rail with any consumer of unknown draw cannot be summed,
            # so it cannot be claimed to have headroom either.
            continue
        used = rail_totals.get(budget.rail, 0.0)
        headroom_w = budget.limit.base() - used
        if headroom_w > 0.0:
            alternatives.append(
                Alternative(
                    text=(
                        f"move the device to rail {budget.rail!r}, which has "
                        f"{headroom_w:.2f} W free"
                    ),
                    value=headroom_w,
                    unit="W",
                    kind=f"derived-by-{check_name}",
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
