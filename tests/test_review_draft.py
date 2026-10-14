"""Read-only correction-draft core: review content never applies a change."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from lockwire import edit, review_draft
from lockwire.edit import Change, contract_revision
from lockwire.review_draft import (
    AffectedResult,
    EvidenceCitation,
    NonSpecProposal,
    ReviewDraftRefusal,
    prepare_review_draft,
)
from lockwire.schema import ArtifactRef


FIXTURES = Path(__file__).parent / "fixtures"


def _inventory(directory: Path) -> dict[str, bytes]:
    return {
        str(child.relative_to(directory)): child.read_bytes()
        for child in directory.rglob("*")
        if child.is_file()
    }


def _inputs(tmp_path: Path) -> tuple[Path, Path, EvidenceCitation]:
    contract = tmp_path / "contract.yaml"
    contract.write_bytes((FIXTURES / "contract_clean.yaml").read_bytes())
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    payload = b"measured discrepancy"
    artifact = evidence_root / "capture.txt"
    artifact.write_bytes(payload)
    return (
        contract,
        evidence_root,
        EvidenceCitation(
            ArtifactRef(path="capture.txt", sha256=hashlib.sha256(payload).hexdigest()),
            "bench capture, 2026-09-14",
            "result.actuator-case.elbow.v1",
            "claim.actuator-torque-envelope",
        ),
    )


def _spec_changes():
    return [
        Change(
            "model/joints/imu_mount/limits/effort",
            3.0,
            "datasheet.pdf",
            "reviewer",
            locked_at=dt.date(2026, 9, 14),
        )
    ]


def _draft(contract: Path, root: Path, citation: EvidenceCitation, **changes):
    if "proposed_patch" not in changes:
        changes["proposed_patch"] = _spec_changes()
    values = {
        "base_raw_revision": contract_revision(contract),
        "discrepancy": "joint torque differs from declared target",
        "evidence": (citation,),
        "alternatives": ("inspect fixture", "revise sourced spec"),
        "rationale": "the cited datasheet corrects the declared sourced limit",
        "correction_category": "spec_design",
        "affected_results": (
            AffectedResult("result.actuator-case.elbow.v1", ("claim.actuator-torque-envelope",)),
        ),
        "required_recheck": ("rerun actuator case",),
        "evidence_root": root,
    }
    values.update(changes)
    return prepare_review_draft(contract, **values)


def test_review_draft_is_hash_bound_deterministic_and_has_no_application_side_effect(
    tmp_path: Path,
):
    contract, root, citation = _inputs(tmp_path)
    before = _inventory(tmp_path)

    draft = _draft(contract, root, citation)

    assert draft.base_raw_revision == contract_revision(contract)
    payload = json.loads(draft.to_json())
    assert payload["proposed_patch"]["kind"] == "lockwire_spec"
    assert payload["rationale"] == "the cited datasheet corrects the declared sourced limit"
    assert payload["evidence"][0]["result_id"] == "result.actuator-case.elbow.v1"
    assert payload["evidence"][0]["claim_id"] == "claim.actuator-torque-envelope"
    assert payload["affected_results"] == [
        {
            "claim_ids": ["claim.actuator-torque-envelope"],
            "result_id": "result.actuator-case.elbow.v1",
        }
    ]
    assert draft.to_json() == _draft(contract, root, citation).to_json()
    assert (
        draft.to_json()
        == _draft(
            contract, root, citation, alternatives=("revise sourced spec", "inspect fixture")
        ).to_json()
    )
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"base_raw_revision": "0" * 64}, "base_raw_revision"),
        ({"discrepancy": ""}, "discrepancy"),
        ({"evidence": ()}, "cited evidence"),
        ({"alternatives": ()}, "alternatives"),
        ({"proposed_patch": []}, "Lockwire spec proposal"),
        (
            {
                "proposed_patch": NonSpecProposal("physical", ""),
                "correction_category": "physical",
            },
            "proposed_patch",
        ),
        ({"proposed_patch": "untyped patch"}, "proposed_patch"),
        ({"rationale": ""}, "rationale"),
        ({"correction_category": "other"}, "correction_category"),
        ({"affected_results": ()}, "affected_results"),
        ({"required_recheck": ()}, "required_recheck"),
    ],
)
def test_review_draft_refuses_incomplete_review_content(tmp_path: Path, changes, message):
    contract, root, citation = _inputs(tmp_path)
    with pytest.raises(ReviewDraftRefusal, match=message):
        _draft(contract, root, citation, **changes)


def test_review_draft_refuses_missing_evidence(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    bad = EvidenceCitation(
        ArtifactRef(path="missing.txt", sha256=citation.artifact.sha256),
        citation.provenance,
        citation.result_id,
        citation.claim_id,
    )
    with pytest.raises(ReviewDraftRefusal, match="not admissible"):
        _draft(contract, root, bad)


def test_review_draft_refuses_traversing_evidence_even_if_caller_bypasses_model_validation(
    tmp_path: Path,
):
    contract, root, citation = _inputs(tmp_path)
    bad = EvidenceCitation(
        ArtifactRef.model_construct(path="../escape.txt", sha256=citation.artifact.sha256),
        citation.provenance,
        citation.result_id,
        citation.claim_id,
    )
    with pytest.raises(ReviewDraftRefusal, match="not admissible"):
        _draft(contract, root, bad)


def test_review_draft_refuses_hash_mismatched_evidence(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    bad = EvidenceCitation(
        ArtifactRef(path=citation.artifact.path, sha256="0" * 64),
        citation.provenance,
        citation.result_id,
        citation.claim_id,
    )
    with pytest.raises(ReviewDraftRefusal, match="sha256 does not match"):
        _draft(contract, root, bad)


@pytest.mark.parametrize(
    "citation",
    [
        EvidenceCitation(
            ArtifactRef(path="capture.txt", sha256="0" * 64),
            "capture",
            "result.unknown",
            "claim.actuator-torque-envelope",
        ),
        EvidenceCitation(
            ArtifactRef(path="capture.txt", sha256="0" * 64),
            "capture",
            "result.actuator-case.elbow.v1",
            "claim.unknown",
        ),
    ],
)
def test_review_draft_refuses_evidence_not_bound_to_affected_result_and_claim(
    tmp_path: Path, citation: EvidenceCitation
):
    contract, root, valid = _inputs(tmp_path)
    citation = EvidenceCitation(
        ArtifactRef(path=citation.artifact.path, sha256=valid.artifact.sha256),
        citation.provenance,
        citation.result_id,
        citation.claim_id,
    )
    with pytest.raises(ReviewDraftRefusal, match="evidence (result_id|claim_id)"):
        _draft(contract, root, citation)


def test_review_draft_refuses_unstable_or_duplicate_result_identity(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    with pytest.raises(ReviewDraftRefusal, match="stable identifiers"):
        _draft(contract, root, citation, affected_results=(AffectedResult("not stable", ("c",)),))
    repeated = AffectedResult("result.actuator-case.elbow.v1", ("claim.actuator-torque-envelope",))
    with pytest.raises(ReviewDraftRefusal, match="unique"):
        _draft(contract, root, citation, affected_results=(repeated, repeated))


@pytest.mark.parametrize(
    ("target", "category"),
    [
        ("physical", "physical"),
        ("model", "model_calibration"),
        ("calibration", "model_calibration"),
    ],
)
def test_review_draft_accepts_typed_nonapplying_proposal(
    tmp_path: Path, target: str, category: str
):
    contract, root, citation = _inputs(tmp_path)
    before = _inventory(tmp_path)

    payload = json.loads(
        _draft(
            contract,
            root,
            citation,
            proposed_patch=NonSpecProposal(target, "set elbow_current_limit = 2.0"),
            correction_category=category,
        ).to_json()
    )

    assert payload["proposed_patch"] == {
        "kind": target,
        "patch": "set elbow_current_limit = 2.0",
    }
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize(
    ("target", "category", "reason"),
    [
        ("model", "physical", "requires correction_category model_calibration"),
        ("calibration", "spec_design", "requires correction_category model_calibration"),
        ("physical", "model_calibration", "requires correction_category physical"),
        ("firmware", "spec_design", "no supported correction_category binding"),
        ("configuration", "spec_design", "no supported correction_category binding"),
    ],
)
def test_review_draft_refuses_unbound_correction_classification(
    tmp_path: Path, target: str, category: str, reason: str
):
    contract, root, citation = _inputs(tmp_path)
    before = _inventory(tmp_path)
    with pytest.raises(ReviewDraftRefusal, match=reason):
        _draft(
            contract,
            root,
            citation,
            proposed_patch=NonSpecProposal(target, "review proposed change"),
            correction_category=category,
        )
    assert _inventory(tmp_path) == before


def test_review_draft_refuses_spec_change_misclassified_as_physical(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    with pytest.raises(ReviewDraftRefusal, match="requires correction_category spec_design"):
        _draft(contract, root, citation, correction_category="physical")


def test_review_draft_requires_deterministic_lockwire_change_date(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    undated = Change("model/joints/imu_mount/limits/effort", 3.0, "datasheet.pdf", "reviewer")

    with pytest.raises(ReviewDraftRefusal, match="locked_at"):
        _draft(contract, root, citation, proposed_patch=[undated])


def test_spec_proposal_uses_the_exact_revision_bytes_after_an_intervening_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract, root, citation = _inputs(tmp_path)
    original = contract.read_bytes()
    changed = b"# intervening bytes\n" + original
    read_artifact = review_draft.read_artifact

    def write_during_evidence_admission(*args, **kwargs):
        payload = read_artifact(*args, **kwargs)
        contract.write_bytes(changed)
        return payload

    monkeypatch.setattr(review_draft, "read_artifact", write_during_evidence_admission)
    draft = _draft(contract, root, citation)

    assert contract.read_bytes() == changed
    assert draft.proposed_patch.original_text.encode() == original


def test_spec_proposal_uses_the_exact_revision_bytes_through_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract, root, citation = _inputs(tmp_path)
    original = contract.read_bytes()
    read_artifact = review_draft.read_artifact
    load_bytes = edit._load_doc_bytes_guarded

    def write_during_evidence_admission(*args, **kwargs):
        payload = read_artifact(*args, **kwargs)
        contract.write_bytes(b"# transient bytes\n" + original)
        return payload

    def restore_then_load(raw_bytes: bytes):
        contract.write_bytes(original)
        return load_bytes(raw_bytes)

    monkeypatch.setattr(review_draft, "read_artifact", write_during_evidence_admission)
    monkeypatch.setattr(edit, "_load_doc_bytes_guarded", restore_then_load)
    draft = _draft(contract, root, citation)

    assert contract.read_bytes() == original
    assert draft.proposed_patch.original_text.encode() == original


def test_spec_proposal_preserves_crlf_and_non_ascii_revision_bytes(tmp_path: Path):
    contract, root, citation = _inputs(tmp_path)
    raw_contract = b"# caf\xc3\xa9\r\n" + contract.read_bytes().replace(b"\n", b"\r\n")
    contract.write_bytes(raw_contract)

    draft = _draft(contract, root, citation)

    assert draft.base_raw_revision == hashlib.sha256(raw_contract).hexdigest()
    assert draft.proposed_patch.original_text.encode() == raw_contract
