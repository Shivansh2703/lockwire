"""lockwire declare / apply / add — the edit primitive."""

from __future__ import annotations
import datetime as dt
import difflib
import hashlib
import json
import multiprocessing
import queue
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
import pytest
from typer.testing import CliRunner
from lockwire import api
from lockwire import edit
from lockwire.cli import app
from lockwire.checks import run_record_checks
from lockwire.edit import (
    Change,
    EditConflict,
    EditRefusal,
    add_entry,
    apply_changes,
    contract_revision,
    parse_value,
    propose_changes,
)
from lockwire.schema import load_contract


runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
LOCKED_AT = dt.date(2026, 9, 6)


def _copy(tmp_path: Path, newline: str = "\n") -> Path:
    dest = tmp_path / "contract.yaml"
    dest.write_bytes(FIXTURE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline.encode()))
    return dest


def _assert_newline_kept(path: Path, newline: str) -> None:
    raw = path.read_bytes()
    assert raw.count(b"\n") == raw.count(newline.encode())
    assert (b"\r" in raw) == (newline == "\r\n")


def _assert_diff_confined(result, before: str, after: str) -> None:
    """The RETURNED diff must be LF-only and exactly as long as the diff of the (newline-translated) texts."""
    ref = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True)))
    assert "\r" not in result.diff
    assert len(result.diff.splitlines()) == len(ref)


