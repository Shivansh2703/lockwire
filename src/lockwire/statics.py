"""Rigid-body statics for stage one — support reactions, determinate sets only.

``docs/design/e5_solver_choice.md`` chose **numpy-stiffness** by
measurement. This module is the *determinate subset* of that choice, and the
narrowing is deliberate, not a substitution:

* For a **statically determinate** support set the reactions follow from
  equilibrium alone and are **independent of the stiffness EI**. The solver study measured
  exactly that — its ``equilibrium-statics`` candidate matched the textbook
  reactions to 1.9e-16 — and rejected it only because it cannot produce
  *deflections* or answer a *redundant* set.
* For a **redundant** set the reactions *do* depend on EI. The contract schema
  is frozen and declares no Young's modulus, no section and no area on
  ``Link`` or ``Physical``, so there is nothing to solve a redundant set with.
  Those read NOT SCORED naming why, never a plausible-looking guess.

Consequently a stiffness solve has no input it could consume here, and a beam
deflection is not offered at all. What that document lists as unproven —
plates, shear-dominated stubby members, out-of-plane frames, dynamics — is
likewise out of stage one and named in the payload rather than omitted.

Sign convention: reactions are positive upward, against gravity acting along
the root frame's -Z. Positions are scalars on one horizontal span axis.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np

STANDARD_G = 9.81
"""m/s^2. The same standard-gravity figure preflight's cross-signal stage
scores an accelerometer's |g| against (``preflight/stages/cross_signal.py``);
kept literal here so this module imports no preflight stage."""


class Indeterminate(ValueError):
    """The support set cannot be answered from the contract alone."""


class ChainError(ValueError):
    """The kinematic chain is not a walkable open chain (a loop, or a zero-length axis)."""


def weight_n(mass_kg: float, g: float = STANDARD_G) -> float:
    """Weight in newtons of a mass in kilograms."""
    return mass_kg * g


def support_reactions(
    load_n: float, load_pos_m: float, support_pos_m: list[float]
) -> list[tuple[float, float]]:
    """Reaction (force N, moment N*m) at each support, in the order given.

    One support is a fixed interface: it carries the whole load plus the moment
    ``W * (load_pos - support)`` — case 1 in reaction form.

    Two supports are the lever rule — case 3, the two-bolt bracket. Each
    is a simple support, so each carries force and no moment; a support behind
    the load correctly comes out negative (it pulls down).

    Three or more supports on one rigid body are statically indeterminate: the
    split depends on stiffness the contract does not declare. Raises
    ``Indeterminate`` rather than returning a number.
    """
    count = len(support_pos_m)
    if count == 0:
        raise Indeterminate("no mount point is declared, so nothing reacts the load")
    if count == 1:
        return [(load_n, load_n * (load_pos_m - support_pos_m[0]))]
    if count == 2:
        near, far = support_pos_m
        if near == far:
            raise Indeterminate(
                "both mount points sit at the same place on the span axis, "
                "so the load split between them is undetermined"
            )
        reaction_far = load_n * (load_pos_m - near) / (far - near)
        return [(load_n - reaction_far, 0.0), (reaction_far, 0.0)]
    raise Indeterminate(
        f"{count} mount points on one rigid body is statically indeterminate; the load "
        "split depends on the structure's stiffness (Young's modulus and section), which "
        "the contract schema does not declare"
    )


# ---------------------------------------------------------------- gravity torque at a pose


class JointTorque(NamedTuple):
    joint: str
    torque_nm: float  # |static gravity torque| about the joint axis
    limit_nm: float | None  # joint effort limit, None when undeclared
    margin_nm: float | None  # limit - torque; negative means the joint cannot hold the pose
    payload_kg: float = 0.0  # declared link payload carried by this joint


def _rpy(r: float, p: float, y: float) -> np.ndarray:
    """URDF fixed-axis roll-pitch-yaw: Rz(y) Ry(p) Rx(r)."""
    cr, sr, cp, sp, cy, sy = (
        math.cos(r),
        math.sin(r),
        math.cos(p),
        math.sin(p),
        math.cos(y),
        math.sin(y),
    )
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _axis_angle(axis: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Rotations about a unit ``axis`` by each angle in ``q``: shape (P, 3, 3)."""
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    c, s = np.cos(q)[:, None, None], np.sin(q)[:, None, None]
    return c * np.eye(3) + s * cross + (1 - c) * np.outer(axis, axis)


