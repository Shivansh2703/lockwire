"""The plan document's typed shape, free: what ``lockwire plan --json`` writes.

``api_payload`` (the app's generated-types source) imports these names from here,
so the open-source ``api.plan`` and the full product emit one document. Classes
moved verbatim: the generated schema and ``api.gen.ts`` depend on their names
and docstrings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from lockwire.plan import AssertionOutcome
from lockwire.report import Finding


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Document(_Strict):
    """``report_json.envelope``'s three header fields, plus the one field the
    MCP transport adds in place (``mcp_server._with_cli_line``). Every machine
    member leads with these; ``artifact_type`` is the discriminator
    already in place, so the union does not invent a second one."""

    format_version: str
    schema_version: str
    cli_line: str | None = None


class Counts(_Strict):
    violation: int
    warning: int
    not_scored: int


#: ``report.verdict_word``'s spelling, not the raw ``VerdictCode``.
VerdictWord = Literal["REFUSED", "NO-GO", "GO_WITH_NOTES", "GO", "NOT_SCORED"]


class _VerdictDocument(_Document):
    """``api.verdict_document``, field for field — the one function behind
    ``check`` and ``plan``, so one model behind both members."""

    robot: str
    verdict: VerdictWord
    exit_code: int
    counts: Counts
    scored: int | None
    findings: list[Finding]


class Instead(_Strict):
    """One short "Instead" row: verb + number + the kind word (copy.instead_row)."""

    text: str
    kind: str


class FindingLabel(_Strict):
    """One finding's human name and typed targets, index-aligned with ``findings``."""

    label: str
    targets: list["Target"]
    #: True only for a robot-wide budget (no rail): the whole robot, not "no part found".
    whole: bool = False
    instead: list[Instead] = []
    fix: str = ""


class PlanPayload(_VerdictDocument):
    """``api.plan`` / ``lockwire plan --json``. ``outcomes`` rides along only
    when the report carries them (``plan.PlanReport``), exactly as
    ``api.verdict_document`` attaches them. ``steps`` is joined from the
    report's own outcomes and findings before the document is emitted."""

    artifact_type: Literal["lockwire-plan-report"]
    outcomes: list[AssertionOutcome] | None = None
    steps: list[PlanStepPayload] = []
    finding_labels: list[FindingLabel] = []

    @model_validator(mode="after")
    def _join_steps(self) -> "PlanPayload":
        if self.outcomes:
            self.steps = plan_steps(self.outcomes, self.findings)
        return self


class Target(_Strict):
    """What a card is about, typed by the engine so the app never parses text."""

    kind: Literal["joint", "link", "device", "rail"]
    id: str
    #: Short display name (the id minus its machine prefix); the id stays the tooltip.
    label: str
    #: Scene part ids (contract ids) this target lights in the 3D view: a device or link is its
    #: own part; a rail is the devices drawing from it (and downstream rails). Engine-resolved.
    parts: list[str] = []


class PlanStepPayload(_Strict):
    """One plan step's disposition (the app shows each agent call
    and lockwire's reply as a read-only timeline, from the command record; the
    operator sees what the agent was told, and nothing in the app
    steers the agent), joined from ``PlanReport``'s own ``outcomes`` and
    ``findings`` — ``plan.py`` already computes both; this only correlates
    what it returns (no second scorer). The join key is identity:
    ``AssertionOutcome.path == Finding.quantity`` — every ``UNSCORED`` outcome
    ``validate_plan`` emits carries exactly one ``not_scored`` Finding at the
    same path, whose ``action`` names the missing fact.
    """

    path: str
    status: Literal["PASS", "FAIL", "UNSCORED", "ERROR"]
    #: Never empty for an UNSCORED step (a coverage gap is named, not
    #: silent). None for a scored step — there is nothing missing to name.
    not_scored_reason: str | None
    #: What the step touches, typed by the engine (served `/v1/run` plan only; [] on the CLI document).
    targets: list["Target"] = []


def plan_steps(outcomes: list[AssertionOutcome], findings: list[Finding]) -> list[PlanStepPayload]:
    """Correlate ``validate_plan``'s outcomes with its own findings by path.

    Reused by ``PlanPayload`` (the CLI/MCP/library document) rather than
    reimplemented — one join, one place.
    """
    reasons = {f.quantity: f.action for f in findings if f.severity == "not_scored"}
    steps = []
    for outcome in outcomes:
        reason = reasons.get(outcome.path) if outcome.status == "UNSCORED" else None
        if outcome.status == "UNSCORED" and reason is None:
            msg = (
                f"join miss: UNSCORED outcome at {outcome.path} has no matching not_scored finding"
            )
            raise ValueError(msg)
        steps.append(
            PlanStepPayload(
                path=outcome.path,
                status=outcome.status,
                not_scored_reason=reason,
            )
        )
    return steps


FindingLabel.model_rebuild()
PlanPayload.model_rebuild()
