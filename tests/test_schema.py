import datetime as dt
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from lockwire.checks import check_reference_integrity
from lockwire.schema import (
    MAX_CONTRACT_BYTES,
    REJECT_YAML_ANCHORS_ALIASES,
    Actuator,
    Band,
    Battery,
    Budget,
    Bus,
    ContextEnvelope,
    Contract,
    DeviceFamily,
    DeviceIdentity,
    DeviceInstance,
    DeviceModel,
    Driver,
    FittedBandAllocation,
    FittedBandEvidence,
    Host,
    InputVoltageRange,
    Joint,
    JointLimits,
    Link,
    LinkBudget,
    ModelRecord,
    Physical,
    PowerDraw,
    PowerMode,
    Provenance,
    Quantity,
    Rail,
    ReviewNote,
    RobotInfo,
    SignalEnvelope,
    ThermalLimit,
    Tolerance,
    Wire,
    load_contract,
    save_contract,
    upgrade_contract_document,
    upgrade_contract_text,
)


def _clean_data() -> dict:
    """Load the checked-in fixture, which is natively v0.2.

    This previously migrated a v0.1 fixture in memory. The file itself is now migrated, so the in-memory
    migration became a double-migration and raised KeyError('bus').
    """
    path = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    with open(path) as f:
        return YAML().load(f)


def test_dds_deadline_must_be_an_integral_nanosecond_count():
    """DDS Duration cannot represent a fractional nanosecond honestly."""
    assert Driver(dds_deadline_s=3e-9).dds_deadline_s == 3e-9
    with pytest.raises(ValidationError, match="integral number of nanoseconds"):
        Driver(dds_deadline_s=1.5e-9)


@pytest.fixture
def clean_contract() -> Contract:
    return Contract.model_validate(_clean_data())


def _rich_contract() -> Contract:
    return Contract.model_validate(
        {
            "schema_version": "0.2",
            "robot": {"name": "richbot"},
            "model": {
                "source": "robot.urdf",
                "source_sha256": "a" * 64,
                "structure_hash": "derived-not-authored",
                "links": [
                    {"name": "base_link", "mass": {"value": 4.0, "unit": "kg"}},
                    {"name": "wheel_link", "mass": {"value": 0.5, "unit": "kg"}},
                ],
                "joints": [
                    {
                        "name": "wheel_joint",
                        "type": "revolute",
                        "parent": "base_link",
                        "child": "wheel_link",
                        "origin": {"xyz": [0.2, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]},
                        "axis": [0.0, 1.0, 0.0],
                        "limits": {
                            "lower": -3.14,
                            "upper": 3.14,
                            "effort": 8.0,
                            "velocity": 20.0,
                        },
                    }
                ],
                "provenance": {
                    "witness": "w2",
                    "source": "robot.urdf",
                    "locked_at": "2026-07-25",
                },
            },
            "families": [{"id": "fam.motor", "name": "Drive motor"}],
            "device_models": [
                {
                    "id": "mdl.motor",
                    "family": "fam.motor",
                    "actuator": {
                        "gear_ratio": 6.0,
                        "torque_constant": 0.08,
                        "torque_continuous": 1.2,
                        "torque_peak": 2.4,
                        "encoder_cpr": 2048,
                    },
                }
            ],
            "devices": [
                {
                    "id": "dev.motor.left",
                    "model": "mdl.motor",
                    "drives_joint": "wheel_joint",
                    "identity": {
                        "bus_ref": "bus.can0",
                        "host": "computer",
                        "can_id": "42",
                    },
                    "driver": {
                        "node": "/drive",
                        "nominal_rate_hz": 100.0,
                        "payload_bytes": 16,
                    },
                    "physical": {
                        "mass": {"value": 300, "unit": "g"},
                        "mount_frame": "base_link",
                    },
                    "power": {
                        "rail": "rail.5v",
                        "draw": {"value": 10, "unit": "W"},
                    },
                    "envelopes": [
                        {
                            "context": "nominal-op",
                            "rate_hz": {"lo": 95, "hi": 105, "unit": "Hz"},
                            "n": 200,
                            "n_eff": 140,
                            "coverage": 0.95,
                            "confidence": 0.95,
                            "signals": {
                                "speed": {
                                    "band": {"lo": -20, "hi": 20, "unit": "rad/s"},
                                    "variance_floor": 0.001,
                                    "n": 200,
                                    "n_eff": 140,
                                    "coverage": 0.95,
                                    "confidence": 0.95,
                                }
                            },
                            "provenance": {
                                "witness": "live",
                                "source": "bag-001",
                                "locked_at": "2026-07-25",
                            },
                        }
                    ],
                    "owner": "robotics",
                }
            ],
            "topology": {
                "hosts": [{"id": "computer"}],
                "buses": [
                    {
                        "id": "bus.can0",
                        "kind": "can",
                        "host": "computer",
                        "bitrate": 1_000_000,
                    }
                ],
                "rails": [
                    {
                        "id": "rail.12v",
                        "voltage": {"value": 12, "unit": "V"},
                        "capacity": {"value": 120, "unit": "W"},
                        "upstream": "battery",
                    },
                    {
                        "id": "rail.5v",
                        "voltage": {"value": 5, "unit": "V"},
                        "capacity": {"value": 30, "unit": "W"},
                        "upstream": "rail.12v",
                        "efficiency": 0.85,
                    },
                ],
                "wires": [
                    {
                        "id": "wire.rail.5v",
                        "awg": 16,
                        "installation": "enclosed",
                        "rail": "rail.5v",
                        "owner": "robotics",
                    }
                ],
                "battery": {
                    "capacity": {"value": 100, "unit": "Wh"},
                    "voltage": {"value": 22.2, "unit": "V"},
                },
            },
            "budgets": [
                {
                    "id": "bud.rail.5v",
                    "kind": "rail_power",
                    "limit": {"value": 25, "unit": "W"},
                    "rail": "rail.5v",
                    "owner": "robotics",
                }
            ],
        }
    )


def test_fixture_loads_and_round_trips(clean_contract, tmp_path):
    out = tmp_path / "roundtrip.yaml"
    save_contract(clean_contract, out)
    # MUTATION-BITE: catches save/load dropping or changing existing contract fields.
    assert load_contract(out) == clean_contract


def test_device_input_voltage_range_is_optional(clean_contract):
    assert all(
        model.input_voltage_range is None for model in clean_contract.device_models
    )  # Standing question: fails if the additive field makes old contracts declare a range.


@pytest.mark.parametrize("field", ["min", "max"])
def test_device_input_voltage_range_requires_voltage_quantities(field):
    values = {
        "min": Quantity(value=7, unit="V"),
        "max": Quantity(value=20, unit="V"),
    }
    values[field] = Quantity(value=7, unit="W")

    with pytest.raises(ValidationError, match="requires dimension 'voltage'"):
        InputVoltageRange(**values)
    # Standing question: fails if either range endpoint stops enforcing the voltage role.


def test_device_input_voltage_range_must_be_ordered():
    with pytest.raises(ValidationError, match="input voltage range min"):
        DeviceModel(
            id="mdl.sensor",
            family="fam.sensor",
            input_voltage_range=InputVoltageRange(
                min=Quantity(value=20, unit="V"),
                max=Quantity(value=7, unit="V"),
            ),
        )
    # Standing question: fails if an inverted declared range is accepted as meaningful.


def test_rail_voltage_range_and_current_limit_are_optional(clean_contract):
    assert all(
        rail.voltage_range is None and rail.current_limit is None
        for rail in (clean_contract.topology.rails if clean_contract.topology else [])
    )  # absent -> not scored, never zero; fails if the additive fields self-populate.


def test_rail_voltage_range_must_be_ordered():
    with pytest.raises(ValidationError, match="input voltage range min"):
        Rail(
            id="rail.new",
            capacity=Quantity(value=10, unit="W"),
            voltage_range=InputVoltageRange(
                min=Quantity(value=50, unit="V"),
                max=Quantity(value=30, unit="V"),
            ),
        )


def test_rail_voltage_outside_voltage_range_refused():
    with pytest.raises(ValidationError, match="outside declared voltage_range"):
        Rail(
            id="rail.new",
            capacity=Quantity(value=10, unit="W"),
            voltage=Quantity(value=60, unit="V"),
            voltage_range=InputVoltageRange(
                min=Quantity(value=30, unit="V"),
                max=Quantity(value=50, unit="V"),
            ),
        )


def test_rail_with_only_voltage_range_loads():
    rail = Rail(
        id="rail.new",
        capacity=Quantity(value=10, unit="W"),
        voltage_range=InputVoltageRange(
            min=Quantity(value=30.4, unit="V"),
            max=Quantity(value=50.4, unit="V"),
        ),
        current_limit=Quantity(value=3, unit="A"),
    )
    assert rail.voltage is None
    assert rail.voltage_range.min.value == 30.4
    assert rail.current_limit.base() == 3.0


def test_rail_current_limit_requires_current_dimension():
    with pytest.raises(ValidationError, match="requires dimension 'current'"):
        Rail(
            id="rail.new",
            capacity=Quantity(value=10, unit="W"),
            current_limit=Quantity(value=3, unit="V"),
        )


