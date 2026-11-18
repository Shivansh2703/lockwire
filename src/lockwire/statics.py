"""Rigid-body statics for stage one - support reactions, determinate sets only.

``docs/design/e5_solver_choice.md`` chose **numpy-stiffness** by
measurement. This module is the *determinate subset* of that choice, and the
narrowing is deliberate, not a substitution:

* For a **statically determinate** support set the reactions follow from
  equilibrium alone and are **independent of the stiffness EI**. The solver study measured
  exactly that - its ``equilibrium-statics`` candidate matched the textbook
  reactions to 1.9e-16 - and rejected it only because it cannot produce
  *deflections* or answer a *redundant* set.
* For a **redundant** set the reactions *do* depend on EI. The contract schema
  is frozen and declares no Young's modulus, no section and no area on
  ``Link`` or ``Physical``, so there is nothing to solve a redundant set with.
  Those read NOT SCORED naming why, never a plausible-looking guess.

Consequently a stiffness solve has no input it could consume here, and a beam
deflection is not offered at all. What that document lists as unproven -
plates, shear-dominated stubby members, out-of-plane frames, dynamics - is
likewise out of stage one and named in the payload rather than omitted.

Sign convention: reactions are positive upward, against gravity acting along
the root frame's -Z. Positions are scalars on one horizontal span axis.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import math
from collections import Counter
from pathlib import Path
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


def primitive_inertia(kind: str, dims: tuple[float, ...], mass: float) -> np.ndarray:
    """3x3 inertia (kg*m^2) of a uniform solid primitive about its own centre of mass.

    Formulas: https://en.wikipedia.org/wiki/List_of_moments_of_inertia (solid cuboid,
    solid cylinder, solid sphere). ``dims``: box (x, y, z) edges; cylinder (radius, length)
    with its axis along z, the URDF convention; sphere (radius,).
    """
    if kind == "box":
        x, y, z = dims
        return np.diag([y * y + z * z, x * x + z * z, x * x + y * y]) * mass / 12
    if kind == "cylinder":
        r, h = dims
        transverse = mass * (3 * r * r + h * h) / 12
        return np.diag([transverse, transverse, mass * r * r / 2])
    if kind == "sphere":
        return np.eye(3) * 0.4 * mass * dims[0] ** 2
    raise ValueError(f"no inertia formula for a {kind}")


def merge_inertia(
    parts: list[tuple[float, tuple[float, float, float], np.ndarray]],
) -> tuple[float, np.ndarray, np.ndarray]:
    """(mass, COM, 3x3 inertia about that COM) of rigid parts given as (mass, COM, own-COM inertia).

    Parallel-axis theorem: I = I_c + m (|d|^2 E - d d^T), d the offset from the combined COM
    (https://en.wikipedia.org/wiki/Parallel_axis_theorem#Tensor_generalization).
    """
    total = sum(m for m, _, _ in parts)
    com = sum(m * np.asarray(c, float) for m, c, _ in parts) / total
    inertia = np.zeros((3, 3))
    for m, c, i_c in parts:
        d = np.asarray(c, float) - com
        inertia += i_c + m * (d @ d * np.eye(3) - np.outer(d, d))
    return total, com, inertia


def weight_n(mass_kg: float, g: float = STANDARD_G) -> float:
    """Weight in newtons of a mass in kilograms."""
    return mass_kg * g


def support_reactions(
    load_n: float, load_pos_m: float, support_pos_m: list[float]
) -> list[tuple[float, float]]:
    """Reaction (force N, moment N*m) at each support, in the order given.

    One support is a fixed interface: it carries the whole load plus the moment
    ``W * (load_pos - support)`` - case 1 in reaction form.

    Two supports are the lever rule - case 3, the two-bolt bracket. Each
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
    device: np.ndarray  # (n,) device kg, 0 for link and payload masses


# Keyed on the file's bytes, not its mtime: a same-size rewrite inside one clock tick keeps the mtime.
@functools.lru_cache(maxsize=16)
def _mjcf_ties(path: str, digest: bytes) -> frozenset[str]:
    from lockwire.mjcf import is_mjcf, looped_joints  # noqa: PLC0415  # mjcf -> check_sim -> checks -> statics

    try:
        return frozenset(looped_joints(Path(path))) if is_mjcf(Path(path)) else frozenset()
    except (OSError, SyntaxError, ValueError, TypeError):  # SyntaxError: xml ParseError
        return frozenset()


def tied_joints(model) -> frozenset[str]:
    """Joints that belong to a tied piece. The ONE place that decides "no per-joint number": an
    MJCF `<equality>` constraint ties bodies into a closed loop or coupled joints
    (mjcf.looped_joints) that an open-tree walk would score wrongly. The file is the one the lock
    names, read beside the lock the model was loaded from (``model._lockfile``, set by
    schema.load_contract and by a check that is given the lock's path), exactly as `check` finds
    it and never from the working directory. A file that is not there shows no tie."""
    from lockwire.checks import _model_on_disk, _urdf_path_candidate  # noqa: PLC0415  # checks -> statics

    named = _urdf_path_candidate(model)
    mjcf = named and named.lower().endswith((".mjcf", ".xml"))
    path = _model_on_disk(model._lockfile, named) if mjcf else None
    if not (path and path.is_file()):
        return frozenset()
    return _mjcf_ties(str(path), hashlib.blake2b(path.read_bytes(), digest_size=16).digest())


def gravity_plan(
    model, under: str | None = None, devices: dict[str, list[tuple]] | None = None,
    *, keep_tied: bool = False,
) -> GravityPlan | None:
    """Walk order of the tree from its single root; ``under`` keeps only the root-to-joint
    path and that joint's subtree (other branches cannot change its subtree's torques).
    ``devices`` maps a link name to the (xyz in that link's frame, kg) of devices mounted on it.

    Raises ChainError for a closed chain or a zero-length axis. Returns None, never a guess,
    when the model has no single root, a rotary joint has no axis, no link has a positive mass, or a
    massive link has no CoM in its own frame. Massless links count as zero (they are
    flagged separately by check_zero_mass).

    Joints of a tied piece (``tied_joints``) are left out of the walk, so every caller scores only
    the joints it can solve; ``keep_tied`` keeps them for a caller that needs the whole body mass.
    """
    tied = frozenset() if keep_tied else tied_joints(model)
    kids: dict[str, list] = {}
    for j in model.joints:
        if j.name in tied:
            continue
        kids.setdefault(j.parent, []).append(j)
    parents = Counter(j.child for j in model.joints)  # parent joints per link
    children = set(parents)
    # SDF may close a loop; URDF keeps one parent (ROS), so only an SDF refuses a looped link
    sdf = any(str(n).lower().endswith(".sdf") for n in (model.provenance.source, model.source))
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
    masses: list[tuple] = []  # (link, local point, kg, payload kg, frames above, device kg)
    stack = [(roots[0], under is None, frozenset())]
    seen = {roots[0]}
    while stack:
        name, inside, above = stack.pop()
        link = links.get(name)
        if link is not None and link.mass is not None and link.mass.base() > 0:
            if link.com is None or link.frame not in (None, name):
                return None
            masses.append((name, link.com, link.mass.base(), 0.0, above, 0.0))
        if link is not None and link.payload is not None:
            kg = link.payload.base()
            masses.append((name, (0.0, 0.0, 0.0), kg, kg, above, 0.0))
        masses += [(name, xyz, kg, 0.0, above, kg) for xyz, kg in (devices or {}).get(name, [])]
        for joint in kids.get(name, []):
            below = inside or joint.name == under
            if not below and joint.child not in path:
                continue  # another branch
            # an `under` walk meets a looped link once, so count its parent joints as well
            if joint.child in seen or (sdf and parents[joint.child] > 1):
                raise ChainError(
                    f"closed chain: joint '{joint.name}' returns to link '{joint.child}'"
                )
            seen.add(joint.child)
            axis, k, ab = None, None, above
            slide = joint.type == "prismatic"  # scored as a force along its axis, held at zero
            if joint.type in ("revolute", "continuous") or slide:
                norm = math.sqrt(sum(a * a for a in joint.axis)) if joint.axis else 0.0
                if not norm and not slide:
                    if joint.axis is None:
                        return None
                    raise ChainError(f"joint '{joint.name}' has a zero-length axis")
                axis = np.array(joint.axis, dtype=float) / norm if norm else None
                if axis is not None and below:
                    k, ab = len(joints), above | {len(joints)}
                    joints.append(joint)
            # planar and floating joints are held at their zero pose
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
        np.array([m[5] for m in masses], dtype=float),
    )


