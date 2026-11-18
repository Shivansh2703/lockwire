"""The standing check: the robot on its feet, each leg swept over the poses it can reach.

Feet are collisions with the word foot in their name (``is_foot_name``); a robot with none
uses the links at the ends of its legs named foot or toe (``is_foot_link_name``). Static,
level ground, vertical foot forces only.
"""

import itertools
import math
import re
import xml.etree.ElementTree as ET
from typing import NamedTuple

import numpy as np

from lockwire import statics

LINE_TOL_M = 0.010  # ponytail: a fixed tolerance, not derived from the foot size
FOOT_RULE = (
    "a collision is a foot when one word of its name is foot (left_foot, foot_fl, leftFoot, foot2); "
    "with none, a link at the end of a leg is a foot when one word of its name is foot or toe "
    "(FL_foot, LF_FOOT, left_toe_link)"
)
NOT_COVERED = (
    "not covered: sideways foot forces, friction, slopes, motion and impacts, legs moving together"
)


class Foot(NamedTuple):
    label: str
    link: str
    kind: str  # sphere, box or cylinder
    dims: tuple[float, ...]  # radius / size / (radius, length)
    xyz: np.ndarray  # the shape's origin in its link frame
    rot: np.ndarray  # and its orientation
    by_link: bool = False  # found by its link's name: of several shapes, the lowest is the foot


def _nums(raw: str | None) -> list[float]:
    try:
        return [float(x) for x in (raw or "").split()]
    except ValueError:
        return []