def _contract_files() -> list[Path]:
    """Every checked-in *contract* yaml — not bag manifests or recording
    baselines, which share the .yaml extension but aren't Contract documents."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    examples_dir = (Path(__file__).parent / ".." / "examples").resolve()
    candidates = sorted((fixtures_dir).glob("*.yaml")) + sorted(
        (fixtures_dir / "x500").glob("*.yaml")
    )
    for path in sorted(examples_dir.glob("**/*.yaml")):
        if (
            "bags" in path.parts
            or path.name == "manifest.yaml"
            or path.name.startswith("baseline_")
        ):
            continue
        candidates.append(path)
    return candidates


@pytest.mark.parametrize(
    "path", _contract_files(), ids=lambda p: str(p.relative_to(Path(__file__).parent.parent))
)
def test_every_checked_in_fixture_and_example_still_loads(path):
    # MUTATION-BITE-adjacent: catches Rail.voltage_range/current_limit breaking a
    # checked-in contract that never declares either field.
    load_contract(path)


def test_v02_round_trip_preserves_every_new_field(tmp_path):
    contract = _rich_contract()
    out = tmp_path / "rich-roundtrip.yaml"
    save_contract(contract, out)
    # MUTATION-BITE: catches any new field omitted by serialization or validation.
    assert load_contract(out) == contract


def _datasheet_provenance() -> dict:
    return {
        "witness": "w0",
        "source": "vendor-datasheet.pdf",
        "locked_at": "2026-07-26",
    }


@pytest.mark.parametrize(
    "entity",
    [
        DeviceFamily(
            id="fam.motor",
            name="Motor",
            min_capture_window_s=30,
            provenance=_datasheet_provenance(),
        ),
        Host(
            id="flight-controller",
            provenance=_datasheet_provenance(),
        ),
        Bus(
            id="bus.can0",
            kind="can",
            host="flight-controller",
            bitrate=1_000_000,
            provenance=_datasheet_provenance(),
        ),
        Rail(
            id="rail.5v",
            voltage=Quantity(value=5, unit="V"),
            capacity=Quantity(value=15, unit="W"),
            provenance=_datasheet_provenance(),
        ),
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="power",
            provenance=_datasheet_provenance(),
        ),
        Budget(
            id="bud.rail.5v",
            kind="rail_power",
            limit=Quantity(value=15, unit="W"),
            rail="rail.5v",
            owner="power",
            provenance=_datasheet_provenance(),
        ),
    ],
    ids=["family", "host", "bus", "rail", "wire", "budget"],
)
def test_provenance_attaches_to_engineering_entities(entity):
    # MUTATION-BITE: each row fails if that entity drops or rejects its provenance block.
    assert entity.provenance == Provenance.model_validate(_datasheet_provenance())


def test_engineering_entities_without_provenance_still_validate():
    contract = _rich_contract()
    # MUTATION-BITE: catches making any newly provenance-capable entity require the field.
    assert contract.families[0].provenance is None
    assert contract.topology.hosts[0].provenance is None
    assert contract.topology.buses[0].provenance is None
    assert contract.topology.rails[0].provenance is None
    assert contract.budgets[0].provenance is None


def _review_note(**overrides) -> dict:
    data = {
        "state": "questioned",
        "witness": "Maya Chen",
        "at": "2026-09-08",
        "note": "the datasheet page does not say 20.8 W",
    }
    data.update(overrides)
    return data


def test_review_state_is_only_approved_or_questioned():
    ReviewNote.model_validate(_review_note(state="approved", note=None))
    ReviewNote.model_validate(_review_note(state="questioned"))
    # MUTATION-BITE: accepting any other string breaks this refusal.
    with pytest.raises(ValidationError):
        ReviewNote.model_validate(_review_note(state="maybe"))


@pytest.mark.parametrize("bad_witness", ["", "   ", "you", "You", "claude", "assistant"])
def test_review_note_refuses_a_blank_or_you_witness(bad_witness):
    # MUTATION-BITE: skipping validate_witness on this field breaks every case here.
    with pytest.raises(ValidationError):
        ReviewNote.model_validate(_review_note(witness=bad_witness))


def test_a_questioned_review_requires_a_note():
    ReviewNote.model_validate(_review_note(state="approved", note=None))
    # MUTATION-BITE: making note optional for "questioned" breaks this refusal.
    with pytest.raises(ValidationError):
        ReviewNote.model_validate(_review_note(state="questioned", note=None))


def test_a_contract_with_review_notes_round_trips(tmp_path):
    # Parity-anchor law: anchored on the checked-in fixture's literal text, not
    # on a model also dumped.
    fixture = Path(__file__).parent / "fixtures" / "review_notes.yaml"
    contract = load_contract(fixture)
    review = contract.topology.rails[0].provenance.reviews[0]
    assert (review.state, review.witness, review.note) == (
        "questioned",
        "Maya Chen",
        "the datasheet page does not say 20.8 W",
    )
    out = tmp_path / "review-roundtrip.yaml"
    save_contract(contract, out)
    assert load_contract(out) == contract


def test_a_contract_with_no_reviews_key_writes_back_with_no_reviews_key(tmp_path):
    # reviews is optional; absent must stay absent. A contract that never
    # mentions reviews must not gain `reviews: []` in every provenance block
    # (contract_clean.yaml carries several) the first time anything writes it
    # -- that's a silent migration of every checked-in lockfile arriving as a
    # side effect. Byte-diff, not object-equality: an object round-trip would
    # still pass while the file text quietly grew the key.
    #
    # The "original" is this box's own first save, not the checked-in fixture
    # text verbatim -- save_contract's dumper normalises quoting/comments
    # regardless of reviews, so a diff against the hand-written fixture would
    # fail on that noise. Comparing a save to a save isolates exactly the
    # question this test asks: does loading a no-reviews contract and saving
    # it again change the bytes.
    fixture = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    original = tmp_path / "original.yaml"
    save_contract(load_contract(fixture), original)
    assert "reviews" not in original.read_text()

    resaved = tmp_path / "resaved.yaml"
    save_contract(load_contract(original), resaved)
    # MUTATION-BITE: `reviews: list[ReviewNote] = []` (no `| None`) reintroduces
    # `reviews: []` in every provenance block on this second save and the
    # diff goes non-empty.
    assert resaved.read_text() == original.read_text()


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (DeviceFamily, {"id": "fam.motor", "name": "Motor"}),
        (Host, {"id": "computer"}),
        (Bus, {"id": "bus.can0", "kind": "can", "host": "computer"}),
        (
            Rail,
            {
                "id": "rail.5v",
                "voltage": {"value": 5, "unit": "V"},
                "capacity": {"value": 15, "unit": "W"},
            },
        ),
        (
            Wire,
            {
                "id": "wire.rail.5v",
                "awg": 16,
                "installation": "free_air",
                "rail": "rail.5v",
                "owner": "power",
            },
        ),
        (
            Budget,
            {
                "id": "bud.rail.5v",
                "kind": "rail_power",
                "limit": {"value": 15, "unit": "W"},
                "rail": "rail.5v",
                "owner": "power",
            },
        ),
    ],
    ids=["family", "host", "bus", "rail", "wire", "budget"],
)
def test_malformed_provenance_fails_loudly(model, data):
    # MUTATION-BITE: catches a loose scalar provenance shape being silently accepted.
    with pytest.raises(ValidationError, match="provenance"):
        model.model_validate({**data, "provenance": "vendor-datasheet.pdf"})


def test_unknown_key_rejected():
    # MUTATION-BITE: catches weakening the closed-world extra="forbid" contract.
    with pytest.raises(ValidationError):
        Contract.model_validate({"robot": {"name": "x"}, "budgetts": []})


def test_unknown_unit_rejected():
    # MUTATION-BITE: catches Quantity accepting units the conversion table cannot handle.
    with pytest.raises(ValidationError):
        Quantity(value=1.0, unit="furlong")


def test_bag_units_validate_with_deliberate_conversion_semantics():
    pressure_pa = Band(lo=101_300, hi=101_400, unit="Pa")
    pressure_hpa = Band(lo=1013, hi=1014, unit="hPa")
    temperature_bag = Band(lo=20, hi=25, unit="degC")
    temperature_contract = Band(lo=20, hi=25, unit="C")

    # MUTATION-BITE: catches Pa being silently aliased to hPa despite the 100x magnitude.
    assert pressure_hpa.contains(pressure_pa)
    # MUTATION-BITE: catches degC diverging from the existing C spelling.
    assert temperature_contract.contains(temperature_bag)


def test_gnss_degree_units_validate():
    envelope = ContextEnvelope(
        context="nominal-op",
        signals={
            "latitude_deg": SignalEnvelope(band=Band(lo=43.6, hi=43.7, unit="deg")),
            "longitude_deg": SignalEnvelope(band=Band(lo=-79.4, hi=-79.3, unit="deg")),
        },
        provenance=Provenance(
            witness="live",
            source="clean-bag",
            locked_at="2026-07-26",
        ),
    )

    # MUTATION-BITE: catches the bag's scalar angle vocabulary being rejected or rewritten.
    assert envelope.signals["latitude_deg"].band.unit == "deg"
    assert envelope.signals["longitude_deg"].band.unit == "deg"


def test_oneshot_variance_min_is_independent_of_variance_floor():
    """Stage-3 field coexists with CUSUM reference; neither aliases the other."""
    signal = SignalEnvelope(
        band=Band(lo=-10.5, hi=-9.0, unit="m/s^2"),
        variance_floor=1.0e-4,
        oneshot_variance_min=5.8e-5,
    )
    assert signal.variance_floor == 1.0e-4
    assert signal.oneshot_variance_min == 5.8e-5
    # Extra key rejection still holds (typo must not silently become a floor).
    with pytest.raises(ValidationError):
        SignalEnvelope(
            band=Band(lo=-10.5, hi=-9.0, unit="m/s^2"),
            variance_min=0.001,  # type: ignore[call-arg]
        )


def test_clean_contract_declares_oneshot_variance_min_on_imu_accel_z():
    contract = load_contract(Path(__file__).parent / "fixtures" / "contract_clean.yaml")
    imu = next(d for d in contract.devices if d.id == "dev.imu")
    # A device may declare several envelopes per context: the shape law
    # forbids mixed provenance inside one envelope.
    nominal = next(e for e in imu.envelopes if e.context == "nominal-op" and "accel_z" in e.signals)
    assert "accel_z" in nominal.signals
    floor = nominal.signals["accel_z"].oneshot_variance_min
    assert floor is not None and floor > 0.0
    # CUSUM reference on bench-idle must remain untouched as its own field.
    bench = next(e for e in imu.envelopes if e.context == "bench-idle")
    assert bench.signals["accel_z"].variance_floor == 1.0e-4
    assert bench.signals["accel_z"].oneshot_variance_min == floor


def test_invalid_unit_rejected_on_assignment():
    quantity = Quantity(value=2.0, unit="W")
    # MUTATION-BITE: catches programmatic mutation bypassing the known-unit validator.
    with pytest.raises(ValidationError, match="unknown unit 'banana'"):
        quantity.unit = "banana"


def test_power_draw_dimension_rejected_on_assignment():
    power = PowerDraw(rail="rail.5v", draw=Quantity(value=2.0, unit="W"))
    # MUTATION-BITE: catches assignment bypassing the dimension guard for a power-role field.
    with pytest.raises(ValidationError, match="requires dimension 'power'.*'current'"):
        power.draw = Quantity(value=2.0, unit="A")


def test_in_place_list_mutation_requires_explicit_revalidation(clean_contract):
    original_count = len(clean_contract.devices)
    clean_contract.devices.append(clean_contract.devices[0].model_copy(deep=True))
    # MUTATION-BITE: documents that list append is not pydantic field assignment.
    assert len(clean_contract.devices) == original_count + 1
    # MUTATION-BITE: catches explicit revalidation missing duplicate ids added in place.
    with pytest.raises(ValidationError, match=r"duplicate id\(s\) in devices"):
        clean_contract.revalidate()


def test_power_draw_rejects_current_dimension():
    # MUTATION-BITE: catches the live wrong-GO bug where a power draw in amps is summed as watts.
    with pytest.raises(ValidationError, match="requires dimension 'power'.*'current'"):
        PowerDraw(rail="rail.5v", draw=Quantity(value=2.0, unit="A"))


def test_power_draw_is_optional():
    """A device on a rail with no declared draw loads cleanly — an
    unknown consumer, never a 0 W one."""
    power = PowerDraw(rail="rail.5v")
    assert power.draw is None


def test_power_modes_require_a_fallback_draw():
    """Modes need a nominal draw to fall back to — a device with modes
    but no nominal draw is refused, not silently unscoreable per mode."""
    with pytest.raises(ValidationError, match="modes require a nominal 'draw'"):
        PowerDraw(
            rail="rail.5v",
            modes=[PowerMode(name="peak", draw=Quantity(value=3.0, unit="W"))],
        )  # bites modes surviving with no fallback draw


def test_power_choices_are_optional_declared_unique_mode_names():
    modes = [PowerMode(name="low", draw=Quantity(value=3.0, unit="W"))]
    power = PowerDraw(
        rail="rail.5v",
        draw=Quantity(value=5.0, unit="W"),
        modes=modes,
        choices=["nominal", "low"],
    )
    assert power.choices == ["nominal", "low"]
    assert PowerDraw(rail="rail.5v", draw=Quantity(value=5.0, unit="W")).choices is None

    for choices in ([], [""], ["   "], ["low", "low"], ["missing"]):
        with pytest.raises(ValidationError):
            PowerDraw(
                rail="rail.5v",
                draw=Quantity(value=5.0, unit="W"),
                modes=modes,
                choices=choices,
            )


def test_rail_voltage_is_optional_capacity_still_required():
    """A rail with no declared voltage loads cleanly; capacity is still
    required — it's the one thing power budgets need regardless of voltage."""
    rail = Rail(id="rail.new", capacity=Quantity(value=10, unit="W"))
    assert rail.voltage is None

    with pytest.raises(ValidationError, match="capacity"):
        Rail.model_validate({"id": "rail.new", "voltage": {"value": 5, "unit": "V"}})


