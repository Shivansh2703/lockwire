"""Known-answer and capture-level Monte Carlo tests for variance CUSUM detection."""

import numpy as np
import pytest

from lockwire_pro.stats.cusum import calibrate_decision_interval, variance_cusum


def _independent_gaussian_trip_rate(
    decision_interval,
    *,
    variance_floor,
    in_control_variance,
    window_size,
    capture_windows,
    trials,
    seed,
):
    """Independently simulate full captures from raw healthy Gaussian samples."""
    rng = np.random.default_rng(seed)
    statistics = np.zeros(trials)
    tripped = np.zeros(trials, dtype=bool)

    for _ in range(capture_windows):
        healthy = rng.normal(
            scale=np.sqrt(in_control_variance),
            size=(trials, window_size),
        )
        window_variances = np.var(healthy, axis=1)
        statistics = np.maximum(
            0.0,
            statistics + variance_floor - window_variances,
        )
        tripped |= statistics >= decision_interval

    return float(np.mean(tripped))


def _assert_rates_match_allocation(rates, allocated_alpha, tolerance):
    for rate in rates:
        assert rate == pytest.approx(allocated_alpha, abs=tolerance)


def test_capture_false_alarm_rate_is_invariant_to_sample_rate_and_capture_length():
    alpha_channel = 0.0025
    calibration_trials = 20_000
    validation_trials = 20_000
    intervals = {}

    for capture_windows in (75, 1_500):
        calibration = calibrate_decision_interval(
            variance_floor=0.5,
            in_control_variance=1.0,
            window_size=4,
            capture_windows=capture_windows,
            alpha_channel=alpha_channel,
            trials=calibration_trials,
            seed=0xB7C05,
            validation_trials=validation_trials,
            validation_seed=0xA11CE,
            measurement_seed=0xC0FFEE,
        )
        intervals[capture_windows] = calibration.decision_interval

        # This validation uses raw Gaussian windows, a fourth seed, and no
        # calibration internals. The tolerance combines the binomial standard
        # errors of threshold calibration and independent rate validation.
        # Bites calibrating sequential ARL0 instead of whole-capture alpha.
        achieved_alpha = _independent_gaussian_trip_rate(
            calibration.decision_interval,
            variance_floor=0.5,
            in_control_variance=1.0,
            window_size=4,
            capture_windows=capture_windows,
            trials=validation_trials,
            seed=0xD15EA5E,
        )
        combined_se = np.sqrt(
            alpha_channel
            * (1.0 - alpha_channel)
            * (1.0 / calibration_trials + 1.0 / validation_trials)
        )

        _assert_rates_match_allocation(
            (achieved_alpha, calibration.achieved_alpha),
            alpha_channel,
            4.0 * combined_se,
        )
        assert calibration.hard_freeze_arl1_windows == int(
            np.ceil(calibration.decision_interval / 0.5)
        )  # Bites tuning h against ARL1 instead of merely reporting it.

    assert intervals[1_500] > intervals[75]  # Bites ignoring the number of looks.

    # Mutation bite: using the 75-window threshold for a 1,500-window capture
    # breaks capture-level alpha, and the same invariance assertion rejects it.
    broken_long_rate = _independent_gaussian_trip_rate(
        intervals[75],
        variance_floor=0.5,
        in_control_variance=1.0,
        window_size=4,
        capture_windows=1_500,
        trials=validation_trials,
        seed=0xBAD5EED,
    )
    with pytest.raises(AssertionError):
        _assert_rates_match_allocation(
            (broken_long_rate,),
            alpha_channel,
            4.0 * combined_se,
        )


@pytest.mark.parametrize("capture_windows", [75, 1_500])
@pytest.mark.parametrize(
    ("seed", "validation_seed", "measurement_seed"),
    [
        (101, 201, 301),
        (102, 202, 302),
        (103, 203, 303),
        (104, 204, 304),
        (105, 205, 305),
    ],
)
def test_calibrated_achieved_alpha_never_exceeds_allocation(
    capture_windows,
    seed,
    validation_seed,
    measurement_seed,
):
    allocated_alpha = 0.005

    calibration = calibrate_decision_interval(
        variance_floor=0.5,
        in_control_variance=1.0,
        window_size=4,
        capture_windows=capture_windows,
        alpha_channel=allocated_alpha,
        trials=4_000,
        seed=seed,
        validation_trials=4_000,
        validation_seed=validation_seed,
        measurement_seed=measurement_seed,
    )

    assert calibration.achieved_alpha <= allocated_alpha