def _words(name: str) -> list[str]:
    """Words split at every character that is not a letter or digit and at lower-to-upper
    camel-case steps, lower case."""
    return re.split(r"[^A-Za-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name).lower())


def is_foot_name(name: str) -> bool:
    """True when a word of the name is ``foot`` or ``foot`` and digits."""
    return any(re.fullmatch(r"foot\d*", w) for w in _words(name))


def is_foot_link_name(name: str) -> bool:
    """True when a word of the link name is ``foot`` or ``toe``, alone or with digits
    (FL_foot, LF_FOOT, left_toe_link; not base_footprint, football or tiptoe)."""
    return any(re.fullmatch(r"(?:foot|toe)\d*", w) for w in _words(name))


def _shape(el: ET.Element, label: str, link: str, by_link: bool = False) -> Foot | None:
    """The collision ``el`` as a Foot, or None for a mesh or a shape with no readable size."""
    geometry = el.find("geometry")
    shape = geometry[0] if geometry is not None and len(geometry) else None
    o = el.find("origin")
    xyz, rpy = (
        _nums(o.get("xyz")) if o is not None else [],
        _nums(o.get("rpy")) if o is not None else [],
    )
    xyz, rpy = (xyz + [0.0] * 3)[:3], (rpy + [0.0] * 3)[:3]
    if shape is None or shape.tag == "mesh":
        return None
    r, length = ((_nums(shape.get(k)) or [0.0])[0] for k in ("radius", "length"))
    dims = {
        "sphere": (r,),
        "cylinder": (r, length),
        "box": tuple(_nums(shape.get("size"))),
    }.get(shape.tag)
    if dims is None or not any(dims) or (shape.tag == "box" and len(dims) != 3):
        return None
    return Foot(label, link, shape.tag, dims, np.array(xyz), statics._rpy(*rpy), by_link)


def _link_feet(root: ET.Element) -> tuple[list[Foot], list[str]]:
    """(feet, unsupported) by link name, for a model with no collision named foot.

    A foot is the end of a leg the robot stands on: links named foot or toe
    (``is_foot_link_name``) with no moving joint below them, and links joined to each other by
    fixed joints are one foot (a toe fixed to its foot is the same rigid body, one foot per
    leg). The robot must stand on them: at least two feet, all hanging from the root body (the
    feet's common ancestor is the root, or a link fixed to it, or under a floating joint; not a
    root named world fixed to the robot), each through at least two moving joints. So two
    fingers of a gripper or two tips of an arm (their common ancestor is a hand or a wrist), a
    fixed-base robot, and a part bolted to the body are not feet. Each sphere, box or cylinder
    collision of a foot is a candidate (``analyse`` keeps the lowest); a foot with none is
    unsupported. A model with no collision at all (or a view that carries none, as for MJCF and
    Webots) has nothing to stand on and gets no feet."""
    if root.find("link/collision") is None:
        return [], []
    links = {lk.get("name", ""): lk for lk in root.findall("link")}
    below: dict[str, list[tuple[str, str]]] = {}
    above: dict[str, tuple[str, str]] = {}
    for j in root.findall("joint"):
        p, c = j.find("parent"), j.find("child")
        if p is not None and c is not None:
            below.setdefault(p.get("link", ""), []).append((j.get("type", ""), c.get("link", "")))
            above[c.get("link", "")] = (j.get("type", ""), p.get("link", ""))
    roots = [n for n in links if n not in above]
    if len(roots) != 1:
        return [], []

    def moves_below(link: str, seen: frozenset = frozenset()) -> bool:
        return any(
            t != "fixed" or (c not in seen and moves_below(c, seen | {link}))
            for t, c in below.get(link, [])
        )

    def chain(link: str) -> list[tuple[str, str]]:
        """[(link, type of the joint above it)] from ``link`` up to the root."""
        out, seen = [], set()
        while link not in seen:
            seen.add(link)
            t, up = above.get(link, ("", ""))
            out.append((link, t))
            if not up:
                break
            link = up
        return out

    def rigid_top(link: str) -> str:
        return next(n for n, t in chain(link) if t != "fixed")

    groups: dict[str, list[str]] = {}  # one rigid body, one foot
    for n in links:
        if is_foot_link_name(n) and not moves_below(n):
            groups.setdefault(rigid_top(n), []).append(n)
    if len(groups) < 2:
        return [], []
    common = set.intersection(*({n for n, _ in chain(top)} for top in groups))
    trunk = next(n for n, _ in chain(next(iter(groups))) if n in common)
    to_root = chain(trunk)
    if any(t not in ("fixed", "floating", "") for _, t in to_root):
        return [], []  # the feet meet on a moving part: a hand, a wrist, not the body
    if roots[0].lower() == "world" and all(t != "floating" for _, t in to_root):
        return [], []  # bolted to the world: a fixed-base robot does not stand
    for top in groups:
        path = chain(top)
        legs = path[: [n for n, _ in path].index(trunk)]
        if sum(t not in ("fixed", "") for _, t in legs) < 2:
            return [], []
    feet, skipped = [], []
    for top, names in groups.items():
        label = min(names, key=lambda n: len(chain(n)))  # the foot link nearest the body
        got = [
            f
            for n in names
            for el in links[n].findall("collision")
            if (f := _shape(el, label, n, True))
        ]
        feet += got
        if not got:
            skipped.append(label)
    return feet, skipped


def find_feet(root: ET.Element) -> tuple[list[Foot], list[str]]:
    """(feet, unsupported): each ``<collision>`` with a foot name (``is_foot_name``) as a box,
    sphere or cylinder; ``unsupported`` names the mesh ones and those with no readable size.
    A model with no collision named foot falls back to feet by link name (``_link_feet``)."""
    feet, skipped = [], []
    for link in root.findall("link"):
        found = [
            el
            for el in link.findall("collision")
            if is_foot_name(el.get("name") or "") and el.find("geometry") is not None
        ]
        for el in found:
            label = link.get("name", "?") + ("" if len(found) == 1 else "/" + el.get("name", ""))
            foot = _shape(el, label, link.get("name", "?"))
            if foot is None:
                skipped.append(label)
            else:
                feet.append(foot)
    return (feet, skipped) if feet or skipped else _link_feet(root)


def lowest_point(foot: Foot, rot: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """World point of the foot's shape with the least z, for link frames ``rot`` (P, 3, 3) and
    ``pos`` (P, 3): a sphere's centre less its radius, a box's lowest corner, a cylinder's lowest
    rim point (the middle of the bottom face when it stands flat)."""
    r = rot @ foot.rot  # shape orientation in the world
    c = pos + rot @ foot.xyz
    down = np.array([0.0, 0.0, -1.0])
    if foot.kind == "sphere":
        return c + down * foot.dims[0]
    if foot.kind == "box":
        corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]) * (
            np.array(foot.dims) / 2
        )
        world = c[:, None, :] + np.einsum("pij,kj->pki", r, corners)
        return world[np.arange(len(c)), world[:, :, 2].argmin(axis=1)]
    radius, length = foot.dims
    axis = r[:, :, 2]  # the cylinder's own z, in the world
    flat = axis[:, 2:3]  # its z component: sign says which end is lower
    side = down + flat * axis  # straight down, with its part along the axis taken out
    norm = np.linalg.norm(side, axis=1, keepdims=True)
    rim = np.where(norm > 1e-9, side / np.maximum(norm, 1e-300), 0.0) * radius
    return c - np.where(flat >= 0, 1.0, -1.0) * axis * (length / 2) + rim


