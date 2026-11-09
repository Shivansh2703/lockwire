"""Read physical PX4 ULog sensor topics into the recording contract.

ULog is self-describing, but most uORB topics are software state rather than
physical measurements and many fields have no unit in the binary format.  This
reader deliberately maps only topics whose PX4 units are stable and useful to
the existing baseline machinery.  Each ULog subscription instance remains a
separate ``DeviceStream`` so multiple sensors are never merged silently.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lockwire_pro.recording import DeviceStream, Recording
from lockwire.schema import CONTEXTS

_MAGIC = b"ULog\x01\x12\x35"
_HEADER_SIZE = 16
_FIELD = re.compile(r"^(?P<type>[A-Za-z_]\w*)(?:\[(?P<count>\d+)\])? (?P<name>\w+)$")
_PRIMITIVES: dict[str, tuple[str, int]] = {
    "bool": ("?", 1),
    "char": ("c", 1),
    "int8_t": ("b", 1),
    "uint8_t": ("B", 1),
    "int16_t": ("h", 2),
    "uint16_t": ("H", 2),
    "int32_t": ("i", 4),
    "uint32_t": ("I", 4),
    "int64_t": ("q", 8),
    "uint64_t": ("Q", 8),
    "float": ("f", 4),
    "double": ("d", 8),
}

# output signal -> (ULog field, unit)
_TOPICS: dict[str, dict[str, tuple[str, str]]] = {
    "sensor_accel": {
        "accel_x": ("x", "m/s^2"),
        "accel_y": ("y", "m/s^2"),
        "accel_z": ("z", "m/s^2"),
        "temperature": ("temperature", "C"),
    },
    "sensor_gyro": {
        "gyro_x": ("x", "rad/s"),
        "gyro_y": ("y", "rad/s"),
        "gyro_z": ("z", "rad/s"),
        "temperature": ("temperature", "C"),
    },
    "sensor_baro": {
        "pressure_pa": ("pressure", "Pa"),
        "temperature": ("temperature", "C"),
    },
    "battery_status": {
        "voltage": ("voltage_v", "V"),
        "current": ("current_a", "A"),
        "remaining": ("remaining", "1"),
        "temperature": ("temperature", "C"),
    },
    # Older PX4 logs publish calibrated IMU data in one combined topic.
    "sensor_combined": {
        "gyro_x": ("gyro_rad[0]", "rad/s"),
        "gyro_y": ("gyro_rad[1]", "rad/s"),
        "gyro_z": ("gyro_rad[2]", "rad/s"),
        "accel_x": ("accelerometer_m_s2[0]", "m/s^2"),
        "accel_y": ("accelerometer_m_s2[1]", "m/s^2"),
        "accel_z": ("accelerometer_m_s2[2]", "m/s^2"),
    },
}


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    code: str
    count: int
    size: int


def _records(raw: bytes):
    position = _HEADER_SIZE
    while position < len(raw):
        if position + 3 > len(raw):
            raise ValueError("ULog ends inside a message header")
        size, message_type = struct.unpack_from("<HB", raw, position)
        position += 3
        end = position + size
        if end > len(raw):
            raise ValueError("ULog ends inside a message payload")
        yield chr(message_type), raw[position:end]
        position = end


def _format_fields(description: str) -> tuple[str, list[_Field]]:
    try:
        name, raw_fields = description.rstrip("\0").split(":", 1)
    except ValueError as error:
        raise ValueError(f"invalid ULog format description {description!r}") from error
    fields: list[_Field] = []
    for description_field in filter(None, raw_fields.split(";")):
        match = _FIELD.fullmatch(description_field)
        if match is None:
            raise ValueError(f"invalid ULog field description {description_field!r}")
        primitive = _PRIMITIVES.get(match["type"])
        if primitive is None:
            # Nested uORB structs are not used by the physical topics above.
            return name, []
        code, size = primitive
        count = int(match["count"] or 1)
        fields.append(_Field(match["name"], code, count, size))
    return name, fields


def _decode(fields: list[_Field], payload: bytes) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    offset = 0
    for field in fields:
        size = field.size * field.count
        if offset + size > len(payload):
            # PX4 may omit trailing explicit alignment padding from a logged
            # message. It never carries a physical value. If a future schema
            # places a real field after _padding, this break would skip it.
            if field.name.startswith("_padding"):
                break
            raise ValueError("ULog data payload is shorter than its declared format")
        values = struct.unpack_from(f"<{field.count}{field.code}", payload, offset)
        offset += size
        if not field.name.startswith("_padding"):
            decoded[field.name] = values[0] if field.count == 1 else values
    return decoded


def _value(row: dict[str, Any], source: str) -> float:
    """Decode one field. Non-finite values pass through — see ``_drop_dead_channels``.

    This used to raise, and refusing the file was the wrong call: PX4
    reports NaN for a battery temperature when the pack has no thermistor, which
    is most packs, so a whole class of real flights was unreadable and a missing
    sensor was reported as a broken tool (exit 2) instead of as not scored.
    """
    if source.endswith("]"):
        name, index = source[:-1].split("[", 1)
        value = row[name][int(index)]
    else:
        value = row[source]
    return float(value)


def _drop_dead_channels(devices: dict[str, DeviceStream], units: dict[str, dict[str, str]]) -> None:
    """Remove channels that were never measured; keep ones that merely glitched.

    The rule: "Drop dead channels, keep partial NaN". The two cases
    are physically different and must not collapse into each other:

    - non-finite in EVERY sample -> no sensor is reporting this. The channel is
      absent, and absence surfaces as ``not_scored``.
    - non-finite in SOME samples -> a sensor is doing something wrong. Those
      samples stay, so ``baseline.check_recording`` reports them as a ``non-finite-sample`` finding
      and scores only the finite samples.

    Dropping every NaN instead would delete the evidence of an intermittently
    faulting sensor — exactly what the gate exists to catch.
    """
    for device_id, stream in devices.items():
        dead = [
            channel
            for channel, values in stream.signals.items()
            if values and not any(math.isfinite(value) for value in values)
        ]
        for channel in dead:
            del stream.signals[channel]
            units.get(device_id, {}).pop(channel, None)


def _utf8(payload: bytes, *, kind: str) -> str:
    """Decode a ULog text field, or raise a named error naming the undecodable bytes."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        bad = payload[error.start : error.end]
        raise ValueError(
            f"invalid UTF-8 in ULog {kind} at offset {error.start}: {bad!r}"
        ) from error


