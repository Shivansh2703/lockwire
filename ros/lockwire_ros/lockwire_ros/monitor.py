"""Continuous monitoring, ROS-free: the last window of traffic -> the gate -> a status.

The core scores a recording in batch; there is no streaming scorer. So the
monitor works in windows: the subscriptions keep appending to each
``DeviceCapture`` (the rows list is the rolling buffer), and every period the
node takes the last ``window_s`` seconds, writes them as a recording and runs
`lockwire preflight` on it out of process, exactly like the lifecycle gate.

Split from ``monitor_node`` so the part with judgement in it (what goes in a
window, what a failure publishes) is testable off-robot.

**Every window publishes, and a failure publishes louder than a pass.** No
traffic, an unreadable field, a missing or hung tool, an exception in the gate
runner: each becomes a REFUSED result and an ERROR status carrying the reason.
A monitor that goes quiet when it breaks reads exactly like a healthy robot.

Python 3.10 compatible.
"""

from __future__ import annotations

import copy
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

from lockwire_ros import core, diagnostics
from lockwire_ros.capture import CaptureError, DeviceCapture, write_recording

Gate = Callable[[str], core.GateResult]
Report = Callable[[List[diagnostics.DiagnosticRow]], None]


def status_row(rows: List[diagnostics.DiagnosticRow]) -> diagnostics.DiagnosticRow:
    """One status for `/lockwire/status`: the summary, with every finding row as a value.

    Level and message are the summary's (GO -> OK, GO with notes -> WARN, NOT
    SCORED -> WARN, NO-GO and REFUSED -> ERROR; the mapping lives in
    ``diagnostics``), so the latched topic and `/diagnostics` cannot disagree.
    """
    summary = rows[0]
    findings = [
        (row.name, "{0}: {1}".format(level_name(row.level), row.message)) for row in rows[1:]
    ]
    return diagnostics.DiagnosticRow(
        level=summary.level, name=summary.name, message=summary.message,
        values=list(summary.values) + findings,
    )


class Monitor:
    """Turns the rolling buffer into one published verdict per window."""

    def __init__(
        self,
        captures: List[DeviceCapture],
        window_s: float,
        context: str,
        gate: Gate,
        report: Report,
        out_dir: str,
        keep_last: int = 0,
    ) -> None:
        self.captures = captures
        self.window_s = window_s
        self.context = context
        self.gate = gate
        self.report = report
        self.out_dir = out_dir
        self.keep_last = keep_last
        self._count = 0
        self._kept: List[str] = []
        self._faults: Dict[str, str] = {}

    def take(self, capture: DeviceCapture, message: Any, recv_time_s: float) -> None:
        """The subscription handler. A message the contract cannot read is a fault, not a crash.

        Raised inside an rclpy callback, the CaptureError would end the spin and
        the node would die without publishing anything. Held here instead, it
        becomes the next window's ERROR, and every window after while it lasts.
        """
        try:
            capture.take(message, recv_time_s)
        except CaptureError as error:
            self._faults[capture.device_id] = "device {0!r} on {1}: {2}".format(
                capture.device_id, capture.topic, error)

    def take_faults(self) -> List[str]:
        """The faults seen since the last call, cleared. Called with the snapshot."""
        faults, self._faults = sorted(self._faults.values()), {}
        return faults

    def snapshot(self, now: float) -> List[DeviceCapture]:
        """Drop everything older than the window (in place, so memory is bounded) and copy the rest.

        Cheap on purpose: the node calls this on the executor thread, then
        gates the copy elsewhere so subscriptions keep receiving meanwhile.
        """
        since = now - self.window_s
        copies = []
        for capture in self.captures:
            capture.rows[:] = [row for row in capture.rows if row[0] >= since]
            capture.deadline_misses[:] = [e for e in capture.deadline_misses if e[0] >= since]
            capture.procedure_events[:] = [e for e in capture.procedure_events if e[0] >= since]
            clone = copy.copy(capture)
            clone.rows = list(capture.rows)
            clone.deadline_misses = list(capture.deadline_misses)
            clone.procedure_events = list(capture.procedure_events)
            copies.append(clone)
        return copies

    def run_window(
        self, window: List[DeviceCapture], faults: Optional[List[str]] = None
    ) -> List[diagnostics.DiagnosticRow]:
        """Write the window, gate it, publish. Never skips the publish.

        Faults (messages the contract could not read) refuse the window without
        gating it: the device's rows are missing, and "no messages" would hide why.
        """
        self._count += 1
        path = os.path.join(self.out_dir, "window-{0:06d}".format(self._count))
        try:
            if faults:
                raise CaptureError("; ".join(faults))
            result = self.gate(write_recording(path, window, self.context, "lockwire_ros.monitor"))
        except Exception as error:  # every failure is a published refusal, never silence
            result = core.GateResult(outcome=core.REFUSED, reason="monitor window failed: {0}".format(error))
        self._retire(path)
        rows = diagnostics.diagnostic_rows(result)
        self.report(rows)
        return rows

    def _retire(self, path: str) -> None:
        self._kept.append(path)
        while len(self._kept) > self.keep_last:
            shutil.rmtree(self._kept.pop(0), ignore_errors=True)

    def close(self) -> None:
        """Remove the temp dir unless recordings were asked to be kept."""
        if not self.keep_last:
            shutil.rmtree(self.out_dir, ignore_errors=True)


def level_name(level: int) -> str:
    return {0: "OK", 1: "WARN", 2: "ERROR", 3: "STALE"}.get(level, str(level))


def default_gate(contract: str, executable: str, timeout_s: float) -> Gate:
    def gate(recording: str) -> core.GateResult:
        return core.run_gate(contract, recording, executable=executable, timeout_s=timeout_s)

    return gate