def split_forces(points, com, weight: float, tol=None) -> tuple[np.ndarray | None, str]:
    """(vertical foot forces N, note): the forces under feet at ground ``points`` that balance
    ``weight`` and its tipping moments about both horizontal axes with the least sum of squares
    (a line of two feet: by the lever rule). None, with why, when a foot would have to pull
    (the centre of mass is outside the feet) or the feet give no area or line to stand on.
    With ``tol`` (m), feet in a line whose centre of mass is farther than that beside the line
    tip."""
    p = np.array(points, dtype=float)[:, :2]
    x, y = p[:, 0] - com[0], p[:, 1] - com[1]  # moments about the centre of mass: they vanish
    if len(p) > 1 and (len(p) == 2 or statics.support_polygon(p) is None):
        i, j = max(
            itertools.combinations(range(len(p)), 2),
            key=lambda k: np.linalg.norm(p[k[0]] - p[k[1]]),
        )
        d = p[j] - p[i]
        t = float((np.array(com[:2]) - p[i]) @ d / (d @ d))
        off = abs(d[0] * (com[1] - p[i][1]) - d[1] * (com[0] - p[i][0])) / math.hypot(*d)
        if tol is not None and off > tol:
            return (
                None,
                f"tips: the centre of mass is {off * 1000:.0f} mm beside the line through the feet",
            )
        if len(p) > 2:
            return None, f"{len(p)} feet in a line: there is no area to stand on"
        f, note = (
            np.array([1 - t, t]) * weight,
            f"a line, not an area: the centre of mass is {off * 1000:.0f} mm beside it",
        )
    elif len(p) < 3:
        return None, "one foot: there is no area to stand on"
    else:
        a = np.vstack([np.ones(len(p)), x, y])
        f, note = a.T @ np.linalg.solve(a @ a.T, np.array([weight, 0.0, 0.0])), ""
    if f.min() < -1e-9 * weight:
        return None, "tips: the centre of mass is outside the feet, so a foot would have to pull"
    return f, note


def load_cases(labels: list[str], points, com, weight: float) -> tuple[list[dict], float | None]:
    """(cases, balance margin m): the load cases of ``stance`` (a): all feet down, (b) each
    diagonal pair of a four-footed robot, (c) one leg, each as {case, label, feet, forces (N, in
    ``feet`` order, None when it tips), note}. The margin is the distance from the centre of mass
    to the nearest edge of the support polygon of (a), negative outside, None without an area.
    Front/hind and left/right are the feet's x and y against the centre of mass (x forward)."""
    pts = np.array(points, dtype=float)
    poly = statics.support_polygon(pts[:, :2])
    margin = statics.tip_distance(com[:2], poly) if poly else None

    def case(name: str, label: str, which: list[int], pair: str = "") -> dict:
        f, note = split_forces(pts[which], com, weight, LINE_TOL_M if name == "all_feet" else None)
        return {
            "case": name,
            "label": label + pair,
            "feet": [labels[i] for i in which],
            "forces": None if f is None else [float(v) for v in f],
            "note": note,
        }

    out = [case("all_feet", "all feet down", list(range(len(labels))))]
    quad = {(bool(p[0] >= com[0]), bool(p[1] >= com[1])): i for i, p in enumerate(pts)}
    if len(labels) == 4 and len(quad) == 4:  # one foot in each of front/hind x left/right
        for name, (a, b), word in (
            ("diag_fl_hr", ((True, True), (False, False)), "front-left with hind-right"),
            ("diag_fr_hl", ((True, False), (False, True)), "front-right with hind-left"),
        ):
            out.append(case(name, "diagonal pair: ", [quad[a], quad[b]], f"{word}, a line"))
    out.append(
        {
            "case": "one_leg",
            "label": "one leg carrying everything (an upper bound)",
            "feet": labels,
            "forces": [weight] * len(labels),
            "note": "an upper bound: each foot alone takes the whole weight",
        }
    )
    return out, margin


