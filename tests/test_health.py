"""Health ledger: row states, reader stale rule, CLI wiring."""

from __future__ import annotations
import datetime as dt
import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.health import (
    CHAIN_ZERO,
    HealthRow,
    append_health_rows,
    chain_break,
    chain_row_hash,
    device_state,
    history,
    latest_by_device,
    read_ledger,
    read_ledger_with_skipped,
    row_age,
    sidecar_path_for,
    summary,
)
from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import Finding, ScoredRecord
from lockwire.report_json import FORMAT_VERSION
from lockwire.schema import (
    SCHEMA_VERSION,
    Band,
    Contract,
    DeviceInstance,
    DeviceModel,
    Driver,
    Quantity,
    Rail,
    RobotInfo,
    Topology,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"

runner = CliRunner()
NOW = dt.datetime(2026, 9, 7, 12, 0, 0, tzinfo=dt.UTC)


def _contract() -> Contract:
    return Contract(
        robot=RobotInfo(name="testbot"),
        # Model "m" declares a hard limit so these devices' range-leg
        # findings stay verdict-bearing — this fixture is about severity
        # ordering, not the watched/no-limit-declared case (that's
        # test_health_watched.py).
        device_models=[
            DeviceModel(
                id="m", family="fam.x", hard_limits={"accel_x": Band(lo=-9.0, hi=9.0, unit="m/s^2")}
            )
        ],
        devices=[
            DeviceInstance(id="dev.faulty", model="m", owner="o"),
            DeviceInstance(id="dev.degrading", model="m", owner="o"),
            DeviceInstance(id="dev.healthy", model="m", owner="o"),
            DeviceInstance(id="dev.quiet", model="m", owner="o"),
        ],
    )


def _write_contract(tmp_path: Path) -> Path:
    path = tmp_path / "lockwire.yaml"
    path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    return path


def _four_state_report() -> PreflightReport:
    """One device per state: faulty (violation + warning both present, severity ordering),
    degrading (warning only), healthy (a passed comparison via scored_records),
    quiet (nothing at all)."""
    findings = [
        Finding(
            severity="violation",
            quantity="devices/dev.faulty/signals/accel_x/hard_limit",
            observed="12.00 m/s^2",
            expected="within hard limit [-9.00, 9.00] m/s^2",
            observed_quantity=Quantity(value=12.0, unit="m/s^2"),
            expected_quantity=Quantity(value=9.0, unit="m/s^2"),
            owner="o",
            action="fix the producer",
        ),
        # Same device also carries a warning — ordering rule: faulty must win.
        Finding(
            severity="warning",
            quantity="devices/dev.faulty/signals/gyro_x/range",
            observed="8.50 rad/s",
            expected="within envelope band [-9.00, 9.00] rad/s",
            observed_quantity=Quantity(value=8.5, unit="rad/s"),
            expected_quantity=Quantity(value=9.0, unit="rad/s"),
            owner="o",
            action="watch it",
        ),
        Finding(
            severity="warning",
            quantity="devices/dev.degrading/signals/gyro_y/range",
            observed="8.60 rad/s",
            expected="within envelope band [-9.00, 9.00] rad/s",
            observed_quantity=Quantity(value=8.6, unit="rad/s"),
            expected_quantity=Quantity(value=9.0, unit="rad/s"),
            owner="o",
            action="watch it",
        ),
    ]
    report = PreflightReport(findings, scored_by_stage={3: 3})
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    return report


def test_faulty_wins_over_degrading_ordering_rule(tmp_path):
    contract_path = _write_contract(tmp_path)
    report = _four_state_report()
    written = append_health_rows(
        contract_path,
        _contract(),
        report,
        source="test",
        context="nominal-op",
        now=NOW,
    )
    assert written == 4

    rows = read_ledger(sidecar_path_for(contract_path))
    latest = latest_by_device(rows)
    assert latest["dev.faulty"].state == "faulty"
    assert latest["dev.degrading"].state == "degrading"
    assert latest["dev.healthy"].state == "healthy"
    assert latest["dev.healthy"].reason == "inside its bands, 1 signals compared"
    assert latest["dev.quiet"].state == "not_watched"
    assert latest["dev.quiet"].n_compared == 0


def test_stale_is_computed_by_the_reader_not_stored(tmp_path):
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    rows = read_ledger(sidecar_path_for(contract_path))
    healthy_row = latest_by_device(rows)["dev.healthy"]
    assert healthy_row.state == "healthy"  # stored state is untouched

    fresh = device_state(healthy_row, NOW + dt.timedelta(days=1))
    assert fresh == "healthy"
    stale = device_state(healthy_row, NOW + dt.timedelta(days=8))
    assert stale == "stale"


def test_append_is_append_two_runs_keep_order(tmp_path):
    contract_path = _write_contract(tmp_path)
    contract = _contract()
    append_health_rows(
        contract_path, contract, _four_state_report(), source="run1", context=None, now=NOW
    )
    append_health_rows(
        contract_path,
        contract,
        _four_state_report(),
        source="run2",
        context=None,
        now=NOW + dt.timedelta(days=1),
    )
    rows = read_ledger(sidecar_path_for(contract_path))
    assert len(rows) == 8
    faulty_history = history(rows, "dev.faulty")
    assert [row.source for row in faulty_history] == ["run1", "run2"]
    assert latest_by_device(rows)["dev.faulty"].source == "run2"


def _row(run: str, state: str, *, device: str = "dev.x", source: str = "s") -> HealthRow:
    return HealthRow(
        run=run,
        run_id="r",
        source=source,
        context=None,
        device=device,
        state=state,
        reason="",
        n_compared=1,
        contract_sha="sha",
        tool="t",
    )


def test_latest_by_device_picks_the_newest_timestamp_not_the_last_appended():
    # E0-e3-lb: an older row appended AFTER a newer one (retry, clock
    # adjustment, merged ledger) must not read as the latest.
    rows = [_row("2026-09-09T10:00:00Z", "healthy"), _row("2026-09-09T09:00:00Z", "faulty")]
    assert latest_by_device(rows)["dev.x"].state == "healthy"


def test_latest_by_device_an_undateable_row_never_beats_a_dateable_one():
    # A row whose timestamp will not parse is kept, not dropped, but it can
    # never masquerade as the latest reading over a row that IS dated —
    # even when it was appended last.
    rows = [_row("2026-09-09T10:00:00Z", "healthy"), _row("not-a-timestamp", "faulty")]
    assert latest_by_device(rows)["dev.x"].state == "healthy"


def test_latest_by_device_two_undateable_rows_fall_back_to_append_order():
    # With no timestamp to compare, file (append) order is the only
    # information left, so the last one written still wins between them.
    rows = [_row("garbage-one", "faulty"), _row("garbage-two", "healthy")]
    assert latest_by_device(rows)["dev.x"].state == "healthy"


def test_corrupt_line_is_skipped_never_a_crash(tmp_path):
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    path = sidecar_path_for(contract_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write("{}\n")  # valid JSON, invalid HealthRow

    rows = read_ledger(path)  # must not raise
    assert len(rows) == 4  # the two corrupt lines are skipped, not counted as rows


def _append_trailing_garbage_timestamp_row(path: Path, good_rows: list, *, trailing_newline: bool):
    """A row that is valid JSON and validates as a ``HealthRow`` (unlike
    ``test_corrupt_line_is_skipped_never_a_crash``'s cases) but whose ``run``
    field is not a real timestamp — the shape a truncated/mid-write ledger
    leaves as its last line (E3-A4)."""
    garbage = json.loads(good_rows[-1].model_dump_json())
    garbage["run"] = "not-a-timestamp"
    text = json.dumps(garbage)
    if trailing_newline:
        text += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_trailing_garbage_timestamp_row_no_newline_survives_device_state(tmp_path):
    """E3-A4: a garbage-timestamp row as the file's LAST line, with no
    trailing newline, must not crash `device_state`/`row_age` through
    `latest_by_device` — it must be read and skipped like any other corrupt
    line, and the last genuine row must still be usable as the device's
    current state."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    path = sidecar_path_for(contract_path)
    good_rows = read_ledger(path)
    device = good_rows[-1].device

    _append_trailing_garbage_timestamp_row(path, good_rows, trailing_newline=False)

    rows = read_ledger(path)  # must not raise
    assert len(rows) == len(good_rows)  # garbage row skipped, not counted as a row
    latest = latest_by_device(rows)[device]
    assert latest.run != "not-a-timestamp"  # the last GENUINE row, never the garbage one
    row_age(latest, NOW)  # must not raise
    assert device_state(latest, NOW) == good_rows[-1].state


def test_trailing_garbage_timestamp_row_with_newline_survives_device_state(tmp_path):
    """Same as above but with a trailing newline after the garbage row — a
    different parse (an extra empty final split) that must be equally safe."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    path = sidecar_path_for(contract_path)
    good_rows = read_ledger(path)
    device = good_rows[-1].device

    _append_trailing_garbage_timestamp_row(path, good_rows, trailing_newline=True)

    rows = read_ledger(path)  # must not raise
    assert len(rows) == len(good_rows)
    latest = latest_by_device(rows)[device]
    assert latest.run != "not-a-timestamp"
    row_age(latest, NOW)  # must not raise
    assert device_state(latest, NOW) == good_rows[-1].state


def test_read_ledger_with_skipped_counts_what_read_ledger_silently_drops(tmp_path):
    """E3-A4-r2: `read_ledger`'s skip count used to reach only a stderr print.
    `read_ledger_with_skipped` is the one place that count is now computable
    by a caller — same rows as `read_ledger`, plus the number."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    path = sidecar_path_for(contract_path)
    good_rows = read_ledger(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write("{}\n")  # valid JSON, invalid HealthRow

    rows, skipped = read_ledger_with_skipped(path)
    assert rows == good_rows
    assert skipped == 2


def test_summary_carries_the_skipped_count_it_is_given(tmp_path):
    """`summary` doesn't re-derive the skip count (it can't — the rows handed
    to it never included the dropped lines) — it just threads through what
    its caller already measured via `read_ledger_with_skipped`."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path, _contract(), _four_state_report(), source="test", context=None, now=NOW
    )
    path = sidecar_path_for(contract_path)
    rows, _ = read_ledger_with_skipped(path)
    assert summary(rows, NOW)["skipped"] == 0
    assert summary(rows, NOW, skipped=3)["skipped"] == 3


