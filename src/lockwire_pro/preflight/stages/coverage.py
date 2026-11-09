"""Coverage disclosure — the permanent offline boundary (needs live hardware).

This is not an unbuilt stage. B86's stub marker (``stage not yet implemented``)
means "we have not written the check yet." These findings mean the opposite:
the offline gate will **never** assert these capabilities; they need a
powered-on robot (contract §2 last row, honesty rule).

Capability names are the source of truth — ``docs/gate_coverage.md`` is tested
against them so a corrected doc cannot drift from what the gate emits.
"""

from __future__ import annotations

from lockwire_pro.preflight.source import PreflightSource
from lockwire.report import Finding
from lockwire.schema import Contract

# Real stage: always discloses; never silent; never affects GO/NO-GO.
IMPLEMENTED = True

# The frozen offline scope boundary: these two capabilities need a powered-on
# robot and are permanently out of reach of an offline gate. Do not paraphrase.
M1B_CAPABILITIES: tuple[str, ...] = (
    "live hardware↔software reconciliation",
    "comm links under representative load",
)

# Operators must not confuse "we will never check this here" with "we have not
# written it yet".
PERMANENT_BOUNDARY_MARKER = "permanently out of scope offline (requires live hardware)"


def check(_contract: Contract, _source: PreflightSource) -> list[Finding]:
    """Emit one ``not_scored`` finding per permanent live-hardware gap. Never return ``[]``."""
    findings = [_disclosure(capability) for capability in M1B_CAPABILITIES]
    if not findings:  # pragma: no cover - M1B_CAPABILITIES is non-empty by contract
        findings.append(
            Finding(
                severity="not_scored",
                quantity="preflight/stage/4/coverage",
                observed="coverage stage produced no disclosures",
                expected="named permanent live-hardware coverage disclosures on every path",
                action="restore M1B_CAPABILITIES so a GO cannot overclaim",
            )
        )
    return findings


def _disclosure(capability: str) -> Finding:
    slug = {
        "live hardware↔software reconciliation": "live_hardware_software_reconciliation",
        "comm links under representative load": "comm_links_under_representative_load",
    }[capability]
    return Finding(
        severity="not_scored",
        quantity=f"preflight/coverage/{slug}",
        observed=f"{capability}: {PERMANENT_BOUNDARY_MARKER}",
        expected=f"{capability} scored against live hardware",
        action=(
            f"a GO here does not mean {capability} was checked; that needs the powered-on robot"
        ),
    )
