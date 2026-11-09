"""Read a real rosbag2 recording into the recording contract.

`recording.py` has said since M0 that its format is "a dependency-free stand-in
for a rosbag/mcap", deliberately not the real thing "until a design partner's
stack demands them". This is the real thing, and it costs less than that note
feared: **no ROS installation is required.** `mcap-ros2-support` depends only on
`mcap`, which depends only on lz4 and zstandard, so `preflight --from run.mcap`
works in a bare venv on a laptop with no ROS anywhere. That is the whole point —
a team with a bag can try lockwire before installing anything.

MCAP has been rosbag2's default storage since Iron (2023), so this covers Iron,
Jazzy, Kilted and Lyrical out of the box. SQLite3 `.db3` bags are refused by
name rather than half-supported (see ``_reject_db3``).

**`log_time` is lockwire's receive time, exactly.** MCAP records `log_time` (when
the recorder wrote the message) and `publish_time` (when the publisher sent it).
`recording.py` is explicit that rate and monotonicity are computed on *receive*
time and must tolerate driver batching — it calls sensor-time "the clock trap" —
so this reads `log_time` and never `publish_time`, and never a stamp inside the
message payload.

**Which topic is which device comes from the contract, not from a guess.** ULog
could hardcode its map because PX4's vocabulary is fixed; ROS 2 topics are named
per robot. `Driver.topic`, `Driver.msg_type` and `Driver.signals` say what
to read and in what unit.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Iterator

from lockwire_pro.recording import DeviceStream, Recording
from lockwire import fieldpath
from lockwire.schema import CONTEXTS, Contract

#: Extra that supplies the decoder, named in the error a user without it will see.
_EXTRA = "lockwire[ros2bag]"

_NS_PER_S = 1_000_000_000
PARTIAL_BAG_MARKER = b'{"partial":true,"reason":"cancelled"}\n'


class Ros2BagError(ValueError):
    """A bag lockwire cannot turn into a recording, with the reason stated."""


def _decoder():
    try:
        from mcap.reader import make_reader
        from mcap_ros2.decoder import DecoderFactory
    except ImportError as error:  # pragma: no cover - exercised by the extra being absent
        raise Ros2BagError(
            f"reading rosbag2/MCAP needs the optional decoder: pip install '{_EXTRA}' ({error})"
        ) from error
    return make_reader, DecoderFactory


def _reject_db3(path: Path) -> None:
    """Refuse SQLite3 bags loudly instead of appearing to half-support them.

    rosbag2 still writes `.db3` on request. Reading it would mean a second
    decoder and a second set of edge cases, and a partial implementation that
    silently skipped topics it could not decode would be worse than a clear
    refusal — the recording it produced would look complete.
    """
    if (path.is_file() and path.suffix == ".db3") or (
        path.is_dir() and not _mcap_files(path) and list(path.glob("*.db3"))
    ):
        folder = path.parent if path.is_file() else path
        raise Ros2BagError(
            f"{path}: SQLite3 (.db3) bags are not supported; lockwire reads MCAP (the "
            "rosbag2 default since Iron). Convert it:\n"
            "  printf 'output_bags:\\n- uri: run_mcap\\n  storage_id: mcap\\n' > to_mcap.yaml\n"
            f"  ros2 bag convert -i {folder} -o to_mcap.yaml\n"
            "then: lockwire bag inspect run_mcap"
        )


def _mcap_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.mcap"))


def bag_files(path: Path) -> list[Path]:
    """The `.mcap` files this path refers to: one file, or a bag directory's split parts."""
    path = Path(path)
    _reject_db3(path)
    if path.is_file():
        if path.suffix != ".mcap":
            raise Ros2BagError(f"{path}: not an .mcap file")
        return [path]
    if path.is_dir():
        files = _mcap_files(path)
        if not files:
            raise Ros2BagError(f"{path}: no .mcap files in this bag directory")
        return files
    raise Ros2BagError(f"{path}: no such bag")


def _plan(contract: Contract) -> dict[str, tuple[str, dict[str, tuple[str, str]]]]:
    """topic -> (device id, {signal: (dotted field path, unit)}), from the contract."""
    plan: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}
    for device in contract.devices:
        driver = device.driver
        if driver is None or not driver.topic or not driver.signals:
            continue
        mapping = {name: (source.field, source.unit) for name, source in driver.signals.items()}
        if driver.topic in plan:
            raise Ros2BagError(
                f"topic {driver.topic!r} is declared by two devices "
                f"({plan[driver.topic][0]!r} and {device.id!r}); a bag message cannot "
                "belong to both"
            )
        plan[driver.topic] = (device.id, mapping)
    return plan