_H = 1e-4  # rad, the central difference for a foot's lever arm
_GRID_MAX = 2500  # poses per leg on the coarse grid (7, else 5, else 3 points per joint)
_ROTARY = ("revolute", "continuous")
NO_FOOT = (
    f'no foot was found; name a collision on each foot link, <collision name="foot"> with a '
    f"sphere, box or cylinder, or name each foot link with the word foot or toe ({FOOT_RULE})"
)
FIX_FOOT = (
    "give it a sphere, box or cylinder collision, named foot or on a link named foot or toe, "
    "to score standing"
)


def empty_stance(reason: str) -> dict:
    return {
        "feet": [],
        "com_m": None,
        "weight_n": None,
        "cases": [],
        "balance_margin_m": None,
        "reason": reason,
    }


def _reach(plan, lp, foot: Foot) -> tuple[tuple | None, list]:
    """(loop, joints) of a foot's leg: the closed loop it sits in with its driven joints, or the
    rotary joints from the root down to it."""
    up = {s[1]: (s[0], s[2]) for s in plan.steps}
    chain, link = [], foot.link
    while link in up:
        link, joint = up[link]
        chain.append(joint)
    names = {j.name for j in chain}
    loop = next(
        (x for x in (lp.loops if lp else []) if names & {j.name for j in (*x[0], *x[1])}), None
    )
    return loop, list(loop[0]) if loop else [
        j for j in reversed(chain) if j.type in _ROTARY and j.axis
    ]


def _leg(plan, lp, foot, loop, vary, feet_of, refine, cases) -> tuple[list[dict], dict]:
    """(rows, facts) for one foot's leg swept over its poses, the other legs at the file pose.

    Joint torque = the leg's gravity term (signed, base held) + F * dz/dq, F the foot's vertical
    force of the case and z the foot's lowest point. Sign: a ground force F (up) at the foot does
    virtual work F * dz on a joint move dq, so it adds F * dz/dq to the generalised force that
    gravity gives (plan_torques signed). Hand case: hip at the origin, axis +y, shin hanging to
    the foot at (x, 0, -L): z(q) = -x sin q - L cos q, dz/dq = -x at q = 0, and the force's
    moment (r x F) . y = -x F: the same. Rows use the magnitude of the sum, as the holding rows do.
    """
    facts: dict = {"unlimited": [], "shared": [], "swept": ""}
    scored = []
    for j in vary:
        if j.limits is None or j.limits.effort_limit is None:
            facts["unlimited"].append(j.name)
        elif feet_of[j.name] > 1:
            facts["shared"].append(j.name)
        else:
            scored.append(j)
    if not scored:
        return [], facts
    nv, ns = len(vary), len(scored)
    sidx = [vary.index(j) for j in scored]
    mine = [
        (c, c["forces"][c["feet"].index(foot.label)])
        for c in cases
        if c["forces"] and foot.label in c["feet"]
    ]
    all_names = [j.name for j in (*loop[0], *loop[1])] if loop else [j.name for j in vary]
    plan_at = {j.name: k for k, j in enumerate(plan.joints)}
    seen: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []

    def evaluate(poses: np.ndarray) -> np.ndarray:
        """(P, cases x scored joints): |torque|, NaN where the pose got no number.

        The lever arm is dz/du by virtual work: the foot's z changes with every joint angle q
        (central differences of the frames), and in a loop the passive ones follow the driven
        ones by the solver's own dx/du."""
        p = len(poses)
        if loop:
            held, _, q, dx = statics.loop_solve_q(lp, loop, poses, explain=False)
            grav, frames = held[:, sidx], lp.plan
        else:
            q, dx, frames = poses, None, plan
            grav = statics.plan_torques(plan, all_names, poses, signed=True)[
                :, [plan_at[j.name] for j in scored]
            ]
        nq, eye = q.shape[1], _H * np.eye(q.shape[1])
        rot, pos = statics._frames(
            frames, all_names, np.vstack([q, *(q + e for e in eye), *(q - e for e in eye)])
        )[:2]
        low = lowest_point(foot, rot[foot.link], pos[foot.link])
        dz = (low[p : p + nq * p, 2] - low[p + nq * p :, 2]).reshape(nq, p).T / (2 * _H)
        lever = dz[:, :nv] if dx is None else dz[:, :nv] + np.einsum("px,pxd->pd", dz[:, nv:], dx)
        lever = lever[:, sidx]
        cols = np.hstack([np.abs(grav + f * lever) for _, f in mine])
        seen.append((poses, low[:p], cols))
        return cols

    def worst_case(poses: np.ndarray) -> np.ndarray:
        """(P, scored joints): the largest of the cases, which is what the refinement chases."""
        return evaluate(poses).reshape(len(poses), len(mine), ns).max(axis=1)

    sides = []
    for j in vary:  # ponytail: an unlimited joint sweeps +/-180 deg
        lo = j.limits.lower if j.limits and j.limits.lower is not None else -math.pi
        hi = j.limits.upper if j.limits and j.limits.upper is not None else math.pi
        sides.append((lo, hi))
    n = next((g for g in (7, 5, 3) if g**nv <= _GRID_MAX), 0)
    at_file = evaluate(np.zeros((1, nv)))[0]
    facts["swept"] = (
        f"{n} points per joint" if n else f"not swept: {nv} joints are too many to grid"
    )
    if n:
        axes = [np.linspace(lo, hi, n) for lo, hi in sides]
        poses = np.array(list(itertools.product(*axes)))
        # ponytail: the refinement chases each joint's worst case, not every case's own peak
        refined = refine(lp, loop, axes, poses, worst_case(poses), worst_case)[2]
        facts["swept"] += {
            "yes": ", refined",
            "no": ", coarse only",
            "capped": ", refined to its cap",
        }[refined]
    allp, allh = np.vstack([x[0] for x in seen]), np.vstack([x[2] for x in seen])
    with np.errstate(invalid="ignore"):
        i = np.where(np.isfinite(allh), allh, -np.inf).argmax(axis=0)
    peak, where = allh[i, np.arange(allh.shape[1])], allp[i]
    rows = []
    for ci, (c, f) in enumerate(mine):
        for ji, j in enumerate(scored):
            k, lim = ci * ns + ji, j.limits.effort_limit
            if not math.isfinite(peak[k]):
                continue  # no pose of this leg got a number
            rows.append(
                {
                    "joint": j.name,
                    "case": c["case"],
                    "feet": c["feet"],
                    "leg": foot.label,
                    "foot_force_n": float(f),
                    "at_file_nm": float(at_file[k]) if math.isfinite(at_file[k]) else None,
                    "peak_nm": float(peak[k]),
                    "peak_pose_deg": {
                        v.name: math.degrees(float(a)) for v, a in zip(vary, where[k])
                    },
                    "limit_nm": float(lim),
                    "share": float(peak[k] / lim) if lim > 0 else math.inf,
                    "passed": bool(peak[k] <= lim),
                }
            )
    pts = np.vstack([low[np.isfinite(low).all(axis=1)] for _, low, _ in seen])
    facts["height_m"] = [float(pts[:, 2].min()), float(pts[:, 2].max())]
    facts["fore_aft_m"] = float(pts[:, 0].max() - pts[:, 0].min())
    return rows, facts


