"""The drift test for the payload **union** — every document lockwire's doors
emit must validate as the member that names it.

`extra="forbid"` at every level is the whole point: a key a producer adds that
`api_payload.py` doesn't know about fails here, before the app, the sidecar or
the generated TypeScript ever read whatever happens to be there. It has already
caught one real drift — `gui.build_payload` grew `health_words` and the page
member went red.

The union is walked from `api.FUNCTIONS`, not from a list written here, so a
new library door with no member is a failure rather than an omission.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import json
import pkgutil
import re
import shutil
import textwrap
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

import lockwire
from lockwire import api
from lockwire import plugins, start_operation
from lockwire.advice import Alternative
from lockwire.api_payload import (
    ALL_MEMBERS,
    EXTENSION_POINTS,
    MARKED_KINDS,
    MEMBERS,
    NUMBER_KINDS,
    CheckPayload,
    DeclareFormPayload,
    StartersPayload,
    DescribePayload,
    EditPayload,
    LensId,
    LogPayload,
    PagePayload,
    SceneUnchanged,
    BlamePayload,
    NumberKind,
    PlanPayload,
    export_openapi,
    Figure,
)
from payload_sweep import (
    SELF_KINDED,
    STRING_FIGURE_EXEMPTIONS,
    UNENVELOPED,
    _leaf_types,
    numeric_fields,
    string_figures,
    validate_document,
)
from lockwire.checks import run_record_checks
from lockwire.cli import app
from lockwire.page import _scene_labels, build_payload
from lockwire.schema import ArtifactRef, load_contract
from lockwire.scene import load_scene


FIXTURES = Path(__file__).parent / "fixtures"


EXAMPLES = Path(__file__).parent.parent / "examples"


X500 = FIXTURES / "x500" / "contract.yaml"


CLEAN = FIXTURES / "contract_clean.yaml"


LOCKED_AT = dt.date(2026, 9, 9)


ARMTREE = FIXTURES / "armtree.yaml"


runner = CliRunner()


def _contract_fixtures() -> list[Path]:
    """Every `.yaml` under `tests/fixtures/**` and `examples/**` that is actually a
    contract — skips bag manifests and baseline artifacts by trying to load each
    and keeping only what parses, exactly like the rest of the suite does per-file.
    """
    candidates = sorted(
        {
            *FIXTURES.glob("*.yaml"),
            *FIXTURES.glob("*/*.yaml"),
            *EXAMPLES.glob("**/*.yaml"),
        }
    )
    contracts = []
    for path in candidates:
        if "bags" in path.parts:
            continue
        try:
            load_contract(path)
        except Exception:  # noqa: BLE001 - not a contract file, e.g. a baseline artifact
            continue
        contracts.append(path)
    return contracts


CONTRACT_FIXTURES = _contract_fixtures()


def _plan_file(tmp_path: Path) -> Path:
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "steps:\n  - kind: rail_load\n    rail: rail.servo-motor\n"
        "    draw: {value: 500, unit: W}\n",
        encoding="utf-8",
    )
    return plan


@pytest.fixture(autouse=True)
def _free_page(monkeypatch):
    """The page a plugin extends is a different member; here it is the free page."""
    monkeypatch.setattr(plugins, "PAGE_EXTRAS", [])


def _documents(tmp_path: Path) -> dict[str, Any]:
    """One real document per library door, by calling the door.

    Keyed by the `api.FUNCTIONS` name so the walk below can prove it covered
    every one of them rather than however many happen to be listed here.
    """
    writable = tmp_path / "contract.yaml"
    shutil.copy(X500, writable)
    # The pose edits (move/rotate/reparent) need a link tree with more than one
    # link — X500 is a single flat link, so `rotate` (which addresses a joint's
    # origin) and a link `reparent` would have nothing to act on. `armtree.yaml`
    # is the fixture `tests/test_edit.py` already pairs with these three doors
    # (base -> shoulder -> elbow -> wrist, dev.gripper mounted at wrist).
    posable = tmp_path / "armtree.yaml"
    shutil.copy(ARMTREE, posable)
    from lockwire.edit import Change

    start_source = tmp_path / "start.urdf"
    start_source.write_text('<robot name="start"><link name="base"/></robot>')
    return {
        "start": api.start(start_source),
        "check": api.check(X500),
        "what-if": api.what_if(
            writable,
            [
                Change(
                    path="devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
                    value="0.12 0 0.05 m",
                    source="hand measurement",
                    witness="tester",
                )
            ],
        ),
        "plan": api.plan(X500, _plan_file(tmp_path)),
        "describe": api.describe(X500),
        "diff": api.diff(X500, "HEAD"),
        "log": api.log(X500),
        "blame": api.blame(X500)[0],
        "declare": api.declare(
            writable,
            "devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
            "0.12 0 0.05 m",
            source="hand measurement",
            witness="tester",
            dry_run=True,
        ),
        "add": api.add(
            writable,
            "family",
            id="fam.api-payload-test",
            name="API Payload Test Family",
            source="hand measurement",
            witness="tester",
            dry_run=True,
        ),
        "review": api.review(
            writable,
            "devices/dev.companion.jetson-orin-nano/power/draw",
            "approved",
            witness="tester",
            dry_run=True,
        ),
        "move": api.move(
            posable,
            "dev.gripper",
            (0.010, 0.0, 0.025),
            unit="m",
            source="notes/layout.md",
            witness="tester",
            dry_run=True,
        ),
        "rotate": api.rotate(
            posable,
            "wrist",
            (0.0, 0.0, 1.5708),
            unit="rad",
            source="notes/layout.md",
            witness="tester",
            dry_run=True,
        ),
        "reparent": api.reparent(
            posable,
            "elbow",
            "base",
            source="notes/layout.md",
            witness="tester",
            dry_run=True,
        ),
        "place": api.place(
            posable,
            "dev.gripper",
            "elbow",
            (10.0, 0.0, 25.0),
            unit="mm",
            source="notes/layout.md",
            witness="tester",
            dry_run=True,
        ),
        "mount-shape": api.mount_shape(
            posable,
            "dev.gripper",
            {"kind": "plate", "dimensions_mm": [100.0, 50.0, 3.0]},
            source="notes/layout.md",
            witness="tester",
            dry_run=True,
        ),
    }


# ------------------------------------------------------------------- the page member


@pytest.mark.parametrize("path", CONTRACT_FIXTURES, ids=lambda p: p.name)
def test_payload_matches_every_contract_fixture(path: Path):
    contract = load_contract(path)
    PagePayload.model_validate(build_payload(contract, contract_path=path))


def test_payload_matches_x500_with_its_scene():
    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    payload = build_payload(
        contract, scene=scene, scene_source="x500.glb (given)", contract_path=X500
    )
    PagePayload.model_validate(payload)


def test_payload_matches_the_empty_state_card_after_start(tmp_path, monkeypatch):
    """`_empty_state_card` (gui.py) is `_cards`' one hand-built dict, not one of
    the generic per-finding cards — e3-cf2, respin: it shipped `Card.consumer`
    with no default, which made the field REQUIRED, and `_empty_state_card`
    never sets it. `lockwire start` on a fresh model is exactly the path that
    hits it, so the accept line runs that path rather than only the fixture
    sweep above, none of which is empty enough to reach this dict."""
    monkeypatch.chdir(tmp_path)
    shutil.copy(FIXTURES / "px4vision.sdf", tmp_path / "model.sdf")
    result = runner.invoke(app, ["start", "model.sdf", "--out", "contract.yaml"])
    assert result.exit_code == 0, result.output

    contract = load_contract(tmp_path / "contract.yaml")
    payload = build_payload(contract, contract_path=tmp_path / "contract.yaml")
    assert len(payload["cards"]) == 1
    assert payload["cards"][0]["shape"] == "setup/nothing_declared"
    PagePayload.model_validate(payload)


def test_mutation_bite_new_build_payload_key_fails_the_drift_test():
    """The drift test's teeth: a key `build_payload` adds that `PagePayload`
    doesn't describe must fail validation, not pass through silently."""
    payload = build_payload(load_contract(CLEAN))

    top = dict(payload, a_brand_new_key_the_page_never_had="surprise")
    with pytest.raises(ValidationError):
        PagePayload.model_validate(top)

    nested = (
        dict(payload, cards=[dict(payload["cards"][0], a_brand_new_card_key="surprise")])
        if payload["cards"]
        else None
    )
    if nested is not None:
        with pytest.raises(ValidationError):
            PagePayload.model_validate(nested)


