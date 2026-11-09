"""The recorded-run format: a dependency-free stand-in for a rosbag/mcap.

Real capstone bags are lost, so this format *is* the de-facto interface every
offline check reads — it gets schema-level care (spec: schema mistakes are the
expensive ones). What it must carry, from the spec:

- per-device messages keyed by **receive-time**, not sensor-time — rate and
  monotonicity are computed on recv-time and must tolerate driver batching
  (the clock trap). Each message carries all of a device's signal fields under
  one recv timestamp, mirroring one ROS message on a topic. Rows may interleave
  devices, but each device's messages must be in nondecreasing receive-time
  order; loading rejects violations rather than silently reordering them.
- environment channels (temp / pressure / voltage) as first-class samples
  alongside device signals — these populate the condition envelope.

A recording is a directory:

    rec/
      manifest.yaml     # context + per-device signal units
      samples.jsonl      # one JSON object per line, streamable
      commands.jsonl     # optional: the command record (command_record.py)

Device signal units are scoped by device ID; environment channels use the
reserved ``environment`` scope:

    units:
      dev.celsius:
        temp: degC
      dev.kelvin:
        temp: K
      environment:
        temp_c: degC

`samples.jsonl` rows are either a device message or an environment sample:

    {"t": 0.005, "dev": "dev.imu", "sig": {"accel_z": -9.81}}
    {"t": 0.000, "env": {"temp_c": 21.3}}

Deliberately *not* rosbag/mcap: those are speculative until a design partner's
stack demands them (M1). Rows-not-one-object keeps it streamable so it *could*
scale; M0 does not build for scale.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ruamel.yaml import YAML

from lockwire.schema import CONTEXTS, ArtifactRef, _load_yaml_guarded, read_artifact
from lockwire.scan import CommandRunner

MANIFEST = "manifest.yaml"
SAMPLES = "samples.jsonl"
# Optional sidecar beside a recording: the command record.
# Written and read by `command_record.py`; `load_recording` ignores it.
COMMANDS = "commands.jsonl"
_MANIFEST_KEYS = frozenset(
    {
        "bag",
        # Seconds the recorder was open (live capture); liveness divides by it.
        "capture_window_s",
        "channel_models",
        "context",
        "dds_deadlines",
        "format_version",
        "generator",
        # B10 ground-truth scaffolding has no consumer; accepting it here leaves D3 unsatisfied.
        "planted_failures",
        # A cancelled live capture, marked at write time (E3-QC); see `Recording.partial`.
        "partial",
        "procedures",
        "seed",
        "units",
    }
)


@dataclass
class DeviceStream:
    """One device's messages across the run, in arrival order."""

    device: str
    recv_times: list[float] = field(default_factory=list)  # receive-time, seconds
    signals: dict[str, list[float]] = field(default_factory=dict)  # signal -> values

    @property
    def duration_s(self) -> float:
        if len(self.recv_times) < 2:
            return 0.0
        return self.recv_times[-1] - self.recv_times[0]


