"""Preflight stage runner: ordered registry, stage identity stamp, no stage logic.

Contract §3.2: every stage emits ``Finding``; this module only dispatches and
stamps ``Finding.stage``. Stage bodies live under ``preflight.stages``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from lockwire.checks import _ScoreBook
from lockwire_pro.preflight.envelopes import signal_view_for_context
from lockwire_pro.preflight.source import GraphUnavailable, PreflightSource
from lockwire_pro.preflight.stages import (
    commands,
    coverage,
    cross_record,
    cross_signal,
    enumeration,
    flow,
    liveness,
    sanity,
)
from lockwire_pro.recording import Recording
from lockwire_pro.preflight.report import (
    EvidenceClass,
    PreflightProfile,
    PreflightReport,
    preflight_policy,
)
from lockwire.report import Finding
from lockwire.schema import Contract

StageFn = Callable[[Contract, PreflightSource], list[Finding]]


class SamplesUnavailable(Exception):
    """A ``PreflightSource`` cannot provide ``samples()``.

    Stage-scoped doubles raise this so ``run_preflight`` can still dispatch
    stages that never need the signal stream. Stages that call ``samples()``
    become ``not_scored`` rather than detonating the whole run.
    """


@dataclass(frozen=True, slots=True)
class StageEntry:
    """One registry row. Parallel stage boxes replace the module, not this row."""

    stage: int
    name: str
    check: StageFn
    implemented: bool


def _entry(stage: int, name: str, module: object) -> StageEntry:
    check = getattr(module, "check")
    implemented = bool(getattr(module, "IMPLEMENTED", True))
    return StageEntry(stage=stage, name=name, check=check, implemented=implemented)


# Ordered registry. Stage boxes replace their module body; do not edit this list
# to "activate" a stage — set ``IMPLEMENTED = True`` in the stage module instead.
STAGES: tuple[StageEntry, ...] = (
    _entry(0, "enumeration", enumeration),
    _entry(1, "liveness", liveness),
    _entry(2, "flow", flow),
    _entry(3, "sanity", sanity),
    _entry(4, "cross_record", cross_record),
    _entry(4, "cross_signal", cross_signal),
    _entry(4, "coverage", coverage),
    _entry(4, "commands", commands),
)


# A device record with fewer samples or less span than this is not scored.
FLOOR_SAMPLES = 10
FLOOR_SPAN_S = 1.0


def _floored(recording: Recording) -> tuple[Recording, dict[str, str]]:
    """Refuse a manifest that overstates the bag; hide under-floor devices.

    A device record under the floor is dropped from the view every stage reads,
    so no stage or counter scores it; the reason is returned for the run's cap.
    """
    times = [t for s in recording.devices.values() for t in s.recv_times]
    times += [t for rows in recording.env.values() for t, _ in rows]
    declared = recording.manifest_duration_s
    if times and declared is not None and not recording.partial:
        span = max(times) - min(times)
        if declared > 2 * span:
            raise ValueError(
                f"recording {recording.path}: manifest duration_s {declared:g} s is more than "
                f"twice the {span:.2f} s its samples span; the bag is truncated or mislabelled"
            )
    reasons = {
        device_id: (
            f"{len(stream.recv_times)} sample(s) over {stream.duration_s:.2f} s; the floor is "
            f"{FLOOR_SAMPLES} samples and {FLOOR_SPAN_S:g} s"
        )
        for device_id, stream in recording.devices.items()
        if len(stream.recv_times) < FLOOR_SAMPLES or stream.duration_s < FLOOR_SPAN_S
    }
    if not reasons:
        return recording, reasons
    kept = {k: v for k, v in recording.devices.items() if k not in reasons}
    return replace(recording, devices=kept), reasons


@dataclass
class _LazySource:
    """Delegate enumerate/liveness; fetch ``samples()`` once, only on demand.

    Stages that never call ``samples()`` never touch the inner source's
    samples method — so a stage-0/1 double that cannot provide a recording
    stays usable under full ``run_preflight`` dispatch (B87/B88/B90 consumers
    rebase cleanly: they just call ``source.samples()`` as usual).
    """

    _inner: PreflightSource
    _enumeration: Any = field(default=None, init=False, repr=False)
    _liveness: Any = field(default=None, init=False, repr=False)
    _samples: Recording | None = field(default=None, init=False, repr=False)
    _samples_error: SamplesUnavailable | None = field(default=None, init=False, repr=False)
    _under_floor: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _raw_empty: bool = field(default=False, init=False, repr=False)

    @property
    def scores_absence(self) -> bool:
        """Forward LiveSource's absence-scoring flag through the lazy wrapper."""
        return bool(getattr(self._inner, "scores_absence", False))

    def host_side_disclosures(self) -> list[Finding]:
        """Forward LiveSource host-side §3.1 disclosures through the lazy wrapper."""
        inner = getattr(self._inner, "host_side_disclosures", None)
        if callable(inner):
            return list(inner())
        return []

    def enumerate(self) -> Any:
        if self._enumeration is None:
            self._enumeration = self._inner.enumerate()
        return self._enumeration

    def liveness(self) -> Any:
        if self._liveness is None:
            self._liveness = self._inner.liveness()
        return self._liveness

    def samples(self) -> Recording:
        if self._samples_error is not None:
            raise self._samples_error
        if self._samples is not None:
            return self._samples
        try:
            raw = self._inner.samples()
        except SamplesUnavailable as exc:
            self._samples_error = exc
            raise
        if not isinstance(raw, Recording):  # a stage-scoped test double
            self._samples = raw
            return raw
        self._raw_empty = not raw.env and not any(s.recv_times for s in raw.devices.values())
        self._samples, self._under_floor = _floored(raw)
        return self._samples