NEWLINES = pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _semantic_sha(path: Path) -> str:
    """The parsed contract identity deliberately ignores YAML comments/formatting."""
    return hashlib.sha256(
        json.dumps(load_contract(path).model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def test_propose_changes_validates_without_creating_a_writer_lock(tmp_path):
    path = _copy(tmp_path)
    before = path.read_bytes(), path.stat().st_mtime_ns

    proposal = propose_changes(
        path,
        [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
    )

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not path.with_name(f".{path.name}.lock").exists()
    assert proposal.contract.rail("rail.pm02-5v").capacity.value == 20.0


def test_propose_declared_rail_capacity_enforcement_is_pure(tmp_path):
    path = _copy(tmp_path)
    before = path.read_bytes()

    proposal = propose_changes(
        path,
        [
            Change(
                "topology/rails/rail.pm02-5v/capacity_enforced_by",
                "fuse",
                "spec.pdf",
                "Pat",
            )
        ],
    )

    assert path.read_bytes() == before
    assert proposal.contract.rail("rail.pm02-5v").capacity_enforced_by == "fuse"


def test_propose_power_choices_is_pure_and_rejects_unknown_modes(tmp_path):
    path = _copy(tmp_path)
    before = path.read_bytes()
    choice_path = "devices/dev.companion.jetson-orin-nano/power/choices"

    proposal = propose_changes(path, [Change(choice_path, ["7W"], "spec.pdf", "Pat")])

    assert path.read_bytes() == before
    assert proposal.contract.device("dev.companion.jetson-orin-nano").power.choices == ["7W"]
    with pytest.raises(EditRefusal, match="entry 1"):
        apply_changes(path, [Change(choice_path, ["missing"], "spec.pdf", "Pat")])
    assert path.read_bytes() == before


def _racing_edit(
    path: str,
    revision: str,
    value: str,
    start,
    results,
    worker: int,
    fault: str | None = None,
) -> None:
    """Run one racing writer and report phases back to its owning test."""
    pid = multiprocessing.current_process().pid

    def report(phase: str, **detail: str) -> None:
        results.put({"worker": worker, "pid": pid, "phase": phase, **detail})

    original_lock = edit._contract_lock

    @contextmanager
    def traced_lock(lock_path: Path):
        report("lock_wait", path=str(lock_path))
        with original_lock(lock_path):
            report("lock_held", path=str(lock_path))
            yield

    report("started")
    start.wait()
    try:
        if fault == "crash":
            raise RuntimeError("injected child crash")
        if fault == "timeout":
            report("forced_timeout")
            time.sleep(60)
        edit._contract_lock = traced_lock
        apply_changes(
            Path(path),
            [Change("topology/rails/rail.pm02-5v/capacity", value, "spec.pdf", "Pat")],
            expected_revision=revision,
        )
    except EditConflict:
        report("outcome", value="conflict")
    except BaseException as error:
        report(
            "exception",
            type=type(error).__name__,
            message=str(error),
            traceback=traceback.format_exc(),
        )
    else:
        report("outcome", value="written")
    finally:
        edit._contract_lock = original_lock


def _worker_state(workers) -> list[dict[str, int | bool | None]]:
    return [
        {"pid": worker.pid, "exitcode": worker.exitcode, "alive": worker.is_alive()}
        for worker in workers
        if worker.pid is not None
    ]


def _reap_workers(workers) -> bool:
    """Boundedly reap only the processes this helper started."""
    for worker in workers:
        if worker.pid is not None:
            worker.join(timeout=0.2)
    for worker in workers:
        if worker.pid is not None and worker.is_alive():
            worker.terminate()
    for worker in workers:
        if worker.pid is not None and worker.is_alive():
            worker.join(timeout=0.2)
    for worker in workers:
        if worker.pid is not None and worker.is_alive():
            worker.kill()
    for worker in workers:
        if worker.pid is not None and worker.is_alive():
            worker.join(timeout=0.2)
    return all(worker.pid is None or not worker.is_alive() for worker in workers)


def _run_racing_edits(
    candidates: list[tuple[Path, str]],
    revision: str,
    *,
    faults: tuple[str | None, ...] = (),
    wait_budget: float = 15,
    owned_workers: list | None = None,
    owned_queues: list | None = None,
) -> list[str]:
    """Collect exact child outcomes or report every observable child state."""
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    workers = [
        context.Process(
            target=_racing_edit,
            args=(
                str(path),
                revision,
                value,
                start,
                results,
                index,
                faults[index] if index < len(faults) else None,
            ),
        )
        for index, (path, value) in enumerate(candidates)
    ]
    if owned_workers is not None:
        owned_workers.extend(workers)
    if owned_queues is not None:
        owned_queues.append(results)
    reports: list[dict[str, str | int | None]] = []
    outcomes: dict[int, str] = {}
    # Short fault-control clocks begin only after the injected branch is observed.
    pending_faults = {
        index: {"timeout": "forced_timeout", "crash": "exception"}[fault]
        for index, fault in enumerate(faults[: len(workers)])
        if fault in {"timeout", "crash"}
    }
    deadline_budget = 15 if pending_faults else wait_budget
    deadline_kind = "fault readiness" if pending_faults else "outcome"
    failure: str | None = None
    states_before_reap: list[dict[str, int | bool | None]] = []
    try:
        for worker in workers:
            worker.start()
        start.set()
        deadline = time.monotonic() + deadline_budget
        while len(outcomes) < len(workers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"child {deadline_kind} deadline after {deadline_budget}s"
                break
            try:
                report = results.get(timeout=remaining)
            except queue.Empty:
                failure = f"child {deadline_kind} deadline after {deadline_budget}s"
                break
            reports.append(report)
            if pending_faults.get(int(report["worker"])) == report["phase"]:
                del pending_faults[int(report["worker"])]
                if not pending_faults:
                    deadline_budget, deadline_kind = wait_budget, "outcome"
                    deadline = time.monotonic() + wait_budget
            if report["phase"] == "outcome" and int(report["worker"]) not in outcomes:
                outcomes[int(report["worker"])] = str(report["value"])
                if not pending_faults:
                    deadline = time.monotonic() + wait_budget
            elif report["phase"] == "exception":
                failure = "child exception"
                break
        if failure is None:
            for worker in workers:
                worker.join(timeout=15)
                if worker.exitcode != 0:
                    failure = "child exited non-zero"
                    break
    except BaseException as error:
        failure = f"parent exception {type(error).__name__}: {error}"
    finally:
        states_before_reap = _worker_state(workers)
        reaped = _reap_workers(workers)
        results.cancel_join_thread()
        results.close()
    if failure is not None:
        raise AssertionError(
            f"{failure}; reports={reports!r}; workers_before_reap={states_before_reap!r}; "
            f"workers_after_reap={_worker_state(workers)!r}; reaped={reaped}"
        )
    return sorted(outcomes.values())


@pytest.fixture
def scripted_race(monkeypatch):
    """Drive the actual collector with a fake clock/queue, not real wait time."""
    now, messages = [0.0], []

    def get(*, timeout):
        if messages and messages[0][0] <= now[0] + timeout:
            now[0], report = messages.pop(0)
            return report
        now[0] += timeout
        raise queue.Empty

    results = Mock()
    results.get.side_effect = get
    workers = [Mock(pid=index + 1, exitcode=0) for index in range(2)]
    for worker in workers:
        worker.is_alive.return_value = False
    context = Mock()
    context.Queue.return_value = results
    context.Process.side_effect = workers
    monkeypatch.setattr(multiprocessing, "get_context", lambda _method: context)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    # Only collection is faked here; the genuine crash/timeout controls own cleanup proof.
    monkeypatch.setattr(f"{__name__}._reap_workers", Mock(return_value=True))
    return now, messages


def test_racing_edit_each_next_outcome_gets_original_wait_budget(scripted_race):
    now, messages = scripted_race
    messages.extend(
        [
            (5, {"worker": 0, "phase": "lock_wait"}),
            (14, {"worker": 0, "phase": "outcome", "value": "written"}),
            (20, {"worker": 1, "phase": "lock_held"}),
            (28, {"worker": 1, "phase": "outcome", "value": "conflict"}),
        ]
    )

    assert _run_racing_edits([(Path("unused"), "20 W")] * 2, "unused") == [
        "conflict",
        "written",
    ]
    assert now[0] == 28


def test_racing_edit_phase_messages_do_not_extend_outcome_deadline(scripted_race):
    now, messages = scripted_race
    messages.extend(
        (at, {"worker": 0, "phase": phase})
        for at, phase in [(5, "started"), (10, "lock_wait"), (14, "lock_held")]
    )

    with pytest.raises(AssertionError, match="child outcome deadline after 15s"):
        _run_racing_edits([(Path("unused"), "20 W")] * 2, "unused")
    assert now[0] == 15


def test_racing_edit_duplicate_outcome_does_not_extend_next_deadline(scripted_race):
    now, messages = scripted_race
    messages.extend(
        [
            (5, {"worker": 0, "phase": "outcome", "value": "written"}),
            (14, {"worker": 0, "phase": "outcome", "value": "written"}),
            (21, {"worker": 1, "phase": "outcome", "value": "conflict"}),
        ]
    )

    with pytest.raises(AssertionError, match="child outcome deadline after 15s"):
        _run_racing_edits([(Path("unused"), "20 W")] * 2, "unused")
    assert now[0] == 20


def test_racing_edit_fault_clock_starts_after_observed_injection(scripted_race):
    now, messages = scripted_race
    messages.extend(
        [
            (2, {"worker": 0, "phase": "started"}),
            (5, {"worker": 1, "phase": "outcome", "value": "written"}),
            (12, {"worker": 0, "phase": "forced_timeout"}),
            (12.5, {"worker": 0, "phase": "lock_wait"}),
        ]
    )

    with pytest.raises(AssertionError, match="child outcome deadline after 1s") as raised:
        _run_racing_edits(
            [(Path("unused"), "20 W")] * 2,
            "unused",
            faults=("timeout", None),
            wait_budget=1,
        )
    assert "forced_timeout" in str(raised.value)
    assert now[0] == 13


@pytest.mark.parametrize("fault", ["timeout", "crash"])
def test_racing_edit_progress_does_not_extend_fault_readiness(scripted_race, fault):
    now, messages = scripted_race
    messages.extend(
        [
            (5, {"worker": 0, "phase": "started"}),
            (10, {"worker": 1, "phase": "outcome", "value": "written"}),
            (14, {"worker": 0, "phase": "lock_wait"}),
        ]
    )

    with pytest.raises(AssertionError, match="child fault readiness deadline after 15s"):
        _run_racing_edits(
            [(Path("unused"), "20 W")] * 2,
            "unused",
            faults=(fault, None),
            wait_budget=1,
        )
    assert now[0] == 15


def test_expected_revision_refuses_a_stale_second_edit_without_touching_first_bytes(tmp_path):
    path = _copy(tmp_path)
    revision = contract_revision(path)
    apply_changes(
        path,
        [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
        expected_revision=revision,
    )
    first_bytes = path.read_bytes()
    with pytest.raises(EditConflict, match="reload"):
        apply_changes(
            path,
            [Change("topology/rails/rail.pm02-5v/capacity", "21 W", "spec.pdf", "Pat")],
            expected_revision=revision,
        )
    assert path.read_bytes() == first_bytes


def test_raw_revision_conflicts_after_comment_only_change_with_same_semantic_contract(tmp_path):
    path = _copy(tmp_path)
    revision = contract_revision(path)
    semantic = _semantic_sha(path)
    path.write_bytes(b"# formatting-only change\n" + path.read_bytes())

    assert contract_revision(path) != revision
    assert _semantic_sha(path) == semantic
    unchanged = path.read_bytes()
    with pytest.raises(EditConflict, match="reload"):
        apply_changes(
            path,
            [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
            expected_revision=revision,
        )
    assert path.read_bytes() == unchanged


def test_concurrent_processes_with_one_revision_cannot_both_write(tmp_path):
    path = _copy(tmp_path)
    revision = contract_revision(path)
    outcomes = _run_racing_edits([(path, "20 W"), (path, "21 W")], revision, wait_budget=15)

    assert outcomes == ["conflict", "written"]


def test_symlink_and_target_share_one_writer_lock_and_preserve_the_alias(tmp_path):
    path = _copy(tmp_path)
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(path.name)
    revision = contract_revision(path)
    outcomes = _run_racing_edits([(path, "20 W"), (alias, "21 W")], revision, wait_budget=15)

    assert outcomes == ["conflict", "written"]
    assert alias.is_symlink()
    assert alias.read_bytes() == path.read_bytes()
    assert "spec.pdf" in path.read_text(encoding="utf-8")
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


def test_racing_edit_child_crash_reports_traceback_and_reaps_owned_worker(tmp_path):
    path = _copy(tmp_path)
    workers, queues = [], []
    try:
        with pytest.raises(AssertionError) as raised:
            _run_racing_edits(
                [(path, "20 W"), (path, "21 W")],
                contract_revision(path),
                faults=("crash", None),
                wait_budget=1,
                owned_workers=workers,
                owned_queues=queues,
            )
        actual_states = _worker_state(workers)
    finally:
        _reap_workers(workers)
        for result in queues:
            result.cancel_join_thread()
            result.close()

    message = str(raised.value)
    assert "child exception" in message
    assert "RuntimeError" in message
    assert "injected child crash" in message
    assert "reaped=True" in message
    assert all(
        state["pid"] is not None and state["exitcode"] is not None and not state["alive"]
        for state in actual_states
    )


def test_racing_edit_child_timeout_reports_phases_and_reaps_owned_worker(tmp_path):
    path = _copy(tmp_path)
    workers, queues = [], []
    try:
        with pytest.raises(AssertionError) as raised:
            _run_racing_edits(
                [(path, "20 W"), (path, "21 W")],
                contract_revision(path),
                faults=("timeout", None),
                wait_budget=1,
                owned_workers=workers,
                owned_queues=queues,
            )
        actual_states = _worker_state(workers)
    finally:
        _reap_workers(workers)
        for result in queues:
            result.cancel_join_thread()
            result.close()

    message = str(raised.value)
    assert "child outcome deadline after 1s" in message
    assert "forced_timeout" in message
    assert "reaped=True" in message
    assert all(
        state["pid"] is not None and state["exitcode"] is not None and not state["alive"]
        for state in actual_states
    )


def test_pre_replace_failure_preserves_the_original_bytes(tmp_path, monkeypatch):
    path = _copy(tmp_path)
    before = path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected pre-replace failure")

    monkeypatch.setattr(edit.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected pre-replace failure"):
        apply_changes(
            path,
            [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
            expected_revision=contract_revision(path),
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "writer",
    ("changes", "review", "add_rail", "add_model", "add_battery"),
)
@pytest.mark.parametrize("failed_destination", ("backup", "contract"))
def test_distinct_edit_writers_preserve_original_bytes_when_replace_fails(
    tmp_path, monkeypatch, writer, failed_destination
):
    """Each writer reaches both replace stages without losing contract or undo bytes."""
    path = _copy(tmp_path)
    before = path.read_bytes()
    backup = path.with_name(path.name + ".bak")
    prior_undo = b"# distinct prior undo bytes\n"
    backup.write_bytes(prior_undo)
    real_replace = edit.os.replace
    destinations = []

    def fail_replace(source, target):
        destination = Path(target)
        destinations.append(destination)
        if destination == (backup if failed_destination == "backup" else path):
            raise OSError(f"injected {failed_destination} replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(edit.os, "replace", fail_replace)
    with pytest.raises(OSError, match=f"injected {failed_destination} replace failure"):
        if writer == "changes":
            apply_changes(
                path,
                [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
                expected_revision=contract_revision(path),
            )
        elif writer == "review":
            edit.apply_review(
                path,
                "topology/rails/rail.pm02-5v/capacity",
                "approved",
                witness="Pat",
                expected_revision=contract_revision(path),
            )
        elif writer == "add_rail":
            add_entry(
                path,
                "rail",
                {"id": "rail.pre-replace", "voltage": "5 V", "capacity": "10 W"},
                source="spec.pdf",
                witness="Pat",
                expected_revision=contract_revision(path),
            )
        elif writer == "add_model":
            add_entry(
                path,
                "model",
                {"id": "mdl.new", "family": "fam.camera.stereo"},
                source="spec.pdf",
                witness="Pat",
                expected_revision=contract_revision(path),
            )
        else:
            add_entry(
                path,
                "battery",
                {"capacity": "932.4 Wh", "nominal_voltage": "25.9 V"},
                source="spec.pdf",
                witness="Pat",
                expected_revision=contract_revision(path),
            )

    assert path.read_bytes() == before
    assert backup.read_bytes() == prior_undo
    assert destinations == ([backup] if failed_destination == "backup" else [backup, path, backup])
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_invalid_edit_with_current_revision_preserves_original_bytes(tmp_path):
    path = _copy(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal):
        apply_changes(
            path,
            [Change("not/a/contract/path", "20 W", "spec.pdf", "Pat")],
            expected_revision=contract_revision(path),
        )
    assert path.read_bytes() == before


def _comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("#")]


def _assert_only_data_lines_touched(before: str, after: str) -> None:
    """No comment line is inserted, removed or rewritten by the edit."""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in before.splitlines()[i1:i2] + after.splitlines()[j1:j2]:
            assert not line.strip().startswith("#"), f"comment line touched: {line!r}"


def _changed_lines(before: str, after: str) -> list[str]:
    """Lines a byte-diff actually touches — genuinely differing content only.

    A naive `unified_diff`/opcode count over-reports: a short unchanged line
    (e.g. a bare ``provenance:`` header) sandwiched between two edited lines
    gets swept into the same 'replace' block as both a deletion and an
    insertion even though its text is identical on both sides. Walking a
    'replace' block position-by-position and only counting pairs whose text
    actually differs gives the true confined-edit count.
    """
    b, a = before.splitlines(), after.splitlines()
    matcher = difflib.SequenceMatcher(None, b, a)
    touched: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                old = b[i1 + k] if i1 + k < i2 else None
                new = a[j1 + k] if j1 + k < j2 else None
                if old != new:
                    touched += [line for line in (old, new) if line is not None]
        else:  # delete / insert
            touched += b[i1:i2] + a[j1:j2]
    return touched


# ---------------------------------------------------------------- parse_value


def test_parse_value_bare_number():
    assert parse_value("42", field_name="efficiency") == 42


def test_parse_value_quantity_string():
    assert parse_value("18 W", field_name="capacity") == {"value": 18.0, "unit": "W"}


def test_parse_value_mount_xyz_triple_space_form():
    assert parse_value("0.12 0 0.05 m", field_name="mount_xyz") == [
        {"value": 0.12, "unit": "m"},
        {"value": 0.0, "unit": "m"},
        {"value": 0.05, "unit": "m"},
    ]


def test_parse_value_mount_xyz_triple_bracket_form():
    assert parse_value("[0.12, 0, 0.05] m", field_name="mount_xyz") == parse_value(
        "0.12 0 0.05 m", field_name="mount_xyz"
    )


def test_parse_value_bool_and_string():
    assert parse_value("true", field_name="x") is True
    assert parse_value('"hello world"', field_name="x") == "hello world"


def test_parse_value_unknown_unit_refused():
    with pytest.raises(EditRefusal):
        parse_value("18 zorp", field_name="capacity")


# ---------------------------------------------------------------- declare (one change)


def test_declare_creates_mount_xyz_keeps_comments(tmp_path):
    contract_path = _copy(tmp_path)
    before_text = contract_path.read_text()

    result = apply_changes(
        contract_path,
        [
            Change(
                path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
                value="0.12 0 0.05 m",
                source="hand measurement",
                witness="w1",
                note="mounted at frame center",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written
    after_text = contract_path.read_text()

    assert _comment_lines(before_text) == _comment_lines(after_text)
    _assert_only_data_lines_touched(before_text, after_text)

    contract = load_contract(contract_path)
    device = contract.device("dev.flight-controller.pixhawk-6c")
    assert device.physical.mount_xyz is not None
    assert [q.value for q in device.physical.mount_xyz] == [0.12, 0.0, 0.05]
    # the device already had provenance (for its mass) — kept, not overwritten
    assert device.provenance.witness == "w0"
    assert (
        "docs.holybro.com/autopilot/pixhawk-6c/technical-specification" in device.provenance.source
    )
    assert "hand measurement" in device.provenance.source
    assert device.provenance.locked_at == LOCKED_AT
    assert "mounted at frame center" in device.provenance.note


def test_a_config_supplied_witness_default_is_marked(monkeypatch, tmp_path):
    """An OMITTED --witness (None) falls back to the per-machine config
    value, and the fallback is marked "(default)" — an explicit name is not.

    Named to match this box's accept line (``-k witness_default``) and its
    own mutation bite: stop appending the mark and this test goes red.
    This is the omission case that must not regress.
    """
    from lockwire.edit import resolve_witness

    config = tmp_path / "config.toml"
    config.write_text('witness = "Priya"\n', encoding="utf-8")
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(config))
    assert resolve_witness(None) == "Priya (default)"
    assert resolve_witness("Alice") == "Alice"


def test_resolve_witness_explicit_empty_string_is_refused_not_defaulted(monkeypatch, tmp_path):
    """An OMITTED --witness takes the config
    default (see test_a_config_supplied_witness_default_is_marked),
    but an EXPLICIT ``--witness ""`` is a different act — the caller
    deliberately passed nothing, so it must not silently be attributed a
    default witness. Refused with the existing blank-witness wording,
    before the config default is ever consulted.
    """
    from lockwire.edit import EditRefusal, resolve_witness

    config = tmp_path / "config.toml"
    config.write_text('witness = "Priya"\n', encoding="utf-8")
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(config))
    with pytest.raises(EditRefusal, match=r"--witness must name a real person, not ''"):
        resolve_witness("")
    with pytest.raises(EditRefusal, match=r"--witness must name a real person, not '  '"):
        resolve_witness("  ")
    # omission (None) is unaffected by this refusal and still gets the default.
    assert resolve_witness(None) == "Priya (default)"


def test_resolve_witness_no_config_falls_through_to_blank(monkeypatch, tmp_path):
    """No config file at the (overridden) path: no default, same refusal as today."""
    from lockwire.edit import resolve_witness

    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "does-not-exist.toml"))
    assert resolve_witness(None) == ""


def test_resolve_witness_rejects_you_and_assistant_name_as_default(monkeypatch, tmp_path):
    """A bad config default (blank, 'you', an assistant name) must not bypass
    validate_witness's bar under the "(default)" mark — it falls through to
    the same refusal a missing default gets.
    """
    from lockwire.edit import resolve_witness

    config = tmp_path / "config.toml"
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(config))
    for bad in ("you", "Claude", "  ", ""):
        config.write_text(f'witness = "{bad}"\n', encoding="utf-8")
        assert resolve_witness(None) == ""


_NO_PROVENANCE_FIXTURE = """schema_version: "0.3"
robot:
  name: witnessbot
families:
  - id: fam.plain
    name: Plain
device_models:
  - id: mdl.plain
    family: fam.plain
devices:
  - id: dev.plain.one
    model: mdl.plain
    owner: test
budgets: []
"""


def test_declare_cli_uses_config_default_witness_marked_on_all_surfaces(monkeypatch, tmp_path):
    """A first declare on a device with no prior provenance —
    so ``_stamp_provenance`` takes its fresh-block branch and writes
    ``prov["witness"]`` outright, rather than the existing-block branch that
    keeps an old witness and only appends the new one to the note (see
    ``test_declare_creates_mount_xyz_keeps_comments`` for that other branch,
    unrelated to this box) — comes back marked "(default)" both in the file
    itself and on the per-device reference page (``describe_device`` — the
    other surface that exists in this tree today; ``blame`` echoes the
    identical string once the shared wording lands).
    """
    from lockwire.describe import describe_device

    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_NO_PROVENANCE_FIXTURE, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('witness = "Priya"\n', encoding="utf-8")
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(config))

    result = runner.invoke(
        app,
        [
            "declare",
            str(contract_path),
            "devices/dev.plain.one/physical/mass",
            "20 g",
            "--source",
            "s",
        ],
    )
    assert result.exit_code == 0, result.output

    contract = load_contract(contract_path)
    device = contract.device("dev.plain.one")
    assert device.provenance is not None
    assert device.provenance.witness == "Priya (default)"

    page = describe_device(contract, device)
    assert "Priya (default)" in page


def test_declare_cli_refuses_explicit_empty_witness_even_with_config_default(monkeypatch, tmp_path):
    """End-to-end: ``lockwire declare --witness ""`` refuses, exit 2,
    nothing written — even though a config default exists and an OMITTED
    --witness on the same contract would succeed via it.
    """
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_NO_PROVENANCE_FIXTURE, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('witness = "Priya"\n', encoding="utf-8")
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(config))
    before = _sha(contract_path)

    result = runner.invoke(
        app,
        [
            "declare",
            str(contract_path),
            "devices/dev.plain.one/physical/mass",
            "20 g",
            "--source",
            "s",
            "--witness",
            "",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--witness must name a real person, not ''" in result.output
    assert _sha(contract_path) == before


def test_declare_refuses_without_witness(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="witness"):
        apply_changes(
            contract_path,
            [
                Change(
                    path="topology/rails/rail.pm02-5v/capacity",
                    value="18 W",
                    source="s",
                    witness="",
                )
            ],
        )
    assert _sha(contract_path) == before


def test_witness_refuses_you(tmp_path):
    """'you' is not a witness — it names no one."""
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="witness"):
        apply_changes(
            contract_path,
            [
                Change(
                    path="topology/rails/rail.pm02-5v/capacity",
                    value="18 W",
                    source="test",
                    witness="you",
                )
            ],
        )
    assert _sha(contract_path) == before


def test_witness_refuses_assistant_name(tmp_path):
    """An assistant's own name is not a human witness."""
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="witness"):
        apply_changes(
            contract_path,
            [
                Change(
                    path="topology/rails/rail.pm02-5v/capacity",
                    value="18 W",
                    source="test",
                    witness="Claude",
                )
            ],
        )
    assert _sha(contract_path) == before


def test_add_entry_refuses_you(tmp_path):
    """Add_entry's witness check refuses 'you' the same as declare/apply."""
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="witness"):
        add_entry(
            contract_path,
            "rail",
            {"id": "rail.new-unknown-v", "capacity": "10 W"},
            source="new PDB",
            witness="you",
        )
    assert _sha(contract_path) == before


def test_declare_refuses_value_schema_rejects(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal):
        apply_changes(
            contract_path,
            [
                Change(
                    path="topology/rails/rail.pm02-5v/capacity",
                    value="18 g",  # mass unit on a power field — dimension mismatch
                    source="s",
                    witness="w1",
                )
            ],
        )
    assert _sha(contract_path) == before


def test_declare_dry_run_writes_nothing(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    result = apply_changes(
        contract_path,
        [
            Change(
                path="topology/rails/rail.pm02-5v/capacity",
                value="18 W",
                source="s",
                witness="w1",
            )
        ],
        dry_run=True,
    )
    assert not result.written
    assert result.diff
    assert _sha(contract_path) == before


# ---------------------------------------------------------------- apply (many changes)


def test_apply_atomic_second_entry_bad_names_it(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    changes = [
        Change(
            path="topology/rails/rail.pm02-5v/capacity", value="18 W", source="s1", witness="w1"
        ),
        Change(
            path="topology/rails/rail.servo-motor/capacity",
            value="18 zorp",  # unknown unit — entry 2 is deliberately bad
            source="s2",
            witness="w2",
        ),
        Change(
            path="topology/rails/rail.pixhawk-telem1-5v/capacity",
            value="7.8 W",
            source="s3",
            witness="w3",
        ),
    ]
    with pytest.raises(EditRefusal, match=r"entry 2"):
        apply_changes(contract_path, changes)
    assert _sha(contract_path) == before


def test_apply_dry_run_refuses_a_negative_mass(tmp_path):
    """apply --dry-run must refuse a negative mass, not exit 0."""
    contract_path = _copy(tmp_path)
    before = contract_path.read_bytes()
    changes_file = tmp_path / "changes.yaml"
    changes_file.write_text(
        "- path: devices/dev.camera.oak-d-lite/physical/mass\n"
        "  value: -1 g\n"
        "  source: s1\n"
        "  witness: w1\n"
    )
    result = runner.invoke(app, ["apply", str(contract_path), str(changes_file), "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "entry 1" in result.output
    assert contract_path.read_bytes() == before


def test_apply_three_good_changes_one_atomic_write(tmp_path):
    contract_path = _copy(tmp_path)
    changes = [
        Change(
            path="topology/rails/rail.pm02-5v/capacity", value="18 W", source="s1", witness="w1"
        ),
        Change(
            path="devices/dev.camera.oak-d-lite/power/draw",
            value="4 W",
            source="s2",
            witness="w2",
        ),
        Change(
            path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
            value="0.1 0 0.02 m",
            source="s3",
            witness="w3",
        ),
    ]
    result = apply_changes(contract_path, changes)
    assert result.written
    assert len(result.lines) == 3
    assert result.diff.count("--- ") == 1  # one combined diff, not three

    contract = load_contract(contract_path)
    assert contract.rail("rail.pm02-5v").capacity.value == 18.0
    assert contract.device("dev.camera.oak-d-lite").power.draw.value == 4.0
    assert contract.device("dev.flight-controller.pixhawk-6c").physical.mount_xyz is not None


def test_apply_can_build_a_missing_physical_block_from_three_facts(tmp_path):
    """The camera has no `physical` block at all; apply builds one, atomically."""
    contract_path = _copy(tmp_path)
    contract = load_contract(contract_path)
    assert contract.device("dev.camera.oak-d-lite").physical is None

    changes = [
        Change(
            path="devices/dev.camera.oak-d-lite/physical/mass",
            value="80 g",
            source="s1",
            witness="w1",
        ),
        Change(
            path="devices/dev.camera.oak-d-lite/physical/mount_frame",
            value="x500_frame",
            source="s1",
            witness="w1",
        ),
        Change(
            path="devices/dev.camera.oak-d-lite/physical/mount_xyz",
            value="0.15 0 -0.02 m",
            source="s1",
            witness="w1",
        ),
    ]
    result = apply_changes(contract_path, changes)
    assert result.written
    contract = load_contract(contract_path)
    physical = contract.device("dev.camera.oak-d-lite").physical
    assert physical.mass.value == 80.0
    assert physical.mount_frame == "x500_frame"
    assert physical.mount_xyz is not None


# ---------------------------------------------------------------- add device|rail|bus


def test_add_rail(tmp_path):
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "rail",
        {"id": "rail.new-5v", "voltage": "5 V", "capacity": "10 W"},
        source="new PDB",
        witness="w1",
    )
    assert result.written
    contract = load_contract(contract_path)
    rail = contract.rail("rail.new-5v")
    assert rail is not None
    assert rail.capacity.value == 10.0
    assert rail.provenance.witness == "w1"


def test_add_device_with_existing_rail_reference(tmp_path):
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "device",
        {
            "id": "dev.sensor.new",
            "model": "mdl.camera.oak-d-lite",
            "owner": "perception",
            "power_rail": "rail.pm02-5v",
            "power_draw": "2 W",
        },
        source="new sensor",
        witness="w1",
    )
    assert result.written
    contract = load_contract(contract_path)
    device = contract.device("dev.sensor.new")
    assert device is not None
    assert device.power.rail == "rail.pm02-5v"


def test_add_device_unknown_rail_refused(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="unknown rail"):
        add_entry(
            contract_path,
            "device",
            {
                "id": "dev.sensor.new",
                "model": "mdl.camera.oak-d-lite",
                "owner": "perception",
                "power_rail": "rail.does-not-exist",
                "power_draw": "2 W",
            },
            source="s",
            witness="w1",
        )
    assert _sha(contract_path) == before


def test_add_rail_without_voltage_is_accepted(tmp_path):
    """Voltage is optional on a rail — capacity alone still describes it."""
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "rail",
        {"id": "rail.new-unknown-v", "capacity": "10 W"},
        source="new PDB",
        witness="w1",
    )
    assert result.written  # bites requiring --voltage
    contract = load_contract(contract_path)
    rail = contract.rail("rail.new-unknown-v")
    assert rail is not None
    assert rail.voltage is None
    assert rail.capacity.value == 10.0


def test_add_device_rail_without_draw_is_accepted(tmp_path):
    """A device may declare a rail with no draw — an unknown consumer."""
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "device",
        {
            "id": "dev.sensor.new",
            "model": "mdl.camera.oak-d-lite",
            "owner": "perception",
            "power_rail": "rail.pm02-5v",
        },
        source="new sensor",
        witness="w1",
    )
    assert result.written  # bites requiring --draw
    contract = load_contract(contract_path)
    device = contract.device("dev.sensor.new")
    assert device.power.rail == "rail.pm02-5v"
    assert device.power.draw is None


def test_add_device_power_draw_without_rail_is_refused(tmp_path):
    contract_path = _copy(tmp_path)
    with pytest.raises(EditRefusal, match="needs --rail"):
        add_entry(
            contract_path,
            "device",
            {
                "id": "dev.sensor.new",
                "model": "mdl.camera.oak-d-lite",
                "owner": "perception",
                "power_draw": "2 W",
            },
            source="s",
            witness="w1",
        )


def test_add_device_mass_without_mount_frame_is_accepted(tmp_path):
    """Mass and mount_frame are independently optional."""
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "device",
        {
            "id": "dev.sensor.new",
            "model": "mdl.camera.oak-d-lite",
            "owner": "perception",
            "physical_mass": "25 g",
        },
        source="new sensor",
        witness="w1",
    )
    assert result.written  # bites requiring --mount-frame
    contract = load_contract(contract_path)
    device = contract.device("dev.sensor.new")
    assert device.physical.mass.value == 25.0
    assert device.physical.mount_frame is None


def test_add_refuses_duplicate_id(tmp_path):
    contract_path = _copy(tmp_path)
    with pytest.raises(EditRefusal, match="already exists"):
        add_entry(
            contract_path,
            "rail",
            {"id": "rail.pm02-5v", "voltage": "5 V", "capacity": "10 W"},
            source="s",
            witness="w1",
        )


def test_add_device_unknown_model_refused(tmp_path):
    """Regression: `add device` must refuse an
    undeclared model id, the same posture as an undeclared rail/bus — before
    this fix `add device` accepted any --model string and `check` was the
    only place 16 dangling model references ever surfaced.
    """
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="model 'mdl\\.does-not-exist' is not declared"):
        add_entry(
            contract_path,
            "device",
            {"id": "dev.sensor.new", "model": "mdl.does-not-exist", "owner": "perception"},
            source="s",
            witness="w1",
        )
    assert _sha(contract_path) == before


# ---------------------------------------------------------------- add family|model|budget


def test_add_family(tmp_path):
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "family",
        {"id": "fam.new", "name": "New family"},
        source="s",
        witness="w1",
    )
    assert result.written
    contract = load_contract(contract_path)
    assert contract.family("fam.new").name == "New family"


def test_add_model_refuses_undeclared_family(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="family 'fam\\.does-not-exist' is not declared"):
        add_entry(
            contract_path,
            "model",
            {"id": "mdl.new", "family": "fam.does-not-exist"},
            source="s",
            witness="w1",
        )
    assert _sha(contract_path) == before


def test_add_model_rejects_stale_raw_revision_without_writing(tmp_path):
    path = _copy(tmp_path)
    revision = contract_revision(path)
    path.write_bytes(b"# another edit landed first\n" + path.read_bytes())
    before = path.read_bytes()
    with pytest.raises(EditConflict, match="reload"):
        add_entry(
            path,
            "model",
            {"id": "mdl.new", "family": "fam.camera.stereo"},
            source="spec.pdf",
            witness="Pat",
            expected_revision=revision,
        )
    assert path.read_bytes() == before


def test_add_model(tmp_path):
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "model",
        {
            "id": "mdl.new",
            "family": "fam.flight-controller.autopilot",
            "input_voltage_min": "7 V",
            "input_voltage_max": "51 V",
        },
        source="SPECS.md#power-module",
        witness="w1",
    )
    assert result.written
    contract = load_contract(contract_path)
    model = contract.device_model("mdl.new")
    assert model.family == "fam.flight-controller.autopilot"
    assert model.input_voltage_range.min.value == 7.0
    assert model.provenance.witness == "w1"


