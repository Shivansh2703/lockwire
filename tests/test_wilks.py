"""Tests for `lockwire_pro.stats.wilks`.

Every expected number here is either hand-computed arithmetic (small cases,
shown in comments) or derived independently in this file via a *different*
code path than `wilks.py` uses (closed-form formulas, not `math.comb`), then
checked against the module. Nothing here is "run the implementation and
record what it printed" — that standing rule comes from the B5 dispatch.

External truth anchor (the published Wilks tolerance-limit numbers, not this module's output):
one-sided 95% coverage / 95% confidence needs n >= 59 (bound = sample
extreme); two-sided 95/95 (both extremes) needs n >= 93.
"""

from fractions import Fraction
from math import comb

import pytest

from lockwire_pro.stats.wilks import (
    OrderStatBound,
    Side,
    UnderSampled,
    achieved_alpha,
    binomial_tail,
    evaluate,
    min_n_required,
)

P = 0.95
CONFIDENCE = 0.95
ALPHA = 1.0 - CONFIDENCE  # 0.05


# ---------------------------------------------------------------------------
# 1. binomial_tail: hand-computed small cases
# ---------------------------------------------------------------------------


def test_binomial_tail_hand_computed_extreme():
    # n=3, r=3 (only the i=3 term survives), p=0.5:
    #   C(3,3) * 0.5^3 * 0.5^0 = 1 * 0.125 * 1 = 0.125
    # Mutation-bite: a `>` vs `>=` bug in the summation bound (range(r+1, n+1)
    # instead of range(r, n+1)) would drop the only term and return 0.0 here;
    # a 1-p/p swap would still give 0.125 by symmetry at p=0.5, so this case
    # alone doesn't catch that — see the next case for that bug.
    assert binomial_tail(3, 3, 0.5) == pytest.approx(0.125)


def test_binomial_tail_hand_computed_full_range():
    # n=3, r=1, p=0.5: sum over i=1,2,3 of C(3,i) 0.5^i 0.5^(3-i).
    # Complement of the i=0 term: 1 - C(3,0)*0.5^0*0.5^3 = 1 - 0.125 = 0.875.
    # Mutation-bite: wrong tail direction (summing i=0..r-1 instead of
    # i=r..n) would return 0.125 here instead of 0.875 — this case catches
    # exactly the bug the n=3,r=3 case above could not distinguish.
    assert binomial_tail(3, 1, 0.5) == pytest.approx(0.875)


def test_binomial_tail_hand_computed_asymmetric_p():
    # n=2, r=2, p=0.9: only i=2 term: C(2,2)*0.9^2*0.1^0 = 0.81.
    # Mutation-bite: a p <-> (1-p) swap in the summand would give 0.1^2=0.01
    # instead of 0.81 — this is the case that specifically catches that bug,
    # since p=0.5 above is symmetric and can't.
    assert binomial_tail(2, 2, 0.9) == pytest.approx(0.81)


def test_binomial_tail_matches_direct_comb_sum():
    # Cross-check against a hand-expanded sum for a case with >2 terms:
    # n=4, r=2, p=0.6:
    #   i=2: C(4,2)*0.6^2*0.4^2 = 6*0.36*0.16   = 0.3456
    #   i=3: C(4,3)*0.6^3*0.4^1 = 4*0.216*0.4   = 0.3456
    #   i=4: C(4,4)*0.6^4*0.4^0 = 1*0.1296      = 0.1296
    #   total                                    = 0.8208
    assert binomial_tail(4, 2, 0.6) == pytest.approx(0.8208)


# ---------------------------------------------------------------------------
# 2. One-sided n>=59 threshold, derived independently (closed form, no comb)
# ---------------------------------------------------------------------------


def _one_sided_closed_form_alpha(n: int, p: float) -> float:
    """Achieved alpha for the one-sided sample-extreme bound: p**n.

    Independent derivation (does not call `wilks.binomial_tail`/`math.comb`):
    using the sample maximum as a one-sided upper bound, the achieved
    coverage equals the max of n iid Uniform(0,1) draws, and
    P(that max < p) = p**n by independence. So the achieved alpha (the
    probability the bound fails to cover proportion p) is exactly p**n.
    This is the textbook one-sided Wilks closed form, not a restatement of
    the general tail-sum code path in wilks.py.
    """
    return p**n


def _search_one_sided_threshold(p: float, alpha: float, n_max: int = 1000) -> int:
    n = 1
    while n <= n_max:
        if _one_sided_closed_form_alpha(n, p) <= alpha:
            return n
        n += 1
    raise AssertionError("no threshold found")


