"""Stage 3 — signal sanity (B88).

Real assertions use synthetic Recording/contract pairs with matching device ids.
The checked-in clean fixture must still GO once bag/contract ids align (B100).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.preflight.source import RecordedSource
from lockwire.checks import _ScoreBook
from lockwire_pro.preflight.stages import sanity
from lockwire_pro.recording import DeviceStream, Recording, load_recording
from lockwire.schema import (
    Band,
    ContextEnvelope,
    Contract,
    DeviceFamily,
    DeviceInstance,
    DeviceModel,
    FittedBandEvidence,
    Provenance,
    RobotInfo,
    SignalEnvelope,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"

_CONTEXT = "bench-idle"
_DEVICE = "dev.imu"
_CHANNEL = "accel_z"
_UNIT = "m/s^2"
_PROVENANCE = Provenance(
    witness="live",
    source="synthetic-b88",
    locked_at="2026-07-27",
)


def _contract(
    *,
    band: Band | None = None,
    hard_limit: Band | None = None,
    variance_floor: float | None = 1.0e-4,
    oneshot_variance_min: float | None = None,
    context: str = _CONTEXT,
) -> Contract:
    """Minimal contract whose device id matches the synthetic recording."""
    if band is None:
        band = Band(lo=-12.0, hi=-8.0, unit=_UNIT)
    signals: dict[str, SignalEnvelope] = {
        _CHANNEL: SignalEnvelope(
            band=band,
            variance_floor=variance_floor,
            oneshot_variance_min=oneshot_variance_min,
        ),
    }
    hard_limits = {_CHANNEL: hard_limit} if hard_limit is not None else {}
    return Contract(
        robot=RobotInfo(name="b88-synthetic"),
        families=[DeviceFamily(id="fam.imu", name="IMU")],
        device_models=[
            DeviceModel(id="mdl.imu", family="fam.imu", hard_limits=hard_limits),
        ],
        devices=[
            DeviceInstance(
                id=_DEVICE,
                model="mdl.imu",
                owner="b88",
                envelopes=[
                    ContextEnvelope(
                        context=context,
                        signals=signals,
                        provenance=_PROVENANCE,
                    )
                ],
            )
        ],
    )


def _recording(
    tmp_path: Path,
    values: list[float],
    *,
    context: str = _CONTEXT,
    unit: str = _UNIT,
) -> Recording:
    n = len(values)
    return Recording(
        context=context,
        units={_DEVICE: {_CHANNEL: unit}},
        devices={
            _DEVICE: DeviceStream(
                device=_DEVICE,
                recv_times=[i * 0.01 for i in range(n)],
                signals={_CHANNEL: list(values)},
            )
        },
        env={},
        sha256="synthetic-b88",
        path=tmp_path,
    )


def _run(contract: Contract, recording: Recording) -> list:
    return sanity.check(contract, RecordedSource(recording))


def test_implemented_flag_replaced_stub():
    assert sanity.IMPLEMENTED is True


def test_clean_fixture_smoke_still_goes():
    """CLI accept residue: clean bag + clean contract must not NO-GO via stage 3."""
    result = CliRunner().invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )
    assert result.exit_code == 0, result.output


def test_clean_fixture_stage3_emits_no_violations():
    contract = load_contract(CLEAN_CONTRACT)
    recording = load_recording(CLEAN_BAG)
    findings = _run(contract, recording)
    assert all(f.severity != "violation" for f in findings)


def test_fitted_signal_range_reports_budget_operands_and_keeps_hard_limit_strict(
    tmp_path,
):
    recording = _recording(tmp_path, [-2.0, 2.0] * 100)
    device = _contract().devices[0]
    evidence = FittedBandEvidence(rank=1, n_eff=100, alpha=0.005)

    fitted = sanity._range_findings(
        device=device,
        quantity=f"devices/{_DEVICE}/signals/{_CHANNEL}/range",
        raw=np.asarray([-2.0, 2.0] * 100),
        recording=recording,
        channel=_CHANNEL,
        limit=Band(lo=-1.0, hi=1.0, unit=_UNIT),
        role="envelope band",
        fitted=evidence,
    )
    strict = sanity._range_findings(
        device=device,
        quantity=f"devices/{_DEVICE}/signals/{_CHANNEL}/hard_limit",
        raw=np.asarray([0.0, 20.0]),
        recording=_recording(tmp_path, [0.0, 20.0]),
        channel=_CHANNEL,
        limit=Band(lo=-10.0, hi=10.0, unit=_UNIT),
        role="hard limit",
    )

    assert fitted
    assert all(f.severity == "violation" for f in fitted)
    assert all("K=" in f.observed and "tail probability=" in f.observed for f in fitted)
    assert len(strict) == 1
    assert strict[0].observed_quantity is not None
    assert strict[0].observed_quantity.value == 20.0


def test_nan_before_range_catches_nonfinite_inside_band(tmp_path):
    """NaN must be refused before range: nan compared to a band reads in-range."""
    # Values sit inside the band numerically except one NaN — range alone would GO.
    values = [-9.8, -9.7, float("nan"), -9.6, -9.5]
    findings = _run(_contract(), _recording(tmp_path, values))

    nan_hits = [f for f in findings if f.quantity.endswith("/nan")]
    assert nan_hits, "NaN sample produced no nan finding — guard missing or after range"
    assert all(f.severity == "violation" for f in nan_hits)
    # Range must not be the only story: a NaN-only miss would look like GO.
    assert not any(f.severity == "violation" and f.quantity.endswith("/range") for f in findings)


def test_nan_ordering_mutation_bite(tmp_path):
    """If NaN were checked only after range, this fixture would silently pass range.

    Pins the trap: every sample fails ``isfinite``, and a band comparison would
    treat each NaN as in-range. Stage 3 must still NO-GO on /nan.
    """
    values = [float("nan"), float("nan"), float("nan")]
    findings = _run(_contract(), _recording(tmp_path, values))
    assert any(f.severity == "violation" and f.quantity.endswith("/nan") for f in findings)
    assert not any(f.quantity.endswith("/range") for f in findings)


def test_range_excursion_is_violation_with_typed_magnitudes(tmp_path):
    band = Band(lo=-10.5, hi=-9.0, unit=_UNIT)
    values = [-9.8, -9.7, -20.0, -9.6]  # -20 is outside
    findings = _run(_contract(band=band), _recording(tmp_path, values))

    range_hits = [
        f for f in findings if f.severity == "violation" and f.quantity.endswith("/range")
    ]
    assert len(range_hits) == 1
    hit = range_hits[0]
    assert hit.observed_quantity is not None
    assert hit.expected_quantity is not None
    assert hit.observed_quantity.value == -20.0
    assert hit.observed_quantity.unit == _UNIT
    assert hit.expected_quantity.unit == _UNIT
    assert hit.expected_quantity.value in (band.lo, band.hi)


def test_hard_limit_excursion_is_violation(tmp_path):
    hard = Band(lo=-235.4, hi=235.4, unit=_UNIT)
    band = Band(lo=-500.0, hi=500.0, unit=_UNIT)  # envelope admits the spike
    values = [-9.8, -9.7, -300.0, -9.6]
    findings = _run(
        _contract(band=band, hard_limit=hard),
        _recording(tmp_path, values),
    )
    hard_hits = [
        f for f in findings if f.severity == "violation" and f.quantity.endswith("/hard_limit")
    ]
    assert hard_hits
    assert hard_hits[0].observed_quantity is not None
    assert hard_hits[0].observed_quantity.value == -300.0


def test_variance_floor_only_is_not_scored_cusum_reference_not_oneshot(tmp_path):
    """variance_floor alone must not score — it is the CUSUM reference (stats_design)."""
    floor = 1.0e-4
    # Exactly frozen across a full one-shot window — a declared oneshot threshold
    # would violation; variance_floor must not.
    values = [-9.81] * 25
    findings = _run(
        _contract(variance_floor=floor, oneshot_variance_min=None),
        _recording(tmp_path, values),
    )
    var_hits = [f for f in findings if f.quantity.endswith("/variance")]
    assert var_hits
    assert all(f.severity == "not_scored" for f in var_hits)
    assert not any(f.severity == "violation" and f.quantity.endswith("/variance") for f in findings)
    hit = var_hits[0]
    assert "oneshot_variance_min" in hit.observed or "oneshot_variance_min" in hit.expected
    assert "CUSUM" in hit.observed or "cusum" in hit.observed.lower()
    assert "stats_design" in hit.observed
    # B83 firewall: no fabricated dimensionless unit on variance magnitudes.
    assert hit.observed_quantity is None
    assert hit.expected_quantity is None


def test_oneshot_variance_min_undeclared_still_not_scored_never_invented(tmp_path):
    values = [-9.81 + 0.01 * ((-1) ** i) for i in range(20)]
    findings = _run(
        _contract(variance_floor=None, oneshot_variance_min=None),
        _recording(tmp_path, values),
    )
    var_hits = [f for f in findings if f.quantity.endswith("/variance")]
    assert var_hits
    assert all(f.severity == "not_scored" for f in var_hits)
    assert not any(f.severity == "violation" and f.quantity.endswith("/variance") for f in findings)


def test_oneshot_variance_min_frozen_is_violation_without_typed_squared_unit(tmp_path):
    """Declared oneshot_variance_min catches a frozen channel; no fabricated unit."""
    # Need >= ONESHOT_VARIANCE_WINDOW identical samples for a fully quiet window.
    values = [-9.81] * 25
    findings = _run(
        _contract(variance_floor=None, oneshot_variance_min=1.0e-4),
        _recording(tmp_path, values),
    )
    var_hits = [
        f for f in findings if f.severity == "violation" and f.quantity.endswith("/variance")
    ]
    assert len(var_hits) == 1
    hit = var_hits[0]
    assert "oneshot_variance_min" in hit.expected
    assert "window" in hit.observed
    # B83 firewall: leave typed magnitudes unset until units.py has a squared dimension.
    assert hit.observed_quantity is None
    assert hit.expected_quantity is None


def test_oneshot_variance_min_healthy_emits_no_variance_violation(tmp_path):
    rng = np.random.default_rng(98)
    noise = rng.normal(0.0, 0.05, size=50)
    values = (-9.8 + noise).tolist()
    findings = _run(
        _contract(variance_floor=None, oneshot_variance_min=1.0e-4),
        _recording(tmp_path, values),
    )
    assert not any(f.severity == "violation" and f.quantity.endswith("/variance") for f in findings)


def test_context_mismatch_emits_not_scored_not_silent_empty(tmp_path):
    """Declared envelope for bench-idle + recording context flight → not silence.

    Returning [] here reads as checked-and-fine (false GO). Must name the mismatch.
    """
    values = [-9.8, -9.7, -9.6, -9.5]
    findings = _run(
        _contract(context="bench-idle"),
        _recording(tmp_path, values, context="flight"),
    )
    assert findings, "context mismatch produced [] — silent false GO"
    mismatch = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == f"devices/{_DEVICE}/signals"
    ]
    assert mismatch, "no context-mismatch not_scored finding"
    text = " ".join(f"{f.observed} {f.expected} {f.action}" for f in mismatch).lower()
    assert "bench-idle" in text
    assert "flight" in text
    assert not any(f.severity == "violation" for f in findings)


def test_context_mismatch_with_hard_limits_discloses_asymmetry(tmp_path):
    """Hard limits still score without context match; asymmetry must be named."""
    hard = Band(lo=-235.4, hi=235.4, unit=_UNIT)
    values = [-9.8, -9.7, -300.0, -9.6]
    findings = _run(
        _contract(context="bench-idle", hard_limit=hard),
        _recording(tmp_path, values, context="flight"),
    )
    mismatch = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == f"devices/{_DEVICE}/signals"
    ]
    assert mismatch
    assert any("hard limit" in f.action.lower() for f in mismatch)
    hard_hits = [
        f for f in findings if f.severity == "violation" and f.quantity.endswith("/hard_limit")
    ]
    assert hard_hits, "hard limits must still apply without context match"


def test_healthy_synthetic_emits_no_violations(tmp_path):
    rng = np.random.default_rng(88)
    noise = rng.normal(0.0, 0.05, size=50)
    values = (-9.8 + noise).tolist()
    findings = _run(
        _contract(oneshot_variance_min=1.0e-4),
        _recording(tmp_path, values),
    )
    assert all(f.severity != "violation" for f in findings)


def test_missing_samples_for_declared_channel_is_not_scored(tmp_path):
    contract = _contract()
    recording = Recording(
        context=_CONTEXT,
        units={_DEVICE: {_CHANNEL: _UNIT}},
        devices={_DEVICE: DeviceStream(device=_DEVICE, recv_times=[], signals={})},
        env={},
        sha256="synthetic-b88-empty",
        path=tmp_path,
    )
    findings = _run(contract, recording)
    assert findings
    assert all(f.severity == "not_scored" for f in findings)
    assert any("not present" in f.observed for f in findings)


def test_stage_never_silently_skips_unit_mismatch(tmp_path):
    """Error path must name the gap — returning [] here would be a false GO."""
    band = Band(lo=-10.5, hi=-9.0, unit=_UNIT)
    # Recording declares a different dimension so conversion fails.
    findings = _run(
        _contract(band=band),
        _recording(tmp_path, [-9.8, -9.7, -9.6], unit="Pa"),
    )
    assert findings, "unit mismatch produced [] — silent pass"
    assert all(f.severity == "not_scored" for f in findings)
    assert any("unit" in f.observed.lower() or "unit" in f.expected.lower() for f in findings)


def test_preflight_quotes_why_on_crossed_hard_limit(tmp_path):
    """A refusal that crosses a bound quotes `why` back verbatim. The
    sanity stage builds its own limit text, so it is a separate refusal path
    from `checks.py`'s and needs its own guard."""
    hard = Band(lo=-235.4, hi=235.4, unit=_UNIT, why="the die desolders past this")
    band = Band(lo=-500.0, hi=500.0, unit=_UNIT)
    findings = _run(
        _contract(band=band, hard_limit=hard),
        _recording(tmp_path, [-9.8, -9.7, -300.0, -9.6]),
    )
    hard_hits = [
        f for f in findings if f.severity == "violation" and f.quantity.endswith("/hard_limit")
    ]
    assert hard_hits
    assert "(why: the die desolders past this)" in hard_hits[0].expected


