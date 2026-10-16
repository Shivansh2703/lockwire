"""Plan-validation suite: an agent's plan checked against the contract before
anything executes. Mutation bites for the four scored kinds, plus load/CLI
plumbing and the not-scored-when-undeclared rule."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.checks import _worst_rail_power
from lockwire.cli import app
from lockwire.plan import (
    JointTarget,
    Plan,
    Publish,
    RailLoad,
    SetPowerMode,
    load_plan,
    validate_plan,
)
from lockwire.report import render, verdict
from lockwire.schema import (
    Band,
    Contract,
    DeviceInstance,
    Driver,
    JointLimits,
    PowerDraw,
    Rail,
    RobotInfo,
    SignalSource,
    Topology,
    load_contract,
)

from conftest import FIXTURES, plain

X500 = FIXTURES / "x500" / "contract.yaml"
# the radio's peak-fallback draw now sits on rail.pm02-5v
# unconditionally, so no Jetson power mode fits under that rail's 15.6 W
# capacity any more (camera 5 W + radio 5.2 W already leaves only 5.4 W, below
# the Jetson's lowest 7 W mode). GO-plan examples use the corrected fixture,
# where the Jetson is on the dedicated 65 W Castle Creations BEC rail instead.
X500_CORRECTED = FIXTURES / "x500" / "contract_corrected.yaml"

runner = CliRunner()


def _violations(findings):
    return [f for f in findings if f.severity == "violation"]


def _not_scored(findings):
    return [f for f in findings if f.severity == "not_scored"]


def _coverage(report):
    return {outcome.path: outcome.status for outcome in report.outcomes}


@pytest.fixture
def x500_contract():
    return load_contract(X500)


def _armed_joint(clean_contract):
    """clean_contract's imu_mount joint, given lower/upper/velocity limits.

    clean_contract only declares effort=2.0; this test needs position and
    velocity limits too, so the fixture is armed once here rather than in
    every test.
    """
    clean_contract.model.joints[0].limits = JointLimits(
        lower=-1.0, upper=1.0, velocity=2.0, effort=2.0
    )
    return clean_contract


# --------------------------------------------------------------- JointTarget


def test_joint_position_in_range_is_silent(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=0.5)])
    assert validate_plan(contract, plan) == []


def test_joint_position_out_of_range_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=5.0)])

    findings = _violations(validate_plan(contract, plan))
    finding = next(f for f in findings if f.quantity == "plan/steps/0/joint_target/position")
    assert "5.0" in finding.observed  # bites dropping the commanded position from the finding
    assert "[-1.0, 1.0]" in finding.expected  # bites comparing against the wrong joint's limits


def test_joint_position_at_upper_bound_is_silent(clean_contract):
    """Bounds are inclusive: exactly at `limits.upper` is not a violation."""
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=1.0)])
    assert validate_plan(contract, plan) == []


def test_joint_position_at_lower_bound_is_silent(clean_contract):
    """Bounds are inclusive: exactly at `limits.lower` is not a violation."""
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=-1.0)])
    assert validate_plan(contract, plan) == []


def test_joint_position_just_below_lower_bound_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=-1.0001)])

    findings = _violations(validate_plan(contract, plan))
    finding = next(f for f in findings if f.quantity == "plan/steps/0/joint_target/position")
    assert "[-1.0, 1.0]" in finding.expected  # bites dropping the lower-bound check


def test_joint_position_just_above_upper_bound_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=1.0001)])

    findings = _violations(validate_plan(contract, plan))
    assert any(f.quantity == "plan/steps/0/joint_target/position" for f in findings)


def test_joint_velocity_over_limit_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", velocity=3.0)])

    findings = _violations(validate_plan(contract, plan))
    assert any(f.quantity == "plan/steps/0/joint_target/velocity" for f in findings)


def test_zero_joint_effort_limit_warns_instead_of_violating(clean_contract):
    contract = _armed_joint(clean_contract)
    contract.model.joints[0].limits.effort = 0.0
    findings = validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", effort=1.0)]))
    assert [f.severity for f in findings if "effort" in f.quantity] == ["warning"]
    assert not _violations(findings)


def test_joint_effort_over_joint_limit_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", effort=2.5)])

    findings = _violations(validate_plan(contract, plan))
    assert any(f.quantity == "plan/steps/0/joint_target/effort" for f in findings)
    # 2.5 is still under the actuator's 3.0 N*m continuous geared torque.
    assert not any(f.quantity == "plan/steps/0/joint_target/effort_actuator" for f in findings)


def test_joint_targets_report_declared_per_limit_enforcement(clean_contract):
    contract = _armed_joint(clean_contract)
    contract.model.joints[0].limits = JointLimits(
        lower=-1.0,
        upper=1.0,
        velocity=2.0,
        effort=2.0,
        lower_enforced_by="fuse",
        upper_enforced_by="firmware",
        velocity_enforced_by="driver",
        effort_enforced_by="this file",
    )

    findings = _violations(
        validate_plan(
            contract,
            Plan(steps=[JointTarget(joint="imu_mount", position=2.0, velocity=3.0, effort=2.5)]),
        )
    )
    expected = {f.quantity: f.expected for f in findings}
    assert "fuse (lower), firmware (upper)" in expected["plan/steps/0/joint_target/position"]
    assert "driver (velocity)" in expected["plan/steps/0/joint_target/velocity"]
    assert "this file (effort)" in expected["plan/steps/0/joint_target/effort"]


def test_joint_effort_over_actuator_rating_is_violation(clean_contract):
    contract = _armed_joint(clean_contract)
    contract.model.joints[0].limits = JointLimits(lower=-1.0, upper=1.0, velocity=2.0, effort=10.0)
    plan = Plan(steps=[JointTarget(joint="imu_mount", effort=3.5)])

    findings = validate_plan(contract, plan)
    finding = next(
        f
        for f in _violations(findings)
        if f.quantity == "plan/steps/0/joint_target/effort_actuator"
    )
    assert "3.5" in finding.observed  # bites dropping the commanded effort
    assert "3.00 N*m" in finding.expected  # bites omitting continuous torque times gear ratio


def _peak_only(clean_contract):
    contract = _armed_joint(clean_contract)
    contract.model.joints[0].limits = JointLimits(lower=-1.0, upper=1.0, velocity=2.0, effort=10.0)
    actuator = next(
        m.actuator for m in contract.device_models if m.actuator and m.actuator.torque_continuous
    )
    actuator.torque_peak = actuator.torque_continuous * 2
    actuator.torque_continuous = None
    return contract, actuator.torque_peak * actuator.gear_ratio


def test_peak_only_actuator_effort_over_peak_is_violation(clean_contract):
    contract, peak = _peak_only(clean_contract)
    plan = Plan(steps=[JointTarget(joint="imu_mount", effort=peak + 0.5)])
    finding = next(
        f
        for f in _violations(validate_plan(contract, plan))
        if f.quantity == "plan/steps/0/joint_target/effort_actuator"
    )
    assert f"{peak:.2f} N*m peak (stall) geared torque" in finding.expected


def test_peak_only_actuator_effort_under_peak_passes_and_is_scored(clean_contract):
    contract, peak = _peak_only(clean_contract)
    findings = validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", effort=peak)]))
    assert not any(f.quantity.endswith("effort_actuator") for f in findings)


def test_unknown_joint_is_not_scored_and_exits_3(clean_contract):
    plan = Plan(steps=[JointTarget(joint="ghost_joint", position=0.1)])

    findings = validate_plan(clean_contract, plan)
    assert _violations(findings) == []
    assert any(
        f.severity == "not_scored" and f.quantity == "plan/steps/0/joint_target/joint"
        for f in findings
    )
    assert render(findings) == 3


def test_continuous_joint_position_not_scored_velocity_still_checked(clean_contract):
    contract = clean_contract
    contract.model.joints[0].type = "continuous"
    contract.model.joints[0].limits = JointLimits(velocity=2.0, effort=2.0)
    plan = Plan(steps=[JointTarget(joint="imu_mount", position=99.0, velocity=3.0)])

    findings = validate_plan(contract, plan)
    assert any(
        f.severity == "not_scored" and f.quantity == "plan/steps/0/joint_target/position"
        for f in findings
    )
    assert any(
        f.severity == "violation" and f.quantity == "plan/steps/0/joint_target/velocity"
        for f in findings
    )


# ------------------------------------------------------------------ RailLoad


def test_two_railloads_fit_alone_but_violate_together(x500_contract):
    # The fixture puts 4 motors' hover-derived draw (246.68 W) on
    # rail.servo-motor, so the rail's baseline is no longer zero; the two
    # RailLoad steps still fit against 888 W alone (with baseline) but not
    # together.
    plan = Plan(
        steps=[
            RailLoad(rail="rail.servo-motor", draw={"value": 500, "unit": "W"}),
            RailLoad(rail="rail.servo-motor", draw={"value": 450, "unit": "W"}),
        ]
    )

    findings = validate_plan(x500_contract, plan)
    violations = [f for f in findings if f.quantity == "plan/steps/1/rail_load/draw"]
    assert len(violations) == 1
    finding = violations[0]
    assert finding.severity == "violation"
    assert "1196.68 W" in finding.observed  # bites scoring each RailLoad step in isolation
    assert "888.00 W" in finding.expected


def test_railload_reports_declared_capacity_enforcement_metadata(x500_contract):
    rail = x500_contract.rail("rail.servo-motor")
    assert rail is not None
    rail.capacity_enforced_by = "fuse"
    finding = next(
        f
        for f in validate_plan(
            x500_contract,
            Plan(steps=[RailLoad(rail="rail.servo-motor", draw={"value": 2000, "unit": "W"})]),
        )
        if f.severity == "violation"
    )
    assert "declared enforcement provenance: fuse" in finding.expected
    assert "does not prove physical protection" in finding.expected


def test_railload_unknown_rail_is_not_scored(x500_contract):
    plan = Plan(steps=[RailLoad(rail="rail.ghost", draw={"value": 1, "unit": "W"})])

    findings = validate_plan(x500_contract, plan)
    assert _violations(findings) == []
    assert any(f.severity == "not_scored" for f in findings)


def test_not_scored_reasons_name_the_real_cause(x500_contract):
    nopower = next(d for d in x500_contract.devices if d.power is None)
    plan = Plan(
        steps=[
            RailLoad(rail="rail.ghost", draw={"value": 1, "unit": "W"}),
            SetPowerMode(device=nopower.id, mode="high"),
        ]
    )
    reasons = [f.action for f in _not_scored(validate_plan(x500_contract, plan))]
    assert reasons[0] == "This robot has no rail named Ghost"
    assert reasons[1].endswith("has no power modes declared")


def _parent_child_rail_contract() -> Contract:
    """A parent rail with no direct devices and a child rail (efficiency 0.8)
    carrying one 40 W device. The child's 40/0.8 = 50 W only shows up on the
    parent through the rollup, not a direct-rail-only sum."""
    return Contract(
        robot=RobotInfo(name="rigtest"),
        devices=[
            DeviceInstance(
                id="dev.child",
                model="mdl.child",
                owner="test",
                power=PowerDraw(rail="rail.child", draw={"value": 40, "unit": "W"}),
            ),
        ],
        topology=Topology(
            rails=[
                Rail(
                    id="rail.parent",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 60, "unit": "W"},
                    upstream="battery",
                ),
                Rail(
                    id="rail.child",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 100, "unit": "W"},
                    upstream="rail.parent",
                    efficiency=0.8,
                ),
            ],
        ),
    )


def test_railload_rail_rollup_includes_child_rail(clean_contract):
    """A plan-time rail_load on a parent rail must count the child rail's
    rolled-up watts (upstream/efficiency), the same number checks._worst_rail_power
    gives `lockwire check` — not just devices declared directly on the parent."""
    contract = _parent_child_rail_contract()

    # 15 W alone would not overflow the 60 W parent capacity; only rolled-up
    # with the child's 50 W does.
    plan = Plan(steps=[RailLoad(rail="rail.parent", draw={"value": 15, "unit": "W"})])
    findings = validate_plan(contract, plan)
    violation = next(f for f in findings if f.quantity == "plan/steps/0/rail_load/draw")
    assert violation.severity == "violation"
    assert "65.00 W" in violation.observed  # 50 W child rollup + 15 W rail_load


def test_negative_joint_target_is_no_go(clean_contract):
    """A signed target compared only against the positive magnitude limit
    let -999 rad/s pass while +999 rad/s correctly failed. Magnitude limits (velocity, effort) must compare
    abs(target); position keeps its signed two-ended range."""
    contract = _armed_joint(clean_contract)

    negative = _violations(
        validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", velocity=-999.0)]))
    )
    assert any(f.quantity == "plan/steps/0/joint_target/velocity" for f in negative)

    positive = _violations(
        validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", velocity=999.0)]))
    )
    assert any(f.quantity == "plan/steps/0/joint_target/velocity" for f in positive)


def test_negative_joint_effort_is_no_go(clean_contract):
    """The effort half of the magnitude fix: -999 N*m must break BOTH compares
    the signed-vs-magnitude bug touched — the joint's own effort limit and the
    driving actuator's geared torque. Bites
    reverting abs() on either effort site."""
    contract = _armed_joint(clean_contract)  # effort limit 2.0, actuator 3.00 N*m geared

    findings = _violations(
        validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", effort=-999.0)]))
    )
    quantities = {f.quantity for f in findings}
    assert "plan/steps/0/joint_target/effort" in quantities
    assert "plan/steps/0/joint_target/effort_actuator" in quantities

    positive = _violations(
        validate_plan(contract, Plan(steps=[JointTarget(joint="imu_mount", effort=999.0)]))
    )
    assert {f.quantity for f in positive} == quantities


def test_unknown_draw_rail_is_not_scored():
    """A rail with an unknown-draw consumer must not be scored a peak at all — plan.py was taking only `_worst_rail_power`'s watts and dropping
    its unknown-consumer set, so a rail with an undeclared device's draw plus
    a legal-looking RailLoad still came back GO."""
    contract = Contract(
        robot=RobotInfo(name="rigtest"),
        devices=[
            DeviceInstance(
                id="dev.unknown",
                model="mdl.u",
                owner="test",
                power=PowerDraw(rail="rail.a"),  # no draw declared
            ),
        ],
        topology=Topology(
            rails=[
                Rail(
                    id="rail.a",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 10, "unit": "W"},
                    upstream="battery",
                ),
            ],
        ),
    )
    plan = Plan(steps=[RailLoad(rail="rail.a", draw={"value": 1, "unit": "W"})])
    findings = validate_plan(contract, plan)
    assert _violations(findings) == []
    finding = next(f for f in findings if f.quantity == "plan/steps/0/rail_load/draw")
    assert finding.severity == "not_scored"
    assert "dev.unknown" in finding.observed


def test_set_power_mode_on_unknown_draw_rail_is_not_scored():
    """The SetPowerMode half of the same gap: pinning one device's mode on
    a rail that also carries an undeclared-draw device must be not_scored, not
    a scored peak. Bites dropping the unknown set from
    `_rail_worst_w_with_device_pinned`, whose only caller is this branch."""
    contract = Contract(
        robot=RobotInfo(name="rigtest"),
        devices=[
            DeviceInstance(
                id="dev.unknown",
                model="mdl.u",
                owner="test",
                power=PowerDraw(rail="rail.a"),  # no draw declared
            ),
            DeviceInstance(
                id="dev.known",
                model="mdl.k",
                owner="test",
                power=PowerDraw(
                    rail="rail.a",
                    draw={"value": 1, "unit": "W"},
                    modes=[{"name": "high", "draw": {"value": 2, "unit": "W"}}],
                ),
            ),
        ],
        topology=Topology(
            rails=[
                Rail(
                    id="rail.a",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 10, "unit": "W"},
                    upstream="battery",
                ),
            ],
        ),
    )
    plan = Plan(steps=[SetPowerMode(device="dev.known", mode="high")])
    findings = validate_plan(contract, plan)
    assert _violations(findings) == []
    finding = next(f for f in findings if f.quantity == "plan/steps/0/set_power_mode/draw")
    assert finding.severity == "not_scored"
    assert "dev.unknown" in finding.observed
    assert render(findings) == 3


def _grandparent_child_rail_contract() -> Contract:
    """A 100 W child rail feeding a 5 W parent at efficiency 0.5 — 10 W added
    to the child is 20 W at the parent, over its 5 W capacity, even though
    the child itself has plenty of headroom."""
    return Contract(
        robot=RobotInfo(name="rigtest"),
        devices=[],
        topology=Topology(
            rails=[
                Rail(
                    id="rail.parent",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 5, "unit": "W"},
                    upstream="battery",
                ),
                Rail(
                    id="rail.child",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 100, "unit": "W"},
                    upstream="rail.parent",
                    efficiency=0.5,
                ),
            ],
        ),
    )


def test_child_rail_load_reaches_parent():
    """A RailLoad on a child rail must be checked against every ancestor's
    capacity too, propagated through each rail's efficiency — plan.py only
    checked the named rail, so this came back GO."""
    contract = _grandparent_child_rail_contract()
    plan = Plan(steps=[RailLoad(rail="rail.child", draw={"value": 10, "unit": "W"})])
    findings = validate_plan(contract, plan)
    violation = next(f for f in findings if f.severity == "violation")
    assert violation.quantity == "plan/steps/0/rail_load/draw"
    assert "rail.parent" in violation.observed
    assert "20.00 W" in violation.observed  # 10 W / 0.5 efficiency


def test_two_legal_loads_overload_shared_parent():
    """Two RailLoad steps on sibling child rails, each individually legal,
    must still be checked together against their shared parent's capacity."""
    contract = Contract(
        robot=RobotInfo(name="rigtest"),
        devices=[],
        topology=Topology(
            rails=[
                Rail(
                    id="rail.parent",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 15, "unit": "W"},
                    upstream="battery",
                ),
                Rail(
                    id="rail.childA",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 100, "unit": "W"},
                    upstream="rail.parent",
                ),
                Rail(
                    id="rail.childB",
                    voltage={"value": 12, "unit": "V"},
                    capacity={"value": 100, "unit": "W"},
                    upstream="rail.parent",
                ),
            ],
        ),
    )

    plan_a = Plan(steps=[RailLoad(rail="rail.childA", draw={"value": 10, "unit": "W"})])
    plan_b = Plan(steps=[RailLoad(rail="rail.childB", draw={"value": 10, "unit": "W"})])
    assert _violations(validate_plan(contract, plan_a)) == []
    assert _violations(validate_plan(contract, plan_b)) == []

    plan_pair = Plan(
        steps=[
            RailLoad(rail="rail.childA", draw={"value": 10, "unit": "W"}),
            RailLoad(rail="rail.childB", draw={"value": 10, "unit": "W"}),
        ]
    )
    findings = validate_plan(contract, plan_pair)
    violations = _violations(findings)
    assert {f.quantity for f in violations} == {
        "plan/steps/0/rail_load/draw",
        "plan/steps/1/rail_load/draw",
    }
    assert all("rail.parent" in f.observed for f in violations)