class GravityPlan(NamedTuple):
    """The pose-independent half of the walk: built once, scored at many poses."""

    steps: list[tuple]  # (parent, child, joint, xyz, R_origin, unit axis | None, frame k | None)
    joints: list  # scored rotary joints, frame order
    links: list[str]  # the link each mass rides on
    local: np.ndarray  # (n, 3) mass points in their link frame
    kg: np.ndarray  # (n,)
    payload: np.ndarray  # (n,) declared payload kg, 0 for link masses
    carries: np.ndarray  # (K, n) 1 where mass i hangs off scored joint k


def gravity_plan(model, under: str | None = None) -> GravityPlan | None:
    """Walk order of the tree from its single root; ``under`` keeps only the root-to-joint
    path and that joint's subtree (other branches cannot change its subtree's torques).

    Raises ChainError for a closed chain or a zero-length axis. Returns None, never a guess,
    when the model has no single root, a rotary joint has no axis, no link has a positive mass, or a
    massive link has no CoM in its own frame. Massless links count as zero (they are
    flagged separately by check_zero_mass).
    """
    kids: dict[str, list] = {}
    for j in model.joints:
        kids.setdefault(j.parent, []).append(j)
    children = {j.child for j in model.joints}
    roots = [lk.name for lk in model.links if lk.name not in children]
    if (
        not model.joints
        or len(roots) != 1
        or not any(lk.mass and lk.mass.base() > 0 for lk in model.links)
    ):
        return None
    links = {lk.name: lk for lk in model.links}
    path: set[str] = set()  # links on the root -> `under` path
    if under is not None:
        parent_of = {j.child: j.parent for j in model.joints}
        name = next(j.parent for j in model.joints if j.name == under)
        while name not in path:
            path.add(name)
            name = parent_of.get(name, name)
    steps: list[tuple] = []
    joints: list = []
    masses: list[tuple] = []  # (link, local point, kg, payload kg, frames above)
    stack = [(roots[0], under is None, frozenset())]
    seen = {roots[0]}
    while stack:
        name, inside, above = stack.pop()
        link = links.get(name)
        if link is not None and link.mass is not None and link.mass.base() > 0:
            if link.com is None or link.frame not in (None, name):
                return None
            masses.append((name, link.com, link.mass.base(), 0.0, above))
        if link is not None and link.payload is not None:
            kg = link.payload.base()
            masses.append((name, (0.0, 0.0, 0.0), kg, kg, above))
        for joint in kids.get(name, []):
            below = inside or joint.name == under
            if not below and joint.child not in path:
                continue  # another branch
            if joint.child in seen:
                raise ChainError(
                    f"closed chain: joint '{joint.name}' returns to link '{joint.child}'"
                )
            seen.add(joint.child)
            axis, k, ab = None, None, above
            if joint.type in ("revolute", "continuous"):
                if joint.axis is None:
                    return None
                norm = math.sqrt(sum(a * a for a in joint.axis))
                if not norm:
                    raise ChainError(f"joint '{joint.name}' has a zero-length axis")
                axis = np.array(joint.axis, dtype=float) / norm
                if below:
                    k, ab = len(joints), above | {len(joints)}
                    joints.append(joint)
            # prismatic, planar and floating joints are held at their zero pose
            o = joint.origin
            xyz = np.array(o.xyz, dtype=float)
            steps.append((name, joint.child, joint, xyz, _rpy(*o.rpy), axis, k))
            stack.append((joint.child, below, ab))
    if not steps or (under is not None and not joints):
        return None  # the root does not reach the joints (a joint names a link not in the model)
    carries = np.array([[k in m[4] for m in masses] for k in range(len(joints))], dtype=float)
    return GravityPlan(
        steps,
        joints,
        [m[0] for m in masses],
        np.array([m[1] for m in masses], dtype=float).reshape(-1, 3),
        np.array([m[2] for m in masses], dtype=float),
        np.array([m[3] for m in masses], dtype=float),
        carries.reshape(len(joints), len(masses)),
    )


