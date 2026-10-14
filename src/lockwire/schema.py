"""Contract schema v0.4 — the physics lockfile.

One YAML file, checked into the robot's repo, recording everything the software
silently assumes about the physical system. Structure follows spec v3:

- Tier A  model:        per-link mass/COM/principal inertia + structure hash,
                        generated from URDF by `lockwire init`
- Tier B  edges:        declared quantity -> code dependency edges
- Tier C  assumptions:  tracked-but-uncheckable register (surfaced, never verified)
- Tier D  devices:      family -> model -> instance split; instances carry
                        identity, driver, physical, power, context envelopes
- budgets:              system-level derived entries (checker sums witnesses)
- topology:             hosts / buses / rails / wires / battery

Invariants enforced here rather than by convention:
- every physical quantity is a Quantity{value, unit}; unknown units fail
  validation; frame-dependent quantities carry a frame
- unknown keys are rejected (extra="forbid") — a typo is a violation, not a
  silently ignored field
- locked entries carry a Provenance block

Cross-entry references use '/'-separated paths ("devices/<id>/physical/mass"),
never dots — device ids themselves contain dots ("dev.imu").
"""

import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML

import numpy as np

from lockwire import units

SCHEMA_VERSION = "0.4"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"0.2", "0.3", SCHEMA_VERSION})
FITTED_BAND_ALPHA_GATE = 0.005

CONTEXTS = (
    "bench-idle",
    "armed-idle",
    "nominal-op",
)  # start with 3 max (spec: context proliferation trap)

# Real checked-in lockfiles are a few KB (fixtures ≈ 4–13 KiB; largest ≈ 11 KiB).
# 256 KiB is ~23× headroom over that, while bounding worst-case ruamel
# flow-collection parse to ~5s (board-measured ~19s/MB). The old 2 MiB
# ceiling did not bound that super-linear path (~1.8 MiB flow list hung).
# Path.stat (no read) still rejects oversized input before the scanner runs.
MAX_CONTRACT_BYTES = 256 * 1024

# lockwire lockfiles are declarative records and need no YAML anchors/aliases.
# After the byte cap and a cheap ruamel load (aliases stay as shared refs),
# the composed-tree walk rejects any node that carries anchor metadata or
# appears twice via shared identity. Mutation bites flip this to False to
# prove the hang returns when the walk is disabled.
REJECT_YAML_ANCHORS_ALIASES = True


class Base(BaseModel):
    """Closed, assignment-validated base for every lockfile record.

    Attribute assignment reruns pydantic validation, so programmatically built
    records retain the same field guarantees as loaded records. In-place
    mutations such as ``contract.devices.append(...)`` are not assignments and
    therefore cannot trigger pydantic; call ``revalidate`` after such edits.
    Rebuilding these small lockfile documents is negligible compared with robot
    enumeration or file I/O.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    def revalidate(self) -> Self:
        """Return a fully revalidated copy, including in-place container edits."""

        return type(self).model_validate(self.model_dump(mode="python"))


class Quantity(Base):
    value: float
    unit: str
    frame: str | None = None

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, v: str) -> str:
        if not units.known(v):
            raise ValueError(f"unknown unit {v!r}")
        return v

    def base(self) -> float:
        return units.to_base(self.value, self.unit)

    def dimension(self) -> str:
        return units.dimension(self.unit)

    def __str__(self) -> str:
        return f"{self.value:g} {self.unit}"


def _quantity_dimension(
    expected: str,
    role: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
    finite: bool = False,
):
    """Build a reusable Quantity validator for a physical field's role."""

    def validate(value: Quantity) -> Quantity:
        actual = value.dimension()
        if actual != expected:
            raise ValueError(
                f"{role} requires dimension {expected!r}, got {actual!r} ({value.unit!r})"
            )
        if nonnegative and value.base() < 0:
            raise ValueError(f"{role} must be >= 0, got {value}")
        if positive and value.base() <= 0:
            raise ValueError(f"{role} must be > 0, got {value}")
        if finite and not math.isfinite(value.base()):
            raise ValueError(f"{role} must be finite, got {value}")
        return value

    return validate


PowerQuantity = Annotated[Quantity, AfterValidator(_quantity_dimension("power", "power quantity"))]
MassQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("mass", "mass quantity", nonnegative=True))
]
EnergyQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("energy", "energy quantity"))
]
TimeQuantity = Annotated[Quantity, AfterValidator(_quantity_dimension("time", "time quantity"))]
VoltageQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("voltage", "voltage quantity"))
]
CurrentQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("current", "current quantity"))
]
ResistanceQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("resistance", "resistance quantity"))
]
LengthQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("length", "length quantity"))
]
TemperatureQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("temperature", "temperature quantity"))
]
ThermalResistanceQuantity = Annotated[
    Quantity,
    AfterValidator(_quantity_dimension("thermal_resistance", "thermal resistance quantity")),
]

PowerLevelQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("power_level", "RF power-level quantity"))
]
GainQuantity = Annotated[Quantity, AfterValidator(_quantity_dimension("gain", "RF gain quantity"))]
DataRateQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("data_rate", "data-rate quantity"))
]
# Carrier frequency uses the existing rate/Hz unit — the registry has no MHz spelling.
FrequencyQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("rate", "carrier-frequency quantity"))
]
AngleQuantity = Annotated[Quantity, AfterValidator(_quantity_dimension("angle", "angle quantity"))]
TorqueQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("torque", "torque quantity"))
]

# Structural/simulation v1 roles deliberately remain distinct even where the
# unit table maps them to the same physical dimension. This keeps, for example,
# an elastic modulus from silently serving as a contact pressure parameter.
AreaQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("area", "area", positive=True, finite=True))
]
SecondMomentQuantity = Annotated[
    Quantity,
    AfterValidator(_quantity_dimension("second_moment", "second moment", positive=True, finite=True)),
]
PressureQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("pressure", "pressure", finite=True))
]
PositivePressureQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("pressure", "pressure", positive=True, finite=True))
]
DensityQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("density", "density", positive=True, finite=True))
]
ForceQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("force", "force", finite=True))
]
PositiveLengthQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("length", "length", positive=True, finite=True))
]
PositiveTimeQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("time", "time", positive=True, finite=True))
]
LinearStiffnessQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("linear_stiffness", "translational stiffness", positive=True, finite=True)
    ),
]
RotationalStiffnessQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("rotational_stiffness", "rotational stiffness", positive=True, finite=True)
    ),
]
LinearDampingQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("linear_damping", "translational damping", positive=True, finite=True)
    ),
]
RotationalDampingQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("rotational_damping", "rotational damping", positive=True, finite=True)
    ),
]
ContactStiffnessQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("contact_stiffness", "contact slope", positive=True, finite=True)
    ),
]
ContactDampingQuantity = Annotated[
    Quantity,
    AfterValidator(
        _quantity_dimension("contact_damping", "contact damping", positive=True, finite=True)
    ),
]
InverseTimeQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("inverse_time", "Rayleigh alpha", nonnegative=True, finite=True))
]
VelocityQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("velocity", "linear velocity", finite=True))
]
AngularRateQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("angular_rate", "angular velocity", finite=True))
]
AngularAccelerationQuantity = Annotated[
    Quantity,
    AfterValidator(_quantity_dimension("angular_acceleration", "angular acceleration", finite=True)),
]
InertiaQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("inertia", "rotational inertia", finite=True))
]
AccelerationQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("acceleration", "acceleration", finite=True))
]
DimensionlessQuantity = Annotated[
    Quantity, AfterValidator(_quantity_dimension("dimensionless", "dimensionless quantity", finite=True))
]


DeclaredEnforcement = Literal["fuse", "firmware", "driver", "this file"]


class Band(Base):
    """Closed interval in a declared unit."""

    lo: float
    hi: float
    unit: str
    # `why` (<=512 chars) and `max_rate` (units/s) are part of the schema.
    # Both are optional and additive: SCHEMA_VERSION does not move and every
    # checked-in fixture still loads. `why` is quoted back verbatim wherever a
    # bound is crossed (checks, plan, preflight sanity) and printed by
    # `describe`. `max_rate` is the limit's rate of change in <unit>/s; scoring it
    # is another module's job, this entry only declares it.
    why: str | None = Field(default=None, max_length=512)
    max_rate: float | None = Field(default=None, gt=0)
    enforced_by: DeclaredEnforcement | None = None

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, v: str) -> str:
        if not units.known(v):
            raise ValueError(f"unknown unit {v!r}")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> "Band":
        if self.lo > self.hi:
            raise ValueError(f"band lo {self.lo} > hi {self.hi}")
        return self

    def contains(self, other: "Band") -> bool:
        lo = units.convert(other.lo, other.unit, self.unit)
        hi = units.convert(other.hi, other.unit, self.unit)
        return self.lo <= lo and hi <= self.hi

    @property
    def why_suffix(self) -> str:
        """The declared reason, quoted verbatim, or nothing when undeclared.

        One helper so every refusal path -- ``checks``, ``plan`` and
        preflight's ``sanity`` stage -- quotes the same text.
        """
        return f" (why: {self.why})" if self.why else ""

    def __str__(self) -> str:
        return f"[{self.lo:g}, {self.hi:g}] {self.unit}{self.why_suffix}"


class ReviewNote(Base):
    """A reviewer's judgement of a declared value, written into the file.

    `state` is exactly one of these two words. The witness gate is the
    *existing* `validate_witness` (edit.py) — imported lazily inside the
    validator to avoid a module-level cycle (edit.py imports from schema.py).
    """

    state: Literal["approved", "questioned"]
    witness: str
    at: dt.date  # required: a review without a date is not a record
    note: str | None = None

    @field_validator("witness")
    @classmethod
    def _validated_witness(cls, v: str) -> str:
        from lockwire.edit import validate_witness

        validate_witness(
            v, refusal='a review witness must not be blank, "you", or an assistant name'
        )
        return v

    @model_validator(mode="after")
    def _questioned_requires_a_note(self) -> "ReviewNote":
        if self.state == "questioned" and not (self.note and self.note.strip()):
            raise ValueError("a questioned review note requires a note")
        return self


class Provenance(Base):
    witness: str  # w0..w6 | live | human
    source: str  # file path, datasheet, bag id
    locked_at: dt.date
    bag_sha256: str | None = None  # keep raw bags; re-deriving later must be possible
    conditions: dict[str, float] | None = None  # env channel -> value at lock time
    # Free text for a human reader. No checker reads it: prose never moves a
    # verdict, and the MCP surface hands it to an agent fenced as untrusted data
    # (tests/test_prose_quarantine.py).
    note: str | None = None
    # Catalog-part slice: the seeded part whose declared mass was
    # copied. Optional and additive so existing lockfiles remain valid. Family
    # references deliberately remain unresolved until their semantics are set.
    compared_from: str | None = None
    # a reviewer's approve/question notes on this declared value.
    # Optional so a contract with none is written back with the key
    # absent, not `reviews: []` -- exclude_none in save_contract then drops it.
    reviews: list[ReviewNote] | None = None


class ArtifactRef(Base):
    """A content-addressed file below a caller-selected evidence root."""

    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def _contained_relative_path(cls, value: str) -> str:
        candidate = Path(value)
        if (
            not value
            or candidate.is_absolute()
            or "://" in value
            or ":" in value
            or "\\" in value
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError("artifact path must be a non-empty contained relative path")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
        return value


def _artifact_path(ref: ArtifactRef, root: Path, *, max_bytes: int) -> Path:
    """Return a contained, regular artifact path after its cheap checks."""

    if max_bytes <= 0:
        raise ValueError("artifact size limit must be positive")
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / ref.path).resolve(strict=True)
    if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
        raise ValueError(f"artifact {ref.path!r} escapes or is not a file below the evidence root")
    if candidate.stat().st_size > max_bytes:
        raise ValueError(f"artifact {ref.path!r} exceeds size limit ({max_bytes} bytes)")
    return candidate


def read_artifact(ref: ArtifactRef, root: Path, *, max_bytes: int = MAX_CONTRACT_BYTES) -> bytes:
    """Read and hash-verify one contained local artifact's exact bytes."""

    candidate = _artifact_path(ref, root, max_bytes=max_bytes)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"artifact {ref.path!r} exceeds size limit ({max_bytes} bytes)")
            digest.update(block)
            chunks.append(block)
    if digest.hexdigest() != ref.sha256:
        raise ValueError(
            f"artifact {ref.path!r} sha256 does not match: "
            f"observed {digest.hexdigest()}, expected {ref.sha256}"
        )
    return b"".join(chunks)


# ---------------------------------------------------------------- Tier A: model


class PrincipalInertia(Base):
    """Principal moments + axes, never raw tensor elements (spec: frame trap)."""

    moments_kgm2: tuple[float, float, float]  # ascending
    axes: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]  # rows = unit eigenvectors in the link inertial frame (URDF: about COM)


class Link(Base):
    name: str
    mass: MassQuantity | None = None  # None -> zero-mass, flagged at check time
    com: tuple[float, float, float] | None = None  # meters, in `frame`
    frame: str | None = None
    inertia: PrincipalInertia | None = None


class JointOrigin(Base):
    """Joint pose in its parent link frame; consumed by URDF drift."""

    xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)


class JointLimits(Base):
    """URDF-native SI limits consumed by dependency edges and the torque budget."""

    lower: float | None = None  # radians or meters, according to joint type
    upper: float | None = None  # radians or meters, according to joint type
    effort: float | None = None  # N*m for rotary joints, N for prismatic joints
    velocity: float | None = None  # rad/s for rotary joints, m/s for prismatic joints
    lower_enforced_by: DeclaredEnforcement | None = None
    upper_enforced_by: DeclaredEnforcement | None = None
    effort_enforced_by: DeclaredEnforcement | None = None
    velocity_enforced_by: DeclaredEnforcement | None = None

    @property
    def effort_limit(self) -> float | None:
        """The effort as a hard limit; URDF effort <= 0 means unset/invalid, not a limit."""
        return self.effort if self.effort is not None and self.effort > 0 else None

    @model_validator(mode="after")
    def _ordered(self) -> "JointLimits":
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError(f"joint limit lower {self.lower} > upper {self.upper}")
        for limit in ("lower", "upper", "effort", "velocity"):
            if getattr(self, f"{limit}_enforced_by") is not None and getattr(self, limit) is None:
                raise ValueError(f"{limit}_enforced_by requires {limit}")
        return self


class Joint(Base):
    """Kinematic joint consumed by joint-limit dependency edges and URDF drift."""

    name: str
    type: Literal["fixed", "revolute", "continuous", "prismatic", "floating", "planar"]
    parent: str
    child: str
    origin: JointOrigin = JointOrigin()
    axis: tuple[float, float, float] | None = None
    limits: JointLimits | None = None


