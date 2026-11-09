"""The v0.4 actuator-case result crosses every Check door without re-evaluation."""

import asyncio
import copy
import datetime as dt
import hashlib
import json
import shlex

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from lockwire_pro import api_pro as api
from lockwire_pro.actuator_case import ClockMapping
from lockwire_pro.api_payload_pro import CheckPayloadPro as CheckPayload
from lockwire.cli import app
from lockwire.edit import Change
from lockwire.mcp_server import _with_cli_line, build_server
from lockwire.report import Finding, RecordReport, ScoredRecord, verdict
from lockwire.schema import ArtifactRef, read_artifact
from lockwire.serve import CheckCommand, _handle_check

from test_actuator_case import _admitted_evidence, _contract, _evidence


def _path_backed_case(tmp_path):
    contract = _contract()
    evidence = _evidence()
    evidence_ref, evidence_root = _admitted_evidence(tmp_path, contract, evidence)

    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text(
        json.dumps(contract.model_dump(mode="json"), separators=(",", ":")),
        encoding="utf-8",
    )
    revision = hashlib.sha256(contract_path.read_bytes()).hexdigest()

    assert evidence.prediction is not None
    evidence.prediction.contract_sha256 = revision
    payload = evidence.model_dump_json().encode()
    (evidence_root / evidence_ref.path).write_bytes(payload)
    evidence_ref.sha256 = hashlib.sha256(payload).hexdigest()
    return contract, contract_path, evidence_ref, evidence_root, revision


def _selection(evidence_ref: ArtifactRef) -> dict[str, ArtifactRef]:
    return {"elbow.nominal": evidence_ref}