def _plan_rail_base_w(contract, rail_id: str) -> float:
    """The rail base watts `lockwire plan` actually scores, read back out of a
    forced-violation finding: RailLoad an amount guaranteed to overflow the
    rail's capacity, then parse the rolled-up draw out of ``observed``."""
    extra = contract.rail(rail_id).capacity.base() + 1.0
    plan = Plan(steps=[RailLoad(rail=rail_id, draw={"value": extra, "unit": "W"})])
    finding = next(f for f in validate_plan(contract, plan) if f.severity == "violation")
    observed_w = float(finding.observed.split(" W", 1)[0])
    return observed_w - extra


def test_overload_finding_carries_typed_watts_and_names_its_rail(x500_contract):
    """The panel draws "194 W of 60 W" and lights the rail from these, not from the text."""
    extra = x500_contract.rail("rail.servo-motor").capacity.base() + 1.0
    plan = Plan(steps=[RailLoad(rail="rail.servo-motor", draw={"value": extra, "unit": "W"})])
    f = next(f for f in validate_plan(x500_contract, plan) if f.severity == "violation")
    assert f.dependent == "rail.servo-motor"
    assert (f.observed_quantity.unit, f.expected_quantity.unit) == ("W", "W")
    assert f.observed_quantity.value > f.expected_quantity.value


