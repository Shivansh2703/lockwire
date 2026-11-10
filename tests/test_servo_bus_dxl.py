import json
import logging

from lockwire.servo_bus import record_servos
from lockwire.servo_bus_dxl import _packet, crc16


class FakeDxl:
    """Two XM430-W350 servos (ids 1, 2); `bad` ids get a corrupted CRC after the settings reads."""

    def __init__(self, bad=()):
        self.sent, self.buf, self.reads, self.bad = [], b"", 0, set(bad)

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, pkt):
        self.sent.append(pkt)
        sid, instr = pkt[4], pkt[7]
        assert crc16(pkt[:-2]).to_bytes(2, "little") == pkt[-2:]
        data = b""
        if instr == 2:
            self.reads += 1
            addr, n = int.from_bytes(pkt[8:10], "little"), int.from_bytes(pkt[10:12], "little")
            regs = bytearray(160)
            regs[0:2] = (1020).to_bytes(2, "little")
            regs[6:8] = bytes([46, sid])
            regs[31] = 80
            regs[32:36] = (160).to_bytes(2, "little") + (95).to_bytes(2, "little")
            regs[38:40] = (1193).to_bytes(2, "little")
            regs[80:86] = (
                (5).to_bytes(2, "little") + (7).to_bytes(2, "little") + (800).to_bytes(2, "little")
            )
            regs[116:120] = (2048 + 100).to_bytes(4, "little")
            regs[126:128] = (-100).to_bytes(2, "little", signed=True)
            regs[128:132] = (100).to_bytes(4, "little")
            regs[132:136] = (0x00FDFFFF if sid == 2 else 2048).to_bytes(4, "little")
            regs[144:147] = (121).to_bytes(2, "little") + bytes([35])
            data = bytes(regs[addr : addr + n])
        payload = (b"\x55\x00" + data).replace(b"\xff\xff\xfd", b"\xff\xff\xfd\xfd")
        body = b"\xff\xff\xfd\x00" + bytes([sid]) + (len(payload) + 2).to_bytes(2, "little")
        crc = crc16(body + payload) ^ (1 if sid in self.bad and self.reads > 2 else 0)
        self.buf = body + payload + crc.to_bytes(2, "little")

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def read_back(path):
    """Samples per device straight from samples.jsonl: no Pro loader."""
    rows = [json.loads(ln) for ln in (path / "samples.jsonl").read_text().splitlines()]
    return {d: [r["sig"] for r in rows if r["dev"] == d] for d in {r["dev"] for r in rows}}


def run(tmp_path, bus):
    clock = iter(x / 20 for x in range(10**4))
    return record_servos(
        bus,
        [1, 2],
        tmp_path,
        kind="dxl",
        seconds=1,
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )


def test_crc_matches_emanual_examples():
    assert _packet(1, 1) == bytes.fromhex("FFFFFD0001030001194E")
    assert _packet(0xFE, 1) == bytes.fromhex("FFFFFD00FE0300013142")
    assert _packet(1, 2, bytes.fromhex("8400 0400")) == bytes.fromhex(
        "FFFFFD00 01 0700 02 8400 0400 1D15"
    )
    assert crc16(bytes.fromhex("FFFFFD00 01 0800 55 00 A6000000")) == 0xC08C


def test_output_loads_with_channels_units_and_stuffed_bytes(tmp_path):
    manifest = run(tmp_path, FakeDxl())
    devs = read_back(tmp_path)
    s, units = devs["servo.1"], manifest["units"]
    assert set(s[0]) == set(units["servo.1"]) and len(s) == 20
    assert s[0]["position_deg"] == 180 and s[0]["velocity_rpm"] == 22.9
    assert s[0]["load_or_current_raw"] == -100 and units["servo.1"]["voltage_v"] == "V"
    assert devs["servo.2"][0]["position_raw"] == 0x00FDFFFF
    assert manifest["generator"]["servos"]["servo.2"]["model_number"] == 1020
    assert manifest["generator"]["servos"]["servo.1"]["p_gain"] == 800
    assert s[0]["goal_position_deg"] == 188.789


def test_only_ping_and_read_are_ever_sent(tmp_path):
    bus = FakeDxl()
    run(tmp_path, bus)
    assert bus.sent and {p[7] for p in bus.sent} == {1, 2}


def test_corrupted_crc_reply_is_rejected_and_logged_not_recorded(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        manifest = run(tmp_path, FakeDxl(bad={2}))
    assert "CRC mismatch" in caplog.text
    assert manifest["generator"]["gaps"][0]["id"] == 2
    assert "servo.2" not in read_back(tmp_path)