def _foot_problem(out: dict, problems: list[tuple[str, str]]) -> dict:
    out["not_scored"] = out["stance"]["reason"] = (
        "; ".join(f"{n}'s foot {why}" for n, why in problems) + f": {FIX_FOOT}"
    )
    return out


def analyse(model, root: ET.Element, mounted: dict, cuts, refine) -> dict:
    """The standing sweep of a model: {"stance": record, "rows": [...], "not_scored": why | None,
    "unlimited": [...], "shared": [...]}. ``cuts()`` gives an SDF's loop pins (sdf.loop_cuts) and
    ``refine`` is checks._loop_refine, whose scorer argument carries the standing torque."""
    feet, skipped = find_feet(root)
    out: dict = {
        "stance": {
            "feet": [],
            "com_m": None,
            "weight_n": None,
            "cases": [],
            "balance_margin_m": None,
            "reason": None,
        },
        "rows": [],
        "not_scored": None,
        "unlimited": [],
        "shared": [],
    }
    if skipped:  # a foot standing cannot use: no scoring on the others
        bare = {k.get("name") for k in root.findall("link") if k.find("collision") is None}
        return _foot_problem(
            out,
            [
                (n, "has no collision" if n in bare else "is a mesh or a shape without a size")
                for n in skipped
            ],
        )
    if not feet:
        out["stance"]["reason"] = NO_FOOT
        return out
    lp = None
    try:
        try:
            plan = statics.gravity_plan(model, devices=mounted, keep_tied=True)  # ponytail: standing still walks a tied piece (open)
        except statics.ChainError as e:
            if not str(e).startswith("closed chain"):
                raise
            lp = statics.loop_plan(model, cuts(), mounted)
            plan = lp.plan if lp else None
    except (OSError, ET.ParseError, ValueError) as e:  # ChainError is a ValueError
        out["not_scored"] = str(e)
        return out
    if plan is None:
        out["not_scored"] = (
            "the model has no single root, or a link with mass has no centre of mass"
        )
        return out
    rot, pos = statics._frames(plan, [], [[]])[:2]
    if unplaced := [f.label for f in feet if f.link not in rot]:
        return _foot_problem(
            out, [(n, "is on a link that has no place in the solved model") for n in unplaced]
        )
    if feet[0].by_link:  # all feet came one way: by link name, keep each foot's lowest shape
        lowest: dict[str, tuple[float, Foot]] = {}
        for f in feet:  # ponytail: the lowest at the file's pose only
            z = float(lowest_point(f, rot[f.link], pos[f.link])[0][2])
            if f.label not in lowest or z < lowest[f.label][0]:
                lowest[f.label] = (z, f)
        feet = [f for _, f in lowest.values()]
    world = np.array([pos[n][0] + rot[n][0] @ q for n, q in zip(plan.links, plan.local)]).reshape(
        -1, 3
    )
    com = (plan.kg[:, None] * world).sum(axis=0) / plan.kg.sum()
    weight = float(plan.kg.sum() * statics.STANDARD_G)
    points = [lowest_point(f, rot[f.link], pos[f.link])[0] for f in feet]
    labels = [f.label for f in feet]
    cases, margin = load_cases(labels, points, com, weight)
    reach = {f.label: _reach(plan, lp, f) for f in feet}
    feet_of: dict[str, int] = {}
    for _, vary in reach.values():  # a joint on the way to two feet is not swept twice
        for j in vary:
            feet_of[j.name] = feet_of.get(j.name, 0) + 1
    ranges = {}
    for f in feet:
        rows, facts = _leg(plan, lp, f, *reach[f.label], feet_of, refine, cases)
        out["rows"] += rows
        out["unlimited"] += facts["unlimited"]
        out["shared"] += facts["shared"]
        ranges[f.label] = facts
    out["stance"] = {
        "feet": [
            {
                "name": f.label,
                "link": f.link,
                "shape": f.kind,
                "position_m": [float(v) for v in p],
                "height_range_m": ranges[f.label].get("height_m"),
                "fore_aft_travel_m": ranges[f.label].get("fore_aft_m"),
                "swept": ranges[f.label]["swept"],
            }
            for f, p in zip(feet, points)
        ],
        "com_m": [float(v) for v in com],
        "weight_n": weight,
        "cases": cases,
        "balance_margin_m": None if margin is None else float(margin),
        "reason": None,
    }
    return out


