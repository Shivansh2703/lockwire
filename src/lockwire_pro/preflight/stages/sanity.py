"""Stage 3 — signal sanity: NaN / range / one-shot variance.

NaN safety is the ``isfinite`` guard, defended by accumulate-don't-short-circuit
across legs: findings are collected into one list, so an empty range result
cannot skip a sibling refusal. ``nan < lo`` and ``nan > hi`` are both False in
IEEE/Python — without the guard a non-finite sample reads as in-range.

Range operand: ``SignalEnvelope.band`` for the recording's context, and
``DeviceModel.hard_limits`` when declared. Hard limits apply without a context
match (pre-existing ``DeviceModel`` data); envelope bands do not — a declared
envelope for another context is disclosed as ``not_scored``, never silent.

Variance operand: ``SignalEnvelope.oneshot_variance_min`` only — compared
against the capture's *minimum sliding-window* population variance (window
``ONESHOT_VARIANCE_WINDOW``), not whole-capture variance. Whole-capture
variance is dominated by the live/held offset of a freeze and is non-monotonic
in fault severity; a dead channel's giveaway is near-zero variance *inside a
window*. ``variance_floor`` remains the CUSUM reference value
(``docs/stats_design.md``) and is never consulted here. Undeclared
``oneshot_variance_min`` → ``not_scored``, never a substitute (D3).
"""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from lockwire_pro.baseline import _converted, _outside_hard_limit, budgeted_exceedances
from lockwire.checks import _ScoreBook
from lockwire_pro.preflight.envelopes import signal_view_for_context
from lockwire_pro.preflight.source import PreflightSource
from lockwire_pro.recording import Recording
from lockwire.report import Finding, format_magnitude
from lockwire.schema import (
    Band,
    Contract,
    DeviceInstance,
    FittedBandEvidence,
    Quantity,
    SignalEnvelope,
)
from lockwire.units import UnitError

IMPLEMENTED = True

# Declared window for the one-shot min-variance statistic. Same length as
# baseline.CUSUM_WINDOW_SIZE: sustained quiet is a windowed phenomenon there
# too (docs/stats_design.md §4). Kept as a local binding so the stage-3
# operand stays explicit in findings and tests.
ONESHOT_VARIANCE_WINDOW = 20


def min_windowed_population_variance(
    values: np.ndarray, *, window: int = ONESHOT_VARIANCE_WINDOW
) -> float:
    """Minimum population variance (ddof=0) over sliding windows of ``window``.

    A frozen-then-held channel has near-zero variance inside any window that
    sits entirely on the hold, regardless of where the held value sits relative
    to the live-segment mean. Whole-capture variance does not have that
    property and must not be used here.
    """
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError("window must be an integer of at least two")
    flat = np.asarray(values, dtype=float)
    if flat.ndim != 1:
        raise ValueError("values must be one-dimensional")
    if flat.size < window:
        raise ValueError(f"need at least {window} samples, got {flat.size}")
    windows = sliding_window_view(flat, window)
    return float(np.min(np.var(windows, axis=1)))