def test_draw_mode_rejects_non_power_quantity():
    # MUTATION-BITE: catches named mode draws bypassing the power-role guard.
    with pytest.raises(ValidationError, match="requires dimension 'power'.*'current'"):
        PowerMode(name="peak", draw=Quantity(value=3.0, unit="A"))


def test_mount_xyz_is_optional_and_requires_length_quantities():
    legacy = Physical(mass=Quantity(value=1, unit="kg"), mount_frame="base")
    assert legacy.mount_xyz is None  # bites making the additive field required

    with pytest.raises(ValidationError, match="requires dimension 'length'.*'time'"):
        Physical(
            mass=Quantity(value=1, unit="kg"),
            mount_frame="base",
            mount_xyz=(
                Quantity(value=0, unit="m"),
                Quantity(value=0, unit="m"),
                Quantity(value=1, unit="s"),
            ),
        )


def test_physical_mass_is_optional_mount_frame_still_required():
    """A physical block with a frame (and optionally an offset) but no mass
    loads cleanly — a missing mass is a coverage gap, not a validation error."""
    placed_only = Physical(mount_frame="base")
    assert placed_only.mass is None


def test_physical_mount_frame_is_optional_too():
    """Mount_frame becomes optional as well — a bare mass with no frame
    loads cleanly (excluded from CG, disclosed, never a validation error)."""
    weighed_only = Physical.model_validate({})
    assert weighed_only.mass is None
    assert weighed_only.mount_frame is None


def test_mount_xyz_still_requires_mount_frame():
    """Mount_frame becoming optional must not let mount_xyz float free of
    a frame — the offset means nothing without one."""
    with pytest.raises(ValidationError, match="mount_xyz requires mount_frame"):
        Physical(
            mount_xyz=(
                Quantity(value=0, unit="m"),
                Quantity(value=0, unit="m"),
                Quantity(value=0, unit="m"),
            )
        )  # bites mount_xyz surviving with no mount_frame


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: Physical(
                mass=Quantity(value=1, unit="W"),
                mount_frame="base",
            ),
            "requires dimension 'mass'",
        ),
        (
            lambda: Rail(
                id="rail",
                voltage=Quantity(value=5, unit="W"),
                capacity=Quantity(value=5, unit="W"),
            ),
            "requires dimension 'voltage'",
        ),
        (
            lambda: Rail(
                id="rail",
                voltage=Quantity(value=5, unit="V"),
                capacity=Quantity(value=2, unit="A"),
            ),
            "requires dimension 'power'",
        ),
        (
            lambda: Battery(capacity=Quantity(value=5, unit="W")),
            "requires dimension 'energy'",
        ),
        (
            lambda: Battery(
                capacity=Quantity(value=5, unit="Wh"),
                voltage=Quantity(value=5, unit="W"),
            ),
            "requires dimension 'voltage'",
        ),
        (
            lambda: Budget(
                id="bud",
                kind="endurance",
                limit=Quantity(value=30, unit="W"),
                owner="robotics",
            ),
            "requires dimension 'time'",
        ),
        (
            lambda: Budget(
                id="bud",
                kind="mass_total",
                limit=Quantity(value=30, unit="min"),
                owner="robotics",
            ),
            "requires dimension 'mass'",
        ),
        (
            lambda: Budget(
                id="bud",
                kind="rail_power",
                limit=Quantity(value=2, unit="A"),
                rail="rail",
                owner="robotics",
            ),
            "requires dimension 'power'",
        ),
    ],
)
def test_every_quantity_role_rejects_the_wrong_dimension(factory, match):
    # MUTATION-BITE: each row kills removal or miswiring of one role-specific dimension guard.
    with pytest.raises(ValidationError, match=match):
        factory()


def test_band_ordering_enforced():
    # MUTATION-BITE: catches inverted envelope bounds reaching comparison code.
    with pytest.raises(ValidationError):
        Band(lo=10, hi=-10, unit="Hz")


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_numbers_refused(bad):
    # a NaN/inf band must never load (NaN compares False, so it would pass the gate).
    with pytest.raises(ValidationError, match="finite"):
        Band(lo=bad, hi=bad, unit="Hz")


def test_tolerance_exactly_one():
    # MUTATION-BITE: catches ambiguous tolerances carrying both relative and absolute values.
    with pytest.raises(ValidationError):
        Tolerance(rel_pct=5, abs=Quantity(value=1, unit="g"))
    # MUTATION-BITE: catches empty tolerances reaching dependency checks.
    with pytest.raises(ValidationError):
        Tolerance()


def test_unknown_context_rejected(clean_contract):
    dumped = clean_contract.model_dump(mode="json")
    dumped["devices"][0]["envelopes"][0]["context"] = "orbit"
    # MUTATION-BITE: catches context proliferation outside the locked context set.
    with pytest.raises(ValidationError):
        Contract.model_validate(dumped)