def test_one_sided_threshold_matches_independent_derivation():
    independent_n = _search_one_sided_threshold(P, ALPHA)
    module_n = min_n_required(P, CONFIDENCE, Side.ONE_SIDED, rank_from_edge=1)
    assert independent_n == module_n == 59  # external truth: Wilks' 95/95 numbers


def test_one_sided_n58_fails_n59_passes():
    # Mutation-bite: an off-by-one in `_one_sided_rank` (e.g. `n - rank_from_edge`
    # instead of `n - rank_from_edge + 1`) shifts this boundary by one sample
    # in either direction — this pair of assertions catches that directly.
    # Demonstrated live (see B5 commit message): reverted after confirming failure.
    result_58 = evaluate(n=58, p=P, confidence=CONFIDENCE, side=Side.ONE_SIDED)
    result_59 = evaluate(n=59, p=P, confidence=CONFIDENCE, side=Side.ONE_SIDED)
    assert isinstance(result_58, UnderSampled)
    assert isinstance(result_59, OrderStatBound)
    assert result_59.ranks == (59,)


# ---------------------------------------------------------------------------
# 3. Two-sided n>=93 threshold, derived independently (closed form, no comb)
# ---------------------------------------------------------------------------


def _two_sided_closed_form_alpha(n: int, p: float) -> float:
    """Achieved alpha for the two-sided both-extremes bound.

    Independent derivation: the coverage of [X_(1), X_(n)] is the range of n
    iid Uniform(0,1) draws, R = U_(n) - U_(1), whose density is
    n(n-1) u^(n-2) (1-u) on [0,1] (standard order-statistics range result).
    P(R < p) is then, by direct integration / the standard identity for the
    range distribution:
        P(R < p) = n*p**(n-1)*(1-p) + p**n
    (the "at most one of the n draws lands outside [0,p]" decomposition).
    This achieved alpha uses no `math.comb`-based tail sum — a different
    code path than wilks.py's general `binomial_tail`.
    """
    return n * p ** (n - 1) * (1 - p) + p**n


def _search_two_sided_threshold(p: float, alpha: float, n_max: int = 1000) -> int:
    n = 2
    while n <= n_max:
        if _two_sided_closed_form_alpha(n, p) <= alpha:
            return n
        n += 1
    raise AssertionError("no threshold found")


def test_two_sided_threshold_matches_independent_derivation():
    independent_n = _search_two_sided_threshold(P, ALPHA)
    module_n = min_n_required(P, CONFIDENCE, Side.TWO_SIDED, rank_from_edge=1)
    assert independent_n == module_n == 93  # external truth: Wilks' 95/95 numbers


def test_two_sided_n92_fails_n93_passes():
    # Mutation-bite: an off-by-one in `_two_sided_rank` (e.g.
    # `n - 2*rank_from_edge` instead of `n - 2*rank_from_edge + 1`) shifts
    # this boundary — this pair of assertions catches that directly.
    # Demonstrated live (see B5 commit message): reverted after confirming failure.
    result_92 = evaluate(n=92, p=P, confidence=CONFIDENCE, side=Side.TWO_SIDED)
    result_93 = evaluate(n=93, p=P, confidence=CONFIDENCE, side=Side.TWO_SIDED)
    assert isinstance(result_92, UnderSampled)
    assert isinstance(result_93, OrderStatBound)
    assert result_93.ranks == (1, 93)


# ---------------------------------------------------------------------------
# 4. Negative controls — envelope must NOT lock
# ---------------------------------------------------------------------------


def test_negative_control_small_n_never_locks():
    # A tiny sample must not produce a locked envelope at any reasonable
    # target — the single worst outcome per the B5 brief is a tight envelope
    # with false confidence. Mutation-bite: an inverted lock condition
    # (`achieved_alpha >= target_alpha` instead of `<=`) would lock this.
    result = evaluate(n=3, p=P, confidence=CONFIDENCE, side=Side.ONE_SIDED)
    assert isinstance(result, UnderSampled)
    assert result.min_n_required == 59


def test_negative_control_stricter_target_at_the_looser_threshold():
    # n=59 is exactly enough for one-sided 95/95, but not for 99/99 — the
    # module must recompute per request, not treat "n>=59" as universally
    # sufficient. Mutation-bite: a hard-coded `n >= 59` shortcut (instead of
    # calling min_n_required with the actual requested p/confidence) would
    # wrongly lock this.
    result = evaluate(n=59, p=0.99, confidence=0.99, side=Side.ONE_SIDED)
    assert isinstance(result, UnderSampled)


