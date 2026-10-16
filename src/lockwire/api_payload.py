"""The payload **union** — one typed member per document lockwire's doors emit.

A union is the easiest place in a codebase to grow a second implementation, so
the rule here is absolute: **every member describes a dict an
existing producer already builds.** Nothing in this file computes a document,
re-walks a contract, or re-derives a verdict. The producers, measured
2026-09-08 on `wt-lockwire-review`:

| member | producer | discriminator |
|---|---|---|
| `CheckPayload` | `api.check` -> `api.check_document` -> `api.verdict_document(..., CHECK_ARTIFACT_TYPE)` plus `headline` (E2-hl) | `lockwire-check-report` |
| `PlanPayload` | `api.plan` -> the same `api.verdict_document`, `PLAN_ARTIFACT_TYPE` | `lockwire-plan-report` |
| `DescribePayload` | `api.describe` -> `describe.describe_all` behind `report_json.envelope` | `lockwire-describe` |
| `EditPayload` | `api.declare` -> `edit.apply_changes` behind `report_json.envelope` | `lockwire-edit-result` |
| `PagePayload` | `gui.build_payload` | *(none — see the seams)* |
| `DiffPayload` | `api.diff` -> `factdiff.FactDiff.as_json` | *(none — see the seams)* |
| `LogPayload` | `api.log` -> git-backed history rows | *(none — see the seams)* |

`CheckPayload` and `PlanPayload` are one shape, because they are one function:
they share `_VerdictDocument` and differ only in the `artifact_type` literal
that discriminates them. Two independent copies of those fields would be the
second implementation this file exists to prevent.

The findings and outcomes inside a member are **not re-modelled**: they are
`report.Finding` and `plan.AssertionOutcome`, imported. Both already carry
`extra="forbid"`.

`extra="forbid"` is on every model here. That is the drift test's teeth: the
day a producer grows a key this file does not know about,
`model_validate(...)` fails here rather than the app, the sidecar or the
generated TypeScript silently reading whatever happens to be there. It has
already earned its keep once — `gui.build_payload` grew `health_words` after
the first version of this file was written, and the test went red.

Number kinds — every number carries its kind
--------------------------------------------
The rule, verbatim: *"Every number on screen is declared, measured, or derived by
a named check; a catalog or projected figure is shown only with its own visual
mark and word (\"catalog\", \"projected\"), never in the same style as a
measured one."*

`NUMBER_KINDS` below is that table: one row per numeric field reachable from
any union member, keyed `Model.field`. It is a **declaration**, not a second
computation — a numeric field's kind is a property of which producer filled it
in, and this file is where the union says so.
`test_every_number_in_the_union_carries_its_kind` walks every member, finds
every numeric field, and fails on any the table does not cover; `export_openapi`
ships the table inside the schema document so the app and the TypeScript
emitter render the mark rather than re-deriving it.

Open seams — named, not silently picked (this file is a freeze point)
---------------------------------------------------------------------
1. **The page member has no discriminator.** `gui.build_payload` predates
   `report_json.envelope` (E0-25) and emits no `artifact_type`, so
   `MEMBERS` (keyed by `artifact_type`) discriminates the enveloped members and `PagePayload`
   is validated by name beside them. Giving the page dict an envelope is a
   `gui.py` change and `gui.py` is the GUI lane's (e1-b3/b4/b5), not this box's.
2. **`scan` has no member and cannot have one yet.** The brief asks for one;
   measured, `cli.scan` dumps YAML straight to stdout (`cli.py:1167`) and has
   no `api` twin and no document producer. A member here would mean *writing*
   that producer, which is the re-implementation this module forbids. It needs an
   `api.scan` first.
3. **MCP's three tool-only shapes are not members.** `describe_device`,
   `describe_system` and `list_devices` (`mcp_server.py`) return bare dicts
   with no envelope and no `api` twin. They are transport shapes, not
   documents; when they route through `api`, they join the union.
4. **`catalog` has no producer today; `projected` does, and it is marked.**
   Measured: `uv run python -c "from lockwire import api; ..."` against
   `tests/fixtures/x500/contract.yaml` — `api.check` returns two
   `kind='projected'` alternatives (`advice.build_rail_alternatives`, reached
   through `checks.check_budgets`), both carrying `mark='projected'`.
   `advice.Alternative` is self-kinded (`SELF_KINDED`, in tests/payload_sweep.py), so this file's
   `_build_kind_declaration`/`_assert_marked_kind_has_mark` never sees it; the enforcement lives on
   `Alternative` itself, next to `Figure`'s copy of the same guard (a projected figure reaching a door
   carries its mark and its word, not a corrected sentence saying it is
   unenforced). `catalog` genuinely has no producer yet; this file does not
   invent the catalog (that is `e1-cat`/O35).
   **The standing rule this seam earns: any `Measured:` claim in shipped
   source names the command that measured it.** A measurement claim with no
   command behind it is the same defect class as a test that asserts a
   document against itself.
5. **O33 — the accessibility strings are NOT in this union.** Plan v8 line 1259
   records no default for whether screen-reader words live in the payload, and
   inventing their shape at the freeze point is worse than leaving the seam
   open. `EXTENSION_POINTS` names the empty slot; nothing is modelled.
6. **No `STOP` vocabulary.** The verdict words here are exactly
   `report.verdict_word`'s (`GO`, `GO_WITH_NOTES`, `NO-GO`, `NOT_SCORED`,
   `REFUSED`). `PagePayload.state`'s `"stop"` is `gui._headline`'s existing
   CSS-state key, not a word on screen; this file does not add one.
7. **`cli_line` is optional on every machine member** because
   `mcp_server._with_cli_line` adds it to `api`'s document in place. Without
   that field `extra="forbid"` would reject the MCP door's own output, which is
   the same document — so the union covers both doors, and
   `test_mcp_documents_validate_as_the_same_union_members` holds it.
8. **`diff` has no envelope either.** `api.diff` (e0-34) returns
   `FactDiff.as_json()` straight, with no `artifact_type`, so `DiffPayload` is
   keyed by name beside the page in `UNENVELOPED`. Enveloping it is a change to
   `factdiff`/`api.diff`, which is e0-63's lane (*"the library leg of diff is
   anchored by nothing"*), not this file's.
9. **`log` is a top-level list.** `api.log` returns history rows directly, so
   `LogPayload` is a named root-list member beside `diff` in `UNENVELOPED`.
   The caller names the member because a list has no discriminator field.
10. **Not every preformatted number could become a `Figure`.** Measured:
   `scripts/pytest_slot.sh uv run pytest -q tests/test_api_payload.py -k
   test_no_door_emits_a_preformatted_number_outside_the_named_exemptions`
   (2026-09-08, `1 passed`) — the figures this door owns and holds typed
   data for are typed (`SceneLabel`, `DesignFacePart.draw`). The rest are
   assembled with words inside `checks.py`, `report.py` and `report_html.py` —
   typing them means rewriting a producer, which is the re-implementation this
   module forbids here. Each is named in `STRING_FIGURE_EXEMPTIONS` with its reason
   rather than left silent, and the exemption list is pinned both ways.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from lockwire import plugins
from lockwire.advice import Alternative
from lockwire.plan import AssertionOutcome
from lockwire.plan_payload import (
    Instead,
    PlanPayload,
    Target,
    VerdictWord,
    _Document,
    _Strict,
    _VerdictDocument,
)
from lockwire.report import ScoredRecord
from lockwire.report_json import (
    CHECK_ARTIFACT_TYPE,
    PLAN_ARTIFACT_TYPE,
)
from lockwire.schema import Provenance

DESCRIBE_ARTIFACT_TYPE = "lockwire-describe"
EDIT_ARTIFACT_TYPE = "lockwire-edit-result"


# --------------------------------------------------------------------- number kinds


#: the five kinds. ``derived`` names its check in the table's second column.
NumberKind = Literal["declared", "measured", "derived", "catalog", "projected"]

#: The two kinds the number-kind rule says may never be rendered in the measured style. A row
#: with one of these must carry the word to render beside the figure.
MARKED_KINDS = ("catalog", "projected")


def _assert_marked_kind_has_mark(kind: str, mark: str | None) -> None:
    """Asserts marked kinds carry their required word. This checks that catalog
    and projected figures declare their mark; it does NOT check that the kind is
    true of the value, because NUMBER_KINDS is keyed on field name and cannot
    distinguish which producer filled it when multiple producers write the same
    field. One function for both the static table (``_build_kind_declaration``)
    and runtime records (``Figure``) prevents drift between two implementations
    of the number-kind rule."""
    if kind in MARKED_KINDS and not mark:
        raise ValueError(f"a {kind!r} number must carry its own word, e.g. mark={kind!r}")


def _build_kind_declaration(kind: NumberKind, by: str, mark: str | None = None) -> dict[str, str]:
    """Builds one ``NUMBER_KINDS`` row, asserting marked kinds carry required marks.

    This declares that a numeric field has a kind from the allowed vocabulary
    (declared, measured, derived, catalog, projected) and that marked kinds carry
    their required word. It does NOT validate that the kind is true of the value —
    the table is keyed on field name and cannot see which producer filled it.
    When multiple producers write the same field, this table asserts only the
    first producer's kind; resolving which producer is correct requires runtime
    context the table cannot have.

    The guard is here rather than in a test because this file is what six other
    boxes add rows to: a box that adds the product's first ``catalog`` figure
    without the word gets an ImportError, not a review comment.
    """
    _assert_marked_kind_has_mark(kind, mark)
    row = {"kind": kind, "by": by}
    if mark:
        row["mark"] = mark
    return row


class Figure(_Strict):
    """**One number on screen, typed** — the shape every figure a door renders
    as a number takes, so the number kind is a property of the value rather than of a
    lookup table someone forgot to extend.

    ``NUMBER_KINDS`` (below) covers numeric-*typed* fields. It cannot cover a
    number a producer has already formatted into a ``str``: measured on the
    x500 fixture, ``SceneLabel.figure`` read ``"35.20 W vs <= 15.60 W"`` — two
    numbers on screen, the exact words, carrying no kind, with nothing to
    notice. *A number stopped being a number, for that guard, the moment
    someone formatted it.* **The number kind is a claim about
    numbers on screen, so a preformatted string figure defeats it.**

    A ``Figure`` carries its own kind, so a field typed as one needs no
    ``NUMBER_KINDS`` row — ``numeric_fields()`` skips it deliberately.
    **The renderer formats it** (``value:.2f`` + unit, ``report.format_magnitude``'s
    one rule, one line of JS in ``gui``/``scene_js``); no producer here emits a
    preformatted numeric string, and
    ``test_no_door_emits_a_preformatted_number_outside_the_named_exemptions``
    holds that against every document the doors actually emit.
    """

    value: float
    unit: str
    kind: NumberKind
    #: The producer that filled it in — ``derived``'s "named check" in the number-kind rule.
    by: str
    #: the word for a ``catalog``/``projected`` figure. Required for those.
    mark: str | None = None

    @model_validator(mode="after")
    def _carries_its_word(self) -> Figure:
        _assert_marked_kind_has_mark(self.kind, self.mark)
        return self


#: ``Model.field`` -> its number-kind and the producer that fills it in. Every
#: numeric field reachable from a union member has a row; the walker below
#: enumerates them from the models, so this table cannot quietly fall behind.
#:
#: Foreign models (``report.Finding``, ``plan.AssertionOutcome``,
#: ``schema.Quantity``) get their rows here too — their fields live in
#: ``report.py``/``plan.py``/``schema.py``, which this box may not touch
#: (``schema.py`` is frozen), so the union declares their kinds rather
#: than annotating them at the source. When those modules carry kinds
#: themselves, these rows move there.
NUMBER_KINDS: dict[str, dict[str, str]] = {
    # --- the verdict documents: every count is report.verdict's arithmetic
    "_VerdictDocument.exit_code": _build_kind_declaration("derived", "report.verdict"),
    "_VerdictDocument.scored": _build_kind_declaration("derived", "report.verdict"),
    "Counts.violation": _build_kind_declaration("derived", "report.verdict"),
    "Counts.warning": _build_kind_declaration("derived", "report.verdict"),
    "Counts.not_scored": _build_kind_declaration("derived", "report.verdict"),
    # --- the Add part picker: a catalog number; its own `kind` field says datasheet or assumed
    "AddPartNumber.value": _build_kind_declaration(
        "catalog", "catalog.SourcedNumber", mark="catalog"
    ),
    # --- one finding's numbers
    "Finding.stage": _build_kind_declaration("declared", "schema.Contract (contract section 3.2)"),
    "Quantity.value": _build_kind_declaration("declared", "schema.Contract"),
    # --- the robot view: model frames composed up the parent chain; limits as declared
    "RobotJoint.position": _build_kind_declaration("derived", "gui._robot_model"),
    "RobotJoint.axis": _build_kind_declaration("derived", "gui._robot_model"),
    "RobotJoint.lower": _build_kind_declaration("declared", "schema.Joint"),
    "RobotJoint.upper": _build_kind_declaration("declared", "schema.Joint"),
    "RobotJoint.effort": _build_kind_declaration("declared", "schema.Joint"),
    "RobotLink.position": _build_kind_declaration("derived", "gui._robot_model"),
    # --- the page dict
    "PagePayload.scored": _build_kind_declaration("derived", "checks.run_record_checks"),
    "PagePayload.violations": _build_kind_declaration("derived", "report.verdict"),
    "PagePayload.warnings": _build_kind_declaration("derived", "report.verdict"),
    "PagePayload.unchecked": _build_kind_declaration("derived", "report.verdict"),
    "Card.count": _build_kind_declaration("derived", "gui._cards"),
    "BudgetRow.value": _build_kind_declaration("derived", "gui._budget_rows"),
    "GraphNode.value": _build_kind_declaration("derived", "report_html._graph"),
    "GraphNode.limit": _build_kind_declaration("declared", "schema.Contract"),
    "BudgetRow.limit": _build_kind_declaration("declared", "schema.Contract"),
    "Constraint.used": _build_kind_declaration("derived", "constraints.constraint_rows"),
    "Constraint.limit": _build_kind_declaration("declared", "constraints.constraint_rows"),
    "Constraint.margin_pct": _build_kind_declaration("derived", "constraints.constraint_rows"),
    # --- the 3D scene: measured off the model file, not off the contract
    "PackedVectors.scale": _build_kind_declaration("measured", "scene.load_scene"),
    "PackedVectors.offset": _build_kind_declaration("measured", "scene.load_scene"),
    "ScenePart.color": _build_kind_declaration("derived", "gui._scene_payload"),
    "ScenePart.translation": _build_kind_declaration("measured", "scene.load_scene"),
    "ScenePayload.bounds": _build_kind_declaration("measured", "scene.load_scene"),
    "ScenePayload.triangles": _build_kind_declaration("measured", "scene.load_scene"),
    "ScenePayload.vertices": _build_kind_declaration("measured", "scene.load_scene"),
    "ScenePayload.display_triangles": _build_kind_declaration("measured", "scene.load_scene"),
    # --- the declare form (E4-df): every row is, by definition, a fact the
    # contract itself would hold — the own example row.
    "DeclareRow.value": _build_kind_declaration("declared", "schema.Contract"),
    # `Provenance.conditions` reaches the union for the first time through
    # `DeclareRow.provenance` — an env channel reading recorded at lock time,
    # not typed anywhere until now.
    "Provenance.conditions": _build_kind_declaration(
        "measured", "schema.Provenance (env channel at lock time)"
    ),
    "ForceFigure.value": _build_kind_declaration("derived", "forces.force_document"),
    "ForceFigure.reaction_n": _build_kind_declaration("derived", "forces.force_document"),
    "ForceFigure.moment_nm": _build_kind_declaration("derived", "forces.force_document"),
}

#: Named, empty. O33 (screen-reader words) has no decided wording and no default;
#: the slot is named so six downstream boxes do not each invent one.
EXTENSION_POINTS: tuple[str, ...] = ("accessibility_strings",)


# ---------------------------------------------------------------- envelope


class WhatIfFact(_Strict):
    quantity: str
    kind: Literal["finding", "passed_comparison"]
    severity: str | None
    source: str | None
    witness: str | None
    provenance_state: Literal["recorded", "missing_evidence"]


class RecordComparison(_VerdictDocument):
    artifact_type: Literal["lockwire-record-comparison"]
    facts: list[WhatIfFact]
    comparisons: list[ScoredRecord]


class WhatIfPayload(_Document):
    artifact_type: Literal["lockwire-what-if-report"]
    current: RecordComparison
    proposed: RecordComparison
    diff: str


class ForceFigure(_Strict):
    """One self-described number from ``forces.force_document``."""

    value: float | list[float] | None
    unit: str
    kind: str
    not_scored_because: str | None = None
    id: str | None = None
    quantity: str | None = None
    complete: bool | None = None
    limitation: str | None = None
    frame: str | None = None
    note: str | None = None
    reaction_n: float | None = None
    moment_nm: float | None = None
    span_axis: str | None = None


class ForceMountShape(_Strict):
    """One mount shape read from ``Physical`` by the force document."""

    id: str
    kind: Literal["plate", "bracket", "tube", "custom-mesh"]
    dimensions: ForceFigure
    fastener_pattern: str | None
    mesh_path: str | None


class ForceDocument(_Strict):
    """The force lane carried by the check document."""

    gravity: ForceFigure
    mass_total: ForceFigure
    cg: ForceFigure
    masses: list[ForceFigure]
    mounts: list[ForceFigure]
    mount_shapes: list[ForceMountShape]
    not_scored: list[ForceFigure]


class CheckPayload(_VerdictDocument):
    """``api.check`` / ``lockwire check --json``.

    ``scene`` is a sidecar-only addition (e1-b2, not a CLI field — T2 keeps
    ``lockwire check`` scene-free by design, ``cli.py:454``): present only on
    the ``/v1/run`` reply, and only when a scene sits beside the contract.
    ``None`` when there is no scene to offer; ``SceneUnchanged`` when the
    caller's ``scene.have`` already matches the current hash.

    ``headline`` (E2-hl) is the plain-words sentence for the verdict --
    ``api.check``'s own field, not shared with ``PlanPayload``
    (its door does not fill it), computed once by ``report.verdict_headline``
    and reused, never re-derived by a consumer.

    ``graph`` rides beside ``scene`` for the same reason and on the same
    condition (E2-j2): the app's Design tab needs the lens bar's words
    (``GraphLens.label``/``.caption``) next to the scene it labels, and
    ``report_html._graph`` already computes exactly this dict for the page
    payload (``gui.py:1019``) — this is the same call, not a second one.
    ``None`` when there is no contract to derive it from (a refusal never
    reaches this far) or no scene sits beside the contract, matching
    ``scene``'s own condition so the two never disagree about whether a
    Design tab has anything to show.
    """

    artifact_type: Literal["lockwire-check-report"]
    scene: ScenePayload | SceneUnchanged | None = None
    headline: Annotated[str, Field(min_length=1, pattern=r"\S")]
    graph: Graph | None = None
    forces: ForceDocument
    revision: str | None = None


class DescribePayload(_Document):
    """``api.describe`` — filename -> Markdown. The member that carries prose,
    not the page dict: ``files`` is ``describe.describe_all``'s output."""

    artifact_type: Literal["lockwire-describe"]
    robot: str
    files: dict[str, str]


