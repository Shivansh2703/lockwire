"""Stages 0/1 against a containerized Linux/ROS 2 target.

Exercises ``LiveSource`` + ``docker_runner`` against the derived
``lockwire-live:b105`` image (pinned ``ros:jazzy-ros-core`` + usbutils/iproute2).
Skip is loud and countable (``LIVE_PATH_UNTESTED``); on this machine Docker is
up and every live case below must EXECUTE, not skip.

Stage-0 device evidence is read **through the runner from the container**
(``fs_access="runner"`` under ``/var/lockwire``). Host Path plants do not clear
an absence — that is the bounce-1 load-bearing claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lockwire_pro.preflight import LiveSource, PreflightSource, run_preflight
from lockwire.report import render
from lockwire_pro.ros2bag import load_ros2_bag
from lockwire.scan import (
    CommandResult,
    docker_runner,
    scan_machine,
    ssh_fetch,
    ssh_runner,
)
from lockwire.schema import load_contract
from lockwire.serve import create_app
from tests.fixtures.live import provision

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT_LIVE = FIXTURES / "contract_live.yaml"
LIVE_DOCKERFILE = FIXTURES / "live" / "Dockerfile"
LIVE_IMAGE = "lockwire-live:b105"

# ---------------------------------------------------------------------------
# Docker availability — skip reason must be explicit and countable.
# ---------------------------------------------------------------------------


def _docker_ready() -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "LIVE_PATH_UNTESTED: docker binary not found"
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"LIVE_PATH_UNTESTED: docker info failed: {error}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown").strip()
        return False, f"LIVE_PATH_UNTESTED: docker info exited {completed.returncode}: {detail}"
    return True, ""


DOCKER_READY, DOCKER_SKIP_REASON = _docker_ready()
requires_docker = pytest.mark.skipif(not DOCKER_READY, reason=DOCKER_SKIP_REASON)


def _exit_code(findings: list) -> int:
    """Mirror ``report.render``'s exit map without printing."""
    return 1 if any(f.severity == "violation" for f in findings) else 0


def _ensure_live_image() -> str:
    """Build ``LIVE_IMAGE`` once; Docker layer cache makes repeats cheap.

    Session fixture calls this once per pytest process — not per test. A no-op
    rebuild against an unchanged Dockerfile resolves from cache in ~1s.

    On CI the image is built and cached by a workflow step BEFORE pytest runs, so
    this call resolves from the local daemon in ~1 s exactly as it does locally.
    The timeout is a backstop, not a budget for a cold pull.

    That distinction was learned wrong first: ``ros:jazzy-ros-core`` is ~758 MB and
    a runner starts each job with an empty layer cache, so this call was paying for
    the whole pull. Supe read one timeout as "marginal" and raised it 300 s → 900 s.
    **It timed out at 900 s too.** The timeout was never the problem — the test was
    doing a network fetch inside a unit-test timeout, and no value fixes that. The
    fix is the workflow cache; this number only catches a genuinely wedged build.

    Deliberately NOT fixed by skipping the container tests on CI. They are the only
    proof the live path works, and a suite that goes green by not running them is
    precisely the defect this milestone exists to kill.
    """
    assert LIVE_DOCKERFILE.is_file(), f"missing {LIVE_DOCKERFILE}"
    completed = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            LIVE_IMAGE,
            "-f",
            str(LIVE_DOCKERFILE),
            str(LIVE_DOCKERFILE.parent),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"docker build {LIVE_IMAGE} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return LIVE_IMAGE


@pytest.fixture(scope="session")
def live_image() -> str:
    if not DOCKER_READY:
        pytest.skip(DOCKER_SKIP_REASON)
    return _ensure_live_image()


def _start_container(image: str) -> str:
    """Start a container isolated from every other DDS participant on the host.

    DDS discovery is UDP multicast on docker's default bridge network, which
    forwards multicast between ALL containers on it — ours across tests, and
    any other process's on the same docker host. Leaving ``ROS_DOMAIN_ID``
    unset means every container defaults to domain 0, so one test's
    ``imu_driver`` node (or a concurrent pytest run's, from a sibling
    worktree sharing this docker daemon) is visible to a container that never
    started one. ``ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`` (Jazzy) confines
    discovery to the container's own network namespace instead — no domain to
    draw, so no collision period and no risk of landing a DDS port
    (7400 + 250*domain) inside the ephemeral range. Verified manually: a
    container started this way still lists its own published topic via
    ``ros2 topic list``, and a second container with the same setting does
    NOT see the first one's topic.
    """
    completed = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-e",
            "ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST",
            image,
            "sleep",
            "600",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def _stop_container(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _start_imu_driver(container_id: str) -> None:
    script_path = "/tmp/imu_driver.py"
    # Write the script into the container, then start it detached.
    put = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_id,
            "bash",
            "-lc",
            f"cat > {script_path}",
        ],
        input=provision.IMU_DRIVER_SCRIPT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert put.returncode == 0
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            container_id,
            "bash",
            "-lc",
            f"source /opt/ros/jazzy/setup.bash && python3 {script_path}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Wait until the node is visible.
    runner = docker_runner(container_id)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = runner(("ros2", "node", "list"))
        if "/imu_driver" in result.stdout:
            # Topic may lag the node slightly.
            topics = runner(("ros2", "topic", "list"))
            if "/imu/data" in topics.stdout:
                return
        time.sleep(0.5)
    raise AssertionError("imu_driver did not appear in ros2 node/topic list within 30s")


@pytest.fixture
def ros_container(live_image: str):
    """A fresh lockwire-live:b105 container; always removed afterwards."""
    if not DOCKER_READY:
        pytest.skip(DOCKER_SKIP_REASON)
    container_id = _start_container(live_image)
    try:
        yield container_id
    finally:
        _stop_container(container_id)


def _live_source(container_id: str, contract_path: Path = CONTRACT_LIVE) -> LiveSource:
    contract = load_contract(contract_path)
    return LiveSource(
        contract=contract,
        runner=docker_runner(container_id),
        container_fs_root=provision.CONTAINER_FS_ROOT,
        robot="livebot",
    )


def _recording_runner(container_id: str) -> tuple:
    """Wrap docker_runner so tests can assert which commands hit the container."""
    inner = docker_runner(container_id)
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...]) -> CommandResult:
        calls.append(command)
        return inner(command)

    return run, calls


# ---------------------------------------------------------------------------
# Unit / shape (no docker required)
# ---------------------------------------------------------------------------


