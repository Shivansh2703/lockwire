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

from lockwire import schema, units

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


# Origin convention for `body` and `frames`: A part's origin is the centre of its case's bounding
# box; x runs along the case length, positive toward the end nearer the output shaft; z runs along
# the output shaft, positive toward the horn; the `horn` frame sits on the shaft axis at the horn
# mounting face with z out of the face, and a motor's `mount` frame sits on its mounting face the
# same way.
class PartBody(BaseModel):
    """A part's case bounding box, metres (length, width, height), from a maker drawing."""

    model_config = ConfigDict(extra="forbid")

    size: tuple[float, float, float]
    source: str

    @field_validator("source")
    @classmethod
    def _source_is_a_url(cls, v: str) -> str:
        if "https://" not in v:
            raise ValueError("source must contain an https:// link to the maker drawing")
        return v


class PartFrame(BaseModel):
    """A named frame on a part. xyz in metres, rpy in radians (like the lockfile's mount_xyz and
    mount_rpy); an axis the drawing does not dimension is None, never 0."""

    model_config = ConfigDict(extra="forbid")

    id: str
    xyz: tuple[float | None, float | None, float | None]
    rpy: tuple[float, float, float] | None = None
    source: str

    @field_validator("source")
    @classmethod
    def _source_is_a_url(cls, v: str) -> str:
        if "https://" not in v:
            raise ValueError("source must contain an https:// link to the maker drawing")
        return v


class HolePattern(BaseModel):
    """A part's mounting-hole spacing, metres: a grid pitch or the centre-to-centre spacing."""

    model_config = ConfigDict(extra="forbid")

    pitch: float = Field(gt=0)
    kind: Literal["grid", "center_spacing"]
    source: str

    @field_validator("source")
    @classmethod
    def _source_is_a_url(cls, v: str) -> str:
        if "https://" not in v:
            raise ValueError("source must contain an https:// link")
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
    maker: str | None = None
    bus: str | None = None  # servo bus word, e.g. feetech-sts, dynamixel-p2
    numbers: dict[str, SourcedNumber] = Field(default_factory=dict)
    note: str | None = None
    body: PartBody | None = None
    frames: list[PartFrame] = []
    hole_pattern: HolePattern | None = None

    @property
    def label(self) -> Literal["cited", "unverified"]:
        """Derived, never stored: cited = every number is a datasheet figure with a URL source
        that is not a retail listing; anything else is unverified."""
        ok = all(is_cited(n.kind, n.source) for n in self.numbers.values())
        return "cited" if ok and self.numbers else "unverified"


_STOCK_DIMENSIONS = {
    "plate": ["length", "width", "thickness"],
    "round_tube": ["outer_diameter", "wall", "length"],
    "round_rod": ["diameter", "length"],
    "square_tube": ["outer_size", "wall", "length"],
    "rect_tube": ["outer_width", "outer_height", "wall", "length"],
    "angle": ["leg_a", "leg_b", "thickness", "length"],
    "tslot": ["length"],
}


class StockShape(BaseModel):
    """A raw stock shape a person sizes by typing its dimensions (all lengths in metres, in the
    order of `dimensions`). Mass comes from the chosen material, or for a T-slot (whose drawn
    shape is only the outer box) from the maker's mass per metre."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    shape: Literal["plate", "round_tube", "round_rod", "square_tube", "rect_tube", "angle", "tslot"]
    dimensions: list[str] = []
    mass_from: Literal["material", "mass_per_length"]
    mass_per_length: SourcedNumber | None = None  # kg/m
    outer: tuple[float, float] | None = None  # metres; tslot cross-section outer box
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_dimensions(cls, v):
        if isinstance(v, dict) and not v.get("dimensions") and v.get("shape") in _STOCK_DIMENSIONS:
            v = {**v, "dimensions": _STOCK_DIMENSIONS[v["shape"]]}
        return v

    @model_validator(mode="after")
    def _mass_matches(self) -> StockShape:
        if (self.mass_from == "mass_per_length") != (self.mass_per_length is not None):
            raise ValueError(
                f"stock {self.id}: mass_per_length is required if and only if mass_from is mass_per_length"
            )
        return self


class WireRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    awg: int
    area_mm2: float
    ohm_per_m: float
    chassis_amps: float


class WireTable(BaseModel):
    """A rule-of-thumb wire table (not a standard); nothing reads it yet."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rule_of_thumb"]
    source: str
    rows: list[WireRow]


class CatalogConnectorType(schema.ConnectorType):
    """A connector type in the lockfile shape; every field it states cites an https source."""

    sources: dict[str, str]

    @model_validator(mode="after")
    def _every_field_cited(self) -> CatalogConnectorType:
        stated = {
            f
            for f in schema.ConnectorType.model_fields
            if f != "id" and getattr(self, f) is not None
        }
        for f in stated:
            if "https://" not in self.sources.get(f, "").strip():
                raise ValueError(f"connector type {self.id}: {f} needs an https source")
        if extra := set(self.sources) - stated:
            raise ValueError(
                f"connector type {self.id}: source for a field not stated: {sorted(extra)}"
            )
        return self


