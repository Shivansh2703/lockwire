"""B100 — fixture alignment: the gate must catch planted failures for real.

Presence floor: every zero-violation assertion is paired with a
positive assertion that at least one device was scored present and live.
Vacuous "zero findings because nothing matched" is the defect this box kills.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording
from lockwire.schema import load_contract

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
BAGS = FIXTURES / "bags"

# Contract instance id aligned to the bag (model id remains mdl.imu.bmi088).
MATCHED_IMU = "dev.imu"

runner = CliRunner()


def _pair(bag_id: str):
    contract = load_contract(CLEAN_CONTRACT)
    recording = load_recording(BAGS / bag_id)
    return contract, RecordedSource(recording), recording


def test_clean_bag_scores_at_least_one_device_present_and_live():
    """THE regression lock: matching ids must produce a real presence score.

    Mutation-bite: rename the contract imu away from the bag again → these
    not_scored enumeration/liveness findings reappear and the floor fails.
    """
    contract, source, recording = _pair("clean")
    # POSITIVE lock, and it must come first. The two `not any(... not_scored)`
    # assertions below are satisfied VACUOUSLY if the contract stops declaring
    # MATCHED_IMU at all — there is then no `devices/dev.imu/...` finding left to
    # find. A reviewer's rename-away mutation proved exactly that: only the `< 14`
    # denominator bit. Assert the declaration itself, so id drift fails loudly and
    # by name rather than leaning on a count.
    declared = {device.id for device in contract.devices}
    assert MATCHED_IMU in declared, (
        f"contract no longer declares {MATCHED_IMU} — the id drift this box exists to "
        f"kill has returned. Declared ids: {sorted(declared)}"
    )
    assert MATCHED_IMU in recording.devices
    assert MATCHED_IMU in {obs.device_id for obs in source.enumerate()}
    assert MATCHED_IMU in {obs.device_id for obs in source.liveness()}

    findings = run_preflight(contract, source)
    violations = [f for f in findings if f.severity == "violation"]
    not_scored = [f for f in findings if f.severity == "not_scored"]
    not_scored_quantities = {f.quantity for f in not_scored}

    assert violations == [], f"clean bag must GO; got violations: {violations}"
    # Presence floor: IMU must NOT be disclosed as unseen.
    assert not any(
        f.quantity == f"devices/{MATCHED_IMU}/enumeration" and f.severity == "not_scored"
        for f in findings
    ), "IMU enumeration not_scored — ids have drifted apart again"
    assert not any(
        f.quantity == f"devices/{MATCHED_IMU}/liveness" and f.severity == "not_scored"
        for f in findings
    ), "IMU liveness not_scored — ids have drifted apart again"

    assert findings.scored == 28  # contract-only budget rows no longer count
    assert findings.scored > len(not_scored)
    assert not_scored_quantities == {
        "budgets/bud.mass.devices",  # unknown masses -> budget not scored
        "devices/dev.barometer/signals/pressure_pa/variance",
        "devices/dev.imu/envelopes/nominal-op/conditions",
        "devices/dev.imu/signals/accel_x/variance",
        "devices/dev.imu/signals/accel_y/variance",
        "devices/dev.imu/signals/gyro_x/variance",
        "devices/dev.imu/signals/gyro_y/variance",
        "devices/dev.imu/signals/gyro_z/variance",
        "devices/dev.radio.rfd900/enumeration",
        # The `commands` stage's two standing
        # disclosures — no command record here, and the bag's own commands
        # unscorable without a contract field marking a channel as commanded.
        "preflight/commands/in_recording",
        "preflight/commands/record",
        "preflight/coverage/comm_links_under_representative_load",
        "preflight/coverage/live_hardware_software_reconciliation",
        "preflight/cross_signal/camera_histogram",
        "preflight/cross_signal/redundant_sensors",
        "preflight/cross_signal/stationary_|g|",
        "preflight/stage/2/timestamps/monotonicity/live_path",
        "preflight/stage/2/timestamps/wall_clock_currency",
        # dev.barometer, dev.gnss and dev.esc.busless
        # carry no `physical` block at all — now not_scored for mass exactly
        # like a `physical` block with a missing mass, instead of a silent skip.
        "devices/dev.barometer/physical/mass",
        "devices/dev.gnss/physical/mass",
        "devices/dev.esc.busless/physical/mass",
    }
    # The pre-B100 bound of 14 is deliberately retired: B117 added honest, named
    # disclosures for five IMU variance channels with no authored one-shot floor;
    # the larger count is not a coverage regression. Physical-less devices added
    # three more (physical-less devices' mass, formerly a silent skip): 15 -> 18.
    # E0/eb-06 (2026-09-08): the `commands` stage's two standing disclosures: 18 -> 20.
    # bud.mass.devices (unknown masses) is one more: 20 -> 21.
    assert len(not_scored) == 21, (
        f"expected 21 named coverage gaps after the mass-disclosure fix, got {len(not_scored)}: "
        + ", ".join(f.quantity for f in not_scored)
    )


def test_cli_clean_goes_with_presence_floor():
    """CLI accept: EXIT=0 and the matched device is not disclosed as unseen."""
    result = runner.invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(BAGS / "clean"), "--no-ledger"],
    )
    assert result.exit_code == 0, result.output
    assert "GO" in result.output
    assert "NO-GO" not in result.output
    flat = " ".join(result.output.split())
    # Absence of the vacuous unseen disclosure for the matched imu.
    assert f"devices/{MATCHED_IMU}/enumeration" not in flat
    assert f"devices/{MATCHED_IMU}/liveness" not in flat


def test_declared_bag_provenance_matches_the_real_recording():
    """Provenance must be CHECKABLE, not decorative.

    This box's whole claim is that the healthy band was fitted from a named
    recording rather than copied from the fault injector. A review found
    that corrupting the declared ``bag_sha256`` turned nothing red — the citation
    was accurate but unlocked, so it could rot or be edited silently and the suite
    would keep passing. An unfalsifiable provenance claim is the same shape of
    defect as the vacuous zero-findings gate this box exists to kill.

    Presence floor: the trailing denominator assertion is mandatory —
    without it this test passes by checking nothing the day someone drops the
    citation.
    """
    contract = load_contract(CLEAN_CONTRACT)
    repo_root = Path(__file__).parent.parent
    checked: list[str] = []

    for device in contract.devices:
        for envelope in device.envelopes or []:
            provenance = envelope.provenance
            if provenance is None or provenance.bag_sha256 is None:
                continue
            bag = repo_root / provenance.source
            if not (bag / "samples.jsonl").exists():
                # Non-bag citations (datasheets, bench sessions with no checked-in
                # artifact) are out of scope here; they are not re-derivable.
                continue
            actual = load_recording(bag).sha256
            assert actual == provenance.bag_sha256, (
                f"{device.id} / {envelope.context}: declared bag_sha256 "
                f"{provenance.bag_sha256} does not match {provenance.source}, whose "
                f"real recording hash is {actual}. Either the band was fitted from a "
                f"different artifact than it cites, or the bag changed under it."
            )
            checked.append(f"{device.id}/{envelope.context}")

    assert checked, (
        "no bag-backed provenance was checked — this test just passed vacuously. "
        "The clean contract must carry at least one envelope citing a checked-in bag "
        "with its bag_sha256, or the fitted-band claim is unverifiable."
    )


def test_rate_collapse_is_stage2_violation_naming_gnss():
    """Planted GNSS rate collapse must NO-GO at stage 2 through the real runner."""
    contract, source, _ = _pair("rate_collapse")
    findings = run_preflight(contract, source)

    # Coverage restoration: the authored GNSS rate band stays strict, so the
    # original /rate_hz assertion from main applies again; this is not a regression.
    rate_hits = [
        f
        for f in findings
        if f.severity == "violation" and f.stage == 2 and f.quantity == "devices/dev.gnss/rate_hz"
    ]
    assert rate_hits, (
        "rate_collapse produced no stage-2 rate_hz violation on dev.gnss — "
        f"violations: {[f.quantity for f in findings if f.severity == 'violation']}"
    )

    result = runner.invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(BAGS / "rate_collapse"), "--no-ledger"],
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "NO-GO" in result.output
    assert "dev.gnss" in flat


def test_under_sampled_barometer_range_is_removed_but_hard_limit_stays_scored():
    """The old observed-extrema pressure band cannot masquerade as a fitted band."""
    contract, source, _ = _pair("range_excursion")
    findings = run_preflight(contract, source)

    barometer = next(device for device in contract.devices if device.id == "dev.barometer")
    assert "pressure_pa" not in barometer.envelopes[0].signals
    model = contract.device_model(barometer.model)
    assert model is not None and "pressure_pa" in model.hard_limits
    assert findings.scored == 28  # contract-only budget rows no longer count
    assert not any(f.quantity.endswith("/pressure_pa/range") for f in findings)


def test_range_excursion_is_deliberately_retired_until_b120_restores_the_band():
    """Pin: range_excursion currently produces no violation by design.

    The fitter measured device/dev.barometer/pressure_pa at n=1500 raw,
    effective n=21, status under-sampled, and band None; the two-sided 95/95
    floor in stats design §1 is 93 effective samples. The removed
    [101295.33, 101358.16] Pa band was only clean_b's 63 Pa-wide raw observed
    extrema. With no pressure signal envelope, the model's [30000, 110000] Pa
    hard limit correctly cannot catch the planted roughly 1.4 kPa excursion
    around the nominal 101325 Pa.

    This pin records a deliberate retirement, not an accident. B120 restores
    the catch; when it does, this test goes red and must be deleted, not updated.
    """
    contract, source, recording = _pair("range_excursion")
    findings = run_preflight(contract, source)

    barometer = next(device for device in contract.devices if device.id == "dev.barometer")
    assert all("pressure_pa" not in envelope.signals for envelope in barometer.envelopes)

    model = contract.device_model(barometer.model)
    assert model is not None
    hard_limit = model.hard_limits["pressure_pa"]
    assert (hard_limit.lo, hard_limit.hi, hard_limit.unit) == (30000, 110000, "Pa")

    pressure = recording.devices["dev.barometer"].signals["pressure_pa"]
    peak = max(pressure)
    assert hard_limit.lo < 101325 < peak < hard_limit.hi
    assert 1300 < peak - 101500 < 1500
    assert findings.scored == 28  # contract-only budget rows no longer count
    assert [finding for finding in findings if finding.severity == "violation"] == []


def test_frozen_but_publishing_is_stage3_variance_violation():
    """B98 inverse of the retired deliberate miss: planted freeze NO-GOes on variance."""
    contract, source, _ = _pair("frozen_but_publishing")
    findings = run_preflight(contract, source)

    var_hits = [
        f
        for f in findings
        if f.severity == "violation"
        and f.stage == 3
        and f.quantity == f"devices/{MATCHED_IMU}/signals/accel_z/variance"
    ]
    assert var_hits, (
        "frozen_but_publishing must NO-GO at stage 3 on accel_z variance — "
        f"violations: {[f.quantity for f in findings if f.severity == 'violation']}"
    )
    assert "oneshot_variance_min" in var_hits[0].expected

    result = runner.invoke(
        app,
        [
            "preflight",
            str(CLEAN_CONTRACT),
            "--from",
            str(BAGS / "frozen_but_publishing"),
            "--no-ledger",
        ],
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "NO-GO" in result.output
    assert MATCHED_IMU in flat
    assert "accel_z" in flat
    assert "variance" in flat


def test_rate_collapse_refusal_disappears_when_fault_neutralized():
    """Mutation-bite: fixture must be testing the planted fault, not always refusing."""
    contract, _, recording = _pair("rate_collapse")
    clean = load_recording(BAGS / "clean")
    # Neutralize in memory: restore healthy GNSS cadence from the clean bag.
    recording.devices["dev.gnss"].recv_times = list(clean.devices["dev.gnss"].recv_times)
    recording.devices["dev.gnss"].signals = {
        name: list(values) for name, values in clean.devices["dev.gnss"].signals.items()
    }
    findings = run_preflight(contract, RecordedSource(recording))
    assert findings.scored == 28  # contract-only budget rows no longer count
    assert not any(
        f.severity == "violation" and f.quantity == "devices/dev.gnss/sustained_rate_liveness"
        for f in findings
    ), "neutralized rate_collapse still refused — fixture is not testing the fault"


def test_frozen_but_publishing_refusal_disappears_when_fault_neutralized():
    """Mutation-bite: restore healthy accel_z from the clean bag → variance GO."""
    contract, _, recording = _pair("frozen_but_publishing")
    clean = load_recording(BAGS / "clean")
    recording.devices[MATCHED_IMU].signals["accel_z"] = list(
        clean.devices[MATCHED_IMU].signals["accel_z"]
    )
    findings = run_preflight(contract, RecordedSource(recording))
    assert not any(
        f.severity == "violation"
        and f.quantity == f"devices/{MATCHED_IMU}/signals/accel_z/variance"
        for f in findings
    ), "neutralized frozen_but_publishing still refused — fixture is not testing the fault"


def test_imu_oneshot_variance_min_is_not_generate_py_constant():
    """Guard: oneshot floor must not equal a named float literal in generate.py."""
    generate_py = (BAGS / "generate.py").read_text()
    literals = {
        float(m.group(1)) for m in re.finditer(r"(?<![\w.])(\d+\.\d+)(?![\w.])", generate_py)
    }

    contract = load_contract(CLEAN_CONTRACT)
    imu = next(d for d in contract.devices if d.id == MATCHED_IMU)
    floors = [
        signal.oneshot_variance_min
        for env in imu.envelopes
        for signal in env.signals.values()
        if signal.oneshot_variance_min is not None
    ]
    assert floors, "expected oneshot_variance_min on at least one IMU envelope"
    for floor in floors:
        assert floor not in literals, (
            f"oneshot_variance_min={floor} collides with a generate.py float literal — "
            "self-grade risk (B100/B93 cycle-1 class)"
        )


def test_barometer_hard_limit_is_not_generate_py_healthy_upper():
    """Guard: contract hard limit must not equal generate.py's injector boundary."""
    generate_py = (BAGS / "generate.py").read_text()
    match = re.search(r"healthy_upper_pa\s*=\s*([0-9.]+)", generate_py)
    assert match is not None, "generate.py must declare healthy_upper_pa for the guard"
    healthy_upper_pa = float(match.group(1))

    contract = load_contract(CLEAN_CONTRACT)
    baro_model = next(m for m in contract.device_models if m.id == "mdl.baro.generic")
    pressure_limit = baro_model.hard_limits["pressure_pa"]

    assert pressure_limit.hi != healthy_upper_pa
    assert pressure_limit.lo != healthy_upper_pa


