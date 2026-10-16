"""Read-only Linux/ROS 2 enumeration into a non-authoritative draft artifact."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lockwire.cli_line import cli_line
from lockwire.schema import (
    SCHEMA_VERSION,
    Bus,
    DeviceFamily,
    DeviceIdentity,
    DeviceInstance,
    DeviceModel,
    Driver,
    Host,
    RobotInfo,
    Topology,
)

SOURCE_NAMES = ("lsusb", "ip-link", "i2c-sysfs", "device-tree", "v4l2", "ros2-nodes")
_USB_LINE = re.compile(
    r"^Bus \d+ Device \d+: ID (?P<vid_pid>[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})(?:\s+(?P<label>.*))?$"
)
_I2C_ENTRY = re.compile(r"^(?P<bus>\d+)-(?P<address>[0-9A-Fa-f]{4})$")
_ROS_DISTRO = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[tuple[str, ...]], CommandResult]


class SourceVisibility(BaseModel):
    """Whether a source was usable; unavailable and malformed are normal draft states.

    ``fs_access`` records *how* a filesystem source was read (i2c / device-tree).
    ``None`` means unknown — callers that refuse host-side facts must treat it as
    unsafe, never as an implicit runner read. Absent from older draft artifacts;
    default keeps those loadable under ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["observed", "unavailable", "malformed"]
    items: int = 0
    detail: str | None = None
    fs_access: Literal["local", "runner"] | None = None


class ScanCandidate(BaseModel):
    """Scanned observations shaped for review, never an authoritative Contract."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: Literal["lockwire-scan-candidate"]
    schema_version: str = SCHEMA_VERSION
    robot: RobotInfo
    families: list[DeviceFamily]
    device_models: list[DeviceModel]
    devices: list[DeviceInstance]
    topology: Topology


class ScanDraft(BaseModel):
    """Scan evidence and its non-authoritative candidate."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: Literal["lockwire-scan-draft"] = "lockwire-scan-draft"
    complete: Literal[False] = False
    sources: dict[str, SourceVisibility]
    candidate: ScanCandidate


class UnsupportedPlatformError(RuntimeError):
    pass


