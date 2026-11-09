"""Familywise error allocation across shared physical failure paths."""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ALPHA_GATE = 0.005

FailurePathKind = Literal["bus", "rail", "host"]
FailurePath = tuple[FailurePathKind, str]


@dataclass(frozen=True)
class PhysicalCheck:
    """A check and every physical path capable of correlating its result."""

    id: str
    paths: tuple[FailurePath, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("check id must not be empty")
        if not self.paths:
            raise ValueError("a check must have at least one physical failure path")
        for kind, path_id in self.paths:
            if kind not in ("bus", "rail", "host"):
                raise ValueError(f"unknown physical failure path kind {kind!r}")
            if not path_id:
                raise ValueError("physical failure path id must not be empty")
        object.__setattr__(self, "paths", tuple(sorted(set(self.paths))))


@dataclass(frozen=True)
class ClusterAllocation:
    """One alarm cluster and its share of the gate-level budget."""

    check_ids: tuple[str, ...]
    paths: tuple[FailurePath, ...]
    alpha: float

    def __post_init__(self) -> None:
        if not self.check_ids:
            raise ValueError("an alarm cluster must contain at least one check")


@dataclass(frozen=True)
class FamilywiseAllocation:
    """The complete per-preflight allocation, with one entry per alarm."""

    alpha_gate: float
    clusters: tuple[ClusterAllocation, ...]


def _alpha_gate(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("alpha_gate must be a real number")
    alpha = float(value)
    if not math.isfinite(alpha) or not 0.0 < alpha <= 1.0:
        raise ValueError("alpha_gate must be finite and in (0, 1]")
    return alpha


def allocate_cluster_budget(
    clusters: Iterable[Iterable[PhysicalCheck]],
    *,
    alpha_gate: float = ALPHA_GATE,
) -> FamilywiseAllocation:
    """Divide ``alpha_gate`` equally across non-empty, disjoint alarm clusters."""
    alpha = _alpha_gate(alpha_gate)
    groups = tuple(tuple(cluster) for cluster in clusters)
    if not groups:
        raise ValueError("cannot allocate a familywise budget across zero clusters")
    if any(not group for group in groups):
        raise ValueError("an alarm cluster must contain at least one check")

    check_ids = [check.id for group in groups for check in group]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("a check must belong to exactly one alarm cluster")

    alpha_cluster = alpha / len(groups)
    allocations = tuple(
        ClusterAllocation(
            check_ids=tuple(sorted(check.id for check in group)),
            paths=tuple(sorted({path for check in group for path in check.paths})),
            alpha=alpha_cluster,
        )
        for group in groups
    )
    return FamilywiseAllocation(alpha_gate=alpha, clusters=allocations)


def familywise_allocation(
    checks: Iterable[PhysicalCheck],
    *,
    alpha_gate: float = ALPHA_GATE,
) -> FamilywiseAllocation:
    """Cluster checks by shared paths, then allocate one budget share per cluster.

    Clustering uses connected components. A check belonging to two physical paths
    therefore joins their groups into one alarm instead of spending the check's
    budget twice or assigning it to an arbitrary path.
    """
    items = tuple(checks)
    if not items:
        raise ValueError("cannot allocate a familywise budget across zero clusters")
    ids = [check.id for check in items]
    if len(ids) != len(set(ids)):
        raise ValueError("check ids must be unique")

    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    path_owner: dict[FailurePath, int] = {}
    for index, check in enumerate(items):
        for path in check.paths:
            previous = path_owner.setdefault(path, index)
            union(index, previous)

    grouped: dict[int, list[PhysicalCheck]] = {}
    for index, check in enumerate(items):
        grouped.setdefault(find(index), []).append(check)
    ordered = sorted(grouped.values(), key=lambda group: min(check.id for check in group))
    return allocate_cluster_budget(ordered, alpha_gate=alpha_gate)