def test_no_contract_band_endpoint_equals_generate_py_healthy_upper():
    """No contract band may copy range_excursion's injector constant.

    B117 nearly lost this B93/B100 self-grade guard when removing the pressure
    band made its old band-specific lookup impossible: an absence check replaced
    the guard. Bind every remaining and future band so the injector can never
    grade its own homework.
    """
    generate_py = (BAGS / "generate.py").read_text()
    match = re.search(r"healthy_upper_pa\s*=\s*([0-9.]+)", generate_py)
    assert match is not None, "generate.py must declare healthy_upper_pa for the guard"
    healthy_upper_pa = float(match.group(1))

    contract = load_contract(CLEAN_CONTRACT)
    bands = {
        f"device_models/{model.id}/hard_limits/{name}": band
        for model in contract.device_models
        for name, band in model.hard_limits.items()
    }
    for device in contract.devices:
        for envelope_index, envelope in enumerate(device.envelopes):
            # Contexts may repeat across provenance-specific envelopes; keep every band keyed.
            prefix = f"devices/{device.id}/envelopes/{envelope_index}/{envelope.context}"
            if envelope.rate_hz is not None:
                bands[f"{prefix}/rate_hz"] = envelope.rate_hz
            bands.update(
                {
                    f"{prefix}/signals/{name}": signal.band
                    for name, signal in envelope.signals.items()
                }
            )
            bands.update(
                {f"{prefix}/conditions/{name}": band for name, band in envelope.conditions.items()}
            )

    assert bands, "contract has no bands, so the self-grade guard checked nothing"
    for path, band in bands.items():
        assert healthy_upper_pa not in (band.lo, band.hi), (
            f"{path} endpoint copies generate.py healthy_upper_pa={healthy_upper_pa} — "
            "the fault injector is grading its own homework"
        )


def test_under_sampled_barometer_range_band_is_not_serialized():
    """Presence lock for the deliberate removal of the old observed extrema."""
    contract = load_contract(CLEAN_CONTRACT)
    baro = next(d for d in contract.devices if d.id == "dev.barometer")
    assert "pressure_pa" not in baro.envelopes[0].signals
    model = contract.device_model(baro.model)
    assert model is not None
    assert "pressure_pa" in model.hard_limits


def test_empty_recording_is_not_scored_never_go(tmp_path):
    """No samples at all must not clear the robot on contract-only checks."""
    import shutil

    from lockwire.report import verdict

    shutil.copy(BAGS / "clean" / "manifest.yaml", tmp_path / "manifest.yaml")
    (tmp_path / "samples.jsonl").write_text("")
    contract = load_contract(CLEAN_CONTRACT)
    findings = run_preflight(contract, RecordedSource(load_recording(tmp_path)))
    assert verdict(findings).code == "NOT_SCORED"
    assert any(f.observed == "recording has no data" for f in findings)
