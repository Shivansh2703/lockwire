"""Forward kinematics, the geometric Jacobian and the Plan reach check."""

from __future__ import annotations

import numpy as np

from typing import NamedTuple

from lockwire.report import Finding
from lockwire.statics import _rpy, link_frames

REACH_TOL_M = 1e-3


def _chain(model, tip: str | None) -> list | None:
    """Joints from the single root down to ``tip`` (default: the single leaf link), else None."""
    parent_of = {j.child: j for j in model.joints}
    names = {lk.name for lk in model.links}
    roots = names - set(parent_of)
    if tip is None:
        leaves = names - {j.parent for j in model.joints}
        tip = next(iter(leaves)) if len(leaves) == 1 else None
    if len(roots) != 1 or tip not in names:
        return None
    path: list = []
    while tip in parent_of and len(path) <= len(model.joints):
        path.append(parent_of[tip])
        tip = parent_of[tip].parent
    return path[::-1] if tip in roots else None


def _unit(joint) -> np.ndarray | None:
    if joint.type not in ("revolute", "continuous", "prismatic"):
        return None
    axis = np.array(joint.axis, dtype=float) if joint.axis else np.zeros(3)
    norm = np.linalg.norm(axis)
    return axis / norm if norm else None


def _rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(angle) * cross + (1 - np.cos(angle)) * cross @ cross


def _walk(model, q: dict[str, float], tip: str | None):
    """(tip 4x4 pose, [(joint, world axis, world origin)]) or None when not computable."""
    path = _chain(model, tip)
    if not path:
        return None
    pose, frames = np.eye(4), []
    for joint in path:
        local = np.eye(4)
        local[:3, :3] = _rpy(*joint.origin.rpy)
        local[:3, 3] = joint.origin.xyz
        pose = pose @ local  # parent frame -> joint frame, then the motion
        axis = _unit(joint)
        if joint.type in ("revolute", "continuous", "prismatic") and axis is None:
            return None  # a movable joint with no usable axis
        if axis is not None:
            frames.append((joint, pose[:3, :3] @ axis, pose[:3, 3].copy()))
            move = np.eye(4)
            value = q.get(joint.name, 0.0)
            if joint.type == "prismatic":
                move[:3, 3] = axis * value
            else:
                move[:3, :3] = _rotation(axis, value)
            pose = pose @ move
    return pose, frames


def fk(model, q: dict[str, float], tip: str | None = None) -> np.ndarray | None:
    """4x4 pose of ``tip`` in the root frame at joint positions ``q`` (rad / m, missing = 0).

    None when the model has no single root, the tip is not a link, or a movable joint has no axis.
    """
    walked = _walk(model, q, tip)
    return None if walked is None else walked[0]


def jacobian(
    model, q: dict[str, float], tip: str | None = None
) -> tuple[list[str], np.ndarray] | None:
    """(joint names, 6xN geometric Jacobian [linear; angular] of the tip point) or None."""
    walked = _walk(model, q, tip)
    if walked is None:
        return None
    pose, frames = walked
    cols = []
    for joint, axis, origin in frames:
        if joint.type == "prismatic":
            cols.append(np.concatenate([axis, np.zeros(3)]))
        else:
            cols.append(np.concatenate([np.cross(axis, pose[:3, 3] - origin), axis]))
    return [j.name for j, _, _ in frames], np.array(cols).reshape(len(cols), 6).T


def _miss(model, goal: np.ndarray, tip: str | None = None) -> float | None:
    """Closest the tip gets to ``goal`` (m) by damped least squares within joint limits."""
    first = jacobian(model, {}, tip)
    if first is None:
        return None
    names = first[0]
    joints = {j.name: j for j in model.joints}
    lo = np.array([_bound(joints[n], "lower") for n in names])
    hi = np.array([_bound(joints[n], "upper") for n in names])
    rng = np.random.default_rng(0)
    starts = [np.clip(np.zeros(len(names)), lo, hi)]
    starts += [rng.uniform(np.maximum(lo, -np.pi), np.minimum(hi, np.pi)) for _ in range(8)]
    best = np.inf
    for q in starts:
        for _ in range(150):
            pose = dict(zip(names, q))
            err = goal - fk(model, pose, tip)[:3, 3]
            jac = jacobian(model, pose, tip)[1][:3]
            step = jac.T @ np.linalg.solve(jac @ jac.T + 1e-4 * np.eye(3), err)
            q = np.clip(q + step, lo, hi)  # clamp every iteration: limits are the point
        best = min(best, float(np.linalg.norm(goal - fk(model, dict(zip(names, q)), tip)[:3, 3])))
    return best