class EditPayload(_Document):
    """``api.declare`` — the write door's document. This is the member that
    carries a diff; ``written`` is False under ``--dry-run``.

    ``contract_sha`` is ``None`` with ``contract_sha_error`` set (E5-D3, same
    shape as e5-d-r2's fix on ``api.add``) when the write landed but the
    post-write disk re-read that names its sha then faulted — ``written``
    still reports what the writer did, never the re-read's outcome."""

    artifact_type: Literal["lockwire-edit-result"]
    contract_sha: str | None
    revision: str | None = None
    contract_sha_error: str | None = None
    written: bool
    diff: str
    lines: list[str]


# --------------------------------------------------------------------------- cards


class RobotJoint(_Strict):
    """A model joint with its frame in the single robot frame (parent chain composed)."""

    name: str
    type: Literal["fixed", "revolute", "continuous", "prismatic", "floating", "planar"]
    parent: str
    child: str
    parent_name: str  # copy.human_name of parent / child: what the app calls the two links
    child_name: str
    position: list[float]  # metres, robot frame
    axis: list[float]  # joint axis in the robot frame
    lower: float | None
    upper: float | None
    effort: float | None


class RobotLink(_Strict):
    name: str
    position: list[float]  # link origin, metres, robot frame


class Card(_Strict):
    severity: Literal["violation", "warning", "not_scored"]
    tag: str
    title: str
    body: str
    fix: str
    raw: str
    hits: list[str]
    faults: list[str]
    affected: list[str]
    targets: list[Target]
    whole: bool = False
    count: int
    count_figure: Figure
    observed: Figure | None
    expected: Figure | None
    alternatives: list[Alternative]
    figure_note: str | None
    shape: str
    #: 1-3 word name of the check (gui.finding_label), for grouped rows.
    label: str
    #: Row title for N repeats of this card, "{n}" standing for the count ("{n} drives unmapped").
    group_title: str
    #: The alternatives as short rows (the full ones stay in ``alternatives``).
    instead: list[Instead]
    also_in_run: bool
    consumer: list[str] | None = None
    #: The first finding's quantity path, so a number or fix can open its Declare row.
    quantity: str | None = None
    #: The first finding's owner, so an owner pill can select that owner's card.
    owner: str | None = None