def test_resolve_paths(clean_contract):
    mass = clean_contract.resolve("devices/dev.imu/physical/mass")
    # MUTATION-BITE: catches list resolution selecting the wrong device row.
    assert mass["value"] == 12
    # MUTATION-BITE: catches resolve losing the full Quantity representation.
    assert mass["unit"] == "g"
    # MUTATION-BITE: catches missing paths silently resolving to another row.
    with pytest.raises(KeyError):
        clean_contract.resolve("devices/dev.nope/physical/mass")


def test_minimal_contract_valid():
    contract = Contract(robot=RobotInfo(name="bare"))
    # MUTATION-BITE: catches failure to advance the authored contract default to v0.4.
    assert contract.schema_version == "0.4"


def test_v04_upgrade_is_pure_and_legacy_documents_remain_legacy():
    legacy = {"schema_version": "0.2", "robot": {"name": "legacy"}}
    proposal = upgrade_contract_document(legacy)
    assert legacy["schema_version"] == "0.2"
    assert proposal["schema_version"] == "0.4"
    assert Contract.model_validate(legacy).schema_version == "0.2"
    assert "schema_version: '0.4'" in upgrade_contract_text(
        'schema_version: "0.3"\nrobot:\n  name: legacy\n'
    )


def test_v04_upgrade_refuses_actuator_cases_hidden_in_legacy_version():
    fixture = Path(__file__).parent / "fixtures" / "actuator_review_e2e" / "contract.yaml"
    legacy = json.loads(fixture.read_text())
    legacy["schema_version"] = "0.3"
    legacy.pop("structural")
    legacy.pop("simulation")
    assert legacy["actuator_cases"]
    with pytest.raises(ValidationError, match="actuator_cases require schema_version '0.4'"):
        Contract.model_validate(legacy)

    with pytest.raises(ValueError, match="legacy contract must not contain actuator_cases"):
        upgrade_contract_document(legacy)
    assert legacy["schema_version"] == "0.3"
    empty = {"schema_version": "0.2", "robot": {"name": "old"}, "actuator_cases": []}
    assert upgrade_contract_document(empty)["actuator_cases"] == []


def _fitted_contract() -> Contract:
    path = "devices/dev.sensor/envelopes/nominal-op/rate_hz"
    return Contract(
        robot=RobotInfo(name="fitted"),
        devices=[
            DeviceInstance(
                id="dev.sensor",
                model="mdl.sensor",
                owner="test",
                envelopes=[
                    ContextEnvelope(
                        context="nominal-op",
                        rate_hz=Band(lo=9.0, hi=11.0, unit="Hz"),
                        fitted_bands={
                            "rate_hz": FittedBandEvidence(
                                rank=1,
                                n_eff=100,
                                alpha=0.005,
                            )
                        },
                        provenance=Provenance(
                            witness="live",
                            source="known-good",
                            locked_at="2026-07-29",
                            bag_sha256="a" * 64,
                        ),
                    )
                ],
            )
        ],
        fitted_band_allocations=[
            FittedBandAllocation(
                context="nominal-op",
                channels=["rate/dev.sensor"],
                fitted_bands=[path],
                denominator=1,
            )
        ],
    )


def test_fitted_band_schema_and_allocation_ledger_validate_together():
    contract = _fitted_contract()
    evidence = contract.devices[0].envelopes[0].fitted_bands["rate_hz"]
    assert evidence.rank == 1
    assert evidence.n_eff == 100
    assert evidence.alpha == 0.005
    assert contract.fitted_band_allocations[0].denominator == 1
    legacy_v03 = contract.model_dump(mode="python")
    legacy_v03["schema_version"] = "0.3"
    assert Contract.model_validate(legacy_v03).schema_version == "0.3"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rank", True),
        ("rank", 0),
        ("n_eff", 1.5),
        ("n_eff", -1),
        ("alpha", float("nan")),
        ("alpha", 0.0),
    ],
)
def test_fitted_band_evidence_rejects_invalid_operands(field, value):
    data = {"rank": 1, "n_eff": 10, "alpha": 0.005}
    data[field] = value
    with pytest.raises(ValidationError):
        FittedBandEvidence.model_validate(data)


def test_fitted_band_evidence_rejects_rank_beyond_effective_count():
    with pytest.raises(ValidationError, match="cannot exceed"):
        FittedBandEvidence(rank=11, n_eff=10, alpha=0.005)


def test_fitted_band_requires_declared_path_and_live_hashed_witness():
    with pytest.raises(ValidationError, match="undeclared"):
        ContextEnvelope(
            context="nominal-op",
            fitted_bands={"rate_hz": FittedBandEvidence(rank=1, n_eff=10, alpha=0.005)},
            provenance=Provenance(
                witness="live",
                source="known-good",
                locked_at="2026-07-29",
                bag_sha256="a" * 64,
            ),
        )
    with pytest.raises(ValidationError, match="witness 'live'"):
        ContextEnvelope(
            context="nominal-op",
            rate_hz=Band(lo=9.0, hi=11.0, unit="Hz"),
            fitted_bands={"rate_hz": FittedBandEvidence(rank=1, n_eff=10, alpha=0.005)},
            provenance=Provenance(
                witness="human",
                source="asserted",
                locked_at="2026-07-29",
            ),
        )


def test_fitted_band_ledger_rejects_stale_alpha_inventory_and_denominator():
    contract = _fitted_contract()
    dumped = contract.model_dump(mode="python")
    dumped["devices"][0]["envelopes"][0]["fitted_bands"]["rate_hz"]["alpha"] = 0.004
    with pytest.raises(ValidationError, match="does not match allocation"):
        Contract.model_validate(dumped)

    dumped = contract.model_dump(mode="python")
    dumped["fitted_band_allocations"][0]["fitted_bands"] = []
    with pytest.raises(ValidationError, match="inventory mismatch"):
        Contract.model_validate(dumped)

    dumped = contract.model_dump(mode="python")
    dumped["fitted_band_allocations"][0]["denominator"] = 2
    with pytest.raises(ValidationError, match="does not match"):
        Contract.model_validate(dumped)


def test_schema_v02_remains_read_compatible_but_cannot_carry_fitted_data():
    old = Contract.model_validate({"schema_version": "0.2", "robot": {"name": "old"}})
    assert old.schema_version == "0.2"

    dumped = _fitted_contract().model_dump(mode="python")
    dumped["schema_version"] = "0.2"
    with pytest.raises(ValidationError, match="require schema_version '0.3' or '0.4'"):
        Contract.model_validate(dumped)


def test_load_rejects_unsupported_schema_version(tmp_path):
    contract_path = tmp_path / "future.yaml"
    contract_path.write_text('schema_version: "99.7"\nrobot:\n  name: futurebot\n')
    # MUTATION-BITE: catches the live bug where an arbitrary future version loads and reports GO.
    with pytest.raises(ValueError, match=r"99\.7.*0\.2"):
        load_contract(contract_path)


def test_old_bus_key_fails_loudly():
    data = {
        "schema_version": "0.2",
        "robot": {"name": "old-format"},
        "devices": [
            {
                "id": "dev.old",
                "model": "mdl.old",
                "identity": {"bus": "usb", "host": "computer"},
                "owner": "robotics",
            }
        ],
    }
    # MUTATION-BITE: catches accepting v0.1 `bus` as a silent non-reference in v0.2.
    with pytest.raises(ValidationError) as exc:
        Contract.model_validate(data)
    message = str(exc.value)
    # MUTATION-BITE: requires the migration error to name the replacement field.
    assert "bus_ref" in message
    # MUTATION-BITE: requires the migration error to identify the rejected old key.
    assert "bus" in message


def _model_with_joint(*, axis=(0.0, 0.0, 1.0), effort=8.0) -> ModelRecord:
    return ModelRecord(
        source="robot.urdf",
        source_sha256="a" * 64,
        structure_hash="same-stale-hash",
        links=[Link(name="base"), Link(name="tool")],
        joints=[
            Joint(
                name="tool_joint",
                type="revolute",
                parent="base",
                child="tool",
                axis=axis,
                limits=JointLimits(effort=effort, velocity=2.0),
            )
        ],
        provenance=Provenance(
            witness="w2",
            source="robot.urdf",
            locked_at="2026-07-25",
        ),
    )


def test_joint_names_must_be_unique():
    # MUTATION-BITE: a duplicate joint name shadows a torque budget lookup silently
    # (checks.py builds {joint.name: joint}); this must fail to load instead.
    with pytest.raises(
        ValidationError, match=r"duplicate.*joint.*tool_joint|tool_joint.*duplicate"
    ):
        ModelRecord(
            source="robot.urdf",
            source_sha256="a" * 64,
            structure_hash="stale",
            links=[Link(name="base"), Link(name="tool")],
            joints=[
                Joint(name="tool_joint", type="revolute", parent="base", child="tool"),
                Joint(name="tool_joint", type="revolute", parent="base", child="tool"),
            ],
            provenance=Provenance(witness="w2", source="robot.urdf", locked_at="2026-07-25"),
        )


def test_link_names_must_be_unique():
    with pytest.raises(ValidationError, match=r"duplicate.*link.*base|base.*duplicate"):
        ModelRecord(
            source="robot.urdf",
            source_sha256="a" * 64,
            structure_hash="stale",
            links=[Link(name="base"), Link(name="base")],
            joints=[],
            provenance=Provenance(witness="w2", source="robot.urdf", locked_at="2026-07-25"),
        )