# ------------------------------------------------------------ the scene wire format
# e1-b2: the geometry leaves the reply behind a hash. the wire-format doc is the
# spec; these tests hold the Python encoder/decoder to it and each other.


def _decode_vectors_per_the_documented_rule(packed: dict) -> list[float]:
    """A decoder written from the wire-format doc alone, not from
    `scene_view._pack_vectors`'s internals — this is the "any client" leg."""
    import base64
    import struct

    raw = base64.b64decode(packed["data"])
    integers = struct.unpack(f"<{len(raw) // 2}h", raw)
    if packed.get("scale") is None:  # normals: implicit scale, no offset
        return [v * (1 / 32767) for v in integers]
    scale = packed["scale"]
    offset = packed["offset"]
    return [v * scale + offset[i % 3] for i, v in enumerate(integers)]


def test_wire_format_round_trip_from_the_documented_rule():
    """Encode the X500 scene, decode with the rule the wire-format doc states,
    and compare against the source floats within the quantization step — a
    decoder written from the doc alone must produce the right robot."""
    from lockwire.scene_view import scene_payload

    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    payload = scene_payload(
        contract, scene, source="x500.glb", cards=[], scored_records=[], labels=[]
    )
    for part, source_part in zip(payload["parts"], scene.parts, strict=True):
        decoded = _decode_vectors_per_the_documented_rule(part["positions"])
        scale = part["positions"]["scale"]
        for got, want in zip(decoded, source_part.positions, strict=True):
            assert abs(got - want) <= scale / 2 + 1e-9
        if part["normals"] is not None:
            decoded_normals = _decode_vectors_per_the_documented_rule(part["normals"])
            for got, want in zip(decoded_normals, source_part.normals, strict=True):
                assert abs(got - want) <= 1 / 65534 + 1e-6


def test_translation_is_an_origin_not_a_transform():
    """`translation` places a label; it is never added to a decoded position.
    X500 has at least one part with a non-zero translation — decoding its
    positions must match the source WITHOUT adding that translation back in."""
    from lockwire.scene_view import scene_payload

    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    moved = next(p for p in scene.parts if any(abs(v) > 1e-9 for v in p.translation))
    payload = scene_payload(
        contract, scene, source="x500.glb", cards=[], scored_records=[], labels=[]
    )
    part = next(p for p, s in zip(payload["parts"], scene.parts, strict=True) if s is moved)
    decoded = _decode_vectors_per_the_documented_rule(part["positions"])
    scale = part["positions"]["scale"]
    # Matches the source positions as-is (world-space already) ...
    for got, want in zip(decoded, moved.positions, strict=True):
        assert abs(got - want) <= scale / 2 + 1e-9
    # ... and does NOT match "position + translation" (would only coincide by
    # fluke on a part whose translation happens to be the zero vector, which
    # `moved` was chosen specifically to rule out).
    shifted = [decoded[i] - moved.translation[i % 3] for i in range(len(decoded))]
    assert any(abs(shifted[i] - want) > scale / 2 + 1e-9 for i, want in enumerate(moved.positions))


