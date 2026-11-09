"""Cubic residual separation and capture-calibrated slow-drift detection."""

from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import ArrayLike, NDArray


@dataclass(frozen=True, slots=True)
class DetrendedSeries:
    """A cubic least-squares trend and the residual it leaves behind."""

    trend: NDArray[np.float64]
    residual: NDArray[np.float64]


def cubic_detrend(values: ArrayLike) -> DetrendedSeries:
    """Remove a cubic least-squares trend over normalized capture time.

    Normalizing sample positions to ``[-1, 1]`` makes the polynomial fit
    independent of sample rate and capture duration and avoids the poor
    conditioning of powers of large sample indices.
    """
    series = np.asarray(values, dtype=np.float64)
    if series.ndim != 1:
        raise ValueError("values must be one-dimensional")
    if not np.all(np.isfinite(series)):
        raise ValueError("values must contain only finite samples")
    if series.size < 4:
        trend = np.full(series.shape, np.mean(series) if series.size else 0.0)
        return DetrendedSeries(trend=trend, residual=series - trend)

    positions = np.linspace(-1.0, 1.0, series.size)
    coefficients = np.polynomial.polynomial.polyfit(positions, series, 3)
    trend = np.polynomial.polynomial.polyval(positions, coefficients)
    return DetrendedSeries(trend=trend, residual=series - trend)


def trend_cusum_statistic(trend: ArrayLike) -> float:
    """Return the largest normalized two-sided CUSUM of a fitted trend."""
    values = np.asarray(trend, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError("trend must be one-dimensional")
    if not np.all(np.isfinite(values)):
        raise ValueError("trend must contain only finite samples")
    if values.size < 2:
        return 0.0
    cumulative = np.cumsum(values - np.mean(values))[:-1] / values.size
    return float(np.max(np.abs(cumulative)))


def drift_decision_interval(
    *,
    sample_count: int,
    baseline_statistic: float,
    residual_scale: float,
    residual_decorrelation: int,
    alpha_channel: float,
) -> float:
    """Calibrate a capture-level decision interval for the trend CUSUM.

    The cubic fit is a linear projection.  Under §2's Gaussian residual model,
    each cumulative fitted-trend contrast is normal with variance obtained from
    the polynomial design matrix.  A two-sided Bonferroni allocation over all
    prefixes controls the probability of any trip in the capture.  Inflating
    the variance by the measured residual decorrelation length prevents raw
    sample rate from masquerading as independent evidence.
    """
    if sample_count < 4:
        return float("inf")
    if not np.isfinite(baseline_statistic) or baseline_statistic < 0.0:
        raise ValueError("baseline_statistic must be finite and non-negative")
    if not np.isfinite(residual_scale) or residual_scale <= 0.0:
        raise ValueError("residual_scale must be finite and greater than zero")
    if residual_decorrelation < 1:
        raise ValueError("residual_decorrelation must be at least one")
    if not np.isfinite(alpha_channel) or not 0.0 < alpha_channel < 1.0:
        raise ValueError("alpha_channel must be finite and between zero and one")

    positions = np.linspace(-1.0, 1.0, sample_count)
    design = np.polynomial.polynomial.polyvander(positions, 3)
    centered_design = design - np.mean(design, axis=0)
    cumulative_design = np.cumsum(centered_design, axis=0)[:-1]
    covariance = np.linalg.inv(design.T @ design)
    leverage = np.einsum(
        "ij,jk,ik->i",
        cumulative_design,
        covariance,
        cumulative_design,
    )
    maximum_standard_error = (
        residual_scale * np.sqrt(residual_decorrelation * float(np.max(leverage))) / sample_count
    )
    looks = sample_count - 1
    critical_z = NormalDist().inv_cdf(1.0 - alpha_channel / (2.0 * looks))
    return float(baseline_statistic + critical_z * maximum_standard_error)
