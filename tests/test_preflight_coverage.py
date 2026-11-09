"""Coverage disclosure: permanent live-hardware gaps, never silent, never score-affecting.

Mutation-bite targets:
- silence one disclosure → capability emission assert red
- drop a capability from code but leave it in the doc (or vice versa) → doc sync red
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.preflight.stages import coverage
from lockwire_pro.preflight.stages.coverage import (
    M1B_CAPABILITIES,
    PERMANENT_BOUNDARY_MARKER,
    check,
)
from lockwire_pro.preflight.stages.flow import _MONOTONICITY_QUANTITY, _WALL_CLOCK_QUANTITY
from lockwire_pro.recording import load_recording
from lockwire.report import render
from lockwire.schema import load_contract

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"
GATE_COVERAGE_DOC = Path(__file__).resolve().parents[1] / "docs" / "gate_coverage.md"

runner = CliRunner()


def _clean_pair():
    return load_contract(CLEAN_CONTRACT), RecordedSource(load_recording(CLEAN_BAG))


def test_implemented_flag_replaces_unbuilt_stub():
    assert coverage.IMPLEMENTED is True


def test_always_emits_both_m1b_disclosures_as_not_scored():
    """Stage must never return [] — permanent boundary always has something to say."""
    contract, source = _clean_pair()
    findings = check(contract, source)

    assert findings, "coverage stage must never emit nothing"
    assert len(findings) == len(M1B_CAPABILITIES)
    assert all(f.severity == "not_scored" for f in findings)
    assert all(f.severity != "violation" and f.severity != "warning" for f in findings)

    observed = " | ".join(f.observed for f in findings)
    for capability in M1B_CAPABILITIES:
        assert capability in observed, f"missing live-hardware capability by name: {capability}"
        assert any(capability in f.observed for f in findings)


def test_coverage_action_names_the_powered_robot_not_the_milestone():
    """G2 audit 2 §5: the action text told the operator "do not treat an M1a
    GO as asserting {capability}" — an internal milestone name, not an
    operator sentence. Say what the offline gate cannot do in plain words.
    Mutation bite: reverting to the "M1a GO" or "deferred to M1b" wording turns this red.
    """
    contract, source = _clean_pair()
    findings = check(contract, source)

    for finding in findings:
        for code in ("M1a", "M1b"):  # internal milestone names never reach the operator
            assert code not in finding.action
            assert code not in finding.expected
            assert code not in finding.observed
        assert "powered" in finding.action.lower() or "power" in finding.action.lower()


def test_permanent_boundary_wording_differs_from_unbuilt_stub():
    """Operators must tell 'never here' from 'not written yet'."""
    contract, source = _clean_pair()
    findings = check(contract, source)

    for finding in findings:
        assert PERMANENT_BOUNDARY_MARKER in finding.observed


def test_disclosures_do_not_flip_go_or_exit_code():
    contract, source = _clean_pair()
    findings = run_preflight(contract, source)
    coverage_findings = [f for f in findings if f.stage == 4 and "coverage" in f.quantity]

    assert coverage_findings
    assert all(f.severity == "not_scored" for f in coverage_findings)
    assert all(f.severity != "violation" for f in findings)

    exit_code = render(findings)
    assert exit_code == 0


def test_cli_clean_fixture_goes_and_names_both_m1b_capabilities():
    """CLI smoke only — exit/GO/names. Marker permanence lives on Finding fields."""
    result = runner.invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )
    assert result.exit_code == 0, result.output
    assert "GO" in result.output
    # Rich wraps mid-phrase; collapse whitespace so the assertion tests presence, not layout.
    flat = " ".join(result.output.split())
    for capability in M1B_CAPABILITIES:
        assert capability in flat, f"CLI output missing {capability!r}"


def test_gate_coverage_doc_lists_exactly_the_m1b_capabilities():
    """Stale-quotation trap: doc list and code list must agree exactly."""
    text = GATE_COVERAGE_DOC.read_text(encoding="utf-8")
    start = text.index("<!-- m1b-capabilities -->")
    end = text.index("<!-- /m1b-capabilities -->")
    block = text[start:end]
    listed = [line[2:].strip() for line in block.splitlines() if line.startswith("- ")]
    assert listed == list(M1B_CAPABILITIES)


def test_gate_coverage_doc_names_b87_and_b90_not_scored_channels():
    """Doc enumerates every not_scored channel this phase introduced."""
    text = GATE_COVERAGE_DOC.read_text(encoding="utf-8")

    assert _MONOTONICITY_QUANTITY in text
    assert _WALL_CLOCK_QUANTITY in text
    # B90 quantity paths — rename in the stage without updating the doc → red.
    assert "preflight/cross_signal/stationary_|g|" in text
    assert "preflight/cross_signal/redundant_sensors" in text
    assert "preflight/cross_signal/camera_histogram" in text


def test_gate_coverage_doc_names_planted_failure_modes_the_gate_catches():
    """B104: planted refusals must be named checkably, not as reassurance.

    Mutation-bite for the capability-list sync remains test_gate_coverage_doc_lists_exactly…
    (change a capability string → red). This test pins the planted-mode table so
    deleting a bag name or quantity path from the doc goes red the same way.
    """
    text = GATE_COVERAGE_DOC.read_text(encoding="utf-8")

    assert "rate_collapse" in text
    assert "devices/dev.gnss/rate_hz" in text
    assert "range_excursion" in text
    assert "devices/dev.barometer/signals/pressure_pa/range" in text
    assert "frozen_but_publishing" in text
    assert "devices/dev.imu/signals/accel_z/variance" in text
    # Contract known_failure_modes coverage is stated, not left silent.
    # Collapse whitespace so a mid-phrase wrap in the markdown cannot vacate the pin.
    flat = " ".join(text.split())
    assert "frozen-but-publishing after brownout" in flat
    assert "Every named `known_failure_modes` entry on this contract is caught" in flat


def test_m1b_capabilities_match_contract_verbatim():
    """Contract §1 / §2 names — paraphrase here is a coverage lie."""
    assert M1B_CAPABILITIES == (
        "live hardware↔software reconciliation",
        "comm links under representative load",
    )
