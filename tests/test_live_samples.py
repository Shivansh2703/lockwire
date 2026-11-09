"""LiveSource.samples(): ros2 bag record through the runner, fetched and
read like a bag, without Docker.

No container needed here: a fake ``CommandRunner`` stands in for ``ros2 bag
record`` (returning 124, timeout's normal end) and a fake ``fetch`` copies an
in-test MCAP fixture into the local capture dir, exercising the exact path
``docker_runner``/``docker_fetch`` would drive against a real robot.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.preflight import LiveSource, SamplesUnavailable, run_preflight
from lockwire.scan import CommandResult
from lockwire.schema import Contract

mcap_ros2_writer = pytest.importorskip(
    "mcap_ros2.writer", reason="needs the optional decoder: pip install 'lockwire[ros2bag]'"
)

IMU_SCHEMA = """\
std_msgs/Header header
geometry_msgs/Quaternion orientation
float64[9] orientation_covariance
geometry_msgs/Vector3 angular_velocity
float64[9] angular_velocity_covariance
geometry_msgs/Vector3 linear_acceleration
float64[9] linear_acceleration_covariance
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
================================================================================
MSG: geometry_msgs/Quaternion
float64 x
float64 y
float64 z
float64 w
================================================================================
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
"""


def _imu(accel_z: float) -> dict:
    zeros = [0.0] * 9
    return {
        "header": {"stamp": {"sec": 0, "nanosec": 0}, "frame_id": "imu_link"},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "orientation_covariance": zeros,
        "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "angular_velocity_covariance": zeros,
        "linear_acceleration": {"x": 0.0, "y": 0.0, "z": accel_z},
        "linear_acceleration_covariance": zeros,
    }


def write_bag(
    path: Path,
    topic: str = "/imu/data",
    count: int = 50,
    period_s: float = 0.01,
    accel_z: float = -9.81,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = mcap_ros2_writer.Writer(stream)
        schema = writer.register_msgdef("sensor_msgs/msg/Imu", IMU_SCHEMA)
        start_ns = 1_700_000_000_000_000_000
        for i in range(count):
            stamp = start_ns + int(i * period_s * 1_000_000_000)
            writer.write_message(topic, schema, _imu(accel_z), log_time=stamp, publish_time=stamp)
        writer.finish()
    return path


CONTRACT_DICT = {
    "schema_version": "0.3",
    "robot": {"name": "livebot"},
    "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
    "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
    "devices": [
        {
            "id": "dev.imu",
            "model": "mdl.imu",
            "owner": "shivansh",
            "driver": {
                "node": "/imu_driver",
                "topic": "/imu/data",
                "msg_type": "sensor_msgs/msg/Imu",
                "nominal_rate_hz": 100.0,
                "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
            },
        }
    ],
}

NO_TOPIC_CONTRACT_DICT = {
    "schema_version": "0.3",
    "robot": {"name": "livebot"},
    "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
    "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
    "devices": [{"id": "dev.imu", "model": "mdl.imu", "owner": "shivansh"}],
}


@pytest.fixture
def contract() -> Contract:
    return Contract.model_validate(CONTRACT_DICT)


def _timeout_runner(calls: list):
    """Records the command tuple, returns 124 — timeout's normal end."""

    def run(command: tuple[str, ...]) -> CommandResult:
        calls.append(command)
        return CommandResult(124, stdout="", stderr="")

    return run


def _fixture_fetch(bag_path: Path):
    """Fake fetch: copies an in-test MCAP bag into the local capture dir."""

    def fetch(remote_dir: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(bag_path, local_dir / "run.mcap")

    return fetch


def _empty_fetch(remote_dir: str, local_dir: Path) -> None:
    """Fake fetch: the remote directory produced no .mcap files."""
    local_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_samples_records_exactly_the_declared_topics(tmp_path, contract):
    calls: list = []
    bag = write_bag(tmp_path / "src.mcap", count=20)
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner(calls),
        fetch=_fixture_fetch(bag),
        seconds=10,
    )

    source.samples()

    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == ("timeout", "-s", "INT", "13")  # seconds + 3 margin
    assert command[4:9] == ("ros2", "bag", "record", "-s", "mcap")
    assert command[9] == "-o"
    assert command[11:] == ("/imu/data",)  # exactly the declared topics