def test_actuator_torque_peak_below_continuous_rejected():
    # MUTATION-BITE: a peak below continuous is a datasheet-transcription error
    # that would silently under-budget the torque check.
    with pytest.raises(ValidationError, match="torque_peak"):
        Actuator(torque_continuous=5.0, torque_peak=4.0)


def test_actuator_torque_peak_equal_continuous_accepted():
    actuator = Actuator(torque_continuous=5.0, torque_peak=5.0)
    assert actuator.torque_peak == actuator.torque_continuous


def test_actuator_torque_peak_only_accepted():
    Actuator(torque_peak=5.0)


def test_actuator_torque_continuous_only_accepted():
    Actuator(torque_continuous=5.0)


def test_structure_hash_is_derived_from_complete_joint_record():
    original = _model_with_joint()
    changed_axis = _model_with_joint(axis=(1.0, 0.0, 0.0))
    changed_limit = _model_with_joint(effort=800.0)
    # MUTATION-BITE: catches trusting the stale authored hash instead of deriving it.
    assert original.structure_hash != "same-stale-hash"
    # MUTATION-BITE: catches the v0.1 hash omission of joint axis.
    assert changed_axis.structure_hash != original.structure_hash
    # MUTATION-BITE: catches the v0.1 hash omission of joint limits.
    assert changed_limit.structure_hash != original.structure_hash


def test_identity_strength_derivation():
    serial = DeviceIdentity(
        bus_ref="bus.serial0",
        host="h",
        serial_number="RFD900X-1234",
    )
    # MUTATION-BITE: catches serial-number identities being weakened from instance identity.
    assert serial.effective_strength() == "instance"

    usb = DeviceIdentity(bus_ref="bus.usb0", host="h", vid_pid="108c:0088")
    # MUTATION-BITE: catches part-level USB descriptors being treated as per-unit identity.
    assert usb.effective_strength() == "model"

    can = DeviceIdentity(bus_ref="bus.can0", host="h", can_id="42")
    # MUTATION-BITE: catches a CAN node id being treated as globally unique instance identity.
    assert can.effective_strength() == "model"

    i2c_strap = DeviceIdentity(bus_ref="bus.i2c0", host="h", address="0x68")
    # MUTATION-BITE: catches wiring-position identity being promoted beyond positional.
    assert i2c_strap.effective_strength() == "positional"

    clone = DeviceIdentity(
        bus_ref="bus.usb0",
        host="h",
        serial_number="00000001",
        strength="model",
    )
    # MUTATION-BITE: catches explicit cloned-serial demotion being ignored.
    assert clone.effective_strength() == "model"


def test_device_without_bus_identity_validates():
    esc = DeviceInstance(id="dev.esc.front-left", model="mdl.esc", owner="flight-controls")
    # MUTATION-BITE: catches restoring the required identity shape for non-bus devices.
    assert esc.identity is None


def _barometer_contract(*, bus_ref: str = "bus.i2c0") -> Contract:
    return Contract.model_validate(
        {
            "robot": {"name": "flight-controller"},
            "families": [{"id": "fam.barometer", "name": "Barometer"}],
            "device_models": [{"id": "mdl.barometer.ms5611", "family": "fam.barometer"}],
            "devices": [
                {
                    "id": "dev.barometer.ms5611",
                    "model": "mdl.barometer.ms5611",
                    "identity": {
                        "bus_ref": bus_ref,
                        "host": "flight-controller",
                        "address": "0x77",
                    },
                    "owner": "flight-controls",
                }
            ],
            "topology": {
                "hosts": [{"id": "flight-controller"}],
                "buses": [
                    {
                        "id": "bus.i2c0",
                        "kind": "i2c",
                        "host": "flight-controller",
                    }
                ],
            },
        }
    )


def test_i2c_barometer_bus_reference_resolves():
    # MUTATION-BITE: rejects removing I2C support or breaking declared-bus reference resolution.
    assert check_reference_integrity(_barometer_contract()) == []


def test_spi_bus_kind_validates():
    bus = Bus(id="bus.spi0", kind="spi", host="flight-controller")
    # MUTATION-BITE: catches removing SPI from the closed bus-kind set.
    assert bus.kind == "spi"


def test_nonexistent_bus_reference_still_rejected():
    findings = check_reference_integrity(_barometer_contract(bus_ref="bus.missing"))
    # MUTATION-BITE: catches optional identity bypassing integrity for identities that are present.
    assert [finding.quantity for finding in findings] == [
        "devices/dev.barometer.ms5611/identity/bus_ref"
    ]


def test_capacity_derating_factor_validated():
    from lockwire.schema import CapacityDerating

    # MUTATION-BITE: catches physically impossible capacity gains from derating.
    with pytest.raises(ValidationError):
        CapacityDerating(below_c=0, capacity_factor=1.5)
    # MUTATION-BITE: catches zero usable capacity being represented as a multiplier.
    with pytest.raises(ValidationError):
        CapacityDerating(below_c=0, capacity_factor=0)


@pytest.mark.parametrize(
    ("section", "row"),
    [
        (
            "devices",
            {"id": "dup", "model": "m", "identity": {"bus_ref": "b", "host": "h"}, "owner": "o"},
        ),
        ("device_models", {"id": "dup", "family": "f"}),
        ("families", {"id": "dup", "name": "family"}),
        (
            "rails",
            {
                "id": "dup",
                "voltage": {"value": 5, "unit": "V"},
                "capacity": {"value": 5, "unit": "W"},
            },
        ),
        ("buses", {"id": "dup", "kind": "can", "host": "h"}),
        (
            "wires",
            {
                "id": "dup",
                "awg": 16,
                "installation": "free_air",
                "rail": "rail.5v",
                "owner": "o",
            },
        ),
        (
            "budgets",
            {
                "id": "dup",
                "kind": "mass_total",
                "limit": {"value": 1, "unit": "kg"},
                "owner": "o",
            },
        ),
        (
            "assumptions",
            {
                "id": "dup",
                "statement": "declared",
                "owner": "o",
                "last_reviewed": "2026-07-25",
            },
        ),
    ],
)
def test_duplicate_ids_rejected_per_list(section, row):
    data = {"schema_version": "0.2", "robot": {"name": "duplicates"}}
    if section in {"rails", "buses", "wires"}:
        data["topology"] = {section: [row, row]}
    else:
        data[section] = [row, row]
    # MUTATION-BITE: each row catches first-match lookup silently shadowing a duplicate id.
    with pytest.raises(ValidationError, match=rf"duplicate id\(s\) in {section}: dup"):
        Contract.model_validate(data)


def test_wire_requires_installation_enum():
    """A gauged wire without installation must fail validation — no silent default."""
    base = {
        "id": "wire.batt",
        "awg": 16,
        "rail": "rail.5v",
        "owner": "power",
    }
    # MUTATION-BITE: catches making installation optional or defaulting to free_air.
    with pytest.raises(ValidationError, match="installation"):
        Wire.model_validate(base)
    with pytest.raises(ValidationError):
        Wire.model_validate({**base, "installation": "chassis"})
    wire = Wire.model_validate({**base, "installation": "enclosed"})
    assert wire.installation == "enclosed"
    assert wire.awg == 16


def test_wire_rejects_non_positive_awg():
    with pytest.raises(ValidationError, match="awg"):
        Wire(
            id="wire.bad",
            awg=0,
            installation="free_air",
            rail="rail.5v",
            owner="power",
        )


# ---------------------------------------------------------------------------
# load guards (declarative data must be cheap to reject)
# ---------------------------------------------------------------------------

# Classic YAML billion-laughs: small on disk, exponential via aliases. Without
# the post-load tree walk this hangs inside ValidationError / CommentedSeq.__repr__.
_BILLION_LAUGHS_YAML = """\
schema_version: "0.2"
robot:
  name: bomb
payload: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f,*f]
h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g,*g]
i: &i [*h,*h,*h,*h,*h,*h,*h,*h,*h,*h]
j: &j [*i,*i,*i,*i,*i,*i,*i,*i,*i,*i]
"""


def _defeat_geometry_yaml(
    *,
    levels: int = 6,
    fanout: int = 4,
    base_n: int = 10,
    name_prefix: str = "a",
) -> str:
    """Reviewer defeat: deep alias fan-out under the byte cap.

    6 alias levels × fan-out 4 → shared-ref / anchor structure the composed-tree
    walk must catch. Base list is intentionally small: with grammar-complete
    anchor detection a single anchor is enough, and a giant base only made
    ruamel's flow parse dominate the sub-second refusal budget.
    ``name_prefix`` selects ASCII (``a`` / ``l1``) or non-ASCII (``café`` /
    ``l1é``) anchor names; the latter defeats any ``[A-Za-z_]`` byte regex.
    """
    leaf = ",".join(["0"] * base_n)
    base_name = name_prefix
    lines = [
        'schema_version: "0.2"',
        "robot:",
        "  name: bomb",
        f"payload: &{base_name} [{leaf}]",
    ]
    prev = base_name
    for index in range(1, levels + 1):
        name = f"l{index}" if name_prefix == "a" else f"l{index}é"
        aliases = ",".join([f"*{prev}"] * fanout)
        lines.append(f"{name}: &{name} [{aliases}]")
        prev = name
    return "\n".join(lines) + "\n"


