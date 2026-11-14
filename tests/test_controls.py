import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from lockwire.controls import (
    ENGINE_CHECKS,
    fk,
    jacobian,
    joint_step,
    link_inertia,
    margin_findings,
    margins,
    peak_effort,
    reach_findings,
    step_response,
)
from lockwire.plan import Plan, Requirement, load_plan, validate_plan
from lockwire.schema import (
    Actuator,
    Contract,
    DeviceInstance,
    DeviceModel,
    JointController,
    RobotInfo,
    load_contract,
    Joint,
    JointLimits,
    Link,
    ModelRecord,
    Provenance,
)

L1, L2 = 0.7, 0.4


def planar(with_axis=True) -> ModelRecord:
    def rev(name, parent, child, x):
        return Joint(
            name=name,
            type="revolute",
            parent=parent,
            child=child,
            axis=(0, 0, 1) if with_axis else None,
            origin={"xyz": (x, 0, 0)},
        )

    return ModelRecord(
        source="p.urdf",
        source_sha256="0" * 64,
        structure_hash="x",
        links=[Link(name=n) for n in ("base", "l1", "l2", "tool")],
        joints=[
            rev("q1", "base", "l1", 0.0),
            rev("q2", "l1", "l2", L1),
            Joint(name="tip", type="fixed", parent="l2", child="tool", origin={"xyz": (L2, 0, 0)}),
        ],
        provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 11, 13)),
    )


Q = {"q1": 0.5, "q2": -0.9}


def test_fk_two_link_planar_matches_closed_form():
    tip = fk(planar(), Q)[:3, 3]
    x = L1 * np.cos(0.5) + L2 * np.cos(0.5 - 0.9)
    y = L1 * np.sin(0.5) + L2 * np.sin(0.5 - 0.9)
    assert tip == pytest.approx([x, y, 0.0], abs=1e-9)


def test_fk_orientation_is_sum_of_joint_angles():
    pose = fk(planar(), Q)
    assert np.arctan2(pose[1, 0], pose[0, 0]) == pytest.approx(-0.4)


def test_fk_jacobian_matches_finite_difference():
    names, jac = jacobian(planar(), Q)
    for k, name in enumerate(names):
        step = 1e-6
        hi = fk(planar(), {**Q, name: Q[name] + step})[:3, 3]
        lo = fk(planar(), {**Q, name: Q[name] - step})[:3, 3]
        assert jac[:3, k] == pytest.approx((hi - lo) / (2 * step), abs=1e-6)
    assert jac[5].tolist() == [1.0, 1.0]


def test_fk_prismatic_joint_slides_along_axis():
    model = planar()
    model.joints[1] = model.joints[1].model_copy(update={"type": "prismatic"})
    assert fk(model, {"q2": 0.25})[:3, 3] == pytest.approx([L1 + L2, 0, 0.25])


def test_fk_missing_input_yields_none_and_no_findings():
    assert fk(planar(with_axis=False), Q) is None
    assert jacobian(planar(with_axis=False), Q) is None
    assert fk(planar(), Q, tip="nowhere") is None
    assert margins(shoulder(gains=False), "shoulder") is None
    assert margin_findings(shoulder(gains=False)) == []
    assert margin_findings(shoulder(drive=False, loop=1.0)) == []


def _plan(*targets):
    return Plan(requirements=[Requirement(id=i, text="t", reach_m=xyz) for i, xyz in targets])


def test_reach_target_past_arm_length_reports_the_miss():
    found = reach_findings(planar(), _plan(("3", (L1 + L2 + 0.10, 0, 0))))
    assert [f.observed for f in found] == ["target 3 is out of reach by 0.10 m"]
    assert found[0].severity == "warning"


def test_reach_reachable_target_is_silent():
    assert reach_findings(planar(), _plan(("1", (0.5, 0.6, 0)), ("2", (L1 + L2, 0, 0)))) == []


def test_reach_target_only_reachable_past_a_joint_limit_is_unreachable():
    model = planar()
    model.joints[0] = model.joints[0].model_copy(
        update={"limits": JointLimits(lower=-0.1, upper=0.1)}
    )
    model.joints[1] = model.joints[1].model_copy(
        update={"limits": JointLimits(lower=-0.1, upper=0.1)}
    )
    found = reach_findings(model, _plan(("4", (0, 0.8, 0))))
    assert "target 4 is out of reach by" in found[0].observed


def test_reach_missing_input_emits_nothing():
    plan = _plan(("3", (5, 0, 0)))
    assert reach_findings(None, plan) == []
    assert reach_findings(planar(with_axis=False), plan) == []
    assert reach_findings(planar(), _plan(("3", None))) == []


