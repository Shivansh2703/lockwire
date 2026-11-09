"""Wilks distribution-free tolerance bounds.

Authoritative design: `docs/stats_design.md` §1. An envelope bound is an order
statistic of a golden-run sample, chosen so the interval covers proportion `p`
of the population with confidence `1-alpha`, with no distributional assumption
(Wilks 1941).

## Derivation (why the code below is what it is)

Let X_1..X_n be iid draws and U_(k) = F(X_(k)) the coverage achieved by using
the k-th smallest observation (ascending rank k, 1-indexed) as a one-sided
upper bound. It is a standard fact that U_(k) ~ Beta(k, n-k+1). We want the
bound to satisfy P(coverage >= p) >= 1-alpha, i.e.

    1 - alpha <= 1 - I_p(k, n-k+1)   =>   alpha >= I_p(k, n-k+1)

and the regularized incomplete beta is exactly the binomial survival function
(a standard identity, with a=k, b=n-k+1, a+b-1=n):

    I_p(k, n-k+1) = sum_{i=k}^{n} C(n,i) p^i (1-p)^(n-i)

So the *achieved* alpha at rank k is that exact tail sum, computed with
`math.comb` — no scipy, no continued-fraction incomplete-beta (frozen
decision, `docs/build_plan.md` box B5). The envelope locks when achieved
alpha <= requested alpha, i.e. achieved confidence >= requested confidence.

For the plain "sample extreme" one-sided bound (k=n, the maximum), this
collapses to the classical closed form achieved-alpha = p**n, which is the
textbook one-sided Wilks result and reproduces n>=59 for p=0.95, alpha=0.05
exactly (p**58 > 0.05, p**59 <= 0.05) — see `tests/test_wilks.py`.

For a two-sided bound using both sample extremes [X_(1), X_(n)], the coverage
is the range U_(n) - U_(1) ~ Beta(n-1, 2), giving achieved-alpha =
I_p(n-1, 2) = the same tail-sum identity with k=n-1, a two-term sum. This
reproduces n>=93 for p=0.95, alpha=0.05.

This module implements the condition from `docs/stats_design.md` §1:
`α ≥ I_p(r, n − r + 1)`, with r = ascending rank of the bound. The envelope
locks when achieved alpha <= requested alpha.

Beyond the extreme, interior order statistics (2nd, 3rd from the edge) trade
a larger required n for robustness to a single outlier — `rank_from_edge`
below selects which one; `rank_from_edge=1` is the sample extreme.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import comb


class Side(str, Enum):
    """Which envelope shape the order statistic(s) bound."""

    ONE_SIDED = "one_sided"
    TWO_SIDED = "two_sided"


def binomial_tail(n: int, r: int, p: float) -> float:
    """Exact P(Binomial(n, p) >= r) = I_p(r, n-r+1), via integer `math.comb`.

    Standard library only. This is the frozen computation path for B5 — no
    scipy, no continued-fraction incomplete-beta.
    """
    if r > n + 1 or r < 0:
        raise ValueError(f"r={r} out of range for n={n}")
    if r > n:
        return 0.0
    if r == 0:
        return 1.0

    # A float is an exact binary rational p_num / denominator. Give every
    # binomial term the common denominator denominator**n, then sum only
    # integer numerators. This keeps enormous `comb` values out of float
    # arithmetic; the sole conversion happens after the exact result is
    # bounded in [0, 1].
    p_num, denominator = p.as_integer_ratio()
    q_num = denominator - p_num
    common_denominator = denominator**n

    # Sum the shorter side, using the exact complement when the lower tail
    # has fewer terms. This matters for public calls with ranks near zero.
    if n - r + 1 <= r:
        numerator = sum(comb(n, i) * p_num**i * q_num ** (n - i) for i in range(r, n + 1))
    else:
        lower_numerator = sum(comb(n, i) * p_num**i * q_num ** (n - i) for i in range(r))
        numerator = common_denominator - lower_numerator

    return float(Fraction(numerator, common_denominator))


def _one_sided_rank(n: int, rank_from_edge: int) -> int:
    """Ascending rank of the order statistic used as a one-sided bound."""
    return n - rank_from_edge + 1


def _two_sided_rank(n: int, rank_from_edge: int) -> int:
    """Ascending-rank floor of the tail sum for a two-sided (both-edge) bound."""
    return n - 2 * rank_from_edge + 1


def achieved_alpha(n: int, p: float, side: Side, rank_from_edge: int = 1) -> float:
    """The alpha (1 - confidence) actually achieved by n samples at coverage p.

    `rank_from_edge=1` uses the sample extreme(s); 2 uses the second-from-edge
    order statistic(s), etc.
    """
    if side is Side.ONE_SIDED:
        r = _one_sided_rank(n, rank_from_edge)
    else:
        r = _two_sided_rank(n, rank_from_edge)
    if r < 1:
        # Not enough samples to even define the requested order statistic(s).
        return 1.0
    return binomial_tail(n, r, p)


def min_n_required(
    p: float,
    confidence: float,
    side: Side,
    rank_from_edge: int = 1,
    *,
    n_max: int = 100_000,
) -> int:
    """Smallest n whose achieved alpha is <= 1 - confidence, at coverage p."""
    target_alpha = 1.0 - confidence
    floor = rank_from_edge if side is Side.ONE_SIDED else 2 * rank_from_edge
    n = max(floor, 1)
    while n <= n_max:
        if achieved_alpha(n, p, side, rank_from_edge) <= target_alpha:
            return n
        n += 1
    raise ValueError(
        f"no n <= {n_max} achieves alpha <= {target_alpha} "
        f"for p={p}, side={side}, rank_from_edge={rank_from_edge}"
    )


@dataclass(frozen=True)
class OrderStatBound:
    """A locked tolerance-envelope bound: which order statistic(s) to use.

    `ranks` are 1-indexed ascending order-statistic positions in the sample
    (e.g. `(n,)` for the one-sided sample maximum, `(1, n)` for both extremes).
    """

    side: Side
    n: int
    p: float
    confidence: float
    rank_from_edge: int
    ranks: tuple[int, ...]
    achieved_confidence: float


@dataclass(frozen=True)
class UnderSampled:
    """n is below the Wilks threshold for the requested (p, confidence).

    Deliberately carries no bound/rank fields: unlike `OrderStatBound`, there
    is no `ranks` or `achieved_confidence` attribute here, so a caller cannot
    accidentally read an envelope bound out of an under-sampled result — any
    such attempt is an `AttributeError`, not a silently wrong number. Callers
    must fall back to hand-typed hard limits (`docs/stats_design.md` §1).
    """

    side: Side
    n: int
    p: float
    confidence: float
    min_n_required: int


WilksResult = OrderStatBound | UnderSampled


def evaluate(
    n: int,
    p: float,
    confidence: float,
    side: Side,
    rank_from_edge: int = 1,
) -> WilksResult:
    """Select the order-statistic envelope bound for `n` samples, or report
    `under-sampled` when `n` is below the threshold for the requested
    (p, confidence). Never returns a locked bound below threshold.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if rank_from_edge < 1:
        raise ValueError(f"rank_from_edge must be >= 1, got {rank_from_edge}")

    threshold = min_n_required(p, confidence, side, rank_from_edge)
    if n < threshold:
        return UnderSampled(side=side, n=n, p=p, confidence=confidence, min_n_required=threshold)

    alpha = achieved_alpha(n, p, side, rank_from_edge)
    if side is Side.ONE_SIDED:
        ranks = (_one_sided_rank(n, rank_from_edge),)
    else:
        ranks = (rank_from_edge, n - rank_from_edge + 1)
    return OrderStatBound(
        side=side,
        n=n,
        p=p,
        confidence=confidence,
        rank_from_edge=rank_from_edge,
        ranks=ranks,
        achieved_confidence=1.0 - alpha,
    )
