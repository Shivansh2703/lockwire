"""Local-only ROS2/MAVLink pass-through and separate passive observation seam."""

import hashlib
import json
from pathlib import Path

import pytest

from lockwire.bench import DEFAULT_CORPUS_PATH, load_corpus
from lockwire_pro.command_record import read_command_records, verify_commands
from lockwire_pro.observation_bridge import ObservationBridge

X500 = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"


def _wire(case_id: str) -> bytes:
    step = next(case.step for case in load_corpus(DEFAULT_CORPUS_PATH) if case.id == case_id)
    return json.dumps(step.model_dump(mode="json"), separators=(",", ":")).encode()


def _mavlink(seq: int) -> bytes:
    from pymavlink.dialects.v20 import common

    encoder = common.MAVLink(None, srcSystem=1, srcComponent=1)
    encoder.seq = seq
    return encoder.heartbeat_encode(6, 8, 0, 0, 4, 3).pack(encoder)


def test_ros2_fixture_relay_and_independent_tap_keep_bytes_order_and_identity(tmp_path):
    relay = ObservationBridge(
        X500, tmp_path / "relay.jsonl", protocol="ros2", source="/cmd", device="rail.servo-motor"
    )
    tap = ObservationBridge(
        X500, tmp_path / "tap.jsonl", protocol="ros2", source="/cmd", device="rail.servo-motor"
    )
    inbound = [_wire("servo-headroom-small"), _wire("pm02-overload-small"), b"not json"]
    sent: list[bytes] = []
    for seq, raw in enumerate(inbound, start=10):
        observed = relay.observe(
            raw,
            source_sequence=seq,
            source_time=float(seq),
            receipt_time=float(seq) + 0.1,
            sink=sent.append,
        )
        passive = tap.tap(
            raw, source_sequence=seq, source_time=float(seq), receipt_time=float(seq) + 0.2
        )
        assert observed.status == passive.status == "OK"
        assert observed.raw_sha256 == passive.raw_sha256 == hashlib.sha256(raw).hexdigest()
        assert observed.forwarded and not passive.forwarded
    assert sent == inbound
    records = read_command_records(tmp_path / "relay.jsonl")
    assert [r.raw_hex for r in records] == [raw.hex() for raw in inbound]
    assert [r.source_sequence for r in records] == [10, 11, 12]
    assert [r.receipt_order for r in records] == [1, 2, 3]
    assert [r.events for r in records] == [["received", "forwarded"]] * 3
    assert [r.overall_verdict.split(":")[0] for r in records] == ["GO", "NO_GO", "NOT_SCORED"]
    assert verify_commands(tmp_path / "relay.jsonl") is None


def test_failed_fixture_delivery_cannot_leave_go_in_received_sidecar(tmp_path):
    path = tmp_path / "failed.jsonl"
    bridge = ObservationBridge(X500, path, protocol="ros2", source="/cmd", device="joint")

    def broken_sink(_raw: bytes) -> None:
        raise OSError("fixture link lost")

    result = bridge.observe(
        _wire("servo-headroom-small"),
        source_sequence=1,
        source_time=1,
        receipt_time=1.1,
        sink=broken_sink,
    )
    assert result.score == "GO" and result.status == "REFUSED"
    assert not result.forwarded and "relay delivery failed" in result.issue
    row = json.loads(path.read_text().splitlines()[0])
    assert row["event"] == "received" and row["overall_verdict"] is None
    record = read_command_records(path)[0]
    assert record.events == ["received"] and record.overall_verdict is None
    assert verify_commands(path) is None


def test_mavlink_proxy_fixture_preserves_bytes_and_reports_loss(tmp_path):
    bridge = ObservationBridge(
        X500,
        tmp_path / "mavlink.jsonl",
        protocol="mavlink",
        source="sys1/comp1",
        device="flight",
        mavlink_system=1,
        mavlink_component=1,
    )
    sent: list[bytes] = []
    first = _mavlink(4)
    second = _mavlink(6)
    one = bridge.observe(first, source_time=1, receipt_time=1.1, sink=sent.append)
    gap = bridge.observe(second, source_time=2, receipt_time=2.1, sink=sent.append)
    stale = bridge.observe(second, source_time=3, receipt_time=3.1, sink=sent.append)
    assert one.status == "OK" and one.score == "NOT_SCORED"
    assert gap.status == "REFUSED" and gap.issue == "lost producer sequence"
    assert stale.status == "REFUSED" and stale.issue == "duplicate or stale producer sequence"
    healed = bridge.observe(_mavlink(7), source_time=4, receipt_time=4.1, sink=sent.append)
    assert healed.status == "REFUSED" and healed.issue == "prior transport continuity break"
    assert sent == [first, second, second, _mavlink(7)]
    assert [r.source_sequence for r in read_command_records(tmp_path / "mavlink.jsonl")] == [
        4,
        6,
        6,
        7,
    ]


