import json
from pathlib import Path

import pytest

import lockwire_pro.observation_bridge as observation_bridge
from lockwire_pro.command_record import read_command_records, verify_commands
from lockwire_pro.observation_bridge import ObservationBridge

CONTRACT = Path(__file__).parent / "fixtures" / "contract_clean.yaml"


def test_ros_delivery_lifecycle_records_actual_publish_outcome(tmp_path):
    path = tmp_path / "delivery.jsonl"
    bridge = ObservationBridge(CONTRACT, path, protocol="ros2", source="/cmd", device="joint")
    delivered = bridge.observe(b"{}", receipt_time=1.25)
    bridge.record_forwarded(
        delivered.command_id, raw_sha256=delivered.raw_sha256, forwarded_time=1.5
    )
    failed = bridge.observe(b'{"target":1}', receipt_time=2.25)
    bridge.record_delivery_failure(
        failed.command_id,
        raw_sha256=failed.raw_sha256,
        failure_time=2.5,
        issue="publisher unavailable",
    )

    records = read_command_records(path)
    assert records[0].events == ["received", "forwarded"]
    assert records[0].forwarded_at == 1.5
    assert records[1].events == ["received", "delivery_failed"]
    assert records[1].forwarded_at is None and not records[1].complete
    assert records[1].overall_verdict == "REFUSED"
    assert records[1].transport_issue == "publisher unavailable"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]["event"] == "delivery_failed"
    assert rows[-1]["overall_verdict"] == "REFUSED"
    assert rows[-1]["transport_issue"] == "publisher unavailable"
    assert verify_commands(path) is None


@pytest.mark.parametrize("event", ["forwarded", "delivery_failed"])
def test_failed_followup_record_refuses_next_observation(tmp_path, monkeypatch, event):
    path = tmp_path / "followup-failure.jsonl"
    bridge = ObservationBridge(CONTRACT, path, protocol="ros2", source="/cmd", device="joint")
    first = bridge.observe(b"{}", receipt_time=1.25)
    append = observation_bridge.append_command_event

    def fail_followup(record_path, command_event):
        if command_event.event == event:
            raise OSError("followup append failed")
        return append(record_path, command_event)

    monkeypatch.setattr(observation_bridge, "append_command_event", fail_followup)
    with pytest.raises(OSError, match="followup append failed"):
        if event == "forwarded":
            bridge.record_forwarded(
                first.command_id, raw_sha256=first.raw_sha256, forwarded_time=1.5
            )
        else:
            bridge.record_delivery_failure(
                first.command_id,
                raw_sha256=first.raw_sha256,
                failure_time=1.5,
                issue="publisher unavailable",
            )

    next_observation = bridge.observe(b'{"target":1}', receipt_time=2.25)
    assert next_observation.status == "REFUSED"
    assert next_observation.issue == "prior transport continuity break"
    assert read_command_records(path)[-1].overall_verdict == "REFUSED"