def test_reach_runs_in_plan_validation(clean_contract):
    contract = clean_contract.model_copy(update={"model": planar()})
    plan = _plan(("3", (L1 + L2 + 0.10, 0, 0)))
    plan.steps = [{"kind": "set_power_mode", "device": "x", "mode": "nominal"}]
    assert "target 3 is out of reach by 0.10 m" in [
        f.observed for f in validate_plan(contract, plan)
    ]


EXAMPLES = Path(__file__).parent.parent / "src" / "lockwire" / "examples"
SHIPPED = sorted(
    p.parent.name for p in EXAMPLES.glob("*/plan.yaml") if (p.parent / "contract.yaml").exists()
)


@pytest.mark.parametrize("name", SHIPPED)
def test_reach_is_silent_on_every_shipped_example(name):
    contract = load_contract(EXAMPLES / name / "contract.yaml")
    plan = load_plan(EXAMPLES / name / "plan.yaml")
    assert all(r.reach_m is None for r in plan.requirements)
    assert reach_findings(contract.model, plan) == []
    assert not [f for f in validate_plan(contract, plan) if f.quantity.endswith("/reach_m")]


# --- joint model and step response ---------------------------------------------
GEAR, ROTOR, LINK_I, ARM_M, ARM_D = 10.0, 1e-4, 0.1, 2.0, 0.5
J_TOTAL = ROTOR * GEAR**2 + LINK_I + ARM_M * ARM_D**2  # 0.01 + 0.1 + 0.5 = 0.61


def shoulder(gains=True, rotor=ROTOR, friction=0.0, drive=True, loop=None, **motor) -> Contract:
    """One revolute shoulder (axis z) carrying a 2 kg arm whose CoM sits 0.5 m out."""
    arm = Link(
        name="arm",
        mass={"value": ARM_M, "unit": "kg"},
        com=(ARM_D, 0, 0),
        frame="arm",
        inertia={"moments_kgm2": (0.05, 0.08, LINK_I), "axes": ((1, 0, 0), (0, 1, 0), (0, 0, 1))},
    )
    model = ModelRecord(
        source="s.urdf",
        source_sha256="0" * 64,
        structure_hash="x",
        links=[Link(name="base"), arm],
        joints=[
            Joint(name="shoulder", type="revolute", parent="base", child="arm", axis=(0, 0, 1))
        ],
        provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 11, 13)),
    )
    act = Actuator(
        gear_ratio=GEAR, rotor_inertia_kgm2=rotor, viscous_friction=friction or None, **motor
    )
    return Contract(
        robot=RobotInfo(name="r"),
        model=model,
        device_models=[DeviceModel(id="m", family="f", actuator=act)],
        devices=[
            DeviceInstance(id="d", model="m", owner="o", drives_joint="shoulder" if drive else None)
        ],
        controllers=[JointController(joint="shoulder", kp=40.0, kd=6.0, loop_rate_hz=loop)]
        if gains
        else None,
    )


def analytic_overshoot(kp, kd, inertia) -> float:
    zeta = kd / (2 * np.sqrt(kp * inertia))
    return 100 * np.exp(-zeta * np.pi / np.sqrt(1 - zeta**2))


def test_link_inertia_is_parallel_axis_about_the_joint_axis():
    assert link_inertia(shoulder().model, "shoulder") == pytest.approx(LINK_I + ARM_M * ARM_D**2)


def test_shoulder_overshoot_matches_second_order_value_within_1pct():
    step = joint_step(shoulder(), "shoulder")
    assert step.inertia_kgm2 == pytest.approx(J_TOTAL)  # gear ratio squared on the rotor
    assert step.overshoot_pct == pytest.approx(analytic_overshoot(40.0, 6.0, J_TOTAL), rel=0.01)


def test_pd_on_pure_inertia_follows_the_closed_form_curve_and_settling():
    kp, kd, inertia = 100.0, 10.0, 1.0  # zeta = 0.5, wn = 10 rad/s
    step = step_response(inertia, kp, kd)
    zeta, wn = 0.5, 10.0
    wd = wn * np.sqrt(1 - zeta**2)
    exact = 1 - np.exp(-zeta * wn * step.t) * (
        np.cos(wd * step.t) + zeta * wn / wd * np.sin(wd * step.t)
    )
    assert step.y == pytest.approx(exact, abs=1e-4)
    last_out = np.nonzero(abs(exact - 1) > 0.02)[0][-1]
    assert step.settling_s == pytest.approx(step.t[last_out + 1])
    rise = step.t[np.argmax(exact >= 0.9)] - step.t[np.argmax(exact >= 0.1)]
    assert step.rise_s == pytest.approx(rise)


