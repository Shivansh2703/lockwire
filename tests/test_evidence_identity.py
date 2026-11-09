"""E0.4: a wrong raw hash, unit, frame, or clock mapping is each refused NOT_SCORED
with a named gap, never silently scored. Reuses the transport-layer fixtures
in test_actuator_check_transport rather than re-deriving them."""

from __future__ import annotations

import hashlib
import json

from lockwire_pro import api_pro as api

from test_actuator_check_transport import _path_backed_case, _selection


def test_wrong_raw_sha256_is_refused_not_scored_with_named_gap(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    artifact = root / ref.path
    artifact.write_bytes(artifact.read_bytes() + b" ")
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["conformance"]["verdict"] == "NOT_SCORED"
    (gap,) = case["conformance"]["gaps"]
    assert gap["expected"] == "contained hash-verified artifacts consumed by this claim"
    assert ref.path in gap["observed"]


def test_wrong_unit_is_refused_not_scored_with_named_gap(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    payload = json.loads((root / ref.path).read_bytes())
    row = next(item for item in payload["signals"] if item["signal"] == "state.position")
    row["value"]["unit"] = "deg"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["conformance"]["verdict"] == "NOT_SCORED"
    (gap,) = case["conformance"]["gaps"]
    assert gap["quantity"] == "actuator_cases/elbow.nominal/measured_position"
    assert "unit='rad'" in gap["expected"]
    assert "unit='deg'" in gap["observed"]


def test_wrong_frame_is_refused_not_scored_with_named_gap(tmp_path):
    _, path, ref, root, revision = _path_backed_case(tmp_path)
    payload = json.loads((root / ref.path).read_bytes())
    row = next(item for item in payload["signals"] if item["signal"] == "state.position")
    row["frame"] = "foreign-frame"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["conformance"]["verdict"] == "NOT_SCORED"
    (gap,) = case["conformance"]["gaps"]
    assert gap["quantity"] == "actuator_cases/elbow.nominal/measured_position"
    assert "frame='elbow'" in gap["expected"]
    assert "frame='foreign-frame'" in gap["observed"]


def test_wrong_clock_mapping_is_refused_not_scored_with_named_gap(tmp_path):
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
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    (root / ref.path).write_bytes(encoded)
    ref.sha256 = hashlib.sha256(encoded).hexdigest()
    document = api.check(
        path, actuator_evidence=_selection(ref), evidence_root=root, expected_revision=revision
    )
    case = document["actuator_cases"][0]
    assert case["conformance"]["verdict"] == "GO"
    assert case["simulation_validity"]["verdict"] == "NOT_SCORED"
    (gap,) = case["simulation_validity"]["gaps"]
    assert gap["quantity"].endswith("/simulation_validity/clock_mapping")
