from pathlib import Path
import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML
from typer.testing import CliRunner
from lockwire.cli import app
from lockwire.scan import (
    CommandResult,
    ScanCandidate,
    UnsupportedPlatformError,
    docker_runner,
    scan_machine,
    subprocess_runner,
)
from lockwire.schema import Contract
from conftest import plain


class FixtureRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> CommandResult:
        self.calls.append(command)
        return self.results.get(command, CommandResult(127, stderr="fixture command absent"))


def _populated_tree(root: Path) -> None:
    i2c = root / "sys/bus/i2c/devices"
    (i2c / "1-0068").mkdir(parents=True)
    (i2c / "1-0068/name").write_text("bmi088\n")
    (i2c / "2-0077").mkdir()
    (i2c / "2-0077/name").write_text("ms5611\n")

    peripheral = root / "proc/device-tree/soc/serial@7e201000"
    peripheral.mkdir(parents=True)
    (peripheral / "compatible").write_bytes(b"brcm,bcm2835-aux-uart\0brcm,bcm2835-uart\0")


def test_populated_synthetic_machine_produces_distinct_scan_candidate(tmp_path):
    _populated_tree(tmp_path)
    runner = FixtureRunner(
        {
            ("lsusb",): CommandResult(
                0,
                "Bus 001 Device 004: ID 046d:0825 Logitech, Inc. Webcam C270\n"
                "Bus 001 Device 005: ID 10c4:ea60 Silicon Labs CP210x UART Bridge\n",
            ),
            ("ip", "-j", "link"): CommandResult(
                0,
                '[{"ifname":"lo","link_type":"loopback"},'
                '{"ifname":"eth0","link_type":"ether"},'
                '{"ifname":"can0","link_type":"none","linkinfo":{"info_kind":"can"}}]',
            ),
            ("v4l2-ctl", "--list-devices"): CommandResult(
                0, "USB Camera (usb-0000:00:14.0-3):\n\t/dev/video0\n"
            ),
            ("ros2", "node", "list"): CommandResult(0, "/camera\n/state_publisher\n"),
        }
    )

    draft = scan_machine("testbot", runner=runner, fs_root=tmp_path, platform="linux")

    assert isinstance(draft.candidate, ScanCandidate)
    with pytest.raises(ValidationError):
        Contract.model_validate(draft.candidate.model_dump())
    assert draft.complete is False
    assert {name: visibility.status for name, visibility in draft.sources.items()} == {
        "lsusb": "observed",
        "ip-link": "observed",
        "i2c-sysfs": "observed",
        "device-tree": "observed",
        "v4l2": "observed",
        "ros2-nodes": "observed",
    }
    assert {bus.kind for bus in draft.candidate.topology.buses} == {
        "usb",
        "ethernet",
        "can",
        "i2c",
    }
    assert {device.identity.address for device in draft.candidate.devices if device.identity} >= {
        "0x68",
        "0x77",
    }
    assert {device.driver.node for device in draft.candidate.devices if device.driver} == {
        "/camera",
        "/state_publisher",
    }
    assert runner.calls == [
        ("lsusb",),
        ("ip", "-j", "link"),
        ("v4l2-ctl", "--list-devices"),
        ("ros2", "node", "list"),
    ]

    # Neither the candidate nor its wrapper directly validates as a Contract.
    with pytest.raises(ValidationError):
        Contract.model_validate(draft.model_dump())


def test_route_d_contract_is_refused_for_unresolved_owner(tmp_path):
    runner = FixtureRunner(
        {
            ("lsusb",): CommandResult(
                0,
                "Bus 001 Device 002: ID 046d:c52b Logitech, Inc. Unifying Receiver\n",
            ),
            ("ip", "-j", "link"): CommandResult(0, "[]"),
            ("v4l2-ctl", "--list-devices"): CommandResult(0),
            ("ros2", "node", "list"): CommandResult(0),
        }
    )
    draft = scan_machine("draftbot", runner=runner, fs_root=tmp_path, platform="linux")
    candidate = draft.candidate

    # Route D: copy the candidate's contract fields directly. The marker is never
    # serialized, removed, forged, or passed to Contract.
    contract = Contract(
        schema_version=candidate.schema_version,
        robot=candidate.robot,
        families=candidate.families,
        device_models=candidate.device_models,
        devices=candidate.devices,
        topology=candidate.topology,
    )
    contract_path = tmp_path / "route-d-contract.yaml"
    with contract_path.open("w") as output:
        YAML().dump(
            contract.model_dump(mode="json", exclude_none=True),
            output,
        )

    result = CliRunner().invoke(app, ["check", str(contract_path)])

    assert result.exit_code == 1
    plain_output = plain(result.output)
    assert "NO-GO" in plain_output
    assert "1 violation(s)" in plain_output
    assert "devices/dev.usb.logitech-inc-unifying-receiver/owner" in plain_output
    assert "UNRESOLVED" in plain_output