def plan_torques(plan: GravityPlan, names: list[str], angles, g: float = STANDARD_G) -> np.ndarray:
    """|Static gravity torque| (P, K) at each scored joint k for each pose row of ``angles``
    (P, len(names), radians; joints not named sit at 0), all poses at once.

    torque_j = axis_j . sum_i (r_i - p_j) x (m_i * g_vec), over every mass i in the subtree
    of joint j (CoM or payload point r_i, joint origin p_j, root frame, gravity along -Z).
    With M = sum m_i and S = sum m_i r_i that is g * (a_y (S_x - p_x M) - a_x (S_y - p_y M)).
    """
    angles = np.array(angles, dtype=float, ndmin=2).reshape(len(angles), len(names))
    col = {n: i for i, n in enumerate(names)}
    count, first = len(angles), plan.steps[0][0]
    rot, pos = {first: np.broadcast_to(np.eye(3), (count, 3, 3))}, {first: np.zeros((count, 3))}
    axes = np.zeros((count, len(plan.joints), 3))
    origins = np.zeros((count, len(plan.joints), 3))
    for parent, child, joint, xyz, r0, axis, k in plan.steps:
        p = pos[parent] + rot[parent] @ xyz
        r = rot[parent] @ r0
        if axis is not None:
            if k is not None:
                axes[:, k], origins[:, k] = r @ axis, p
            if joint.name in col:
                r = r @ _axis_angle(axis, angles[:, col[joint.name]])
        rot[child], pos[child] = r, p
    world = np.zeros((count, 0, 3))  # a subtree with no mass carries nothing
    if plan.links:
        world = np.stack([pos[n] + rot[n] @ q for n, q in zip(plan.links, plan.local)], axis=1)
    mass = plan.carries @ plan.kg  # (K,)
    moment = np.einsum("kn,pni->pki", plan.carries, plan.kg[None, :, None] * world)
    return np.abs(
        g
        * (
            axes[..., 1] * (moment[..., 0] - origins[..., 0] * mass)
            - axes[..., 0] * (moment[..., 1] - origins[..., 1] * mass)
        )
    )


def plan_rows(plan: GravityPlan, torques) -> list[JointTorque]:
    """One JointTorque per scored joint from a row of ``plan_torques``."""
    out = []
    for joint, t, kg in zip(plan.joints, torques, plan.carries @ plan.payload):
        t = float(t)
        limit = joint.limits.effort_limit if joint.limits is not None else None
        out.append(
            JointTorque(joint.name, t, limit, None if limit is None else limit - t, float(kg))
        )
    return out


def joint_gravity_torques(
    model,
    pose: dict[str, float] | None = None,
    g: float = STANDARD_G,
    under: str | None = None,
) -> list[JointTorque] | None:
    """Static gravity torque at each revolute joint of a kinematic tree, at ``pose``.

    One-shot form of ``gravity_plan`` + ``plan_torques``; None or ChainError as there.
    A link's declared ``payload`` is a point mass at its origin.
    """
    plan, pose = gravity_plan(model, under), pose or {}
    if plan is None:
        return None
    return plan_rows(plan, plan_torques(plan, list(pose), [list(pose.values())], g)[0])