@dataclass
class Recording:
    context: str
    units: dict[str, dict[str, str]]  # device id (or "environment") -> signal -> unit
    devices: dict[str, DeviceStream]  # device id -> stream
    env: dict[str, list[tuple[float, float]]]  # env channel -> [(recv_time, value)]
    sha256: str  # over manifest + samples — the recording hash that travels with a lock
    path: Path
    # Runtime loader provenance; never serialized. A recording built by
    # `load_recording` leaves this None — the value names a foreign format this
    # recording was derived from, not the format it is stored in.
    source: Literal["ulog", "ros2bag"] | None = None
    # E3-FL: whether `load_ros2_bag` saw every device's messages already in
    # non-decreasing `log_time` order, read file-order (`log_time_order=False`,
    # true receipt/write order) *before* its own defensive per-device sort ran.
    # `load_recording` (the native format) enforces monotonicity by REJECTING a
    # violation; a `.mcap` bag load enforces it by silently REORDERING one — this
    # field is what makes that reordering observable instead of hidden. `None`
    # when the fact was never determined (native format, or PX4 ulog — both
    # leave `source` at `None`/`"ulog"`; only `load_ros2_bag` sets this).
    # Never serialized, runtime-only, same as `source`.
    receipt_monotonic: bool | None = None
    # A cancelled live capture (E3-QC): the container-side recorder was stopped
    # before its window ended, proved by the corpse (jobs.kill_container_process),
    # so whatever it wrote is a truncated bag, not a complete one. `False` by
    # default — every existing caller keeps its current, complete-bag meaning.
    # Never inferred from row counts; only a caller that knows the capture was
    # cut short sets it.
    partial: bool = False
    # The manifest's own `bag.duration_s`, if it states one (the under-floor check compares it
    # against the observed sample span). None for mcap/ulog and older manifests.
    manifest_duration_s: float | None = None
    # How long the recorder was open, when the writer knows (live capture node, LiveSource).
    # Gross liveness divides by it so a delivery burst cannot shrink the span. None = unknown.
    capture_window_s: float | None = None
    row_count: int = 0  # parsed nonblank sample rows, in the original file order
    dds_deadlines: dict[str, float] = field(default_factory=dict)
    dds_deadline_misses: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    # device -> procedure id -> (kind, selected signal, discrete code).
    # The selected signal and code travel with the evidence; an identifier and
    # kind alone are insufficient to bind an observed marker to the contract.
    procedures: dict[str, dict[str, tuple[str, str, int]]] = field(default_factory=dict)
    # device -> procedure id -> [(receive time, kind, selected signal, code)].
    procedure_events: dict[str, dict[str, list[tuple[float, str, str, int]]]] = field(
        default_factory=dict
    )

    def unit_of(self, channel: str, *, device: str | None = None) -> str:
        if device is not None:
            try:
                return self.units[device][channel]
            except KeyError:
                raise KeyError(
                    f"recording {self.path}: no unit declared for signal {channel!r} "
                    f"on device {device!r}"
                ) from None

        matches = {
            device_id: device_units[channel]
            for device_id, device_units in self.units.items()
            if channel in device_units
        }
        if not matches:
            raise KeyError(f"recording {self.path}: no unit declared for channel {channel!r}")
        distinct_units = set(matches.values())
        if len(distinct_units) > 1:
            raise KeyError(
                f"recording {self.path}: signal {channel!r} has device-specific units; "
                "pass device=..."
            )
        return next(iter(distinct_units))


def _refuse_nonfinite(token: str) -> float:
    raise ValueError(f"non-finite number {token} is not allowed in a recording")


