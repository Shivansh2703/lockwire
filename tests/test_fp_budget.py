"""Independent-draw calibration of the preflight false-positive budget."""

import math
import os

from lockwire_pro.stats.clusters import ALPHA_GATE
from scripts.calibrate_fp_budget import (
    BASELINE_SEED,
    DRAW_SEED,
    FULL_TRIALS,
    calibrate,
    format_result,
    one_sided_binomial_upper,
)
from scripts.measure_stacked_gate import (
    measure_planted_catch,
    measure_stacked_false_alarms,
)

FAST_TRIALS = 32
FAST_SMOKE_CEILING = 0.25
STACKED_SMOKE_SEED = 20260726
STACKED_SMOKE_TRIALS = 2


def test_exact_upper_bound_has_a_hand_computable_zero_success_anchor():
    trials = 2000
    expected = 1.0 - 0.05 ** (1.0 / trials)
    assert math.isclose(one_sided_binomial_upper(0, trials), expected, rel_tol=1e-10)


def test_full_trial_count_is_derived_for_96_percent_detection():
    assert FULL_TRIALS == 2000


def test_seeded_calibration_is_repeatable():
    first = calibrate(trials=2, baseline_seed=BASELINE_SEED, draw_seed=DRAW_SEED)
    second = calibrate(trials=2, baseline_seed=BASELINE_SEED, draw_seed=DRAW_SEED)
    assert first == second


def test_independent_clean_draws_stay_inside_false_positive_budget(capsys):
    """Measure 32 draws by default; only the opt-in N=2000 run can certify alpha=0.005.

    The fast path is a deterministic, real detector measurement that catches gross
    band-tightening and alpha-misallocation mutations. Its 95% upper bound is too wide
    to establish the 0.005 budget. Set LOCKWIRE_FP_FULL=1 for the derived N=2000 run,
    which has 96% detection power against the specified fourfold breach.
    """
    full = os.environ.get("LOCKWIRE_FP_FULL") == "1"
    trials = FULL_TRIALS if full else FAST_TRIALS
    mutation = os.environ.get("LOCKWIRE_FP_MUTATION") or None
    result = calibrate(
        trials=trials,
        baseline_seed=BASELINE_SEED,
        draw_seed=DRAW_SEED,
        mutation=mutation,
    )

    with capsys.disabled():
        print(format_result(result), flush=True)

    if full:
        assert result.upper_bound <= ALPHA_GATE, (
            f"{result.confidence:.0%} upper bound {result.upper_bound:.6f} "
            f"exceeds gate alpha {ALPHA_GATE:.6f}"
        )
    else:
        assert result.proportion <= FAST_SMOKE_CEILING, (
            f"fast smoke measurement {result.proportion:.6f} exceeds "
            f"gross-regression ceiling {FAST_SMOKE_CEILING:.2f}; "
            "run LOCKWIRE_FP_FULL=1 to certify the alpha budget"
        )


def test_stacked_gate_harness_is_seed_deterministic():
    """Smoke: B76 harness reproduces under the accept-line seed (N kept tiny)."""
    kwargs = {
        "trials": STACKED_SMOKE_TRIALS,
        "baseline_seed": STACKED_SMOKE_SEED,
        "draw_seed": STACKED_SMOKE_SEED + 1_000_000,
    }
    first = measure_stacked_false_alarms(**kwargs)
    second = measure_stacked_false_alarms(**kwargs)
    assert first == second

    catch_a = measure_planted_catch(seed=STACKED_SMOKE_SEED)
    catch_b = measure_planted_catch(seed=STACKED_SMOKE_SEED)
    assert catch_a == catch_b
    assert {row.family for row in catch_a} == {
        "cusum",
        "rate-check",
        "hard-limit-exceedance",
        "condition-envelope",
        "drift-cusum",
        "wilks-band-exceedance",
    }
