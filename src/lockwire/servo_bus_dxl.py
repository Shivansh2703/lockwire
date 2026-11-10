"""Dynamixel X series, Protocol 2.0 half of the read-only servo recorder (see servo_bus.py).

Sends only ping (0x01) and read (0x02); `_packet` refuses anything else.
Packet layout, LEN = params + 3, byte stuffing (FF FF FD -> FF FF FD FD, LEN counts the
stuffed bytes, CRC over the stuffed packet, unstuff only after the CRC matches), status
packet INST 0x55 + ERR byte, and the example packets used in the tests:
https://emanual.robotis.com/docs/en/dxl/protocol2/
CRC: 16-bit, polynomial 0x8005, init 0, MSB-first, over header..last param, sent low byte
first (Robotis' published table/algorithm; the table here is generated from the polynomial
and checked against the e-manual example CRCs in tests/test_servo_bus_dxl.py).
Control table (XM430-W350 page, https://emanual.robotis.com/docs/en/dxl/x/xm430-w350/):
model 0 (2 B), firmware 6, ID 7, temp limit 31, max V 32 (2), min V 34 (2), current limit
38 (2, 2.69 mA), present current 126 (2, 2.69 mA), velocity 128 (4, 0.229 rpm), position
132 (4, 4096 pulses/rev), input voltage 144 (2, 0.1 V), temperature 146 (1, 1 degC).
XL430-W250 page: same addresses except 126 is Present Load (0.1 %) and there is no current
limit. So 126 is recorded raw (`load_or_current_raw`) with the model number; the current
limit is read only for model 1020 (XM430-W350), the one page where it was checked.
Goal and gains (same XM430-W350 control table): Goal Position 116 (4 B, 1 pulse), Position
D Gain 80, I Gain 82, P Gain 84 (2 B each, 0-16383, raw).
"""

import logging

from lockwire.servo_bus import PING, READ

log = logging.getLogger(__name__)
_STATUS = 0x55
_SETTINGS = (0, 86)  # model 0-1 .. current limit 38-39 .. gains D 80, I 82, P 84
_LIVE = (
    116,
    31,
)  # 116..146: goal, ..., current/load 126, velocity, position, ..., voltage, temperature
_POS = 10  # offset of 126 inside the live block
_XM430_W350 = 1020
_UNITS = {
    "position_raw": "count",
    "goal_position_deg": "deg",
    "position_deg": "deg",  # 360/4096 = 0.088 deg per count
    "velocity_rpm": "rpm",
    "load_or_current_raw": "raw",  # 126 is current (XM) or load (XL); see model_number
    "voltage_v": "V",
    "temperature_c": "degC",
}


def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005 if crc & 0x8000 else crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC = _make_crc_table()


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def _packet(servo_id: int, instr: int, params: bytes = b"") -> bytes:
    if instr not in (PING, READ):
        raise ValueError(f"servo_bus is read-only; refusing instruction {instr:#04x}")
    payload = (bytes([instr]) + params).replace(b"\xff\xff\xfd", b"\xff\xff\xfd\xfd")
    body = b"\xff\xff\xfd\x00" + bytes([servo_id]) + (len(payload) + 2).to_bytes(2, "little")
    body += payload
    return body + crc16(body).to_bytes(2, "little")


def _txrx(ser, servo_id: int, instr: int, params: bytes = b"") -> bytes | None:
    """Params of a valid status reply, or None (silence, wrong ID, error byte, bad CRC)."""
    ser.reset_input_buffer()
    ser.write(_packet(servo_id, instr, params))
    head = ser.read(7)
    if len(head) < 7 or head[:4] != b"\xff\xff\xfd\x00" or head[4] != servo_id:
        return None
    n = int.from_bytes(head[5:7], "little")
    rest = ser.read(n)  # INST, ERR, params, CRC
    if len(rest) < n or n < 4:
        return None
    if crc16(head + rest[:-2]) != int.from_bytes(rest[-2:], "little"):
        log.warning("servo %d: reply rejected, CRC mismatch", servo_id)
        return None
    if rest[0] != _STATUS or rest[1] & 0x7F:  # bit 7 is only the hardware-alert flag
        return None
    return rest[2:-2].replace(b"\xff\xff\xfd\xfd", b"\xff\xff\xfd")


def _int(data: bytes, i: int, n: int, signed: bool = False) -> int:
    return int.from_bytes(data[i : i + n], "little", signed=signed)


def read_settings(ser, i: int) -> tuple[dict, dict[str, str]] | None:
    data = _txrx(ser, i, PING) is not None and _txrx(ser, i, READ, bytes([0, 0, _SETTINGS[1], 0]))
    if not data or len(data) < _SETTINGS[1]:
        return None
    model = _int(data, 0, 2)
    info = {
        "kind": "dynamixel-x",
        "model_number": model,
        "firmware": data[6],
        "id": data[7],
        "max_temperature_c": data[31],
        "p_gain": _int(data, 84, 2),
        "i_gain": _int(data, 82, 2),
        "d_gain": _int(data, 80, 2),
        "max_voltage_v": _int(data, 32, 2) / 10,
        "min_voltage_v": _int(data, 34, 2) / 10,
    }
    if model == _XM430_W350:
        info["current_limit_ma"] = round(_int(data, 38, 2) * 2.69, 2)
    return info, dict(_UNITS)


def read_live(ser, info: dict) -> dict[str, float] | None:
    data = _txrx(ser, info["id"], READ, bytes([_LIVE[0], 0, _LIVE[1], 0]))
    if data is None or len(data) < _LIVE[1]:
        return None
    goal = _int(data, 0, 4, True)
    data = data[_POS:]
    pos = _int(data, 6, 4, True)
    return {
        "goal_position_deg": round(goal * 360 / 4096, 3),
        "load_or_current_raw": _int(data, 0, 2, True),
        "velocity_rpm": round(_int(data, 2, 4, True) * 0.229, 3),
        "position_raw": pos,
        "position_deg": round(pos * 360 / 4096, 3),
        "voltage_v": _int(data, 18, 2) / 10,
        "temperature_c": data[20],
    }