def test_unknown_producer_metadata_and_malformed_frame_never_become_clearance(tmp_path):
    ros = ObservationBridge(
        X500, tmp_path / "ros.jsonl", protocol="ros2", source="/cmd", device="joint"
    )
    unknown = ros.observe(_wire("servo-headroom-small"), receipt_time=1)
    assert unknown.status == "NOT_SCORED" and unknown.source_sequence is None
    assert unknown.receipt_order == 1
    assert read_command_records(tmp_path / "ros.jsonl")[0].overall_verdict == "NOT_SCORED"
    mav = ObservationBridge(
        X500,
        tmp_path / "mav.jsonl",
        protocol="mavlink",
        source="sys1",
        device="flight",
        mavlink_system=1,
        mavlink_component=1,
    )
    sent: list[bytes] = []
    bad = mav.observe(b"\xfd\x02", source_time=1, receipt_time=2, sink=sent.append)
    assert bad.status == "REFUSED" and "malformed" in bad.issue
    assert sent == [b"\xfd\x02"]
    with pytest.raises(PermissionError, match="cannot write"):
        ros.tap(b"x", sink=sent.append)
    assert sent == [b"\xfd\x02"]


def test_broken_record_chain_refuses_visibly_but_never_withholds_inbound_bytes(tmp_path):
    path = tmp_path / "record.jsonl"
    bridge = ObservationBridge(X500, path, protocol="ros2", source="/cmd", device="joint")
    bridge.observe(_wire("servo-headroom-small"), source_sequence=1, source_time=1, receipt_time=1)
    path.write_bytes(path.read_bytes().replace(b'"source_sequence": 1', b'"source_sequence": 9'))
    assert verify_commands(path) is not None
    sent: list[bytes] = []
    raw = _wire("pm02-overload-small")
    result = bridge.observe(raw, source_sequence=2, source_time=2, receipt_time=2, sink=sent.append)
    assert result.status == "REFUSED" and "continuity failed" in result.issue
    assert result.forwarded and sent == [raw]


def test_mavlink_crc_and_unsupported_message_are_unadmitted_raw_evidence(tmp_path):
    first = ObservationBridge(
        X500,
        tmp_path / "first.jsonl",
        protocol="mavlink",
        source="sys1",
        device="flight",
        mavlink_system=1,
        mavlink_component=1,
    )
    changed = ObservationBridge(
        X500,
        tmp_path / "changed.jsonl",
        protocol="mavlink",
        source="sys1",
        device="flight",
        mavlink_system=1,
        mavlink_component=1,
    )
    frame = _mavlink(7)
    bad_crc = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    a = first.observe(frame, source_time=1, receipt_time=2)
    b = changed.observe(bad_crc, source_time=1, receipt_time=2)
    assert a.status == "OK" and a.score == "NOT_SCORED"
    assert b.status == "REFUSED" and "CRC" in b.issue
    assert a.raw_sha256 != b.raw_sha256
    assert read_command_records(tmp_path / "changed.jsonl")[0].overall_verdict == "REFUSED"
    unsupported = bytearray(_mavlink(8))
    unsupported[7:10] = b"\xff\xff\xff"  # no common-dialect message definition
    sent: list[bytes] = []
    result = first.observe(bytes(unsupported), source_time=2, receipt_time=3, sink=sent.append)
    assert result.status == "REFUSED" and "unsupported" in result.issue
    assert sent == [bytes(unsupported)]


def test_mavlink_wrong_identity_crc_bypass_and_unsigned_only_admission(tmp_path, monkeypatch):
    from pymavlink.dialects.v20 import common

    frame = _mavlink(12)
    wrong = ObservationBridge(
        X500,
        tmp_path / "wrong.jsonl",
        protocol="mavlink",
        source="sys2",
        device="flight",
        mavlink_system=2,
        mavlink_component=1,
    )
    sent: list[bytes] = []
    identity = wrong.observe(frame, source_time=1, receipt_time=2, sink=sent.append)
    assert identity.status == "REFUSED" and "system/component mismatch" in identity.issue
    assert sent == [frame]

    bridge = ObservationBridge(
        X500,
        tmp_path / "bypass.jsonl",
        protocol="mavlink",
        source="sys1",
        device="flight",
        mavlink_system=1,
        mavlink_component=1,
    )
    monkeypatch.setattr(common, "MAVLINK_IGNORE_CRC", "1")
    bypass = bridge.observe(frame, source_time=1, receipt_time=2)
    assert bypass.status == "REFUSED" and "CRC checking disabled" in bypass.issue
    monkeypatch.undo()

    signed = bytearray(frame)
    signed[2] = 1
    signed.extend(bytes(range(13)))
    refused = bridge.observe(bytes(signed), source_time=2, receipt_time=3, sink=sent.append)
    assert refused.status == "REFUSED" and "signature verifier" in refused.issue
    assert sent == [frame, bytes(signed)]
