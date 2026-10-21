"""The mechanism check: a **closed, named set** of mechanisms, answered in closed form.

Version one answers exactly one kind — the **planar four-bar linkage** — by the
Grashof condition. With ``s`` the shortest link, ``l`` the longest and ``p``, ``q``
the other two, a four-bar has a link that can fully rotate iff ``s + l <= p + q``.
That is an exact algebraic statement about four measured lengths: no solver, no
iteration, no tolerance argument, and therefore nothing this module waits on.
(The force solver is a separate module; a mechanism verdict never comes from it.)

**Everything else is NOT SCORED, naming the kind it found.** A five-bar loop is not
scored as a four-bar built from four of its five links: that would be a confident,
plausible, wrong answer, and it is the exact failure this module exists to prevent.
The same refusal covers a loop whose joints are not all revolute, a loop that is not
a planar linkage, and a loop whose geometry the contract does not record. A number
is only ever printed when it was measured from the contract; nothing is
defaulted and nothing is guessed.

**The sentence is composed here**. Every verdict carries the finished
plain-words ``text`` that a terminal, a page or an agent shows verbatim. No surface
downstream assembles a phrase out of these fields.

Geometry, and why no forward kinematics is needed: a URDF joint's origin is its pose
in the *parent* link's frame, and the *child* link's frame is the joint frame. So for
a link that is the parent of one loop joint and the child of the other, the two
attachment points in that link's own frame are ``joint.origin.xyz`` and the origin —
and the link's length is the norm of that one vector. Reading a closed loop therefore
needs nothing but the joints the contract already records (no second walk of the
model, no re-parse of the URDF).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from lockwire.schema import Joint, ModelRecord

#: The named set. Version one holds exactly one entry; a kind absent from it is
#: NOT SCORED by name rather than approximated by the nearest thing that is here.
NAMED_SET: tuple[str, ...] = ("four-bar",)

#: Joint types a planar linkage of pin joints is made of. A loop containing any
#: other type is a different machine and is not scored.
_PIN_JOINTS = frozenset({"revolute", "continuous"})

#: A joint that declares its link goes all the way round. This is the *declaration*
#: the measured geometry is compared against — the second operand, without which
#: there is nothing to score.
FULLY_ROTATING_TYPE = "continuous"

_NUMBER_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}

Verdict = Literal["rotates", "change-point", "locking", "not-scored"]


@dataclass(frozen=True)
class Mechanism:
    """One closed loop in the model, and what the closed form says about it.

    ``s_plus_l``/``p_plus_q`` are ``None`` on every not-scored path — a declined
    answer carries no numbers at all, so no reader can mistake a blank for a zero.
    """

    kind: str
    verdict: Verdict
    links: tuple[str, ...]
    joints: tuple[str, ...]
    text: str
    s_plus_l: float | None = None
    p_plus_q: float | None = None
    lengths_m: tuple[float, ...] = ()
    declared_fully_rotating: tuple[str, ...] = ()
    #: Joints that really turn all the way round: the two at the ends of the shortest
    #: link. Grashof's verdict is for the linkage; every other joint only rocks.
    rotating_joints: tuple[str, ...] = ()

    @property
    def scored(self) -> bool:
        return self.verdict != "not-scored"


def _kind_word(count: int) -> str:
    return f"{_NUMBER_WORDS.get(count, str(count))}-bar"


def _fmt(value: float) -> str:
    """Lengths in metres, trailing zeros trimmed — the measured value, not a rounding."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _norm(xyz: tuple[float, float, float]) -> float:
    return math.dist(xyz, (0.0, 0.0, 0.0))


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# ------------------------------------------------------------------ loop finding


def _loop_joints(joints: list[Joint]) -> list[Joint]:
    """The 2-core: strip links of degree < 2 until nothing more can be stripped.

    What survives is exactly the part of the model that contains closed loops — a
    tree prunes away to nothing, which is why an ordinary robot produces no
    mechanism entries at all and this check costs a clean contract zero findings.
    """
    alive = [joint for joint in joints if joint.parent != joint.child]
    while alive:
        degree: Counter[str] = Counter()
        for joint in alive:
            degree[joint.parent] += 1
            degree[joint.child] += 1
        leaves = {link for link, count in degree.items() if count < 2}
        if not leaves:
            return alive
        alive = [j for j in alive if j.parent not in leaves and j.child not in leaves]
    return alive


