"""B93 — planted-failure demo: per-stage isolation + clean demo GO.

Assert on Finding objects (severity / quantity / stage), never console prose.
Stages 0 and 1 cannot emit violations through RecordedSource (Rule 4) —
those tests prove the reachable outcomes: matched present+live, and absence
disclosed as not_scored.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.preflight.stages.coverage import M1B_CAPABILITIES, PERMANENT_BOUNDARY_MARKER
from lockwire_pro.recording import load_recording
from lockwire.schema import load_contract

from conftest import plain

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_CONTRACT = FIXTURES / "contract_demo.yaml"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
EMPTY_CONTRACT = FIXTURES / "contract_empty.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"
BAGS = FIXTURES / "bags"

PRESENT_DEVICES = ("dev.imu", "dev.barometer", "dev.gnss")
ABSENT_DEVICE = "dev.radio.absent"

runner = CliRunner()


def _demo_findings(bag_id: str = "clean"):
    contract = load_contract(DEMO_CONTRACT)
    source = RecordedSource(load_recording(BAGS / bag_id))
    return run_preflight(contract, source)


def _violations(findings):
    return [f for f in findings if f.severity == "violation"]


def _warnings(findings):
    return [f for f in findings if f.severity == "warning"]


def test_clean_demo_goes_with_matched_devices_and_honest_gaps():
    """Positive path lit: present devices scored; absent + live-hardware gaps disclosed."""
    findings = _demo_findings("clean")

    assert _violations(findings) == []
    assert _warnings(findings) == []

    # Stage 0/1 positive path: matched devices produce no enumeration/liveness findings.
    for device_id in PRESENT_DEVICES:
        assert not any(f.quantity == f"devices/{device_id}/enumeration" for f in findings), (
            f"{device_id} should be present (matched), not disclosed absent"
        )
        assert not any(f.quantity == f"devices/{device_id}/liveness" for f in findings), (
            f"{device_id} should be live (matched), not disclosed absent"
        )

    # a declared device absent from the recording is disclosed as not seen (NOT SCORED).
    absent_enum = [f for f in findings if f.quantity == f"devices/{ABSENT_DEVICE}/enumeration"]
    absent_live = [f for f in findings if f.quantity == f"devices/{ABSENT_DEVICE}/liveness"]
    assert len(absent_enum) == 1 and absent_enum[0].severity == "not_scored"
    assert "declared and not seen" in absent_enum[0].action
    assert len(absent_live) == 1 and absent_live[0].severity == "not_scored"
    assert "declared and not seen" in absent_live[0].action

    # Condition envelopes actually score under nominal-op (no context-mismatch disclosure).
    assert not any(
        f.severity == "not_scored"
        and "conditions" in f.quantity
        and "recording context is" in f.observed
        for f in findings
    )

    # Permanent live-hardware boundary — both capabilities named.
    coverage = [f for f in findings if f.quantity.startswith("preflight/coverage/")]
    assert len(coverage) == len(M1B_CAPABILITIES)
    observed = " | ".join(f.observed for f in coverage)
    for capability in M1B_CAPABILITIES:
        assert capability in observed
        assert PERMANENT_BOUNDARY_MARKER in observed


def test_stage0_enumeration_matched_and_absent():
    """Stage 0 reachable outcomes only — violations unreachable via RecordedSource."""
    findings = _demo_findings("clean")
    stage0 = [f for f in findings if f.stage == 0]

    assert all(f.severity == "not_scored" for f in stage0)
    assert all(f.quantity.endswith("/enumeration") for f in stage0)
    assert {f.quantity for f in stage0} == {f"devices/{ABSENT_DEVICE}/enumeration"}
    assert _violations(findings) == []


def test_stage1_liveness_matched_and_absent():
    """Stage 1 reachable outcomes only — violations unreachable via RecordedSource."""
    findings = _demo_findings("clean")
    stage1 = [f for f in findings if f.stage == 1]

    assert all(f.severity == "not_scored" for f in stage1)
    assert all(f.quantity.endswith("/liveness") for f in stage1)
    assert {f.quantity for f in stage1} == {f"devices/{ABSENT_DEVICE}/liveness"}
    assert _violations(findings) == []


def test_stage2_flow_isolated_violation():
    findings = _demo_findings("preflight_flow")
    violations = _violations(findings)

    assert violations, "planted rate collapse must trip stage 2"
    assert all(f.stage == 2 for f in violations)
    assert any(f.quantity == "devices/dev.gnss/rate_hz" for f in violations) or any(
        f.quantity == "devices/dev.gnss/recv_gap" for f in violations
    )
    # No other stage contributes a violation.
    assert not any(f.severity == "violation" and f.stage != 2 for f in findings)


def test_stage3_sanity_isolated_violation():
    findings = _demo_findings("preflight_sanity")
    violations = _violations(findings)

    assert violations, "planted pressure excursion must trip stage 3"
    assert all(f.stage == 3 for f in violations)
    assert any(f.quantity == "devices/dev.barometer/signals/pressure_pa/range" for f in violations)
    assert not any(f.severity == "violation" and f.stage != 3 for f in findings)


def test_stage3_frozen_isolated_variance_violation():
    """B107: frozen-but-publishing trips stage-3 oneshot variance on the demo contract."""
    findings = _demo_findings("preflight_frozen")
    violations = _violations(findings)

    assert violations, "planted frozen IMU must trip stage 3"
    assert all(f.stage == 3 for f in violations)
    assert any(f.quantity == "devices/dev.imu/signals/accel_z/variance" for f in violations), [
        f.quantity for f in violations
    ]
    assert any("oneshot_variance_min" in f.expected for f in violations)
    assert not any(f.severity == "violation" and f.stage != 3 for f in findings)


def test_stage4_cross_record_isolated_violation():
    findings = _demo_findings("preflight_cross_record")
    violations = _violations(findings)

    assert violations, "planted temp condition exit must trip stage 4a"
    assert all(f.stage == 4 for f in violations)
    assert all("conditions" in f.quantity or f.quantity.startswith("budgets/") for f in violations)
    assert not any("stationary_|g|" in f.quantity for f in violations)
    # Stages 0–3 stay clean on this plant.
    assert not any(f.severity == "violation" and f.stage in {0, 1, 2, 3} for f in findings)


def test_stage4_cross_signal_isolated_violation():
    findings = _demo_findings("preflight_cross_signal")
    violations = _violations(findings)

    assert violations, "planted |g| excursion must trip stage 4b"
    assert all(f.stage == 4 for f in violations)
    assert all("stationary_|g|" in f.quantity for f in violations)
    assert not any("conditions" in f.quantity and f.severity == "violation" for f in findings)
    assert not any(f.severity == "violation" and f.stage in {0, 1, 2, 3} for f in findings)


def test_cli_clean_contract_still_goes():
    result = runner.invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )
    assert result.exit_code == 0, result.output
    flat = plain(result.output)
    scored = re.search(r"\((\d+) scored\)", flat)
    assert scored is not None and int(scored.group(1)) > 0  # Presence floor beside zero NO-GO.
    assert "NO-GO" not in flat
    # The new stage-4 `commands` stage always
    # discloses two not_scored facts — no command record beside this recording, and
    # commands inside the bag itself being unscorable: 18 -> 20.
    # a mass budget with an unknown mass is not scored, so one more disclosure (20 -> 21)
    assert "21 check(s) not scored" in flat


def test_cli_empty_contract_says_nothing_scored_never_records_agree():
    result = runner.invoke(
        app,
        ["preflight", str(EMPTY_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )

    assert result.exit_code == 3, result.output
    flat = " ".join(result.output.split())
    assert "NOT SCORED — nothing compared; 9 check(s) not scored" in flat
    assert "records agree" not in flat
    # The verdict word itself must never be GO. Disclosure prose elsewhere on
    # the page legitimately says "a GO here does not mean X was checked", so
    # this checks the verdict phrasing specifically, not a blanket substring ban.
    assert "GO — nothing" not in flat
    assert "GO —" not in flat


def test_cli_demo_contract_absent_device_is_not_scored_naming_it():
    """the demo contract declares dev.radio.absent with a driver and the bag
    lacks it, so the run is NOT SCORED (exit 3) naming the device, never GO."""
    result = runner.invoke(
        app,
        ["preflight", str(DEMO_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )
    assert result.exit_code == 3, result.output
    flat = " ".join(result.output.split())
    assert "NOT SCORED — dev.radio.absent not measured" in flat
    # Absence disclosed; a matched device must not be described as unobserved.
    assert "dev.radio.absent" in flat
    assert "devices/dev.imu/enumeration" not in flat


def test_demo_contract_thresholds_are_not_injector_constants():
    """Guard: no contract_demo threshold may equal a generate.py injector literal.

    A band measured from a recording is legitimate; a band
    equal to a constant in the code that creates the fault is not. Mutation-bite:
    point a demo threshold back at an injector constant → this assertion fails.

    Also covers ``oneshot_variance_min`` (B107 / B93–B100 lineage): the frozen
    demo floor must be fitted from the clean bag, never a generate.py float.
    """
    generate_py = (BAGS / "generate.py").read_text()
    injector_constants = {
        float(match.group(1))
        for match in re.finditer(
            r"(?:demo_upper_pa|demo_temp_hi|healthy_upper_pa|"
            r"clean_pressure_max|clean_temp_max)\s*=\s*([0-9.]+)",
            generate_py,
        )
    }
    # Also harvest simple numeric reference_value= literals near demo plants.
    injector_constants.update(
        float(match.group(1))
        for match in re.finditer(r"reference_value\s*=\s*([0-9.]+)", generate_py)
    )
    # All float literals — the B100-class hunt that caught 101500.0 / self-grade.
    injector_constants.update(
        float(match.group(1))
        for match in re.finditer(r"(?<![\w.])(\d+\.\d+)(?![\w.])", generate_py)
    )
    assert injector_constants, "generate.py must expose injector literals for the guard"

    contract = load_contract(DEMO_CONTRACT)
    thresholds: set[float] = set()
    oneshot_floors: set[float] = set()
    for device in contract.devices:
        for envelope in device.envelopes:
            if envelope.rate_hz is not None:
                thresholds.update({envelope.rate_hz.lo, envelope.rate_hz.hi})
            for band in (envelope.conditions or {}).values():
                thresholds.update({band.lo, band.hi})
            for signal in (envelope.signals or {}).values():
                if signal.band is not None:
                    thresholds.update({signal.band.lo, signal.band.hi})
                if signal.oneshot_variance_min is not None:
                    oneshot_floors.add(signal.oneshot_variance_min)
                    thresholds.add(signal.oneshot_variance_min)

    assert oneshot_floors, (
        "contract_demo.yaml must declare oneshot_variance_min on at least one "
        "IMU envelope so the preflight_frozen demo plant is scored"
    )

    overlap = thresholds & injector_constants
    assert not overlap, (
        "contract_demo.yaml threshold(s) equal generate.py injector constant(s): "
        f"{sorted(overlap)}. Fit bands from the clean recording; define plant "
        "magnitude against the clean signal's own statistics — never share a literal."
    )
