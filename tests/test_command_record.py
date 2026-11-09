"""The command record (E-B B-9): logical rows, linked events, the shared chain."""

from __future__ import annotations

from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import stat
import threading

import pytest
from typer.testing import CliRunner

from lockwire_pro import command_record, health
from lockwire.cli import app
from lockwire_pro.command_record import (
    CommandEvent,
    Margin,
    append_command_event,
    clock_for,
    commands_path_for,
    read_command_records,
    verify_commands,
)

runner = CliRunner()


def _received(command_id: str, *, t: float, clock: str = "ros2bag", **extra) -> CommandEvent:
    fields = {
        "event": "received",
        "command_id": command_id,
        "t": t,
        "clock": clock,
        "device": "dev.joint_a",
        "issuer": "agent:planner-1 (topic /cmd, observed — not authenticated)",
        "command": {"target_rad": 1.2},
        "command_class": "joint_target",
        "contract_sha": "abc123def456",
        "witness": "witness:bench-cam",
        "source": "ros2bag",
        "device_mapping": "dev.joint_a -> /joint_a/cmd",
    }
    return CommandEvent(**{**fields, **extra})


def _forwarded(command_id: str, *, t: float, clock: str = "ros2bag", **extra) -> CommandEvent:
    fields = {
        "event": "forwarded",
        "command_id": command_id,
        "t": t,
        "clock": clock,
        "device": "dev.joint_a",
        "issuer": "agent:planner-1",
        "device_verdict": "within the joint's declared range",
        "system_verdict": "rail 5v has 1.1 A of headroom",
        "decided_by": "topology/rails/5v",
        "margins": {"topology/rails/5v": Margin(value=2.4, limit=3.5, unit="A", margin=0.31)},
    }
    return CommandEvent(**{**fields, **extra})


def _completed(command_id: str, *, t: float, clock: str = "ros2bag", **extra) -> CommandEvent:
    fields = {
        "event": "completed",
        "command_id": command_id,
        "t": t,
        "clock": clock,
        "device": "dev.joint_a",
        "issuer": "agent:planner-1",
        "outcome": "encoder read 1.19 rad after settle",
        "settle_s": 0.5,
    }
    return CommandEvent(**{**fields, **extra})


def _write_lifecycle(path: Path, command_id: str, *, t0: float = 0.0) -> None:
    append_command_event(path, _received(command_id, t=t0))
    append_command_event(path, _forwarded(command_id, t=t0 + 0.01))
    append_command_event(path, _completed(command_id, t=t0 + 0.51))


def _append_in_process(path: str, source: str, start) -> None:
    start.wait(timeout=5)
    for index in range(6):
        append_command_event(
            Path(path),
            _received(
                f"{source}-{index}",
                t=float(index),
                issuer=f"source:{source}",
            ),
        )


def test_logical_command_assembles_from_three_events(tmp_path: Path) -> None:
    """One logical record from received/forwarded/completed, even when the three
    events are NOT contiguous in the file (another command's events interleaved)."""
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0))
    append_command_event(path, _received("cmd-2", t=0.001))
    append_command_event(path, _forwarded("cmd-1", t=0.01))
    append_command_event(path, _forwarded("cmd-2", t=0.011))
    append_command_event(path, _completed("cmd-2", t=0.511))
    append_command_event(path, _completed("cmd-1", t=0.51))

    records = {record.command_id: record for record in read_command_records(path)}
    assert set(records) == {"cmd-1", "cmd-2"}
    one = records["cmd-1"]
    assert one.events == ["received", "forwarded", "completed"]
    assert one.complete is True
    assert (one.received_at, one.forwarded_at, one.completed_at) == (0.0, 0.01, 0.51)
    assert one.outcome == "encoder read 1.19 rad after settle"
    assert one.system_verdict == "rail 5v has 1.1 A of headroom"


def test_incomplete_command_is_recorded_not_withheld(tmp_path: Path) -> None:
    """A command whose `completed` never arrives is still returned, marked
    incomplete — never dropped (never withhold)."""
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0))
    append_command_event(path, _forwarded("cmd-1", t=0.01))

    (record,) = read_command_records(path)
    assert record.complete is False
    assert record.events == ["received", "forwarded"]
    assert record.outcome is None


