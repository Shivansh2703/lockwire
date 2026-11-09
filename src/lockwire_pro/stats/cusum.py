"""One-sided CUSUM detection for sensors that keep publishing but stop moving."""

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike


@dataclass(frozen=True, slots=True)
class CusumResult:
    """The decision and the CUSUM state at that decision (or at end of input)."""

    tripped: bool
    trip_index: int | None
    statistic: float


@dataclass(frozen=True, slots=True)
class CusumCalibration:
    """A frozen threshold and its false-alarm rate on a held-out simulation."""

    decision_interval: float
    achieved_alpha: float
    hard_freeze_arl1_windows: int


def _validate_positive_finite(value: float, name: str) -> None:
    if not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _validate_positive_int(value: int, name: str, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else f"at least {minimum}"
        raise ValueError(f"{name} must be an integer of {qualifier}")


def _monte_carlo_capture_maxima(
    *,
    variance_ratio: float,
    window_size: int,
    capture_windows: int,
    trials: int,
    seed: int,
) -> np.ndarray:
    """Simulate each healthy capture's maximum normalized CUSUM statistic."""
    rng = np.random.default_rng(seed)
    statistics = np.zeros(trials)
    maxima = np.zeros(trials)
    for _ in range(capture_windows):
        # For iid Gaussian samples, n * population_variance / sigma**2 is
        # chi-square(n - 1). Work in units where variance_floor == 1.
        window_variances = (
            variance_ratio / window_size * rng.chisquare(window_size - 1, size=trials)
        )
        statistics = np.maximum(
            0.0,
            statistics + 1.0 - window_variances,
        )
        maxima = np.maximum(maxima, statistics)
    return maxima


def calibrate_decision_interval(
    variance_floor: float,
    *,
    in_control_variance: float,
    window_size: int,
    capture_windows: int,
    alpha_channel: float,
    trials: int = 50_000,
    seed: int = 0xB7C05,
    validation_trials: int | None = None,
    validation_seed: int = 0xA11CE,
    measurement_seed: int = 0xC0FFEE,
) -> CusumCalibration:
    """Calibrate a capture-level variance-CUSUM threshold by seeded Monte Carlo.

    The in-control model is iid Gaussian with variance ``in_control_variance``.
    Each simulated window is passed through the same population-variance
    (``ddof=0``) statistic used by :func:`variance_cusum`. The decision interval
    is the empirical upper-tail cutoff of the maximum CUSUM statistic reached
    anywhere in ``capture_windows``. Thus the simulated probability of at least
    one trip in the whole capture is at most ``alpha_channel``.

    A second simulation under ``validation_seed`` freezes the initial cutoff,
    raising it if necessary to leave a three-standard-error margin below
    ``alpha_channel``. A third, held-out simulation under ``measurement_seed``
    measures ``achieved_alpha`` without changing that frozen cutoff. All three
    seeds must differ so the reported false-alarm rate is independent of the
    draws that selected the decision interval.
    ``hard_freeze_arl1_windows`` reports detection speed after an exactly
    constant sensor begins; it is a property of the calibrated threshold, not a
    calibration target.

    This function is a baseline/calibration operation, not part of the live
    check path. Store the returned ``decision_interval`` and pass that value to
    :func:`variance_cusum`; the live check is then entirely deterministic.
    Calibration is model-specific: changing the healthy variance, window size,
    healthy distribution, capture length, or channel allocation requires
    recalibration. In particular, do not substitute a tabular mean-CUSUM
    approximation (including Siegmund's) for this variance-CUSUM simulation.
    """
    _validate_positive_finite(variance_floor, "variance_floor")
    _validate_positive_finite(in_control_variance, "in_control_variance")
    if not np.isfinite(alpha_channel) or not 0.0 < alpha_channel < 1.0:
        raise ValueError("alpha_channel must be finite and between zero and one")
    _validate_positive_int(window_size, "window_size", minimum=2)
    _validate_positive_int(capture_windows, "capture_windows")
    _validate_positive_int(trials, "trials", minimum=2)
    if validation_trials is None:
        validation_trials = trials
    _validate_positive_int(validation_trials, "validation_trials", minimum=2)
    if len({seed, validation_seed, measurement_seed}) != 3:
        raise ValueError("seed, validation_seed, and measurement_seed must differ")

    minimum_tail_observations = 20
    allowed_calibration_trips = int(np.floor(alpha_channel * trials))
    expected_validation_trips = int(np.floor(alpha_channel * validation_trials))
    if allowed_calibration_trips < minimum_tail_observations:
        raise ValueError("trials must provide at least 20 alpha_channel tail observations")
    if expected_validation_trips < minimum_tail_observations:
        raise ValueError("validation_trials must provide at least 20 expected alpha_channel trips")
    variance_ratio = in_control_variance / variance_floor
    calibration_maxima = _monte_carlo_capture_maxima(
        variance_ratio=variance_ratio,
        window_size=window_size,
        capture_windows=capture_windows,
        trials=trials,
        seed=seed,
    )
    cutoff_index = trials - allowed_calibration_trips
    threshold_ratio = float(np.partition(calibration_maxima, cutoff_index)[cutoff_index])

    validation_maxima = _monte_carlo_capture_maxima(
        variance_ratio=variance_ratio,
        window_size=window_size,
        capture_windows=capture_windows,
        trials=validation_trials,
        seed=validation_seed,
    )

    validation_margin = int(
        np.ceil(3.0 * np.sqrt(alpha_channel * (1.0 - alpha_channel) * validation_trials))
    )
    validated_trip_limit = expected_validation_trips - validation_margin
    validation_cutoff_index = validation_trials - validated_trip_limit - 1
    validated_threshold_ratio = np.nextafter(
        float(np.partition(validation_maxima, validation_cutoff_index)[validation_cutoff_index]),
        np.inf,
    )
    threshold_ratio = max(threshold_ratio, validated_threshold_ratio)

    measurement_maxima = _monte_carlo_capture_maxima(
        variance_ratio=variance_ratio,
        window_size=window_size,
        capture_windows=capture_windows,
        trials=validation_trials,
        seed=measurement_seed,
    )
    achieved_alpha = float(np.mean(measurement_maxima >= threshold_ratio))
    decision_interval = float(variance_floor * threshold_ratio)

    return CusumCalibration(
        decision_interval=decision_interval,
        achieved_alpha=achieved_alpha,
        hard_freeze_arl1_windows=int(np.ceil(threshold_ratio)),
    )


def variance_cusum(
    samples: ArrayLike,
    variance_floor: float,
    *,
    window_size: int,
    decision_interval: float,
) -> CusumResult:
    """Detect sustained sub-floor variance in non-overlapping sample windows.

    Each complete window contributes ``variance_floor - variance`` to a
    downward, one-sided CUSUM, reset at zero. Thus a single quiet window is not
    a failure: only accumulated evidence above the supplied, previously
    calibrated ``decision_interval`` trips the detector. Population variance
    (``ddof=0``) is used because each window is the complete observed interval,
    rather than an estimate consumed elsewhere.

    Use :func:`calibrate_decision_interval` during baseline generation, then
    persist its result. This function performs no simulation and is
    deterministic for fixed inputs.

    ``trip_index`` is the zero-based index of the last raw sample in the window
    that crossed ``decision_interval``. Incomplete trailing windows carry no
    variance evidence.
    """
    if isinstance(window_size, bool) or not isinstance(window_size, int) or window_size < 2:
        raise ValueError("window_size must be an integer of at least two")
    _validate_positive_finite(variance_floor, "variance_floor")
    _validate_positive_finite(decision_interval, "decision_interval")

    values = np.asarray(samples, dtype=float)
    if values.ndim != 1:
        raise ValueError("samples must be one-dimensional")
    if not np.all(np.isfinite(values)):
        raise ValueError("samples must contain only finite values")

    statistic = 0.0
    complete_length = values.size - values.size % window_size
    for start in range(0, complete_length, window_size):
        variance = float(np.var(values[start : start + window_size]))
        statistic = max(0.0, statistic + variance_floor - variance)
        if statistic >= decision_interval:
            return CusumResult(
                tripped=True,
                trip_index=start + window_size - 1,
                statistic=statistic,
            )

    return CusumResult(tripped=False, trip_index=None, statistic=statistic)
