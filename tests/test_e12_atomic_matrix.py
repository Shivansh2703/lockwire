"""E1.2 replacement boundary proof: conflicts and failed replacements keep bytes intact."""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import inspect
from pathlib import Path

import pytest

from lockwire import edit, review_draft
from lockwire_pro import analysis
from lockwire.edit import Change, EditConflict, contract_revision
from lockwire.review_draft import AffectedResult, EvidenceCitation, prepare_review_draft
from lockwire.schema import ArtifactRef


FIXTURES = Path(__file__).parent / "fixtures"


def _copy_contract(tmp_path: Path) -> Path:
    contract = tmp_path / "contract.yaml"
    contract.write_bytes((FIXTURES / "x500" / "contract.yaml").read_bytes())
    return contract


def test_edit_conflict_and_failed_replacements_preserve_contract(tmp_path, monkeypatch):
    contract = _copy_contract(tmp_path)
    opened = contract_revision(contract)
    edit.apply_changes(
        contract,
        [Change("topology/rails/rail.pm02-5v/capacity", "20 W", "spec.pdf", "Pat")],
        expected_revision=opened,
    )
    baseline = contract.read_bytes()

    with pytest.raises(EditConflict, match="reload"):
        edit.apply_changes(
            contract,
            [Change("topology/rails/rail.pm02-5v/capacity", "21 W", "spec.pdf", "Pat")],
            expected_revision=opened,
        )
    assert contract.read_bytes() == baseline

    real_replace = edit.os.replace
    with monkeypatch.context() as patched:

        def fail_contract(source, destination):
            if Path(destination) == contract:
                raise OSError("contract replace failed")
            return real_replace(source, destination)

        patched.setattr(edit.os, "replace", fail_contract)
        with pytest.raises(OSError, match="contract replace failed"):
            edit.apply_changes(
                contract,
                [Change("topology/rails/rail.pm02-5v/capacity", "21 W", "spec.pdf", "Pat")],
                expected_revision=contract_revision(contract),
            )
    assert contract.read_bytes() == baseline


def test_analysis_sidecar_refusal_and_failed_replace_keep_prior_bytes(tmp_path, monkeypatch):
    contract = tmp_path / "contract.yaml"
    contract.write_text("robot: {name: fixture}\n", encoding="utf-8")
    sidecar = tmp_path / "proposal.json"
    baseline = b'{"status":"PRIOR"}\n'
    sidecar.write_bytes(baseline)
    argv = [
        "--contract",
        str(contract),
        "--expected-revision",
        "0" * 64,
        "--procedure",
        "static",
        "--evidence-root",
        str(tmp_path),
        "--package-manifest",
        str(tmp_path / "package.json"),
        "--document",
        str(sidecar),
    ]

    assert analysis.main(argv) == 2
    assert sidecar.read_bytes() == baseline

    monkeypatch.setattr(
        analysis,
        "prepare_analysis",
        lambda *_args, **_kwargs: {"status": "PREPARED", "deck_template": None},
    )
    monkeypatch.setattr(
        analysis.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed"))
    )
    with pytest.raises(OSError, match="replace failed"):
        analysis.main(argv)
    assert sidecar.read_bytes() == baseline


def test_review_draft_is_in_memory_and_has_no_apply_flash_or_configure_call(tmp_path):
    contract = _copy_contract(tmp_path)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    payload = b"measured discrepancy"
    (evidence_root / "capture.txt").write_bytes(payload)
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    draft = prepare_review_draft(
        contract,
        base_raw_revision=contract_revision(contract),
        discrepancy="joint torque differs from declared target",
        evidence=(
            EvidenceCitation(
                ArtifactRef(path="capture.txt", sha256=hashlib.sha256(payload).hexdigest()),
                "bench capture",
                "result.actuator-case.elbow.v1",
                "claim.actuator-torque-envelope",
            ),
        ),
        alternatives=("inspect fixture",),
        proposed_patch=[
            Change(
                "topology/rails/rail.pm02-5v/capacity",
                "20 W",
                "datasheet.pdf",
                "reviewer",
                locked_at=dt.date(2026, 9, 14),
            )
        ],
        rationale="the cited datasheet corrects the declared sourced limit",
        correction_category="spec_design",
        affected_results=(
            AffectedResult("result.actuator-case.elbow.v1", ("claim.actuator-torque-envelope",)),
        ),
        required_recheck=("rerun actuator case",),
        evidence_root=evidence_root,
    )

    assert draft.base_raw_revision == contract_revision(contract)
    after = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(review_draft.prepare_review_draft)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"apply_changes", "flash", "configure"}.isdisjoint(calls)

    # Drafting itself never writes (asserted above) — but an explicit
    # edit.apply_changes of the same patch, through the existing edit door,
    # DOES write.
    result = edit.apply_changes(
        contract,
        [
            Change(
                "topology/rails/rail.pm02-5v/capacity",
                "20 W",
                "datasheet.pdf",
                "reviewer",
                locked_at=dt.date(2026, 9, 14),
            )
        ],
        expected_revision=draft.base_raw_revision,
    )
    assert result.written is True
    assert contract.read_bytes() != before["contract.yaml"]


def test_add_entry_stale_revision_conflict_and_failed_replace_preserve_bytes(tmp_path, monkeypatch):
    contract = _copy_contract(tmp_path)
    original = contract.read_bytes()
    opened = contract_revision(contract)

    with pytest.raises(EditConflict, match="reload"):
        edit.add_entry(
            contract,
            "rail",
            {"id": "rail.e42-test-5v", "voltage": "5 V", "capacity": "10 W"},
            source="new PDB",
            witness="w1",
            expected_revision="0" * 64,
        )
    assert contract.read_bytes() == original

    result = edit.add_entry(
        contract,
        "rail",
        {"id": "rail.e42-test-5v", "voltage": "5 V", "capacity": "10 W"},
        source="new PDB",
        witness="w1",
        expected_revision=opened,
    )
    assert result.written
    baseline = contract.read_bytes()

    real_replace = edit.os.replace
    with monkeypatch.context() as patched:

        def fail_contract(source, destination):
            if Path(destination) == contract:
                raise OSError("contract replace failed")
            return real_replace(source, destination)

        patched.setattr(edit.os, "replace", fail_contract)
        with pytest.raises(OSError, match="contract replace failed"):
            edit.add_entry(
                contract,
                "rail",
                {"id": "rail.e42-test-5v-b", "voltage": "5 V", "capacity": "10 W"},
                source="new PDB",
                witness="w1",
                expected_revision=contract_revision(contract),
            )
    assert contract.read_bytes() == baseline
