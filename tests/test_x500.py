from pathlib import Path

import pytest

from lockwire.checks import run_record_checks
from lockwire.schema import load_contract

X500_CONTRACT = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"


def test_x500_demo_is_two_pm02_power_violations_without_noise():
    """The telemetry radio joins rail.pm02-5v (via
    rail.pixhawk-telem1-5v, TELEM1's published current limiter). Both the
    nominal and 7W-mode PM02 budgets now carry the radio's peak-fallback draw
    (5.2 W, transmit), which is real since the radio is physically fed through
    the FC from that rail. The 7W-mode budget flips pass -> violation
    (7 + 5 + 5.2 = 17.2 W > 15.6 W); the nominal budget was already a
    violation and grows (25 + 5 + 5.2 = 35.2 W)."""
    contract = load_contract(X500_CONTRACT)

    findings = run_record_checks(contract)
    violations = [finding for finding in findings if finding.severity == "violation"]
    warnings = [finding for finding in findings if finding.severity == "warning"]

    assert len(violations) == 2  # bites losing the newly-real 7W-mode violation
    assert warnings == []  # bites warning noise obscuring the headline demo defect
    finding = next(f for f in violations if f.quantity == "budgets/bud.power.pm02-5v")
    assert finding.severity == "violation"  # bites demoting the unsafe power budget to a warning
    assert set(finding.dependent.split(", ")) == {  # bites losing any overloaded dependent
        "dev.companion.jetson-orin-nano",
        "dev.camera.oak-d-lite",
        "dev.radio.rfd900x",
    }
    assert finding.owner == "power-integration"  # bites losing the budget owner
    assert finding.observed_quantity is not None
    assert finding.expected_quantity is not None
    assert finding.observed_quantity.value == pytest.approx(  # bites misreporting the 35.2 W draw
        35.20
    )
    assert finding.observed_quantity.unit == "W"
    assert finding.expected_quantity.value == pytest.approx(  # bites misreporting the rail limit
        15.60
    )
    assert finding.expected_quantity.unit == "W"
    # Human prose stays the rendered form; typed fields are the machine-readable magnitudes.
    assert finding.observed == "35.20 W (draw mode: nominal)"
    assert finding.expected == "<= 15.60 W"

    mode_finding = next(f for f in violations if f.quantity == "budgets/bud.power.pm02-5v.7w-mode")
    assert mode_finding.observed_quantity.value == pytest.approx(  # bites misreporting 17.2 W
        17.20
    )
    assert mode_finding.observed == "17.20 W (draw mode: 7W)"

    budget = next(budget for budget in contract.budgets if budget.id == "bud.power.pm02-5v")
    rail = contract.rail("rail.pm02-5v")
    assert budget.limit.base() == pytest.approx(15.60)  # bites drifting the published budget limit
    assert rail.capacity.base() == pytest.approx(15.60)  # bites drifting the PM02 rail capacity
    assert (
        rail.voltage.base() * 3.0
        == pytest.approx(  # bites losing the published 5.2 V x 3 A basis
            15.60
        )
    )

    telem_rail = contract.rail("rail.pixhawk-telem1-5v")
    assert telem_rail.capacity.base() == pytest.approx(  # bites drifting the TELEM1 limiter figure
        7.8
    )
    assert telem_rail.upstream == "rail.pm02-5v"  # bites losing the pass-through wiring


