"""Python 3.12 sidecar for recording ROS command observations.

The ROS Humble node is Python 3.10; this tiny line-oriented process keeps the
core import on its supported interpreter. It never controls relay delivery.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lockwire_pro.observation_bridge import ObservationBridge


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: relay_writer.py CONTRACT RECORD SOURCE DEVICE", file=sys.stderr)
        return 2
    contract, record, source, device = sys.argv[1:]
    try:
        bridge = ObservationBridge(
            Path(contract), Path(record), protocol="ros2", source=source, device=device
        )
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": str(error)}), flush=True)
        return 2

    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request.get("operation", "observe")
            if operation == "continuity_break":
                bridge.break_continuity()
                response = {"status": "REFUSED", "issue": "observer queue overflow"}
            elif operation == "observe":
                raw_hex = request["raw_hex"]
                if not isinstance(raw_hex, str):
                    raise TypeError("raw_hex must be a string")
                source_time = request.get("source_time")
                if source_time is not None and not isinstance(source_time, (float, int)):
                    raise ValueError("source_time must be a number when provided")
                receipt_time = request.get("receipt_time")
                if not isinstance(receipt_time, (float, int)):
                    raise ValueError("receipt_time is required")
                observation = bridge.observe(
                    bytes.fromhex(raw_hex),
                    source_time=float(source_time) if source_time is not None else None,
                    receipt_time=float(receipt_time),
                )
                response = {
                    "status": observation.status,
                    "issue": observation.issue,
                    "receipt_order": observation.receipt_order,
                    "raw_sha256": observation.raw_sha256,
                    "source_sequence": observation.source_sequence,
                    "source_time": observation.source_time,
                    "command_id": observation.command_id,
                }
            elif operation == "forwarded":
                bridge.record_forwarded(
                    request["command_id"],
                    raw_sha256=request["raw_sha256"],
                    forwarded_time=float(request["event_time"]),
                )
                response = {"status": "OK"}
            elif operation == "delivery_failed":
                bridge.record_delivery_failure(
                    request["command_id"],
                    raw_sha256=request["raw_sha256"],
                    failure_time=float(request["event_time"]),
                    issue=str(request["issue"]),
                )
                response = {"status": "REFUSED"}
            else:
                raise ValueError("unsupported observer operation")
        except Exception as error:  # noqa: BLE001
            response = {"error": str(error)}
        print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
