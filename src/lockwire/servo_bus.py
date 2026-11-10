"""Read-only recorder for the Feetech STS / SCS half-duplex servo bus (SO-101, LeKiwi).

Sends only ping (0x01) and read (0x02); `_packet` refuses anything else.
Register map and byte order: Feetech's own SDK, ftservo/FTServo_Python scservo_sdk
(https://github.com/ftservo/FTServo_Python): sms_sts.py addresses 56 position, 58 speed,
60 load, 62 voltage, 63 temperature, 69 current; protocol_packet_handler.py "SCServo bit
end (STS/SMS=0, SCS=1)" with scs_makeword: 0 = little-endian, 1 = big-endian.
Units and the 0-17 settings block: STS memory table transcription
https://aifitlab-wiki.super.site/feetech-servo-motor-docs/feetech-sts-servo-memory-table-analysis
(position 360/4096 deg, voltage 0.1 V, current 6.5 mA, load 0.1 % duty, direction bits:
position/speed bit 15, load bit 10). SCS: same addresses in the SDK; the 13-17 settings
layout and the current scale are assumed equal to STS (not confirmed), so SCS records
current as a raw count and no degrees.
Goal and gains: sms_sts.py / scservo_sdk scscl.py both put Goal Position at 42 (2 bytes,
same byte order and direction bit 15 as position; STS 0.087 deg = 360/4096 per count). The
SDK defines no P/I/D constants; the same STS memory table gives Kp 21, Kd 22, Ki 23 (1 byte
each, raw register values, vendor scaling 1/4 and 1/8 not applied). SCS gains are not read
(layout unconfirmed); SCS records goal_position_raw.
"""

import json
import math
import time
from pathlib import Path

from lockwire.hw import plain_yaml

MANIFEST = "manifest.yaml"  # a recording folder: these two files (Pro recording.py reads them)
SAMPLES = "samples.jsonl"
PING, READ = 0x01, 0x02
_SETTINGS = (0, 24)  # firmware 0-1, END 2, servo ver 3-4, ID 5, ... 13-17 limits, 21-23 Kp Kd Ki
_LIVE = (
    42,
    29,
)  # 42..70: goal (2 B), ..., position 56, speed, load, voltage, temperature, ..., current
_POS = 14  # position offset inside the live block (56 - 42)
_LOAD_NOTE = "load_duty_permille is the drive duty (per-mille of max), not measured torque"
_UNITS = {
    "position_raw": "count",
    "speed_raw": "count",
    "load_duty_permille": "permille",  # drive duty, NOT measured torque
    "voltage_v": "V",
    "temperature_c": "degC",
}


def _packet(servo_id: int, instr: int, params: bytes = b"") -> bytes:
    if instr not in (PING, READ):
        raise ValueError(f"servo_bus is read-only; refusing instruction {instr:#04x}")
    body = bytes([servo_id, len(params) + 2, instr]) + params
    return b"\xff\xff" + body + bytes([~sum(body) & 0xFF])


def _port_open(port: str, baud: int):
    try:
        import serial
    except ImportError as error:
        raise RuntimeError("servo recording needs pyserial: pip install 'lockwire[bus]'") from error
    return serial.Serial(port, baud, timeout=0.05)


def _txrx(ser, servo_id: int, instr: int, params: bytes = b"") -> bytes | None:
    """Params of a valid status reply, or None (silence, wrong ID, bad checksum)."""
    ser.reset_input_buffer()
    ser.write(_packet(servo_id, instr, params))
    head = ser.read(4)
    if len(head) < 4 or head[:2] != b"\xff\xff" or head[2] != servo_id or head[3] < 2:
        return None
    rest = ser.read(head[3])  # error byte, params, checksum
    if len(rest) < head[3] or (~sum(head[2:] + rest[:-1]) & 0xFF) != rest[-1]:
        return None
    return rest[1:-1]


def _word(data: bytes, i: int, big: bool) -> int:
    return int.from_bytes(data[i : i + 2], "big" if big else "little")


def _signed(v: int, bit: int) -> int:
    return -(v & ~(1 << bit)) if v & (1 << bit) else v


def _sample(data: bytes, sts: bool) -> dict[str, float]:
    big = not sts
    goal = _signed(_word(data, 0, big), 15)
    data = data[_POS:]
    pos = _signed(_word(data, 0, big), 15)
    sig = {
        "position_raw": pos,
        "speed_raw": _signed(_word(data, 2, big), 15),
        "load_duty_permille": _signed(_word(data, 4, big), 10),
        "voltage_v": data[6] / 10,
        "temperature_c": data[7],
    }
    if sts:
        sig["position_deg"] = round(pos * 360 / 4096, 3)
        sig["goal_position_deg"] = round(goal * 360 / 4096, 3)
        sig["current_ma"] = _word(data, 13, big) * 6.5
    else:
        sig["current_raw"] = _word(data, 13, big)
        sig["goal_position_raw"] = goal
    return sig


