"""Turn live ROS 2 topics into a lockwire recording.

The gate reads a recording. This is what produces one from a running robot,
so `lockwire preflight` can be pointed at what the machine is doing *now*
rather than at something captured by hand earlier.

**It writes lockwire's native recording format, not a bag.** The format is
plain YAML plus JSONL (`lockwire/recording.py`), writable from any Python with
no dependencies, and `RecordedSource` already consumes it and is the proven
path. Recording a bag instead would need `rosbag2`, which is in `ros_base` and
not in `ros_core`, and would then need decoding back out again.

**Receive time, not sensor time.** `recording.py` is explicit that rate and
monotonicity are computed on receive time and must tolerate driver batching —
it calls sensor-time "the clock trap". So `t` here is when *this process* got
the message, taken from one monotonic clock, and never a stamp inside the
message payload.

**Field extraction is message-type agnostic.** `get_message` builds the
subscription from the contract's `msg_type` string and `message_to_ordereddict`
flattens any message, so a dotted path like `linear_acceleration.z` is a plain
dict walk. No per-message-type code exists here and none should.

Python 3.10 compatible.
"""

from __future__ import annotations

import json
import math
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from lockwire_ros import fieldpath

# Contexts the lockfile allows (lockwire.schema.CONTEXTS), restated because this
# package must not import lockwire. A mismatch fails at load with a clear error
# rather than silently writing a recording the gate will reject.
CONTEXTS = ("bench-idle", "armed-idle", "nominal-op")

MANIFEST = "manifest.yaml"
SAMPLES = "samples.jsonl"


class CaptureError(RuntimeError):
    """Capture could not produce a recording the gate would accept."""


def deadline_nanoseconds(value: object) -> int:
    """Return the exact DDS duration represented by a declared seconds value."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 1e-9):
        raise CaptureError("dds_deadline_s must be finite and at least 1 ns")
    nanoseconds = Decimal(str(value)) * Decimal("1e9")
    if nanoseconds != nanoseconds.to_integral_value():
        raise CaptureError("dds_deadline_s must be an integral number of nanoseconds")
    return int(nanoseconds)


def resolve_field(payload: Dict[str, Any], path: str) -> float:
    """Walk a field path (`fieldpath` syntax, arrays included) and return a float.

    Raises rather than substituting a default. A capture that quietly wrote 0.0
    for a field it could not find would hand the gate a healthy-looking number
    with no evidence behind it, which is the exact shape this product exists to
    refuse.
    """
    try:
        node = fieldpath.resolve(payload, path)
    except fieldpath.FieldPathError as error:
        raise CaptureError(str(error)) from error
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise CaptureError(
            "field path {0!r} resolved to {1!r}, which is not a number".format(path, node)
        )
    return float(node)


class DeviceCapture:
    """One contract device's topic, message type and signal map."""

    def __init__(
        self,
        device_id: str,
        topic: str,
        msg_type: str,
        signals: Dict[str, Tuple[str, str]],
        dds_deadline_s: Optional[float] = None,
        procedures: Optional[Dict[str, Tuple[str, int, str]]] = None,
    ) -> None:
        #: signals maps lockwire signal name -> (dotted field path, unit)
        self.device_id = device_id
        self.topic = topic
        self.msg_type = msg_type
        self.signals = signals
        self.dds_deadline_s = dds_deadline_s
        # id -> (declared signal, discrete code, reset/watchdog/update kind).
        # These are observations only; capture never emits a command.
        self.procedures = procedures or {}
        self.deadline_misses: List[Tuple[float, int]] = []
        self.procedure_events: List[Tuple[float, str]] = []
        self.rows: List[Tuple[float, Dict[str, float]]] = []

    def units(self) -> Dict[str, str]:
        return {name: unit for name, (_, unit) in self.signals.items()}

    def take(self, message: Any, recv_time_s: float) -> None:
        """Record one message. All of a device's fields share one timestamp.

        `recording.py` requires every message from a device to carry the same
        field set, so this writes all signals or raises — never a partial row.
        """
        from rosidl_runtime_py import message_to_ordereddict

        payload = message_to_ordereddict(message)
        values = {name: resolve_field(payload, path) for name, (path, _) in self.signals.items()}
        self.rows.append((recv_time_s, values))
        for procedure_id, (signal, code, _kind) in self.procedures.items():
            if values[signal] == float(code):
                self.procedure_events.append((recv_time_s, procedure_id))