def test_live_source_satisfies_protocol_and_scores_absence():
    """LiveSource is a PreflightSource and opts into absence scoring."""

    contract = load_contract(CONTRACT_LIVE)

    def _empty_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(127, stderr="not a live run")

    source = LiveSource(contract=contract, runner=_empty_runner)
    assert isinstance(source, PreflightSource)
    assert LiveSource.scores_absence is True
    assert source.scores_absence is True


def test_live_source_samples_without_fetch_refuses_honestly():
    """samples() is implemented, but still refuses cleanly with no fetch wired."""
    from lockwire_pro.preflight import SamplesUnavailable

    contract = load_contract(CONTRACT_LIVE)

    def _empty_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(127, stderr="unused")

    source = LiveSource(contract=contract, runner=_empty_runner)
    with pytest.raises(SamplesUnavailable, match="no fetch configured"):
        source.samples()


def test_ros2_command_failure_is_refused_not_absent():
    """eb-17: a nonzero-exit ``ros2`` reads NOT SCORED, never an ABSENT device.

    Before the fix, ``_ros2_lines`` swallowed a nonzero exit into ``[]``, so a
    dead daemon / wrong ``ROS_DOMAIN_ID`` / missing ``ros2`` on PATH scored the
    declared driver as a stage-1 ``violation`` (ABSENT) exactly like a real
    driver-down — a confident false NO-GO with no measurement behind it.
    """
    from lockwire_pro.preflight.runner import STAGES, _dispatch_stage

    contract = load_contract(CONTRACT_LIVE)

    def _refusing_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(1, stderr="could not contact ROS daemon")

    source = LiveSource(contract=contract, runner=_refusing_runner)
    entry = next(e for e in STAGES if e.name == "liveness")
    findings = _dispatch_stage(entry, contract, source)

    assert len(findings) == 1, findings
    finding = findings[0]
    assert finding.severity == "not_scored", finding
    assert finding.quantity == "preflight/stage/1/liveness", finding
    assert "ros2 node list" in finding.observed and "exit 1" in finding.observed, finding
    # The whole-graph refusal must never read as a per-device ABSENT violation.
    assert not any(f.severity == "violation" for f in findings)


def _graph_runner(node_lines: str, topic_lines: str) -> CommandResult:
    """Build a fake ``ros2`` runner reporting the given node/topic list output."""

    def _run(command: tuple[str, ...]) -> CommandResult:
        if command == ("ros2", "node", "list"):
            return CommandResult(0, stdout=node_lines)
        if command == ("ros2", "topic", "list"):
            return CommandResult(0, stdout=topic_lines)
        raise AssertionError(f"unexpected command: {command}")  # pragma: no cover

    return _run


def test_liveness_topic_only_miss_is_weak_evidence_not_node_missing():
    """eb-22: node up, declared topic absent → topic-missing wording, NOT node-missing.

    Composable nodes and intra-process topics genuinely do not appear in
    ``ros2 topic list`` — this must read as weak evidence (``not_scored``),
    never as the driver/node no-go.
    """
    from lockwire_pro.preflight.stages import liveness

    contract = load_contract(CONTRACT_LIVE)
    source = LiveSource(contract=contract, runner=_graph_runner("/imu_driver\n", ""))
    observed = source.liveness()
    assert len(observed) == 1
    assert observed[0].node == "/imu_driver" and not observed[0].node_missing
    assert observed[0].topic is None and observed[0].topic_missing

    findings = liveness.check(contract, source)
    assert len(findings) == 1, findings
    assert findings[0].quantity == "devices/dev.imu/liveness/topic", findings
    assert findings[0].severity == "not_scored", findings
    assert "composable" in findings[0].observed, findings
    assert not any(f.quantity == "devices/dev.imu/liveness" for f in findings), findings


def test_liveness_node_only_miss_is_real_no_go_not_topic_missing():
    """eb-22: topic up, declared node absent → node-missing wording, NOT topic-missing.

    Mutation-bite: swap the wording keys (render node-missing text for the
    topic branch, or vice versa) and this test goes red — the accept bite.
    """
    from lockwire_pro.preflight.stages import liveness

    contract = load_contract(CONTRACT_LIVE)
    source = LiveSource(contract=contract, runner=_graph_runner("", "/imu/data\n"))
    observed = source.liveness()
    assert len(observed) == 1
    assert observed[0].node is None and observed[0].node_missing
    assert observed[0].topic == "/imu/data" and not observed[0].topic_missing

    findings = liveness.check(contract, source)
    assert len(findings) == 1, findings
    assert findings[0].quantity == "devices/dev.imu/liveness", findings
    assert findings[0].severity == "violation", findings
    assert "node" in findings[0].observed and "not on the ROS graph" in findings[0].observed
    assert not any(f.quantity == "devices/dev.imu/liveness/topic" for f in findings), findings


def test_liveness_both_operands_present_is_clean():
    """eb-22: node and topic both on the graph → no missing-operand finding at all."""
    from lockwire_pro.preflight.stages import liveness

    contract = load_contract(CONTRACT_LIVE)
    source = LiveSource(
        contract=contract, runner=_graph_runner("/imu_driver\n", "/imu/data sensor_msgs/msg/Imu\n")
    )
    observed = source.liveness()
    assert len(observed) == 1
    assert not observed[0].node_missing and not observed[0].topic_missing

    findings = liveness.check(contract, source)
    assert findings == [], findings


def test_live_contract_identity_not_derived_from_provisioner():
    """House-bug guard (B93/B100 lineage): declared identity is authored YAML.

    Mutation-bite: make the fixture's declared identity derive from
    ``provision.PLANTED_I2C_ADDRESS_HEX`` (e.g. rewrite the YAML from that
    constant, or construct ``DeviceIdentity(address=…)`` from it) and this
    assertion goes red.
    """
    contract_text = CONTRACT_LIVE.read_text()
    # Body only — comments may name the provisioner; derivation lives in code.
    body = "\n".join(
        line for line in contract_text.splitlines() if not line.lstrip().startswith("#")
    )
    provision_src = Path(provision.__file__).read_text()
    this_src = Path(__file__).read_text()

    assert "PLANTED_I2C_ADDRESS_HEX" in provision_src
    # Contract YAML body must not reference the provisioner constant by name.
    assert "PLANTED_" not in body
    assert "provision" not in body

    # Authored address literal must be present as YAML, not imported.
    assert 'address: "0x68"' in body or "address: '0x68'" in body

    # Tests must not assign identity fields from provisioner constants.
    assert not re.search(r"address\s*=\s*provision\.PLANTED_", this_src)
    assert not re.search(r"address\s*=\s*PLANTED_I2C_ADDRESS", this_src)
    assert not re.search(
        r"DeviceIdentity\([^)]*address\s*=\s*provision\.",
        this_src,
    )

    # Values may coincide; derivation is the forbidden pattern. Confirm the
    # loaded contract still carries the authored literal.
    contract = load_contract(CONTRACT_LIVE)
    imu = contract.device("dev.imu")
    assert imu is not None and imu.identity is not None
    assert imu.identity.address == "0x68"


