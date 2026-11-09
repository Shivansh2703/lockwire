"""``lockwire sample --host`` — the same /sys+/proc sampler over ssh.

Three claims are load-bearing here and each has a test: every probe is a read
(lockwire never actuates), the ssh invocation is an argument list no hostile value can turn into a
shell command (the injection fix), and every device class states why its read is
safe (``docs/probes.md``). Nothing here contacts a real host.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lockwire_pro.sample_remote as R
from lockwire.cli import app
from lockwire_pro.recording import load_recording
from lockwire_pro.sample_local import discover_readers

SRC = Path(R.__file__)
DOC = Path(__file__).parent.parent / "docs" / "probes.md"

HOSTILE = (
    "-oProxyCommand=touch /tmp/pwned",  # ssh's real attack: a host that is an option
    "robot.local; touch /tmp/pwned",
    "robot.local`touch /tmp/pwned`",
    "robot.local$(touch /tmp/pwned)",
    "robot.local\ntouch /tmp/pwned",
    "robot.local | sh",
    "$(hostname)",
    "--",
    "",
)


# The reviewed set of files lockwire may read on a robot, written out once, by
# hand. It is the anchor for test_no_probe_writes: adding a probe to PROBES
# without a reviewer editing this literal is a failure, by design.
EXPECTED_PROBE_PATHS = {
    "/sys/class/thermal/thermal_zone*/type",
    "/sys/class/thermal/thermal_zone*/temp",
    "/sys/class/thermal/cooling_device*/cur_state",
    "/sys/class/hwmon/hwmon*/name",
    "/sys/class/hwmon/hwmon*/in*_input",
    "/sys/class/hwmon/hwmon*/curr*_input",
    "/sys/class/hwmon/hwmon*/temp*_input",
    "/sys/class/hwmon/hwmon*/power*_input",
    "/sys/class/hwmon/hwmon*/energy*_input",
    "/proc/loadavg",
    "/proc/net/dev",
}


# --- invocation ------------------------------------------------------------


@pytest.fixture
def known_hosts(tmp_path):
    pin = tmp_path / "pinned host keys"
    pin.write_text("robot.local ssh-ed25519 AAAA\n")
    return pin


def test_invocation_is_a_list_never_a_string(known_hosts):
    argv = R.build_invocation("pi@robot.local", port=2222, known_hosts=known_hosts)
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert argv[0] == "ssh"
    assert argv[-3:] == ["pi@robot.local", "python3", "-"]
    # The destination comes after "--", so ssh can never read it as an option.
    assert argv[argv.index("pi@robot.local") - 1] == "--"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"


def test_ssh_is_the_default_and_port_is_optional(known_hosts):
    """ssh alone must work: a path that needs the port when ssh works fails here."""
    argv = R.build_invocation("robot.local", known_hosts=known_hosts)
    assert argv[0] == "ssh"
    assert "-p" not in argv
    assert argv[-3:] == ["robot.local", "python3", "-"]


def test_every_invocation_carries_a_connect_timeout_and_never_prompts(known_hosts):
    for host in ("robot.local", "a@b"):
        argv = R.build_invocation(host, known_hosts=known_hosts)
        joined = " ".join(argv)
        assert "BatchMode=yes" in joined  # a password prompt would hang forever
        assert f"ConnectTimeout={R.DEFAULT_CONNECT_TIMEOUT_S}" in joined
        assert "StrictHostKeyChecking=yes" in argv
        assert f'UserKnownHostsFile="{known_hosts}"' in argv
        assert "GlobalKnownHostsFile=/dev/null" in argv
        assert {
            "VerifyHostKeyDNS=no",
            "NoHostAuthenticationForLocalhost=no",
            "UpdateHostKeys=no",
            "ControlPath=none",
        }.issubset(argv)


def test_ssh_config_cannot_supply_trust_outside_the_pin(tmp_path, known_hosts):
    known_hosts.write_text("")
    config = tmp_path / "ssh_config"
    config.write_text("Host robot\n HostName localhost\n KnownHostsCommand /usr/bin/true\n")
    argv = R.build_invocation("robot", known_hosts=known_hosts)
    result = subprocess.run(
        argv[:1] + ["-G", "-F", str(config)] + argv[1:], capture_output=True, text=True, check=True
    )
    assert "knownhostscommand " not in result.stdout and "hostname localhost" in result.stdout


# --- the shell is a non-door ----------------------------------------------


def test_no_hostile_value_reaches_a_shell(tmp_path):
    """The whole claim, on the shared path every caller goes through:

    1. the module contains no shell execution of any kind;
    2. ``build_invocation`` — which every remote call builds its argv with —
       rejects a hostile destination before returning anything;
    3. ``fetch_snapshot`` therefore starts no process for one.
    """
    text = SRC.read_text()
    assert "shell=True" not in text
    assert "os.system" not in text
    assert "os.popen" not in text
    # No f-string or concatenation is ever handed to subprocess: the only
    # subprocess call takes the list build_invocation returned.
    assert re.search(r"subprocess\.run\(\s*argv,", text)
    assert len(re.findall(r"subprocess\.run\(", text)) == 1

    started: list[list[str]] = []

    def spy(*args, **kwargs):  # pragma: no cover - must never run
        started.append(args[0])
        raise AssertionError("a process was started for a hostile host")

    for host in HOSTILE:
        with pytest.raises(ValueError):
            R.build_invocation(host)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(subprocess, "run", spy)
        try:
            with pytest.raises(ValueError):
                R.fetch_snapshot(host, tmp_path)
        finally:
            monkey.undo()
    assert started == []
    assert not Path("/tmp/pwned").exists()


def test_a_hostile_path_from_the_robot_cannot_escape_the_mirror(tmp_path):
    """The robot's reply is untrusted too: a returned path must be ours."""
    with pytest.raises(ValueError):
        R._validate_remote_path("/sys/../../etc/passwd")
    with pytest.raises(ValueError):
        R._validate_remote_path("/etc/passwd")
    assert R._validate_remote_path("/proc/loadavg") == Path("/proc/loadavg")