def _frames(
    plan: GravityPlan, names: list[str], angles
) -> tuple[dict, dict, np.ndarray, np.ndarray]:
    """Root-frame rotation (P, 3, 3) and position (P, 3) of every link, and each scored joint's
    axis and origin (P, K, 3), for each pose row of ``angles`` (joints not named sit at 0)."""
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
            if joint.name in col and joint.type != "prismatic":
                r = r @ _axis_angle(axis, angles[:, col[joint.name]])
        rot[child], pos[child] = r, p
    return rot, pos, axes, origins


def plan_torques(
    plan: GravityPlan, names: list[str], angles, g: float = STANDARD_G, signed: bool = False
) -> np.ndarray:
    """|Static gravity torque| (P, K) at each scored joint k for each pose row of ``angles``
    (P, len(names), radians; joints not named sit at 0), all poses at once. ``signed`` keeps the
    sign of rotary torques. A prismatic joint scores its weight along its axis, m * g * |axis_z| in N.

    torque_j = axis_j . sum_i (r_i - p_j) x (m_i * g_vec), over every mass i in the subtree
    of joint j (CoM or payload point r_i, joint origin p_j, root frame, gravity along -Z).
    With M = sum m_i and S = sum m_i r_i that is g * (a_y (S_x - p_x M) - a_x (S_y - p_y M)).
    """
    angles = np.array(angles, dtype=float, ndmin=2).reshape(len(angles), len(names))
    count = len(angles)
    rot, pos, axes, origins = _frames(plan, names, angles)
    world = np.zeros((count, 0, 3))  # a subtree with no mass carries nothing
    if plan.links:
        world = np.stack([pos[n] + rot[n] @ q for n, q in zip(plan.links, plan.local)], axis=1)
    mass = plan.carries @ plan.kg  # (K,)
    moment = np.einsum("kn,pni->pki", plan.carries, plan.kg[None, :, None] * world)
    lever = g * (
        axes[..., 1] * (moment[..., 0] - origins[..., 0] * mass)
        - axes[..., 0] * (moment[..., 1] - origins[..., 1] * mass)
    )
    slide = np.array([j.type == "prismatic" for j in plan.joints], dtype=bool)
    return np.where(slide, g * mass * np.abs(axes[..., 2]), lever if signed else np.abs(lever))


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


