"""Pure, non-network tests for scripts/public_validation.py.

Covers window derivation, cohort selection predicates, and verdict
aggregation. The ``select``/``fetch``/``run`` subcommands themselves hit the
network or the filesystem work directory and are exercised manually, not here.
"""

from __future__ import annotations

from pathlib import Path

from lockwire_pro.ulog import load_ulog
from scripts.public_validation import (
    aggregate_verdict,
    derive,
    healthy,
    usable,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "ulog"
    / "1b8e43d5-04d8-4130-8227-628898f319c5"
    / "baseline.ulg"
)


# ---------------------------------------------------------------------------
# ULog window derivation
# ---------------------------------------------------------------------------


def test_derive_full_window_preserves_header_and_stays_loadable(tmp_path):
    raw = FIXTURE.read_bytes()

    derived = derive(raw, offset_us=0, window_us=100_000_000)

    assert derived[:16] == raw[:16]
    out_path = tmp_path / "full.ulg"
    out_path.write_bytes(derived)
    recording = load_ulog(out_path, context="nominal-op")
    assert recording.devices


def test_derive_zero_length_window_yields_fewer_bytes():
    raw = FIXTURE.read_bytes()

    full = derive(raw, offset_us=0, window_us=100_000_000)
    empty = derive(raw, offset_us=0, window_us=0)

    assert len(empty) < len(full)
    # The header (and any non-data control messages) still come through.
    assert empty[:16] == raw[:16]


# ---------------------------------------------------------------------------
# Cohort selection predicates
# ---------------------------------------------------------------------------


def _row(**overrides):
    row = {
        "sys_hw": "PX4_FMU_V5",
        "vehicle_uuid": "abc123",
        "duration_s": 120,
        "error_labels": [],
        "num_logged_errors": 0,
    }
    row.update(overrides)
    return row


def test_usable_rejects_px4_sitl():
    assert usable(_row(sys_hw="PX4_SITL")) is False


def test_usable_accepts_real_hardware_row():
    assert usable(_row()) is True


def test_healthy_rejects_non_empty_error_labels():
    assert healthy(_row(error_labels=[2])) is False


def test_healthy_rejects_logged_errors():
    assert healthy(_row(num_logged_errors=1)) is False


def test_healthy_and_usable_accept_a_normal_log():
    row = _row()
    assert healthy(row) is True
    assert usable(row) is True


# ---------------------------------------------------------------------------
# Verdict aggregation
# ---------------------------------------------------------------------------


def test_aggregate_verdict_refuses_if_any_window_refused():
    assert aggregate_verdict(["GO", "NO-GO", "GO"]) == "NO-GO"


def test_aggregate_verdict_all_go_is_go():
    assert aggregate_verdict(["GO", "GO"]) == "GO"


def test_aggregate_verdict_only_not_comparable_is_not_a_go():
    assert aggregate_verdict(["not-comparable", "error"]) != "GO"
    assert aggregate_verdict(["not-comparable", "error"]) == "not-comparable"


def test_aggregate_verdict_empty_is_not_a_go():
    assert aggregate_verdict([]) != "GO"
