"""Seeded Monte Carlo calibration for the preflight false-positive budget."""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

from lockwire_pro.baseline import BaselineArtifact, build_baseline, check_recording
from lockwire.schema import Band
from lockwire_pro.stats.clusters import ALPHA_GATE

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "tests" / "fixtures" / "bags" / "generate.py"
BASELINE_SEED = 20260725
DRAW_SEED = 20261725
CONFIDENCE = 0.95
TARGET_DETECTION_POWER = 0.96
BREACH_EXPECTED_TRIPS_AT_2000 = 3.22


def _derive_full_trials() -> int:
    breach_trip_probability = BREACH_EXPECTED_TRIPS_AT_2000 / 2000
    minimum = math.ceil(math.log1p(-TARGET_DETECTION_POWER) / math.log1p(-breach_trip_probability))
    return math.ceil(minimum / 100) * 100


FULL_TRIALS = _derive_full_trials()


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    trials: int
    false_positives: int
    total_findings: int
    proportion: float
    confidence: float
    upper_bound: float


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lockwire_bag_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import generator at {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _log_binomial_term(n: int, k: int, probability: float) -> float:
    if probability == 0.0:
        return 0.0 if k == 0 else -math.inf
    if probability == 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(probability)
        + (n - k) * math.log1p(-probability)
    )


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    terms = [_log_binomial_term(trials, k, probability) for k in range(successes + 1)]
    pivot = max(terms)
    return math.exp(pivot) * sum(math.exp(term - pivot) for term in terms)


def one_sided_binomial_upper(
    successes: int,
    trials: int,
    *,
    confidence: float = CONFIDENCE,
) -> float:
    """Exact Clopper-Pearson upper bound, evaluated in log space."""
    if not 0 <= successes <= trials or trials <= 0:
        raise ValueError("require 0 <= successes <= trials and trials > 0")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between zero and one")
    if successes == trials:
        return 1.0

    target = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(80):
        candidate = (low + high) / 2.0
        if _binomial_cdf(successes, trials, candidate) > target:
            low = candidate
        else:
            high = candidate
    return high


def _mutate_baseline(baseline: BaselineArtifact, mutation: str | None) -> BaselineArtifact:
    if mutation is None:
        return baseline
    profiles = {}
    for channel_id, profile in baseline.profiles.items():
        if mutation == "tight-band" and profile.band is not None:
            midpoint = (profile.band.lo + profile.band.hi) / 2.0
            half_width = (profile.band.hi - profile.band.lo) * 0.005
            profile = replace(
                profile,
                band=Band(
                    lo=midpoint - half_width,
                    hi=midpoint + half_width,
                    unit=profile.band.unit,
                ),
            )
        elif mutation == "misallocated-alpha":
            profile = replace(profile, alpha_channel=0.5)
        profiles[channel_id] = profile
    return replace(baseline, profiles=profiles)


def calibrate(
    *,
    trials: int = FULL_TRIALS,
    baseline_seed: int = BASELINE_SEED,
    draw_seed: int = DRAW_SEED,
    mutation: str | None = None,
) -> CalibrationResult:
    """Measure clean-preflight false positives over independent seeded draws."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    if baseline_seed >= draw_seed and baseline_seed < draw_seed + trials:
        raise ValueError("baseline seed must be independent of all draw seeds")

    generator = _load_generator()
    baseline_recording = generator.clean_recording(baseline_seed)
    baseline = _mutate_baseline(
        build_baseline(baseline_recording, context=baseline_recording.context),
        mutation,
    )

    false_positives = 0
    total_findings = 0
    for seed in range(draw_seed, draw_seed + trials):
        findings = check_recording(generator.clean_recording(seed), baseline)
        total_findings += len(findings)
        false_positives += bool(findings)

    return CalibrationResult(
        trials=trials,
        false_positives=false_positives,
        total_findings=total_findings,
        proportion=false_positives / trials,
        confidence=CONFIDENCE,
        upper_bound=one_sided_binomial_upper(false_positives, trials),
    )


def format_result(result: CalibrationResult) -> str:
    breach_trip_probability = BREACH_EXPECTED_TRIPS_AT_2000 / 2000
    detection_power = 1.0 - math.exp(result.trials * math.log1p(-breach_trip_probability))
    return (
        f"measured clean false-positive proportion: {result.false_positives}/{result.trials} "
        f"= {result.proportion:.6f}; {result.confidence:.0%} one-sided exact binomial CI "
        f"[0.000000, {result.upper_bound:.6f}]; gate alpha={ALPHA_GATE:.6f}; "
        f"findings={result.total_findings}; fourfold-breach detection={detection_power:.1%}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=FULL_TRIALS)
    parser.add_argument("--baseline-seed", type=int, default=BASELINE_SEED)
    parser.add_argument("--draw-seed", type=int, default=DRAW_SEED)
    args = parser.parse_args()
    result = calibrate(
        trials=args.trials,
        baseline_seed=args.baseline_seed,
        draw_seed=args.draw_seed,
    )
    print(format_result(result))
    return int(result.upper_bound > ALPHA_GATE)


if __name__ == "__main__":
    raise SystemExit(main())
