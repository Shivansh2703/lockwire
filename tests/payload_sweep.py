"""Test-support for the payload union: the number-kind sweep and the string-figure guard.

Moved out of ``lockwire.api_payload``: only tests walk the union this way, so
shipped code does not carry it.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Union, get_args

from pydantic import BaseModel, Field, TypeAdapter

from lockwire.advice import Alternative
from lockwire.api_payload import (
    ALL_MEMBERS,
    CheckPayload,
    DescribePayload,
    EditPayload,
    PlanPayload,
    WhatIfPayload,
    BlamePayload,
    DeclareFormPayload,
    DiffPayload,
    Figure,
    LogPayload,
    PagePayload,
    StartPayload,
    StartersPayload,
)

#: The discriminated union of everything ``report_json.envelope`` leads (free members).
MachineDocument = Annotated[
    Union[
        WhatIfPayload,
        CheckPayload,
        PlanPayload,
        DescribePayload,
        EditPayload,
    ],
    Field(discriminator="artifact_type"),
]

MACHINE_DOCUMENT = TypeAdapter(MachineDocument)

#: The members with no ``artifact_type`` to discriminate on, keyed by the door
#: that emits them (seams 1 and 8). ``validate_document`` cannot route these;
#: the caller names them, which is exactly what the seams say is missing.
UNENVELOPED: dict[str, type[BaseModel]] = {
    "start": StartPayload,
    "starters": StartersPayload,
    "page": PagePayload,
    "diff": DiffPayload,
    "log": LogPayload,
    "blame": BlamePayload,
    "declare_form": DeclareFormPayload,
}


def validate_document(document: dict[str, Any]) -> BaseModel:
    """Validate one machine document against whichever member its own
    ``artifact_type`` names. Nothing is guessed: the discriminator picks."""
    return MACHINE_DOCUMENT.validate_python(document)


# ---------------------------------------------------- the number-kind sweep


def _leaf_types(annotation: Any) -> set[type]:
    """Every concrete type inside an annotation — through ``| None``, ``list``,
    ``dict`` and nesting. ``Literal["a"]``'s args are values, not types, so
    they drop out; that is deliberate, a Literal of strings is not a number."""
    args = get_args(annotation)
    if not args:
        return {annotation} if isinstance(annotation, type) else set()
    found: set[type] = set()
    for arg in args:
        found |= _leaf_types(arg)
    return found


def _declaring_class(model: type[BaseModel], field: str) -> type:
    """The class in ``model``'s MRO that actually declares ``field``.

    ``check`` and ``plan`` share ``_VerdictDocument``; keying an inherited
    field by the subclass would put the same field in the table three times
    and let one copy rot. One declaration, one row.
    """
    for base in model.__mro__:
        if field in getattr(base, "__annotations__", {}):
            return base
    return model


#: **Self-kinding records** — models whose every instance states its own number
#: kind, so a ``NUMBER_KINDS`` row would be worse than none: a static table can
#: say only one kind per field, and these fields' kinds vary per value
#: (``advice.Alternative.kind`` is ``derived-by-<check>`` on one remedy and
#: ``projected`` on the next). A row would *misdescribe* them.
#:
#: Listed, not duck-typed. Measured (``grep -n "class BudgetRow" -A 12
#: src/lockwire/api_payload.py`` shows ``value``/``unit``/``kind`` fields;
#: ``grep -n 'budget.kind ==' src/lockwire/report_html.py`` shows those
#: ``kind`` values are ``"rail_power"``/``"mass_total"``, 2026-09-08):
#: ``BudgetRow`` also has ``value``/``unit``/``kind``, and its ``kind`` is a
#: rail category, not a number-kind — so "has the three fields" would have
#: silently excused two real rows.
#: A box adding a new self-kinding record and forgetting this tuple gets a red
#: from ``test_every_number_in_the_union_carries_its_kind`` naming both
#: remedies, which is the failure we want.
SELF_KINDED: tuple[type[BaseModel], ...] = (Figure, Alternative)


def carries_its_own_kind(model: type[BaseModel]) -> bool:
    """Does this model state its own number-kind on every instance?"""
    return model in SELF_KINDED


def numeric_fields(members: tuple[type[BaseModel], ...] = ALL_MEMBERS) -> dict[str, str]:
    """``Model.field`` -> the model it is *declared* on, for every field in the
    union whose value carries a number — walked from the models, never listed
    by hand, so ``NUMBER_KINDS`` cannot quietly fall behind a new field.

    ``bool`` is a subclass of ``int`` in Python but is not a number on screen,
    and the exact-type membership below keeps it out.
    """
    found: dict[str, str] = {}
    seen: set[type] = set()

    def walk(model: type[BaseModel]) -> None:
        if model in seen or carries_its_own_kind(model):
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            leaves = _leaf_types(field.annotation)
            self_kinded = any(
                isinstance(leaf, type)
                and issubclass(leaf, BaseModel)
                and carries_its_own_kind(leaf)
                for leaf in leaves
            )
            if (int in leaves or float in leaves) and not self_kinded:
                owner = _declaring_class(model, name).__name__
                found[f"{owner}.{name}"] = owner
            for leaf in leaves:
                if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                    walk(leaf)

    for member in members:
        walk(member)
    return found


# ------------------------------------------- the escape hatch, closed by name

#: A ``str`` field whose *value* opens with a number: ``"35.20 W vs ..."``,
#: ``"0.00 V below rail.pm02-5v"``. That is a
#: finding by default — a number on screen with no kind — so the ones that
#: legitimately stay strings are named here **with the reason**, and
#: ``test_no_door_emits_a_preformatted_number_outside_the_named_exemptions``
#: asserts set equality **both ways**: an unnamed one is a failure, and a name
#: whose field stops carrying numbers is a failure too, so this list cannot rot.
#:
#: Measured: ``pytest -q tests/test_api_payload.py
#: -k test_no_door_emits_a_preformatted_number_outside_the_named_exemptions``
#: (2026-09-08, ``1 passed``) — its ``_every_document`` fixture builds the x500
#: and clean contracts' page payloads and the check/preflight documents this
#: names. Every entry falls into one of four reasons, and only the first is a
#: real number-kind gap:
#:  * **producer-owned prose** — the number is assembled with words inside
#:    ``checks.py`` / ``report.py`` / ``report_html.py``. Typing it means
#:    rewriting a producer, which is the re-implementation this module forbids this
#:    file. Named, so the box that owns that producer can close it.
#:  * **a sentence that opens with a count** — "1 cable has no speed written
#:    down" is prose, not a figure; there is no value/unit to type.
#:  * **a date** — not a magnitude.
#:  * **a version** — not a magnitude, and it is the one case that would slip
#:    past a naive "does ``float(s)`` parse" rule, which is why the guard reads
#:    the *leading* token instead.
#:
#: **Admission rule**: a field is exempt from the string-figure guard if its
#: value is one of the following **classes of data**, never if it is merely a
#: magnitude that happens to be formatted as a string:
#:
#: 1. **An identifier** (hash, sha, object id, rail name) — a value whose
#:    digits carry no magnitude; the number is a name, not a quantity.
#: 2. **Prose** (sentence, words, text) — human-readable text that happens to
#:    open with a digit, not a number to compare, add or plot.
#: 3. **A date or version** — a semantic/temporal value, not quantitative.
#: 4. **Binary data** (base64, blob) — encoded or binary content, not a number
#:    on screen.
#:
#: What does NOT qualify: any field whose value a reader could compare ("which
#: is bigger?"), add ("sum them up"), or plot ("trending up?") — even if it
#: has already been formatted into a string. That is the whole reason this
#: guard and this list exist.
STRING_FIGURE_EXEMPTIONS: dict[str, str] = {
    "ScoredRecord.observed": "existing checker prose; observed_quantity is its typed twin",
    "ScoredRecord.expected": "existing checker prose; expected_quantity is its typed twin",
    "ScoredRecord.margin": "existing checker comparison margin prose",
    # producer-owned prose: checks.py assembles number + words at ~20 sites
    "AddsUpRow.margin": "checks.py builds `<magnitude> <words>` at ~20 sites (its box, not this one)",
    "AddsUpRow.name": "gui._scored_title's sentence, built from the same check-record prose",
    "AddsUpRow.text": "`<observed> vs <expected>`, both already strings on the check record",
    # producer-owned prose: report.py's human rendering, whose typed twins
    # (`Finding.observed_quantity`/`expected_quantity`) already exist beside it
    "Finding.observed": "report.py's human magnitude; `Finding.observed_quantity` is the typed twin",
    "Finding.expected": "report.py's human magnitude; `Finding.expected_quantity` is the typed twin",
    "Finding.delta": "report.py's rendered difference, e.g. '+1.60 W (+10%)'",
    # producer-owned prose: report_html.py's graph, number-or-words per node
    "GraphNode.sub": "report_html.py: a magnitude on some nodes, 'mass unknown' on others",
    "GraphEdge.label": "report_html.py: the rail draw it labels an edge with",
    # sentences that open with a count
    "Card.title": "a sentence — '1 cable has no speed written down'",
    "Card.body": "a sentence — '2 checks here could not run, on ...'",
    "PagePayload.qualifier": "a sentence — '8 things add up · 9 things I couldn't check'",
    # opaque identifiers and blobs that happen to open with a digit
    "EditPayload.contract_sha": "a git object id — an identifier, not a magnitude",
    "EditPayload.revision": "raw contract-byte revision token — an identifier, not a magnitude",
    "CheckPayload.revision": "raw contract-byte revision token — an identifier, not a magnitude",
    "LogEntry.sha": "a git object id — an identifier, not a magnitude",
    "BlameRow.last_changing_commit": "a git object id — an identifier, not a magnitude",
    "BlameRow.locked_at": "a recorded ISO date, not a magnitude",
    "PackedVectors.data": "base64 vertex data — a blob, not a magnitude on screen",
    "ScenePayload.asset_sha256": "sha256 of selected root-file bytes, not a magnitude or bundle identity",
    "ScenePayload.expected_asset_sha256": "a declared root-file sha256, not a magnitude",
    "SceneUnchanged.asset_sha256": "sha256 of selected root-file bytes, not a magnitude or bundle identity",
    "SceneUnchanged.expected_asset_sha256": "a declared root-file sha256, not a magnitude",
    "ScenePayload.hash": "sha256 of the scene's geometry (e1-b2) — an identifier, not a magnitude",
    "StartPayload.revision": "sha256 of the exact lockfile bytes — an identifier, not a magnitude",
    "ArtifactRef.sha256": "sha256 pin for admitted artifact bytes — an identifier, not a magnitude",
    # an identifier that happens to open with a digit — the guard's one class of
    # false positive, named rather than quietly special-cased in the regex
    "DesignFacePart.rail": "a rail's short name ('5v') — an identifier, not a magnitude",
    # dates and versions, which are not magnitudes
    "BudgetRow.locked_at": "an ISO date, not a magnitude",
    "LogEntry.date": "an ISO date, not a magnitude",
    "_Document.format_version": "a version, not a magnitude",
    # factdiff preserves each declared value's JSON type; numeric magnitudes
    # remain numeric/structured, while strings here are prose, dates or identifiers
    "FieldChangeRow.old": "a preserved string value — prose/date/identifier, not a magnitude",
    "FieldChangeRow.new": "a preserved string value — prose/date/identifier, not a magnitude",
    "_Document.schema_version": "a version, not a magnitude",
    # prose: schema.Provenance.note is a witness's free-text note (schema.py's
    # own docstring: "no checker reads it") that reaches the union for the
    # first time through DeclareRow.provenance (E4-df) — a sentence that can
    # happen to open with a digit ("610 g is the published dry frame-kit
    # weight..."), not a magnitude to compare, add or plot.
    "Provenance.note": "schema.Provenance's free-text note — prose, not a magnitude (schema.py: 'no checker reads it')",
}

#: A value that *opens with* a number, comparator and sign allowed:
#: ``"35.20 W vs ..."``, ``"<= 15.60 W"``, ``"-3 dB"``. Deliberately not
#: ``float(value)`` — that would miss the case this whole change exists for
#: (``"35.20 W vs <= 15.60 W"`` does not parse as a float) and would flag
#: ``format_version="2"``, which is not a number on screen.
_LEADS_WITH_A_NUMBER = re.compile(r"^\s*[<>=]{0,2}\s*[-+]?[0-9]")


def string_figures(document: BaseModel) -> dict[str, str]:
    """``Model.field`` -> the offending value, for every ``str`` field in a
    *validated* document whose value opens with a number.

    Runs on instances, not annotations, because that is where a preformatted
    figure hides: the annotation says ``str`` and tells you nothing.
    """
    found: dict[str, str] = {}

    def walk(model: BaseModel) -> None:
        for name in type(model).model_fields:
            owner = _declaring_class(type(model), name).__name__
            value = getattr(model, name)
            items: Any = value
            if isinstance(value, dict):
                items = value.values()
            elif not isinstance(value, (list, tuple)):
                items = (value,)
            for item in items:
                if isinstance(item, str):
                    if _LEADS_WITH_A_NUMBER.match(item):
                        found.setdefault(f"{owner}.{name}", item)
                elif isinstance(item, BaseModel):
                    walk(item)

    walk(document)
    return found