class AddsUpRow(_Strict):
    name: str
    text: str
    margin: str
    raw: str
    # The standing mark when this pass is a budget cleared by
    # raising its limit — the only card a raised-then-passing budget reaches.
    mark: str | None


class SetupGap(_Strict):
    title: str
    why: str
    snippet: str


class BudgetRow(_Strict):
    id: str
    kind: str
    value: float | None
    limit: float
    unit: str
    direction: str
    state: str
    note: str | None
    # E1-f2, landed since this model's first version: a limit carries the same
    # provenance trio the findings do, read off `budget.provenance`.
    source: str | None
    witness: str | None
    locked_at: str | None
    # The standing mark when this limit was raised to clear a
    # violation. Rendered, never scored — no verdict or exit code moves.
    limit_raised: str | None


class Constraint(_Strict):
    """one constraint instance the engine evaluates, for badges and margin bars
    (``constraints.constraint_rows``). ``domain`` maps the graph lenses: mechanical ->
    mechanical, power -> electrical, data -> data, health -> health, plus thermal (no lens).
    ``used``/``limit`` are null when the engine has no number (status "unset"); each carries
    its number-kind in ``used_kind``/``limit_kind`` (per row; NUMBER_KINDS is per field)."""

    id: str
    label: str
    domain: Literal["mechanical", "electrical", "data", "thermal", "health"]
    used: float | None
    limit: float | None
    unit: str
    margin_pct: float | None
    status: Literal["ok", "warn", "bad", "unset"]
    used_kind: NumberKind | None
    limit_kind: NumberKind | None
    #: Scene part ids (contract ids that have geometry) / every contract id involved.
    parts: list[str]
    components: list[str]


