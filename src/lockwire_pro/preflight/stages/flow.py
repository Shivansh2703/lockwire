"""Stage 2 — data flow: rate in band, timestamps monotonic and current."""

from __future__ import annotations

import numpy as np

from lockwire_pro.baseline import _instantaneous_rates, budgeted_exceedances
from lockwire.checks import _ScoreBook
from lockwire_pro.preflight.source import LiveSource, PreflightSource
from lockwire_pro.recording import DeviceStream, Recording
from lockwire.report import Finding, format_magnitude
from lockwire.schema import (
    Band,
    ContextEnvelope,
    Contract,
    DeviceInstance,
    FittedBandEvidence,
    Quantity,
)

IMPLEMENTED = True

_MONOTONICITY_QUANTITY = "preflight/stage/2/timestamps/monotonicity/live_path"
_WALL_CLOCK_QUANTITY = "preflight/stage/2/timestamps/wall_clock_currency"
_GROSS_LIVENESS_FRACTION = 0.5

# One finding, two facts — both must stay visible (monotonicity rule).
#
# B112: this text said "a future LiveSource has never been exercised" for an hour
# after B105 shipped LiveSource and exercised it against a container. A disclosure
# written while something is unbuilt becomes false the moment it is built, and no
# test fails — the mirror of this milestone's usual defect, and no less a lie for
# pointing the safe way. State only what is true TODAY, and re-read this when the
# live samples path lands.
#
# E3-FL: that moment is E3-DC (LiveSource.samples() now captures for real). Verified
# by execution, NOT by reading, that the charter's premise ("the fetched recording
# goes through load_recording exactly like a recorded one") is false: LiveSource
# calls load_ros2_bag, never load_recording (source.py:422; grep confirms no other
# caller). load_ros2_bag does not reject a non-monotonic capture the way
# load_recording does either — by default the mcap reader hands back messages
# already sorted by log_time (`iter_decoded_messages(log_time_order=True)`), so a
# bag written with reversed timestamps reads back monotonic with no error and no
# trace (measured: three messages written 0.1s, 0.0s, 0.2s past a base read back as
# 0.0, 0.1, 0.2). Scoring recv_times post-load would score a fact load_ros2_bag
# manufactures, not one it observed — the exact "green that cannot go red" rule 30
# warns about. So `_messages` now reads file order instead
# (`log_time_order=False`, which measurably matches write order) and
# `load_ros2_bag` records whether that RAW order was already monotonic, before its
# own defensive per-device sort runs — `Recording.receipt_monotonic`. That is a
# real, falsifiable fact about the capture: a single recorder process appends as it
# receives, so file order is receipt order, and a violation there is a genuine
# capture anomaly (e.g. a clock step), not a mcap storage artifact.
_MONOTONICITY_OBSERVED = (
    "through a RecordedSource loading the native recording format, recv-time "
    "monotonicity is enforced upstream by load_recording, which rejects "
    "non-monotonic recv-times, so the property cannot fail here; a LiveSource or "
    "a RecordedSource wrapping a rosbag2 .mcap bag (E3-DC/E3-FL) instead scores "
    "this quantity directly from the bag's own receipt order — this disclosure "
    "fires only when no such capture was loaded"
)
_WALL_CLOCK_OBSERVED = "wall-clock currency is a live-path property; not scored against a recording"
_WALL_CLOCK_DEFAULT_ACTION = "score wall-clock currency only through an exercised LiveSource"