def test_duplicate_events_are_retained_in_their_written_order(tmp_path: Path) -> None:
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0))
    append_command_event(path, _received("cmd-1", t=0.1))

    (record,) = read_command_records(path)
    assert record.events == ["received", "received"]
    assert [json.loads(line)["t"] for line in path.read_text().splitlines()] == [0.0, 0.1]
    assert verify_commands(path) is None


def test_unknown_command_class_is_retained_as_observed(tmp_path: Path) -> None:
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0, command_class="future-command"))

    (record,) = read_command_records(path)
    assert record.command_class == "future-command"
    assert verify_commands(path) is None


def test_unrepresentable_command_is_recorded_not_dropped(tmp_path: Path) -> None:
    """A payload that cannot be JSON-represented is recorded AS unrepresentable,
    keeping its text — a recorder that discards it has altered the record."""
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0, command=b"\xff\xfe raw frame"))

    (record,) = read_command_records(path)
    assert record.representable is False
    assert "raw frame" in (record.command_text or "")
    assert verify_commands(path) is None


def test_chain_shares_eb03_mechanism(tmp_path: Path) -> None:
    """The chain is health.py's, imported — not a second implementation.

    Both legs are anchored outside command_record: the function objects must BE
    health.py's, and a written row's hash must equal health.chain_row_hash
    recomputed independently over the row on disk.
    """
    assert command_record.chain_row_hash is health.chain_row_hash
    assert command_record.chain_last_hash is health.chain_last_hash
    assert command_record.chain_break is health.chain_break

    path = commands_path_for(tmp_path)
    _write_lifecycle(path, "cmd-1")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    prev = health.CHAIN_ZERO
    for row in rows:
        assert row["prev"] == prev
        assert row["hash"] == health.chain_row_hash(row, prev)
        prev = row["hash"]


def test_broken_chain_names_the_line(tmp_path: Path) -> None:
    """Tampering, deletion from the middle and reordering are each named by line.

    Truncation from the END is stated as the chain's known ceiling: the
    remaining prefix is a valid chain, so it verifies clean.
    """
    path = commands_path_for(tmp_path)
    _write_lifecycle(path, "cmd-1")
    _write_lifecycle(path, "cmd-2", t0=1.0)
    lines = path.read_text().splitlines()
    assert len(lines) == 6
    assert verify_commands(path) is None

    tampered = json.loads(lines[3])
    tampered["system_verdict"] = "no rail decided this"
    path.write_text("\n".join([*lines[:3], json.dumps(tampered), *lines[4:]]) + "\n")
    assert verify_commands(path) == (4, "hash does not match the row's contents")

    path.write_text("\n".join([*lines[:2], *lines[3:]]) + "\n")  # row 3 deleted
    broken = verify_commands(path)
    assert broken is not None and broken[0] == 3

    reordered = [*lines[:2], lines[3], lines[2], *lines[4:]]
    path.write_text("\n".join(reordered) + "\n")
    broken = verify_commands(path)
    assert broken is not None and broken[0] == 3

    path.write_text("\n".join(lines[:4]) + "\n")  # truncated from the end
    assert verify_commands(path) is None  # the documented ceiling, not a pass


def test_concurrent_processes_keep_every_event_and_one_continuous_chain(tmp_path: Path) -> None:
    path = commands_path_for(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=_append_in_process, args=(str(path), source, start))
        for source in ("left", "right")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive(), "writer process did not clean up"
        assert process.exitcode == 0

    events = [
        CommandEvent.model_validate(json.loads(line)) for line in path.read_text().splitlines()
    ]
    assert len(events) == 12
    assert {event.command_id for event in events} == {
        f"{source}-{index}" for source in ("left", "right") for index in range(6)
    }
    for source in ("left", "right"):
        assert [event.command_id for event in events if event.issuer == f"source:{source}"] == [
            f"{source}-{index}" for index in range(6)
        ]
    assert verify_commands(path) is None


