"""The paid fields of the page payload: the run section, the health strip, and the Run/Health modes.

Registered on ``plugins.PAGE_EXTRAS``; ``lockwire.page.build_payload`` calls it and merges the
returned fields over the free payload."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lockwire.page import _COMMAND_MODES, _cards
from lockwire.report import Finding
from lockwire.report_html import _redact_path
from lockwire.schema import Contract
from lockwire_pro import health
from lockwire_pro.preflight.report import PreflightReport

_STAGE_NAMES = {
    0: "Is everything plugged in",
    1: "Is everything talking",
    2: "Is the data flowing",
    3: "Do the readings make sense",
    4: "Do the records agree",
}


def _stage_rows(findings: list[Finding]) -> list[dict[str, Any]]:
    scored_by_stage = findings.scored_by_stage if isinstance(findings, PreflightReport) else {}
    rows = []
    for stage, name in _STAGE_NAMES.items():
        at_stage = [finding for finding in findings if finding.stage == stage]
        rows.append(
            {
                "stage": stage,
                "name": name,
                "scored": scored_by_stage.get(stage),
                "violations": sum(finding.severity == "violation" for finding in at_stage),
                "unchecked": sum(finding.severity == "not_scored" for finding in at_stage),
            }
        )
    return rows


MODE_ORDER = ["design", "declare", "check", "plan", "run", "health", "reference"]
MODE_LABELS = {
    "design": "Design",
    "declare": "Declare",
    "check": "Check",
    "plan": "Plan",
    "run": "Run",
    "health": "Health",
    "reference": "Reference",
}

# The paid commands' modes, and `scan` moving off the free "reference" default.
COMMAND_MODES = {
    **_COMMAND_MODES,
    "actuator-review-draft": "check",  # read-only carrier plus engineer review draft
    "preflight": "run",
    "bag": "run",  # `bag check` is preflight --from a bag; `bag inspect` reads the same bag
    "baseline": "run",
    "baseline-review": "run",
    "baseline-check": "run",
    "scan": "run",
    "sample": "health",  # the robot's own computer as a recording
    "health": "health",  # the ledger's per-part state
}

# The sidecar path is `health.py`'s to decide, not a second copy of the rule
# here (E0-25): kept as the alias.
health_ledger_path = health.sidecar_path_for


def _read_health_summary(contract_path: Path, *, now: datetime) -> dict[str, Any] | None:
    """The health strip/face's summary — ``health.py``'s own reader
    (``read_ledger`` + ``own_and_foreign`` + ``summary``), not a second
    implementation of it. Foreign-contract-version rows are dropped by the same
    predicate the CLI table and ``api.health`` use (E0-25)."""
    rows, skipped = health.read_ledger_with_skipped(health_ledger_path(contract_path))
    own, _foreign = health.own_and_foreign(rows, contract_path)
    if not own:
        return None
    return health.summary(own, now, skipped=skipped)


def _read_health_devices(
    contract: Contract, contract_path: Path, *, now: datetime
) -> list[dict[str, Any]]:
    """Each row's own contract sha, bag sha and "why faulty" recording —
    ``health.build_document``'s own per-device shape, not a second reader.
    A row from before ``bag_sha`` existed carries ``None`` for both new keys
    (health.py already refuses to guess one), never a fabricated value. The
    `reason` and `reason_code` likewise remain health.py's, so a ledger with no
    current rows can disclose a legacy-identity or foreign-contract exclusion
    instead of being presented as an empty ledger."""
    document = health.build_document(contract_path, contract, now=now)
    return [
        {
            "id": entry["id"],
            "kind": entry["kind"],
            "state": entry["state"],
            "word": entry["word"],
            "reason": entry["reason"],
            "reason_code": entry["coverage"]["reason_code"],
            "contract_sha": entry.get("contract_sha"),
            "bag_sha": entry.get("bag_sha"),
            "recording": entry.get("recording"),
        }
        for entry in document.devices
    ]


BASELINE_DRAWER = {
    "title": "Saved baseline",
    "content": "Fits a reviewable band to a known-good recording, for later "
    "comparison against new recordings.",
    "provenance": "from a recording you provide",
    "cli_line": "lockwire baseline <recording-dir> --context <context>",
}


def page_extras(
    *,
    contract: Contract,
    contract_path: Path | None,
    preflight: tuple[list[Finding], str] | None,
    run_findings: list[Finding],
    redact_paths: bool,
    payload: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    reference = payload["faces"]["reference"]
    drawers = list(reference["drawers"])
    at = next((i for i, d in enumerate(drawers) if d["title"] == "Hardware scan"), len(drawers))
    drawers.insert(at, BASELINE_DRAWER)
    run = None
    if preflight is not None:
        run = {
            "recording": _redact_path(preflight[1]) if redact_paths else preflight[1],
            "cards": _cards(contract, run_findings, redact_paths=redact_paths),
            "stages": _stage_rows(preflight[0]),
            "violations": sum(finding.severity == "violation" for finding in run_findings),
            "silent": not preflight[0],
        }
    return {
        "modes": MODE_ORDER,
        "mode_labels": MODE_LABELS,
        "command_modes": COMMAND_MODES,
        "faces": {**payload["faces"], "reference": {**reference, "drawers": drawers}},
        "run": run,
        "health": (
            _read_health_summary(contract_path, now=now) if contract_path is not None else None
        ),
        # Per-device shas plus the "why faulty" recording, additive to `health`.
        "health_devices": (
            _read_health_devices(contract, contract_path, now=now)
            if contract_path is not None
            else None
        ),
        # The one word table (health.py's WORDS) — the page JS reads DATA.health_words.
        "health_words": {
            state: word.format(days=health.STALE_DAYS) if state == "stale" else word
            for state, word in health.WORDS.items()
        },
    }
