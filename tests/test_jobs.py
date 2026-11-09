"""``lockwire_pro.jobs`` — a run that can be stopped, and proof that stopping it stopped it.

The decisive claim is the one in the module docstring: when ``cancel()`` returns,
the work is not still happening. These tests check the child's corpse, not the
return value.
"""

from __future__ import annotations

import ast
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.jobs import (
    GRACE_SECONDS,
    Job,
    JobCancelled,
    build_argv,
    kill_container_process,
    start_job,
)
from lockwire.scan import CommandResult

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = FIXTURES / "contract_clean.yaml"
RECORDING = FIXTURES / "bags" / "clean"
JOBS_SOURCE = Path(__file__).parent.parent / "src" / "lockwire_pro" / "jobs.py"

# A child that refuses to go quietly: it ignores SIGTERM, starts a grandchild in
# the same process group, and sleeps. Run as `[sys.executable, script]` — an argv
# list, so this test file does not build a command out of text either.
STUBBORN = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if len(sys.argv) > 1:
    time.sleep(300)
    raise SystemExit
child = subprocess.Popen([sys.executable, __file__, "grandchild"])
pids = sys.argv[0] + ".pids"
with open(pids + ".tmp", "w") as stream:
    stream.write(f"{os.getpid()} {child.pid}")