def test_preflight_writes_sidecar_and_prints_the_line(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    result = runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)])
    assert result.exit_code == 0
    assert "health:" in result.output and "parts recorded" in result.output
    sidecar = sidecar_path_for(contract_path)
    assert sidecar.exists()
    rows = read_ledger(sidecar)
    assert len(rows) > 0


def test_health_command_prints_table_and_exits_3_with_no_sidecar(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3
    assert "NOT SCORED" in result.output


def test_health_command_exits_1_when_a_row_is_faulty(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    from lockwire.schema import load_contract

    loaded = load_contract(contract_path)
    faulty_finding = Finding(
        severity="violation",
        quantity=f"devices/{loaded.devices[0].id}/signals/accel_x/hard_limit",
        observed="99.00 m/s^2",
        expected="within hard limit [-9.00, 9.00] m/s^2",
        observed_quantity=Quantity(value=99.0, unit="m/s^2"),
        expected_quantity=Quantity(value=9.0, unit="m/s^2"),
        owner=loaded.devices[0].owner,
        action="fix it",
    )
    append_health_rows(
        contract_path,
        loaded,
        PreflightReport([faulty_finding]),
        source="test",
        context="nominal-op",
        now=NOW,
    )

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 1
    assert "NO-GO" in result.output


def test_health_after_all_not_watched_preflight_is_not_scored_never_go(tmp_path):
    """Reviewer bounce, box H-a: rows all not_watched must never read GO.

    Box H-a2 (M1 follow-up): the clean fixture now genuinely scores
    comparisons (sanity/flow/cross_signal record passes too — see
    ``test_clean_fixture_records_passes_and_reads_healthy``), so it no longer
    proves this guarantee. ``contract_context_mismatch.yaml`` turned out not
    to either: its one finding is a context-mismatch *warning*, and
    ``_device_row`` counts a warning as a comparison (``n_compared`` includes
    it) even though it never compared a number — so that device reads
    ``degrading``, not ``not_watched`` (pre-existing behaviour, unrelated to
    this box). A synthetic empty ``PreflightReport`` — no findings, no
    scored_records — is what actually reproduces every row reading
    ``not_watched``.

    Day-one rule, amended after review: `nothing_watched`
    (scored == 0, no fault, no foreign) is still true here — but a row was
    just recorded for every device (`written == 4`), so this is NOT the
    day-one absence-of-history case; it is "ran once and compared nothing"
    against a contract that already declares a hard limit. The bounce's
    decisive item was that those two states must not render identically: the
    CLI now shows the per-device table (each device's own "nothing compared
    yet" reason, since a hard limit already IS declared — no false "declare
    a hard limit" advice), not the day-one sentence.
    """
    contract_path = _write_contract(tmp_path)
    written = append_health_rows(
        contract_path,
        _contract(),
        PreflightReport([]),
        source="test",
        context=None,
        now=NOW,
    )
    assert written == 4

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3
    assert "NOT SCORED" in result.output
    assert "GO" not in result.output.replace("NOT SCORED", "")
    # history exists — the table, not the day-one sentence (rich wraps "runs
    # recorded" onto two lines at this width, so check the row content that
    # survives wrapping instead of the header text).
    assert "dev.faulty" in result.output
    assert "nothing" in result.output and "compared yet" in result.output  # per-device reason
    assert "declare a hard limit" not in result.output  # already declared — not false advice


def test_clean_fixture_records_passes_and_reads_healthy(tmp_path):
    """M1 follow-up (H-a2): a preflight pass is recorded per device, so the
    clean fixture's ledger has real ``healthy`` rows, not all ``not_watched``.

    Mutation bite: comment out any one of sanity/flow/cross_signal's
    ``scores.passed(...)`` call sites (or drop the ``_SCORED_STAGES`` entry in
    ``preflight/runner.py``) and this test's ``n_compared``/``signals``
    assertions go red.
    """
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    result = runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)])
    assert result.exit_code == 0, result.output

    rows = read_ledger(sidecar_path_for(contract_path))
    latest = latest_by_device(rows)
    imu = latest["dev.imu"]
    assert imu.state == "healthy"
    assert imu.n_compared >= 1
    assert imu.reason == f"inside its bands, {imu.n_compared} signals compared"
    # The accel band numbers (the "signals" field): dev.imu's declared
    # accel_z hard limit is a plain (non-fitted) band, so its pass carries a
    # real observed/band pair, not just a count.
    accel = imu.signals["accel_z"]
    assert accel.unit == "m/s^2"
    assert accel.observed_lo == accel.observed_hi  # a scored pass is a single value, not a range
    assert accel.band_lo <= accel.observed_lo <= accel.band_hi
    assert accel.outside is False

    healthy_count = sum(1 for row in latest.values() if row.state == "healthy")
    assert healthy_count >= 1

    health_result = runner.invoke(app, ["health", str(contract_path)])
    assert health_result.exit_code == 0, health_result.output
    assert "GO" in health_result.output
    # `health`'s verdict line names how many parts had >=1 comparison (the
    # scored count); every watched device here is healthy, so it equals
    # healthy_count.
    assert f"records agree ({healthy_count} scored)" in health_result.output