@pytest.mark.parametrize("rail_id", ["rail.pm02-5v", "rail.servo-motor"])
def test_x500_rail_base_agrees_with_checks(x500_contract, rail_id):
    """plan.py's RailLoad base watts must equal checks.py's worst-rail-power
    number for the same rail — two answers for one number is the defect
    this test guards against."""
    assert _plan_rail_base_w(x500_contract, rail_id) == pytest.approx(
        _worst_rail_power(x500_contract, rail_id)[0]
    )


@pytest.mark.parametrize("rail_id", ["rail.pixhawk-telem1-5v", "rail.cc-bec-pro-5v"])
def test_x500_corrected_rail_base_agrees_with_checks(rail_id):
    """Same invariant as test_x500_rail_base_agrees_with_checks, on the two
    rails of the corrected fixture that test doesn't already cover."""
    contract = load_contract(X500_CORRECTED)
    assert _plan_rail_base_w(contract, rail_id) == pytest.approx(
        _worst_rail_power(contract, rail_id)[0]
    )


# -------------------------------------------------------------- SetPowerMode


def test_set_power_mode_lowest_still_violates_once_radio_is_on_the_rail(x500_contract):
    """The radio's peak-fallback draw (5.2 W) is now real,
    unconditional load on rail.pm02-5v alongside the camera (5 W); even the
    Jetson's lowest 7W mode overflows the 15.6 W rail (7+5+5.2=17.2 W)."""
    device = x500_contract.device("dev.companion.jetson-orin-nano")
    assert device is not None and device.power is not None
    device.power.choices = ["7W"]
    plan = Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="7W")])

    findings = validate_plan(x500_contract, plan)
    violations = _violations(findings)
    assert len(violations) == 1
    assert violations[0].quantity == "plan/steps/0/set_power_mode/draw"
    assert "17.20 W" in violations[0].observed