def _no_anchor_flow_list_yaml(*, approx_bytes: int) -> str:
    """Build a no-anchor flow-list document sized near ``approx_bytes``."""
    header = 'schema_version: "0.2"\nrobot:\n  name: flow\nx: ['
    trailer = "]\n"
    # Each "1," is 2 bytes; leave room for header/trailer and a final "1".
    filler_budget = max(approx_bytes - len(header) - len(trailer) - 1, 0)
    n_commas = filler_budget // 2
    return header + ("1," * n_commas) + "1" + trailer


# OS-level kill budget for unguarded bite subprocesses. Must cover cold
# import of lockwire/pydantic/ruamel plus payload open, yet stay well under
# the suite's overall timeout. SIGALRM cannot interrupt ruamel's C scanner;
# subprocess.TimeoutExpired (SIGKILL) can. Board range: 2–3s.
_UNGUARDED_LOAD_TIMEOUT_S = 3.0


def _assert_unguarded_load_times_out(
    path: Path,
    *,
    disable_attr: str,
    disable_value: object,
    stringify_exc: bool = False,
    timeout_s: float = _UNGUARDED_LOAD_TIMEOUT_S,
) -> None:
    """Prove removing ``disable_attr`` makes ``load_contract`` hang past ``timeout_s``.

    The unguarded load runs in a child process so the OS can SIGKILL C-level
    ruamel work that in-process ``signal.SIGALRM`` cannot interrupt.
    ``subprocess.TimeoutExpired`` is the success condition; a timely return
    means the bite failed (guard was not load-bearing).
    """
    if stringify_exc:
        # Landed hang: ValidationError stringification recurses
        # CommentedSeq.__repr__ over the aliased structure.
        load_lines = textwrap.dedent(
            """\
            try:
                load_contract(path)
            except Exception as exc:
                str(exc)
                raise
            """
        )
    else:
        load_lines = "load_contract(path)\n"
    script = (
        textwrap.dedent(
            f"""\
            from pathlib import Path
            from lockwire import schema as schema_mod
            from lockwire.schema import load_contract

            schema_mod.{disable_attr} = {disable_value!r}
            path = Path({str(path)!r})
            """
        )
        + load_lines
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            timeout=timeout_s,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return  # hang reproduced — bite holds
    pytest.fail(
        f"unguarded load returned in <{timeout_s}s "
        f"(exit {completed.returncode}) after disabling {disable_attr}; "
        f"expected OS-level hang. stderr:\n{completed.stderr}"
    )


def test_load_rejects_anchor_bomb_subsecond(tmp_path):
    path = tmp_path / "bomb.yaml"
    path.write_text(_BILLION_LAUGHS_YAML)
    started = time.perf_counter()
    # MUTATION-BITE: catches missing anchor/alias composed-tree guard.
    with pytest.raises(ValueError, match=r"anchors/aliases are not permitted"):
        load_contract(path)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"anchor-bomb refusal took {elapsed:.3f}s (want <2s)"


def test_load_rejects_defeat_geometry_alias_bomb_subsecond(tmp_path):
    """ASCII deep-alias defeat: fan-out 4 × 6 levels, small base list."""
    text = _defeat_geometry_yaml()
    path = tmp_path / "defeat.yaml"
    path.write_text(text)
    assert path.stat().st_size < MAX_CONTRACT_BYTES
    started = time.perf_counter()
    # MUTATION-BITE: catches missing tree-walk refusal of shared-ref bombs.
    with pytest.raises(ValueError, match=r"anchors/aliases are not permitted"):
        load_contract(path)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"defeat-geometry refusal took {elapsed:.3f}s (want <1s)"


def test_load_rejects_non_ascii_defeat_geometry_subsecond(tmp_path):
    """Round-2 defeat: non-ASCII anchor names (``&café``) that beat ``[A-Za-z_]``."""
    text = _defeat_geometry_yaml(name_prefix="café")
    path = tmp_path / "defeat-unicode.yaml"
    path.write_text(text, encoding="utf-8")
    assert path.stat().st_size < MAX_CONTRACT_BYTES
    started = time.perf_counter()
    # MUTATION-BITE: catches a byte-regex guard that never sees non-ASCII names.
    with pytest.raises(ValueError, match=r"anchors/aliases are not permitted"):
        load_contract(path)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"non-ASCII defeat refusal took {elapsed:.3f}s (want <1s)"


def test_load_rejects_oversized_contract_subsecond(tmp_path):
    path = tmp_path / "giant.yaml"
    # Just over the byte ceiling — enough to trip the stat guard, not enough
    # to stress the filesystem. A quoted scalar of this size hangs ruamel
    # without the guard (see mutation bite below).
    oversized = MAX_CONTRACT_BYTES + 1
    path.write_bytes(b'"' + (b"x" * (oversized - 2)) + b'"')
    started = time.perf_counter()
    # MUTATION-BITE: catches missing byte-size pre-parse guard.
    with pytest.raises(ValueError, match=r"size limit"):
        load_contract(path)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"oversized refusal took {elapsed:.3f}s (want <2s)"


def test_load_rejects_oversized_flow_list_subsecond(tmp_path):
    """No-anchor flow list over the byte cap is refused by Path.stat before parse.

    Board: ~200 KiB flow list → ~4s ruamel parse; ~1.8 MiB hangs. The 256 KiB
    cap must refuse oversized flow YAML in bounded time (stat is instant).
    """
    text = _no_anchor_flow_list_yaml(approx_bytes=300 * 1024)
    path = tmp_path / "flow-over.yaml"
    path.write_text(text)
    assert path.stat().st_size > MAX_CONTRACT_BYTES
    started = time.perf_counter()
    # MUTATION-BITE: catches a cap high enough that flow-list parse still hangs.
    with pytest.raises(ValueError, match=r"size limit"):
        load_contract(path)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"oversized flow-list refusal took {elapsed:.3f}s (want <1s)"


def test_load_rejects_deep_nesting_no_traceback(tmp_path):
    """6 KB of `[` blows ruamel's recursive descent before the anchor guard runs."""
    path = tmp_path / "deep.yaml"
    path.write_text("[" * 6000 + "]" * 6000)
    assert path.stat().st_size < MAX_CONTRACT_BYTES
    # MUTATION-BITE: catches a bare RecursionError reaching the caller instead
    # of the tool's refusal shape.
    with pytest.raises(ValueError, match=r"nesting too deep"):
        load_contract(path)


def test_under_cap_flow_list_reaches_validation_bounded(tmp_path):
    """~200 KiB no-anchor flow list under the cap reaches schema validation.

    Bound is WORK, not wall-clock: fixture bytes sit under ``MAX_CONTRACT_BYTES``
    and ``load_contract`` reaches pydantic (extra key ``x`` → ValidationError)
    rather than a size-guard refusal. A multi-minute ceiling is hang-sanity only.
    """
    text = _no_anchor_flow_list_yaml(approx_bytes=200 * 1024)
    path = tmp_path / "flow-under.yaml"
    path.write_text(text)
    size = path.stat().st_size
    # Work bound: under the byte cap, yet large enough to stress flow-list parse.
    assert size < MAX_CONTRACT_BYTES, f"under-cap fixture is {size} bytes"
    assert size > 150 * 1024, f"under-cap fixture too small ({size} bytes)"
    started = time.perf_counter()
    try:
        load_contract(path)
    except ValidationError:
        pass  # primary: size guard admitted the file; schema validation ran
    except ValueError as exc:
        # MUTATION-BITE: a neutered/too-tight size guard rejects under-cap input
        # with the size-limit ValueError instead of reaching validation.
        pytest.fail(f"guard rejected under-cap flow list before validation: {exc}")
    else:
        pytest.fail("expected ValidationError from extra top-level key 'x'")
    elapsed = time.perf_counter() - started
    # Secondary sanity only — not a load-sensitive gate (minutes, not seconds).
    assert elapsed < 300.0, f"under-cap flow list hung ({elapsed:.1f}s)"


def _time_admitted_under_cap_parse(path: Path) -> float:
    """Time one ``load_contract`` that must reach pydantic ValidationError.

    Same ValidationError-not-ValueError discipline as the under-cap test:
    admission past the size guard is the property under test; schema rejection
    of the extra top-level key ``x`` proves validation ran.
    """
    started = time.perf_counter()
    try:
        load_contract(path)
    except ValidationError:
        pass
    except ValueError as exc:
        pytest.fail(f"guard rejected under-cap flow list before validation: {exc}")
    else:
        pytest.fail("expected ValidationError from extra top-level key 'x'")
    elapsed = time.perf_counter() - started
    # Hang backstop only (same 300 s ceiling) — not a speed gate.
    assert elapsed < 300.0, f"under-cap flow list hung ({elapsed:.1f}s)"
    return elapsed


def test_admitted_parse_growth_bounded_16x_spread(tmp_path):
    """Admitted parse growth is bounded: t(16S) <= 3 * 16 * t(S) (= 48×).

    Absolute timing was removed of legitimate ADMITTED parses (flaky under load).
    This pins the complementary half of the bounded-parse property — bounded to admit —
    with a COMPARATIVE instrument: measure growth ratio, not wall-clock speed.
    Best-of-5 damps scheduler noise on the now-sub-second S sample.
    """
    # Geometry: S and 16S, both under MAX_CONTRACT_BYTES (256 KiB).
    # Target S≈14 KiB, 16S≈224 KiB — same shape, scaled counts.
    size_s = 14 * 1024
    size_16s = 14 * 16 * 1024
    path_s = tmp_path / "flow-s.yaml"
    path_16s = tmp_path / "flow-16s.yaml"
    path_s.write_text(_no_anchor_flow_list_yaml(approx_bytes=size_s))
    path_16s.write_text(_no_anchor_flow_list_yaml(approx_bytes=size_16s))
    bytes_s = path_s.stat().st_size
    bytes_16s = path_16s.stat().st_size
    assert bytes_s < MAX_CONTRACT_BYTES, f"S fixture is {bytes_s} bytes"
    assert bytes_16s < MAX_CONTRACT_BYTES, f"16S fixture is {bytes_16s} bytes"
    # Confirm the intended ~16× size relationship before comparing times.
    size_ratio = bytes_16s / bytes_s
    assert 14.0 <= size_ratio <= 18.0, f"size ratio {size_ratio:.2f} not ~16×"

    # Best-of-5 (minimum elapsed) per size — t(S) is ~0.25–0.35 s, so
    # per-sample scheduler noise is proportionally large; five samples cost little.
    t_s = min(_time_admitted_under_cap_parse(path_s) for _ in range(5))
    t_16s = min(_time_admitted_under_cap_parse(path_16s) for _ in range(5))
    # Why 16× spread (not 4×): with sizes S and kS, pure linear reads ~k and
    # pure quadratic asymptotes to k². Bound is c·k with generous c=3.
    # At k=4: linear≈4, quadratic≈16, bound=12 → detection window only 33%,
    # while measured wall-clock noise here is 30–48%. Diluted O(n²) injections
    # landed ratios 6.4–11.4 and passed. Structurally (4a+16b)/(a+b) ≤ 16
    # always, so a 12× line cannot separate quadratic share from noise.
    # At k=16: linear≈16, quadratic≈256, bound=48. Even a heavily diluted
    # quadratic (quad share ≥25% at S) reads ≥ ~70 — gap to 48 sits far
    # outside any plausible noise band.
    # MUTATION-BITE: quadratic walk in the admitted loader → ratio ≫ 48.
    growth_slack = 3.0
    spread = 16.0
    bound = growth_slack * spread
    ratio = t_16s / t_s
    assert t_16s <= bound * t_s, (
        f"parse growth unbounded: t(S)={t_s:.4f}s t(16S)={t_16s:.4f}s "
        f"ratio={ratio:.2f} (want <= {bound:.0f}×)"
    )


def test_anchor_bomb_guard_is_load_bearing(tmp_path):
    """Disable the tree walk — the hang must return."""
    path = tmp_path / "bomb.yaml"
    path.write_text(_BILLION_LAUGHS_YAML)
    # MUTATION-BITE: without the alias forbid, ValidationError stringification
    # hangs on CommentedSeq.__repr__ over the aliased structure. Subprocess
    # SIGKILL is required — in-process SIGALRM cannot cut deep recursion reliably.
    _assert_unguarded_load_times_out(
        path,
        disable_attr="REJECT_YAML_ANCHORS_ALIASES",
        disable_value=False,
        stringify_exc=True,
    )


def test_defeat_geometry_guard_is_load_bearing(tmp_path, monkeypatch):
    """ASCII defeat: with guard → named refusal; without → reaches validation."""
    path = tmp_path / "defeat.yaml"
    path.write_text(_defeat_geometry_yaml())
    # WITH guard: named refusal (small base — no hang to wait for).
    with pytest.raises(ValueError, match=r"anchors/aliases are not permitted"):
        load_contract(path)
    # WITHOUT guard: anchors accepted; schema validation sees the extra keys.
    # MUTATION-BITE: asymmetry proves the walk is load-bearing (not a timeout).
    monkeypatch.setattr("lockwire.schema.REJECT_YAML_ANCHORS_ALIASES", False)
    with pytest.raises(ValidationError):
        load_contract(path)


def test_non_ascii_defeat_geometry_guard_is_load_bearing(tmp_path, monkeypatch):
    """Non-ASCII defeat: with guard → named refusal; without → reaches validation."""
    path = tmp_path / "defeat-unicode.yaml"
    path.write_text(_defeat_geometry_yaml(name_prefix="café"), encoding="utf-8")
    with pytest.raises(ValueError, match=r"anchors/aliases are not permitted"):
        load_contract(path)
    # MUTATION-BITE: asymmetry proves Unicode anchors still need the tree walk.
    monkeypatch.setattr("lockwire.schema.REJECT_YAML_ANCHORS_ALIASES", False)
    with pytest.raises(ValidationError):
        load_contract(path)


def test_size_guard_is_load_bearing(tmp_path):
    """Remove only the byte cap — the scanner hang must return under timeout."""
    path = tmp_path / "giant.yaml"
    # Multi-MB quoted scalar: large enough that unguarded ruamel scanning
    # hangs in C (original red-team payload was ~50MB / 20s). Do not shrink —
    # a decorative small payload would let SIGALRM "work" without proving the
    # real hang.
    n = 32 * 1024 * 1024
    path.write_bytes(b'"' + (b"x" * n) + b'"')
    # MUTATION-BITE: without the size bound, load_contract hangs in the scanner.
    _assert_unguarded_load_times_out(
        path,
        disable_attr="MAX_CONTRACT_BYTES",
        disable_value=10**12,
    )


def test_quoted_ampersand_and_asterisk_still_load(tmp_path):
    """False-positive control: ``&`` / ``*`` inside quoted scalars are data."""
    path = tmp_path / "quoted.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            schema_version: "0.2"
            robot:
              name: "A*B & co"
            assumptions:
              - id: note.amp
                statement: "see https://example.com/a&b and &anchor_name or *alias_name"
                owner: robotics
                last_reviewed: "2026-07-26"
            """
        )
    )
    # Tree walk only sees composed anchors/aliases — quoted content is inert.
    # MUTATION-BITE: catches a naive [&*] scan rejecting legitimate quoted content.
    contract = load_contract(path)
    assert contract.robot.name == "A*B & co"
    assert "&anchor_name" in contract.assumptions[0].statement
    assert "*alias_name" in contract.assumptions[0].statement


def test_block_scalar_ampersand_still_loads(tmp_path):
    """False-positive control: ``&`` inside a ``|`` block scalar is inert to ruamel."""
    path = tmp_path / "block.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            schema_version: "0.2"
            robot:
              name: blockbot
            assumptions:
              - id: note.block
                statement: |
                  payload looks like &anchor_name and *alias_name
                  but lives inside a block scalar
                owner: robotics
                last_reviewed: "2026-07-26"
            """
        )
    )
    # MUTATION-BITE: catches a byte scanner treating block-scalar & as structural.
    contract = load_contract(path)
    assert "&anchor_name" in contract.assumptions[0].statement
    assert "*alias_name" in contract.assumptions[0].statement