def test_health_counts_comparisons_not_rows_one_part_not_watched(tmp_path, monkeypatch):
    class FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    import lockwire.cli as cli_module

    monkeypatch.setattr(cli_module.dt, "datetime", FrozenDatetime)
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[
            DeviceInstance(id="dev.healthy", model="m", owner="o"),
            DeviceInstance(id="dev.quiet", model="m", owner="o"),
        ],
    )
    report = PreflightReport([])
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 0
    assert "1 part(s) not watched" in result.output


def test_health_foreign_contract_rows_marked_and_excluded(tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.healthy", model="m", owner="o")],
    )
    report = PreflightReport([])
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)

    # The contract changes underneath the ledger row: its sha no longer matches.
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot2}\n", encoding="utf-8")

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3  # nothing compared under the CURRENT contract
    assert "(other contract)" in result.output
    assert "another contract version" in result.output


def test_health_excludes_rechained_row_with_stale_full_revision(tmp_path):
    """A matching legacy prefix is not enough for a newly written row.

    This models a tampered/rechained sidecar row whose short display digest was
    restamped to the current contract while the recorded full raw-byte revision
    remains the preceding result's dependency. The public library report must
    fail closed without changing either the contract or the pre-existing ledger
    bytes before the deliberate test tamper.
    """
    from lockwire_pro import api_pro as api
    from lockwire.edit import contract_revision

    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.healthy", model="m", owner="o")],
    )
    report = PreflightReport([])
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)
    ledger = sidecar_path_for(contract_path)
    baseline = ledger.read_bytes()
    prior_revision = contract_revision(contract_path)

    # The declared contract changes. Rechain a forged row that advertises the
    # new legacy prefix but retains the old full result dependency.
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot2}\n", encoding="utf-8")
    row = json.loads(baseline.decode().strip())
    from lockwire_pro.health import contract_sha

    row["contract_sha"] = contract_sha(contract_path)
    row["contract_revision"] = prior_revision
    row["prev"] = CHAIN_ZERO
    row["hash"] = chain_row_hash({**row, "hash": ""}, CHAIN_ZERO)
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    from lockwire_pro.health import read_ledger_lines

    assert chain_break(read_ledger_lines(ledger)) is None
    document = api.health(contract_path, now=NOW)
    assert document["watched"] == 0
    assert document["worst"] is None
    assert document["per_device"] == {}
    assert ledger.read_bytes() != baseline  # only the explicit tamper changed it