def test_set_power_mode_lowest_is_go_when_jetson_is_on_its_own_rail():
    """Same 7W plan, corrected fixture: the Jetson is on the dedicated 65 W
    Castle Creations BEC rail there, so its lowest mode is a real pass even
    with the radio's draw now on rail.pm02-5v."""
    contract = load_contract(X500_CORRECTED)
    plan = Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="7W")])

    findings = validate_plan(contract, plan)
    assert _violations(findings) == []


def test_set_power_mode_highest_is_violation(x500_contract):
    plan = Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="25W")])

    findings = _violations(validate_plan(x500_contract, plan))
    assert any(f.quantity == "plan/steps/0/set_power_mode/draw" for f in findings)


def test_set_power_mode_choice_refuses_declared_disallowed_mode():
    contract = load_contract(X500_CORRECTED)
    device = contract.device("dev.companion.jetson-orin-nano")
    assert device is not None and device.power is not None
    device.power.choices = ["7W"]

    allowed = validate_plan(contract, Plan(steps=[SetPowerMode(device=device.id, mode="7W")]))
    refused = validate_plan(contract, Plan(steps=[SetPowerMode(device=device.id, mode="15W")]))

    assert _violations(allowed) == []
    finding = next(f for f in _violations(refused) if f.quantity.endswith("set_power_mode/mode"))
    assert "declared" in finding.observed and "not allowed" in finding.observed
    assert finding.expected == "one of: 7W"
    assert _coverage(refused)["plan/steps/0/set_power_mode/mode"] == "FAIL"