def load_ulog(path: Path, *, context: str = "nominal-op") -> Recording:
    """Load supported physical topics from a PX4 ``.ulg`` file."""
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < _HEADER_SIZE or raw[:7] != _MAGIC:
        raise ValueError(f"{path}: not a ULog file")
    if context not in CONTEXTS:
        raise ValueError(f"unknown context {context!r}; allowed: {CONTEXTS}")

    formats: dict[str, list[_Field]] = {}
    subscriptions: dict[int, tuple[str, int]] = {}
    devices: dict[str, DeviceStream] = {}
    units: dict[str, dict[str, str]] = {}

    for message_type, payload in _records(raw):
        if message_type == "F":
            topic, fields = _format_fields(_utf8(payload, kind="format description"))
            formats[topic] = fields
        elif message_type == "A":
            if len(payload) < 3:
                raise ValueError("invalid ULog subscription message")
            multi_id, message_id = struct.unpack_from("<BH", payload)
            subscriptions[message_id] = (
                _utf8(payload[3:], kind="subscription name").rstrip("\0"),
                multi_id,
            )
        elif message_type == "R":
            if len(payload) >= 2:
                subscriptions.pop(struct.unpack_from("<H", payload)[0], None)
        elif message_type == "D":
            if len(payload) < 2:
                raise ValueError("invalid ULog data message")
            message_id = struct.unpack_from("<H", payload)[0]
            subscription = subscriptions.get(message_id)
            if subscription is None:
                continue
            topic, multi_id = subscription
            mapping = _TOPICS.get(topic)
            fields = formats.get(topic)
            if mapping is None or not fields:
                continue
            row = _decode(fields, payload[2:])
            if "timestamp" not in row:
                raise ValueError(f"ULog physical topic {topic!r} has no timestamp")
            timestamp = float(row["timestamp"]) / 1_000_000.0
            device_id = f"px4.{topic}.{multi_id}"
            stream = devices.setdefault(device_id, DeviceStream(device=device_id))
            if stream.recv_times and timestamp < stream.recv_times[-1]:
                raise ValueError(f"ULog topic {device_id!r} has decreasing timestamps")
            # Older firmware omits some fields (battery temperature): that channel is
            # absent, so it surfaces as not scored instead of refusing the log.
            mapping = {o: m for o, m in mapping.items() if m[0].split("[")[0] in row}
            signals = {output: _value(row, source) for output, (source, _unit) in mapping.items()}
            stream.recv_times.append(timestamp)
            for name, value in signals.items():
                stream.signals.setdefault(name, []).append(value)
            units.setdefault(
                device_id, {output: unit for output, (_source, unit) in mapping.items()}
            )

    if not devices:
        supported = ", ".join(sorted(_TOPICS))
        raise ValueError(f"{path}: no supported physical topics; expected one of: {supported}")

    _drop_dead_channels(devices, units)
    return Recording(
        context=context,
        units=units,
        devices=devices,
        env={},
        sha256=hashlib.sha256(raw).hexdigest(),
        path=path,
        source="ulog",
    )