def captures_from_contract(contract: Dict[str, Any]) -> List[DeviceCapture]:
    """Read the capture plan out of a loaded lockfile.

    Devices without `driver.msg_type` and `driver.signals` are skipped — they
    are the older contract shape and simply say nothing about live capture.
    """
    plan: List[DeviceCapture] = []
    for device in contract.get("devices") or []:
        driver = device.get("driver") or {}
        signals = driver.get("signals") or {}
        topic = driver.get("topic")
        msg_type = driver.get("msg_type")
        if not signals or not msg_type or not topic:
            continue
        resolved: Dict[str, Tuple[str, str]] = {}
        for name, source in signals.items():
            field = source.get("field")
            unit = source.get("unit")
            if not field or not unit:
                # Cannot happen through schema validation (the schema makes both
                # required); caught here so a hand-edited file fails loudly.
                raise CaptureError(
                    "device {0!r} signal {1!r} is missing field or unit".format(
                        device.get("id"), name
                    )
                )
            resolved[name] = (field, unit)
        deadline = driver.get("dds_deadline_s")
        if deadline is not None:
            try:
                deadline_nanoseconds(deadline)
            except CaptureError as error:
                raise CaptureError("device {0!r}: {1}".format(device["id"], error)) from error
        procedures: Dict[str, Tuple[str, int, str]] = {}
        for raw in driver.get("procedures") or []:
            if not isinstance(raw, dict):
                raise CaptureError("device {0!r}: procedure must be a mapping".format(device["id"]))
            procedure_id = raw.get("id")
            kind = raw.get("kind")
            signal = raw.get("signal")
            code = raw.get("code")
            if (
                not isinstance(procedure_id, str) or not procedure_id.strip()
                or kind not in ("reset", "watchdog", "update")
                or not isinstance(signal, str) or signal not in resolved
                or isinstance(code, bool) or not isinstance(code, int) or code < 0
            ):
                raise CaptureError(
                    "device {0!r}: procedure must name a declared signal with a "
                    "nonnegative integer code".format(device["id"])
                )
            if procedure_id in procedures:
                raise CaptureError("device {0!r}: duplicate procedure id {1!r}".format(
                    device["id"], procedure_id
                ))
            procedures[procedure_id] = (signal, code, kind)
        plan.append(DeviceCapture(device["id"], topic, msg_type, resolved, deadline, procedures))
    return plan


