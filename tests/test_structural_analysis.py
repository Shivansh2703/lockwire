"""Structural declaration/admission tests grounded in the exact solver's primary manual.

CalculiX CrunchiX User's Manual 2.23: https://www.dhondt.de/ccx_2.23.pdf . Dynamics use
§§6.9.2, 6.9.5, 6.9.6, and 6.9.7; material/contact/mass cases use the manual's input-keyword
entries ``*ELASTIC``, ``*PLASTIC``, ``*DENSITY``, ``*MASS``, ``*CONTACT PAIR``,
``*SURFACE INTERACTION``, ``*SURFACE BEHAVIOR``, ``*DYNAMIC``, and ``*INITIAL CONDITIONS``.
These are schema/source tests, not solver or numerical evidence.
"""

import hashlib
import importlib
import json
import os
import runpy
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lockwire_pro import analysis
from lockwire_pro.analysis import AnalysisRefused, build_analysis_argv, prepare_analysis
from lockwire_pro.jobs import Job, JobCancelled, start_analysis_job
from lockwire.schema import (
    AnymalScenario,
    ContactInput,
    Contract,
    ControllerInput,
    InertiaTensor,
    MaterialInput,
    SectionInput,
    SectionOrientation,
    StructuralProcedure,
    X500Scenario,
    load_contract,
    load_contract_bytes,
    save_contract,
    upgrade_contract_document,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
PROVENANCE = {"witness": "human", "source": "engineering declaration", "locked_at": "2026-09-14"}


def quantity(value, unit, frame=None):
    result = {"value": value, "unit": unit}
    if frame is not None:
        result["frame"] = frame
    return result


def structural_document(*, source_sha256=HASH_A):
    return {
        "schema_version": "0.4",
        "robot": {"name": "fixture"},
        "model": {
            "source": "robot.urdf",
            "source_sha256": source_sha256,
            "structure_hash": HASH_B,
            "links": [{"name": "arm"}],
            "provenance": PROVENANCE,
        },
        "structural": {
            "version": "1",
            "model": {
                "source_sha256": source_sha256,
                "structure_hash": HASH_B,
                "source_frame": "base",
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
                    "id": "bar",
                    "kind": "truss",
                    "material_id": "steel",
                    "area": quantity(0.01, "m^2"),
                    "provenance": PROVENANCE,
                }
            ],
            "assignments": [
                {
                    "id": "arm-elements",
                    "region": {"kind": "model_link", "name": "arm"},
                    "section_id": "bar",
                    "provenance": PROVENANCE,
                }
            ],
            "procedures": [
                {
                    "id": "static",
                    "kind": "linear_static",
                    "assignment_ids": ["arm-elements"],
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


def test_legacy_upgrade_is_pure_and_refuses_future_sections():
    legacy = {"schema_version": "0.3", "robot": {"name": "old"}}
    proposed = upgrade_contract_document(legacy)
    assert proposed == {"schema_version": "0.5", "robot": {"name": "old"}}
    assert legacy["schema_version"] == "0.3"

    future_syntax = deepcopy(legacy)
    future_syntax["structural"] = {}
    with pytest.raises(ValueError, match="legacy contract"):
        upgrade_contract_document(future_syntax)


def test_linear_static_does_not_invent_mass_damping_or_contact_requirements():
    contract = Contract.model_validate(structural_document())
    assert contract.structural.procedures[0].kind == "linear_static"
    assert contract.structural.masses == []
    assert contract.structural.contacts == []


def test_plastic_curve_has_primary_solver_units_and_ordering():
    valid = {
        "id": "plastic",
        "kind": "isotropic_elastoplastic",
        "youngs_modulus": quantity(200, "GPa"),
        "poisson_ratio": quantity(0.3, "1"),
        "plastic_curve": [
            {
                "von_mises_yield_stress": quantity(250, "MPa"),
                "equivalent_plastic_strain": quantity(0, "1"),
            },
            {
                "von_mises_yield_stress": quantity(300, "MPa"),
                "equivalent_plastic_strain": quantity(0.02, "1"),
            },
        ],
        "provenance": PROVENANCE,
    }
    assert len(MaterialInput.model_validate(valid).plastic_curve) == 2
    invalid = deepcopy(valid)
    invalid["plastic_curve"][0]["equivalent_plastic_strain"]["value"] = 0.01
    with pytest.raises(ValidationError, match="first equivalent plastic strain"):
        MaterialInput.model_validate(invalid)
    invalid = deepcopy(valid)
    invalid["plastic_curve"][1]["von_mises_yield_stress"]["value"] = 200
    with pytest.raises(ValidationError, match="nondecreasing"):
        MaterialInput.model_validate(invalid)


def test_section_orientation_and_torsion_constant_are_distinct_and_physical():
    with pytest.raises(ValidationError, match="nonzero and non-collinear"):
        SectionOrientation.model_validate(
            {
                "id": "beam-axis",
                "frame": "base",
                "longitudinal": [1, 0, 0],
                "in_plane_reference": [2, 0, 0],
                "provenance": PROVENANCE,
            }
        )

    section = {
        "id": "beam",
        "kind": "beam",
        "material_id": "steel",
        "area": quantity(1, "m^2"),
        "principal_second_moment_y": quantity(1, "m^4"),
        "principal_second_moment_z": quantity(1, "m^4"),
        "torsion_constant_j": quantity(1, "m^4"),
        "orientation_id": "beam-axis",
        "provenance": PROVENANCE,
    }
    assert SectionInput.model_validate(section).torsion_constant_j.unit == "m^4"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SectionInput.model_validate(section | {"warping_constant_cw": quantity(1, "m^4")})


def test_inertia_tensor_refuses_nonphysical_principal_moments():
    tensor = {
        "xx": quantity(1, "kg*m^2", "body"),
        "yy": quantity(1, "kg*m^2", "body"),
        "zz": quantity(3, "kg*m^2", "body"),
        "xy": quantity(0, "kg*m^2", "body"),
        "xz": quantity(0, "kg*m^2", "body"),
        "yz": quantity(0, "kg*m^2", "body"),
        "frame": "body",
    }
    with pytest.raises(ValidationError, match="triangle inequality"):
        InertiaTensor.model_validate(tensor)
    tensor["zz"]["value"] = 2
    assert InertiaTensor.model_validate(tensor).zz.value == 2


def test_dynamic_mass_and_damping_are_explicit_while_contact_remains_optional():
    document = structural_document()
    procedure = {
        "id": "modes",
        "kind": "modal",
        "assignment_ids": ["arm-elements"],
        "mode_count": 3,
        "damping_mode": "undamped",
        "provenance": PROVENANCE,
    }
    document["structural"]["procedures"] = [procedure]
    with pytest.raises(ValidationError, match="lacks density or explicit mass"):
        Contract.model_validate(document)

    document["structural"]["materials"][0]["density"] = quantity(7850, "kg/m^3")
    assert Contract.model_validate(document).structural.contacts == []
    missing_mode = deepcopy(procedure)
    missing_mode.pop("damping_mode")
    with pytest.raises(ValidationError, match="explicit damping_mode"):
        StructuralProcedure.model_validate(missing_mode)


def test_contact_law_enforces_surface_selectors_and_parameter_units():
    contact = {
        "id": "bearing",
        "master": {"kind": "surface_set", "name": "master"},
        "slave": {"kind": "surface_set", "name": "slave"},
        "frame": "base",
        "normal": {
            "kind": "linear",
            "pressure_overclosure_slope": quantity(1e9, "N/m^3"),
            "tensile_stress": quantity(0, "Pa"),
            "clearance_coefficient": quantity(0, "1"),
        },
        "tangential": {
            "friction_coefficient": quantity(0.2, "1"),
            "stick_slope": quantity(1e8, "N/m^3"),
        },
        "provenance": PROVENANCE,
    }
    assert ContactInput.model_validate(contact).normal.kind == "linear"
    contact["normal"]["pressure_overclosure_slope"] = quantity(1, "N/m")
    with pytest.raises(ValidationError, match="contact slope"):
        ContactInput.model_validate(contact)


def test_fixed_scenarios_and_disabled_controller_are_declarations():
    common = {
        "id": "x500-case",
        "plant_id": "x500",
        "controller_id": "px4",
        "negative_controller_id": "off",
        "world_id": "world",
        "physics_id": "physics",
        "runtime_id": "gazebo",
        "initial_state_id": "x500-initial",
        "target_altitude": quantity(2, "m"),
        "settled_hover_duration": quantity(10, "s"),
        "deadline": quantity(90, "s"),
        "phase_durations": {
            "takeoff": quantity(10, "s"),
            "settle": quantity(10, "s"),
            "hover": quantity(10, "s"),
            "land": quantity(30, "s"),
        },
        "random_seed": 7,
        "simulated_time": quantity(60, "s"),
        "require_nonzero_sensing": True,
        "require_nonzero_actuation": True,
        "require_actual_feedback": True,
        "require_controller_disabled_negative": True,
        "acceptance_limit_ids": ["rms", "peak", "horizontal", "landing"],
        "provenance": PROVENANCE,
    }
    assert X500Scenario.model_validate(common).target_altitude.base() == 2
    changed = deepcopy(common)
    changed["settled_hover_duration"]["value"] = 9
    with pytest.raises(ValidationError, match="exactly 10 s"):
        X500Scenario.model_validate(changed)

    disabled = {
        "id": "off",
        "kind": "disabled",
        "implicit_hold": False,
        "provenance": PROVENANCE,
    }
    assert ControllerInput.model_validate(disabled).implicit_hold is False
    with pytest.raises(ValidationError, match="implicit_hold=False"):
        ControllerInput.model_validate(disabled | {"implicit_hold": True})

    anymal = {
        "id": "anymal-case",
        "plant_id": "anymal",
        "controller_id": "ros-control",
        "negative_controller_id": "off",
        "world_id": "world",
        "physics_id": "physics",
        "runtime_id": "gazebo",
        "initial_state_id": "anymal-initial",
        "knee_cycle_amplitude": quantity(0.15, "rad"),
        "knee_cycle_count": 2,
        "trajectory": "smooth",
        "knee_targets": {"front": quantity(0.15, "rad")},
        "compensating_hip_targets": {"front": quantity(-0.075, "rad")},
        "phase_durations": {
            "stand": quantity(2, "s"),
            "cycle_1": quantity(2, "s"),
            "cycle_2": quantity(2, "s"),
            "recover": quantity(2, "s"),
        },
        "max_abs_roll_pitch": quantity(0.1, "rad"),
        "random_seed": 7,
        "simulated_time": quantity(8, "s"),
        "require_nonzero_sensing": True,
        "require_nonzero_actuation": True,
        "require_actual_feedback": True,
        "require_controller_disabled_negative": True,
        "acceptance_limit_ids": ["tracking", "contact", "attitude"],
        "provenance": PROVENANCE,
    }
    assert AnymalScenario.model_validate(anymal).knee_cycle_count == 2


def test_simulation_graph_requires_real_feedback_fixed_time_and_fixed_x500_limits():
    artifact = {
        "role": "bundled_input",
        "ref": {"path": "bundle/input.sdf", "sha256": HASH_A},
        "format": "sdf",
        "provenance": PROVENANCE,
    }
    controller_artifact = deepcopy(artifact)
    controller_artifact["format"] = "controller_config"
    controller_artifact["ref"]["path"] = "bundle/controller.yaml"
    scenario = {
        "id": "flight",
        "kind": "x500_takeoff_hover_land",
        "plant_id": "x500",
        "controller_id": "px4",
        "negative_controller_id": "off",
        "world_id": "world",
        "physics_id": "physics",
        "runtime_id": "gazebo",
        "initial_state_id": "initial",
        "target_altitude": quantity(2, "m"),
        "settled_hover_duration": quantity(10, "s"),
        "deadline": quantity(90, "s"),
        "phase_durations": {
            "takeoff": quantity(10, "s"),
            "settle": quantity(10, "s"),
            "hover": quantity(10, "s"),
            "land": quantity(30, "s"),
        },
        "random_seed": 7,
        "simulated_time": quantity(60, "s"),
        "require_nonzero_sensing": True,
        "require_nonzero_actuation": True,
        "require_actual_feedback": True,
        "require_controller_disabled_negative": True,
        "acceptance_limit_ids": ["rms", "peak", "horizontal", "landing"],
        "provenance": PROVENANCE,
    }
    simulation = {
        "version": "1",
        "worlds": [
            {
                "id": "world",
                "artifact": artifact,
                "root_frame": "world",
                "gravity": {"values": [0, 0, -9.81], "unit": "m/s^2", "frame": "world"},
                "provenance": PROVENANCE,
            }
        ],
        "plants": [
            {
                "id": "x500",
                "kind": "px4_x500",
                "artifact": artifact,
                "source_sha256": HASH_A,
                "structure_hash": HASH_B,
                "base_frame": "body",
                "has_collisions": True,
                "has_verified_inertials": True,
                "interfaces": [
                    {"id": "cmd", "role": "command", "unit": "1", "frame": "body"},
                    {
                        "id": "state",
                        "role": "state_feedback",
                        "unit": "m",
                        "frame": "world",
                        "independently_measured": True,
                    },
                    {"id": "imu", "role": "sensor", "unit": "m/s^2", "frame": "body"},
                    {"id": "motor", "role": "actuator", "unit": "N", "frame": "body"},
                ],
                "damping_mode": "undamped",
                "provenance": PROVENANCE,
            }
        ],
        "initial_states": [
            {
                "id": "initial",
                "plant_id": "x500",
                "position": {"values": [0, 0, 0], "unit": "m", "frame": "world"},
                "orientation_xyzw": [0, 0, 0, 1],
                "linear_velocity": {
                    "values": [0, 0, 0],
                    "unit": "m/s",
                    "frame": "world",
                },
                "angular_velocity": {
                    "values": [0, 0, 0],
                    "unit": "rad/s",
                    "frame": "world",
                },
                "provenance": PROVENANCE,
            }
        ],
        "controllers": [
            {
                "id": "px4",
                "kind": "px4_sitl",
                "config": controller_artifact,
                "admitted_identity": "px4_sitl",
                "update_period": quantity(10, "ms"),
                "command_interfaces": ["cmd"],
                "state_interfaces": ["state"],
                "implicit_hold": False,
                "provenance": PROVENANCE,
            },
            {
                "id": "off",
                "kind": "disabled",
                "implicit_hold": False,
                "provenance": PROVENANCE,
            },
        ],
        "physics": [
            {
                "id": "physics",
                "engine_profile": "gz-harmonic",
                "max_step_size": quantity(1, "ms"),
                "solver_iterations": 50,
                "real_time_factor": 1,
                "collision_contact_enabled": True,
                "random_seed": 7,
                "simulated_time": quantity(60, "s"),
                "provenance": PROVENANCE,
            }
        ],
        "runtimes": [
            {
                "id": "gazebo",
                "package_profile": "gazebo-harmonic-px4",
                "max_wall_time": quantity(120, "s"),
                "provenance": PROVENANCE,
            }
        ],
        "scenarios": [scenario],
        "acceptance_limits": [
            {
                "id": "rms",
                "kind": "x500_position",
                "metric": "hover_altitude_rms",
                "threshold": quantity(0.2, "m"),
                "provenance": PROVENANCE,
            },
            {
                "id": "peak",
                "kind": "x500_position",
                "metric": "hover_altitude_peak",
                "threshold": quantity(0.5, "m"),
                "provenance": PROVENANCE,
            },
            {
                "id": "horizontal",
                "kind": "x500_position",
                "metric": "horizontal_displacement",
                "threshold": quantity(0.5, "m"),
                "provenance": PROVENANCE,
            },
            {
                "id": "landing",
                "kind": "x500_land_disarm",
                "deadline": quantity(90, "s"),
                "require_landed": True,
                "require_disarmed": True,
                "provenance": PROVENANCE,
            },
        ],
        "provenance": PROVENANCE,
    }
    contract = Contract.model_validate(
        {"schema_version": "0.4", "robot": {"name": "x500"}, "simulation": simulation}
    )
    assert contract.simulation.scenarios[0].require_actual_feedback is True

    broken = deepcopy(simulation)
    broken["controllers"][0]["state_interfaces"] = ["cmd"]
    with pytest.raises(ValidationError, match="independent plant feedback"):
        Contract.model_validate(
            {"schema_version": "0.4", "robot": {"name": "x500"}, "simulation": broken}
        )


def test_analysis_preparation_is_hash_bound_allowlisted_and_never_starts_solver(
    tmp_path, monkeypatch
):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    model = evidence / "robot.urdf"
    model.write_text('<robot name="fixture"><link name="arm"/></robot>')
    source_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    document = structural_document(source_sha256=source_hash)
    document["structural"]["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": "robot.urdf", "sha256": source_hash},
        "format": "urdf",
        "provenance": PROVENANCE,
    }
    contract_path = tmp_path / "robot.lockwire.yaml"
    save_contract(Contract.model_validate(document), contract_path)
    revision = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "package.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "1",
                "profiles": {
                    "ccx-2.23": {
                        "backend": "calculix_ccx",
                        "solver_version": "2.23",
                        "offline": True,
                        "admitted_procedures": ["linear_static"],
                        "procedure_templates": {"linear_static": "linear-static-v1"},
                        "allowed_artifact_formats": ["urdf"],
                    },
                    "ccx-other": {
                        "backend": "calculix_ccx",
                        "solver_version": "2.23",
                        "offline": True,
                        "admitted_procedures": ["linear_static"],
                        "procedure_templates": {"linear_static": "linear-static-other"},
                        "allowed_artifact_formats": ["urdf"],
                    },
                },
            }
        )
    )
    with pytest.raises(AnalysisRefused, match="revision changed"):
        prepare_analysis(
            contract_path,
            expected_revision=HASH_B,
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest_path,
        )
    result = prepare_analysis(
        contract_path,
        expected_revision=revision,
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest_path,
    )
    assert result["status"] == "PREPARED"
    assert result["solver_started"] is False
    assert result["physical_conformance"] == "NOT_SCORED"

    original_raw = contract_path.read_bytes()
    replacement = deepcopy(document)
    replacement["structural"]["runtime"]["package_profile"] = "ccx-other"
    replacement_path = tmp_path / "replacement.lockwire.yaml"
    save_contract(Contract.model_validate(replacement), replacement_path)
    replacement_raw = replacement_path.read_bytes()
    replaced = False
    original_read_bytes = Path.read_bytes

    def replace_after_bound_read(path, *args, **kwargs):
        nonlocal replaced
        value = original_read_bytes(path, *args, **kwargs)
        if Path(path) == contract_path and not replaced:
            replaced = True
            contract_path.write_bytes(replacement_raw)
        return value

    monkeypatch.setattr(Path, "read_bytes", replace_after_bound_read)
    bound = prepare_analysis(
        contract_path,
        expected_revision=revision,
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest_path,
    )
    assert replaced is True
    assert bound["contract_revision"] == hashlib.sha256(original_raw).hexdigest()
    assert bound["runtime_profile"] == "ccx-2.23"

    monkeypatch.undo()

    model.write_text('<robot name="fixture"><link name="arm"/><plugin filename="evil.so"/></robot>')
    evil_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    evil_document = structural_document(source_sha256=evil_hash)
    evil_document["structural"]["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": "robot.urdf", "sha256": evil_hash},
        "format": "urdf",
        "provenance": PROVENANCE,
    }
    save_contract(Contract.model_validate(evil_document), contract_path)
    evil_revision = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    with pytest.raises(AnalysisRefused, match="not admitted"):
        prepare_analysis(
            contract_path,
            expected_revision=evil_revision,
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest_path,
        )