def test_x500_scored_floor_and_not_scored_set_after_b1_contract_growth():
    """A zero/floor assertion always carries a concrete number.

    The fixture declares the mass-total and endurance budgets, the
    Jetson's input_voltage_range, and the RFD900x's radio/link_budget/bus
    bitrates. It also declares published-default
    bitrates for bus.uart.telemetry/companion and bus.usb.companion (57600
    bps, 921600 bps, 5 Gbit/s); no device on those buses declares
    nominal_rate_hz+payload_bytes traffic, so check_bus_bandwidth has nothing
    to weigh against the now-known bitrate. A check closes the gap
    this leaves: a bus with a bitrate and no declared traffic is now disclosed
    as its own not_scored finding rather than emitting nothing, so these
    three buses join the not-scored set instead of vanishing from it. It
    also declares 4 motors' hover-derived draw on rail.servo-motor (a new
    scored rail_power budget) and a carrier_frequency for the RFD900x link
    budget. The antenna-gain gap is closed too: RFDesign's own
    RFD900x modem bundle page publishes a bundled antenna gain (3 dBi
    half-wave dipole), so the link budget is now fully populated and scores a
    real pass (large headroom over the 10 dB requirement at 2 km) rather than
    emitting a not_scored finding. This asserts the scored floor those
    additions bought, and pins the exact not-scored set left behind so an
    accidental regression (a silently-dropped budget, a reverted bitrate)
    shows up as a name mismatch instead of a vague count drift.
    """
    contract = load_contract(X500_CONTRACT)
    report = run_record_checks(contract)

    assert report.scored is not None
    assert report.scored >= 6  # bites losing a scored budget/voltage-compat comparison
    # bud.mass.payload has unknown masses, so it is not scored: 10 -> 9.
    assert report.scored == 9  # one scored key per comparison — 9 total on x500
    assert (
        report.scored_keys
        == {
            "budgets/bud.power.pm02-5v",
            "budgets/bud.power.pm02-5v.7w-mode",
            "budgets/bud.power.pixhawk-telem1",  # the radio's new TELEM1 rail budget
            "budgets/bud.power.servo-motor",
            "budgets/bud.endurance.mission",
            "devices/dev.companion.jetson-orin-nano/power/rail",
            "devices/dev.radio.rfd900x/power/rail",  # voltage compatibility, radio now on a modeled rail
            "devices/dev.radio.rfd900x/link_budget",  # no longer shares a key with the voltage-compat comparison above
            "rails/rail.pixhawk-telem1-5v",  # new rail's voltage comparison vs its upstream
        }
    )

    not_scored = {f.quantity for f in report if f.severity == "not_scored"}
    assert not_scored == {
        "model/structure_hash",  # no URDF provenance (pre-existing)
        "budgets/bud.mass.payload",  # unknown masses -> budget not scored
        "topology/buses/bus.i2c.external/bandwidth",  # Pixhawk 6C spec publishes no I2C rate
        "topology/buses/bus.can.dronecan/bandwidth",  # here3+ declares rate but no payload
        "topology/buses/bus.uart.telemetry/bandwidth",  # bitrate known, no declared traffic
        "topology/buses/bus.uart.companion/bandwidth",  # bitrate known, no declared traffic
        "topology/buses/bus.usb.companion/bandwidth",  # bitrate known, no declared traffic
        # jetson-orin-nano, camera.oak-d-lite and
        # magnetometer.ist8310 carry no `physical` block at all (mass
        # unknown); this used to be silently excluded from the mass budget,
        # now disclosed the same as a `physical` block with a missing mass.
        "devices/dev.companion.jetson-orin-nano/physical/mass",
        "devices/dev.camera.oak-d-lite/physical/mass",
        "devices/dev.magnetometer.ist8310/physical/mass",
    }

    violations = [f for f in report if f.severity == "violation"]
    assert len(violations) == 2  # PM02 headline + the newly-real 7W-mode violation
    assert {f.quantity for f in violations} == {
        "budgets/bud.power.pm02-5v",
        "budgets/bud.power.pm02-5v.7w-mode",
    }
    assert [f for f in report if f.severity == "warning"] == []


def test_x500_5v_overload_is_never_offered_a_battery_voltage_rail():
    """The 14.8 V motor rail would destroy 5 V loads, and TELEM1 is fed from the
    overloaded rail itself; neither is a real move, so only the regulator remains."""
    contract = load_contract(X500_CONTRACT)
    finding = next(
        f for f in run_record_checks(contract) if f.quantity == "budgets/bud.power.pm02-5v"
    )
    assert [a.kind for a in finding.alternatives] == ["projected"]