def test_set_power_mode_unknown_device_or_mode_not_scored(x500_contract):
    plan = Plan(steps=[SetPowerMode(device="dev.ghost", mode="7W")])
    assert any(f.severity == "not_scored" for f in validate_plan(x500_contract, plan))

    plan = Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="99W")])
    assert any(f.severity == "not_scored" for f in validate_plan(x500_contract, plan))


# ------------------------------------------------------------------- Publish


def _wired_for_publish(x500_contract):
    """x500 fixture has no ROS driver wired up; arm one signal for the box."""
    device = x500_contract.device("dev.camera.oak-d-lite")
    device.driver = Driver(
        topic="/camera/imu",
        msg_type="sensor_msgs/msg/Imu",
        signals={"accel_z": SignalSource(field="linear_acceleration.z", unit="m/s^2")},
    )
    model = x500_contract.device_model("mdl.camera.oak-d-lite")
    model.hard_limits = {"accel_z": Band(lo=-50.0, hi=50.0, unit="m/s^2")}
    return x500_contract


def test_publish_outside_hard_limit_is_violation(x500_contract):
    contract = _wired_for_publish(x500_contract)
    plan = Plan(
        steps=[
            Publish(
                topic="/camera/imu",
                msg_type="sensor_msgs/msg/Imu",
                msg={"linear_acceleration": {"z": 120.0}},
            )
        ]
    )

    findings = _violations(validate_plan(contract, plan))
    finding = next(f for f in findings if f.quantity == "plan/steps/0/publish/accel_z")
    assert "120.0" in finding.observed  # bites dropping the resolved field value
    assert "[-50, 50]" in finding.expected  # bites comparing against the wrong signal's band


def test_publish_inside_hard_limit_is_silent(x500_contract):
    contract = _wired_for_publish(x500_contract)
    plan = Plan(
        steps=[
            Publish(
                topic="/camera/imu",
                msg_type="sensor_msgs/msg/Imu",
                msg={"linear_acceleration": {"z": 5.0}},
            )
        ]
    )
    assert validate_plan(contract, plan) == []


def test_publish_unknown_topic_is_not_scored(x500_contract):
    contract = _wired_for_publish(x500_contract)
    plan = Plan(steps=[Publish(topic="/nowhere", msg_type="sensor_msgs/msg/Imu", msg={"x": 1.0})])
    findings = validate_plan(contract, plan)
    assert _violations(findings) == []
    assert any(
        f.severity == "not_scored" and f.quantity == "plan/steps/0/publish/topic" for f in findings
    )


