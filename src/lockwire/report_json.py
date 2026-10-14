"""The machine-readable preflight document.

`lockwire preflight --json <path>` writes one JSON file describing the same run
the terminal just rendered. It exists because an out-of-process consumer — the
`lockwire_ros` gate node is the first — must read a verdict as *data*, and the
alternative is scraping rich-rendered prose. A gate whose consumer parses
colour codes is a gate that breaks silently the first time a message is reworded.

Three properties this file is responsible for, each load-bearing:

- **The verdict is not recomputed here.** ``report.verdict`` decides GO/NO-GO
  once; this module reports what it decided. A second implementation of the
  exit map would eventually disagree with the terminal, and a gate that
  disagrees with itself is worse than no gate.
- **`not_scored` findings are carried, not filtered.** They are the half the
  product exists to say out loud (`docs/gate_coverage.md`). A document that
  emitted only violations would let a consumer render a clean green screen over
  an admitted coverage hole.
- **A vacuous pass is visibly distinct.** ``NOT_SCORED`` (exit 3) and ``scored``
  travel together so no consumer can read "nothing was checked" as clearance.

Writing this file never changes the verdict — like ``--html``, it is an
additional output of a run that already happened.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lockwire.report import Finding, RecordReport, limit_raised_mark, verdict, verdict_word
from lockwire.schema import SCHEMA_VERSION, Contract

ARTIFACT_TYPE = "lockwire-preflight-report"
HEALTH_ARTIFACT_TYPE = "lockwire-health-report"
CHECK_ARTIFACT_TYPE = "lockwire-check-report"
PLAN_ARTIFACT_TYPE = "lockwire-plan-report"
BASELINE_CHECK_ARTIFACT_TYPE = "lockwire-baseline-check-report"
#: Bumped only when the document's shape changes in a way a consumer must notice.
#: Independent of the lockfile ``SCHEMA_VERSION``, which is also reported below.
#: "2": ``verdict`` now spells NO-GO hyphenated, byte-identical to the
#: terminal, instead of the raw ``NO_GO`` Python identifier — a breaking change
#: for any consumer comparing the raw string.
FORMAT_VERSION = "2"


#: The keys ``envelope`` owns. A body may not use them — see the guard below.
HEADER_KEYS = ("artifact_type", "format_version", "schema_version")


def envelope(artifact_type: str, body: dict[str, Any]) -> dict[str, Any]:
    """Lead any machine-readable document with the same three header fields.

    ``artifact_type`` says which document this is, and is the only key a
    consumer needs to read before it knows how to read the rest; the two
    versions say what may have changed under it — ``FORMAT_VERSION`` the
    envelope and body shape, ``SCHEMA_VERSION`` the lockfile the run used.
    One helper, so a second machine door cannot be added unversioned: the
    body is merged flat behind the header rather than nested, because
    ``preflight``'s document is already flat and a consumer already reads it.

    The flat merge is what makes a colliding body key dangerous: a body with
    its own ``format_version`` would silently take the header's place (or be
    taken by it, depending which side won), and the consumers that read the
    header would be reading a body field without knowing. So a collision is
    refused loudly at the one point every document passes through, rather than
    left for whoever notices the wrong number months later.
    """
    clash = sorted(set(body) & set(HEADER_KEYS))
    if clash:
        raise ValueError(
            f"{artifact_type}: body key(s) {clash} collide with the envelope header; "
            f"the header owns {list(HEADER_KEYS)} and the body is merged flat behind it"
        )
    return {
        "artifact_type": artifact_type,
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        **body,
    }


def preflight_document(
    contract: Contract,
    findings: list[Finding],
    recording: str,
) -> dict[str, Any]:
    """Build the JSON-ready document for one preflight run.

    ``findings`` is whatever ``run_preflight`` returned; when that is a
    ``PreflightReport`` the per-stage scored counts ride along, and when it is a
    plain list they are simply absent rather than invented.
    """
    decision = verdict(findings)
    scored_by_stage = getattr(findings, "scored_by_stage", None)
    document = envelope(
        ARTIFACT_TYPE,
        {
            "robot": contract.robot.name,
            "source": recording,
            # Hyphenated: byte-identical to the terminal's spelling, not
            # the raw underscored VerdictCode.
            "verdict": verdict_word(decision.code),
            "exit_code": decision.exit_code,
            "counts": {
                "violation": decision.violations,
                "warning": decision.warnings,
                "not_scored": decision.not_scored,
            },
            "scored": decision.scored,
            "profile": getattr(findings, "profile", "analysis"),
            "required_evidence": getattr(findings, "required_evidence", []),
            "missing_required_evidence": getattr(findings, "missing_required_evidence", []),
            # every limit raised to clear a violation, as data. A cleared
            # budget emits no finding, so without this key the one edit that can make
            # a robot look safe by moving the goalposts is invisible to a consumer.
            # An annotation only: the verdict and exit code above are untouched.
            # rendered regardless of the budget's current severity — the
            # mark must survive a later re-breach, not vanish exactly when a raised
            # limit turns out not to have held (see report.limit_raised_mark).
            "limits_raised": [
                {"budget": budget.id, "mark": mark}
                for budget in contract.budgets
                if (mark := limit_raised_mark(budget.provenance)) is not None
            ],
            "findings": [finding.model_dump(mode="json") for finding in findings],
        },
    )
    if scored_by_stage is not None:
        # JSON object keys are strings; stage identity stays an integer in Python.
        document["scored_by_stage"] = {
            str(stage): n for stage, n in sorted(scored_by_stage.items())
        }
    return document


def baseline_check_document(report: RecordReport, recording: str) -> dict[str, Any]:
    """Build the JSON document for the baseline-check report already rendered."""
    decision = verdict(report)
    return envelope(
        BASELINE_CHECK_ARTIFACT_TYPE,
        {
            "source": recording,
            "verdict": verdict_word(decision.code),
            "exit_code": decision.exit_code,
            "counts": {
                "violation": decision.violations,
                "warning": decision.warnings,
                "not_scored": decision.not_scored,
            },
            "scored": decision.scored,
            "findings": [finding.model_dump(mode="json") for finding in report],
        },
    )


def write_preflight_json(
    contract: Contract,
    findings: list[Finding],
    recording: str,
    out: Path,
) -> None:
    """Write one preflight run as JSON, replacing ``out``, re-running nothing."""
    document = preflight_document(contract, findings, recording)
    # sort_keys is off on purpose: the top-level order above reads verdict-first,
    # which is what a human debugging a node's input wants to see.
    out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