def test_samples_returns_recording_with_fixtures_messages(tmp_path, contract):
    bag = write_bag(tmp_path / "src.mcap", count=37)
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner([]),
        fetch=_fixture_fetch(bag),
    )

    recording = source.samples()

    assert recording.source == "ros2bag"
    assert len(recording.devices["dev.imu"].recv_times) == 37
    assert source.message_count == 37
    assert source.capture_seconds == source.seconds == recording.capture_window_s


def test_samples_are_cached_same_object(tmp_path, contract):
    bag = write_bag(tmp_path / "src.mcap", count=5)
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner([]),
        fetch=_fixture_fetch(bag),
    )

    first = source.samples()
    second = source.samples()

    assert first is second


def test_keep_dir_receives_the_fetched_files(tmp_path, contract):
    bag = write_bag(tmp_path / "src.mcap", count=5)
    keep_dir = tmp_path / "kept"
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner([]),
        fetch=_fixture_fetch(bag),
        keep_dir=keep_dir,
    )

    source.samples()

    assert (keep_dir / "run.mcap").is_file()
    assert source.capture_dir == keep_dir


def test_live_capture_target_dir_is_unique_within_a_second(tmp_path, contract, monkeypatch):
    """E0-09: two captures whose ``stamp`` resolves to the same second must not
    collide on the remote bag path — a second ``ros2 bag record -o`` into an
    already-existing dir fails, or worse, partially overwrites the first.
    """
    import datetime as _dt

    from lockwire_pro.preflight import source as source_module

    frozen = _dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=_dt.timezone.utc)

    class _FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(source_module.dt, "datetime", _FrozenDatetime)

    bag = write_bag(tmp_path / "src.mcap", count=5)
    remote_dirs: list[str] = []

    def fetch(remote_dir: str, local_dir: Path) -> None:
        remote_dirs.append(remote_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(bag, local_dir / "run.mcap")

    source_a = LiveSource(contract=contract, runner=_timeout_runner([]), fetch=fetch)
    source_b = LiveSource(contract=contract, runner=_timeout_runner([]), fetch=fetch)
    source_a.samples()
    source_b.samples()

    assert len(remote_dirs) == 2
    assert remote_dirs[0] != remote_dirs[1]
    assert remote_dirs[0].startswith("/tmp/lockwire-live-20260908T120000Z")


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_no_declared_topics_refuses_before_recording():
    contract = Contract.model_validate(NO_TOPIC_CONTRACT_DICT)
    calls: list = []
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner(calls),
        fetch=lambda remote, local: None,  # never reached
    )

    with pytest.raises(SamplesUnavailable, match="no device declares a topic"):
        source.samples()
    assert calls == []  # never attempted to record


def test_runner_failure_names_stderr(contract):
    def failing_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(1, stderr="ros2: command not found")

    source = LiveSource(contract=contract, runner=failing_runner, fetch=lambda remote, local: None)

    with pytest.raises(SamplesUnavailable, match="ros2: command not found"):
        source.samples()


def test_fetch_yields_no_mcap_refuses_as_zero_messages(contract):
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner([]),
        fetch=_empty_fetch,
        seconds=5,
    )

    with pytest.raises(SamplesUnavailable, match="captured 0 messages in 5 s on /imu/data"):
        source.samples()


def test_zero_captured_messages_refuses_not_a_pass(tmp_path, contract):
    """The bag exists but never carries the declared topic — 0 messages, not a pass."""
    bag = write_bag(tmp_path / "src.mcap", topic="/some/other/topic", count=5)
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner([]),
        fetch=_fixture_fetch(bag),
    )

    with pytest.raises(SamplesUnavailable, match="captured 0 messages"):
        source.samples()