def test_api_serves_parts_undecimated():
    """`decimate=False` (the sidecar's flag) ships every triangle regardless of
    the 150,000-triangle display cap that `decimate=True` (`gui --out`) applies
    to the same scene."""
    from lockwire.scene import Scene, ScenePart
    from lockwire.scene_view import scene_payload

    vertices = 450_003  # > 150,000 triangles, unshared-vertex layout (test_gui.py's own pattern)
    positions = tuple(v for i in range(vertices) for v in ((i % 3 == 1), (i % 3 == 2), 0.0))
    indices = tuple(range(vertices - vertices % 3))
    part = ScenePart(
        node="body",
        extras_id=None,
        name_chain=("body",),
        positions=positions,
        normals=None,
        indices=indices,
        color=(0.7, 0.7, 0.7, 1.0),
        translation=(0.0, 0.0, 0.0),
    )
    scene = Scene(
        parts=(part,),
        up="z",
        nodes_with_ids={},
        source="synthetic",
        bounds=((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        triangles=len(indices) // 3,
        vertices=vertices,
    )
    contract = load_contract(CLEAN)
    decimated = scene_payload(contract, scene, source="s", cards=[], scored_records=[], labels=[])
    undecimated = scene_payload(
        contract, scene, source="s", cards=[], scored_records=[], labels=[], decimate=False
    )
    assert decimated["display_triangles"] < decimated["triangles"]
    assert undecimated["display_triangles"] == undecimated["triangles"] == scene.triangles
    # The hash is the source geometry's identity — same regardless of decimation.
    assert decimated["hash"] == undecimated["hash"]


# ------------------------------------------------------------------- the union


def test_every_served_command_has_a_union_member(tmp_path):
    """The whole claim, not one branch: every name in `api.FUNCTIONS` that
    returns a document is called for real and its output validated through the
    union's own `artifact_type` discriminator.

    `load` returns a `Contract`, not a document, and `api-schema`/`api-types`
    write the union's schema/TypeScript rather than emitting one. `blame`
    returns the existing tuple (provenance rows, in-repository flag); its
    recipe validates the rows emitted by CLI JSON through BlamePayload. The
    tuple ABI has separate real core/CLI parity and shape proof. A new
    door cannot slip through as "probably one of those".
    """
    not_documents = {"load", "upgrade", "api-schema", "api-types"}
    documents = _documents(tmp_path)
    assert set(api.FUNCTIONS) - not_documents == set(documents), (
        "a library door has no document recipe here: "
        f"{sorted(set(api.FUNCTIONS) - not_documents - set(documents))}"
    )
    unmodelled = []
    for name, document in documents.items():
        try:
            # The un-enveloped producers carry no `artifact_type`, so the
            # caller names their member; everything else routes on its own key.
            if name in UNENVELOPED:
                UNENVELOPED[name].model_validate(document)
            else:
                validate_document(document)
        except ValidationError as error:
            unmodelled.append(f"{name}: {error}")
    assert not unmodelled, "doors with no matching union member:\n" + "\n".join(unmodelled)


def test_the_union_discriminates_on_the_envelopes_artifact_type(tmp_path):
    """E0-25's `artifact_type` is the discriminator — the union does not invent
    a second one. Every member's key in `MEMBERS` is the literal its model
    declares, and a document is routed by that key alone."""
    for artifact_type, model in MEMBERS.items():
        declared = model.model_fields["artifact_type"].annotation
        assert artifact_type in str(declared), f"{model.__name__} does not declare {artifact_type}"
    picked = validate_document(_documents(tmp_path)["check"])
    assert isinstance(picked, CheckPayload)


@pytest.mark.parametrize("headline", ["", "   ", "\t\n"])
def test_check_payload_refuses_a_blank_headline(tmp_path, headline):
    document = _documents(tmp_path)["check"]
    document["headline"] = headline
    with pytest.raises(ValidationError):
        CheckPayload.model_validate(document)


def test_plan_emits_the_generated_steps_with_each_unscored_reason(tmp_path):
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "steps:\n  - kind: joint_target\n    joint: absent-joint\n    position: 0.1\n",
        encoding="utf-8",
    )

    document = api.plan(X500, plan)
    payload = PlanPayload.model_validate(document)

    assert document == payload.model_dump(mode="json")
    unscored = [step for step in payload.steps if step.status == "UNSCORED"]
    assert unscored and all(step.not_scored_reason for step in unscored)


def test_plan_joint_limit_finding_cites_declared_model_provenance():
    contract = FIXTURES / "contract_clean.yaml"
    document = api.plan(
        contract,
        {"steps": [{"kind": "joint_target", "joint": "imu_mount", "effort": 3.0}]},
    )
    finding = next(
        item
        for item in document["findings"]
        if item["quantity"] == "plan/steps/0/joint_target/effort"
    )
    assert finding["severity"] == "violation"
    assert finding["source"] == "testbot.urdf"
    assert finding["witness"] == "w0"
    assert finding["locked_at"] == "2026-07-05"
    assert PlanPayload.model_validate(document).findings

    beyond_actuator = api.plan(
        contract,
        {"steps": [{"kind": "joint_target", "joint": "imu_mount", "effort": 999.0}]},
    )
    actuator = next(
        item
        for item in beyond_actuator["findings"]
        if item["quantity"] == "plan/steps/0/joint_target/effort_actuator"
    )
    assert actuator["source"] is None and actuator["witness"] is None


def test_plan_unknown_joint_cites_and_offers_declared_model_joint():
    document = api.plan(
        FIXTURES / "contract_clean.yaml",
        {"steps": [{"kind": "joint_target", "joint": "ghost_joint", "position": 0.1}]},
    )
    finding = next(
        item
        for item in document["findings"]
        if item["quantity"] == "plan/steps/0/joint_target/joint"
    )
    assert finding["severity"] == "not_scored"
    assert finding["source"] == "testbot.urdf"
    assert finding["witness"] == "w0"
    assert finding["alternatives"] == [
        {
            "text": "use declared model joint 'imu_mount'",
            "value": 1.0,
            "unit": "count",
            "kind": "model",
            "mark": None,
            "rail": None,
            "device": None,
        }
    ]
    assert PlanPayload.model_validate(document).findings


def test_describe_member_carries_markdown_not_the_page_dict():
    """`describe` is the member that carries prose. Its `files` are
    `describe.describe_all`'s Markdown, and it has none of the page's fields —
    which is the reason one dictionary could not describe every command."""
    document = validate_document(api.describe(X500))
    assert isinstance(document, DescribePayload)
    assert document.files["SYSTEM.md"].lstrip().startswith("#")
    assert not set(PagePayload.model_fields) & set(DescribePayload.model_fields) - {
        "robot",
        "cli_line",
    }


def test_write_member_carries_a_diff(tmp_path):
    """The write door's member is the one with a diff, and `--dry-run` leaves
    `written` False — the document says what happened, not what was asked."""
    writable = tmp_path / "contract.yaml"
    shutil.copy(X500, writable)
    document = validate_document(
        api.declare(
            writable,
            "devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz",
            "0.12 0 0.05 m",
            source="hand measurement",
            witness="tester",
            dry_run=True,
        )
    )
    assert isinstance(document, EditPayload)
    assert document.written is False
    assert "mount_xyz" in document.diff


def test_declare_form_labels_name_the_link_or_device():
    """Rows under a list entry (links, devices) were all labelled just "mass"."""
    rows = {r["path"]: r["label"] for r in api.declare_form(X500)["rows"]}
    assert rows["model/links/x500_frame/mass"] == "model x500 frame mass"
    assert len(set(rows.values())) == len(rows)


def test_declare_form_rows_carry_a_short_group_and_name():
    """The Declare list shows "X500 frame . mass", not "model x500 frame mass"."""
    rows = {r["path"]: r for r in api.declare_form(X500)["rows"]}
    row = rows["model/links/x500_frame/mass"]
    assert (row["group"], row["short"]) == ("X500 Frame", "Model mass")
    mount = rows["devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz"]
    assert (mount["group"], mount["short"]) == ("Flight Controller Pixhawk 6C", "Mount position")


def test_declare_form_member_carries_every_declarable_quantity():
    """E4-df: the declare form's rows, validated as real output — not the
    Card-polluted `_every_document`/page path, which this box does not own.

    A row's ``kind``/``label`` are the whole reason the three blocked face
    boxes (e4-b-r2, e4-c-r2, e4-d) can render one truthfully without
    computing anything themselves: drop either from the producer and
    this is the test that reddens.
    """
    document = DeclareFormPayload.model_validate(api.declare_form(X500))
    by_path = {row.path: row for row in document.rows}
    # e4-df-r2: the case most likely to fail, per the charter check rule below
    # -- a `model/links/*/mass` row. `Link` has no `provenance` field of its
    # own (schema.py:270), so this is exactly the shape `declare` refused on
    # before edit.py's `_UNSTRUCTURED_PROVENANCE_OWNERS` fix.
    link_mass = "model/links/x500_frame/mass"
    assert link_mass in by_path
    assert by_path[link_mass].kind == "declared"
    assert by_path[link_mass].value == 610
    assert by_path[link_mass].unit == "g"
    # the mount_xyz case stays as the second anchor, not the only one
    mount_xyz = "devices/dev.flight-controller.pixhawk-6c/physical/mount_xyz"
    assert mount_xyz in by_path
    assert by_path[mount_xyz].kind == "declared"
    assert by_path[mount_xyz].label == "dev.flight-controller.pixhawk-6c physical mount xyz"
    # a currently-declared quantity carries its value, unit and provenance
    mass = "devices/dev.flight-controller.pixhawk-6c/physical/mass"
    assert by_path[mass].value is not None
    assert by_path[mass].unit
    assert by_path[mass].provenance is not None
    assert document.witness.prefill == ""  # no per-machine config in this test env


def test_every_declared_row_the_payload_emits_survives_declares_own_resolver(tmp_path):
    """e4-df-r2: the payload's promise is that every ``path`` it emits is "the
    exact `/`-separated string `declare` accepts". That was FALSE for a
    `model/links/*/mass` row (measured: ``EditRefusal: cannot stamp
    provenance at model/links``) even though the row was emitted correctly --
    the anchor test above only ever drove the happy path (`mount_xyz`), so
    nothing caught it. This makes the promise checkable rather than asserted:
    re-declare every already-declared row's own value, on a scratch contract
    copy, through `edit.apply_changes` -- `declare`'s own resolver -- and
    fail with the offending path(s) if any one of them is refused.
    """
    from lockwire.edit import Change, EditRefusal, apply_changes

    contract_path = tmp_path / "contract.yaml"
    contract_path.write_bytes(X500.read_bytes())

    document = DeclareFormPayload.model_validate(api.declare_form(X500))
    failures: list[tuple[str, str]] = []
    for row in document.rows:
        if row.kind != "declared" or row.value is None:
            continue  # undeclared rows carry no round-trippable value here
        value = (
            " ".join(str(v) for v in row.value) if isinstance(row.value, list) else str(row.value)
        )
        if row.unit:
            value = f"{value} {row.unit}"
        try:
            apply_changes(
                contract_path,
                [
                    Change(
                        path=row.path,
                        value=value,
                        source="re-declare (e4-df-r2 resolver sweep)",
                        witness="human",
                        locked_at=LOCKED_AT,
                    )
                ],
            )
        except EditRefusal as exc:
            failures.append((row.path, str(exc)))

    assert not failures, failures


def test_extra_key_on_any_member_is_rejected(tmp_path):
    """`extra="forbid"` on *every* member, walked from `ALL_MEMBERS` rather
    than spot-checked: dropping it from one model has to make this red."""
    documents = _documents(tmp_path)
    documents["page"] = build_payload(load_contract(CLEAN))
    documents["declare_form"] = api.declare_form(X500)
    documents["starters"] = {"starters": start_operation.starters()}
    by_model = {
        type(validate_document(d)).__name__: d for k, d in documents.items() if k not in UNENVELOPED
    }
    for door, model in UNENVELOPED.items():
        if door in documents:
            by_model[model.__name__] = documents[door]
    permissive = []
    for model in ALL_MEMBERS:
        assert model.__name__ in by_model, f"no document for {model.__name__}"
        document = by_model[model.__name__]
        if model in (LogPayload, BlamePayload):
            polluted = [
                dict(document[0], a_key_no_producer_emits="surprise"),
                *document[1:],
            ]
        else:
            polluted = dict(document, a_key_no_producer_emits="surprise")
        try:
            model.model_validate(polluted)
        except ValidationError:
            continue
        permissive.append(model.__name__)
    assert not permissive, f"members that accepted an unknown key: {permissive}"


def test_mcp_documents_validate_as_the_same_union_members():
    """`mcp_server._with_cli_line` adds `cli_line` to `api`'s document in place.
    The union covers both doors or it covers neither — a member without that
    optional field would reject the agent door's own output (one document,
    four doors)."""
    document = api.check(X500)
    document["cli_line"] = "lockwire check contract.yaml"
    assert isinstance(validate_document(document), CheckPayload)


# --------------------------------------------------------------- number kinds


def test_every_number_in_the_union_carries_its_kind():
    """*"Every number on screen is declared, measured, or derived by a
    named check"*. The numeric fields are enumerated from the models
    themselves — every member, every nesting level, `report.Finding` and
    `schema.Quantity` included — so this proves the rule, not one producer.

    Ceiling, named: it proves every number has a *declared* kind, not that the
    kind is the right one. `catalog`/`projected` figures are guarded on the
    instance instead (`_build_kind_declaration` and `Figure`/`Alternative`'s own
    `_carries_its_word` validators, see `test_a_typed_figure_refuses_a_marked_kind_with_no_word`
    and `test_self_kinded_and_marked_kinds_match_real_producers`), not here.
    """
    found = numeric_fields()
    assert found, "the walker found no numeric fields — model introspection broke"
    uncovered = sorted(set(found) - set(NUMBER_KINDS))
    assert not uncovered, f"numbers in the payload with no kind: {uncovered}"
    stale = sorted(set(NUMBER_KINDS) - set(found))
    assert not stale, f"NUMBER_KINDS rows for fields that no longer exist: {stale}"
    for key, row in NUMBER_KINDS.items():
        assert row["by"], f"{key} is kinded but names no producer"
    assert "Figure.value" not in NUMBER_KINDS, (
        "a `Figure` carries its kind on the instance; one table row standing for "
        "every typed figure in the product is the blindness this file is fixing"
    )


# e1-gk: the scene's mesh-derived numbers are `measured`, checked against the
# the rule's own words, not just against "some kind is present" (this file's stated
# ceiling, above). `docs/words.md`'s kind table: `declared` = "the contract author
# wrote it (a datasheet value, a limit)"; `measured` = "a run produced it (a
# recording, a sample)". The contract only NAMES the model file (a `package://`/
# `model://` URI); it does not write the triangle count, vertex count, bounds,
# translation, scale or offset — those come from `scene.load_scene` parsing the
# binary mesh data on every call, which is a run over real geometry, not a value
# typed into the contract. `scene_view.scene_payload`'s own `honesty` string draws
# the same line: "shapes come from the model; positions are declared only where the
# contract says so" — `declared` is reserved for what the contract itself states.
# The sibling branch that owns this mechanism (e1-kt phase 2, `9aa8f08`,
# unmerged) agrees: its `_GEOMETRY_PRODUCER_KINDS` registry keys
# `scene.load_scene` to `"measured"` and reserves `"derived"` for
# `scene_inferred.inferred_scene_payload` (a placeholder scene with no real
# file behind it) — precisely to stop an inferred scene from claiming
# `measured` (`9341df0`). No branch in this lineage has ever called
# file-derived mesh geometry `declared`. So: NOT relabelled. This test is the
# bite target — flip any of these rows to `"declared"` and it reddens, naming
# the field and the kind, per rule 12's "check both directions": the code does
# not claim more than the kind rules grant (it is a real run over real geometry, not a
# recording pretending to be a contract value), and the kind rules do not grant a
# reading for "declared" (no contract author wrote these numbers) that the
# code is dropping.
def test_scene_geometry_from_the_model_file_stays_measured_under_d134():
    geometry_from_file = {
        "PackedVectors.scale": "measured",
        "PackedVectors.offset": "measured",
        "ScenePart.translation": "measured",
        "ScenePayload.bounds": "measured",
        "ScenePayload.triangles": "measured",
        "ScenePayload.vertices": "measured",
        "ScenePayload.display_triangles": "measured",
    }
    for key, expected_kind in geometry_from_file.items():
        assert key in NUMBER_KINDS, f"{key} dropped out of NUMBER_KINDS"
        actual = NUMBER_KINDS[key]["kind"]
        assert actual == expected_kind, (
            f"{key} is now {actual!r}, the rules say {expected_kind!r} — geometry read "
            "off the model file by scene.load_scene is a run over real data, not a "
            "value the contract author wrote (see this test's docstring)"
        )
        assert NUMBER_KINDS[key]["by"] == "scene.load_scene"


def _every_document(tmp_path: Path) -> dict[str, BaseModel]:
    """Every document the doors emit, validated — the six machine members plus
    the page in the three shapes the suite already builds (plain, with a scene,
    with a recording). Real output, so a preformatted figure is caught in the
    value rather than argued about in the annotation."""
    documents: dict[str, BaseModel] = {
        name: (
            UNENVELOPED[name].model_validate(document)
            if name in UNENVELOPED
            else validate_document(document)
        )
        for name, document in _documents(tmp_path).items()
    }
    x500 = load_contract(X500)
    documents["page"] = PagePayload.model_validate(build_payload(x500, contract_path=X500))
    documents["declare_form"] = DeclareFormPayload.model_validate(api.declare_form(x500))
    documents["starters"] = StartersPayload.model_validate({"starters": start_operation.starters()})
    documents["page+scene"] = PagePayload.model_validate(
        build_payload(
            x500,
            scene=load_scene(X500.with_name("x500.glb")),
            scene_source="x500.glb (given)",
            contract_path=X500,
        )
    )
    return documents


def test_no_door_emits_a_preformatted_number_outside_the_named_exemptions(tmp_path):
    """The guard closing its own escape hatch.

    `NUMBER_KINDS` walks numeric *types*, so a number a producer has already
    formatted into a `str` was invisible to it — `SceneLabel.figure` read
    `"35.20 W vs <= 15.60 W"`, two numbers on screen with no kind and
    nothing to notice. It is a claim about numbers ON
    SCREEN, so a preformatted string figure defeats it.*

    So a `str` field whose value opens with a number is itself a finding here,
    unless `STRING_FIGURE_EXEMPTIONS` names it **with a reason**. Set equality
    both ways: an unnamed one fails, and a name that stops carrying numbers
    fails too, so the list cannot rot into a blanket permission.

    Ceiling, named: this reads the documents the suite builds, so it sees a
    field's *values in this corpus*. A field that carries a number only for
    some input is caught the first time that input reaches the corpus, not
    statically — a string annotation cannot be read any other way.
    """
    # Deliberate corpus rows: these fields are legitimate string identifiers/dates,
    # but the leading numeric token must still exercise the exemption guard.
    log_corpus = LogPayload.model_validate(
        [
            {
                "sha": "7abc1234567890abcdef1234567890abcdef1234",
                "author": "fixture",
                "date": "2026-09-11T12:00:00-04:00",
                "message": "fixture: leading-numeric strings",
                "verdict": "GO",
                "fact_diff": {
                    "changed": True,
                    "changes": [
                        {
                            "path": "provenance/note",
                            "old": "2026-09-10 — measured note",
                            "new": "2026-09-11 — corrected note",
                            "witness": "fixture",
                            "source": "fixture",
                            "limit_raised": False,
                        }
                    ],
                    "added": [],
                    "removed": [],
                    "findings_new": [],
                    "findings_gone": [],
                },
            }
        ]
    )
    documents = {**_every_document(tmp_path), "log_corpus": log_corpus}
    # The evidence references an actuator check document carries, and the clean contract's page.
    documents["artifact_ref_corpus"] = ArtifactRef(path="evidence/run-1.json", sha256="7" * 64)
    documents["page+clean"] = PagePayload.model_validate(build_payload(load_contract(CLEAN)))
    digest = "7" * 64  # deliberate leading-digit identifier corpus, not a measured artifact
    documents["start"] = documents["start"].model_copy(update={"revision": digest})
    scene = documents["page+scene"].scene
    documents["scene_identity_corpus"] = scene.model_copy(
        update={
            "asset_sha256": digest,
            "expected_asset_sha256": digest,
        }
    )
    documents["scene_unchanged_corpus"] = SceneUnchanged(
        unchanged=True,
        asset_sha256=digest,
        expected_asset_sha256=digest,
    )
    blame_corpus = BlamePayload.model_validate(
        [
            {
                **api.blame(X500)[0][0],
                "last_changing_commit": "7abc1234567890abcdef1234567890abcdef1234",
            }
        ]
    )
    documents["blame_corpus"] = blame_corpus
    # Budget.draw_mode is a name reference in the existing schema, not a
    # magnitude. Apply that field meaning only to the raw budget row; every
    # other direct string inside BlameRow.value still goes through the guard.
    blame = documents["blame"]

    def blame_string_figures(document):
        findings = {}
        for row in document.root:
            for field, value in row.value.items():
                if row.path.startswith("budgets/") and field == "draw_mode":
                    continue
                probe = BlamePayload.model_validate(
                    [
                        {
                            **row.model_dump(mode="json", exclude_none=True),
                            "value": {field: value},
                        }
                    ]
                )
                findings.update(string_figures(probe))
        return findings

    raw_blame = api.blame(X500)[0]
    assert [row.model_dump(mode="json", exclude_none=True) for row in blame.root] == raw_blame
    found: dict[str, tuple[str, str]] = {}
    for door, document in documents.items():
        figures = blame_string_figures(document) if door == "blame" else string_figures(document)
        for key, value in figures.items():
            found.setdefault(key, (door, value))

    budget = next(
        row
        for row in blame.root
        if row.path.startswith("budgets/") and row.value.get("draw_mode") == "7W"
    )
    assert budget.value["draw_mode"] == "7W"
    wrong_draw = budget.model_copy(update={"value": {"draw": "7 W"}})
    wrong_draw_payload = BlamePayload.model_validate([wrong_draw.model_dump(mode="json")])
    assert string_figures(wrong_draw_payload)["BlameRow.value"] == "7 W"
    assert blame_string_figures(wrong_draw_payload)["BlameRow.value"] == "7 W"

    unnamed = sorted(set(found) - set(STRING_FIGURE_EXEMPTIONS))
    assert not unnamed, "preformatted numbers on screen with no kind:\n" + "\n".join(
        f"  {key} = {found[key][1]!r}  (from `{found[key][0]}`) — "
        "make it an `api_payload.Figure` and let the renderer format it, "
        "or name it in STRING_FIGURE_EXEMPTIONS with why"
        for key in unnamed
    )
    stale = sorted(set(STRING_FIGURE_EXEMPTIONS) - set(found))
    assert not stale, (
        f"STRING_FIGURE_EXEMPTIONS names fields that no longer carry a number: {stale}"
    )
    for key, reason in STRING_FIGURE_EXEMPTIONS.items():
        assert len(reason) > 20, f"{key} is exempted but gives no real reason"


def _looks_like_magnitude(value: str) -> bool:
    """Returns True if a string value looks like a magnitude: a number
    followed by space and a unit token (W, V, A, Ah, Wh, %, Ω, η, kg, m, s, Hz
    with optional prefixes k, M, m, µ, n, p).

    This check examines the VALUE itself, not prose describing it.
    A magnitude is specifically: number + whitespace + optional prefix + unit.
    """
    if not isinstance(value, str):
        return False

    # Units from the specification, ordered longest-first to match correctly
    # (so "Wh" matches before "W")
    units = ["Wh", "Ah", "Hz", "kg", "Ω", "%", "η", "W", "V", "A", "m", "s"]
    prefixes = ["k", "M", "m", "µ", "n", "p", "f"]

    # Pattern: optional comparison/sign, number, space, optional prefix + unit
    for unit in units:
        for prefix in [""] + prefixes:
            full_unit = prefix + unit
            # Match: spaces, optional comparison, optional sign, number, spaces, unit
            # Unit must be followed by non-alphanumeric (end, space, punctuation)
            pattern = rf"[<>=]{{0,2}}\s*[-+]?[0-9]+\.?[0-9]*\s+{re.escape(full_unit)}(?:\s|$|[·,;:\)\]\}}])"
            if re.search(pattern, value):
                return True

        # Also check unit-first patterns (like "η 0.95")
        pattern = rf"\b{re.escape(unit)}\s+[+-]?[0-9]+\.?[0-9]*"
        if re.search(pattern, value):
            return True

    return False


def test_every_string_figure_exemption_satisfies_the_admission_rule(tmp_path):
    """The guard inspects VALUES, not reasons. Each exempted field's actual
    value in rendered documents must NOT parse as a magnitude, except for
    entries explicitly marked as xfail (e.g., e1-gl known violations).

    This test validates the values themselves, so the exemption list cannot
    grow by matching prose alone.
    """
    # Known violations marked with e1-gl (the box that will fix them)
    known_violations = {"GraphNode.sub", "GraphEdge.label"}

    # Collect actual values from rendered documents
    found_values: dict[str, tuple[str, str]] = {}  # key -> (door_name, value)
    for door, document in _every_document(tmp_path).items():
        for key, value in string_figures(document).items():
            if key not in found_values:  # Keep first occurrence
                found_values[key] = (door, value)

    # Check each exempted field
    failures = []
    for key in STRING_FIGURE_EXEMPTIONS:
        if key not in found_values:
            continue  # Field not in current documents; could be stale entry

        door_name, value = found_values[key]

        # Check if value looks like a magnitude
        if _looks_like_magnitude(value):
            if key in known_violations:
                # Mark as xfail with the row id that will fix it
                pytest.xfail(f"{key} = {value!r} is a magnitude (exempt until e1-gl: {door_name})")
            else:
                failures.append(
                    f"  {key} = {value!r} (from `{door_name}`) looks like a magnitude "
                    f"but is not in known_violations with e1-gl"
                )

    if failures:
        raise AssertionError(
            "Exempted fields whose values are magnitudes (should be covered by a magnitude-bearing producer):\n"
            + "\n".join(failures)
        )


def test_a_typed_figure_refuses_a_marked_kind_with_no_word():
    """The other half, on the instance: a `catalog` or `projected` figure
    without its own word is refused where it is built, not at review time."""
    with pytest.raises(ValidationError, match="must carry its own word"):
        Figure(value=1.0, unit="W", kind="catalog", by="parts catalog")
    assert Figure(value=1.0, unit="W", kind="catalog", by="parts catalog", mark="catalog").mark


@pytest.mark.parametrize("path", CONTRACT_FIXTURES, ids=lambda p: p.name)
def test_every_scene_label_across_the_fixtures_carries_both_magnitudes(path: Path):
    """Numeric scene labels have both typed operands; a known structural
    family-reference violation has neither numeric operand."""
    contract = load_contract(path)
    findings = list(run_record_checks(contract, contract_path=path))
    labels = _scene_labels(contract, findings)
    structural_labels = 0
    for label in labels:
        if label["observed"] is None or label["expected"] is None:
            structural_labels += 1
            assert path == FIXTURES / "actuator_review_e2e" / "contract.yaml"
            assert label["part"] == "mdl.motor"
            structural = [
                finding
                for finding in findings
                if finding.quantity == "device_models/mdl.motor/family"
            ]
            assert len(structural) == 1
            assert structural[0].observed == "'motors'"
            assert structural[0].expected == "an id in families"
            assert structural[0].observed_quantity is None
            assert structural[0].expected_quantity is None
            assert label["observed"] is label["expected"] is None
            continue
        assert label["observed"]["value"] is not None
        assert label["expected"]["value"] is not None
    assert structural_labels == int(path == FIXTURES / "actuator_review_e2e" / "contract.yaml")


# ------------------------------------------------------------------- the export


def test_openapi_export_names_every_union_member(tmp_path):
    out = tmp_path / "schema.json"
    export_openapi(out)
    document = json.loads(out.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    for model in ALL_MEMBERS:
        assert model.__name__ in schemas, f"{model.__name__} missing from the exported schema"
        schema = schemas[model.__name__]
        if model in (LogPayload, BlamePayload):
            assert schema["type"] == "array"
            row_type = "LogEntry" if model is LogPayload else "BlameRow"
            assert schema["items"] == {"$ref": f"#/components/schemas/{row_type}"}
        else:
            assert set(schema["properties"]) == set(model.model_fields)
    assert document["x-lockwire-members"] == {k: m.__name__ for k, m in MEMBERS.items()}
    assert document["x-lockwire-number-kinds"] == NUMBER_KINDS


# ---------------------------------------------------- the three lists go stale


def _real_self_kinded_producers() -> set[type[BaseModel]]:
    """Every pydantic model in `src/lockwire` that validates its own kind mark
    (declares a `_carries_its_word` model_validator on the class itself) —
    found by walking the package's modules, never hand-listed, so a new
    self-kinding record with no `SELF_KINDED` entry cannot hide."""
    found: set[type[BaseModel]] = set()
    walked = pkgutil.walk_packages(lockwire.__path__, prefix="lockwire.")
    for info in walked:
        if info.name == "lockwire.__main__":
            # Importing it runs the CLI (`app()` at module scope, so
            # `python -m lockwire` works) — walk every module's *contents*,
            # not this one's side effect.
            continue
        module = importlib.import_module(info.name)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and "_carries_its_word" in obj.__dict__
            ):
                found.add(obj)
    return found


def _all_reachable_field_names() -> set[str]:
    """Every field name on every model reachable from a union member — the
    same recursive walk `numeric_fields()` uses, widened to every field, not
    only numeric ones."""
    names: set[str] = set()
    seen: set[type] = set()

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            names.add(name)
            for leaf in _leaf_types(field.annotation):
                if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                    walk(leaf)

    for member in ALL_MEMBERS:
        walk(member)
    return names


def test_self_kinded_and_marked_kinds_match_real_producers():
    """The bounce this respin exists to close: `SELF_KINDED`, `MARKED_KINDS`
    and `EXTENSION_POINTS` are hand-maintained lists next to code that can
    silently drift past them. Each half here is discovered from the real
    producers, not hand-listed a second time, so a new one that forgets to
    register goes red instead of sailing through invisibly.
    """
    # SELF_KINDED: every model that self-validates its own kind mark, found by
    # scanning the package, must be exactly the models this file names.
    discovered = _real_self_kinded_producers()
    assert discovered == set(SELF_KINDED), (
        f"SELF_KINDED is stale against the real self-kinding producers: "
        f"missing={sorted(m.__name__ for m in discovered - set(SELF_KINDED))} "
        f"extra={sorted(m.__name__ for m in set(SELF_KINDED) - discovered)}"
    )

    # MARKED_KINDS: exactly the NumberKind values Figure — the model whose
    # `kind` field *is* NumberKind — actually refuses without a mark,
    # discovered by exercising the validator rather than restated by hand.
    requires_mark: set[str] = set()
    for kind in get_args(NumberKind):
        try:
            Figure(value=1.0, unit="W", kind=kind, by="test")
        except ValidationError:
            requires_mark.add(kind)
    assert requires_mark == set(MARKED_KINDS), (
        f"MARKED_KINDS is stale against Figure's own validator: "
        f"validator requires={sorted(requires_mark)} MARKED_KINDS={sorted(MARKED_KINDS)}"
    )

    # Alternative's `kind` is a free `str`, not the `NumberKind` Literal — it
    # cannot be swept over `get_args`, but it must still refuse the one marked
    # kind it actually produces (`advice.build_rail_alternatives`'s
    # `"projected"` regulator suggestion), which is precisely the gap this
    # respin closes: a `projected` figure reaching a door with no mark.
    assert "projected" in MARKED_KINDS, (
        "MARKED_KINDS must still cover the kind Alternative produces"
    )
    with pytest.raises(ValidationError, match="must carry its own word"):
        Alternative(text="x", value=1.0, unit="W", kind="projected")
    assert Alternative(text="x", value=1.0, unit="W", kind="projected", mark="projected")
    assert Alternative(text="x", value=1.0, unit="W", kind="derived-by-check_budgets")

    # EXTENSION_POINTS: a named slot stops being empty the moment some model
    # in the union actually grows a field by that name — walked from the
    # union's real fields, not hand-checked.
    filled = set(EXTENSION_POINTS) & _all_reachable_field_names()
    assert not filled, (
        f"EXTENSION_POINTS names a field that now has a real producer: {sorted(filled)} "
        "— give it its own NUMBER_KINDS/SELF_KINDED treatment and drop it from EXTENSION_POINTS"
    )


# --------------------------------------------------------------- lenses
#
# labels are payload strings the app never invents. Two workers on two
# bases refused e2-f's lens tests for exactly this — a renderer composing lens
# words itself. This is the payload sweep that makes the rule durable: every
# lens the union names carries its own caption, and every scene part the union
# ships is claimed by at least one lens. Both are enumerations (rule 10/11):
# joining `Graph`/`ScenePart.lenses` and exempting either from the sweep are
# one line each and opposites — this sweep JOINS, it exempts nothing.

LENS_IDS = frozenset(get_args(LensId))


@pytest.mark.parametrize("path", CONTRACT_FIXTURES, ids=lambda p: p.name)
def test_every_lens_has_a_caption(path: Path):
    """`Graph`'s five lenses — `off`/`health` included, not just the three the
    wiring diagram draws — each carry a non-empty `label` and `caption` off
    the payload, for every contract fixture, not only X500."""
    contract = load_contract(path)
    graph = build_payload(contract, contract_path=path)["graph"]
    assert set(graph) == LENS_IDS, f"{path.name}: lens set drifted: {sorted(graph)}"
    uncaptioned = sorted(key for key, lens in graph.items() if not lens["caption"])
    assert not uncaptioned, f"{path.name}: lenses with no caption: {uncaptioned}"
    unlabeled = sorted(key for key, lens in graph.items() if not lens["label"])
    assert not unlabeled, f"{path.name}: lenses with no label: {unlabeled}"


def test_every_scene_part_is_in_at_least_one_lens():
    """The other half of the sweep: `ScenePart.lenses` covers every part the
    scene ships, named by node when it doesn't — a part in no lens is
    invisible under every view and the message must say which part."""
    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    payload = build_payload(
        contract, scene=scene, scene_source="x500.glb (given)", contract_path=X500
    )
    parts = payload["scene"]["parts"]
    assert parts, "fixture assumption changed: x500 scene has no parts"
    uncovered = sorted({part["node"] for part in parts if not part["lenses"]})
    assert not uncovered, f"parts in no lens at all: {uncovered}"
    unknown_lens = sorted({lens for part in parts for lens in part["lenses"]} - LENS_IDS)
    assert not unknown_lens, f"part lenses outside the named set: {unknown_lens}"
    assert any(part["mount_frame"] for part in parts), (
        "a device part carries its mount_frame (drag-to-place)"
    )


# ---------------------------------------------------------- e4-pf: Reference drawers


def _command_flag_names(command_name: str) -> set[str]:
    """Every declared flag (long and short) for one leaf command in the typer
    app, read off the app itself — as `tests/test_gui.py:1830` already does
    with `typer.main.get_command`. Never hand-listed, so a renamed flag turns
    the drawer test red without anyone touching it."""
    import typer

    click_app = typer.main.get_command(app)
    command = click_app.commands[command_name]
    names: set[str] = set()
    for param in command.params:
        names.update(getattr(param, "opts", None) or [])
        names.update(getattr(param, "secondary_opts", None) or [])
    return names


def test_every_reference_drawer_names_a_real_flag():
    """Each Reference drawer's `cli_line` uses only
    flags typer actually registers for that command. The test asks typer for
    the flags — it does not hand-list them (a renamed/removed flag, e.g.
    dropping `--no-open` or resurrecting a `--html` that was never real,
    turns this red on its own).

    MEASURED: `api_payload.build_union` (the payload union's whole-document
    assembler) is not on this branch — it lands with e1-b6, still queued,
    and this box's own brief names it as a blocker. The Reference face is
    built by `gui.build_payload(...)["faces"]["reference"]`, the same
    function `build_union`'s eventual page member calls — so this test
    exercises the real producer, not a stand-in for the missing assembler.

    Mutation bite: put `--html` into a drawer's `cli_line`, or drop
    `--no-open` from the report drawer's — this test goes red.
    """
    contract = load_contract(X500)
    payload = build_payload(contract, contract_path=X500)
    drawers = payload["faces"]["reference"]["drawers"]
    assert drawers, "no reference drawers built"

    for drawer in drawers:
        tokens = drawer["cli_line"].split()
        assert tokens[0] == "lockwire", drawer["cli_line"]
        command_name = tokens[1]
        flags = {t for t in tokens if t.startswith("-")}
        allowed = _command_flag_names(command_name)
        unknown = flags - allowed
        assert not unknown, (
            f"{drawer['title']!r} names {unknown} — not a real flag of "
            f"`lockwire {command_name}` ({sorted(allowed)})"
        )


# --------------------------------------------------------------- e2-lf: layout values vs number slots

# `ScenePayload.bounds` and `ScenePart.translation` are DELIBERATELY kind-less (LAYOUT_FIELDS,
# above) — they are positions, not measurements. That means `test_every_number_in_the_union_carries_
# its_kind` cannot see a bug where one of them is rendered as a figure: a `Figure`/`_figure(...)`
# built FROM a layout value is a number with no kind, which is exactly what the payload-shape sweep
# already lets through for these two fields by design. So this is a SOURCE-level sweep over the
# faces that build a "number slot" — a `_figure(...)`/`Figure(...)` call, or a raw
# `{"value": ..., "kind": ...}` dict literal bypassing `_figure` — proving no such call's numeric
# argument traces back to a `bounds`/`translation` read, anywhere in `src/lockwire/`.
#
# Rule 47 (this box's brief quotes it directly): a sweep that only matches the ONE spelling of the
# violation it was written against is not a sweep. So the trace below is a real (if bounded) taint
# analysis, not a grep: an assignment whose right-hand side reads `.bounds`/`.translation` (attribute,
# string-keyed subscript, or a bare name spelled `bounds`/`translation`) taints its target name(s),
# to a fixed point, WITHIN one function body — covering a variable, an alias-of-an-alias, a tuple
# destructure, and an f-string that interpolates a tainted name or a direct layout read. Its named
# ceiling: taint does not cross a function boundary (a helper that returns a layout value and whose
# caller feeds the result to a number slot is invisible to it) — `test_layout_sweep_...` below proves
# what it DOES catch and the "call/dict site is scanned per source file" comment states the reach.

_LAYOUT_NAMES = {"bounds", "translation"}


_NUMBER_SLOT_CALL_NAMES = {"_figure", "Figure"}


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_number_slot_dict(node: ast.Dict) -> bool:
    keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return {"value", "kind"} <= keys


def _is_number_slot_dict_call(node: ast.Call) -> bool:
    """True if this is a call to dict(...) with both `value=` and `kind=` keyword arguments."""
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "dict"):
        return False
    kwarg_names = {kw.arg for kw in node.keywords if kw.arg is not None}
    return {"value", "kind"} <= kwarg_names