def test_existing_fixtures_still_load():
    """Negative control: the guard must not reject legitimate lockfiles."""
    fixtures = Path(__file__).parent / "fixtures"
    for rel, expected_version in (
        ("contract_clean.yaml", "0.3"),
        ("x500/contract.yaml", "0.2"),
        ("x500/contract_corrected.yaml", "0.2"),
    ):
        contract = load_contract(fixtures / rel)
        # MUTATION-BITE: catches a guard threshold low enough to reject real fixtures.
        assert contract.robot.name
        assert contract.schema_version == expected_version


def test_anchor_reject_flag_defaults_on():
    # MUTATION-BITE: catches disabling the composed-tree walk by default.
    assert REJECT_YAML_ANCHORS_ALIASES is True


# --- `why` and `max_rate` on Band ------------------------------------
# The `why` (<=512 chars) and `max_rate` (units/s) fields on a Band are part of
# the schema and must stay there.


def test_band_why_field_optional_defaults_none():
    band = Band(lo=0.0, hi=1.0, unit="V")
    assert band.why is None
    assert str(band) == "[0, 1] V"

    said = Band(lo=0.0, hi=1.0, unit="V", why="the regulator lets go above 1V")
    assert said.why == "the regulator lets go above 1V"
    assert str(said) == "[0, 1] V (why: the regulator lets go above 1V)"

    with pytest.raises(ValidationError):
        Band(lo=0.0, hi=1.0, unit="V", why="x" * 513)


def test_band_max_rate_field_optional_defaults_none():
    band = Band(lo=0.0, hi=1.0, unit="V")
    assert band.max_rate is None
    assert Band(lo=0.0, hi=1.0, unit="V", max_rate=0.5).max_rate == 0.5
    # units/s is a rate of change, so it is strictly positive.
    with pytest.raises(ValidationError):
        Band(lo=0.0, hi=1.0, unit="V", max_rate=0.0)


def test_band_enforced_by_is_optional_and_closed():
    assert Band(lo=0.0, hi=1.0, unit="V").enforced_by is None
    for value in ("fuse", "firmware", "driver", "this file"):
        assert Band(lo=0.0, hi=1.0, unit="V", enforced_by=value).enforced_by == value
    with pytest.raises(ValidationError):
        Band(lo=0.0, hi=1.0, unit="V", enforced_by="operator")


