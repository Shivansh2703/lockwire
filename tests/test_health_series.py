"""E3-a: twenty clean runs make twenty plotted points, against the declared band.

Reuses the fixture pattern from ``test_health.py`` (a synthetic contract + model
declaring a hard limit, ``append_health_rows`` per run) rather than driving a
real recording through ``preflight`` twenty times — this box only tests the
series/band assembly in ``health.build_document``, not the checks that feed it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from lockwire_pro.cli_pro import build_health_document as cli_build_health_document
from lockwire_pro.health import (
    append_health_rows,
    build_document,
    device_series,
    read_ledger,
    sidecar_path_for,
)
from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import ScoredRecord
from lockwire.schema import Band, Contract, DeviceInstance, DeviceModel, Quantity, RobotInfo

NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)


def _band_contract() -> Contract:
    return Contract(
        robot=RobotInfo(name="testbot"),
        device_models=[
            DeviceModel(
                id="m", family="fam.x", hard_limits={"accel_x": Band(lo=-9.0, hi=9.0, unit="m/s^2")}
            )
        ],
        devices=[DeviceInstance(id="dev.imu", model="m", owner="o")],
    )


def _clean_report(value: float) -> PreflightReport:
    """A single passing comparison for ``dev.imu``'s ``accel_x`` channel."""
    report = PreflightReport([], scored_by_stage={3: 1})
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.imu/signals/accel_x/hard_limit",
            observed=f"{value:.2f} m/s^2",
            expected="within hard limit [-9.00, 9.00] m/s^2",
            margin="wide",
            observed_quantity=Quantity(value=value, unit="m/s^2"),
            expected_quantity=Quantity(value=9.0, unit="m/s^2"),
        )
    ]
    return report


def _write_contract(tmp_path: Path) -> Path:
    path = tmp_path / "lockwire.yaml"
    path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    return path


def _twenty_clean_runs(tmp_path: Path) -> Path:
    contract_path = _write_contract(tmp_path)
    contract = _band_contract()
    for i in range(20):
        append_health_rows(
            contract_path,
            contract,
            _clean_report(1.0 + 0.01 * i),
            source="test",
            context="nominal-op",
            now=NOW + dt.timedelta(minutes=i),
        )
    return contract_path


def test_twenty_clean_runs_make_twenty_plotted_points(tmp_path):
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()
    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    series = dev["series"]
    assert len(series) == 1
    s = series[0]
    assert s["channel"] == "accel_x"
    assert len(s["points"]) == 20
    assert s["band"] == {"lo": -9.0, "hi": 9.0, "unit": "m/s^2"}
    assert {p["kind"] for p in s["points"]} == {"measured"}
    assert document.series_excluded_foreign == 0


def test_out_of_order_append_orders_by_run_not_position(tmp_path):
    """A merged or hand-copied ledger appends rows out of chronological
    order. Append the LATER run first, then an EARLIER one — position is the
    reverse of time, exactly the case this covers — and assert the series
    still comes back ordered by each point's own ``run`` timestamp."""
    contract_path = _write_contract(tmp_path)
    contract = _band_contract()
    append_health_rows(
        contract_path,
        contract,
        _clean_report(5.0),
        source="late",
        context="nominal-op",
        now=NOW + dt.timedelta(minutes=10),
    )
    append_health_rows(
        contract_path,
        contract,
        _clean_report(1.0),
        source="early",
        context="nominal-op",
        now=NOW,
    )

    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    points = dev["series"][0]["points"]
    assert [p["run"] for p in points] == sorted(p["run"] for p in points)
    assert [p["value"] for p in points] == [1.0, 5.0]


def test_foreign_rows_are_never_plotted(tmp_path):
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()

    # A row scored against a different contract sha, same device/channel.
    ledger = sidecar_path_for(contract_path)
    rows = read_ledger(ledger)
    foreign = rows[-1].model_copy(update={"contract_sha": "0" * 64})
    import json

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(foreign.model_dump(mode="json")) + "\n")

    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    assert len(dev["series"][0]["points"]) == 20  # the foreign row never joins the series
    assert document.series_excluded_foreign == 1


def test_series_is_sufficient_to_draw_the_chart(tmp_path):
    """No consumer of the series needs to go back to the ledger: every point
    carries value, run, run_id and kind, and the band sits beside them once."""
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()
    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    s = dev["series"][0]
    assert s["band"] is not None
    for point in s["points"]:
        assert set(point) >= {"value", "run", "run_id", "kind"}
        assert point["value"] is not None
        assert point["run"]
        assert point["run_id"]


def test_every_point_carries_its_kind(tmp_path):
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()
    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    for point in dev["series"][0]["points"]:
        assert point["kind"] == "measured"


def test_watched_only_device_gets_series_with_no_band(tmp_path):
    """A device whose model declares no hard limit for the channel is
    watched against its baseline only — its points still record, its band is
    ``None``, never a guessed one."""
    contract_path = _write_contract(tmp_path)
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        device_models=[DeviceModel(id="m", family="fam.x")],  # no hard_limits
        devices=[DeviceInstance(id="dev.baro", model="m", owner="o")],
    )
    report = PreflightReport([], scored_by_stage={3: 1})
    report.scored_records = [
        ScoredRecord(
            quantity="devices/dev.baro/signals/pressure_pa/range",
            observed="101000 Pa",
            expected="within envelope band [95000, 105000] Pa",
            margin="wide",
            observed_quantity=Quantity(value=101000, unit="Pa"),
            expected_quantity=Quantity(value=105000, unit="Pa"),
        )
    ]
    append_health_rows(contract_path, contract, report, source="test", context=None, now=NOW)

    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.baro")
    assert dev["state"] == "watched"
    assert len(dev["series"]) == 1
    assert dev["series"][0]["band"] is None
    assert len(dev["series"][0]["points"]) == 1