# --------------------------------------------------------------------------- graph


class GraphNode(_Strict):
    id: str
    label: str
    sub: str
    kind: Literal["link", "device", "rail", "source", "host", "bus"]
    #: The number ``sub`` words, in base units: W (power: device draw, rail load),
    #: bit/s (bus), kg (mechanical). None when undeclared or not summable.
    value: float | None = None
    #: A rail's capacity in W; ``value > limit`` is an overloaded rail.
    limit: float | None = None


class GraphEdge(_Strict):
    from_: str = Field(alias="from")
    to: str
    label: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


#: The full lens namespace (E1-ln) — every place a lens is drawn, colored or
#: joined against a scene part uses this set. the words that go with
#: each member (``GraphLens.label``/``.caption``) are the payload's, never
#: composed in the app/GUI JS.
LensId = Literal["off", "mechanical", "power", "data", "health"]


class GraphLens(_Strict):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    #: Button text ("Power") — payload-owned so the face never derives it
    #: from the key.
    label: str
    #: The one-line hint under the lens bar ("What feeds what, and how many
    #: watts down each wire").
    caption: str


class Graph(_Strict):
    off: GraphLens
    mechanical: GraphLens
    power: GraphLens
    data: GraphLens
    health: GraphLens


# --------------------------------------------------------------------------- scene
# scene.py/scene_view.py pack positions/normals/indices as base64 dicts — modelled
# as they are, not decoded.


