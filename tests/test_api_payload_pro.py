"""The paid half of the payload union (`lockwire_pro.api_payload_pro`): preflight, health, run
state, analysis, and the page fields only the paid app fills. The free half is
`test_api_payload.py`; this file holds every paid member to the same drift rules."""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
from pathlib import Path
from typing import Annotated, Any, Union

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

import lockwire
import lockwire_pro
from lockwire import plugins
from lockwire.api_payload import ALL_MEMBERS as FREE_MEMBERS
from lockwire.api_payload import MEMBERS as FREE_ENVELOPED
from lockwire.api_payload import NUMBER_KINDS as FREE_NUMBER_KINDS
from lockwire.api_payload import export_openapi
from lockwire.cli import app
from lockwire.page import build_payload
from typer.testing import CliRunner
from lockwire.schema import load_contract
from lockwire_pro import api_pro as api
from lockwire_pro.api_payload_pro import (
    ALL_MEMBERS,
    MEMBERS,
    NUMBER_KINDS,
    AnalysisJobStatePayload,
    AnalysisResultPayload,
    PagePayloadPro,
    RunStatePayload,
)
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording
from payload_sweep import STRING_FIGURE_EXEMPTIONS, SELF_KINDED, numeric_fields, string_figures
from test_api_payload import _lockwire_src_files, layout_reaching_number_slots

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
X500 = FIXTURES / "x500" / "contract.yaml"
CLEAN = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"

UNION = TypeAdapter(
    Annotated[
        Union[tuple([*FREE_ENVELOPED.values(), *MEMBERS.values()])],
        Field(discriminator="artifact_type"),
    ]
)


def _run_page() -> dict[str, Any]:
    contract = load_contract(CLEAN)
    findings = run_preflight(contract, RecordedSource(load_recording(CLEAN_BAG)))
    return build_payload(contract, preflight=(findings, "bags/clean"))


def test_plugin_registers_exactly_these_members():
    plugins.ensure()
    assert plugins.PAYLOAD_MEMBERS == MEMBERS
    assert set(plugins.PAYLOAD_EXTRA) == set(ALL_MEMBERS) - set(MEMBERS.values())


def test_paid_page_validates_plain_and_with_a_recording():
    PagePayloadPro.model_validate(build_payload(load_contract(X500), contract_path=X500))
    PagePayloadPro.model_validate(_run_page())


def test_mutation_bite_new_page_key_fails_the_paid_drift_test():
    """A key the paid page adds that `PagePayloadPro` doesn't describe must fail, at the top
    level and inside `run` (its own model)."""
    payload = _run_page()
    with pytest.raises(ValidationError):
        PagePayloadPro.model_validate(dict(payload, a_brand_new_key_the_page_never_had="x"))
    with pytest.raises(ValidationError):
        PagePayloadPro.model_validate(
            dict(payload, run=dict(payload["run"], a_brand_new_run_key="surprise"))
        )


def test_the_free_page_member_refuses_the_paid_fields():
    """The free `PagePayload` carries no run/health fields: the paid page is a different member."""
    from lockwire.api_payload import PagePayload

    assert not {"run", "health", "health_words", "health_devices"} & set(PagePayload.model_fields)
    with pytest.raises(ValidationError):
        PagePayload.model_validate(_run_page())


def test_paid_doors_have_a_union_member():
    assert (set(api.FUNCTIONS) - set(lockwire.api.FUNCTIONS)) >= {"preflight", "health"}
    for name, document in {
        "preflight": api.preflight(CLEAN, CLEAN_BAG),
        "health": api.health(X500),
    }.items():
        assert type(UNION.validate_python(document)).__name__ == f"{name.capitalize()}Payload"


def test_every_paid_member_artifact_type_is_its_key():
    for artifact_type, model in MEMBERS.items():
        assert artifact_type in str(model.model_fields["artifact_type"].annotation)


def test_extra_key_on_any_paid_member_is_rejected():
    page = build_payload(load_contract(CLEAN))
    run_state = {
        "status": "running",
        "exit_code": None,
        "rows_written": 0,
        "document": None,
        "output": "",
        "errors": "",
        "error": None,
        "health": api.health_document(CLEAN),
    }
    analysis_result = {
        "document_version": "1",
        "kind": "analysis_result",
        "status": "COMPLETED",
        "solver_started": True,
        "contract_revision": "a" * 64,
        "procedure_id": "static",
        "procedure_kind": "linear_static",
        "runtime_profile": "calculix",
        "package_manifest_sha256": "b" * 64,
        "solver": {},
        "deck_template": "direct-b31-square-v1",
        "artifacts": [],
        "physical_conformance": "NOT_SCORED",
        "simulation_validity": "NOT_SCORED",
        "output_sha256": "c" * 64,
        "results": [],
        "comparisons": [],
        "reason": "unsupported acceptance metric",
    }
    by_model: dict[str, dict] = {
        "PreflightPayload": api.preflight(CLEAN, CLEAN_BAG),
        RunStatePayload.__name__: run_state,
        AnalysisResultPayload.__name__: analysis_result,
        AnalysisJobStatePayload.__name__: {
            "status": "completed",
            "exit_code": 0,
            "rows_written": 0,
            "document": analysis_result,
            "output": "",
            "errors": "",
            "error": None,
        },
        PagePayloadPro.__name__: dict(
            page,
            **{
                k: v
                for k, v in _run_page().items()
                if k in ("run", "health", "health_words", "health_devices")
            },
        ),
    }
    by_model["HealthPayload"] = api.health(X500)
    permissive = []
    for model in ALL_MEMBERS:
        if model.__name__ not in by_model:
            continue
        document = by_model[model.__name__]
        try:
            model.model_validate(dict(document, a_key_no_producer_emits="surprise"))
        except ValidationError:
            continue
        permissive.append(model.__name__)
    assert not permissive, f"paid members that accepted an unknown key: {permissive}"