def subprocess_runner(command: tuple[str, ...], *, timeout: float = 10) -> CommandResult:
    """Run a fixed read-only enumeration command without invoking a shell."""

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return CommandResult(127, stderr=str(error))
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(1, stderr=str(error))
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def docker_runner(
    container: str, *, timeout: float = 10, ros_distro: str = "jazzy"
) -> CommandRunner:
    """Run scan commands inside a container via ``docker exec``.

    Same ``CommandResult`` shape and timeout/OSError behaviour as
    ``subprocess_runner``. ROS 2 commands are prefixed with the distro setup
    script (``bash -lc 'source /opt/ros/<distro>/setup.bash && …'``); other
    commands still run under ``bash -lc`` so a missing binary surfaces as a
    non-zero exit (scan's ``_unavailable`` path), not a local ``FileNotFoundError``.

    ``ros_distro`` is spliced unquoted into the ``bash -lc`` string (it names a
    path segment, ``/opt/ros/<distro>/setup.bash``, so quoting alone would not
    stop a ``../`` traversal); it must match a plain distro-name charset or this
    raises ``ValueError`` before any command runs.
    """
    if not _ROS_DISTRO.match(ros_distro):
        raise ValueError(f"invalid --ros-distro {ros_distro!r}: must match {_ROS_DISTRO.pattern}")

    def run(command: tuple[str, ...]) -> CommandResult:
        quoted = cli_line(*command)
        if "ros2" in command:
            inner = f"source /opt/ros/{ros_distro}/setup.bash && {quoted}"
        else:
            inner = quoted
        docker_cmd = ("docker", "exec", container, "bash", "-lc", inner)
        try:
            completed = subprocess.run(
                docker_cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            return CommandResult(127, stderr=str(error))
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(1, stderr=str(error))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    return run


Fetch = Callable[[str, Path], None]


def _ssh_error_detail(detail: str, identity_file: Path, known_hosts: Path) -> str:
    """Do not return credential-file locations from ssh/scp diagnostics."""
    return detail.replace(str(identity_file), "<ssh-key-file>").replace(
        str(known_hosts), "<ssh-known-hosts-file>"
    )


def ssh_runner(
    host: str,
    *,
    port: int,
    identity_file: Path,
    known_hosts: Path,
    user: str = "root",
    timeout: float = 10,
    ros_distro: str = "jazzy",
) -> CommandRunner:
    """Run the fixed live-capture commands over a host-verified SSH session.

    This is the transport counterpart to :func:`docker_runner`: command tuples
    stay command tuples until ``cli_line`` quotes the remote ``bash -lc`` input.
    The caller supplies the target's known-hosts file; this helper never disables
    host-key verification merely to make a local fixture convenient.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError(f"invalid SSH host {host!r}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user):
        raise ValueError(f"invalid SSH user {user!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid SSH port {port!r}")
    if not _ROS_DISTRO.match(ros_distro):
        raise ValueError(f"invalid --ros-distro {ros_distro!r}: must match {_ROS_DISTRO.pattern}")

    def run(command: tuple[str, ...]) -> CommandResult:
        inner = cli_line(*command)
        if "ros2" in command:
            inner = f"source /opt/ros/{ros_distro}/setup.bash && {inner}"
        argv = (
            "ssh",
            "-T",
            "-p",
            str(port),
            "-i",
            str(identity_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={max(1, int(timeout))}",
            "--",
            f"{user}@{host}",
            cli_line("bash", "-lc", inner),
        )
        try:
            completed = subprocess.run(
                argv, check=False, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError as error:
            return CommandResult(
                127, stderr=_ssh_error_detail(str(error), identity_file, known_hosts)
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(
                1, stderr=_ssh_error_detail(str(error), identity_file, known_hosts)
            )
        return CommandResult(
            completed.returncode,
            completed.stdout,
            _ssh_error_detail(completed.stderr, identity_file, known_hosts),
        )

    return run


def _is_live_capture_dir(remote_dir: str) -> bool:
    """Accept only non-traversing paths produced by the live recorder."""
    return (
        remote_dir.startswith("/tmp/lockwire-live-")
        and re.fullmatch(r"[A-Za-z0-9._/-]+", remote_dir) is not None
        and all(part not in {".", ".."} for part in remote_dir.split("/"))
    )


def _ssh_bag_manifest(runner: CommandRunner, remote_dir: str) -> dict[str, tuple[str, int]]:
    """Read the finalized bag identity from the verified SSH target."""
    entries = runner(
        ("find", remote_dir, "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\t%y\n")
    )
    if entries.returncode != 0:
        raise OSError(f"remote bag listing failed: {entries.stderr.strip()}")
    manifest: dict[str, tuple[str, int]] = {}
    for line in entries.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[1] != "f" or not re.fullmatch(r"[A-Za-z0-9._-]+", fields[0]):
            raise OSError("remote bag contains an unsafe or non-file entry")
        name = fields[0]
        if name in manifest:
            raise OSError("remote bag contains duplicate names")
        digest = runner(("sha256sum", f"{remote_dir}/{name}"))
        size = runner(("stat", "-c", "%s", f"{remote_dir}/{name}"))
        if digest.returncode != 0 or size.returncode != 0:
            raise OSError(f"remote bag identity unavailable for {name}")
        sha = digest.stdout.split(maxsplit=1)[0] if digest.stdout.strip() else ""
        raw_size = size.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not raw_size.isdigit():
            raise OSError(f"remote bag identity malformed for {name}")
        manifest[name] = (sha, int(raw_size))
    if "metadata.yaml" not in manifest or not any(name.endswith(".mcap") for name in manifest):
        raise OSError("remote bag is missing metadata.yaml or MCAP data")
    return manifest


def ssh_fetch(
    host: str,
    *,
    port: int,
    identity_file: Path,
    known_hosts: Path,
    remote_dir: str,
    local_dir: Path,
    user: str = "root",
    timeout: float = 60,
) -> None:
    """Fetch one finalized SSH live-capture directory with host verification."""
    if not _is_live_capture_dir(remote_dir):
        raise ValueError(f"invalid live capture directory {remote_dir!r}")
    runner = ssh_runner(
        host,
        port=port,
        identity_file=identity_file,
        known_hosts=known_hosts,
        user=user,
        timeout=timeout,
    )
    # The SSH host key anchors the expected source bytes before transfer.
    expected = _ssh_bag_manifest(runner, remote_dir)
    argv = (
        "scp",
        "-q",
        "-r",
        "-P",
        str(port),
        "-i",
        str(identity_file),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        f"{user}@{host}:{remote_dir}",
        str(local_dir),
    )
    try:
        completed = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        raise OSError(
            f"scp fetch failed: {_ssh_error_detail(str(error), identity_file, known_hosts)}"
        ) from None
    if completed.returncode != 0:
        detail = (
            _ssh_error_detail(completed.stderr.strip(), identity_file, known_hosts)
            or f"scp exited {completed.returncode}"
        )
        raise OSError(f"scp fetch {host}:{remote_dir}: {detail}")
    if _ssh_bag_manifest(runner, remote_dir) != expected:
        raise OSError("remote bag changed during SSH transfer")
    if not local_dir.is_dir():
        raise OSError("scp did not create a local bag directory")
    local_names = {item.name for item in local_dir.iterdir()}
    if local_names != set(expected):
        raise OSError("fetched bag file set differs from remote source")
    for name, (digest, size) in expected.items():
        item = local_dir / name
        if not item.is_file() or item.is_symlink() or item.stat().st_size != size:
            raise OSError(f"fetched bag file differs from remote source: {name}")
        actual = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise OSError(f"fetched bag digest differs from remote source: {name}")


def docker_fetch(container: str, *, timeout: float = 60) -> Fetch:
    """``docker cp`` a directory out of ``container`` into a local path.

    Same error posture as ``docker_runner``: a failure raises rather than
    returning a silently-empty directory, so a capture that never left the
    container cannot be mistaken for an empty recording.
    """

    def fetch(remote_dir: str, local_dir: Path) -> None:
        completed = subprocess.run(
            ("docker", "cp", f"{container}:{remote_dir}", str(local_dir)),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"docker cp exited {completed.returncode}"
            raise OSError(f"docker cp {container}:{remote_dir} {local_dir}: {detail}")

    return fetch


def local_fetch(remote_dir: str, local_dir: Path) -> None:
    """Copy a capture directory that already sits on the machine lockwire runs on."""
    import shutil

    shutil.copytree(remote_dir, local_dir)


def scan_machine(
    robot: str,
    *,
    runner: CommandRunner = subprocess_runner,
    fs_root: Path = Path("/"),
    fs_access: Literal["local", "runner"] = "local",
    platform: str = sys.platform,
) -> ScanDraft:
    """Enumerate a Linux machine; command execution and filesystem root are injectable.

    ``fs_access="local"`` reads i2c/device-tree via ``Path`` under ``fs_root``
    (host-side). ``fs_access="runner"`` reads the same trees by executing
    ``ls``/``cat``/``find`` through ``runner`` — required when the target is a
    container and host Path would silently sample the wrong machine.
    """

    if not platform.startswith("linux"):
        raise UnsupportedPlatformError(
            f"lockwire scan supports Linux + ROS 2 only; current platform is {platform!r}"
        )

    builder = _DraftBuilder(robot)
    _scan_lsusb(builder, runner)
    _scan_ip_link(builder, runner)
    if fs_access == "runner":
        _scan_i2c_via_runner(builder, runner, fs_root)
        _scan_device_tree_via_runner(builder, runner, fs_root)
    else:
        _scan_i2c(builder, fs_root)
        _scan_device_tree(builder, fs_root)
    _scan_v4l2(builder, runner)
    _scan_ros2_nodes(builder, runner)
    return builder.build()


class _DraftBuilder:
    def __init__(self, robot: str) -> None:
        self.robot = robot
        self.host_id = "host.local"
        self.families: dict[str, DeviceFamily] = {}
        self.models: dict[str, DeviceModel] = {}
        self.devices: list[DeviceInstance] = []
        self.buses: dict[str, Bus] = {}
        self.sources: dict[str, SourceVisibility] = {}
        self._ids: dict[str, int] = {}

    def family(self, key: str, name: str) -> str:
        family_id = f"fam.{_slug(key)}"
        self.families.setdefault(family_id, DeviceFamily(id=family_id, name=name))
        return family_id

    def model(self, namespace: str, key: str, family: str) -> str:
        model_id = f"mdl.{_slug(namespace)}.{_slug(key)}"
        self.models.setdefault(model_id, DeviceModel(id=model_id, family=family))
        return model_id

    def device_id(self, namespace: str, key: str) -> str:
        base = f"dev.{_slug(namespace)}.{_slug(key)}"
        count = self._ids.get(base, 0) + 1
        self._ids[base] = count
        return base if count == 1 else f"{base}.{count}"

    def bus(
        self, bus_id: str, kind: Literal["usb", "ethernet", "serial", "can", "i2c", "spi"]
    ) -> str:
        self.buses.setdefault(bus_id, Bus(id=bus_id, kind=kind, host=self.host_id))
        return bus_id

    def visibility(
        self,
        source: str,
        status: Literal["observed", "unavailable", "malformed"],
        items: int = 0,
        detail: str | None = None,
        fs_access: Literal["local", "runner"] | None = None,
    ) -> None:
        self.sources[source] = SourceVisibility(
            status=status, items=items, detail=detail, fs_access=fs_access
        )

    def build(self) -> ScanDraft:
        candidate = ScanCandidate(
            artifact_type="lockwire-scan-candidate",
            robot=RobotInfo(name=self.robot),
            families=list(self.families.values()),
            device_models=list(self.models.values()),
            devices=self.devices,
            topology=Topology(
                hosts=[Host(id=self.host_id)],
                buses=list(self.buses.values()),
            ),
        )
        return ScanDraft(
            sources={name: self.sources[name] for name in SOURCE_NAMES},
            candidate=candidate,
        )


def _run(runner: CommandRunner, command: tuple[str, ...]) -> CommandResult:
    try:
        return runner(command)
    except (FileNotFoundError, OSError) as error:
        return CommandResult(127, stderr=str(error))


def _unavailable(builder: _DraftBuilder, source: str, result: CommandResult) -> bool:
    if result.returncode == 0:
        return False
    detail = result.stderr.strip() or f"command exited {result.returncode}"
    builder.visibility(source, "unavailable", detail=detail)
    return True


def _scan_lsusb(builder: _DraftBuilder, runner: CommandRunner) -> None:
    result = _run(runner, ("lsusb",))
    if _unavailable(builder, "lsusb", result):
        return
    matches = [_USB_LINE.fullmatch(line.strip()) for line in result.stdout.splitlines()]
    parsed = [match for match in matches if match is not None]
    if any(match is None for match in matches):
        builder.visibility("lsusb", "malformed", detail="one or more unparseable lsusb rows")
        return

    bus_ref = builder.bus("bus.usb", "usb") if parsed else None
    family = builder.family("usb-device", "USB device") if parsed else None
    for match in parsed:
        vid_pid = match.group("vid_pid").lower()
        label = match.group("label") or vid_pid
        model = builder.model("usb", vid_pid, family)
        builder.devices.append(
            DeviceInstance(
                id=builder.device_id("usb", label),
                model=model,
                identity=DeviceIdentity(
                    bus_ref=bus_ref,
                    host=builder.host_id,
                    vid_pid=vid_pid,
                ),
                owner="UNRESOLVED",
            )
        )
    builder.visibility("lsusb", "observed", len(parsed))


def _scan_ip_link(builder: _DraftBuilder, runner: CommandRunner) -> None:
    result = _run(runner, ("ip", "-j", "link"))
    if _unavailable(builder, "ip-link", result):
        return
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError("top-level JSON is not a list")
    except (json.JSONDecodeError, ValueError) as error:
        builder.visibility("ip-link", "malformed", detail=str(error))
        return

    count = 0
    ignored = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("ifname"), str):
            builder.visibility("ip-link", "malformed", detail="link row missing string ifname")
            return
        if row.get("link_type") == "loopback":
            continue
        info = row.get("linkinfo")
        if isinstance(info, dict) and info.get("info_kind") == "can":
            kind = "can"
        elif row.get("link_type") == "ether":
            kind = "ethernet"
        else:
            ignored += 1
            continue
        builder.bus(f"bus.net.{_slug(row['ifname'])}", kind)
        count += 1
    detail = f"{ignored} unsupported interface(s) omitted" if ignored else None
    builder.visibility("ip-link", "observed", count, detail)


def _rooted(fs_root: Path, absolute: str) -> Path:
    return fs_root / absolute.lstrip("/")


def _append_i2c_device(builder: _DraftBuilder, entry_name: str, label: str) -> None:
    match = _I2C_ENTRY.fullmatch(entry_name)
    if match is None:
        return
    family = builder.family("i2c-device", "I2C device")
    bus_ref = builder.bus(f"bus.i2c{match.group('bus')}", "i2c")
    model = builder.model("i2c", label, family)
    builder.devices.append(
        DeviceInstance(
            id=builder.device_id("i2c", label),
            model=model,
            identity=DeviceIdentity(
                bus_ref=bus_ref,
                host=builder.host_id,
                address=hex(int(match.group("address"), 16)),
            ),
            owner="UNRESOLVED",
        )
    )


def _scan_i2c(builder: _DraftBuilder, fs_root: Path) -> None:
    path = _rooted(fs_root, "/sys/bus/i2c/devices")
    if not path.is_dir():
        builder.visibility(
            "i2c-sysfs", "unavailable", detail=f"{path} is absent", fs_access="local"
        )
        return

    count = 0
    try:
        entries = sorted(path.iterdir(), key=lambda entry: entry.name)
    except OSError as error:
        builder.visibility("i2c-sysfs", "unavailable", detail=str(error), fs_access="local")
        return
    for entry in entries:
        if _I2C_ENTRY.fullmatch(entry.name) is None:
            continue
        label = _read_text(entry / "name") or entry.name
        _append_i2c_device(builder, entry.name, label)
        count += 1
    builder.visibility("i2c-sysfs", "observed", count, fs_access="local")


def _scan_i2c_via_runner(builder: _DraftBuilder, runner: CommandRunner, fs_root: Path) -> None:
    """Read i2c sysfs through ``runner`` so a container target is not replaced by the host."""

    path = _rooted(fs_root, "/sys/bus/i2c/devices").as_posix()
    listing = _run(runner, ("ls", "-1", path))
    if listing.returncode != 0:
        detail = listing.stderr.strip() or f"{path} is absent"
        builder.visibility("i2c-sysfs", "unavailable", detail=detail, fs_access="runner")
        return

    count = 0
    for entry_name in sorted(line.strip() for line in listing.stdout.splitlines() if line.strip()):
        if _I2C_ENTRY.fullmatch(entry_name) is None:
            continue
        name_path = f"{path}/{entry_name}/name"
        named = _run(runner, ("cat", name_path))
        label = (
            named.stdout.strip() if named.returncode == 0 and named.stdout.strip() else entry_name
        )
        _append_i2c_device(builder, entry_name, label)
        count += 1
    builder.visibility("i2c-sysfs", "observed", count, fs_access="runner")


def _scan_device_tree(builder: _DraftBuilder, fs_root: Path) -> None:
    path = _rooted(fs_root, "/proc/device-tree")
    if not path.is_dir():
        builder.visibility(
            "device-tree", "unavailable", detail=f"{path} is absent", fs_access="local"
        )
        return

    count = 0
    try:
        compatible_files = sorted(path.rglob("compatible"))
    except OSError as error:
        builder.visibility("device-tree", "unavailable", detail=str(error), fs_access="local")
        return
    for compatible_file in compatible_files:
        try:
            compatible = compatible_file.read_bytes().split(b"\0", 1)[0].decode()
        except (OSError, UnicodeDecodeError):
            continue
        if not compatible:
            continue
        family = builder.family("device-tree-peripheral", "Device-tree peripheral")
        model = builder.model("device-tree", compatible, family)
        relative = compatible_file.parent.relative_to(path).as_posix()
        builder.devices.append(
            DeviceInstance(
                id=builder.device_id("device-tree", relative),
                model=model,
                owner="UNRESOLVED",
            )
        )
        count += 1
    builder.visibility("device-tree", "observed", count, fs_access="local")


def _scan_device_tree_via_runner(
    builder: _DraftBuilder, runner: CommandRunner, fs_root: Path
) -> None:
    """Read device-tree compatibles through ``runner`` (never host Path)."""

    path = _rooted(fs_root, "/proc/device-tree").as_posix()
    listing = _run(runner, ("find", path, "-name", "compatible", "-type", "f"))
    if listing.returncode != 0:
        detail = listing.stderr.strip() or f"{path} is absent"
        builder.visibility("device-tree", "unavailable", detail=detail, fs_access="runner")
        return

    count = 0
    root_prefix = path.rstrip("/") + "/"
    for compatible_path in sorted(
        line.strip() for line in listing.stdout.splitlines() if line.strip()
    ):
        raw = _run(runner, ("cat", compatible_path))
        if raw.returncode != 0 or not raw.stdout:
            # ``cat`` text mode may stop at NUL; fall back to a byte-safe read.
            raw = _run(runner, ("head", "-c", "256", compatible_path))
            if raw.returncode != 0 or not raw.stdout:
                continue
        compatible = raw.stdout.split("\0", 1)[0].strip()
        if not compatible:
            continue
        family = builder.family("device-tree-peripheral", "Device-tree peripheral")
        model = builder.model("device-tree", compatible, family)
        parent = compatible_path.rsplit("/", 1)[0]
        relative = parent[len(root_prefix) :] if parent.startswith(root_prefix) else parent
        builder.devices.append(
            DeviceInstance(
                id=builder.device_id("device-tree", relative or "root"),
                model=model,
                owner="UNRESOLVED",
            )
        )
        count += 1
    builder.visibility("device-tree", "observed", count, fs_access="runner")


def _scan_v4l2(builder: _DraftBuilder, runner: CommandRunner) -> None:
    result = _run(runner, ("v4l2-ctl", "--list-devices"))
    if _unavailable(builder, "v4l2", result):
        return
    labels = [
        line.strip().removesuffix(":")
        for line in result.stdout.splitlines()
        if line and not line[0].isspace() and line.rstrip().endswith(":")
    ]
    if result.stdout.strip() and not labels:
        builder.visibility("v4l2", "malformed", detail="no parseable v4l2 device groups")
        return

    for label in labels:
        family = builder.family("camera", "Camera")
        model = builder.model("v4l2", label, family)
        builder.devices.append(
            DeviceInstance(
                id=builder.device_id("v4l2", label),
                model=model,
                owner="UNRESOLVED",
            )
        )
    builder.visibility("v4l2", "observed", len(labels))


def _scan_ros2_nodes(builder: _DraftBuilder, runner: CommandRunner) -> None:
    result = _run(runner, ("ros2", "node", "list"))
    if _unavailable(builder, "ros2-nodes", result):
        return
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    nodes = [line for line in lines if line.startswith("/")]
    if len(nodes) != len(lines):
        builder.visibility("ros2-nodes", "malformed", detail="non-absolute node name in output")
        return

    for node in nodes:
        family = builder.family("ros2-node", "ROS 2 node")
        model = builder.model("ros2-node", node, family)
        builder.devices.append(
            DeviceInstance(
                id=builder.device_id("ros2-node", node),
                model=model,
                driver=Driver(node=node),
                owner="UNRESOLVED",
            )
        )
    builder.visibility("ros2-nodes", "observed", len(nodes))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"
