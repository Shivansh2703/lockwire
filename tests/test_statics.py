"""Gravity torque on a two-link planar arm; expected values are hand-computed."""

import datetime as dt
import math

import pytest

from lockwire.schema import Joint, JointLimits, Link, ModelRecord, Provenance
from lockwire.statics import (
    joint_gravity_torques,
    move_peak_torques,
    rnea,
    support_polygon,
    system_com,
    tip_distance,
    trapezoid_profile,
)


def arm(shoulder_effort=100.0, elbow_effort=100.0) -> ModelRecord:
    """Horizontal arm along +X, joints about +Y: link1 2 kg (CoM 0.5 m), link2 1 kg (CoM 0.5 m)."""

    def link(name, kg):
        return Link(name=name, mass={"value": kg, "unit": "kg"}, com=(0.5, 0, 0), frame=name)

    def joint(name, parent, child, x, effort):
        return Joint(
            name=name,
            type="revolute",
            parent=parent,
            child=child,
            axis=(0, 1, 0),
            origin={"xyz": (x, 0, 0)},
            limits=JointLimits(effort=effort),
        )

    return ModelRecord(
        source="arm.urdf",
        source_sha256="0" * 64,
        structure_hash="x",
        links=[Link(name="base"), link("l1", 2.0), link("l2", 1.0), Link(name="tool")],
        joints=[
            joint("shoulder", "base", "l1", 0.0, shoulder_effort),
            joint("elbow", "l1", "l2", 1.0, elbow_effort),
            Joint(name="tip", type="fixed", parent="l2", child="tool", origin={"xyz": (1, 0, 0)}),
        ],
        provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 10, 19)),
    )


def test_horizontal_arm_torques_by_hand():
    shoulder, elbow = joint_gravity_torques(arm())
    assert shoulder.torque_nm == pytest.approx(9.81 * (2 * 0.5 + 1 * 1.5))  # 24.525
    assert elbow.torque_nm == pytest.approx(9.81 * 1 * 0.5)  # 4.905
    assert shoulder.margin_nm == pytest.approx(100 - 24.525)


def gripper_arm() -> ModelRecord:
    """arm() with a two-finger gripper on the tool: 0.5 kg per finger at the tool origin (2 m)."""
    m = arm()
    for f in ("f1", "f2"):
        m.links.append(Link(name=f, mass={"value": 0.5, "unit": "kg"}, com=(0, 0, 0), frame=f))
        m.joints.append(Joint(name=f"{f}_j", type="revolute", parent="tool", child=f, axis=(0, 0, 1),
                              limits=JointLimits(effort=1.0)))  # fmt: skip
    return m


def test_tree_shoulder_carries_both_fingers():
    """The arm starter must score with its gripper: a branched tree scores with the fingers'
    masses in the shoulder's sum (before trees: None, so the check was silent)."""
    rows = {r.joint: r for r in joint_gravity_torques(gripper_arm())}
    assert rows["shoulder"].torque_nm == pytest.approx(9.81 * (1 + 1.5 + 2 * 0.5 * 2))  # 44.145
    assert rows["elbow"].torque_nm == pytest.approx(9.81 * (0.5 + 2 * 0.5 * 1))  # 14.715
    assert rows["f1_j"].torque_nm == pytest.approx(0.0, abs=1e-9)  # finger CoM on its axis


def test_payload_and_hanging_pose():
    """Payload at the tool tip: a link field, a point mass at that link's origin (2 m out)."""
    m = arm()
    m.links[3] = Link(name="tool", payload={"value": 1, "unit": "kg"})
    shoulder, elbow = joint_gravity_torques(m)
    assert elbow.payload_kg == 1.0
    assert elbow.torque_nm == pytest.approx(9.81 * (1 * 0.5 + 1 * 1.0))  # payload at tip, 1 m out
    assert shoulder.torque_nm == pytest.approx(9.81 * (2 * 0.5 + 1 * 1.5 + 1 * 2.0))
    hanging = joint_gravity_torques(arm(), {"shoulder": math.pi / 2})
    assert hanging[0].torque_nm == pytest.approx(0.0, abs=1e-9)


def test_axisless_model_is_none():
    m = arm()
    m.joints[1].axis = None
    assert joint_gravity_torques(m) is None


