"""Stage 4 — sample-derived cross-checks (stationary |g|, redundant agree, histogram).

Stationary-|g| gating to ``bench-idle`` / ``armed-idle`` is CORRECT DESIGN, not a
coverage gap: you cannot check gravity against a moving robot (``nominal-op``).
That guard is disclosed as ``not_scored`` naming the condition, never as silence.

Redundant-sensor agreement and camera-histogram legs are honest refusals: the
lockfile has no redundancy declaration, and the recording format carries no image
data. Both emit ``not_scored`` naming the missing operand; follow-on boxes B94
and B95 schedule the schema/format work. Do not invent declarations or grow
``recording.py`` here.
"""

from __future__ import annotations

import math

from lockwire.checks import _ScoreBook
from lockwire_pro.preflight.envelopes import ContextSignalView, signal_view_for_context
from lockwire_pro.preflight.source import PreflightSource
from lockwire_pro.recording import DeviceStream, Recording
from lockwire.report import Finding, format_magnitude
from lockwire.schema import (
    Band,
    Contract,
    Quantity,
)

# B90: stage implemented for real (stub replaced).
IMPLEMENTED = True

# Contexts that declare a stationary platform. Checking |g| under nominal-op
# would be a false witness — motion is in-regime there. This is a condition
# guard, not missing coverage (plan review amendment 4).
_STATIONARY_CONTEXTS = frozenset({"bench-idle", "armed-idle"})

_STANDARD_G = 9.81  # m/s^2; contract §2 / spec v3
_ACCEL_AXES = ("accel_x", "accel_y", "accel_z")

# Distinct reason strings — ACCEPT asserts these phrases, not a count.
REASON_NONSTATIONARY = "context does not assert a stationary platform"
REASON_NO_REDUNDANCY_DECL = (
    "lockfile schema has no redundancy declaration naming which sensors are "
    "peers; redundant-sensor agreement not scored"
)
REASON_NO_IMAGE_DATA = (
    "recording format carries no image data or histogram summary channel; "
    "camera histogram not scored"
)
REASON_NO_ACCEL = "no accelerometer samples in the recording; stationary |g| not scored"
REASON_NO_DEVICE = "recording device has no matching contract device; stationary |g| not scored"
REASON_NO_ENVELOPE = "no device envelope for this stationary context; stationary |g| not scored"
REASON_NO_ACCEL_ENVELOPE = (
    "device envelope has no accelerometer band to source |g| tolerance; stationary |g| not scored"
)
REASON_INCOMPLETE_ACCEL_TRIO = (
    "accelerometer stream is missing one or more of accel_x/accel_y/accel_z; "
    "|g| needs all three axes and is not scored"
)