def test_preflight_quotes_why_on_fitted_band_violation(tmp_path):
    """The fitted-band violation is the same crossed-bound refusal as the
    hard-limit one and hand-builds its own limit text the same way, so it must
    quote `why` back too."""
    findings = sanity._range_findings(
        device=_contract().devices[0],
        quantity=f"devices/{_DEVICE}/signals/{_CHANNEL}/range",
        raw=np.asarray([-2.0, 2.0] * 100),
        recording=_recording(tmp_path, [-2.0, 2.0] * 100),
        channel=_CHANNEL,
        limit=Band(lo=-1.0, hi=1.0, unit=_UNIT, why="the frame cracks past this"),
        role="envelope band",
        fitted=FittedBandEvidence(rank=1, n_eff=100, alpha=0.005),
    )
    assert findings
    assert all(f.severity == "violation" for f in findings)
    assert all("(why: the frame cracks past this)" in f.expected for f in findings)


def test_l35_empty_sample_array_is_not_scored_not_a_crash(tmp_path):
    """an empty sample array must read not_scored, not raise from np.argmin."""
    contract = _contract(hard_limit=Band(lo=-12.0, hi=-8.0, unit=_UNIT))
    findings = sanity.check(contract, RecordedSource(_recording(tmp_path, [])), scores=_ScoreBook())
    assert findings
    assert all(f.severity == "not_scored" for f in findings)
