"""Design-rule presets by robot type.

A preset only supplies a default limit where a rule has none of its own (today: a wire gauge the
cited NEC/PowerStream tables do not cover). It never tightens a cited limit, and a number
without a read source is advice (``source is None``), so it can never produce a NO-GO.

The robot kind is read from ``robot.id`` of a shipped starter; any other id gets no preset.
"""

from __future__ import annotations

from dataclasses import dataclass

from lockwire.schema import Contract

NOT_SOURCED = "not yet sourced"


@dataclass(frozen=True)
class Preset:
    name: str
    fuse_max_a_by_awg: dict[int, float]  # fallback fuse ceiling for gauges the tables lack
    source: str | None = None  # None: advice, "not yet sourced"


PRESETS: dict[str, Preset] = {
    "rover": Preset("rover", {}),
    "arm": Preset("arm", {}),
    "drone": Preset("drone", {}),  # drone limits: not yet sourced
    # FRC numbers (120 A main, 40 A to 12 AWG) came from a research note that read the wrong
    # manual page; the game manual was not read, so they stay unsourced advice.
    "frc": Preset("frc", {12: 40.0}),
}

# Starter robot.id -> preset name (the starters' ids are the only kind marker the contract has).
_STARTER_KIND = {
    "turtlebot3-burger": "rover",
    "rover-param": "rover",
    "so101": "arm",
    "x500-v2-px4": "drone",
}

# CLI wiring (lane A2): `presets.override = preset` from a --preset option.
override: str | None = None


def choose_preset(c: Contract, explicit: str | None = None) -> Preset | None:
    """The preset for this contract: ``explicit`` (or the CLI override) wins, else by starter."""
    name = explicit or override or _STARTER_KIND.get(c.robot.id or "")
    if name is not None and name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose one of {', '.join(sorted(PRESETS))}")
    return PRESETS.get(name) if name else None