# ------------------------------------------------------- torque through a closed loop

_LOOP_STEP = 0.1  # rad of driven motion per continuation step away from the modelled pose
_LOOP_ITERS = 8  # Newton iterations per step; a pin gap under _LOOP_TOL metres counts as closed
_LOOP_TOL = 1e-9
# Locking (links lining up, the torque growing without bound): the smallest singular value of the
# pins' Jacobian with unit columns, 0 when two passive links pull the pin the same way and 1 when
# square to each other. 0.1 is about 8 degrees from lined up, wide enough that a 0.1 rad step
# does not jump over a locking pose; a pose closer than that gets no number.
_LOOP_SINGULAR = 0.1


class LoopPlan(NamedTuple):
    plan: GravityPlan  # the model as a tree: every loop cut at one passive pin
    # per independent loop: driven joints, passive joints, pins as (link, point, link, point)
    loops: list[tuple[list, list, list[tuple]]]
    idle: list[list[str]] = []  # joint names of each loop that has no driven joint


def loop_plan(model, cuts: dict[str, tuple[float, float, float]], devices=None) -> LoopPlan | None:
    """Cut each planar closed loop at the joints named in ``cuts`` (joint -> its pin as a point in
    its child link's frame) and sort the joints that turn a pin into driven (a positive effort
    limit) and passive (solved from the driven ones). None when the cut tree cannot be walked.

    ChainError, never a guess, for what is not solved: a driven or sliding pin, pin axes that are
    not parallel, driven joints that do not exactly fix the loop (fewer leave it free, more make
    the load split indeterminate), and anything that moves above or below the loop.
    """
    by = {j.name: j for j in model.joints}
    tree = model.model_copy(update={"joints": [j for j in model.joints if j.name not in cuts]})
    plan = gravity_plan(tree, devices=devices)
    if plan is None:
        return None
    up = {j.child: j for j in tree.joints}

    def chain(link: str) -> list:
        out = []
        while link in up:
            out.append(up[link])
            link = up[link].parent
        return out

    rot, _pos, axes, _ = _frames(plan, [], [[]])
    axis_of = {j.name: axes[0, k] for k, j in enumerate(plan.joints)}
    groups: list[tuple[dict, list]] = []  # (joints that turn a pin, by name; pins)
    for name, point in cuts.items():
        cut = by.get(name)
        if cut is None or cut.parent not in rot or cut.child not in rot:
            raise ChainError(f"closed chain: the lock does not hold the loop joint '{name}'")
        if cut.type not in ("revolute", "continuous") or not cut.axis or not any(cut.axis):
            raise ChainError(
                f"closed chain: the loop closes at joint '{name}', which is a {cut.type} joint, not a "
                "revolute pin; Lockwire solves a loop only when every joint in it is a revolute pin, "
                "so the loop's joints are not scored"
            )
        if cut.limits is not None and cut.limits.effort_limit is not None:
            raise ChainError(
                f"closed chain: the joint '{name}' that closes the loop has an effort limit, so it "
                "counts as a motor; Lockwire needs the closing pin free, so give it effort 0"
            )
        a, b = chain(cut.parent), chain(cut.child)
        shared = {j.name for j in a} & {j.name for j in b}
        turn = {j.name: j for j in (*a, *b) if j.name not in shared and j.type != "fixed"}
        above = [j.name for j in a if j.name in shared and j.type != "fixed"]
        if above or any(j.type not in ("revolute", "continuous") for j in turn.values()):
            bad = [j for j in turn.values() if j.type not in ("revolute", "continuous")]
            raise ChainError(
                f"closed chain: the loop hangs from a moving joint ('{above[0]}'); Lockwire solves a "
                f"loop only when its base is fixed, so '{above[0]}' and the loop are not scored; to "
                f"check the loop alone, make '{above[0]}' a fixed joint in a copy of the file"
                if above
                else f"closed chain: the loop through '{name}' contains a {bad[0].type} joint "
                f"('{bad[0].name}'); Lockwire solves a loop only when every joint in it is a "
                "revolute pin, so the loop's joints are not scored"
            )
        unit = np.array(cut.axis, dtype=float) / math.sqrt(sum(v * v for v in cut.axis))
        ref = rot[cut.parent][0] @ _rpy(*cut.origin.rpy) @ unit
        if any(abs(abs(float(axis_of[n] @ ref)) - 1) > 1e-6 for n in turn):
            raise ChainError(
                f"closed chain: the loop through '{name}' is not flat (its joint axes are not all "
                "parallel); Lockwire solves only flat loops, so the loop's joints are not scored"
            )
        pin = (cut.parent, np.array(cut.origin.xyz, dtype=float), cut.child, np.array(point))
        hit = [g for g in groups if g[0].keys() & turn.keys()]  # loops that share a joint
        for g in hit:
            groups.remove(g)
            turn.update(g[0])
        groups.append((turn, [pin, *(p for g in hit for p in g[1])]))
    loops, idle = [], []
    for turn, pins in groups:
        hang = [
            j.name
            for j in tree.joints
            if j.name not in turn and j.type != "fixed" and any(k.name in turn for k in chain(j.parent))
        ]  # fmt: skip
        if hang:
            raise ChainError(
                f"closed chain: joint '{hang[0]}' hangs from a link of the loop; Lockwire solves a "
                "loop only when nothing moves below it, so the loop is not scored; to check the loop "
                f"alone, make '{hang[0]}' a fixed joint in a copy of the file"
            )
        order = [j for j in tree.joints if j.name in turn]
        driven = [j for j in order if j.limits is not None and j.limits.effort_limit is not None]
        passive = [j for j in order if j not in driven]
        if driven and len(passive) != 2 * len(pins):  # a planar pin removes two freedoms
            free = len(turn) - 2 * len(pins)
            raise ChainError(
                f"closed chain: the loop needs exactly {free} motor-driven joints to hold its shape, "
                f"but {len(driven)} have an effort limit ({', '.join(j.name for j in driven)}); "
                "Lockwire cannot split the load, so the loop is not scored; give a passive pin an "
                "effort limit of 0"
            )
        if driven:
            loops.append((driven, passive, pins))
        else:
            idle.append([j.name for j in order])
    return LoopPlan(plan, loops, idle)