def _components(joints: list[Joint]) -> list[list[Joint]]:
    """Connected components of the 2-core, each a candidate mechanism."""
    parent: dict[str, str] = {}

    def find(link: str) -> str:
        parent.setdefault(link, link)
        while parent[link] != link:
            parent[link] = parent[parent[link]]
            link = parent[link]
        return link

    for joint in joints:
        parent[find(joint.parent)] = find(joint.child)

    grouped: dict[str, list[Joint]] = {}
    for joint in joints:
        grouped.setdefault(find(joint.parent), []).append(joint)
    return [grouped[key] for key in sorted(grouped)]


def _cycle_order(component: list[Joint]) -> tuple[str, ...] | None:
    """Link names in cycle order, or ``None`` when the component is not one simple cycle.

    A component where some link carries three or more joints is two loops sharing a
    link — a coupled linkage, which the named set does not hold.
    """
    neighbours: dict[str, list[str]] = {}
    for joint in component:
        neighbours.setdefault(joint.parent, []).append(joint.child)
        neighbours.setdefault(joint.child, []).append(joint.parent)
    if len(neighbours) != len(component) or any(len(n) != 2 for n in neighbours.values()):
        return None

    start = min(neighbours)
    order = [start]
    previous, current = start, neighbours[start][0]
    while current != start:
        order.append(current)
        first, second = neighbours[current]
        previous, current = current, second if first == previous else first
    return tuple(order)


def find_loops(joints: list[Joint]) -> list[list[Joint]]:
    """Every closed loop in the recorded joints, as its list of joints."""
    return _components(_loop_joints(joints))


# ------------------------------------------------------------------ the four-bar


def _declared_fully_rotating(component: list[Joint]) -> tuple[str, ...]:
    """Loop joints declared ``continuous`` — the operand the geometry is compared to."""
    return tuple(
        joint.name
        for joint in sorted(component, key=lambda j: j.name)
        if joint.type == FULLY_ROTATING_TYPE
    )


def _decline(
    kind: str,
    links: tuple[str, ...],
    joints: tuple[str, ...],
    because: str,
    declared: tuple[str, ...],
) -> Mechanism:
    return Mechanism(
        kind=kind,
        verdict="not-scored",
        links=links,
        joints=joints,
        declared_fully_rotating=declared,
        text=(
            f"not scored: this loop is {because}. The mechanism check answers "
            f"{', '.join(NAMED_SET)} linkages and nothing else, so no number is reported here."
        ),
    )


def _directed_lengths(
    order: tuple[str, ...], component: list[Joint]
) -> tuple[list[float], list[tuple[float, float, float]]] | None:
    """One length per link, or ``None`` when the loop is not a directed cycle.

    Each link must be the parent of exactly one loop joint and the child of exactly
    one other; only then are both of its attachment points expressible in its own
    frame without forward kinematics.
    """
    parented: dict[str, Joint] = {}
    childed: dict[str, Joint] = {}
    for joint in component:
        if joint.parent in parented or joint.child in childed:
            return None
        parented[joint.parent] = joint
        childed[joint.child] = joint
    if set(parented) != set(order) or set(childed) != set(order):
        return None
    offsets = [parented[link].origin.xyz for link in order]
    return [_norm(offset) for offset in offsets], offsets


