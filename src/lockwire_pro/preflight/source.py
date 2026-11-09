"""The preflight machine-state seam.

Contract §3.1: the gate reads the machine through ONE abstraction — enumerate /
liveness / samples. This module lands the protocol and both first implementations
(`RecordedSource`, `LiveSource`). They derive observations; they do not judge
and they emit no findings.
"""

from __future__ import annotations

import datetime as dt
import shlex
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from lockwire_pro.recording import Recording
from lockwire.scan import CommandRunner, CommandResult, scan_machine
from lockwire.schema import Contract, DeviceIdentity, DeviceInstance, IdentityStrength

# Grace beyond the requested capture window before ``timeout`` SIGINTs the
# still-running ``ros2 bag record`` and finalizes the bag.
_LIVE_TIMEOUT_MARGIN_S = 3
# ``timeout``'s own exit code when it terminated the child on schedule — the
# normal end of a bounded capture, never itself a failure.
_TIMEOUT_EXIT_CODE = 124
# How often the pid-discovery loop re-probes the container while the capture's
# own ``docker exec`` blocks on a different thread (E3-QC).
_PID_POLL_INTERVAL_S = 0.2
_PID_DISCOVERY_TIMEOUT_S = 3.0

# Identity descriptors compared when BOTH declared and observed are populated.
_IDENTITY_FIELDS = (
    "bus_ref",
    "host",
    "vid_pid",
    "serial_number",
    "can_id",
    "address",
)


@dataclass(frozen=True)
class DeviceObservation:
    """A device the source observed as present (stage 0).

    Carries the observed device id and whatever identity descriptors the source
    can actually see — same fields as ``DeviceIdentity`` so stage 0 can compare
    like with like, including ``effective_strength()``'s instance/model/positional
    ladder. A recorded bag has no bus descriptors; only ``device_id`` is populated.
    """

    device_id: str
    bus_ref: str | None = None
    host: str | None = None
    vid_pid: str | None = None
    serial_number: str | None = None
    can_id: str | None = None
    address: str | None = None
    strength: IdentityStrength | None = None

    def effective_strength(self) -> IdentityStrength | None:
        """Mirror ``DeviceIdentity.effective_strength``; ``None`` when unseen."""
        if self.strength is not None:
            return self.strength
        if self.serial_number is not None:
            return "instance"
        if self.vid_pid is not None or self.can_id is not None:
            return "model"
        if self.bus_ref is not None or self.host is not None or self.address is not None:
            return "positional"
        return None


@dataclass(frozen=True)
class LivenessObservation:
    """A node/driver the source observed as up (stage 1).

    Fields mirror ``Driver``. ``device_id`` ties the observation to a contract
    device; a recorded bag cannot see node/topic, only that messages arrived.
    """

    device_id: str
    node: str | None = None
    topic: str | None = None
    nominal_rate_hz: float | None = None
    payload_bytes: int | None = None
    # eb-22: a declared operand that IS populated but did not appear on the
    # graph — distinct from an operand the driver never declared (which stays
    # ``None`` either way and sets no flag). Node absence is a real no-go;
    # topic absence alone is weak evidence (composable nodes and intra-process
    # topics never appear in ``ros2 topic list``). Never collapse the two.
    node_missing: bool = False
    topic_missing: bool = False


@runtime_checkable
class PreflightSource(Protocol):
    """The load-bearing seam: what the machine currently is (contract §3.1)."""

    def enumerate(self) -> list[DeviceObservation]:
        """Devices present on the bus (stage 0)."""
        ...

    def liveness(self) -> list[LivenessObservation]:
        """Which declared nodes/drivers are up (stage 1)."""
        ...

    def samples(self) -> Recording:
        """The signal stream for stages 2–4 (includes ``Recording.env``)."""
        ...


def _live_device_ids(recording: Recording) -> list[str]:
    """Device ids that actually produced at least one message.

    ``recording.units`` declares devices; ``recording.devices`` holds the ones
    that produced messages. A manifest-declared device with zero messages is
    ABSENT / NOT LIVE — never silently present. The reserved ``environment``
    unit scope is not a device.
    """
    return sorted(device_id for device_id, stream in recording.devices.items() if stream.recv_times)


@dataclass(frozen=True)
class RecordedSource:
    """``PreflightSource`` backed by a loaded ``Recording`` (the offline prove path)."""

    recording: Recording
    # Recorded bags cannot observe bus absence — only message-producing ids.
    scores_absence: ClassVar[bool] = False

    def enumerate(self) -> list[DeviceObservation]:
        return [
            DeviceObservation(device_id=device_id) for device_id in _live_device_ids(self.recording)
        ]

    def liveness(self) -> list[LivenessObservation]:
        return [
            LivenessObservation(device_id=device_id)
            for device_id in _live_device_ids(self.recording)
        ]

    def samples(self) -> Recording:
        return self.recording


