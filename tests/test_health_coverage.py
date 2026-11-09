"""Evidence coverage sits beside the colour (E3-j; plan v8 line 902).

Per-device coverage — applicable comparisons, how many were made, samples
observed, the newest sample's age, an absent-vs-silent capture value, and a
fixed-word reason code — plus a stale fault that still reads as a fault, and
an interface-poll rate that is never mistaken for sample freshness.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.edit import contract_revision
from lockwire_pro.health import (
    CHAIN_ZERO,
    append_health_rows,
    build_document,
    chain_row_hash,
    contract_sha,
    sidecar_path_for,
)
from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import Finding
from lockwire.schema import Contract, DeviceInstance, RobotInfo

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"

runner = CliRunner()
NOW = dt.datetime(2026, 9, 7, 12, 0, 0, tzinfo=dt.UTC)


def _contract() -> Contract:
    return Contract(
        robot=RobotInfo(name="testbot"),
        devices=[
            DeviceInstance(id="dev.faulty", model="m", owner="o"),
            DeviceInstance(id="dev.silent", model="m", owner="o"),
        ],
    )


def _write_contract(tmp_path: Path) -> Path:
    path = tmp_path / "lockwire.yaml"
    path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    return path


def _write_ledger_row(contract_path: Path, *, run_iso: str, state: str, reason: str) -> None:
    """A hand-built ledger row (bypassing append_health_rows) so the coverage
    test can control the row's age directly, the same technique the mutation
    bite needs for the CLI accept line."""
    from lockwire_pro.health import HealthRow

    sha = contract_sha(contract_path)
    row = HealthRow(
        run=run_iso,
        run_id="r1",
        source="test",
        context=None,
        device="dev.imu",
        state=state,
        reason=reason,
        n_compared=1,
        contract_sha=sha,
        contract_revision=contract_revision(contract_path),
        tool="lockwire test",
    )
    payload = row.model_dump(mode="json")
    payload["prev"] = CHAIN_ZERO
    h = chain_row_hash(payload, CHAIN_ZERO)
    payload["hash"] = h
    sidecar_path_for(contract_path).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_stale_fault_still_reads_faulty(tmp_path):
    """Decisive item: a 40-day-old faulty row must still say faulty and NO-GO,
    not have "stale" swallow the fault (plan v8: "a missing update never
    makes a fault look cleared")."""
    contract_path = _write_contract(tmp_path)
    old_run = (NOW - dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_ledger_row(contract_path, run_iso=old_run, state="faulty", reason="over limit")

    result = runner.invoke(app, ["health", str(contract_path)])
    assert "over a limit" in result.output
    assert result.exit_code == 1

    document = build_document(contract_path, _contract(), now=NOW).as_dict()
    imu = next(d for d in document["devices"] if d["id"] == "dev.imu")
    assert imu["state"] == "faulty"
    assert "over a limit" in imu["word"]
    assert "40 day" in imu["word"]


def test_absent_capture_is_not_a_proven_silence(tmp_path):
    contract_path = _write_contract(tmp_path)
    findings = [
        Finding(
            severity="not_scored",
            quantity="devices/dev.silent/rate_hz",
            observed="no samples for this device in the preflight source",
            expected="receive stream present so rate / cadence can be scored",
            action="n/a",
        )
    ]
    report = PreflightReport(findings, scored_by_stage={2: 0})
    append_health_rows(contract_path, _contract(), report, source="test", context=None, now=NOW)
    document = build_document(contract_path, _contract(), now=NOW)
    by_id = {d["id"]: d for d in document.as_dict()["devices"]}
    assert by_id["dev.silent"]["coverage"]["capture"] == "silent"
    # dev.faulty has no findings at all this run — no recorded samples, ever.
    assert by_id["dev.faulty"]["coverage"]["capture"] == "absent"
    assert by_id["dev.silent"]["coverage"]["capture"] != by_id["dev.faulty"]["coverage"]["capture"]


def test_poll_rate_is_not_freshness(tmp_path):
    contract_path = _write_contract(tmp_path)
    old_run = (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_ledger_row(contract_path, run_iso=old_run, state="healthy", reason="ok")
    document = build_document(contract_path, _contract(), now=NOW).as_dict()
    imu = next(d for d in document["devices"] if d["id"] == "dev.imu")
    assert document["interface_poll_s"] != imu["coverage"]["newest_sample_age_s"]
    assert imu["coverage"]["newest_sample_age_s"] == 86400.0


def test_missing_count_reads_unknown_not_zero(tmp_path):
    """A row written before this box landed carries no samples_observed — it
    must read "unknown", never a false "0" (a zero is a claim)."""
    contract_path = _write_contract(tmp_path)
    _write_ledger_row(
        contract_path, run_iso=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), state="healthy", reason="ok"
    )
    document = build_document(contract_path, _contract(), now=NOW).as_dict()
    imu = next(d for d in document["devices"] if d["id"] == "dev.imu")
    assert imu["coverage"]["samples_observed"] == "unknown"
    assert imu["coverage"]["samples_observed"] != 0


def test_coverage_fields_are_complete_for_every_device():
    """The library door's document — every device row carries the full
    coverage set so a page consumer computes nothing (E2's Run face)."""
    from lockwire_pro import api_pro as api

    document = api.health_document(str(CLEAN_CONTRACT))
    required = {
        "applicable",
        "compared",
        "samples_observed",
        "newest_sample_age_s",
        "capture",
        "reason_code",
    }
    assert document["devices"]
    for device in document["devices"]:
        assert required <= device["coverage"].keys()
    assert isinstance(document["interface_poll_s"], float)


def test_declared_device_with_no_samples_is_absent_not_faulty(tmp_path):
    """the ledger row says 'absent' and health reports NOT SCORED, never a violation."""
    contract_path = _write_contract(tmp_path)
    findings = [
        Finding(
            severity="not_scored",
            quantity="devices/dev.silent/rate_hz",
            observed="no samples for this device in the preflight source",
            expected="receive stream present so rate / cadence can be scored",
            action="n/a",
        )
    ]
    report = PreflightReport(findings, scored_by_stage={2: 0})
    append_health_rows(contract_path, _contract(), report, source="test", context=None, now=NOW)
    document = build_document(contract_path, _contract(), now=NOW)
    row = {d["id"]: d for d in document.as_dict()["devices"]}["dev.silent"]
    assert row["state"] == "absent"
    assert document.verdict != "NO-GO" and document.exit_code != 1
    assert not any(f.severity == "violation" for f in document.findings)
