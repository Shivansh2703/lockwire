"""Read-only, evidence-bound correction drafts for engineer review.

Drafting is deliberately not an edit, a command, or evidence of an applied
correction.  An engineer must take any later write through the existing edit
door with its own current revision check.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lockwire.edit import Change, EditProposal, EditRefusal, propose_changes_from_bytes
from lockwire.schema import ArtifactRef, read_artifact


CorrectionCategory = Literal["physical", "model_calibration", "spec_design"]
NonSpecTarget = Literal["physical", "firmware", "configuration", "model", "calibration"]


class ReviewSpecChange(BaseModel):
    """One authored, non-applying actuator spec change at the public draft door."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    value: str
    source: str
    witness: str
    locked_at: str
    unit: str | None = None
    note: str | None = None

    def to_change(self) -> Change:
        locked_at = dt.date.fromisoformat(self.locked_at)
        if locked_at.isoformat() != self.locked_at:
            raise ValueError("proposed change locked_at must be YYYY-MM-DD")
        return Change(
            self.path,
            self.value,
            self.source,
            self.witness,
            note=self.note,
            locked_at=locked_at,
            unit=self.unit,
        )


def required_correction_category(proposal_kind: str) -> CorrectionCategory | None:
    """The correction classes supported by an actual validated proposal kind."""
    if proposal_kind == "lockwire_spec":
        return "spec_design"
    if proposal_kind == "physical":
        return "physical"
    if proposal_kind in {"model", "calibration"}:
        return "model_calibration"
    return None


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")


class ReviewDraftRefusal(ValueError):
    """A draft lacks reviewable, bound information."""


@dataclass(frozen=True)
class EvidenceCitation:
    """One hash-verified artifact bound to identified results and claims."""

    artifact: ArtifactRef
    provenance: str
    result_id: str
    claim_id: str


@dataclass(frozen=True)
class AffectedResult:
    """A result that cannot support the named current claims until rechecked."""

    result_id: str
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class NonSpecProposal:
    """A typed, non-applying patch for state outside the Lockwire spec."""

    target: NonSpecTarget
    patch: str