def _identity_agrees(declared: DeviceIdentity, observed: DeviceObservation) -> bool:
    """True when every field populated on BOTH sides agrees (D3)."""
    for name in _IDENTITY_FIELDS:
        want = getattr(declared, name)
        seen = getattr(observed, name)
        if want is None or seen is None:
            continue
        if want != seen:
            return False
    # At least one descriptor must have been compared, or the match is vacuous.
    return any(
        getattr(declared, name) is not None and getattr(observed, name) is not None
        for name in _IDENTITY_FIELDS
    )


def _observation_from_scanned(device_id: str, scanned: DeviceInstance) -> DeviceObservation:
    identity = scanned.identity
    if identity is None:
        return DeviceObservation(device_id=device_id)
    return DeviceObservation(
        device_id=device_id,
        bus_ref=identity.bus_ref,
        host=identity.host,
        vid_pid=identity.vid_pid,
        serial_number=identity.serial_number,
        can_id=identity.can_id,
        address=identity.address,
        strength=identity.strength,
    )


class GraphUnavailable(Exception):
    """``ros2`` exited nonzero listing the graph — a REFUSED command, not an
    empty graph. Collapsing the two used to score every declared device
    ABSENT and hand back a confident false NO-GO for a dead daemon, a wrong
    ``ROS_DOMAIN_ID``, or no ``ros2`` on ``PATH`` (unscored reads
    NOT SCORED with its reason, never a number and never a fault)."""

    def __init__(self, command: tuple[str, ...], returncode: int) -> None:
        self.command = command
        self.returncode = returncode
        super().__init__(f"the graph could not be listed ({' '.join(command)}, exit {returncode})")


def _ros2_lines(runner: CommandRunner, command: tuple[str, ...]) -> list[str]:
    result: CommandResult = runner(command)
    if result.returncode != 0:
        raise GraphUnavailable(command, result.returncode)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@dataclass