def _analyse_loop(component: list[Joint]) -> Mechanism:
    joint_names = tuple(sorted(joint.name for joint in component))
    declared = _declared_fully_rotating(component)
    order = _cycle_order(component)
    if order is None:
        links = tuple(sorted({j.parent for j in component} | {j.child for j in component}))
        return _decline(
            "coupled linkage",
            links,
            joint_names,
            f"a coupled linkage: {len(links)} links share {len(component)} joints, "
            "so it is not one simple loop",
            declared,
        )

    kind = _kind_word(len(order))
    if kind not in NAMED_SET:
        return _decline(kind, order, joint_names, f"a {kind}", declared)

    types = {joint.type for joint in component}
    if not types <= _PIN_JOINTS:
        other = ", ".join(sorted(types - _PIN_JOINTS))
        return _decline(
            kind,
            order,
            joint_names,
            f"a four-link loop containing a {other} joint, not a linkage of pin joints",
            declared,
        )

    measured = _directed_lengths(order, component)
    if measured is None:
        return _decline(
            kind,
            order,
            joint_names,
            "a four-link loop that is not a directed cycle (a link is the parent, or the "
            "child, of both its joints), so its link lengths are recorded in frames this "
            "check cannot relate",
            declared,
        )
    lengths, offsets = measured

    axes = [joint.axis for joint in component]
    if any(axis is None for axis in axes):
        return _decline(
            kind,
            order,
            joint_names,
            "a four-bar whose joint axes the contract does not record, so it cannot be "
            "confirmed planar — re-run init against a URDF that declares joint axes",
            declared,
        )
    reference = axes[0]
    assert reference is not None
    if any(_cross(reference, axis) != (0.0, 0.0, 0.0) for axis in axes if axis is not None):
        return _decline(
            kind,
            order,
            joint_names,
            "a spatial four-link loop: its joint axes are not parallel, and the Grashof "
            "condition is a statement about a planar linkage",
            declared,
        )
    off_plane = [
        link for link, offset in zip(order, offsets, strict=True) if _dot(reference, offset) != 0.0
    ]
    if off_plane:
        return _decline(
            kind,
            order,
            joint_names,
            "a spatial four-link loop: "
            + ", ".join(off_plane)
            + " is offset along the joint axis rather than across it, so the loop does not "
            "lie in one plane",
            declared,
        )
    if any(length == 0.0 for length in lengths):
        zero = [link for link, length in zip(order, lengths, strict=True) if length == 0.0]
        return _decline(
            kind,
            order,
            joint_names,
            "a four-bar with a zero-length link (" + ", ".join(zero) + "): its two joint "
            "origins coincide, so there is no length to compare",
            declared,
        )

    ordered = sorted(lengths)
    shortest, longest = ordered[0], ordered[3]
    s_plus_l = shortest + longest
    p_plus_q = ordered[1] + ordered[2]
    crank = order[lengths.index(shortest)]
    sums = f"s + l = {_fmt(s_plus_l)} m, p + q = {_fmt(p_plus_q)} m"

    if math.isclose(s_plus_l, p_plus_q, rel_tol=1e-9):
        verdict = "change-point"
        text = (
            f"this four-bar is a change-point linkage: {sums} are equal, the boundary of the "
            f"Grashof condition. The shortest link ({crank}, {_fmt(shortest)} m) can turn all "
            "the way round, but the linkage passes through a folded position where its motion "
            "is indeterminate — which branch it takes is not decided by the link lengths."
        )
    elif s_plus_l < p_plus_q:
        verdict: Verdict = "rotates"
        text = (
            f"this four-bar has a fully rotating link: {sums}, and the Grashof condition "
            f"s + l <= p + q holds, so the shortest link ({crank}, {_fmt(shortest)} m) turns "
            "all the way round relative to the other three."
        )
    else:
        verdict = "locking"
        text = (
            f"this four-bar has no fully rotating link: {sums}, so the Grashof condition "
            "s + l <= p + q fails and every link only rocks between two limits."
        )

    by_ends = {frozenset((j.parent, j.child)): j.name for j in component}
    rotating = tuple(
        sorted(
            {
                by_ends[frozenset((order[k], order[(k + step) % 4]))]
                for k, length in enumerate(lengths)
                if math.isclose(length, shortest, rel_tol=1e-9)
                for step in (-1, 1)
            }
        )
        if verdict != "locking"
        else ()
    )
    return Mechanism(
        kind=kind,
        verdict=verdict,
        links=order,
        joints=joint_names,
        text=text,
        s_plus_l=s_plus_l,
        p_plus_q=p_plus_q,
        lengths_m=tuple(lengths),
        declared_fully_rotating=declared,
        rotating_joints=rotating,
    )


def analyse_model(model: ModelRecord | None) -> list[Mechanism]:
    """Every closed loop the recorded model contains, answered or declined by name."""
    if model is None or not model.joints:
        return []
    return [_analyse_loop(component) for component in find_loops(model.joints)]