def test_add_budget_refuses_undeclared_rail(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="unknown rail"):
        add_entry(
            contract_path,
            "budget",
            {
                "id": "bud.new",
                "budget_kind": "rail_power",
                "rail": "rail.does-not-exist",
                "limit": "10 W",
                "owner": "power-integration",
            },
            source="s",
            witness="w1",
        )
    assert _sha(contract_path) == before


def test_add_budget(tmp_path):
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "budget",
        {
            "id": "bud.new-5v",
            "budget_kind": "rail_power",
            "rail": "rail.pm02-5v",
            "limit": "10 W",
            "owner": "power-integration",
        },
        source="s",
        witness="w1",
    )
    assert result.written
    contract = load_contract(contract_path)
    budget = next(b for b in contract.budgets if b.id == "bud.new-5v")
    assert budget.rail == "rail.pm02-5v"
    assert budget.limit.value == 10.0


def test_one_submit_writes_one_witness_line(tmp_path):
    """`add device` with a new --family and --model creates all
    three records in one write, and the file carries the witness's name
    exactly once — the family and model provenance created *because of* this
    device point at it instead of repeating the witness (parity-anchor law:
    the expected values below are literal, not derived from a second run).
    """
    contract_path = _copy(tmp_path)
    result = api.add(
        contract_path,
        "device",
        id="dev.camera.oakd-lite-2",
        family="fam.camera.depth",
        model="mdl.camera.oak-d-lite-2",
        owner="perception",
        source="datasheets/oakd-lite.pdf",
        witness="Maya Chen",
    )
    assert result["written"]
    text = contract_path.read_text()
    assert text.count("Maya Chen") == 1
    contract = load_contract(contract_path)
    assert contract.family("fam.camera.depth").name == "Camera Depth"
    assert contract.family("fam.camera.depth").provenance.witness != "Maya Chen"
    assert contract.device_model("mdl.camera.oak-d-lite-2").provenance.witness != "Maya Chen"
    device = contract.device("dev.camera.oakd-lite-2")
    assert device.provenance.witness == "Maya Chen"
    assert device.provenance.source == "datasheets/oakd-lite.pdf"
    assert len(result["lines"]) == 3