def test_every_paid_number_carries_its_kind():
    found = numeric_fields(ALL_MEMBERS)
    own = {key for key in found if key not in FREE_NUMBER_KINDS}
    assert own <= set(NUMBER_KINDS), (
        f"paid numbers with no number kind: {sorted(own - set(NUMBER_KINDS))}"
    )
    stale = sorted(set(NUMBER_KINDS) - set(found))
    assert not stale, f"paid NUMBER_KINDS rows for fields that no longer exist: {stale}"
    assert all(row["by"] for row in NUMBER_KINDS.values())


#: Paid-only fields the free sweep never sees, so the free list's two-way (stale)
#: check cannot hold them. Not stale-checked: whether a sha256 opens with a digit
#: depends on the contract bytes, which change with any schema edit.
PAID_STRING_FIGURE_EXEMPTIONS = {
    "ClaimContext.contract_revision": "sha256 of the exact contract bytes: an identifier, not a magnitude",
}


def test_the_paid_page_and_actuator_documents_carry_no_preformatted_number(tmp_path):
    from test_actuator_check_transport import _path_backed_case, _selection

    _contract, path, evidence_ref, evidence_root, revision = _path_backed_case(tmp_path)
    from lockwire_pro.api_payload_pro import CheckPayloadPro as CheckPayload

    actuator = CheckPayload.model_validate(
        api.check(
            path,
            actuator_evidence=_selection(evidence_ref),
            evidence_root=evidence_root,
            expected_revision=revision,
        )
    )
    unnamed = {}
    for name, document in {
        "actuator": actuator,
        "page+run": PagePayloadPro.model_validate(_run_page()),
    }.items():
        for key, value in string_figures(document).items():
            if key not in STRING_FIGURE_EXEMPTIONS | PAID_STRING_FIGURE_EXEMPTIONS.keys():
                unnamed[key] = (name, value)
    assert not unnamed, f"preformatted numbers outside the named exemptions: {unnamed}"


def test_the_paid_schema_document_names_every_paid_member(tmp_path):
    export_openapi(tmp_path / "schema.json")
    document = json.loads((tmp_path / "pro" / "schema.json").read_text(encoding="utf-8"))
    for model in ALL_MEMBERS:
        assert model.__name__ in document["components"]["schemas"]
    assert document["x-lockwire-members"] == {k: m.__name__ for k, m in MEMBERS.items()}
    assert document["x-lockwire-number-kinds"] == NUMBER_KINDS
    free = json.loads((tmp_path / "schema.json").read_text(encoding="utf-8"))
    assert not {m.__name__ for m in ALL_MEMBERS} & {m.__name__ for m in FREE_MEMBERS}
    assert not {m.__name__ for m in ALL_MEMBERS} & set(free["components"]["schemas"])


def test_no_paid_self_kinding_record_hides():
    found: set[type[BaseModel]] = set()
    for info in pkgutil.walk_packages(lockwire_pro.__path__, prefix="lockwire_pro."):
        module = importlib.import_module(info.name)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and "_carries_its_word" in obj.__dict__
            ):
                found.add(obj)
    assert found <= set(SELF_KINDED), sorted(m.__name__ for m in found - set(SELF_KINDED))


def test_no_layout_value_reaches_a_paid_number_slot():
    files = _lockwire_src_files(("lockwire_pro",))
    assert files
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        violations.extend(layout_reaching_number_slots(tree, path.name))
    assert not violations, violations


def test_api_schema_command_emits_every_union_member(tmp_path):
    """The CLI door for the same export — `lockwire api-schema --out` is what
    `packaging/generate-types.md` runs, so it must emit the union and not just
    the page member it emitted before this box."""
    out = tmp_path / "schema.json"
    result = runner.invoke(app, ["api-schema", "--out", str(out)])
    assert result.exit_code == 0, result.output
    schemas = json.loads(out.read_text(encoding="utf-8"))["components"]["schemas"]
    missing = [m.__name__ for m in FREE_MEMBERS if m.__name__ not in schemas]
    assert not missing, f"members missing from `lockwire api-schema`: {missing}"


APP_SCHEMA = Path(__file__).parent.parent / "app" / "src" / "renderer" / "schema.json"


def test_committed_renderer_schema_is_not_stale(tmp_path):
    """The runtime validator's committed schema byte-matches its exporter."""
    generated = tmp_path / "schema.json"
    export_openapi(generated)
    assert APP_SCHEMA.read_bytes() == generated.read_bytes()
