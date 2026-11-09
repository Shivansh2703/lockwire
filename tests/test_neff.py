import numpy as np
import pytest

from lockwire_pro.stats.neff import (
    autocorrelation,
    decorrelation_length,
    effective_sample_size,
)


def test_autocorrelation_known_answer() -> None:
    # For [1, 2, 3], deviations are [-1, 0, 1] and their squared sum is 2:
    # rho_1 = 0 / 2 and rho_2 = -1 / 2. This is external hand arithmetic.
    actual = autocorrelation([1.0, 2.0, 3.0])
    # Bites mean-centering, lag indexing, and per-lag-denominator mutations.
    assert np.allclose(actual, [1.0, 0.0, -0.5], atol=1e-12)
    # Negative control: a stub returning zero correlations cannot pass.
    assert actual[2] < -0.4  # Bites an implementation that discards negative rho.


def test_white_noise_effective_count_is_close_to_raw_count() -> None:
    rng = np.random.default_rng(20260725)
    samples = rng.normal(size=20_000)

    n_eff = effective_sample_size(samples)

    # Independent white noise has theoretical rho_k=0 and N_eff=N. A 5% band is
    # much wider than O(1/sqrt(N)) sampling noise, but bites a spurious long-lag sum.
    assert n_eff >= 0.95 * samples.size
    # Bites summing past the first non-positive rho and claiming extra evidence.
    assert n_eff <= samples.size


def test_ar1_effective_count_matches_closed_form() -> None:
    rng = np.random.default_rng(731)
    phi = 0.8
    innovations = rng.normal(size=52_000)
    process = np.empty_like(innovations)
    process[0] = innovations[0] / np.sqrt(1.0 - phi**2)
    for index in range(1, process.size):
        process[index] = phi * process[index - 1] + innovations[index]
    samples = process[2_000:]  # Remove any residual dependence on initialization.

    n_eff = effective_sample_size(samples)
    # An AR(1) has rho_k=phi^k, so tau=1+2*sum(phi^k)
    # = (1+phi)/(1-phi)=9 and asymptotic N_eff=N/9=5555.56.
    expected = samples.size * (1.0 - phi) / (1.0 + phi)
    # 12% allows seeded finite-sample estimation error while biting missing factor
    # 2, unweighted covariance, wrong truncation, and raw-N implementations.
    assert n_eff == pytest.approx(expected, rel=0.12)
    # Negative control: this must fail for the tempting implementation N_eff=N.
    assert n_eff < samples.size / 4
    # The same closed form implies a nine-sample decorrelation length; this band
    # bites returning N_eff itself, sqrt(tau), or a fixed stride of one.
    assert 7 <= decorrelation_length(samples) <= 12


def test_empty_singleton_and_constant_series() -> None:
    empty = np.asarray([], dtype=float)
    # Bites indexing lag zero on an empty input.
    assert autocorrelation(empty).size == 0
    # Bites division by zero for N=0.
    assert effective_sample_size(empty) == 0.0
    # Bites inventing a positive spacing for a recording with no samples.
    assert decorrelation_length(empty) == 0

    # Bites a formula that requires at least one non-zero lag.
    assert effective_sample_size([4.0]) == 1.0
    # Bites an off-by-one that returns zero spacing for one observation.
    assert decorrelation_length([4.0]) == 1

    constant = np.full(4, 7.0)
    # Bites dividing zero lag covariance by zero variance.
    assert np.array_equal(autocorrelation(constant), np.ones(4))
    # Bites treating repeated identical readings as four independent observations.
    assert effective_sample_size(constant) == pytest.approx(1.0)
    # Bites deriving a unit stride from the undefined constant-series correlation.
    assert decorrelation_length(constant) == 4