def chain1(com=(0, 0, 0.5), axis=(0, 1, 0), rpy=(0, 0, 0), limits=None, kg=2.0):
    """One revolute joint under a base, one `kg` link (effort 9 N*m): shared with the reach tests."""
    j = Joint(name="j", type="revolute", parent="base", child="l", axis=axis,
              origin={"xyz": (0, 0, 0), "rpy": rpy}, limits=JointLimits(effort=9.0, **(limits or {})))  # fmt: skip
    return ModelRecord(
        source="c.urdf", source_sha256="0" * 64, structure_hash="x",
        links=[Link(name="base"), Link(name="l", mass={"value": kg, "unit": "kg"}, com=com, frame="l")],
        joints=[j], provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 10, 20)),
    )  # fmt: skip


def test_closed_chain_under_a_root_raises_not_hangs():
    """A loop under one root must end, typed (it used to hang)."""
    from pathlib import Path

    from lockwire.api import load
    from lockwire.statics import ChainError

    with pytest.raises(ChainError, match="closed chain"):
        joint_gravity_torques(
            load(Path(__file__).parent / "fixtures/fourbar_world/lockwire.yaml").model
        )


@pytest.mark.parametrize("name", ["fivebar_sym.sdf", "fivebar_sym_closure_first.sdf"])
def test_closed_loop_torque_matches_the_hand_worked_five_bar(name):
    """1 kg at the apex of a symmetric five-bar, hips mirrored by d. Virtual work: each hip holds
    (W/2) |dz/dpsi|, z the apex height and psi the crank's splay from vertical. The answer must
    not depend on which of the lower link's two joints the file declares first."""
    from pathlib import Path

    from lockwire.sdf import loop_cuts, parse_sdf
    from lockwire.statics import loop_plan, loop_torques

    sdf = Path(__file__).parent / "fixtures" / name
    lp = loop_plan(parse_sdf(sdf), loop_cuts(sdf))
    ((driven, passive, _pins),) = lp.loops
    assert [j.name for j in driven] == ["hip_r", "hip_l"] and len(passive) == 2
    d, e, l1, l2 = 0.2, 0.02, 0.05, 0.13
    psi = math.atan2(0.03, 0.04) - d  # +d about +Y closes the right crank's splay
    x = e + l1 * math.sin(psi)  # the crank tip's distance from the centre line
    want = 9.81 / 2 * (l1 * math.sin(psi) + x * l1 * math.cos(psi) / math.sqrt(l2**2 - x**2))
    got = loop_torques(lp, lp.loops[0], [[d, -d]])[0]
    assert [abs(t) for t in got] == pytest.approx([want, want], rel=1e-6)


def test_closed_loop_gives_no_number_past_a_locking_pose():
    """Parallelogram four-bar, 1 kg coupler on 0.05 m cranks: the driven crank holds m g L sin(q)
    until the cranks lie flat along the coupler (q = 90 deg), where the linkage can flip. A pose
    past that closes too, on either assembly, so it must come back with no number."""
    from pathlib import Path

    from lockwire.sdf import loop_cuts, parse_sdf
    from lockwire.statics import loop_plan, loop_torques

    sdf = Path(__file__).parent / "fixtures/fourbar_par.sdf"
    lp = loop_plan(parse_sdf(sdf), loop_cuts(sdf))
    held = loop_torques(lp, lp.loops[0], [[0.7]])[0, 0]
    assert abs(held) == pytest.approx(9.81 * 0.05 * math.sin(0.7), rel=1e-6)
    assert math.isnan(loop_torques(lp, lp.loops[0], [[1.9]])[0, 0])


def test_axis_is_normalised_and_zero_axis_is_typed():
    """An unnormalised axis: (0,0.1,0) and (0,2,0) equal (0,1,0); a zero axis names the joint."""
    from lockwire.statics import ChainError

    got = [
        joint_gravity_torques(chain1(com=(0.5, 0, 0), axis=(0, n, 0)))[0].torque_nm
        for n in (0.1, 1, 2)
    ]
    assert got == [pytest.approx(9.81)] * 3  # 2 kg * 0.5 m * g
    with pytest.raises(ChainError, match="'j'"):
        joint_gravity_torques(chain1(axis=(0, 0, 0)))


def test_x_axis_term_with_yawed_origin_by_hand():
    """Sign and x-term guard: axis (1,0,0), origin yaw 45 deg.

    World axis (s,s,0), s=sqrt(.5); CoM (0,.5,0) -> r=(-s/2, s/2, 0), F=(0,0,-2g).
    r x F = (-r_y*2g, r_x*2g, 0) = (-6.936, -6.936, 0); axis . = -9.81 (stored as |.|).
    A wrong sign or dropped x-term gives 0 here, not 9.81.
    """
    (row,) = joint_gravity_torques(chain1(com=(0, 0.5, 0), axis=(1, 0, 0), rpy=(0, 0, math.pi / 4)))
    assert row.torque_nm == pytest.approx(9.81)