def _monotonicity_findings(
    recording: Recording, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Score recv-time monotonicity from the bag's own receipt-order fact.

    `recording.receipt_monotonic` is `None` for the native format / ulog (where
    `load_recording` already enforces the property by rejection, so it is
    disclosed rather than scored — see `_MONOTONICITY_OBSERVED`), and `True`/
    `False` for a rosbag2 `.mcap` load (`load_ros2_bag`), where it names a fact
    genuinely observed on the raw capture, not manufactured by the loader.
    """
    if recording.receipt_monotonic is None:
        return [
            Finding(
                severity="not_scored",
                quantity=_MONOTONICITY_QUANTITY,
                observed=_MONOTONICITY_OBSERVED,
                expected="monotonic receive-times verified on the live path",
                action=(
                    "load a live or .mcap-bag capture to score live-path "
                    "monotonicity directly; RecordedSource-native monotonicity "
                    "remains enforced at load_recording"
                ),
            )
        ]

    total_samples = sum(len(stream.recv_times) for stream in recording.devices.values())
    all_times = [t for stream in recording.devices.values() for t in stream.recv_times]
    span_s = (max(all_times) - min(all_times)) if all_times else 0.0
    evidence = (
        f"{total_samples} sample(s) across {len(recording.devices)} device(s), "
        f"recv-time span {format_magnitude(span_s, 's')}"
    )

    if recording.receipt_monotonic:
        if scores is not None:
            scores.passed(
                _MONOTONICITY_QUANTITY,
                f"{evidence}, arrived log-time non-decreasing in file order "
                "(before load's defensive per-device sort)",
                "monotonic receive-times verified on the live path",
                "no reordering was needed to build this recording",
            )
        return []

    return [
        Finding(
            severity="violation",
            quantity=_MONOTONICITY_QUANTITY,
            observed=(
                f"{evidence}, arrived out of log-time order in file order "
                "(before load's defensive per-device sort) — load_ros2_bag "
                "silently reordered them to build this recording"
            ),
            expected="monotonic receive-times verified on the live path",
            action=(
                "investigate the capture pipeline for non-monotonic receipt "
                "order (clock step, multi-threaded logger); every other "
                "stage-2/3 check on this run used timestamps reordered "
                "without your knowledge"
            ),
        )
    ]


def _wall_clock_not_scored_finding(action: str = _WALL_CLOCK_DEFAULT_ACTION) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=_WALL_CLOCK_QUANTITY,
        observed=_WALL_CLOCK_OBSERVED,
        expected="last sample current versus wall clock",
        action=action,
    )


def _wall_clock_findings(
    recording: Recording,
    source: PreflightSource,
    contract: Contract,
    *,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """Score the capture's trailing gap against each live device's own declared
    cadence (never an invented default tolerance).

    Deliberately NOT "now minus last recv-time": that would also fire for an
    old ``.mcap`` bag opened offline hours later, which is not a currency
    *violation* — reading an old file is not the file going stale. Scoped to
    ``LiveSource`` and measured against its own capture window instead: the
    gap between the last message received and ``seconds`` (the window
    ``ros2 bag record`` was asked to run) — a publisher that goes quiet before
    the window closes is a real, live-path-only signal.
    """
    # run_preflight wraps every source in runner._LazySource (samples() fetched
    # once, on demand); unwrap it to reach the real LiveSource, the same way
    # _LazySource itself forwards scores_absence and host_side_disclosures
    # through to its ``_inner``.
    live_source = getattr(source, "_inner", source)
    if not isinstance(live_source, LiveSource) or live_source.seconds is None:
        return [_wall_clock_not_scored_finding()]

    capture_seconds = float(live_source.seconds)
    periods = [
        1.0 / device.driver.nominal_rate_hz
        for device in contract.devices
        if device.driver is not None
        and device.driver.nominal_rate_hz
        and device.driver.nominal_rate_hz > 0.0
        and device.id in recording.devices
        and recording.devices[device.id].recv_times
    ]
    all_times = [t for stream in recording.devices.values() for t in stream.recv_times]
    if not periods or not all_times:
        return [
            _wall_clock_not_scored_finding(
                "declare driver.nominal_rate_hz on a live device with samples to "
                "score wall-clock currency against its own cadence"
            )
        ]

    tolerance_s = min(periods)  # tightest declared cadence among live devices with data
    last_recv_s = max(all_times)
    trailing_gap_s = capture_seconds - last_recv_s
    evidence = (
        f"last sample at {format_magnitude(last_recv_s, 's')} into a "
        f"{format_magnitude(capture_seconds, 's')} capture window, trailing gap "
        f"{format_magnitude(trailing_gap_s, 's')}"
    )

    if trailing_gap_s <= tolerance_s:
        if scores is not None:
            scores.passed(
                _WALL_CLOCK_QUANTITY,
                f"{evidence} at or before the capture's end",
                "last sample current versus wall clock",
                f"{format_magnitude(tolerance_s - trailing_gap_s, 's')} inside "
                "the tightest declared cadence",
            )
        return []

    return [
        Finding(
            severity="violation",
            quantity=_WALL_CLOCK_QUANTITY,
            observed=f"{evidence}, exceeding the tightest declared cadence "
            f"({format_magnitude(tolerance_s, 's')})",
            expected="last sample current versus wall clock",
            action=(
                "the publisher went quiet before the capture window closed; "
                "check it is still running and republishing before trusting "
                "this capture"
            ),
        )
    ]


def check(
    contract: Contract, source: PreflightSource, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Rate in band, gap-vs-cadence currency, and named live-path disclosures."""
    recording = source.samples()
    findings: list[Finding] = [
        *_monotonicity_findings(recording, scores=scores),
        *_wall_clock_findings(recording, source, contract, scores=scores),
    ]

    for device in contract.devices:
        findings.extend(_dds_deadline_findings(device, recording, scores=scores))
        findings.extend(_procedure_findings(device, recording, scores=scores))
        findings.extend(_device_flow_findings(device, recording, scores=scores))

    # Never-silent guard. Before E3-FL the two disclosures above always fired,
    # so `findings` was never empty and this was pure belt. E3-FL made both
    # disclosures scoreable: a fully-scored live capture now legitimately
    # empties `findings` (every leg went to `scores` as a ScoredRecord
    # instead). Only treat that as suspicious silence when there is no
    # ScoreBook to have caught it — `scores is None` means nowhere for a pass
    # to go, so an empty return really would be unexplained.
    if not findings and scores is None:
        findings.append(
            Finding(
                severity="not_scored",
                quantity="preflight/stage/2/flow",
                observed="stage 2 produced no findings",
                expected="at least one named disclosure or scored leg",
                action="investigate empty stage-2 emission",
            )
        )
    return findings


def _dds_deadline_findings(
    device: DeviceInstance, recording: Recording, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    driver = device.driver
    if driver is None or driver.msg_type is None:
        return []
    quantity = f"devices/{device.id}/dds_requested_deadline"
    bound = driver.dds_deadline_s
    if bound is None:
        # DDS requested-deadline monitoring is an opt-in Driver capability.
        # Without a declared bound there is no middleware condition to score
        # or disclose; a declared bound still fails closed below if capture
        # did not retain its event evidence.
        return []
    captured_bound = recording.dds_deadlines.get(device.id)
    if captured_bound is None:
        return [
            Finding(
                severity="not_scored",
                quantity=quantity,
                observed="recording has no DDS requested-deadline event capture",
                expected=f"event capture at declared {format_magnitude(bound, 's')} deadline",
                owner=device.owner,
                action=(
                    "capture with a deadline-capable ROS 2 subscription; "
                    "receipt gaps do not prove it"
                ),
            )
        ]
    if captured_bound != bound:
        return [
            Finding(
                severity="violation",
                quantity=quantity,
                observed=f"recorded DDS bound {format_magnitude(captured_bound, 's')}",
                expected=f"contract DDS bound {format_magnitude(bound, 's')}",
                owner=device.owner,
                action="recapture with the selected contract deadline; bounds differ",
            )
        ]
    events = recording.dds_deadline_misses.get(device.id, [])
    missed = sum(count for _, count in events)
    if missed:
        return [
            Finding(
                severity="violation",
                quantity=quantity,
                observed=f"{missed} middleware requested-deadline miss(es) in {len(events)} event(s)",
                expected=f"zero misses at {format_magnitude(bound, 's')} declared deadline",
                owner=device.owner,
                action="inspect publisher timing and DDS path; this does not identify packet loss",
            )
        ]
    if scores is not None:
        scores.passed(
            quantity,
            observed="zero middleware requested-deadline misses in the capture window",
            expected=f"zero misses at {format_magnitude(bound, 's')} declared deadline",
            margin="no requested-deadline event reported",
        )
    return []


def _procedure_findings(
    device: DeviceInstance, recording: Recording, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Fail closed on declared reset/watchdog/update evidence.

    A marker says only that the selected subscribed signal carried its declared
    code. It neither commands a device nor proves a physical reset, watchdog,
    or update succeeded.
    """
    driver = device.driver
    if driver is None or not driver.procedures:
        return []
    findings: list[Finding] = []
    declared = recording.procedures.get(device.id, {})
    observed = recording.procedure_events.get(device.id, {})
    for procedure in driver.procedures:
        quantity = f"devices/{device.id}/procedures/{procedure.id}"
        expected = (procedure.kind, procedure.signal, procedure.code)
        if declared.get(procedure.id) != expected:
            findings.append(
                Finding(
                    severity="violation",
                    quantity=quantity,
                    observed="recording lacks the declared procedure identity, kind, signal, and code",
                    expected=f"captured {procedure.kind} marker {procedure.id!r}",
                    owner=device.owner,
                    action="recapture with the selected contract's declared procedure markers",
                )
            )
            continue
        hits = observed.get(procedure.id, [])
        if not hits:
            findings.append(
                Finding(
                    severity="violation",
                    quantity=quantity,
                    observed=f"no captured {procedure.kind} marker for declared procedure {procedure.id!r}",
                    expected=f"at least one captured {procedure.kind} marker",
                    owner=device.owner,
                    action="perform and capture the declared procedure; missing evidence refuses",
                )
            )
            continue
        if any(hit[1:] != expected for hit in hits):
            findings.append(
                Finding(
                    severity="violation",
                    quantity=quantity,
                    observed="captured procedure evidence does not match the declared kind, signal, and code",
                    expected=f"captured {procedure.kind} marker {procedure.id!r} on {procedure.signal!r} at code {procedure.code}",
                    owner=device.owner,
                    action="recapture from the selected contract signal and declared procedure code",
                )
            )
            continue
        if scores is not None:
            scores.passed(
                quantity,
                observed=f"{len(hits)} captured {procedure.kind} marker(s) for {procedure.id!r}",
                expected=f"at least one captured {procedure.kind} marker",
                margin="declared procedure identity, kind, signal, and code matched the recording",
            )
    return findings


def _device_flow_findings(
    device: DeviceInstance, recording: Recording, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    envelope = _rate_envelope_for_context(device, recording.context)
    band = envelope.rate_hz if envelope is not None else None
    fitted = envelope.fitted_bands.get("rate_hz") if envelope is not None else None
    nominal = device.driver.nominal_rate_hz if device.driver is not None else None
    if band is None and nominal is None:
        # Check not armed — silent, not assumed healthy (same shape as unarmed thermal).
        return []

    stream = recording.devices.get(device.id)
    if stream is None or not stream.recv_times:
        findings = [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/rate_hz",
                observed="no samples for this device in the preflight source",
                expected="receive stream present so rate / cadence can be scored",
                owner=device.owner,
                action=(
                    "source cannot see this declaration's samples; data-flow "
                    "not scored (declared and not seen)"
                ),
            )
        ]
        if band is None:
            findings.append(_gap_without_envelope_finding(device, recording.context))
        elif fitted is not None:
            findings.append(_gap_without_asserted_envelope_finding(device, recording.context))
        return findings

    out: list[Finding] = []
    if band is None:
        out.append(
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/rate_hz",
                observed=(
                    f"no ContextEnvelope.rate_hz for recording context {recording.context!r}"
                ),
                expected="declared rate band for the active context",
                owner=device.owner,
                action="add a context rate_hz envelope, or accept rate-in-band as not scored",
            )
        )
        out.append(_gap_without_envelope_finding(device, recording.context))
        if nominal is not None and nominal > 0.0:
            out.extend(
                _gross_sustained_rate_findings(
                    device, stream, nominal, recording.capture_window_s, scores=scores
                )
            )
    else:
        if fitted is None:
            out.extend(_rate_in_band_findings(device, stream, band, scores=scores))
        else:
            out.extend(_fitted_rate_findings(device, stream, band, fitted))
        if fitted is not None:
            out.append(_gap_without_asserted_envelope_finding(device, recording.context))
            if nominal is not None and nominal > 0.0:
                out.extend(
                    _gross_sustained_rate_findings(
                        device, stream, nominal, recording.capture_window_s, scores=scores
                    )
                )
        elif band.lo > 0.0:
            out.extend(_gap_vs_cadence_findings(device, stream, band.lo, scores=scores))
    return out


def _gap_without_envelope_finding(device: DeviceInstance, context: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"devices/{device.id}/recv_gap",
        observed=(
            f"device {device.id}: recv_gap not scored — no envelope declared "
            f"for recording context {context!r}"
        ),
        expected="declared rate_hz envelope for per-gap comparison",
        owner=device.owner,
        action=(
            "declare a context rate_hz envelope to score inter-message gaps; "
            "no default jitter tolerance is invented"
        ),
    )


def _gap_without_asserted_envelope_finding(device: DeviceInstance, context: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"devices/{device.id}/recv_gap",
        observed=(
            f"device {device.id}: recv_gap not scored — the {context!r} rate band "
            "is machine-fitted, not an asserted cadence limit"
        ),
        expected="an asserted rate_hz lower bound for strict per-gap comparison",
        owner=device.owner,
        action=(
            "declare an asserted cadence limit if strict maximum-gap scoring is required; "
            "the fitted rate band is scored only by its exceedance budget"
        ),
    )


def _rate_envelope_for_context(device: DeviceInstance, context: str) -> ContextEnvelope | None:
    for envelope in device.envelopes:
        if envelope.context == context and envelope.rate_hz is not None:
            return envelope
    return None


def _windowed_rates(recv_times: list[float], lo: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Mean rate over sliding windows, so one backlog burst or slow tick is not a NO-GO.

    Each window opens at a distinct receive time and spans max(1 s, 5 / lo) seconds
    (the whole capture when shorter); its rate is (n - 1) / (t_last - t_first).
    Equal receive times are driver batching (same rule as _instantaneous_rates).
    A window holding only its opening message is a silence and scores 0 Hz.
    """
    t = np.unique(np.asarray(recv_times, dtype=float))
    window_s = max(1.0, 5.0 / lo) if lo > 0.0 else 1.0
    if t.size < 2:
        return t[:0], t[:0], window_s
    if t[-1] - t[0] <= window_s:
        starts = np.array([0])
    else:
        starts = np.flatnonzero(t + window_s <= t[-1])
    ends = np.searchsorted(t, t[starts] + window_s, side="right") - 1
    spans = t[ends] - t[starts]
    rates = np.divide(ends - starts, spans, out=np.zeros(spans.size), where=spans > 0.0)
    return t[starts], rates, window_s


def _rate_in_band_findings(
    device: DeviceInstance, stream: DeviceStream, band: Band, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    starts, rates, window_s = _windowed_rates(stream.recv_times, band.lo)
    if rates.size == 0:
        return [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/rate_hz",
                observed="insufficient positive recv-time deltas to compute instantaneous rate",
                expected=str(band),
                owner=device.owner,
                action="need at least two distinct receive times to score rate in band",
            )
        ]

    # Worst window: the one furthest outside the band, else the one closest to an edge.
    i = int(np.argmin(np.minimum(rates - band.lo, band.hi - rates)))
    rate = float(rates[i])
    observed = (
        f"{format_magnitude(rate, band.unit)} mean over the {window_s:g} s window "
        f"opening at recv_time {starts[i]:.6f} s "
        f"({starts[i] - starts[0]:.3f} s into the capture)"
    )
    edge = band.lo if (rate - band.lo) <= (band.hi - rate) else band.hi
    if band.lo <= rate <= band.hi:
        if scores is not None:
            scores.passed(
                f"devices/{device.id}/rate_hz",
                observed=observed,
                expected=str(band),
                margin=(
                    f"{format_magnitude(min(rate - band.lo, band.hi - rate), band.unit)}"
                    " inside the declared context band (worst window)"
                ),
                observed_quantity=Quantity(value=rate, unit=band.unit),
                expected_quantity=Quantity(value=edge, unit=band.unit),
            )
        return []

    return [
        Finding(
            severity="violation",
            quantity=f"devices/{device.id}/rate_hz",
            observed=observed,
            expected=str(band),
            observed_quantity=Quantity(value=rate, unit=band.unit),
            expected_quantity=Quantity(value=edge, unit=band.unit),
            owner=device.owner,
            action="restore publish rate to the declared context band",
        )
    ]


def _fitted_rate_findings(
    device: DeviceInstance,
    stream: DeviceStream,
    band: Band,
    evidence: FittedBandEvidence,
) -> list[Finding]:
    rates = _instantaneous_rates(stream.recv_times)
    if rates.size == 0:
        return [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/rate_hz",
                observed="insufficient positive recv-time deltas to compute instantaneous rate",
                expected=str(band),
                owner=device.owner,
                action="need at least two distinct receive times to score rate in band",
            )
        ]

    return [
        Finding(
            severity="violation",
            quantity=f"devices/{device.id}/rate_hz",
            observed=decision.summary(),
            expected=f"fitted-band exceedance count below k* within {band}",
            observed_quantity=Quantity(value=decision.observed_value, unit=band.unit),
            expected_quantity=Quantity(value=decision.bound, unit=band.unit),
            owner=device.owner,
            action=(
                "restore publish-rate distribution to the fitted context band; "
                "excursion pattern is diagnostic only"
            ),
        )
        for decision in budgeted_exceedances(
            rates,
            band,
            evidence,
            detrend=False,
        )
    ]


def _gap_vs_cadence_findings(
    device: DeviceInstance,
    stream: DeviceStream,
    cadence_hz: float,
    *,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    if len(stream.recv_times) < 2:
        return [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/recv_gap",
                observed="fewer than two receive times; gap-vs-cadence not scored",
                expected="inter-message gaps within expected cadence",
                owner=device.owner,
                action="need at least two messages to score within-recording currency",
            )
        ]

    gaps = np.diff(np.asarray(stream.recv_times, dtype=float))
    # Zero-width gaps are driver batching (same rule as _instantaneous_rates), not staleness.
    positive = gaps[gaps > 0.0]
    if positive.size == 0:
        return [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/recv_gap",
                observed="all recv-time deltas are zero (batched); gap-vs-cadence not scored",
                expected="positive inter-message gaps within expected cadence",
                owner=device.owner,
                action="need positive receive-time spacing to score within-recording currency",
            )
        ]

    # Two missed periods, not one: a loaded host delays single messages without the
    # stream being stale. No contract field declares a recv_gap tolerance today.
    i = int(np.argmax(gaps))
    max_gap = float(gaps[i])
    max_period = 2.0 / cadence_hz
    expected = (
        f"<= {format_magnitude(max_period, 's')} (two missed periods at {cadence_hz:g} Hz cadence)"
    )
    observed = (
        f"{format_magnitude(max_gap, 's')} between recv_times "
        f"{stream.recv_times[i]:.6f} s and {stream.recv_times[i + 1]:.6f} s"
    )
    if max_gap <= max_period:
        if scores is not None:
            scores.passed(
                f"devices/{device.id}/recv_gap",
                observed=observed,
                expected=expected,
                margin=f"{format_magnitude(max_period - max_gap, 's')} to spare",
                observed_quantity=Quantity(value=max_gap, unit="s"),
                expected_quantity=Quantity(value=max_period, unit="s"),
            )
        return []

    return [
        Finding(
            severity="violation",
            quantity=f"devices/{device.id}/recv_gap",
            observed=observed,
            expected=expected,
            observed_quantity=Quantity(value=max_gap, unit="s"),
            expected_quantity=Quantity(value=max_period, unit="s"),
            owner=device.owner,
            action="inter-message gap exceeds two expected periods within the recording",
        )
    ]


def _gross_sustained_rate_findings(
    device: DeviceInstance,
    stream: DeviceStream,
    nominal_rate_hz: float,
    window_s: float | None = None,
    *,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """Catch capture-wide loss of liveness without inventing a jitter envelope.

    With a recorded capture window, the mean is messages over that window: a host that
    delivers a backlog in one burst collapses the receive span, and dividing by the span
    would read a starved stream as fast. Without one, the receive span is all there is.
    """
    recv_times = np.asarray(stream.recv_times, dtype=float)
    positive_intervals = np.diff(recv_times) > 0.0
    elapsed_s = float(recv_times[-1] - recv_times[0])
    if window_s is not None and recv_times.size:
        # Never shorter than the span: a mislabelled window must not make a stream look faster.
        window_s = max(window_s, elapsed_s)
        achieved_rate_hz = recv_times.size / window_s
        basis = f"over the {format_magnitude(window_s, 's')} capture window"
    elif elapsed_s <= 0.0 or not np.any(positive_intervals):
        return [
            Finding(
                severity="not_scored",
                quantity=f"devices/{device.id}/sustained_rate_liveness",
                observed="no positive capture span; gross sustained-rate liveness not scored",
                expected="at least two distinct receive times spanning the capture",
                owner=device.owner,
                action="capture distinct receive times to determine sustained stream liveness",
            )
        ]
    else:
        achieved_rate_hz = float(np.count_nonzero(positive_intervals) / elapsed_s)
        basis = (
            "over the receive span (no capture window recorded; a delivery burst "
            "can shorten the span and overstate the rate)"
        )
    liveness_floor_hz = nominal_rate_hz * _GROSS_LIVENESS_FRACTION
    if achieved_rate_hz >= liveness_floor_hz:
        if scores is not None:
            scores.passed(
                f"devices/{device.id}/sustained_rate_liveness",
                observed=f"{format_magnitude(achieved_rate_hz, 'Hz')} achieved mean {basis}",
                expected=f">= {format_magnitude(liveness_floor_hz, 'Hz')} gross liveness floor",
                margin=(
                    f"{format_magnitude(achieved_rate_hz - liveness_floor_hz, 'Hz')} above floor"
                ),
                observed_quantity=Quantity(value=achieved_rate_hz, unit="Hz"),
                expected_quantity=Quantity(value=liveness_floor_hz, unit="Hz"),
            )
        return []

    return [
        Finding(
            severity="violation",
            quantity=f"devices/{device.id}/sustained_rate_liveness",
            observed=f"{format_magnitude(achieved_rate_hz, 'Hz')} achieved mean {basis}",
            expected=(
                f">= {format_magnitude(liveness_floor_hz, 'Hz')} gross liveness floor "
                f"({_GROSS_LIVENESS_FRACTION:.0%} of "
                f"{format_magnitude(nominal_rate_hz, 'Hz')} driver nominal); "
                "the capture-wide mean separates sustained collapse from isolated late "
                "messages; this is a liveness determination, not a declared tolerance "
                "or rate envelope"
            ),
            observed_quantity=Quantity(value=achieved_rate_hz, unit="Hz"),
            expected_quantity=Quantity(value=liveness_floor_hz, unit="Hz"),
            owner=device.owner,
            action="restore sustained publishing; per-gap timing remains not scored",
        )
    ]
