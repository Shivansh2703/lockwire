"""The parts catalog — a library of parts a user might pick from.

Deliberately NOT part of the contract schema (`schema.py`, frozen). A contract is a
declared, witnessed statement about the robot you have; a catalog is a library of parts you might
use to build one. They answer different questions and don't share a lifecycle — a catalog entry
changes when a vendor revises a datasheet, a contract entry changes when this robot's build
changes. Keeping the catalog its own model in its own file means the freeze on `schema.py` is
never touched: no field here is a contract field, so there is nothing to validate against
the contract schema.

Every number on a catalog part carries a `kind` and a `source`: a real
number quotes where it came from; a placeholder says plainly that it is a placeholder. Nothing on
a catalog part is presented as a real number without one of these two badges — an unlabelled
number is a bug in the seed data, not a permitted state (see `_source_not_blank` below and
`tests/test_catalog.py::test_every_seeded_number_has_a_kind_and_a_source`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from lockwire import units

_SEED_PATH = Path(__file__).parent / "catalog_seed.yaml"
_ROBOTS_PATH = Path(__file__).parent / "catalog_robots.yaml"

# "datasheet": a manufacturer datasheet, spec page or product-listing figure, cited by URL/name.
# "source_file": a real number read out of a file already in this repo (e.g. a vendor URDF's
#   <mass> tag) — real, but the citation is a file, not a published datasheet.
# "assumed": fabricated for seeding, no real part backs it; the source string must say so.
NumberKind = Literal["datasheet", "source_file", "assumed"]


def is_cited(kind: str, source: str) -> bool:
    """The cited rule: a datasheet figure whose source names a URL that is not a retail listing."""
    return (
        kind == "datasheet"
        and bool(re.search(r"https?://", source))
        and "retail" not in source.lower()
    )


class SourcedNumber(BaseModel):
    """One number on a catalog part, badged with where it came from."""

    model_config = ConfigDict(extra="forbid")

    value: float
    unit: str
    kind: NumberKind
    source: str

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source must not be blank — a number without a source is not allowed")
        return v


class CatalogPart(BaseModel):
    """One entry in the catalog: a part a user might pick, not a claim about a robot they have.

    Reconciliation with `schema.py`'s `DeviceModel`: two vocabularies for "a part"
    now exist and they are deliberately not one. `CatalogPart` is the SHOPPING vocabulary — a part
    you might pick, sourced numbers, no robot attached. `DeviceModel` is the REGISTRY vocabulary —
    checks, hard limits and known failure modes for a device on a robot you actually have. They
    share only `id` and `family`; nothing else lines up because the two answer different
    questions and don't share a lifecycle (see the module docstring). A consumer that needs both
    must go through one door (the one-door rule), not read both models directly — `grep -rln
    CatalogPart src/lockwire/ tests/` names every module that may reference this class, and
    `test_no_module_straddles_catalog_and_registry_vocabularies` below fails the day that set
    grows to include a module that also references `DeviceModel`. Merging the two, if ever wanted,
    is a `schema.py` change and therefore a maintainer's call — not a refactor to do
    on a reading of "decide the reconciliation."
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    family: str
    name: str
    bus: str | None = None  # servo bus word, e.g. feetech-sts, dynamixel-p2
    numbers: dict[str, SourcedNumber] = Field(default_factory=dict)
    note: str | None = None

    @property
    def label(self) -> Literal["cited", "unverified"]:
        """Derived, never stored: cited = every number is a datasheet figure with a URL source
        that is not a retail listing; anything else is unverified."""
        ok = all(is_cited(n.kind, n.source) for n in self.numbers.values())
        return "cited" if ok and self.numbers else "unverified"


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[CatalogPart]

    @field_validator("parts")
    @classmethod
    def _ids_unique(cls, v: list[CatalogPart]) -> list[CatalogPart]:
        seen: set[str] = set()
        for part in v:
            if part.id in seen:
                raise ValueError(f"duplicate catalog part id: {part.id}")
            seen.add(part.id)
        return v


# Units Part Swap and the servo check read without converting: a part must state them so.
_FIXED_UNITS = {
    "stall_torque": "N*m",
    "stall_torque_voltage": "V",
    "input_voltage_min": "V",
    "input_voltage_max": "V",
}


class PrivatePartsError(ValueError):
    """A parts file is broken; the message starts with its path."""


def _refuse_bad_units(file: Path, part: dict) -> None:
    numbers = part.get("numbers")
    if not isinstance(numbers, dict):
        return
    for key, unit in _FIXED_UNITS.items():  # read as-is by Part Swap and the servo check
        got = numbers.get(key)
        if isinstance(got, dict) and got.get("unit") != unit:
            raise PrivatePartsError(
                f"{file}: part {part.get('id')!r} numbers.{key}.unit is "
                f"{got.get('unit')!r}; write it in {unit}"
                + (" (1 kg*cm = 0.0980665 N*m)" if unit == "N*m" else "")
            )
    mass = numbers.get("mass")  # Part Swap converts mass to kg
    if isinstance(mass, dict) and not (
        units.known(str(mass.get("unit"))) and units.dimension(mass["unit"]) == "mass"
    ):
        raise PrivatePartsError(
            f"{file}: part {part.get('id')!r} numbers.mass.unit is {mass.get('unit')!r}; "
            "write it in kg or g"
        )