MechanicalKey = Literal[
    "youngs_modulus",
    "youngs_modulus_z",
    "yield_strength",
    "tensile_strength",
    "tensile_strength_z",
    "poisson_ratio",
]


class MaterialProperties(BaseModel):
    """Cited stiffness and strength of a catalog material (catalog-side; schema.Material is
    untouched). Moduli and strengths are in MPa, the printed figure kept in each source."""

    model_config = ConfigDict(extra="forbid")

    name: str
    numbers: dict[MechanicalKey, SourcedNumber]
    caveat: str | None = None


class BeltProfile(BaseModel):
    """Tooth pitch of a timing-belt profile, in metres, as the maker's catalogue prints it."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    aliases: list[str] = []
    pitch: float = Field(gt=0)
    source: str = Field(pattern=r"^https://")


class Availability(BaseModel):
    """Whether a maker still sells a part, as the maker's own page states it."""

    model_config = ConfigDict(extra="forbid")

    part: str
    status: Literal["active", "discontinued"]
    replaced_by: str | None = None
    source: str = Field(pattern=r"^https://")
    read_on: dt.date


class HoleSize(BaseModel):
    """A clearance-hole or tap-drill diameter for a metric fastener, metres, as a supplier chart
    prints it."""

    model_config = ConfigDict(extra="forbid")

    fastener: str
    fit: Literal["close", "medium", "free", "tap"]
    diameter_m: float = Field(gt=0)
    fit_class: str
    source: str = Field(pattern=r"^https://")


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[CatalogPart]
    connector_types: list[CatalogConnectorType] = []
    wire_table: WireTable | None = None
    materials: list[schema.Material] = []
    stock: list[StockShape] = []
    mechanical: list[MaterialProperties] = []
    belt_profiles: list[BeltProfile] = []
    availability: list[Availability] = []
    hole_sizes: list[HoleSize] = []

    @field_validator("hole_sizes")
    @classmethod
    def _hole_sizes_unique(cls, v: list[HoleSize]) -> list[HoleSize]:
        keys = [(h.fastener.lower(), h.fit) for h in v]
        if dup := sorted({k for k in keys if keys.count(k) > 1}):
            raise ValueError(f"duplicate hole size: {dup}")
        return v

    @model_validator(mode="after")
    def _availability_parts(self) -> Catalog:
        ids = [a.part for a in self.availability]
        if dup := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"duplicate availability record: {dup}")
        if unknown := sorted(set(ids) - {p.id for p in self.parts}):
            raise ValueError(f"availability names no part: {unknown}")
        return self

    @field_validator("belt_profiles")
    @classmethod
    def _belt_profiles_unique(cls, v: list[BeltProfile]) -> list[BeltProfile]:
        names = [b.profile.lower() for b in v]
        if dup := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate belt profile: {sorted(dup)}")
        return v

    @model_validator(mode="after")
    def _mechanical_names(self) -> Catalog:
        names = [m.name.lower() for m in self.mechanical]
        if dup := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate mechanical entry: {sorted(dup)}")
        known = {m.name.lower() for m in self.materials}
        if unknown := sorted(set(names) - known):
            raise ValueError(f"mechanical entry names no material: {unknown}")
        return self

    @field_validator("connector_types")
    @classmethod
    def _connector_ids_unique(cls, v: list[CatalogConnectorType]) -> list[CatalogConnectorType]:
        ids = [c.id for c in v]
        if dup := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"duplicate connector type id: {sorted(dup)}")
        return v

    @field_validator("materials")
    @classmethod
    def _material_names_unique(cls, v: list[schema.Material]) -> list[schema.Material]:
        names = [m.name.lower() for m in v]
        if dup := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate material name: {sorted(dup)}")
        return v

    @field_validator("stock")
    @classmethod
    def _stock_ids_unique(cls, v: list[StockShape]) -> list[StockShape]:
        ids = [s.id for s in v]
        if dup := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"duplicate stock id: {sorted(dup)}")
        return v

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


_CACHE: dict[tuple, object] = {}


def _stamp(p: Path) -> tuple | None:
    """Identity of a file on disk: path, mtime and size (None when absent)."""
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p.resolve()), st.st_mtime_ns, st.st_size)


def _cached(key: tuple, build):
    """Build once per key; hand each caller its own deep copy so nobody can corrupt the cache.
    Errors are not cached (build raises before the store)."""
    if key not in _CACHE:
        # ponytail: stale stamps (edited files) are never evicted; fine for a CLI process
        _CACHE[key] = build()
    return _CACHE[key]