def test_publish_dimension_mismatch_is_not_scored_and_does_not_hide_later_steps(x500_contract):
    """Signal unit vs hard-limit unit in different dimensions must not raise UnitError
    out of validate_plan — it must score not_scored for that step and keep scoring the
    rest of the plan (D-uncaught-UnitError bounce)."""
    contract = _wired_for_publish(x500_contract)
    # hard_limits declared in kg; the signal is m/s^2 — different dimensions.
    model = contract.device_model("mdl.camera.oak-d-lite")
    model.hard_limits = {"accel_z": Band(lo=0.0, hi=10.0, unit="kg")}

    plan = Plan(
        steps=[
            Publish(
                topic="/camera/imu",
                msg_type="sensor_msgs/msg/Imu",
                msg={"linear_acceleration": {"z": 5.0}},
            ),
            RailLoad(rail="rail.servo-motor", draw={"value": 950, "unit": "W"}),
        ]
    )

    findings = validate_plan(contract, plan)
    not_scored = next(f for f in findings if f.quantity == "plan/steps/0/publish/accel_z")
    assert not_scored.severity == "not_scored"
    assert "m/s^2" in not_scored.observed and "kg" in not_scored.observed
    assert any(f.quantity == "plan/steps/1/rail_load/draw" for f in _violations(findings))

    assert render(findings) == 1  # violation wins, not a tool failure


# ------------------------------------------------------- assertion coverage


def test_coverage_publish_inside_is_pass_and_outside_is_fail(x500_contract):
    contract = _wired_for_publish(x500_contract)
    inside = validate_plan(
        contract,
        Plan(
            steps=[
                Publish(
                    topic="/camera/imu",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": 5.0}},
                )
            ]
        ),
    )
    outside = validate_plan(
        contract,
        Plan(
            steps=[
                Publish(
                    topic="/camera/imu",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": 500.0}},
                )
            ]
        ),
    )

    path = "plan/steps/0/publish/accel_z"
    assert _coverage(inside) == {path: "PASS"}
    assert _coverage(outside) == {path: "FAIL"}
    finding = next(f for f in outside if f.quantity == path)
    assert "declared" in (finding.expected + finding.action).lower()
    assert "unsafe" not in (finding.observed + finding.expected + finding.action).lower()


def test_coverage_publish_empty_signal_map_is_visible_unscored(x500_contract):
    contract = _wired_for_publish(x500_contract)
    contract.device("dev.camera.oak-d-lite").driver.signals = {}
    report = validate_plan(
        contract,
        Plan(
            steps=[
                Publish(
                    topic="/camera/imu",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": 5.0}},
                )
            ]
        ),
    )

    path = "plan/steps/0/publish/signals"
    assert _coverage(report) == {path: "UNSCORED"}
    assert any(f.quantity == path and f.severity == "not_scored" for f in report)
    assert verdict(report).code == "NOT_SCORED"


def test_coverage_publish_missing_and_nonnumeric_fields_are_each_visible_unscored(x500_contract):
    contract = _wired_for_publish(x500_contract)
    driver = contract.device("dev.camera.oak-d-lite").driver
    driver.signals["gyro_z"] = SignalSource(field="angular_velocity.z", unit="rad/s")
    model = contract.device_model("mdl.camera.oak-d-lite")
    model.hard_limits["gyro_z"] = Band(lo=-10.0, hi=10.0, unit="rad/s")
    report = validate_plan(
        contract,
        Plan(
            steps=[
                Publish(
                    topic="/camera/imu",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": "not-a-number"}},
                )
            ]
        ),
    )

    expected = {
        "plan/steps/0/publish/accel_z": "UNSCORED",
        "plan/steps/0/publish/gyro_z": "UNSCORED",
    }
    assert _coverage(report) == expected
    assert {f.quantity for f in _not_scored(report)} == set(expected)


def test_coverage_publish_wrong_message_type_is_error_and_refused(x500_contract):
    contract = _wired_for_publish(x500_contract)
    report = validate_plan(
        contract,
        Plan(
            steps=[
                Publish(
                    topic="/camera/imu",
                    msg_type="geometry_msgs/msg/Twist",
                    msg={"linear_acceleration": {"z": 500.0}},
                )
            ]
        ),
    )

    assert _coverage(report) == {"plan/steps/0/publish/msg_type": "ERROR"}
    assert _violations(report) == []
    assert verdict(report).code == "REFUSED"
    assert render(report) == 2


def test_coverage_publish_unknown_topic_is_visible_unscored(x500_contract):
    report = validate_plan(
        x500_contract,
        Plan(
            steps=[
                Publish(
                    topic="/unknown",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": 5.0}},
                )
            ]
        ),
    )

    path = "plan/steps/0/publish/topic"
    assert _coverage(report) == {path: "UNSCORED"}
    assert any(f.quantity == path and f.severity == "not_scored" for f in report)


def test_coverage_empty_plan_is_visible_unscored_without_clean_agreement(x500_contract):
    report = validate_plan(x500_contract, Plan(steps=[]))

    assert _coverage(report) == {"plan/steps": "UNSCORED"}
    decision = verdict(report)
    assert decision.code == "NOT_SCORED"
    assert decision.exit_code == 3
    assert "records agree" not in decision.summary


def test_coverage_mixed_plan_preserves_pass_fail_and_unscored(x500_contract):
    """Step 0's 7W mode is no longer a pass on this contract
    (the radio's peak-fallback draw now overflows rail.pm02-5v even at the
    Jetson's lowest mode — see test_set_power_mode_lowest_still_violates_*),
    so this mixed-outcome fixture is now FAIL/FAIL/UNSCORED, not PASS/FAIL/UNSCORED."""
    contract = _wired_for_publish(x500_contract)
    report = validate_plan(
        contract,
        Plan(
            steps=[
                SetPowerMode(device="dev.companion.jetson-orin-nano", mode="7W"),
                Publish(
                    topic="/camera/imu",
                    msg_type="sensor_msgs/msg/Imu",
                    msg={"linear_acceleration": {"z": 500.0}},
                ),
                Publish(topic="/unknown", msg_type="sensor_msgs/msg/Imu", msg={}),
            ]
        ),
    )

    assert _coverage(report) == {
        "plan/steps/0/set_power_mode/draw": "FAIL",
        "plan/steps/1/publish/accel_z": "FAIL",
        "plan/steps/2/publish/topic": "UNSCORED",
    }
    assert report.scored == 2
    assert verdict(report).code == "NO_GO"


