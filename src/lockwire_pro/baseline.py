"""Golden-recording profiling and deterministic preflight comparisons.

The schema can persist locked envelopes, but it cannot represent an under-sampled
channel or the full CUSUM calibration. ``BaselineArtifact`` therefore carries both
the schema-native ``ContextEnvelope`` rows and explicit per-channel profiles. An
under-sampled profile has no band, and is omitted from the corresponding schema
envelope; callers may only check it when a hand-authored hard limit is supplied.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from math import comb
from typing import TYPE_CHECKING, Literal, Mapping

import numpy as np

from lockwire import units
from lockwire_pro.recording import Recording
from lockwire.report import LARGE_EVIDENCE_GAP, Finding, RecordReport, _console
from lockwire.schema import (
    CONTEXTS,
    Band,
    ContextEnvelope,
    FittedBandAllocation,
    FittedBandEvidence,
    Provenance,
    SignalEnvelope,
)
from lockwire_pro.stats.clusters import (
    ALPHA_GATE,
    FamilywiseAllocation,
    PhysicalCheck,
    familywise_allocation,
)
from lockwire_pro.stats.cusum import calibrate_decision_interval, variance_cusum
from lockwire_pro.stats.drift import (
    cubic_detrend,
    drift_decision_interval,
    trend_cusum_statistic,
)
from lockwire_pro.stats.neff import decorrelation_length, effective_sample_size
from lockwire_pro.stats.wilks import OrderStatBound, Side, UnderSampled, evaluate, min_n_required

if TYPE_CHECKING:  # rich is the terminal door's dependency (the library door never needs it)
    from rich.console import Console

COVERAGE = 0.95
CONFIDENCE = 0.95
CUSUM_WINDOW_SIZE = 20
_RECORDING_UNIT_ALIASES = {
    "Pa": ("hPa", 0.01),
    "degC": ("C", 1.0),
}

ChannelKind = Literal["rate", "signal", "condition"]
ProfileStatus = Literal["locked", "under-sampled"]
Mechanism = Literal[
    "wilks-band-exceedance",
    "cusum",
    "drift-cusum",
    "rate-check",
    "hard-limit-exceedance",
    "condition-envelope",
    "non-finite-sample",
    "device-absent",
    "channel-unseen",
]


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    """All evidence needed to explain or consume one derived channel."""

    id: str
    kind: ChannelKind
    name: str
    device: str | None
    unit: str
    status: ProfileStatus
    n: int
    n_eff: float
    effective_n: int
    decorrelation: int
    requested_coverage: float
    requested_confidence: float
    min_n_required: int
    achieved_confidence: float | None
    rank_from_edge: int | None
    band: Band | None
    observed_range: Band
    alpha_channel: float
    variance_floor: float | None = None
    cusum_window_size: int | None = None
    cusum_decision_interval: float | None = None
    cusum_achieved_alpha: float | None = None
    drift_reference: float | None = None
    drift_residual_scale: float | None = None
    drift_residual_decorrelation: int | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "unit": self.unit,
            "n": self.n,
            "n_eff": self.n_eff,
            "effective_n": self.effective_n,
            "decorrelation": self.decorrelation,
            "requested_coverage": self.requested_coverage,
            "requested_confidence": self.requested_confidence,
            "min_n_required": self.min_n_required,
            "alpha_channel": self.alpha_channel,
        }
        if self.device is not None:
            data["device"] = self.device
        if self.achieved_confidence is not None:
            data["achieved_coverage"] = self.requested_coverage
            data["achieved_confidence"] = self.achieved_confidence
        if self.rank_from_edge is not None:
            data["rank_from_edge"] = self.rank_from_edge
        if self.band is not None:
            data["band"] = self.band.model_dump(mode="json")
        data["observed_range"] = self.observed_range.model_dump(mode="json")
        if self.variance_floor is not None:
            data["cusum"] = {
                "variance_floor": self.variance_floor,
                "window_size": self.cusum_window_size,
                "decision_interval": self.cusum_decision_interval,
                "achieved_alpha": self.cusum_achieved_alpha,
            }
        if self.drift_reference is not None:
            data["drift_cusum"] = {
                "reference": self.drift_reference,
                "residual_scale": self.drift_residual_scale,
                "residual_decorrelation": self.drift_residual_decorrelation,
                "alpha_channel": self.alpha_channel,
            }
        return data


@dataclass(frozen=True, slots=True)
class BaselineArtifact:
    """A reviewable baseline plus schema-native per-device envelopes."""

    context: str
    recording_sha256: str
    source: str
    envelopes: dict[str, ContextEnvelope]
    profiles: dict[str, ChannelProfile]
    start_timestamp_s: float
    end_timestamp_s: float
    duration_s: float
    known_good_rationale: str | None = None
    operator: str | None = None
    setup: str | None = None
    firmware: str | None = None
    calibration: str | None = None
    dropped_messages: int | None = None
    recording_completeness: str | None = None
    fitted_band_allocations: tuple[FittedBandAllocation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_version": "1",
            "context": self.context,
            "recording": {
                "source": self.source,
                "sha256": self.recording_sha256,
                "timing": {
                    "clock": "recording receive-time",
                    "start_timestamp_s": self.start_timestamp_s,
                    "end_timestamp_s": self.end_timestamp_s,
                    "duration_s": self.duration_s,
                },
                "completeness": {
                    "dropped_messages": self.dropped_messages,
                    "recording_completeness": self.recording_completeness,
                },
            },
            "provenance": {
                "known_good_rationale": self.known_good_rationale,
                "operator": self.operator,
                "setup": self.setup,
                "firmware": self.firmware,
                "calibration": self.calibration,
            },
            "envelopes": {
                device: envelope.model_dump(mode="json", exclude_none=True)
                for device, envelope in sorted(self.envelopes.items())
            },
            "fitted_band_allocations": [
                allocation.model_dump(mode="json", exclude_none=True)
                for allocation in self.fitted_band_allocations
            ],
            "channels": {
                channel_id: profile.to_dict()
                for channel_id, profile in sorted(self.profiles.items())
            },
        }


@dataclass(frozen=True, slots=True)
class BaselineReviewChannel:
    """Evidence from one serialized channel, normalized for human review."""

    id: str
    status: ProfileStatus
    n: int
    n_eff: float
    effective_n: int
    decorrelation: int
    requested_coverage: float
    requested_confidence: float
    min_n_required: int
    achieved_coverage: float | None
    achieved_confidence: float | None
    observed_range: Band | None
    fitted_band: Band | None


@dataclass(frozen=True, slots=True)
class BaselineReview:
    """The traceability and evidence fields needed by the review command."""

    context: str
    source: str
    recording_sha256: str
    start_timestamp_s: float | None
    end_timestamp_s: float | None
    duration_s: float | None
    known_good_rationale: str | None
    operator: str | None
    setup: str | None
    firmware: str | None
    calibration: str | None
    dropped_messages: int | None
    recording_completeness: str | None
    channels: tuple[BaselineReviewChannel, ...]


@dataclass(frozen=True, slots=True)
class BaselineFinding:
    """One consolidated preflight finding."""

    mechanism: Mechanism
    channels: tuple[str, ...]
    detail: str
    cluster_root: str | None = None
    cluster_members: tuple[str, ...] = ()
    # "not_scored" = nothing was compared; it is not a violation.
    severity: Literal["violation", "not_scored"] = "violation"
    device: str = ""


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"baseline artifact {field} must be a mapping")
    return value


def _required(data: Mapping[str, object], field: str, channel_id: str | None = None) -> object:
    try:
        return data[field]
    except KeyError as error:
        location = f"channel {channel_id!r}" if channel_id is not None else "recording"
        raise ValueError(f"baseline artifact {location} is missing {field!r}") from error


def _optional_string(data: Mapping[str, object], field: str) -> str | None:
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"baseline artifact {field} must be a string or null")
    return _normalize_optional_string(value)


def _optional_float(data: Mapping[str, object], field: str) -> float | None:
    value = data.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"baseline artifact {field} must be numeric or null") from error


def _optional_dropped_messages(data: Mapping[str, object]) -> int | None:
    """Shared chokepoint for completeness.dropped_messages on every artifact loader."""
    dropped_messages_raw = data.get("dropped_messages")
    if dropped_messages_raw is None:
        return None
    if isinstance(dropped_messages_raw, bool):
        raise ValueError("baseline artifact dropped_messages must be an integer or null")
    try:
        dropped_messages = int(dropped_messages_raw)
    except (TypeError, ValueError) as error:
        raise ValueError("baseline artifact dropped_messages must be an integer or null") from error
    if dropped_messages < 0 or dropped_messages != dropped_messages_raw:
        raise ValueError("baseline artifact dropped_messages must be a non-negative integer")
    return dropped_messages


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def parse_baseline_review(data: object) -> BaselineReview:
    """Validate and normalize the evidence fields in a serialized baseline artifact."""
    root = _mapping(data, "root")
    if root.get("artifact_version") != "1":
        raise ValueError("baseline artifact has unsupported or missing artifact_version")

    context = root.get("context")
    if not isinstance(context, str):
        raise ValueError("baseline artifact context must be a string")

    recording = _mapping(root.get("recording"), "recording")
    source = _required(recording, "source")
    sha256 = _required(recording, "sha256")
    if not isinstance(source, str) or not isinstance(sha256, str):
        raise ValueError("baseline artifact recording source and sha256 must be strings")

    # These additions remain optional when reading artifact_version 1 so artifacts
    # emitted before provenance evidence was added still open for review. Their
    # normalized None values are explicit gaps for the review renderer.
    timing = _mapping(recording.get("timing", {}), "recording timing")
    completeness = _mapping(recording.get("completeness", {}), "recording completeness")
    provenance = _mapping(root.get("provenance", {}), "provenance")

    serialized_channels = _mapping(root.get("channels"), "channels")
    if not serialized_channels:
        raise ValueError("baseline artifact contains no channels")

    channels: list[BaselineReviewChannel] = []
    for channel_id, serialized in sorted(serialized_channels.items()):
        if not isinstance(channel_id, str):
            raise ValueError("baseline artifact channel ids must be strings")
        channel = _mapping(serialized, f"channel {channel_id!r}")
        status = _required(channel, "status", channel_id)
        if status not in ("locked", "under-sampled"):
            raise ValueError(f"baseline artifact channel {channel_id!r} has invalid status")

        try:
            channels.append(
                BaselineReviewChannel(
                    id=channel_id,
                    status=status,
                    n=int(_required(channel, "n", channel_id)),
                    n_eff=float(_required(channel, "n_eff", channel_id)),
                    effective_n=int(_required(channel, "effective_n", channel_id)),
                    decorrelation=int(_required(channel, "decorrelation", channel_id)),
                    requested_coverage=float(_required(channel, "requested_coverage", channel_id)),
                    requested_confidence=float(
                        _required(channel, "requested_confidence", channel_id)
                    ),
                    min_n_required=int(_required(channel, "min_n_required", channel_id)),
                    achieved_coverage=(
                        float(channel["achieved_coverage"])
                        if "achieved_coverage" in channel
                        else None
                    ),
                    achieved_confidence=(
                        float(channel["achieved_confidence"])
                        if "achieved_confidence" in channel
                        else None
                    ),
                    observed_range=(
                        Band.model_validate(_mapping(channel["observed_range"], "observed_range"))
                        if "observed_range" in channel
                        else None
                    ),
                    fitted_band=(
                        Band.model_validate(_mapping(channel["band"], "band"))
                        if "band" in channel
                        else None
                    ),
                )
            )
        except TypeError as error:
            raise ValueError(
                f"baseline artifact channel {channel_id!r} has invalid numeric evidence"
            ) from error
        except ValueError as error:
            if str(error).startswith("baseline artifact"):
                raise
            raise ValueError(
                f"baseline artifact channel {channel_id!r} has invalid numeric evidence"
            ) from error

    return BaselineReview(
        context=context,
        source=source,
        recording_sha256=sha256,
        start_timestamp_s=_optional_float(timing, "start_timestamp_s"),
        end_timestamp_s=_optional_float(timing, "end_timestamp_s"),
        duration_s=_optional_float(timing, "duration_s"),
        known_good_rationale=_optional_string(provenance, "known_good_rationale"),
        operator=_optional_string(provenance, "operator"),
        setup=_optional_string(provenance, "setup"),
        firmware=_optional_string(provenance, "firmware"),
        calibration=_optional_string(provenance, "calibration"),
        dropped_messages=_optional_dropped_messages(completeness),
        recording_completeness=_optional_string(completeness, "recording_completeness"),
        channels=tuple(channels),
    )


def _rate_id(device: str) -> str:
    return f"rate/{device}"


def _signal_id(device: str, name: str) -> str:
    return f"device/{device}/{name}"


def _condition_id(name: str) -> str:
    return f"environment/{name}"


def _instantaneous_rates(times: list[float]) -> np.ndarray:
    deltas = np.diff(np.asarray(times, dtype=float))
    # Equal receive times represent driver batching, not infinite independent rate
    # evidence. Ignore those zero-width gaps and let the capture-wide rate check
    # account for all received messages.
    return 1.0 / deltas[deltas > 0.0]


def _canonical_values(values: list[float] | np.ndarray, unit: str) -> tuple[np.ndarray, str]:
    canonical, factor = _RECORDING_UNIT_ALIASES.get(unit, (unit, 1.0))
    return np.asarray(values, dtype=float) * factor, canonical


_REDUNDANT_SENSOR_INSTANCE = re.compile(
    r"^(?P<host>.+)\.(?P<family>sensor_(?:accel|gyro|baro))\.(?P<instance>\d+)$"
)


def _redundant_quantity_families(recording: Recording) -> set[tuple[str, str]]:
    if recording.source != "ulog":
        return set()
    candidates = [
        (match["host"], match["family"])
        for device in recording.devices
        if (match := _REDUNDANT_SENSOR_INSTANCE.fullmatch(device)) is not None
    ]
    return {family for family, count in Counter(candidates).items() if count > 1}


def _device_cluster_path(
    device: str,
    redundant_families: set[tuple[str, str]],
) -> tuple[str, str]:
    match = _REDUNDANT_SENSOR_INSTANCE.fullmatch(device)
    if match is not None and (match["host"], match["family"]) in redundant_families:
        return ("host", f"quantity:{match['host']}.{match['family']}")
    return ("host", f"device:{device}")


def _cluster_allocation(recording: Recording) -> FamilywiseAllocation:
    checks: list[PhysicalCheck] = []
    redundant_families = _redundant_quantity_families(recording)
    for device, stream in sorted(recording.devices.items()):
        path = (_device_cluster_path(device, redundant_families),)
        checks.append(PhysicalCheck(id=_rate_id(device), paths=path))
        checks.extend(
            PhysicalCheck(id=_signal_id(device, name), paths=path)
            for name in sorted(stream.signals)
        )
    checks.extend(
        PhysicalCheck(id=_condition_id(name), paths=(("host", "environment"),))
        for name in sorted(recording.env)
    )
    return familywise_allocation(checks, alpha_gate=ALPHA_GATE)


def _cluster_alphas(recording: Recording) -> dict[str, float]:
    channel_alphas: dict[str, float] = {}
    for cluster in _cluster_allocation(recording).clusters:
        alpha_channel = cluster.alpha / len(cluster.check_ids)
        channel_alphas.update(dict.fromkeys(cluster.check_ids, alpha_channel))
    return channel_alphas


def _fitted_inventory_path(channel_id: str, context: str, devices: tuple[str, ...]) -> list[str]:
    if channel_id.startswith("rate/"):
        device = channel_id.removeprefix("rate/")
        return [f"devices/{device}/envelopes/{context}/rate_hz"]
    if channel_id.startswith("device/"):
        _, device, name = channel_id.split("/", 2)
        return [f"devices/{device}/envelopes/{context}/signals/{name}"]
    if channel_id.startswith("environment/"):
        name = channel_id.removeprefix("environment/")
        return [f"devices/{device}/envelopes/{context}/conditions/{name}" for device in devices]
    raise ValueError(f"unknown baseline channel id {channel_id!r}")


def _fitted_band_allocations(
    recording: Recording,
    profiles: Mapping[str, ChannelProfile],
    *,
    context: str,
) -> tuple[FittedBandAllocation, ...]:
    allocation = _cluster_allocation(recording)
    devices = tuple(sorted(recording.devices))
    rows = []
    for cluster in allocation.clusters:
        fitted_paths = [
            path
            for channel_id in cluster.check_ids
            if profiles[channel_id].band is not None
            for path in _fitted_inventory_path(channel_id, context, devices)
        ]
        rows.append(
            FittedBandAllocation(
                context=context,
                channels=list(cluster.check_ids),
                fitted_bands=sorted(fitted_paths),
                denominator=len(allocation.clusters) * len(cluster.check_ids),
            )
        )
    return tuple(rows)


def _cluster_identities(recording: Recording) -> dict[str, str]:
    """Retain the physical root that clustering otherwise discards after allocation."""
    identities: dict[str, str] = {}
    for cluster in _cluster_allocation(recording).clusters:
        # Baseline channels are deliberately keyed to one physical quantity root.
        # B63 owns any future expansion of the provenance scope beyond that root.
        if len(cluster.paths) != 1:
            raise ValueError("baseline alarm cluster must have exactly one physical root")
        kind, path_id = cluster.paths[0]
        if kind == "host":
            root = path_id.removeprefix("quantity:").removeprefix("device:")
        else:
            root = f"{kind}.{path_id}"
        identities.update(dict.fromkeys(cluster.check_ids, root))
    return identities


def _cusum_parameters(
    values: np.ndarray,
    *,
    alpha_channel: float,
    calibration_cache: dict[tuple[int, float], tuple[float, float]],
) -> tuple[float, int, float, float] | None:
    complete_windows = values.size // CUSUM_WINDOW_SIZE
    if complete_windows < 2:
        return None
    windows = values[: complete_windows * CUSUM_WINDOW_SIZE].reshape(
        complete_windows, CUSUM_WINDOW_SIZE
    )
    variance_floor = float(np.min(np.var(windows, axis=1)))
    if not math.isfinite(variance_floor) or variance_floor <= 0.0:
        return None

    key = (complete_windows, alpha_channel)
    cached = calibration_cache.get(key)
    if cached is None:
        trials = max(50_000, math.ceil(20 / alpha_channel))
        calibration = calibrate_decision_interval(
            1.0,
            in_control_variance=1.0,
            window_size=CUSUM_WINDOW_SIZE,
            capture_windows=complete_windows,
            alpha_channel=alpha_channel,
            trials=trials,
            validation_trials=trials,
        )
        cached = (calibration.decision_interval, calibration.achieved_alpha)
        calibration_cache[key] = cached
    decision_ratio, achieved_alpha = cached
    return (
        variance_floor,
        CUSUM_WINDOW_SIZE,
        variance_floor * decision_ratio,
        achieved_alpha,
    )


def _profile(
    *,
    channel_id: str,
    kind: ChannelKind,
    name: str,
    device: str | None,
    unit: str,
    values: np.ndarray,
    alpha_channel: float,
    calibration_cache: dict[tuple[int, float], tuple[float, float]],
) -> ChannelProfile:
    n = int(values.size)
    detrended = cubic_detrend(values) if kind == "signal" else None
    envelope_values = detrended.residual if detrended is not None else values
    n_eff = effective_sample_size(envelope_values)
    decorrelation = decorrelation_length(envelope_values)
    reduced = np.sort(envelope_values[:: max(1, decorrelation)])
    effective_n = int(reduced.size)
    # The merged Wilks implementation overflows for large-n interior ranks because
    # its exact integer combinations are converted to floats. The sample extremes
    # are the documented rank-1 path and retain the full distribution-free guarantee.
    wilks = evaluate(effective_n, COVERAGE, CONFIDENCE, Side.TWO_SIDED)

    band = None
    achieved_confidence = None
    rank_from_edge = None
    status: ProfileStatus = "under-sampled"
    if isinstance(wilks, OrderStatBound):
        # Wilks ranks are derived from the decorrelated population they index.
        lo_rank, hi_rank = wilks.ranks
        band = Band(lo=float(reduced[lo_rank - 1]), hi=float(reduced[hi_rank - 1]), unit=unit)
        achieved_confidence = wilks.achieved_confidence
        rank_from_edge = wilks.rank_from_edge
        status = "locked"

    cusum = None
    if kind == "signal":
        cusum = _cusum_parameters(
            values,
            alpha_channel=alpha_channel,
            calibration_cache=calibration_cache,
        )

    return ChannelProfile(
        id=channel_id,
        kind=kind,
        name=name,
        device=device,
        unit=unit,
        status=status,
        n=n,
        n_eff=n_eff,
        effective_n=effective_n,
        decorrelation=decorrelation,
        requested_coverage=COVERAGE,
        requested_confidence=CONFIDENCE,
        min_n_required=(
            wilks.min_n_required
            if isinstance(wilks, UnderSampled)
            else min_n_required(COVERAGE, CONFIDENCE, Side.TWO_SIDED)
        ),
        achieved_confidence=achieved_confidence,
        rank_from_edge=rank_from_edge,
        band=band,
        observed_range=Band(
            lo=float(np.min(values)),
            hi=float(np.max(values)),
            unit=unit,
        ),
        alpha_channel=alpha_channel,
        variance_floor=cusum[0] if cusum else None,
        cusum_window_size=cusum[1] if cusum else None,
        cusum_decision_interval=cusum[2] if cusum else None,
        cusum_achieved_alpha=cusum[3] if cusum else None,
        drift_reference=(
            trend_cusum_statistic(detrended.trend) if detrended is not None and n >= 4 else None
        ),
        drift_residual_scale=(
            float(np.std(detrended.residual)) if detrended is not None and n >= 4 else None
        ),
        drift_residual_decorrelation=(decorrelation if detrended is not None and n >= 4 else None),
    )


def build_baseline(
    recording: Recording,
    *,
    context: str,
    locked_at: dt.date | None = None,
    known_good_rationale: str | None = None,
    operator: str | None = None,
    setup: str | None = None,
    firmware: str | None = None,
    calibration: str | None = None,
    dropped_messages: int | None = None,
    recording_completeness: str | None = None,
) -> BaselineArtifact:
    """Derive safe envelopes from a known-good recording."""
    known_good_rationale = _normalize_optional_string(known_good_rationale)
    operator = _normalize_optional_string(operator)
    setup = _normalize_optional_string(setup)
    firmware = _normalize_optional_string(firmware)
    calibration = _normalize_optional_string(calibration)
    recording_completeness = _normalize_optional_string(recording_completeness)

    if context not in CONTEXTS:
        raise ValueError(f"unknown context {context!r}; allowed: {CONTEXTS}")
    if not recording.devices:
        raise ValueError("recording contains no device streams")
    if dropped_messages is not None and dropped_messages < 0:
        raise ValueError("dropped_messages must be non-negative")

    timestamps = [
        timestamp for stream in recording.devices.values() for timestamp in stream.recv_times
    ]
    timestamps.extend(
        timestamp for timed_values in recording.env.values() for timestamp, _ in timed_values
    )
    if not timestamps:
        raise ValueError("recording contains no timestamped samples")
    start_timestamp_s = min(timestamps)
    end_timestamp_s = max(timestamps)

    alpha_by_channel = _cluster_alphas(recording)
    calibration_cache: dict[tuple[int, float], tuple[float, float]] = {}
    profiles: dict[str, ChannelProfile] = {}

    for device, stream in sorted(recording.devices.items()):
        rate_id = _rate_id(device)
        profiles[rate_id] = _profile(
            channel_id=rate_id,
            kind="rate",
            name="rate_hz",
            device=device,
            unit="Hz",
            values=_instantaneous_rates(stream.recv_times),
            alpha_channel=alpha_by_channel[rate_id],
            calibration_cache=calibration_cache,
        )
        for name, samples in sorted(stream.signals.items()):
            channel_id = _signal_id(device, name)
            values, unit = _canonical_values(samples, recording.unit_of(name, device=device))
            profiles[channel_id] = _profile(
                channel_id=channel_id,
                kind="signal",
                name=name,
                device=device,
                unit=unit,
                values=values,
                alpha_channel=alpha_by_channel[channel_id],
                calibration_cache=calibration_cache,
            )

    for name, timed_values in sorted(recording.env.items()):
        channel_id = _condition_id(name)
        values, unit = _canonical_values(
            [value for _, value in timed_values],
            recording.unit_of(name, device="environment"),
        )
        profiles[channel_id] = _profile(
            channel_id=channel_id,
            kind="condition",
            name=name,
            device=None,
            unit=unit,
            values=values,
            alpha_channel=alpha_by_channel[channel_id],
            calibration_cache=calibration_cache,
        )

    provenance = Provenance(
        witness="live",
        source=str(recording.path),
        locked_at=locked_at or dt.date.today(),
        bag_sha256=recording.sha256,
    )
    envelopes: dict[str, ContextEnvelope] = {}
    for device, stream in sorted(recording.devices.items()):
        rate = profiles[_rate_id(device)]
        signals: dict[str, SignalEnvelope] = {}
        for name in sorted(stream.signals):
            profile = profiles[_signal_id(device, name)]
            if profile.band is None:
                continue
            signals[name] = SignalEnvelope(
                n=profile.n,
                n_eff=profile.n_eff,
                coverage=profile.requested_coverage,
                confidence=profile.achieved_confidence,
                band=profile.band,
                variance_floor=profile.variance_floor,
            )
        conditions = {
            name: profile.band
            for name in sorted(recording.env)
            if (profile := profiles[_condition_id(name)]).band is not None
        }
        fitted_bands: dict[str, FittedBandEvidence] = {}
        if rate.band is not None:
            fitted_bands["rate_hz"] = FittedBandEvidence(
                rank=rate.rank_from_edge,
                n_eff=rate.effective_n,
                alpha=rate.alpha_channel,
            )
        for name in sorted(signals):
            profile = profiles[_signal_id(device, name)]
            fitted_bands[f"signals/{name}"] = FittedBandEvidence(
                rank=profile.rank_from_edge,
                n_eff=profile.effective_n,
                alpha=profile.alpha_channel,
            )
        for name in sorted(conditions):
            profile = profiles[_condition_id(name)]
            fitted_bands[f"conditions/{name}"] = FittedBandEvidence(
                rank=profile.rank_from_edge,
                n_eff=profile.effective_n,
                alpha=profile.alpha_channel,
            )
        envelopes[device] = ContextEnvelope(
            context=context,
            n=rate.n,
            n_eff=rate.n_eff,
            coverage=rate.requested_coverage if rate.band is not None else None,
            confidence=rate.achieved_confidence,
            rate_hz=rate.band,
            signals=signals,
            conditions=conditions,
            fitted_bands=fitted_bands,
            provenance=provenance,
        )

    return BaselineArtifact(
        context=context,
        recording_sha256=recording.sha256,
        source=str(recording.path),
        envelopes=envelopes,
        fitted_band_allocations=_fitted_band_allocations(
            recording,
            profiles,
            context=context,
        ),
        profiles=profiles,
        start_timestamp_s=start_timestamp_s,
        end_timestamp_s=end_timestamp_s,
        duration_s=end_timestamp_s - start_timestamp_s,
        known_good_rationale=known_good_rationale,
        operator=operator,
        setup=setup,
        firmware=firmware,
        calibration=calibration,
        dropped_messages=dropped_messages,
        recording_completeness=recording_completeness,
    )


def _comb_or_zero(n: int, k: int) -> int:
    return 0 if k < 0 or k > n else comb(n, k)


def _exceedance_tail(m: int, n: int, b: int, k: int) -> Fraction:
    pooled_rank = b + k - 1
    numerator = sum(
        comb(m, healthy_exceedances) * _comb_or_zero(n, pooled_rank - healthy_exceedances)
        for healthy_exceedances in range(b)
    )
    return Fraction(numerator, comb(m + n, pooled_rank))


def _critical_exceedance_count(m: int, n: int, b: int, alpha: float) -> int:
    """Smallest k whose exact exchangeability tail is within ``alpha``."""
    target = Fraction(str(alpha))
    if _exceedance_tail(m, n, b, 1) <= target:
        return 1
    if _exceedance_tail(m, n, b, n) > target:
        return n + 1
    low, high = 2, n
    while low < high:
        candidate = (low + high) // 2
        if _exceedance_tail(m, n, b, candidate) <= target:
            high = candidate
        else:
            low = candidate + 1
    return low


@dataclass(frozen=True, slots=True)
class BudgetedExceedance:
    """One side of a fitted-band decision that exhausted its allocated budget."""

    side: Literal["below", "above"]
    exceedances: int
    critical_count: int
    current_n: int
    baseline_n_eff: int
    rank: int
    alpha_side: float
    achieved_tail: Fraction
    observed_value: float
    bound: float

    def summary(self) -> str:
        return (
            f"{self.side}: K={self.exceedances}, k*={self.critical_count}, "
            f"n={self.current_n}, m={self.baseline_n_eff}, b={self.rank}, "
            f"allocated alpha_side={self.alpha_side:.17g}, "
            f"achieved tail probability={float(self.achieved_tail):.17g}"
        )


def budgeted_exceedances(
    values: np.ndarray,
    band: Band,
    evidence: FittedBandEvidence,
    *,
    detrend: bool,
    decorrelation: int | None = None,
    raw_level: bool = False,
) -> list[BudgetedExceedance]:
    """Apply the locked §5 fitted-band decision to one current capture.

    ``raw_level`` also tests the undetrended samples against the band: detrending removes a
    constant offset, so a band declared in real units would never see a level shift (M15).
    """
    series = [cubic_detrend(values).residual if detrend else values]
    if detrend and raw_level:
        series.append(values)
    decisions: list[BudgetedExceedance] = []
    for envelope_values in series:
        stride = max(
            1,
            decorrelation if decorrelation is not None else decorrelation_length(envelope_values),
        )
        reduced = envelope_values[::stride]
        n = int(reduced.size)
        if n == 0:
            continue
        alpha_side = evidence.alpha / 2
        critical = _critical_exceedance_count(evidence.n_eff, n, evidence.rank, alpha_side)
        for side, count, observed_value, bound in (
            ("below", int(np.count_nonzero(reduced < band.lo)), float(np.min(reduced)), band.lo),
            ("above", int(np.count_nonzero(reduced > band.hi)), float(np.max(reduced)), band.hi),
        ):
            if count >= critical and all(d.side != side for d in decisions):
                decisions.append(
                    BudgetedExceedance(
                        side=side,
                        exceedances=count,
                        critical_count=critical,
                        current_n=n,
                        baseline_n_eff=evidence.n_eff,
                        rank=evidence.rank,
                        alpha_side=alpha_side,
                        achieved_tail=_exceedance_tail(evidence.n_eff, n, evidence.rank, count),
                        observed_value=observed_value,
                        bound=bound,
                    )
                )
    return decisions


def _converted(values: list[float] | np.ndarray, from_unit: str, to_unit: str) -> np.ndarray:
    canonical_values, canonical_unit = _canonical_values(values, from_unit)
    if canonical_unit == to_unit:
        return canonical_values
    return np.asarray(
        [units.convert(float(value), canonical_unit, to_unit) for value in canonical_values]
    )


def _outside_hard_limit(values: np.ndarray, limit: Band) -> bool:
    return bool(np.any(values < limit.lo) or np.any(values > limit.hi))


def _finite_only(
    values: np.ndarray,
    channel_id: str,
    cluster_root: str,
    findings: list[BaselineFinding],
    reported: set[str],
) -> np.ndarray:
    """Drop NaN/inf samples so they are never scored as a pass; say so once per channel."""
    finite = values[np.isfinite(values)]
    if finite.size != values.size and channel_id not in reported:
        reported.add(channel_id)
        findings.append(
            BaselineFinding(
                mechanism="non-finite-sample",
                channels=(channel_id,),
                detail=(
                    f"{channel_id}: {values.size - finite.size} of {values.size} samples "
                    "were not finite and were not scored"
                ),
                cluster_root=cluster_root,
                cluster_members=(channel_id,),
            )
        )
    return finite


def _wilks_shifted(values: np.ndarray, profile: ChannelProfile) -> bool:
    if profile.band is None or profile.rank_from_edge is None:
        return False
    evidence = FittedBandEvidence(
        rank=profile.rank_from_edge,
        n_eff=profile.effective_n,
        alpha=profile.alpha_channel,
    )
    return bool(
        budgeted_exceedances(
            values,
            profile.band,
            evidence,
            detrend=profile.kind == "signal",
            decorrelation=profile.decorrelation,
        )
    )


def _drifted(values: np.ndarray, profile: ChannelProfile) -> bool:
    if (
        profile.drift_reference is None
        or profile.drift_residual_scale is None
        or profile.drift_residual_decorrelation is None
        or values.size < 4
    ):
        return False
    detrended = cubic_detrend(values)
    decision_interval = drift_decision_interval(
        sample_count=int(values.size),
        baseline_statistic=profile.drift_reference,
        residual_scale=profile.drift_residual_scale,
        residual_decorrelation=profile.drift_residual_decorrelation,
        alpha_channel=profile.alpha_channel,
    )
    return trend_cusum_statistic(detrended.trend) > decision_interval


def check_recording(
    recording: Recording,
    baseline: BaselineArtifact,
    *,
    signal_hard_limits: dict[tuple[str, str], Band] | None = None,
    condition_hard_limits: dict[str, Band] | None = None,
    nominal_rates_hz: dict[str, float] | None = None,
) -> list[BaselineFinding]:
    """Compare a capture with a baseline, using hard limits for refused bands."""
    if recording.context != baseline.context:
        raise ValueError(
            f"recording context {recording.context!r} does not match baseline "
            f"context {baseline.context!r}"
        )
    signal_hard_limits = signal_hard_limits or {}
    condition_hard_limits = condition_hard_limits or {}
    nominal_rates_hz = nominal_rates_hz or {}
    findings: list[BaselineFinding] = []
    cluster_roots = _cluster_identities(recording)
    nonfinite_reported: set[str] = set()

    condition_channels: list[str] = []
    for name, limit in sorted(condition_hard_limits.items()):
        if name not in recording.env:
            continue
        values = _converted(
            [value for _, value in recording.env[name]],
            recording.unit_of(name, device="environment"),
            limit.unit,
        )
        values = _finite_only(
            values,
            _condition_id(name),
            cluster_roots[_condition_id(name)],
            findings,
            nonfinite_reported,
        )
        if _outside_hard_limit(values, limit):
            condition_channels.append(_condition_id(name))
    if condition_channels:
        findings.append(
            BaselineFinding(
                mechanism="condition-envelope",
                channels=tuple(condition_channels),
                detail="current conditions exceed the hand-authored supported region",
                cluster_root=cluster_roots[condition_channels[0]],
                cluster_members=tuple(condition_channels),
            )
        )

    handled_signals: set[str] = set()
    for (device, name), limit in sorted(signal_hard_limits.items()):
        stream = recording.devices.get(device)
        if stream is None or name not in stream.signals:
            continue
        values = _converted(
            stream.signals[name],
            recording.unit_of(name, device=device),
            limit.unit,
        )
        channel_id = _signal_id(device, name)
        values = _finite_only(
            values, channel_id, cluster_roots[channel_id], findings, nonfinite_reported
        )
        if _outside_hard_limit(values, limit):
            findings.append(
                BaselineFinding(
                    mechanism="hard-limit-exceedance",
                    channels=(channel_id,),
                    detail=f"{channel_id} exceeds {limit}",
                    cluster_root=cluster_roots[channel_id],
                    cluster_members=(channel_id,),
                )
            )
            handled_signals.add(channel_id)

    for device, stream in sorted(recording.devices.items()):
        for name, samples in sorted(stream.signals.items()):
            channel_id = _signal_id(device, name)
            if channel_id in handled_signals:
                continue
            profile = baseline.profiles.get(channel_id)
            if profile is None:
                findings.append(
                    BaselineFinding(
                        mechanism="channel-unseen",
                        channels=(channel_id,),
                        detail=f"{channel_id} was not in the baseline, so it was not scored",
                        cluster_root=cluster_roots[channel_id],
                        cluster_members=(channel_id,),
                        severity="not_scored",
                    )
                )
                continue
            values = _converted(
                samples,
                recording.unit_of(name, device=device),
                profile.unit,
            )
            values = _finite_only(
                values, channel_id, cluster_roots[channel_id], findings, nonfinite_reported
            )
            if values.size == 0:
                continue
            if (
                profile.variance_floor is not None
                and profile.cusum_window_size is not None
                and profile.cusum_decision_interval is not None
                and variance_cusum(
                    values,
                    profile.variance_floor,
                    window_size=profile.cusum_window_size,
                    decision_interval=profile.cusum_decision_interval,
                ).tripped
            ):
                findings.append(
                    BaselineFinding(
                        mechanism="cusum",
                        channels=(channel_id,),
                        detail=f"{channel_id} has sustained sub-baseline variance",
                        cluster_root=cluster_roots[channel_id],
                        cluster_members=(channel_id,),
                    )
                )
                continue
            if _drifted(values, profile):
                findings.append(
                    BaselineFinding(
                        mechanism="drift-cusum",
                        channels=(channel_id,),
                        detail=f"{channel_id} has excess slow drift",
                        cluster_root=cluster_roots[channel_id],
                        cluster_members=(channel_id,),
                    )
                )
                continue
            if profile.status == "locked" and _wilks_shifted(values, profile):
                findings.append(
                    BaselineFinding(
                        mechanism="wilks-band-exceedance",
                        channels=(channel_id,),
                        detail=f"{channel_id} population shifted beyond its Wilks band",
                        cluster_root=cluster_roots[channel_id],
                        cluster_members=(channel_id,),
                    )
                )

    for device, stream in sorted(recording.devices.items()):
        profile = baseline.profiles.get(_rate_id(device))
        if profile is None:
            continue  # the unseen device's channels are already reported as channel-unseen
        rates = _instantaneous_rates(stream.recv_times)
        tripped = profile.status == "locked" and _wilks_shifted(rates, profile)
        nominal = nominal_rates_hz.get(device)
        if not tripped and profile.status == "under-sampled" and nominal is not None:
            observed = (
                (len(stream.recv_times) - 1) / stream.duration_s if stream.duration_s > 0.0 else 0.0
            )
            tripped = observed < nominal and not math.isclose(
                observed, nominal, rel_tol=1e-9, abs_tol=1e-12
            )
        if tripped:
            findings.append(
                BaselineFinding(
                    mechanism="rate-check",
                    channels=(_rate_id(device),),
                    detail=f"{device} receive rate is below its supported cadence",
                    cluster_root=cluster_roots[_rate_id(device)],
                    cluster_members=(_rate_id(device),),
                )
            )

    # A declared device the recording never carried was not measured.
    absent = sorted(
        {p.device for p in baseline.profiles.values() if p.device} - set(recording.devices)
    )
    for device in absent:
        members = tuple(sorted(p.id for p in baseline.profiles.values() if p.device == device))
        findings.append(
            BaselineFinding(
                mechanism="device-absent",
                channels=members,
                detail=f"{device} is in the baseline but absent from this recording; not scored",
                cluster_members=members,
                severity="not_scored",
                device=device,
            )
        )

    return findings


def _percent(value: float | None) -> str:
    return "not achieved" if value is None else f"{value:.2%}"


def _baseline_value(value: object | None, *, suffix: str = "") -> str:
    if value is None:
        return "[bold white on red] GAP [/]"
    return f"{value}{suffix}"


def _format_band(band: Band) -> str:
    """Render a fitted band the way ``format_magnitude`` renders one value, as a range."""
    return f"{band.lo:.2f}–{band.hi:.2f} {band.unit}"


def _render_baseline_channel(channel: BaselineReviewChannel, console: Console) -> None:
    if channel.status == "under-sampled":
        console.print(f"\n[bold white on red] UNDER-SAMPLED [/] [bold]{channel.id}[/]")
    else:
        console.print(f"\n[bold green]LOCKED[/] [bold]{channel.id}[/]")

    gap = channel.n / channel.n_eff if channel.n_eff > 0 else float("inf")
    gap_text = f"{gap:,.1f}× raw/effective"
    if gap >= LARGE_EVIDENCE_GAP:
        gap_text = f"[bold red]{gap_text} — LARGE GAP[/]"
    console.print(
        f"  evidence: n=[bold]{channel.n:,}[/]  |  "
        f"n_eff=[bold]{channel.n_eff:,.1f}[/]  |  {gap_text}"
    )

    if channel.status == "under-sampled":
        shortfall = max(0, channel.min_n_required - channel.effective_n)
        band_suffix = f" {_format_band(channel.fitted_band)}" if channel.fitted_band else ""
        console.print(
            "  envelope: [bold red]NOT LOCKED[/] — "
            f"effective n={channel.effective_n:,}, minimum={channel.min_n_required:,} "
            f"([bold red]short by {shortfall:,}[/]){band_suffix}"
        )
        observed_range = channel.observed_range
        if observed_range is None:
            rendered_range = _baseline_value(None)
        else:
            rendered_range = (
                f"[bold]{observed_range.lo:g} to {observed_range.hi:g} {observed_range.unit}[/]"
            )
        console.print(f"  observed range: {rendered_range}")
    else:
        band_suffix = f" {_format_band(channel.fitted_band)}" if channel.fitted_band else ""
        console.print(f"  envelope: [bold green]LOCKED[/]{band_suffix}")
        if channel.observed_range is not None:
            observed_range = channel.observed_range
            console.print(
                "  observed range: "
                f"[bold]{observed_range.lo:g} to {observed_range.hi:g} {observed_range.unit}[/]"
            )

    console.print(
        "  coverage / confidence achieved: "
        f"{_percent(channel.achieved_coverage)} / {_percent(channel.achieved_confidence)}"
    )
    console.print(
        "  coverage / confidence requested: "
        f"{_percent(channel.requested_coverage)} / {_percent(channel.requested_confidence)}"
    )
    console.print(f"  decorrelation length: {channel.decorrelation:,} samples")


def render_baseline_review(review: BaselineReview, console: Console | None = None) -> None:
    """Render baseline evidence for a human decision without mutating or blessing it."""
    console = console or _console()
    under_sampled = sum(channel.status == "under-sampled" for channel in review.channels)
    large_gaps = sum(
        channel.n_eff <= 0 or channel.n / channel.n_eff >= LARGE_EVIDENCE_GAP
        for channel in review.channels
    )

    console.print("[bold]BASELINE REVIEW — HUMAN DECISION REQUIRED[/]")
    console.print(f"context: {review.context}")
    console.print(f"source: {review.source}")
    console.print("recording sha256:")
    console.print(f"  [bold]{review.recording_sha256}[/]")
    console.print("recording timing:")
    console.print(f"  start timestamp: {_baseline_value(review.start_timestamp_s, suffix=' s')}")
    console.print(f"  end timestamp: {_baseline_value(review.end_timestamp_s, suffix=' s')}")
    console.print(f"  duration: {_baseline_value(review.duration_s, suffix=' s')}")
    console.print("known-good provenance:")
    console.print(f"  rationale: {_baseline_value(review.known_good_rationale)}")
    console.print(f"  operator: {_baseline_value(review.operator)}")
    console.print(f"  setup: {_baseline_value(review.setup)}")
    console.print(f"  firmware: {_baseline_value(review.firmware)}")
    console.print(f"  calibration: {_baseline_value(review.calibration)}")
    console.print("recording completeness:")
    console.print(f"  dropped messages: {_baseline_value(review.dropped_messages)}")
    console.print(f"  assessment: {_baseline_value(review.recording_completeness)}")
    console.print("This report does not approve, bless, or modify the baseline.")
    if under_sampled or large_gaps:
        console.print(
            "\n[bold white on red] WEAK EVIDENCE [/] "
            f"[bold red]{under_sampled}/{len(review.channels)} channels under-sampled; "
            f"{large_gaps}/{len(review.channels)} have a ≥{LARGE_EVIDENCE_GAP}× "
            "n-to-n_eff gap[/]"
        )
    else:
        console.print("\n[bold green]No under-sampled channels or large evidence gaps detected.[/]")

    for channel in review.channels:
        _render_baseline_channel(channel, console)


def _timing_float(timing: Mapping[str, object], field: str) -> float:
    """Artifact timing is required as float for check; missing fields collapse to 0.0."""
    value = _optional_float(timing, field)
    return 0.0 if value is None else value


def parse_baseline_artifact(data: object) -> BaselineArtifact:
    """Rebuild a checkable baseline from the YAML ``to_dict()`` emits.

    ``parse_baseline_review`` only restores review evidence. ``check_recording``
    needs the full channel profiles (Wilks band, CUSUM, drift). This adapter is
    the round-trip that makes a blessed artifact file operable. Shared
    completeness / provenance / timing guards come from ``baseline`` so check
    and review reject the same corruptions.
    """
    root = _mapping(data, "root")
    if root.get("artifact_version") != "1":
        raise ValueError("baseline artifact has unsupported or missing artifact_version")

    context = root.get("context")
    if not isinstance(context, str):
        raise ValueError("baseline artifact context must be a string")

    recording = _mapping(root.get("recording"), "recording")
    source = _required(recording, "source")
    sha256 = _required(recording, "sha256")
    if not isinstance(source, str) or not isinstance(sha256, str):
        raise ValueError("baseline artifact recording source and sha256 must be strings")

    timing = _mapping(recording.get("timing", {}), "recording timing")
    completeness = _mapping(recording.get("completeness", {}), "recording completeness")
    provenance = _mapping(root.get("provenance", {}), "provenance")
    serialized_channels = _mapping(root.get("channels"), "channels")
    if not serialized_channels:
        raise ValueError("baseline artifact contains no channels")

    profiles: dict[str, ChannelProfile] = {}
    for channel_id, serialized in serialized_channels.items():
        if not isinstance(channel_id, str):
            raise ValueError("baseline artifact channel ids must be strings")
        channel = _mapping(serialized, f"channel {channel_id!r}")
        status = _required(channel, "status", channel_id)
        if status not in ("locked", "under-sampled"):
            raise ValueError(f"baseline artifact channel {channel_id!r} has invalid status")
        kind = _required(channel, "kind", channel_id)
        if kind not in ("rate", "signal", "condition"):
            raise ValueError(f"baseline artifact channel {channel_id!r} has invalid kind")
        cusum = channel.get("cusum")
        cusum_data = _mapping(cusum, f"channel {channel_id!r} cusum") if cusum is not None else {}
        drift = channel.get("drift_cusum")
        drift_data = (
            _mapping(drift, f"channel {channel_id!r} drift_cusum") if drift is not None else {}
        )
        device = channel.get("device")
        if device is not None and not isinstance(device, str):
            raise ValueError(f"baseline artifact channel {channel_id!r} device must be a string")
        try:
            profiles[channel_id] = ChannelProfile(
                id=channel_id,
                kind=kind,
                name=str(_required(channel, "name", channel_id)),
                device=device,
                unit=str(_required(channel, "unit", channel_id)),
                status=status,
                n=int(_required(channel, "n", channel_id)),
                n_eff=float(_required(channel, "n_eff", channel_id)),
                effective_n=int(_required(channel, "effective_n", channel_id)),
                decorrelation=int(_required(channel, "decorrelation", channel_id)),
                requested_coverage=float(_required(channel, "requested_coverage", channel_id)),
                requested_confidence=float(_required(channel, "requested_confidence", channel_id)),
                min_n_required=int(_required(channel, "min_n_required", channel_id)),
                achieved_confidence=(
                    float(channel["achieved_confidence"])
                    if "achieved_confidence" in channel
                    else None
                ),
                rank_from_edge=(
                    int(channel["rank_from_edge"]) if "rank_from_edge" in channel else None
                ),
                band=(
                    Band.model_validate(_mapping(channel["band"], f"channel {channel_id!r} band"))
                    if "band" in channel
                    else None
                ),
                observed_range=Band.model_validate(
                    _mapping(
                        _required(channel, "observed_range", channel_id),
                        f"channel {channel_id!r} observed_range",
                    )
                ),
                alpha_channel=float(_required(channel, "alpha_channel", channel_id)),
                variance_floor=(
                    float(cusum_data["variance_floor"]) if "variance_floor" in cusum_data else None
                ),
                cusum_window_size=(
                    int(cusum_data["window_size"]) if "window_size" in cusum_data else None
                ),
                cusum_decision_interval=(
                    float(cusum_data["decision_interval"])
                    if "decision_interval" in cusum_data
                    else None
                ),
                cusum_achieved_alpha=(
                    float(cusum_data["achieved_alpha"]) if "achieved_alpha" in cusum_data else None
                ),
                drift_reference=(
                    float(drift_data["reference"]) if "reference" in drift_data else None
                ),
                drift_residual_scale=(
                    float(drift_data["residual_scale"]) if "residual_scale" in drift_data else None
                ),
                drift_residual_decorrelation=(
                    int(drift_data["residual_decorrelation"])
                    if "residual_decorrelation" in drift_data
                    else None
                ),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"baseline artifact channel {channel_id!r} has invalid check evidence"
            ) from error

    return BaselineArtifact(
        context=context,
        recording_sha256=sha256,
        source=source,
        envelopes={},
        profiles=profiles,
        start_timestamp_s=_timing_float(timing, "start_timestamp_s"),
        end_timestamp_s=_timing_float(timing, "end_timestamp_s"),
        duration_s=_timing_float(timing, "duration_s"),
        known_good_rationale=_optional_string(provenance, "known_good_rationale"),
        operator=_optional_string(provenance, "operator"),
        setup=_optional_string(provenance, "setup"),
        firmware=_optional_string(provenance, "firmware"),
        calibration=_optional_string(provenance, "calibration"),
        dropped_messages=_optional_dropped_messages(completeness),
        recording_completeness=_optional_string(completeness, "recording_completeness"),
    )


def baseline_findings_as_report(findings: list[BaselineFinding]) -> list[Finding]:
    """Project baseline findings onto the existing report Finding grammar."""
    return [
        Finding(
            severity=finding.severity,
            quantity="preflight/recording"
            if finding.device
            else finding.channels[0]
            if len(finding.channels) == 1
            else finding.mechanism,
            observed=f"{finding.device} declared and not seen"
            if finding.device
            else finding.mechanism,
            expected="within blessed baseline",
            action=finding.detail,
            cluster_root=finding.cluster_root,
            cluster_members=finding.cluster_members,
        )
        for finding in findings
    ]


def baseline_record_report(findings: list[BaselineFinding], passed: list) -> RecordReport:
    """The one report both baseline-check doors render. A declared device never measured
    withholds GO by name: the headline reads the absent finding, not a zero count."""
    compared = len(passed) + sum(f.severity == "violation" for f in findings)
    return RecordReport(
        baseline_findings_as_report(findings), scored=compared, scored_records=passed
    )