def test_sparse_machine_records_every_blind_spot(tmp_path):
    runner = FixtureRunner({})

    draft = scan_machine("sparsebot", runner=runner, fs_root=tmp_path, platform="linux")

    assert isinstance(draft.candidate, ScanCandidate)
    assert all(source.status == "unavailable" for source in draft.sources.values())
    assert all(source.detail for source in draft.sources.values())
    assert draft.candidate.devices == []
    assert draft.candidate.topology.buses == []


def test_runner_fs_access_reads_i2c_via_commands_not_host_path(tmp_path):
    """fs_access='runner' must ls/cat through the runner — never Path.iterdir."""
    planted = tmp_path / "sys/bus/i2c/devices/1-0068"
    planted.mkdir(parents=True)
    (planted / "name").write_text("bmi088\n")
    # Deliberately NOT creating the tree where Path would look if mis-routed.
    root_str = "/var/lockwire"
    runner = FixtureRunner(
        {
            ("lsusb",): CommandResult(0),
            ("ip", "-j", "link"): CommandResult(0, "[]"),
            ("ls", "-1", f"{root_str}/sys/bus/i2c/devices"): CommandResult(0, "1-0068\n"),
            ("cat", f"{root_str}/sys/bus/i2c/devices/1-0068/name"): CommandResult(0, "bmi088\n"),
            ("find", f"{root_str}/proc/device-tree", "-name", "compatible", "-type", "f"): (
                CommandResult(1, stderr="absent")
            ),
            ("v4l2-ctl", "--list-devices"): CommandResult(0),
            ("ros2", "node", "list"): CommandResult(0),
        }
    )

    draft = scan_machine(
        "runnerbot",
        runner=runner,
        fs_root=Path(root_str),
        fs_access="runner",
        platform="linux",
    )

    assert draft.sources["i2c-sysfs"].status == "observed"
    assert draft.sources["i2c-sysfs"].items == 1
    assert draft.sources["i2c-sysfs"].fs_access == "runner"
    assert draft.sources["device-tree"].status == "unavailable"
    assert draft.sources["device-tree"].fs_access == "runner"
    assert {d.identity.address for d in draft.candidate.devices if d.identity} == {"0x68"}
    assert ("ls", "-1", f"{root_str}/sys/bus/i2c/devices") in runner.calls
    assert ("cat", f"{root_str}/sys/bus/i2c/devices/1-0068/name") in runner.calls


def test_local_fs_access_records_local_on_filesystem_sources(tmp_path):
    """Path-backed i2c/device-tree must record fs_access='local' on both branches."""
    _populated_tree(tmp_path)
    runner = FixtureRunner(
        {
            ("lsusb",): CommandResult(0),
            ("ip", "-j", "link"): CommandResult(0, "[]"),
            ("v4l2-ctl", "--list-devices"): CommandResult(0),
            ("ros2", "node", "list"): CommandResult(0),
        }
    )

    draft = scan_machine(
        "localbot", runner=runner, fs_root=tmp_path, fs_access="local", platform="linux"
    )

    assert draft.sources["i2c-sysfs"].status == "observed"
    assert draft.sources["i2c-sysfs"].fs_access == "local"
    assert draft.sources["device-tree"].status == "observed"
    assert draft.sources["device-tree"].fs_access == "local"
    # Non-filesystem sources leave the access fact unset.
    assert draft.sources["lsusb"].fs_access is None


def test_pre_b111_source_visibility_without_fs_access_still_loads():
    """Artifact compat: an older visibility dict (no fs_access) still validates."""
    from lockwire.scan import SourceVisibility

    visibility = SourceVisibility.model_validate(
        {"status": "observed", "items": 1, "detail": "/var/lockwire/sys/bus/i2c/devices"}
    )
    assert visibility.fs_access is None
    assert visibility.status == "observed"

    # Full draft wrapper round-trip with older source shapes.
    from lockwire.scan import ScanDraft

    draft = ScanDraft.model_validate(
        {
            "artifact_type": "lockwire-scan-draft",
            "complete": False,
            "sources": {
                name: {"status": "unavailable", "items": 0, "detail": "absent"}
                for name in (
                    "lsusb",
                    "ip-link",
                    "i2c-sysfs",
                    "device-tree",
                    "v4l2",
                    "ros2-nodes",
                )
            },
            "candidate": {
                "artifact_type": "lockwire-scan-candidate",
                "schema_version": "0.2",
                "robot": {"name": "oldbot"},
                "families": [],
                "device_models": [],
                "devices": [],
                "topology": {"hosts": [{"id": "host.local"}], "buses": []},
            },
        }
    )
    assert draft.sources["i2c-sysfs"].fs_access is None
    assert draft.candidate.artifact_type == "lockwire-scan-candidate"