# --- lockwire never actuates: every probe is a read -------------------------------------------


def test_no_probe_writes():
    """The claim is *no probe*, so it is asserted over the shared PROBES list
    and the one shared remote program, not over a call site."""
    for probe in R.PROBES:
        for pattern in probe.paths:
            assert pattern.startswith(("/sys/", "/proc/")), (probe.device_class, pattern)
    program = R._REMOTE_PROGRAM
    assert "open(path)" in program  # read mode, no second argument
    for forbidden in ('"w"', "'w'", "wb", "a+", "r+", "os.write", "ioctl", "shutil", "os.remove"):
        assert forbidden not in program, forbidden
    # Anchored outside both legs: the program is *built* from PROBES, so
    # comparing the two would only prove they agree. This literal is the third
    # party — the exact, reviewed set of files lockwire is allowed to read.
    assert {p for probe in R.PROBES for p in probe.paths} == EXPECTED_PROBE_PATHS
    # Any absolute path in the program, not just /sys and /proc ones — a
    # smuggled "/dev/i2c-1" must show up here, not slip past the pattern.
    globbed = set(re.findall(r'["\'](/[^"\']*)["\']', program))
    assert globbed == EXPECTED_PROBE_PATHS


def test_every_probe_class_states_why_it_is_safe():
    doc = DOC.read_text()
    for probe in R.PROBES:
        assert f"`{probe.device_class}`" in doc, probe.device_class
        # Verbatim, not a prefix: a 40-char prefix match let the doc and the
        # code drift apart in the tail and still pass.
        assert probe.why_safe in doc, probe.device_class
        assert len(probe.why_safe) > 40, probe.device_class
    for refusal in R.REFUSED:
        assert f"`{refusal.device_class}`" in doc, refusal.device_class
        assert refusal.why_unsafe in doc, refusal.device_class


def test_no_service_or_unit_file_is_installed_on_the_robot():
    """E8 owns packaging; a systemd unit in this diff is a bounce."""
    text = SRC.read_text().lower()
    for forbidden in ("systemctl", "systemd", ".service", "unit file", "apt-get", "pip install"):
        assert forbidden not in text, forbidden


def test_no_mcp_tool_opens_a_connection():
    """Deliberate: an agent does not open ssh sessions. No MCP tool reaches the
    remote transport."""
    mcp = (Path(R.__file__).parents[1] / "lockwire" / "mcp_server.py").read_text()
    assert "sample_remote" not in mcp
    assert "ssh" not in mcp


# --- failure posture -------------------------------------------------------