def test_dockerfile_pins_base_digest_not_floating_tag():
    """Derived image must not track a moving tag like ``latest`` or bare ``jazzy-ros-core``."""
    text = LIVE_DOCKERFILE.read_text()
    assert "sha256:17142ce4cb561d70d61f4fb485177caf0d6cceca4ccc418704168ff45f3713e5" in text
    assert "usbutils" in text and "iproute2" in text
    assert "FROM ros:jazzy-ros-core@sha256:" in text
    assert "FROM ros:jazzy-ros-core\n" not in text
    assert ":latest" not in text


# ---------------------------------------------------------------------------
# Live container cases — must EXECUTE here (Docker is up).
# ---------------------------------------------------------------------------


@requires_docker
def test_present_and_live_against_container(ros_container: str):
    """Device visible + imu_driver up → scored present and live (not not_scored)."""
    provision.plant_i2c_device_in_container(ros_container)
    _start_imu_driver(ros_container)
    source = _live_source(ros_container)
    contract = source.contract

    present = source.enumerate()
    live = source.liveness()
    # Presence floor: denominator before any zero-assertion.
    assert len(present) >= 1, "no devices enumerated on the target"
    assert len(live) >= 1, "no drivers observed live on the target"
    assert {obs.device_id for obs in present} == {"dev.imu"}
    assert {obs.device_id for obs in live} == {"dev.imu"}
    assert present[0].address == "0x68"
    assert live[0].node == "/imu_driver"
    assert live[0].topic == "/imu/data"
    # Host-side disclosures stay empty — every fs source went through the runner.
    # Presence floor: devices enumerated (>0) above; disclosure zero
    # is only meaningful because filesystem sources were actually examined.
    disclosures = source.host_side_disclosures()
    assert len(present) > 0  # denominator for the zero below
    assert disclosures == []

    findings = run_preflight(contract, source)
    stage0 = [f for f in findings if f.stage == 0]
    stage1 = [f for f in findings if f.stage == 1]
    assert not any(f.severity == "violation" for f in stage0), stage0
    assert not any(f.severity == "violation" for f in stage1), stage1
    # Matched path: no unseen disclosure for the imu.
    assert not any(f.quantity == "devices/dev.imu/enumeration" for f in stage0)
    assert not any(f.quantity == "devices/dev.imu/liveness" for f in stage1)
    # Stages 2–4 must be not_scored (samples refused), never a pass-by-silence.
    later = [f for f in findings if f.stage is not None and f.stage >= 2]
    assert later, "stages 2–4 produced no findings — silence is not a pass"
    assert all(f.severity == "not_scored" for f in later)
    assert _exit_code(findings) == 0


@requires_docker
def test_device_absent_is_stage0_violation(ros_container: str, tmp_path: Path):
    """Contract declares a device the container does not have → stage-0 violation.

    Mutation-bite (revert after): plant the declared device **in the container**
    — the stage-0 violation must vanish. A host-filesystem plant must NOT clear it.
    """
    provision.clear_i2c_devices_in_container(ros_container)
    _start_imu_driver(ros_container)  # driver up; absence is the bus, not the node

    # Host plant must not masquerade as container evidence.
    host_plant = tmp_path / "host_plant"
    provision.plant_i2c_device(host_plant)

    runner, calls = _recording_runner(ros_container)
    source = LiveSource(
        contract=load_contract(CONTRACT_LIVE),
        runner=runner,
        container_fs_root=provision.CONTAINER_FS_ROOT,
        robot="livebot",
    )
    present = source.enumerate()
    assert present == [], f"expected no match on empty container bus, got {present}"
    # Proof: stage 0 listed i2c through the runner against the container path.
    i2c_ls = ("ls", "-1", f"{provision.CONTAINER_FS_ROOT}/sys/bus/i2c/devices")
    assert i2c_ls in calls, f"i2c was not listed via runner; calls={calls}"

    findings = run_preflight(source.contract, source)
    stage0_violations = [f for f in findings if f.stage == 0 and f.severity == "violation"]
    assert stage0_violations, f"expected stage-0 violation, got {findings}"
    assert any(
        f.quantity == "devices/dev.imu/enumeration" and "absent" in f.observed
        for f in stage0_violations
    ), stage0_violations
    assert _exit_code(findings) == 1

    # Host plant still must not clear the violation (bounce-1: container facts only).
    findings_host = run_preflight(source.contract, source)
    assert any(
        f.stage == 0 and f.severity == "violation" and "absent" in f.observed for f in findings_host
    ), "host Path plant incorrectly cleared a container absence"

    # --- mutation-bite: plant inside the container → violation gone ---
    provision.plant_i2c_device_in_container(ros_container)
    source_present = _live_source(ros_container)
    findings_present = run_preflight(source_present.contract, source_present)
    stage0_after = [f for f in findings_present if f.stage == 0 and f.severity == "violation"]
    assert not stage0_after, (
        "mutation-bite failed: planting the declared device in the container "
        f"should clear stage-0; still have {stage0_after}"
    )


@requires_docker
def test_driver_down_is_stage1_violation(ros_container: str):
    """Device present in container, node NOT started → stage-1 violation, EXIT=1.

    Mutation-bite (revert after): start the driver — the stage-1 violation
    must vanish.
    """
    provision.plant_i2c_device_in_container(ros_container)
    # Do NOT start imu_driver.
    source = _live_source(ros_container)
    present = source.enumerate()
    assert len(present) >= 1, "presence floor: device must be on the bus for driver-down"
    live = source.liveness()
    # eb-22: both operands are declared and neither is up → a partial
    # observation is still returned (not an empty list) with both `_missing`
    # flags set, so the stage can word the node absence as a real no-go.
    assert len(live) == 1, f"expected one partial observation, got {live}"
    assert live[0].node is None and live[0].topic is None
    assert live[0].node_missing and live[0].topic_missing

    findings = run_preflight(source.contract, source)
    stage1_violations = [f for f in findings if f.stage == 1 and f.severity == "violation"]
    assert stage1_violations, f"expected stage-1 violation, got {findings}"
    assert any(f.quantity == "devices/dev.imu/liveness" for f in stage1_violations), (
        stage1_violations
    )
    assert _exit_code(findings) == 1
    # Stage 0 must be clean (device is present).
    assert not any(f.stage == 0 and f.severity == "violation" for f in findings)

    # --- mutation-bite: start the driver → stage-1 violation gone ---
    _start_imu_driver(ros_container)
    source_up = _live_source(ros_container)
    findings_up = run_preflight(source_up.contract, source_up)
    stage1_after = [f for f in findings_up if f.stage == 1 and f.severity == "violation"]
    assert not stage1_after, (
        f"mutation-bite failed: starting imu_driver should clear stage-1; still have {stage1_after}"
    )