def test_primitive_inertia_matches_textbook_values():
    import numpy as np

    from lockwire.statics import primitive_inertia

    assert np.allclose(np.diag(primitive_inertia("sphere", (0.2,), 5)), 0.08)  # 2/5 m r^2
    cyl = np.diag(primitive_inertia("cylinder", (0.1, 0.6), 3))
    assert np.allclose(cyl, [0.0975, 0.0975, 0.015])  # m(3r^2+h^2)/12 twice, m r^2/2 on the axis
    assert np.allclose(
        np.diag(primitive_inertia("box", (1, 2, 3), 12)), [13, 10, 5]
    )  # m(b^2+c^2)/12


def test_merged_boxes_give_the_hand_summed_inertia():
    import numpy as np

    from lockwire.statics import merge_inertia, primitive_inertia

    own = primitive_inertia("box", (1, 1, 1), 1)  # 1/6 on each axis
    mass, com, inertia = merge_inertia([(1, (1, 0, 0), own), (1, (-1, 0, 0), own)])
    # Ixx = 2*(1/6); Iyy = Izz = 2*(1/6) + 2*(1 kg * (1 m)^2), the parallel-axis term
    assert mass == 2 and np.allclose(com, 0)
    assert np.allclose(np.diag(inertia), [1 / 3, 7 / 3, 7 / 3], atol=1e-12)


def test_support_polygon_drops_interior_and_collinear_points():
    hull = support_polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)])
    assert sorted(hull) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert support_polygon([(0, 0), (1, 0), (2, 0)]) is None


def test_tip_distance_is_signed_by_side_of_the_polygon():
    hull = support_polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
    assert tip_distance((0.5, 1.0), hull) == pytest.approx(0.5)
    assert tip_distance((3.0, 1.0), hull) == pytest.approx(-1.0)


def test_support_com_uses_link_origins_through_fixed_joints():
    model = arm()
    com = system_com(model)  # l1 2 kg at x 0.5, l2 1 kg at x 1.5 with elbow at 1.0
    assert com[0] == pytest.approx((2 * 0.5 + 1 * 1.5) / 3)


def two_link(l1=0.5, l2=0.4, m1=2.0, m2=1.0):
    """Planar arm in the XZ plane, joints about +Y, point mass at each link's far end (0,0,-l)."""

    def link(name, kg, com):
        return Link(name=name, mass={"value": kg, "unit": "kg"}, com=com, frame=name)

    def joint(name, parent, child, z):
        return Joint(name=name, type="revolute", parent=parent, child=child, axis=(0, 1, 0),
                     origin={"xyz": (0, 0, z)}, limits=JointLimits(effort=50.0, velocity=2.0, acceleration=4.0))  # fmt: skip

    return ModelRecord(
        source="p.urdf", source_sha256="0" * 64, structure_hash="x",
        links=[Link(name="base"), link("a", m1, (0, 0, -l1)), link("b", m2, (0, 0, -l2))],
        joints=[joint("j1", "base", "a", 0.0), joint("j2", "a", "b", -l1)],
        provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 11, 13)),
    )  # fmt: skip


def test_rnea_single_pendulum_matches_closed_form():
    """q = 0 hangs straight down; a point mass l below a +Y axis needs m l^2 qdd + m g l sin q."""
    m, arm_l, q, qdd = 1.5, 0.7, 0.9, 2.3
    ((_, tau),) = rnea(chain1(com=(0, 0, -arm_l), kg=m), [q], [1.1], [qdd])
    assert tau == pytest.approx(m * arm_l**2 * qdd + m * 9.81 * arm_l * math.sin(q), abs=1e-6)