# --------------------------------------------------------------------- misc


def test_empty_plan_is_one_not_scored_finding(x500_contract):
    findings = validate_plan(x500_contract, Plan(steps=[]))
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert findings[0].quantity == "plan/steps"


def test_verdict_matches_render_for_violation_and_go(x500_contract):
    violating = validate_plan(
        x500_contract,
        Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="25W")]),
    )
    assert verdict(violating).exit_code == 1

    # the radio's peak-fallback draw now sits on rail.pm02-5v
    # too, so the "clean" GO example needs the corrected fixture, where the
    # Jetson is on its own dedicated 65 W BEC rail (see X500_CORRECTED above).
    clean = validate_plan(
        load_contract(X500_CORRECTED),
        Plan(steps=[SetPowerMode(device="dev.companion.jetson-orin-nano", mode="7W")]),
    )
    assert verdict(clean).exit_code == 0


# ----------------------------------------------------------------------- CLI


def test_cli_plan_go(tmp_path: Path):
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "steps:\n"
        "  - kind: set_power_mode\n"
        "    device: dev.companion.jetson-orin-nano\n"
        '    mode: "7W"\n'
    )
    # the radio's peak-fallback draw now sits on rail.pm02-5v
    # too, so this GO example needs the corrected fixture, where the Jetson is
    # on its own dedicated 65 W BEC rail (see X500_CORRECTED above).
    result = runner.invoke(app, ["plan", str(X500_CORRECTED), str(plan_path)])
    assert result.exit_code == 0, result.output


def test_cli_plan_no_go(tmp_path: Path):
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "steps:\n"
        "  - kind: set_power_mode\n"
        "    device: dev.companion.jetson-orin-nano\n"
        '    mode: "25W"\n'
    )
    result = runner.invoke(app, ["plan", str(X500), str(plan_path)])
    assert result.exit_code == 1, result.output


def test_cli_plan_missing_file_is_tool_failure(tmp_path: Path):
    result = runner.invoke(app, ["plan", str(X500), str(tmp_path / "nope.yaml")])
    assert result.exit_code == 2


def test_cli_plan_unknown_kind_is_tool_failure(tmp_path: Path):
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("steps:\n  - kind: teleport\n    joint: shoulder\n")
    result = runner.invoke(app, ["plan", str(X500), str(plan_path)])
    assert result.exit_code == 2, result.output


