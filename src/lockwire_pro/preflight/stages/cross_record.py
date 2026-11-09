"""Stage 4 — cross-checks that reuse record-level checks (B89).

Wiring, not checking: ``check_budgets`` and ``check_condition_envelopes``
(checks.py) already implement budget sums and the condition-envelope test as
record-level checks (contract §2 stage 4). This module only calls them and
stamps their findings into stage 4 — zero new check logic, zero new
thresholds.

Context gating is CORRECT DESIGN, not a coverage gap (matches B90 stationary
``|g|`` and B88 stage-3 envelope matching): a ``ContextEnvelope``'s condition
bands claim health only under that envelope's ``context``. Scoring a
``bench-idle`` envelope against ``nominal-op`` data asserts something the
author never claimed. When the recording's context matches no declared
condition envelope for a device, emit a ``warning`` naming the unbaselined
operating context, the contexts that do have condition baselines, and the
remedy (re-baseline under the operating context) — never a violation, never
``not_scored`` (the check ran and answered "no coverage here"; that is a
completed negative determination, not a coverage gap). A missing axis
(declared, no data) is a different refusal; that is still Rule 3 and stays
``not_scored``.

``check_condition_envelopes`` wants one scalar ``current_conditions`` value
per axis, but ``Recording.env`` is a time series per channel. Per the
stage-4 pre-dispatch design rule (Rule 1 — folded here now that
the brief that set it is gone): reduce each axis to its
MIN and MAX across the recording, and call the record-level check twice —
once with the vector of per-axis minima, once with the vector of per-axis
maxima — deduping the combined findings on (quantity, observed). Mean/median/
last-value would hide a brief excursion, which is exactly the fault this
stage exists to catch.

Two further traps, also ruled pre-dispatch:

- Rule 2: the same axis name can be declared with different units by
  different devices under the *matching* context. One flat
  ``current_conditions`` dict cannot honor both, so a genuine unit conflict
  is refused (``not_scored`` naming the axis and the conflicting units)
  rather than silently resolved.
- Rule 3: an axis with no matching environment channel, no samples, or an
  unconvertible unit is refused the same way — never defaulted to a
  plausible number.

For both refusal cases, this stage also drops the axis from the copy of the
contract handed to ``check_condition_envelopes`` (see ``_contract_scoped_to``).
Without that, the record-level check would independently score the same gap
as a second, conflicting verdict — a "current conditions unavailable"
**violation** on the very axis this stage just disclosed as ``not_scored``.
Scoping is data plumbing (which axes we ask the check to score), not new
check logic: no new threshold, no new pass/fail rule. It remains necessary
for axis-level refusals on a context-matched path; context mismatch never
reaches the check at all.
"""

from __future__ import annotations

import numpy as np

from lockwire import units
from lockwire_pro.baseline import budgeted_exceedances
from lockwire.checks import _ScoreBook, check_budgets
from lockwire_pro.paid_checks import check_condition_envelopes
from lockwire_pro.preflight.source import PreflightSource
from lockwire_pro.recording import Recording
from lockwire.report import Finding
from lockwire.schema import Contract, DeviceInstance, Quantity

# B89: stage implemented for real (stub replaced).
IMPLEMENTED = True

_QUANTITY_PREFIX = "preflight/stage/4/cross_record/conditions"


def check(
    contract: Contract, source: PreflightSource, *, scores: _ScoreBook | None = None
) -> list[Finding]:
    """Fold the existing record-level budget and condition-envelope checks in.

    ``check_budgets`` already records a ``ScoredRecord`` on its passing branch
    when handed a ``_ScoreBook`` (checks.py) — this only wires the same book
    the runner threads through the other stages into that existing call.
    """
    recording = source.samples()
    findings: list[Finding] = []
    findings.extend(check_budgets(contract, scores=scores))
    findings.extend(_condition_envelope_findings(contract, recording))
    return findings