def test_health_legacy_revision_unknown_is_visible_not_current(tmp_path):
    """Old rows remain readable but cannot become current from a short prefix.

    Mutation bite: restoring the legacy ``not row.contract_revision`` current
    waiver in ``own_and_foreign`` makes the public health summary count this
    row and removes the explicit revision-unknown document/CLI disclosure.
    """
    from lockwire_pro import api_pro as api
    from lockwire_pro.health import contract_sha, read_ledger_lines

    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.healthy", model="m", owner="o")],
    )
    report = PreflightReport([])
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)
    ledger = sidecar_path_for(contract_path)
    baseline = ledger.read_bytes()

    # Simulate a pre-E1.3 row: omit only the later optional field, then retain
    # a valid chain and the same legacy prefix. Its original bytes need no
    # migration to remain readable.
    row = json.loads(baseline.decode().strip())
    row.pop("contract_revision")
    row["contract_sha"] = contract_sha(contract_path)
    row["prev"] = CHAIN_ZERO
    row["hash"] = chain_row_hash({**row, "hash": ""}, CHAIN_ZERO)
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert chain_break(read_ledger_lines(ledger)) is None

    summary = api.health(contract_path, now=NOW)
    assert summary["watched"] == 0
    assert summary["worst"] is None
    assert summary["per_device"] == {}
    document = api.health_document(contract_path, now=NOW)
    assert document["foreign_rows"] == 0
    device = next(item for item in document["devices"] if item["id"] == "dev.healthy")
    assert device["reason"] == "(revision unknown) recorded result predates full contract identity"
    assert device["coverage"]["reason_code"] == "legacy_revision_unknown"
    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3
    assert "(revision unknown)" in result.output
    assert ledger.read_bytes() != baseline  # only this deliberate legacy simulation changed it