def write_recording(
    out_dir: str,
    captures: List[DeviceCapture],
    context: str,
    generator: str = "lockwire_ros.capture",
    env_units: Optional[Dict[str, str]] = None,
    env_rows: Optional[List[Tuple[float, Dict[str, float]]]] = None,
    capture_window_s: Optional[float] = None,
) -> str:
    """Write manifest.yaml + samples.jsonl, and refuse to write a useless one.

    Rows are emitted in nondecreasing receive-time order across the whole file,
    matching what `load_recording` enforces per device. Times are rebased so the
    first sample is t=0 — durations and gaps are what the checks read, and an
    absolute monotonic epoch is meaningless outside this process.
    """
    if context not in CONTEXTS:
        raise CaptureError("unknown context {0!r}; allowed: {1}".format(context, CONTEXTS))

    produced = [capture for capture in captures if capture.rows]
    if not produced:
        # An empty recording would sail through the gate as "nothing scored".
        # Refusing here means a capture that saw no traffic reports that fact
        # instead of manufacturing a vacuous pass.
        raise CaptureError(
            "no messages captured on any declared topic ({0}) — refusing to write an "
            "empty recording".format(", ".join(sorted(c.topic for c in captures)) or "none")
        )

    # Final item is an observed procedure descriptor.  Retain all four
    # declared fields in the evidence row: an ID alone could be hand-written
    # against the wrong source signal or numeric code.
    flat: List[
        Tuple[float, str, Dict[str, float], int, Optional[Tuple[str, str, int, str]]]
    ] = []
    for capture in produced:
        for recv_time, values in capture.rows:
            flat.append((recv_time, capture.device_id, values, 0, None))
        for recv_time, count in capture.deadline_misses:
            flat.append((recv_time, capture.device_id, {}, count, None))
        for recv_time, procedure_id in capture.procedure_events:
            try:
                signal, code, kind = capture.procedures[procedure_id]
            except KeyError as error:
                raise CaptureError(
                    "procedure event {0!r} has no declared capture binding".format(procedure_id)
                ) from error
            flat.append((recv_time, capture.device_id, {}, 0, (procedure_id, signal, code, kind)))
    for recv_time, values in env_rows or []:
        flat.append((recv_time, "", values, 0, None))
    flat.sort(key=lambda row: row[0])
    origin = flat[0][0]

    os.makedirs(out_dir, exist_ok=True)

    units: Dict[str, Dict[str, str]] = {c.device_id: c.units() for c in produced}
    if env_units:
        units["environment"] = dict(env_units)

    # Hand-rolled YAML rather than a dependency: the manifest is two levels of
    # plain string mapping, and this package takes nothing outside ros_core.
    lines = ["context: {0}".format(context), "generator: {0}".format(generator)]
    if capture_window_s is not None and capture_window_s > 0:
        lines.append("capture_window_s: {0}".format(round(capture_window_s, 6)))
    deadlines = {c.device_id: c.dds_deadline_s for c in produced if c.dds_deadline_s is not None}
    if deadlines:
        lines.append("dds_deadlines:")
        for device_id, bound in sorted(deadlines.items()):
            lines.append("  {0}: {1}".format(_yaml_key(device_id), bound))
    procedures = {capture.device_id: capture.procedures for capture in produced if capture.procedures}
    if procedures:
        lines.append("procedures:")
        for device_id, declared in sorted(procedures.items()):
            lines.append("  {0}:".format(_yaml_key(device_id)))
            for procedure_id, (signal, code, kind) in sorted(declared.items()):
                lines.append("    - id: {0}".format(_yaml_scalar(procedure_id)))
                lines.append("      kind: {0}".format(_yaml_scalar(kind)))
                lines.append("      signal: {0}".format(_yaml_scalar(signal)))
                lines.append("      code: {0}".format(code))
    lines.append("units:")
    for scope in sorted(units):
        lines.append("  {0}:".format(_yaml_key(scope)))
        for signal in sorted(units[scope]):
            lines.append(
                "    {0}: {1}".format(_yaml_key(signal), _yaml_scalar(units[scope][signal]))
            )
    manifest_path = os.path.join(out_dir, MANIFEST)
    with open(manifest_path, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")

    samples_path = os.path.join(out_dir, SAMPLES)
    with open(samples_path, "w", encoding="utf-8") as stream:
        for recv_time, device_id, values, missed_count, procedure_event in flat:
            stamp = round(recv_time - origin, 9)
            if missed_count:
                row = {"t": stamp, "dev": device_id, "dds_deadline_missed": missed_count}
            elif procedure_event is not None:
                procedure_id, signal, code, kind = procedure_event
                row = {
                    "t": stamp,
                    "dev": device_id,
                    "procedure_event": {
                        "id": procedure_id,
                        "kind": kind,
                        "signal": signal,
                        "code": code,
                    },
                }
            elif device_id:
                row = {"t": stamp, "dev": device_id, "sig": values}
            else:
                row = {"t": stamp, "env": values}
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    return out_dir


def _yaml_key(text: str) -> str:
    """Quote a mapping key unless it is unambiguously plain."""
    if text and all(ch.isalnum() or ch in "._-/" for ch in text):
        return text
    return json.dumps(text)


def _yaml_scalar(text: str) -> str:
    """Always quote unit strings: `m/s^2` and friends are not plain YAML scalars."""
    return json.dumps(text)