@requires_docker
def test_render_exit_codes_match_cli_map(ros_container: str):
    """EXIT=1 for absent / driver-down via the real render() exit map."""
    provision.clear_i2c_devices_in_container(ros_container)
    absent_source = _live_source(ros_container)
    assert render(run_preflight(absent_source.contract, absent_source)) == 1

    provision.plant_i2c_device_in_container(ros_container)
    down_source = _live_source(ros_container)
    assert render(run_preflight(down_source.contract, down_source)) == 1


@pytest.mark.parametrize(
    "remote_dir",
    ("/tmp/lockwire-live-/../outside", "/tmp/lockwire-live-case/./capture"),
)
def test_ssh_fetch_rejects_traversal_before_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote_dir: str
):
    """Traversal is refused before constructing SSH or invoking scp."""

    def transport_must_not_start(*_args, **_kwargs):
        raise AssertionError("invalid remote directory reached transport")

    monkeypatch.setattr("lockwire.scan.ssh_runner", transport_must_not_start)
    monkeypatch.setattr("lockwire.scan.subprocess.run", transport_must_not_start)

    with pytest.raises(ValueError, match="invalid live capture directory"):
        ssh_fetch(
            "127.0.0.1",
            port=22,
            identity_file=tmp_path / "identity",
            known_hosts=tmp_path / "known_hosts",
            remote_dir=remote_dir,
            local_dir=tmp_path / "capture",
        )


def test_ssh_runner_passes_one_shell_quoted_remote_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls = []

    def capture(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("lockwire.scan.subprocess.run", capture)
    runner = ssh_runner(
        "127.0.0.1",
        port=22,
        identity_file=tmp_path / "key",
        known_hosts=tmp_path / "known_hosts",
    )
    assert runner(("kill", "-0", "1234")).returncode == 0
    assert runner(("ros2", "node", "list")).returncode == 0
    assert len(calls) == 2
    for argv in calls:
        option_pairs = tuple(zip(argv, argv[1:]))
        assert ("-o", "StrictHostKeyChecking=yes") in option_pairs
        assert ("-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}") in option_pairs
        assert ("-o", "GlobalKnownHostsFile=/dev/null") in option_pairs
        assert argv[argv.index("--") + 1 :] == ("root@127.0.0.1", argv[-1])
        assert shlex.split(argv[-1])[:2] == ["bash", "-lc"]
        assert len(shlex.split(argv[-1])) == 3
    assert shlex.split(calls[0][-1])[2] == "kill -0 1234"
    assert shlex.split(calls[1][-1])[2] == ("source /opt/ros/jazzy/setup.bash && ros2 node list")


def test_ssh_runner_does_not_return_credential_paths_in_transport_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key = tmp_path / "private-key"
    known_hosts = tmp_path / "known-hosts"

    def failed(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 255, "", f"load {key}: bad key; host verification at {known_hosts} failed"
        )

    monkeypatch.setattr("lockwire.scan.subprocess.run", failed)
    result = ssh_runner(
        "127.0.0.1",
        port=22,
        identity_file=key,
        known_hosts=known_hosts,
    )(("ros2", "node", "list"))
    assert result.returncode == 255
    assert str(key) not in result.stderr and str(known_hosts) not in result.stderr
    assert "<ssh-key-file>" in result.stderr
    assert "<ssh-known-hosts-file>" in result.stderr


@pytest.mark.parametrize("alter_remote", [False, True])
def test_ssh_fetch_refuses_altered_copied_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alter_remote: bool
):
    """SCP success cannot bless a bag different from the host-verified source."""
    remote = {"metadata.yaml": b"metadata", "bag_0.mcap": b"original"}
    remote_dir = "/tmp/lockwire-live-case"

    def remote_run(command):
        if command[0] == "find":
            return CommandResult(0, "metadata.yaml\tf\nbag_0.mcap\tf\n")
        name = command[-1].rsplit("/", 1)[-1]
        if command[0] == "sha256sum":
            return CommandResult(0, f"{hashlib.sha256(remote[name]).hexdigest()}  {name}\n")
        if command[0] == "stat":
            return CommandResult(0, f"{len(remote[name])}\n")
        raise AssertionError(command)

    def fake_scp(argv, **_kwargs):
        assert argv[0] == "scp"
        option_pairs = tuple(zip(argv, argv[1:]))
        assert ("-o", "StrictHostKeyChecking=yes") in option_pairs
        assert ("-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}") in option_pairs
        assert ("-o", "GlobalKnownHostsFile=/dev/null") in option_pairs
        local = Path(argv[-1])
        local.mkdir()
        (local / "metadata.yaml").write_bytes(remote["metadata.yaml"])
        (local / "bag_0.mcap").write_bytes(remote["bag_0.mcap"] if alter_remote else b"altered!")
        if alter_remote:
            remote["bag_0.mcap"] = b"changed!"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("lockwire.scan.ssh_runner", lambda *_a, **_k: remote_run)
    monkeypatch.setattr("lockwire.scan.subprocess.run", fake_scp)
    with pytest.raises(OSError, match="remote bag changed|digest differs"):
        ssh_fetch(
            "127.0.0.1",
            port=22,
            identity_file=tmp_path / "key",
            known_hosts=tmp_path / "known_hosts",
            remote_dir=remote_dir,
            local_dir=tmp_path / "capture",
        )


def test_failed_recorder_kill_refuses_capture(monkeypatch: pytest.MonkeyPatch):
    """A failed corpse proof cannot become a successful partial recording."""
    from lockwire_pro.preflight import SamplesUnavailable

    source = LiveSource(
        contract=load_contract(CONTRACT_LIVE),
        runner=lambda _cmd: CommandResult(137),
        fetch=lambda _remote, _local: pytest.fail("failed kill must refuse before fetching"),
        cancellable=True,
    )
    monkeypatch.setattr("lockwire_pro.jobs.kill_container_process", lambda *_a, **_k: False)

    def failed_capture(_command, _timeout, _unavailable):
        source._capture_active = True
        source._recorder_pid = 123
        assert source.cancel() is False
        assert source._cancelled is False
        source._capture_active = False
        return CommandResult(137)

    monkeypatch.setattr(source, "_run_cancellable", failed_capture)
    with pytest.raises(SamplesUnavailable, match="could not prove recorder stopped"):
        source.samples()


