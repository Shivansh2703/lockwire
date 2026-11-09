"""ROS2/MAVLink command observation shared by fixtures and live transports.

A relay accepts only inbound bytes and hands that same object to a caller's sink.
A tap has no sink. Neither creates a command or certifies missing producer metadata.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from lockwire_pro.command_record import CommandEvent, append_command_event
from lockwire_pro.health import contract_sha
from lockwire.plan import Plan, validate_plan
from lockwire.report import verdict
from lockwire.schema import Contract, load_contract

Status = Literal["OK", "NOT_SCORED", "REFUSED"]


@dataclass(frozen=True)
class Observation:
    status: Status
    issue: str | None
    score: str
    source_sequence: int | None
    source_time: float | None
    receipt_time: float
    receipt_order: int
    raw_sha256: str
    command_id: str
    forwarded: bool = False


def _mavlink_message(raw: bytes, expected_system: int, expected_component: int):
    """Admit common-dialect v2 with checked CRC and expected header ids, not authentication."""
    if len(raw) < 12 or raw[0] != 0xFD:
        return None, "malformed MAVLink v2 frame"
    if raw[2] & 1:
        return None, "signed MAVLink frame lacks a configured signature verifier"
    try:
        from pymavlink.dialects.v20 import common
    except ImportError:
        return None, "pymavlink decoder unavailable"
    if common.MAVLINK_IGNORE_CRC:
        return None, "MAVLink CRC checking disabled; refusing admission"
    try:
        message = common.MAVLink(None).parse_char(raw)
    except common.MAVError as error:
        return None, f"MAVLink parse or CRC failure: {error}"
    if message is None or bytes(message.get_msgbuf()) != raw:
        return None, "incomplete or multiple MAVLink frames"
    if message.get_type().startswith("UNKNOWN"):
        return None, "unsupported MAVLink message in common dialect"
    if (message.get_srcSystem(), message.get_srcComponent()) != (
        expected_system,
        expected_component,
    ):
        return None, "MAVLink source system/component mismatch"
    return message, None


class ObservationBridge:
    """One ordered source, with an optional fixture-only pass-through sink."""

    def __init__(
        self,
        contract_path: Path,
        record_path: Path,
        *,
        protocol: Literal["ros2", "mavlink"],
        source: str,
        device: str,
        witness: str | None = None,
        mavlink_system: int | None = None,
        mavlink_component: int | None = None,
    ) -> None:
        if not source or not device:
            raise ValueError("source and device are required")
        if protocol == "mavlink" and (mavlink_system is None or mavlink_component is None):
            raise ValueError("MAVLink observation requires expected system and component")
        self.contract: Contract = load_contract(contract_path)
        self.contract_sha = contract_sha(contract_path)
        self.record_path = Path(record_path)
        self.protocol = protocol
        self.source = source
        self.device = device
        self.witness = witness
        self.mavlink_system = mavlink_system
        self.mavlink_component = mavlink_component
        self._last_sequence: int | None = None
        self._receipt_order = 0
        self._continuity_broken = False

    def _score(self, raw: bytes) -> tuple[dict[str, object], str]:
        if self.protocol == "mavlink":
            # Transport admission does not map a MAVLink dialect message to a Plan step.
            return {"unmapped_mavlink_sha256": hashlib.sha256(raw).hexdigest()}, "NOT_SCORED"
        try:
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise ValueError("command is not an object")
            report = validate_plan(self.contract, Plan.model_validate({"steps": [command]}))
            return command, verdict(report).code
        except Exception as error:  # a scorer fault must not withhold an inbound command
            return {
                "unscorable_raw_sha256": hashlib.sha256(raw).hexdigest()
            }, f"NOT_SCORED: {error}"

    def observe(
        self,
        raw: bytes,
        *,
        source_sequence: int | None = None,
        source_time: float | None = None,
        receipt_time: float | None = None,
        sink: Callable[[bytes], None] | None = None,
    ) -> Observation:
        if not isinstance(raw, bytes):
            raise TypeError("observed command must be bytes")
        if sink is not None and not callable(sink):
            raise TypeError("sink must be callable")
        self._receipt_order += 1
        order = self._receipt_order
        received = time.time() if receipt_time is None else receipt_time
        digest = hashlib.sha256(raw).hexdigest()
        if self.protocol == "mavlink":
            message, issue = _mavlink_message(raw, self.mavlink_system, self.mavlink_component)
            parsed_sequence = message.get_seq() if message is not None else None
            if issue is None and source_sequence is not None and parsed_sequence != source_sequence:
                issue = "declared sequence disagrees with validated MAVLink frame"
            source_sequence = parsed_sequence
        else:
            issue = None
        if issue is None and source_sequence is None:
            issue = "producer sequence unknown; local receipt order cannot prove no loss"
        advancing = source_sequence is not None and self._last_sequence is None
        if issue is None and self._last_sequence is not None:
            delta = (
                (source_sequence - self._last_sequence) % 256
                if self.protocol == "mavlink"
                else source_sequence - self._last_sequence
            )
            advancing = 0 < delta <= (127 if self.protocol == "mavlink" else delta)
            if delta == 0 or delta < 0 or (self.protocol == "mavlink" and delta > 127):
                issue = "duplicate or stale producer sequence"
            elif delta != 1:
                issue = "lost producer sequence"
        if advancing:
            self._last_sequence = source_sequence
        if issue is None and source_time is None:
            issue = "producer time unknown"
        if self._continuity_broken and (issue is None or "unknown" in issue):
            issue = "prior transport continuity break"
        status: Status = (
            "OK" if issue is None else ("NOT_SCORED" if "unknown" in issue else "REFUSED")
        )
        if status == "REFUSED":
            self._continuity_broken = True
        command, score = self._score(raw)
        overall = score if status == "OK" else status
        command_id = f"{self.protocol}:{self.source}:{order}:{uuid.uuid4().hex}"
        recorded = False
        try:
            append_command_event(
                self.record_path,
                CommandEvent(
                    event="received",
                    command_id=command_id,
                    t=received,
                    clock="unknown",
                    device=self.device,
                    issuer=self.source,  # observed channel, not authenticated person
                    command=command,
                    # A received row cannot claim Plan clearance before delivery.
                    overall_verdict=None if overall in ("GO", "NO_GO") else overall,
                    contract_sha=self.contract_sha,
                    witness=self.witness,
                    source=f"{self.protocol}:{self.source}",
                    source_sequence=source_sequence,
                    source_time=source_time,
                    receipt_time=received,
                    receipt_order=order,
                    raw_sha256=digest,
                    raw_hex=raw.hex(),
                    transport_issue=issue,
                ),
            )
            recorded = True
        except (OSError, ValueError) as error:
            status, issue = "REFUSED", f"command record continuity failed: {error}"
        forwarded = False
        if sink is not None:
            try:
                sink(raw)  # already received bytes, never decoded/re-encoded
                forwarded = True
            except Exception as error:
                status, issue = "REFUSED", f"relay delivery failed: {error}"
            if forwarded and recorded:
                try:
                    append_command_event(
                        self.record_path,
                        CommandEvent(
                            event="forwarded",
                            command_id=command_id,
                            t=time.time(),
                            clock="unknown",
                            device=self.device,
                            issuer=self.source,
                            source=f"{self.protocol}:{self.source}",
                            raw_sha256=digest,
                            overall_verdict=overall,
                        ),
                    )
                except (OSError, ValueError) as error:
                    status, issue = "REFUSED", f"forwarded byte record failed: {error}"
        if status == "REFUSED":
            self._continuity_broken = True
        return Observation(
            status,
            issue,
            score,
            source_sequence,
            source_time,
            received,
            order,
            digest,
            command_id,
            forwarded,
        )

    def record_forwarded(self, command_id: str, *, raw_sha256: str, forwarded_time: float) -> None:
        self._append_followup(
            CommandEvent(
                event="forwarded",
                command_id=command_id,
                t=forwarded_time,
                clock="unknown",
                device=self.device,
                issuer=self.source,
                source=f"{self.protocol}:{self.source}",
                raw_sha256=raw_sha256,
            )
        )

    def record_delivery_failure(
        self, command_id: str, *, raw_sha256: str, failure_time: float, issue: str
    ) -> None:
        self._append_followup(
            CommandEvent(
                event="delivery_failed",
                command_id=command_id,
                t=failure_time,
                clock="unknown",
                device=self.device,
                issuer=self.source,
                source=f"{self.protocol}:{self.source}",
                raw_sha256=raw_sha256,
                overall_verdict="REFUSED",
                transport_issue=issue,
            )
        )

    def _append_followup(self, event: CommandEvent) -> None:
        try:
            append_command_event(self.record_path, event)
        except (OSError, ValueError):
            self.break_continuity()
            raise

    def break_continuity(self) -> None:
        """Refuse later observations after the caller reports an unrecorded gap."""
        self._continuity_broken = True

    def tap(self, raw: bytes, **metadata: object) -> Observation:
        """Passive ingress; no send or publish capability is accepted."""
        if "sink" in metadata:
            raise PermissionError("passive tap cannot write or forward")
        return self.observe(raw, **metadata)