def load_catalog(path: Path | str | None = None) -> Catalog:
    """Parsed once per process per file state (path, mtime, size, plus the private parts file
    for the default); every call returns its own copy."""
    p = Path(path) if path is not None else _SEED_PATH
    private = Path.home() / ".lockwire" / "parts.yaml"
    key = ("catalog", _stamp(p), _stamp(private) if path is None else None)
    return _cached(key, lambda: _load_catalog(path)).model_copy(deep=True)


def materials() -> list[schema.Material]:
    """The cited material densities, each pasteable into a lockfile as is."""
    return load_catalog().materials


def stock() -> list[StockShape]:
    """The stock shapes a person can size by typing dimensions."""
    return load_catalog().stock


def structure_parts() -> list[CatalogPart]:
    """Kit channels, plates and standoffs (family fam.structure.*), apart from Components."""
    return [p for p in load_catalog().parts if p.family.startswith("fam.structure.")]


def material(name: str) -> schema.Material | None:
    """Exact name, any case; an unknown name is None."""
    return next((m for m in materials() if m.name.lower() == name.lower()), None)


def material_properties(name: str) -> MaterialProperties | None:
    """Cited stiffness and strength for a material; exact name, any case; unknown is None."""
    return next((m for m in load_catalog().mechanical if m.name.lower() == name.lower()), None)


def belt_pitch(profile: str) -> BeltProfile | None:
    """Tooth pitch row for a belt profile or alias, any case; unknown is None."""
    want = profile.lower()
    return next(
        (
            b
            for b in load_catalog().belt_profiles
            if want in [b.profile.lower(), *(a.lower() for a in b.aliases)]
        ),
        None,
    )


_FIT_ALIASES = {"clearance": "medium", "normal": "medium", "loose": "free", "tap drill": "tap"}


def hole_size(fastener: str, fit: str) -> dict | None:
    """Hole diameter for a metric size ("M3", any case) and fit: close | medium | free | tap
    (aliases: clearance, normal = medium; loose = free; tap drill = tap). Unknown is None."""
    fit = _FIT_ALIASES.get(fit.lower(), fit.lower())
    row = next(
        (
            h
            for h in load_catalog().hole_sizes
            if h.fastener.lower() == fastener.lower() and h.fit == fit
        ),
        None,
    )
    return row.model_dump(include={"diameter_m", "fit_class", "source"}) if row else None


def availability(part_id: str) -> dict | None:
    """Maker-stated status of a part; unknown id is None; no record reads "not checked"."""
    cat = load_catalog()
    if part_id not in {p.id for p in cat.parts}:
        return None
    rec = next((a for a in cat.availability if a.part == part_id), None)
    if rec is None:
        return {"status": "not checked", "replaced_by": None, "source": None, "read_on": None}
    return rec.model_dump(include={"status", "replaced_by", "source", "read_on"})


def _load_catalog(path: Path | str | None = None) -> Catalog:
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
RobotQuantity = Literal[
    "mass_total", "dof", "reach_m", "payload_kg", "joint_speed_max", "joint_torque_max"
]


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
    """The robot rows (default: the ones shipped with lockwire); parsed once per file state."""
    key = ("robots", _stamp(Path(path or _ROBOTS_PATH)))
    return [r.model_copy(deep=True) for r in _cached(key, lambda: _load_robots(path))]


def _load_robots(path: Path | str | None = None) -> list[Robot]:
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


# ---------------------------------------------------------------- pinout cards
_PINOUTS_DIR = Path(__file__).parent / "pinouts"


class PinoutConnector(schema.Connector):
    """A connector on a pinout card, plus the pin table it was read from."""

    source: str  # "<url> (<doc title/table>)"

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a pinout connector must name the pin table it was read from")
        return v


class PinoutCard(BaseModel):
    """One board or driver: sourced limits, connectors and their pins. Not a claim about a robot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    maker: str
    numbers: dict[str, SourcedNumber] = Field(default_factory=dict)
    connectors: list[PinoutConnector] = Field(default_factory=list)
    note: str | None = None


def load_pinouts(folder: Path | str | None = None) -> list[PinoutCard]:
    """One card per `*.yaml` in `folder` (default: the pinouts shipped with lockwire); parsed
    once per folder state."""
    files = sorted(Path(folder if folder is not None else _PINOUTS_DIR).glob("*.yaml"))
    key = ("pinouts", tuple(_stamp(f) for f in files))
    return [c.model_copy(deep=True) for c in _cached(key, lambda: _load_pinouts(files))]


def _load_pinouts(files: list[Path]) -> list[PinoutCard]:
    yaml = YAML(typ="safe")
    return [PinoutCard.model_validate(yaml.load(f.read_text(encoding="utf-8"))) for f in files]