def test_viscous_friction_is_reflected_through_the_gear_ratio_squared():
    step = joint_step(shoulder(friction=1e-3), "shoulder")
    assert step.damping_nms_rad == pytest.approx(1e-3 * GEAR**2)


def test_missing_input_gives_no_step_and_no_finding():
    assert joint_step(shoulder(gains=False), "shoulder") is None
    assert joint_step(shoulder(drive=False), "shoulder") is None
    assert margins(shoulder(gains=False), "shoulder") is None
    assert margin_findings(shoulder(gains=False)) == []
    assert margin_findings(shoulder(drive=False, loop=1.0)) == []


# --- margins, bandwidth, ZOH, effort --------------------------------------------
KP, KD = 40.0, 6.0
WC = np.sqrt((KD**2 + np.sqrt(KD**4 + 4 * J_TOTAL**2 * KP**2)) / (2 * J_TOTAL**2))  # |L| = 1
PM = np.degrees(np.arctan(KD * WC / KP))  # PD on a pure inertia: 180 + angle(C) - 180


def test_margin_of_pd_on_inertia_matches_closed_form_crossover_and_phase():
    m = margins(shoulder(), "shoulder")
    assert m.crossover_rad_s == pytest.approx(WC, rel=1e-3)
    assert m.phase_margin_deg == pytest.approx(PM, abs=0.05)


def test_zoh_is_half_a_sample_of_delay_at_the_loop_rate():
    base, fast = margins(shoulder(), "shoulder"), margins(shoulder(loop=20.0), "shoulder")
    assert fast.crossover_rad_s == pytest.approx(base.crossover_rad_s)
    assert base.phase_margin_deg - fast.phase_margin_deg == pytest.approx(
        np.degrees(WC / 20.0 / 2), abs=0.05
    )


def test_margin_gain_margin_appears_only_once_the_delay_exists():
    assert margins(shoulder(), "shoulder").gain_margin_db is None
    assert margins(shoulder(loop=20.0), "shoulder").gain_margin_db > 0


def test_bandwidth_above_a_tenth_of_the_loop_rate_gets_an_advice_finding():
    fc = WC / (2 * np.pi)  # 1.8 Hz
    (f,) = [x for x in margin_findings(shoulder(loop=0.5 * fc * 10)) if "loop_rate" in x.quantity]
    assert f.quantity == "controllers/shoulder/loop_rate_hz"
    assert f.advice_unsourced and f.severity == "warning"
    assert not [x for x in margin_findings(shoulder(loop=3 * fc * 10)) if "loop_rate" in x.quantity]


def test_bandwidth_finding_needs_a_loop_rate_and_never_not_scored():
    assert margin_findings in ENGINE_CHECKS  # wired into the checks run
    assert margin_findings(shoulder()) == []


def test_margin_finding_fires_when_delay_eats_the_phase_margin():
    (f,) = margin_findings(shoulder(loop=8.0))[-1:]
    assert f.quantity == "controllers/shoulder/phase_margin" and f.advice_unsourced


def test_effort_peak_is_kp_times_the_unit_step_against_kt_stall_gear_limit():
    peak, limit = peak_effort(shoulder(torque_constant=0.05, stall_current_a=2.0), "shoulder")
    assert peak == pytest.approx(KP, rel=1e-3)
    assert limit == pytest.approx(0.05 * 2.0 * GEAR)


def test_effort_finding_only_over_the_limit_and_silent_without_a_cited_current():
    over = margin_findings(shoulder(torque_constant=0.05, stall_current_a=2.0))
    assert [f.quantity for f in over] == ["controllers/shoulder/effort"]
    assert margin_findings(shoulder(torque_constant=0.05, stall_current_a=100.0)) == []
    assert peak_effort(shoulder(torque_constant=0.05), "shoulder") is None


def test_every_advice_text_names_what_its_number_is_based_on():
    fs = margin_findings(shoulder(loop=8.0, torque_constant=0.05, stall_current_a=2.0))
    by = {f.quantity.rsplit("/", 1)[1]: f.expected for f in fs}
    assert set(by) == {"loop_rate_hz", "phase_margin", "effort"}
    assert "rule of thumb" in by["loop_rate_hz"] and "not yet tied to a source" in by["loop_rate_hz"]
    assert "rule of thumb" in by["phase_margin"] and "not yet tied to a source" in by["phase_margin"]
    assert "stall current" in by["effort"] and "gear ratio" in by["effort"]