def _samples_unavailable_finding(entry: StageEntry) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"preflight/stage/{entry.stage}/{entry.name}",
        observed="samples unavailable from preflight source",
        expected="signal stream for stages that consult samples()",
        action="provide a PreflightSource that implements samples()",
    )


def _graph_unavailable_finding(entry: StageEntry, error: GraphUnavailable) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"preflight/stage/{entry.stage}/{entry.name}",
        observed=str(error),
        expected="the ROS graph listable via ros2 node/topic list",
        action=(
            "check ROS_DOMAIN_ID and QoS (reliability, depth) first, then whether ros2 is "
            "on PATH and the daemon is up — the command failed, not the graph"
        ),
    )


# Stages that accept a ``scores: _ScoreBook`` kwarg and record a ScoredRecord
# on their passing branch (M1 health follow-up: a healthy device needs at
# least one recorded pass, not just an absence of findings). enumeration,
# liveness and coverage are untouched — their pass/fail is presence, not a
# band/rate comparison, and this box's fixture proof does not need them.
_SCORED_STAGES = frozenset({"sanity", "flow", "cross_record", "cross_signal"})


def _dispatch_stage(
    entry: StageEntry,
    contract: Contract,
    source: PreflightSource,
    scores: _ScoreBook | None = None,
) -> list[Finding]:
    """Run one stage; convert deliberate samples gaps into not_scored."""
    try:
        if entry.name in _SCORED_STAGES:
            return list(entry.check(contract, source, scores=scores))
        return list(entry.check(contract, source))
    except SamplesUnavailable:
        return [_samples_unavailable_finding(entry)]
    except GraphUnavailable as error:
        return [_graph_unavailable_finding(entry, error)]


def _determined(findings: list[Finding]) -> int:
    """Count one armed check when its outcome is pass, warning, or violation."""
    return 0 if any(finding.severity == "not_scored" for finding in findings) else 1


def _enumeration_scored(contract: Contract, source: PreflightSource) -> int:
    observed_by_id = {observation.device_id: observation for observation in source.enumerate()}
    scored = 0
    for device in contract.devices:
        if device.identity is None:
            continue
        observed = observed_by_id.get(device.id)
        if observed is None:
            scored += int(bool(getattr(source, "scores_absence", False)))
            continue
        scored += 1  # declared presence versus observed presence
        scored += sum(
            getattr(device.identity, field) is not None and getattr(observed, field) is not None
            for field in enumeration._IDENTITY_FIELDS
        )
    return scored


def _liveness_scored(contract: Contract, source: PreflightSource) -> int:
    observed_by_id = {observation.device_id: observation for observation in source.liveness()}
    scored = 0
    for device in contract.devices:
        if device.driver is None:
            continue
        observed = observed_by_id.get(device.id)
        if observed is None:
            scored += int(bool(getattr(source, "scores_absence", False)))
            continue
        scored += 1  # declared liveness versus observed liveness
        scored += sum(
            getattr(device.driver, field) is not None and getattr(observed, field) is not None
            for field in liveness._DRIVER_FIELDS
        )
    return scored