@dataclass(frozen=True)
class ReviewDraft:
    """A deterministic proposal only; it has no application operation."""

    base_raw_revision: str
    discrepancy: str
    evidence: tuple[EvidenceCitation, ...]
    alternatives: tuple[str, ...]
    proposed_patch: EditProposal | NonSpecProposal
    rationale: str
    correction_category: CorrectionCategory
    affected_results: tuple[AffectedResult, ...]
    required_recheck: tuple[str, ...]

    def to_json(self) -> str:
        """Stable review payload, preserving no caller-dependent ordering."""
        if isinstance(self.proposed_patch, EditProposal):
            proposed_patch = {
                "kind": "lockwire_spec",
                "patch": self.proposed_patch.result.diff,
            }
        else:
            proposed_patch = {
                "kind": self.proposed_patch.target,
                "patch": self.proposed_patch.patch,
            }
        return json.dumps(
            {
                "affected_results": [
                    {"claim_ids": item.claim_ids, "result_id": item.result_id}
                    for item in self.affected_results
                ],
                "alternatives": self.alternatives,
                "base_raw_revision": self.base_raw_revision,
                "correction_category": self.correction_category,
                "discrepancy": self.discrepancy,
                "evidence": [
                    {
                        "artifact": {"path": item.artifact.path, "sha256": item.artifact.sha256},
                        "claim_id": item.claim_id,
                        "provenance": item.provenance,
                        "result_id": item.result_id,
                    }
                    for item in self.evidence
                ],
                "proposed_patch": proposed_patch,
                "rationale": self.rationale,
                "required_recheck": self.required_recheck,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def _required(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewDraftRefusal(f"{name} is required")
    return value.strip()


def _named(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    if not values:
        raise ReviewDraftRefusal(f"{name} is required")
    result = tuple(sorted({_required(value, name=name) for value in values}))
    if not result:
        raise ReviewDraftRefusal(f"{name} is required")
    return result


def _identities(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    result = _named(values, name=name)
    if any(_IDENTITY.fullmatch(value) is None for value in result):
        raise ReviewDraftRefusal(f"{name} must contain stable identifiers")
    return result


def prepare_review_draft(
    contract_path: Path | str,
    *,
    base_raw_revision: str,
    discrepancy: str,
    evidence: tuple[EvidenceCitation, ...],
    alternatives: tuple[str, ...],
    proposed_patch: list[Change] | NonSpecProposal,
    rationale: str,
    correction_category: CorrectionCategory,
    affected_results: tuple[AffectedResult, ...],
    required_recheck: tuple[str, ...],
    evidence_root: Path | str,
) -> ReviewDraft:
    """Validate a cited correction draft without writing any product state."""
    contract = Path(contract_path)
    raw_contract = contract.read_bytes()
    if hashlib.sha256(raw_contract).hexdigest() != base_raw_revision:
        raise ReviewDraftRefusal(
            "base_raw_revision is stale or does not name the current raw contract bytes"
        )
    if correction_category not in {"physical", "model_calibration", "spec_design"}:
        raise ReviewDraftRefusal(
            "correction_category must be physical, model_calibration, or spec_design"
        )
    if not evidence:
        raise ReviewDraftRefusal("discrepancy requires cited evidence")

    results: list[AffectedResult] = []
    for affected in affected_results:
        if not isinstance(affected, AffectedResult):
            raise ReviewDraftRefusal("affected_results must contain AffectedResult values")
        results.append(
            AffectedResult(
                result_id=_identities((affected.result_id,), name="result_id")[0],
                claim_ids=_identities(affected.claim_ids, name="claim_ids"),
            )
        )
    ordered_results = tuple(sorted(results, key=lambda item: item.result_id))
    if not ordered_results:
        raise ReviewDraftRefusal("affected_results is required")
    if len({item.result_id for item in ordered_results}) != len(ordered_results):
        raise ReviewDraftRefusal("affected result_ids must be unique")
    claims_by_result = {item.result_id: set(item.claim_ids) for item in ordered_results}

    citations: list[EvidenceCitation] = []
    for citation in evidence:
        if not isinstance(citation, EvidenceCitation):
            raise ReviewDraftRefusal("evidence must contain EvidenceCitation values")
        provenance = _required(citation.provenance, name="evidence provenance")
        result_id = _identities((citation.result_id,), name="evidence result_id")[0]
        claim_id = _identities((citation.claim_id,), name="evidence claim_id")[0]
        if result_id not in claims_by_result:
            raise ReviewDraftRefusal("evidence result_id must name an affected result")
        if claim_id not in claims_by_result[result_id]:
            raise ReviewDraftRefusal("evidence claim_id must name a claim of its affected result")
        try:
            read_artifact(citation.artifact, Path(evidence_root))
        except (OSError, ValueError) as error:
            raise ReviewDraftRefusal(
                f"evidence {citation.artifact.path!r} is not admissible: {error}"
            ) from error
        citations.append(EvidenceCitation(citation.artifact, provenance, result_id, claim_id))

    ordered_evidence = tuple(
        sorted(
            citations,
            key=lambda item: (
                item.artifact.path,
                item.artifact.sha256,
                item.provenance,
                item.result_id,
                item.claim_id,
            ),
        )
    )
    citation_keys = {
        (
            item.artifact.path,
            item.artifact.sha256,
            item.provenance,
            item.result_id,
            item.claim_id,
        )
        for item in ordered_evidence
    }
    if len(citation_keys) != len(ordered_evidence):
        raise ReviewDraftRefusal("evidence citations must be unique")

    if isinstance(proposed_patch, list):
        if correction_category != required_correction_category("lockwire_spec"):
            raise ReviewDraftRefusal(
                "a Lockwire spec proposal requires correction_category spec_design"
            )
        if any(not isinstance(change, Change) for change in proposed_patch):
            raise ReviewDraftRefusal("Lockwire spec proposal must contain Change values")
        if any(change.locked_at is None for change in proposed_patch):
            raise ReviewDraftRefusal(
                "Lockwire spec proposal changes require locked_at for deterministic review"
            )
        try:
            proposal: EditProposal | NonSpecProposal = propose_changes_from_bytes(
                contract, raw_contract, proposed_patch
            )
        except EditRefusal as error:
            raise ReviewDraftRefusal(f"Lockwire spec proposal is not valid: {error}") from error
    elif isinstance(proposed_patch, NonSpecProposal):
        if proposed_patch.target not in {
            "physical",
            "firmware",
            "configuration",
            "model",
            "calibration",
        }:
            raise ReviewDraftRefusal("non-spec proposal target is not supported")
        category_for_target = required_correction_category(proposed_patch.target)
        if category_for_target is None:
            raise ReviewDraftRefusal(
                f"{proposed_patch.target} proposal has no supported correction_category binding"
            )
        if correction_category != category_for_target:
            raise ReviewDraftRefusal(
                f"{proposed_patch.target} proposal requires correction_category {category_for_target}"
            )
        proposal = NonSpecProposal(
            proposed_patch.target,
            _required(proposed_patch.patch, name="proposed_patch"),
        )
    else:
        raise ReviewDraftRefusal("proposed_patch must be a list of Change or NonSpecProposal")
    return ReviewDraft(
        base_raw_revision=base_raw_revision,
        discrepancy=_required(discrepancy, name="discrepancy"),
        evidence=ordered_evidence,
        alternatives=_named(alternatives, name="alternatives"),
        proposed_patch=proposal,
        rationale=_required(rationale, name="rationale"),
        correction_category=correction_category,
        affected_results=ordered_results,
        required_recheck=_named(required_recheck, name="required_recheck"),
    )