def _bound(joint, side: str) -> float:
    value = getattr(joint.limits, side, None) if joint.limits else None
    if joint.type == "continuous" or value is None:
        return -np.inf if side == "lower" else np.inf
    return value


def reach_findings(model, plan) -> list:
    """One warning per Plan requirement whose Cartesian ``reach_m`` the arm cannot reach.

    Nothing when there is no model, no kinematic chain, or no reach target.
    """
    from lockwire.report import Finding

    out = []
    for req in plan.requirements if model is not None else []:
        if req.reach_m is None:
            continue
        miss = _miss(model, np.array(req.reach_m, dtype=float))
        if miss is not None and miss > REACH_TOL_M:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"plan/requirements/{req.id}/reach_m",
                    observed=f"target {req.id} is out of reach by {miss:.2f} m",
                    expected="a tip position the arm reaches inside its joint limits",
                    action="move the target closer, or change the arm or its joint limits",
                )
            )
    return out


class Step(NamedTuple):
    """Unit step of one joint: samples plus the three readouts (None when never reached)."""

    t: np.ndarray
    y: np.ndarray
    rise_s: float | None  # 10% -> 90% of the setpoint
    overshoot_pct: float
    settling_s: float | None  # last time outside the +-2% band
    inertia_kgm2: float
    damping_nms_rad: float


def link_inertia(model, joint_name: str) -> float | None:
    """Inertia (kg*m^2) of everything distal to ``joint_name`` about its axis, at the zero pose.

    Parallel-axis theorem on each link's mass at its CoM plus its principal inertia. None when the
    joint is not revolute, or a massive link has no CoM or declares a non-link frame.
    """
    joint = next((j for j in model.joints if j.name == joint_name), None)
    frames = link_frames(model)
    axis = _unit(joint) if joint is not None and joint.type in ("revolute", "continuous") else None
    if axis is None or frames is None:
        return None
    axis = frames[joint.child][1] @ axis
    kids: dict[str, list[str]] = {}
    for j in model.joints:
        kids.setdefault(j.parent, []).append(j.child)
    distal, stack = set(), [joint.child]
    while stack:
        name = stack.pop()
        distal.add(name)
        stack += kids.get(name, [])
    origin, total = frames[joint.child][0], 0.0
    for lk in model.links:
        if lk.name not in distal or lk.mass is None or lk.mass.base() <= 0:
            continue
        if lk.com is None or lk.frame not in (None, lk.name):
            return None
        pos, rot = frames[lk.name]
        lever = pos + rot @ np.array(lk.com) - origin
        lever -= axis * (axis @ lever)
        total += lk.mass.base() * float(lever @ lever)
        if lk.inertia is not None:  # rows of axes are the principal directions
            about = lk.inertia.axes @ (rot.T @ axis)
            total += float(np.dot(lk.inertia.moments_kgm2, about**2))
    return total