class PackedVectors(_Strict):
    type: Literal["i16"]
    data: str
    scale: float | None = None
    offset: list[float] | None = None


class PackedIndices(_Strict):
    type: Literal["u16", "u32"]
    data: str


class SceneGroup(_Strict):
    """the component drop-down a part is listed under (id, 1-3 word label, kind)."""

    id: str
    label: str
    kind: Literal["device", "rail", "bus", "link", "other"]


class ScenePart(_Strict):
    id: str | None
    node: str
    #: Plain name for the row (copy.human_name of node); node stays the tooltip.
    name: str
    state: Literal["fault", "warn", "affected", "ok", "unchecked", "plain"]
    positions: PackedVectors
    normals: PackedVectors | None
    indices: PackedIndices
    color: list[float]
    translation: list[float]
    #: Every lens this part belongs to (E1-ln) — computed from the
    #: contract join (`scene_view.part_lenses`), never guessed by the face.
    #: `off` and `health` are whole-scene overlays so every part carries
    #: them; `mechanical`/`power`/`data` are earned by the part's contract
    #: role, same join `report_html._graph` uses for the wiring diagram.
    lenses: list[LensId]
    group: SceneGroup
    #: A device part's declared physical.mount_frame; a drop on it places the dragged part there.
    mount_frame: str | None = None


