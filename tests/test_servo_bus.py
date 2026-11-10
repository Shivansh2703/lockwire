import json

from lockwire.servo_bus import record_servos


class FakeBus:
    """Six STS servos; `dead` ids stop answering after `die_after` reads, `bad` get a wrong checksum."""

    def __init__(self, die_after=10**9, dead=(), bad=()):
        self.sent, self.buf, self.reads = [], b"", 0
        self.die_after, self.dead, self.bad = die_after, set(dead), set(bad)

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, pkt):
        self.sent.append(pkt)
        sid, instr, params = pkt[2], pkt[4], pkt[5:-1]
        self.reads += instr == 2
        if sid in self.dead and self.reads > self.die_after:
            return
        data = b""
        if instr == 2:
            addr, n = params
            regs = bytearray(80)
            regs[0:6] = bytes([1, 2, 0, 3, 4, sid])
            regs[21:24] = bytes([32, 16, 0])  # Kp Kd Ki
            regs[42:44] = (0x8000 | 100).to_bytes(2, "little")  # goal, negative (bit 15)
            regs[13:18] = bytes([70, 126, 60]) + (1000).to_bytes(2, "little")
            regs[56:58] = (2048).to_bytes(2, "little")
            regs[58:60] = (0x8000 | 50).to_bytes(2, "little")  # negative speed
            regs[60:62] = (0x400 | 120).to_bytes(2, "little")
            regs[62:64] = bytes([121, 35])
            regs[69:71] = (100).to_bytes(2, "little")
            data = bytes(regs[addr : addr + n])
        body = bytes([sid, len(data) + 2, 0]) + data
        chk = ~sum(body) & 0xFF
        self.buf = (
            b"\xff\xff" + body + bytes([chk ^ 1 if sid in self.bad and self.reads > 6 else chk])
        )

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
        bus, list(range(1, 7)), tmp_path, seconds=1, clock=lambda: next(clock), sleep=lambda s: None
    )


def test_output_loads_with_every_channel_and_unit(tmp_path):
    manifest = run(tmp_path, FakeBus())
    devs = read_back(tmp_path)
    assert sorted(devs) == [f"servo.{i}" for i in range(1, 7)]
    s = devs["servo.3"]
    assert set(s[0]) == set(manifest["units"]["servo.3"]) and len(s) == 20
    assert s[0]["speed_raw"] == -50 and s[0]["load_duty_permille"] == -120
    assert s[0]["position_deg"] == 180 and s[0]["current_ma"] == 650
    assert manifest["generator"]["servos"]["servo.3"]["max_temperature_c"] == 70
    assert manifest["generator"]["servos"]["servo.3"]["p_gain"] == 32
    assert s[0]["goal_position_deg"] == -8.789


def test_only_ping_and_read_are_ever_sent(tmp_path):
    bus = FakeBus()
    run(tmp_path, bus)
    assert bus.sent and {p[4] for p in bus.sent} == {1, 2}


def test_silent_servo_is_a_gap_not_a_crash(tmp_path):
    manifest = run(tmp_path, FakeBus(die_after=30, dead={2}))
    gap = manifest["generator"]["gaps"][0]
    assert gap["id"] == 2 and "stopped answering" in gap["message"]
    assert len(read_back(tmp_path)["servo.2"]) < 20


def test_bad_checksum_reply_is_not_recorded(tmp_path):
    manifest = run(tmp_path, FakeBus(bad={4}))
    assert manifest["generator"]["gaps"][0]["id"] == 4
    assert "servo.4" not in read_back(tmp_path)
