"""``lockwire sample --host`` — the *same* ``/sys`` + ``/proc`` sampler, reached
over ssh ("ssh by default, the port as an option").

**This is a transport, not a second sampler** (one evaluator). Every parser, unit divisor
and signal name lives in :mod:`lockwire_pro.sample_local` and is reused untouched:
this module copies a small, fixed set of read-only files off the robot into a
temporary mirror directory and then calls :func:`lockwire_pro.sample_local.sample_local`
against that mirror. If a divisor is ever wrong it is wrong in exactly one place.

**Lockwire never actuates.** Every probe is a ``read()`` of a file under
``/sys`` or ``/proc``. The shared, single list of what is read is :data:`PROBES`,
and :data:`REFUSED` names the device classes whose reads are *not* safe (a
register cleared on read, a bus transaction that moves hardware) and which this
module therefore refuses to touch at all. ``docs/probes.md`` argues the safety of
each class; ``tests/test_sample_remote.py`` holds the doc and the code to each
other. **Untested on hardware** — no real board has run this.

**The shell is a non-door** (E0's injection fix). ``ssh`` is invoked with an
argument *list* and never a shell string; the remote command is a constant
(``python3 -``) with the program delivered on stdin, so no caller-supplied text
is ever interpreted by any shell, local or remote. The destination is validated
against a strict charset and passed after ``--`` so a hostile "hostname" such as
``-oProxyCommand=...`` cannot be read by ssh as an option.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from lockwire_pro.recording import MANIFEST
from lockwire_pro.sample_local import DEFAULT_DEVICE, sample_local

DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Probe:
    """One device class, the read-only paths it is read through, and the reason
    a read of those paths cannot move hardware."""

    device_class: str
    paths: tuple[str, ...]
    why_safe: str


# The single shared probe list: what the remote helper globs, what
# ``docs/probes.md`` must argue for, and what the never-actuates guard tests walk. Adding a
# class here without a documented reason is a test failure, by design.
PROBES: tuple[Probe, ...] = (
    Probe(
        device_class="thermal",
        paths=(
            "/sys/class/thermal/thermal_zone*/type",
            "/sys/class/thermal/thermal_zone*/temp",
        ),
        why_safe=(
            "sysfs thermal attributes are kernel-cached values re-read from the "
            "SoC sensor by the thermal core on its own timer; the open/read does "
            "not issue a bus transaction and clears nothing."
        ),
    ),
    Probe(
        device_class="cooling",
        paths=("/sys/class/thermal/cooling_device*/cur_state",),
        why_safe=(
            "cur_state is readable and writable; only the write actuates a fan or "
            "a frequency cap. The read returns the cooling device's current state "
            "and has no side effect. lockwire never opens it for writing."
        ),
    ),
    Probe(
        device_class="hwmon",
        paths=(
            "/sys/class/hwmon/hwmon*/name",
            "/sys/class/hwmon/hwmon*/in*_input",
            "/sys/class/hwmon/hwmon*/curr*_input",
            "/sys/class/hwmon/hwmon*/temp*_input",
            "/sys/class/hwmon/hwmon*/power*_input",
            "/sys/class/hwmon/hwmon*/energy*_input",
        ),
        why_safe=(
            "Only the *_input attributes are read. The kernel hwmon driver owns "
            "the bus and performs the conversion; the sysfs read returns its "
            "result. The alarm/limit attributes that a read *does* clear on some "
            "chips (*_alarm, *_max_alarm) are deliberately not in this list."
        ),
    ),
    Probe(
        device_class="loadavg",
        paths=("/proc/loadavg",),
        why_safe="A procfs counter of the scheduler's own state. No device is involved.",
    ),
    Probe(
        device_class="net",
        paths=("/proc/net/dev",),
        why_safe=(
            "Monotonic per-interface counters kept by the network stack. Reading "
            "them does not reset them (unlike an ethtool register dump on some NICs)."
        ),
    ),
)


@dataclass(frozen=True)
class Refusal:
    """A device class whose reads are *not* safe, and which this module refuses."""

    device_class: str
    why_unsafe: str


REFUSED: tuple[Refusal, ...] = (
    Refusal(
        device_class="i2c/smbus",
        why_unsafe=(
            "A read is a bus transaction the host originates: an address write "
            "followed by a read. Many registers are read-to-clear (INA219 status, "
            "PCA9685 fault bits) and a raw probe of an unknown address can latch "
            "a device into a mode. Refused: the kernel driver's hwmon view is read "
            "instead."
        ),
    ),
    Refusal(
        device_class="serial/uart",
        why_unsafe=(
            "Opening a tty asserts modem control lines (DTR/RTS) and resets many "
            "boards, and a protocol read requires sending a request frame first — "
            "a transient command that can move hardware. Refused entirely."
        ),
    ),
    Refusal(
        device_class="spi",
        why_unsafe=(
            "SPI has no read without a write: every transfer clocks bytes out as "
            "it clocks bytes in. There is no non-actuating probe. Refused."
        ),
    ),
    Refusal(
        device_class="vcgencmd",
        why_unsafe=(
            "Not unsafe, but not transportable: it is a command run on the board, "
            "not a file, so it cannot be mirrored. `sample --host` reports it as an "
            "absent category rather than splicing in the *local* machine's answer."
        ),
    ),
)

# The whole remote program. It is a constant: nothing a caller supplies is ever
# interpolated into it, and it is delivered on stdin rather than as an argument,
# so no shell (local or remote) ever sees it. It opens files for reading only.
_REMOTE_PROGRAM = (
    "import glob, json, os, sys\n"
    "out = {}\n"
    "for pat in " + json.dumps([p for probe in PROBES for p in probe.paths]) + ":\n"
    "    for path in glob.glob(pat):\n"
    "        if not os.path.isfile(path):\n"
    "            continue\n"
    "        try:\n"
    "            with open(path) as handle:\n"
    "                out[path] = handle.read(65536)\n"
    "        except OSError:\n"
    "            continue\n"
    "u = os.uname()\n"
    'json.dump({"files": out, "node": u.nodename, "release": u.release}, sys.stdout)\n'
)

# user@host / host, hostname or IPv4 only. Deliberately strict: it forbids the
# leading "-" that would make ssh read the destination as an option, and every
# shell metacharacter. IPv6 literals and ssh:// URLs are not accepted (say so
# rather than loosening the charset — use a Host alias in ~/.ssh/config).
_HOST = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_invocation(
    host: str,
    *,
    known_hosts: Path | None = None,
    port: int | None = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    ssh: str = "ssh",
) -> list[str]:
    """The ssh argument **list** for one snapshot. Never a string, never a shell.

    ``port`` is optional (ssh alone must work). Raises ``ValueError``
    for a destination outside :data:`_HOST` — before any process is started.
    Host aliases, routing and authentication still use SSH configuration, but
    host verification uses only ``known_hosts``. It neither learns extra keys
    into that file nor reuses a connection authenticated against another pin.
    The file must already exist; unknown or changed host keys fail.
    """
    if not _HOST.fullmatch(host):
        raise ValueError(
            f"invalid --host {host!r}: expected [user@]hostname using "
            f"letters, digits, dot, dash or underscore"
        )
    if known_hosts is None:
        raise ValueError("--host requires an explicit --known-hosts file")
    pin = str(known_hosts.absolute())
    if any(char in pin for char in ("\n", "\r", "\x00", "%", "$")):
        raise ValueError("invalid --known-hosts filename")
    try:
        exists = known_hosts.is_file()
    except OSError as error:
        raise ValueError("--known-hosts file is inaccessible") from error
    if not exists:
        raise ValueError("--known-hosts must name an existing file")
    if type(connect_timeout) is not int or connect_timeout <= 0:
        raise ValueError("invalid connect timeout: expected a positive integer")
    # OpenSSH splits this option as a filename list even though argv is a list.
    quoted_pin = '"' + pin.replace("\\", "\\\\").replace('"', '\\"') + '"'
    argv = [
        ssh,
        "-T",  # no pty: the program arrives on stdin
        "-o",
        "BatchMode=yes",  # never prompt for a password; fail instead of hanging
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={quoted_pin}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        # The caller's file is the sole trust source, including when a Host
        # alias configures other sources or skips verification on loopback.
        "-o",
        "KnownHostsCommand=none",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "NoHostAuthenticationForLocalhost=no",
        # Do not grow the pin or reuse a connection verified with other pins.
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ControlPath=none",
        "-o",
        f"ConnectTimeout={int(connect_timeout)}",
    ]
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"invalid --port {port!r}: expected 1-65535")
        argv += ["-p", str(int(port))]
    argv += ["--", host, "python3", "-"]
    return argv


def _validate_remote_path(path: str) -> Path:
    """A path the *robot* sent back is untrusted: it must be one of ours."""
    if not path.startswith(("/sys/", "/proc/")) or ".." in Path(path).parts:
        raise ValueError(f"remote returned a path outside /sys and /proc: {path!r}")
    return Path(path)


def fetch_snapshot(
    host: str,
    mirror: Path,
    *,
    known_hosts: Path | None = None,
    port: int | None = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, str]:
    """Copy one read-only snapshot of the robot's ``/sys`` and ``/proc`` probe
    files into ``mirror``. Returns the remote's ``{node, release}``.

    Raises ``OSError`` — never hangs — when ssh is missing, cannot connect, or
    the robot has no ``python3``.
    """
    argv = build_invocation(
        host, known_hosts=known_hosts, port=port, connect_timeout=connect_timeout
    )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout < float("inf")
    ):
        raise ValueError("invalid timeout: expected a finite positive number")

    def redacted(detail: str) -> str:
        assert known_hosts is not None
        for pin in (str(known_hosts.absolute()), str(known_hosts)):
            detail = detail.replace(pin, "(known-hosts)")
        return detail

    try:
        completed = subprocess.run(
            argv,
            input=_REMOTE_PROGRAM,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise OSError(f"{argv[0]}: not found on this machine ({redacted(str(error))})") from error
    except subprocess.TimeoutExpired as error:
        raise OSError(
            f"{host}: no snapshot within {timeout:g}s — the host may be unreachable "
            f"or the sampler hung; nothing was written"
        ) from error
    except OSError as error:
        raise OSError(f"{host}: {redacted(str(error))}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"ssh exited {completed.returncode}"
        raise OSError(f"{host}: {redacted(detail)}")
    try:
        payload = json.loads(completed.stdout)
        files = payload["files"]
        if not isinstance(files, dict) or any(
            not isinstance(path, str) or not isinstance(text, str) for path, text in files.items()
        ):
            raise ValueError("files must be an object of path/text strings")
        paths = {path: _validate_remote_path(path) for path in files}
    except (KeyError, TypeError, ValueError) as error:
        raise OSError(f"{host}: unreadable snapshot ({redacted(str(error))})") from error

    for remote_path, text in files.items():
        local = mirror / paths[remote_path].relative_to("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text)
    return {"node": str(payload.get("node", host)), "release": str(payload.get("release", ""))}


def sample_remote(
    *,
    host: str,
    out_dir: Path,
    known_hosts: Path | None = None,
    port: int | None = None,
    device: str = DEFAULT_DEVICE,
    contract_path: Path | None = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Path:
    """One snapshot of ``host``'s own compute, written to ``out_dir`` as a normal
    lockwire recording. Reuses :func:`sample_local` for every parse and divisor.

    ponytail: one snapshot per invocation, so there is no ``--seconds``/``--hz``
    here — a timed series over ssh would be one round trip per tick. The real
    answer is the on-robot sampler E8 packages; this is the honest minimum until
    then, and a one-sample recording scores through preflight exactly as any other.
    """
    with tempfile.TemporaryDirectory(prefix="lockwire-remote-") as scratch:
        mirror = Path(scratch)
        remote = fetch_snapshot(
            host,
            mirror,
            known_hosts=known_hosts,
            port=port,
            connect_timeout=connect_timeout,
            timeout=timeout,
        )
        sample_local(
            seconds=1.0,
            hz=1.0,  # -> exactly one tick; the mirror is a single snapshot
            out_dir=out_dir,
            device=device,
            sysfs_root=mirror / "sys",
            proc_root=mirror / "proc",
            contract_path=contract_path,
        )
    _retag_manifest(out_dir, host=host, remote=remote)
    return out_dir


def _retag_manifest(out_dir: Path, *, host: str, remote: dict[str, str]) -> None:
    """``sample_local`` stamps the manifest with *this* machine's name; the
    recording is the robot's, so say whose it is."""
    yaml = YAML()
    path = out_dir / MANIFEST
    with path.open() as handle:
        manifest = yaml.load(handle)
    manifest["generator"]["source"] = "lockwire sample --host"
    manifest["generator"]["host"] = remote["node"] or host
    manifest["generator"]["kernel"] = remote["release"]
    manifest["generator"]["via"] = host
    with path.open("w") as handle:
        yaml.dump(manifest, handle)