def test_lock_removal_deterministically_loses_an_event_then_restores(
    tmp_path: Path, monkeypatch
) -> None:
    path = commands_path_for(tmp_path)
    original_lock = command_record._append_lock
    original_replace = command_record.os.replace
    barrier = threading.Barrier(2)

    @contextmanager
    def unguarded(_path: Path):
        yield

    def synchronized_replace(source: Path, destination: Path) -> None:
        if destination == path:
            # Both writers have already copied the original bytes into separate
            # temp files. Releasing here makes the final replacement lose one.
            barrier.wait(timeout=5)
        original_replace(source, destination)

    with monkeypatch.context() as mutation:
        mutation.setattr(command_record, "_append_lock", unguarded)
        mutation.setattr(command_record.os, "replace", synchronized_replace)
        errors: list[BaseException] = []

        def append_racing_event(index: int) -> None:
            try:
                append_command_event(path, _received(f"race-{index}", t=index))
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=append_racing_event, args=(index,)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert errors == []

    assert command_record._append_lock is original_lock
    events = [
        CommandEvent.model_validate(json.loads(line)) for line in path.read_text().splitlines()
    ]
    assert {event.command_id for event in events} < {"race-0", "race-1"}
    assert verify_commands(path) is None
    safe = commands_path_for(tmp_path / "safe")
    append_command_event(safe, _received("safe", t=0.0))
    assert verify_commands(safe) is None