def _flow_scored(contract: Contract, source: PreflightSource) -> int:
    recording = source.samples()
    scored = 0
    for device in contract.devices:
        envelope = flow._rate_envelope_for_context(device, recording.context)
        band = envelope.rate_hz if envelope is not None else None
        fitted = envelope.fitted_bands.get("rate_hz") if envelope is not None else None
        nominal = device.driver.nominal_rate_hz if device.driver is not None else None
        if band is None and nominal is None:
            continue
        stream = recording.devices.get(device.id)
        if stream is None or not stream.recv_times:
            continue
        findings = flow._device_flow_findings(device, recording)
        if band is None:
            gross = [
                finding
                for finding in findings
                if finding.quantity == f"devices/{device.id}/sustained_rate_liveness"
            ]
            scored += _determined(gross)
            continue
        rate = [
            finding for finding in findings if finding.quantity == f"devices/{device.id}/rate_hz"
        ]
        scored += _determined(rate)
        if fitted is not None and nominal is not None and nominal > 0.0:
            gross = [
                finding
                for finding in findings
                if finding.quantity == f"devices/{device.id}/sustained_rate_liveness"
            ]
            scored += _determined(gross)
        if fitted is None and band.lo > 0.0:
            gap = [
                finding
                for finding in findings
                if finding.quantity == f"devices/{device.id}/recv_gap"
            ]
            scored += _determined(gap)
    return scored


def _sanity_scored(contract: Contract, source: PreflightSource) -> int:
    recording = source.samples()
    scored = 0
    for device in contract.devices:
        model = contract.device_model(device.model)
        hard_limits = model.hard_limits if model is not None else {}
        signal_view = signal_view_for_context(device, recording.context)
        envelope_signals = signal_view.signals
        for channel in sorted(set(hard_limits) | set(envelope_signals)):
            stream = recording.devices.get(device.id)
            samples = stream.signals.get(channel) if stream is not None else None
            if samples is None:
                continue
            raw = sanity.np.asarray(samples, dtype=float)
            scored += 1  # finite-sample check
            if sanity.np.any(~sanity.np.isfinite(raw)):
                continue
            declaration = envelope_signals.get(channel)
            signal = declaration.signal if declaration is not None else None
            hard_limit = hard_limits.get(channel)
            if signal is not None:
                scored += _determined(
                    sanity._range_findings(
                        device=device,
                        quantity=f"devices/{device.id}/signals/{channel}/range",
                        raw=raw,
                        recording=recording,
                        channel=channel,
                        limit=signal.band,
                        role="envelope band",
                        fitted=declaration.fitted,
                    )
                )
            if hard_limit is not None:
                scored += _determined(
                    sanity._range_findings(
                        device=device,
                        quantity=f"devices/{device.id}/signals/{channel}/hard_limit",
                        raw=raw,
                        recording=recording,
                        channel=channel,
                        limit=hard_limit,
                        role="hard limit",
                    )
                )
            scored += _determined(
                sanity._variance_leg(
                    device=device,
                    quantity=f"devices/{device.id}/signals/{channel}/variance",
                    raw=raw,
                    recording=recording,
                    channel=channel,
                    signal=signal,
                )
            )
    return scored


def _cross_record_scored(contract: Contract, source: PreflightSource) -> int:
    recording = source.samples()
    # Review 2026-10-20 H5: budget rows read only the contract, so they say
    # nothing about the robot and never count toward a preflight's scored total.
    scored = 0

    axis_units = cross_record._declared_axis_units(contract, recording.context)
    resolved_axes = {
        axis
        for axis, units in axis_units.items()
        if len(units) == 1
        and not isinstance(cross_record._resolve_axis(axis, next(iter(units)), recording), str)
    }
    for device in contract.devices:
        device_axes = {
            axis
            for envelope in device.envelopes
            if envelope.context == recording.context and envelope.conditions
            for axis in envelope.conditions
        }
        scored += len(device_axes & resolved_axes)
    return scored