class LiveSource:
    """``PreflightSource`` backed by ``scan`` against a live (containerized) target.

    Stage 0/1 run against real infrastructure. ``samples()`` does too
    (live capture): given ``fetch``, it captures for real through ``ros2 bag
    record`` and loads the result with ``load_ros2_bag`` — never
    ``load_recording``, and never a bare refusal. What that load can and
    cannot promise stage 2 is in ``flow.py``'s monotonicity disclosure
    (E3-FL): the loader observes and reports the capture's own receipt order
    rather than asserting one it cannot verify.

    Filesystem enumeration (i2c, device-tree) goes through ``runner`` against
    ``container_fs_root`` — never a host ``Path`` — so stage-0 evidence is a
    fact about the named container, not the machine running the suite.
    """

    contract: Contract
    runner: CommandRunner
    container_fs_root: str = "/var/lockwire"
    robot: str = "live-target"
    # Load-bearing default: local Path would sample the host, not the container.
    # The disclosure belt keys on the recorded access fact — flipping this to
    # ``"local"`` must surface ``not_scored``, never silently score host reads.
    fs_access: Literal["local", "runner"] = "runner"
    # Live bus observation: a declared device/driver the target does not show
    # is a scored absence (violation), not an unseen disclosure.
    scores_absence: ClassVar[bool] = True
    # Capture operands. ``fetch`` pulls the finalized bag directory off the
    # target — ``docker_fetch(container)`` or a local copy — into a Path this
    # process can read with the existing bag reader. ``None`` means samples()
    # cannot be provided at all (the honest refusal, still available).
    fetch: Callable[[str, Path], None] | None = None
    seconds: float = 10.0
    # Evidence, like a ``--from`` file: keep the finalized bag here instead of a
    # temp dir that gets cleaned up.
    keep_dir: Path | None = None
    # Reachability for `kill_container_process` (E3-QC). When True, the capture's
    # single blocking `docker exec` runs on a background thread while this
    # instance polls the SAME runner for the recorder's own pid, so `cancel()`
    # has a corpse to prove before that first call ever returns. Off by default
    # so a caller with no cancel intent keeps issuing exactly the one `ros2 bag
    # record` command — existing tests key on that count. The CLI's `--live`
    # path turns this on unconditionally; it is not a user-facing flag.
    cancellable: bool = False
    # Set once samples() succeeds; read by the CLI for its capture line.
    capture_seconds: float | None = field(default=None, init=False, repr=False)
    capture_dir: Path | None = field(default=None, init=False, repr=False)
    message_count: int = field(default=0, init=False, repr=False)
    # The container-side `ros2 bag record` pid, learned by the poll loop while
    # `cancellable`. `None` until discovered (or forever, if discovery never
    # ran or never saw it) — `cancel()` treats `None` as "nothing to kill".
    _recorder_pid: int | None = field(default=None, init=False, repr=False)
    _capture_target_dir: str | None = field(default=None, init=False, repr=False)
    _capture_active: bool = field(default=False, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _cancel_in_progress: bool = field(default=False, init=False, repr=False)
    _cancel_failed: bool = field(default=False, init=False, repr=False)
    _draft_devices: list[DeviceInstance] = field(default_factory=list, init=False, repr=False)
    _host_side_disclosures: list = field(default_factory=list, init=False, repr=False)
    _capture: Recording | None = field(default=None, init=False, repr=False)
    # A refusal is cached too — a live capture (``ros2 bag record`` for real
    # seconds) must run exactly once per source, success or failure, or the
    # CLI's own samples() call plus run_preflight's first scoring stage would
    # each trigger a fresh recording (double capture time; two attempts that
    # can disagree on a real robot).
    _capture_error: "Exception | None" = field(default=None, init=False, repr=False)

    def cancel(self) -> bool:
        """Stop an in-flight capture by killing the container-side recorder, and
        prove it (E3-QC).

        The reachability half of ``kill_container_process``: the pid this uses
        is the one ``_run_cancellable``'s poll loop discovered through the same
        ``runner`` while the capture's own blocking ``docker exec`` was still in
        flight on a background thread — the only way anything outside that
        thread ever learns it. Cancelling the local ``docker exec`` client on
        its own (``Job.cancel``'s reach) only detaches it; the recorder on the
        other side keeps recording, which is exactly what this closes.

        Returns ``True`` when there is no active capture, or once the exact
        recorder pid is proven gone. During an active capture, failure to find
        that pid or prove its corpse returns ``False``.
        """
        if not self._capture_active:
            return True
        deadline = time.monotonic() + _PID_DISCOVERY_TIMEOUT_S
        while self._recorder_pid is None and time.monotonic() < deadline:
            self._discover_recorder_pid()
            if self._recorder_pid is None:
                time.sleep(_PID_POLL_INTERVAL_S)
        if self._recorder_pid is None:
            self._cancel_failed = True
            return False
        from lockwire_pro.jobs import kill_container_process

        self._cancel_in_progress = True
        try:
            try:
                stopped = kill_container_process(self.runner, self._recorder_pid)
            except Exception:  # transport failure is a failed corpse proof
                self._cancel_failed = True
                return False
            self._cancelled = stopped
            self._cancel_failed = not stopped
            return stopped
        finally:
            self._cancel_in_progress = False

    def host_side_disclosures(self) -> list:
        """Findings for any host-side enumeration that must not score as a bus fact.

        Empty when every filesystem source was read through the runner. Populated
        only if a host Path contribution is detected — callers (stage 0) surface
        these as ``not_scored`` with the §3.1 live-path wording.
        """
        return list(self._host_side_disclosures)

    def enumerate(self) -> list[DeviceObservation]:
        from lockwire.report import Finding

        self._host_side_disclosures.clear()
        # Target is Linux even when the host running lockwire is not (macOS CI/dev).
        # fs_access="runner" is load-bearing: local Path would sample this Mac.
        draft = scan_machine(
            self.robot,
            runner=self.runner,
            fs_root=Path(self.container_fs_root),
            fs_access=self.fs_access,
            platform="linux",
        )
        # Belt: key on the recorded access fact, not on path-shaped detail strings.
        # Absent / unknown fs_access is unsafe — disclose, never treat as runner.
        for name, visibility in draft.sources.items():
            if name not in ("i2c-sysfs", "device-tree"):
                continue
            if visibility.fs_access == "runner":
                continue
            self._host_side_disclosures.append(
                Finding(
                    severity="not_scored",
                    quantity=f"preflight/live/{name}",
                    observed=(
                        f"{name} evidence was not read through the container runner "
                        f"— the live path is untested"
                    ),
                    expected=f"{name} observed inside the named container",
                    action=(
                        "route filesystem enumeration through the container runner; "
                        "do not score host Path reads as bus facts"
                    ),
                )
            )
        self._draft_devices = list(draft.candidate.devices)
        scanned_with_identity = [
            device for device in self._draft_devices if device.identity is not None
        ]
        observations: list[DeviceObservation] = []
        for declared in self.contract.devices:
            if declared.identity is None:
                continue
            match = next(
                (
                    scanned
                    for scanned in scanned_with_identity
                    if scanned.identity is not None
                    and _identity_agrees(
                        declared.identity,
                        _observation_from_scanned(declared.id, scanned),
                    )
                ),
                None,
            )
            if match is None:
                continue
            observations.append(_observation_from_scanned(declared.id, match))
        return observations

    def liveness(self) -> list[LivenessObservation]:
        nodes = {line for line in _ros2_lines(self.runner, ("ros2", "node", "list"))}
        topics = {
            line.split(maxsplit=1)[0]
            for line in _ros2_lines(self.runner, ("ros2", "topic", "list"))
            if line.startswith("/")
        }
        observations: list[LivenessObservation] = []
        for declared in self.contract.devices:
            driver = declared.driver
            if driver is None:
                continue
            node = driver.node
            topic = driver.topic
            # Nothing declared to probe → cannot claim live.
            if node is None and topic is None:
                continue
            # eb-22: a partial observation — each populated operand is scored
            # for its OWN presence rather than collapsing the pair into one
            # all-or-nothing continue. A populated-but-unseen operand sets its
            # `_missing` flag so the stage can word a node miss (real no-go)
            # differently from a topic miss (weak evidence) instead of
            # discarding the whole observation.
            node_seen = node is not None and node in nodes
            topic_seen = topic is not None and topic in topics
            observations.append(
                LivenessObservation(
                    device_id=declared.id,
                    node=node if node_seen else None,
                    topic=topic if topic_seen else None,
                    node_missing=node is not None and not node_seen,
                    topic_missing=topic is not None and not topic_seen,
                )
            )
        return observations

    def samples(self) -> Recording:
        """Record the contract's declared topics for ``seconds`` and read them back.

        For a bounded window, ``ros2 bag record`` runs through the same
        ``runner`` stage 0/1 use, the finalized bag is fetched by ``fetch``,
        and the existing ``load_ros2_bag`` reader turns it into the same
        ``Recording`` shape the offline path uses — so stages 2-4 score a live
        target exactly as they score a file. Cached: the capture happens once
        per ``LiveSource`` instance.
        """
        if self._capture is not None:
            return self._capture
        if self._capture_error is not None:
            raise self._capture_error
        try:
            return self._capture_once()
        except Exception as error:
            self._capture_error = error
            raise

    def _capture_once(self) -> Recording:
        # Deferred: runner imports this module; avoid an import cycle at load.
        from lockwire_pro.preflight.runner import SamplesUnavailable
        from lockwire_pro.ros2bag import PARTIAL_BAG_MARKER, Ros2BagError, load_ros2_bag

        topics = sorted(
            {
                device.driver.topic
                for device in self.contract.devices
                if device.driver is not None and device.driver.topic
            }
        )
        if not topics:
            raise SamplesUnavailable(
                "no device declares a topic; declare driver.topic/msg_type/signals "
                "on the devices you want scored live"
            )
        if self.fetch is None:
            raise SamplesUnavailable(
                "LiveSource has no fetch configured; pass docker_fetch(container) "
                "or lockwire.scan.local_fetch"
            )

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        unique = uuid.uuid4().hex[:8]
        target_dir = f"/tmp/lockwire-live-{stamp}-{unique}"
        self._capture_target_dir = target_dir
        timeout_s = int(self.seconds) + _LIVE_TIMEOUT_MARGIN_S
        command = (
            "timeout",
            "-s",
            "INT",
            str(timeout_s),
            "ros2",
            "bag",
            "record",
            "-s",
            "mcap",
            "-o",
            target_dir,
            *topics,
        )
        if self.cancellable:
            result = self._run_cancellable(command, timeout_s, SamplesUnavailable)
        else:
            result = self.runner(command)
        # A failed kill can finish the recorder later; it is never a proven partial.
        while self._cancel_in_progress:
            time.sleep(_PID_POLL_INTERVAL_S)
        if self._cancel_failed:
            raise SamplesUnavailable("live capture cancellation could not prove recorder stopped")
        if result.returncode not in (0, _TIMEOUT_EXIT_CODE) and not self._cancelled:
            raise SamplesUnavailable(
                f"ros2 bag record failed (exit {result.returncode}): "
                f"{result.stderr.strip() or 'no stderr'}"
            )

        if self.keep_dir is not None:
            local_dir = self.keep_dir
        else:
            # A unique path, not a unique DIRECTORY: ``mkdtemp`` both reserves the
            # name and creates it, but ``fetch`` (``docker cp`` in particular) must
            # see a destination that does not yet exist — an existing directory
            # makes ``docker cp`` nest ``target_dir`` one level inside it instead
            # of copying its contents, and the recording becomes invisible to
            # ``load_ros2_bag``'s top-level-only glob. Reserve the unique name,
            # then remove the now-empty directory so ``fetch`` creates it fresh.
            local_dir = Path(tempfile.mkdtemp(prefix="lockwire-live-"))
            local_dir.rmdir()
        try:
            self.fetch(target_dir, local_dir)
        except OSError as error:
            raise SamplesUnavailable(f"live bag fetch failed: {error}") from error

        if self._cancelled:
            # Keep the corpse-proved partial status with a retained directory.
            (local_dir / "lockwire-partial.json").write_bytes(PARTIAL_BAG_MARKER)

        try:
            recording = load_ros2_bag(local_dir, self.contract, partial=self._cancelled)
        except Ros2BagError as error:
            raise SamplesUnavailable(
                f"captured 0 messages in {self.seconds:g} s on {', '.join(topics)} "
                f"— is the driver publishing? ({error})"
            ) from error

        # A successful load_ros2_bag already guarantees at least one message per
        # collected device (Ros2BagError above is the honest "0 messages" case);
        # this is the presence-floor count for the CLI line and capture metadata.
        message_count = sum(len(stream.recv_times) for stream in recording.devices.values())

        if not self._cancelled:  # a cut-short recorder was not open for the whole window
            recording.capture_window_s = float(self.seconds)
        self.capture_seconds = self.seconds
        self.capture_dir = local_dir
        self.message_count = message_count
        self._capture = recording
        return recording

    def _discover_recorder_pid(self) -> None:
        """Find this capture's recorder, excluding its timeout/shell wrappers."""
        if self._capture_target_dir is None:
            return
        # -ww: procps cuts `args` at $COLUMNS, which can drop the target dir matched below.
        processes = self.runner(("ps", "-ww", "-eo", "pid=,args="))
        if processes.returncode != 0:
            return
        for line in processes.stdout.splitlines():
            fields = line.strip().split(maxsplit=1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            try:
                argv = shlex.split(fields[1])
            except ValueError:
                continue
            ros2_at = next(
                (index for index, value in enumerate(argv) if Path(value).name == "ros2"), None
            )
            if ros2_at not in (0, 1):
                continue
            if ros2_at == 1 and not Path(argv[0]).name.startswith("python"):
                continue
            if [Path(value).name for value in argv[ros2_at : ros2_at + 3]] != [
                "ros2",
                "bag",
                "record",
            ]:
                continue
            if self._capture_target_dir not in argv:
                continue
            self._recorder_pid = int(fields[0])
            return

    def _run_cancellable(
        self, command: tuple[str, ...], timeout_s: int, unavailable: type[Exception]
    ) -> CommandResult:
        """Run ``command`` (the blocking ``ros2 bag record``) on a background
        thread while this thread polls the same ``runner`` for its pid (E3-QC).

        ``self.runner(command)`` alone blocks for the whole capture window —
        there is no way to learn the recorder's container-side pid from inside
        that one call while it is still running. This is the concurrency
        `cancel()` needs: the record command and the pid probe are two
        independent ``docker exec`` sessions running at once, both through the
        same ``runner``, so neither blocks the other.
        """
        box: dict[str, Any] = {}

        def _record() -> None:
            try:
                box["result"] = self.runner(command)
            except Exception as error:  # noqa: BLE001 - re-raised on the caller thread
                box["error"] = error

        thread = threading.Thread(target=_record, daemon=True)
        self._capture_active = True
        thread.start()
        deadline = time.monotonic() + timeout_s
        while thread.is_alive() and self._recorder_pid is None and time.monotonic() < deadline:
            self._discover_recorder_pid()
            if self._recorder_pid is not None:
                break
            time.sleep(_PID_POLL_INTERVAL_S)
        # Generous join cap: the record thread has its own `timeout` bound
        # (`timeout_s`, already the seconds+margin the child obeys); this only
        # guards against a runner double that never returns at all.
        try:
            thread.join(timeout=timeout_s + _LIVE_TIMEOUT_MARGIN_S + 5)
            if thread.is_alive():
                raise unavailable("ros2 bag record did not finish (runner call still running)")
        finally:
            self._capture_active = False
        if "error" in box:
            raise box["error"]
        return box["result"]