def test_no_fetch_configured_refuses_honestly(contract):
    source = LiveSource(contract=contract, runner=_timeout_runner([]))

    with pytest.raises(SamplesUnavailable, match="no fetch configured"):
        source.samples()


# ---------------------------------------------------------------------------
# a live capture runs exactly once (bounce fix: failures cache too)
# ---------------------------------------------------------------------------


def test_capture_runs_once_across_direct_call_and_run_preflight(tmp_path, contract):
    """Success: the CLI's own samples() call plus run_preflight's stages must
    not each trigger a fresh ``ros2 bag record`` — one recording, shared."""
    calls: list = []
    bag = write_bag(tmp_path / "src.mcap", count=10)
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner(calls),
        fetch=_fixture_fetch(bag),
    )

    source.samples()  # the CLI's own capture-line call
    run_preflight(contract, source)  # stages 2-4 each ask for samples() too

    record_calls = [c for c in calls if c[:2] == ("timeout", "-s")]
    assert len(record_calls) == 1, f"expected exactly one ros2 bag record, got {record_calls}"


def test_capture_failure_is_cached_not_retried(contract):
    """Refusal (recorder failure) must also run the recorder exactly once."""
    calls: list = []

    def failing_runner(command: tuple[str, ...]) -> CommandResult:
        calls.append(command)
        return CommandResult(1, stderr="ros2: command not found")

    source = LiveSource(contract=contract, runner=failing_runner, fetch=lambda remote, local: None)

    with pytest.raises(SamplesUnavailable, match="ros2: command not found"):
        source.samples()
    with pytest.raises(SamplesUnavailable, match="ros2: command not found"):
        source.samples()  # must reuse the cached refusal, not record again

    findings = run_preflight(contract, source)
    later = [f for f in findings if f.stage is not None and f.stage >= 2]
    assert later and all(f.severity == "not_scored" for f in later)

    record_calls = [c for c in calls if c[:2] == ("timeout", "-s")]
    assert len(record_calls) == 1, (
        f"expected exactly one ros2 bag record attempt, got {record_calls}"
    )