def test_achieved_alpha_varies_with_held_out_measurement_draw():
    achieved = []
    intervals = []

    for measurement_seed in (301, 303, 305):
        calibration = calibrate_decision_interval(
            variance_floor=0.5,
            in_control_variance=1.0,
            window_size=4,
            capture_windows=75,
            alpha_channel=0.005,
            trials=4_000,
            seed=101,
            validation_trials=4_000,
            validation_seed=201,
            measurement_seed=measurement_seed,
        )
        achieved.append(calibration.achieved_alpha)
        intervals.append(calibration.decision_interval)

    assert len(set(intervals)) == 1
    assert len(set(achieved)) == 3
    assert max(achieved) <= 0.005


def test_known_answer_cusum_walk_and_trip_index():
    samples = [-2.0, 2.0, -1.0, 1.0] + [0.0] * 10

    # Window variances are [4, 1, 0, 0, 0, 0, 0], so with floor 2 the states
    # are [0, 1, 3, 5, 7, 9, 11]. The supplied h=10.5 crosses at index 13.
    result = variance_cusum(
        samples,
        variance_floor=2.0,
        window_size=2,
        decision_interval=10.5,
    )

    assert result.tripped is True  # Bites a reversed deficit (variance - floor).
    assert result.trip_index == 13  # Bites reporting a window number, start, or one-based index.
    assert result.statistic == pytest.approx(11.0)  # Bites failure to reset negative evidence.


def test_healthy_quiet_channel_stays_silent():
    samples = np.tile([-0.2, 0.2], 20)

    result = variance_cusum(
        samples,
        variance_floor=0.02,
        window_size=2,
        decision_interval=0.1,
    )

    assert result.tripped is False  # Bites reversing the sub-floor comparison.
    assert result.trip_index is None  # Bites manufacturing a trip location on a pass.
    assert result.statistic == pytest.approx(0.0)  # Bites accumulating above-floor evidence.


def test_hard_freeze_trips_after_healthy_prefix():
    samples = np.concatenate([np.tile([-1.0, 1.0], 4), np.zeros(12)])

    result = variance_cusum(
        samples,
        variance_floor=0.05,
        window_size=2,
        decision_interval=0.25,
    )

    assert result.tripped is True  # Bites a detector that misses exactly-zero variance.
    assert result.trip_index == 17  # Bites off-by-one errors across the healthy prefix.
    assert result.statistic == pytest.approx(0.25)  # Bites returning a fabricated walk state.


def test_slow_freeze_accumulates_sub_floor_evidence():
    amplitudes = [0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0]
    samples = np.array([value for amplitude in amplitudes for value in (-amplitude, amplitude)])

    result = variance_cusum(
        samples,
        variance_floor=0.05,
        window_size=2,
        decision_interval=0.25,
    )

    assert result.tripped is True  # Bites replacing accumulation with a one-window decision.
    assert result.trip_index == 21  # Bites firing early before cumulative evidence reaches h.
    assert result.statistic == pytest.approx(0.275)  # Bites discarding gradual deficits.


@pytest.mark.parametrize(
    ("samples", "window_size"),
    [
        ([], 2),
        ([1.0], 2),
        ([1.0, 2.0, 3.0], 4),
    ],
)
def test_inputs_without_a_complete_window_are_inconclusive(samples, window_size):
    result = variance_cusum(
        samples,
        variance_floor=0.1,
        window_size=window_size,
        decision_interval=0.5,
    )

    assert result.tripped is False  # Bites treating missing variance evidence as a freeze.
    assert result.trip_index is None  # Bites indexing an incomplete or absent window.
    assert result.statistic == pytest.approx(0.0)  # Bites consuming a trailing partial window.


def test_all_identical_values_trip_when_there_is_enough_evidence():
    result = variance_cusum(
        [3.0] * 12,
        variance_floor=1.0,
        window_size=2,
        decision_interval=5.5,
    )

    assert result.tripped is True  # Bites special-casing constant data as inconclusive.
    assert result.trip_index == 11  # Bites crossing on > rather than >=.
    assert result.statistic == pytest.approx(6.0)  # Bites failure to accumulate zero variance.