def test_existing_limits_accept_optional_closed_enforcement_metadata():
    provenance = Provenance(witness="w", source="spec", locked_at=dt.date(2026, 1, 1))
    limits = JointLimits(
        lower=-1,
        upper=1,
        effort=2,
        velocity=3,
        lower_enforced_by="fuse",
        upper_enforced_by="firmware",
        effort_enforced_by="driver",
        velocity_enforced_by="this file",
    )
    assert limits.effort_enforced_by == "driver"
    voltage = InputVoltageRange(
        min=Quantity(value=4, unit="V"),
        max=Quantity(value=6, unit="V"),
        min_enforced_by="fuse",
        max_enforced_by="driver",
    )
    assert voltage.max_enforced_by == "driver"
    thermal = ThermalLimit(
        temperature=Quantity(value=90, unit="C"),
        kind="throttle",
        provenance=provenance,
        temperature_enforced_by="firmware",
    )
    assert thermal.temperature_enforced_by == "firmware"
    # The RF range is an FSPL input, not an enforcement threshold.  The fade
    # margin is the checked limit and is the only LinkBudget field that admits
    # this metadata.
    link = LinkBudget(
        required_range=Quantity(value=1, unit="m"),
        fade_margin_required=Quantity(value=1, unit="dB"),
        installation_loss=Quantity(value=1, unit="dB"),
        fade_margin_required_enforced_by="driver",
    )
    assert link.fade_margin_required_enforced_by == "driver"
    rail = Rail(id="rail", capacity=Quantity(value=1, unit="W"), capacity_enforced_by="fuse")
    assert rail.capacity_enforced_by == "fuse"
    budget = Budget(
        id="budget",
        kind="mass_total",
        limit=Quantity(value=1, unit="kg"),
        owner="owner",
        limit_enforced_by="firmware",
    )
    assert budget.limit_enforced_by == "firmware"
    with pytest.raises(ValidationError, match="current_limit_enforced_by requires current_limit"):
        Rail(id="rail", capacity=Quantity(value=1, unit="W"), current_limit_enforced_by="fuse")
    with pytest.raises(ValidationError, match="lower_enforced_by requires lower"):
        JointLimits(lower_enforced_by="fuse")
    with pytest.raises(ValidationError):
        DeviceFamily(
            id="fam",
            name="Family",
            min_capture_window_s=1,
            min_capture_window_s_enforced_by="firmware",
        )
    with pytest.raises(ValidationError):
        LinkBudget(
            required_range=Quantity(value=1, unit="m"),
            fade_margin_required=Quantity(value=1, unit="dB"),
            installation_loss=Quantity(value=1, unit="dB"),
            required_range_enforced_by="this file",
        )


def test_hard_limits_band_accepts_why():
    """`hard_limits` values are typed `Band`, so the field arrives there too."""
    model = DeviceModel(
        id="mdl.x",
        family="sensor",
        provenance=Provenance(witness="w0", source="datasheet", locked_at=dt.date(2026, 1, 1)),
        hard_limits={
            "rail": Band(lo=4.5, hi=6.0, unit="V", why="above 6V the LDO cooks", max_rate=2.0)
        },
    )
    assert model.hard_limits["rail"].why == "above 6V the LDO cooks"
    assert model.hard_limits["rail"].max_rate == 2.0
    assert "why: above 6V the LDO cooks" in str(model.hard_limits["rail"])


@pytest.mark.parametrize(
    "fixture",
    sorted(p.name for p in (Path(__file__).parent / "fixtures").glob("contract_*.yaml"))
    + ["x500/contract.yaml", "x500/contract_corrected.yaml"],
)
def test_all_fixtures_still_load_after_why_max_rate(fixture):
    """The misapplication check: a fixture that stops loading, or a
    schema version bump."""
    from lockwire.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == "0.4"
    contract = load_contract(Path(__file__).parent / "fixtures" / fixture)
    for model in contract.device_models:
        for band in model.hard_limits.values():
            assert band.why is None
            assert band.max_rate is None
            assert band.enforced_by is None


def _contract_at_version(tmp_path: Path, version: str) -> Path:
    """A copy of the clean fixture whose declared ``schema_version`` is ``version``."""
    source = (Path(__file__).parent / "fixtures" / "contract_clean.yaml").read_text()
    lines = [
        f'schema_version: "{version}"' if line.startswith("schema_version") else line
        for line in source.splitlines()
    ]
    path = tmp_path / "lockwire.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_driver_procedure_requires_a_declared_signal_and_discrete_code():
    from lockwire.schema import Driver, DriverProcedure, SignalSource

    Driver(
        msg_type="std_msgs/msg/UInt8",
        signals={"watchdog_code": SignalSource(field="data", unit="1")},
        procedures=[
            DriverProcedure(id="watchdog-kick-v1", kind="watchdog", signal="watchdog_code", code=7)
        ],
    )
    with pytest.raises(ValidationError, match="undeclared signals"):
        Driver(
            msg_type="std_msgs/msg/UInt8",
            signals={"watchdog_code": SignalSource(field="data", unit="1")},
            procedures=[
                DriverProcedure(id="watchdog-kick-v1", kind="watchdog", signal="other", code=7)
            ],
        )
    with pytest.raises(ValidationError, match="nonnegative integer"):
        DriverProcedure(id="bad", kind="watchdog", signal="watchdog_code", code=-1)
    with pytest.raises(ValidationError):
        DriverProcedure(id="bad", kind="watchdog", signal="watchdog_code", code=True)


def test_unsupported_schema_version_raises_typed(tmp_path):
    """A contract from a newer (or older) lockwire is a named refusal, not a bare
    ValueError — every library caller (``lockwire.api.load``, ``edit``, the MCP
    server) can tell this one failure from a parse error. A *supported* version
    still loads."""
    from lockwire.schema import SUPPORTED_SCHEMA_VERSIONS, UnsupportedSchemaVersion

    for version in ("0.1", "0.5"):
        with pytest.raises(UnsupportedSchemaVersion) as caught:
            load_contract(_contract_at_version(tmp_path, version))
        assert caught.value.found == version
        assert caught.value.supported == ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        assert version in str(caught.value)
        # Still a ValueError, so no existing `except ValueError` caller changes.
        assert isinstance(caught.value, ValueError)

    # 0.2 is supported (SUPPORTED_SCHEMA_VERSIONS) — it must clear the version
    # guard. It still fails validation here because the clean fixture declares
    # 0.3-only fields, which is a different refusal and the point of the test.
    try:
        load_contract(_contract_at_version(tmp_path, "0.2"))
    except UnsupportedSchemaVersion:  # pragma: no cover
        pytest.fail("0.2 is in SUPPORTED_SCHEMA_VERSIONS and must not be refused")
    except ValueError:
        pass


def test_tolerance_is_scored_by_checks() -> None:
    """`DependencyEdge.tolerance` was validated for shape
    but honoured by no check — that has changed: `checks.py::check_edge_tolerance`
    now reads `.tolerance` and is joined into `run_record_checks`, so an observed
    value IS compared against this band. This guard used to assert the opposite (no
    check reads `.tolerance`) and went red exactly as designed the night that
    landed — its own docstring predicted that as "the correct trigger for this
    test, not a reason to weaken it." The record moves with the fact: this guard is
    turned around and now fails if scoring disappears from `checks.py` OR if the
    SCORED note in schema.py reverts to claiming tolerance is unscored.

    CEILING: this guards the SCORING path only — `checks.py`,
    which is the whole of what `run_record_checks` reaches (every `check_*`
    function it calls is defined in that one file). It deliberately does NOT guard
    rendering, describing or printing a tolerance value elsewhere (e.g.
    `describe.py`'s edge-matrix table, which prints `.tolerance` for a human to
    read — printing a value is not scoring it).
    """
    schema_path = Path(__file__).parent.parent / "src" / "lockwire" / "schema.py"
    text = schema_path.read_text()
    assert "SCORED: shape is validated" in text, (
        "marker comment 'SCORED: shape is validated' is missing or reworded in schema.py; the note must say tolerance is scored — restore or update it next to the tolerance field in DependencyEdge"
    )
    assert "DECLARED-FOR-FUTURE (E0-70)" not in text, (
        "schema.py's tolerance note still claims DECLARED-FOR-FUTURE (unscored), but checks.py scores it as of e3-tol — update the note, don't just add a new one"
    )

    checks_path = Path(__file__).parent.parent / "src" / "lockwire" / "checks.py"
    hits = subprocess.run(
        ["grep", "-n", r"\.tolerance", str(checks_path)],
        capture_output=True,
        text=True,
    )
    # grep exit 0 == at least one match. schema.py's own field/attribute
    # declarations (`tolerance: Tolerance`, the docstring lines) are not
    # `.tolerance` reads in checks.py, and neither is a non-scoring module
    # (describe.py, report*.py, gui.py, ...) printing the value — only
    # checks.py scores.
    assert hits.returncode == 0, (
        "checks.py no longer reads .tolerance — scoring disappeared; if that is intentional, revert the SCORED note in schema.py back to DECLARED-FOR-FUTURE in the same commit"
    )


def test_compared_from_is_optional_and_round_trips_without_changing_legacy_provenance():
    legacy = Provenance(witness="w0", source="datasheet", locked_at="2026-09-27")
    copied = Provenance(
        witness="w0",
        source="datasheet",
        locked_at="2026-09-27",
        compared_from="x500.flight-controller.pixhawk-6c",
    )
    assert legacy.compared_from is None
    assert "compared_from" not in legacy.model_dump(exclude_none=True)
    assert copied.model_dump()["compared_from"] == "x500.flight-controller.pixhawk-6c"