LOOP_WHY = (
    "cannot be assembled",
    "near lock",
    "reached only through a lock",
    "pin limit",
)  # what _why says


def loop_torques(lp: LoopPlan, loop: tuple, angles, g: float = STANDARD_G) -> np.ndarray:
    """Signed static gravity torque of ``loop_solve``; NaN where a pose got no number."""
    return loop_solve(lp, loop, angles, g)[0]


def loop_solve(
    lp: LoopPlan, loop: tuple, angles, g: float = STANDARD_G, explain: bool = True
) -> tuple[np.ndarray, list[str]]:
    """``loop_solve_q`` without the joint angles."""
    return loop_solve_q(lp, loop, angles, g, explain)[:2]


def loop_solve_q(
    lp: LoopPlan, loop: tuple, angles, g: float = STANDARD_G, explain: bool = True
) -> tuple[np.ndarray, list[str], np.ndarray, np.ndarray]:
    """(held, why, q, dx_du): ``q`` (P, driven + passive) are the closed loop's joint angles from
    the modelled pose, driven then passive, and ``dx_du`` (P, passive, driven) how the passive ones
    follow the driven ones; both meaningful only where the row of ``held`` is not NaN.

    ``held`` is the signed static gravity torque (P, driven) at the loop's driven joints for each row of
    driven angles ``angles`` (P, driven; radians from the modelled pose), the loop closed at each.

    Virtual work on the cut tree: with Q the tree's gravity torques and x(u) the passive angles
    the pins force for driven angles u, the driven joints hold Q_u + (dx/du)^T Q_x; an ideal
    passive pin holds nothing. x is followed from the modelled pose (where an SDF is assembled
    by construction) in steps of _LOOP_STEP by Newton on the pin gaps. A row is NaN where the
    loop does not close along that path, a passive joint leaves its limits, or the path comes
    within _LOOP_SINGULAR of a locking pose: no number there, where it would grow without bound.
    Also why each such row got none (LOOP_WHY), decided from the pose itself, not from where the
    path stopped (see ``_why``); "" for a row scored.
    """
    driven, passive, pins = loop
    names = [j.name for j in (*driven, *passive)]
    target = np.array(angles, dtype=float).reshape(-1, len(driven))
    nd, cols = len(driven), range(len(driven), len(names))

    def gap(q: np.ndarray) -> np.ndarray:  # (P, 3 * pins): each pin's two halves apart
        rot, pos, _, _ = _frames(lp.plan, names, q)
        return np.hstack([pos[a] + rot[a] @ pa - pos[b] - rot[b] @ pb for a, pa, b, pb in pins])

    def jac(q: np.ndarray, which) -> np.ndarray:  # d gap / d q[:, which], central differences
        h = 1e-6 * np.eye(len(names))
        return np.stack([(gap(q + h[c]) - gap(q - h[c])) / 2e-6 for c in which], axis=2)

    x = np.zeros((len(target), len(passive)))
    code = np.zeros(len(target), dtype=int)  # 0 = scored, else lost on the path

    def out(bad: np.ndarray, why: int) -> None:
        code[(code == 0) & bad] = why

    jx = jac(np.zeros((len(target), len(names))), cols)
    steps = max(1, math.ceil(float(np.abs(target).max(initial=0.0)) / _LOOP_STEP))
    for s in range(1, steps + 1):
        u = target * s / steps
        for _ in range(_LOOP_ITERS):
            q = np.hstack([u, x])
            x = x - np.einsum("pij,pj->pi", np.linalg.pinv(jac(q, cols)), gap(q))
            lost = ~np.isfinite(x).all(axis=1)
            x[lost] = 0.0  # a row that ran away stays out, harmlessly at zero
            out(lost, 1)
        out(~(np.linalg.norm(gap(np.hstack([u, x])), axis=1) < _LOOP_TOL), 1)
        # Checked at every step, not only the end: past a locking pose the path can come out on
        # the linkage's other assembly, closed and wrong. A row that comes near one, or goes
        # through one (the pins' Jacobian turns inside out), is out for good. Servo ranges are
        # per joint, so their corners often ask for a pose the linkage locks before reaching.
        was, jx = jx, jac(np.hstack([u, x]), cols)
        unit = jx / np.maximum(np.linalg.norm(jx, axis=1, keepdims=True), 1e-300)
        near = np.linalg.svd(unit, compute_uv=False)[:, -1] < _LOOP_SINGULAR
        out(near, 2)
        out(np.linalg.det(np.swapaxes(was, 1, 2) @ jx) <= 0, 3)
    for k, j in enumerate(passive):
        if j.limits is not None:
            lo = j.limits.lower if j.limits.lower is not None else -math.inf
            hi = j.limits.upper if j.limits.upper is not None else math.inf
            out(~((x[:, k] >= lo) & (x[:, k] <= hi)), 4)
    q = np.hstack([target, x])
    at = {j.name: k for k, j in enumerate(lp.plan.joints)}
    tq = plan_torques(lp.plan, names, q, g, signed=True)[:, [at[n] for n in names]]
    dx_du = -np.linalg.pinv(jx) @ jac(q, range(nd))  # (P, passive, driven)
    held = tq[:, :nd] + np.einsum("pxd,px->pd", dx_du, tq[:, nd:])
    lost = np.flatnonzero(code)
    why = [""] * len(target)
    for r, w in zip(lost, _why(lp, loop, target[lost], x[lost], gap, jac) if explain else ()):
        why[r] = w
    return np.where((code == 0)[:, None], held, np.nan), why, q, dx_du