def test_claim_context_binds_each_lane_without_borrowing_the_other_lane(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    physical = case["conformance"]["context"]
    simulation = case["simulation_validity"]["context"]
    for claim_id, context in (("conformance", physical), ("simulation_validity", simulation)):
        assert (context["case_id"], context["claim_id"], context["contract_revision"]) == (
            "elbow.nominal",
            claim_id,
            revision,
        )
        assert context["selected_evidence"] == ref.model_dump(mode="json")
        assert context["freshness"] == "bound_to_revision"
        assert context["dependencies"]
        assert context["uncertainty"] == "unknown"
    assert physical["evidence_class"] == "physical_measurement"
    assert physical["source_witnesses"] == [
        {"source": "measurement-log/run-1", "witness": "bench operator"},
    ]
    assert physical["validation_reference"] is None
    assert physical["calibration_artifacts"] == []
    assert simulation["evidence_class"] == "prediction_with_held_out_reference"
    assert simulation["source_witnesses"] == []
    assert simulation["validation_reference"] is not None
    assert simulation["reference_held_out"] is True
    assert simulation["calibration_artifacts"]
    assert simulation["validation_reference"] in simulation["dependencies"]
    assert simulation["validation_reference"] not in physical["dependencies"]
    CheckPayload.model_validate(document)

    missing = api.check(path)["actuator_cases"][0]
    for claim_id in ("conformance", "simulation_validity"):
        context = missing[claim_id]["context"]
        assert context["freshness"] == context["evidence_class"] == "unverified"
        assert context["selected_evidence"] is None
        assert context["dependencies"] == []


def test_review_draft_refuses_missing_claim_identity_binding(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    document = api.actuator_review_draft(
        path,
        "elbow.nominal",
        evidence=ref,
        evidence_root=root,
        expected_revision=revision,
        provenance="bench operator",
        discrepancy="tracking mismatch",
        alternatives=("repeat capture",),
        proposed_target="physical",
        proposed_patch="inspect linkage",
        rationale="selected capture binds the finding",
        correction_category="physical",
        required_recheck=("repeat elbow nominal capture",),
    )
    CheckPayload.model_validate(document)
    for claim_id in ("conformance", "simulation_validity"):
        binding = document["actuator_cases"][0][claim_id]["context"]["draft_binding"]
        assert binding == {
            "base_raw_revision": revision,
            "result_id": "elbow.nominal",
            "claim_id": claim_id,
            "evidence": [{"artifact": ref.model_dump(mode="json"), "provenance": "bench operator"}],
        }
    document["actuator_cases"][0]["simulation_validity"]["context"]["draft_binding"] = None
    with pytest.raises(ValidationError, match="missing its claim identity binding"):
        CheckPayload.model_validate(document)


@pytest.mark.parametrize(
    ("category", "target", "reason"),
    [
        ("physical", "model", "requires correction_category model_calibration"),
        ("spec_design", "calibration", "requires correction_category model_calibration"),
        ("model_calibration", "physical", "requires correction_category physical"),
    ],
)
def test_public_actuator_draft_refuses_contradictory_category_and_target(
    tmp_path, category, target, reason
):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=reason):
        api.actuator_review_draft(
            path,
            "elbow.nominal",
            evidence=ref,
            evidence_root=root,
            expected_revision=revision,
            provenance="bench operator",
            discrepancy="tracking mismatch",
            alternatives=("repeat capture",),
            proposed_target=target,
            proposed_patch="review proposed change",
            rationale="selected capture binds the finding",
            correction_category=category,
            required_recheck=("repeat elbow nominal capture",),
        )
    assert path.read_bytes() == before


def test_public_actuator_spec_design_proposal_is_validated_and_case_bound(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    before = path.read_bytes()
    inputs = {
        "evidence": ref,
        "evidence_root": root,
        "expected_revision": revision,
        "provenance": "bench operator",
        "discrepancy": "declared tracking target needs design review",
        "alternatives": ("retain the current target and improve the mechanism",),
        "rationale": "review an authored contract target without claiming physical improvement",
        "correction_category": "spec_design",
        "required_recheck": ("repeat elbow nominal capture after engineer review",),
    }
    change = Change(
        "actuator_cases/elbow.nominal/position_tracking_max_error",
        "0.06",
        "design-review",
        "bench operator",
        locked_at=dt.date(2026, 9, 28),
        unit="rad",
    )
    document = api.actuator_review_draft(
        path, "elbow.nominal", proposed_changes=(change,), **inputs
    )
    draft = document["actuator_review_draft"]
    assert draft["correction_category"] == "spec_design"
    assert draft["proposed_patch"]["kind"] == "lockwire_spec"
    assert "0.06" in draft["proposed_patch"]["patch"]
    assert "source design-review" in draft["proposed_patch"]["patch"]
    assert "witness bench operator" in draft["proposed_patch"]["patch"]
    assert draft["base_raw_revision"] == revision
    assert {entry["claim_id"] for entry in draft["evidence"]} == {
        "conformance",
        "simulation_validity",
    }
    CheckPayload.model_validate(document)
    assert path.read_bytes() == before

    contradictory = copy.deepcopy(document)
    contradictory["actuator_review_draft"]["correction_category"] = "physical"
    with pytest.raises(ValidationError, match="category does not match its proposed change"):
        CheckPayload.model_validate(contradictory)

    unrelated = Change(
        "model/joints/elbow/limits/effort",
        "4.0",
        "design-review",
        "bench operator",
        locked_at=dt.date(2026, 9, 28),
    )
    with pytest.raises(ValueError, match="selected actuator case"):
        api.actuator_review_draft(path, "elbow.nominal", proposed_changes=(unrelated,), **inputs)
    assert path.read_bytes() == before


def test_public_model_calibration_draft_requires_its_own_admitted_claim(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    before = path.read_bytes()
    inputs = {
        "evidence": ref,
        "evidence_root": root,
        "expected_revision": revision,
        "provenance": "bench operator",
        "discrepancy": "prediction differs from the held-out reference",
        "alternatives": ("retain the current model",),
        "proposed_target": "calibration",
        "proposed_patch": "review the calibration inputs",
        "rationale": "the selected case has a separate simulation-validity claim",
        "correction_category": "model_calibration",
        "required_recheck": ("rerun held-out validation",),
    }
    document = api.actuator_review_draft(path, "elbow.nominal", **inputs)
    assert document["actuator_review_draft"]["proposed_patch"]["kind"] == "calibration"
    assert (
        document["actuator_cases"][0]["simulation_validity"]["context"]["evidence_class"]
        == "prediction_with_held_out_reference"
    )

    payload = json.loads((root / ref.path).read_bytes())
    payload["prediction"] = None
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError, match="requires admitted simulation_validity evidence"):
        api.actuator_review_draft(path, "elbow.nominal", **inputs)
    assert path.read_bytes() == before


def test_review_draft_binds_multiple_distinct_citations_for_one_claim(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    document = api.actuator_review_draft(
        path,
        "elbow.nominal",
        evidence=ref,
        evidence_root=root,
        expected_revision=revision,
        provenance="bench operator",
        discrepancy="tracking mismatch",
        alternatives=("repeat capture",),
        proposed_target="physical",
        proposed_patch="inspect linkage",
        rationale="selected capture binds the finding",
        correction_category="physical",
        required_recheck=("repeat elbow nominal capture",),
    )
    second_payload = b"independent second capture"
    second_path = f"{ref.path}.alternative"
    (root / second_path).write_bytes(second_payload)
    second_ref = ArtifactRef(path=second_path, sha256=hashlib.sha256(second_payload).hexdigest())
    assert read_artifact(second_ref, root) == second_payload
    second = copy.deepcopy(document["actuator_review_draft"]["evidence"][0])
    second["artifact"] = second_ref.model_dump(mode="json")
    second["provenance"] = "different capture"
    document["actuator_review_draft"]["evidence"].append(second)
    binding = document["actuator_cases"][0]["conformance"]["context"]["draft_binding"]
    binding["evidence"].append({"artifact": second["artifact"], "provenance": second["provenance"]})
    CheckPayload.model_validate(document)

    binding["evidence"].pop()
    with pytest.raises(ValidationError, match="missing its claim identity binding"):
        CheckPayload.model_validate(document)
    binding["evidence"].append({"artifact": second["artifact"], "provenance": "altered capture"})
    with pytest.raises(ValidationError, match="missing its claim identity binding"):
        CheckPayload.model_validate(document)


def test_review_draft_refuses_an_exact_duplicate_citation(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    document = api.actuator_review_draft(
        path,
        "elbow.nominal",
        evidence=ref,
        evidence_root=root,
        expected_revision=revision,
        provenance="bench operator",
        discrepancy="tracking mismatch",
        alternatives=("repeat capture",),
        proposed_target="physical",
        proposed_patch="inspect linkage",
        rationale="selected capture binds the finding",
        correction_category="physical",
        required_recheck=("repeat elbow nominal capture",),
    )
    document["actuator_review_draft"]["evidence"].append(
        copy.deepcopy(document["actuator_review_draft"]["evidence"][0])
    )
    with pytest.raises(ValidationError, match="duplicate actuator review citation"):
        CheckPayload.model_validate(document)


@pytest.mark.parametrize("failure", [None, "unit", "frame", "value_frame"])
def test_public_check_preserves_binding_operands_and_separate_claim_gaps(tmp_path, failure):
    _contract_model, contract_path, evidence_ref, evidence_root, revision = _path_backed_case(
        tmp_path
    )
    payload = json.loads((evidence_root / evidence_ref.path).read_bytes())
    row = next(item for item in payload["signals"] if item["signal"] == "state.position")
    if failure == "unit":
        row["value"]["unit"] = "deg"
    elif failure == "frame":
        row["frame"] = "foreign-frame"
    elif failure == "value_frame":
        row["value"]["frame"] = "foreign-frame"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (evidence_root / evidence_ref.path).write_bytes(encoded)
    evidence_ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        contract_path,
        actuator_evidence=_selection(evidence_ref),
        evidence_root=evidence_root,
        expected_revision=revision,
    )
    case = document["actuator_cases"][0]
    physical, simulation = case["conformance"], case["simulation_validity"]
    assert physical["verdict"] == (
        "NOT_SCORED" if failure in {"unit", "frame", "value_frame"} else "GO"
    )
    assert simulation["verdict"] == "GO"
    assert [record["quantity"] for record in simulation["comparisons"]] == [
        "actuator_cases/elbow.nominal/simulation_validity/rmse",
        "actuator_cases/elbow.nominal/simulation_validity/peak",
    ]
    assert all(record["provenance"] is None for record in simulation["comparisons"])
    assert all(record["observed"] == "0.01 rad" for record in simulation["comparisons"])
    assert case["evidence"] == evidence_ref.model_dump(mode="json")
    passes = {record["quantity"]: record for record in physical["comparisons"]}
    position_key = "actuator_cases/elbow.nominal/physical_position"
    if failure in {"unit", "frame", "value_frame"}:
        assert position_key not in passes
        (gap,) = physical["gaps"]
        assert gap["quantity"] == "actuator_cases/elbow.nominal/measured_position"
        assert "unit='rad'" in gap["expected"] and "frame='elbow'" in gap["expected"]
        observed = (
            "unit='deg'"
            if failure == "unit"
            else "value_frame='foreign-frame'"
            if failure == "value_frame"
            else "frame='foreign-frame'"
        )
        assert observed in gap["observed"]
        assert (gap["source"], gap["witness"]) == ("measurement-log/run-1", "bench operator")
        assert simulation["gaps"] == []
    else:
        assert physical["gaps"] == simulation["gaps"] == []
        position = passes[position_key]
        assert position["observed"] == "0.11 rad"
        assert position["expected"] == "0.1 rad"
        assert position["observed_quantity"] == {"value": 0.11, "unit": "rad", "frame": "elbow"}
        assert position["provenance"]["source"] == "measurement-log/run-1"
        assert position["provenance"]["witness"] == "bench operator"
        assert all(record["provenance"] for record in physical["comparisons"])
        CheckPayload.model_validate(document)


@pytest.mark.parametrize("conflict", [False, True])
def test_public_receiver_voltage_preserves_nested_frame_or_refuses_conflict(tmp_path, conflict):
    _contract_model, contract_path, evidence_ref, evidence_root, revision = _path_backed_case(
        tmp_path
    )
    payload = json.loads((evidence_root / evidence_ref.path).read_bytes())
    row = next(item for item in payload["signals"] if item["signal"] == "state.voltage")
    row["value"]["frame"] = "receiver-sense"
    if conflict:
        row["frame"] = "battery-bus"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (evidence_root / evidence_ref.path).write_bytes(encoded)
    evidence_ref.sha256 = hashlib.sha256(encoded).hexdigest()

    document = api.check(
        contract_path,
        actuator_evidence=_selection(evidence_ref),
        evidence_root=evidence_root,
        expected_revision=revision,
    )
    physical = document["actuator_cases"][0]["conformance"]
    voltage_key = "actuator_cases/elbow.nominal/receiver_voltage"
    passes = {record["quantity"]: record for record in physical["comparisons"]}
    if conflict:
        assert physical["verdict"] == "NOT_SCORED"
        assert voltage_key not in passes
        gap = next(
            item for item in physical["gaps"] if item["quantity"].endswith("receiver_voltage")
        )
        assert "frame='battery-bus'" in gap["observed"]
        assert "value_frame='receiver-sense'" in gap["observed"]
        assert (gap["source"], gap["witness"]) == ("measurement-log/run-1", "bench operator")
    else:
        assert physical["verdict"] == "GO"
        assert passes[voltage_key]["observed_quantity"] == {
            "value": 12.0,
            "unit": "V",
            "frame": "receiver-sense",
        }
        assert passes[voltage_key]["provenance"] == {
            "source": "measurement-log/run-1",
            "witness": "bench operator",
            "locked_at": "2026-09-27",
            "bag_sha256": None,
            "conditions": None,
            "note": None,
            "reviews": None,
            "compared_from": None,
        }


def test_public_position_comparison_transports_uncertainty_or_refuses_missing_operand(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    baseline_case = document["actuator_cases"][0]
    (baseline,) = [
        row
        for row in baseline_case["conformance"]["comparisons"]
        if row["quantity"].endswith("/physical_position")
    ]
    assert baseline["uncertainty"] == {"value": 0.005, "unit": "rad", "frame": None}
    assert (baseline["provenance"]["source"], baseline["provenance"]["witness"]) == (
        "measurement-log/run-1",
        "bench operator",
    )
    baseline_sha = baseline_case["evidence"]["sha256"]

    payload = json.loads((root / ref.path).read_bytes())
    measured = next(row for row in payload["signals"] if row["signal"] == "state.position")
    measured["uncertainty"]["value"] = 0.006
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    revised = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    revised_case = revised["actuator_cases"][0]
    (revised_position,) = [
        row
        for row in revised_case["conformance"]["comparisons"]
        if row["quantity"].endswith("/physical_position")
    ]
    assert revised_position["uncertainty"] == {"value": 0.006, "unit": "rad", "frame": None}
    assert revised_case["evidence"]["sha256"] == ref.sha256 != baseline_sha
    assert (
        revised_position["observed"],
        revised_position["expected"],
        revised_position["margin"],
        revised_position["provenance"]["source"],
        revised_position["provenance"]["witness"],
    ) == (
        baseline["observed"],
        baseline["expected"],
        baseline["margin"],
        baseline["provenance"]["source"],
        baseline["provenance"]["witness"],
    )

    measured.pop("uncertainty")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    missing = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    physical = missing["actuator_cases"][0]["conformance"]
    assert not [
        row for row in physical["comparisons"] if row["quantity"].endswith("/physical_position")
    ]
    assert any(
        gap["quantity"].endswith("/physical_position") and gap["severity"] == "not_scored"
        for gap in physical["gaps"]
    )


@pytest.mark.parametrize(
    ("signal", "value", "quantity", "observed", "expected"),
    [
        (
            "state.position",
            0.20,
            "physical_position",
            {"value": 0.10, "unit": "rad", "frame": None},
            {"value": 0.05, "unit": "rad", "frame": None},
        ),
        (
            "state.voltage",
            15.0,
            "receiver_voltage",
            {"value": 15.0, "unit": "V", "frame": None},
            {"value": 14.0, "unit": "V", "frame": None},
        ),
    ],
)
def test_public_physical_violation_carries_typed_operands_and_measurement_provenance(
    tmp_path, signal, value, quantity, observed, expected
):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    payload = json.loads((root / ref.path).read_bytes())
    row = next(item for item in payload["signals"] if item["signal"] == signal)
    row["value"]["value"] = value
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()

    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    physical = document["actuator_cases"][0]["conformance"]
    finding = next(item for item in physical["gaps"] if item["quantity"].endswith(quantity))
    assert finding["severity"] == "violation"
    assert finding["observed_quantity"] == observed
    assert finding["expected_quantity"] == expected
    assert (finding["source"], finding["witness"]) == ("measurement-log/run-1", "bench operator")
    CheckPayload.model_validate(document)


@pytest.mark.parametrize("missing", [None, "omitted", "source", "witness"])
def test_public_measurement_provenance_is_supplied_or_explicitly_unknown(tmp_path, missing):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    payload = json.loads((root / ref.path).read_bytes())
    row = next(r for r in payload["signals"] if r["signal"] == "state.position")
    row["value"]["value"] = 0.30
    if missing == "omitted":
        row.pop("provenance")
    elif missing is not None:
        row["provenance"][missing] = " "
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["simulation_validity"]["verdict"] == "GO"
    assert len(case["simulation_validity"]["comparisons"]) == 2
    assert all(
        record["quantity"] != "actuator_cases/elbow.nominal/physical_position"
        for record in case["conformance"]["comparisons"]
    )
    (gap,) = case["conformance"]["gaps"]
    if missing is None:
        assert case["conformance"]["verdict"] == "NO-GO"
        assert (gap["source"], gap["witness"]) == ("measurement-log/run-1", "bench operator")
    else:
        assert case["conformance"]["verdict"] == "NOT_SCORED"
        assert (
            gap["observed"] == "measurement provenance unknown: source or witness is not recorded"
        )
        assert gap["source"] is None and gap["witness"] is None


@pytest.mark.parametrize("failure", [None, "position", "held_out", "calibration_hash"])
def test_public_simulation_failures_preserve_physical_go(tmp_path, failure):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    baseline = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    original_physical = baseline["actuator_cases"][0]["conformance"]
    assert original_physical["verdict"] == "GO"
    payload = json.loads((root / ref.path).read_bytes())
    if failure == "position":
        payload["prediction"]["samples"][0]["position"]["value"] = 0.30
    elif failure == "held_out":
        payload["validation_reference"]["held_out"] = False
    elif failure == "calibration_hash":
        payload["prediction"]["calibration_hashes"] = ["0" * 64]
    # Rebind changed prediction/reference bytes and the outer evidence payload.
    for lane in ("prediction", "validation_reference"):
        artifact = payload[lane]["artifact"]
        encoded = json.dumps(
            {key: value for key, value in payload[lane].items() if key != "artifact"},
            separators=(",", ":"),
        ).encode()
        (root / artifact["path"]).write_bytes(encoded)
        artifact["sha256"] = hashlib.sha256(encoded).hexdigest()
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert document["revision"] == revision
    assert case["evidence"] == ref.model_dump(mode="json")
    expected_physical = copy.deepcopy(original_physical)
    expected_physical["context"]["selected_evidence"] = ref.model_dump(mode="json")
    assert case["conformance"] == expected_physical
    simulation = case["simulation_validity"]
    prefix = "actuator_cases/elbow.nominal/simulation_validity/"
    if failure is None:
        assert simulation["verdict"] == "GO"
        assert simulation["scored"] == 1
        assert simulation["gaps"] == []
        comparisons = simulation["comparisons"]
        assert [record["quantity"] for record in comparisons] == [
            prefix + "rmse",
            prefix + "peak",
        ]
        assert all(record["provenance"] is None for record in comparisons)
        assert all(record["observed"] == "0.01 rad" for record in comparisons)
        assert all(
            record["observed_quantity"]["value"] == pytest.approx(0.01)
            and record["observed_quantity"]["unit"] == "rad"
            and record["observed_quantity"].get("frame") is None
            for record in comparisons
        )
        assert all(record["expected_quantity"] is not None for record in comparisons)
    elif failure == "position":
        assert simulation["comparisons"] == []
        assert simulation["verdict"] == "NO-GO"
        assert simulation["scored"] == 1
        assert {gap["quantity"] for gap in simulation["gaps"]} == {
            prefix + "rmse",
            prefix + "peak",
        }
        for gap in simulation["gaps"]:
            assert gap["severity"] == "violation"
            assert gap["observed"] == "0.19 rad"
            assert gap["expected"] == (
                "<= 0.03 rad" if gap["quantity"].endswith("/rmse") else "<= 0.04 rad"
            )
    else:
        assert simulation["comparisons"] == []
        assert simulation["verdict"] == "NOT_SCORED"
        assert simulation["scored"] == 0
        (gap,) = simulation["gaps"]
        assert gap["severity"] == "not_scored"
        assert gap["quantity"] == prefix + ("reference" if failure == "held_out" else "identity")


def test_claim_gaps_do_not_leak_across_cases_or_lanes():
    quantities = (
        "actuator_cases/elbow.nominal/measured_position",
        "actuator_cases/elbow.nominal/simulation_validity/identity",
        "actuator_cases/other/measured_position",
        "actuator_cases/other/conformance/evidence",
        "actuator_cases/other/simulation_validity/identity",
        "global/conformance/evidence",
        "global/simulation_validity/identity",
    )
    findings = tuple(
        Finding(
            severity="not_scored",
            quantity=quantity,
            observed="missing",
            expected="evidence",
            action="supply",
        )
        for quantity in quantities
    )
    decision = verdict(RecordReport([], scored=0))
    comparisons = tuple(
        ScoredRecord(quantity=quantity, observed="1 rad", expected="1 rad", margin="agrees")
        for quantity in (quantities[0], quantities[1], quantities[2], quantities[5])
    )
    for lane, expected in (("conformance", quantities[0]), ("simulation_validity", quantities[1])):
        claim = api._actuator_claim_document("elbow.nominal", decision, findings, lane, comparisons)
        assert [row["quantity"] for row in claim["comparisons"]] == (
            [quantities[0]] if lane == "conformance" else [quantities[1]]
        )
        assert [gap["quantity"] for gap in claim["gaps"]] == [expected]


def test_contract_only_check_names_two_not_scored_gaps_without_inflating_scored(tmp_path):
    contract, contract_path, _evidence_ref, _evidence_root, revision = _path_backed_case(tmp_path)

    document = api.check(contract_path)
    actuator_case = document["actuator_cases"][0]
    assert document["revision"] == revision
    assert actuator_case["case_id"] == "elbow.nominal"
    assert actuator_case["evidence"] is None
    assert [actuator_case[lane]["label"] for lane in ("conformance", "simulation_validity")] == [
        "physical conformance",
        "simulation validity",
    ]
    assert [actuator_case[lane]["verdict"] for lane in ("conformance", "simulation_validity")] == [
        "NOT_SCORED",
        "NOT_SCORED",
    ]
    assert [
        actuator_case[lane]["gaps"][0]["quantity"]
        for lane in ("conformance", "simulation_validity")
    ] == [
        "actuator_cases/elbow.nominal/conformance/evidence",
        "actuator_cases/elbow.nominal/simulation_validity/evidence",
    ]

    without_cases = contract.model_copy(deep=True)
    without_cases.actuator_cases = []
    without_cases_path = tmp_path / "without-cases.yaml"
    without_cases_path.write_text(
        json.dumps(without_cases.model_dump(mode="json"), separators=(",", ":")),
        encoding="utf-8",
    )
    baseline = api.check(without_cases_path)
    assert document["scored"] == baseline["scored"]
    assert document["counts"]["not_scored"] == baseline["counts"]["not_scored"] + 2


def test_library_cli_sidecar_and_mcp_preserve_the_same_two_carrier_verdicts(tmp_path):
    pytest.importorskip("mcp")
    _contract_model, contract_path, evidence_ref, evidence_root, revision = _path_backed_case(
        tmp_path
    )
    selection = _selection(evidence_ref)
    library = api.check(
        contract_path,
        actuator_evidence=selection,
        evidence_root=evidence_root,
        expected_revision=revision,
    )
    actuator_case = library["actuator_cases"][0]
    assert [actuator_case[lane]["verdict"] for lane in ("conformance", "simulation_validity")] == [
        "GO",
        "GO",
    ]
    assert actuator_case["case_id"] == "elbow.nominal"
    assert actuator_case["evidence"] == evidence_ref.model_dump(mode="json")

    selection_arg = f"elbow.nominal={evidence_ref.path}@{evidence_ref.sha256}"
    json_out = tmp_path / "check.json"
    cli = CliRunner().invoke(
        app,
        [
            "check",
            str(contract_path),
            "--actuator-evidence",
            selection_arg,
            "--evidence-root",
            str(evidence_root),
            "--expected-revision",
            revision,
            "--json",
            str(json_out),
        ],
    )
    assert cli.exit_code == library["exit_code"], cli.output
    assert json.loads(json_out.read_text(encoding="utf-8")) == library

    sidecar = _handle_check(
        CheckCommand.model_validate(
            {
                "command": "check",
                "args": {"contract": str(contract_path)},
                "actuator_evidence": {"elbow.nominal": evidence_ref.model_dump(mode="json")},
                "evidence_root": str(evidence_root),
                "expected_revision": revision,
            }
        )
    )
    assert sidecar.payload == library
    expected_line = " ".join(
        shlex.quote(part)
        for part in (
            "lockwire",
            "check",
            str(contract_path),
            "--actuator-evidence",
            selection_arg,
            "--evidence-root",
            str(evidence_root),
            "--expected-revision",
            revision,
        )
    )
    assert sidecar.cli_line == expected_line

    async def call_mcp():
        from mcp.client import Client

        async with Client(build_server()) as client:
            return await client.call_tool(
                "check_contract",
                {
                    "contract": str(contract_path),
                    "actuator_evidence": {"elbow.nominal": evidence_ref.model_dump(mode="json")},
                    "evidence_root": str(evidence_root),
                    "expected_revision": revision,
                },
            )

    mcp = asyncio.run(call_mcp())
    assert not mcp.is_error
    mcp_payload = dict(mcp.structured_content)
    assert mcp_payload.pop("cli_line") == expected_line
    agent = _with_cli_line(dict(library), expected_line)  # worst first, no empty fields
    assert mcp_payload == {k: v for k, v in agent.items() if k != "cli_line"}


def test_all_evidence_inputs_are_required_together_and_stale_revisions_refuse(tmp_path):
    _contract_model, contract_path, evidence_ref, evidence_root, revision = _path_backed_case(
        tmp_path
    )
    selection = _selection(evidence_ref)

    with pytest.raises(
        ValueError,
        match="requires actuator_evidence, evidence_root, and expected_revision together",
    ):
        api.check(contract_path, actuator_evidence=selection)
    with pytest.raises(ValueError, match="contract changed since evidence was selected"):
        api.check(
            contract_path,
            actuator_evidence=selection,
            evidence_root=evidence_root,
            expected_revision="0" * 64,
        )

    incomplete_cli = CliRunner().invoke(
        app,
        [
            "check",
            str(contract_path),
            "--actuator-evidence",
            f"elbow.nominal={evidence_ref.path}@{evidence_ref.sha256}",
        ],
    )
    assert incomplete_cli.exit_code == 2
    assert (
        "requires actuator_evidence, evidence_root, and expected_revision together"
        in incomplete_cli.output
    )

    stale_sidecar = _handle_check(
        CheckCommand.model_validate(
            {
                "command": "check",
                "args": {"contract": str(contract_path)},
                "actuator_evidence": {"elbow.nominal": evidence_ref.model_dump(mode="json")},
                "evidence_root": str(evidence_root),
                "expected_revision": "0" * 64,
            }
        )
    )
    assert stale_sidecar.exit_code == 1
    assert stale_sidecar.payload is None
    assert (
        stale_sidecar.refusal
        == "contract changed since evidence was selected; reload before checking"
    )
    assert stale_sidecar.cli_line is not None
    assert f"--expected-revision {'0' * 64}" in stale_sidecar.cli_line


@pytest.mark.parametrize("selection", ["outer", "physical"])
def test_public_hash_refusal_preserves_digest_operands(tmp_path, selection):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    stale = ref
    if selection == "physical":
        payload = json.loads((root / ref.path).read_bytes())
        row = next(row for row in payload["signals"] if row["signal"] == "state.position")
        stale = ArtifactRef.model_validate(row["artifact"])
    artifact = root / stale.path
    artifact.write_bytes(artifact.read_bytes() + b" ")
    actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert actual != stale.sha256
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["evidence"] == ref.model_dump(mode="json")
    physical = case["conformance"]
    assert physical["verdict"] == "NOT_SCORED"
    assert physical["comparisons"] == []
    lanes = [physical]
    if selection == "outer":
        assert case["simulation_validity"]["verdict"] == "NOT_SCORED"
        assert case["simulation_validity"]["comparisons"] == []
        lanes.append(case["simulation_validity"])
    else:
        assert case["simulation_validity"]["verdict"] == "GO"
    for lane in lanes:
        (gap,) = lane["gaps"]
        assert stale.path in gap["observed"]
        assert gap["expected"] == "contained hash-verified artifacts consumed by this claim"
        assert f"observed {actual}" in gap["observed"]
        assert f"expected {stale.sha256}" in gap["observed"]
        assert gap["source"] is None and gap["witness"] is None


def test_public_clock_refusal_preserves_mapping_operands(tmp_path):
    contract, path, ref, root, _ = _path_backed_case(tmp_path)
    mapping_ref = contract.actuator_cases[0].clock_mapping_artifacts[2]
    mapping_path = root / mapping_ref.path
    mapping = json.loads(mapping_path.read_bytes())
    mapping["target_epoch"] = "different-case-run"
    encoded = json.dumps(mapping, separators=(",", ":")).encode()
    mapping_path.write_bytes(encoded)
    mapping_ref.sha256 = hashlib.sha256(encoded).hexdigest()
    path.write_text(json.dumps(contract.model_dump(mode="json"), separators=(",", ":")))
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = json.loads((root / ref.path).read_bytes())
    payload["validation_reference"]["clock_mapping"] = mapping_ref.model_dump(mode="json")
    payload["prediction"]["contract_sha256"] = revision
    prediction_mapping = json.loads(
        (root / payload["prediction"]["clock_mapping"]["path"]).read_bytes()
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["conformance"]["verdict"] == "GO"
    assert case["conformance"]["gaps"] == []
    assert case["simulation_validity"]["verdict"] == "NOT_SCORED"
    assert case["simulation_validity"]["comparisons"] == []
    (gap,) = case["simulation_validity"]["gaps"]
    assert gap["quantity"].endswith("/simulation_validity/clock_mapping")
    operands = gap["observed"].split("prediction mapping: ", 1)[1]
    predicted, reference = operands.split("; reference mapping: ", 1)
    assert json.loads(predicted) == ClockMapping.model_validate(prediction_mapping).model_dump(
        mode="json"
    )
    assert json.loads(reference) == ClockMapping.model_validate(mapping).model_dump(mode="json")
    assert mapping_ref.sha256 == hashlib.sha256(mapping_path.read_bytes()).hexdigest()
    assert case["evidence"] == ref.model_dump(mode="json")
    assert gap["expected"] == (
        "contract-declared mappings into one exact target clock domain and epoch; "
        f"match prediction target domain={prediction_mapping['target_clock_domain']!r}, "
        f"epoch={prediction_mapping['target_epoch']!r}"
    )
    assert gap["source"] is None and gap["witness"] is None


@pytest.mark.parametrize("failure", ["physical_clock", "event_clock", "calibration"])
def test_remaining_public_refusals_preserve_bound_operands(tmp_path, failure):
    from test_actuator_case import _with_event_timeline

    contract, evidence = _contract(), _evidence()
    if failure == "event_clock":
        _with_event_timeline(contract, evidence)
    ref, root = _admitted_evidence(tmp_path, contract, evidence)
    mapping = None
    if failure != "calibration":
        mapping_ref = contract.actuator_cases[0].clock_mapping_artifacts[0]
        mapping_path = root / mapping_ref.path
        mapping = json.loads(mapping_path.read_bytes())
        mapping["max_error"]["value"] = 0.2
        encoded = json.dumps(mapping, separators=(",", ":")).encode()
        mapping_path.write_bytes(encoded)
        mapping_ref.sha256 = hashlib.sha256(encoded).hexdigest()
        for event in evidence.events:
            if event.clock_mapping and event.clock_mapping.path == mapping_ref.path:
                event.clock_mapping = mapping_ref
    else:
        evidence.prediction.calibration_hashes = ["0" * 64]
    path = tmp_path / "lockwire.yaml"
    path.write_text(json.dumps(contract.model_dump(mode="json"), separators=(",", ":")))
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence.prediction.contract_sha256 = revision
    encoded = evidence.model_dump_json().encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    failed, other = (
        ("simulation_validity", "conformance")
        if failure == "calibration"
        else ("conformance", "simulation_validity")
    )
    assert case[failed]["verdict"] == "NOT_SCORED"
    assert case[other]["verdict"] == "GO" and case[other]["gaps"] == []
    (gap,) = case[failed]["gaps"]
    if failure == "calibration":
        assert f"calibration_hashes={['0' * 64]!r}" in gap["observed"]
        expected = sorted(ref.sha256 for ref in contract.actuator_cases[0].calibration_artifacts)
        assert f"calibration_hashes={expected!r}" in gap["expected"]
        assert revision in gap["observed"] and revision in gap["expected"]
    else:
        operands = gap["observed"].split("command mapping: ", 1)[1]
        command, feedback = operands.split("; feedback mapping: ", 1)
        feedback = feedback.split("; command source_time=", 1)[0]
        resolved = ClockMapping.model_validate(mapping).model_dump(mode="json")
        assert json.loads(command) == resolved and json.loads(feedback) == resolved
        assert mapping_ref.sha256 == hashlib.sha256(mapping_path.read_bytes()).hexdigest()
        assert case["evidence"] == ref.model_dump(mode="json")
        assert (
            f"combined mapping error <= {contract.actuator_cases[0].clock_alignment_max_error}"
            in gap["expected"]
        )
        assert gap["quantity"].endswith(
            "feedback_timeline/clock_mapping" if failure == "event_clock" else "feedback_deadline"
        )
    assert gap["source"] is None and gap["witness"] is None