def test_analysis_main_preserves_prior_proposal_on_refusal_and_write_failure(
    tmp_path, monkeypatch, capsys
):
    contract_path = tmp_path / "robot.lockwire.yaml"
    contract_path.write_text("robot: {name: fixture}\n", encoding="utf-8")
    document_path = tmp_path / "proposal.json"
    prior = b'{"status": "PRIOR"}\n'
    document_path.write_bytes(prior)
    argv = [
        "--contract",
        str(contract_path),
        "--expected-revision",
        HASH_B,
        "--procedure",
        "static",
        "--evidence-root",
        str(tmp_path),
        "--package-manifest",
        str(tmp_path / "package.json"),
        "--document",
        str(document_path),
    ]
    assert analysis.main(argv) == 2
    assert document_path.read_bytes() == prior
    assert capsys.readouterr().err.startswith("analysis refused: ")

    monkeypatch.setattr(
        analysis,
        "prepare_analysis",
        lambda *_args, **_kwargs: {"status": "PREPARED", "deck_template": None},
    )
    monkeypatch.setattr(
        analysis.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("no replace"))
    )
    with pytest.raises(OSError, match="no replace"):
        analysis.main(argv)
    assert document_path.read_bytes() == prior


def test_analysis_main_bounds_refusal_on_error_channel(tmp_path, monkeypatch, capsys):
    contract_path = tmp_path / "robot.lockwire.yaml"
    contract_path.write_text("robot: {name: fixture}\n", encoding="utf-8")
    monkeypatch.setattr(
        analysis,
        "execute_direct_truss",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AnalysisRefused("first line\n" + "x" * 1024)
        ),
    )
    argv = [
        "--contract",
        str(contract_path),
        "--expected-revision",
        HASH_B,
        "--procedure",
        "static",
        "--evidence-root",
        str(tmp_path),
        "--package-manifest",
        str(tmp_path / "package.json"),
        "--document",
        str(tmp_path / "proposal.json"),
    ]
    assert analysis.main(argv) == 2
    error = capsys.readouterr().err
    assert error.startswith("analysis refused: first line ")
    assert error.count("\n") == 1
    assert len(error) <= len("analysis refused: ") + 512 + 1


def test_analysis_argv_preserves_hostile_values_as_single_arguments(tmp_path):
    hostile = tmp_path / "contract; touch never"
    argv = build_analysis_argv(
        hostile,
        expected_revision=HASH_A,
        procedure_id="static; echo no",
        evidence_root=tmp_path,
        package_manifest_path=tmp_path / "package.json",
        document_path=tmp_path / "document.json",
    )
    assert argv[argv.index("--contract") + 1] == str(hostile)
    assert argv[argv.index("--procedure") + 1] == "static; echo no"


def test_frozen_analysis_and_owner_use_sidecar_child_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis.sys, "frozen", True, raising=False)
    argv = build_analysis_argv(
        tmp_path / "contract.yaml",
        expected_revision=HASH_A,
        procedure_id="static",
        evidence_root=tmp_path,
        package_manifest_path=tmp_path / "package.json",
        document_path=tmp_path / "document.json",
    )
    assert argv[:2] == [sys.executable, "--internal-analysis"]
    assert analysis._ccx_owner_argv(17, tmp_path / "ccx_2.23", tmp_path) == [
        sys.executable,
        "--internal-ccx-owner",
        "17",
        str(tmp_path / "ccx_2.23"),
        str(tmp_path),
    ]


@pytest.mark.parametrize(
    "mode,module",
    [
        ("--internal-analysis", "lockwire_pro.analysis"),
        ("--internal-ccx-owner", "lockwire_pro._ccx_owner"),
    ],
)
def test_frozen_sidecar_dispatches_its_private_children(monkeypatch, mode, module):
    calls = []
    monkeypatch.setattr(
        importlib.import_module(module), "main", lambda args: calls.append(args) or 37
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["lockwire-sidecar", mode, "bound-arg"])
    entry = Path(__file__).parent.parent / "packaging/pro/lockwire_entry.py"
    with pytest.raises(SystemExit, match="37"):
        runpy.run_path(str(entry), run_name="__main__")
    assert calls == [["bound-arg"]]


def test_direct_truss_template_rejects_unadmitted_deck_before_solver(tmp_path):
    deck = Path(__file__).parent / "fixtures/calculix_benchmarks/rotated_truss.inp"
    good = deck.read_bytes()
    assert analysis._admit_direct_truss_deck(good).requests == {
        ("U", "TIP", 2),
        ("U", "ROOT", 1),
        ("RF", "ROOT", 1),
    }
    with pytest.raises(AnalysisRefused, match="not admitted"):
        analysis._admit_direct_truss_deck(good.replace(b"*END STEP", b"*DYNAMIC\n*END STEP"))
    with pytest.raises(AnalysisRefused, match="nonfinite|finite decimal"):
        analysis._admit_direct_truss_deck(good.replace(b"1, 0., 0., 0.", b"1, NaN, 0., 0."))


def test_node_print_requires_unique_finite_closed_results():
    result = "S T E P 1\nINCREMENT 1\n displacements (vx,vy,vz) for set TIP\n 2 1.0E+00 0.0E+00 0.0E+00\n"
    assert analysis.parse_linear_static_node_print(result) == {("U", "TIP", 2): (1.0, 0.0, 0.0)}
    with pytest.raises(AnalysisRefused, match="duplicate"):
        analysis.parse_linear_static_node_print(result + " 2 1.0E+00 0.0E+00 0.0E+00\n")
    with pytest.raises(AnalysisRefused, match="malformed"):
        analysis.parse_linear_static_node_print(result.replace("1.0E+00", "NaN"))


DIRECT_DECLARED_TRUSS_DECK = """*HEADING
*NODE
1, 0, 0, 0
2, 5, 0, 0
*ELEMENT, TYPE=T3D2, ELSET=TRUSS
1, 1, 2
*NSET, NSET=ROOT
1
*NSET, NSET=TIP
2
*MATERIAL, NAME=STEEL
*ELASTIC
200000000000, 0.3
*SOLID SECTION, ELSET=TRUSS, MATERIAL=STEEL
0.001
*BOUNDARY
ROOT, 1, 3
TIP, 2, 3
*STEP
*STATIC
*CLOAD
TIP, 1, 10000
*NODE PRINT, NSET=TIP
U
*NODE PRINT, NSET=ROOT
RF
*END STEP
"""