def _recording_sha256(manifest_raw: bytes, samples_raw: bytes) -> str:
    """Hash both files with unambiguous boundaries."""
    digest = hashlib.sha256()
    for name, raw in ((MANIFEST, manifest_raw), (SAMPLES, samples_raw)):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def load_recording(path: Path) -> Recording:
    path = Path(path)
    manifest_path = path / MANIFEST
    samples_path = path / SAMPLES
    if not manifest_path.exists():
        raise FileNotFoundError(f"{path}: no {MANIFEST}")
    if not samples_path.exists():
        raise FileNotFoundError(f"{path}: no {SAMPLES}")

    y = YAML()
    manifest_raw = manifest_path.read_bytes()
    manifest = _load_yaml_guarded(y, manifest_raw.decode())
    unknown_keys = sorted(set(manifest) - _MANIFEST_KEYS)
    if unknown_keys:
        raise ValueError(f"{manifest_path}: unknown manifest keys: {unknown_keys}")
    context = manifest["context"]
    if context not in CONTEXTS:
        raise ValueError(f"{manifest_path}: unknown context {context!r}; allowed: {CONTEXTS}")
    partial = manifest.get("partial", False)
    if not isinstance(partial, bool):
        raise ValueError(f"{manifest_path}: partial must be true or false, got {partial!r}")
    bag = manifest.get("bag")
    declared = bag.get("duration_s") if hasattr(bag, "get") else None
    numeric = isinstance(declared, (int, float)) and not isinstance(declared, bool)
    manifest_duration_s = float(declared) if numeric else None
    window = manifest.get("capture_window_s")
    if window is not None and (
        isinstance(window, bool)
        or not isinstance(window, (int, float))
        or not math.isfinite(window)
        or window <= 0
    ):
        raise ValueError(f"{manifest_path}: capture_window_s must be positive seconds")
    raw_units = manifest.get("units", {})
    if not hasattr(raw_units, "items") or any(
        not hasattr(device_units, "items") for device_units in raw_units.values()
    ):
        raise ValueError(
            f"{manifest_path}: units must map each device (or 'environment') "
            "to a signal-to-unit map"
        )
    for device in raw_units:
        if str(device) != "environment" and str(device).strip().casefold() == "environment":
            raise KeyError(
                f"{manifest_path}: device {device!r} uses reserved unit scope 'environment'"
            )
    units = {
        str(device): {str(signal): str(unit) for signal, unit in device_units.items()}
        for device, device_units in raw_units.items()
    }

    raw_deadlines = manifest.get("dds_deadlines", {})
    if not hasattr(raw_deadlines, "items"):
        raise ValueError(f"{manifest_path}: dds_deadlines must map device IDs to positive seconds")
    dds_deadlines: dict[str, float] = {}
    for device_id, bound in raw_deadlines.items():
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            raise ValueError(f"{manifest_path}: dds_deadlines/{device_id} must be positive seconds")
        value = float(bound)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{manifest_path}: dds_deadlines/{device_id} must be positive seconds")
        dds_deadlines[str(device_id)] = value

    raw_procedures = manifest.get("procedures", {})
    if not hasattr(raw_procedures, "items"):
        raise ValueError(
            f"{manifest_path}: procedures must map device IDs to procedure declarations"
        )
    procedures: dict[str, dict[str, tuple[str, str, int]]] = {}
    for raw_device_id, declared in raw_procedures.items():
        device_id = str(raw_device_id)
        if not isinstance(declared, list):
            raise ValueError(f"{manifest_path}: procedures/{device_id} must be a list")
        for procedure in declared:
            if (
                not hasattr(procedure, "keys")
                or set(procedure) != {"id", "kind", "signal", "code"}
                or not isinstance(procedure["id"], str)
                or not procedure["id"].strip()
                or procedure["kind"] not in {"reset", "watchdog", "update"}
                or not isinstance(procedure["signal"], str)
                or not procedure["signal"].strip()
                or procedure["signal"] not in units.get(device_id, {})
                or isinstance(procedure["code"], bool)
                or not isinstance(procedure["code"], int)
                or procedure["code"] < 0
            ):
                raise ValueError(
                    f"{manifest_path}: procedures/{device_id} declarations need id, kind, declared signal, and nonnegative integer code"
                )
            if procedure["id"] in procedures.setdefault(device_id, {}):
                raise ValueError(f"{manifest_path}: duplicate procedure id {procedure['id']!r}")
            procedures[device_id][procedure["id"]] = (
                procedure["kind"],
                procedure["signal"],
                procedure["code"],
            )

    raw = samples_path.read_bytes()
    sha = _recording_sha256(manifest_raw, raw)

    devices: dict[str, DeviceStream] = {}
    env: dict[str, list[tuple[float, float]]] = {}
    dds_deadline_misses: dict[str, list[tuple[float, int]]] = {}
    procedure_events: dict[str, dict[str, list[tuple[float, str, str, int]]]] = {}
    row_count = 0

    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line, parse_constant=_refuse_nonfinite)
        t = float(row["t"])
        if "dev" in row and "dds_deadline_missed" in row:
            dev = str(row["dev"])
            if set(row) != {"t", "dev", "dds_deadline_missed"} or dev not in dds_deadlines:
                raise ValueError(
                    f"{samples_path}:{lineno}: undeclared or malformed DDS deadline event"
                )
            count = row["dds_deadline_missed"]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError(
                    f"{samples_path}:{lineno}: DDS deadline miss count must be positive"
                )
            events = dds_deadline_misses.setdefault(dev, [])
            if events and t < events[-1][0]:
                raise ValueError(f"{samples_path}:{lineno}: DDS deadline events out of time order")
            events.append((t, count))
        elif "dev" in row and "procedure_event" in row:
            dev = str(row["dev"])
            procedure = row["procedure_event"]
            if (
                set(row) != {"t", "dev", "procedure_event"}
                or not hasattr(procedure, "keys")
                or set(procedure) != {"id", "kind", "signal", "code"}
                or not isinstance(procedure["id"], str)
                or not procedure["id"].strip()
                or procedure["kind"] not in {"reset", "watchdog", "update"}
                or not isinstance(procedure["signal"], str)
                or not procedure["signal"].strip()
                or isinstance(procedure["code"], bool)
                or not isinstance(procedure["code"], int)
                or procedure["code"] < 0
            ):
                raise ValueError(
                    f"{samples_path}:{lineno}: undeclared or malformed procedure event"
                )
            procedure_id = procedure["id"]
            evidence = (procedure["kind"], procedure["signal"], procedure["code"])
            if evidence != procedures.get(dev, {}).get(procedure_id):
                raise ValueError(
                    f"{samples_path}:{lineno}: procedure event does not match its declaration"
                )
            events = procedure_events.setdefault(dev, {}).setdefault(procedure_id, [])
            if events and t < events[-1][0]:
                raise ValueError(f"{samples_path}:{lineno}: procedure events out of time order")
            events.append((t, *evidence))
        elif "dev" in row:
            row_count += 1
            dev = row["dev"]
            # Reserve the human-entered scope case-insensitively and ignore only
            # surrounding whitespace; device IDs themselves are not normalized.
            if str(dev).strip().casefold() == "environment":
                raise KeyError(
                    f"{samples_path}:{lineno}: device {dev!r} uses reserved unit scope "
                    "'environment'"
                )
            stream = devices.get(dev)
            if stream is None:
                stream = devices[dev] = DeviceStream(device=dev)
            if stream.recv_times and t < stream.recv_times[-1]:
                raise ValueError(
                    f"{samples_path}:{lineno}: receive time {t} for {dev!r} "
                    f"precedes previous time {stream.recv_times[-1]}"
                )
            signals = row.get("sig", {})
            for name in signals:
                if name not in units.get(dev, {}):
                    raise ValueError(
                        f"{samples_path}:{lineno}: no unit declared for signal {name!r} "
                        f"on device {dev!r}"
                    )
            if stream.recv_times and signals.keys() != stream.signals.keys():
                missing = sorted(stream.signals.keys() - signals.keys())
                extra = sorted(signals.keys() - stream.signals.keys())
                raise ValueError(
                    f"{samples_path}:{lineno}: ragged signals for {dev!r}; "
                    f"missing={missing}, extra={extra}"
                )
            # Device rows promise complete messages, so reject changing field sets
            # instead of inventing gaps or a second timestamp model. This keeps
            # recv_times[i] paired exactly with every signal[i].
            stream.recv_times.append(t)
            for name, value in signals.items():
                stream.signals.setdefault(name, []).append(float(value))
        elif "env" in row:
            row_count += 1
            for name, value in row["env"].items():
                if name not in units.get("environment", {}):
                    raise ValueError(
                        f"{samples_path}:{lineno}: no unit declared for environment "
                        f"channel {name!r}"
                    )
                samples = env.setdefault(name, [])
                if samples and t < samples[-1][0]:
                    raise ValueError(
                        f"{samples_path}:{lineno}: receive time {t} for environment "
                        f"channel {name!r} precedes previous time {samples[-1][0]}"
                    )
                samples.append((t, float(value)))
        else:
            raise ValueError(f"{samples_path}:{lineno}: row has neither 'dev' nor 'env': {row!r}")

    return Recording(
        context=context,
        units=units,
        devices=devices,
        env=env,
        sha256=sha,
        path=path,
        partial=partial,
        manifest_duration_s=manifest_duration_s,
        capture_window_s=None if window is None else float(window),
        row_count=row_count,
        dds_deadlines=dds_deadlines,
        dds_deadline_misses=dds_deadline_misses,
        procedures=procedures,
        procedure_events=procedure_events,
    )


