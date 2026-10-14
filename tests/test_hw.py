"""W0 hardware-change event log and routing."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from lockwire.hw import (
    build_event,
    change_kind,
    events_path_for,
    load_log,
    parse_quantities,
    record_hardware_change,
    route_event,
)
from lockwire.schema import Quantity, load_contract

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN = FIXTURES / "contract_clean.yaml"
FIXED_AT = datetime(2026, 7, 27, tzinfo=UTC)


def _paths(routing) -> set[str]:
    return {entry.path for entry in routing.touched}


def test_parse_quantities_from_spec_example():
    quantities = parse_quantities("added FLIR, 42 g, nose mount")
    assert quantities == [Quantity(value=42, unit="g")]
    assert change_kind("added FLIR, 42 g, nose mount") == "add"


def test_events_path_is_sidecar_beside_contract():
    assert events_path_for(Path("lockwire.yaml")) == Path("lockwire.hw.yaml")
    assert events_path_for(Path("tests/fixtures/contract_clean.yaml")) == Path(
        "tests/fixtures/contract_clean.hw.yaml"
    )


def test_record_persists_event_and_leaves_contract_bytes_untouched(tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_bytes(CLEAN.read_bytes())
    before = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    contract = load_contract(contract_path)

    event, log_path, routing = record_hardware_change(
        contract,
        contract_path,
        "added FLIR, 42 g, nose mount",
        recorded_at=FIXED_AT,
    )

    assert log_path == tmp_path / "lockwire.hw.yaml"
    assert log_path.is_file()
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == before

    loaded = load_log(log_path)
    assert len(loaded.events) == 1
    assert loaded.events[0].declaration == event.declaration
    assert loaded.events[0].quantities == [Quantity(value=42, unit="g")]
    assert loaded.events[0].recorded_at == FIXED_AT

    assert "budgets/bud.mass.devices" in _paths(routing)
    assert "model" in _paths(routing)
    mass = next(e for e in routing.touched if e.path == "budgets/bud.mass.devices")
    assert mass.owner == "shivansh"
    assert routing.undetermined  # bus/rail unbound on a device addition


def test_mass_routing_names_mass_budget_owner(clean_contract):
    event = build_event("added FLIR, 42 g, nose mount", recorded_at=FIXED_AT)
    routing = route_event(clean_contract, event)
    assert "budgets/bud.mass.devices" in _paths(routing)
    assert "model" in _paths(routing)
    # Unbound device add must not silently claim power budgets are unaffected.
    assert any("rail not bound" in gap for gap in routing.undetermined)
    assert any("bus not bound" in gap for gap in routing.undetermined)
    assert "budgets/bud.power.rail5v" not in _paths(routing)


def test_device_binding_routes_bus_rail_and_power_budgets(clean_contract):
    event = build_event(
        "replaced IMU, 12 g",
        recorded_at=FIXED_AT,
        device="dev.imu",
    )
    routing = route_event(clean_contract, event)
    paths = _paths(routing)
    assert "devices/dev.imu" in paths
    assert "topology/buses/bus.usb0" in paths
    assert "topology/rails/rail.5v" in paths
    assert "topology/rails/rail.22v" in paths  # upstream of rail.5v
    assert "budgets/bud.power.rail5v" in paths
    assert "budgets/bud.endurance" in paths
    assert "budgets/bud.mass.devices" in paths
    assert "devices/dev.imu/physical/mass" in paths
    power = next(e for e in routing.touched if e.path == "budgets/bud.power.rail5v")
    assert power.owner == "shivansh"
    assert routing.undetermined == []


def test_explicit_bus_and_rail_bind_without_existing_device(clean_contract):
    event = build_event(
        "added FLIR, 42 g, nose mount",
        recorded_at=FIXED_AT,
        bus="bus.usb0",
        rail="rail.5v",
    )
    routing = route_event(clean_contract, event)
    paths = _paths(routing)
    assert "topology/buses/bus.usb0" in paths
    assert "topology/rails/rail.5v" in paths
    assert "budgets/bud.power.rail5v" in paths
    assert "budgets/bud.endurance" in paths
    assert routing.undetermined == []


def test_undetermined_when_declaration_cannot_be_routed(clean_contract):
    event = build_event("rearranged cable harness", recorded_at=FIXED_AT)
    routing = route_event(clean_contract, event)
    assert routing.touched == []
    assert routing.undetermined
    assert any("could not determine" in gap for gap in routing.undetermined)


def test_unknown_device_binding_is_named_not_silent(clean_contract):
    event = build_event(
        "added FLIR, 42 g",
        recorded_at=FIXED_AT,
        device="dev.cam.flir",
    )
    routing = route_event(clean_contract, event)
    assert any("dev.cam.flir" in gap for gap in routing.undetermined)
    # Mass still routes from the quantity even when the device id is unknown.
    assert "budgets/bud.mass.devices" in _paths(routing)


def test_append_is_append_only(tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_bytes(CLEAN.read_bytes())
    contract = load_contract(contract_path)

    record_hardware_change(contract, contract_path, "added FLIR, 42 g", recorded_at=FIXED_AT)
    record_hardware_change(
        contract,
        contract_path,
        "removed spare antenna, 30 g",
        recorded_at=FIXED_AT,
    )
    log = load_log(events_path_for(contract_path))
    assert [event.declaration for event in log.events] == [
        "added FLIR, 42 g",
        "removed spare antenna, 30 g",
    ]


def test_mutation_bite_mass_budget_routing_is_required(clean_contract):
    """Mutation-bite: drop mass_total routing and this assertion goes red."""
    event = build_event("added FLIR, 42 g, nose mount", recorded_at=FIXED_AT)
    routing = route_event(clean_contract, event)
    assert any(
        entry.path == "budgets/bud.mass.devices" and entry.owner == "shivansh"
        for entry in routing.touched
    )