def step_response(
    inertia: float,
    kp: float,
    kd: float = 0.0,
    ki: float = 0.0,
    damping: float = 0.0,
    t_end: float | None = None,
    dt: float | None = None,
) -> Step:
    """Unit-step response of ``inertia*q'' = kp*e + ki*int(e) - kd*q' - damping*q'`` (RK4).

    Gains are joint-side (N*m/rad, N*m*s/rad); ``damping`` is the joint-side viscous friction.
    The default window is 100/wn seconds and the step is 1/400 of one natural period.
    """
    wn = np.sqrt(kp / inertia)
    dt = dt or 2 * np.pi / wn / 400
    t = np.arange(0.0, (t_end or 100 / wn) + dt / 2, dt)

    def f(x):
        err = 1.0 - x[0]
        return np.array([x[1], (kp * err + ki * x[2] - (kd + damping) * x[1]) / inertia, err])

    x, y = np.zeros(3), np.empty(len(t))
    for k in range(len(t)):
        y[k] = x[0]
        k1 = f(x)
        k2 = f(x + dt / 2 * k1)
        k3 = f(x + dt / 2 * k2)
        k4 = f(x + dt * k3)
        x = x + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
    cross = lambda lvl: np.nonzero(y >= lvl)[0][:1]  # noqa: E731
    lo, hi = cross(0.1), cross(0.9)
    outside = np.nonzero(abs(y - 1.0) > 0.02)[0]
    settle = (
        None
        if len(outside) and outside[-1] == len(t) - 1
        else t[outside[-1] + 1 if len(outside) else 0]
    )
    return Step(
        t,
        y,
        float(t[hi[0]] - t[lo[0]]) if len(lo) and len(hi) else None,
        float(max(y.max() - 1.0, 0.0) * 100),
        None if settle is None else float(settle),
        inertia,
        damping,
    )


def _plant(contract, joint_name: str):
    """(controller, actuator, reflected inertia, reflected damping) of one joint, else None.

    Reflected inertia is rotor*N^2 + the link inertia about the axis, viscous friction is
    reflected the same way. Needs a controller with kp > 0, a device driving the joint whose
    actuator has gear_ratio and rotor_inertia_kgm2, and a model inertia; any gap gives None.
    """
    ctl = next((c for c in contract.controllers or () if c.joint == joint_name), None)
    dev = next((d for d in contract.devices if d.drives_joint == joint_name), None)
    model = contract.device_model(dev.model) if dev is not None else None
    act = model.actuator if model is not None else None
    if not ctl or not ctl.kp or act is None or contract.model is None:
        return None
    if act.gear_ratio is None or act.rotor_inertia_kgm2 is None:
        return None
    link = link_inertia(contract.model, joint_name)
    if link is None:
        return None
    gear2 = act.gear_ratio**2
    return ctl, act, act.rotor_inertia_kgm2 * gear2 + link, (act.viscous_friction or 0.0) * gear2


def joint_step(contract, joint_name: str) -> Step | None:
    """Step response of one joint from its declared gains, motor and inertia; else None."""
    plant = _plant(contract, joint_name)
    if plant is None:
        return None
    ctl, _, inertia, damping = plant
    return step_response(inertia, ctl.kp, kd=ctl.kd or 0.0, ki=ctl.ki or 0.0, damping=damping)


class Margins(NamedTuple):
    """Open-loop readouts of one joint; ``None`` where the loop never crosses."""

    crossover_rad_s: float | None  # last |L| = 1 going down
    phase_margin_deg: float | None
    gain_margin_db: float | None  # at the first -180 degree crossing
    loop_rate_hz: float | None  # None -> no sampling delay was applied


def margins(contract, joint_name: str) -> Margins | None:
    """Margins of L(jw) = (kp + ki/jw + kd*jw) / (J*(jw)^2 + b*jw) * exp(-jw*Ts/2); else None.

    The ZOH is approximated by half a sample of delay, Ts = 1/loop_rate_hz; without a loop
    rate no delay is applied. Kd acts on the measurement, which leaves the loop transfer unchanged.
    """
    plant = _plant(contract, joint_name)
    if plant is None:
        return None
    ctl, _, inertia, damping = plant
    ts = 1.0 / ctl.loop_rate_hz if ctl.loop_rate_hz else 0.0
    wn = np.sqrt(ctl.kp / inertia)
    w = wn * np.logspace(-2, 3, 20000)
    c = ctl.kp + 1j * (ctl.kd or 0.0) * w - 1j * (ctl.ki or 0.0) / w
    den = -inertia * w**2 + 1j * damping * w
    # continuous phase: C in (-90, 90), plant in (-180, -90), delay linear in w
    phase = np.degrees(np.angle(c) - np.angle(den) - w * ts / 2)
    mag = np.abs(c / den)
    down = np.nonzero((mag[:-1] >= 1) & (mag[1:] < 1))[0]
    wc = pm = gm = None
    if len(down):
        k = down[-1]
        f = np.log(mag[k] / 1) / np.log(mag[k] / mag[k + 1])  # log-log interpolation
        wc = float(w[k] * (w[k + 1] / w[k]) ** f)
        pm = float(180.0 + phase[k] + f * (phase[k + 1] - phase[k]))
    cross = np.nonzero((phase[:-1] > -180) & (phase[1:] <= -180))[0]
    if len(cross):
        k = cross[0]
        f = (phase[k] + 180) / (phase[k] - phase[k + 1])
        gm = float(-20 * np.log10(mag[k] * (mag[k + 1] / mag[k]) ** f))
    return Margins(wc, pm, gm, ctl.loop_rate_hz)