def test_cli_ssh_requires_caller_target_and_image(tmp_path: Path):
    from typer.testing import CliRunner

    from lockwire.cli import app

    key = tmp_path / "key"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("fixture key")
    known_hosts.write_text("fixture pin")
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_LIVE),
            "--live",
            "--ssh-host",
            "127.0.0.1",
            "--ssh-identity-file",
            str(key),
            "--ssh-known-hosts",
            str(known_hosts),
            "--no-ledger",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "target and image declarations" in result.output
    assert "capturing" not in result.output


def test_cli_ssh_unknown_host_refuses_before_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from typer.testing import CliRunner

    from lockwire.cli import app

    key = tmp_path / "key"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("fixture key")
    known_hosts.write_text("fixture pin")
    monkeypatch.setattr(
        "lockwire_pro.cli_pro.ssh_runner",
        lambda *_a, **_k: lambda _cmd: CommandResult(255, stderr="Host key verification failed"),
    )
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_LIVE),
            "--live",
            "--ssh-host",
            "127.0.0.1",
            "--ssh-target",
            "declared-loopback",
            "--ssh-image",
            "declared-fixture-image",
            "--ssh-identity-file",
            str(key),
            "--ssh-known-hosts",
            str(known_hosts),
            "--no-ledger",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Host key verification failed" in result.output
    assert "capturing" not in result.output