def test_one_submit_refuses_existing_model_from_different_family_without_writing(tmp_path):
    path = _copy(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match="belongs to family 'fam.camera.stereo'"):
        add_entry(
            path,
            "device",
            {
                "id": "dev.camera.other-family",
                "family": "fam.camera.depth-new",
                "model": "mdl.camera.oak-d-lite",
                "owner": "perception",
            },
            source="datasheets/camera.pdf",
            witness="Maya Chen",
            expected_revision=contract_revision(path),
        )
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".bak").exists()


def test_device_is_never_written_without_its_family(tmp_path):
    """A test's name is a claim: every device this call writes must
    reference a family that exists in the SAME file — never a dangling
    reference to a family the batch writer skipped or wrote out of order."""
    contract_path = _copy(tmp_path)
    add_entry(
        contract_path,
        "device",
        {
            "id": "dev.camera.oakd-lite-3",
            "family": "fam.camera.depth-2",
            "model": "mdl.camera.oak-d-lite-3",
            "owner": "perception",
        },
        source="s",
        witness="w1",
    )
    contract = load_contract(contract_path)
    device = contract.device("dev.camera.oakd-lite-3")
    model = contract.device_model(device.model)
    assert contract.family(model.family) is not None


def test_a_failed_third_record_leaves_the_file_byte_identical(tmp_path):
    """The device id already exists, so the third record (the device) fails
    after the family and model would have been built — the whole submit is
    one transaction: nothing lands, not even the two records that were fine.
    """
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="already exists"):
        add_entry(
            contract_path,
            "device",
            {
                "id": "dev.camera.oak-d-lite",  # already in the x500 fixture
                "family": "fam.camera.depth-3",
                "model": "mdl.camera.oak-d-lite-4",
                "owner": "perception",
            },
            source="s",
            witness="w1",
        )
    assert _sha(contract_path) == before
    contract = load_contract(contract_path)
    assert contract.family("fam.camera.depth-3") is None  # never landed either


