"""One-at-a-time sensitivity (Free): which declared number moves a joint's torque margin most.

Information, not a finding: a plain function that emits nothing into the verdict. Each input a
gravity margin reads (link mass, link CoM, joint origin length, device mass) is moved by a
RELATIVE amount, minus then plus, and the worst-pose margin is recomputed with checks.gravity_loads.
The Pro corner sweep over declared tolerances.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checks import gravity_loads
from .schema import Contract

ENGINE_CHECKS: tuple = ()  # a readout; it emits no finding
_EPS = 1e-12  # float noise is not a driver
_KIND_ORDER = {"mass": 0, "com": 1, "length": 2, "device mass": 3}  # tie-break: equal swings


@dataclass(frozen=True)
class Driver:
    joint: str
    input: str  # e.g. "mass of hand"
    kind: str  # key of _KIND_ORDER
    minus_nm: float  # margin change when the input drops by rel
    plus_nm: float  # margin change when the input rises by rel

    @property
    def swing_nm(self) -> float:
        return max(abs(self.minus_nm), abs(self.plus_nm))


def _scale(x: float, step: float) -> float:
    return x * (1 + step)  # relative: 5% of THIS number, whatever its unit


def _edits(c: Contract):
    """(label, kind, apply(contract copy, step)) for every input a gravity margin can read."""
    if c.model is None:
        return
    for i, link in enumerate(c.model.links):
        if link.mass is not None and link.mass.value:
            yield f"mass of {link.name}", "mass", lambda k, s, i=i: _mass(k.model.links[i], s)
        if link.com is not None and any(link.com):
            yield f"CoM of {link.name}", "com", lambda k, s, i=i: _com(k.model.links[i], s)
    for i, j in enumerate(c.model.joints):
        if any(j.origin.xyz):
            yield f"origin of {j.name}", "length", lambda k, s, i=i: _origin(k.model.joints[i], s)
    for i, d in enumerate(c.devices):
        if d.physical is not None and d.physical.mass is not None and d.physical.mass.value:
            yield (
                f"mass of {d.id}",
                "device mass",
                lambda k, s, i=i: _mass(k.devices[i].physical, s),
            )


def _mass(holder, step: float) -> None:
    holder.mass = holder.mass.model_copy(update={"value": _scale(holder.mass.value, step)})


def _com(link, step: float) -> None:
    link.com = tuple(_scale(v, step) for v in link.com)


def _origin(joint, step: float) -> None:
    joint.origin = joint.origin.model_copy(
        update={"xyz": tuple(_scale(v, step) for v in joint.origin.xyz)}
    )


def _margins(c: Contract) -> dict[str, float]:
    loads = gravity_loads(c) if c.model is not None else None
    if not loads:
        return {}
    limits = {j.name: j.limits.effort_limit for j in c.model.joints if j.limits is not None}
    return {n: limits[n] - t for n, t in loads.items() if limits.get(n) is not None}


def sensitivity(c: Contract, rel: float = 0.05) -> dict[str, list[Driver]]:
    """joint -> drivers of its worst-pose gravity margin, biggest swing first.

    Joints without an effort limit, or a contract gravity cannot score, are absent (never a
    not-scored line). Every input is moved by +-rel of itself; `rel` is a fraction.
    """
    base = _margins(c)
    out: dict[str, list[Driver]] = {n: [] for n in base}
    for label, kind, apply in _edits(c):
        moved = []
        for step in (-rel, rel):
            k = c.model_copy(deep=True)
            apply(k, step)
            moved.append(_margins(k))
        for joint, m0 in base.items():
            lo, hi = (m.get(joint, m0) - m0 for m in moved)
            if max(abs(lo), abs(hi)) > _EPS:
                out[joint].append(Driver(joint, label, kind, lo, hi))
    for rows in out.values():
        rows.sort(key=lambda d: (-round(d.swing_nm, 9), _KIND_ORDER[d.kind]))
    return {n: rows for n, rows in out.items() if rows}
