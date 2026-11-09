"""Preflight run report: the profile/evidence policy and ``PreflightReport``."""

from __future__ import annotations

from typing import Literal

from pydantic import TypeAdapter

from lockwire.report import Finding, RecordReport, ScoredRecord


PreflightProfile = Literal["analysis", "powered-release"]
EvidenceClass = Literal[
    "watched-command-stream",
    "live-hardware-software-reconciliation",
    "comm-links-under-representative-load",
]


def preflight_policy(
    profile: PreflightProfile,
    required_evidence: list[EvidenceClass] | None = None,
) -> tuple[PreflightProfile, list[EvidenceClass]]:
    """Validate explicit release requirements at every entry point."""
    profile = TypeAdapter(PreflightProfile).validate_python(profile)
    required = TypeAdapter(list[EvidenceClass]).validate_python(
        required_evidence if required_evidence is not None else [],
    )
    if profile == "powered-release" and not required:
        raise ValueError("powered-release needs a nonempty required_evidence list")
    return profile, list(dict.fromkeys(required))


class PreflightReport(RecordReport):
    """Preflight findings plus scored-check totals for the whole run and each stage."""

    scored_by_stage: dict[int, int]

    def __init__(
        self,
        findings: list[Finding] | None = None,
        *,
        scored_by_stage: dict[int, int] | None = None,
        scored_records: list[ScoredRecord] | None = None,
        profile: PreflightProfile = "analysis",
        required_evidence: list[EvidenceClass] | None = None,
        missing_required_evidence: list[EvidenceClass] | None = None,
    ) -> None:
        stage_counts = dict(sorted((scored_by_stage or {}).items()))
        super().__init__(findings, scored=sum(stage_counts.values()), scored_records=scored_records)
        self.scored_by_stage = stage_counts
        self.profile, self.required_evidence = preflight_policy(profile, required_evidence)
        self.missing_required_evidence = list(missing_required_evidence or [])