def test_an_absolute_source_path_is_refused(tmp_path):
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="cannot travel with the contract"):
        add_entry(
            contract_path,
            "device",
            {
                "id": "dev.camera.oakd-lite-5",
                "family": "fam.camera.depth-4",
                "model": "mdl.camera.oak-d-lite-5",
                "owner": "perception",
            },
            source="/etc/passwd",
            witness="w1",
        )
    assert _sha(contract_path) == before


def test_an_assistant_name_as_witness_is_refused(tmp_path):
    """`validate_witness`'s denylist is a fixed word list (`edit.py:_ASSISTANT_NAME_DENYLIST`),
    not a detector — it does not catch a human name an agent types on its own
    behalf . This proves only the documented gap: the literal
    denylisted word "claude" is refused.
    """
    contract_path = _copy(tmp_path)
    before = _sha(contract_path)
    with pytest.raises(EditRefusal, match="must name a real person"):
        add_entry(
            contract_path,
            "device",
            {
                "id": "dev.camera.oakd-lite-6",
                "family": "fam.camera.depth-5",
                "model": "mdl.camera.oak-d-lite-6",
                "owner": "perception",
            },
            source="d.pdf",
            witness="claude",
        )
    assert _sha(contract_path) == before


def test_apply_creates_budget_block(tmp_path):
    """Regression: `apply` with path == 'budgets' used to refuse
    "cannot stamp provenance at budgets" — a bare collection path has no
    owning block for `declare`'s per-fact provenance model. `apply` now
    treats this shape as create-many, one provenance per row.
    """
    contract_path = _copy(tmp_path)
    change = Change(
        path="budgets",
        value=[
            {
                "id": "bud.pm07-5v",
                "kind": "rail_power",
                "rail": "rail.pm02-5v",
                "limit": {"value": 15.6, "unit": "W"},
                "owner": "power-integration",
            }
        ],
        source="SPECS.md#power-module; 5.2V * 3A = 15.6W",
        witness="audit-engineer",
        locked_at=LOCKED_AT,
    )
    result = apply_changes(contract_path, [change])
    assert result.written
    contract = load_contract(contract_path)
    budget = next(b for b in contract.budgets if b.id == "bud.pm07-5v")
    assert budget.limit.value == 15.6
    assert budget.provenance.witness == "audit-engineer"


# ---------------------------------------------------------------- check after edit


def test_check_after_declared_capacity_fixes_one_budget_not_the_other(tmp_path):
    """The demo's fix path (E1 brief): raise rail.pm02-5v's capacity AND the
    matching budget's limit to 18 W (apply, atomic) — the 17.20 W finding
    clears, the 35.20 W one does not (its own limit is untouched).

    Note: `bud.power.*`'s over-budget finding compares against the Budget's
    own `limit` field, not the rail's `capacity` directly (checks.py:999-1050)
    — the rail's capacity only bounds how high a budget's limit may be set
    (checks.py:1006). Both facts must be declared for a clean pass; declaring
    the rail capacity alone leaves both budgets violating their own limit.
    """
    from lockwire.checks import run_record_checks

    contract_path = _copy(tmp_path)
    before = run_record_checks(load_contract(contract_path))
    before_violations = {f.quantity for f in before if f.severity == "violation"}
    assert "budgets/bud.power.pm02-5v" in before_violations
    assert "budgets/bud.power.pm02-5v.7w-mode" in before_violations

    apply_changes(
        contract_path,
        [
            Change(
                path="topology/rails/rail.pm02-5v/capacity",
                value="18 W",
                source="remeasured PM02 output",
                witness="w2",
            ),
            Change(
                path="budgets/bud.power.pm02-5v.7w-mode/limit",
                value="18 W",
                source="remeasured PM02 output",
                witness="w2",
            ),
        ],
    )
    after = run_record_checks(load_contract(contract_path))
    after_findings = {f.quantity: f.severity for f in after}
    assert after_findings.get("budgets/bud.power.pm02-5v") == "violation"  # 35.20 W still over
    assert "budgets/bud.power.pm02-5v.7w-mode" not in after_findings  # 17.20 W now clears
    assert "budgets/bud.power.pm02-5v.7w-mode/limit" not in after_findings


def test_apply_accepts_scene_import_list_plus_unit_entry(tmp_path):
    """Exact shape `lockwire scene import` writes (E2_scene_import.md line 11-23).

    The X500 fixture's jetson-orin-nano carries no `physical` block at all and
    its mass is published as UNKNOWN. Since `Physical.mass` optional,
    scene import's mount_xyz + mount_frame entries (it never emits a mass fact)
    are enough on their own to reach a contract that validates — the device's
    mass stays undeclared and not_scored, exactly as UNKNOWN demands.
    """
    contract_path = _copy(tmp_path)
    before_text = contract_path.read_text()
    changes = [
        Change(
            path="devices/dev.companion.jetson-orin-nano/physical/mount_xyz",
            value=[0.0, 0.096, 0.13],
            unit="m",
            source="x500.glb node dev.companion.jetson-orin-nano; confirmed by shivansh",
            witness="human",
            note="position read from the model on 2026-09-06 and confirmed by shivansh",
        ),
        Change(
            path="devices/dev.companion.jetson-orin-nano/physical/mount_frame",
            value="x500_frame",
            source="x500.glb node dev.companion.jetson-orin-nano; confirmed by shivansh",
            witness="human",
        ),
    ]
    result = apply_changes(contract_path, changes)
    assert result.written
    after_text = contract_path.read_text()
    assert _comment_lines(before_text) == _comment_lines(after_text)

    contract = load_contract(contract_path)
    physical = contract.device("dev.companion.jetson-orin-nano").physical
    assert physical.mount_frame == "x500_frame"
    assert physical.mass is None  # bites Physical.mass becoming required again
    assert [q.value for q in physical.mount_xyz] == [0.0, 0.096, 0.13]
    assert {q.unit for q in physical.mount_xyz} == {"m"}


# ---------------------------------------------------------------- byte diff confinement
# Review bounce: a plain `YAML()` round-trip keeps ruamel's defaults (no
# preserve_quotes, 2-space sequence indent, 80-col rewrap) — every write
# reformatted the whole file even though comments survived. edit.py now owns
# its own writer settings; these tests pin the confined-diff guarantee down.


@NEWLINES
def test_declare_diff_confined_to_edit_and_provenance(tmp_path, newline):
    """Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS."""
    contract_path = _copy(tmp_path, newline)
    before_text = contract_path.read_text()
    result = apply_changes(
        contract_path,
        [
            Change(
                path="topology/rails/rail.pm02-5v/capacity",
                value="18 W",
                source="s",
                witness="w1",
                locked_at=LOCKED_AT,
            )
        ],
    )
    after_text = contract_path.read_text()
    touched = _changed_lines(before_text, after_text)
    assert len(touched) <= 8, touched
    for line in touched:
        assert any(key in line for key in ("capacity", "witness", "source", "locked_at", "note")), (
            f"line outside rail.pm02-5v's block/provenance: {line!r}"
        )
    # style survives everywhere else: quoting, indent and line width untouched
    assert before_text.count('"') == after_text.count('"')
    assert before_text.count("    - id: rail.pixhawk-telem1-5v") == after_text.count(
        "    - id: rail.pixhawk-telem1-5v"
    )
    long_note_line = next(
        line for line in before_text.splitlines() if "search-engine-paraphrase" in line
    )
    assert long_note_line in after_text.splitlines()  # never re-wrapped
    _assert_diff_confined(result, before_text, after_text)
    _assert_newline_kept(contract_path, newline)


@NEWLINES
def test_apply_diff_confined_for_three_changes(tmp_path, newline):
    """Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS."""
    contract_path = _copy(tmp_path, newline)
    before_text = contract_path.read_text()
    changes = [
        Change(
            path="topology/rails/rail.pm02-5v/capacity",
            value="18 W",
            source="s1",
            witness="w1",
            locked_at=LOCKED_AT,
        ),
        Change(
            path="devices/dev.camera.oak-d-lite/power/draw",
            value="4 W",
            source="s2",
            witness="w2",
            locked_at=LOCKED_AT,
        ),
        Change(
            path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
            value="0.1 0 0.02 m",
            source="s3",
            witness="w3",
            locked_at=LOCKED_AT,
        ),
    ]
    result = apply_changes(contract_path, changes)
    after_text = contract_path.read_text()
    touched = _changed_lines(before_text, after_text)
    assert len(touched) <= 30, touched
    allowed = ("capacity", "draw", "mount_xyz", "value:", "witness", "source", "locked_at", "note")
    for line in touched:
        assert any(key in line for key in allowed), f"unexpected line touched: {line!r}"
    _assert_diff_confined(result, before_text, after_text)
    _assert_newline_kept(contract_path, newline)


@NEWLINES
def test_add_rail_diff_confined(tmp_path, newline):
    """Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS."""
    contract_path = _copy(tmp_path, newline)
    before_text = contract_path.read_text()
    add_result = add_entry(
        contract_path,
        "rail",
        {"id": "rail.new-5v", "voltage": "5 V", "capacity": "10 W"},
        source="s",
        witness="w1",
        locked_at=LOCKED_AT,
    )
    after_text = contract_path.read_text()
    touched = _changed_lines(before_text, after_text)
    assert len(touched) <= 8, touched
    for line in touched:
        assert any(
            key in line
            for key in (
                "rail.new-5v",
                "voltage",
                "capacity",
                "provenance",
                "witness",
                "source",
                "locked_at",
            )
        ), f"line outside the new rail block: {line!r}"
    _assert_diff_confined(add_result, before_text, after_text)
    _assert_newline_kept(contract_path, newline)