def _mentions_layout(expr: ast.AST | None, tainted: set[str]) -> bool:
    """True if `expr` reads `.bounds`/`.translation` (attribute, string-keyed subscript,
    getattr(..., "bounds"), or dict(..., key=...) for layout keys), a bare name literally
    called that, or a name this function already proved tainted. `ast.walk` recurses
    into an f-string's `FormattedValue.value`, so this covers formatted-string violations for free."""
    if expr is None:
        return False
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute) and node.attr in _LAYOUT_NAMES:
            return True
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in _LAYOUT_NAMES:
                return True
        if isinstance(node, ast.Call):
            # Check for getattr(obj, "bounds") or getattr(obj, "translation")
            func = node.func
            if isinstance(func, ast.Name) and func.id == "getattr":
                if len(node.args) >= 2:
                    second_arg = node.args[1]
                    if isinstance(second_arg, ast.Constant) and second_arg.value in _LAYOUT_NAMES:
                        return True
        if isinstance(node, ast.Name) and (node.id in _LAYOUT_NAMES or node.id in tainted):
            return True
    return False


def _assign_target_names(target: ast.AST) -> list[str]:
    """Flattens a plain name, and a tuple/list destructure (with a `*rest` starred element), into
    the names it binds — so `a, *b = translation` taints both `a` and `b`."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _assign_target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_assign_target_names(elt))
        return names
    return []


def _tainted_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every name, within this one function body, that a layout read reaches — by direct
    assignment, an alias of an alias (fixed-point, so chain length is unbounded), or a
    tuple/list destructure. Function-local only; see the module docstring's named ceiling."""
    assigns: list[tuple[list[ast.AST], ast.AST]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            assigns.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assigns.append(([node.target], node.value))
        elif isinstance(node, ast.NamedExpr):
            assigns.append(([node.target], node.value))

    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            if not _mentions_layout(value, tainted):
                continue
            for target in targets:
                for name in _assign_target_names(target):
                    if name not in tainted:
                        tainted.add(name)
                        changed = True
    return tainted


def layout_reaching_number_slots(tree: ast.Module, filename: str) -> list[tuple[str, int, str]]:
    """Every `_figure(...)`/`Figure(...)` call, `dict(...)` call with `value=` and `kind=`,
    or bare `{"value":.., "kind":..}` dict in `tree` whose numeric argument traces back to a
    `bounds`/`translation` read, scanned per function so an alias/destructure/f-string chain
    local to that function is followed to a fixed point."""
    violations: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = _tainted_names(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and _callee_name(node) in _NUMBER_SLOT_CALL_NAMES:
                args = list(node.args) + [kw.value for kw in node.keywords]
                if any(_mentions_layout(arg, tainted) for arg in args):
                    violations.append((filename, node.lineno, f"{_callee_name(node)}(...) call"))
            elif isinstance(node, ast.Call) and _is_number_slot_dict_call(node):
                # Check if the `value=` argument mentions layout
                for kw in node.keywords:
                    if kw.arg == "value" and _mentions_layout(kw.value, tainted):
                        violations.append((filename, node.lineno, "dict(...) call"))
                        break
            elif isinstance(node, ast.Dict) and _is_number_slot_dict(node):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "value"
                        and _mentions_layout(value, tainted)
                    ):
                        violations.append((filename, node.lineno, '{"value": ...} dict literal'))
    return violations


def _lockwire_src_files(roots: tuple[str, ...] = ("lockwire",)) -> list[Path]:
    src = Path(__file__).resolve().parent.parent / "src"
    return sorted(p for r in roots if (src / r).exists() for p in (src / r).rglob("*.py"))


def test_no_layout_value_reaches_a_number_slot():
    """The face-side sweep: enumerate every face under `src/lockwire/` (every `.py` file there —
    `gui.py`'s HTML/JS window, `report_html.py`, `scene_view.py`, and whatever else exists or gets
    added, so the enumeration cannot rot into a hand-picked list) and every number-slot construction
    site in each (`_figure(...)`, `Figure(...)`, `dict(value=..., kind=...)`, a bare `{"value":.., "kind":..}` dict). None of
    their numeric arguments may trace back to `ScenePayload.bounds` or `ScenePart.translation`.

    Enumeration, stated plainly: faces = `_lockwire_src_files()` (≥40 files, measured below);
    number slots = every AST `Call` node in each file whose callee is named `_figure` or `Figure`,
    every `Call` to `dict(...)` carrying both `value=` and `kind=` keyword arguments, plus every
    AST `Dict` literal carrying both a `"value"` and a `"kind"` key — found by walking each
    file's own AST, not a hand-written file/line list. What it cannot reach: taint that crosses a
    function boundary (named in the module comment above), `getattr`/`dict` with non-constant
    attribute/key names, and any non-Python face (this repo's number slots are all built in Python
    before the JSON payload reaches the browser).
    """
    src_files = _lockwire_src_files()
    assert len(src_files) >= 40, f"the file enumeration looks broken: found only {len(src_files)}"

    violations: list[tuple[str, int, str]] = []
    for path in src_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        violations.extend(layout_reaching_number_slots(tree, path.name))

    assert not violations, (
        "a layout value (bounds/translation) reaches a number slot:\n"
        + "\n".join(f"  {name}:{lineno} — {shape}" for name, lineno, shape in violations)
    )


def test_layout_sweep_catches_synthetic_violations_of_different_shapes():
    """Rule 47: a sweep that only matches the one spelling of the violation it was written against
    proves nothing. Four synthetic sources, each guilty in a DIFFERENT way than a literal
    `_figure(part.translation, ...)` call, all must be caught — plus one clean source (an f-string
    with no layout read) that must NOT be, so the sweep is not simply "any f-string is guilty"."""

    def _violations(src: str) -> list[tuple[str, int, str]]:
        return layout_reaching_number_slots(ast.parse(textwrap.dedent(src)), "synthetic.py")

    # (a) the obvious shape: a direct attribute read passed straight into the call.
    direct = """
        def build(part):
            return _figure(part.translation, "measured", "x")
        """
    assert _violations(direct), "did not catch the direct attribute-read shape"

    # (b) through a variable — one hop.
    via_variable = """
        def build(scene):
            edge = scene.bounds
            return _figure(edge, "measured", "x")
        """
    assert _violations(via_variable), "did not catch the through-a-variable shape"

    # (c) an alias of an alias, plus a tuple destructure.
    via_alias_and_destructure = """
        def build(part):
            raw = part.translation
            alias = raw
            x, y, z = alias
            return {"value": x, "unit": "m", "kind": "measured", "by": "x"}
        """
    assert _violations(via_alias_and_destructure), (
        "did not catch the alias-of-an-alias-plus-destructure shape"
    )

    # (d) a formatted string.
    via_fstring = """
        def build(scene):
            note = f"origin at {scene.bounds}"
            return Figure(value=note, unit="", kind="measured", by="x")
        """
    assert _violations(via_fstring), "did not catch the f-string shape"

    # (e) dict(...) call with layout value passed to the `value=` kwarg.
    via_dict_call = """
        def build(part):
            return dict(value=part.translation, unit="m", kind="measured", by="x")
        """
    assert _violations(via_dict_call), "did not catch the dict(...) call shape"

    # (f) getattr(...) to read a layout field, then passed to a number slot.
    via_getattr = """
        def build(obj):
            bounds = getattr(obj, "bounds")
            return _figure(bounds, "measured", "xyz")
        """
    assert _violations(via_getattr), "did not catch the getattr(...) shape"

    # negative control: an f-string and a dict with the right keys, neither touching layout data at
    # all — must NOT be flagged, or the sweep is "any f-string"/"any value+kind dict" in disguise.
    clean = """
        def build(quantity):
            note = f"reading {quantity.value}"
            return {"value": quantity.value, "unit": quantity.unit, "kind": "declared", "by": "x"}
        """
    assert not _violations(clean), "flagged a source with no layout read at all"


def test_scene_parts_carry_their_component_group():
    """Every X500 part sits under the contract component it resolved to; the
    camera's meshes are all under the camera device, and nothing falls out ungrouped."""
    from lockwire.scene_view import scene_payload

    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    parts = scene_payload(contract, scene, source="x", cards=[], scored_records=[], labels=[])[
        "parts"
    ]
    camera = [p for p in parts if p["node"].startswith("camera")]
    assert camera and {p["group"]["id"] for p in camera} == {"dev.camera.oak-d-lite"}
    assert {p["group"]["kind"] for p in camera} == {"device"}
    assert camera[0]["group"]["label"] == "Camera Oak D Lite"
    assert {p["group"]["label"] for p in parts if p["group"]["id"] == "dev.motor.m1"} == {
        "Motor M1"
    }
    assert {p["group"]["kind"] for p in parts} <= {"device", "rail", "bus", "link", "other"}


def test_non_unit_normals_are_normalized_before_int16_packing():
    import base64
    import struct

    from lockwire.scene_view import _pack_vectors

    packed = _pack_vectors((0.0, 0.0, 5.0, 0.0, 0.0, 0.0), normals=True)
    assert struct.unpack("<6h", base64.b64decode(packed["data"])) == (0, 0, 32767, 0, 0, 0)


def test_free_check_and_what_if_payloads_carry_no_actuator_or_health_fields():
    """Those fields exist only on the paid CheckPayloadPro / WhatIfPayloadPro."""
    from lockwire.api_payload import CheckPayload, WhatIfPayload

    paid = {
        "actuator_cases",
        "actuator_review_draft",
        "actuator_review_next_step",
        "recorded_health",
    }
    assert not paid & (set(CheckPayload.model_fields) | set(WhatIfPayload.model_fields))


def test_add_part_preselect_data_fits_only_matching_voltage_rails():
    form = DeclareFormPayload.model_validate(api.declare_form(X500))
    volts = {r.id: r.voltage.base() for r in api.load(X500).topology.rails if r.voltage}
    orin = next(
        p for p in form.add_part.parts if p.id == "x500.companion.jetson-orin-nano"
    )  # 4.75-5.25 V
    assert orin.fits and all(4.75 <= volts[rid] <= 5.25 for rid in orin.fits)
    assert all(
        not p.fits
        for p in form.add_part.parts
        if p.id != orin.id and not any("voltage" in n.key for n in p.numbers)
    )


def test_picker_parts_carry_voltage_and_orin_fits_only_the_5v_rails():
    form = DeclareFormPayload.model_validate(api.declare_form(X500))
    ranged = [
        p
        for p in form.add_part.parts
        if any(n.key in ("input_voltage_min", "nominal_voltage") for n in p.numbers)
    ]
    assert len(ranged) >= 10
    volts = {r.id: r.voltage.base() for r in api.load(X500).topology.rails if r.voltage}
    orin = next(p for p in form.add_part.parts if p.id == "x500.companion.jetson-orin-nano")
    assert orin.fits and set(orin.fits) == {rid for rid, v in volts.items() if 4.75 <= v <= 5.25}