_WHY_SEEDS = 60  # ponytail: at most this many starts per pose; a closure only on a branch none hits reads "cannot be assembled"


def _why(lp, loop, target, last, gap, jac) -> list[str]:
    """Why each unscored pose (target driven angles, ``last`` the path's final passive angles) got no
    number, from a direct closure attempt at the pose: Newton on the same pin gaps from the path's last
    passive angles, then a coarse spread over each passive range. Inside the passive limits and
    _LOOP_SINGULAR of locking: "near lock"; inside the limits and clear of it: "reached only through
    a lock" (the linkage can sit there, but not by moving from the modelled pose); closed only outside
    the limits: "pin limit"; no closure: "cannot be assembled"."""
    if not len(target):
        return []
    driven, passive, _ = loop
    nd, npas = len(driven), len(passive)
    rng = [
        (
            j.limits.lower if j.limits and j.limits.lower is not None else -math.pi,
            j.limits.upper if j.limits and j.limits.upper is not None else math.pi,
        )
        for j in passive
    ]
    k = max(2, min(12, int((_WHY_SEEDS - 1) ** (1 / npas))))
    spread = np.array(list(itertools.product(*[np.linspace(lo, hi, k) for lo, hi in rng])))
    spread = spread[: _WHY_SEEDS - 1]
    out = []
    for u, x0 in zip(target, last):
        x = np.vstack([x0, spread])
        t = np.tile(u, (len(x), 1))
        with np.errstate(all="ignore"):
            for _ in range(40):
                q = np.hstack([t, x])
                x = x - np.einsum(
                    "pij,pj->pi", np.linalg.pinv(jac(q, range(nd, nd + npas))), gap(q)
                )
            q = np.hstack([t, x])
            closed = np.linalg.norm(gap(q), axis=1) < _LOOP_TOL
            jx = jac(q, range(nd, nd + npas))
            unit = jx / np.maximum(np.linalg.norm(jx, axis=1, keepdims=True), 1e-300)
            lock = np.linalg.svd(unit, compute_uv=False)[:, -1]
        inside = np.all(
            [(x[:, i] >= lo) & (x[:, i] <= hi) for i, (lo, hi) in enumerate(rng)], axis=0
        )
        clear = closed & inside
        out.append(
            LOOP_WHY[0]
            if not closed.any()
            else LOOP_WHY[3]
            if not clear.any()
            else LOOP_WHY[2]
            if (lock[clear] >= _LOOP_SINGULAR).any()
            else LOOP_WHY[1]
        )
    return out


