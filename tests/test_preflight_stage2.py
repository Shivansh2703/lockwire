"""Stage 2 data-flow checks (B87).

Real assertions use synthetic Recording + Contract constructions with MATCHING
device ids. CLI smoke confirms the clean fixture still GOes once ids align (B100).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from lockwire.checks import _ScoreBook
from lockwire.cli import app
from lockwire_pro.preflight.source import LiveSource, RecordedSource
from lockwire_pro.preflight.stages import flow
from lockwire_pro.preflight.stages.flow import (
    _MONOTONICITY_OBSERVED,
    _MONOTONICITY_QUANTITY,
    _WALL_CLOCK_OBSERVED,
    _WALL_CLOCK_QUANTITY,
)
from lockwire_pro.recording import DeviceStream, Recording
from lockwire.report import render
from lockwire.schema import (
    Band,
    ContextEnvelope,
    Contract,
    DeviceInstance,
    Driver,
    DriverProcedure,
    SignalSource,
    FittedBandEvidence,
    Provenance,
    RobotInfo,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"
RATE_COLLAPSE_BAG = FIXTURES / "bags" / "rate_collapse"
UNENVELOPED_CONTRACT = FIXTURES / "contract_rate_unenveloped.yaml"
DEVICE_ID = "dev.imu"

runner = CliRunner()


def _provenance() -> Provenance:
    return Provenance(
        witness="live",
        source="synthetic-b87",
        locked_at=date(2026, 7, 5),
    )


def _contract_with_rate_band(
    *,
    context: str,
    rate_hz: Band,
    nominal_rate_hz: float | None = 200.0,
):
    envelope = ContextEnvelope(
        context=context,
        rate_hz=rate_hz,
        provenance=_provenance(),
    )
    return Contract(
        robot=RobotInfo(name="flow-test"),
        devices=[
            DeviceInstance(
                id=DEVICE_ID,
                model="mdl.imu",
                driver=Driver(nominal_rate_hz=nominal_rate_hz),
                envelopes=[envelope],
                owner="test",
            )
        ],
    )


def _recording(
    tmp_path: Path,
    *,
    recv_times: list[float],
    context: str = "bench-idle",
    receipt_monotonic: bool | None = None,
) -> Recording:
    return Recording(
        context=context,
        units={DEVICE_ID: {"accel_z": "m/s^2"}},
        devices={
            DEVICE_ID: DeviceStream(
                device=DEVICE_ID,
                recv_times=list(recv_times),
                signals={"accel_z": [0.0] * len(recv_times)},
            )
        },
        env={},
        sha256="synthetic-b87",
        path=tmp_path,
        receipt_monotonic=receipt_monotonic,
    )


def _findings(contract, recording):
    return flow.check(contract, RecordedSource(recording))


def test_implemented_flag_is_true():
    assert flow.IMPLEMENTED is True


def test_stage_never_returns_empty(tmp_path):
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    findings = _findings(contract, _recording(tmp_path, recv_times=[0.0, 0.005]))
    assert findings


def test_rate_collapse_is_violation_with_typed_quantities(tmp_path):
    """Synthetic rate collapse with matching device ids → stage-2 rate violation.

    Mutation-bite: widen the rate-band comparison so this collapse fits inside
    the band (or skip the out-of-band test) and this assertion goes red.
    """
    band = Band(lo=190, hi=210, unit="Hz")
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=band)
    # Exact 1 Hz — well below the declared band.
    recording = _recording(tmp_path, recv_times=[float(i) for i in range(8)])
    findings = _findings(contract, recording)

    violations = [
        f
        for f in findings
        if f.severity == "violation" and f.quantity == f"devices/{DEVICE_ID}/rate_hz"
    ]
    assert len(violations) == 1
    finding = violations[0]
    assert finding.observed_quantity is not None
    assert finding.expected_quantity is not None
    assert finding.observed_quantity.unit == "Hz"
    assert finding.observed_quantity.value < band.lo
    assert finding.expected_quantity.value == band.lo
    assert DEVICE_ID in finding.quantity


def test_fitted_rate_violation_reports_budget_operands():
    device = DeviceInstance(id=DEVICE_ID, model="mdl.imu", owner="test")
    stream = DeviceStream(
        device=DEVICE_ID,
        recv_times=[i * 0.01 for i in range(101)],
        signals={},
    )
    findings = flow._fitted_rate_findings(
        device,
        stream,
        Band(lo=190, hi=210, unit="Hz"),
        FittedBandEvidence(rank=1, n_eff=100, alpha=0.005),
    )

    assert len(findings) == 1
    assert findings[0].severity == "violation"
    for operand in ("K=", "k*=", "n=", "m=", "b=", "alpha_side=", "tail probability="):
        assert operand in findings[0].observed


def test_in_band_rate_is_not_a_rate_violation(tmp_path):
    band = Band(lo=190, hi=210, unit="Hz")
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=band)
    # Exact 200 Hz.
    recording = _recording(tmp_path, recv_times=[i * 0.005 for i in range(40)])
    findings = _findings(contract, recording)

    assert not any(f.severity == "violation" and f.quantity.endswith("/rate_hz") for f in findings)


def test_gap_without_envelope_is_not_scored_and_sustained_collapse_still_fails(tmp_path):
    contract = Contract(
        robot=RobotInfo(name="flow-test"),
        devices=[
            DeviceInstance(
                id=DEVICE_ID,
                model="mdl.imu",
                driver=Driver(nominal_rate_hz=200.0),
                owner="test",
            )
        ],
    )

    recording = _recording(tmp_path, recv_times=[float(i) for i in range(8)])
    findings = _findings(contract, recording)

    gap_disclosures = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == f"devices/{DEVICE_ID}/recv_gap"
    ]
    sustained = [
        f
        for f in findings
        if f.severity == "violation"
        and f.quantity == f"devices/{DEVICE_ID}/sustained_rate_liveness"
    ]
    assert (
        len(gap_disclosures) == 1
        and DEVICE_ID in gap_disclosures[0].observed
        and "no envelope declared" in gap_disclosures[0].observed
        and len(sustained) == 1
        and "50%" in sustained[0].expected
        and "liveness determination" in sustained[0].expected
        and "not a declared tolerance" in sustained[0].expected
    )


def test_one_late_message_is_not_a_sustained_collapse(tmp_path):
    contract = load_contract(UNENVELOPED_CONTRACT)
    # 200 Hz for 100 messages, then one message arrives 100 ms late. The gap leg
    # is not scored; the capture-wide achieved rate remains far above 50%.
    recv_times = [i * 0.005 for i in range(100)] + [0.595]
    findings = _findings(contract, _recording(tmp_path, recv_times=recv_times))
    disclosures = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == f"devices/{DEVICE_ID}/recv_gap"
    ]
    sustained_violations = [f for f in findings if f.quantity.endswith("/sustained_rate_liveness")]
    assert (
        len(disclosures) == 1
        and "no envelope declared" in disclosures[0].observed
        and sustained_violations == []
    )


def test_small_delta_gap_breach_renders_distinct_numbers(tmp_path):
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=200, hi=210, unit="Hz"),
    )
    findings = _findings(contract, _recording(tmp_path, recv_times=[0.0, 0.010410414]))
    gap = next(f for f in findings if f.quantity == f"devices/{DEVICE_ID}/recv_gap")
    console = Console(record=True, width=200, color_system=None)
    render([gap], console)
    line = next(text for text in console.export_text().splitlines() if "observed " in text)
    observed = line.split("observed ", 1)[1].split(" s", 1)[0]
    expected = line.split("expected <= ", 1)[1].split(" s", 1)[0]
    assert len((observed, expected)) == 2 and observed != expected


def test_unenveloped_clean_goes_and_sustained_collapse_does_not():
    cases = (
        (CLEAN_BAG, 0, "GO", "NO-GO"),
        (RATE_COLLAPSE_BAG, 1, "NO-GO", None),
    )
    results = [
        (
            expected_exit,
            required,
            forbidden,
            runner.invoke(
                app,
                ["preflight", str(UNENVELOPED_CONTRACT), "--from", str(bag), "--no-ledger"],
            ),
        )
        for bag, expected_exit, required, forbidden in cases
    ]
    assert all(
        result.exit_code == expected_exit
        and required in result.output
        and (forbidden is None or forbidden not in result.output)
        # PR-2 lists each not-compared check again after the verdict: 3 findings + 3 list rows
        and " ".join(result.output.split()).count("no envelope declared") == 6
        for expected_exit, required, forbidden, result in results
    )


def test_monotonicity_live_path_not_scored_carries_both_facts(tmp_path):
    """No bag/live receipt-order fact (``receipt_monotonic is None``): stays a
    not_scored disclosure — only the native/ulog case; live/.mcap now scores."""
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    findings = _findings(contract, _recording(tmp_path, recv_times=[0.0, 0.005]))

    mono = [f for f in findings if f.quantity == _MONOTONICITY_QUANTITY]
    assert len(mono) == 1
    finding = mono[0]
    assert finding.severity == "not_scored"
    assert finding.observed == _MONOTONICITY_OBSERVED
    # Fact (a): RecordedSource / load_recording upstream enforcement.
    assert "RecordedSource" in finding.observed
    assert "load_recording" in finding.observed
    assert "enforced upstream" in finding.observed
    assert "cannot fail here" in finding.observed
    # Fact (b), E3-FL: the live/.mcap path now scores this quantity directly
    # (see test_monotonicity_scores_pass_with_evidence_numbers and
    # test_monotonicity_scores_violation_when_receipt_order_broke below) — this
    # disclosure only fires when there is no such receipt-order fact to score.
    # B112 corrected this text once already, after it spent an hour describing
    # LiveSource as "future" though it had shipped; "refuses samples()" was the
    # next thing that went stale (E3-DC made samples() work) and is asserted
    # ABSENT here so the wrong wording cannot silently come back.
    assert "LiveSource" in finding.observed
    assert "refuses samples()" not in finding.observed, (
        "E3-FL: LiveSource.samples() works now (E3-DC) — this text described "
        "a permanent refusal that stopped being true"
    )
    assert "receipt order" in finding.observed
    assert "future" not in finding.observed, (
        "B112: the disclosure must not describe LiveSource as future — it shipped"
    )
    assert "live_path" in finding.quantity


def test_monotonicity_scores_pass_with_evidence_numbers(tmp_path):
    """E3-FL: a genuinely monotonic bag capture SCORES — never a bare pass."""
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    recording = _recording(tmp_path, recv_times=[0.0, 0.005], receipt_monotonic=True)
    scores = _ScoreBook()
    findings = flow.check(contract, RecordedSource(recording), scores=scores)

    # Never a bare pass: no not_scored/violation Finding at this quantity —
    # the pass lives in the ScoreBook, with evidence numbers in its text.
    assert not [f for f in findings if f.quantity == _MONOTONICITY_QUANTITY]
    passed = [r for r in scores.records if r.quantity == _MONOTONICITY_QUANTITY]
    assert len(passed) == 1
    record = passed[0]
    assert "2 sample" in record.observed  # sample count
    assert "recv-time span" in record.observed and "s" in record.observed  # recv-time span
    assert "non-decreasing" in record.observed


def test_monotonicity_scores_violation_when_receipt_order_broke(tmp_path):
    """MUTATION-BITE target: produce the state where the finding goes red.

    ``receipt_monotonic=False`` is what ``load_ros2_bag`` (E3-FL) reports when a
    real capture's messages arrived out of log-time order in file order, before
    its own defensive sort. Proves the pass above has a real failing condition
    (rule 30) rather than being unfalsifiable.
    """
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    recording = _recording(tmp_path, recv_times=[0.0, 0.005], receipt_monotonic=False)
    scores = _ScoreBook()
    findings = flow.check(contract, RecordedSource(recording), scores=scores)

    violations = [
        f for f in findings if f.quantity == _MONOTONICITY_QUANTITY and f.severity == "violation"
    ]
    assert len(violations) == 1
    finding = violations[0]
    assert "2 sample" in finding.observed
    assert "out of log-time order" in finding.observed
    assert not [r for r in scores.records if r.quantity == _MONOTONICITY_QUANTITY]


def test_wall_clock_currency_not_scored_by_name(tmp_path):
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    findings = _findings(contract, _recording(tmp_path, recv_times=[0.0, 0.005]))

    wall = [f for f in findings if f.quantity == _WALL_CLOCK_QUANTITY]
    assert len(wall) == 1
    assert wall[0].severity == "not_scored"
    assert wall[0].observed == _WALL_CLOCK_OBSERVED
    assert "wall-clock" in wall[0].observed


def _live_source(*, seconds: float, contract, recording: Recording) -> LiveSource:
    """A LiveSource whose samples() returns ``recording`` from cache — the real
    ``_capture_once`` (docker/ros2) is never invoked, no docker needed."""
    source = LiveSource(contract=contract, runner=lambda *a, **k: None, seconds=seconds)
    source._capture = recording
    return source


def test_wall_clock_currency_scores_pass_with_evidence_numbers(tmp_path):
    """E3-FL: a live capture whose last sample lands inside the window SCORES."""
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
        nominal_rate_hz=5.0,  # period 0.2s
    )
    # Capture window 1.0s; last sample at 0.95s — 0.05s trailing gap, inside 0.2s.
    recording = _recording(tmp_path, recv_times=[0.0, 0.5, 0.95])
    source = _live_source(seconds=1.0, contract=contract, recording=recording)
    scores = _ScoreBook()
    findings = flow.check(contract, source, scores=scores)

    assert not [f for f in findings if f.quantity == _WALL_CLOCK_QUANTITY]
    passed = [r for r in scores.records if r.quantity == _WALL_CLOCK_QUANTITY]
    assert len(passed) == 1
    assert "trailing gap" in passed[0].observed
    assert "capture window" in passed[0].observed


def test_wall_clock_currency_scores_violation_when_publisher_goes_quiet_early(tmp_path):
    """MUTATION-BITE target: produce the state where wall-clock currency goes red.

    The publisher's last sample lands well before the capture window closes —
    a real live-path signal a stale/offline .mcap read must NOT also trigger
    (see test_a_bag_recording_runs_through_the_real_gate in test_ros2bag.py,
    which loads a bag dated 2023 through RecordedSource and must stay GO).
    """
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
        nominal_rate_hz=5.0,  # period 0.2s
    )
    # Capture window 5.0s; last sample at 0.5s — 4.5s trailing gap, way past 0.2s.
    recording = _recording(tmp_path, recv_times=[0.0, 0.3, 0.5])
    source = _live_source(seconds=5.0, contract=contract, recording=recording)
    scores = _ScoreBook()
    findings = flow.check(contract, source, scores=scores)

    violations = [
        f for f in findings if f.quantity == _WALL_CLOCK_QUANTITY and f.severity == "violation"
    ]
    assert len(violations) == 1
    assert "trailing gap" in violations[0].observed
    assert "exceeding the tightest declared cadence" in violations[0].observed
    assert not [r for r in scores.records if r.quantity == _WALL_CLOCK_QUANTITY]


def test_no_rate_operands_is_silent_not_assumed_healthy(tmp_path):
    """Device with neither nominal_rate_hz nor rate_hz envelope: rate legs stay silent."""
    contract = Contract(
        robot=RobotInfo(name="flow-test"),
        devices=[
            DeviceInstance(
                id=DEVICE_ID,
                model="mdl.imu",
                driver=Driver(nominal_rate_hz=None),
                owner="test",
            )
        ],
    )

    findings = _findings(contract, _recording(tmp_path, recv_times=[0.0, 1.0]))
    assert not any(DEVICE_ID in f.quantity and f.quantity.endswith("/rate_hz") for f in findings)
    assert not any(DEVICE_ID in f.quantity and f.quantity.endswith("/recv_gap") for f in findings)
    # Stage still discloses live-path legs — never empty.
    assert any(f.quantity == _MONOTONICITY_QUANTITY for f in findings)


def test_missing_samples_for_declared_rate_device_is_not_scored(tmp_path):
    contract = _contract_with_rate_band(
        context="bench-idle",
        rate_hz=Band(lo=190, hi=210, unit="Hz"),
    )
    recording = Recording(
        context="bench-idle",
        units={},
        devices={},
        env={},
        sha256="synthetic-b87-empty",
        path=tmp_path,
    )
    findings = _findings(contract, recording)
    absent = [
        f
        for f in findings
        if f.quantity == f"devices/{DEVICE_ID}/rate_hz" and f.severity == "not_scored"
    ]
    assert absent
    assert "no samples" in absent[0].observed


def test_missing_samples_do_not_hide_missing_envelope_disclosure(tmp_path):
    contract = load_contract(UNENVELOPED_CONTRACT)
    recording = Recording(
        context="bench-idle",
        units={},
        devices={},
        env={},
        sha256="synthetic-b114-empty",
        path=tmp_path,
    )
    findings = _findings(contract, recording)
    imu = [f for f in findings if f.quantity.startswith(f"devices/{DEVICE_ID}/")]
    assert (
        len(imu) == 2
        and {f.quantity.rsplit("/", 1)[-1] for f in imu} == {"rate_hz", "recv_gap"}
        and "no samples" in imu[0].observed
        and "no envelope declared" in imu[1].observed
    )


def test_cli_clean_fixture_still_goes():
    """Smoke: clean fixture GOes after B100 id alignment. Planted bags: see B100 tests."""
    result = runner.invoke(
        app,
        ["preflight", str(CLEAN_CONTRACT), "--from", str(CLEAN_BAG), "--no-ledger"],
    )
    assert result.exit_code == 0, result.output
    assert "GO" in result.output
    assert "NO-GO" not in result.output
    flat = " ".join(result.output.split())
    # Presence floor: matched imu must not be disclosed as unseen.
    assert "devices/dev.imu/enumeration" not in flat
    assert "devices/dev.imu/liveness" not in flat


@pytest.mark.parametrize(
    ("kind", "procedure_id", "signal", "code"),
    [
        ("reset", "reset-v1", "reset_code", 3),
        ("watchdog", "watchdog-kick-v1", "watchdog_code", 7),
        ("update", "update-v1", "update_code", 9),
    ],
)
def test_declared_procedure_marker_scores_only_when_full_binding_is_captured(
    tmp_path, kind, procedure_id, signal, code
):
    """Missing or altered procedure evidence is a refusal, never a pass."""
    procedure = DriverProcedure(id=procedure_id, kind=kind, signal=signal, code=code)
    contract = Contract(
        robot=RobotInfo(name="procedure-test"),
        devices=[
            DeviceInstance(
                id=DEVICE_ID,
                model="mdl.imu",
                owner="test",
                driver=Driver(
                    msg_type="std_msgs/msg/UInt8",
                    signals={signal: SignalSource(field="data", unit="1")},
                    procedures=[procedure],
                ),
            )
        ],
    )
    recording = _recording(tmp_path, recv_times=[0.0, 0.2, 0.4])
    binding = (procedure.kind, procedure.signal, procedure.code)
    recording.procedures = {DEVICE_ID: {procedure.id: binding}}
    recording.procedure_events = {DEVICE_ID: {procedure.id: [(0.2, *binding)]}}
    scores = _ScoreBook()
    findings = flow.check(contract, RecordedSource(recording), scores=scores)
    quantity = f"devices/{DEVICE_ID}/procedures/{procedure.id}"
    assert not any(f.quantity == quantity and f.severity == "violation" for f in findings)
    assert any(record.quantity == quantity for record in scores.records)

    # MUTATION-BITE: removing the declared event, or accepting an event whose
    # kind, selected signal, or code differs from the contract, turns the
    # public preflight path NO-GO.
    recording.procedure_events = {}
    missing = _findings(contract, recording)
    assert any(
        f.quantity == quantity and f.severity == "violation" and f"no captured {kind}" in f.observed
        for f in missing
    )
    wrong_kind = "watchdog" if kind == "reset" else "reset"
    for altered_binding in (
        (wrong_kind, procedure.signal, procedure.code),
        (procedure.kind, "other_signal", procedure.code),
        (procedure.kind, procedure.signal, code + 1),
    ):
        recording.procedure_events = {DEVICE_ID: {procedure.id: [(0.2, *binding)]}}
        recording.procedures = {DEVICE_ID: {procedure.id: altered_binding}}
        altered = _findings(contract, recording)
        assert any(
            f.quantity == quantity
            and f.severity == "violation"
            and "identity, kind, signal, and code" in f.observed
            for f in altered
        )
    recording.procedures = {DEVICE_ID: {procedure.id: binding}}
    for altered_binding in (
        (wrong_kind, procedure.signal, procedure.code),
        (procedure.kind, "other_signal", procedure.code),
        (procedure.kind, procedure.signal, code + 1),
    ):
        recording.procedure_events = {DEVICE_ID: {procedure.id: [(0.2, *altered_binding)]}}
        altered = _findings(contract, recording)
        assert any(
            f.quantity == quantity
            and f.severity == "violation"
            and "does not match the declared kind, signal, and code" in f.observed
            for f in altered
        )


def test_declared_dds_deadline_event_is_a_named_violation(tmp_path):
    contract = Contract(
        robot=RobotInfo(name="deadline-test"),
        devices=[
            DeviceInstance(
                id=DEVICE_ID,
                model="mdl.imu",
                owner="test",
                driver=Driver(msg_type="sensor_msgs/msg/Imu", dds_deadline_s=0.1),
            )
        ],
    )
    recording = _recording(tmp_path, recv_times=[0.0, 0.2, 0.4])
    recording.dds_deadlines = {DEVICE_ID: 0.1}
    recording.dds_deadline_misses = {DEVICE_ID: [(0.1, 1)]}
    findings = _findings(contract, recording)
    assert any(
        f.quantity == f"devices/{DEVICE_ID}/dds_requested_deadline"
        and f.severity == "violation"
        and "1 middleware" in f.observed
        for f in findings
    )

    recording.dds_deadline_misses = {}
    assert not any(
        f.quantity == f"devices/{DEVICE_ID}/dds_requested_deadline" and f.severity == "violation"
        for f in _findings(contract, recording)
    )
    recording.dds_deadlines = {}
    assert any(
        f.quantity == f"devices/{DEVICE_ID}/dds_requested_deadline" and f.severity == "not_scored"
        for f in _findings(contract, recording)
    )
    contract.devices[0].driver.dds_deadline_s = None
    assert not any(
        f.quantity == f"devices/{DEVICE_ID}/dds_requested_deadline"
        for f in _findings(contract, recording)
    )


def _five_hz(t0: float, t1: float, period: float = 0.2) -> list[float]:
    return [t0 + i * period for i in range(round((t1 - t0) / period))]


def test_rate_band_scores_windows_and_gaps_tolerate_one_slow_tick(tmp_path):
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=Band(lo=3, hi=8, unit="Hz"))
    burst = _five_hz(0, 3) + [3.0, 3.00005, 3.00010] + _five_hz(3.2, 6)
    slow_tick = _five_hz(0, 3) + _five_hz(3.3, 6)  # one 0.5 s gap: below 2/lo, above 1/lo
    for times in (burst, slow_tick):
        bad = _findings(contract, _recording(tmp_path, recv_times=times))
        assert not [f for f in bad if f.severity == "violation"], bad


def test_sustained_slow_stretch_fails_and_names_its_window(tmp_path):
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=Band(lo=3, hi=8, unit="Hz"))
    times = _five_hz(0, 4) + _five_hz(4, 7, period=0.5) + _five_hz(7, 11)  # mean ~4 Hz
    findings = _findings(contract, _recording(tmp_path, recv_times=times))
    rate = [f for f in findings if f.quantity.endswith("/rate_hz") and f.severity == "violation"]
    assert len(rate) == 1 and rate[0].observed_quantity.value < 3
    assert "recv_time 4.000000 s" in rate[0].observed


def test_long_silence_fails_the_gap_leg(tmp_path):
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=Band(lo=3, hi=8, unit="Hz"))
    times = _five_hz(0, 3) + _five_hz(4.5, 8)
    findings = _findings(contract, _recording(tmp_path, recv_times=times))
    gap = [f for f in findings if f.quantity.endswith("/recv_gap") and f.severity == "violation"]
    assert len(gap) == 1 and "2.800000 s and 4.500000 s" in gap[0].observed


def test_gap_boundary_is_two_missed_periods_and_independent_of_rate(tmp_path):
    contract = _contract_with_rate_band(context="bench-idle", rate_hz=Band(lo=3, hi=8, unit="Hz"))

    def kinds(resume: float) -> set[str]:  # actual gap = resume - 5.8 s; limit 2/3 s
        times = _five_hz(0, 6) + _five_hz(resume, 13)
        fs = _findings(contract, _recording(tmp_path, recv_times=times))
        return {f.quantity.rsplit("/", 1)[-1] for f in fs if f.severity == "violation"}

    assert kinds(6.4) == set()  # 0.6 s gap: neither
    assert kinds(6.6) == {"recv_gap"}  # 0.8 s gap: rate window stays in band
    assert kinds(6.8) == {"recv_gap", "rate_hz"}  # 1.0 s gap: both reported, none dropped
