"""Gravity torque on a two-link planar arm; expected values are hand-computed."""

import datetime as dt
import math

import pytest

from lockwire.schema import Joint, JointLimits, Link, ModelRecord, Provenance
from lockwire.statics import joint_gravity_torques


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
