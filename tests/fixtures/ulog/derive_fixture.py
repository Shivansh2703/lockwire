#!/usr/bin/env python3
"""Derive the small committed ULog fixture from its full source log."""

from __future__ import annotations

import argparse
import struct
from decimal import Decimal
from pathlib import Path

TOPICS = {"sensor_accel", "sensor_baro", "sensor_combined", "sensor_gyro"}
# Three seconds keeps the fixture modest while preserving the rate-dependent raw
# counts: a 200 Hz topic contributes about 600 samples, while a 1 Hz topic has 3-4.
WINDOW_US = 3_000_000


def derive(
    source: Path,
    destination: Path,
    *,
    offset_us: int = 0,
    window_us: int = WINDOW_US,
) -> None:
    if offset_us < 0:
        raise ValueError("window offset must be non-negative")
    if window_us <= 0:
        raise ValueError("window duration must be positive")
    raw = source.read_bytes()
    if raw[:7] != b"ULog\x01\x12\x35":
        raise ValueError(f"{source}: not a ULog file")
    output = bytearray(raw[:16])
    subscriptions: dict[int, str] = {}
    first_timestamps: dict[int, int] = {}
    position = 16
    while position < len(raw):
        start = position
        size, raw_type = struct.unpack_from("<HB", raw, position)
        position += 3
        end = position + size
        payload = raw[position:end]
        position = end
        message_type = chr(raw_type)
        keep = message_type in {"B", "F", "I", "M", "P", "Q"}
        if message_type == "A":
            message_id = struct.unpack_from("<H", payload, 1)[0]
            topic = payload[3:].decode("utf-8").rstrip("\0")
            if topic in TOPICS:
                subscriptions[message_id] = topic
                keep = True
        elif message_type == "D":
            message_id = struct.unpack_from("<H", payload)[0]
            if message_id in subscriptions:
                # PX4 physical uORB topics declare uint64 timestamp as their
                # first field, immediately after the data message id.
                timestamp = struct.unpack_from("<Q", payload, 2)[0]
                # Window offsets are per-subscription first-sample time, not
                # ULog header (logging-start) or vehicle boot time.
                first_timestamp = first_timestamps.setdefault(message_id, timestamp)
                elapsed = timestamp - first_timestamp
                keep = offset_us <= elapsed <= offset_us + window_us
        if keep:
            output.extend(raw[start:end])
    destination.write_bytes(output)


def _seconds_to_us(value: str) -> int:
    seconds = Decimal(value)
    microseconds = seconds * 1_000_000
    if microseconds != microseconds.to_integral_value():
        raise argparse.ArgumentTypeError("seconds must resolve to a whole number of microseconds")
    return int(microseconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--offset-seconds",
        type=_seconds_to_us,
        default=0,
        metavar="SECONDS",
        help="start each selected subscription's window this far after its first sample",
    )
    parser.add_argument(
        "--window-seconds",
        type=_seconds_to_us,
        default=WINDOW_US,
        metavar="SECONDS",
        help="duration retained from each selected subscription (default: 3)",
    )
    args = parser.parse_args()
    derive(
        args.source,
        args.destination,
        offset_us=args.offset_seconds,
        window_us=args.window_seconds,
    )


if __name__ == "__main__":
    main()