def _fee_settings(ser, i: int, kind: str):
    sts, big = kind == "sts", kind != "sts"
    data = _txrx(ser, i, PING) is not None and _txrx(ser, i, READ, bytes(_SETTINGS))
    if not data:
        return None
    info = {
        "kind": kind,
        "firmware": f"{data[0]}.{data[1]}",
        "servo_version": f"{data[3]}.{data[4]}",
        "id": data[5],
        "max_temperature_c": data[13],
        "max_voltage_v": data[14] / 10,
        "min_voltage_v": data[15] / 10,
        "max_torque_permille": _word(data, 16, big),
    }
    if sts:
        info |= {"p_gain": data[21], "d_gain": data[22], "i_gain": data[23]}
    extra = (
        {"position_deg": "deg", "goal_position_deg": "deg", "current_ma": "mA"}
        if sts
        else {"current_raw": "count", "goal_position_raw": "count"}
    )
    return info, {**_UNITS, **extra}


def _fee_live(ser, info: dict):
    data = _txrx(ser, info["id"], READ, bytes(_LIVE))
    if data is None or len(data) < _LIVE[1]:
        return None
    return _sample(data, info["kind"] == "sts")


def check_rate(rate_hz: float) -> None:
    if not rate_hz > 0 or math.isinf(rate_hz):
        raise ValueError(f"--rate must be a number of samples per second above 0, not {rate_hz:g}")


def parse_ids(text: str) -> list[int]:
    """`1,3,5-7` as servo IDs; refuses a reversed range, a non-number and the broadcast ID."""
    ids: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        try:
            a, b = int(lo), int(hi or lo)
        except ValueError:
            raise ValueError(f"--ids: {part!r} is not a servo ID or a range like 1-6") from None
        if a > b:
            raise ValueError(f"--ids {part}: write the low ID first, e.g. {b}-{a}")
        if not 0 <= a <= b <= 253:
            raise ValueError("--ids: servo IDs run 0-253 (254 is the broadcast ID)")
        ids += range(a, b + 1)
    return ids


def record_servos(
    ser,
    ids: list[int],
    out: Path,
    *,
    kind: str = "sts",
    rate_hz: float = 20.0,
    seconds: float = 10.0,
    port: str = "",
    context: str = "bench-idle",
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict:
    """Record the bus into `out` (manifest.yaml + samples.jsonl); returns the manifest. A bus
    that fails mid-recording still writes what was read (marked partial), then raises."""
    check_rate(rate_hz)
    if kind == "dxl":
        from lockwire import servo_bus_dxl as dxl

        read_settings, read_live = dxl.read_settings, dxl.read_live
    else:
        read_settings = lambda ser, i: _fee_settings(ser, i, kind)  # noqa: E731
        read_live = _fee_live
    servos: dict[str, dict] = {}
    units: dict[str, dict[str, str]] = {}
    gaps: list[dict] = []
    for i in ids:
        got = read_settings(ser, i)
        if not got:
            gaps.append({"id": i, "from_s": 0.0, "message": f"servo {i} did not answer at start"})
            continue
        servos[f"servo.{i}"], units[f"servo.{i}"] = got
    if not servos:
        raise RuntimeError("no servo answered on the bus; check port, baud rate and power")
    out.mkdir(parents=True, exist_ok=True)  # only once a servo answered: no empty folder left

    rows, open_gap, failed = [], {}, None
    start = clock()
    try:
        for n in range(max(1, round(seconds * rate_hz))):
            wait = start + n / rate_hz - clock()
            if wait > 0:
                sleep(wait)
            t = round(clock() - start, 6)
            for dev, info in servos.items():
                sig = read_live(ser, info)
                if sig is None:
                    if dev not in open_gap:
                        open_gap[dev] = {
                            "id": info["id"],
                            "from_s": t,
                            "message": f"servo {info['id']} stopped answering",
                        }
                        gaps.append(open_gap[dev])
                    continue
                if dev in open_gap:
                    open_gap.pop(dev)["to_s"] = t
                rows.append(json.dumps({"t": t, "dev": dev, "sig": sig}, sort_keys=True))
    except OSError as error:  # the port dropped (serial errors are OSError): keep what was read
        failed = error
    (out / SAMPLES).write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    manifest = {
        "context": context,
        "capture_window_s": seconds,
        **({"partial": True} if failed else {}),
        "generator": {
            "source": "lockwire record-servos",
            "port": port,
            "rate_hz": rate_hz,
            **({} if kind == "dxl" else {"load_note": _LOAD_NOTE}),
            "servos": servos,
            "gaps": gaps,
        },
        "units": units,
    }
    with (out / MANIFEST).open("w", encoding="utf-8") as handle:
        plain_yaml().dump(manifest, handle)
    if failed:
        raise OSError(
            f"the bus failed mid-recording ({failed}); saved {len(rows)} sample(s) to {out}"
        ) from failed
    return manifest