def _context_mismatch_finding(
    device: DeviceInstance, recording_context: str, declared_contexts: list[str]
) -> Finding:
    """Warn that no condition baselines cover the recording's operating context.

    ``declared_contexts`` is only envelopes that carry ``conditions`` (see
    ``_condition_envelope_findings``) — a device may still declare a rate/signal
    envelope for the operating context and warn here, because that envelope
    declares no condition axes. Wording must say "no condition baselines", not
    "no envelope for this context."
    """
    declared = ", ".join(declared_contexts)
    return Finding(
        severity="warning",
        quantity=f"devices/{device.id}/conditions",
        observed=(
            f"operating context {recording_context!r} has no condition baselines; "
            f"condition baselines cover {declared}"
        ),
        expected=(f"condition baselines covering operating context {recording_context!r}"),
        owner=device.owner,
        action=(
            f"re-baseline under operating context {recording_context!r} "
            f"(condition envelopes currently cover {declared} only)"
        ),
    )


def _declared_axis_units(contract: Contract, context: str) -> dict[str, set[str]]:
    """axis name -> every distinct unit declared for it under ``context``."""
    axis_units: dict[str, set[str]] = {}
    for device in contract.devices:
        for envelope in device.envelopes:
            if envelope.context != context or not envelope.conditions:
                continue
            for axis, band in envelope.conditions.items():
                axis_units.setdefault(axis, set()).add(band.unit)
    return axis_units


def _resolve_axis(axis: str, target_unit: str, recording: Recording) -> tuple[float, float] | str:
    """Reduce one axis's recorded time series to (min, max) in ``target_unit``.

    Returns a reason string instead of a value pair when the axis cannot be
    resolved (Rule 3) — no environment channel, no samples, no declared
    recording unit, or an unconvertible unit.
    """
    values = _resolve_axis_values(axis, target_unit, recording)
    if isinstance(values, str):
        return values
    return float(np.min(values)), float(np.max(values))


def _resolve_axis_values(axis: str, target_unit: str, recording: Recording) -> np.ndarray | str:
    """Resolve one condition series into the declared unit without reducing it."""
    samples = recording.env.get(axis)
    if samples is None:
        return f"no environment channel named {axis!r} in this recording"
    if not samples:
        return f"environment channel {axis!r} has no samples in this recording"
    source_unit = recording.units.get("environment", {}).get(axis)
    if source_unit is None:
        return f"recording declares no unit for environment channel {axis!r}"
    raw = [value for _, value in samples]
    try:
        values = np.asarray(
            [units.convert(value, source_unit, target_unit) for value in raw],
            dtype=float,
        )
    except units.UnitError:
        return (
            f"recording unit {source_unit!r} for {axis!r} is not convertible "
            f"to declared unit {target_unit!r}"
        )
    return values


def _conflict_finding(axis: str, distinct_units: set[str]) -> Finding:
    unit_list = ", ".join(sorted(distinct_units))
    return Finding(
        severity="not_scored",
        quantity=f"{_QUANTITY_PREFIX}/{axis}",
        observed=(
            f"axis {axis!r} is declared with conflicting units ({unit_list}) "
            "across the contract; one flat current-conditions value cannot "
            "satisfy both"
        ),
        expected="a single declared unit per condition axis across the contract",
        action=(
            "reconcile the conflicting unit declarations for this axis "
            "(reject one baseline or re-lock it), or accept it stays unscored"
        ),
    )


def _unscored_axis_finding(axis: str, reason: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"{_QUANTITY_PREFIX}/{axis}",
        observed=f"axis {axis!r}: {reason}",
        expected=f"environment samples for {axis!r}, convertible to its declared unit",
        action=(
            "record the missing environment channel, or accept the axis "
            "stays unscored rather than defaulting it"
        ),
    )