def test_cli_plan_unknown_joint_is_not_scored_exit_3(tmp_path: Path):
    """A plan whose only assertion names a joint the contract cannot
    resolve must never read as GO — audit evidence was `GO — nothing scored;
    1 check(s) not scored`, exit 0, on an 800 N·m assertion against an unknown
    joint. That silent GO is the defect this closes."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "steps:\n  - kind: joint_target\n    joint: ghost_joint\n    effort: 800.0\n"
    )
    result = runner.invoke(app, ["plan", str(FIXTURES / "contract_clean.yaml"), str(plan_path)])
    flat = plain(result.output)
    assert "NOT SCORED" in flat
    assert "GO" not in flat
    assert result.exit_code == 3, result.output


def test_load_plan_rejects_deep_nesting_no_traceback(tmp_path: Path):
    # MUTATION-BITE: 6 KB of `[` blows ruamel's recursive descent before the
    # anchor guard runs, raising a bare RecursionError instead of a refusal.
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("[" * 6000 + "]" * 6000)
    with pytest.raises(ValueError, match=r"nesting too deep"):
        load_plan(plan_path)


def test_load_plan_unknown_kind_raises(tmp_path: Path):
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("steps:\n  - kind: teleport\n    joint: shoulder\n")
    with pytest.raises(ValueError):
        load_plan(plan_path)


# --------------------------------------------------- max_rate
#
# An optional `max_rate` (units/s) on a Band,
# "scored by `plan` and `check` (a step that is inside the band at both ends and
# destructive in between). ... Never enforced on a device."
#
# A plan carries no timestamps, so the elapsed time between two publishes on a
# topic is the contract's own `driver.nominal_rate_hz`. Both operands must be
# present or the comparison says so out loud — see the two not-scored tests.


def _wired_for_rate(x500_contract, *, max_rate=100.0, rate_hz=100.0):
    """`_wired_for_publish` plus a declared rate of change and publish rate."""
    contract = _wired_for_publish(x500_contract)
    device = next(d for d in contract.devices if d.model == "mdl.camera.oak-d-lite")
    assert device.driver is not None
    device.driver.nominal_rate_hz = rate_hz
    model = contract.device_model("mdl.camera.oak-d-lite")
    model.hard_limits = {"accel_z": Band(lo=-50.0, hi=50.0, unit="m/s^2", max_rate=max_rate)}
    return contract


def _accel_plan(*values):
    return Plan(
        steps=[
            Publish(
                topic="/camera/imu",
                msg_type="sensor_msgs/msg/Imu",
                msg={"linear_acceleration": {"z": v}},
            )
            for v in values
        ]
    )


def test_step_within_max_rate_is_silent(x500_contract):
    """Both endpoints in band and the move between them inside `max_rate`: GO,
    and no rate finding at all."""
    # 0.5 m/s^2 per message at 100 Hz = 50 m/s^2/s, half the declared bound.
    report = validate_plan(_wired_for_rate(x500_contract), _accel_plan(-0.5, 0.0))
    assert [f for f in report if f.quantity.endswith("/rate") and f.severity != "not_scored"] == []
    assert _violations(report) == []
    assert verdict(report).code == "GO"
    assert _coverage(report)["plan/steps/1/publish/accel_z/rate"] == "PASS"


def test_step_exceeding_max_rate_is_violation(x500_contract):
    """Endpoints legal, the move between them destructive: NO-GO (the verdict word is NO-GO)."""
    # -49 -> 49 m/s^2 in one 100 Hz message = 9800 m/s^2/s against a 100 bound.
    report = validate_plan(_wired_for_rate(x500_contract), _accel_plan(-49.0, 49.0))
    assert _violations(report) and verdict(report).code == "NO_GO"
    finding = next(
        f for f in _violations(report) if f.quantity == "plan/steps/1/publish/accel_z/rate"
    )
    assert "9800" in finding.observed  # bites dropping the Hz factor
    assert "100" in finding.expected  # bites comparing against the wrong bound
    # The endpoints themselves are legal — this finding is about the move.
    assert _coverage(report)["plan/steps/1/publish/accel_z"] == "PASS"
    assert _coverage(report)["plan/steps/1/publish/accel_z/rate"] == "FAIL"


def test_max_rate_first_value_is_visibly_not_scored(x500_contract):
    """No previous value means no rate — and that must be visible, never a
    silent pass. A rate limit that quietly stops applying has stopped biting."""
    report = validate_plan(_wired_for_rate(x500_contract), _accel_plan(-49.0))
    finding = next(
        f for f in _not_scored(report) if f.quantity == "plan/steps/0/publish/accel_z/rate"
    )
    assert "first value" in finding.observed
    assert _coverage(report)["plan/steps/0/publish/accel_z/rate"] == "UNSCORED"


def test_max_rate_without_declared_publish_rate_is_visibly_not_scored(x500_contract):
    """A plan has no timestamps; without `driver.nominal_rate_hz` the time
    between two steps is unknown. Say so, do not pass silently."""
    contract = _wired_for_rate(x500_contract, rate_hz=None)
    report = validate_plan(contract, _accel_plan(-49.0, 49.0))
    finding = next(
        f for f in _not_scored(report) if f.quantity == "plan/steps/1/publish/accel_z/rate"
    )
    assert "nominal_rate_hz" in finding.observed
    assert _violations(report) == []
    assert _coverage(report)["plan/steps/1/publish/accel_z/rate"] == "UNSCORED"


def test_max_rate_never_actuates_only_scores(x500_contract):
    """The rule: "compare now, predict eventually, never actuate". A
    max_rate crossing produces a Finding and nothing else — the contract and the
    plan come back untouched and validate_plan is repeatable.

    Ceiling, named not chased: the source scan below is the same text-level
    device-write shape `tests/test_never_actuates.py` uses repo-wide, narrowed
    to plan.py. An indirectly-built write (a subprocess spawn, a composed /dev
    path) still defeats it."""
    import re

    contract = _wired_for_rate(x500_contract)
    plan = _accel_plan(-49.0, 49.0)
    before = (contract.model_dump(), plan.model_dump())

    first = [(f.severity, f.quantity) for f in validate_plan(contract, plan)]
    second = [(f.severity, f.quantity) for f in validate_plan(contract, plan)]

    assert ("violation", "plan/steps/1/publish/accel_z/rate") in first
    assert first == second  # scoring twice changes nothing
    assert (contract.model_dump(), plan.model_dump()) == before

    source = (Path(__file__).parent.parent / "src" / "lockwire" / "plan.py").read_text()
    assert not re.search(r"\bos\.write\(|\bioctl\(", source)
    assert not re.search(r"^\s*(import|from)\s+(smbus2?|serial)\b", source, re.MULTILINE)


def test_cli_plan_zero_effort_limit_is_go_with_notes_not_crash(tmp_path: Path):
    """A 0 effort limit is unset, so the plan warns (exit 0) instead of
    crashing on an UNSCORED outcome with no not_scored finding."""
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        (FIXTURES / "contract_clean.yaml").read_text().replace("effort: 2.0", "effort: 0.0")
    )
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("steps:\n  - kind: joint_target\n    joint: imu_mount\n    effort: 1.0\n")
    out = tmp_path / "plan.json"
    result = runner.invoke(app, ["plan", str(contract), str(plan_path), "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "torque is not checked" in plain(result.output)
    steps = json.loads(out.read_text())["steps"]
    assert "plan/steps/0/joint_target/effort" not in {s["path"] for s in steps}


def test_free_api_plan_document_carries_joined_steps(tmp_path: Path):
    """The free ``api.plan`` (no plugin override) writes ``steps`` and ``finding_labels``."""
    from lockwire import api

    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "steps:\n  - kind: publish\n    topic: /unknown\n    msg_type: sensor_msgs/msg/Imu\n    msg: {}\n"
    )
    document = api.plan(X500, plan_path)
    assert document["finding_labels"] == []
    assert [s["status"] for s in document["steps"]] == ["UNSCORED"]
    assert document["steps"][0]["not_scored_reason"]