def _walk(payload: Any, path: str, *, topic: str) -> float:
    """Resolve a field path (``fieldpath`` syntax) on a decoded message; never substitute a default."""
    try:
        node = fieldpath.resolve(payload, path)
    except fieldpath.FieldPathError as error:
        raise Ros2BagError(f"topic {topic}: {error}") from error
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise Ros2BagError(
            f"topic {topic}: field path {path!r} resolved to {node!r}, which is not a number"
        )
    return float(node)


def _example_field(node: Any, prefix: str = "") -> str | None:
    """The first numeric field of a decoded message, spelled as a contract field path.

    Arrays parallel to a ``name`` array (JointState) are spelled by name, which
    survives a driver that reorders its joints. Only a hint: the unit is never
    guessed.
    """
    names = getattr(node, "name", None)
    for slot in getattr(node, "__slots__", None) or ():
        value = getattr(node, slot)
        path = prefix + slot
        if slot == "header" or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return path
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            if isinstance(names, list) and names and len(names) == len(value):
                return f"{path}[name={names[0]}]"
            return f"{path}[0]"
        found = _example_field(value, path + ".")
        if found:
            return found
    return None


def inspect_bag(path: Path) -> dict[str, Any]:
    """What a bag holds, no contract needed: per topic its type, count and mean rate."""
    files = bag_files(Path(path))
    make_reader, DecoderFactory = _decoder()
    from mcap.exceptions import McapError

    factory = DecoderFactory()
    rows: dict[str, dict[str, Any]] = {}
    for part in files:
        try:
            with part.open("rb") as stream:
                for schema, channel, message in make_reader(stream).iter_messages(
                    log_time_order=False
                ):
                    row = rows.get(channel.topic)
                    if row is None:
                        decode = schema and factory.decoder_for(channel.message_encoding, schema)
                        row = rows[channel.topic] = {
                            "topic": channel.topic,
                            "msg_type": schema.name if schema else "",
                            "messages": 0,
                            "first": message.log_time,
                            "last": message.log_time,
                            "example_field": _example_field(decode(message.data))
                            if decode
                            else None,
                        }
                    row["messages"] += 1
                    row["first"] = min(row["first"], message.log_time)
                    row["last"] = max(row["last"], message.log_time)
        except (McapError, struct.error) as error:
            raise Ros2BagError(f"{part}: not a readable MCAP file ({error})") from error
    start = min((r["first"] for r in rows.values()), default=0)
    end = max((r["last"] for r in rows.values()), default=0)
    topics = []
    for row in sorted(rows.values(), key=lambda row: row["topic"]):
        span = (row.pop("last") - row.pop("first")) / _NS_PER_S
        rate = (row["messages"] - 1) / span if span > 0 else None
        topics.append({**row, "rate_hz": None if rate is None else round(rate, 2)})
    return {"bag": str(path), "duration_s": round((end - start) / _NS_PER_S, 3), "topics": topics}


def _messages(files: list[Path]) -> Iterator[tuple[str, int, Any]]:
    """(topic, log_time_ns, decoded message) across every part of the bag.

    ``log_time_order=False``: file/write order, not the mcap reader's own
    log-time sort. E3-FL needs the messages as the recorder actually appended
    them so a genuine out-of-order receipt (e.g. a clock step) is still visible
    as out-of-order here — ``log_time_order=True`` (the default) would silently
    hand back an already-sorted stream and hide it before `load_ros2_bag` ever
    sees it (verified: a bag written with reversed timestamps reads back sorted
    under the default).
    """
    make_reader, DecoderFactory = _decoder()
    from mcap.exceptions import McapError

    for part in files:
        try:
            with part.open("rb") as stream:
                reader = make_reader(stream, decoder_factories=[DecoderFactory()])
                for _schema, channel, message, decoded in reader.iter_decoded_messages(
                    log_time_order=False
                ):
                    yield channel.topic, message.log_time, decoded
        except (McapError, struct.error) as error:
            raise Ros2BagError(f"{part}: not a readable MCAP file ({error})") from error