def _model_structure_hash(links: list[Link], joints: list[Joint]) -> str:
    """Canonical v0.2 hash; axis and limits are load-bearing joint structure."""

    payload = {
        "links": sorted(link.name for link in links),
        "joints": sorted(
            (joint.model_dump(mode="json", exclude_none=True) for joint in joints),
            key=lambda joint: joint["name"],
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ModelRecord(Base):
    source: str
    source_sha256: str
    structure_hash: str  # derived from links+joints when joints are recorded
    # Exact bytes for external URDF/SDF mesh files, relative to the contract.
    # A selected model with mesh geometry refuses an undeclared or changed file.
    external_assets: list[ArtifactRef] = []
    links: list[Link] = []
    joints: list[Joint] = []  # dependency edges target limits; URDF drift consumes the full row
    zero_mass_links: list[str] = []
    provenance: Provenance

    @model_validator(mode="after")
    def _unique_names(self) -> "ModelRecord":
        for kind, rows in (("link", self.links), ("joint", self.joints)):
            names = [row.name for row in rows]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise ValueError(f"duplicate {kind} name(s): {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _unique_external_assets(self) -> "ModelRecord":
        paths = [asset.path for asset in self.external_assets]
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            raise ValueError(f"duplicate external asset path(s): {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _derive_structure_hash(self) -> "ModelRecord":
        # v0.1 init records have no joints and keep their historical URDF hash. Once
        # joints are present, there is one source of truth: the recorded structure.
        if self.joints:
            # This derived write lives inside the model validator. Normal assignment
            # here would recursively invoke this validator under validate_assignment.
            object.__setattr__(
                self,
                "structure_hash",
                _model_structure_hash(self.links, self.joints),
            )
        return self


# ---------------------------------------------------------------- Tier B: edges


class Tolerance(Base):
    rel_pct: float | None = None
    abs: Quantity | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Tolerance":
        if (self.rel_pct is None) == (self.abs is None):
            raise ValueError("tolerance needs exactly one of rel_pct / abs")
        return self


class DependencyEdge(Base):
    quantity: str  # contract path, e.g. "devices/dev.imu/physical/mass"
    # SCORED: shape is validated (`Tolerance._exactly_one`) and
    # an observed value IS compared against this band -- `checks.py::check_edge_tolerance`
    # reads `.tolerance` and is joined into `run_record_checks`. This field was
    # previously declared-for-future (validated but unscored); it is now
    # scored. If `checks.py` ever stops reading `.tolerance`, update this comment back
    # (see tests/test_schema.py::test_tolerance_is_scored_by_checks).
    tolerance: Tolerance
    code: list[str]  # repo paths that assume this quantity
    owner: str

    @field_validator("code")
    @classmethod
    def _nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("edge needs at least one code path")
        return v


# ---------------------------------------------------------------- Tier C: register


class Assumption(Base):
    """Named, owned, last-reviewed. A list, never a verifier."""

    id: str
    statement: str
    owner: str
    last_reviewed: dt.date


# ---------------------------------------------------------------- Tier D: devices


class DeviceFamily(Base):
    """Device-class defaults consumed by baseline checks; provenance supports report citation."""

    id: str  # e.g. "fam.imu.mems"
    name: str
    min_capture_window_s: float | None = None  # 1 Hz GPS needs minutes, not 30 s
    notes: str | None = None
    provenance: Provenance | None = None


class Actuator(Base):
    """Datasheet facts inherited by instances and consumed by the torque budget.

    Scalars use SI: torque_constant in N*m/A and torque ratings in N*m. These
    units are fixed because the deliberately-small unit registry has no torque
    dimension yet; making the convention explicit prevents unitless ambiguity.
    """

    gear_ratio: float | None = None
    torque_constant: float | None = None
    torque_continuous: float | None = None
    torque_peak: float | None = None
    encoder_cpr: int | None = None

    @field_validator(
        "gear_ratio",
        "torque_constant",
        "torque_continuous",
        "torque_peak",
        "encoder_cpr",
    )
    @classmethod
    def _positive(cls, v: float | int | None) -> float | int | None:
        if v is not None and v <= 0:
            raise ValueError(f"actuator values must be positive, got {v}")
        return v

    @model_validator(mode="after")
    def _peak_at_least_continuous(self) -> "Actuator":
        if (
            self.torque_peak is not None
            and self.torque_continuous is not None
            and self.torque_peak < self.torque_continuous
        ):
            raise ValueError(
                f"torque_peak ({self.torque_peak}) must be >= "
                f"torque_continuous ({self.torque_continuous})"
            )
        return self


class InputVoltageRange(Base):
    """Device input limits consumed by the voltage-compatibility check."""

    min: VoltageQuantity
    max: VoltageQuantity
    min_enforced_by: DeclaredEnforcement | None = None
    max_enforced_by: DeclaredEnforcement | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "InputVoltageRange":
        if self.min.base() > self.max.base():
            raise ValueError(f"input voltage range min {self.min} > max {self.max}")
        return self


class ThermalLimit(Base):
    """Published junction trip point consumed by the thermal-budget check.

    ``kind`` names the trip: ``throttle`` is the actionable gate; ``shutdown``
    is the hard stop and may appear as finding context, not the fire threshold.
    For Jetson Orin-class modules the published operand is the composite
    T.SoC junction (equal-ratio CPU/GPU/SoC) — no discrete per-zone trip
    points are published. ``provenance`` is required: a limit without a named
    published source is a hand-typed guess.
    """

    temperature: TemperatureQuantity
    kind: Literal["throttle", "shutdown"]
    provenance: Provenance
    temperature_enforced_by: DeclaredEnforcement | None = None


class Radio(Base):
    """Telemetry-radio datasheet facts consumed by the link-budget check.

    RX sensitivity is rate-dependent (e.g. RFD900x 10⁻⁵ BER table); ``air_rate``
    binds the sensitivity figure — a sensitivity without its rate is meaningless.
    Carrier frequency is an instance/link-budget fact (selected channel), not a
    model default: an undeclared frequency is ``not_scored``, never an assumed
    mid-band channel.
    """

    tx_power: PowerLevelQuantity  # dBm
    rx_sensitivity: PowerLevelQuantity  # dBm at air_rate
    air_rate: DataRateQuantity  # bit/s (or kbit/s / Mbit/s)

    @field_validator("air_rate")
    @classmethod
    def _positive_air_rate(cls, v: Quantity) -> Quantity:
        if v.base() <= 0:
            raise ValueError(f"air_rate must be positive, got {v}")
        return v


class AntennaGain(Base):
    """Installed antenna gain with its own datasheet provenance.

    The radio datasheet may publish a monopole figure without binding it to a
    SKU; the contract must declare the antenna. The check never assumes a
    bundled antenna.
    """

    gain: GainQuantity  # dBi
    provenance: Provenance


class LinkBudget(Base):
    """Mission RF requirements and installed antennas for one telemetry radio.

    Consumed by ``check_link_budget``. Missing carrier frequency or either
    antenna emits ``not_scored`` rather than inventing a channel or gain.

    ``installation_loss`` is mandatory: real links carry
    feedline, connector, and miscellaneous cable loss (typically 1–3 dB). A
    silent default of zero would report an optimistic margin — the wrong
    direction on a check whose job is catching inadequate links. Omission
    fails validation; an author who knows their run has 3 dB of feedline
    loss declares it here.
    """

    required_range: LengthQuantity
    fade_margin_required: GainQuantity  # dB
    fade_margin_required_enforced_by: DeclaredEnforcement | None = None
    installation_loss: GainQuantity  # dB; feedline + connector + misc — no default
    carrier_frequency: FrequencyQuantity | None = None  # Hz; None -> not_scored
    tx_antenna: AntennaGain | None = None
    rx_antenna: AntennaGain | None = None

    @field_validator("required_range")
    @classmethod
    def _positive_range(cls, v: Quantity) -> Quantity:
        if v.base() <= 0:
            raise ValueError(f"required_range must be positive, got {v}")
        return v

    @field_validator("fade_margin_required")
    @classmethod
    def _nonnegative_margin(cls, v: Quantity) -> Quantity:
        if v.base() < 0:
            raise ValueError(f"fade_margin_required must be >= 0, got {v}")
        return v

    @field_validator("installation_loss")
    @classmethod
    def _nonnegative_installation_loss(cls, v: Quantity) -> Quantity:
        if v.base() < 0:
            raise ValueError(f"installation_loss must be >= 0, got {v}")
        return v

    @field_validator("carrier_frequency")
    @classmethod
    def _positive_frequency(cls, v: Quantity | None) -> Quantity | None:
        if v is not None and v.base() <= 0:
            raise ValueError(f"carrier_frequency must be positive, got {v}")
        return v


class DeviceModel(Base):
    """Shareable registry artifact: checks, limits, failure modes. Never local config.

    Invariant (day one, before any registry exists): model artifacts are
    DECLARATIVE DATA — envelopes, limits, failure-mode strings. Never code,
    templates-with-logic, or anything evaluated at check time. The checker is
    the only code that runs; a community-published model must not be able to
    execute anything on robot infrastructure.
    """

    id: str  # e.g. "mdl.imu.bmi088"
    family: str  # -> DeviceFamily.id
    match: dict[str, str] = {}  # stable identity descriptors (vid_pid, can_id, ...)
    hard_limits: dict[str, Band] = {}  # channel -> absolute rating (hand-typed, sparse)
    known_failure_modes: list[str] = []
    actuator: Actuator | None = None  # datasheet operands consumed by the torque budget
    input_voltage_range: InputVoltageRange | None = None
    provenance: Provenance | None = None
    thermal_limit: ThermalLimit | None = None  # datasheet trip; thermal-budget check
    radio: Radio | None = None  # datasheet RF operands consumed by the link-budget check


IdentityStrength = Literal["instance", "model", "positional"]


class DeviceIdentity(Base):
    """Stable descriptors only — never port or enumeration order (spec: USB re-enum trap).

    `bus_ref` is consumed by reference integrity, bandwidth budgeting, and the
    bus identity-collision check.

    Identity strength varies by bus — the gate must know how
    much "the same device is present" can mean here:
    - instance:   a per-unit id exists (DroneCAN 128-bit UID, GigE MAC, 1-Wire ROM,
                  a genuine USB iSerialNumber)
    - model:      only part-level descriptors (VID:PID, WHO_AM_I, CANopen vendor+product;
                  USB clones ship duplicate serials — override to "model" if suspect)
    - positional: identity is really wiring position (I2C address straps, mux channel)
    `strength` overrides; otherwise derived from which descriptors are present.
    """

    bus_ref: str  # -> Bus.id; reference integrity, bandwidth, identity-collision checks
    host: str  # -> Host.id
    vid_pid: str | None = None
    serial_number: str | None = None
    can_id: str | None = None
    address: str | None = None
    strength: IdentityStrength | None = None

    def effective_strength(self) -> IdentityStrength:
        if self.strength is not None:
            return self.strength
        if self.serial_number is not None:
            return "instance"
        if self.vid_pid is not None or self.can_id is not None:
            return "model"
        return "positional"


class SignalSource(Base):
    """Where one lockwire signal lives inside a ROS message, and in what unit.

    `field` is a dotted path into the message (`linear_acceleration.z`).

    **`unit` is required and never inferred** — type it. A `sensor_msgs/msg/Imu` linear acceleration is m/s² by REP 145
    and could be derived from the message type, and that is exactly the shortcut
    this refuses: an inferred unit is a number nobody typed and nobody checked.
    Unit handling has already produced three defects. Adding
    inference as a fallback re-opens all of them.
    """

    field: str
    unit: str

    @field_validator("field")
    @classmethod
    def _nonempty_field(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("signal field path must not be blank")
        return v

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, v: str) -> str:
        # Same gate every other unit in the contract passes through (see Quantity).
        if not units.known(v):
            raise ValueError(f"unknown unit {v!r}")
        return v


class DriverProcedure(Base):
    """A discrete reset/watchdog/update marker observed on a declared signal.

    This is evidence that a procedure marker arrived at the capture subscriber;
    it does not command a device or imply that the device performed an action.
    """

    id: str
    kind: Literal["reset", "watchdog", "update"]
    signal: str
    code: Annotated[int, Field(strict=True)]

    @field_validator("id", "signal")
    @classmethod
    def _nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("driver procedure id and signal must not be blank")
        return value

    @field_validator("code")
    @classmethod
    def _nonnegative_code(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("driver procedure code must be a nonnegative integer")
        return value


class Driver(Base):
    """Runtime feed metadata; rate × payload is consumed by the bandwidth budget."""

    node: str | None = None
    topic: str | None = None
    # A watched write channel is distinct from this driver's measurement topic.
    # The owning DeviceInstance's identity.bus_ref remains the physical topology
    # binding; no second bus or logic-pair association is declared here.
    command_topic: str | None = None
    nominal_rate_hz: float | None = None
    dds_deadline_s: float | None = None  # opt-in ROS 2 requested deadline, not packet loss
    payload_bytes: int | None = None  # bandwidth-budget summand
    # ROS 2 capture operands. Optional and read by nothing in the check
    # path — `lockwire_ros`'s capture node is the only consumer, so a contract
    # without them behaves exactly as it did before schema 0.3 gained them.
    # ULog could hardcode its topic->signal map (`ulog.py`) because PX4's
    # vocabulary is fixed; ROS 2 topics are named per robot, so the map has to
    # live in the contract.
    msg_type: str | None = None  # e.g. "sensor_msgs/msg/Imu"
    signals: dict[str, SignalSource] = {}  # lockwire signal name -> where it lives
    procedures: list[DriverProcedure] = []  # observed reset/watchdog/update markers

    @field_validator("dds_deadline_s")
    @classmethod
    def _positive_dds_deadline(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or v < 1e-9):
            raise ValueError("dds_deadline_s must be finite and at least 1 ns")
        if v is not None:
            nanoseconds = Decimal(str(v)) * Decimal("1e9")
            if nanoseconds != nanoseconds.to_integral_value():
                raise ValueError("dds_deadline_s must be an integral number of nanoseconds")
        return v

    @field_validator("payload_bytes")
    @classmethod
    def _positive_payload(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(f"payload_bytes must be positive, got {v}")
        return v

    @field_validator("command_topic")
    @classmethod
    def _nonblank_command_topic(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("command_topic must not be blank")
        return v

    @model_validator(mode="after")
    def _signals_need_a_message_type(self) -> "Driver":
        """A field path means nothing without the message it indexes into."""
        if self.signals and self.msg_type is None:
            raise ValueError(
                f"driver declares signals {sorted(self.signals)} but no msg_type; "
                "a field path cannot be resolved without the message type"
            )
        procedure_ids = [procedure.id for procedure in self.procedures]
        if len(set(procedure_ids)) != len(procedure_ids):
            raise ValueError("driver procedure IDs must be unique")
        missing = sorted({procedure.signal for procedure in self.procedures} - set(self.signals))
        if missing:
            raise ValueError(
                f"driver procedures name undeclared signals {missing}; "
                "a procedure marker must use a declared signal"
            )
        return self


class MountShape(Base):
    """Small mounting geometry beside a device's declared offset."""

    kind: Literal["plate", "bracket", "tube", "custom-mesh"]
    dimensions_mm: tuple[float, float, float]
    fastener_pattern: str | None = None
    mesh_path: str | None = None

    @field_validator("dimensions_mm")
    @classmethod
    def _positive_dimensions(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(not math.isfinite(dimension) or dimension <= 0 for dimension in value):
            raise ValueError("mount_shape dimensions_mm must be three positive finite numbers")
        return value

    @field_validator("fastener_pattern")
    @classmethod
    def _nonblank_fastener_pattern(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("mount_shape fastener_pattern must not be blank")
        return value

    @model_validator(mode="after")
    def _mesh_matches_kind(self) -> "MountShape":
        if self.kind == "custom-mesh":
            if not self.mesh_path or not self.mesh_path.strip():
                raise ValueError("custom-mesh mount_shape requires mesh_path")
            from lockwire.edit import source_is_absolute

            if source_is_absolute(self.mesh_path):
                raise ValueError("custom-mesh mesh_path must be non-absolute")
        elif self.mesh_path is not None:
            raise ValueError("mesh_path is only valid for a custom-mesh mount_shape")
        return self


class Physical(Base):
    """Installed facts consumed by mass budgets, reference integrity, and CG reporting."""

    mass: MassQuantity | None = None  # absent -> not scored, never summed as 0
    mount_frame: str | None = None  # absent -> not scored, never crashes a consumer
    mount_xyz: tuple[LengthQuantity, LengthQuantity, LengthQuantity] | None = None
    """Device offset in ``mount_frame``; consumed by severity-free CG reporting."""
    mount_shape: MountShape | None = None

    @model_validator(mode="after")
    def _mount_xyz_needs_a_frame(self) -> "Physical":
        if self.mount_xyz is not None and self.mount_frame is None:
            raise ValueError("mount_xyz requires mount_frame")
        return self


class PowerMode(Base):
    """Named alternate draw consumed by power-budget mode selection."""

    name: str
    draw: PowerQuantity

    @field_validator("name")
    @classmethod
    def _nonempty_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("power mode name must not be blank")
        return v


class PowerDraw(Base):
    """Nominal draw plus sparse named overrides consumed by power budgets."""

    rail: str  # -> Rail.id
    draw: PowerQuantity | None = None  # absent -> not scored, never summed as 0
    modes: list[PowerMode] = []
    choices: list[str] | None = None  # optional allowed power-mode names

    @model_validator(mode="after")
    def _unique_mode_names(self) -> "PowerDraw":
        names = [mode.name for mode in self.modes]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate power mode name(s): {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _modes_need_a_fallback_draw(self) -> "PowerDraw":
        if self.modes and self.draw is None:
            raise ValueError("power modes require a nominal 'draw' as their fallback")
        return self

    @model_validator(mode="after")
    def _choices_are_declared_modes(self) -> "PowerDraw":
        if self.choices is None:
            return self
        if not self.choices or any(not choice.strip() for choice in self.choices):
            raise ValueError("power choices must not be empty or blank")
        duplicates = sorted({choice for choice in self.choices if self.choices.count(choice) > 1})
        if duplicates:
            raise ValueError(f"duplicate power choice(s): {', '.join(duplicates)}")
        declared = {"nominal", *(mode.name for mode in self.modes)}
        unknown = sorted(set(self.choices) - declared)
        if unknown:
            raise ValueError("power choices must name declared modes: " + ", ".join(unknown))
        return self


class EnvelopeEvidence(Base):
    """Wilks evidence consumed by `baseline review` and under-sampling reporting."""

    n: int | None = None
    n_eff: float | None = None
    coverage: float | None = None
    confidence: float | None = None

    @model_validator(mode="after")
    def _valid_evidence(self) -> "EnvelopeEvidence":
        if self.n is not None and self.n <= 0:
            raise ValueError(f"n must be positive, got {self.n}")
        if self.n_eff is not None and self.n_eff <= 0:
            raise ValueError(f"n_eff must be positive, got {self.n_eff}")
        if self.n is not None and self.n_eff is not None and self.n_eff > self.n:
            raise ValueError(f"n_eff {self.n_eff} cannot exceed n {self.n}")
        if (self.coverage is None) != (self.confidence is None):
            raise ValueError("coverage and confidence must be provided together")
        for name, value in (("coverage", self.coverage), ("confidence", self.confidence)):
            if value is not None and not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        return self


class SignalEnvelope(EnvelopeEvidence):
    """Wilks signal envelope consumed by baseline review and preflight."""

    band: Band
    variance_floor: float | None = None  # CUSUM reference value
    # Stage-3 one-shot floor (preflight ``sanity``): the capture's minimum
    # sliding-window population variance must be >= this value. Not the CUSUM
    # reference — that is ``variance_floor`` (docs/stats_design.md);
    # baseline/CUSUM must not read this field.
    oneshot_variance_min: float | None = None


class FittedBandEvidence(Base):
    """Locked operands for the §5 fitted-band decision."""

    rank: int
    n_eff: int
    alpha: float

    @field_validator("rank", "n_eff", mode="before")
    @classmethod
    def _strict_positive_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("rank and n_eff must be positive integers")
        return value

    @model_validator(mode="after")
    def _valid_fitted_evidence(self) -> "FittedBandEvidence":
        if self.rank > self.n_eff:
            raise ValueError(f"rank {self.rank} cannot exceed fitted n_eff {self.n_eff}")
        if not math.isfinite(self.alpha) or not 0.0 < self.alpha <= 1.0:
            raise ValueError(f"alpha must be finite and in (0, 1], got {self.alpha}")
        return self


class ContextEnvelope(EnvelopeEvidence):
    """Wilks context envelope consumed by baseline review and preflight."""

    context: str
    rate_hz: Band | None = None
    startup_ms_p95: float | None = None
    signals: dict[str, SignalEnvelope] = {}
    conditions: dict[str, Band] = {}  # env channel -> range actually baselined (condition envelope)
    fitted_bands: dict[str, FittedBandEvidence] = {}
    provenance: Provenance

    @field_validator("context")
    @classmethod
    def _known_context(cls, v: str) -> str:
        if v not in CONTEXTS:
            raise ValueError(f"unknown context {v!r}; allowed: {CONTEXTS}")
        return v

    @model_validator(mode="after")
    def _valid_fitted_band_paths(self) -> "ContextEnvelope":
        declared = set()
        if self.rate_hz is not None:
            declared.add("rate_hz")
        declared.update(f"signals/{name}" for name in self.signals)
        declared.update(f"conditions/{name}" for name in self.conditions)
        unknown = sorted(set(self.fitted_bands) - declared)
        if unknown:
            raise ValueError("fitted_bands refers to undeclared band(s): " + ", ".join(unknown))
        if self.fitted_bands:
            if self.provenance.witness != "live":
                raise ValueError("fitted_bands requires provenance witness 'live'")
            if self.provenance.bag_sha256 is None:
                raise ValueError("fitted_bands requires provenance bag_sha256")
        return self


class FittedBandAllocation(Base):
    """One fit-time cluster allocation and its fitted inventory."""

    context: str
    channels: list[str]
    fitted_bands: list[str] = []
    denominator: int

    @field_validator("context")
    @classmethod
    def _known_context(cls, value: str) -> str:
        if value not in CONTEXTS:
            raise ValueError(f"unknown context {value!r}; allowed: {CONTEXTS}")
        return value

    @field_validator("denominator", mode="before")
    @classmethod
    def _strict_positive_denominator(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("denominator must be a positive integer")
        return value

    @model_validator(mode="after")
    def _valid_inventory(self) -> "FittedBandAllocation":
        if not self.channels:
            raise ValueError("allocation channels must not be empty")
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("allocation channels must be unique")
        if len(self.fitted_bands) != len(set(self.fitted_bands)):
            raise ValueError("allocation fitted_bands must be unique")
        if any(not value for value in self.channels):
            raise ValueError("allocation channel ids must not be empty")
        if any(not value for value in self.fitted_bands):
            raise ValueError("allocation fitted-band paths must not be empty")
        return self


class DeviceThermal(Base):
    """Installed thermal path consumed by the thermal-budget check.

    ``resistance`` is published enclosure/heatsink RθJA (junction-to-ambient)
    from the contract author's datasheet; ``ambient`` is the declared
    operating air temperature. Declared per-device: it does not account for
    co-located dissipation, so a device packed beside hot neighbours can run
    hotter than this figure implies. Either may be absent — the check emits
    ``not_scored`` naming the gap rather than guessing a resistance.
    """

    resistance: ThermalResistanceQuantity | None = None
    ambient: TemperatureQuantity | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _resistance_needs_provenance(self) -> "DeviceThermal":
        if self.resistance is not None and self.provenance is None:
            raise ValueError(
                "thermal resistance requires provenance (enclosure/heatsink datasheet)"
            )
        return self


class DeviceInstance(Base):
    """Local facts; identity is optional for devices that do not sit on a data bus.

    When present, `identity` is consumed by reference integrity, bandwidth
    budgeting, and identity checks. `drives_joint` pairs torque-budget model
    and joint operands.
    """

    id: str  # e.g. "dev.imu"
    model: str  # -> DeviceModel.id
    drives_joint: str | None = None  # -> ModelRecord.joints.name; torque-budget pairing
    identity: DeviceIdentity | None = None
    driver: Driver | None = None
    physical: Physical | None = None
    power: PowerDraw | None = None
    envelopes: list[ContextEnvelope] = []
    depends: list[str] = []  # code paths (sugar for Tier B edges on this device)
    owner: str
    provenance: Provenance | None = None
    thermal: DeviceThermal | None = None  # installed RθJA + ambient; thermal-budget check
    link_budget: LinkBudget | None = None  # mission RF requirements + installed antennas


# ---------------------------------------------------------------- topology & budgets


class Host(Base):
    """Topology host consumed by reference integrity; provenance supports report citation."""

    id: str
    notes: str | None = None
    provenance: Provenance | None = None


class LogicPair(Base):
    id: str
    driver: str
    receiver: str
    operating_supply_min: VoltageQuantity | None = None
    operating_supply_max: VoltageQuantity | None = None
    voh_min: VoltageQuantity | None = None
    vol_max: VoltageQuantity | None = None
    vih_min: VoltageQuantity | None = None
    vil_max: VoltageQuantity | None = None
    provenance: Provenance | None = None


class I2CSegment(Base):
    unbuffered: bool | None = None
    mode: str | None = None
    vdd_min_v: float | None = None
    vdd_max_v: float | None = None
    pullup_min_ohm: float | None = None
    pullup_max_ohm: float | None = None
    capacitance_max_pf: float | None = None
    rise_time_max_ns: float | None = None
    sink_current_min_ma: float | None = None
    vol_max_v: float | None = None
    provenance: Provenance | None = None


class CANSegment(Base):
    linear: bool | None = None
    nodes: list[str] = []
    termination_nodes: list[str] = []
    termination_ohm: list[float] = []
    cable_impedance_ohm: float | None = None
    impedance_tolerance_ohm: float | None = None
    provenance: Provenance | None = None


class Bus(Base):
    """Physical bus; bitrate feeds bandwidth checks and provenance supports report citation."""

    id: str
    kind: Literal["usb", "ethernet", "serial", "can", "i2c", "spi"]
    host: str  # -> Host.id
    bitrate: float | None = None  # raw bits/s consumed by the bandwidth budget
    provenance: Provenance | None = None
    i2c_segment: I2CSegment | None = None
    can_segment: CANSegment | None = None

    @field_validator("bitrate")
    @classmethod
    def _positive_bitrate(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"bitrate must be positive, got {v}")
        return v


class Rail(Base):
    """Electrical rail consumed by power checks; provenance supports report citation."""

    id: str
    voltage: VoltageQuantity | None = None  # absent -> not scored, never crashes a consumer
    voltage_range: InputVoltageRange | None = None  # published min/max V, optional
    current_limit: CurrentQuantity | None = None  # published max A, optional
    capacity: PowerQuantity  # power the rail can deliver
    current_limit_enforced_by: DeclaredEnforcement | None = None
    capacity_enforced_by: DeclaredEnforcement | None = None
    upstream: str | None = None  # -> parent Rail.id or "battery"; rail-tree rollup
    efficiency: float = 1.0  # converter efficiency consumed by rail-tree rollup
    charged_by: str | None = None  # -> Device.id of an alternator/generator feeding this rail
    provenance: Provenance | None = None

    @field_validator("efficiency")
    @classmethod
    def _valid_efficiency(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"efficiency must be in (0, 1], got {v}")
        return v

    @model_validator(mode="after")
    def _voltage_within_range(self) -> "Rail":
        if self.current_limit_enforced_by is not None and self.current_limit is None:
            raise ValueError("current_limit_enforced_by requires current_limit")
        if self.voltage is not None and self.voltage_range is not None:
            v = self.voltage.base()
            lo, hi = self.voltage_range.min.base(), self.voltage_range.max.base()
            if not (lo <= v <= hi):
                raise ValueError(
                    f"rail voltage {self.voltage} outside declared voltage_range "
                    f"{self.voltage_range.min}-{self.voltage_range.max}"
                )
        return self


class CapacityDerating(Base):
    """Usable-capacity factor at or below a pack temperature.

    Cold is the dominant endurance threat for Li-ion (Peukert/C-rate ≈ negligible):
    ~20-35% capacity loss at 0 C, 40%+ below -10 C, plus IR-driven sag cutoffs.
    Declared rows make the endurance budget condition-aware.
    """

    below_c: float  # applies at pack temperatures <= this
    capacity_factor: float  # usable fraction of nominal capacity

    @field_validator("capacity_factor")
    @classmethod
    def _sane_factor(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"capacity_factor must be in (0, 1], got {v}")
        return v


class Battery(Base):
    """Pack facts consumed by endurance and rail-voltage consistency checks."""

    capacity: EnergyQuantity  # energy (nominal, at room temperature)
    voltage: VoltageQuantity | None = None  # rail-voltage consistency; endurance interpretation
    derating: list[CapacityDerating] = []


class Wire(Base):
    """Power conductor; ampacity check compares published AWG limit to rail draw.

    ``installation`` is mandatory: free-air and enclosed ampacities differ
    2–3× in published tables, so a silent default would report a confident wrong
    answer. AWG is a gauge index (not a Quantity); ampacity current is the
    **worst** rolled-up rail watts across nominal and every declared device
    ``PowerMode``, divided by rail volts — never a silently-defaulted single
    mode. ``draw_mode`` is retained for Budget-parity / explicit
    documentation but is not the ampacity selection path.
    """

    id: str
    awg: int  # American Wire Gauge (positive integer sizes; e.g. 16)
    installation: Literal["free_air", "enclosed"]  # no default — required at validation
    rail: str  # -> Rail.id; computed draw is the rolled-up load on this rail
    draw_mode: str = "nominal"  # retained; ampacity evaluates worst declared mode
    owner: str
    provenance: Provenance | None = None

    @field_validator("awg")
    @classmethod
    def _positive_awg(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"awg must be a positive gauge number, got {v}")
        return v

    @field_validator("draw_mode")
    @classmethod
    def _nonempty_draw_mode(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("wire draw_mode must not be blank")
        return v


class Topology(Base):
    hosts: list[Host] = []
    buses: list[Bus] = []
    logic_pairs: list[LogicPair] = []
    rails: list[Rail] = []
    wires: list[Wire] = []
    battery: Battery | None = None


class Budget(Base):
    """Derived entry consumed by budget checks; provenance supports report citation."""

    id: str
    kind: Literal["rail_power", "endurance", "mass_total"]
    limit: Quantity  # dimension is selected by kind below
    limit_enforced_by: DeclaredEnforcement | None = None
    rail: str | None = None  # rail_power only
    draw_mode: str = "nominal"  # named device-power override selected by power budget checks
    sources: Literal["devices", "links", "links+devices"] = "devices"  # mass_total: what to
    # sum — explicit so double-counting a device that also appears in the URDF is a declared
    # choice, not an accident
    owner: str
    provenance: Provenance | None = None

    @field_validator("draw_mode")
    @classmethod
    def _nonempty_draw_mode(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("budget draw_mode must not be blank")
        return v

    @model_validator(mode="after")
    def _rail_iff_rail_power(self) -> "Budget":
        if (self.kind == "rail_power") != (self.rail is not None):
            raise ValueError("rail must be set exactly when kind == rail_power")
        expected = {
            "rail_power": "power",
            "endurance": "time",
            "mass_total": "mass",
        }[self.kind]
        _quantity_dimension(expected, f"{self.kind} budget limit")(self.limit)
        return self


class RobotInfo(Base):
    name: str
    id: str | None = None


EvidenceClass = Literal["measured", "synthetic"]


class ActuatorSignalBinding(Base):
    """One named signal the selected actuator case consumes."""

    signal: str
    scope: Literal["command", "observation"]
    unit: Literal["rad", "rad/s", "N*m", "V", "A"]
    frame: str | None = None
    provenance_class: EvidenceClass

    @field_validator("signal")
    @classmethod
    def _nonempty_signal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actuator signal binding must name a signal")
        return value


class ActuatorEventBinding(Base):
    event: str
    scope: Literal["command", "observation"]
    provenance_class: EvidenceClass

    @field_validator("event")
    @classmethod
    def _nonempty_event(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actuator event binding must name an event")
        return value


class IndependentLoadBinding(Base):
    """The independently calibrated load witness; never a command-derived torque."""

    signal: str
    scope: Literal["observation"]
    unit: Literal["N*m"]
    joint: str
    frame: str
    sign: Literal[-1, 1]
    application_point: tuple[float, float, float]
    axis: tuple[float, float, float]
    applicability: Literal["static", "dynamic"]
    provenance_class: Literal["measured"]

    @field_validator("signal", "joint", "frame")
    @classmethod
    def _nonempty_load_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("independent load binding fields must not be blank")
        return value


class ActuatorCaseBindings(Base):
    command_position: ActuatorSignalBinding
    command_velocity: ActuatorSignalBinding
    command_torque: ActuatorSignalBinding
    measured_position: ActuatorSignalBinding
    measured_joint_torque: ActuatorSignalBinding
    receiver_voltage: ActuatorSignalBinding | None = None
    command_issued_event: ActuatorEventBinding
    feedback_event: ActuatorEventBinding
    independent_load: IndependentLoadBinding
    source_voltage: ActuatorSignalBinding | None = None
    load_current: ActuatorSignalBinding | None = None

    @model_validator(mode="after")
    def _roles_are_explicit(self) -> "ActuatorCaseBindings":
        expected = {
            "command_position": ("command", "rad"),
            "command_velocity": ("command", "rad/s"),
            "command_torque": ("command", "N*m"),
            "measured_position": ("observation", "rad"),
            "measured_joint_torque": ("observation", "N*m"),
            "receiver_voltage": ("observation", "V"),
        }
        for name, (scope, unit) in expected.items():
            row = getattr(self, name)
            if row is None:
                continue
            if row.scope != scope or row.unit != unit:
                raise ValueError(f"{name} requires scope {scope!r} and unit {unit!r}")
        if self.measured_position.frame is None or self.measured_joint_torque.frame is None:
            raise ValueError(
                "measured position and joint torque bindings require an explicit frame"
            )
        if (
            self.command_issued_event.scope != "command"
            or self.feedback_event.scope != "observation"
        ):
            raise ValueError(
                "command_issued_event and feedback_event require command/observation scope"
            )
        for name, unit in (("source_voltage", "V"), ("load_current", "A")):
            row = getattr(self, name)
            if row is not None and (row.scope != "observation" or row.unit != unit):
                raise ValueError(f"{name} requires scope 'observation' and unit {unit!r}")
        return self


class DCRailDeliveryTarget(Base):
    """Declared worst-case operands for one steady, single-series DC path."""

    applicability: Literal["single_series_dc_steady"]
    condition_id: str
    source_node: str
    receiver_node: str
    return_node: str
    source_voltage_min: VoltageQuantity
    source_resistance_max: ResistanceQuantity
    outbound_resistance_max: ResistanceQuantity
    return_resistance_max: ResistanceQuantity
    contact_resistance_max: ResistanceQuantity
    peak_current_max: CurrentQuantity
    provenance_artifacts: list[ArtifactRef]

    @field_validator("condition_id", "source_node", "receiver_node", "return_node")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DC delivery condition and nodes must not be blank")
        return value

    @model_validator(mode="after")
    def _physical_operands(self) -> "DCRailDeliveryTarget":
        if self.source_voltage_min.base() <= 0 or self.peak_current_max.base() <= 0:
            raise ValueError("DC delivery source voltage and peak current must be positive")
        for name in (
            "source_resistance_max",
            "outbound_resistance_max",
            "return_resistance_max",
            "contact_resistance_max",
        ):
            if getattr(self, name).base() < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not self.provenance_artifacts:
            raise ValueError("DC delivery requires at least one provenance artifact")
        return self


class TransientDCEvidenceBinding(Base):
    """Opt-in join between one actuator response and its measured DC evidence.

    This does not reinterpret ``DCRailDeliveryTarget``'s steady single-series
    calculation. It names the measured channel/part/supply identity, common
    mapped time basis, and case-specific transient limits instead.
    """

    applicability: Literal["actuator_command_response_transient"] | None = None
    channel: str | None = None
    part: str | None = None
    supply_rail: str | None = None
    source_node: str | None = None
    receiver_node: str | None = None
    return_node: str | None = None
    time_basis_clock_domain: str | None = None
    time_basis_epoch: str | None = None
    voltage_min: VoltageQuantity | None = None
    current_max: CurrentQuantity | None = None
    response_latency_max: TimeQuantity | None = None
    evidence_artifact: ArtifactRef | None = None
    provenance_artifacts: list[ArtifactRef] = Field(default_factory=list)

    @field_validator(
        "channel", "part", "supply_rail", "source_node", "receiver_node", "return_node",
        "time_basis_clock_domain", "time_basis_epoch",
    )
    @classmethod
    def _nonempty_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("transient DC binding identity fields must not be blank")
        return value

    @model_validator(mode="after")
    def _positive_limits_and_provenance(self) -> "TransientDCEvidenceBinding":
        if any(limit is not None and limit.base() <= 0 for limit in (
            self.voltage_min, self.current_max, self.response_latency_max
        )):
            raise ValueError("transient DC voltage, current, and latency limits must be positive")
        return self


class ActuatorCase(Base):
    """The small v0.4 actuator proof's declared operands, not a universal model."""

    id: str
    actuator_device: str
    joint: str
    rail: str
    load_torque: TorqueQuantity  # design input only; never combined with command torque
    load_applicability: Literal["static", "dynamic"]
    position_tracking_max_error: AngleQuantity
    feedback_deadline_max: TimeQuantity
    clock_alignment_max_error: TimeQuantity
    prediction_position_rmse_max: AngleQuantity
    prediction_position_peak_max: AngleQuantity
    model_input_manifest: ArtifactRef
    parameter_artifacts: list[ArtifactRef] = []
    calibration_artifacts: list[ArtifactRef] = []
    clock_mapping_artifacts: list[ArtifactRef]
    bindings: ActuatorCaseBindings
    dc_delivery: DCRailDeliveryTarget | None = None
    transient_dc: TransientDCEvidenceBinding | None = None
    transient_dc_agreement_required: bool | None = None
    feedback_loss_max: Annotated[int, Field(strict=True, ge=0)] | None = None

    @field_validator("id", "actuator_device", "joint", "rail")
    @classmethod
    def _nonempty_case_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actuator case identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def _positive_limits_and_matching_load(self) -> "ActuatorCase":
        for name in (
            "position_tracking_max_error",
            "feedback_deadline_max",
            "clock_alignment_max_error",
            "prediction_position_rmse_max",
            "prediction_position_peak_max",
        ):
            if getattr(self, name).base() <= 0:
                raise ValueError(f"{name} must be positive")
        if self.bindings.independent_load.joint != self.joint:
            raise ValueError("independent load binding must name the actuator case joint")
        if self.bindings.independent_load.applicability != self.load_applicability:
            raise ValueError("independent load applicability must match the declared load context")
        if not self.clock_mapping_artifacts:
            raise ValueError("actuator cases require at least one declared clock mapping artifact")
        hashes = [self.model_input_manifest.sha256]
        hashes += [
            item.sha256
            for item in (
                self.parameter_artifacts + self.calibration_artifacts + self.clock_mapping_artifacts
            )
        ]
        if self.dc_delivery is not None:
            hashes += [item.sha256 for item in self.dc_delivery.provenance_artifacts]
        if self.transient_dc is not None:
            if (
                self.transient_dc.part is not None
                and self.transient_dc.part != self.actuator_device
            ):
                raise ValueError("transient DC part must match actuator_device")
            if (
                self.transient_dc.supply_rail is not None
                and self.transient_dc.supply_rail != self.rail
            ):
                raise ValueError("transient DC supply_rail must match actuator case rail")
            if self.transient_dc.evidence_artifact is not None:
                hashes += [self.transient_dc.evidence_artifact.sha256]
            hashes += [item.sha256 for item in self.transient_dc.provenance_artifacts]
        if len(hashes) != len(set(hashes)):
            raise ValueError("actuator case artifact hashes must be distinct")
        return self


# ------------------------------------------------ structural + simulation v1


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("identifier must not be blank")
    return value


def _unique_row_ids(label: str, rows: list[Any]) -> None:
    ids = [row.id for row in rows]
    duplicates = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate id(s) in {label}: {', '.join(duplicates)}")


class VectorQuantity(Base):
    """A framed physical vector; its semantic consumer validates the role dimension."""

    values: tuple[float, float, float]
    unit: str
    frame: str

    @field_validator("values")
    @classmethod
    def _finite_values(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("vector values must be finite")
        return value

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, value: str) -> str:
        if not units.known(value):
            raise ValueError(f"unknown unit {value!r}")
        return value

    @field_validator("frame")
    @classmethod
    def _frame_not_blank(cls, value: str) -> str:
        return _nonblank(value)

    def require_dimension(self, expected: str, role: str) -> None:
        if units.dimension(self.unit) != expected:
            raise ValueError(f"{role} requires dimension {expected!r}, got {self.unit!r}")


class FrameInput(Base):
    id: str
    parent: str
    translation: VectorQuantity
    rotation_xyzw: tuple[float, float, float, float]
    provenance: Provenance

    @field_validator("id", "parent")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _valid_transform(self) -> "FrameInput":
        self.translation.require_dimension("length", "frame translation")
        if self.translation.frame != self.parent:
            raise ValueError("frame translation must be expressed in its parent frame")
        if not all(math.isfinite(component) for component in self.rotation_xyzw):
            raise ValueError("frame quaternion must be finite")
        norm = math.sqrt(sum(component * component for component in self.rotation_xyzw))
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("frame quaternion must be normalized")
        return self


class InputArtifact(Base):
    role: str
    ref: ArtifactRef
    format: Literal[
        "urdf",
        "sdf",
        "calculix_inp",
        "mesh",
        "json",
        "yaml",
        "csv",
        "world",
        "controller_config",
    ]
    provenance: Provenance

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        return _nonblank(value)


class RuntimeSelection(Base):
    id: str
    package_profile: str
    max_wall_time: PositiveTimeQuantity
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("package_profile")
    @classmethod
    def _safe_manifest_key(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("package_profile must be a safe manifest key")
        return value


class StructuralModelBinding(Base):
    source_sha256: str
    structure_hash: str
    source_frame: str
    model_artifact: InputArtifact | None = None
    mesh_artifact: InputArtifact | None = None
    provenance: Provenance

    @field_validator("source_sha256", "structure_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("model binding hashes must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("source_frame")
    @classmethod
    def _source_frame(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _artifact_binding(self) -> "StructuralModelBinding":
        if self.model_artifact is not None and self.model_artifact.ref.sha256 != self.source_sha256:
            raise ValueError("structural model artifact must match the bound model source hash")
        return self


class RegionSelector(Base):
    kind: Literal["model_link", "node_set", "element_set", "surface_set", "mesh_region"]
    name: str
    source: Literal["contract.model"] = "contract.model"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _nonblank(value)


Axis = Literal["x", "y", "z"]


class DofSelector(Base):
    translation: list[Axis] = []
    rotation: list[Axis] = []
    frame: str
    prescribed_translation: dict[Axis, Quantity] = {}
    prescribed_rotation: dict[Axis, Quantity] = {}

    @field_validator("frame")
    @classmethod
    def _frame(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _valid_dofs(self) -> "DofSelector":
        if not self.translation and not self.rotation:
            raise ValueError("DOF selector must select at least one axis")
        if len(self.translation) != len(set(self.translation)) or len(self.rotation) != len(
            set(self.rotation)
        ):
            raise ValueError("DOF selector axes must be unique")
        if not set(self.prescribed_translation).issubset(self.translation):
            raise ValueError("prescribed translation axes must be selected")
        if not set(self.prescribed_rotation).issubset(self.rotation):
            raise ValueError("prescribed rotation axes must be selected")
        for value in self.prescribed_translation.values():
            if value.dimension() != "length" or value.frame not in {None, self.frame}:
                raise ValueError("prescribed translations require length in the selector frame")
        for value in self.prescribed_rotation.values():
            if value.dimension() != "angle" or value.frame not in {None, self.frame}:
                raise ValueError("prescribed rotations require angle in the selector frame")
        return self


class SectionOrientation(Base):
    id: str
    frame: str
    longitudinal: tuple[float, float, float]
    in_plane_reference: tuple[float, float, float]
    rotation_about_local_axis: AngleQuantity | None = None
    provenance: Provenance

    @field_validator("id", "frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _right_handed_basis(self) -> "SectionOrientation":
        first = np.asarray(self.longitudinal, dtype=float)
        reference = np.asarray(self.in_plane_reference, dtype=float)
        if not np.isfinite(first).all() or not np.isfinite(reference).all():
            raise ValueError("section orientation vectors must be finite")
        first_norm = np.linalg.norm(first)
        if first_norm <= 1e-12 or np.linalg.norm(reference) <= 1e-12:
            raise ValueError("section orientation vectors must be nonzero and non-collinear")
        reference_orthogonal = reference - first * np.dot(reference, first) / (first_norm**2)
        second_norm = np.linalg.norm(reference_orthogonal)
        if second_norm <= 1e-12:
            raise ValueError("section orientation vectors must be nonzero and non-collinear")
        first /= first_norm
        second = reference_orthogonal / second_norm
        third = np.cross(first, second)
        basis = np.column_stack((first, second, third))
        if not np.allclose(basis.T @ basis, np.eye(3), atol=1e-9) or np.linalg.det(basis) <= 0:
            raise ValueError("section orientation must yield a right-handed orthonormal basis")
        return self


class PlasticPoint(Base):
    von_mises_yield_stress: PositivePressureQuantity
    equivalent_plastic_strain: DimensionlessQuantity


class MaterialInput(Base):
    id: str
    kind: Literal["linear_isotropic", "isotropic_elastoplastic"]
    youngs_modulus: PositivePressureQuantity
    poisson_ratio: DimensionlessQuantity
    density: DensityQuantity | None = None
    plastic_curve: list[PlasticPoint] = []
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _material_law(self) -> "MaterialInput":
        if not -1.0 < self.poisson_ratio.base() < 0.5:
            raise ValueError("Poisson ratio must be strictly between -1 and 0.5")
        if self.kind == "linear_isotropic" and self.plastic_curve:
            raise ValueError("linear isotropic material cannot declare a plastic curve")
        if self.kind == "isotropic_elastoplastic" and not self.plastic_curve:
            raise ValueError("isotropic elastoplastic material requires a plastic curve")
        if self.plastic_curve:
            strains = [point.equivalent_plastic_strain.base() for point in self.plastic_curve]
            stresses = [point.von_mises_yield_stress.base() for point in self.plastic_curve]
            if strains[0] != 0.0:
                raise ValueError("plastic curve first equivalent plastic strain must be exactly zero")
            if any(right <= left for left, right in zip(strains, strains[1:], strict=False)):
                raise ValueError("plastic curve strain must increase strictly")
            if any(right < left for left, right in zip(stresses, stresses[1:], strict=False)):
                raise ValueError("plastic curve yield stress must be nondecreasing")
        return self


class SectionInput(Base):
    id: str
    kind: Literal["truss", "beam", "frame", "plate", "shell", "solid"]
    material_id: str
    area: AreaQuantity | None = None
    principal_second_moment_y: SecondMomentQuantity | None = None
    principal_second_moment_z: SecondMomentQuantity | None = None
    torsion_constant_j: SecondMomentQuantity | None = None
    shear_area_y: AreaQuantity | None = None
    shear_area_z: AreaQuantity | None = None
    thickness: PositiveLengthQuantity | None = None
    orientation_id: str | None = None
    provenance: Provenance

    @field_validator("id", "material_id")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _shape(self) -> "SectionInput":
        if self.kind == "truss":
            if self.area is None:
                raise ValueError("truss section requires area")
            if any(
                value is not None
                for value in (
                    self.principal_second_moment_y,
                    self.principal_second_moment_z,
                    self.torsion_constant_j,
                    self.shear_area_y,
                    self.shear_area_z,
                    self.thickness,
                    self.orientation_id,
                )
            ):
                raise ValueError("truss section accepts only area")
        if self.kind in {"beam", "frame"} and any(
            value is None
            for value in (
                self.area,
                self.principal_second_moment_y,
                self.principal_second_moment_z,
                self.torsion_constant_j,
                self.orientation_id,
            )
        ):
            raise ValueError("beam/frame section requires area, Iy, Iz, J, and orientation")
        if self.kind in {"plate", "shell"} and self.thickness is None:
            raise ValueError("plate/shell section requires thickness")
        if self.kind in {"plate", "shell"} and any(
            value is not None
            for value in (
                self.area,
                self.principal_second_moment_y,
                self.principal_second_moment_z,
                self.torsion_constant_j,
                self.shear_area_y,
                self.shear_area_z,
            )
        ):
            raise ValueError("plate/shell section accepts thickness and optional orientation only")
        if self.kind == "solid" and any(
            value is not None
            for value in (
                self.area,
                self.principal_second_moment_y,
                self.principal_second_moment_z,
                self.torsion_constant_j,
                self.thickness,
                self.orientation_id,
            )
        ):
            raise ValueError("solid section does not accept invented section dimensions")
        return self


class ElementAssignment(Base):
    id: str
    region: RegionSelector
    section_id: str
    provenance: Provenance

    @field_validator("id", "section_id")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)


class InertiaTensor(Base):
    xx: InertiaQuantity
    yy: InertiaQuantity
    zz: InertiaQuantity
    xy: InertiaQuantity
    xz: InertiaQuantity
    yz: InertiaQuantity
    frame: str

    @field_validator("frame")
    @classmethod
    def _frame(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _physical_tensor(self) -> "InertiaTensor":
        values = [self.xx, self.yy, self.zz, self.xy, self.xz, self.yz]
        if any(value.frame not in {None, self.frame} for value in values):
            raise ValueError("inertia tensor components must use the tensor frame")
        xx, yy, zz, xy, xz, yz = (value.base() for value in values)
        tensor = np.array(((xx, xy, xz), (xy, yy, yz), (xz, yz, zz)), dtype=float)
        moments, basis = np.linalg.eigh(tensor)
        if not np.isfinite(moments).all() or moments[0] <= 0:
            raise ValueError("inertia tensor must have strictly positive principal moments")
        tolerance = max(float(moments[-1]), 1.0) * 1e-12
        if moments[-1] > moments[0] + moments[1] + tolerance:
            raise ValueError("inertia principal moments must satisfy the triangle inequality")
        if np.linalg.det(basis) < 0:
            basis[:, 0] *= -1
        if not np.allclose(basis.T @ basis, np.eye(3), atol=1e-9) or np.linalg.det(basis) <= 0:
            raise ValueError("inertia tensor must yield a right-handed orthonormal principal basis")
        return self


class MassAssignment(Base):
    id: str
    region: RegionSelector
    mass: MassQuantity
    center_of_mass: VectorQuantity | None = None
    rotational_inertia: InertiaTensor | None = None
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _physical_mass(self) -> "MassAssignment":
        if self.mass.base() <= 0 or not math.isfinite(self.mass.base()):
            raise ValueError("mass assignment requires positive finite mass")
        if self.center_of_mass is not None:
            self.center_of_mass.require_dimension("length", "center of mass")
        if self.rotational_inertia is not None:
            if self.center_of_mass is None:
                raise ValueError("rotational inertia requires an explicit center of mass")
            if self.center_of_mass.frame != self.rotational_inertia.frame:
                raise ValueError("center of mass and rotational inertia must use one frame")
        return self


class ConnectionInput(Base):
    id: str
    kind: Literal["rigid", "pinned", "spring", "damper"]
    endpoint_a: RegionSelector
    endpoint_b: RegionSelector
    dofs: DofSelector
    translational_stiffness: LinearStiffnessQuantity | None = None
    rotational_stiffness: RotationalStiffnessQuantity | None = None
    damping_id: str | None = None
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _operands(self) -> "ConnectionInput":
        if self.kind == "spring" and not (
            self.translational_stiffness or self.rotational_stiffness
        ):
            raise ValueError("spring connection requires stiffness")
        if self.kind == "damper" and self.damping_id is None:
            raise ValueError("damper connection requires an explicit damping reference")
        if self.kind in {"rigid", "pinned"} and any(
            value is not None
            for value in (
                self.translational_stiffness,
                self.rotational_stiffness,
                self.damping_id,
            )
        ):
            raise ValueError("rigid/pinned connections do not accept spring or damping operands")
        return self


class SupportInput(Base):
    id: str
    region: RegionSelector
    dofs: DofSelector
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)


class AnalyticAmplitude(Base):
    kind: Literal["constant", "sine", "ramp"]
    scale: float = 1.0
    frequency: FrequencyQuantity | None = None
    phase: AngleQuantity | None = None
    duration: PositiveTimeQuantity | None = None

    @model_validator(mode="after")
    def _shape(self) -> "AnalyticAmplitude":
        if not math.isfinite(self.scale):
            raise ValueError("amplitude scale must be finite")
        if self.kind == "sine" and self.frequency is None:
            raise ValueError("sine amplitude requires frequency")
        if self.kind == "ramp" and self.duration is None:
            raise ValueError("ramp amplitude requires duration")
        return self


class SeriesAmplitude(Base):
    kind: Literal["input_series"] = "input_series"
    artifact: InputArtifact
    time_column: str
    value_column: str
    time_unit: str
    value_unit: str
    interpolation: Literal["linear"] = "linear"
    extrapolation: Literal["refuse"] = "refuse"

    @field_validator("time_column", "value_column")
    @classmethod
    def _columns(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _series_roles(self) -> "SeriesAmplitude":
        if not units.known(self.time_unit) or units.dimension(self.time_unit) != "time":
            raise ValueError("input-series time unit must have time dimension")
        if not units.known(self.value_unit) or units.dimension(self.value_unit) != "dimensionless":
            raise ValueError("input-series amplitude values must be dimensionless")
        if self.artifact.format not in {"csv", "json"}:
            raise ValueError("input-series artifact must be csv or json")
        return self


class LoadInput(Base):
    id: str
    kind: Literal["force", "moment", "gravity", "pressure", "prescribed_displacement"]
    region: RegionSelector
    vector: VectorQuantity | None = None
    scalar: Quantity | None = None
    application_frame: str
    amplitude: AnalyticAmplitude | SeriesAmplitude | None = None
    provenance: Provenance

    @field_validator("id", "application_frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _load_role(self) -> "LoadInput":
        vector_dimensions = {
            "force": "force",
            "moment": "torque",
            "gravity": "acceleration",
            "prescribed_displacement": "length",
        }
        scalar_dimensions = {"pressure": "pressure"}
        if self.kind in vector_dimensions:
            if self.vector is None or self.scalar is not None:
                raise ValueError(f"{self.kind} load requires only a vector")
            self.vector.require_dimension(vector_dimensions[self.kind], f"{self.kind} load")
            if self.vector.frame != self.application_frame:
                raise ValueError("load vector must use the application frame")
        else:
            if self.scalar is None or self.vector is not None:
                raise ValueError(f"{self.kind} load requires only a scalar")
            if self.scalar.dimension() != scalar_dimensions[self.kind]:
                raise ValueError(f"{self.kind} load has the wrong unit")
            if self.scalar.frame not in {None, self.application_frame}:
                raise ValueError("load scalar must use the application frame")
        return self


class DampingInput(Base):
    id: str
    kind: Literal["rayleigh", "modal_ratio", "viscous_translational", "viscous_rotational"]
    rayleigh_alpha: InverseTimeQuantity | None = None
    rayleigh_beta: TimeQuantity | None = None
    modal_ratio: DimensionlessQuantity | None = None
    translational: LinearDampingQuantity | None = None
    rotational: RotationalDampingQuantity | None = None
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _one_law(self) -> "DampingInput":
        populated = {
            "rayleigh_alpha": self.rayleigh_alpha,
            "rayleigh_beta": self.rayleigh_beta,
            "modal_ratio": self.modal_ratio,
            "translational": self.translational,
            "rotational": self.rotational,
        }
        required = {
            "rayleigh": {"rayleigh_alpha", "rayleigh_beta"},
            "modal_ratio": {"modal_ratio"},
            "viscous_translational": {"translational"},
            "viscous_rotational": {"rotational"},
        }[self.kind]
        actual = {name for name, value in populated.items() if value is not None}
        if actual != required:
            raise ValueError(f"{self.kind} damping requires exactly {', '.join(sorted(required))}")
        if self.rayleigh_beta is not None and (
            self.rayleigh_beta.base() < 0 or not math.isfinite(self.rayleigh_beta.base())
        ):
            raise ValueError("Rayleigh beta must be nonnegative and finite")
        if self.modal_ratio is not None and not 0 <= self.modal_ratio.base() < 1:
            raise ValueError("modal damping ratio must be in [0, 1)")
        return self


class ContactTabularPoint(Base):
    pressure: PressureQuantity
    overclosure: LengthQuantity


class ContactNormalLaw(Base):
    kind: Literal["hard", "linear", "exponential", "tabular"]
    pressure_overclosure_slope: ContactStiffnessQuantity | None = None
    tensile_stress: PressureQuantity | None = None
    clearance_coefficient: DimensionlessQuantity | None = None
    clearance_distance: PositiveLengthQuantity | None = None
    zero_clearance_pressure: PositivePressureQuantity | None = None
    points: list[ContactTabularPoint] = []

    @model_validator(mode="after")
    def _law_shape(self) -> "ContactNormalLaw":
        scalar_values = (
            self.pressure_overclosure_slope,
            self.tensile_stress,
            self.clearance_coefficient,
            self.clearance_distance,
            self.zero_clearance_pressure,
        )
        if self.kind == "hard" and (any(value is not None for value in scalar_values) or self.points):
            raise ValueError("hard contact has no numeric contract default")
        if self.kind == "linear" and self.pressure_overclosure_slope is None:
            raise ValueError("linear contact requires pressure-overclosure slope")
        if self.clearance_coefficient is not None and self.clearance_coefficient.base() < 0:
            raise ValueError("contact clearance coefficient must be nonnegative")
        if self.kind == "linear" and any(
            value is not None for value in (self.clearance_distance, self.zero_clearance_pressure)
        ):
            raise ValueError("linear contact does not accept exponential operands")
        if self.kind == "exponential" and (
            self.clearance_distance is None or self.zero_clearance_pressure is None
        ):
            raise ValueError("exponential contact requires clearance and zero-clearance pressure")
        if self.kind == "exponential" and any(
            value is not None
            for value in (
                self.pressure_overclosure_slope,
                self.tensile_stress,
                self.clearance_coefficient,
            )
        ):
            raise ValueError("exponential contact accepts only clearance and pressure operands")
        if self.kind == "tabular":
            if not self.points:
                raise ValueError("tabular contact requires points")
            if any(value is not None for value in scalar_values):
                raise ValueError("tabular contact accepts only pressure-overclosure points")
            closures = [point.overclosure.base() for point in self.points]
            pressures = [point.pressure.base() for point in self.points]
            if any(right <= left for left, right in zip(closures, closures[1:], strict=False)):
                raise ValueError("tabular contact overclosure must increase strictly")
            if any(right < left for left, right in zip(pressures, pressures[1:], strict=False)):
                raise ValueError("tabular contact pressure must be nondecreasing")
        elif self.points:
            raise ValueError("contact points are only valid for a tabular law")
        return self


class ContactTangentialLaw(Base):
    friction_coefficient: DimensionlessQuantity
    stick_slope: ContactStiffnessQuantity | None = None

    @model_validator(mode="after")
    def _friction(self) -> "ContactTangentialLaw":
        if self.friction_coefficient.base() < 0:
            raise ValueError("friction coefficient must be nonnegative")
        return self


class ContactDampingLaw(Base):
    coefficient: ContactDampingQuantity
    tangential_fraction: DimensionlessQuantity

    @model_validator(mode="after")
    def _fraction(self) -> "ContactDampingLaw":
        if not 0 <= self.tangential_fraction.base() <= 1:
            raise ValueError("contact tangential damping fraction must be in [0, 1]")
        return self


class ContactInput(Base):
    id: str
    master: RegionSelector
    slave: RegionSelector
    frame: str
    normal: ContactNormalLaw
    tangential: ContactTangentialLaw | None = None
    damping: ContactDampingLaw | None = None
    provenance: Provenance

    @field_validator("id", "frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _surfaces(self) -> "ContactInput":
        if self.master.kind != "surface_set" or self.slave.kind != "surface_set":
            raise ValueError("contact master and slave must be surface_set selectors")
        if self.master == self.slave:
            raise ValueError("contact master and slave must be distinct")
        return self


class StructuralProcedure(Base):
    id: str
    kind: Literal[
        "linear_static",
        "modal",
        "harmonic",
        "transient",
        "nonlinear_static",
        "impact",
    ]
    assignment_ids: list[str]
    support_ids: list[str] = []
    load_ids: list[str] = []
    mass_ids: list[str] = []
    contact_ids: list[str] = []
    damping_mode: Literal["declared", "undamped"] | None = None
    damping_ids: list[str] = []
    mode_count: Annotated[int, Field(strict=True, gt=0)] | None = None
    frequency: FrequencyQuantity | None = None
    duration: PositiveTimeQuantity | None = None
    time_step: PositiveTimeQuantity | None = None
    initial_velocity: VectorQuantity | None = None
    geometric_nonlinearity: bool | None = None
    plasticity: bool | None = None
    max_increments: Annotated[int, Field(strict=True, gt=0)] | None = None
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _procedure_shape(self) -> "StructuralProcedure":
        if not self.assignment_ids:
            raise ValueError("structural procedure requires assignments")
        dynamic = self.kind in {"modal", "harmonic", "transient", "impact"}
        if dynamic and self.damping_mode is None:
            raise ValueError("dynamic procedure requires explicit damping_mode")
        if self.damping_mode == "declared" and not self.damping_ids:
            raise ValueError("declared damping mode requires damping references")
        if self.damping_mode == "undamped" and self.damping_ids:
            raise ValueError("undamped procedure must not reference damping")
        if not dynamic and (self.damping_mode is not None or self.damping_ids):
            raise ValueError("static procedures do not consume damping")
        if self.kind == "modal" and self.mode_count is None:
            raise ValueError("modal procedure requires mode_count")
        if self.kind == "harmonic" and (self.frequency is None or not self.load_ids):
            raise ValueError("harmonic procedure requires frequency and excitation loads")
        if self.frequency is not None and (
            self.frequency.base() <= 0 or not math.isfinite(self.frequency.base())
        ):
            raise ValueError("procedure frequency must be positive and finite")
        if self.kind == "transient" and (
            self.duration is None
            or self.time_step is None
            or (not self.load_ids and self.initial_velocity is None)
        ):
            raise ValueError("transient procedure requires duration, step, and excitation")
        if self.kind == "nonlinear_static" and (
            self.max_increments is None
            or self.geometric_nonlinearity is None
            or self.plasticity is None
            or not self.load_ids
        ):
            raise ValueError("nonlinear static requires flags, increments, and loads")
        if self.kind == "impact":
            if (
                self.duration is None
                or self.time_step is None
                or self.initial_velocity is None
                or not self.contact_ids
            ):
                raise ValueError("impact requires duration, step, initial velocity, and contact")
            self.initial_velocity.require_dimension("velocity", "impact initial velocity")
        if self.duration is not None and self.time_step is not None:
            if self.time_step.base() > self.duration.base():
                raise ValueError("procedure time step must not exceed duration")
        return self


class ScalarAgreementLimit(Base):
    kind: Literal["scalar_reference"] = "scalar_reference"
    id: str
    metric: str
    comparator: Literal["absolute", "relative"]
    threshold: Quantity
    reference: Literal["analytic"] | ArtifactRef
    provenance: Provenance


class RmsPeakAgreementLimit(Base):
    kind: Literal["rms_peak"] = "rms_peak"
    id: str
    metric: str
    rms_threshold: Quantity
    peak_threshold: Quantity
    window: PositiveTimeQuantity
    provenance: Provenance

    @model_validator(mode="after")
    def _matching_roles(self) -> "RmsPeakAgreementLimit":
        if self.rms_threshold.dimension() != self.peak_threshold.dimension():
            raise ValueError("RMS and peak thresholds must have matching dimensions")
        return self


class EnergyBalanceLimit(Base):
    kind: Literal["energy_balance"] = "energy_balance"
    id: str
    threshold: EnergyQuantity
    provenance: Provenance


class RefinementLimit(Base):
    kind: Literal["refinement"] = "refinement"
    id: str
    coarse_procedure_id: str
    fine_procedure_id: str
    requirement: Literal["fine_nonworse"] = "fine_nonworse"
    provenance: Provenance


class ContactBehaviorLimit(Base):
    kind: Literal["contact_behavior"] = "contact_behavior"
    id: str
    metric: Literal["separation", "slip", "penetration", "rebound_velocity"]
    threshold: Quantity
    provenance: Provenance

    @model_validator(mode="after")
    def _metric_unit(self) -> "ContactBehaviorLimit":
        expected = "velocity" if self.metric == "rebound_velocity" else "length"
        if self.threshold.dimension() != expected:
            raise ValueError(f"{self.metric} threshold requires {expected}")
        return self


StructuralAcceptanceLimit = Annotated[
    ScalarAgreementLimit
    | RmsPeakAgreementLimit
    | EnergyBalanceLimit
    | RefinementLimit
    | ContactBehaviorLimit,
    Field(discriminator="kind"),
]


class StructuralInputs(Base):
    version: Literal["1"] = "1"
    model: StructuralModelBinding
    frames: list[FrameInput] = []
    orientations: list[SectionOrientation] = []
    materials: list[MaterialInput]
    sections: list[SectionInput]
    assignments: list[ElementAssignment]
    masses: list[MassAssignment] = []
    connections: list[ConnectionInput] = []
    supports: list[SupportInput] = []
    loads: list[LoadInput] = []
    damping: list[DampingInput] = []
    contacts: list[ContactInput] = []
    procedures: list[StructuralProcedure]
    acceptance_limits: list[StructuralAcceptanceLimit] = []
    runtime: RuntimeSelection
    provenance: Provenance

    @model_validator(mode="after")
    def _resolved_graph(self) -> "StructuralInputs":
        collections = {
            "frames": self.frames,
            "orientations": self.orientations,
            "materials": self.materials,
            "sections": self.sections,
            "assignments": self.assignments,
            "masses": self.masses,
            "connections": self.connections,
            "supports": self.supports,
            "loads": self.loads,
            "damping": self.damping,
            "contacts": self.contacts,
            "procedures": self.procedures,
            "acceptance_limits": self.acceptance_limits,
        }
        for label, rows in collections.items():
            _unique_row_ids(label, rows)

        frame_ids = {self.model.source_frame, "world"}
        for frame in self.frames:
            if frame.id in frame_ids:
                raise ValueError(f"duplicate or reserved frame id {frame.id!r}")
            if frame.parent not in frame_ids and not frame.parent.startswith("model_link:"):
                raise ValueError(f"frame {frame.id!r} has unknown or forward parent {frame.parent!r}")
            frame_ids.add(frame.id)
        for orientation in self.orientations:
            if orientation.frame not in frame_ids:
                raise ValueError(f"orientation {orientation.id!r} has unknown frame")

        materials = {row.id: row for row in self.materials}
        orientations = {row.id for row in self.orientations}
        sections = {row.id: row for row in self.sections}
        assignments = {row.id: row for row in self.assignments}
        masses = {row.id: row for row in self.masses}
        dampers = {row.id for row in self.damping}
        supports = {row.id for row in self.supports}
        loads = {row.id for row in self.loads}
        contacts = {row.id for row in self.contacts}
        for section in self.sections:
            if section.material_id not in materials:
                raise ValueError(f"section {section.id!r} references unknown material")
            if section.orientation_id is not None and section.orientation_id not in orientations:
                raise ValueError(f"section {section.id!r} references unknown orientation")
        for assignment in self.assignments:
            if assignment.section_id not in sections:
                raise ValueError(f"assignment {assignment.id!r} references unknown section")
            if (
                assignment.section_id in sections
                and sections[assignment.section_id].kind == "solid"
                and assignment.region.kind != "mesh_region"
            ):
                raise ValueError("solid section assignments require a mesh_region selector")
        for connection in self.connections:
            if connection.damping_id is not None and connection.damping_id not in dampers:
                raise ValueError(f"connection {connection.id!r} references unknown damping")

        for procedure in self.procedures:
            references = (
                ("assignment", procedure.assignment_ids, assignments),
                ("mass", procedure.mass_ids, masses),
                ("support", procedure.support_ids, supports),
                ("load", procedure.load_ids, loads),
                ("damping", procedure.damping_ids, dampers),
                ("contact", procedure.contact_ids, contacts),
            )
            for label, ids, inventory in references:
                missing = sorted(set(ids) - set(inventory))
                if missing:
                    raise ValueError(
                        f"procedure {procedure.id!r} references unknown {label}(s): "
                        + ", ".join(missing)
                    )
            if len(procedure.assignment_ids) != len(set(procedure.assignment_ids)):
                raise ValueError(f"procedure {procedure.id!r} repeats an assignment")

            if procedure.kind in {"modal", "harmonic", "transient", "impact"}:
                selected_masses = [masses[row_id] for row_id in procedure.mass_ids]
                for assignment_id in procedure.assignment_ids:
                    assignment = assignments[assignment_id]
                    section = sections[assignment.section_id]
                    has_density = materials[section.material_id].density is not None
                    explicit_here = [row for row in selected_masses if row.region == assignment.region]
                    if has_density and explicit_here:
                        raise ValueError(
                            f"procedure {procedure.id!r} double-counts density and explicit mass"
                        )
                    if not has_density and not explicit_here:
                        raise ValueError(
                            f"procedure {procedure.id!r} lacks density or explicit mass for "
                            f"assignment {assignment_id!r}"
                        )
            if procedure.kind == "nonlinear_static" and procedure.plasticity:
                for assignment_id in procedure.assignment_ids:
                    assignment = assignments[assignment_id]
                    section = sections[assignment.section_id]
                    if materials[section.material_id].kind != "isotropic_elastoplastic":
                        raise ValueError(
                            f"procedure {procedure.id!r} enables plasticity for a linear material"
                        )
            for support_id in procedure.support_ids:
                support = next(row for row in self.supports if row.id == support_id)
                matching_kinds = {
                    sections[assignments[assignment_id].section_id].kind
                    for assignment_id in procedure.assignment_ids
                    if assignments[assignment_id].region == support.region
                }
                if support.dofs.rotation and matching_kinds.intersection({"truss", "solid"}):
                    raise ValueError(
                        f"support {support.id!r} selects rotational DOFs unavailable to its formulation"
                    )
        return self


class SignalInterface(Base):
    id: str
    role: Literal["command", "state_feedback", "sensor", "actuator"]
    unit: str
    frame: str
    independently_measured: bool = False

    @field_validator("id", "frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        if not units.known(value):
            raise ValueError(f"unknown unit {value!r}")
        return value

    @model_validator(mode="after")
    def _feedback_source(self) -> "SignalInterface":
        if self.role == "state_feedback" and not self.independently_measured:
            raise ValueError("state feedback must be independently measured, not command echo")
        if self.role != "state_feedback" and self.independently_measured:
            raise ValueError("independently_measured is reserved for state feedback")
        return self


class WorldInput(Base):
    id: str
    artifact: InputArtifact
    root_frame: str
    gravity: VectorQuantity
    provenance: Provenance

    @field_validator("id", "root_frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _gravity_role(self) -> "WorldInput":
        self.gravity.require_dimension("acceleration", "world gravity")
        if self.gravity.frame != self.root_frame:
            raise ValueError("gravity must be expressed in the world root frame")
        return self


class PlantDampingInput(Base):
    id: str
    joint: str | None = None
    translational: LinearDampingQuantity | None = None
    rotational: RotationalDampingQuantity | None = None
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _coefficient(self) -> "PlantDampingInput":
        if (self.translational is None) == (self.rotational is None):
            raise ValueError("plant damping requires exactly one translational/rotational value")
        return self


class PlantInput(Base):
    id: str
    kind: Literal["px4_x500", "anymal"]
    artifact: InputArtifact
    source_sha256: str
    structure_hash: str
    base_frame: str
    has_collisions: Literal[True]
    has_verified_inertials: Literal[True]
    interfaces: list[SignalInterface]
    damping_mode: Literal["declared", "undamped"]
    damping_ids: list[str] = []
    provenance: Provenance

    @field_validator("id", "base_frame")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("source_sha256", "structure_hash")
    @classmethod
    def _hashes(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("plant hashes must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _capabilities(self) -> "PlantInput":
        if self.artifact.ref.sha256 != self.source_sha256:
            raise ValueError("plant artifact must match the bound plant source hash")
        _unique_row_ids("plant interfaces", self.interfaces)
        roles = {interface.role for interface in self.interfaces}
        if not {"command", "state_feedback", "sensor", "actuator"}.issubset(roles):
            raise ValueError(
                "plant requires command, independent state-feedback, sensor, and actuator interfaces"
            )
        if self.damping_mode == "declared" and not self.damping_ids:
            raise ValueError("declared plant damping requires references")
        if self.damping_mode == "undamped" and self.damping_ids:
            raise ValueError("undamped plant must not reference damping")
        return self


class InitialStateInput(Base):
    id: str
    plant_id: str
    position: VectorQuantity
    orientation_xyzw: tuple[float, float, float, float]
    linear_velocity: VectorQuantity
    angular_velocity: VectorQuantity
    joint_positions: dict[str, AngleQuantity] = {}
    joint_velocities: dict[str, AngularRateQuantity] = {}
    provenance: Provenance

    @field_validator("id", "plant_id")
    @classmethod
    def _text(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _state_roles(self) -> "InitialStateInput":
        self.position.require_dimension("length", "initial position")
        self.linear_velocity.require_dimension("velocity", "initial linear velocity")
        self.angular_velocity.require_dimension("angular_rate", "initial angular velocity")
        frames = {self.position.frame, self.linear_velocity.frame, self.angular_velocity.frame}
        if len(frames) != 1:
            raise ValueError("initial base pose and twist must use one world frame")
        if not all(math.isfinite(component) for component in self.orientation_xyzw):
            raise ValueError("initial orientation must be finite")
        norm = math.sqrt(sum(component * component for component in self.orientation_xyzw))
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("initial orientation quaternion must be normalized")
        return self


class ControllerInput(Base):
    id: str
    kind: Literal["px4_sitl", "ros2_control", "disabled"]
    config: InputArtifact | None = None
    admitted_identity: str | None = None
    update_period: PositiveTimeQuantity | None = None
    command_interfaces: list[str] = []
    state_interfaces: list[str] = []
    implicit_hold: bool
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("admitted_identity")
    @classmethod
    def _identity(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("controller identity must be a safe package-manifest key")
        return value

    @model_validator(mode="after")
    def _controller_shape(self) -> "ControllerInput":
        if self.kind == "disabled":
            if self.implicit_hold:
                raise ValueError("disabled controller requires implicit_hold=False")
            if any(
                value is not None for value in (self.config, self.admitted_identity, self.update_period)
            ) or self.command_interfaces or self.state_interfaces:
                raise ValueError("disabled controller accepts no executable configuration or interfaces")
        elif (
            self.config is None
            or self.admitted_identity is None
            or self.update_period is None
            or not self.command_interfaces
            or not self.state_interfaces
        ):
            raise ValueError("enabled controller requires config, admitted identity, period, and maps")
        return self


class PhysicsInput(Base):
    id: str
    engine_profile: str
    max_step_size: PositiveTimeQuantity
    solver_iterations: Annotated[int, Field(strict=True, gt=0)]
    real_time_factor: float = Field(gt=0)
    collision_contact_enabled: bool
    random_seed: Annotated[int, Field(strict=True, ge=0)]
    simulated_time: PositiveTimeQuantity
    time_policy: Literal["fixed"] = "fixed"
    provenance: Provenance

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("engine_profile")
    @classmethod
    def _profile(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("engine_profile must be a safe manifest key")
        return value

    @model_validator(mode="after")
    def _fixed_time_controls(self) -> "PhysicsInput":
        if self.max_step_size.base() > self.simulated_time.base():
            raise ValueError("physics max step must not exceed fixed simulated time")
        if not math.isfinite(self.real_time_factor):
            raise ValueError("physics real-time factor must be finite")
        return self


class X500Scenario(Base):
    id: str
    kind: Literal["x500_takeoff_hover_land"] = "x500_takeoff_hover_land"
    plant_id: str
    controller_id: str
    negative_controller_id: str
    world_id: str
    physics_id: str
    runtime_id: str
    initial_state_id: str
    target_altitude: PositiveLengthQuantity
    settled_hover_duration: PositiveTimeQuantity
    deadline: PositiveTimeQuantity
    phase_durations: dict[Literal["takeoff", "settle", "hover", "land"], PositiveTimeQuantity]
    random_seed: Annotated[int, Field(strict=True, ge=0)]
    simulated_time: PositiveTimeQuantity
    require_nonzero_sensing: Literal[True]
    require_nonzero_actuation: Literal[True]
    require_actual_feedback: Literal[True]
    require_controller_disabled_negative: Literal[True]
    acceptance_limit_ids: list[str]
    provenance: Provenance

    @model_validator(mode="after")
    def _fixed_x500_case(self) -> "X500Scenario":
        if not math.isclose(self.target_altitude.base(), 2.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("X500 target altitude must be exactly 2 m")
        if not math.isclose(
            self.settled_hover_duration.base(), 10.0, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("X500 settled hover must be exactly 10 s")
        if not math.isclose(self.deadline.base(), 90.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("X500 landing/disarm deadline must be exactly 90 s")
        if set(self.phase_durations) != {"takeoff", "settle", "hover", "land"}:
            raise ValueError("X500 phase allocation must declare takeoff/settle/hover/land")
        if not math.isclose(
            self.phase_durations["hover"].base(), 10.0, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("X500 hover phase allocation must be 10 s")
        if not math.isclose(
            sum(value.base() for value in self.phase_durations.values()),
            self.simulated_time.base(),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("X500 phases must allocate all fixed simulated time")
        if self.simulated_time.base() > self.deadline.base():
            raise ValueError("X500 simulated time must not exceed its 90 s deadline")
        return self


class AnymalScenario(Base):
    id: str
    kind: Literal["anymal_squat"] = "anymal_squat"
    plant_id: str
    controller_id: str
    negative_controller_id: str
    world_id: str
    physics_id: str
    runtime_id: str
    initial_state_id: str
    knee_cycle_amplitude: AngleQuantity
    knee_cycle_count: Literal[2]
    trajectory: Literal["smooth"]
    knee_targets: dict[str, AngleQuantity]
    compensating_hip_targets: dict[str, AngleQuantity]
    phase_durations: dict[
        Literal["stand", "cycle_1", "cycle_2", "recover"], PositiveTimeQuantity
    ]
    max_abs_roll_pitch: AngleQuantity
    random_seed: Annotated[int, Field(strict=True, ge=0)]
    simulated_time: PositiveTimeQuantity
    require_nonzero_sensing: Literal[True]
    require_nonzero_actuation: Literal[True]
    require_actual_feedback: Literal[True]
    require_controller_disabled_negative: Literal[True]
    acceptance_limit_ids: list[str]
    provenance: Provenance

    @model_validator(mode="after")
    def _fixed_anymal_case(self) -> "AnymalScenario":
        if not math.isclose(
            self.knee_cycle_amplitude.base(), 0.15, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("ANYmal knee cycle amplitude must be exactly 0.15 rad")
        if not self.knee_targets or not self.compensating_hip_targets:
            raise ValueError("ANYmal requires predeclared knee and compensating hip targets")
        if set(self.phase_durations) != {"stand", "cycle_1", "cycle_2", "recover"}:
            raise ValueError("ANYmal phase allocation must declare stand/two cycles/recover")
        if not math.isclose(
            sum(value.base() for value in self.phase_durations.values()),
            self.simulated_time.base(),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("ANYmal phases must allocate all fixed simulated time")
        if self.max_abs_roll_pitch.base() <= 0:
            raise ValueError("ANYmal roll/pitch bound must be positive")
        return self


SimulationScenario = Annotated[X500Scenario | AnymalScenario, Field(discriminator="kind")]


class X500PositionLimit(Base):
    kind: Literal["x500_position"] = "x500_position"
    id: str
    metric: Literal["hover_altitude_rms", "hover_altitude_peak", "horizontal_displacement"]
    threshold: PositiveLengthQuantity
    provenance: Provenance


class X500LandingLimit(Base):
    kind: Literal["x500_land_disarm"] = "x500_land_disarm"
    id: str
    deadline: PositiveTimeQuantity
    require_landed: Literal[True]
    require_disarmed: Literal[True]
    provenance: Provenance


class AnymalTrackingLimit(Base):
    kind: Literal["anymal_tracking"] = "anymal_tracking"
    id: str
    rms_threshold: AngleQuantity
    peak_threshold: AngleQuantity
    provenance: Provenance


class AnymalBodyContactLimit(Base):
    kind: Literal["anymal_body_contact"] = "anymal_body_contact"
    id: str
    allow_unintended_body_ground_contact: Literal[False]
    provenance: Provenance


class AnymalAttitudeLimit(Base):
    kind: Literal["anymal_attitude"] = "anymal_attitude"
    id: str
    max_abs_roll_pitch: AngleQuantity
    provenance: Provenance


SimulationAcceptanceLimit = Annotated[
    X500PositionLimit
    | X500LandingLimit
    | AnymalTrackingLimit
    | AnymalBodyContactLimit
    | AnymalAttitudeLimit,
    Field(discriminator="kind"),
]


class SimulationInputs(Base):
    version: Literal["1"] = "1"
    frames: list[FrameInput] = []
    worlds: list[WorldInput]
    plants: list[PlantInput]
    initial_states: list[InitialStateInput]
    controllers: list[ControllerInput]
    physics: list[PhysicsInput]
    damping: list[PlantDampingInput] = []
    runtimes: list[RuntimeSelection]
    scenarios: list[SimulationScenario]
    acceptance_limits: list[SimulationAcceptanceLimit]
    provenance: Provenance

    @model_validator(mode="after")
    def _resolved_scenarios(self) -> "SimulationInputs":
        collections = {
            "frames": self.frames,
            "worlds": self.worlds,
            "plants": self.plants,
            "initial_states": self.initial_states,
            "controllers": self.controllers,
            "physics": self.physics,
            "damping": self.damping,
            "runtimes": self.runtimes,
            "scenarios": self.scenarios,
            "acceptance_limits": self.acceptance_limits,
        }
        for label, rows in collections.items():
            _unique_row_ids(label, rows)

        frame_ids = {"world"}
        for frame in self.frames:
            if frame.id in frame_ids or frame.parent not in frame_ids:
                raise ValueError(f"simulation frame {frame.id!r} has duplicate/unknown parent")
            frame_ids.add(frame.id)
        for world in self.worlds:
            if world.root_frame not in frame_ids:
                raise ValueError(f"world {world.id!r} uses an unknown root frame")
            if world.artifact.format not in {"world", "sdf"}:
                raise ValueError("world artifact must use world or sdf format")
        for plant in self.plants:
            if plant.artifact.format not in {"urdf", "sdf"}:
                raise ValueError("plant artifact must use urdf or sdf format")
        for controller in self.controllers:
            if controller.config is not None and controller.config.format not in {
                "controller_config",
                "json",
                "yaml",
            }:
                raise ValueError("controller config has an unsupported data format")

        worlds = {row.id: row for row in self.worlds}
        plants = {row.id: row for row in self.plants}
        states = {row.id: row for row in self.initial_states}
        controllers = {row.id: row for row in self.controllers}
        physics = {row.id: row for row in self.physics}
        damping = {row.id for row in self.damping}
        runtimes = {row.id for row in self.runtimes}
        limits = {row.id: row for row in self.acceptance_limits}
        for plant in self.plants:
            missing_damping = sorted(set(plant.damping_ids) - damping)
            if missing_damping:
                raise ValueError(
                    f"plant {plant.id!r} references unknown damping: {', '.join(missing_damping)}"
                )
        for state in self.initial_states:
            if state.plant_id not in plants:
                raise ValueError(f"initial state {state.id!r} references unknown plant")

        for scenario in self.scenarios:
            references = {
                "world": (scenario.world_id, worlds),
                "plant": (scenario.plant_id, plants),
                "controller": (scenario.controller_id, controllers),
                "negative controller": (scenario.negative_controller_id, controllers),
                "physics": (scenario.physics_id, physics),
                "runtime": (scenario.runtime_id, runtimes),
                "initial state": (scenario.initial_state_id, states),
            }
            for label, (row_id, inventory) in references.items():
                if row_id not in inventory:
                    raise ValueError(f"scenario {scenario.id!r} references unknown {label} {row_id!r}")
            plant = plants[scenario.plant_id]
            controller = controllers[scenario.controller_id]
            negative = controllers[scenario.negative_controller_id]
            state = states[scenario.initial_state_id]
            physical = physics[scenario.physics_id]
            if state.plant_id != plant.id:
                raise ValueError("scenario initial state must belong to its plant")
            if state.position.frame != worlds[scenario.world_id].root_frame:
                raise ValueError("scenario initial state must use the selected world root frame")
            expected = (
                ("px4_x500", "px4_sitl")
                if scenario.kind == "x500_takeoff_hover_land"
                else ("anymal", "ros2_control")
            )
            if (plant.kind, controller.kind) != expected:
                raise ValueError(f"scenario {scenario.id!r} has incompatible plant/controller")
            interfaces = {interface.id: interface for interface in plant.interfaces}
            if any(
                interface_id not in interfaces or interfaces[interface_id].role != "command"
                for interface_id in controller.command_interfaces
            ):
                raise ValueError("controller command map must reference plant command interfaces")
            if any(
                interface_id not in interfaces
                or interfaces[interface_id].role != "state_feedback"
                or not interfaces[interface_id].independently_measured
                for interface_id in controller.state_interfaces
            ):
                raise ValueError("controller state map must reference independent plant feedback")
            if negative.kind != "disabled" or negative.implicit_hold:
                raise ValueError("scenario negative requires a disabled controller without implicit hold")
            if not physical.collision_contact_enabled:
                raise ValueError("declared simulation scenarios require collision/contact enabled")
            if scenario.random_seed != physical.random_seed or not math.isclose(
                scenario.simulated_time.base(), physical.simulated_time.base(), abs_tol=1e-12
            ):
                raise ValueError("scenario seed/time must match its fixed physics declaration")
            missing_limits = sorted(set(scenario.acceptance_limit_ids) - set(limits))
            if missing_limits:
                raise ValueError(
                    f"scenario {scenario.id!r} references unknown limits: {', '.join(missing_limits)}"
                )
            selected = [limits[row_id] for row_id in scenario.acceptance_limit_ids]
            if scenario.kind == "x500_takeoff_hover_land":
                position = {
                    row.metric: row.threshold.base()
                    for row in selected
                    if isinstance(row, X500PositionLimit)
                }
                expected_position = {
                    "hover_altitude_rms": 0.2,
                    "hover_altitude_peak": 0.5,
                    "horizontal_displacement": 0.5,
                }
                landing = [row for row in selected if isinstance(row, X500LandingLimit)]
                if position != expected_position or len(landing) != 1 or not math.isclose(
                    landing[0].deadline.base(), 90.0, abs_tol=1e-12
                ):
                    raise ValueError("X500 scenario requires the fixed position and land/disarm limits")
            else:
                tracking = [row for row in selected if isinstance(row, AnymalTrackingLimit)]
                contact = [row for row in selected if isinstance(row, AnymalBodyContactLimit)]
                attitude = [row for row in selected if isinstance(row, AnymalAttitudeLimit)]
                if (
                    len(tracking) != 1
                    or not math.isclose(tracking[0].rms_threshold.base(), 0.05, abs_tol=1e-12)
                    or not math.isclose(tracking[0].peak_threshold.base(), 0.15, abs_tol=1e-12)
                    or len(contact) != 1
                    or len(attitude) != 1
                    or not math.isclose(
                        attitude[0].max_abs_roll_pitch.base(),
                        scenario.max_abs_roll_pitch.base(),
                        abs_tol=1e-12,
                    )
                ):
                    raise ValueError("ANYmal scenario requires fixed tracking/contact/attitude limits")
        return self


class Gearbox(Base):
    """A CAD-lite reduction stage between two recorded joints.

    Declarative data only, like `Budget`: `ratio` and the two joint references
    are consumed by nothing yet — this is the record, not a torque-budget
    integration. `input_joint`/`output_joint` existence and any position in
    the link tree are `edit.add_entry`'s and `edit.reparent`'s job, not
    this model's; that keeps the reference check in one place.
    """

    id: str
    ratio: float
    input_joint: str
    output_joint: str
    provenance: Provenance | None = None

    @field_validator("ratio")
    @classmethod
    def _positive_ratio(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("gearbox ratio must be > 0")
        return v


class Contract(Base):
    schema_version: str = SCHEMA_VERSION
    robot: RobotInfo
    model: ModelRecord | None = None
    edges: list[DependencyEdge] = []
    assumptions: list[Assumption] = []
    families: list[DeviceFamily] = []
    device_models: list[DeviceModel] = []
    devices: list[DeviceInstance] = []
    topology: Topology | None = None
    budgets: list[Budget] = []
    gearboxes: list[Gearbox] = []
    fitted_band_allocations: list[FittedBandAllocation] = []
    actuator_cases: list[ActuatorCase] = []
    structural: StructuralInputs | None = None
    simulation: SimulationInputs | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> "Contract":
        groups = {
            "devices": self.devices,
            "device_models": self.device_models,
            "families": self.families,
            "rails": self.topology.rails if self.topology is not None else [],
            "buses": self.topology.buses if self.topology is not None else [],
            "wires": self.topology.wires if self.topology is not None else [],
            "budgets": self.budgets,
            "gearboxes": self.gearboxes,
            "assumptions": self.assumptions,
            "actuator_cases": self.actuator_cases,
        }
        for name, rows in groups.items():
            ids = [row.id for row in rows]
            duplicates = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
            if duplicates:
                raise ValueError(f"duplicate id(s) in {name}: {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _valid_fitted_band_allocations(self) -> "Contract":
        fitted_inventory: dict[str, FittedBandEvidence] = {}
        for device in self.devices:
            for envelope in device.envelopes:
                for path, evidence in envelope.fitted_bands.items():
                    inventory_path = f"devices/{device.id}/envelopes/{envelope.context}/{path}"
                    if inventory_path in fitted_inventory:
                        raise ValueError(f"duplicate fitted-band inventory path {inventory_path!r}")
                    fitted_inventory[inventory_path] = evidence

        if (fitted_inventory or self.fitted_band_allocations) and self.schema_version not in {
            "0.3",
            "0.4",
        }:
            raise ValueError(
                "fitted_bands and fitted_band_allocations require schema_version '0.3' or '0.4'"
            )
        if not fitted_inventory and not self.fitted_band_allocations:
            return self
        if fitted_inventory and not self.fitted_band_allocations:
            raise ValueError("fitted_bands requires fitted_band_allocations")

        allocated_inventory: dict[str, FittedBandAllocation] = {}
        allocated_channels: set[tuple[str, str]] = set()
        cluster_counts = Counter(row.context for row in self.fitted_band_allocations)
        for row in self.fitted_band_allocations:
            scoped_channels = {(row.context, channel) for channel in row.channels}
            overlap = allocated_channels.intersection(scoped_channels)
            if overlap:
                raise ValueError(
                    "allocation channels appear in multiple clusters: "
                    + ", ".join(f"{context}:{channel}" for context, channel in sorted(overlap))
                )
            allocated_channels.update(scoped_channels)
            expected_denominator = cluster_counts[row.context] * len(row.channels)
            if row.denominator != expected_denominator:
                raise ValueError(
                    f"allocation denominator {row.denominator} for context "
                    f"{row.context!r} does not match {cluster_counts[row.context]} "
                    f"cluster(s) × {len(row.channels)} channel(s) = "
                    f"{expected_denominator}"
                )
            for path in row.fitted_bands:
                if path in allocated_inventory:
                    raise ValueError(f"fitted-band path {path!r} appears in multiple allocations")
                allocated_inventory[path] = row

        current_paths = set(fitted_inventory)
        allocated_paths = set(allocated_inventory)
        if current_paths != allocated_paths:
            missing = sorted(current_paths - allocated_paths)
            stale = sorted(allocated_paths - current_paths)
            details = []
            if missing:
                details.append("missing from ledger: " + ", ".join(missing))
            if stale:
                details.append("not in current inventory: " + ", ".join(stale))
            raise ValueError("fitted-band allocation inventory mismatch; " + "; ".join(details))

        for path, evidence in fitted_inventory.items():
            row = allocated_inventory[path]
            expected_alpha = FITTED_BAND_ALPHA_GATE / row.denominator
            if not math.isclose(evidence.alpha, expected_alpha, rel_tol=1e-12, abs_tol=0.0):
                raise ValueError(
                    f"fitted alpha {evidence.alpha} for {path!r} does not match "
                    f"allocation {FITTED_BAND_ALPHA_GATE}/{row.denominator} = "
                    f"{expected_alpha}"
                )
        return self

    @model_validator(mode="after")
    def _valid_actuator_cases(self) -> "Contract":
        if self.actuator_cases and self.schema_version != "0.4":
            raise ValueError("actuator_cases require schema_version '0.4'")
        for case in self.actuator_cases:
            device = self.device(case.actuator_device)
            joint = (
                next((item for item in self.model.joints if item.name == case.joint), None)
                if self.model is not None
                else None
            )
            if device is None or device.drives_joint != case.joint:
                raise ValueError(
                    f"actuator case {case.id!r} must bind its driving device and joint"
                )
            if device.power is None or device.power.rail != case.rail:
                raise ValueError(f"actuator case {case.id!r} must bind its device's declared rail")
            if joint is None or joint.type not in {"revolute", "continuous"}:
                raise ValueError(f"actuator case {case.id!r} requires a declared rotary joint")
            model = self.device_model(device.model)
            if model is None or model.actuator is None:
                raise ValueError(f"actuator case {case.id!r} requires an actuator device model")
        return self

    @model_validator(mode="after")
    def _valid_structural_simulation_sections(self) -> "Contract":
        if (self.structural is not None or self.simulation is not None) and self.schema_version != "0.4":
            raise ValueError("structural and simulation sections require schema_version '0.4'")
        if self.structural is not None:
            if self.model is None:
                raise ValueError("structural section requires Contract.model")
            binding = self.structural.model
            if (
                binding.source_sha256 != self.model.source_sha256
                or binding.structure_hash != self.model.structure_hash
            ):
                raise ValueError("structural model binding does not match Contract.model hashes")
            known_links = {link.name for link in self.model.links}
            for frame in self.structural.frames:
                if frame.parent.startswith("model_link:"):
                    link = frame.parent.removeprefix("model_link:")
                    if link not in known_links:
                        raise ValueError(f"frame {frame.id!r} references unknown model link {link!r}")
        return self

    # -- lookups ---------------------------------------------------------

    def device(self, id: str) -> DeviceInstance | None:
        return next((d for d in self.devices if d.id == id), None)

    def device_model(self, id: str) -> DeviceModel | None:
        return next((m for m in self.device_models if m.id == id), None)

    def family(self, id: str) -> DeviceFamily | None:
        return next((f for f in self.families if f.id == id), None)

    def rail(self, id: str) -> Rail | None:
        if self.topology is None:
            return None
        return next((r for r in self.topology.rails if r.id == id), None)

    def wire(self, id: str) -> Wire | None:
        if self.topology is None:
            return None
        return next((w for w in self.topology.wires if w.id == id), None)

    def resolve(self, path: str) -> Any:
        """Resolve a '/'-separated contract path; KeyError if any segment misses.

        List segments match by 'id' (or 'name' for links/hosts without ids).
        """
        node: Any = self.model_dump(mode="python")
        for seg in path.split("/"):
            if isinstance(node, dict):
                if seg not in node:
                    raise KeyError(f"{path!r}: no key {seg!r}")
                node = node[seg]
            elif isinstance(node, list):
                hits = [
                    x
                    for x in node
                    if isinstance(x, dict) and (x.get("id") == seg or x.get("name") == seg)
                ]
                if not hits:
                    raise KeyError(f"{path!r}: no entry with id/name {seg!r}")
                node = hits[0]
            else:
                raise KeyError(f"{path!r}: {seg!r} reached a leaf")
        return node


# ---------------------------------------------------------------- io


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.sort_base_mapping_type_on_output = False  # keep authored order
    # match edit.py's fallback style (4/2, `edit.py::_DEFAULT_SEQUENCE_STYLE`)
    # so a first `add`/`declare` after `init` sniffs the style it just wrote instead
    # of reflowing the whole file. Indent only affects the
    # emitter, so this is a no-op for `load_contract`'s use of this same loader.
    y.indent(mapping=2, sequence=4, offset=2)
    # Same reason edit.py never re-wraps: ruamel's default width (80) folds a
    # long scalar (a sha256, a source path) onto a continuation line, which
    # then reads as a changed line on the next unrelated add/declare once the
    # editor's un-folded width redumps it.
    y.width = 10_000
    return y


_ANCHOR_ALIAS_FORBIDDEN = "YAML anchors/aliases are not permitted in a lockfile"


def _node_has_anchor(node: Any) -> bool:
    """True when ruamel attached a named anchor to this composed node."""
    anchor = getattr(node, "anchor", None)
    if anchor is None:
        return False
    return bool(getattr(anchor, "value", None))


def _reject_yaml_anchors_aliases(node: Any) -> None:
    """Refuse any loaded YAML graph that contains anchors or aliases.

    ruamel keeps aliases as shared object references and does not expand them,
    so a billion-laughs document loads as a small physical graph. Walk that
    graph once: anchor metadata (``.anchor.value``) catches every named anchor
    regardless of Unicode; a second visit to the same container ``id`` is an
    alias. The visited-set bounds the walk to unique physical nodes.
    """
    if not REJECT_YAML_ANCHORS_ALIASES:
        return
    visited: set[int] = set()

    def walk(n: Any) -> None:
        if _node_has_anchor(n):
            raise ValueError(_ANCHOR_ALIAS_FORBIDDEN)
        if isinstance(n, dict | list):
            nid = id(n)
            if nid in visited:
                raise ValueError(_ANCHOR_ALIAS_FORBIDDEN)
            visited.add(nid)
            if isinstance(n, dict):
                for key, value in n.items():
                    walk(key)
                    walk(value)
            else:
                for item in n:
                    walk(item)

    walk(node)


def _load_yaml_guarded(loader: YAML, stream: Any) -> Any:
    """Load YAML, mapping ruamel's bare ``RecursionError`` to the tool's refusal shape.

    Deeply nested YAML (e.g. thousands of ``[`` characters) blows the recursive
    descent parser's stack before any size/anchor guard runs, surfacing a raw
    traceback instead of a clean refusal. Every loader in this codebase
    (contract, plan, recording manifest) routes through here.
    """
    try:
        return loader.load(stream)
    except RecursionError as error:
        raise ValueError("YAML nesting too deep to parse safely") from error


def _guard_contract_raw(path: Path) -> None:
    """Refuse oversized contracts cheaply, before YAML parse or validation.

    Byte-size is checked from ``Path.stat`` (no read) so a giant scalar never
    reaches ruamel's scanner. Anchors/aliases are refused after load by
    ``_reject_yaml_anchors_aliases`` — the parser decides membership.
    """
    size = path.stat().st_size
    if size > MAX_CONTRACT_BYTES:
        raise ValueError(f"contract file exceeds size limit ({size} bytes > {MAX_CONTRACT_BYTES})")


class UnsupportedSchemaVersion(ValueError):
    """A contract declares a ``schema_version`` this lockwire cannot read.

    Error path only — no schema *field* is added, moved or retyped by this
    class (the schema freeze binds the fields, not the refusal a bad one raises).
    It subclasses ``ValueError`` so every existing ``except ValueError`` caller
    keeps working; the point of the type is that a door can recognise this one
    failure and refuse it as a verdict (REFUSED, exit 2) instead of
    printing a plain "failed" line, or, for a library caller, an anonymous
    ``ValueError`` indistinguishable from a parse error.
    """

    def __init__(self, found: object, supported: frozenset[str]) -> None:
        self.found = found
        self.supported = ", ".join(sorted(supported))
        super().__init__(
            f"contract declares schema_version {found!r}; this lockwire supports {self.supported}"
        )


def load_contract(path: Path) -> Contract:
    _guard_contract_raw(path)
    return load_contract_bytes(path.read_bytes())


def load_contract_bytes(raw: bytes) -> Contract:
    """Validate one caller-held contract byte sequence without reopening its path."""

    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError(f"contract file exceeds size limit ({len(raw)} bytes > {MAX_CONTRACT_BYTES})")
    data = _load_yaml_guarded(_yaml(), StringIO(raw.decode("utf-8")))
    _reject_yaml_anchors_aliases(data)
    declared = data.get("schema_version", SCHEMA_VERSION) if isinstance(data, dict) else None
    if declared not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(declared, SUPPORTED_SCHEMA_VERSIONS)
    return Contract.model_validate(data)


def upgrade_contract_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a proposed v0.4 document without mutating the caller's object."""

    proposed = deepcopy(document)
    declared = proposed.get("schema_version", "0.2")
    if declared not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(declared, SUPPORTED_SCHEMA_VERSIONS)
    if declared in {"0.2", "0.3"} and ("structural" in proposed or "simulation" in proposed):
        raise ValueError("legacy contract must not contain structural or simulation v1 sections")
    if declared in {"0.2", "0.3"} and proposed.get("actuator_cases"):
        raise ValueError("legacy contract must not contain actuator_cases")
    proposed["schema_version"] = "0.4"
    return proposed


def upgrade_contract_text(text: str) -> str:
    """Pure in-memory preview of the explicit v0.4 upgrade."""

    data = _load_yaml_guarded(_yaml(), StringIO(text))
    _reject_yaml_anchors_aliases(data)
    if not isinstance(data, dict):
        raise ValueError("contract document must be a mapping")
    output = StringIO()
    _yaml().dump(upgrade_contract_document(data), output)
    return output.getvalue()


def save_contract(contract: Contract, path: Path) -> None:
    data = contract.model_dump(mode="json", exclude_none=True)
    with open(path, "w") as f:
        _yaml().dump(data, f)
