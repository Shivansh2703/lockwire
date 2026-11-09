"""The command record: ``commands.jsonl`` beside a recording.

One row per observed command — but the outcome (what the feedback sensor read
after the settle time) does not exist at the moment the command arrives, so a
single sealed hash-chained row can never be written whole. So a command is a
**logical record** assembled from **append-only linked physical events**:
``received``, ``forwarded``, ``delivery_failed``, ``completed``. A failed
transport attempt is distinct from an actuator completion. Each event is one hash-chained
JSON-Lines row; a reader reassembles the logical command by following
``command_id`` across its events, wherever they land in the file.

**The physical shape is the plan's stated default** (*"does 'one row' mean one
logical record with linked completion, or one physical line? Default: one logical
record."*). Whether one logical command may legitimately span more than the three
named physical events is an open review question (physical row count vs "one
row") — built here to the three-event minimum, unresolved, flagged for review.

**Pass-through is absolute**: this module is a meter on the wire, never
an actuator. It forwards unchanged, never originates, never alters, never
withholds. Concretely: a command payload is written exactly as handed over; a
payload that cannot be represented as JSON is recorded *as unrepresentable*,
with its text kept, rather than dropped (see ``_payload``); and a command whose
``completed`` event never arrives is still returned by the reader, marked
incomplete, rather than hidden.

**Correlation is by identity, never by nearest timestamp.** Two overlapping
targets on the same device are told apart by ``command_id`` alone. Nearest-
timestamp correlation is the wrong answer the plan rules out.

**Clock model — stated, never assumed.** The ROS capture rebases a monotonic
clock to its first sample (``ros2bag.py`` tags ``source="ros2bag"``); the ULog
path uses payload timestamps directly (``ulog.py``, ``source="ulog"``). Where
neither holds, ``clock`` is ``"unknown"`` and stays that way — a record with an
unknown clock never claims a synchronised correlation. ``clock_for`` is the one
place that mapping lives.

**The chain is tamper-EVIDENT, not tamper-proof.** It is the health ledger's
chain, imported (``chain_row_hash``/``chain_last_hash``/``chain_break`` from
``health.py``) rather than reimplemented — two chains that drift would give two
different answers about whether a record was tampered with. It detects an edit
to any row after the fact and names the line, exactly as ``health --verify``
does. It cannot prove *who* wrote a row, cannot stop someone who rewrites the
whole chain from a clean start, and cannot catch rows truncated from the *end*
(the remaining prefix is a valid chain; that needs an anchor kept elsewhere).
Observed issuer text is distinguished from authenticated identity: a topic name
is a channel, not proof of who sent it — hence ``issuer`` names an observation.

Field list is versioned the ledger's way (``health.py``'s ``HealthRow``): a
sidecar-only pydantic model whose new fields carry defaults, so a reader that
predates a field still parses old rows. No contract-schema fields.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from lockwire import __version__, _filelock
from lockwire_pro.health import (
    CHAIN_ZERO,
    chain_break,
    chain_last_hash,
    chain_row_hash,
    read_ledger_lines,
)
from lockwire_pro.recording import COMMANDS
from lockwire.schema import Base

__all__ = [
    "CHAIN_ZERO",
    "COMMAND_CLASSES",
    "CommandEvent",
    "CommandRecord",
    "append_command_event",
    "chain_break",
    "chain_last_hash",
    "chain_row_hash",
    "clock_for",
    "commands_path_for",
    "read_command_records",
    "read_ledger_lines",
    "verify_commands",
]

EventKind = Literal["received", "forwarded", "delivery_failed", "completed"]
ClockModel = Literal["ros2bag", "ulog", "unknown"]

# The only supported command classes (v8 "What a command means"), each with the
# settle time after which the feedback sensor's read counts as the outcome. A
# class not named here is recorded as-is with no settle rule — never rejected.
COMMAND_CLASSES: dict[str, float] = {
    "joint_target": 0.5,
    "rail_load": 0.2,
    "power_mode": 1.0,
    "publish": 0.0,
}


def commands_path_for(recording_dir: Path) -> Path:
    """``rec/`` -> ``rec/commands.jsonl`` (the sidecar convention, mirroring
    ``health.sidecar_path_for``)."""
    return Path(recording_dir) / COMMANDS


@contextmanager
def _append_lock(path: Path) -> Iterator[None]:
    """Hold the per-record advisory lock across tail read and append."""
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        _filelock.lock(handle.fileno())
        try:
            yield
        finally:
            _filelock.unlock(handle.fileno())


def clock_for(source: str | None) -> ClockModel:
    """The clock model a recording source implies — stated, never guessed.

    ``ros2bag`` rebases a monotonic clock to its first sample; ``ulog`` uses
    payload timestamps directly. Anything else (a hand-written recording, a
    foreign bridge, no source at all) is ``"unknown"``: the record says
    synchronisation is unknown rather than inventing a correlation.
    """
    return source if source in ("ros2bag", "ulog") else "unknown"


class Margin(Base):
    """One margin at the instant the command was scored."""

    value: float
    limit: float
    unit: str
    margin: float


class CommandEvent(Base):
    """One physical row of ``commands.jsonl``: a lifecycle event of a command.

    Shared by all three kinds; a field that only exists for one kind carries a
    default, which is also how the field list is versioned (``HealthRow``'s
    convention) — an older reader parses a newer row.
    """

    event: EventKind
    command_id: str
    t: float
    clock: ClockModel
    device: str
    issuer: str  # observed text: a channel/agent id/person, NOT authenticated
    command: Any = None  # forwarded unchanged, exactly as handed over
    command_text: str | None = None  # set iff the payload is unrepresentable
    representable: bool = True
    command_class: str | None = None
    # `forwarded`: the verdicts and margins at that instant.
    device_verdict: str | None = None
    system_verdict: str | None = None
    decided_by: str | None = None  # the rail/budget/bus/battery/joint that decided it
    margins: dict[str, Margin] = {}
    # `completed`: what the feedback sensor read after the settle time.
    outcome: str | None = None
    settle_s: float | None = None
    # Provenance, on every row.
    contract_sha: str = ""
    witness: str | None = None
    source: str | None = None
    device_mapping: str | None = None
    source_sequence: int | None = None
    source_time: float | None = None
    receipt_time: float | None = None
    receipt_order: int | None = None
    raw_sha256: str | None = None
    raw_hex: str | None = None
    transport_issue: str | None = None
    overall_verdict: str | None = None
    schema_version: int = 1
    tool: str = f"lockwire {__version__}"
    # The hash chain, written by `append_command_event` (imported from health.py).
    prev: str = ""
    hash: str = ""


class CommandRecord(Base):
    """One logical command, assembled from its linked events.

    Every field v8 line 1120 requires is here, on this row. ``complete`` is
    False when the ``completed`` event has not arrived (or never will) — the
    record is still returned, never withheld.
    """

    command_id: str
    issuer: str
    device: str
    command: Any = None
    command_text: str | None = None
    representable: bool = True
    command_class: str | None = None
    clock: ClockModel = "unknown"
    received_at: float | None = None
    forwarded_at: float | None = None
    completed_at: float | None = None
    device_verdict: str | None = None
    system_verdict: str | None = None
    decided_by: str | None = None
    margins: dict[str, Margin] = {}
    outcome: str | None = None
    settle_s: float | None = None
    contract_sha: str = ""
    witness: str | None = None
    source: str | None = None
    device_mapping: str | None = None
    source_sequence: int | None = None
    source_time: float | None = None
    receipt_time: float | None = None
    receipt_order: int | None = None
    raw_sha256: str | None = None
    raw_hex: str | None = None
    transport_issue: str | None = None
    overall_verdict: str | None = None
    schema_version: int = 1
    tool: str = f"lockwire {__version__}"
    complete: bool = False
    events: list[EventKind] = []


def _payload(command: Any) -> tuple[Any, str | None, bool]:
    """``(command, command_text, representable)`` — never drops the command.

    A payload JSON can hold rides through untouched. One it cannot (bytes, an
    object, a NaN) is recorded *as unrepresentable*, keeping its text, because
    a recorder that discards what it cannot parse has altered the record.
    """
    try:
        json.dumps(command, allow_nan=False)
    except (TypeError, ValueError):
        return None, repr(command), False
    return command, None, True


def append_command_event(
    path: Path,
    event: CommandEvent,
) -> CommandEvent:
    """Append one lifecycle event, hash-chained onto whatever is already there.

    The one function every writer goes through, so no unchained row can be
    appended — the same shape as ``health.append_health_rows``. Returns the
    event as written (``prev``/``hash`` filled in).

    The whole verified file is rewritten under the record lock before an
    atomic replacement. This is O(n) in record size, deliberately bounded to
    the command-record core rather than a low-latency relay. Failures before
    ``os.replace`` preserve the old bytes. A directory ``fsync`` failure after
    replacement is reported to the caller because the new row may be visible
    but its durability is no longer knowable.
    """
    path = Path(path)
    command, text, representable = _payload(event.command)
    event = event.model_copy(
        update={"command": command, "command_text": text, "representable": representable}
    )
    payload = event.model_dump(mode="json")
    serialized: str
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock(path):
        if verify_commands(path) is not None:
            raise ValueError("refusing to append to a command record with a broken hash chain")
        prev = chain_last_hash(path)
        payload["prev"] = prev
        payload["hash"] = chain_row_hash(payload, prev)
        serialized = json.dumps(payload, ensure_ascii=False) + "\n"
        original = path.read_bytes() if path.exists() else b""
        existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            if existing_mode is not None:
                os.fchmod(fd, existing_mode)
            handle = os.fdopen(fd, "wb")
            fd = -1  # ``handle`` now owns the descriptor.
            with handle:
                if handle.write(original) != len(original):
                    raise OSError("short write while rewriting command record")
                encoded = serialized.encode("utf-8")
                if handle.write(encoded) != len(encoded):
                    raise OSError("short write while appending command record")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd != -1:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)
    return CommandEvent.model_validate(payload)


def _merge(record: CommandRecord, event: CommandEvent) -> CommandRecord:
    """Fold one event into its logical record. Later events fill fields in;
    they never overwrite one already set, so a replayed event cannot alter the
    record."""
    update: dict[str, Any] = {"events": [*record.events, event.event]}
    if event.event == "received":
        update["received_at"] = event.t
    elif event.event == "forwarded":
        update["forwarded_at"] = event.t
    elif event.event == "delivery_failed":
        # A failed send is not the actuator outcome represented by completed.
        update["overall_verdict"] = "REFUSED"
        update["transport_issue"] = event.transport_issue
    else:
        update["completed_at"] = event.t
        update["complete"] = True
    for name in (
        "issuer",
        "device",
        "command",
        "command_text",
        "command_class",
        "device_verdict",
        "system_verdict",
        "decided_by",
        "outcome",
        "settle_s",
        "contract_sha",
        "witness",
        "source",
        "device_mapping",
        "source_sequence",
        "source_time",
        "receipt_time",
        "receipt_order",
        "raw_sha256",
        "raw_hex",
        "transport_issue",
        "overall_verdict",
    ):
        value = getattr(event, name)
        if value not in (None, "") and getattr(record, name) in (None, ""):
            update[name] = value
    if event.margins and not record.margins:
        update["margins"] = event.margins
    if not event.representable:
        update["representable"] = False
    if record.clock == "unknown" and event.clock != "unknown":
        update["clock"] = event.clock
    if record.clock != "unknown" and event.clock != record.clock:
        # Two events disagreeing about the clock is exactly the case where
        # synchronisation is not known. Say so; never pick one.
        update["clock"] = "unknown"
    return record.model_copy(update=update)


def records_from_events(events: Iterable[CommandEvent]) -> list[CommandRecord]:
    """Assemble logical commands from events, correlated by ``command_id``.

    Events need not be contiguous: two commands interleaved in the file, or a
    ``completed`` a thousand rows after its ``received``, reassemble the same.
    Order of the returned records is first-appearance order.
    """
    records: dict[str, CommandRecord] = {}
    for event in events:
        record = records.get(event.command_id)
        if record is None:
            record = CommandRecord(
                command_id=event.command_id,
                issuer=event.issuer,
                device=event.device,
                clock=event.clock,
            )
        records[event.command_id] = _merge(record, event)
    return list(records.values())


def read_command_records(path: Path) -> list[CommandRecord]:
    """Read ``commands.jsonl`` back into logical commands.

    A line that is not a valid event is skipped for assembly (the chain still
    sees it — use ``verify_commands`` for that), so one corrupt row cannot hide
    the rest of the record.
    """
    events: list[CommandEvent] = []
    for row in read_ledger_lines(Path(path)):
        try:
            events.append(CommandEvent.model_validate(row))
        except ValueError:
            continue
    return records_from_events(events)


def verify_commands(path: Path) -> tuple[int, str] | None:
    """The first row whose chain does not hold, as ``(1-based line, why)``.

    The health ledger's own ``chain_break``, on this file's rows — one chain
    implementation, one answer about tampering. ``None`` means every row holds
    (which does not prove rows were not truncated from the end; see the module
    docstring).
    """
    rows = read_ledger_lines(Path(path))
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return chain_break(rows[:index]) or (index + 1, "row is not a JSON object")
    return chain_break(rows)