def load_catalog(path: Path | str | None = None) -> Catalog:
    """Read the catalog from `path` (default: the seeded catalog shipped with lockwire).

    The default also merges the user's private parts from ~/.lockwire/parts.yaml when present;
    an explicit `path` reads only that file.
    """
    yaml = YAML(typ="safe")
    p = Path(path) if path is not None else _SEED_PATH
    try:
        data = yaml.load(p.read_text(encoding="utf-8"))
    except (YAMLError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        raise PrivatePartsError(
            f"{path or p} is not a parts catalog; pass a catalog .yaml, or leave it off for the bundled one"
        )
    for part in data.get("parts", []):
        if isinstance(part, dict):
            _refuse_bad_units(p, part)
    private = Path.home() / ".lockwire" / "parts.yaml"
    if path is None and private.is_file():
        try:
            mine = yaml.load(private.read_text(encoding="utf-8"))
        except (OSError, YAMLError) as error:
            raise PrivatePartsError(
                f"{private}: not valid YAML: {str(error).splitlines()[0]}"
            ) from error
        if not isinstance(mine, list):
            raise PrivatePartsError(f"{private}: must be a list of parts")
        public_ids = {part["id"] for part in data["parts"]}
        for n, part in enumerate(mine, 1):
            if not isinstance(part, dict) or "id" not in part:
                raise PrivatePartsError(f"{private}: entry {n} has no id")
            if part["id"] in public_ids:
                raise PrivatePartsError(f"{private}: part {part['id']!r} reuses a public id")
            _refuse_bad_units(private, part)
        try:
            return Catalog.model_validate({**data, "parts": [*data["parts"], *mine]})
        except ValidationError as error:
            first = error.errors()[0]
            loc, where = first["loc"][1:], ""  # ("parts", index, field, ...)
            if loc and isinstance(loc[0], int):
                part_id = mine[loc[0] - len(data["parts"])]["id"]
                where = f"part {part_id!r} {'.'.join(map(str, loc[1:])) or 'entry'}: "
            raise PrivatePartsError(f"{private}: {where}{first['msg']}") from error
    return Catalog.model_validate(data)


# What a whole-robot sheet's mass figure includes; a URDF often models less.
Scope = Literal["with_battery", "dry", "with_gripper", "with_cable", "arm_only"]
RobotQuantity = Literal["mass_total", "dof", "reach_m", "payload_kg", "joint_speed_max", "joint_torque_max"]


class RobotNumber(SourcedNumber):
    """A robot-row number: a vendor datasheet figure, URL first, with the day it was read."""

    scope: Scope | None = None
    retrieved: dt.date

    @field_validator("kind")
    @classmethod
    def _datasheet_only(cls, v: str) -> str:
        if v != "datasheet":
            raise ValueError("robot rows take datasheet numbers only")
        return v

    @field_validator("source")
    @classmethod
    def _url_first(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("a robot-row source starts with the https:// URL that was opened")
        return v

    @property
    def url(self) -> str:
        return self.source.split()[0] if self.source.startswith("https://") else self.source


class Robot(BaseModel):
    """One whole robot and what its vendor publishes about it (catalog_robots.yaml)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    variant: str | None = None
    aliases: list[str]
    fingerprint: list[str]
    numbers: dict[RobotQuantity, RobotNumber]
    known_urdf_deviations: list[str] = []

    @model_validator(mode="after")
    def _required(self) -> Robot:
        mass = self.numbers.get("mass_total")
        if mass is None or mass.scope is None:
            raise ValueError(f"{self.id}: mass_total (with a scope) is required")
        return self


def load_robots(path: Path | str | None = None) -> list[Robot]:
    """The robot rows (default: the ones shipped with lockwire)."""
    data = YAML(typ="safe").load(Path(path or _ROBOTS_PATH).read_text(encoding="utf-8"))
    robots = [Robot.model_validate(row) for row in data["robots"]]
    if len({r.id for r in robots}) != len(robots):
        raise ValueError("duplicate robot id in the catalog")
    return robots


def recognise(name: str, links: Iterable[str], model: str | None = None) -> Robot | None:
    """`model` (a row id) wins; else an exact alias of the URDF's robot name; else the one row
    whose link names overlap >= 80 % (Jaccard); else None. An unknown `model` is refused."""
    robots = load_robots()
    if model is not None:
        for robot in robots:
            if robot.id == model:
                return robot
        known = ", ".join(r.id for r in robots)
        raise ValueError(f"no robot {model!r} in the catalog; known: {known}")
    for robot in robots:
        if name.lower() in (a.lower() for a in robot.aliases):
            return robot
    have = set(links)
    scored = sorted(
        ((len(have & set(r.fingerprint)) / len(have | set(r.fingerprint)), r) for r in robots),
        key=lambda pair: -pair[0],
    )
    best, runner_up = scored[0][0], scored[1][0] if len(scored) > 1 else 0.0
    return scored[0][1] if best >= 0.8 and runner_up < best else None