def test_truncated_ip_json_is_not_promoted_to_confident_fields(tmp_path):
    runner = FixtureRunner(
        {
            ("lsusb",): CommandResult(0),
            ("ip", "-j", "link"): CommandResult(0, '[{"ifname":"eth0"'),
            ("v4l2-ctl", "--list-devices"): CommandResult(0),
            ("ros2", "node", "list"): CommandResult(0),
        }
    )

    draft = scan_machine("garbagebot", runner=runner, fs_root=tmp_path, platform="linux")

    assert draft.sources["ip-link"].status == "malformed"
    assert draft.sources["ip-link"].items == 0
    assert draft.candidate.topology.buses == []


def test_unsupported_platform_fails_before_enumeration(tmp_path):
    runner = FixtureRunner({})

    with pytest.raises(UnsupportedPlatformError, match=r"Linux \+ ROS 2 only.*darwin"):
        scan_machine("mac", runner=runner, fs_root=tmp_path, platform="darwin")

    assert runner.calls == []


def test_cli_registers_scan_and_fails_cleanly_when_unsupported(monkeypatch):
    def unsupported(_robot):
        raise UnsupportedPlatformError("Linux + ROS 2 only; fixture platform")

    monkeypatch.setattr("lockwire.cli.scan_machine", unsupported)

    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 1
    plain_output = plain(result.output)
    assert "scan failed" in plain_output
    assert "Linux + ROS 2 only" in plain_output


# --- Real subprocess_runner (no mocks): shapes scan_machine callers depend on ---


def test_subprocess_runner_success_returns_stdout_and_zero():
    result = subprocess_runner(("/bin/echo", "lockwire-scan-ok"))

    assert isinstance(result, CommandResult)
    assert result.returncode == 0
    assert result.stdout == "lockwire-scan-ok\n"
    assert result.stderr == ""


def test_subprocess_runner_nonzero_exit_preserves_returncode():
    # argv form — runner never invokes a shell itself.
    result = subprocess_runner(("sh", "-c", "exit 3"))

    assert isinstance(result, CommandResult)
    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr == ""


def test_subprocess_runner_missing_binary_returns_127():
    missing = "/nonexistent/lockwire/b80/missing-binary"
    result = subprocess_runner((missing,))

    assert isinstance(result, CommandResult)
    assert result.returncode == 127
    assert missing in result.stderr
    assert result.stdout == ""


def test_subprocess_runner_timeout_returns_1():
    # Real hang: sleep outlives a short injected timeout (default remains 10).
    result = subprocess_runner(("sh", "-c", "sleep 5"), timeout=0.3)

    assert isinstance(result, CommandResult)
    assert result.returncode == 1
    assert "timed out" in result.stderr.lower()
    assert result.stdout == ""


def test_docker_runner_rejects_bad_ros_distro():
    # A crafted --ros-distro must never reach the bash -lc string built in
    # docker_runner's inner closure (scan.py's `run`) — it should be refused
    # at construction time, before any docker exec.
    with pytest.raises(ValueError, match="ros-distro"):
        docker_runner("some-container", ros_distro="jazzy && touch /tmp/pwn")


def _captured_inner_string(monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...]) -> str:
    """Run ``command`` through ``docker_runner`` and return the ``bash -lc`` inner string.

    ``docker_runner`` only sourced the ROS distro when ``command[0] ==
    "ros2"``, so a record command wrapped in ``timeout`` (``command[0] ==
    "timeout"``) never found ``ros2`` on ``PATH`` inside the container and died
    at exit 127 — the live path was silently dead for every wrapped command.
    """
    calls: list[tuple] = []

    def fake_run(docker_cmd, **kwargs):
        calls.append(docker_cmd)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    import lockwire.scan as scan_module

    monkeypatch.setattr(scan_module.subprocess, "run", fake_run)
    runner = docker_runner("some-container")
    runner(command)
    assert len(calls) == 1
    docker_cmd = calls[0]
    assert docker_cmd[:5] == ("docker", "exec", "some-container", "bash", "-lc")
    return docker_cmd[5]


def test_docker_runner_does_not_source_ros_distro_for_a_non_ros_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Substring hazard: a path/argument that merely mentions "ros2" must not
    # trigger sourcing — only an actual "ros2" token does.
    inner = _captured_inner_string(monkeypatch, ("lsusb",))

    assert not inner.startswith("source ")
    assert "source" not in inner

    inner_path_lookalike = _captured_inner_string(monkeypatch, ("cat", "/opt/ros2-tools/version"))
    assert "source" not in inner_path_lookalike