def test_under_sampled_carries_no_bound_fields():
    # Design requirement: a caller cannot accidentally read a bound out of an
    # under-sampled result. UnderSampled has no `ranks` / `achieved_confidence`
    # attributes at all, so any such access is an AttributeError, not a
    # silently-wrong number.
    result = evaluate(n=3, p=P, confidence=CONFIDENCE, side=Side.ONE_SIDED)
    assert isinstance(result, UnderSampled)
    assert not hasattr(result, "ranks")
    assert not hasattr(result, "achieved_confidence")
    with pytest.raises(AttributeError):
        result.ranks  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 5. Achieved coverage/confidence reporting (consumed by B11/B12)
# ---------------------------------------------------------------------------


def test_locked_bound_reports_achieved_confidence_at_least_requested():
    result = evaluate(n=59, p=P, confidence=CONFIDENCE, side=Side.ONE_SIDED)
    assert isinstance(result, OrderStatBound)
    # p**59 <= 0.05  =>  achieved_confidence = 1 - p**59 >= 0.95
    assert result.achieved_confidence >= CONFIDENCE
    assert result.achieved_confidence == pytest.approx(1.0 - P**59)


def test_interior_order_statistic_needs_more_samples_than_the_extreme():
    # The stats design: "more samples -> use interior order statistics
    # ... for the same guarantee with robustness to single outliers." The
    # 2nd-from-edge bound must require n strictly greater than the extreme's
    # threshold for the same (p, confidence).
    extreme_threshold = min_n_required(P, CONFIDENCE, Side.ONE_SIDED, rank_from_edge=1)
    interior_threshold = min_n_required(P, CONFIDENCE, Side.ONE_SIDED, rank_from_edge=2)
    assert interior_threshold > extreme_threshold


def test_large_n_deep_interior_rank_matches_exact_integer_tail():
    # At p=1/2, the second half of every binomial term has the common exact
    # denominator 2**n. For the 200th-from-edge bound, the required upper
    # tail is equivalently 0..199 failures, so its exact numerator is the
    # integer sum C(5000,0) + ... + C(5000,199). The implementation must not
    # convert any of those enormous integers to float before forming the
    # final bounded ratio.
    n = 5000
    rank_from_edge = 200
    exact_alpha = Fraction(
        sum(comb(n, failures) for failures in range(rank_from_edge)),
        2**n,
    )

    result = evaluate(
        n=n,
        p=0.5,
        confidence=0.95,
        side=Side.ONE_SIDED,
        rank_from_edge=rank_from_edge,
    )

    assert isinstance(result, OrderStatBound)  # bites premature int-to-float overflow
    assert result.achieved_confidence == float(1 - exact_alpha)  # bites inexact tail math


def test_n5000_third_from_edge_matches_exact_integer_tail():
    # p is an exact binary rational p_num/p_den. For the third-from-edge
    # bound there are exactly three terms (0, 1, or 2 failures), all over
    # p_den**n. This integer numerator is the exact binomial result for the
    # float supplied to evaluate(), independently reduced by Fraction.
    n = 5000
    p = 0.999
    p_num, p_den = p.as_integer_ratio()
    q_num = p_den - p_num
    exact_alpha = Fraction(
        p_num**n + n * p_num ** (n - 1) * q_num + comb(n, 2) * p_num ** (n - 2) * q_num**2,
        p_den**n,
    )

    result = evaluate(
        n=n,
        p=p,
        confidence=0.001,
        side=Side.ONE_SIDED,
        rank_from_edge=3,
    )

    assert isinstance(result, OrderStatBound)  # bites large-N interior-rank overflow
    assert result.ranks == (4998,)  # bites third-from-edge rank selection
    assert result.achieved_confidence == float(1 - exact_alpha)  # bites approximate tail math


# ---------------------------------------------------------------------------
# 6. achieved_alpha / binomial_tail consistency at the anchors (sanity, not
#    the primary derivation above)
# ---------------------------------------------------------------------------


def test_achieved_alpha_one_sided_matches_comb_sum_at_n59():
    # Direct check that achieved_alpha routes through the exact comb-based
    # tail sum and agrees with the single-term closed form p**59.
    expected = sum(comb(59, i) * P**i * (1 - P) ** (59 - i) for i in range(59, 60))
    assert achieved_alpha(59, P, Side.ONE_SIDED) == pytest.approx(expected)
    assert expected == pytest.approx(P**59)