def _contract_scoped_to(contract: Contract, *, context: str, resolved_axes: set[str]) -> Contract:
    """Deep-copy ``contract`` to only matching-context envelopes and resolved axes.

    Two jobs, both data plumbing:

    1. Keep only envelopes whose ``context`` matches the recording — so
       ``check_condition_envelopes`` never scores a claim the author never made
       about this recording.
    2. Drop condition axes this stage could not resolve — so the record-level
       check does not independently re-score an axis already disclosed as
       ``not_scored`` (a disclosure AND a "no data" violation for the same gap).

    No new threshold, no new pass/fail rule.
    """
    scoped = contract.model_copy(deep=True)
    for device in scoped.devices:
        device.envelopes = [e for e in device.envelopes if e.context == context]
        for envelope in device.envelopes:
            if envelope.conditions:
                fitted_axes = {
                    path.removeprefix("conditions/")
                    for path in envelope.fitted_bands
                    if path.startswith("conditions/")
                }
                envelope.fitted_bands = {
                    path: evidence
                    for path, evidence in envelope.fitted_bands.items()
                    if not path.startswith("conditions/")
                }
                envelope.conditions = {
                    axis: band
                    for axis, band in envelope.conditions.items()
                    if axis in resolved_axes and axis not in fitted_axes
                }
    return scoped


def _fitted_condition_findings(
    contract: Contract,
    recording: Recording,
    *,
    resolved_axes: set[str],
) -> list[Finding]:
    findings = []
    for device in contract.devices:
        for envelope in device.envelopes:
            if envelope.context != recording.context:
                continue
            for axis, band in envelope.conditions.items():
                evidence = envelope.fitted_bands.get(f"conditions/{axis}")
                if evidence is None or axis not in resolved_axes:
                    continue
                values = _resolve_axis_values(axis, band.unit, recording)
                if isinstance(values, str):
                    continue
                for decision in budgeted_exceedances(
                    values,
                    band,
                    evidence,
                    detrend=False,
                ):
                    findings.append(
                        Finding(
                            severity="violation",
                            quantity=f"devices/{device.id}/conditions/{axis}",
                            observed=decision.summary(),
                            expected=(
                                f"fitted-band exceedance count below k* within "
                                f"condition band {band}"
                            ),
                            observed_quantity=Quantity(
                                value=decision.observed_value,
                                unit=band.unit,
                            ),
                            expected_quantity=Quantity(
                                value=decision.bound,
                                unit=band.unit,
                            ),
                            owner=device.owner,
                            action=(
                                "restore the condition distribution to the fitted band; "
                                "excursion pattern is diagnostic only"
                            ),
                        )
                    )
    return findings


def _condition_envelope_findings(contract: Contract, recording: Recording) -> list[Finding]:
    findings: list[Finding] = []
    matching_any = False

    for device in contract.devices:
        declared_contexts = sorted(
            {envelope.context for envelope in device.envelopes if envelope.conditions}
        )
        if not declared_contexts:
            continue
        if recording.context not in declared_contexts:
            findings.append(_context_mismatch_finding(device, recording.context, declared_contexts))
            continue
        matching_any = True

    if not matching_any:
        return findings

    axis_units = _declared_axis_units(contract, recording.context)
    if not axis_units:
        return findings

    disclosures: list[Finding] = []
    resolved_axes: set[str] = set()
    current_min: dict[str, float] = {}
    current_max: dict[str, float] = {}

    for axis in sorted(axis_units):
        distinct_units = axis_units[axis]
        if len(distinct_units) > 1:
            disclosures.append(_conflict_finding(axis, distinct_units))
            continue

        target_unit = next(iter(distinct_units))
        resolution = _resolve_axis(axis, target_unit, recording)
        if isinstance(resolution, str):
            disclosures.append(_unscored_axis_finding(axis, resolution))
            continue

        lo, hi = resolution
        resolved_axes.add(axis)
        current_min[axis] = lo
        current_max[axis] = hi

    filtered_contract = _contract_scoped_to(
        contract, context=recording.context, resolved_axes=resolved_axes
    )

    combined: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for current_conditions in (current_min, current_max):
        for finding in check_condition_envelopes(filtered_contract, current_conditions):
            key = (finding.quantity, finding.observed)
            if key in seen:
                continue
            seen.add(key)
            combined.append(finding)

    fitted = _fitted_condition_findings(
        contract,
        recording,
        resolved_axes=resolved_axes,
    )
    return findings + disclosures + fitted + combined
