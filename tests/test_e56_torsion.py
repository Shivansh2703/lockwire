"""Pinned CalculiX 2.23 torsion benchmark fixtures; no product-admission claim."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lockwire_pro import analysis
from lockwire_pro.analysis import AnalysisRefused, prepare_analysis
from lockwire_pro.jobs import start_analysis_job
from lockwire.schema import Contract, save_contract

FIXTURES = Path(__file__).parent / "fixtures" / "calculix_benchmarks"
MANIFEST = json.loads((FIXTURES / "torsion_c3d20_manifest.json").read_text())
ROW = re.compile(
    r"^\s*(?P<node>\d+)\s+(?P<x>[+-]?[\d.]+[Ee][+-]?\d+)\s+"
    r"(?P<y>[+-]?[\d.]+[Ee][+-]?\d+)\s+(?P<z>[+-]?[\d.]+[Ee][+-]?\d+)\s*$"
)


PROVENANCE = {
    "witness": "fixture",
    "source": "pinned CalculiX torsion benchmark",
    "locked_at": "2026-09-28",
}


def quantity(value: float, unit: str, frame: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"value": value, "unit": unit}
    if frame is not None:
        result["frame"] = frame
    return result


def prepared_torsion_contract(tmp_path: Path, mesh: int = 8) -> tuple[Path, Path, Path, str]:
    """Build the one public solid-annulus declaration for the native Job gate."""
    case = next(case for case in MANIFEST["cases"] if case["id"] == f"torsion_c3d20_{mesh}")
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / "torsion.inp"
    deck.write_bytes((FIXTURES / case["deck"]).read_bytes())
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = {
        "schema_version": "0.4",
        "robot": {"name": "torsion-fixture"},
        "model": {
            "source": "torsion.urdf",
            "source_sha256": digest,
            "structure_hash": "b" * 64,
            "links": [{"name": "shaft"}],
            "provenance": PROVENANCE,
        },
        "structural": {
            "version": "1",
            "model": {
                "source_sha256": digest,
                "structure_hash": "b" * 64,
                "source_frame": "base",
                "model_artifact": {
                    "role": "structural_model",
                    "ref": {"path": deck.name, "sha256": digest},
                    "format": "calculix_inp",
                    "provenance": PROVENANCE,
                },
                "provenance": PROVENANCE,
            },
            "materials": [
                {
                    "id": "steel",
                    "kind": "linear_isotropic",
                    "youngs_modulus": quantity(200, "GPa"),
                    "poisson_ratio": quantity(0.3, "1"),
                    "provenance": PROVENANCE,
                }
            ],
            "sections": [
                {
                    "id": "shaft",
                    "kind": "solid",
                    "material_id": "steel",
                    "provenance": PROVENANCE,
                }
            ],
            "assignments": [
                {
                    "id": "shaft-elements",
                    "region": {"kind": "mesh_region", "name": "SHAFT"},
                    "section_id": "shaft",
                    "provenance": PROVENANCE,
                }
            ],
            "supports": [
                {
                    "id": "fixed-support",
                    "region": {"kind": "node_set", "name": "SUPPORT"},
                    "dofs": {"translation": ["x", "y", "z"], "frame": "base"},
                    "provenance": PROVENANCE,
                },
                {
                    "id": "prescribed-top-twist",
                    "region": {"kind": "node_set", "name": "TOP"},
                    "dofs": {
                        "rotation": ["x"],
                        "frame": "base",
                        "prescribed_rotation": {"x": quantity(0.001, "rad", "base")},
                    },
                    "provenance": PROVENANCE,
                },
            ],
            "procedures": [
                {
                    "id": "static",
                    "kind": "linear_static",
                    "assignment_ids": ["shaft-elements"],
                    "support_ids": ["fixed-support", "prescribed-top-twist"],
                    "provenance": PROVENANCE,
                }
            ],
            "acceptance_limits": [
                {
                    "id": "torsion-midspan-u3",
                    "kind": "scalar_reference",
                    "metric": "torsion_midspan_u3",
                    "comparator": "relative",
                    "threshold": quantity(2, "%"),
                    "reference": "analytic",
                    "provenance": PROVENANCE,
                }
            ],
            "runtime": {
                "id": "ccx",
                "package_profile": "ccx-2.23",
                "max_wall_time": quantity(30, "s"),
                "provenance": PROVENANCE,
            },
            "provenance": PROVENANCE,
        },
    }
    contract = tmp_path / "torsion.lockwire.yaml"
    save_contract(Contract.model_validate(document), contract)
    manifest = tmp_path / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": "1",
                "profiles": {
                    "ccx-2.23": {
                        "backend": "calculix_ccx",
                        "solver_version": "2.23",
                        "offline": True,
                        "admitted_procedures": ["linear_static"],
                        "procedure_templates": {"linear_static": "direct-torsion-c3d20-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest, digest


def prepare_torsion(contract: Path, evidence: Path, manifest: Path) -> dict[str, object]:
    return prepare_analysis(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )


def torsion_document(contract: Path) -> dict[str, object]:
    return analysis.load_contract_bytes(contract.read_bytes()).model_dump(mode="json")


def rebind_torsion_bytes(document: dict[str, object], evidence: Path) -> None:
    digest = hashlib.sha256((evidence / "torsion.inp").read_bytes()).hexdigest()
    document["model"].update(source_sha256=digest)
    document["structural"]["model"].update(source_sha256=digest)
    document["structural"]["model"]["model_artifact"]["ref"].update(sha256=digest)


def result_torque_x(
    rows: list[dict[str, object]],
    nodes: dict[int, tuple[float, float, float]],
    node_set: str,
) -> float:
    return sum(
        nodes[int(row["node"])][1] * float(row["values"][2])
        - nodes[int(row["node"])][2] * float(row["values"][1])
        for row in rows
        if row["kind"] == "RF" and row["node_set"] == node_set
    )


def parse_node_print(text: str) -> dict[tuple[str, str, int], tuple[float, float, float]]:
    values: dict[tuple[str, str, int], tuple[float, float, float]] = {}
    kind = node_set = None
    for line in text.splitlines():
        match = re.search(r"\b(displacements|forces) \([vf]x,[vf]y,[vf]z\) for set (\S+)", line)
        if match:
            kind = "U" if match.group(1) == "displacements" else "RF"
            node_set = match.group(2)
        row = ROW.match(line)
        if row and kind and node_set:
            values[(kind, node_set, int(row["node"]))] = tuple(
                float(row[name]) for name in ("x", "y", "z")
            )
    return values


def parse_nodes(text: str) -> dict[int, tuple[float, float, float]]:
    nodes: dict[int, tuple[float, float, float]] = {}
    in_nodes = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            in_nodes = line.upper() == "*NODE"
        elif in_nodes:
            node, x, y, z = (value.strip() for value in line.split(","))
            nodes[int(node)] = (float(x), float(y), float(z))
    return nodes


def torque_x(
    parsed: dict[tuple[str, str, int], tuple[float, float, float]],
    nodes: dict[int, tuple[float, float, float]],
    node_set: str,
) -> float:
    return sum(
        nodes[node][1] * values[2] - nodes[node][2] * values[1]
        for (kind, printed_set, node), values in parsed.items()
        if kind == "RF" and printed_set == node_set
    )


def ccx() -> Path:
    configured = os.environ.get("LOCKWIRE_CCX")
    if not configured:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 pinned ccx_2.23 for the torsion numerical gate")
    binary = Path(configured)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("LOCKWIRE_CCX must name an executable absolute path")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != analysis._PACKAGED_CCX_SHA256:
        pytest.fail("LOCKWIRE_CCX must match the E5.4 pinned CalculiX executable")
    return binary


def run_deck(binary: Path, deck: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    run_dir = tmp_path / deck.stem
    run_dir.mkdir()
    copied = run_dir / deck.name
    shutil.copy2(deck, copied)
    return subprocess.run(
        [str(binary), "-i", copied.stem],
        cwd=run_dir,
        env={"PATH": "/usr/bin:/bin", "HOME": str(run_dir), "OMP_NUM_THREADS": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_torsion_fixture_identity_units_and_reference_are_pinned() -> None:
    expected_units = {
        "length": "m",
        "outer_radius": "m",
        "inner_radius": "m",
        "twist": "rad",
        "youngs_modulus": "Pa",
        "poisson_ratio": "1",
    }
    for case in MANIFEST["cases"]:
        deck = FIXTURES / case["deck"]
        declared = case["declared_inputs"]
        assert {key: value["unit"] for key, value in declared.items()} == expected_units
        provenance = case["fixture_provenance"]
        assert provenance["sha256"] == hashlib.sha256(deck.read_bytes()).hexdigest()
        assert provenance["source_path"] == f"tests/fixtures/calculix_benchmarks/{deck.name}"
        assert case["fixture_provenance"]["third_party_deck_redistributed"] is False
        assert "*RIGID BODY" not in deck.read_text() and "*EQUATION" not in deck.read_text()
    declared = MANIFEST["cases"][0]["declared_inputs"]
    shear_modulus = declared["youngs_modulus"]["value"] / (
        2 * (1 + declared["poisson_ratio"]["value"])
    )
    polar_moment = (
        math.pi
        * (declared["outer_radius"]["value"] ** 4 - declared["inner_radius"]["value"] ** 4)
        / 2
    )
    outputs = MANIFEST["cases"][0]["reference_outputs"]
    expected_torque = (
        shear_modulus * polar_moment * declared["twist"]["value"] / declared["length"]["value"]
    )
    expected_u3 = declared["outer_radius"]["value"] * declared["twist"]["value"] / 2
    assert outputs["reaction_torque_x"]["value"] == pytest.approx(expected_torque)
    assert outputs["midspan_outer_u3"]["value"] == pytest.approx(expected_u3)


def test_torsion_pinned_calculix_reactions_match_the_annular_reference(tmp_path: Path) -> None:
    binary = ccx()
    for case in MANIFEST["cases"]:
        deck = FIXTURES / case["deck"]
        result = run_deck(binary, deck, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        parsed = parse_node_print((tmp_path / deck.stem / f"{deck.stem}.dat").read_text())
        nodes = parse_nodes(deck.read_text())
        expected = case["reference_outputs"]["reaction_torque_x"]["value"]
        assert torque_x(parsed, nodes, "TOP") == pytest.approx(expected, abs=0.01, rel=0.02)
        assert torque_x(parsed, nodes, "SUPPORT") == pytest.approx(-expected, abs=0.01, rel=0.02)


def test_torsion_pinned_calculix_torque_doubles_with_modulus(tmp_path: Path) -> None:
    binary = ccx()
    mutant = tmp_path / "torsion_double_stiffness.inp"
    source = (FIXTURES / "torsion_c3d20_8.inp").read_text()
    mutant.write_text(source.replace("200000000000., .3", "400000000000., .3"))
    result = run_deck(binary, mutant, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = parse_node_print((tmp_path / mutant.stem / f"{mutant.stem}.dat").read_text())
    expected = MANIFEST["cases"][0]["reference_outputs"]["reaction_torque_x"]["value"]
    actual = torque_x(parsed, parse_nodes(mutant.read_text()), "TOP")
    assert actual == pytest.approx(2 * expected, abs=0.01, rel=0.02)


def test_torsion_public_preparation_binds_declared_twist_to_pinned_bytes(tmp_path: Path) -> None:
    contract, evidence, manifest, digest = prepared_torsion_contract(tmp_path)
    prepared = prepare_torsion(contract, evidence, manifest)
    assert (prepared["deck_template"], prepared["status"], prepared["solver_started"]) == (
        "direct-torsion-c3d20-v1",
        "PREPARED",
        False,
    )
    model = next(row for row in prepared["artifacts"] if row["role"] == "structural_model")
    assert model["sha256"] == digest


def test_torsion_public_preparation_refuses_changed_declared_twist(tmp_path: Path) -> None:
    contract, evidence, manifest, _ = prepared_torsion_contract(tmp_path)
    document = torsion_document(contract)
    document["structural"]["supports"][1]["dofs"]["prescribed_rotation"].update(
        x=quantity(0.002, "rad", "base")
    )
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(
        AnalysisRefused, match="TOP displacement field differs from declared x rotation"
    ):
        prepare_torsion(contract, evidence, manifest)


def test_torsion_public_preparation_refuses_wrong_mesh_region(tmp_path: Path) -> None:
    contract, evidence, manifest, _ = prepared_torsion_contract(tmp_path)
    document = torsion_document(contract)
    document["structural"]["assignments"][0]["region"]["name"] = "OTHER"
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="one linear isotropic SHAFT solid"):
        prepare_torsion(contract, evidence, manifest)


def test_torsion_public_preparation_refuses_missing_mesh_region(tmp_path: Path) -> None:
    contract, _, _, _ = prepared_torsion_contract(tmp_path)
    document = torsion_document(contract)
    del document["structural"]["assignments"][0]["region"]
    with pytest.raises(ValueError, match="region"):
        Contract.model_validate(document)


def test_torsion_public_preparation_refuses_rebound_unpinned_bytes(tmp_path: Path) -> None:
    contract, evidence, manifest, _ = prepared_torsion_contract(tmp_path)
    document = torsion_document(contract)
    deck = evidence / "torsion.inp"
    deck.write_text(deck.read_text().replace("** SI", "** MUTATED SI", 1))
    rebind_torsion_bytes(document, evidence)
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="not one retained byte-pinned C3D20 annulus"):
        prepare_torsion(contract, evidence, manifest)


def test_torsion_public_job_binds_declared_twist_to_the_pinned_solid_annulus(
    tmp_path: Path,
) -> None:
    """Native Job contract: one pinned deck, declared twist field, and analytic score."""
    ccx()
    contract, evidence, manifest, digest = prepared_torsion_contract(tmp_path)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=120)
    assert result["status"] == "COMPLETED"
    assert result["solver_started"] is True
    assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
    model = next(row for row in result["artifacts"] if row["role"] == "structural_model")
    assert model["sha256"] == digest
    assert hashlib.sha256((evidence / "torsion.inp").read_bytes()).hexdigest() == digest
    assert result["simulation_validity"] == "GO"
    assert result["physical_conformance"] == "NOT_SCORED"
    operands = result["analytic_operands"]
    assert operands["twist"]["value"] == pytest.approx(0.001)
    assert operands["polar_moment"]["unit"] == "m^4"
    rows = result["results"]
    nodes = parse_nodes((evidence / "torsion.inp").read_text())
    assert next(
        row["values"][2]
        for row in rows
        if row["kind"] == "U" and row["node_set"] == "MIDSPAN" and row["node"] == 11
    ) == pytest.approx(
        MANIFEST["cases"][0]["reference_outputs"]["midspan_outer_u3"]["value"], rel=0.02
    )
    expected_torque = MANIFEST["cases"][0]["reference_outputs"]["reaction_torque_x"]["value"]
    assert result_torque_x(rows, nodes, "TOP") == pytest.approx(expected_torque, rel=0.02)
    assert result_torque_x(rows, nodes, "SUPPORT") == pytest.approx(-expected_torque, rel=0.02)
    assert {row["metric"] for row in result["comparisons"]} == {
        "torsion_midspan_u3",
        "torsion_top_torque_x",
        "torsion_support_torque_x",
    }
    assert all(row["passed"] is True for row in result["comparisons"])
    assert job.exit_code == 0
    assert not job.work_dir.exists()