def test_health_json_excludes_foreign_contract_rows_like_the_table_does(tmp_path):
    """``--json`` must score the same rows the table does. Mutation bite: revert
    ``own_rows`` back to ``rows`` at the ``health_summary(...)`` call site in
    cli.py -> this goes red (watched/worst/per_device count the foreign row)."""
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.healthy", model="m", owner="o")],
    )
    report = PreflightReport([])
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.healthy/signals/temp_c/range",
            observed="25.0 C",
            expected="within band [-10.0, 60.0] C",
            margin="35 C to spare",
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)

    # The contract changes underneath the ledger row: its sha no longer matches.
    contract_path.write_text("schema_version: '0.3'\nrobot: {name: testbot2}\n", encoding="utf-8")

    out_path = tmp_path / "out.json"
    result = runner.invoke(app, ["health", str(contract_path), "--json", str(out_path)])
    assert result.exit_code == 3  # matches the table's not_watched verdict
    import json

    summary = json.loads(out_path.read_text(encoding="utf-8"))
    assert summary["watched"] == 0
    assert summary["worst"] is None  # summary()'s own empty-per_device word for "nothing scored"
    assert summary["per_device"] == {}


def test_silent_device_is_absent_not_faulty_not_not_watched(tmp_path):
    """A device that declares a rate expectation (a driver)
    and produced zero samples this run is an absolute fault, with or without
    a baseline — flow.py's own ``devices/<id>/rate_hz`` "no samples" marker
    is the definitive signal.

    Mutation bite: drop the ``_silent_device_finding`` mapping (or its call
    site in ``_device_row``) and this test goes red — the device reads
    ``not_watched`` again.
    """
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[
            DeviceInstance(
                id="dev.silent",
                model="m",
                owner="o",
                driver=Driver(node="/sensors/imu", topic="/imu/data", nominal_rate_hz=200),
            )
        ],
    )
    silence = Finding(
        severity="not_scored",
        quantity="devices/dev.silent/rate_hz",
        observed="no samples for this device in the preflight source",
        expected="receive stream present so rate / cadence can be scored",
        owner="o",
        action="source cannot see this declaration's samples; data-flow not scored",
    )
    append_health_rows(
        contract_path,
        contract,
        PreflightReport([silence]),
        source="test",
        context=None,
        now=NOW,
    )
    row = latest_by_device(read_ledger(sidecar_path_for(contract_path)))["dev.silent"]
    assert row.state == "absent"  # was "faulty"
    assert row.reason == "no samples: /imu/data"

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3  # NOT SCORED, was 1 (NO-GO)
    assert "NOT SCORED" in result.output and "VIOLATION" not in result.output


def test_old_ledger_row_faulty_with_nothing_compared_reads_absent(tmp_path):
    """ "the health ledger row for that device reads `absent` (n_compared 0),
    never `faulty`; `lockwire health` reports the same". A row written before that says "faulty" with n_compared 0 (the review's own
    ledger); the chain is append-only, so it is read as absent, not NO-GO."""
    shutil.copytree(Path(__file__).parent / "fixtures" / "old_ledger", tmp_path / "l")
    contract = tmp_path / "l" / "c.yaml"
    assert '"state": "faulty"' in (tmp_path / "l" / "c.health.jsonl").read_text()
    result = runner.invoke(app, ["health", str(contract)])
    assert result.exit_code == 3
    assert "NOT SCORED" in result.output and "VIOLATION" not in result.output