def _declared_truss_document(digest):
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["materials"][0]["id"] = "steel"
    structural["sections"][0]["area"] = quantity(0.001, "m^2")
    structural["assignments"][0]["region"] = {"kind": "element_set", "name": "TRUSS"}
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": "truss.inp", "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["supports"] = [
        {
            "id": "root",
            "region": {"kind": "node_set", "name": "ROOT"},
            "dofs": {"translation": ["x", "y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        },
        {
            "id": "tip",
            "region": {"kind": "node_set", "name": "TIP"},
            "dofs": {"translation": ["y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        },
    ]
    structural["loads"] = [
        {
            "id": "tip-force",
            "kind": "force",
            "region": {"kind": "node_set", "name": "TIP"},
            "vector": {"values": [10000, 0, 0], "unit": "N", "frame": "base"},
            "application_frame": "base",
            "provenance": PROVENANCE,
        },
    ]
    structural["procedures"][0]["support_ids"] = ["root", "tip"]
    structural["procedures"][0]["load_ids"] = ["tip-force"]
    return document


def _prepared_declared_truss(tmp_path, deck_text=DIRECT_DECLARED_TRUSS_DECK):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    deck = evidence / "truss.inp"
    deck.write_text(deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    contract = tmp_path / "robot.lockwire.yaml"
    save_contract(Contract.model_validate(_declared_truss_document(digest)), contract)
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
                        "procedure_templates": {"linear_static": "direct-truss-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def _prepared_declared_modal_truss(
    tmp_path, deck_text=None, *, source_name="truss_modal_refined_8.inp", density=1
):
    source = Path(__file__).parent / "fixtures/calculix_dynamics" / source_name
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    deck = evidence / "modal.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["materials"][0]["id"] = "bar_material"
    structural["materials"][0]["youngs_modulus"] = quantity(100, "Pa")
    structural["materials"][0]["density"] = quantity(density, "kg/m^3")
    structural["sections"][0]["material_id"] = "bar_material"
    structural["sections"][0]["area"] = quantity(1e-4, "m^2")
    structural["assignments"][0]["region"] = {"kind": "element_set", "name": "BAR"}
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["supports"] = [
        {
            "id": "root",
            "region": {"kind": "node_set", "name": "ROOT"},
            "dofs": {"translation": ["x", "y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        },
        {
            "id": "axial",
            "region": {"kind": "node_set", "name": "AXIAL_NODES"},
            "dofs": {"translation": ["y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        },
    ]
    structural["procedures"] = [
        {
            "id": "modal",
            "kind": "modal",
            "assignment_ids": ["arm-elements"],
            "support_ids": ["root", "axial"],
            "mode_count": 1,
            "damping_mode": "undamped",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "admitted_procedures": ["modal"],
                        "undamped_procedures": ["modal"],
                        "procedure_templates": {"modal": "direct-modal-truss-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def _prepared_two_dof_modal(tmp_path, deck_text=None):
    source = Path(__file__).parent / "fixtures/calculix_dynamics/two_dof_modal_reference.inp"
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / "two-dof-modal.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["masses"] = [
        {
            "id": "oscillator-mass",
            "region": {"kind": "model_link", "name": "arm"},
            "mass": quantity(1, "kg"),
            "provenance": PROVENANCE,
        }
    ]
    structural["acceptance_limits"] = [
        {
            "id": "modal-analytic",
            "kind": "scalar_reference",
            "metric": "two_dof_modal_frequency",
            "comparator": "relative",
            "threshold": quantity(5, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    structural["procedures"] = [
        {
            "id": "two-dof-modal",
            "kind": "modal",
            "assignment_ids": ["arm-elements"],
            "mass_ids": ["oscillator-mass"],
            "mode_count": 1,
            "damping_mode": "undamped",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "admitted_procedures": ["modal"],
                        "undamped_procedures": ["modal"],
                        "procedure_templates": {"modal": "direct-two-dof-modal-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def _prepared_two_dof_response(tmp_path, kind, deck_text=None, threshold=None):
    source_name = {
        "harmonic": "two_dof_modal_harmonic.inp",
        "transient": "two_dof_modal_transient_damped.inp",
    }[kind]
    source = Path(__file__).parent / "fixtures/calculix_dynamics" / source_name
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / f"two-dof-{kind}.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["masses"] = [
        {
            "id": "oscillator-mass",
            "region": {"kind": "model_link", "name": "arm"},
            "mass": quantity(1, "kg"),
            "provenance": PROVENANCE,
        }
    ]
    structural["loads"] = [
        {
            "id": "oscillator-force",
            "kind": "force",
            "region": {"kind": "model_link", "name": "arm"},
            "vector": {"values": [1, 0, 0], "unit": "N", "frame": "base"},
            "application_frame": "base",
            "provenance": PROVENANCE,
        }
    ]
    structural["damping"] = [
        {
            "id": "rayleigh",
            "kind": "rayleigh",
            "rayleigh_alpha": quantity(1, "1/s"),
            "rayleigh_beta": quantity(0, "s"),
            "provenance": PROVENANCE,
        }
    ]
    procedure = {
        "id": f"two-dof-{kind}",
        "kind": kind,
        "assignment_ids": ["arm-elements"],
        "mass_ids": ["oscillator-mass"],
        "load_ids": ["oscillator-force"],
        "damping_mode": "declared",
        "damping_ids": ["rayleigh"],
        "provenance": PROVENANCE,
    }
    if kind == "harmonic":
        procedure["frequency"] = quantity(0.7957747154594768, "Hz")
        metric = "two_dof_harmonic_amplitude"
    else:
        procedure["duration"] = quantity(0.01, "s")
        procedure["time_step"] = quantity(0.001, "s")
        metric = "two_dof_transient_response"
    structural["procedures"] = [procedure]
    structural["acceptance_limits"] = [
        {
            "id": f"{kind}-analytic",
            "kind": "scalar_reference",
            "metric": metric,
            "comparator": "relative",
            "threshold": threshold or quantity(5, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "admitted_procedures": [kind],
                        "procedure_templates": {kind: f"direct-two-dof-{kind}-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def _fake_two_dof_response_ccx(tmp_path, monkeypatch, captured_name):
    captured = Path(__file__).parent / "fixtures/calculix_dynamics/raw" / captured_name
    binary = tmp_path / "ccx_2.23"
    binary.write_text(f"#!/bin/sh\ncp {captured} model.dat\n")
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("kind", "captured_name", "captured_sha256", "expected_result_kind", "expected_count"),
    [
        (
            "harmonic",
            "two_dof_harmonic.dat",
            "7f715452514ab4db11426b283cca739d2d3c4b9916455e843a4da61c60d8228c",
            "harmonic_displacement_amplitude",
            1,
        ),
        (
            "transient",
            "two_dof_transient.dat",
            "798652ecac8e0fb7bcd5a3fbc3f780f5fa3dc13fab7ca29f1c202e3a52c8d245",
            "transient_response",
            2,
        ),
    ],
)
def test_two_dof_dynamic_response_scores_retained_ccx_output(
    tmp_path,
    monkeypatch,
    kind,
    captured_name,
    captured_sha256,
    expected_result_kind,
    expected_count,
):
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, kind)
    _fake_two_dof_response_ccx(tmp_path, monkeypatch, captured_name)
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id=f"two-dof-{kind}",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert (result["status"], result["solver_started"], result["simulation_validity"]) == (
        "COMPLETED",
        True,
        "GO",
    )
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["output_sha256"] == captured_sha256
    assert len(result["results"]) == expected_count
    assert all(row["kind"] == expected_result_kind for row in result["results"])
    assert len(result["comparisons"]) == (1 if kind == "harmonic" else 4)
    assert all(row["passed"] for row in result["comparisons"])


def test_two_dof_dynamic_response_refuses_changed_damping_deck(tmp_path):
    source = Path(__file__).parent / "fixtures/calculix_dynamics/two_dof_modal_transient_damped.inp"
    changed = source.read_text().replace(",,1.,0.", ",,0.,0.")
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, "transient", changed)
    with pytest.raises(AnalysisRefused, match="differs from the retained source"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-transient",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("damping", "rayleigh_alpha", quantity(0, "1/s")),
        ("masses", "mass", quantity(2, "kg")),
    ],
)
def test_two_dof_dynamic_response_refuses_changed_declared_operand(tmp_path, section, field, value):
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, "transient")
    document = analysis.load_contract_bytes(contract.read_bytes()).model_dump(mode="json")
    document["structural"][section][0][field] = value
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="mass, load, or damping differs"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-transient",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("rayleigh_alpha", quantity(1, "s"), "Rayleigh alpha requires dimension 'inverse_time'"),
        ("rayleigh_beta", quantity(1, "1/s"), "time quantity requires dimension 'time'"),
    ],
)
def test_two_dof_dynamic_response_rejects_wrong_dimension_rayleigh_damping(
    tmp_path, field, value, match
):
    contract, _, _ = _prepared_two_dof_response(tmp_path, "transient")
    document = analysis.load_contract_bytes(contract.read_bytes()).model_dump(mode="json")
    document["structural"]["damping"][0][field] = value
    with pytest.raises(ValidationError, match=match):
        Contract.model_validate(document)


def test_two_dof_dynamic_response_refuses_missing_velocity_row(tmp_path, monkeypatch):
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, "transient")
    captured = Path(__file__).parent / "fixtures/calculix_dynamics/raw/two_dof_transient.dat"
    broken = tmp_path / "missing-velocity.dat"
    broken.write_text(
        captured.read_text().replace("velocities (vx,vy,vz)", "velocity (vx,vy,vz)", 1)
    )
    binary = tmp_path / "ccx_2.23"
    binary.write_text(f"#!/bin/sh\ncp {broken} model.dat\n")
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    with pytest.raises(AnalysisRefused, match="retained response time grid"):
        analysis.execute_direct_truss(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-transient",
            evidence_root=evidence,
            package_manifest_path=manifest,
            work_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("kind", "captured_name", "mutate", "reason"),
    [
        (
            "harmonic",
            "two_dof_harmonic.dat",
            lambda text: text.replace(
                "velocities (vx,vy,vz) for set OSCILLATOR and time  0.7957747E+00",
                "velocities (vx,vy,vz) for set OSCILLATOR and time  0.9000000E+00",
                1,
            ),
            "inconsistent or undeclared response frequency",
        ),
        (
            "transient",
            "two_dof_transient.dat",
            lambda text: (
                text
                + "\n displacements (vx,vy,vz) for set OSCILLATOR and time  0.1500000E-02\n\n"
                + "         1  1.000000E-06 0.000000E+00 0.000000E+00\n\n"
                + " velocities (vx,vy,vz) for set OSCILLATOR and time  0.1500000E-02\n\n"
                + "         1  1.000000E-03 0.000000E+00 0.000000E+00\n"
            ),
            "missing, off-grid, or zero response",
        ),
    ],
)
def test_two_dof_dynamic_response_refuses_mismatched_frequency_or_off_grid_sample(
    tmp_path, monkeypatch, kind, captured_name, mutate, reason
):
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, kind)
    captured = Path(__file__).parent / "fixtures/calculix_dynamics/raw" / captured_name
    broken = tmp_path / f"broken-{kind}.dat"
    broken.write_text(mutate(captured.read_text()))
    binary = tmp_path / "ccx_2.23"
    binary.write_text(f"#!/bin/sh\ncp {broken} model.dat\n")
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    with pytest.raises(AnalysisRefused, match=reason):
        analysis.execute_direct_truss(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id=f"two-dof-{kind}",
            evidence_root=evidence,
            package_manifest_path=manifest,
            work_dir=tmp_path,
        )


@pytest.mark.parametrize(("kind", "expected_count"), [("harmonic", 1), ("transient", 2)])
def test_real_pinned_ccx_job_scores_two_dof_dynamic_response(
    tmp_path, monkeypatch, kind, expected_count
):
    source_root = Path(__file__).parents[1] / "src"
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(source_root) + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
    )
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, kind)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id=f"two-dof-{kind}",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=130)
    assert (
        result["kind"],
        result["status"],
        result["solver_started"],
        result["simulation_validity"],
    ) == ("analysis_result", "COMPLETED", True, "GO")
    assert result["physical_conformance"] == "NOT_SCORED"
    assert len(result["results"]) == expected_count
    assert all(row["passed"] for row in result["comparisons"])
    assert job.exit_code == 0
    assert not job.work_dir.exists()


def test_real_pinned_ccx_job_scores_two_dof_modal_frequency(tmp_path, monkeypatch):
    source_root = Path(__file__).parents[1] / "src"
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(source_root) + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
    )
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_two_dof_modal(tmp_path)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="two-dof-modal",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=130)
    assert (result["kind"], result["status"], result["solver_started"]) == (
        "analysis_result",
        "COMPLETED",
        True,
    )
    assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
    assert result["results"] == [
        {
            "kind": "modal_frequency",
            "mode": 1,
            "value": pytest.approx(1.5915494309189535),
            "unit": "Hz",
        }
    ]
    assert (result["physical_conformance"], result["simulation_validity"]) == ("NOT_SCORED", "GO")
    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["metric"] == "two_dof_modal_frequency"
    assert result["comparisons"][0]["passed"] is True
    assert hashlib.sha256(result["output_text"].encode()).hexdigest() == result["output_sha256"]
    assert job.exit_code == 0
    assert not job.work_dir.exists()


def test_two_dof_modal_public_job_preparation_binds_exact_source(tmp_path):
    contract, evidence, manifest = _prepared_two_dof_modal(tmp_path)
    prepared = prepare_analysis(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="two-dof-modal",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert prepared["deck_template"] == "direct-two-dof-modal-v1"
    assert prepared["physical_conformance"] == "NOT_SCORED"
    assert prepared["simulation_validity"] == "NOT_SCORED"

    altered = (evidence / "two-dof-modal.inp").read_text().replace("10000.", "1000.")
    contract, evidence, manifest = _prepared_two_dof_modal(tmp_path / "altered", altered)
    with pytest.raises(AnalysisRefused, match="differs from the retained source"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-modal",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


@pytest.mark.parametrize(
    ("section", "field", "value", "reason"),
    [
        (
            "acceptance_limits",
            None,
            [],
            "requires one positive relative 'two_dof_modal_frequency' limit",
        ),
        ("procedures", "mass_ids", [], "requires one declared 1 kg mass"),
        ("masses", "mass", quantity(2, "kg"), "mass differs from retained source"),
    ],
)
def test_two_dof_modal_refuses_missing_or_wrong_declared_mass(
    tmp_path, section, field, value, reason
):
    contract, evidence, manifest = _prepared_two_dof_modal(tmp_path)
    document = load_contract_bytes(contract.read_bytes()).model_dump(mode="json")
    if field is None:
        document["structural"][section] = value
    else:
        document["structural"][section][0][field] = value
    if section == "procedures":
        with pytest.raises(ValidationError, match="lacks density or explicit mass"):
            Contract.model_validate(document)
        return
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match=reason):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-modal",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


@pytest.mark.parametrize(
    ("source_name", "density", "captured_name", "expected_frequency"),
    [
        ("truss_modal_refined_8.inp", 1, "truss_modal_refined_8.dat", 2.499836),
        ("truss_modal_4rho_refined_8.inp", 4, "truss_modal_4rho_refined_8.dat", 1.249918),
    ],
)
def test_direct_modal_truss_job_records_measured_frequency_without_scoring(
    tmp_path, monkeypatch, source_name, density, captured_name, expected_frequency
):
    contract, evidence, manifest = _prepared_declared_modal_truss(
        tmp_path, source_name=source_name, density=density
    )
    captured = Path(__file__).parent / "fixtures/calculix_dynamics/raw" / captured_name
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        f"#!/bin/sh\ncp {captured} model.dat\nprintf modal-eigen-artifact > model.eig\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="modal",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert result["results"] == [
        {"kind": "modal_frequency", "mode": 1, "value": expected_frequency, "unit": "Hz"}
    ]
    assert result["simulation_validity"] == "NOT_SCORED"
    assert "no supported modal acceptance metric" in result["reason"]
    assert (
        result["analytic_operands"]["eigen_artifact_sha256"]
        == hashlib.sha256(b"modal-eigen-artifact").hexdigest()
    )


def test_real_pinned_ccx_job_records_the_2_5_hz_truss_axial_frequency(tmp_path, monkeypatch):
    """Keep the 2.5 Hz truss claim, through the Job path with the real pinned ccx."""
    source_root = Path(__file__).parents[1] / "src"
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(source_root) + (os.pathsep + inherited_pythonpath if inherited_pythonpath else ""),
    )
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_declared_modal_truss(tmp_path)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="modal",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=130)
    assert (result["kind"], result["status"], result["solver_started"]) == (
        "analysis_result",
        "COMPLETED",
        True,
    )
    row = result["results"][0]
    assert row["kind"] == "modal_frequency"
    assert abs(row["value"] - 2.5) / 2.5 < 0.02
    assert job.exit_code == 0
    assert not job.work_dir.exists()


def test_direct_modal_truss_refuses_missing_modal_row(tmp_path, monkeypatch):
    contract, evidence, manifest = _prepared_declared_modal_truss(tmp_path)
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        "#!/bin/sh\n"
        "printf ' MODE NO    EIGENVALUE                       FREQUENCY\\n PARTICIPATION FACTORS\\n' > model.dat\n"
        "printf modal-eigen-artifact > model.eig\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    with pytest.raises(AnalysisRefused, match="modal eigenvalue output is missing"):
        analysis.execute_direct_truss(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="modal",
            evidence_root=evidence,
            package_manifest_path=manifest,
            work_dir=tmp_path,
        )


def test_modal_frequency_parser_requires_retained_ccx_completion_marker():
    captured = (
        Path(__file__).parent / "fixtures/calculix_dynamics/raw/truss_modal_refined_8.dat"
    ).read_text()
    rows = analysis.parse_modal_frequencies(captured)
    assert rows[0][0] == 1
    assert rows[0][1] == pytest.approx(2.499836)
    marker = "     P A R T I C I P A T I O N   F A C T O R S\n"
    marker_index = captured.index(marker)
    truncated = captured[:marker_index]
    with pytest.raises(AnalysisRefused, match="incomplete modal eigenvalue table"):
        analysis.parse_modal_frequencies(truncated)


def test_direct_modal_truss_refuses_overlapping_deck_boundary_records(tmp_path):
    source = (
        Path(__file__).parent / "fixtures/calculix_dynamics/truss_modal_refined_8.inp"
    ).read_text()
    boundary = "ROOT, 1, 3\n"
    assert source.count(boundary) == 1
    overlapping = source.replace(boundary, "ROOT, 1, 2\nROOT, 2, 3\n")
    contract, evidence, manifest = _prepared_declared_modal_truss(tmp_path, overlapping)
    with pytest.raises(AnalysisRefused, match="deck boundaries differ from declaration"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="modal",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_direct_truss_preparation_rejects_unsupported_deck(tmp_path):
    contract, evidence, manifest = _prepared_declared_truss(
        tmp_path, DIRECT_DECLARED_TRUSS_DECK.replace("*END STEP", "*DYNAMIC\n*END STEP")
    )
    with pytest.raises(AnalysisRefused, match="not admitted"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_direct_truss_preparation_binds_material_area_support_and_load(tmp_path):
    contract, evidence, manifest = _prepared_declared_truss(tmp_path)
    revision = hashlib.sha256(contract.read_bytes()).hexdigest()
    prepared = prepare_analysis(
        contract,
        expected_revision=revision,
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert (prepared["status"], prepared["solver_started"], prepared["physical_conformance"]) == (
        "PREPARED",
        False,
        "NOT_SCORED",
    )
    original = _declared_truss_document(
        hashlib.sha256((evidence / "truss.inp").read_bytes()).hexdigest()
    )
    for field, expected in (
        ("area", "area differs"),
        ("modulus", "material differs"),
        ("support", "boundaries differ"),
        ("load", "loads differ"),
        ("connection", "one bound static truss"),
    ):
        changed = deepcopy(original)
        structural = changed["structural"]
        if field == "area":
            structural["sections"][0]["area"] = quantity(0.01, "m^2")
        elif field == "modulus":
            structural["materials"][0]["youngs_modulus"] = quantity(100, "GPa")
        elif field == "support":
            structural["supports"][1]["dofs"]["translation"] = ["z"]
        elif field == "load":
            structural["loads"][0]["vector"]["values"] = [9000, 0, 0]
        else:
            structural["connections"] = [
                {
                    "id": "joint",
                    "kind": "rigid",
                    "endpoint_a": {"kind": "node_set", "name": "ROOT"},
                    "endpoint_b": {"kind": "node_set", "name": "TIP"},
                    "dofs": {"translation": ["x"], "frame": "base"},
                    "provenance": PROVENANCE,
                }
            ]
        save_contract(Contract.model_validate(changed), contract)
        with pytest.raises(AnalysisRefused, match=expected):
            prepare_analysis(
                contract,
                expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
                procedure_id="static",
                evidence_root=evidence,
                package_manifest_path=manifest,
            )


def test_declared_truss_job_refuses_without_pinned_solver(tmp_path, monkeypatch):
    contract, evidence, manifest = _prepared_declared_truss(tmp_path)
    monkeypatch.delenv("LOCKWIRE_CCX", raising=False)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    with pytest.raises(RuntimeError, match="analysis job failed"):
        job.result(timeout=15)
    assert job.exit_code == 2
    assert not job.work_dir.exists()


def test_direct_truss_runs_pinned_solver_and_binds_complete_output(tmp_path, monkeypatch):
    contract, evidence, manifest = _prepared_declared_truss(tmp_path)
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        "#!/bin/sh\n"
        "cat > model.dat <<'EOF'\n"
        " displacements (vx,vy,vz) for set TIP and time 1.0\n"
        " 2 2.500000E-04 0.000000E+00 0.000000E+00\n"
        " forces (fx,fy,fz) for set ROOT and time 1.0\n"
        " 1 -1.000000E+04 0.000000E+00 0.000000E+00\n"
        "EOF\n"
    )
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(analysis, "_PACKAGED_CCX_SHA256", digest)
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert (result["kind"], result["status"], result["solver_started"]) == (
        "analysis_result",
        "COMPLETED",
        True,
    )
    assert result["solver"]["binary_sha256"] == digest
    assert (
        result["output_sha256"]
        == hashlib.sha256((tmp_path / "solver/model.dat").read_bytes()).hexdigest()
    )
    assert result["results"] == [
        {"kind": "RF", "node_set": "ROOT", "node": 1, "values": [-10000.0, 0.0, 0.0], "unit": "N"},
        {"kind": "U", "node_set": "TIP", "node": 2, "values": [0.00025, 0.0, 0.0], "unit": "m"},
    ]
    assert hashlib.sha256(result["output_text"].encode()).hexdigest() == result["output_sha256"]
    assert result["physical_conformance"] == "NOT_SCORED"


def test_verified_solver_copy_survives_source_replacement(tmp_path, monkeypatch):
    source = tmp_path / "ccx_2.23"
    source.write_bytes(b"trusted solver bytes")
    monkeypatch.setenv("LOCKWIRE_CCX", str(source))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied, digest = analysis._verified_ccx(private)
    source.write_bytes(b"replacement")
    assert copied.read_bytes() == b"trusted solver bytes"
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest
    assert copied.stat().st_mode & 0o777 == 0o500


def test_pinned_solver_copies_and_verifies_its_private_macos_libraries(tmp_path, monkeypatch):
    package = tmp_path / "package"
    binary_dir = package / "bin"
    library_dir = package / "lib"
    binary_dir.mkdir(parents=True)
    library_dir.mkdir()
    source = binary_dir / "ccx_2.23"
    source.write_bytes(b"pinned solver")
    libraries = {"libgfortran.5.dylib": b"fortran", "libquadmath.0.dylib": b"quadmath"}
    for name, content in libraries.items():
        (library_dir / name).write_bytes(content)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("LOCKWIRE_CCX", str(source))
    monkeypatch.setattr(analysis, "_PACKAGED_CCX_SHA256", digest)
    monkeypatch.setattr(analysis, "_MACOS_CCX_SHA256", digest)
    monkeypatch.setattr(
        analysis,
        "_MACOS_CCX_LIBRARIES",
        {
            name: (hashlib.sha256(content).hexdigest(), len(content))
            for name, content in libraries.items()
        },
    )
    private = tmp_path / "private"
    private.mkdir()
    copied, _ = analysis._verified_ccx(private)
    assert copied == private / "bin" / "ccx_2.23"
    for name, content in libraries.items():
        assert (private / "lib" / name).read_bytes() == content
    (library_dir / "libquadmath.0.dylib").write_bytes(b"tampered")
    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(AnalysisRefused, match="libquadmath.*hash differs"):
        analysis._verified_ccx(second)


def test_node_print_reader_refuses_symlink_and_oversize(tmp_path, monkeypatch):
    output = tmp_path / "model.dat"
    target = tmp_path / "target.dat"
    target.write_text("untrusted")
    output.symlink_to(target)
    with pytest.raises(AnalysisRefused, match="cannot be read"):
        analysis._read_node_print(output)
    output.unlink()
    monkeypatch.setattr(analysis, "_MAX_SOLVER_OUTPUT", 4)
    output.write_bytes(b"12345")
    with pytest.raises(AnalysisRefused, match="oversized"):
        analysis._read_node_print(output)


def _assert_no_live_pids(pids):
    for _ in range(40):
        states = [
            subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            for pid in pids
        ]
        if all(not state or state.startswith("Z") for state in states):
            return
        time.sleep(0.1)
    assert False, f"solver processes still live: {dict(zip(pids, states, strict=True))}"


def _resistant_solver(tmp_path):
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        "#!/bin/sh\ntrap '' TERM INT\nsleep 30 &\necho \"$$ $!\" > solver.pids\nwait\n"
    )
    binary.chmod(0o700)
    return binary


def test_solver_timeout_terminates_owned_process_group(tmp_path):
    binary = _resistant_solver(tmp_path)
    with pytest.raises(AnalysisRefused, match="timed out"):
        analysis._run_ccx(binary, tmp_path, timeout=0.1)
    marker = tmp_path / "solver.pids"
    if marker.exists():
        _assert_no_live_pids([int(pid) for pid in marker.read_text().split()])
    else:
        # The owner can exhaust this 0.1s budget before the shell writes its
        # marker. Prove neither owner nor copied command survived startup.
        ps = subprocess.run(["/bin/ps", "-axo", "state=,command="], capture_output=True, text=True)
        assert ps.returncode == 0
        assert not any(
            (str(binary) in row or ("lockwire_pro._ccx_owner" in row and str(tmp_path) in row))
            and not row.lstrip().startswith("Z")
            for row in ps.stdout.splitlines()
        )


def test_repeated_cancel_cannot_interrupt_solver_group_reap(tmp_path):
    binary = _resistant_solver(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from lockwire_pro.analysis import _run_ccx\n"
        "_run_ccx(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(analysis.__file__).parents[1])}
    # Fixed Python executable and test-owned files.
    process = subprocess.Popen(
        [sys.executable, str(runner), str(binary), str(tmp_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if (tmp_path / "solver.pids").exists():
                break
            if process.poll() is not None:
                pytest.fail("analysis child exited before launching resistant solver")
            time.sleep(0.05)
        else:
            pytest.fail("resistant solver did not start")
        pids = [int(pid) for pid in (tmp_path / "solver.pids").read_text().split()]
        os.kill(process.pid, signal.SIGTERM)
        time.sleep(0.1)
        if process.poll() is None:
            os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
        _assert_no_live_pids(pids)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_pinned_ccx_job_matches_direct_truss_reference(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_declared_truss(tmp_path)
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=130)
    assert result["kind"] == "analysis_result"
    assert result["status"] == "COMPLETED"
    assert result["solver_started"] is True
    assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
    assert hashlib.sha256(result["output_text"].encode()).hexdigest() == result["output_sha256"]
    rows = {(row["kind"], row["node_set"], row["node"]): row["values"] for row in result["results"]}
    assert rows[("U", "TIP", 2)][0] == pytest.approx(0.00025, rel=0.001)
    assert rows[("RF", "ROOT", 1)][0] == pytest.approx(-10000, rel=0.001)
    assert job.exit_code == 0
    assert not job.work_dir.exists()


def _actual_work_truss_deck() -> str:
    """The r11 2048 elements in today's declared, equation-free axial language."""
    segments = 2048
    rows = ["*HEADING", "*NODE"]
    rows.extend(f"{index + 1}, {5 * index / segments}, 0., 0." for index in range(segments + 1))
    rows.append("*ELEMENT, TYPE=T3D2, ELSET=TRUSS")
    rows.extend(f"{index}, {index}, {index + 1}" for index in range(1, segments + 1))
    rows.extend(["*NSET, NSET=ROOT", "1", "*NSET, NSET=TIP", str(segments + 1), "*NSET, NSET=FREE"])
    rows.extend(
        ", ".join(str(node) for node in range(first, min(first + 16, segments + 1)))
        for first in range(2, segments + 1, 16)
    )
    rows.extend(
        [
            "*MATERIAL, NAME=STEEL",
            "*ELASTIC",
            "200000000000, 0.3",
            "*SOLID SECTION, ELSET=TRUSS, MATERIAL=STEEL",
            "0.001",
            "*BOUNDARY",
            "ROOT, 1, 3",
            "TIP, 2, 3",
            "FREE, 2, 3",
            "*STEP",
            "*STATIC",
            "*CLOAD",
            "TIP, 1, 10000",
            "*NODE PRINT, NSET=TIP",
            "U",
            "*NODE PRINT, NSET=ROOT",
            "RF",
            "*END STEP",
        ]
    )
    return "\n".join(rows)


def _prepared_live_truss(tmp_path, deck: str):
    contract, evidence, manifest = _prepared_declared_truss(tmp_path, deck)
    document = load_contract(contract).model_dump(mode="json")
    structural = document["structural"]
    free = deepcopy(structural["supports"][1])
    free["id"] = "free"
    free["region"]["name"] = "FREE"
    structural["supports"].append(free)
    structural["procedures"][0]["support_ids"].append("free")
    save_contract(Contract.model_validate(document), contract)
    return contract, evidence, manifest


def _record_process_command(trace: list[dict], argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=1, check=False)
    except subprocess.TimeoutExpired as error:
        trace.append(
            {
                "monotonic_ns": time.monotonic_ns(),
                "argv": argv,
                "returncode": None,
                "stdout": error.stdout.decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or ""),
                "stderr": error.stderr.decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else (error.stderr or ""),
                "error": "timeout",
            }
        )
        raise
    trace.append(
        {
            "monotonic_ns": time.monotonic_ns(),
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    return completed


def _solver_process(pid: int, trace: list[dict]) -> tuple[int, int, str, str] | None:
    result = _record_process_command(
        trace,
        [
            "/bin/ps",
            "-ww",
            "-p",
            str(pid),
            "-o",
            "pid=",
            "-o",
            "ppid=",
            "-o",
            "pgid=",
            "-o",
            "state=",
            "-o",
            "command=",
        ],
    )
    fields = result.stdout.strip().split(maxsplit=4)
    if result.returncode or len(fields) != 5 or not all(part.isdecimal() for part in fields[:3]):
        return None
    observed_pid, ppid, pgid = (int(part) for part in fields[:3])
    return ppid, pgid, fields[3], fields[4] if observed_pid == pid else ""


def _observe_stopped_solver(
    job: Job, seconds: float, trace: list[dict], frozen_sidecar: Path | None = None
) -> int | None:
    expected = str(job.work_dir / "solver/bin/ccx_2.23") + " -i model"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and job.process.poll() is None:
        children = _record_process_command(trace, ["/usr/bin/pgrep", "-P", str(job.process.pid)])
        for named in children.stdout.split():
            if not named.isdecimal():
                continue
            child = int(named)
            candidates = [(child, job.process.pid)]
            owner = _solver_process(child, trace)
            if (
                owner is not None
                and owner[0] == job.process.pid
                and (
                    owner[3].startswith(f"{frozen_sidecar} --internal-ccx-owner ")
                    if frozen_sidecar is not None
                    else "lockwire_pro._ccx_owner" in owner[3]
                )
            ):
                nested = _record_process_command(trace, ["/usr/bin/pgrep", "-P", str(child)])
                candidates.extend(
                    (int(pid), child) for pid in nested.stdout.split() if pid.isdecimal()
                )
            for pid, parent in candidates:
                identity = _solver_process(pid, trace)
                if identity is None or identity[:2] != (parent, pid) or identity[3] != expected:
                    continue
                if identity[2].startswith("Z"):
                    continue
                try:
                    os.kill(pid, signal.SIGSTOP)
                    trace.append(
                        {"monotonic_ns": time.monotonic_ns(), "signal": "SIGSTOP", "pid": pid}
                    )
                except ProcessLookupError:
                    continue
                stopped = _solver_process(pid, trace)
                if (
                    stopped is not None
                    and stopped[:2] == (parent, pid)
                    and stopped[2].startswith("T")
                    and stopped[3] == expected
                ):
                    return pid
                if stopped is not None and stopped[1] == pid and stopped[3] == expected:
                    os.kill(pid, signal.SIGCONT)
                    trace.append(
                        {"monotonic_ns": time.monotonic_ns(), "signal": "SIGCONT", "pid": pid}
                    )
        time.sleep(0.005)
    return None


def _solver_stopped_or_gone(pid: int, expected: str, trace: list[dict]) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        identity = _solver_process(pid, trace)
        if identity is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        elif identity[3] != expected or identity[2].startswith("Z"):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform != "darwin", reason="E5.4 pinned executable is macOS arm64")
@pytest.mark.parametrize(
    "harddeath,observation_seconds", [(False, 10), (True, 3)], ids=["cancel", "parent-sigkill"]
)
def test_real_pinned_ccx_public_job_cleanup_after_live_observation(
    tmp_path, monkeypatch, harddeath, observation_seconds
):
    """Only a seen, stopped real solver makes cancellation or parent death decisive."""
    sidecar_name = os.environ.get("LOCKWIRE_FROZEN_SIDECAR")
    sidecar_hash = os.environ.get("LOCKWIRE_FROZEN_SIDECAR_SHA256")
    frozen_requested = (
        "LOCKWIRE_FROZEN_SIDECAR" in os.environ or "LOCKWIRE_FROZEN_SIDECAR_SHA256" in os.environ
    )
    if frozen_requested and (not sidecar_name or not sidecar_hash):
        pytest.fail("set both LOCKWIRE_FROZEN_SIDECAR and LOCKWIRE_FROZEN_SIDECAR_SHA256")
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        if frozen_requested:
            pytest.fail("frozen cleanup proof requires LOCKWIRE_CCX")
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    frozen_sidecar = Path(sidecar_name).resolve() if sidecar_name else None
    if frozen_sidecar is not None:
        if (
            len(sidecar_hash) != 64
            or not frozen_sidecar.is_file()
            or not os.access(frozen_sidecar, os.X_OK)
            or hashlib.sha256(frozen_sidecar.read_bytes()).hexdigest() != sidecar_hash
        ):
            pytest.fail("frozen sidecar is not the executable bound by its SHA256")
        monkeypatch.setattr(
            analysis,
            "sys",
            SimpleNamespace(executable=str(frozen_sidecar), frozen=True, platform=sys.platform),
        )
    deck = _actual_work_truss_deck()
    for attempt in range(8):
        attempt_root = tmp_path / str(attempt)
        attempt_root.mkdir()
        trace: list[dict] = [
            {
                "mode": "frozen" if frozen_sidecar is not None else "source",
                "sidecar": str(frozen_sidecar) if frozen_sidecar is not None else None,
                "sidecar_sha256": sidecar_hash,
            }
        ]
        job: Job | None = None
        native: int | None = None
        expected = ""
        product_gone: bool | None = None
        try:
            contract, evidence, manifest = _prepared_live_truss(attempt_root, deck)
            job = start_analysis_job(
                contract,
                expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
                procedure_id="static",
                evidence_root=evidence,
                package_manifest_path=manifest,
            )
            expected = str(job.work_dir / "solver/bin/ccx_2.23") + " -i model"
            native = _observe_stopped_solver(job, observation_seconds, trace, frozen_sidecar)
            if native is None:
                job.result(timeout=15)
                continue
            if harddeath:
                os.kill(job.process.pid, signal.SIGKILL)
                trace.append(
                    {
                        "monotonic_ns": time.monotonic_ns(),
                        "signal": "SIGKILL",
                        "pid": job.process.pid,
                    }
                )
                assert job.process.wait(timeout=3) == -signal.SIGKILL
            else:
                job.cancel()
                assert job.cancelled and job.exit_code is not None
                with pytest.raises(JobCancelled):
                    job.result()
            product_gone = _solver_stopped_or_gone(native, expected, trace)
            trace.append(
                {
                    "monotonic_ns": time.monotonic_ns(),
                    "phase": "before fixture cleanup",
                    "product_solver_gone": product_gone,
                }
            )
            if harddeath:
                assert job.exit_code == -signal.SIGKILL
            else:
                assert not job.work_dir.exists()
        finally:
            try:
                if job is not None:
                    job.cancel()
            finally:
                try:
                    if native is not None:
                        identity = _solver_process(native, trace)
                        if (
                            identity is not None
                            and identity[1] == native
                            and identity[3] == expected
                            and not identity[2].startswith("Z")
                        ):
                            try:
                                os.killpg(native, signal.SIGKILL)
                                trace.append(
                                    {
                                        "monotonic_ns": time.monotonic_ns(),
                                        "signal": "cleanup SIGKILL",
                                        "pgid": native,
                                    }
                                )
                            except (ProcessLookupError, PermissionError) as error:
                                trace.append(
                                    {
                                        "monotonic_ns": time.monotonic_ns(),
                                        "signal": "cleanup SIGKILL",
                                        "pgid": native,
                                        "error": repr(error),
                                    }
                                )
                        cleanup_gone = _solver_stopped_or_gone(native, expected, trace)
                        trace.append(
                            {
                                "monotonic_ns": time.monotonic_ns(),
                                "phase": "after fixture cleanup",
                                "owned_solver_gone": cleanup_gone,
                            }
                        )
                        assert cleanup_gone, f"fixture failed to remove owned solver {native}"
                finally:
                    receipt = attempt_root / "process-observation.json"
                    receipt.write_text(json.dumps(trace, indent=2))
                    print(f"attempt={attempt + 1} native={native} trace={receipt}")
        if native is not None:
            assert product_gone, (
                f"real solver {native} survived {'analysis parent SIGKILL' if harddeath else 'Job.cancel'}; "
                f"fixture cleanup is separately proved in {receipt}"
            )
            return
    pytest.fail(f"no live pinned CalculiX observed in eight attempts; traces: {tmp_path}")


def _prepared_declared_b31_cantilever(tmp_path, deck_text=None, limit_metric=None, threshold=None):
    source = Path(__file__).parent / "fixtures/calculix_benchmarks/cantilever_b31_4.inp"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    deck = evidence / "cantilever.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["materials"][0]["youngs_modulus"] = quantity(210, "GPa")
    structural["sections"] = [
        {
            "id": "square",
            "kind": "beam",
            "material_id": "steel",
            "area": quantity(0.0001, "m^2"),
            "principal_second_moment_y": quantity(0.01**4 / 12, "m^4"),
            "principal_second_moment_z": quantity(0.01**4 / 12, "m^4"),
            "torsion_constant_j": quantity(0.1406 * 0.01**4, "m^4"),
            "orientation_id": "axis",
            "provenance": PROVENANCE,
        }
    ]
    structural["orientations"] = [
        {
            "id": "axis",
            "frame": "base",
            "longitudinal": [1, 0, 0],
            "in_plane_reference": [0, 1, 0],
            "provenance": PROVENANCE,
        }
    ]
    structural["assignments"][0]["section_id"] = "square"
    structural["assignments"][0]["region"] = {"kind": "element_set", "name": "BEAM"}
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["supports"] = [
        {
            "id": "root",
            "region": {"kind": "node_set", "name": "ROOT"},
            "dofs": {"translation": ["x", "y", "z"], "rotation": ["x", "y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        }
    ]
    structural["loads"] = [
        {
            "id": "tip-force",
            "kind": "force",
            "region": {"kind": "node_set", "name": "TIP"},
            "vector": {"values": [0, -100, 0], "unit": "N", "frame": "base"},
            "application_frame": "base",
            "provenance": PROVENANCE,
        }
    ]
    structural["procedures"][0]["support_ids"] = ["root"]
    structural["procedures"][0]["load_ids"] = ["tip-force"]
    if limit_metric is not None:
        structural["acceptance_limits"] = [
            {
                "id": "cantilever-tip",
                "kind": "scalar_reference",
                "metric": limit_metric,
                "comparator": "relative",
                "threshold": threshold or quantity(2, "%"),
                "reference": "analytic",
                "provenance": PROVENANCE,
            }
        ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "procedure_templates": {"linear_static": "direct-b31-square-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def test_direct_b31_preparation_binds_square_section_support_and_force(tmp_path):
    contract, evidence, manifest = _prepared_declared_b31_cantilever(tmp_path)
    revision = hashlib.sha256(contract.read_bytes()).hexdigest()
    prepared = prepare_analysis(
        contract,
        expected_revision=revision,
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert (prepared["deck_template"], prepared["status"], prepared["solver_started"]) == (
        "direct-b31-square-v1",
        "PREPARED",
        False,
    )
    for changed, message in (
        ("*ELEMENT, TYPE=B32, ELSET=BEAM", "element type"),
        ("*BEAM SECTION, ELSET=BEAM, MATERIAL=STEEL, SECTION=CIRC", "rectangular"),
        ("0.02, 0.02", "beam size"),
        ("0., 0., 1.", "beam orientation"),
        ("ROOT, 1, 5", "boundaries differ"),
        ("TIP, 2, -200.", "loads differ"),
        ("*DYNAMIC", "not admitted"),
    ):
        original = (evidence / "cantilever.inp").read_text()
        target = {
            "*ELEMENT, TYPE=B32, ELSET=BEAM": "*ELEMENT, TYPE=B31, ELSET=BEAM",
            "*BEAM SECTION, ELSET=BEAM, MATERIAL=STEEL, SECTION=CIRC": "*BEAM SECTION, ELSET=BEAM, MATERIAL=STEEL, SECTION=RECT",
            "0.02, 0.02": "0.01, 0.01",
            "0., 0., 1.": "0., 1., 0.",
            "ROOT, 1, 5": "ROOT, 1, 6",
            "TIP, 2, -200.": "TIP, 2, -100.",
            "*DYNAMIC": "*STATIC",
        }[changed]
        assert target in original
        modified = original.replace(target, changed)
        with pytest.raises(AnalysisRefused, match=message):
            analysis._admit_direct_truss_deck(
                modified.encode(),
                b31_square=True,
                declared_material=(210e9, 0.3),
                declared_element_set="BEAM",
                declared_material_name="STEEL",
                declared_boundaries={("ROOT", dof) for dof in range(1, 7)},
                declared_loads={("TIP", 2): -100.0},
                declared_side=0.01,
                declared_orientation=(0, 1, 0),
            )


def test_direct_b31_refuses_mismatched_declared_inertia(tmp_path):
    contract, evidence, manifest = _prepared_declared_b31_cantilever(tmp_path)
    document = analysis.load_contract_bytes(contract.read_bytes()).model_dump(mode="json")
    document["structural"]["sections"][0]["principal_second_moment_y"] = quantity(2e-9, "m^4")
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="section moments"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def _prepared_declared_plate_s4(
    tmp_path, mesh=8, deck_text=None, thickness=0.02, pressure=-100000.0
):
    source = Path(__file__).parent / f"fixtures/calculix_benchmarks/plate_s4_{mesh}.inp"
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / "plate.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["materials"][0]["youngs_modulus"] = quantity(210, "GPa")
    structural["sections"] = [
        {
            "id": "steel",
            "kind": "plate",
            "material_id": "steel",
            "thickness": quantity(thickness, "m"),
            "provenance": PROVENANCE,
        }
    ]
    structural["assignments"][0]["section_id"] = "steel"
    structural["assignments"][0]["region"] = {"kind": "element_set", "name": "PLATE"}
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["supports"] = [
        {
            "id": "edges",
            "region": {"kind": "node_set", "name": "SUPPORT"},
            "dofs": {"translation": ["z"], "frame": "base"},
            "provenance": PROVENANCE,
        },
        {
            "id": "pin-x",
            "region": {"kind": "node_set", "name": "PIN_X"},
            "dofs": {"translation": ["x"], "frame": "base"},
            "provenance": PROVENANCE,
        },
        {
            "id": "pin-y",
            "region": {"kind": "node_set", "name": "PIN_Y"},
            "dofs": {"translation": ["y"], "frame": "base"},
            "provenance": PROVENANCE,
        },
    ]
    structural["loads"] = [
        {
            "id": "pressure",
            "kind": "pressure",
            "region": {"kind": "element_set", "name": "PLATE"},
            "scalar": quantity(pressure, "Pa"),
            "application_frame": "base",
            "provenance": PROVENANCE,
        }
    ]
    structural["procedures"][0].update(
        support_ids=["edges", "pin-x", "pin-y"], load_ids=["pressure"]
    )
    structural["acceptance_limits"] = [
        {
            "id": "plate-center",
            "kind": "scalar_reference",
            "metric": "plate_center_u3",
            "comparator": "relative",
            "threshold": quantity(2, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "procedure_templates": {"linear_static": "direct-plate-s4-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def test_direct_plate_s4_preparation_binds_mesh_and_declarations(tmp_path):
    for mesh in (8, 16):
        contract, evidence, manifest = _prepared_declared_plate_s4(tmp_path / str(mesh), mesh)
        prepared = prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )
        assert (prepared["deck_template"], prepared["status"], prepared["solver_started"]) == (
            "direct-plate-s4-v1",
            "PREPARED",
            False,
        )


@pytest.mark.parametrize(
    ("deck_change", "contract_change", "message"),
    [
        (lambda text: text.replace("PLATE 8X8", "PLATE 16X16"), None, "heading"),
        (lambda text: text.replace("TYPE=S4,", "TYPE=S4R,"), None, "only S4 plate"),
        (lambda text: text.replace("0.02\n*BOUNDARY", "0.01\n*BOUNDARY"), None, "thickness"),
        (
            lambda text: text.replace("210000000000.", "200000000000."),
            None,
            "elastic material differs",
        ),
        (
            lambda text: text.replace("PLATE, P, -100000.", "PLATE, P, -90000."),
            None,
            "pressure differs",
        ),
        (lambda text: text.replace("SUPPORT, 3, 3", "SUPPORT, 1, 1"), None, "supports differ"),
        (
            lambda text: text.replace("*NSET, NSET=PIN_Y\n1, 2\n", "*NSET, NSET=PIN_Y\n2\n"),
            None,
            "three in-plane pin",
        ),
        (
            None,
            lambda doc: doc["structural"]["loads"][0].update(scalar=quantity(-100000, "N")),
            "pressure",
        ),
    ],
)
def test_direct_plate_s4_refuses_mismatched_inputs(tmp_path, deck_change, contract_change, message):
    original = (
        Path(__file__).parent.joinpath("fixtures/calculix_benchmarks/plate_s4_8.inp").read_text()
    )
    deck_text = deck_change(original) if deck_change else None
    if deck_change:
        assert deck_text != original
    contract, evidence, manifest = _prepared_declared_plate_s4(tmp_path, deck_text=deck_text)
    with pytest.raises(ValidationError if contract_change else AnalysisRefused, match=message):
        if contract_change:
            document = analysis.load_contract_bytes(contract.read_bytes()).model_dump(mode="json")
            contract_change(document)
            save_contract(Contract.model_validate(document), contract)
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_direct_plate_s4_real_job_matches_navier_and_refines(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 pinned ccx_2.23 for the real plate Job gate")
    binary = Path(named)
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    results = []
    expected = -0.004062352660 * 100000.0 / (210e9 * 0.02**3 / (12 * (1 - 0.3**2)))
    for mesh in (8, 16):
        contract, evidence, manifest = _prepared_declared_plate_s4(tmp_path / str(mesh), mesh)
        job = start_analysis_job(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )
        result = job.result(timeout=120)
        assert result["status"] == "COMPLETED"
        assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
        assert result["physical_conformance"] == "NOT_SCORED"
        assert result["simulation_validity"] == ("GO" if mesh == 16 else "NO_GO")
        assert result["comparisons"][0]["metric"] == "plate_center_u3"
        observed = result["comparisons"][0]["observed"]["value"]
        assert result["comparisons"][0]["expected"]["value"] == pytest.approx(expected)
        assert result["comparisons"][0]["passed"] is (mesh == 16)
        assert (abs(observed - expected) / abs(expected) <= 0.02) is (mesh == 16)
        operands = result["analytic_operands"]
        assert operands["side"]["value"] == 1.0
        assert operands["youngs_modulus"]["value"] == pytest.approx(210e9)
        assert operands["thickness"]["value"] == pytest.approx(0.02)
        assert operands["pressure"]["value"] == pytest.approx(-100000.0)
        model = next(row for row in result["artifacts"] if row["role"] == "structural_model")
        assert model["sha256"] == hashlib.sha256((evidence / "plate.inp").read_bytes()).hexdigest()
        results.append(abs(observed - expected))
    assert results[1] < results[0]


def _prepared_declared_thick_shear(
    tmp_path,
    mesh=1,
    deck_text=None,
    youngs=210,
    metric="thick_shear_top_u1",
    region_kind="mesh_region",
    region_name="BLOCK",
):
    source = Path(__file__).parent / f"fixtures/calculix_benchmarks/thick_shear_c3d20_{mesh}.inp"
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / "shear.inp"
    deck.write_text(source.read_text() if deck_text is None else deck_text)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["materials"] = [
        {
            "id": "steel",
            "kind": "linear_isotropic",
            "youngs_modulus": quantity(youngs, "GPa"),
            "poisson_ratio": quantity(0.3, "1"),
            "provenance": PROVENANCE,
        }
    ]
    structural["sections"] = [
        {"id": "solid", "kind": "solid", "material_id": "steel", "provenance": PROVENANCE}
    ]
    structural["assignments"] = [
        {
            "id": "block",
            "region": {"kind": region_kind, "name": region_name},
            "section_id": "solid",
            "provenance": PROVENANCE,
        }
    ]
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["supports"] = [
        {
            "id": "fixed",
            "region": {"kind": "node_set", "name": "SUPPORT"},
            "dofs": {"translation": ["x", "y", "z"], "frame": "base"},
            "provenance": PROVENANCE,
        }
    ]
    structural["procedures"][0].update(assignment_ids=["block"], support_ids=["fixed"], load_ids=[])
    structural["acceptance_limits"] = [
        {
            "id": "shear-reference",
            "kind": "scalar_reference",
            "metric": metric,
            "comparator": "relative",
            "threshold": quantity(2, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "procedure_templates": {"linear_static": "direct-shear-c3d20-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


@pytest.mark.parametrize("mesh", [1, 2])
def test_direct_thick_shear_preparation_admits_only_pinned_homogeneous_fixtures(tmp_path, mesh):
    contract, evidence, manifest = _prepared_declared_thick_shear(tmp_path / str(mesh), mesh)
    prepared = prepare_analysis(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert prepared["deck_template"] == "direct-shear-c3d20-v1"
    assert prepared["status"] == "PREPARED" and prepared["solver_started"] is False


@pytest.mark.parametrize(
    ("deck_change", "youngs", "metric", "message"),
    [
        (
            lambda text: text.replace("210000000000.", "200000000000."),
            210,
            "thick_shear_top_u1",
            "byte-pinned",
        ),
        (
            lambda text: text.replace("83333.333333333328", "83334.333333333328", 1),
            210,
            "thick_shear_top_u1",
            "byte-pinned",
        ),
        (None, 200, "thick_shear_top_u1", "material differs"),
        (None, 210, "plate_center_u3", "relative top-shear limit up to 2 percent"),
    ],
)
def test_direct_thick_shear_refuses_changed_inputs(tmp_path, deck_change, youngs, metric, message):
    original = (
        Path(__file__)
        .parent.joinpath("fixtures/calculix_benchmarks/thick_shear_c3d20_1.inp")
        .read_text()
    )
    contract, evidence, manifest = _prepared_declared_thick_shear(
        tmp_path,
        deck_text=deck_change(original) if deck_change else None,
        youngs=youngs,
        metric=metric,
    )
    with pytest.raises(AnalysisRefused, match=message):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_direct_thick_shear_refuses_undeclared_mesh_region(tmp_path):
    contract, evidence, manifest = _prepared_declared_thick_shear(tmp_path, region_name="OTHER")
    with pytest.raises(AnalysisRefused, match="declared solid and fixed support"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_direct_thick_shear_refuses_element_set_as_solid_region(tmp_path):
    with pytest.raises(
        ValueError, match="solid section assignments require a mesh_region selector"
    ):
        _prepared_declared_thick_shear(tmp_path, region_kind="element_set")


def test_direct_thick_shear_real_job_matches_analytic_reference_and_refines(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip(
            "set LOCKWIRE_CCX to the E5.4 pinned ccx_2.23 for the real thick-shear Job gate"
        )
    binary = Path(named)
    if (
        not binary.is_absolute()
        or not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    errors = []
    expected = 1e6 / (210e9 / (2 * (1 + 0.3)))
    for mesh in (1, 2):
        contract, evidence, manifest = _prepared_declared_thick_shear(tmp_path / str(mesh), mesh)
        job = start_analysis_job(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )
        result = job.result(timeout=120)
        assert result["status"] == "COMPLETED"
        assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
        assert result["physical_conformance"] == "NOT_SCORED"
        assert result["simulation_validity"] == "GO"
        assert [row["metric"] for row in result["comparisons"]] == [
            "thick_shear_top_u1",
            "thick_shear_support_rf1",
        ]
        displacement = result["comparisons"][0]
        assert displacement["observed"]["value"] == pytest.approx(expected, rel=0.02)
        assert result["comparisons"][1]["observed"]["value"] == pytest.approx(-1e6, rel=0.02)
        errors.append(displacement["relative_error"])
    assert errors[1] <= errors[0]


def test_direct_b31_real_job_matches_independent_cantilever_reference(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 pinned ccx_2.23 for the public B31 Job gate")
    binary = Path(named)
    if (
        not binary.is_absolute()
        or binary.name != "ccx_2.23"
        or binary.is_symlink()
        or not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_declared_b31_cantilever(
        tmp_path, limit_metric="b31_tip_uy"
    )
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    result = job.result(timeout=30)
    assert result["status"] == "COMPLETED"
    assert result["contract_revision"] == hashlib.sha256(contract.read_bytes()).hexdigest()
    actual = {
        (row["kind"], row["node_set"], row["node"]): row["values"] for row in result["results"]
    }
    assert actual[("U", "TIP", 5)][1] == pytest.approx(-0.19047619047619047, rel=0.02, abs=1e-8)
    assert actual[("RF", "ROOT", 1)][1] == pytest.approx(100, rel=0.02, abs=0.001)
    assert hashlib.sha256(result["output_text"].encode()).hexdigest() == result["output_sha256"]
    assert result["package_manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["simulation_validity"] == "GO"
    assert result["acceptance_limit_id"] == "cantilever-tip"
    assert [row["metric"] for row in result["comparisons"]] == [
        "b31_tip_uy",
        "b31_root_force_balance",
    ]
    assert all(row["passed"] for row in result["comparisons"])
    assert result["comparisons"][0]["expected"]["value"] == pytest.approx(-0.19047619047619047)


def test_analysis_job_rejects_tampered_solver_output_and_cleans_workdir(tmp_path):
    class CompletedProcess:
        def poll(self):
            return 0

    work_dir = tmp_path / "bad-analysis-output"
    work_dir.mkdir()
    (work_dir / "document.json").write_text(
        json.dumps(
            {
                "kind": "analysis_result",
                "status": "COMPLETED",
                "solver_started": True,
                "output_text": "solver bytes\n",
                "output_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    (work_dir / "output.txt").write_text("", encoding="utf-8")
    (work_dir / "errors.txt").write_text("", encoding="utf-8")
    job = Job("analyze", CompletedProcess(), work_dir)

    snapshot = job.snapshot()

    assert snapshot["status"] == "failed"
    assert snapshot["document"] is None
    assert "output bytes do not match their hash" in snapshot["error"]
    assert not work_dir.exists()


@pytest.mark.parametrize(
    ("limit_metric", "root_force_x", "verdict"),
    [
        ("b31_tip_uy", 10, "NO_GO"),
        ("unsupported_tip_metric", 0, "NOT_SCORED"),
    ],
)
def test_direct_b31_score_refuses_unbalanced_or_unsupported_result(
    tmp_path, monkeypatch, limit_metric, root_force_x, verdict
):
    contract, evidence, manifest = _prepared_declared_b31_cantilever(
        tmp_path, limit_metric=limit_metric
    )
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        "#!/bin/sh\n"
        "cat > model.dat <<'EOF'\n"
        " displacements (vx,vy,vz) for set TIP and time 1.0\n"
        " 5 0.000000E+00 -1.904762E-01 0.000000E+00\n"
        " forces (fx,fy,fz) for set ROOT and time 1.0\n"
        f" 1 {root_force_x:.6E} 1.000000E+02 0.000000E+00\n"
        "EOF\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert result["status"] == "COMPLETED"
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["simulation_validity"] == verdict
    if verdict == "NO_GO":
        assert result["comparisons"][0]["passed"] is True
        assert result["comparisons"][1]["passed"] is False
    else:
        assert "unsupported acceptance metric" in result["reason"]


def test_direct_b31_refuses_non_cantilever_topology(tmp_path):
    source = Path(__file__).parent / "fixtures/calculix_benchmarks/cantilever_b31_4.inp"
    original = source.read_text()
    for old, new, message in (
        ("4, 4, 5", "4, 2, 5", "unbranched"),
        ("*NSET, NSET=ROOT\n1", "*NSET, NSET=ROOT\n1, 2", "single nodes"),
        ("3, 0.5, 0., 0.", "3, 0.5, 0.01, 0.", "straight"),
        (
            ("1, 0., 0., 0.\n2, 0.25, 0., 0.\n3, 0.5, 0., 0.\n4, 0.75, 0., 0.\n5, 1., 0., 0."),
            (
                "1, 0., 1000000000., 0.\n2, 0.25, 1000000000., 0.\n"
                "3, 0.5, 1000000000.1, 0.\n4, 0.75, 1000000000., 0.\n"
                "5, 1., 1000000000., 0."
            ),
            "straight",
        ),
        (
            ("1, 0., 0., 0.\n2, 0.25, 0., 0.\n3, 0.5, 0., 0.\n4, 0.75, 0., 0.\n5, 1., 0., 0."),
            (
                "1, 0., 0., 1000000000.\n2, 0.25, 0., 1000000000.\n"
                "3, 0.5, 0., 1000000000.1\n4, 0.75, 0., 1000000000.\n"
                "5, 1., 0., 1000000000."
            ),
            "straight",
        ),
        ("4, 4, 5", "4, 4, 3", "repeats a beam link"),
    ):
        changed = original.replace(old, new)
        assert changed != original
        with pytest.raises(AnalysisRefused, match=message):
            analysis._admit_direct_truss_deck(
                changed.encode(),
                b31_square=True,
                declared_material=(210e9, 0.3),
                declared_element_set="BEAM",
                declared_material_name="STEEL",
                declared_boundaries={("ROOT", dof) for dof in range(1, 7)},
                declared_loads={("TIP", 2): -100.0},
                declared_side=0.01,
                declared_orientation=(0, 1, 0),
            )


def _prepared_sliding_contact(tmp_path, *, extended=False):
    source = (
        Path(__file__).parent
        / "fixtures/calculix_nonlinear"
        / ("sliding_contact_extended_travel.inp" if extended else "sliding_contact_static.inp")
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    deck = evidence / "sliding.inp"
    deck.write_bytes(source.read_bytes())
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["materials"] = [
        {
            "id": "elastic-block",
            "kind": "linear_isotropic",
            "youngs_modulus": quantity(1000, "Pa"),
            "poisson_ratio": quantity(0.3, "1"),
            "provenance": PROVENANCE,
        }
    ]
    structural["sections"] = [
        {
            "id": "contact-blocks",
            "kind": "solid",
            "material_id": "elastic-block",
            "provenance": PROVENANCE,
        }
    ]
    structural["assignments"] = [
        {
            "id": "upper-elements",
            "region": {"kind": "mesh_region", "name": "UPPER"},
            "section_id": "contact-blocks",
            "provenance": PROVENANCE,
        }
    ]
    structural["contacts"] = [
        {
            "id": "sliding-law",
            "master": {"kind": "surface_set", "name": "LOWER_TOP"},
            "slave": {"kind": "surface_set", "name": "UPPER_BOTTOM"},
            "frame": "base",
            "normal": {
                "kind": "linear",
                "pressure_overclosure_slope": quantity(1_000_000, "N/m^3"),
            },
            "tangential": {"friction_coefficient": quantity(0.2, "1")},
            "provenance": PROVENANCE,
        }
    ]
    structural["loads"] = [
        {
            "id": "upper-normal",
            "kind": "force",
            "region": {"kind": "node_set", "name": "UPPER_TOP"},
            "vector": {"values": [0, 0, -40], "unit": "N", "frame": "base"},
            "application_frame": "base",
            "provenance": PROVENANCE,
        },
        {
            "id": "upper-slide",
            "kind": "prescribed_displacement",
            "region": {"kind": "node_set", "name": "UPPER_TOP"},
            "vector": {"values": [0.04 if extended else 0.02, 0, 0], "unit": "m", "frame": "base"},
            "application_frame": "base",
            "provenance": PROVENANCE,
        },
    ]
    structural["procedures"] = [
        {
            "id": "sliding",
            "kind": "nonlinear_static",
            "assignment_ids": ["upper-elements"],
            "load_ids": ["upper-normal", "upper-slide"],
            "contact_ids": ["sliding-law"],
            "geometric_nonlinearity": True,
            "plasticity": False,
            "max_increments": 100,
            "provenance": PROVENANCE,
        }
    ]
    structural["acceptance_limits"] = [
        {
            "id": "sliding-coulomb",
            "kind": "scalar_reference",
            "metric": "sliding_contact_extended_travel" if extended else "sliding_contact_static",
            "comparator": "relative",
            "threshold": quantity(5, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "admitted_procedures": ["nonlinear_static"],
                        "procedure_templates": {"nonlinear_static": "sliding-contact-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def _prepared_frictionless_impact(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = (
        Path(__file__).parent
        / "fixtures"
        / "calculix_nonlinear"
        / "frictionless_elastic_impact.inp"
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    deck = evidence / source.name
    deck.write_bytes(source.read_bytes())
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    document = structural_document(source_sha256=digest)
    structural = document["structural"]
    structural["model"]["model_artifact"] = {
        "role": "structural_model",
        "ref": {"path": deck.name, "sha256": digest},
        "format": "calculix_inp",
        "provenance": PROVENANCE,
    }
    structural["materials"] = [
        {
            "id": "elastic",
            "kind": "linear_isotropic",
            "youngs_modulus": quantity(1000, "Pa"),
            "poisson_ratio": quantity(0.3, "1"),
            "density": quantity(1, "kg/m^3"),
            "provenance": PROVENANCE,
        }
    ]
    structural["sections"] = [
        {"id": "solid", "kind": "solid", "material_id": "elastic", "provenance": PROVENANCE}
    ]
    structural["assignments"] = [
        {
            "id": aid,
            "region": {"kind": "mesh_region", "name": region},
            "section_id": "solid",
            "provenance": PROVENANCE,
        }
        for aid, region in (("wall-solid", "WALL"), ("impactor-solid", "IMPACTOR"))
    ]
    structural["masses"] = [
        {
            "id": "ref-point-mass",
            "region": {"kind": "node_set", "name": "REF"},
            "mass": quantity(0.9, "kg"),
            "provenance": PROVENANCE,
        }
    ]
    structural["supports"] = [
        {
            "id": sid,
            "region": {"kind": "node_set", "name": region},
            "dofs": {"translation": axes, "frame": "base"},
            "provenance": PROVENANCE,
        }
        for sid, region, axes in (
            ("wall-fixed", "WALL_FIXED", ["x", "y", "z"]),
            ("ref-xy", "REF", ["x", "y"]),
            ("rotation-fixed", "101", ["x", "y", "z"]),
        )
    ]
    structural["contacts"] = [
        {
            "id": "elastic-contact",
            "master": {"kind": "surface_set", "name": "WALL_TOP"},
            "slave": {"kind": "surface_set", "name": "IMPACTOR_BOTTOM"},
            "frame": "base",
            "normal": {"kind": "linear", "pressure_overclosure_slope": quantity(1e7, "N/m^3")},
            "provenance": PROVENANCE,
        }
    ]
    structural["procedures"] = [
        {
            "id": "impact",
            "kind": "impact",
            "assignment_ids": ["wall-solid", "impactor-solid"],
            "support_ids": ["wall-fixed", "ref-xy", "rotation-fixed"],
            "mass_ids": ["ref-point-mass"],
            "contact_ids": ["elastic-contact"],
            "damping_mode": "undamped",
            "duration": quantity(0.15, "s"),
            "time_step": quantity(1e-5, "s"),
            "initial_velocity": {"values": [0, 0, -1], "unit": "m/s", "frame": "base"},
            "max_increments": 30000,
            "provenance": PROVENANCE,
        }
    ]
    structural["acceptance_limits"] = [
        {
            "id": "impact-reference",
            "kind": "scalar_reference",
            "metric": "frictionless_elastic_impact",
            "comparator": "relative",
            "threshold": quantity(5, "%"),
            "reference": "analytic",
            "provenance": PROVENANCE,
        }
    ]
    contract = tmp_path / "robot.lockwire.yaml"
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
                        "admitted_procedures": ["impact"],
                        "undamped_procedures": ["impact"],
                        "procedure_templates": {"impact": "frictionless-impact-v1"},
                        "allowed_artifact_formats": ["calculix_inp"],
                    }
                },
            }
        )
    )
    return contract, evidence, manifest


def test_frictionless_impact_preparation_binds_deck_and_declared_mass(tmp_path):
    contract, evidence, manifest = _prepared_frictionless_impact(tmp_path)
    prepared = prepare_analysis(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="impact",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert prepared["status"] == "PREPARED"
    assert prepared["deck_template"] == "frictionless-impact-v1"
    assert not prepared["solver_started"]
    document = load_contract(contract).model_dump(mode="json")
    document["structural"]["masses"][0]["mass"] = quantity(1, "kg")
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="point mass"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="impact",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def test_frictionless_impact_refuses_wrong_deck_and_wrong_support(tmp_path):
    contract, evidence, manifest = _prepared_frictionless_impact(tmp_path)
    deck = evidence / "frictionless_elastic_impact.inp"
    mutated = deck.read_text().replace("101, 1, 3", "101, 1, 2")
    deck.write_text(mutated)
    document = load_contract(contract).model_dump(mode="json")
    mutated_sha = hashlib.sha256(deck.read_bytes()).hexdigest()
    document["model"]["source_sha256"] = mutated_sha
    document["structural"]["model"]["source_sha256"] = mutated_sha
    document["structural"]["model"]["model_artifact"]["ref"]["sha256"] = mutated_sha
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="fixture SHA-256"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="impact",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )
    deck.write_bytes(
        (Path(__file__).parent / "fixtures" / "calculix_nonlinear" / deck.name).read_bytes()
    )
    original_sha = hashlib.sha256(deck.read_bytes()).hexdigest()
    document["model"]["source_sha256"] = original_sha
    document["structural"]["model"]["source_sha256"] = original_sha
    document["structural"]["model"]["model_artifact"]["ref"]["sha256"] = original_sha
    document["structural"]["supports"][2]["dofs"]["translation"] = ["x", "y"]
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="support differs"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="impact",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )


def _short_native_impact_history(*, final_contact=False, early_tensile=False):
    """Short native-format sections exercise refusal logic, never a solver result."""
    blocks = ["\n S T E P 1\n"]
    stdout = [" SELECTED time increment (not considering penalty contact):1.000000e-02\n"]
    increments = 4 if early_tensile else 3
    for increment in range(1, increments + 1):
        stamp = f"{increment / 100:.7E}"
        active = (
            increment == 2
            or (early_tensile and increment == 3)
            or (final_contact and increment == 3)
        )
        count = 28 if active else 0
        velocity = (
            (-1.0, 0.0, 0.5, 1.0)[increment - 1]
            if early_tensile
            else (-1.0, 0.0, 1.0)[increment - 1]
        )
        position = -0.1001 if active else (-0.01 if increment == 1 else -0.05)
        force = (-1.0 if early_tensile and increment == 2 else 100.0) if active else 0.0
        rows3 = (" 2 1 -1.000000E-04 0.000000E+00 0.000000E+00\n" * 28) if active else ""
        stresses = (" 2 1 1.000000E+03 0.000000E+00 0.000000E+00\n" * 28) if active else ""
        spring = (" 2 1 1.000000E-02\n" * 28) if active else ""
        block = f"""
 INCREMENT {increment}

 displacements (vx,vy,vz) for set REF and time {stamp}

 100 0.000000E+00 0.000000E+00 {position:.6E}

 velocities (vx,vy,vz) for set REF and time {stamp}

 100 0.000000E+00 0.000000E+00 {velocity:.6E}

 internal energy (element, energy) for set SYSTEM_ENERGY and time {stamp}

 1 0.000000E+00
 2 0.000000E+00
 3 0.000000E+00

 total internal energy for set SYSTEM_ENERGY and time {stamp}

 0.000000E+00

 kinetic energy (elem, energy) for set SYSTEM_ENERGY and time {stamp}

 1 0.000000E+00
 2 2.000000E-01
 3 0.000000E+00

 total kinetic energy for set SYSTEM_ENERGY and time {stamp}

 2.000000E-01

 relative contact displacement (slave element+face,normal,tang1,tang2) for all contact elements and time {stamp}

{rows3}
 contact stress (slave element+face,press,tang1,tang2) for all contact elements and time {stamp}

{stresses}
 contact spring energy (slave element+face,energy) for all contact elements and time {stamp}

{spring}
 total contact spring energy for time {stamp}

 {0.28 if active else 0.0:.6E}

 total number of contact elements for time {stamp}

 {count}

"""
        for label, z in (
            ("total surface force", force),
            ("total normal surface force", force),
            ("total shear surface force", 0.0),
        ):
            block += f""" statistics for slave set IMPACTOR_BOTTOM, master set WALL_TOP and time {stamp}

 {label} (fx,fy,fz) and moment about the origin (mx,my,mz)

 0.000000E+00 0.000000E+00 {z:.6E} 0.000000E+00 0.000000E+00 0.000000E+00

"""
        blocks.append(block)
        stdout.append(f""" actual total time={stamp}
 external work = 0.000000e+00
 work performed by the damping forces = 0.000000e+00
 energy lost due to friction = 0.000000e+00
""")
    return "".join(blocks), "".join(stdout)


def test_frictionless_impact_parser_refuses_missing_malformed_truncated_and_still_contact():
    native, stdout = _short_native_impact_history()
    parsed = analysis._parse_frictionless_impact_output(
        native, stdout, increments=3, time_step_s=0.01
    )
    assert parsed["first_contact_increment"] == 2
    assert parsed["last_contact_increment"] == 2
    with pytest.raises(AnalysisRefused, match="total number of contact elements"):
        analysis._parse_frictionless_impact_output(
            native.replace("total number of contact elements", "missing contact elements", 1),
            stdout,
            increments=3,
            time_step_s=0.01,
        )
    with pytest.raises(AnalysisRefused, match="nonfinite"):
        analysis._parse_frictionless_impact_output(
            native.replace("-1.000000E-04", "NaN", 1), stdout, increments=3, time_step_s=0.01
        )
    with pytest.raises(AnalysisRefused, match="truncated increments"):
        analysis._parse_frictionless_impact_output(
            native.rsplit(" INCREMENT 3", 1)[0], stdout, increments=3, time_step_s=0.01
        )
    still_contact, _ = _short_native_impact_history(final_contact=True)
    with pytest.raises(AnalysisRefused, match="final separation"):
        analysis._parse_frictionless_impact_output(
            still_contact, stdout, increments=3, time_step_s=0.01
        )
    with pytest.raises(AnalysisRefused, match="final geometric separation"):
        analysis._parse_frictionless_impact_output(
            native.replace("-5.000000E-02", "-2.000000E-01"),
            stdout,
            increments=3,
            time_step_s=0.01,
        )
    with pytest.raises(AnalysisRefused, match="REF geometric gap"):
        analysis._parse_frictionless_impact_output(
            native.replace("2 1 -1.000000E-04", "2 1 -2.000000E-04", 1),
            stdout,
            increments=3,
            time_step_s=0.01,
        )
    with pytest.raises(AnalysisRefused, match="declared linear penalty"):
        analysis._parse_frictionless_impact_output(
            native.replace("2 1 1.000000E+03", "2 1 0.000000E+00", 1),
            stdout,
            increments=3,
            time_step_s=0.01,
        )
    early_tensile, longer_stdout = _short_native_impact_history(early_tensile=True)
    with pytest.raises(AnalysisRefused, match="one-increment release artifact"):
        analysis._parse_frictionless_impact_output(
            early_tensile,
            longer_stdout,
            increments=4,
            time_step_s=0.01,
        )
    with pytest.raises(AnalysisRefused, match="damping"):
        analysis._parse_frictionless_impact_output(
            native,
            stdout.replace(
                "work performed by the damping forces = 0.000000e+00",
                "work performed by the damping forces = 1.000000e-02",
                1,
            ),
            increments=3,
            time_step_s=0.01,
        )


def test_frictionless_impact_scorer_keeps_five_percent_limit_and_mass_crosscheck(tmp_path):
    contract, _, _ = _prepared_frictionless_impact(tmp_path)
    native, stdout = _short_native_impact_history()
    parsed = analysis._parse_frictionless_impact_output(
        native, stdout, increments=3, time_step_s=0.01
    )
    score = analysis._score_frictionless_impact(load_contract(contract).structural, parsed)
    assert score["simulation_validity"] == "NO_GO"
    assert next(row for row in score["comparisons"] if row["metric"] == "impact_effective_mass")[
        "observed"
    ]["value"] == pytest.approx(0.5)
    assert score["threshold"]["value"] == 5
    document = load_contract(contract).model_dump(mode="json")
    document["structural"]["acceptance_limits"][0]["threshold"] = quantity(6, "%")
    structural = Contract.model_validate(document).structural
    assert (
        analysis._score_frictionless_impact(structural, parsed)["simulation_validity"]
        == "NOT_SCORED"
    )


def test_real_pinned_ccx_job_scores_frictionless_impact_go(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_frictionless_impact(tmp_path)
    result = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="impact",
        evidence_root=evidence,
        package_manifest_path=manifest,
    ).result(timeout=130)
    assert result["status"] == "COMPLETED"
    assert result["solver"]["binary_sha256"] == analysis._PACKAGED_CCX_SHA256
    assert result["artifacts"][0]["sha256"] == analysis._FRICTIONLESS_IMPACT_SHA256
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["simulation_validity"] == "GO"
    assert all(row["passed"] for row in result["comparisons"])
    impact = result["results"][0]
    assert (impact["first_contact_increment"], impact["last_contact_increment"]) == (10002, 10100)
    assert impact["release_tail_force_n"] < 0
    assert impact["native_elke_final_j_diagnostic_only"] < impact["reconstructed_energy_final_j"]


def _native_sliding_contact_output(
    *, omit_cfs=False, positive_sliding=True, compressive_contact=True, mobilized=True
):
    cdis = " 2 1 -4.000000E-05 1.500000E-02 0.000000E+00"
    if not positive_sliding:
        cdis = " 2 1 -4.000000E-05 0.000000E+00 0.000000E+00"
    cstr = " 2 1 4.000000E+01 8.000000E+00 0.000000E+00"
    if not mobilized:
        cstr = " 2 1 4.000000E+01 7.900000E+00 0.000000E+00"
    if not compressive_contact:
        cstr = " 2 1 -4.000000E+01 8.000000E+00 0.000000E+00"
    rows = "\n".join(cdis for _ in range(28))
    stresses = "\n".join(cstr for _ in range(28))

    def block(label, normal, shear):
        return (
            "statistics for slave set UPPER_BOTTOM, master set LOWER_TOP and time 1.0\n"
            f" {label} (fx,fy,fz)\n"
            "area, normal force (+ = tension) and shear force (size)\n"
            f" 9.907992E-01 {normal:.6E} {shear:.6E}\n"
        )

    output = (
        "S T E P 1\nINCREMENT 24\n"
        "relative contact displacement (slave element+face,normal,tang1,tang2) for all contact elements and time 1.0\n"
        f"{rows}\n"
        "contact stress (slave element+face,press,tang1,tang2) for all contact elements and time 1.0\n"
        f"{stresses}\n"
        "total number of contact elements for time 1.0\n28\n"
        + block("total surface force", -39.99462, 7.356406)
        + block("total normal surface force", -39.99462, 0.0)
        + block("total shear surface force", 0.0, 7.356406)
    )
    return (
        output.replace("total shear surface force", "missing shear surface force")
        if omit_cfs
        else output
    )


def _sliding_ccx_script(output):
    return "#!/bin/sh\ncat > model.dat <<'EOF'\n" + output + "EOF\n"


def _native_sliding_contact_history_output():
    valid = _native_sliding_contact_output()
    return "\n".join(
        valid.replace("time 1.0", f"time {(index + 1) / 24:.8f}") for index in range(24)
    )


def test_sliding_contact_public_job_refuses_substituted_solver_binary(tmp_path, monkeypatch):
    contract, evidence, manifest = _prepared_sliding_contact(tmp_path)
    binary = tmp_path / "ccx_2.23"
    binary.write_text(_sliding_ccx_script(_native_sliding_contact_output()))
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    job = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="sliding",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    with pytest.raises(RuntimeError, match="pinned CalculiX resource ccx_2.23 hash differs"):
        job.result(timeout=30)
    assert job.exit_code == 2
    assert not job.work_dir.exists()


def test_real_pinned_ccx_job_scores_sliding_contact_no_go(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_sliding_contact(tmp_path)
    result = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="sliding",
        evidence_root=evidence,
        package_manifest_path=manifest,
    ).result(timeout=130)
    assert result["status"] == "COMPLETED"
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["simulation_validity"] == "NO_GO"
    assert (
        next(
            row for row in result["comparisons"] if row["metric"] == "sliding_contact_coulomb_shear"
        )["relative_error"]
        > 0.05
    )


def test_real_pinned_ccx_job_scores_extended_sliding_contact_go(tmp_path):
    named = os.environ.get("LOCKWIRE_CCX")
    if not named:
        pytest.skip("set LOCKWIRE_CCX to the E5.4 packaged ccx_2.23 for the real Job gate")
    binary = Path(named)
    if (
        not binary.is_file()
        or hashlib.sha256(binary.read_bytes()).hexdigest() != analysis._PACKAGED_CCX_SHA256
    ):
        pytest.fail("LOCKWIRE_CCX does not match the E5.4 pinned executable")
    contract, evidence, manifest = _prepared_sliding_contact(tmp_path, extended=True)
    result = start_analysis_job(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="sliding",
        evidence_root=evidence,
        package_manifest_path=manifest,
    ).result(timeout=130)
    assert result["status"] == "COMPLETED"
    assert result["physical_conformance"] == "NOT_SCORED"
    assert result["simulation_validity"] == "GO"
    assert all(row["passed"] for row in result["comparisons"])
    assert result["comparisons"][-1]["relative_error"] < 0.05


def test_sliding_contact_parser_binds_cnum_and_refuses_missing_cfs_or_malformed_rows():
    valid = _native_sliding_contact_output()
    admission = analysis._SlidingContactAdmission()
    parsed = analysis._parse_sliding_contact_output(valid, admission)
    assert parsed["contact_rows"] == 28
    assert parsed["normal_force_n"] == pytest.approx(39.99462)
    assert parsed["positive_tangential_motion"] == 1.0
    assert parsed["compressive_contact"] == 1.0
    for output, reason in (
        (_native_sliding_contact_output(omit_cfs=True), "CF/CFN/CFS"),
        (valid.replace(" 2 1 -4.000000E-05 1.500000E-02 0.000000E+00", " 2 1 nope", 1), "CDIS"),
        (valid.replace("time 1.0\n28", "time 1.0\nnot-a-count", 1), "1 CNUM block"),
        (
            valid + "total number of contact elements for time malformed\nnot-a-count\n",
            "1 CNUM block",
        ),
        (valid.replace("time 1.0\n28", "time 1.0\n27", 1), "CNUM differs"),
        (
            valid.replace(" 2 1 4.000000E+01 8.000000E+00", " 2 1 0.000000E+00 8.000000E+00", 1),
            "positive pressure",
        ),
    ):
        with pytest.raises(AnalysisRefused, match=reason):
            analysis._parse_sliding_contact_output(output, admission)
    history = _native_sliding_contact_history_output()
    history_admission = analysis._SlidingContactAdmission(
        history_blocks=24, tangential_displacement_m=0.04
    )
    assert analysis._parse_sliding_contact_output(history, history_admission)["contact_rows"] == 28
    with pytest.raises(AnalysisRefused, match="CF/CFN/CFS blocks are malformed"):
        analysis._parse_sliding_contact_output(
            history.replace("total normal surface force", "total surface force", 1),
            history_admission,
        )


def test_sliding_contact_refuses_unbound_declaration_and_scores_missing_contact_status_no_go(
    tmp_path,
):
    contract, evidence, manifest = _prepared_sliding_contact(tmp_path, extended=True)
    revision = hashlib.sha256(contract.read_bytes()).hexdigest()
    prepared = prepare_analysis(
        contract,
        expected_revision=revision,
        procedure_id="sliding",
        evidence_root=evidence,
        package_manifest_path=manifest,
    )
    assert prepared["status"] == "PREPARED"
    assert prepared["artifacts"][0]["sha256"] == analysis._SLIDING_CONTACT_EXTENDED_SHA256
    evidence.joinpath("sliding.inp").write_bytes(
        evidence.joinpath("sliding.inp").read_bytes() + b"\n"
    )
    with pytest.raises(AnalysisRefused, match="sha256 does not match"):
        prepare_analysis(
            contract,
            expected_revision=revision,
            procedure_id="sliding",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )

    contract, evidence, manifest = _prepared_sliding_contact(tmp_path / "declared", extended=True)
    document = load_contract(contract).model_dump(mode="json")
    document["structural"]["loads"][0]["vector"]["values"][2] = -39
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="declared friction, normal load, or travel differs"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="sliding",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )
    document["structural"]["loads"][0]["vector"]["values"][2] = -40
    document["structural"]["contacts"][0]["tangential"]["friction_coefficient"]["value"] = 0.1
    save_contract(Contract.model_validate(document), contract)
    with pytest.raises(AnalysisRefused, match="declared friction, normal load, or travel differs"):
        prepare_analysis(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="sliding",
            evidence_root=evidence,
            package_manifest_path=manifest,
        )

    parsed = analysis._parse_sliding_contact_output(
        _native_sliding_contact_output(positive_sliding=False),
        analysis._SlidingContactAdmission(),
    )
    assert parsed["positive_tangential_motion"] == 0.0
    unmobilized = analysis._parse_sliding_contact_output(
        _native_sliding_contact_output(mobilized=False),
        analysis._SlidingContactAdmission(),
    )
    assert unmobilized["coulomb_mobilized_at_printed_precision"] == 0.0
    score = analysis._score_sliding_contact(
        load_contract(contract).structural,
        analysis._SlidingContactAdmission(tangential_displacement_m=0.04),
        parsed,
    )
    assert score["simulation_validity"] == "NO_GO"


@pytest.mark.parametrize("threshold", [5.0, 1e9])
def test_m13_b31_refuses_unbounded_relative_threshold(tmp_path, monkeypatch, threshold):
    """a relative threshold of 500% or more must not let a 400%-off tip read GO."""
    contract, evidence, manifest = _prepared_declared_b31_cantilever(
        tmp_path, limit_metric="b31_tip_uy", threshold=quantity(threshold, "1")
    )
    binary = tmp_path / "ccx_2.23"
    binary.write_text(
        "#!/bin/sh\n"
        "cat > model.dat <<'EOF'\n"
        " displacements (vx,vy,vz) for set TIP and time 1.0\n"
        " 5 0.000000E+00 -9.523810E-01 0.000000E+00\n"
        " forces (fx,fy,fz) for set ROOT and time 1.0\n"
        " 1 0.000000E+00 1.000000E+02 0.000000E+00\n"
        "EOF\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("LOCKWIRE_CCX", str(binary))
    monkeypatch.setattr(
        analysis, "_PACKAGED_CCX_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="static",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert result["simulation_validity"] == "NOT_SCORED"
    assert "threshold" in result["reason"]


@pytest.mark.parametrize("threshold", [5.0, 1e9])
def test_m13_two_dof_refuses_unbounded_relative_threshold(tmp_path, monkeypatch, threshold):
    """the two-DOF response limit must be bounded, not merely positive."""
    contract, evidence, manifest = _prepared_two_dof_response(
        tmp_path, "harmonic", threshold=quantity(threshold, "1")
    )
    _fake_two_dof_response_ccx(tmp_path, monkeypatch, "two_dof_harmonic.dat")
    with pytest.raises(
        AnalysisRefused, match="positive relative 'two_dof_harmonic_amplitude' limit up to 5"
    ):
        analysis.execute_direct_truss(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="two-dof-harmonic",
            evidence_root=evidence,
            package_manifest_path=manifest,
            work_dir=tmp_path,
        )


def test_m14_two_dof_go_names_the_reference_fixture(tmp_path, monkeypatch):
    """a two-DOF GO scores the pinned reference deck, so the reason says so."""
    contract, evidence, manifest = _prepared_two_dof_response(tmp_path, "harmonic")
    _fake_two_dof_response_ccx(tmp_path, monkeypatch, "two_dof_harmonic.dat")
    result = analysis.execute_direct_truss(
        contract,
        expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
        procedure_id="two-dof-harmonic",
        evidence_root=evidence,
        package_manifest_path=manifest,
        work_dir=tmp_path,
    )
    assert result["simulation_validity"] == "GO"
    assert result["reason"].endswith("(reference fixture, not your model)")


def test_m14_thick_shear_go_names_the_reference_fixture():
    """the shear verdict scores a hard-coded 1 MPa on a 1 m block, not the user's model."""
    NS = SimpleNamespace
    threshold = NS(base=lambda: 0.02, model_dump=lambda mode: {"value": 2, "unit": "%"})
    limit = NS(id="shear", provenance=NS(model_dump=lambda mode: {}), threshold=threshold)
    expected_u1 = 1e6 / (210e9 / 2.6)
    result = analysis._score_direct_thick_shear(
        NS(model=NS(source_frame="base")),
        {"declared_material": (210e9, 0.3), "acceptance_limit": limit},
        NS(requests={("RF", "SUPPORT", 2)}),
        {("U", "TOP", 1): (expected_u1, 0.0, 0.0), ("RF", "SUPPORT", 2): (-1e6, 0.0, 0.0)},
    )
    assert result["simulation_validity"] == "GO"
    assert result["reason"].endswith("(reference fixture, not your model)")


def test_l48_unknown_deck_template_is_refused_not_unbound(tmp_path, monkeypatch):
    """a template with no admission branch refuses by name instead of a TypeError."""
    contract, evidence, manifest = _prepared_declared_b31_cantilever(
        tmp_path, limit_metric="b31_tip_uy"
    )
    real = analysis.prepare_analysis
    monkeypatch.setattr(
        analysis, "prepare_analysis", lambda *a, **k: {**real(*a, **k), "deck_template": "bogus-v1"}
    )
    monkeypatch.setattr(
        analysis, "_DIRECT_DECK_TEMPLATES", analysis._DIRECT_DECK_TEMPLATES | {"bogus-v1"}
    )
    with pytest.raises(AnalysisRefused, match="bogus-v1"):
        analysis.execute_direct_truss(
            contract,
            expected_revision=hashlib.sha256(contract.read_bytes()).hexdigest(),
            procedure_id="static",
            evidence_root=evidence,
            package_manifest_path=manifest,
            work_dir=tmp_path,
        )


def test_impact_verdict_names_the_reference_fixture():
    """The impact verdict scores the pinned reference deck, so the reason says so."""
    NS = SimpleNamespace
    limit = analysis.ScalarAgreementLimit.model_construct(
        id="impact",
        metric="frictionless_elastic_impact",
        reference="analytic",
        comparator="relative",
        threshold=NS(
            dimension=lambda: "dimensionless",
            base=lambda: 0.02,
            model_dump=lambda mode: {"value": 2, "unit": "%"},
        ),
        provenance=NS(model_dump=lambda mode: {}),
    )
    ideal_compression = (2 * 0.5 / 1e7) ** 0.5
    parsed = {
        "effective_mass_kg": 1.0,
        "rebound_velocity_m_s": 1.0,
        "incident_velocity_m_s": -1.0,
        "reconstructed_energy_final_j": 0.5,
        "reconstructed_energy_max_j": 0.5,
        "reconstructed_energy_min_j": 0.5,
        "impulse_n_s": 2.0,
        "peak_compression_m": ideal_compression,
        "peak_force_n": 1e7 * ideal_compression,
        "release_tail_force_n": 0.0,
        "native_elke_final_j_diagnostic_only": 0.0,
    }
    result = analysis._score_frictionless_impact(NS(acceptance_limits=[limit]), parsed)
    assert result["simulation_validity"] == "GO"
    assert result["reason"].endswith("(reference fixture, not your model)")