# ------------------------------------------------------------------ tip-over


def link_frames(model) -> dict[str, tuple[np.ndarray, np.ndarray]] | None:
    """Root-frame (position, rotation) of every link with all joints at zero; gravity is -Z.

    A joint's own angle only turns its child's descendants, so at zero each child sits at its joint
    origin. None, never a guess, for no single root, a closed chain or a link the root cannot reach.
    """
    kids: dict[str, list] = {}
    for j in model.joints:
        kids.setdefault(j.parent, []).append(j)
    children = {j.child for j in model.joints}
    roots = [lk.name for lk in model.links if lk.name not in children]
    if len(roots) != 1:
        return None
    out = {roots[0]: (np.zeros(3), np.eye(3))}
    stack = [roots[0]]
    while stack:
        name = stack.pop()
        pos, rot = out[name]
        for j in kids.get(name, []):
            if j.child in out:
                return None
            out[j.child] = (pos + rot @ np.array(j.origin.xyz), rot @ _rpy(*j.origin.rpy))
            stack.append(j.child)
    return out if len(out) == len(model.links) else None


def system_com(model, frames=None) -> np.ndarray | None:
    """Root-frame centre of mass of the link masses at the zero pose; None unless every massive
    link has a CoM in its own frame (device masses are not included: ponytail, add via mount_xyz)."""
    frames = frames or link_frames(model)
    if frames is None:
        return None
    total, moment = 0.0, np.zeros(3)
    for lk in model.links:
        if lk.mass is None or lk.mass.base() <= 0:
            continue
        if lk.com is None or lk.frame not in (None, lk.name):
            return None
        pos, rot = frames[lk.name]
        total += lk.mass.base()
        moment += lk.mass.base() * (pos + rot @ np.array(lk.com))
    return moment / total if total else None