def check(
    contract: Contract, source: PreflightSource, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Emit a finding on every path — never return ``[]`` (false-GO silence)."""
    recording = source.samples()
    findings: list[Finding] = []
    findings.extend(_stationary_g_findings(contract, recording, scores=scores))
    findings.append(_redundant_sensors_refusal())
    findings.append(_camera_histogram_refusal())
    if not findings:  # pragma: no cover - belt against silence
        findings.append(
            Finding(
                severity="not_scored",
                quantity="preflight/stage/4/cross_signal",
                observed="stage produced no findings",
                expected="at least one finding on every path",
                action="fix cross_signal so it cannot silently pass",
            )
        )
    return findings


def _redundant_sensors_refusal() -> Finding:
    return Finding(
        severity="not_scored",
        quantity="preflight/cross_signal/redundant_sensors",
        observed=REASON_NO_REDUNDANCY_DECL,
        expected="an authored lockfile declaration of redundant sensor peers",
        action=(
            "declare which sensors are redundant peers in the lockfile; do not infer it "
            "from device-id string shape"
        ),
    )


def _camera_histogram_refusal() -> Finding:
    return Finding(
        severity="not_scored",
        quantity="preflight/cross_signal/camera_histogram",
        observed=REASON_NO_IMAGE_DATA,
        expected="image samples or a histogram summary channel in the recording",
        action="record image data or a histogram summary channel to score this",
    )


def _stationary_g_findings(
    contract: Contract, recording: Recording, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Score |g|≈9.81 only under stationary contexts; otherwise disclose the guard."""
    if recording.context not in _STATIONARY_CONTEXTS:
        return [
            Finding(
                severity="not_scored",
                quantity="preflight/cross_signal/stationary_|g|",
                observed=(f"recording context {recording.context!r}: {REASON_NONSTATIONARY}"),
                expected=(
                    "context bench-idle or armed-idle (stationary platform) "
                    "before |g|≈9.81 can be scored — gating is correct design, "
                    "not a coverage gap"
                ),
                action=(
                    "run preflight under a stationary context, or accept that "
                    "|g| is not scored while the platform may be moving"
                ),
            )
        ]

    accel_devices = _accel_devices(recording)
    if not accel_devices:
        return [
            Finding(
                severity="not_scored",
                quantity="preflight/cross_signal/stationary_|g|",
                observed=REASON_NO_ACCEL,
                expected="accelerometer samples so |g| can be compared to 9.81",
                action="record IMU accel axes under a stationary context",
            )
        ]

    findings: list[Finding] = []
    for device_id, stream in accel_devices:
        finding = _score_device_g(contract, recording.context, device_id, stream, scores=scores)
        if finding is not None:
            findings.append(finding)
    # Every IMU passed: stage stays non-silent via the two refusal legs.
    return findings


def _accel_devices(recording: Recording) -> list[tuple[str, DeviceStream]]:
    return [
        (device_id, stream)
        for device_id, stream in sorted(recording.devices.items())
        if any(name.startswith("accel_") for name in stream.signals)
    ]


def _score_device_g(
    contract: Contract,
    context: str,
    device_id: str,
    stream: DeviceStream,
    *,
    scores: _ScoreBook | None = None,
) -> Finding | None:
    """Return a finding, or ``None`` when |g| passes cleanly (sibling legs speak)."""
    quantity = f"devices/{device_id}/stationary_|g|"
    device = contract.device(device_id)
    if device is None:
        return Finding(
            severity="not_scored",
            quantity=quantity,
            observed=f"{REASON_NO_DEVICE} (device {device_id!r})",
            expected="a contract device matching the recording device id",
            action="align recording device ids with the lockfile, or declare the device",
        )

    signal_view = signal_view_for_context(device, context)
    if not signal_view.envelopes:
        return Finding(
            severity="not_scored",
            quantity=quantity,
            observed=f"{REASON_NO_ENVELOPE} (context {context!r})",
            expected=f"a locked envelope for context {context!r} with accel bands",
            owner=device.owner,
            action="lock a stationary-context envelope, or accept |g| is not scored",
        )

    mag_band = _magnitude_band_from_view(signal_view)
    if mag_band is None:
        fitted_accel = sorted(
            name
            for name, declaration in signal_view.signals.items()
            if name.startswith("accel_") and declaration.fitted is not None
        )
        if fitted_accel:
            return Finding(
                severity="not_scored",
                quantity=quantity,
                observed=(
                    "stationary |g| not scored — accelerometer envelope bands are "
                    f"machine-fitted residual limits ({', '.join(fitted_accel)}), "
                    "not asserted gravity witness limits"
                ),
                expected="an asserted accel_* envelope band sourcing the |g| witness limit",
                owner=device.owner,
                action=(
                    "declare an asserted physical accelerometer limit for the gravity witness, "
                    "or accept |g| stays not scored"
                ),
            )
        return Finding(
            severity="not_scored",
            quantity=quantity,
            observed=REASON_NO_ACCEL_ENVELOPE,
            expected="an accel_* signal envelope band sourcing the |g| witness limit",
            owner=device.owner,
            action="lock an accelerometer band on the stationary-context envelope",
        )

    if not all(axis in stream.signals for axis in _ACCEL_AXES):
        present = sorted(name for name in stream.signals if name.startswith("accel_"))
        return Finding(
            severity="not_scored",
            quantity=quantity,
            observed=(
                f"{REASON_INCOMPLETE_ACCEL_TRIO} "
                f"(present: {', '.join(present) if present else 'none'})"
            ),
            expected=("accel_x, accel_y, and accel_z so |g| is a real magnitude, not a subset"),
            owner=device.owner,
            action=(
                "record the full accelerometer trio under the stationary context, "
                "or accept that |g| is not scored"
            ),
        )

    observed_g = _mean_accel_magnitude(stream)
    if observed_g is None or not math.isfinite(observed_g):
        return Finding(
            severity="not_scored",
            quantity=quantity,
            observed="accelerometer samples missing or non-finite; |g| not scored",
            expected=f"finite accel samples to compare against {_STANDARD_G} m/s^2",
            owner=device.owner,
            action="repair the recording's accelerometer stream",
        )

    expected = (
        f"≈ {format_magnitude(_STANDARD_G, 'm/s^2')} within envelope-derived |g| band {mag_band}"
    )
    if mag_band.lo <= observed_g <= mag_band.hi:
        if scores is not None:
            lower_margin = observed_g - mag_band.lo
            upper_margin = mag_band.hi - observed_g
            nearer_bound = mag_band.lo if lower_margin <= upper_margin else mag_band.hi
            margin = min(lower_margin, upper_margin)
            # Quantity path shaped like a sanity signal-band record (devices/<id>/
            # signals/<channel>/<leg>) so health.py's signal-band reader picks up
            # the accel band numbers on the healthy path the same way it already
            # does for a sanity range pass — same shape, different check.
            scores.passed(
                f"devices/{device_id}/signals/accel/stationary_g",
                observed=format_magnitude(observed_g, "m/s^2"),
                expected=expected,
                margin=f"{format_magnitude(margin, 'm/s^2')} inside the |g| band",
                observed_quantity=Quantity(value=observed_g, unit="m/s^2"),
                expected_quantity=Quantity(value=nearer_bound, unit="m/s^2"),
            )
        return None

    return Finding(
        severity="violation",
        quantity=quantity,
        observed=format_magnitude(observed_g, "m/s^2"),
        expected=expected,
        observed_quantity=Quantity(value=observed_g, unit="m/s^2"),
        expected_quantity=Quantity(value=_STANDARD_G, unit="m/s^2"),
        delta=(
            f"{format_magnitude(observed_g - _STANDARD_G, 'm/s^2')} from "
            f"{format_magnitude(_STANDARD_G, 'm/s^2')}"
        ),
        owner=device.owner,
        action=("reconcile IMU accel with gravity under the locked envelope, or re-baseline"),
    )


def _magnitude_band_from_view(signal_view: ContextSignalView) -> Band | None:
    """Derive a |g| witness band from asserted accel_* envelopes only.

    Uses ``accel_z`` when present (gravity-dominated axis under stationarity), else
    the first accel_* band. Signed endpoints become a positive magnitude interval.
    """
    accel = {
        name: declaration.signal
        for name, declaration in signal_view.signals.items()
        if name.startswith("accel_") and declaration.fitted is None
    }
    if not accel:
        return None
    key = "accel_z" if "accel_z" in accel else sorted(accel)[0]
    band = accel[key].band
    lo = 0.0 if band.lo < 0 < band.hi else min(abs(band.lo), abs(band.hi))
    hi = max(abs(band.lo), abs(band.hi))
    return Band(lo=lo, hi=hi, unit=band.unit)


def _mean_accel_magnitude(stream: DeviceStream) -> float | None:
    """Mean |a| from the standard accel trio only — never a subset under the |g| name."""
    if not all(axis in stream.signals for axis in _ACCEL_AXES):
        return None
    lengths = {len(stream.signals[name]) for name in _ACCEL_AXES}
    if len(lengths) != 1:
        return None
    n = next(iter(lengths))
    if n == 0:
        return None
    total = 0.0
    for i in range(n):
        total += math.sqrt(sum(stream.signals[name][i] ** 2 for name in _ACCEL_AXES))
    return total / n