_SAVED_HASHES = (
    "recording_sha256",
    "contract_sha256",
    "firmware_sha256",
    "model_sha256",
    "input_sha256",
    "output_sha256",
)
_TARGET_IDENTITIES = ("build", "pin", "address", "memory", "partition", "signed_slot")
_SELECTED_TARGET_IDENTITY_COMMAND = ("cat", "/var/lockwire/selected-target.json")


def _selected_target_identity(raw: str | bytes, *, source: str) -> dict[str, str]:
    """Read one complete selected-target identity from its named source."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{source} has duplicate key {key!r}")
            result[key] = value
        return result

    try:
        target = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{source} must be UTF-8 JSON") from error
    if (
        not isinstance(target, dict)
        or set(target) != set(_TARGET_IDENTITIES)
        or any(
            not isinstance(target[key], str) or not target[key].strip()
            for key in _TARGET_IDENTITIES
        )
    ):
        raise ValueError(f"{source} must declare every selected-target identity")
    return target


def read_selected_target(runner: CommandRunner) -> dict[str, str]:
    """Read identity from the selected target through its own runner."""
    result = runner(_SELECTED_TARGET_IDENTITY_COMMAND)
    if result.returncode != 0:
        command = " ".join(_SELECTED_TARGET_IDENTITY_COMMAND)
        raise ValueError(
            f"selected target identity could not be read ({command}, exit {result.returncode})"
        )
    return _selected_target_identity(result.stdout, source="selected target identity")


def load_saved_recording(
    path: Path,
    *,
    manifest_ref: ArtifactRef,
    evidence_root: Path,
    contract_sha256: str,
    selected_target_runner: CommandRunner,
    policy_out: dict | None = None,
) -> tuple[Recording, str]:
    """Open native saved Test evidence against a caller-held manifest identity.

    The manifest's six selected-target fields are immutable declarations bound
    to an externally expected hash. The same values must also be read from the
    selected target through its own runner before this replay is accepted.
    """
    raw = read_artifact(manifest_ref, Path(evidence_root))

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"saved Test manifest has duplicate key {key!r}")
            result[key] = value
        return result

    try:
        manifest = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("saved Test manifest must be UTF-8 JSON") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) - {"profile", "required_evidence"}
        != {"format_version", "sample_row_count", "selected_target", *_SAVED_HASHES}
        or type(manifest["format_version"]) is not int
        or manifest["format_version"] != 1
    ):
        raise ValueError("saved Test manifest has missing, extra, or unsupported fields")
    policy_keys = {"profile", "required_evidence"} & set(manifest)
    if policy_keys and policy_keys != {"profile", "required_evidence"}:
        raise ValueError("saved Test manifest needs both profile and required_evidence")
    # Import after the recording module has loaded: report's baseline types
    # depend on Recording and must not create a module-import cycle.
    from lockwire_pro.preflight.report import preflight_policy

    if policy_keys and not isinstance(manifest["required_evidence"], list):
        raise ValueError("saved Test required_evidence must be a list")
    profile, requirements = preflight_policy(
        manifest.get("profile", "analysis"),
        manifest.get("required_evidence", []),
    )
    if policy_out is not None:
        policy_out.update(profile=profile, required_evidence=requirements, legacy=not policy_keys)
    if any(
        not isinstance(manifest[key], str)
        or len(manifest[key]) != 64
        or any(char not in "0123456789abcdef" for char in manifest[key])
        for key in _SAVED_HASHES
    ):
        raise ValueError("saved Test manifest identities must be lowercase SHA-256")
    target = _selected_target_identity(
        json.dumps(manifest["selected_target"]), source="saved Test selected-target identities"
    )
    if target != read_selected_target(selected_target_runner):
        raise ValueError("saved Test selected target differs from the selected target identity")
    count = manifest["sample_row_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("saved Test sample_row_count must be a nonnegative integer")
    if manifest["contract_sha256"] != contract_sha256:
        raise ValueError("saved Test contract identity differs from the selected contract")

    recording = load_recording(path)
    if recording.sha256 != manifest["recording_sha256"]:
        raise ValueError("saved Test recording bytes or order differ from the manifest")
    if recording.row_count != count:
        raise ValueError("saved Test sample row count differs from the manifest")
    return recording, manifest["output_sha256"]