def support_polygon(points) -> list[tuple[float, float]] | None:
    """Convex hull (counter-clockwise, Andrew's monotone chain) of ground-plane points.

    None when the contacts do not span an area (fewer than three non-collinear points): a segment
    has no inside, so a two-wheel balancer is not judged by a polygon it does not have.
    """
    pts = sorted({(round(float(x), 9), round(float(y), 9)) for x, y in points})
    if len(pts) < 3:
        return None

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    hull: list[tuple[float, float]] = []
    for chain in (pts, pts[::-1]):
        part: list[tuple[float, float]] = []
        for p in chain:
            while len(part) >= 2 and cross(part[-2], part[-1], p) <= 0:
                part.pop()
            part.append(p)
        hull += part[:-1]
    return hull if len(hull) >= 3 else None


def tip_distance(point, hull) -> float:
    """Signed distance in m from ``point`` to the nearest hull edge: positive inside, negative outside."""
    best = math.inf
    for a, b in zip(hull, hull[1:] + hull[:1]):
        ex, ey = b[0] - a[0], b[1] - a[1]
        best = min(best, (ex * (point[1] - a[1]) - ey * (point[0] - a[0])) / math.hypot(ex, ey))
    return best


# ------------------------------------------------- inverse dynamics


def _rnea_bodies(model, under: str | None = None):
    """(bodies, joints) for RNEA, bodies parent-first as (parent idx | -1, joint | None, xyz, R0,
    unit axis | None, mass, COM, 3x3 inertia about COM); None when an input is missing.

    Prismatic, planar and floating joints are held at zero like ``gravity_plan``. Link
    inertia is its principal moments rotated into the link frame; a link with a mass and no
    declared inertia is a point mass. A payload is a point mass at the link origin.
    """
    gp = gravity_plan(model, under)
    if gp is None:
        return None
    links = {lk.name: lk for lk in model.links}
    index = {gp.steps[0][0]: 0}
    bodies = [(-1, None, np.zeros(3), np.eye(3), None, *_link_mass(links.get(gp.steps[0][0])))]
    joints = []
    for parent, child, joint, xyz, r0, axis, k in gp.steps:
        index[child] = len(bodies)
        if axis is not None and joint.type != "prismatic":
            joints.append(joint)
        else:
            axis = None
        bodies.append((index[parent], joint, xyz, r0, axis, *_link_mass(links.get(child))))
    return bodies, joints


def _link_mass(link):
    parts = []
    if link is not None and link.mass is not None and link.mass.base() > 0:
        inertia = np.zeros((3, 3))
        if link.inertia is not None:
            axes = np.array(link.inertia.axes, dtype=float)
            inertia = axes.T @ np.diag(link.inertia.moments_kgm2) @ axes
        parts.append((link.mass.base(), tuple(link.com), inertia))
    if link is not None and link.payload is not None:
        parts.append((link.payload.base(), (0.0, 0.0, 0.0), np.zeros((3, 3))))
    if not parts:
        return 0.0, np.zeros(3), np.zeros((3, 3))
    return merge_inertia(parts)