def test_not_watched_reason_says_nothing_declared_when_nothing_is(tmp_path):
    """The word tells the user what to do — a device with no
    hard limit and no baseline reads "nothing declared to compare against"
    instead of the generic "nothing compared yet"."""
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        devices=[DeviceInstance(id="dev.bare", model="m", owner="o")],
    )
    append_health_rows(
        contract_path, contract, PreflightReport([]), source="test", context=None, now=NOW
    )
    row = latest_by_device(read_ledger(sidecar_path_for(contract_path)))["dev.bare"]
    assert row.state == "not_watched"
    assert row.reason == "nothing declared to compare against"


def test_hard_limit_violation_without_baseline_is_faulty_exit_1(tmp_path):
    """A hard-limit violation is faulty whether or not any envelope
    (baseline) is declared for the channel — measured true before this box's
    changes (the finding already lands as a violation); this pins it."""
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        device_models=[
            DeviceModel(
                id="mdl.imu",
                family="fam.imu",
                hard_limits={"accel_z": {"lo": -235.4, "hi": 235.4, "unit": "m/s^2"}},
            )
        ],
        devices=[DeviceInstance(id="dev.imu", model="mdl.imu", owner="o")],  # no envelopes
    )
    violation = Finding(
        severity="violation",
        quantity="devices/dev.imu/signals/accel_z/hard_limit",
        observed="300.00 m/s^2",
        expected="within hard limit [-235.40, 235.40] m/s^2",
        observed_quantity=Quantity(value=300.0, unit="m/s^2"),
        expected_quantity=Quantity(value=235.4, unit="m/s^2"),
        owner="o",
        action="reconcile the signal with the declared hard limit",
    )
    append_health_rows(
        contract_path,
        contract,
        PreflightReport([violation]),
        source="test",
        context=None,
        now=NOW,
    )
    row = latest_by_device(read_ledger(sidecar_path_for(contract_path)))["dev.imu"]
    assert row.state == "faulty"

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 1


def test_rail_gets_a_health_row_only_when_it_has_a_finding(tmp_path):
    """Rails/batteries get a row keyed by their id only when
    they have a finding this run (never an unconditional per-rail row like
    devices get) — violation -> faulty, kind "rail".

    Mutation bite: drop the rail-row wiring in ``append_health_rows`` and
    this test's ``rail.pm02-5v`` row goes missing.
    """
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        topology=Topology(
            rails=[
                Rail(id="rail.pm02-5v", capacity={"value": 15.6, "unit": "W"}),
                Rail(id="rail.quiet", capacity={"value": 5.0, "unit": "W"}),
            ]
        ),
    )
    violation = Finding(
        severity="violation",
        quantity="topology/rails/rail.pm02-5v/current_limit",
        observed="6.77 A computed draw on rail 'rail.pm02-5v'",
        expected="<= 0.00 A published current_limit",
        action="shed load on rail 'rail.pm02-5v'",
    )
    n = append_health_rows(
        contract_path,
        contract,
        PreflightReport([violation]),
        source="test",
        context=None,
        now=NOW,
    )
    assert n == 1  # only the rail with a finding gets a row; rail.quiet gets none
    rows = read_ledger(sidecar_path_for(contract_path))
    row = latest_by_device(rows)["rail.pm02-5v"]
    assert row.state == "faulty"
    assert row.kind == "rail"
    assert "rail.quiet" not in latest_by_device(rows)

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 1
    # The device column wraps a long label ("(rail) rail.pm02-5v") onto two
    # lines at this table width, so check the parts rather than one substring.
    assert "(rail)" in result.output
    assert "rail.pm02-5v" in result.output


def test_clean_fixture_ledger_unchanged_except_kind_field(tmp_path):
    """Byte-compare guard: this box's changes must not alter the clean
    fixture's ledger rows beyond adding the optional ``kind`` field and the
    not_watched reason wording — no new rows, no state changes,
    no new violations. No rail rows: the clean fixture's rails never carry a
    ``topology/rails/`` finding under `preflight` (those checks only run
    under `check`/`plan`, untouched by this box)."""
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    result = runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)])
    assert result.exit_code == 0

    rows = read_ledger(sidecar_path_for(contract_path))
    assert len(rows) == 5  # exactly the 5 declared devices; no rail rows appeared
    by_device = {row.device: row for row in rows}
    assert by_device["dev.imu"].state == "healthy"
    assert by_device["dev.barometer"].state == "healthy"
    assert by_device["dev.gnss"].state == "healthy"
    assert by_device["dev.radio.rfd900"].state == "not_watched"
    assert by_device["dev.esc.busless"].state == "not_watched"
    # The only textual change on the clean fixture: not_watched's reason for
    # a device with no hard limit and no baseline.
    assert by_device["dev.radio.rfd900"].reason == "nothing declared to compare against"
    assert by_device["dev.esc.busless"].reason == "nothing declared to compare against"
    assert all(row.kind == "device" for row in rows)