def text_lines(standing: dict) -> list[str]:
    """The ``check --all`` block: the stance, then one line per joint and case, then what is
    not covered."""
    stance = standing["stance"]
    com, margin = stance["com_m"], stance["balance_margin_m"]
    lines = [
        f"Standing: {len(stance['feet'])} feet ({', '.join(f['name'] for f in stance['feet'])}), "
        f"centre of mass at ({com[0]:.3f}, {com[1]:.3f}) m, weight {stance['weight_n']:.1f} N"
        + ("" if margin is None else f", {margin * 1000:.0f} mm from the nearest edge of the feet")
    ]
    for case in stance["cases"]:
        shares = (
            "tips (no force split)"
            if case["forces"] is None
            else ", ".join(f"{n} {f:.1f} N" for n, f in zip(case["feet"], case["forces"]))
        )
        lines.append(
            f"  {case['label']}: {shares}" + (f" ({case['note']})" if case["note"] else "")
        )
    for row in standing["rows"]:
        at = "not solved" if row["at_file_nm"] is None else f"{row['at_file_nm']:.2f}"
        pose = ", ".join(f"{k}={v:.0f}deg" for k, v in row["peak_pose_deg"].items())
        lines.append(
            f"  {row['joint']} {row['case']}: {at} N*m at the file's pose, peak {row['peak_nm']:.2f} "
            f"at {pose} ({row['share'] * 100:.0f}% of {row['limit_nm']:.2f} N*m)"
        )
    return [*lines, "  " + NOT_COVERED]
