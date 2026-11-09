"""``lockwire sample --local`` — record the robot's own compute as a recording.

Health "in a uart or spi way... all of these to be automatic", "installing a
thing on the main compute" — the first sampler needs no hardware at all, because
every robot's computer already exposes its own health under ``/sys`` and
``/proc``. This reads those files (stdlib only: ``pathlib``/``subprocess``, no
new dependency) for a bounded window and writes them out as a normal lockwire
recording (``manifest.yaml`` + ``samples.jsonl``, see ``recording.py``) under one
device id, so the same ``preflight``/``checks.py`` machinery that scores a bag
scores this too.

Sources read (research: p1_compute_health_sources.md), all read-only, all
silently skipped when absent (root is never required):

- ``/sys/class/thermal/thermal_zone*/{type,temp}`` -> ``thermal/<type>`` (degC)
- ``/sys/class/hwmon/hwmon*/{name,in*_input,curr*_input,power*_input,temp*_input,
  energy*_input}`` -> ``hwmon/<name>/{in,curr,power,temp,energy}N`` (V/A/W/degC/J;
  Jetson INA3221 rails appear here as ``hwmon/ina3221/in1`` etc.)
- ``/proc/loadavg`` -> ``loadavg/{1m,5m,15m}`` (dimensionless)
- ``/proc/net/dev`` -> ``net/<iface>/{rx,tx}_{errors,dropped}`` (counters)
- ``/sys/class/thermal/cooling_device*/cur_state`` -> ``cooling/<name>/cur_state``
- Raspberry Pi ``vcgencmd get_throttled`` / ``measure_temp``, only if the binary
  is on PATH (subprocess, 2 s timeout) -> ``vcgencmd/{throttled,temp}``

Kernel doc ``Documentation/ABI/testing/sysfs-class-hwmon`` is the source of the
hwmon divisors: ``in``/``curr``/``temp`` are milli-units (/1e3) but ``power``
(microWatt) and ``energy`` (microJoule) are micro-units (/1e6) — a box H-b
review bounce on 87d3a30 found ``power1_input`` divided by 1e3 instead of 1e6
(7.5 W read back as 7500.0 W). The thermal path is unaffected (still /1000);
the mutation bite this box's brief calls for is dropping that division there.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


from lockwire.hw import plain_yaml
from lockwire_pro.recording import MANIFEST, SAMPLES

DEFAULT_DEVICE = "dev.compute"
# Sampling the compute at rest, with no declared operating context to match —
# "bench-idle" is the closest fit of the 3 contexts. Owner default, flagged.
DEFAULT_CONTEXT = "bench-idle"
_VCGENCMD_TIMEOUT_S = 2.0


@dataclass
class _Reader:
    signal: str
    unit: str
    read: Callable[[], float]


def _read_float(path: Path) -> float:
    return float(path.read_text().strip())


def _discover_thermal(sysfs_root: Path) -> list[_Reader]:
    base = sysfs_root / "class" / "thermal"
    readers: list[_Reader] = []
    if not base.is_dir():
        return readers
    for zone in sorted(base.glob("thermal_zone*")):
        type_file, temp_file = zone / "type", zone / "temp"
        if not type_file.is_file() or not temp_file.is_file():
            continue
        try:
            zone_type = type_file.read_text().strip()
            _read_float(temp_file)  # verify readable now; skip silently if not
        except OSError:
            continue

        def _read(temp_file: Path = temp_file) -> float:
            return _read_float(temp_file) / 1000.0

        readers.append(_Reader(signal=f"thermal/{zone_type}", unit="degC", read=_read))
    return readers


def _discover_cooling(sysfs_root: Path) -> list[_Reader]:
    base = sysfs_root / "class" / "thermal"
    readers: list[_Reader] = []
    if not base.is_dir():
        return readers
    for dev in sorted(base.glob("cooling_device*")):
        state_file = dev / "cur_state"
        if not state_file.is_file():
            continue
        try:
            _read_float(state_file)
        except OSError:
            continue

        def _read(state_file: Path = state_file) -> float:
            return _read_float(state_file)

        readers.append(_Reader(signal=f"cooling/{dev.name}/cur_state", unit="1", read=_read))
    return readers


# hwmon sysfs prefix -> (unit, divisor), per kernel doc
# Documentation/ABI/testing/sysfs-class-hwmon: in/curr/temp are milli-units,
# power/energy are micro-units. A prefix not in this map is never globbed, so
# it is simply not read (not listed as "unreadable" — that key is reserved for
# a file this map does expect but the kernel won't let us read).
_HWMON_KINDS: dict[str, tuple[str, float]] = {
    "in": ("V", 1e3),
    "curr": ("A", 1e3),
    "temp": ("degC", 1e3),
    "power": ("W", 1e6),
    "energy": ("J", 1e6),
}


def _discover_hwmon(sysfs_root: Path, unreadable: list[str]) -> list[_Reader]:
    base = sysfs_root / "class" / "hwmon"
    readers: list[_Reader] = []
    if not base.is_dir():
        return readers
    for chip in sorted(base.glob("hwmon*")):
        name_file = chip / "name"
        if not name_file.is_file():
            continue
        try:
            name = name_file.read_text().strip()
        except OSError:
            continue
        for prefix, (unit, divisor) in _HWMON_KINDS.items():
            for input_file in sorted(chip.glob(f"{prefix}*_input")):
                channel = input_file.name.removesuffix("_input")  # e.g. "in1", "curr1"
                try:
                    _read_float(input_file)
                except OSError:
                    unreadable.append(f"hwmon/{name}/{channel}")
                    continue

                def _read(input_file: Path = input_file, divisor: float = divisor) -> float:
                    return _read_float(input_file) / divisor

                readers.append(_Reader(signal=f"hwmon/{name}/{channel}", unit=unit, read=_read))
    return readers


def _discover_loadavg(proc_root: Path) -> list[_Reader]:
    path = proc_root / "loadavg"
    if not path.is_file():
        return []
    try:
        path.read_text()
    except OSError:
        return []

    def _make(index: int) -> Callable[[], float]:
        def _read() -> float:
            return float(path.read_text().split()[index])

        return _read

    return [
        _Reader(signal=f"loadavg/{label}", unit="1", read=_make(i))
        for i, label in enumerate(("1m", "5m", "15m"))
    ]


# /proc/net/dev fields after the interface name: 8 receive then 8 transmit
# columns (bytes packets errs drop fifo frame compressed multicast | bytes
# packets errs drop fifo colls carrier compressed).
_NET_FIELDS = {"rx_errors": 2, "rx_dropped": 3, "tx_errors": 10, "tx_dropped": 11}


def _discover_net(proc_root: Path) -> list[_Reader]:
    path = proc_root / "net" / "dev"
    if not path.is_file():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    ifaces: list[str] = []
    for line in lines[2:]:  # first 2 lines are the header
        line = line.strip()
        if not line or ":" not in line:
            continue
        ifaces.append(line.split(":", 1)[0].strip())

    def _make(iface: str, col: int) -> Callable[[], float]:
        def _read() -> float:
            for line in path.read_text().splitlines()[2:]:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                iface_name, rest = line.split(":", 1)
                if iface_name.strip() != iface:
                    continue
                return float(rest.split()[col])
            raise OSError(f"{iface}: no longer in {path}")

        return _read

    return [
        _Reader(signal=f"net/{iface}/{field}", unit="1", read=_make(iface, col))
        for iface in ifaces
        for field, col in _NET_FIELDS.items()
    ]


def _discover_vcgencmd() -> list[_Reader]:
    if shutil.which("vcgencmd") is None:
        return []

    def _run(*args: str) -> str:
        result = subprocess.run(
            ["vcgencmd", *args],
            capture_output=True,
            text=True,
            timeout=_VCGENCMD_TIMEOUT_S,
            check=False,
        )
        return result.stdout.strip()

    try:
        _run("get_throttled")
        _run("measure_temp")
    except (OSError, subprocess.SubprocessError):
        return []

    def _read_throttled() -> float:
        out = _run("get_throttled")  # "throttled=0x50000"
        return float(int(out.split("=", 1)[1], 16))

    def _read_temp() -> float:
        out = _run("measure_temp")  # "temp=53.8'C"
        return float(out.split("=", 1)[1].rstrip("'C"))

    return [
        _Reader(signal="vcgencmd/throttled", unit="1", read=_read_throttled),
        _Reader(signal="vcgencmd/temp", unit="degC", read=_read_temp),
    ]


# category name -> discovery function; used both to build readers and to report
# which whole categories were absent on this machine. Every function takes the
# same (sysfs_root, proc_root, unreadable) signature; only hwmon uses the third
# argument today (a per-file readable-but-listed-as-such log).
_CATEGORIES: dict[str, Callable[[Path, Path, list[str]], list[_Reader]]] = {
    "thermal": lambda sysfs_root, proc_root, unreadable: _discover_thermal(sysfs_root),
    "hwmon": lambda sysfs_root, proc_root, unreadable: _discover_hwmon(sysfs_root, unreadable),
    "cooling": lambda sysfs_root, proc_root, unreadable: _discover_cooling(sysfs_root),
    "loadavg": lambda sysfs_root, proc_root, unreadable: _discover_loadavg(proc_root),
    "net": lambda sysfs_root, proc_root, unreadable: _discover_net(proc_root),
    # vcgencmd is the one category that is a *command on this machine*, not a
    # file under the roots — so it is only honest when the roots are the real
    # ones. Against a mirrored or fake tree (``sample --host``, a fixture) it
    # would splice the local board's readings into someone else's recording.
    "vcgencmd": lambda sysfs_root, proc_root, unreadable: (
        _discover_vcgencmd() if sysfs_root == Path("/sys") else []
    ),
}


def discover_readers(
    sysfs_root: Path, proc_root: Path
) -> tuple[list[_Reader], list[str], list[str]]:
    """Return (readers, absent_categories, unreadable_signals) for this machine
    (or a fake tree). ``unreadable`` names individual sysfs files that exist
    but raised ``OSError`` on read (e.g. permission denied) — the category
    itself is not counted absent just because one of its files was."""
    readers: list[_Reader] = []
    absent: list[str] = []
    unreadable: list[str] = []
    for category, discover in _CATEGORIES.items():
        found = discover(sysfs_root, proc_root, unreadable)
        if found:
            readers.extend(found)
        else:
            absent.append(category)
    return readers, absent, unreadable


def undeclared_signals(contract, device: str, signals: list[str]) -> list[str]:
    """Signals this recording carries for ``device`` that ``contract`` never
    declares an envelope for — they will score as not compared (never fails)."""
    declared: set[str] = set()
    for instance in contract.devices:
        if instance.id != device:
            continue
        for envelope in instance.envelopes:
            declared.update(envelope.signals)
    return [signal for signal in signals if signal not in declared]


def sample_local(
    *,
    seconds: float,
    hz: float = 1.0,
    out_dir: Path,
    device: str = DEFAULT_DEVICE,
    sysfs_root: Path = Path("/sys"),
    proc_root: Path = Path("/proc"),
    contract_path: Path | None = None,
) -> Path:
    """Sample this machine's own /sys + /proc health for ``seconds`` at ``hz``,
    writing a lockwire recording directory to ``out_dir``. Returns ``out_dir``."""
    if hz <= 0:
        raise ValueError(f"hz must be positive, got {hz}")
    if seconds <= 0:
        raise ValueError(f"seconds must be positive, got {seconds}")

    readers, absent, unreadable = discover_readers(sysfs_root, proc_root)
    signal_names = sorted(r.signal for r in readers)
    units = {r.signal: r.unit for r in readers}

    period = 1.0 / hz
    n_ticks = max(1, round(seconds * hz))
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    sample_lines: list[str] = []
    for i in range(n_ticks):
        target = start + i * period
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        t = time.monotonic() - start
        sig = {r.signal: r.read() for r in readers}
        sample_lines.append(json.dumps({"t": t, "dev": device, "sig": sig}, sort_keys=True))

    (out_dir / SAMPLES).write_text("\n".join(sample_lines) + ("\n" if sample_lines else ""))

    manifest = {
        "context": DEFAULT_CONTEXT,
        "format_version": "1.0",
        "generator": {
            "source": "lockwire sample --local",
            "host": platform.node(),
            "kernel": platform.release(),
            "absent": absent,
            "unreadable": unreadable,
        },
        "units": {device: units},
    }
    y = plain_yaml()
    with (out_dir / MANIFEST).open("w") as handle:
        y.dump(manifest, handle)

    if contract_path is not None:
        from lockwire.schema import load_contract

        contract = load_contract(contract_path)
        missing = undeclared_signals(contract, device, signal_names)
        for signal in missing:
            print(
                f"warn: {contract_path} does not declare signal {signal!r} for "
                f"device {device!r} — it will score as not compared"
            )

    return out_dir