def test_no_ledger_writes_nothing(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    result = runner.invoke(
        app, ["preflight", str(contract_path), "--from", str(bag_path), "--no-ledger"]
    )
    assert result.exit_code == 0
    assert not sidecar_path_for(contract_path).exists()
    assert "health:" not in result.output


# --- the hash chain (E-B B-3) ---------------------------------------------


def _flat(output: str) -> str:
    """Rich wraps at the terminal width; the assertions care about words."""
    return " ".join(output.split())


def _append_runs(contract_path: Path, runs: int) -> int:
    contract = _contract()
    written = 0
    for index in range(runs):
        written += append_health_rows(
            contract_path,
            contract,
            [],
            source=f"unit-test run {index}",
            context=None,
            now=NOW + dt.timedelta(minutes=index),
        )
    return written


def _rows_on_disk(contract_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in sidecar_path_for(contract_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rewrite(contract_path: Path, rows: list[dict]) -> None:
    sidecar_path_for(contract_path).write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_ledger_rows_carry_prev_and_hash(tmp_path):
    contract_path = _write_contract(tmp_path)
    _append_runs(contract_path, 2)

    rows = _rows_on_disk(contract_path)
    assert len(rows) == 8
    assert all(row.get("hash") for row in rows), rows[0]
    # Each row's prev is the previous row's hash, and its hash covers itself.
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later["prev"] == earlier["hash"]
    assert chain_break(rows) is None


def test_first_row_prev_is_zeros(tmp_path):
    contract_path = _write_contract(tmp_path)
    _append_runs(contract_path, 1)

    rows = _rows_on_disk(contract_path)
    assert rows[0]["prev"] == CHAIN_ZERO == "0" * 64
    assert rows[1]["prev"] != CHAIN_ZERO


def test_verify_passes_on_twenty_clean_rows(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    assert _append_runs(contract_path, 5) == 20

    result = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert "chain verified" in _flat(result.output)
    assert "20 rows" in _flat(result.output)


def test_verify_names_the_first_broken_line_after_hand_edit(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    _append_runs(contract_path, 5)

    rows = _rows_on_disk(contract_path)
    rows[11]["device"] = "dev.tampered"
    _rewrite(contract_path, rows)

    result = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert result.exit_code == 1, result.output
    assert "chain broken at line 12" in _flat(result.output)


def test_verify_names_a_row_deleted_from_the_middle(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    _append_runs(contract_path, 5)

    rows = _rows_on_disk(contract_path)
    del rows[11]
    _rewrite(contract_path, rows)

    result = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert result.exit_code == 1, result.output
    # The deleted row was line 12; the row that took its place breaks the link.
    assert "chain broken at line 12" in _flat(result.output)


def test_verify_names_a_reordering(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    _append_runs(contract_path, 5)

    rows = _rows_on_disk(contract_path)
    rows[11], rows[12] = rows[12], rows[11]
    _rewrite(contract_path, rows)

    result = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert result.exit_code == 1, result.output
    assert "chain broken at line 12" in _flat(result.output)


def test_verify_on_an_empty_ledger_is_not_scored_never_verified(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)

    result = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert result.exit_code == 3, result.output
    assert "no rows to verify" in _flat(result.output)
    assert "verified" not in _flat(result.output).replace("no rows to verify", "")


def test_preflight_and_baseline_check_both_write_chained_rows(tmp_path):
    """Both real writers, end to end — chaining one of them is the bounce."""
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    preflight = runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)])
    assert preflight.exit_code == 0, preflight.output
    after_preflight = _rows_on_disk(contract_path)
    assert after_preflight and all(row.get("hash") for row in after_preflight)

    artifact = tmp_path / "baseline.yaml"
    built = runner.invoke(app, ["baseline", str(bag_path), "--context", "nominal-op"])
    assert built.exit_code == 0, built.output
    artifact.write_text(built.output, encoding="utf-8")

    checked = runner.invoke(
        app,
        ["baseline-check", str(bag_path), str(artifact), "--contract", str(contract_path)],
    )
    assert checked.exit_code == 0, checked.output
    after_both = _rows_on_disk(contract_path)
    assert len(after_both) > len(after_preflight), "baseline-check wrote no ledger rows"
    # The baseline-check rows continue the preflight chain rather than
    # starting their own: one ledger, one chain, two writers.
    assert all(row.get("hash") for row in after_both)
    assert after_both[len(after_preflight)]["prev"] == after_preflight[-1]["hash"]
    assert chain_break(after_both) is None

    verified = runner.invoke(app, ["health", "--verify", str(contract_path)])
    assert verified.exit_code == 0, verified.output


def test_contract_file_untouched_by_health_chain(tmp_path):
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)
    before = contract_path.read_bytes()

    assert (
        runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)]).exit_code
        == 0
    )
    _append_runs(contract_path, 2)

    assert contract_path.read_bytes() == before
    # The chain is sidecar-only: no `prev`/`hash` in the contract schema.
    assert "prev" not in Contract.model_fields
    assert "hash" not in Contract.model_fields