class SceneLabel(_Strict):
    """The pinned label the 3D scene draws on a faulty part.

    ``figure: str`` until 2026-09-08 — ``"35.20 W vs <= 15.60 W"``, the very
    string that showed a formatted number defeats ``NUMBER_KINDS``. Now the two
    numbers are typed and the callout in ``scene_js`` formats them.
    Numeric findings carry typed magnitudes. Structural findings can carry
    text operands without numeric quantities, so either figure remains
    ``| None``; the renderer omits an absent figure instead of inventing one.
    """

    part: str
    severity: str
    title: str
    observed: Figure | None
    #: How ``expected`` bounds ``observed`` — ``"<="``, ``">="``, or ``""``.
    #: A comparator is a symbol, not a number, so it stays a string.
    comparator: str
    expected: Figure | None


class ScenePayload(_Strict):
    asset_path: str | None = None
    asset_sha256: str | None = None
    expected_asset_sha256: str | None = None
    source: str
    #: sha256 of the source geometry (``scene_view.scene_hash``) — same scene,
    #: same hash, regardless of display decimation. What a caller's next
    #: ``scene.have`` is compared against (e1-b2).
    hash: str
    parts: list[ScenePart]
    labels: list[SceneLabel]
    scene_only: list[str]
    contract_only: list[str]
    bounds: list[list[float]]
    triangles: int
    vertices: int
    display_triangles: int
    honesty: str


class SceneUnchanged(_Strict):
    """The sidecar's reply when a caller's ``scene.have`` already matches —
    ships no geometry (e1-b2). ``unchanged`` is always ``True``; the shape
    exists so ``{"unchanged": true}`` is a typed member, not a bare dict."""

    unchanged: Literal[True]
    asset_path: str | None = None
    asset_sha256: str | None = None
    expected_asset_sha256: str | None = None


# --------------------------------------------------------------------------- faces


class DesignFacePart(_Strict):
    id: str
    rail: str
    #: ``"0.40 W"`` until 2026-09-08; a declared draw is a number on screen, so
    #: it is typed. ``None`` when the device declares no power at all — the
    #: renderer draws the em dash it used to be handed.
    draw: Figure | None


class DesignFace(_Strict):
    title: str
    parts: list[DesignFacePart]
    cli: str


class DeclareFace(_Strict):
    title: str
    lines: list[str]


class PlanFace(_Strict):
    title: str
    cli: str
    # `gui._plan_face` emits `{"path", "status"}` per outcome, which is
    # `plan.AssertionOutcome` field for field — the same model the plan
    # document uses, not a second copy of it.
    outcomes: list[AssertionOutcome] | None


class ReferenceFaceDevice(_Strict):
    id: str
    name: str  # copy.human_name of the id; the id stays the jump target
    text: str