def test_device_with_no_recorded_row_gets_an_empty_series_not_absent(tmp_path):
    contract_path = _write_contract(tmp_path)
    contract = _band_contract()
    document = build_document(contract_path, contract, now=NOW)
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    assert dev["series"] == []


def test_device_series_helper_reuses_history_directly():
    """``device_series`` (no second walk) reused straight from the module,
    not only through ``build_document`` — the same object the accept line reads."""
    contract = _band_contract()
    assert device_series([], contract, contract.devices[0]) == ([], 0)


def test_two_offsets_for_the_same_instant_sort_equal(tmp_path):
    """E3-a-r3: a foreign machine's offset must not misorder against a local
    one — the exact r2 bounce. p2 and p3 name the SAME instant (13:00 UTC),
    one spelled "Z" and one spelled "-04:00" ("09:00-04:00" = 13:00 UTC), and
    p1 is a genuinely earlier instant (12:00 UTC).

    Sorted by RAW STRING, "09:00:00-04:00" < "12:00:00Z" < "13:00:00Z" —p3
    would read as the EARLIEST point, ahead of p1, though it is really tied
    with p2 for latest. Sorted by the PARSED instant, p1 comes first and p2/p3
    tie (kept in stable, append order): [1.0, 2.0, 3.0].
    """
    contract_path = _write_contract(tmp_path)
    contract = _band_contract()
    append_health_rows(
        contract_path, contract, _clean_report(1.0), source="a", context="nominal-op", now=NOW
    )
    append_health_rows(
        contract_path, contract, _clean_report(2.0), source="b", context="nominal-op", now=NOW
    )
    append_health_rows(
        contract_path, contract, _clean_report(3.0), source="c", context="nominal-op", now=NOW
    )

    ledger = sidecar_path_for(contract_path)
    rows = read_ledger(ledger)
    assert len(rows) == 3
    assert rows[0].run == "2026-09-08T12:00:00Z"  # p1: genuinely earliest
    rows[1] = rows[1].model_copy(update={"run": "2026-09-08T13:00:00Z"})  # p2
    rows[2] = rows[2].model_copy(
        update={"run": "2026-09-08T09:00:00-04:00"}
    )  # p3, same instant as p2

    import json

    ledger.write_text(
        "\n".join(json.dumps(r.model_dump(mode="json")) for r in rows) + "\n", encoding="utf-8"
    )

    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    points = dev["series"][0]["points"]
    assert [p["value"] for p in points] == [1.0, 2.0, 3.0]


def test_garbage_timestamp_is_excluded_and_counted(tmp_path):
    """E3-a-r3: a row whose ``run`` cannot be parsed is excluded from the
    series entirely (never sorted to the head or the tail) and produces a
    named, distinguishable count — never merged into ``series_excluded_foreign``."""
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()

    ledger = sidecar_path_for(contract_path)
    rows = read_ledger(ledger)
    garbage = rows[-1].model_copy(update={"run": "not-a-timestamp"})
    # Insert (never append last): the LATEST row for the device must stay
    # parseable, so this test exercises only `device_series`'s exclusion, not
    # the separate (and out-of-scope) `latest_by_device`
    # "most recent by file position" defect that a trailing garbage row
    # would otherwise trip over via `device_state`/`row_age`.
    rows = rows[:-1] + [garbage, rows[-1]]

    import json

    ledger.write_text(
        "\n".join(json.dumps(r.model_dump(mode="json")) for r in rows) + "\n", encoding="utf-8"
    )

    document = build_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    points = dev["series"][0]["points"]
    assert len(points) == 20  # the garbage row joins neither end of the series
    assert all(p["run"] != "not-a-timestamp" for p in points)
    assert document.series_excluded_unplaceable == 1
    assert document.series_excluded_foreign == 0  # a different reason, a different count


def test_cli_reaches_the_series_through_its_own_build_function(tmp_path):
    """The CLI door's ``health`` command builds its document through the exact
    same ``build_document`` import cli.py uses for both the table and the
    ``--json`` summary — proving the series is reachable through the CLI's own
    code path, not a second one this box built only for the library door.

    ``--json`` itself stays the narrower per-device *state* summary
    (``health.summary``, shared verbatim with the GUI's strict payload) — widening
    that flat JSON to carry a nested series would require touching
    ``gui.py``/``api_payload.py``'s strict ``HealthPayload``, outside
    this box's file list. See the worker report for this discrepancy against
    the brief's table, flagged per BRIEF-PROVENANCE LAW.
    """
    contract_path = _twenty_clean_runs(tmp_path)
    contract = _band_contract()
    document = cli_build_health_document(contract_path, contract, now=NOW + dt.timedelta(days=1))
    dev = next(d for d in document.devices if d["id"] == "dev.imu")
    assert len(dev["series"][0]["points"]) == 20
