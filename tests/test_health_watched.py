""" "Watched, no limit declared" is not a fault, and the day-one
document says what to author when nothing has been compared.

Two branches, both in ``health.py``'s ``_device_row``/``build_document``:
(1) a device with baseline history and no declared hard limit reads
``watched``, never ``healthy``/``faulty`` — its drift is shown, never a
verdict. (2) when no comparison exists anywhere in the contract, the
document says so in one sentence and carries no per-device series/band.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from typer.testing import CliRunner

from lockwire_pro import api_pro as api
from lockwire.cli import app
from lockwire_pro.health import append_health_rows, latest_by_device, read_ledger, sidecar_path_for
from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import Finding
from lockwire.schema import (
    Contract,
    ContextEnvelope,
    DeviceInstance,
    DeviceModel,
    Provenance,
    RobotInfo,
    SignalEnvelope,
)
from lockwire.schema import Band as _Band

runner = CliRunner()
NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)


def _write_contract(tmp_path: Path) -> Path:
    path = tmp_path / "lockwire.yaml"
    path.write_text("schema_version: '0.3'\nrobot: {name: testbot}\n", encoding="utf-8")
    return path


def _no_limit_contract() -> Contract:
    """One device, baseline history declared (a signal envelope), no model
    hard limit anywhere — the watched-only shape."""
    return Contract(
        robot=RobotInfo(name="testbot"),
        devices=[
            DeviceInstance(
                id="dev.gnss",
                model="mdl.gnss.generic",  # not in device_models — no hard limit
                owner="o",
                envelopes=[
                    ContextEnvelope(
                        context="nominal-op",
                        signals={
                            "gnss_altitude_m": SignalEnvelope(
                                band=_Band(lo=-5.0, hi=500.0, unit="m")
                            )
                        },
                        provenance=Provenance(
                            witness="w0", source="bag", locked_at=dt.date(2026, 9, 1)
                        ),
                    )
                ],
            )
        ],
    )


def _drift_violation() -> Finding:
    """A fitted-band exceedance on the envelope-band ("range") leg — exactly
    what ``checks.py``'s fitted-band budget already raises (unmodified by
    this box)."""
    return Finding(
        severity="violation",
        quantity="devices/dev.gnss/signals/gnss_altitude_m/range",
        observed="612.00 m",
        expected="fitted-band exceedance count below k* within envelope band [-5.00, 500.00] m",
        owner="o",
        action="reconcile the signal distribution with the declared envelope band",
    )


def test_drift_with_no_limit_is_never_a_fault(tmp_path):
    """The failing check: a fault raised from a fitted band with no
    declared limit. Mutation: drop the `declares_limit` gate in
    `_device_row` (treat the drift violation like any other) and this goes
    red — the device reads `faulty`, exit 1."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path,
        _no_limit_contract(),
        PreflightReport([_drift_violation()]),
        source="test",
        context="nominal-op",
        now=NOW,
    )
    row = latest_by_device(read_ledger(sidecar_path_for(contract_path)))["dev.gnss"]
    assert row.state != "faulty"
    assert row.state == "watched"

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code != 1
    assert "faulty" not in result.output.lower()


def test_watched_row_says_no_limit_declared(tmp_path):
    """The row itself carries the words "no limit declared" — dropping them
    from WORDS/the reason is the second half of that failing check."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path,
        _no_limit_contract(),
        PreflightReport([_drift_violation()]),
        source="test",
        context="nominal-op",
        now=NOW,
    )
    row = latest_by_device(read_ledger(sidecar_path_for(contract_path)))["dev.gnss"]
    assert "no limit declared" in row.reason

    # The document every door (CLI table, MCP, page) reads its "word" and
    # "reason" from — checked here directly rather than through the CLI's
    # rich-rendered table, whose column wrapping is cosmetic and unrelated
    # to this claim.
    document = api.health_document(contract_path, now=NOW)
    entry = next(d for d in document["devices"] if d["id"] == "dev.gnss")
    assert entry["word"] == "watched, no limit declared"
    assert "no limit declared" in entry["reason"]
    assert "612.00" in entry["reason"]  # the drift itself is shown, not hidden

    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 0
    assert "faulty" not in result.output.lower()


def test_day_one_branches_on_applicable_comparisons(tmp_path):
    """v8 line 900: the day-one branch is on whether a comparison happened,
    never on whether a baseline was authored. A device with a baseline
    declared (``_device_declared`` True) but zero runs must still read
    ``nothing_watched`` at the document level, same as one with nothing
    declared at all — mutating the gate to key off "declared" instead of
    ``scored`` makes this red (it would then read the ordinary per-device
    table, not the day-one document)."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path,
        _no_limit_contract(),  # baseline authored (envelope with signals)
        PreflightReport([]),  # but nothing was ever compared
        source="test",
        context=None,
        now=NOW,
    )
    document = api.health_document(contract_path, now=NOW)
    assert document["nothing_watched"] is True
    assert document["scored"] == 0
    assert document["next_step"] == "Nothing watched yet"
    assert "author" in document["next_action"] or "declare" in document["next_action"]