def test_zero_message_capture_is_cached_not_retried(contract):
    """0 messages is also a refusal — one attempt, cached, stages 2-4 not_scored."""
    calls: list = []
    source = LiveSource(
        contract=contract,
        runner=_timeout_runner(calls),
        fetch=_empty_fetch,
    )

    with pytest.raises(SamplesUnavailable, match="captured 0 messages"):
        source.samples()

    findings = run_preflight(contract, source)
    later = [f for f in findings if f.stage is not None and f.stage >= 2]
    assert later and all(f.severity == "not_scored" for f in later)

    record_calls = [c for c in calls if c[:2] == ("timeout", "-s")]
    assert len(record_calls) == 1, (
        f"expected exactly one ros2 bag record attempt, got {record_calls}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def test_cli_live_without_container_or_local_exits_2():
    result = runner.invoke(
        app,
        ["preflight", str(FIXTURES / "contract_live.yaml"), "--live", "--no-ledger"],
    )
    assert result.exit_code == 2, result.output
    assert "--container" in result.output or "--local" in result.output


def test_cli_live_with_from_exits_2(tmp_path):
    result = runner.invoke(
        app,
        [
            "preflight",
            str(FIXTURES / "contract_live.yaml"),
            "--live",
            "--local",
            "--from",
            str(tmp_path),
            "--no-ledger",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--live" in result.output and "--from" in result.output


def test_cli_live_local_end_to_end(tmp_path, monkeypatch):
    """A fake-runner --local run shows the two capture lines and the stage render.

    pins the verdict word and exit code; "GO" alone is in every outcome.
    """
    bag = write_bag(tmp_path / "src.mcap", count=500)  # 4.99 s of a 5 s window

    def fake_subprocess_runner(command: tuple[str, ...], *, timeout: float = 10) -> CommandResult:
        return CommandResult(124, stdout="", stderr="")

    monkeypatch.setattr("lockwire_pro.cli_pro.subprocess_runner", fake_subprocess_runner)
    monkeypatch.setattr("lockwire_pro.cli_pro.local_fetch", _fixture_fetch(bag))

    contract_path = tmp_path / "contract.yaml"
    from ruamel.yaml import YAML

    YAML().dump(CONTRACT_DICT, contract_path.open("w"))

    result = runner.invoke(
        app,
        ["preflight", str(contract_path), "--live", "--local", "--seconds", "5", "--no-ledger"],
    )

    assert "capturing" in result.output
    assert "captured 500 messages" in result.output
    assert "\nGO — records agree" in result.output and result.exit_code == 0, result.output


def test_cli_live_records_exactly_once(tmp_path, monkeypatch):
    """Bounce fix: the CLI's capture-line samples() call plus run_preflight's
    stages must record exactly once, not twice — same command tuple both times
    would mean a doubled capture window on a real robot."""
    calls: list = []
    bag = write_bag(tmp_path / "src.mcap", count=10)

    def counting_subprocess_runner(
        command: tuple[str, ...], *, timeout: float = 10
    ) -> CommandResult:
        calls.append(command)
        return CommandResult(124, stdout="", stderr="")

    monkeypatch.setattr("lockwire_pro.cli_pro.subprocess_runner", counting_subprocess_runner)
    monkeypatch.setattr("lockwire_pro.cli_pro.local_fetch", _fixture_fetch(bag))

    contract_path = tmp_path / "contract.yaml"
    from ruamel.yaml import YAML

    YAML().dump(CONTRACT_DICT, contract_path.open("w"))

    runner.invoke(
        app, ["preflight", str(contract_path), "--live", "--local", "--seconds", "5", "--no-ledger"]
    )

    record_calls = [c for c in calls if c[:2] == ("timeout", "-s")]
    assert len(record_calls) == 1, (
        f"expected exactly one ros2 bag record command, got {record_calls}"
    )


def test_cli_live_local_no_ros2_refuses_up_front_exit_2(tmp_path, monkeypatch):
    """A machine with no ros2 must refuse as a tool failure, not
    score every declared driver as absent (evidence 068)."""

    def fake_subprocess_runner(command: tuple[str, ...], *, timeout: float = 10) -> CommandResult:
        return CommandResult(127, stdout="", stderr="ros2: command not found")

    monkeypatch.setattr("lockwire_pro.cli_pro.subprocess_runner", fake_subprocess_runner)

    contract_path = tmp_path / "contract.yaml"
    from ruamel.yaml import YAML

    YAML().dump(CONTRACT_DICT, contract_path.open("w"))

    result = runner.invoke(
        app,
        ["preflight", str(contract_path), "--live", "--local", "--seconds", "5", "--no-ledger"],
    )
    assert result.exit_code == 2, result.output
    assert "ros2 is not on this machine's PATH" in result.output
    assert "capturing" not in result.output
    assert "GO" not in result.output and "NO-GO" not in result.output


def test_cli_live_local_with_ros2_present_runs_stages_as_before(tmp_path, monkeypatch):
    """With ros2 present the probe passes through and the run proceeds as before.

    five messages are under the sample floor, so NOT SCORED, exit 3.
    """
    bag = write_bag(tmp_path / "src.mcap", count=5)

    def fake_subprocess_runner(command: tuple[str, ...], *, timeout: float = 10) -> CommandResult:
        if command[:2] == ("ros2", "--help"):
            return CommandResult(0, stdout="usage: ros2 ...", stderr="")
        return CommandResult(124, stdout="", stderr="")

    monkeypatch.setattr("lockwire_pro.cli_pro.subprocess_runner", fake_subprocess_runner)
    monkeypatch.setattr("lockwire_pro.cli_pro.local_fetch", _fixture_fetch(bag))

    contract_path = tmp_path / "contract.yaml"
    from ruamel.yaml import YAML

    YAML().dump(CONTRACT_DICT, contract_path.open("w"))

    result = runner.invoke(
        app,
        ["preflight", str(contract_path), "--live", "--local", "--seconds", "5", "--no-ledger"],
    )
    assert "capturing" in result.output
    assert "captured 5 messages" in result.output
    assert "NOT SCORED — nothing compared; dev.imu not measured: 5 sample(s)" in result.output
    assert result.exit_code == 3, result.output


def test_cli_live_capture_failed_with_nodes_up_is_not_scored(tmp_path, monkeypatch):
    """the capture fails but every node is up; the CLI must say NOT SCORED,
    exit 3, never carry on to GO on enumeration and liveness alone."""
    from lockwire_pro.preflight.source import DeviceObservation, LivenessObservation

    class _NodesUpCaptureFails:
        scores_absence = True
        message_count = 0
        capture_dir = None

        def __init__(self, contract, **_kwargs):
            self.contract = contract

        def enumerate(self):
            return [DeviceObservation(device_id=d.id) for d in self.contract.devices]

        def liveness(self):
            return [
                LivenessObservation(device_id=d.id, node=d.driver.node, topic=d.driver.topic)
                for d in self.contract.devices
            ]

        def samples(self):
            raise SamplesUnavailable("live capture failed: recorder exited 1")

        def cancel(self):
            return False

    ok = lambda command, *, timeout=10: CommandResult(0, stdout="", stderr="")  # noqa: E731
    monkeypatch.setattr("lockwire_pro.cli_pro.subprocess_runner", ok)
    monkeypatch.setattr("lockwire_pro.cli_pro.LiveSource", _NodesUpCaptureFails)
    contract_path = tmp_path / "contract.yaml"
    from ruamel.yaml import YAML

    YAML().dump(CONTRACT_DICT, contract_path.open("w"))
    args = ["preflight", str(contract_path), "--live", "--local", "--no-ledger"]
    result = runner.invoke(app, args)
    assert "\nNOT SCORED — nothing compared" in result.output, result.output
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# cancellable capture — reachability for kill_container_process (E3-QC)
# ---------------------------------------------------------------------------


class _FakeLiveRunner:
    """One ``CommandRunner`` serving all three legs a cancellable capture uses,
    with no docker daemon: the blocking ``ros2 bag record`` (via ``timeout``),
    the concurrent exact-process probe (``ps``), and ``kill_container_process``'s own
    ``kill -0/-TERM/-KILL`` corpse-check — same shape as ``scan.py``'s own
    tests fake ``docker exec`` with a plain callable.
    """

    def __init__(self, *, record_seconds: float, recorder_pid: int, dies_on: str | None):
        self.record_seconds = record_seconds
        self.recorder_pid = recorder_pid
        self.dies_on = dies_on
        self.alive = True
        self.signals_received: list[str] = []
        self._started = threading.Event()
        self.target_dir: str | None = None

    def __call__(self, command: tuple[str, ...]) -> CommandResult:
        if command[0] == "timeout":  # the ros2 bag record invocation itself
            self.target_dir = command[command.index("-o") + 1]
            self._started.set()
            time.sleep(self.record_seconds)
            return CommandResult(124, stdout="", stderr="")
        if command[:4] == ("ps", "-ww", "-eo", "pid=,args="):
            if self._started.is_set() and self.alive:
                return CommandResult(
                    0,
                    stdout=(
                        f"100 timeout -s INT 4 ros2 bag record -o {self.target_dir}\n"
                        f"{self.recorder_pid} /usr/bin/python3 /opt/ros/jazzy/bin/ros2 "
                        f"bag record -s mcap -o {self.target_dir} /imu/data\n"
                    ),
                )
            return CommandResult(0, stdout="")
        if command[0] == "kill":
            if command[1] == "-0":
                return CommandResult(0 if self.alive else 1)
            sig = command[1].removeprefix("-")
            self.signals_received.append(sig)
            if self.alive and sig == self.dies_on:
                self.alive = False
            return CommandResult(0)
        raise AssertionError(f"unexpected command for a cancellable live capture: {command}")


def test_cancellable_capture_discovers_and_kills_the_container_side_recorder(contract):
    """`cancel()` is reachable while a `cancellable=True` capture is in flight.

    The record command blocks for `record_seconds` on its own background
    thread (`_run_cancellable`); this asserts the pid becomes visible to the
    main thread *before* that call returns, and that `cancel()` then reaches
    `kill_container_process` for real — TERM sent to the discovered pid, not
    merely `kill_container_process` existing and never being called.
    """
    runner = _FakeLiveRunner(record_seconds=2.0, recorder_pid=4242, dies_on="TERM")
    source = LiveSource(
        contract=contract,
        runner=runner,
        fetch=_empty_fetch,
        seconds=1,
        cancellable=True,
    )

    def _drive() -> None:
        # An empty fetch (no messages captured) makes samples() raise
        # SamplesUnavailable once the fake record call returns — expected and
        # irrelevant here; this test is about the pid discovered and killed
        # *while that call is still blocking*, not the capture's own result.
        try:
            source.samples()
        except SamplesUnavailable:
            pass

    driver = threading.Thread(target=_drive)
    driver.start()
    try:
        deadline = time.monotonic() + 5
        while source._recorder_pid is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert source._recorder_pid == 4242, (
            "the exact recorder pid was not distinguished from its timeout wrapper"
        )

        assert source.cancel() is True
        assert runner.signals_received == ["TERM"], (
            "cancel() did not reach the container-side recorder via kill_container_process"
        )
    finally:
        driver.join(timeout=5)
        assert not driver.is_alive(), "the capture thread outlived the test"


def test_cancelled_nonempty_capture_is_surfaced_as_partial(tmp_path, contract):
    bag = write_bag(tmp_path / "src.mcap", count=5)
    runner = _FakeLiveRunner(record_seconds=2.0, recorder_pid=4242, dies_on="TERM")
    source = LiveSource(
        contract=contract,
        runner=runner,
        fetch=_fixture_fetch(bag),
        seconds=1,
        cancellable=True,
    )
    result = []

    driver = threading.Thread(target=lambda: result.append(source.samples()))
    driver.start()
    try:
        deadline = time.monotonic() + 5
        while source._recorder_pid is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert source.cancel() is True
    finally:
        driver.join(timeout=5)

    assert not driver.is_alive(), "the capture thread outlived the test"
    assert result[0].partial is True and result[0].capture_window_s is None
    assert len(result[0].devices["dev.imu"].recv_times) == 5


def test_cli_signal_stops_the_exact_recorder_and_surfaces_partial_capture(tmp_path):
    """Exercise the CLI's installed SIGTERM handler in a real child process."""
    bag = write_bag(tmp_path / "source.mcap", count=5)
    started = tmp_path / "recorder.pid"
    ros2 = tmp_path / "ros2"
    ros2.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, shutil, signal, sys, time\n"
        f"bag = pathlib.Path({str(bag)!r})\n"
        f"started = pathlib.Path({str(started)!r})\n"
        "if sys.argv[1:] == ['--help']:\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:3] == ['bag', 'record']:\n"
        "    target = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "    def stop(*_args):\n"
        "        target.mkdir(parents=True, exist_ok=True)\n"
        "        shutil.copy(bag, target / 'run.mcap')\n"
        "        raise SystemExit(0)\n"
        "    signal.signal(signal.SIGTERM, stop)\n"
        "    signal.signal(signal.SIGINT, stop)\n"
        "    started.write_text(str(os.getpid()))\n"
        "    while True: time.sleep(1)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    ros2.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    # A narrow terminal: procps `ps` cuts args at $COLUMNS unless told not to.
    env["COLUMNS"] = "80"
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "lockwire",
            "preflight",
            str(Path(__file__).parent / "fixtures" / "contract_live.yaml"),
            "--live",
            "--local",
            "--seconds",
            "30",
            "--keep-bag",
            str(tmp_path / "kept"),
            "--no-ledger",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started.exists(), process.communicate(timeout=5)[0]
        recorder_pid = int(started.read_text())

        process.send_signal(signal.SIGTERM)
        output = process.communicate(timeout=15)[0]

        assert process.returncode == -signal.SIGTERM, output
        assert "captured 5 messages (partial capture)" in output
        with pytest.raises(ProcessLookupError):
            os.kill(recorder_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