class ReferenceDrawer(_Strict):
    """One of the Reference face's four drawers — a thing the
    CLI already produces (``describe``/``report``/``baseline``/``scan``),
    named with its content, its provenance and the exact CLI line that
    reproduces it in a terminal. Nothing here is computed twice: ``content``
    is always a producer's own output (``describe_all`` for the describe
    drawer; a fixed description for the other three, which need inputs the
    page does not have — a recording, this machine)."""

    title: str
    content: str
    provenance: str
    cli_line: str


class ReferenceFace(_Strict):
    title: str
    devices: list[ReferenceFaceDevice]
    drawers: list[ReferenceDrawer]
    #: The rail roll-up rendered as a power tree (source ->
    #: converter -> rail -> loads), plain pre-formatted text — not a number-kind
    #: number, the same treatment `ReferenceDrawer.cli_line` above gets.
    #: `None` when the contract has no rails to draw (`describe.render_power_tree`
    #: returns `[]`).
    power_tree: str | None


class Faces(_Strict):
    design: DesignFace
    declare: DeclareFace
    plan: PlanFace
    reference: ReferenceFace


# --------------------------------------------------------------------- declare form


class DeclareRow(_Strict):
    """One quantity ``lockwire declare`` could write, present or not (E4-df).

    Walked from ``schema.Contract``'s own models (nothing here re-derives
    a value or invents a path) — ``path`` is the exact ``/``-separated string
    ``declare``'s own ``path`` argument takes. ``kind`` is always ``"declared"``:
    every one of these is, by definition, a fact the contract itself would
    hold (the own example row is ``Quantity.value`` -> ``declared``). Scoped
    to ``schema.Quantity``-typed fields only — a bare unitless number
    (``efficiency``, ``gear_ratio``) is not a "quantity" in the sense and is
    out of this box's charter.

    ``value``/``unit`` are ``None`` when the fact has never been declared;
    ``provenance`` is the nearest ancestor block's own ``provenance`` (schema's
    per-block provenance, same rule ``edit.provenance_owner_path`` already
    encodes), ``None`` when nothing has stamped one yet.
    """

    path: str
    label: str
    #: The part/device the quantity belongs to ("Base") and what it is ("mass"):
    #: the short pair the Declare list shows, so the app never trims `label`.
    group: str
    short: str
    unit: str | None
    kind: NumberKind
    value: float | list[float] | None
    provenance: Provenance | None
    #: The drawn part to light when the row's own link has no geometry (inertial-only).
    part: str | None = None


class DeclareWitnessBox(_Strict):
    """The declare form's witness field, pre-filled exactly as a submit with no
    ``--witness`` would resolve it (``edit.resolve_witness``) — a config
    default arrives already marked ``"(default)"``; no default is `""`, which
    still refuses on submit exactly like the CLI (O8's refusal side)."""

    prefill: str


class AddPartNumber(_Strict):
    """One sourced catalog number, pre-filled in the Add part form with its badge."""

    key: str
    value: float
    unit: str
    kind: Literal["datasheet", "source_file", "assumed"]
    source: str


class AddPartOption(_Strict):
    """A catalog part the Add part form can pick, and the contract rails its voltage fits."""

    id: str
    name: str
    numbers: list[AddPartNumber]
    #: ids of the contract's rails inside the part's input voltage range; empty = none, never a guess.
    fits: list[str]


class AddPartPayload(_Strict):
    rails: list[str]
    parts: list[AddPartOption]


class DeclareFormPayload(_Strict):
    """``gui.build_declare_form`` — every declarable quantity plus the witness
    box (E4-df). Unenveloped like the page (seam 1): this box's face render is
    the app's own, not this door's, so there is no ``artifact_type`` to
    discriminate on yet."""

    rows: list[DeclareRow]
    witness: DeclareWitnessBox
    revision: str | None = None
    add_part: AddPartPayload = Field(default_factory=lambda: AddPartPayload(rails=[], parts=[]))


# --------------------------------------------------------------------- the page


class StartPayload(_Strict):
    path: str
    revision: str
    written: bool
    note: str | None = None


class Starter(_Strict):
    """A bundled "New robot" starter: its folder name, the archetype title, and the real robot it is based on."""

    name: str
    title: str
    based_on: str


class StartersPayload(_Strict):
    starters: list[Starter]


class PagePayload(_Strict):
    """``gui.build_payload``'s dict, field for field — the union's largest
    member, and the only one with no envelope (seam 1 in the module docstring).
    """

    robot: str
    revision: str | None = None
    contract_id: str
    #: `gui._headline`'s CSS state key, not a word on screen. The word STOP is
    #: reserved for a future change; nothing here spells it.
    state: Literal["stop", "warn", "neutral", "go"]
    headline: str
    qualifier: str
    scored: int | None
    violations: int
    warnings: int
    unchecked: int
    adds_up: list[AddsUpRow]
    cards: list[Card]
    joints: list[RobotJoint]
    links: list[RobotLink]
    graph: Graph
    budgets: list[BudgetRow]
    constraints: list[Constraint]
    setup: list[SetupGap]
    scene: ScenePayload | None
    modes: list[str]
    mode_labels: dict[str, str]
    command_modes: dict[str, str]
    faces: Faces
    cli_line: str | None
    #: E0-30/`report_html.document_identity` — contract path, commit/hash,
    #: lockwire version, generated timestamp, the same footer `report` renders.
    document_identity: str