def _start_ssh_container(image: str, tmp_path: Path) -> tuple[str, int, Path, Path]:
    """Start one named local sshd target and return its verified SSH credentials."""
    name = f"lockwire-e33-{uuid.uuid4().hex[:12]}"
    completed = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST",
            "-p",
            "127.0.0.1::22",
            image,
            "/usr/sbin/sshd",
            "-D",
            "-e",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    container_id = completed.stdout.strip()
    key = tmp_path / "e33_ed25519"
    known_hosts = tmp_path / "known_hosts"
    try:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            [
                "docker",
                "cp",
                str(key.with_suffix(".pub")),
                f"{container_id}:/root/.ssh/authorized_keys",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["docker", "exec", container_id, "chown", "root:root", "/root/.ssh/authorized_keys"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["docker", "exec", container_id, "chmod", "600", "/root/.ssh/authorized_keys"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        key_mode = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "stat",
                "-c",
                "%u:%g:%a",
                "/root/.ssh/authorized_keys",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert key_mode == "0:0:600", f"local sshd key ownership/mode: {key_mode}"
        sshd_config = subprocess.run(
            ["docker", "exec", container_id, "/usr/sbin/sshd", "-T"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        settings = dict(line.split(maxsplit=1) for line in sshd_config.splitlines())
        assert settings["strictmodes"] == "yes"
        assert settings["pubkeyauthentication"] == "yes"
        assert settings["permitrootlogin"] in {"yes", "prohibit-password", "without-password"}
        assert ".ssh/authorized_keys" in settings["authorizedkeysfile"].split()
        mapped = subprocess.run(
            ["docker", "port", container_id, "22/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        port = int(mapped.rsplit(":", 1)[1])
        deadline = time.monotonic() + 30
        scanned = None
        while time.monotonic() < deadline:
            candidate = subprocess.run(
                ["ssh-keyscan", "-p", str(port), "127.0.0.1"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if candidate.returncode == 0 and candidate.stdout.strip():
                scanned = candidate
                break
            time.sleep(0.2)
        assert scanned is not None, "local sshd did not publish a host key within 30s"
        known_hosts.write_text(scanned.stdout, encoding="utf-8")
        return container_id, port, key, known_hosts
    except Exception:
        _stop_container(container_id)
        raise


def _wait_for_ssh_capture_sample(runner, recorder_pid: int, target_dir: str) -> None:
    """Wait for the recorder's subscription and one real declared IMU message."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        bag = runner(
            (
                "find",
                target_dir,
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-name",
                "*.mcap",
            )
        )
        topic = runner(("ros2", "topic", "info", "/imu/data"))
        subscribers = re.search(r"Subscription count:\s*([1-9]\d*)", topic.stdout)
        if bag.returncode == 0 and bag.stdout.strip() and subscribers:
            sample = runner(("ros2", "topic", "echo", "/imu/data", "--once"))
            assert sample.returncode == 0 and "linear_acceleration:" in sample.stdout, (
                "declared /imu/data was not observed while SSH recorder was subscribed"
            )
            assert runner(("kill", "-0", str(recorder_pid))).returncode == 0, (
                "SSH recorder stopped before cancellation"
            )
            return
        time.sleep(0.1)
    raise AssertionError("SSH recorder never subscribed and opened an IMU MCAP")


@requires_docker
def test_cli_ssh_live_capture_uses_declared_loopback_target(live_image: str, tmp_path: Path):
    """The public CLI reaches the same verified local SSH fixture."""
    from typer.testing import CliRunner

    from lockwire.cli import app

    container_id, port, key, known_hosts = _start_ssh_container(live_image, tmp_path)
    try:
        provision.plant_i2c_device_in_container(container_id)
        _start_imu_driver(container_id)
        result = CliRunner().invoke(
            app,
            [
                "preflight",
                str(CONTRACT_LIVE),
                "--live",
                "--ssh-host",
                "127.0.0.1",
                "--ssh-port",
                str(port),
                "--ssh-target",
                "declared-loopback-fixture",
                "--ssh-image",
                live_image,
                "--ssh-identity-file",
                str(key),
                "--ssh-known-hosts",
                str(known_hosts),
                "--seconds",
                "5",
                "--no-ledger",
            ],
        )
        assert result.exit_code in (0, 1), result.output
        assert "declared-loopback-fixture" in result.output
        assert "declared image" in result.output
        assert re.search(r"captured\s+[1-9]\d*\s+messages", result.output), result.output
    finally:
        _stop_container(container_id)


@requires_docker
def test_ssh_live_capture_cancel_marks_partial_and_leaves_no_owned_orphan(
    live_image: str, tmp_path: Path
):
    """E3.3 local-only proof: SSH reaches a real ROS capture and proves its corpse."""
    container_id, port, key, known_hosts = _start_ssh_container(live_image, tmp_path)
    try:
        provision.plant_i2c_device_in_container(container_id)
        _start_imu_driver(container_id)
        runner = ssh_runner(
            "127.0.0.1", port=port, identity_file=key, known_hosts=known_hosts, timeout=20
        )
        source = LiveSource(
            contract=load_contract(CONTRACT_LIVE),
            runner=runner,
            fetch=lambda remote, local: ssh_fetch(
                "127.0.0.1",
                port=port,
                identity_file=key,
                known_hosts=known_hosts,
                remote_dir=remote,
                local_dir=local,
                timeout=30,
            ),
            container_fs_root=provision.CONTAINER_FS_ROOT,
            robot="local-ssh-fixture",
            seconds=30,
            cancellable=True,
            keep_dir=tmp_path / "partial-bag",
        )
        result: list = []
        driver = threading.Thread(target=lambda: result.append(source.samples()))
        driver.start()
        deadline = time.monotonic() + 30
        while source._recorder_pid is None and driver.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert source._recorder_pid is not None, "SSH capture never exposed its recorder PID"
        recorder_pid = source._recorder_pid
        assert source._capture_target_dir is not None
        _wait_for_ssh_capture_sample(runner, recorder_pid, source._capture_target_dir)
        assert source.cancel() is True, "SSH cancellation did not prove the recorder corpse"
        driver.join(timeout=30)
        assert not driver.is_alive(), "SSH capture thread outlived cancellation"
        assert result and result[0].partial is True
        assert source.message_count > 0
        assert runner(("kill", "-0", str(recorder_pid))).returncode != 0
    finally:
        _stop_container(container_id)
    absent = subprocess.run(
        ["docker", "inspect", container_id], check=False, capture_output=True, text=True, timeout=30
    )
    assert absent.returncode != 0, "owned SSH fixture container survived cleanup"


@requires_docker
def test_served_ssh_run_cancel_retains_marked_partial_bag_and_proves_remote_corpse(
    live_image: str, tmp_path: Path
):
    """The public Run door reaches CLI SSH capture and its signal cleanup."""
    container_id, port, key, known_hosts = _start_ssh_container(live_image, tmp_path)
    contract = tmp_path / "contract.yaml"
    shutil.copyfile(CONTRACT_LIVE, contract)
    bag = tmp_path / "partial-bag"
    token = "local-ssh-run-token"
    runner = ssh_runner(
        "127.0.0.1", port=port, identity_file=key, known_hosts=known_hosts, timeout=20
    )
    recorder_pid = None
    try:
        provision.plant_i2c_device_in_container(container_id)
        _start_imu_driver(container_id)
        with TestClient(create_app(token), base_url="http://127.0.0.1") as client:
            request = {
                "contract": str(contract),
                "live": True,
                "seconds": 30.0,
                "ssh_host": "127.0.0.1",
                "ssh_port": port,
                "ssh_user": "root",
                "ssh_identity_file": str(key),
                "ssh_known_hosts": str(known_hosts),
                "ssh_target": "declared-loopback-fixture",
                "ssh_image": live_image,
                "keep_bag": str(bag),
            }
            started = client.post(
                "/v1/run",
                headers={"X-Lockwire-Token": token},
                json={"command": "run-start", "args": request},
            ).json()
            assert started["refusal"] is None
            deadline = time.monotonic() + 30
            remote_dir = None
            while recorder_pid is None and time.monotonic() < deadline:
                processes = runner(("ps", "-eo", "pid=,args="))
                for line in processes.stdout.splitlines():
                    fields = line.strip().split(maxsplit=1)
                    if (
                        len(fields) == 2
                        and fields[0].isdigit()
                        and re.search(r"\bros2 bag record\b", fields[1])
                        and "/tmp/lockwire-live-" in fields[1]
                    ):
                        recorder_pid = int(fields[0])
                        remote_dir = re.search(r"/tmp/lockwire-live-[A-Za-z0-9._-]+", fields[1])
                        break
                if recorder_pid is None:
                    time.sleep(0.1)
            assert recorder_pid is not None, "public SSH Run never reached a recorder"
            assert remote_dir is not None, "public SSH Run recorder has no bag destination"
            _wait_for_ssh_capture_sample(runner, recorder_pid, remote_dir.group())
            cancelled = client.post(
                "/v1/run",
                headers={"X-Lockwire-Token": token},
                json={"command": "run-cancel", "args": {}},
            ).json()
            assert cancelled["refusal"] is None
            state = cancelled["payload"]
            assert state["status"] == "cancelled"
            assert "declared-loopback-fixture" in state["output"]
            assert f"declared image {live_image}" in state["output"]
            assert "partial capture" in state["output"]
            assert key.read_text(encoding="utf-8") not in json.dumps(cancelled)
            assert str(key) not in json.dumps(cancelled)
            assert str(known_hosts) not in json.dumps(cancelled)
            assert (bag / "lockwire-partial.json").read_text(encoding="utf-8") == (
                '{"partial":true,"reason":"cancelled"}\n'
            )
            assert list(bag.glob("*.mcap"))
            assert load_ros2_bag(bag, load_contract(contract)).partial is True
            assert runner(("kill", "-0", str(recorder_pid))).returncode != 0
    finally:
        _stop_container(container_id)
    absent = subprocess.run(
        ["docker", "inspect", container_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert absent.returncode != 0, "owned SSH fixture container survived cleanup"


# ---------------------------------------------------------------------------
# samples() through a real ros2 bag record against the container
# ---------------------------------------------------------------------------


@requires_docker
def test_live_samples_scores_stages_2_3_on_a_publishing_container(ros_container: str):
    """A clean publisher: ``samples()`` captures for real and stages 2/3 SCORE."""
    provision.plant_i2c_device_in_container(ros_container)
    _start_imu_driver(ros_container)

    from lockwire_pro.preflight import SamplesUnavailable
    from lockwire.scan import docker_fetch

    contract = load_contract(CONTRACT_LIVE)
    source = LiveSource(
        contract=contract,
        runner=docker_runner(ros_container, timeout=15),
        fetch=docker_fetch(ros_container),
        container_fs_root=provision.CONTAINER_FS_ROOT,
        robot="livebot",
        seconds=5,
    )

    try:
        recording = source.samples()
    except SamplesUnavailable as error:  # pragma: no cover - only if the container never spins up
        pytest.fail(f"expected a real capture, got a refusal: {error}")

    assert recording.devices["dev.imu"].recv_times, "expected messages on /imu/data"
    assert source.message_count > 0

    findings = run_preflight(contract, source)
    stage2 = [f for f in findings if f.stage == 2]
    stage3 = [f for f in findings if f.stage == 3]
    # E3-FL: a fully-scored stage-2 leg (monotonicity, wall-clock currency,
    # rate_hz, recv_gap all PASS) legitimately returns an EMPTY findings list —
    # every PASS lands in ``scores``/``scored_records``, never in ``findings``
    # (see e.g. flow._rate_in_band_findings's clean-band branch, unchanged by
    # this box). ``scored_by_stage`` is the presence-floor denominator (a GO that
    # scored nothing proves nothing) and the real signal that stage 2 was scored, not silent.
    assert findings.scored_by_stage[2] > 0, findings.scored_by_stage
    assert not any(f.severity == "not_scored" for f in stage2), stage2
    # Same shape as stage 2 above: sanity.py's PASS branches also go through
    # ScoreBook.passed rather than a Finding, so a fully-scored stage 3 is
    # also legitimately empty here.
    assert findings.scored_by_stage[3] > 0, findings.scored_by_stage
    assert not any(f.severity == "not_scored" for f in stage3), stage3
    assert _exit_code(findings) == 0


@requires_docker
def test_live_samples_refuses_when_publisher_is_stopped(ros_container: str):
    """No publisher: stage 1 fails NO-GO, and stages 2-4 stay not_scored (0 messages)."""
    provision.plant_i2c_device_in_container(ros_container)
    # Do NOT start imu_driver — nothing publishes on /imu/data.

    from lockwire.scan import docker_fetch

    contract = load_contract(CONTRACT_LIVE)
    source = LiveSource(
        contract=contract,
        runner=docker_runner(ros_container, timeout=8),
        fetch=docker_fetch(ros_container),
        container_fs_root=provision.CONTAINER_FS_ROOT,
        robot="livebot",
        seconds=2,
    )

    findings = run_preflight(contract, source)
    stage1_violations = [f for f in findings if f.stage == 1 and f.severity == "violation"]
    assert stage1_violations, f"expected stage-1 violation, got {findings}"
    later = [f for f in findings if f.stage is not None and f.stage >= 2]
    assert later and all(f.severity == "not_scored" for f in later)
    assert _exit_code(findings) == 1


@requires_docker
def test_cli_preflight_live_container_end_to_end(ros_container: str, tmp_path: Path):
    """A captured target sample must produce a public, source-bound Live GO."""
    import json

    from typer.testing import CliRunner

    from lockwire.cli import app
    from lockwire_pro.ros2bag import load_ros2_bag

    provision.plant_i2c_device_in_container(ros_container)
    _start_imu_driver(ros_container)

    bag_path = tmp_path / "live-bag"
    document_path = tmp_path / "preflight.json"
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_LIVE),
            "--live",
            "--container",
            ros_container,
            "--seconds",
            "5",
            "--keep-bag",
            str(bag_path),
            "--no-ledger",
            "--json",
            str(document_path),
        ],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(document_path.read_text())
    assert document["robot"] == "livebot"
    assert document["verdict"] == "GO"
    assert document["exit_code"] == 0
    assert document["counts"]["violation"] == 0
    assert all(document["scored_by_stage"][str(stage)] > 0 for stage in range(4))

    # The public source names this target and count; the retained, finalized
    # MCAP supplies the independent receipt behind that number.
    bag_files = sorted(bag_path.glob("*.mcap"))
    assert bag_files and all(part.stat().st_size > 0 for part in bag_files)
    recording = load_ros2_bag(bag_path, load_contract(CONTRACT_LIVE))
    assert recording.receipt_monotonic is True
    assert re.fullmatch(r"[0-9a-f]{64}", recording.sha256)
    count = sum(len(stream.recv_times) for stream in recording.devices.values())
    assert count > 0
    assert document["source"] == f"live: container {ros_container}, 5 s, {count} msgs"
    assert f"captured {count} messages" in result.output
    assert bag_files[0].name in result.output


@requires_docker
def test_cli_preflight_live_container_returns_completed_no_go_without_publisher(
    ros_container: str, tmp_path: Path
):
    """The public CLI must finish with a scored nonzero NO-GO when the topic is silent."""
    import json

    from typer.testing import CliRunner

    from lockwire.cli import app

    provision.plant_i2c_device_in_container(ros_container)
    # Deliberately leave /imu/data without a publisher: this is an intentional,
    # completed failure result, not a missing-tool or refused-run case.
    document_path = tmp_path / "preflight.json"
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_LIVE),
            "--live",
            "--container",
            ros_container,
            "--seconds",
            "2",
            "--no-ledger",
            "--json",
            str(document_path),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "NO-GO" in result.output, result.output
    document = json.loads(document_path.read_text())
    assert document["verdict"] == "NO-GO"
    assert document["exit_code"] == 1
    assert document["counts"]["violation"] > 0
    assert any(
        finding["severity"] == "violation" and finding.get("quantity")
        for finding in document["findings"]
    ), document["findings"]


@requires_docker
def test_stage0_evidence_is_container_lsusb_and_runner_i2c(ros_container: str):
    """Direct proof: lsusb and runner-routed i2c are container facts, not host Path.

    ``lsusb`` finding a root hub is a fact about the MACHINE, not about lockwire.
    Docker Desktop's linuxkit VM exposes a `vhci_hcd` virtual host controller, so
    locally `lsusb` reports `1d6b:0002`/`0003` and exits 0; a GitHub Actions runner
    has no USB controller at all, so the same command exits 1 with empty output.
    Asserting the hub was an environment assumption riding along, and it turned CI
    red (run 30325925031) while every Mac stayed green. What this test is actually
    for — evidence comes from the CONTAINER, not from a host ``Path`` — is carried
    by the i2c half below, which is strictly stronger: the planted sysfs tree exists
    only inside the container, so a host read of the same root finds nothing.
    """
    # Routed through the container runner either way; the outcome is the host's
    # USB topology, so both states are legitimate and neither is a host leak.
    lsusb = docker_runner(ros_container)(("lsusb",))
    assert lsusb.returncode in (0, 1), lsusb.stderr
    if lsusb.returncode == 0:
        assert "1d6b:0002" in lsusb.stdout or "1d6b:0003" in lsusb.stdout, lsusb.stdout

    draft = scan_machine(
        "proof",
        runner=docker_runner(ros_container),
        fs_root=Path(provision.CONTAINER_FS_ROOT),
        fs_access="runner",
        platform="linux",
    )
    # Never "malformed": the scanner must classify a bus-less host as unavailable,
    # not as garbage it half-parsed.
    assert draft.sources["lsusb"].status in ("observed", "unavailable")
    if draft.sources["lsusb"].status == "observed":
        assert draft.sources["lsusb"].items >= 1
    assert draft.sources["ip-link"].status == "observed"
    # No plant yet → i2c unavailable or empty observed, never a host Path leak.
    assert draft.sources["i2c-sysfs"].status in ("observed", "unavailable")
    if draft.sources["i2c-sysfs"].status == "observed":
        assert draft.sources["i2c-sysfs"].items == 0
    assert draft.sources["i2c-sysfs"].fs_access == "runner"
    assert draft.sources["device-tree"].fs_access == "runner"

    provision.plant_i2c_device_in_container(ros_container)
    draft_planted = scan_machine(
        "proof",
        runner=docker_runner(ros_container),
        fs_root=Path(provision.CONTAINER_FS_ROOT),
        fs_access="runner",
        platform="linux",
    )
    assert draft_planted.sources["i2c-sysfs"].status == "observed"
    assert draft_planted.sources["i2c-sysfs"].items >= 1
    assert draft_planted.sources["i2c-sysfs"].fs_access == "runner"
    addresses = {
        d.identity.address
        for d in draft_planted.candidate.devices
        if d.identity is not None and d.identity.address is not None
    }
    assert "0x68" in addresses


# ---------------------------------------------------------------------------
# B111 — host-side disclosure tracks the access fact, not a path string
# ---------------------------------------------------------------------------


def test_host_side_belt_fires_on_local_access_despite_container_shaped_paths():
    """ACCEPT: local fs_access discloses even when paths look container-shaped.

    Adversarial construction: ``fs_access="local"`` with ``fs_root`` and
    ``container_fs_root`` both ``/var/lockwire``. Detail strings contain that
    root (indistinguishable from the honest runner case to the old heuristic);
    the belt must still fire because it keys on the recorded access fact.
    """
    from lockwire_pro.preflight.stages import enumeration

    contract = load_contract(CONTRACT_LIVE)

    def _empty_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(127, stderr="not a live run")

    root = "/var/lockwire"
    source = LiveSource(
        contract=contract,
        runner=_empty_runner,
        container_fs_root=root,
        fs_access="local",
        robot="livebot",
    )
    # Prove the detail string looks container-shaped (old heuristic would stay quiet).
    draft = scan_machine(
        "adversary",
        runner=_empty_runner,
        fs_root=Path(root),
        fs_access="local",
        platform="linux",
    )
    for name in ("i2c-sysfs", "device-tree"):
        detail = draft.sources[name].detail or ""
        assert draft.sources[name].fs_access == "local"
        # Detail names the container-shaped root — string heuristic cannot tell.
        assert root in detail, f"{name} detail={detail!r}"

    source.enumerate()
    disclosures = source.host_side_disclosures()
    # Presence floor: both filesystem sources were examined.
    assert len(disclosures) == 2, disclosures
    assert {f.quantity for f in disclosures} == {
        "preflight/live/i2c-sysfs",
        "preflight/live/device-tree",
    }
    assert all(f.severity == "not_scored" for f in disclosures)
    assert all("the live path is untested" in f.observed for f in disclosures)

    # Stage 0 surfaces the belt findings (not a silent LiveSource-only list).
    stage0 = enumeration.check(contract, source)
    host_findings = [f for f in stage0 if f.quantity.startswith("preflight/live/")]
    assert len(host_findings) == 2, host_findings
    assert all("the live path is untested" in f.observed for f in host_findings)


def test_unknown_fs_access_is_disclosed_never_trusted_as_runner(monkeypatch):
    """Absent access fact defaults to unknown → belt discloses (honest default)."""
    from lockwire.scan import ScanDraft, SourceVisibility

    contract = load_contract(CONTRACT_LIVE)

    def _empty_runner(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(127, stderr="unused")

    source = LiveSource(contract=contract, runner=_empty_runner, fs_access="runner")

    def _forged_draft(*_args, **_kwargs):
        # Craft a draft where filesystem sources omit fs_access (pre-B111 shape).
        real = scan_machine(
            "forged",
            runner=_empty_runner,
            fs_root=Path("/var/lockwire"),
            fs_access="runner",
            platform="linux",
        )
        sources = {
            name: SourceVisibility(
                status=vis.status,
                items=vis.items,
                detail=vis.detail,
                fs_access=None if name in ("i2c-sysfs", "device-tree") else vis.fs_access,
            )
            for name, vis in real.sources.items()
        }
        return ScanDraft(sources=sources, candidate=real.candidate)

    monkeypatch.setattr("lockwire_pro.preflight.source.scan_machine", _forged_draft)
    source.enumerate()
    disclosures = source.host_side_disclosures()
    assert len(disclosures) == 2, disclosures
    assert all("the live path is untested" in f.observed for f in disclosures)


def test_honest_runner_path_emits_no_host_side_disclosure_unit():
    """Runner-backed LiveSource (no docker) — belt quiet; filesystem sources examined."""
    contract = load_contract(CONTRACT_LIVE)
    root = "/var/lockwire"
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> CommandResult:
        calls.append(command)
        if command[:2] == ("ls", "-1") and command[-1].endswith("/sys/bus/i2c/devices"):
            return CommandResult(0, "1-0068\n")
        if command[0] == "cat" and command[-1].endswith("/1-0068/name"):
            return CommandResult(0, "bmi088\n")
        if command[0] == "find" and "device-tree" in command[1]:
            return CommandResult(1, stderr=f"{command[1]} is absent")
        return CommandResult(127, stderr="fixture command absent")

    source = LiveSource(
        contract=contract,
        runner=runner,
        container_fs_root=root,
        fs_access="runner",
        robot="livebot",
    )
    present = source.enumerate()
    # Presence floor: runner actually hit both filesystem sources.
    assert any(c[:2] == ("ls", "-1") for c in calls), calls
    assert any(c[0] == "find" for c in calls), calls
    disclosures = source.host_side_disclosures()
    assert disclosures == [], disclosures
    assert len(present) >= 1
    assert present[0].device_id == "dev.imu"


def test_live_contract_rate_band_tolerates_loaded_host_stamp_burst():
    """A recorder stamping a backlog microseconds apart must not NO-GO the live fixture."""
    from lockwire_pro.preflight.stages.flow import _rate_in_band_findings
    from lockwire_pro.recording import DeviceStream

    device = load_contract(CONTRACT_LIVE).devices[0]
    band = device.envelopes[0].rate_hz
    stream = DeviceStream(device=device.id, recv_times=[0.0, 0.37, 0.37005, 0.6])
    assert _rate_in_band_findings(device, stream, band) == []