def test_an_unreachable_host_fails_clearly_instead_of_hanging(tmp_path, monkeypatch, known_hosts):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.5
        raise subprocess.TimeoutExpired(args[0], 0.5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(OSError, match="unreachable"):
        R.fetch_snapshot("robot.local", tmp_path, known_hosts=known_hosts, timeout=0.5)


def test_a_missing_ssh_binary_is_reported(tmp_path, monkeypatch, known_hosts):
    monkeypatch.setattr(subprocess, "run", _raise(FileNotFoundError("no ssh")))
    with pytest.raises(OSError, match="not found on this machine"):
        R.fetch_snapshot("robot.local", tmp_path, known_hosts=known_hosts)


def _raise(error):
    def go(*args, **kwargs):
        raise error

    return go


# --- end to end, through a fake ssh on PATH -------------------------------

_PAYLOAD_FILES = {
    "/sys/class/thermal/thermal_zone0/type": "cpu-thermal\n",
    "/sys/class/thermal/thermal_zone0/temp": "45123\n",
    "/sys/class/thermal/cooling_device0/cur_state": "0\n",
    "/sys/class/hwmon/hwmon0/name": "ina3221\n",
    "/sys/class/hwmon/hwmon0/in1_input": "5000\n",
    "/sys/class/hwmon/hwmon0/power1_input": "7500000\n",
    "/proc/loadavg": "0.50 0.40 0.30 1/200 12345\n",
    "/proc/net/dev": (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    "
        "packets errs drop fifo colls carrier compressed\n"
        "  eth0: 100 1 0 3 0 0 0 0 200 2 0 0 0 0 0 0\n"
    ),
}


def _fake_ssh(tmp_path: Path) -> Path:
    """A stub transport: an executable named ``ssh`` that replays one recorded
    snapshot payload and logs the argv it was given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = json.dumps({"files": _PAYLOAD_FILES, "node": "robot", "release": "6.1.0-rpi"})
    script = bin_dir / "ssh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({payload!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_a_stubbed_transport_produces_a_recording_the_normal_loader_reads(
    tmp_path, monkeypatch, known_hosts
):
    monkeypatch.setenv("PATH", f"{_fake_ssh(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    out = tmp_path / "rec"
    R.sample_remote(host="pi@robot.local", out_dir=out, port=2222, known_hosts=known_hosts)

    recording = load_recording(out)
    stream = recording.devices["dev.compute"]
    # Anchored outside both paths: literal expected values from the payload,
    # not a comparison of the remote path against the local one.
    assert stream.signals["thermal/cpu-thermal"][0] == pytest.approx(45.123)
    assert stream.signals["hwmon/ina3221/in1"][0] == pytest.approx(5.0)
    assert stream.signals["hwmon/ina3221/power1"][0] == pytest.approx(7.5)
    assert stream.signals["net/eth0/rx_dropped"][0] == pytest.approx(3.0)

    manifest = (out / "manifest.yaml").read_text()
    assert "lockwire sample --host" in manifest
    assert "robot" in manifest  # the robot's name, not this laptop's

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[-3:] == ["pi@robot.local", "python3", "-"]
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"


def test_the_cli_reaches_the_same_transport(tmp_path, monkeypatch, known_hosts):
    monkeypatch.setenv("PATH", f"{_fake_ssh(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    out = tmp_path / "rec"
    result = CliRunner().invoke(
        app,
        ["sample", "--host", "pi@robot.local", "--out", str(out)]
        + ["--known-hosts", str(known_hosts)],
    )
    assert result.exit_code == 0, result.output
    assert load_recording(out).devices["dev.compute"].signals["loadavg/1m"][0] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["sample", "--out", "X"], "pass --local or --host"),
        (["sample", "--local", "--host", "h", "--out", "X"], "not both"),
        (["sample", "--host", "-oProxyCommand=x", "--out", "X"], "invalid --host"),
    ],
)
def test_the_cli_refuses_an_impossible_or_hostile_source(tmp_path, args, message):
    args = [str(tmp_path / "rec") if a == "X" else a for a in args]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert message in result.output


def test_vcgencmd_is_absent_for_a_mirrored_tree(tmp_path, monkeypatch):
    """A command on *this* machine must never be spliced into a recording of
    another one — nor into a fixture-tree sample."""
    monkeypatch.setattr("lockwire_pro.sample_local.shutil.which", lambda _name: "/usr/bin/vcgencmd")
    _, absent, _unreadable = discover_readers(tmp_path / "sys", tmp_path / "proc")
    assert "vcgencmd" in absent


@pytest.mark.parametrize(
    "options", [{}, {"port": 0}, {"port": "22"}, {"connect_timeout": 0}, {"timeout": float("nan")}]
)
def test_bad_remote_arguments_refuse_before_process(tmp_path, monkeypatch, known_hosts, options):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("process started"))
    if options:
        options["known_hosts"] = known_hosts
    with pytest.raises(ValueError):
        R.sample_remote(host="robot.local", out_dir=tmp_path / "rec", **options)
    assert not (tmp_path / "rec").exists()


@pytest.mark.parametrize("failure", ["unknown host", "REMOTE HOST IDENTIFICATION HAS CHANGED"])
def test_pin_failure_redacts_path_and_preserves_output(tmp_path, monkeypatch, known_hosts, failure):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", f"{failure}: {known_hosts}"),
    )
    out = tmp_path / "rec"
    out.mkdir()
    (out / "keep").write_text("unchanged")
    result = CliRunner().invoke(
        app,
        ["sample", "--host", "robot.local", "--known-hosts", str(known_hosts), "--out", str(out)],
    )
    assert result.exit_code == 2 and failure in result.output
    assert str(known_hosts) not in result.output and "(known-hosts)" in result.output
    assert list(out.iterdir()) == [out / "keep"] and (out / "keep").read_text() == "unchanged"


def test_invalid_remote_reply_writes_nothing(tmp_path, monkeypatch, known_hosts):
    payload = json.dumps({"files": {"/proc/loadavg": "ok", "/etc/passwd": "bad"}})
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, payload, "")
    )
    mirror = tmp_path / "mirror"
    with pytest.raises(OSError, match="unreadable snapshot"):
        R.fetch_snapshot("robot.local", mirror, known_hosts=known_hosts)
    assert not mirror.exists()