def test_health_json_has_envelope(tmp_path):
    """`health --json` writes a versioned document, not a bare summary().

    Before E0-25 it wrote `summary()` straight out — four body keys, no way for
    a consumer to detect a shape change. The body is unchanged; the three
    header fields are new.
    """
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    out = tmp_path / "h.json"

    result = runner.invoke(app, ["health", str(contract_path), "--json", str(out)])
    assert result.exit_code == 3
    document = json.loads(out.read_text())
    assert document["artifact_type"] == "lockwire-health-report"
    assert document["format_version"] == FORMAT_VERSION
    assert document["schema_version"] == SCHEMA_VERSION
    # summary()'s own shape is wrapped, never rewritten (the brief's constraint).
    assert set(document) >= {"last_run", "watched", "worst", "per_device"}


def test_preflight_row_carries_bag_sha_and_contract_sha(tmp_path):
    """A row `preflight` writes carries the bag sha it was
    scored against (`Recording.sha256`, already computed at load time) and
    the contract sha — both real, quoted from `lockwire health`'s own table
    output, never a second implementation's guess."""
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    bag_path = tmp_path / "clean"
    shutil.copytree(CLEAN_BAG, bag_path)

    assert (
        runner.invoke(app, ["preflight", str(contract_path), "--from", str(bag_path)]).exit_code
        == 0
    )

    rows = read_ledger(sidecar_path_for(contract_path))
    assert rows
    from lockwire_pro.recording import load_recording

    expected_bag_sha = load_recording(bag_path).sha256
    assert len(expected_bag_sha) == 64
    for row in rows:
        assert row.bag_sha == expected_bag_sha
        assert len(row.contract_sha) == 12

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 0
    assert expected_bag_sha in result.output
    assert rows[0].contract_sha in result.output


def test_historical_row_with_no_bag_sha_renders_absent_never_fabricated(tmp_path):
    """A row written before `bag_sha` existed carries "" — the table must
    render it as absent ("-"), never invent or recompute one (an
    explicit prohibition, same rule as a rail's unknown voltage)."""
    contract_path = tmp_path / "contract_clean.yaml"
    shutil.copy(CLEAN_CONTRACT, contract_path)
    from lockwire.schema import load_contract

    loaded = load_contract(contract_path)
    faulty_finding = Finding(
        severity="violation",
        quantity=f"devices/{loaded.devices[0].id}/signals/accel_x/hard_limit",
        observed="99.00 m/s^2",
        expected="within hard limit [-9.00, 9.00] m/s^2",
        observed_quantity=Quantity(value=99.0, unit="m/s^2"),
        expected_quantity=Quantity(value=9.0, unit="m/s^2"),
        owner=loaded.devices[0].owner,
        action="fix it",
    )
    # No bag_sha passed — the historical-row case.
    append_health_rows(
        contract_path,
        loaded,
        PreflightReport([faulty_finding]),
        source="test",
        context="nominal-op",
        now=NOW,
    )
    rows = read_ledger(sidecar_path_for(contract_path))
    assert rows[0].bag_sha == ""

    from lockwire_pro.health import build_document

    document = build_document(contract_path, loaded, now=NOW)
    faulty_entry = next(e for e in document.devices if e["id"] == loaded.devices[0].id)
    assert faulty_entry["bag_sha"] is None
    assert faulty_entry["recording"] is None

    result = runner.invoke(app, ["health", str(contract_path)])
    assert "-" in result.output  # the bag sha column renders absent, not a guess


def test_health_document_names_the_joint_each_device_drives():
    """The Run page lights the joint the contract says a device drives (no name guessing)."""
    from lockwire_pro import api_pro as api
    from lockwire_pro.api_payload_pro import HealthDocumentPayload

    expected = {d.id: d.drives_joint for d in api.load(CLEAN_CONTRACT).devices}
    assert any(expected.values())
    doc = api.health_document(CLEAN_CONTRACT)
    HealthDocumentPayload.model_validate(doc)
    assert {d["id"]: d["drives_joint"] for d in doc["devices"] if d["id"] in expected} == expected