def test_failed_serialization_write_or_replace_preserves_the_prior_record(
    tmp_path: Path, monkeypatch
) -> None:
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("first", t=0.0))
    before = path.read_bytes()
    original_dumps = command_record.json.dumps

    def fail_serializer(*args, **kwargs):
        raise OSError("serializer failed")

    monkeypatch.setattr(command_record.json, "dumps", fail_serializer)
    with pytest.raises(OSError, match="serializer failed"):
        append_command_event(path, _received("serializer", t=1.0))
    assert path.read_bytes() == before
    monkeypatch.setattr(command_record.json, "dumps", original_dumps)

    original_fdopen = command_record.os.fdopen

    class PartialWriter:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, data: bytes) -> int:
            self.handle.write(data[:10])
            self.handle.flush()
            raise OSError("write failed")

        def flush(self) -> None:
            self.handle.flush()

        def fileno(self) -> int:
            return self.handle.fileno()

    def fail_write(fd, *args, **kwargs):
        return PartialWriter(original_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(command_record.os, "fdopen", fail_write)
    with pytest.raises(OSError, match="write failed"):
        append_command_event(path, _received("partial", t=2.0))
    assert path.read_bytes() == before
    assert verify_commands(path) is None
    monkeypatch.setattr(command_record.os, "fdopen", original_fdopen)

    def fail_replace(*_args) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(command_record.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        append_command_event(path, _received("replace", t=3.0))
    assert path.read_bytes() == before
    assert verify_commands(path) is None

    new_path = commands_path_for(tmp_path / "new")
    with pytest.raises(OSError, match="replace failed"):
        append_command_event(new_path, _received("new", t=4.0))
    assert not new_path.exists()


def test_post_replace_directory_sync_error_does_not_report_success(
    tmp_path: Path, monkeypatch
) -> None:
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("first", t=0.0))
    path.chmod(0o640)
    original_fsync = command_record.os.fsync
    calls = 0

    def fail_directory_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory sync failed after replacement")
        original_fsync(fd)

    monkeypatch.setattr(command_record.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync failed after replacement"):
        append_command_event(path, _received("second", t=1.0))
    assert verify_commands(path) is None
    assert [event.command_id for event in read_command_records(path)] == ["first", "second"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_unsynced_clock_is_stated_not_guessed(tmp_path: Path) -> None:
    """Where neither the ROS nor the ULog convention holds, the record says the
    clock is unknown — and two events disagreeing about it stays unknown too."""
    assert clock_for("ros2bag") == "ros2bag"
    assert clock_for("ulog") == "ulog"
    assert clock_for(None) == "unknown"
    assert clock_for("some-foreign-bridge") == "unknown"

    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-1", t=0.0, clock="unknown"))
    append_command_event(path, _forwarded("cmd-1", t=0.01, clock="unknown"))
    (record,) = read_command_records(path)
    assert record.clock == "unknown"

    mixed = commands_path_for(tmp_path / "mixed")
    append_command_event(mixed, _received("cmd-2", t=0.0, clock="ros2bag"))
    append_command_event(mixed, _forwarded("cmd-2", t=0.01, clock="ulog"))
    (record,) = read_command_records(mixed)
    assert record.clock == "unknown"


def test_overlapping_commands_correlate_by_identity_not_time(tmp_path: Path) -> None:
    """Two overlapping targets on the SAME device, whose events are nearer in
    time to the other command's than to their own, still reassemble correctly."""
    path = commands_path_for(tmp_path)
    append_command_event(path, _received("cmd-a", t=0.000))
    append_command_event(path, _received("cmd-b", t=0.001))
    # cmd-b completes first, and each completion is nearest in time to the OTHER
    # command's forward — nearest-timestamp correlation would swap the outcomes.
    append_command_event(path, _forwarded("cmd-a", t=0.010))
    append_command_event(path, _forwarded("cmd-b", t=0.011))
    append_command_event(path, _completed("cmd-b", t=0.012, outcome="b: encoder read 0.40 rad"))
    append_command_event(path, _completed("cmd-a", t=0.900, outcome="a: encoder read 1.19 rad"))

    records = {record.command_id: record for record in read_command_records(path)}
    assert records["cmd-a"].outcome == "a: encoder read 1.19 rad"
    assert records["cmd-b"].outcome == "b: encoder read 0.40 rad"
    assert records["cmd-a"].completed_at == 0.900
    assert records["cmd-b"].completed_at == 0.012


REQUIRED_FIELDS = (
    # v8 line 1120, in full.
    "received_at",
    "issuer",
    "device",
    "command",
    "device_verdict",
    "system_verdict",
    "decided_by",
    "margins",
    "outcome",
    # v8 line 1113, B-9's own row.
    "command_id",
    "forwarded_at",
    "contract_sha",
    "witness",
    "source",
    "schema_version",
    "tool",
    "device_mapping",
)


def test_required_fields_present(tmp_path: Path) -> None:
    """Every field v8 lines 1120 and 1113 require is on the assembled record,
    with a value — including the system-level verdict naming what decided it."""
    path = commands_path_for(tmp_path)
    _write_lifecycle(path, "cmd-1")
    (record,) = read_command_records(path)

    missing = [
        name
        for name in REQUIRED_FIELDS
        if not hasattr(record, name) or getattr(record, name) in (None, "", {}, [])
    ]
    assert missing == [], f"required fields missing or empty: {missing}"
    assert record.decided_by == "topology/rails/5v"
    assert record.settle_s == 0.5


def test_commands_jsonl_readable_by_preflight_from(tmp_path: Path) -> None:
    """The READ path: a recording carrying commands.jsonl is still loaded by
    `preflight --from`, and the record reads back from the directory preflight
    read.

    Ceiling, stated: no preflight stage consumes command rows yet — the
    `commands` stage is eb-06. This proves the sidecar rides in the recording
    directory without disturbing the load, and reads back from there.
    """
    contract = Path(__file__).parent / "fixtures" / "contract_clean.yaml"
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "manifest.yaml").write_text(
        "context: nominal-op\nunits:\n  dev.imu:\n    accel_z: m/s^2\n"
    )
    (rec / "samples.jsonl").write_text(
        '{"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}}\n'
        '{"t": 0.01, "dev": "dev.imu", "sig": {"accel_z": -9.80}}\n'
    )
    _write_lifecycle(commands_path_for(rec), "cmd-1")

    result = runner.invoke(app, ["preflight", str(contract), "--from", str(rec), "--no-ledger"])
    assert result.exit_code in (0, 1, 3), result.output  # loaded and scored, not a refused run (2)

    (record,) = read_command_records(commands_path_for(rec))
    assert record.command_id == "cmd-1"
    assert record.complete is True
    assert verify_commands(commands_path_for(rec)) is None


def test_a_non_object_row_is_refused_not_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "commands.jsonl"
    path.write_text('"just a string"\n')
    with pytest.raises(ValueError, match="broken hash chain"):
        append_command_event(path, _received("c1", t=1.0))
    assert path.read_text() == '"just a string"\n'
