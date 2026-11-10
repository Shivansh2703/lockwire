"""Cited motor + prop thrust tables (thrust_tables.yaml): the maker's bench rows, nothing more.

Reading between two rows is linear and stated; reading outside the first or last row is refused
(None), never extrapolated. The cited/unverified label is the catalog's rule.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator
from ruamel.yaml import YAML

from lockwire.catalog import NumberKind, is_cited

_PATH = Path(__file__).parent / "thrust_tables.yaml"


class Row(NamedTuple):
    throttle_pct: float
    thrust_g: float
    current_a: float
    power_w: float


class ThrustTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    motor: str
    prop: str
    cells: int
    voltage_v: float
    motor_mass_g: float
    max_continuous_a: float | None
    rows: list[Row]
    kind: NumberKind
    source: str
    note: str | None = None

    @field_validator("rows")
    @classmethod
    def _rising(cls, v: list[Row]) -> list[Row]:
        if len(v) < 2 or any(b.thrust_g <= a.thrust_g for a, b in zip(v, v[1:])):
            raise ValueError("a thrust table needs two or more rows, thrust rising")
        return v

    @property
    def label(self) -> Literal["cited", "unverified"]:
        return "cited" if is_cited(self.kind, self.source) else "unverified"

    @property
    def top(self) -> Row:
        return self.rows[-1]

    def at_thrust(self, thrust_g: float) -> tuple[float, float] | None:
        """(throttle %, current A) at `thrust_g`, linear between the two rows around it;
        None outside the table (below its first row or above its last)."""
        for a, b in zip(self.rows, self.rows[1:]):
            if a.thrust_g <= thrust_g <= b.thrust_g:
                f = (thrust_g - a.thrust_g) / (b.thrust_g - a.thrust_g)
                return (
                    a.throttle_pct + f * (b.throttle_pct - a.throttle_pct),
                    a.current_a + f * (b.current_a - a.current_a),
                )
        return None


@cache
def load_thrust_tables() -> dict[str, ThrustTable]:
    data = YAML(typ="safe").load(_PATH.read_text(encoding="utf-8"))
    tables = [ThrustTable.model_validate(t) for t in data["tables"]]
    if len({t.id for t in tables}) != len(tables):
        raise ValueError("duplicate thrust table id")
    return {t.id: t for t in tables}