os.replace(pids + ".tmp", pids)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    """True while the pid exists. A reaped child raises; a live one does not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _stubborn_job(tmp_path: Path) -> tuple[Job, int, int]:
    """A started, uncooperative child; returns the job and both live pids."""
    script = tmp_path / "stubborn.py"
    script.write_text(STUBBORN, encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, str(script)], start_new_session=True
    )
    pids = script.with_suffix(".py.pids")
    deadline = time.monotonic() + 20
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pids.exists(), "stubborn child never started its grandchild"
    child_pid, grandchild_pid = (int(part) for part in pids.read_text().split())
    return Job("preflight", process, work_dir), child_pid, grandchild_pid


def test_cancel_kills_the_child_and_its_grandchild_that_ignore_sigterm(tmp_path):
    """`cancel()` does not return until the work is actually gone.

    The child ignores SIGTERM, so a TERM-only cancel would leave it running and
    report success — the failure this box exists to prevent. Both the child and
    the grandchild it started are checked by pid after the call, not by trusting
    a return value.
    """
    job, child_pid, grandchild_pid = _stubborn_job(tmp_path)
    assert _alive(child_pid) and _alive(grandchild_pid)

    job.cancel(grace=0.5)

    assert job.cancelled is True
    assert not _alive(child_pid), f"child {child_pid} survived cancel()"
    assert _wait_gone(grandchild_pid), f"grandchild {grandchild_pid} survived cancel()"
    assert job.exit_code is not None, "cancel() returned without reaping the child"


def test_cancel_sends_sigterm_before_sigkill(tmp_path):
    """TERM first, then KILL: a cooperative child gets to close its files.

    A cancel that goes straight to SIGKILL would truncate a ledger line mid-write.
    The child here exits 0 on TERM, so a SIGKILL would show as -SIGKILL instead.
    """
    script = tmp_path / "polite.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, str(script)], start_new_session=True
    )
    time.sleep(0.5)  # let the handler install; a bare exec would miss the signal
    job = Job("preflight", process, work_dir)
    job.cancel(grace=GRACE_SECONDS)
    assert job.exit_code == 0, f"expected a clean TERM exit, got {job.exit_code}"
    assert job.exit_code != -signal.SIGKILL


def test_cancel_writes_no_ledger_rows_and_leaves_no_artefact(tmp_path):
    """A cancelled run leaves the ledger as it found it and no readable leftovers.

    Three leftovers are checked: the health sidecar beside the contract (byte for
    byte), the job's scratch directory (which is where the `--json` document was
    being written, so a partial document dies with it), and the document itself,
    which `result()` refuses to invent.
    """
    contract = tmp_path / "contract.yaml"
    shutil.copy(CONTRACT, contract)
    sidecar = tmp_path / "contract.health.jsonl"
    sidecar.write_text('{"pre-existing": "row"}\n', encoding="utf-8")
    before = sidecar.read_bytes()

    job = start_job("preflight", contract=contract, recording=RECORDING)
    job.cancel()

    assert job.cancelled is True
    assert job.rows_written == 0, f"a cancelled run appended {job.rows_written} row(s)"
    assert sidecar.read_bytes() == before, "the ledger changed under a cancelled run"
    assert not job.work_dir.exists(), f"cancel left {job.work_dir} behind"
    with pytest.raises(JobCancelled):
        job.result(timeout=5)


def test_cancel_snapshot_keeps_partial_output_after_scratch_is_removed(tmp_path):
    job, _child_pid, _grandchild_pid = _stubborn_job(tmp_path)
    (job.work_dir / "output.txt").write_text("captured 5 messages (partial capture)\n")

    job.cancel(grace=0.5)

    assert job.snapshot() == {
        "status": "cancelled",
        "exit_code": -signal.SIGKILL,
        "rows_written": 0,
        "document": None,
        "output": "captured 5 messages (partial capture)\n",
        "errors": "",
        "error": None,
    }


def test_a_no_go_document_is_completed_not_cancelled(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "document.json").write_text('{"verdict": "NO-GO"}')
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, "-c", "raise SystemExit(1)"], start_new_session=True
    )
    process.wait()

    snapshot = Job("preflight", process, work_dir).snapshot()

    assert snapshot["status"] == "completed"
    assert snapshot["exit_code"] == 1
    assert snapshot["document"] == {"verdict": "NO-GO"}


def test_job_verdict_and_exit_code_match_the_cli(tmp_path):
    """The job's document and code are the CLI's, anchored to a literal verdict.

    Both legs run the same implementation, so agreement alone proves nothing
    (parity-anchor law): the literal `GO`/`0` is the third leg, and it is what
    goes red if the job's argv wires up the wrong contract, recording or context.
    """
    job_contract = tmp_path / "job.yaml"
    cli_contract = tmp_path / "cli.yaml"
    shutil.copy(CONTRACT, job_contract)
    shutil.copy(CONTRACT, cli_contract)

    job = start_job("preflight", contract=job_contract, recording=RECORDING)
    document = job.result(timeout=300)

    result = CliRunner().invoke(app, ["preflight", str(cli_contract), "--from", str(RECORDING)])

    assert document["verdict"] == "GO"
    assert job.exit_code == 0
    assert document["verdict"] in result.stdout
    assert job.exit_code == result.exit_code
    assert document["exit_code"] == result.exit_code
    assert job.rows_written == 5


def test_no_command_is_ever_built_from_text():
    """No invocation in `jobs.py` can become a shell line, for any input.

    Two halves, because either alone is a hole. The source half walks every call
    in the module's AST — prose in a docstring is not code, so it is parsed, not
    grepped — and requires that no call passes `shell=`, that none of the
    string-executing builtins is called, and that every `subprocess` call's
    command argument is a plain list or name, never an f-string, a `+`, a
    `.join()` or a `.format()`. The behaviour half feeds every caller-supplied
    value a string of shell metacharacters and requires each one back as its own
    intact argv element — unquoted, unescaped, unsplit, which is only true when
    no shell is involved.

    Ceiling: this governs `jobs.py`. `scan.py`'s `docker exec` path deliberately
    builds a `bash -lc` string and is E0-01's business, not this module's.
    """
    tree = ast.parse(JOBS_SOURCE.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert calls, "parsed no calls — the walk is not looking at the module"
    for call in calls:
        name = ast.unparse(call.func)
        assert not any(kw.arg == "shell" for kw in call.keywords), f"{name} passes shell="
        assert name not in ("os.system", "os.popen", "subprocess.getoutput", "eval", "exec"), (
            f"{name} executes a string as a command"
        )
        if name.startswith("subprocess.") and call.args:
            command = call.args[0]
            assert isinstance(command, ast.Name | ast.List), (
                f"{name} is handed `{ast.unparse(command)}`, which is assembled, not a list"
            )

    nasty = "; rm -rf / #$(touch pwned) `id` &&\n'\""
    argv = build_argv(
        "preflight",
        contract=f"/tmp/a{nasty}.yaml",
        recording=f"/tmp/b{nasty}",
        context=f"ctx{nasty}",
        json_path=Path(f"/tmp/c{nasty}.json"),
    )
    assert argv[:4] == [sys.executable, "-m", "lockwire", "preflight"]
    for value in (f"/tmp/a{nasty}.yaml", f"/tmp/b{nasty}", f"ctx{nasty}", f"/tmp/c{nasty}.json"):
        assert value in argv, f"{value!r} was mangled or merged instead of passed as one argument"
    assert build_argv("scan", robot=f"bot{nasty}")[-1] == f"bot{nasty}"
    for element in argv:
        assert isinstance(element, str)


def test_build_argv_uses_python_module_form_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)

    argv = build_argv("preflight", contract=CONTRACT, recording=RECORDING)

    assert argv[:4] == [sys.executable, "-m", "lockwire", "preflight"]
    assert argv[4:] == [str(CONTRACT), "--from", str(RECORDING), "--context", "nominal-op"]


@pytest.mark.parametrize(
    ("kwargs", "tail"),
    [
        (
            {"recording": RECORDING},
            [str(CONTRACT), "--from", str(RECORDING), "--context", "nominal-op"],
        ),
        (
            {"live": True, "container": "robo1", "seconds": 7},
            [
                str(CONTRACT),
                "--live",
                "--seconds",
                "7",
                "--context",
                "nominal-op",
                "--container",
                "robo1",
            ],
        ),
    ],
)
def test_build_argv_uses_frozen_cli_without_module_launcher(monkeypatch, kwargs, tail):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    argv = build_argv("preflight", contract=CONTRACT, **kwargs)

    assert argv[:2] == [sys.executable, "preflight"]
    assert "-m" not in argv
    assert argv[2:] == tail


class _FakeContainerProcess:
    """A `CommandRunner` stand-in for one pid inside a container (E3-QC).

    No docker daemon needed: `kill_container_process` only ever talks to its
    `runner` argument, exactly like `scan.py`'s own tests fake `docker exec`
    with a plain callable. `dies_on` says which signal (if any) ends it.
    """

    def __init__(self, dies_on: str | None):
        self.dies_on = dies_on
        self.alive = True
        self.signals_received: list[str] = []

    def run(self, command: tuple[str, ...]) -> CommandResult:
        verb = command[0]
        if verb != "kill":
            raise AssertionError(f"kill_container_process sent a non-kill command: {command}")
        if command[1] == "-0":
            return CommandResult(0 if self.alive else 1)
        sig = command[1].removeprefix("-")
        self.signals_received.append(sig)
        if self.alive and sig == self.dies_on:
            self.alive = False
        return CommandResult(0)


def test_kill_container_process_already_dead_sends_nothing():
    """A pid that no longer resolves is already a corpse — nothing to send."""
    target = _FakeContainerProcess(dies_on=None)
    target.alive = False

    assert kill_container_process(target.run, 4242, grace=0) is True
    assert target.signals_received == []


def test_kill_container_process_term_is_enough():
    """A cooperative container-side recorder stops on TERM; KILL is never sent."""
    target = _FakeContainerProcess(dies_on="TERM")

    assert kill_container_process(target.run, 4242, grace=0) is True
    assert target.signals_received == ["TERM"]


def test_kill_container_process_falls_back_to_kill():
    """A recorder that ignores TERM gets KILL next, same order as `Job.cancel`."""
    target = _FakeContainerProcess(dies_on="KILL")

    assert kill_container_process(target.run, 4242, grace=0) is True
    assert target.signals_received == ["TERM", "KILL"]


def test_kill_container_process_reports_false_when_the_corpse_cannot_be_proven():
    """A pid that survives TERM and KILL is reported unstopped, never claimed dead."""
    target = _FakeContainerProcess(dies_on=None)

    assert kill_container_process(target.run, 4242, grace=0) is False
    assert target.signals_received == ["TERM", "KILL"]


def test_start_job_live_needs_container_or_local():
    """A live job still needs a target — same shape the CLI itself requires."""
    with pytest.raises(ValueError, match="needs container=, local=True, or ssh_host="):
        start_job("preflight", contract=CONTRACT, live=True)


def test_start_job_refuses_live_scan():
    """Only preflight has --live; scan never gets a container/local target."""
    with pytest.raises(ValueError, match="only preflight can run live"):
        start_job("scan", live=True)


def test_build_argv_live_local():
    """`build_argv` builds `--live --local`, not `--from`, when live=True."""
    argv = build_argv(
        "preflight", contract=CONTRACT, live=True, local=True, seconds=7, context="ctx"
    )
    assert "--from" not in argv
    assert argv[5:] == ["--live", "--seconds", "7", "--context", "ctx", "--local"]


def test_build_argv_live_container():
    argv = build_argv("preflight", contract=CONTRACT, live=True, container="robo1")
    assert "--container" in argv and "robo1" in argv
    assert "--local" not in argv


def test_build_argv_live_and_recording_are_exclusive():
    with pytest.raises(ValueError, match="exclusive with recording="):
        build_argv("preflight", contract=CONTRACT, recording=RECORDING, live=True, local=True)


def test_build_argv_live_needs_container_or_local():
    with pytest.raises(
        ValueError, match="needs exactly one of container=, local=True, or ssh_host="
    ):
        build_argv("preflight", contract=CONTRACT, live=True)


def test_build_argv_live_ssh_requires_declared_identity_and_exclusive_target(tmp_path):
    options = {
        "ssh_host": "127.0.0.1",
        "ssh_port": 2222,
        "ssh_user": "root",
        "ssh_identity_file": tmp_path / "id",
        "ssh_known_hosts": tmp_path / "known_hosts",
        "ssh_target": "declared-loopback",
        "ssh_image": "declared-image",
        "keep_bag": tmp_path / "partial-bag",
    }
    argv = build_argv("preflight", contract=CONTRACT, live=True, **options)
    assert argv[-16:] == [
        "--ssh-host",
        "127.0.0.1",
        "--ssh-port",
        "2222",
        "--ssh-user",
        "root",
        "--ssh-identity-file",
        str(options["ssh_identity_file"]),
        "--ssh-known-hosts",
        str(options["ssh_known_hosts"]),
        "--ssh-target",
        "declared-loopback",
        "--ssh-image",
        "declared-image",
        "--keep-bag",
        str(options["keep_bag"]),
    ]
    with pytest.raises(ValueError, match="exactly one"):
        build_argv("preflight", contract=CONTRACT, live=True, container="other", **options)
    with pytest.raises(ValueError, match="needs identity file"):
        build_argv("preflight", contract=CONTRACT, live=True, ssh_host="127.0.0.1")


def _slow_but_clean_job(tmp_path: Path, *, live: bool) -> Job:
    """A child that ignores SIGTERM for a while, then exits 0 on its own —
    standing in for a live job's own SIGTERM handler running
    `kill_container_process`'s TERM-then-KILL round-trip before it lets the
    signal finish the process (E3-QC)."""
    script = tmp_path / "slow_clean.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(3)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    ready = tmp_path / "ready"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, str(script), str(ready)], start_new_session=True
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "slow child never installed its SIGTERM handler"
    return Job("preflight", process, work_dir, live=live)


def test_cancel_widens_its_wait_for_a_live_job_before_escalating(tmp_path):
    """A non-live job's default `cancel()` would SIGKILL this child before its
    3s self-exit; a live job's cancel() gives it enough room to exit cleanly —
    proving `self.live` actually changes what `cancel()` does, not just what
    its docstring claims."""
    job = _slow_but_clean_job(tmp_path, live=True)
    job.cancel()
    assert job.exit_code == 0, (
        f"a live job's widened wait should have caught the clean exit, got {job.exit_code}"
    )


def test_live_cancel_keeps_transport_alive_until_handler_finishes(tmp_path):
    """The CLI must retain its SSH child while its TERM handler closes the bag."""
    script = tmp_path / "live_transport.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "ready, signaled, observed, pids = map(Path, sys.argv[1:])\n"
        "transport = os.fork()\n"
        "if transport == 0:\n"
        "    signal.signal(signal.SIGTERM, lambda *_: signaled.touch())\n"
        "    ready.touch()\n"
        "    while True: time.sleep(0.1)\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        "def stop(*_):\n"
        "    deadline = time.monotonic() + 1\n"
        "    while not signaled.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    observed.write_text('signaled' if signaled.exists() else 'alive')\n"
        "    os.kill(transport, signal.SIGKILL)\n"
        "    os.waitpid(transport, 0)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "pids.write_text(str(transport))\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    ready = tmp_path / "transport-ready"
    signaled = tmp_path / "transport-signaled"
    observed = tmp_path / "transport-observed"
    pids = tmp_path / "transport-pid"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    process = subprocess.Popen(
        [sys.executable, str(script), str(ready), str(signaled), str(observed), str(pids)],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pids.exists(), "live transport fixture never installed its handler"
        transport_pid = int(pids.read_text())

        job = Job("preflight", process, work_dir, live=True)
        job.cancel(grace=0.5)

        assert observed.read_text() == "alive", (
            "live cancellation signaled transport before CLI cleanup"
        )
        assert job.exit_code == 0
        assert not _alive(transport_pid), "live transport survived cancellation"
        assert not work_dir.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def test_cancel_does_not_widen_the_wait_for_a_non_live_job(tmp_path):
    job = _slow_but_clean_job(tmp_path, live=False)
    job.cancel(grace=GRACE_SECONDS)
    assert job.exit_code == -signal.SIGKILL, (
        f"a non-live job's default grace should have escalated to SIGKILL, got {job.exit_code}"
    )


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown job kind"):
        start_job("edit")


def _dead_child(code: str):
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, "-c", code], start_new_session=True
    )
    process.wait()
    return process


def test_a_killed_child_reads_failed_not_completed(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "document.json").write_text('{"verdict": "GO"}')
    process = _dead_child("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    snapshot = Job("preflight", process, work_dir).snapshot()
    assert snapshot["status"] == "failed"
    assert "killed" in snapshot["error"]


def test_a_failed_scan_reads_failed_not_completed(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    snapshot = Job("scan", _dead_child("raise SystemExit(3)"), work_dir).snapshot()
    assert snapshot["status"] == "failed"


def test_a_job_past_its_time_limit_is_stopped_and_reads_failed(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    job = Job("scan", process, work_dir, timeout=0.2)
    process.wait(timeout=10)
    snapshot = job.snapshot()
    assert job.timed_out
    assert snapshot["status"] == "failed"
    assert "time limit" in snapshot["error"]
