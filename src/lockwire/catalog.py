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

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ruamel.yaml import YAML

_SEED_PATH = Path(__file__).parent / "catalog_seed.yaml"

# "datasheet": a manufacturer datasheet, spec page or product-listing figure, cited by URL/name.
# "source_file": a real number read out of a file already in this repo (e.g. a vendor URDF's
#   <mass> tag) — real, but the citation is a file, not a published datasheet.
# "assumed": fabricated for seeding, no real part backs it; the source string must say so.
NumberKind = Literal["datasheet", "source_file", "assumed"]


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
    numbers: dict[str, SourcedNumber] = Field(default_factory=dict)
    note: str | None = None


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


def load_catalog(path: Path | str | None = None) -> Catalog:
    """Read the catalog from `path` (default: the seeded catalog shipped with lockwire)."""
    yaml = YAML(typ="safe")
    p = Path(path) if path is not None else _SEED_PATH
    data = yaml.load(p.read_text())
    return Catalog.model_validate(data)