def test_rnea_two_link_matches_lagrange_with_coriolis():
    """Textbook two-link arm (point masses): mass matrix, Coriolis/centrifugal h, gravity."""
    l1, l2, m1, m2, g = 0.5, 0.4, 2.0, 1.0, 9.81
    q1, q2, v1, v2, a1, a2 = 0.6, -0.8, 1.3, -0.7, 2.0, 1.5
    h = m2 * l1 * l2 * math.sin(q2)
    m12 = m2 * l2 * l2 + m2 * l1 * l2 * math.cos(q2)
    m11 = (m1 + m2) * l1 * l1 + m2 * l2 * l2 + 2 * m2 * l1 * l2 * math.cos(q2)
    t1 = m11 * a1 + m12 * a2 - h * (2 * v1 * v2 + v2 * v2)
    t1 += (m1 + m2) * g * l1 * math.sin(q1) + m2 * g * l2 * math.sin(q1 + q2)
    t2 = m12 * a1 + m2 * l2 * l2 * a2 + h * v1 * v1 + m2 * g * l2 * math.sin(q1 + q2)
    got = rnea(two_link(), [q1, q2], [v1, v2], [a1, a2])
    assert [t for _, t in got] == pytest.approx([t1, t2], abs=1e-9)


def test_rnea_declared_inertia_adds_to_the_pendulum():
    """A rod's own inertia about its COM adds I*qdd to the point-mass torque."""
    from lockwire.schema import PrincipalInertia

    rec = chain1(com=(0, 0, -0.5), kg=2.0)
    rec.links[1].inertia = PrincipalInertia(
        moments_kgm2=(0.1, 0.1, 0.1), axes=((1, 0, 0), (0, 1, 0), (0, 0, 1))
    )
    ((_, tau),) = rnea(rec, [0.0], [0.0], [3.0])
    assert tau == pytest.approx((0.1 + 2.0 * 0.25) * 3.0, abs=1e-9)


def test_rnea_missing_inputs_is_none():
    assert rnea(chain1(com=None), [0.0], [0.0], [0.0]) is None


def test_rnea_trapezoid_profile_covers_the_distance_and_obeys_limits():
    t, s, sd, sdd = trapezoid_profile(1.0, 2.0, 4.0)
    assert (s[-1], sd.max(), sdd.max()) == pytest.approx((1.0, 2.0, 4.0))
    assert t[-1] == pytest.approx(1.0 / 2.0 + 2.0 / 4.0)


def test_rnea_move_peak_torque_exceeds_gravity_alone_and_needs_limits():
    """Hanging at rest the arm holds nothing; moving it asks for the inertial torque."""
    rows = move_peak_torques(two_link(), {}, {"j1": 1.0, "j2": 0.5})
    assert all(r.torque_nm > 0 for r in rows)
    assert rows[1].margin_nm == pytest.approx(50.0 - rows[1].torque_nm)
    assert move_peak_torques(chain1(), {}, {"j": 1.0}) is None


def test_move_peak_torque_is_the_mid_move_maximum_not_the_last_sample():
    """Point-mass pendulum, 0 -> 0.3 rad at vmax 1, amax 10: the peak is the end of the ramp-up."""
    m, arm_l, g = 1.0, 1.0, 9.81
    rec = chain1(com=(0, 0, -arm_l), kg=m, limits={"velocity": 1.0, "acceleration": 10.0})
    (row,) = move_peak_torques(rec, {}, {"j": 0.3})
    ramp_up = m * arm_l**2 * 10.0 + m * g * arm_l * math.sin(0.5 * 10.0 * 0.1**2)
    assert row.torque_nm == pytest.approx(ramp_up, abs=1e-6)
    assert row.torque_nm > m * g * arm_l * math.sin(0.3) + 5.0


def test_rnea_gyroscopic_term_of_an_asymmetric_body():
    """Spinning about z while the axis-x joint is tilted q: tau_x = (I3 - I2) w^2 sin q cos q."""
    from lockwire.schema import PrincipalInertia

    eye = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    body = Link(name="b", mass={"value": 1.0, "unit": "kg"}, com=(0, 0, 0), frame="b",
                inertia=PrincipalInertia(moments_kgm2=(1.0, 2.0, 3.0), axes=eye))  # fmt: skip
    j1 = Joint(name="j1", type="revolute", parent="base", child="a", axis=(0, 0, 1))
    j2 = Joint(name="j2", type="revolute", parent="a", child="b", axis=(1, 0, 0))
    rec = ModelRecord(
        source="g.urdf", source_sha256="0" * 64, structure_hash="x",
        links=[Link(name="base"), Link(name="a"), body], joints=[j1, j2],
        provenance=Provenance(witness="human", source="t", locked_at=dt.date(2026, 11, 13)),
    )  # fmt: skip
    q, w = 0.5, 1.7
    got = dict(rnea(rec, [0.0, q], [w, 0.0], [0.0, 0.0]))
    assert abs(got["j2"]) == pytest.approx(w * w * math.sin(q) * math.cos(q), abs=1e-9)