def test_day_one_document_emits_no_series(tmp_path):
    """The Python half of the day-one face's data: `nothing_watched: true`
    and the sentence, no per-device series/band for a face to draw chart
    furniture from."""
    contract_path = _write_contract(tmp_path)
    document = api.health_document(contract_path, now=NOW)  # no ledger at all yet
    assert document["nothing_watched"] is True
    assert document["devices"] == []
    assert document["verdict"] == "NOT_SCORED"
    assert document["exit_code"] == 3
    assert "lockwire baseline" in document["next_action"]


def test_day_one_says_what_to_author(tmp_path):
    """CLI accept line 2: NOT SCORED, exit 3, one sentence naming what to
    author, the exact `lockwire baseline …` command, no table."""
    contract_path = _write_contract(tmp_path)
    result = runner.invoke(app, ["health", str(contract_path)])
    assert result.exit_code == 3
    assert "NOT SCORED" in result.output
    assert "lockwire baseline" in result.output
    assert "runs recorded" not in result.output  # no per-device table


def test_mcp_day_one_document(tmp_path):
    """The MCP `health` tool serves `api.health_document` verbatim
    (`mcp_server.py`'s `health` tool body) — this is that same document."""
    contract_path = _write_contract(tmp_path)
    document = api.health_document(contract_path, now=NOW)
    assert document["nothing_watched"] is True
    assert "next_step" in document


def _declared_limit_contract() -> Contract:
    """One device whose model declares a hard limit — the review bounce's
    Case A shape: a contract that already declares, unlike
    `_no_limit_contract()`."""
    return Contract(
        robot=RobotInfo(name="testbot"),
        device_models=[
            DeviceModel(
                id="m",
                family="fam.x",
                hard_limits={"accel_x": _Band(lo=-9.0, hi=9.0, unit="m/s^2")},
            )
        ],
        devices=[DeviceInstance(id="dev.imu", model="m", owner="o")],
    )


def test_recorded_run_that_compared_nothing_shows_the_table(tmp_path):
    """Bounce fix (b): a device with a declared hard limit and one recorded
    run that compared nothing is `nothing_watched` at the document level
    (scored == 0, unchanged), but it has HISTORY — the CLI must show its row,
    not swallow it into the generic day-one sentence. Mutation: drop the
    `has_history` gate in cli.py (go back to `if document.nothing_watched:`)
    and this goes red — the table disappears and the generic sentence
    (which falsely tells an already-declaring contract to "declare a hard
    limit") appears instead."""
    contract_path = _write_contract(tmp_path)
    append_health_rows(
        contract_path,
        _declared_limit_contract(),
        PreflightReport([]),  # a run happened; it compared nothing
        source="test",
        context=None,
        now=NOW,
    )
    document = api.health_document(contract_path, now=NOW)
    assert document["nothing_watched"] is True  # unchanged (can stay as is)

    result = runner.invoke(app, ["health", str(contract_path)])
    assert "runs recorded" in result.output  # the table, not the sentence
    assert "dev.imu" in result.output
    assert "declare a hard limit" not in result.output  # already declared — not false advice


def test_never_run_and_ran_once_are_not_identical(tmp_path):
    """Bounce's decisive item: a contract never run and one run once (that
    compared nothing) must not be byte-identical at the CLI — the recorded
    run is history a user can see."""
    never_dir = tmp_path / "never"
    never_dir.mkdir()
    never_run_path = _write_contract(never_dir)
    ran_once_dir = tmp_path / "ran_once"
    ran_once_dir.mkdir()
    ran_once_path = _write_contract(ran_once_dir)
    append_health_rows(
        ran_once_path,
        _declared_limit_contract(),
        PreflightReport([]),
        source="test",
        context=None,
        now=NOW,
    )

    never_run_output = runner.invoke(app, ["health", str(never_run_path)]).output
    ran_once_output = runner.invoke(app, ["health", str(ran_once_path)]).output
    assert never_run_output != ran_once_output
