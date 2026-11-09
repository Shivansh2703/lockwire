import math

import numpy as np
import pytest

from lockwire_pro.stats.clusters import (
    ALPHA_GATE,
    ClusterAllocation,
    PhysicalCheck,
    allocate_cluster_budget,
    familywise_allocation,
)


def _check(check_id: str, *paths: tuple[str, str]) -> PhysicalCheck:
    return PhysicalCheck(id=check_id, paths=paths)


def test_five_cluster_known_answer_and_conservation() -> None:
    checks = [_check(f"check.{index}", ("rail", f"rail.{index}")) for index in range(5)]

    allocation = familywise_allocation(checks)

    # Hand anchor: 0.005 / 5 = 0.001 per cluster, and 5 * 0.001 = 0.005.
    # Bites raw-check division, a missing cluster, and leaked or double-spent budget.
    assert [cluster.alpha for cluster in allocation.clusters] == pytest.approx([0.001] * 5)
    assert math.fsum(c.alpha for c in allocation.clusters) == pytest.approx(0.005)
    assert allocation.alpha_gate == ALPHA_GATE


def test_shared_path_collapses_correlated_flags_to_one_alarm() -> None:
    checks = [
        _check(f"device.{index}.voltage", ("rail", "rail.5v"), ("bus", f"bus.{index}"))
        for index in range(4)
    ]

    allocation = familywise_allocation(checks)

    # Inspection anchor: all four rows literally contain ("rail", "rail.5v"), so they
    # describe one rail alarm. Bites treating four correlated flags as four alarms.
    assert len(allocation.clusters) == 1
    assert allocation.clusters[0].check_ids == (
        "device.0.voltage",
        "device.1.voltage",
        "device.2.voltage",
        "device.3.voltage",
    )
    # Hand anchor: one cluster receives the entire 0.005 gate allocation.
    assert allocation.clusters[0].alpha == 0.005


def test_bridge_merges_paths_without_counting_a_check_twice() -> None:
    checks = [
        _check("left", ("bus", "bus.a")),
        _check("bridge", ("bus", "bus.a"), ("rail", "rail.x")),
        _check("right", ("rail", "rail.x")),
    ]

    allocation = familywise_allocation(checks)

    # Inspection anchor: left shares bus.a with bridge, which shares rail.x with right.
    # The connected component is {left, bridge, right}. Bites arbitrary path priority
    # and assigning the bridge to two clusters, which would double-spend its alarm.
    assert len(allocation.clusters) == 1
    assert allocation.clusters[0].check_ids == ("bridge", "left", "right")
    assert math.fsum(c.alpha for c in allocation.clusters) == 0.005


def test_raw_check_count_does_not_tighten_a_single_cluster() -> None:
    one = familywise_allocation([_check("only", ("host", "host.main"))])
    many = familywise_allocation(
        [_check(f"check.{index}", ("host", "host.main")) for index in range(500)]
    )

    # Fixed-budget anchor: both inputs contain exactly one physical-path cluster,
    # so each gets 0.005. Bites dividing by the 500 correlated raw checks or samples.
    assert one.clusters[0].alpha == 0.005
    assert many.clusters[0].alpha == 0.005
    assert len(many.clusters) == 1


def test_single_check_gets_its_clusters_full_allocation() -> None:
    single = _check("solo", ("bus", "bus.solo"))

    allocation = allocate_cluster_budget([(single,)], alpha_gate=0.002)

    # Hand anchor: 0.002 / 1 cluster = 0.002, independent of the one check inside.
    # Bites a second division by checks-per-cluster.
    assert allocation.clusters[0].check_ids == ("solo",)
    assert allocation.clusters[0].alpha == 0.002


def test_zero_clusters_and_zero_check_cluster_are_explicit_errors() -> None:
    # Known failure-mode anchor: neither case has a denominator with statistical
    # meaning. Bites division by zero and a gate crash that could fail open.
    with pytest.raises(ValueError, match="zero clusters"):
        allocate_cluster_budget([])
    with pytest.raises(ValueError, match="at least one check"):
        allocate_cluster_budget([[]])
    with pytest.raises(ValueError, match="at least one check"):
        ClusterAllocation(check_ids=(), paths=(), alpha=0.005)


def test_multiple_bus_membership_joins_both_bus_groups() -> None:
    checks = [
        _check("on-a", ("bus", "bus.a")),
        _check("dual-homed", ("bus", "bus.a"), ("bus", "bus.b")),
        _check("on-b", ("bus", "bus.b")),
    ]

    allocation = familywise_allocation(checks)

    # Inspection anchor: dual-homed explicitly names both buses, so all three checks
    # are connected. Bites choosing the first bus and leaving bus.b under-corrected.
    assert len(allocation.clusters) == 1
    assert allocation.clusters[0].check_ids == ("dual-homed", "on-a", "on-b")


@pytest.mark.parametrize("alpha_gate", [0.0, -0.1, 1.1, float("inf"), float("nan")])
def test_invalid_gate_budget_is_rejected(alpha_gate: float) -> None:
    # Closed-form domain anchor: a probability is finite and lies in (0, 1].
    # Bites nonsensical allocations, including a zero numerator that masks bad input.
    with pytest.raises(ValueError, match=r"in \(0, 1\]"):
        familywise_allocation([_check("check", ("host", "host"))], alpha_gate=alpha_gate)


def test_simulated_familywise_alpha_is_bounded_across_cluster_sizes() -> None:
    rng = np.random.default_rng(20260705)
    trials = 200_000
    cluster_count = 5

    for checks_per_cluster in (1, 4, 20):
        groups = [
            [
                _check(f"cluster.{cluster}.check.{check}", ("host", f"host.{cluster}"))
                for check in range(checks_per_cluster)
            ]
            for cluster in range(cluster_count)
        ]
        allocation = allocate_cluster_budget(groups)
        p_values = rng.random((trials, cluster_count, checks_per_cluster))

        # The Bonferroni combined decision is equivalently any raw p at or below
        # alpha_cluster / k. Bites reusing alpha_cluster as every check's threshold:
        # before calibration the fixed-seed k=4 measurement is 0.019485.
        alarms = np.any(
            p_values
            <= np.asarray(
                [cluster.alpha / len(cluster.check_ids) for cluster in allocation.clusters]
            )[None, :, None],
            axis=(1, 2),
        )
        achieved_alpha = float(np.mean(alarms))

        assert achieved_alpha <= ALPHA_GATE, (
            f"k={checks_per_cluster}: achieved alpha {achieved_alpha:.6f} "
            f"exceeds alpha_gate {ALPHA_GATE:.6f}"
        )