@NEWLINES
def test_declare_appends_provenance_never_overwrites_another_facts_source(tmp_path, newline):
    """Integration defect: declaring the radio's mount point replaced its
    entire device provenance, erasing the mass/datasheet source it also
    covers. One provenance block per device is shared across facts (
    provenance-per-fact is a schema change) — until
    then, declare/apply must APPEND to an existing block, never overwrite it.

    Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS.
    """
    contract_path = _copy(tmp_path, newline)
    before_text = contract_path.read_text()
    result = apply_changes(
        contract_path,
        [
            Change(
                path="devices/dev.radio.rfd900x/physical/mount_xyz",
                value="0.0 -0.12 0.05 m",
                source="bench tape measure",
                witness="human",
                locked_at=LOCKED_AT,
            )
        ],
    )
    after_text = contract_path.read_text()

    contract = load_contract(contract_path)
    device = contract.device("dev.radio.rfd900x")
    assert device.physical.mount_xyz is not None
    # the OLD facts' provenance survives: original witness and source both present
    assert device.provenance.witness == "w0"
    assert (
        "https://store.rfdesign.com.au/rfd-900x-modem/; docs.holybro.com/autopilot/pixhawk-6c/technical-specification"
        in device.provenance.source
    )
    # the new fact is appended, not silently dropped
    assert "bench tape measure" in device.provenance.source
    assert "mount_xyz" in device.provenance.source
    assert "human" in device.provenance.note

    touched = _changed_lines(before_text, after_text)
    assert len(touched) <= 8, touched
    _assert_diff_confined(result, before_text, after_text)
    _assert_newline_kept(contract_path, newline)


# ---------------------------------------------------------------- G3: writer keeps file style


def test_add_family_confines_diff_on_init_written_file(tmp_path, testbot_urdf):
    """Audit evidence 015: `init` dumps 2/0-style lists (schema.py's default),
    while the editor used to hardcode 4/2 — the first `add` after `init`
    reflowed the whole file. The editor now sniffs the file's own style and
    `init`'s own dump was aligned to the editor's no-list fallback, so either
    way the diff stays confined to the new block.
    """
    contract_path = tmp_path / "contract.yaml"
    result = runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    before = contract_path.read_text()

    add_result = add_entry(
        contract_path,
        "family",
        {"id": "fam.new", "name": "New family"},
        source="s",
        witness="w1",
        locked_at=LOCKED_AT,
    )
    assert add_result.written
    after = contract_path.read_text()

    touched = _changed_lines(before, after)
    # the new block is 6 lines (id, name, provenance{witness,source,locked_at})
    # plus the empty `families: []` -> block-list flip.
    assert len(touched) <= 8, touched


