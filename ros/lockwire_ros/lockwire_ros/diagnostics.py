"""Preflight findings as `/diagnostics`, so the verdict shows up where crews already look.

Split in two on purpose. ``diagnostic_rows`` is pure Python over the JSON
document and can be tested on any machine; ``to_diagnostic_array`` is the thin
part that needs ``diagnostic_msgs`` and therefore a ROS environment. The mapping
is the part with judgement in it, so the mapping is the part that stays testable.

**The severity mapping, and why each choice is the way it is.**

- ``violation`` -> ERROR. It is a no-go.
- ``warning``   -> WARN. MEL-style: named, degraded, still cleared to operate.
- ``not_scored``-> **OK, collected into one ``coverage`` row.** These are
  disclosures, not faults. Raising them to WARN would paint the robot degraded
  on every clean run, and a monitor that is always yellow is a monitor nobody
  reads — the crying-wolf death condition this product is designed around. But
  they must still be *visible*, because "what the gate did not check" is the
  half the product exists to say out loud (`docs/gate_coverage.md`), so they are
  carried by name rather than dropped.
- a refusal (the gate could not run) -> ERROR, and worded so it cannot be
  misread as the robot being fine.

Python 3.10 compatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from lockwire_ros import core

# diagnostic_msgs/msg/DiagnosticStatus level constants, restated so this module
# imports nothing from ROS and stays testable off-robot. Values are fixed by the
# message definition and cannot drift.
OK = 0
WARN = 1
ERROR = 2
STALE = 3

_SEVERITY_LEVEL = {"violation": ERROR, "warning": WARN}


@dataclass(frozen=True)
class DiagnosticRow:
    """One `/diagnostics` entry, before it becomes a ROS message."""

    level: int
    name: str
    message: str
    values: List[Tuple[str, str]] = field(default_factory=list)


def _summary_level(result: core.GateResult) -> int:
    if result.outcome == core.REFUSED:
        return ERROR
    if result.outcome == core.NO_GO:
        return ERROR
    if result.scored_nothing:
        # Exits 0 and blocks nothing, so this is not a false no-go — but a pass
        # with a zero denominator is a real problem and must not read as health.
        return WARN
    if result.counts.get("warning", 0):
        return WARN
    return OK


def _finding_message(finding: Dict[str, Any]) -> str:
    observed = finding.get("observed", "")
    expected = finding.get("expected", "")
    message = "observed {0}, expected {1}".format(observed, expected)
    delta = finding.get("delta")
    if delta:
        message += " (delta {0})".format(delta)
    return message


def _finding_values(finding: Dict[str, Any]) -> List[Tuple[str, str]]:
    keys = (
        "severity",
        "stage",
        "observed",
        "expected",
        "delta",
        "dependent",
        "owner",
        "source",
        "action",
    )
    return [(key, str(finding.get(key))) for key in keys if finding.get(key) is not None]


def diagnostic_rows(result: core.GateResult, prefix: str = "lockwire") -> List[DiagnosticRow]:
    """Every row this run should publish, summary first."""
    summary_values: List[Tuple[str, str]] = [("outcome", result.outcome)]
    if result.verdict is not None:
        summary_values.append(("verdict", result.verdict))
    if result.exit_code is not None:
        summary_values.append(("exit_code", str(result.exit_code)))
    if result.scored is not None:
        summary_values.append(("scored", str(result.scored)))
    for severity in ("violation", "warning", "not_scored"):
        if severity in result.counts:
            summary_values.append((severity, str(result.counts[severity])))

    rows = [
        DiagnosticRow(
            level=_summary_level(result),
            name="{0}: preflight".format(prefix),
            message=result.reason,
            values=summary_values,
        )
    ]

    for finding in result.findings:
        level = _SEVERITY_LEVEL.get(finding.get("severity", ""))
        if level is None:
            continue
        rows.append(
            DiagnosticRow(
                level=level,
                name="{0}: {1}".format(prefix, finding.get("quantity", "<unnamed>")),
                message=_finding_message(finding),
                values=_finding_values(finding),
            )
        )

    disclosures = [f for f in result.findings if f.get("severity") == "not_scored"]
    if disclosures:
        rows.append(
            DiagnosticRow(
                level=OK,
                name="{0}: coverage".format(prefix),
                message="{0} check(s) not scored — this GO does not cover them".format(
                    len(disclosures)
                ),
                values=[
                    (str(f.get("quantity", "<unnamed>")), str(f.get("observed", "")))
                    for f in disclosures
                ],
            )
        )
    return rows


def to_diagnostic_array(rows: List[DiagnosticRow], stamp, hardware_id: str = ""):
    """Wrap rows in a ``DiagnosticArray``. Requires a ROS environment."""
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

    array = DiagnosticArray()
    array.header.stamp = stamp
    array.status = [
        DiagnosticStatus(
            level=bytes([row.level]),
            name=row.name,
            message=row.message,
            hardware_id=hardware_id,
            values=[KeyValue(key=key, value=value) for key, value in row.values],
        )
        for row in rows
    ]
    return array