def _origin_ns(collected: dict[str, list[tuple[int, dict[str, float]]]]) -> int:
    """Earliest ``log_time_ns`` across all devices.

    Sorted by time only — two messages on one topic can share a ``log_time``,
    and the values are plain dicts with no ordering, so a bare ``sorted(rows)``
    falls through to comparing dicts and raises ``TypeError`` on the tie.
    """
    return min(sorted(rows, key=lambda row: row[0])[0][0] for rows in collected.values())


def load_ros2_bag(
    path: Path,
    contract: Contract,
    *,
    context: str = "nominal-op",
    partial: bool = False,
) -> Recording:
    """Load the contract's declared topics out of a rosbag2 MCAP recording.

    An ordinary short bag is not evidence of cancellation. ``partial=True``
    comes from a caller that proved the recorder stopped, or from lockwire's
    exact marker saved after that proof with a retained live capture.
    """
    path = Path(path)
    if context not in CONTEXTS:
        raise Ros2BagError(f"unknown context {context!r}; allowed: {CONTEXTS}")

    marker = path / "lockwire-partial.json"
    marker_bytes = None
    if path.is_dir() and (marker.exists() or marker.is_symlink()):
        if marker.is_symlink() or not marker.is_file():
            raise Ros2BagError(f"{marker}: invalid partial-capture marker")
        marker_bytes = marker.read_bytes()
        if marker_bytes != PARTIAL_BAG_MARKER:
            raise Ros2BagError(f"{marker}: invalid partial-capture marker")
        partial = True

    files = bag_files(path)
    plan = _plan(contract)
    if not plan:
        raise Ros2BagError(
            "no device in this contract declares driver.topic + driver.signals, so there "
            "is nothing to read out of this bag (see Driver.signals in schema.py)"
        )

    # Collected then sorted rather than appended per device: a bag's chunks are
    # ordered per chunk, not globally, and load_recording rejects a device whose
    # receive times step backwards.
    collected: dict[str, list[tuple[int, dict[str, float]]]] = {}
    units: dict[str, dict[str, str]] = {}
    seen_topics: set[str] = set()

    for topic, log_time_ns, decoded in _messages(files):
        seen_topics.add(topic)
        target = plan.get(topic)
        if target is None:
            continue
        device_id, mapping = target
        values = {
            name: _walk(decoded, field, topic=topic) for name, (field, _unit) in mapping.items()
        }
        collected.setdefault(device_id, []).append((log_time_ns, values))
        units.setdefault(device_id, {name: unit for name, (_f, unit) in mapping.items()})

    if not collected:
        declared = ", ".join(sorted(plan))
        present = ", ".join(sorted(seen_topics)) or "none"
        raise Ros2BagError(
            f"{path}: none of the contract's declared topics appear in this bag. "
            f"declared: {declared}. present in bag: {present}"
        )

    # Rebase onto the first message. log_time is nanoseconds since the epoch
    # (~1.7e18), and float64 cannot hold that with sub-millisecond resolution —
    # keeping it absolute would quantise the very intervals the rate checks read.
    origin_ns = _origin_ns(collected)

    # E3-FL: the receipt-order fact, taken BEFORE the sort below reorders
    # anything. `_messages` reads file order (log_time_order=False), which for
    # a single recorder process appending as it receives is the true receipt
    # sequence — so an out-of-order device here is a genuine capture anomaly
    # (e.g. a clock step), not a bag-storage artifact of chunk layout.
    receipt_monotonic = all(
        rows[i][0] <= rows[i + 1][0] for rows in collected.values() for i in range(len(rows) - 1)
    )

    devices: dict[str, DeviceStream] = {}
    for device_id, rows in collected.items():
        rows.sort(key=lambda row: row[0])
        stream = DeviceStream(device=device_id)
        for log_time_ns, values in rows:
            stream.recv_times.append((log_time_ns - origin_ns) / _NS_PER_S)
            for name, value in values.items():
                stream.signals.setdefault(name, []).append(value)
        devices[device_id] = stream

    digest = hashlib.sha256()
    for part in files:
        digest.update(part.name.encode())
        digest.update(b"\0")
        digest.update(part.read_bytes())
    if marker_bytes is not None:
        digest.update(marker.name.encode())
        digest.update(b"\0")
        digest.update(marker_bytes)

    return Recording(
        context=context,
        units=units,
        devices=devices,
        env={},
        sha256=digest.hexdigest(),
        path=path,
        source="ros2bag",
        receipt_monotonic=receipt_monotonic,
        partial=partial,
    )