def _cross_signal_scored(contract: Contract, source: PreflightSource) -> int:
    recording = source.samples()
    if recording.context not in cross_signal._STATIONARY_CONTEXTS:
        return 0
    scored = 0
    for device_id, stream in cross_signal._accel_devices(recording):
        finding = cross_signal._score_device_g(contract, recording.context, device_id, stream)
        if finding is None or finding.severity != "not_scored":
            scored += 1
    return scored


def _count_stage_scored(
    entry: StageEntry,
    contract: Contract,
    source: PreflightSource,
) -> int:
    counters = {
        "enumeration": _enumeration_scored,
        "liveness": _liveness_scored,
        "flow": _flow_scored,
        "sanity": _sanity_scored,
        "cross_record": _cross_record_scored,
        "cross_signal": _cross_signal_scored,
        "coverage": lambda _contract, _source: 0,
        "commands": commands.scored,
    }
    return counters[entry.name](contract, source)


def _cap_findings(contract: Contract, view: _LazySource) -> list[Finding]:
    """Why this run cannot be GO, one finding per reason.

    ``report.verdict`` reads any ``not_scored`` on ``preflight/recording`` as a cap:
    at most NOT SCORED, even inside a plain list (review M30).
    """
    recording = view._samples
    if recording is None:
        error = view._samples_error or "no stage read the recording"
        reasons = [f"no recording to score: {error}"]
    elif view._raw_empty:
        reasons = ["recording has no data"]
    else:
        reasons = ["partial recording"] if getattr(recording, "partial", False) else []
        for device in contract.devices:
            if device.driver is None or device.id in recording.devices:
                continue
            why = view._under_floor.get(device.id, "declared with a driver and not seen")
            reasons.append(f"{device.id} not measured: {why}")
    return [
        Finding(
            severity="not_scored",
            quantity="preflight/recording",
            observed=reason,
            expected="every device declared with a driver measured in a complete recording",
            action="record the robot again, long enough and with every declared device",
        )
        for reason in reasons
    ]


def run_preflight(
    contract: Contract,
    source: PreflightSource,
    *,
    profile: PreflightProfile = "analysis",
    required_evidence: list[EvidenceClass] | None = None,
) -> PreflightReport:
    """Run every stage, stamp findings, and count determined checks by stage."""
    profile, required = preflight_policy(profile, required_evidence)
    view: PreflightSource = _LazySource(source)  # type: ignore[assignment]
    findings: list[Finding] = []
    scored_by_stage: dict[int, int] = {}
    scores = _ScoreBook()
    watched_evidence = False
    for entry in STAGES:
        stage_findings = _dispatch_stage(entry, contract, view, scores)
        for finding in stage_findings:
            findings.append(finding.model_copy(update={"stage": entry.stage}))
        if not (
            len(stage_findings) == 1
            and stage_findings[0].quantity == f"preflight/stage/{entry.stage}/{entry.name}"
            and stage_findings[0].severity == "not_scored"
        ):
            stage_scored = _count_stage_scored(entry, contract, view)
            scored_by_stage[entry.stage] = scored_by_stage.get(entry.stage, 0) + stage_scored
            if entry.name == "commands":
                watched_evidence = commands.has_watched_evidence(stage_findings, stage_scored)
    for stage in range(5):
        scored_by_stage.setdefault(stage, 0)
    missing: list[EvidenceClass] = []
    if profile == "powered-release":
        # Coverage's permanent live-hardware boundaries apply even to LiveSource.
        # No stage supplies scored proof for these classes; missing disclosures
        # must not turn into evidence. Enumeration/liveness alone is not proof.
        for evidence in required:
            if evidence == "watched-command-stream":
                available = watched_evidence
            else:
                available = False
            if not available:
                missing.append(evidence)
    scored_records = scores.records
    findings += _cap_findings(contract, view)  # type: ignore[arg-type]
    recording = view._samples  # only what a stage already fetched; never trigger a capture here
    if recording is None or not any(d.id in recording.devices for d in contract.devices):
        # Nothing about the robot was measured (no capture,
        # an empty bag, or no declared device in it). Contract-only checks that
        # still ran proved nothing, so the run scores nothing.
        scored_by_stage = dict.fromkeys(scored_by_stage, 0)
        scored_records = []
    return PreflightReport(
        findings,
        scored_by_stage=scored_by_stage,
        scored_records=scored_records,
        profile=profile,
        required_evidence=required,
        missing_required_evidence=missing,
    )