# ---------------------------------------------------------------------- diff
# `api.diff` (e0-34, merged after this file's first version) returns
# `factdiff.FactDiff.as_json()` — a document with no `report_json.envelope`, so
# like the page it is keyed by name rather than discriminated. Seam 8 below.


class FieldChangeRow(_Strict):
    path: str
    #: A declared value of whatever type the contract field holds — a float, a
    #: string, a list. `None` when the field was not declared on that side.
    old: Any
    new: Any
    witness: str | None
    source: str | None
    limit_raised: bool


class EntityChangeRow(_Strict):
    kind: str
    id: str


class FindingRefRow(_Strict):
    severity: str
    quantity: str


class DiffPayload(_Strict):
    """`api.diff` / `lockwire diff --json` — what changed in the facts."""

    changed: bool
    changes: list[FieldChangeRow]
    added: list[EntityChangeRow]
    removed: list[EntityChangeRow]
    findings_new: list[FindingRefRow]
    findings_gone: list[FindingRefRow]


class LogEntry(_Strict):
    """One git-backed contract-history row from ``api.log``."""

    sha: str
    author: str
    date: str
    message: str
    verdict: VerdictWord
    fact_diff: DiffPayload | None


class LogPayload(RootModel[list[LogEntry]]):
    """``api.log`` / ``lockwire log --json`` history, newest first."""


class BlameRow(_Strict):
    """One unchanged declared fact row from the public blame producer."""

    path: str
    value: dict[str, Any]
    source: str
    witness: str
    locked_at: str
    compared_from: str | None = None
    last_changing_commit: str | None = None


class BlamePayload(RootModel[list[BlameRow]]):
    """``lockwire blame --json`` rows; api.blame retains its tuple ABI."""


# ------------------------------------------------------------------ the union

#: ``artifact_type`` -> the member that describes that document. The six
#: enveloped members; the page dict is keyed separately because it has none.
MEMBERS: dict[str, type[_Document]] = {
    "lockwire-what-if-report": WhatIfPayload,
    CHECK_ARTIFACT_TYPE: CheckPayload,
    PLAN_ARTIFACT_TYPE: PlanPayload,
    DESCRIBE_ARTIFACT_TYPE: DescribePayload,
    EDIT_ARTIFACT_TYPE: EditPayload,
}

#: Every member, enveloped and un-enveloped, in one tuple — what the sweep and
#: the export walk.
ALL_MEMBERS: tuple[type[BaseModel], ...] = (
    *MEMBERS.values(),
    PagePayload,
    DiffPayload,
    LogPayload,
    BlamePayload,
    DeclareFormPayload,
    StartPayload,
    StartersPayload,
)


# --------------------------------------------------------------------- export


def openapi_document(
    all_members: tuple[type[BaseModel], ...],
    members: dict[str, type[_Document]],
    number_kinds: dict[str, dict[str, str]],
) -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    for model in all_members:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    return {
        "openapi": "3.1.0",
        "info": {"title": "lockwire", "version": "0.1.0"},
        "paths": {},
        "components": {"schemas": schemas},
        "x-lockwire-members": {kind: model.__name__ for kind, model in members.items()},
        "x-lockwire-number-kinds": number_kinds,
        "x-lockwire-extension-points": list(EXTENSION_POINTS),
    }


def _write_document(path: Path | str, document: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def export_openapi(path: Path | str) -> None:
    """Write the whole union as one OpenAPI-shaped schema document.

    Not a served OpenAPI document — there are no paths here; it is the schema
    map ``openapi-typescript`` reads (``packaging/generate-types.md``) to emit
    ``app/src/shared/api.gen.ts`` (e1-d3). Every member is named, so a member
    added without a discriminator entry cannot silently generate a missing TS
    type, and ``x-lockwire-number-kinds`` ships the table so the app renders
    a catalog or projected figure's mark rather than re-deriving it.

    Members a plugin registers (``plugins.PAYLOAD_*``) go to a sibling
    ``pro/<name>``; the free document never carries them.
    """
    _write_document(path, openapi_document(ALL_MEMBERS, MEMBERS, NUMBER_KINDS))
    plugins.ensure()
    if plugins.PAYLOAD_MEMBERS or plugins.PAYLOAD_EXTRA:
        out = Path(path).parent / "pro" / Path(path).name
        out.parent.mkdir(exist_ok=True)
        _write_document(
            out,
            openapi_document(
                (*plugins.PAYLOAD_MEMBERS.values(), *plugins.PAYLOAD_EXTRA),
                plugins.PAYLOAD_MEMBERS,
                plugins.PAYLOAD_NUMBER_KINDS,
            ),
        )