def test_add_family_confines_diff_on_init_anymal_d(tmp_path):
    """The real case a review bounce caught: comparing a reconstructed
    float's PARSED VALUE to the original (a first pass of this fix) misses
    ULP drift, since a drifted string still parses back to the same float64
    — only comparing TEXT catches it. `anymal_d.urdf` init's to a contract
    with real (not hand-picked) high-precision inertia floats; `add family`
    must touch only the new block, not those floats.
    """
    urdf = Path(__file__).parent / "fixtures" / "anymal_d.urdf"
    contract_path = tmp_path / "contract.yaml"
    result = runner.invoke(app, ["init", str(urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    before = contract_path.read_text()

    add_result = add_entry(
        contract_path,
        "family",
        {"id": "fam.new", "name": "New family"},
        source="s",
        witness="w1",
        locked_at=LOCKED_AT,
    )
    assert add_result.written
    after = contract_path.read_text()

    touched = _changed_lines(before, after)
    # the new block: `families: []` -> `families:` + 6 new lines.
    assert len(touched) <= 8, touched


@NEWLINES
def test_add_family_confines_diff_on_x500_fixture(tmp_path, newline):
    """Same guarantee on a hand-written, already-4/2 fixture (no regression). Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS."""
    contract_path = _copy(tmp_path, newline)
    before = contract_path.read_text()

    add_result = add_entry(
        contract_path,
        "family",
        {"id": "fam.new", "name": "New family"},
        source="s",
        witness="w1",
        locked_at=LOCKED_AT,
    )
    assert add_result.written
    after = contract_path.read_text()

    touched = _changed_lines(before, after)
    assert len(touched) <= 7, touched
    _assert_diff_confined(add_result, before, after)
    _assert_newline_kept(contract_path, newline)


_HANDWRITTEN_2_0_STYLE = """schema_version: "0.3"
robot:
  name: handbot
families:
- id: fam.a
  name: A
device_models: []
devices: []
budgets: []
"""


@NEWLINES
def test_add_family_keeps_a_hand_written_files_own_2_0_style(tmp_path, newline):
    """A file whose lists are NOT `init`'s/the editor's 4/2 default (e.g.
    hand-written 2/0 style, `families:\\n- id:`) keeps its own style — the
    sniff reads it from the existing `fam.a` entry, it isn't forced to 4/2.

    Fails on Windows, where the CRLF fixture was read as LF; runs LF and CRLF on every OS.
    """
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(_HANDWRITTEN_2_0_STYLE.replace("\n", newline).encode())

    before = contract_path.read_text()
    add_result = add_entry(
        contract_path,
        "family",
        {"id": "fam.new", "name": "New family"},
        source="s",
        witness="w1",
        locked_at=LOCKED_AT,
    )
    assert add_result.written
    after = contract_path.read_text()
    assert "- id: fam.a\n  name: A\n" in after  # untouched entry: still 2/0, unreindented
    assert "- id: fam.new\n  name: New family\n" in after  # new entry: same style
    _assert_diff_confined(add_result, before, after)
    _assert_newline_kept(contract_path, newline)


_FLOAT_FIXTURE = """schema_version: "0.3"
robot:
  name: floatbot
topology:
  rails:
    - id: r.main
      capacity: {value: 1.0e-05, unit: W}
      efficiency: 0.10
      voltage: {value: 2.5e-3, unit: V}
      provenance:
        witness: w0
        source: s0
        locked_at: 2026-01-01
families:
  - id: fam.precise
    name: Precise
    min_capture_window_s: 4.227609635471957e-06
device_models: []
devices: []
budgets: []
"""


def test_declare_preserves_untouched_float_scalars(tmp_path):
    """Audit evidence 016: ruamel's `ScalarFloat` round-trip reconstructs a
    scientific-notation scalar from stored width/precision metadata rather
    than keeping its text, and that reconstruction can drift the last digit
    by one ULP (`...957e-06` -> `...956e-06`) even when the field was never
    touched. An unrelated declare must not move it, nor a plain trailing-zero
    float (`0.10`) or short exponent (`1.0e-05`).
    """
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_FLOAT_FIXTURE)

    result = apply_changes(
        contract_path,
        [
            Change(
                path="families/fam.precise/notes",
                value="unrelated",
                source="s",
                witness="w1",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written
    after = contract_path.read_text()
    assert "1.0e-05" in after
    assert "0.10" in after
    assert "2.5e-3" in after
    assert "4.227609635471957e-06" in after


def test_add_battery_creates_block_with_comment_provenance(tmp_path):
    """No add-battery command existed (audit finding 7/evidence 065,082):
    `lockwire add battery` now creates the singleton `topology/battery` block.
    `Battery` has no `provenance` field and forbids extras, so source/witness
    land as a YAML comment instead — best-effort, not machine-readable.
    """
    contract_path = _copy(tmp_path)
    result = add_entry(
        contract_path,
        "battery",
        {"capacity": "932.4 Wh", "nominal_voltage": "25.9 V", "chemistry": "Li-ion"},
        source="SPECS.md PDF p7",
        witness="human",
        locked_at=LOCKED_AT,
    )
    assert result.written
    contract = load_contract(contract_path)
    assert contract.topology.battery.capacity.value == 932.4
    assert contract.topology.battery.voltage.value == 25.9

    text = contract_path.read_text()
    assert "SPECS.md PDF p7" in text
    assert "human" in text
    assert "Li-ion" in text


def test_declare_topology_battery_capacity_records_comment_provenance(tmp_path):
    """Bite: before this fix, `_stamp_provenance` tried to write a
    `provenance:` key into `Battery`, which `load_contract` then refused
    (extra fields forbidden) — `declare topology/battery/capacity` never
    worked. It now records the same comment-based provenance `add battery`
    does.
    """
    contract_path = _copy(tmp_path)
    result = apply_changes(
        contract_path,
        [
            Change(
                path="topology/battery/capacity",
                value="932.4 Wh",
                source="SPECS.md PDF p7",
                witness="human",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written
    contract = load_contract(contract_path)
    assert contract.topology.battery.capacity.value == 932.4

    text = contract_path.read_text()
    assert "SPECS.md PDF p7" in text
    assert "human" in text


def test_declare_model_link_mass_records_comment_provenance(tmp_path):
    """Bite: `Link` (schema.py:270) has no `provenance` field either,
    and the schema forbids extras -- same situation the battery was
    already ruled on. Before this fix, `declare model/links/x500_frame/mass`
    hit `EditRefusal: cannot stamp provenance at model/links` (measured on this
    exact row); it now records a comment stamp the same way `add battery` and
    `declare topology/battery/capacity` do.
    """
    contract_path = _copy(tmp_path)
    result = apply_changes(
        contract_path,
        [
            Change(
                path="model/links/x500_frame/mass",
                value="615 g",
                source="hand re-weigh",
                witness="human",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written
    contract = load_contract(contract_path)
    frame = next(link for link in contract.model.links if link.name == "x500_frame")
    assert frame.mass.value == 615

    text = contract_path.read_text()
    assert "hand re-weigh" in text
    assert "human" in text


# ---------------------------------------------------------------- non-git backup


def test_edit_backup_outside_git_repo(tmp_path):
    """The non-git half: no `.git` anywhere above the contract, so the
    pre-edit bytes must survive in a sibling `.bak` — git isn't there to
    fall back on.
    """
    contract_path = _copy(tmp_path)
    before_text = contract_path.read_text()

    result = apply_changes(
        contract_path,
        [
            Change(
                path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
                value="0.12 0 0.05 m",
                source="hand measurement",
                witness="w1",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written

    backup_path = contract_path.with_name(contract_path.name + ".bak")
    assert backup_path.read_text() == before_text


def test_edit_no_backup_inside_git_repo(tmp_path):
    """Inside a repo, git already is the undo — no `.bak` written."""
    (tmp_path / ".git").mkdir()
    contract_path = _copy(tmp_path)

    result = apply_changes(
        contract_path,
        [
            Change(
                path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
                value="0.12 0 0.05 m",
                source="hand measurement",
                witness="w1",
                locked_at=LOCKED_AT,
            )
        ],
    )
    assert result.written

    backup_path = contract_path.with_name(contract_path.name + ".bak")
    assert not backup_path.exists()


# ------------------------------------------------------- the standing mark

_MARK_CONTRACT = (
    'schema_version: "0.3"\n'
    "robot:\n"
    "  name: markbot\n"
    "families:\n"
    "  - id: fam.compute\n"
    "    name: Companion computer\n"
    "device_models:\n"
    "  - id: mdl.compute.generic\n"
    "    family: fam.compute\n"
    "topology:\n"
    "  rails:\n"
    "    - id: rail.5v\n"
    "      voltage: {{value: 5, unit: V}}\n"
    "      capacity: {{value: 60, unit: W}}\n"
    "devices:\n"
    "  - id: dev.jetson\n"
    "    model: mdl.compute.generic\n"
    "    owner: shivansh\n"
    "    power:\n"
    "      rail: rail.5v\n"
    "      draw: {{value: {draw}, unit: W}}\n"
    "budgets:\n"
    "  - id: bud.power.5v\n"
    "    kind: rail_power\n"
    "    rail: rail.5v\n"
    "    limit: {{value: {limit}, unit: W}}\n"
    "    owner: shivansh\n"
    "    provenance:\n"
    "      witness: human\n"
    "      source: datasheet\n"
    "      locked_at: 2026-09-06\n"
)

MARK_PREFIX = "cleared by re-declaring the limit on "


def _mark_contract(tmp_path: Path, *, draw: float, limit: float) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "contract.yaml"
    path.write_text(_MARK_CONTRACT.format(draw=draw, limit=limit))
    return path


def _declare(path: Path, field: str, value: str) -> None:
    apply_changes(
        path,
        [Change(path=field, value=value, source="test", witness="human", locked_at=LOCKED_AT)],
    )


def test_raising_a_budget_limit_past_a_violation_writes_the_standing_mark_in_the_file(
    tmp_path: Path,
) -> None:
    """A declare that clears a violation by moving the limit rather
    than the draw leaves a standing line in the contract, in the existing
    ``provenance.note`` field (no new schema field)."""
    path = _mark_contract(tmp_path, draw=25, limit=20)
    assert any(
        finding.severity == "violation"
        for finding in run_record_checks(load_contract(path))
        if finding.quantity.startswith("budgets/bud.power.5v")
    ), "fixture must start in violation, or the mark has nothing to be about"

    _declare(path, "budgets/bud.power.5v/limit", "28 W")

    note = load_contract(path).budgets[0].provenance.note
    assert f"{MARK_PREFIX}{LOCKED_AT.isoformat()}, witness human" in note
    assert not any(
        finding.severity == "violation"
        for finding in run_record_checks(load_contract(path))
        if finding.quantity.startswith("budgets/bud.power.5v")
    )


def test_an_edit_that_clears_no_violation_by_raising_a_limit_writes_no_mark(
    tmp_path: Path,
) -> None:
    """The mark names the goalposts moving, not any edit: a raise over a budget
    that was already passing, and a violation cleared by shedding real draw,
    both leave the note unmarked."""
    passing = _mark_contract(tmp_path / "a", draw=12, limit=20)
    _declare(passing, "budgets/bud.power.5v/limit", "25 W")
    assert MARK_PREFIX not in (load_contract(passing).budgets[0].provenance.note or "")

    shed = _mark_contract(tmp_path / "b", draw=25, limit=20)
    _declare(shed, "devices/dev.jetson/power/draw", "12 W")
    assert MARK_PREFIX not in (load_contract(shed).budgets[0].provenance.note or "")


# ------------------------------------------------------ pose edits: move/rotate/reparent

ARMTREE = Path(__file__).parent / "fixtures" / "armtree.yaml"
POSE_EXPECTED = Path(__file__).parent / "fixtures" / "anchors" / "armtree_pose_edits.expected.yaml"
POSE_DATE = dt.date(2026, 9, 8)


def _armtree(tmp_path: Path) -> Path:
    dest = tmp_path / "lockwire.yaml"
    dest.write_bytes(ARMTREE.read_bytes())
    return dest


def _expected_fragments() -> dict[str, str]:
    """The checked-in expected provenance blocks, keyed by their `### ` marker."""
    blocks: dict[str, str] = {}
    key = None
    for line in POSE_EXPECTED.read_text().splitlines():
        if line.startswith("### "):
            key = line[4:].strip()
            blocks[key] = ""
        elif key is not None:
            blocks[key] += line + "\n"
    return {k: v.rstrip("\n") for k, v in blocks.items()}


def _fragment_diff(fragment: str, written: str, label: str) -> str:
    """A readable diff when a fragment is missing, not a bare `assert x in y`.

    LEGIBILITY law: `assert <300 lines> in <400 lines>` fails loudly and teaches
    nothing, and the cheapest way out of an unreadable failure is to re-pin the
    expected value — which is how a wrong expectation gets blessed. The diff is
    against the closest run of lines in the written file, so what actually
    changed is on the screen.
    """
    want = fragment.splitlines()
    have = written.splitlines()
    best, best_score = [], -1.0
    for start in range(max(len(have) - len(want) + 1, 1)):
        window = have[start : start + len(want)]
        score = difflib.SequenceMatcher(None, want, window).ratio()
        if score > best_score:
            best, best_score = window, score
    return "\n".join(
        [f"{label}: the written provenance does not match the checked-in expectation"]
        + list(difflib.unified_diff(want, best, "expected", "written", lineterm=""))
    )


def test_every_pose_edit_stamps_its_own_witness(tmp_path):
    """All THREE pose edits — move, rotate and re-parent — stamp source and
    witness through the one shared stamper, and each is its own submit.

    The name says *every*, so the test exercises all three and the bite mutates
    the SHARED path (`edit._stamp_provenance`), not one operation's call to it.

    Anchored leg (PARITY-ANCHOR law): the assertion is against
    `armtree_pose_edits.expected.yaml`, bytes nothing under test computes.
    Comparing the library door to the CLI door would compare two identical
    wrongs — a stamper that silently stopped stamping would keep them agreeing.
    """
    contract = _armtree(tmp_path)
    from lockwire.edit import move, reparent, rotate

    move(
        contract,
        "dev.gripper",
        (0.010, 0.0, 0.025),
        unit="m",
        source="notes/layout.md",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )
    rotate(
        contract,
        "wrist",
        (0.0, 0.0, 1.5708),
        unit="rad",
        source="notes/layout.md",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )
    reparent(
        contract,
        "elbow",
        "base",
        source="notes/layout.md",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )

    written = contract.read_text()
    for label, fragment in _expected_fragments().items():
        assert fragment in written, _fragment_diff(fragment, written, label)
    # Three submits, so the note carries a line per declared fact — not one
    # stamp covering all three, which is what a shared-stamper bug looks like.
    assert written.count("witness Maya Chen") == 4, written


def test_a_reparent_that_closes_a_cycle_is_refused(tmp_path):
    """`shoulder` onto `wrist`, where the drawn tree already has
    base -> shoulder -> elbow -> wrist. The refusal NAMES the cycle and the
    chain, rather than failing generically: a cycle and an orphan are different
    mistakes and the message has to say which one this is.

    Why this one is decisive: a cycle that lands in the file makes
    `checks._fixed_frame_transforms` return no transform for the whole subtree
    (its `frame in resolving` guard), so the CG composition goes quiet instead
    of wrong, and nothing else in the repo would catch it.
    """
    contract = _armtree(tmp_path)
    from lockwire.edit import reparent

    with pytest.raises(EditRefusal) as error:
        reparent(
            contract,
            "shoulder",
            "wrist",
            source="notes/layout.md",
            witness="Maya Chen",
            locked_at=POSE_DATE,
        )
    message = str(error.value)
    assert "CYCLE" in message, message
    assert "shoulder -> elbow -> wrist" in message, message
    assert "ORPHAN" not in message, message

    # A link cannot be its own parent either, and that says CYCLE too.
    with pytest.raises(EditRefusal) as error:
        reparent(
            contract,
            "shoulder",
            "shoulder",
            source="notes/layout.md",
            witness="Maya Chen",
            locked_at=POSE_DATE,
        )
    assert "CYCLE" in str(error.value), error.value


def test_a_reparent_that_orphans_a_subtree_is_refused(tmp_path):
    """`shoulder` onto `detached_cart`, a link the drawn tree connects to
    nothing. Moving shoulder under it would leave shoulder, elbow and wrist
    hanging off no root — the refusal names ORPHAN and names the root the
    subtree currently reaches, so it cannot be confused with the cycle refusal.
    """
    contract = _armtree(tmp_path)
    from lockwire.edit import reparent

    with pytest.raises(EditRefusal) as error:
        reparent(
            contract,
            "shoulder",
            "detached_cart",
            source="notes/layout.md",
            witness="Maya Chen",
            locked_at=POSE_DATE,
        )
    message = str(error.value)
    assert "ORPHAN" in message, message
    assert "detached_cart" in message and "base" in message, message
    assert "CYCLE" not in message, message


def test_add_gearbox_ratio_edit_and_refusals(tmp_path):
    """One `add` kind carrying a ratio plus joint references — reusing
    add_entry's "unknown X" reference refusal and reparent's existing CYCLE
    refusal rather than new gearbox-specific tree logic."""
    from lockwire.edit import reparent

    contract_path = _armtree(tmp_path)
    gbx = {
        "id": "gbx.shoulder",
        "input_joint": "base_to_shoulder",
        "output_joint": "shoulder_to_elbow",
    }
    add_entry(
        contract_path,
        "gearbox",
        {**gbx, "ratio": 4.0},
        source="d",
        witness="w1",
        locked_at=LOCKED_AT,
    )

    # (1) editing the ratio and reopening changes only that field's bytes.
    after_add = contract_path.read_text()
    apply_changes(
        contract_path,
        [
            Change(
                path="gearboxes/gbx.shoulder/ratio",
                value="9.0",
                source="d",
                witness="w1",
                locked_at=LOCKED_AT,
            )
        ],
    )
    touched = _changed_lines(after_add, contract_path.read_text())
    assert touched and len(touched) <= 8
    assert all(
        any(
            key in line for key in ("ratio", "provenance", "witness", "source", "locked_at", "note")
        )
        for line in touched
    ), touched
    assert load_contract(contract_path).gearboxes[0].ratio == 9.0

    # (2) reparent still refuses a cycle in the link tree the gearbox's joints sit in.
    with pytest.raises(EditRefusal, match="CYCLE"):
        reparent(contract_path, "shoulder", "wrist", source="n", witness="w1", locked_at=LOCKED_AT)

    # (3) an output joint referencing a missing part is refused.
    before = _sha(contract_path)
    bad = {
        "id": "gbx.bad",
        "ratio": 2.0,
        "input_joint": "base_to_shoulder",
        "output_joint": "no_such_joint",
    }
    with pytest.raises(EditRefusal, match="unknown joint"):
        add_entry(contract_path, "gearbox", bad, source="d", witness="w1", locked_at=LOCKED_AT)
    assert _sha(contract_path) == before


def test_a_rotation_without_a_unit_is_refused(tmp_path):
    """The joint-unit rule read across to rotation: a rotation with no unit
    is REFUSED, never read as radians. `--rpy 0,0,90` meant in degrees and
    silently taken as radians is a rotation no later check can tell from an
    intended one.
    """
    contract = _armtree(tmp_path)
    from lockwire.edit import rotate

    with pytest.raises(EditRefusal) as error:
        rotate(
            contract,
            "wrist",
            (0.0, 0.0, 90.0),
            unit=None,
            source="notes/layout.md",
            witness="Maya Chen",
            locked_at=POSE_DATE,
        )
    assert "--unit is required" in str(error.value), error.value

    # And the unit is USED, not merely demanded: 90 deg is not 90 rad.
    rotate(
        contract,
        "wrist",
        (0.0, 0.0, 90.0),
        unit="deg",
        source="notes/layout.md",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )
    joint = next(j for j in load_contract(contract).model.joints if j.name == "elbow_to_wrist")
    assert joint.origin.rpy[2] == pytest.approx(1.5707963267948966)


def test_a_refused_pose_edit_leaves_the_file_byte_identical(tmp_path):
    """Every pose refusal happens before `apply_changes` opens the file, so a
    refused edit cannot leave a partial write behind. Checked by sha, over every
    refusal the three operations can raise.
    """
    contract = _armtree(tmp_path)
    before = _sha(contract)
    from lockwire.edit import move, reparent, rotate

    # Named, not numbered: `refusal 3 wrote to the file` sends the next reader
    # counting entries in a list, which is exactly the unreadable failure the
    # LEGIBILITY law is about.
    refusals = {
        "reparent closing a cycle": lambda: reparent(
            contract, "shoulder", "wrist", source="notes/layout.md", witness="Maya Chen"
        ),
        "reparent orphaning a subtree": lambda: reparent(
            contract, "shoulder", "detached_cart", source="notes/layout.md", witness="Maya Chen"
        ),
        "reparent onto a parent that does not resolve": lambda: reparent(
            contract, "shoulder", "nosuchlink", source="notes/layout.md", witness="Maya Chen"
        ),
        "rotate with no unit": lambda: rotate(
            contract, "wrist", (0, 0, 1), unit=None, source="notes/layout.md", witness="Maya Chen"
        ),
        "rotate a device, which records no orientation": lambda: rotate(
            contract,
            "dev.gripper",
            (0, 0, 1),
            unit="rad",
            source="notes/layout.md",
            witness="Maya Chen",
        ),
        "move with no unit": lambda: move(
            contract,
            "dev.gripper",
            (1, 0, 0),
            unit=None,
            source="notes/layout.md",
            witness="Maya Chen",
        ),
        "move with a unit of the wrong dimension": lambda: move(
            contract,
            "dev.gripper",
            (1, 0, 0),
            unit="W",
            source="notes/layout.md",
            witness="Maya Chen",
        ),
        "move a target that is neither device nor link": lambda: move(
            contract,
            "nosuchthing",
            (1, 0, 0),
            unit="m",
            source="notes/layout.md",
            witness="Maya Chen",
        ),
        "move witnessed by 'you'": lambda: move(
            contract, "dev.gripper", (1, 0, 0), unit="m", source="notes/layout.md", witness="you"
        ),
        "move sourced from an absolute path": lambda: move(
            contract,
            "dev.gripper",
            (1, 0, 0),
            unit="m",
            source="/Users/maya/layout.md",
            witness="Maya Chen",
        ),
    }
    for label, call in refusals.items():
        with pytest.raises(EditRefusal):
            call()
        assert _sha(contract) == before, f"{label} wrote to the file"


def test_an_absolute_source_path_is_refused_for_every_pose_edit(tmp_path):
    """`--source` names evidence that travels with the contract, so an
    absolute path is refused on every pose door — the gate is
    `edit.validate_source`, shared with declare/apply/add, not a fourth copy.
    """
    contract = _armtree(tmp_path)
    from lockwire.edit import move, reparent, rotate

    for call in (
        lambda: move(
            contract,
            "dev.gripper",
            (1, 0, 0),
            unit="m",
            source="/Users/maya/layout.md",
            witness="Maya Chen",
        ),
        lambda: rotate(
            contract,
            "wrist",
            (0, 0, 1),
            unit="rad",
            source="/Users/maya/layout.md",
            witness="Maya Chen",
        ),
        lambda: reparent(
            contract, "elbow", "base", source="/Users/maya/layout.md", witness="Maya Chen"
        ),
    ):
        with pytest.raises(EditRefusal) as error:
            call()
        assert "absolute path" in str(error.value), error.value


# ------------------------------------------------------------ mount CAD


def test_place_and_both_mount_shape_writers_reload_from_the_contract(tmp_path):
    """Placement and shape are contract edits, so a fresh load sees both.

    The first shape enters through the general ``lockwire declare`` door; the
    second enters through the dedicated editor API.  They therefore prove the
    two writers meet at the same single ``Physical.mount_shape`` field.
    """
    contract = _armtree(tmp_path)
    placed = api.place(
        contract,
        "dev.gripper",
        "elbow",
        (10.0, 0.0, 25.0),
        unit="mm",
        source="notes/mount.md",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )
    assert placed["written"] is True

    declared = runner.invoke(
        app,
        [
            "declare",
            str(contract),
            "devices/dev.gripper/physical/mount_shape",
            '{"kind":"plate","dimensions_mm":[100,50,3],"fastener_pattern":"4x M3"}',
            "--source",
            "notes/mount.md",
            "--witness",
            "Maya Chen",
            "--locked-at",
            POSE_DATE.isoformat(),
        ],
    )
    assert declared.exit_code == 0, declared.output
    first = load_contract(contract).device("dev.gripper").physical
    assert first.mount_frame == "elbow"
    assert [quantity.unit for quantity in first.mount_xyz] == ["mm", "mm", "mm"]
    assert [quantity.value for quantity in first.mount_xyz] == [10.0, 0.0, 25.0]
    assert first.mount_shape.model_dump() == {
        "kind": "plate",
        "dimensions_mm": (100.0, 50.0, 3.0),
        "fastener_pattern": "4x M3",
        "mesh_path": None,
    }

    edited = api.mount_shape(
        contract,
        "dev.gripper",
        {
            "kind": "custom-mesh",
            "dimensions_mm": [101.0, 51.0, 4.0],
            "mesh_path": "cad/gripper-mount.stl",
        },
        source="cad/gripper-mount.step",
        witness="Maya Chen",
        locked_at=POSE_DATE,
    )
    assert edited["written"] is True
    shape = load_contract(contract).device("dev.gripper").physical.mount_shape
    assert shape.kind == "custom-mesh"
    assert shape.dimensions_mm == (101.0, 51.0, 4.0)
    assert shape.mesh_path == "cad/gripper-mount.stl"


def test_invalid_mount_shape_and_place_refusals_preserve_exact_bytes(tmp_path):
    """Malformed forms, absolute meshes and unresolved parents write no bytes."""
    contract = _armtree(tmp_path)
    before = contract.read_bytes()
    calls = {
        "declare malformed shape": lambda: apply_changes(
            contract,
            [
                Change(
                    path="devices/dev.gripper/physical/mount_shape",
                    value="plate: 100x50x3",
                    source="notes/mount.md",
                    witness="Maya Chen",
                )
            ],
        ),
        "editor absolute custom mesh": lambda: api.mount_shape(
            contract,
            "dev.gripper",
            {
                "kind": "custom-mesh",
                "dimensions_mm": [100, 50, 3],
                "mesh_path": "/Users/maya/cad/mount.stl",
            },
            source="notes/mount.md",
            witness="Maya Chen",
        ),
        "editor invalid dimensions": lambda: api.mount_shape(
            contract,
            "dev.gripper",
            {"kind": "tube", "dimensions_mm": [100, 0, 3]},
            source="notes/mount.md",
            witness="Maya Chen",
        ),
        "place on unresolved link": lambda: api.place(
            contract,
            "dev.gripper",
            "missing_link",
            (0, 0, 0),
            unit="mm",
            source="notes/mount.md",
            witness="Maya Chen",
        ),
    }
    for label, call in calls.items():
        with pytest.raises(EditRefusal):
            call()
        assert contract.read_bytes() == before, f"{label} wrote to the file"


def test_edit_keeps_file_mode(tmp_path):
    """An edit keeps the contract's file mode. The lock file beside the contract is kept on
    purpose: it is the shared inode other editors wait on."""
    path = _copy(tmp_path)
    path.chmod(0o644)
    apply_changes(path, [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")])
    assert path.stat().st_mode & 0o777 == 0o644
