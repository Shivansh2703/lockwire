"""B117 fitted-band contract, gate, and independent-draw acceptance."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from lockwire_pro.baseline import (
    _critical_exceedance_count,
    budgeted_exceedances,
    build_baseline,
)
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording
from lockwire.schema import Band, FittedBandEvidence, load_contract

FIXTURES = Path(__file__).parent / "fixtures"
BAGS = FIXTURES / "bags"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
GENERATOR = BAGS / "generate.py"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("b117_bag_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import generator at {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_shared_budgeted_exceedance_math_and_required_report_operands():
    evidence = FittedBandEvidence(
        rank=1,
        n_eff=600,
        alpha=0.00017857142857142857,
    )
    assert _critical_exceedance_count(600, 600, 1, evidence.alpha / 2) == 14

    values = np.concatenate((np.zeros(580), np.full(20, 2.0)))
    decisions = budgeted_exceedances(
        values,
        Band(lo=-1.0, hi=1.0, unit="m/s^2"),
        evidence,
        detrend=False,
        decorrelation=1,
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.side == "above"
    assert decision.exceedances == 20
    assert decision.critical_count == 14
    assert decision.current_n == 600
    assert decision.baseline_n_eff == 600
    assert decision.rank == 1
    assert decision.alpha_side == evidence.alpha / 2
    assert 0.0 < float(decision.achieved_tail) <= decision.alpha_side
    assert (
        decision.summary() == "above: K=20, k*=14, n=600, m=600, b=1, "
        "allocated alpha_side=8.9285714285714286e-05, "
        "achieved tail probability=8.118910763569466e-07"
    )


def test_build_baseline_serializes_exact_fixture_bands_evidence_and_ledger():
    """Four human-authored strict limits stay outside the fitted inventory.

    ``dev.barometer/rate_hz``, ``dev.imu/rate_hz``, ``dev.imu/accel_z``, and
    ``dev.gnss/rate_hz`` are excluded because the fitter must not
    overwriting a number a human authored.
    """
    recording = load_recording(BAGS / "clean")
    artifact = build_baseline(
        recording,
        context="nominal-op",
        locked_at=dt.date(2026, 7, 29),
    )
    contract = load_contract(CLEAN_CONTRACT)

    excluded = {
        "devices/dev.barometer/envelopes/nominal-op/rate_hz",
        "devices/dev.gnss/envelopes/nominal-op/rate_hz",
        "devices/dev.imu/envelopes/nominal-op/rate_hz",
        "devices/dev.imu/envelopes/nominal-op/signals/accel_z",
    }
    expected_ledger = []
    for allocation in artifact.fitted_band_allocations:
        row = allocation.model_dump(mode="json")
        row["fitted_bands"] = [path for path in row["fitted_bands"] if path not in excluded]
        expected_ledger.append(row)
    assert [
        row.model_dump(mode="json") for row in contract.fitted_band_allocations
    ] == expected_ledger

    generated_fitted = {}
    for device_id, generated in artifact.envelopes.items():
        prefix = f"devices/{device_id}/envelopes/{generated.context}"
        for path, evidence in generated.fitted_bands.items():
            band = (
                generated.rate_hz
                if path == "rate_hz"
                else generated.signals[path.removeprefix("signals/")].band
            )
            generated_fitted[f"{prefix}/{path}"] = (band, evidence)

    checked_in_fitted = {}
    for device in contract.devices:
        for envelope in device.envelopes:
            prefix = f"devices/{device.id}/envelopes/{envelope.context}"
            for path, evidence in envelope.fitted_bands.items():
                band = (
                    envelope.rate_hz
                    if path == "rate_hz"
                    else envelope.signals[path.removeprefix("signals/")].band
                )
                checked_in_fitted[f"{prefix}/{path}"] = (band, evidence)

    assert checked_in_fitted.keys() == generated_fitted.keys() - excluded
    for path, (checked_in_band, checked_in_evidence) in checked_in_fitted.items():
        generated_band, generated_evidence = generated_fitted[path]
        # Bands are recomputed float arithmetic: cross-arch (arm64 vs x86_64) last-bit
        # drift is expected and not a real regression, so compare with a tight
        # relative tolerance rather than bit-exact equality. Unit and evidence
        # must still match exactly.
        assert checked_in_band.unit == generated_band.unit, path
        assert checked_in_band.lo == pytest.approx(generated_band.lo, rel=1e-12), path
        assert checked_in_band.hi == pytest.approx(generated_band.hi, rel=1e-12), path
        assert checked_in_evidence == generated_evidence, path

    imu = contract.device("dev.imu")
    assert imu is not None
    # A device may declare several envelopes per context: the B106/B107 shape law
    # forbids mixed provenance inside one envelope.
    accel_z_envelope = next(
        envelope
        for envelope in imu.envelopes
        if envelope.context == "nominal-op" and "accel_z" in envelope.signals
    )
    assert accel_z_envelope.rate_hz == Band(lo=180, hi=225, unit="Hz")
    assert accel_z_envelope.signals["accel_z"].band == Band(lo=-10.5, hi=-9.0, unit="m/s^2")
    assert accel_z_envelope.provenance is not None
    assert accel_z_envelope.provenance.witness == "human"

    gnss = contract.device("dev.gnss")
    assert gnss is not None
    gnss_rate_envelope = next(
        envelope
        for envelope in gnss.envelopes
        if envelope.context == "nominal-op" and envelope.rate_hz is not None
    )
    assert gnss_rate_envelope.rate_hz == Band(lo=7.5, hi=8.5, unit="Hz")
    assert gnss_rate_envelope.provenance is not None
    assert gnss_rate_envelope.provenance.witness == "human"

    assert excluded.isdisjoint(checked_in_fitted)
    barometer = contract.device("dev.barometer")
    assert barometer is not None
    assert "pressure_pa" not in barometer.envelopes[0].signals


@pytest.mark.parametrize("draw", ["clean", "clean_b", 31, 20260729])
def test_clean_and_independent_healthy_draws_go_with_presence_floor(generator, draw):
    contract = load_contract(CLEAN_CONTRACT)
    recording = (
        load_recording(BAGS / draw) if isinstance(draw, str) else generator.clean_recording(draw)
    )
    report = run_preflight(contract, RecordedSource(recording))
    violations = [finding for finding in report if finding.severity == "violation"]

    assert report.scored == 28  # contract-only budget rows no longer count
    assert violations == []