def read_ulog_rows(raw: bytes, topics: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """Return every decoded row for the requested uORB ``topics``, in log order.

    Unlike :func:`load_ulog` (which maps a fixed physical-sensor topic set into
    ``Recording``/``DeviceStream``), this hands back whichever fields each
    topic's own ULog format declares, unfiltered -- the E5.10 X500 judge
    (``jobs._x500_evidence_reason``) reads position/land-detected/actuator/
    status fields directly. Reuses the same record/format/decode loop as
    :func:`load_ulog`. Raises if any requested topic never appears.
    """
    if len(raw) < _HEADER_SIZE or raw[:7] != _MAGIC:
        raise ValueError("not a ULog file")
    wanted = set(topics)
    formats: dict[str, list[_Field]] = {}
    subscriptions: dict[int, str] = {}
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in wanted}
    for message_type, payload in _records(raw):
        if message_type == "F":
            topic, fields = _format_fields(_utf8(payload, kind="format description"))
            formats[topic] = fields
        elif message_type == "A":
            if len(payload) < 3:
                raise ValueError("invalid ULog subscription message")
            _multi_id, message_id = struct.unpack_from("<BH", payload)
            name = _utf8(payload[3:], kind="subscription name").rstrip("\0")
            if name in wanted:
                subscriptions[message_id] = name
        elif message_type == "R":
            if len(payload) >= 2:
                subscriptions.pop(struct.unpack_from("<H", payload)[0], None)
        elif message_type == "D":
            if len(payload) < 2:
                raise ValueError("invalid ULog data message")
            message_id = struct.unpack_from("<H", payload)[0]
            topic = subscriptions.get(message_id)
            fields = formats.get(topic) if topic is not None else None
            if topic is None or not fields:
                continue
            row = _decode(fields, payload[2:])
            if "timestamp" not in row:
                raise ValueError(f"ULog topic {topic!r} has no timestamp")
            rows[topic].append(row)
    missing = sorted(name for name in wanted if not rows[name])
    if missing:
        raise ValueError(f"ULog is missing required topic(s): {', '.join(missing)}")
    return rows