def check(
    contract: Contract, source: PreflightSource, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Score declared signal channels against envelope band / hard limits / oneshot variance."""
    recording = source.samples()
    findings: list[Finding] = []
    for device in contract.devices:
        findings.extend(_check_device(contract, device, recording, scores=scores))
    return findings


def _check_device(
    contract: Contract,
    device: DeviceInstance,
    recording: Recording,
    *,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    model = contract.device_model(device.model)
    hard_limits = model.hard_limits if model is not None else {}
    signal_view = signal_view_for_context(device, recording.context)
    envelope_signals = signal_view.signals

    declared_other = sorted(
        {
            env.context
            for env in device.envelopes
            if env.context != recording.context and env.signals
        }
    )

    channels = sorted(set(hard_limits) | set(envelope_signals))
    out: list[Finding] = []

    # Declared monitoring for another context + no match for the recording:
    # absence of output would read as checked-and-fine (false GO). Disclose.
    if declared_other and not signal_view.envelopes:
        declared = ", ".join(declared_other)
        hard_note = (
            " Hard limits still apply without a context match; envelope bands do not."
            if hard_limits
            else ""
        )
        out.append(
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/signals",
                observed=(
                    f"signal envelopes declared for {declared}; "
                    f"recording context is {recording.context!r}"
                ),
                expected=(f"a ContextEnvelope matching recording context {recording.context!r}"),
                owner=device.owner,
                action=(
                    "record under a declared envelope context, or declare an envelope "
                    f"for {recording.context!r}.{hard_note}"
                ),
            )
        )

    if not channels:
        return out

    for channel in channels:
        declaration = envelope_signals.get(channel)
        out.extend(
            _check_channel(
                device=device,
                channel=channel,
                recording=recording,
                signal=declaration.signal if declaration is not None else None,
                fitted=declaration.fitted if declaration is not None else None,
                hard_limit=hard_limits.get(channel),
                scores=scores,
            )
        )
    return out


def _check_channel(
    *,
    device: DeviceInstance,
    channel: str,
    recording: Recording,
    signal: SignalEnvelope | None,
    fitted: FittedBandEvidence | None,
    hard_limit: Band | None,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """One channel: NaN first, then range, then variance. Every gap is named."""
    quantity_base = f"devices/{device.id}/signals/{channel}"
    stream = recording.devices.get(device.id)
    samples = stream.signals.get(channel) if stream is not None else None

    if samples is None:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity_base,
                observed="signal not present in preflight samples",
                expected="samples available to score signal sanity",
                owner=device.owner,
                action="provide samples for this declared channel or drop the declaration",
            )
        ]

    raw = np.asarray(samples, dtype=float)
    if raw.size == 0:  # L35: nothing to score; same not_scored shape as a missing signal
        return [
            Finding(
                severity="not_scored",
                quantity=quantity_base,
                observed="0 sample(s)",
                expected="samples available to score signal sanity",
                owner=device.owner,
                action="capture samples for this declared channel before scoring",
            )
        ]
    findings: list[Finding] = []

    # Non-finite samples are a hard no-go; without this guard they read in-range.
    if np.any(~np.isfinite(raw)):
        findings.append(
            Finding(
                severity="violation",
                quantity=f"{quantity_base}/nan",
                observed="non-finite sample (NaN or Inf)",
                expected="all samples finite",
                owner=device.owner,
                action="fix the producer — non-finite samples are a hard no-go",
            )
        )
        return findings

    # Range and variance accumulate — neither leg's empty result skips the other.
    if signal is not None:
        findings.extend(
            _range_findings(
                device=device,
                quantity=f"{quantity_base}/range",
                raw=raw,
                recording=recording,
                channel=channel,
                limit=signal.band,
                role="envelope band",
                fitted=fitted,
                scores=scores,
            )
        )
    if hard_limit is not None:
        findings.extend(
            _range_findings(
                device=device,
                quantity=f"{quantity_base}/hard_limit",
                raw=raw,
                recording=recording,
                channel=channel,
                limit=hard_limit,
                role="hard limit",
                scores=scores,
            )
        )

    findings.extend(
        _variance_leg(
            device=device,
            quantity=f"{quantity_base}/variance",
            raw=raw,
            recording=recording,
            channel=channel,
            signal=signal,
            scores=scores,
        )
    )

    return findings


def _variance_leg(
    *,
    device: DeviceInstance,
    quantity: str,
    raw: np.ndarray,
    recording: Recording,
    channel: str,
    signal: SignalEnvelope | None,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """Score ``oneshot_variance_min`` only; never read ``variance_floor`` (CUSUM)."""
    floor = signal.oneshot_variance_min if signal is not None else None
    if floor is None:
        cusum_note = (
            "; SignalEnvelope.variance_floor is the CUSUM reference value "
            "(docs/stats_design.md), not a one-shot preflight threshold"
            if signal is not None and signal.variance_floor is not None
            else ""
        )
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"oneshot_variance_min undeclared{cusum_note}",
                expected="SignalEnvelope.oneshot_variance_min declared for this context",
                owner=device.owner,
                action=(
                    "declare oneshot_variance_min on the context envelope — "
                    "do not treat variance_floor as a preflight pass/fail threshold"
                ),
            )
        ]

    window = ONESHOT_VARIANCE_WINDOW
    if raw.size < window:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"{raw.size} sample(s)",
                expected=f"at least {window} samples to score min windowed variance",
                owner=device.owner,
                action="capture a longer window before scoring oneshot_variance_min",
            )
        ]

    if not math.isfinite(floor) or floor < 0.0:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"oneshot_variance_min={floor!r}",
                expected="a finite, non-negative declared oneshot_variance_min",
                owner=device.owner,
                action="fix the declared oneshot_variance_min — refusing to invent a substitute",
            )
        ]

    try:
        from_unit = recording.unit_of(channel, device=device.id)
        values = _converted(raw, from_unit, signal.band.unit)
    except (KeyError, UnitError) as error:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"unit conversion failed: {error}",
                expected=f"samples convertible to envelope band unit {signal.band.unit!r}",
                owner=device.owner,
                action="align recording units with the declared envelope band",
            )
        ]

    # Local quiet, not whole-capture variance: a freeze's giveaway is a window
    # of near-zero variance, independent of the live/held offset.
    observed_var = min_windowed_population_variance(values, window=window)
    if observed_var >= floor:
        if scores is not None:
            scores.passed(
                quantity,
                observed=f"min windowed variance (window={window}) {observed_var:.6g}",
                expected=f">= {floor:.6g} declared oneshot_variance_min",
                margin=f"{observed_var - floor:.6g} above the declared floor",
            )
        return []

    # Typed magnitudes left unset: units.py has no squared dimension for the
    # variance of a physical signal (B83 firewall). Same move as voltage-compat
    # leaving expected_quantity unset rather than fabricating a scalar unit.
    return [
        Finding(
            severity="violation",
            quantity=quantity,
            observed=f"min windowed variance (window={window}) {observed_var:.6g}",
            expected=f">= {floor:.6g} declared oneshot_variance_min",
            delta=f"short by {floor - observed_var:.6g}",
            owner=device.owner,
            action=(
                "sensor appears frozen/stuck — min windowed variance below "
                "declared oneshot_variance_min"
            ),
        )
    ]


def _range_findings(
    *,
    device: DeviceInstance,
    quantity: str,
    raw: np.ndarray,
    recording: Recording,
    channel: str,
    limit: Band,
    role: str,
    fitted: FittedBandEvidence | None = None,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    try:
        from_unit = recording.unit_of(channel, device=device.id)
        values = _converted(raw, from_unit, limit.unit)
    except (KeyError, UnitError) as error:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed=f"unit conversion failed: {error}",
                expected=f"samples convertible to {role} unit {limit.unit!r}",
                owner=device.owner,
                action="align recording units with the declared band/hard limit",
            )
        ]

    if fitted is not None:
        fitted_findings = [
            Finding(
                severity="violation",
                quantity=quantity,
                observed=decision.summary(),
                # A crossed-bound refusal like the hard-limit branch below,
                # same hand-built limit text, so it quotes the declared `why` too.
                expected=(
                    f"fitted-band exceedance count below k* within {role} "
                    f"[{format_magnitude(limit.lo, limit.unit)}, "
                    f"{format_magnitude(limit.hi, limit.unit)}]{limit.why_suffix}"
                ),
                observed_quantity=Quantity(value=decision.observed_value, unit=limit.unit),
                expected_quantity=Quantity(value=decision.bound, unit=limit.unit),
                delta=(
                    f"{decision.side} bound by "
                    f"{format_magnitude(abs(decision.observed_value - decision.bound), limit.unit)}"
                ),
                owner=device.owner,
                action=(
                    f"reconcile the signal distribution with the declared {role}; "
                    "excursion pattern is diagnostic only"
                ),
            )
            for decision in budgeted_exceedances(
                values,
                limit,
                fitted,
                detrend=True,
                raw_level=True,
            )
        ]
        if not fitted_findings and scores is not None:
            scores.passed(
                quantity,
                observed="no fitted-band exceedances",
                expected=(
                    f"fitted-band exceedance count below k* within {role} "
                    f"[{format_magnitude(limit.lo, limit.unit)}, "
                    f"{format_magnitude(limit.hi, limit.unit)}]"
                ),
                margin="clean — no excursion budget spent",
            )
        return fitted_findings

    if not _outside_hard_limit(values, limit):
        if scores is not None:
            lower_margin = values - limit.lo
            upper_margin = limit.hi - values
            tightest = int(np.argmin(np.minimum(lower_margin, upper_margin)))
            tight_value = float(values[tightest])
            if lower_margin[tightest] <= upper_margin[tightest]:
                bound, margin_val = limit.lo, float(lower_margin[tightest])
            else:
                bound, margin_val = limit.hi, float(upper_margin[tightest])
            scores.passed(
                quantity,
                observed=format_magnitude(tight_value, limit.unit),
                expected=(
                    f"within {role} [{format_magnitude(limit.lo, limit.unit)}, "
                    f"{format_magnitude(limit.hi, limit.unit)}]"
                ),
                margin=f"{format_magnitude(margin_val, limit.unit)} inside the {role}",
                observed_quantity=Quantity(value=tight_value, unit=limit.unit),
                expected_quantity=Quantity(value=bound, unit=limit.unit),
            )
        return []

    lo_hits = values[values < limit.lo]
    hi_hits = values[values > limit.hi]
    if lo_hits.size:
        observed_value = float(np.min(lo_hits))
        bound = limit.lo
        side = "below"
    else:
        observed_value = float(np.max(hi_hits))
        bound = limit.hi
        side = "above"

    return [
        Finding(
            severity="violation",
            quantity=quantity,
            observed=format_magnitude(observed_value, limit.unit),
            # The declared `why` is quoted back verbatim on a crossed
            # bound. This stage builds its own limit text rather than using
            # ``str(Band)``, so it appends the same suffix explicitly.
            expected=(
                f"within {role} [{format_magnitude(limit.lo, limit.unit)}, "
                f"{format_magnitude(limit.hi, limit.unit)}]{limit.why_suffix}"
            ),
            observed_quantity=Quantity(value=observed_value, unit=limit.unit),
            expected_quantity=Quantity(value=bound, unit=limit.unit),
            delta=f"{side} bound by {format_magnitude(abs(observed_value - bound), limit.unit)}",
            owner=device.owner,
            action=f"reconcile the signal with the declared {role}",
        )
    ]