def rnea(model, q, qd, qdd, g: float = STANDARD_G, under: str | None = None):
    """Joint torques (N*m, signed) a motion needs: recursive Newton-Euler inverse dynamics.

    ``q``, ``qd``, ``qdd`` are one value per rotary joint in ``gravity_plan(model, under).joints``
    order (rad, rad/s, rad/s^2). Returns ``[(joint, torque)]`` over that order, or None when an
    input is missing (as ``gravity_plan``). Gravity acts along the root frame's -Z; the angle is
    the right-handed rotation about the joint axis, so a point mass at (0, 0, -l) under a +Y axis
    gives m*l^2*qdd + m*g*l*sin(q). Luh-Walker recursion, body frames
    (https://en.wikipedia.org/wiki/Newton-Euler_equations; Featherstone, Rigid Body Dynamics
    Algorithms, alg. 5.1). Gravity enters as a root acceleration of +g along Z.
    """
    built = _rnea_bodies(model, under)
    if built is None:
        return None
    bodies, joints = built
    col = {j.name: i for i, j in enumerate(joints)}
    n = len(bodies)
    rot, w, al, ac_o, force, moment = ([None] * n for _ in range(6))
    w[0], al[0], ac_o[0] = np.zeros(3), np.zeros(3), np.array([0.0, 0.0, g])
    for i in range(1, n):
        p, joint, xyz, r0, axis, *_ = bodies[i]
        k = col.get(joint.name) if axis is not None else None
        rot[i] = r0 if k is None else r0 @ _axis_angle(axis, np.array([q[k]]))[0]
        rt = rot[i].T
        wp = rt @ w[p]
        w[i], al[i] = wp, rt @ al[p]
        if k is not None:
            w[i] = wp + axis * qd[k]
            al[i] = rt @ al[p] + axis * qdd[k] + np.cross(wp, axis * qd[k])
        r = xyz
        ac_o[i] = rt @ (ac_o[p] + np.cross(al[p], r) + np.cross(w[p], np.cross(w[p], r)))
    for i in range(n):
        *_, m, c, inertia = bodies[i]
        a_c = ac_o[i] + np.cross(al[i], c) + np.cross(w[i], np.cross(w[i], c))
        force[i] = m * a_c
        moment[i] = inertia @ al[i] + np.cross(w[i], inertia @ w[i]) + np.cross(c, force[i])
    tau = np.zeros(len(joints))
    for i in range(n - 1, 0, -1):
        p, joint, xyz, _, axis, *_ = bodies[i]
        f_p = rot[i] @ force[i]
        if axis is not None:
            tau[col[joint.name]] = axis @ moment[i]
        force[p] = force[p] + f_p
        moment[p] = moment[p] + rot[i] @ moment[i] + np.cross(xyz, f_p)
    return [(j.name, float(tau[i])) for i, j in enumerate(joints)]


def trapezoid_profile(distance: float, vmax: float, amax: float, per_phase: int = 20):
    """(t, s, sd, sdd) samples of the fastest rest-to-rest move over ``distance`` (rad).

    The same trapezoid-or-triangle as ``plan.trapezoid_time``; each phase is sampled at both
    its ends with its own acceleration, so the torque step at a phase edge is not skipped.
    """
    d = abs(distance)
    v = min(vmax, math.sqrt(d * amax))
    ta = v / amax
    tc = d / v - ta
    sign = math.copysign(1.0, distance)
    t0 = s0 = v0 = 0.0
    rows = []
    for dur, acc in ((ta, amax), (tc, 0.0), (ta, -amax)):
        for u in np.linspace(0.0, dur, per_phase):
            rows.append(
                (t0 + u, sign * (s0 + v0 * u + acc * u * u / 2), sign * (v0 + acc * u), sign * acc)
            )
        t0, s0, v0 = t0 + dur, s0 + v0 * dur + acc * dur * dur / 2, v0 + acc * dur
    return np.array(rows).T


def move_peak_torques(model, start: dict[str, float], target: dict[str, float], g=STANDARD_G):
    """Peak |torque| each joint needs over the minimum-time move ``start`` -> ``target``.

    Every joint follows the slowest joint's trapezoid scaled to its own distance, so none
    exceeds its speed or acceleration limit. One ``JointTorque`` per scored joint; None when
    the model cannot be walked or any moving joint lacks a declared speed and acceleration limit.
    """
    gp = gravity_plan(model)
    if gp is None:
        return None
    names = [j.name for j in gp.joints]
    delta = [target.get(n, start.get(n, 0.0)) - start.get(n, 0.0) for n in names]
    slow = None  # (time, |delta|, vmax, amax) of the joint that takes longest
    for j, d in zip(gp.joints, delta):
        lim = j.limits
        if not d:
            continue
        if lim is None or not lim.velocity or not lim.acceleration:
            return None
        t = abs(d) / lim.velocity + lim.velocity / lim.acceleration
        if abs(d) <= lim.velocity**2 / lim.acceleration:
            t = 2 * math.sqrt(abs(d) / lim.acceleration)
        if slow is None or t > slow[0]:
            slow = (t, abs(d), lim.velocity, lim.acceleration)
    if slow is None:
        return None
    _, s, sd, sdd = trapezoid_profile(slow[1], slow[2], slow[3])[:, :]
    s, sd, sdd = s / slow[1], sd / slow[1], sdd / slow[1]  # unit-distance shape
    peak = np.zeros(len(names))
    for a, b, c in zip(s, sd, sdd):
        tau = rnea(
            model,
            [start.get(n, 0.0) + d * a for n, d in zip(names, delta)],
            [d * b for d in delta],
            [d * c for d in delta],
            g,
        )
        peak = np.maximum(peak, np.abs([x for _, x in tau]))
    return plan_rows(gp, peak)