def peak_effort(contract, joint_name: str) -> tuple[float, float] | None:
    """(peak joint torque of the unit step, limit) in N*m; None without a cited current.

    Torque is kp*e + ki*int(e) - kd*q'. The limit is torque_constant * stall_current_a * N.
    """
    plant = _plant(contract, joint_name)
    if plant is None:
        return None
    ctl, act, _, _ = plant
    if not act.torque_constant or not act.stall_current_a:
        return None
    step = joint_step(contract, joint_name)
    e = 1.0 - step.y
    u = ctl.kp * e + (ctl.ki or 0.0) * np.cumsum(e) * (step.t[1] - step.t[0])
    u = u - (ctl.kd or 0.0) * np.gradient(step.y, step.t, edge_order=2)
    return float(np.abs(u).max()), act.torque_constant * act.stall_current_a * act.gear_ratio


# Both thresholds below are rules of thumb without a traced source: advice only.
LOOP_RATE_DIVISOR = 10  # crossover above loop_rate/10 (digital-control rule of thumb)
MIN_PHASE_MARGIN_DEG = 30.0


def margin_findings(contract, *, contract_path=None) -> list[Finding]:
    """Advice findings on bandwidth vs loop rate, phase margin and effort; silent on gaps."""
    out = []
    for ctl in contract.controllers or ():
        m = margins(contract, ctl.joint)
        if m is None:
            continue
        where = f"controllers/{ctl.joint}"
        if m.loop_rate_hz and m.crossover_rad_s:
            fc = m.crossover_rad_s / (2 * np.pi)
            if fc > m.loop_rate_hz / LOOP_RATE_DIVISOR:
                out.append(
                    Finding(
                        severity="warning",
                        quantity=f"{where}/loop_rate_hz",
                        observed=f"crossover {fc:.3g} Hz at a {m.loop_rate_hz:g} Hz loop",
                        expected=f"crossover below loop_rate/{LOOP_RATE_DIVISOR} (a common rule of thumb for sampled control loops; not yet tied to a source)",
                        action=f"lower {ctl.joint}'s gains or raise its loop rate",
                        advice_unsourced=True,
                    )
                )
        if m.phase_margin_deg is not None and m.phase_margin_deg < MIN_PHASE_MARGIN_DEG:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"{where}/phase_margin",
                    observed=f"phase margin {m.phase_margin_deg:.0f} deg",
                    expected=f"at least {MIN_PHASE_MARGIN_DEG:g} deg (a common design rule of thumb; not yet tied to a source)",
                    action=f"add damping (kd) or lower kp on {ctl.joint}",
                    advice_unsourced=True,
                )
            )
        effort = peak_effort(contract, ctl.joint)
        if effort is not None and effort[0] > effort[1]:
            out.append(
                Finding(
                    severity="warning",
                    quantity=f"{where}/effort",
                    observed=f"a 1 rad step asks {effort[0]:.3g} N*m",
                    expected=f"within {effort[1]:.3g} N*m (motor torque constant x its cited stall current x gear ratio)",
                    action=f"lower {ctl.joint}'s kp, or use a stronger driver",
                    advice_unsourced=True,
                )
            )
    return out


ENGINE_CHECKS: tuple = (margin_findings,)
