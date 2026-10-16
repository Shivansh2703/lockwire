"""The GUI page: it must open on something, group honestly, and never soften a finding away."""

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.page import _targets, build_declare_form, build_payload, short_name
from lockwire.report import Finding
from lockwire.schema import (
    Bus,
    Contract,
    Host,
    RobotInfo,
    Topology,
    load_contract,
)
from lockwire.urdf import parse_urdf

from conftest import LIMIT_RAISED_MARK_TEXT


runner = CliRunner()


FIXTURES = Path(__file__).parent / "fixtures"


X500 = FIXTURES / "x500" / "contract.yaml"


CLEAN = FIXTURES / "contract_clean.yaml"


DEMO = FIXTURES / "contract_demo.yaml"


EMPTY = FIXTURES / "contract_empty.yaml"


# --------------------------------------------------------------------------- naming


def test_short_name_drops_only_the_machine_prefix():
    assert short_name("dev.companion.jetson-orin-nano") == "companion.jetson-orin-nano"
    assert short_name("rail.pm02-5v") == "pm02-5v"
    assert short_name("x500_frame") == "x500_frame"


# ------------------------------------------------------------------- opening screen


def test_x500_power_card_note_is_plain_words():
    cards = build_payload(load_contract(X500))["cards"]
    card = next(c for c in cards if "bud.power.pm02-5v" in c["raw"])
    assert card["figure_note"] == (
        "Observed is the check's sum of your declared draws; Expected is your declared limit."
    )


def test_headline_states_track_severity():
    stop = build_payload(load_contract(X500))
    assert stop["state"] == "stop"
    assert "Don't power it on yet" in stop["headline"]
    # B10 (2026-09-06): the radio joining the power tree adds a second real
    # violation (bud.power.pm02-5v.7w-mode flips to 17.2 W > 15.6 W).
    assert "2 things do not add up" in stop["headline"]

    quiet = build_payload(load_contract(CLEAN))
    assert quiet["state"] in {"go", "warn"}
    assert "Don't power it on" not in quiet["headline"]


def test_headline_says_nothing_compared_when_scored_is_zero_even_with_a_warning(
    empty_budget_contract,
):
    """A2 (usage audit finding 4): an empty rail budget scores zero comparisons
    and fires a WARNING, not a violation. The old wording ("Nothing stops
    you - 1 note worth reading") reads as a pass over an empty denominator.
    Mutation bite: dropping the `adds_up == 0` branch (or checking it after
    the warnings branch) turns this red.

    NOT SCORED is amber, not grey — state is "warn", not
    "neutral".
    """
    payload = build_payload(empty_budget_contract)

    assert payload["scored"] == 0
    assert payload["warnings"] >= 1
    assert payload["headline"] == "Nothing could be compared yet"
    assert payload["state"] == "warn"  # bites it staying green/"go" or grey/"neutral"


def test_not_scored_headline_is_amber_not_grey(empty_budget_contract):
    """NOT SCORED is "amber not green" —
    and, by the same token, not grey either. `_STATE_BY_VERDICT_CODE` and the
    zero-denominator branch in `_headline` both used to collapse `NOT_SCORED`
    to `"neutral"`, which renders with `var(--muted)` (grey), not the `"warn"`
    state's `var(--warn)` (amber).

    Mutation bite: reverting either mapping back to `"neutral"` turns this red.
    """
    from lockwire.checks import run_record_checks
    from lockwire.report import verdict

    decision = verdict(run_record_checks(empty_budget_contract))
    assert decision.code == "NOT_SCORED"

    payload = build_payload(empty_budget_contract)
    assert payload["state"] == "warn"


def test_empty_state_after_start_names_the_next_step(tmp_path, monkeypatch):
    """The empty-state face. `lockwire start` on a fresh SDF/URDF records the
    frame but declares nothing (no devices/rails/buses/budgets) -- the coordinator's
    PX4 repro (usage audit 2026-09-06) measured an empty qualifier and zero cards,
    which reads as "everything agrees" over a page that checked nothing. The
    qualifier must name the next step, and "What I found" must carry one card
    with the same next step instead of falling back to the generic "nothing
    disagrees" wording.
    """
    monkeypatch.chdir(tmp_path)
    shutil.copy(FIXTURES / "px4vision.sdf", tmp_path / "model.sdf")
    result = runner.invoke(app, ["start", "model.sdf", "--out", "contract.yaml"])
    assert result.exit_code == 0, result.output

    payload = build_payload(load_contract(tmp_path / "contract.yaml"))
    assert payload["headline"] == "Nothing could be compared yet"
    next_step = (
        "nothing declared yet — add what is on it: lockwire add device|rail|bus …, "
        "or declare from the model: lockwire scene import"
    )
    assert payload["qualifier"] == next_step
    assert len(payload["cards"]) == 1
    card = payload["cards"][0]
    assert card["title"] == "Nothing to check yet"
    assert card["body"] == next_step


def test_qualifier_is_silent_when_nothing_was_unchecked():
    """`0 things I couldn't check` is noise that reads as a defect."""
    payload = build_payload(load_contract(X500))
    payload["unchecked"] = 0
    from lockwire.page import _headline

    _state, _headline_text, qualifier = _headline("GO_WITH_NOTES", 0, 1, 0)
    assert qualifier == ""


_STATE_BY_VERDICT_CODE = {
    "REFUSED": "stop",
    "NO_GO": "stop",
    "GO_WITH_NOTES": "warn",
    # A2 (usage audit finding 4): a zero denominator is never green. `_headline`
    # overrides "go" to "warn" whenever `adds_up == 0`, which for every
    # committed fixture coincides exactly with `NOT_SCORED` (amber, not
    # grey) (a warning fixture with a zero denominator too would also show
    # "warn" despite carrying `GO_WITH_NOTES` — none of these fixtures
    # exercise that combination).
    "NOT_SCORED": "warn",
    "GO": "go",
}


@pytest.mark.parametrize(
    "contract_path", sorted(FIXTURES.glob("contract_*.yaml")) + [X500], ids=lambda p: p.name
)
def test_gui_state_agrees_with_verdict_code(contract_path):
    """The state gui.py shows must map from `verdict()`'s code across every fixture."""
    from lockwire.checks import run_record_checks
    from lockwire.report import verdict

    contract = load_contract(contract_path)
    decision = verdict(run_record_checks(contract))
    payload = build_payload(contract)
    assert payload["state"] == _STATE_BY_VERDICT_CODE[decision.code]


def test_gui_state_is_sourced_from_report_verdict(monkeypatch):
    """`build_payload`/`_headline` must decide `state` by calling `report.verdict`,
    the same function `report_json`/`mcp_server`/the terminal already use — not by
    a local `if violations: ... elif warnings: ...` re-derivation, which is a
    second implementation of the exit map that can silently drift from the others.

    Mutation bite: reverting `_headline` to derive `state` from raw severity
    counts again stops calling `verdict` and this fails (call count stays 0).
    """
    import lockwire.page as gui_module
    from lockwire.report import verdict as real_verdict

    calls = []

    def spy(findings):
        calls.append(findings)
        return real_verdict(findings)

    monkeypatch.setattr(gui_module, "verdict", spy)
    build_payload(load_contract(X500))
    assert calls, "build_payload never called report.verdict()"


# ----------------------------------------------------------------------- the wording


def test_rail_card_names_the_rail_not_the_budget_id():
    """`bud.power.pm02-5v` is a lockfile key. `pm02-5v` is the thing on the bench."""
    cards = build_payload(load_contract(X500))["cards"]
    stop = [card for card in cards if card["severity"] == "violation"]
    # B10 (2026-09-06): the radio joining the power tree adds a second real
    # violation on the same rail (bud.power.pm02-5v.7w-mode flips to a violation
    # too), so both the nominal and 7W-mode budgets now render a card.
    assert len(stop) == 2
    titles = sorted(card["title"] for card in stop)
    # The two budgets differ only by draw mode; the mode is the only thing that
    # tells the two cards apart, so the non-nominal one must say it.
    assert titles == [
        "PM02 5 V rail overloaded",
        "PM02 5 V rail overloaded in the 7W mode",
    ]
    for card in stop:
        assert "bud." not in card["title"]
        assert "15.6 W" in card["body"]
        # the console string carries a parenthetical the sentence must not inherit
        assert "draw mode" not in card["body"]
    bodies = " | ".join(card["body"] for card in stop)
    assert "35.2 W" in bodies and "17.2 W" in bodies


def test_card_carries_its_quantity_and_owner_for_click_through():
    """A card's number opens its Declare row; its owner pill selects it."""
    stop = [c for c in build_payload(load_contract(X500))["cards"] if c["severity"] == "violation"]
    assert sorted(c["quantity"] for c in stop) == [
        "budgets/bud.power.pm02-5v",
        "budgets/bud.power.pm02-5v.7w-mode",
    ]
    assert all(c["owner"] for c in stop)
    assert {row["rail"] for c in stop for row in c["instead"]} == {
        "rail.pm02-5v"
    }  # typed, not parsed


def test_rail_card_highlights_the_rail_node_that_exists_on_the_graph():
    """A budget id is never a graph node, so an unresolved hit highlights nothing."""
    payload = build_payload(load_contract(X500))
    stop = next(card for card in payload["cards"] if card["severity"] == "violation")
    power_nodes = {node["id"] for node in payload["graph"]["power"]["nodes"]}
    assert "rail.pm02-5v" in stop["hits"]
    assert set(stop["hits"]) & power_nodes, "the card lights up nothing on the graph"
    assert not any(hit.startswith("bud.") for hit in stop["hits"])


def test_blind_spots_never_use_a_measurement_sentence():
    """`Measured <not declared>` is ungrammatical and reads like a reading was taken."""
    cards = build_payload(load_contract(X500))["cards"]
    blind = [card for card in cards if card["severity"] == "not_scored"]
    assert blind, "fixture no longer has a blind spot to check"
    for card in blind:
        assert not card["body"].startswith("Measured ")
        assert "Needs " not in card["body"], "a blind spot has no measured-vs-needed pair"


# ---------------------------------------------------------------------- the grouping


def test_identical_blind_spots_collapse_into_one_card():
    """Four unrated buses are one sentence, not four near-identical ones.

    B1b (2026-09-06) declared published-default bitrates on the X500
    reference contract's three previously-unrated UART/USB buses, so that
    fixture no longer exercises this grouping (see tests/test_x500.py for
    why). This test now builds its own minimal contract with four
    still-unrated buses so the collapse behaviour keeps a regression test
    independent of the reference fixture's own research state.
    """
    contract = Contract(
        robot=RobotInfo(name="blind-spot-testbot"),
        topology=Topology(
            hosts=[Host(id="h1")],
            buses=[
                Bus(id="bus.uart.telemetry", kind="serial", host="h1"),
                Bus(id="bus.uart.companion", kind="serial", host="h1"),
                Bus(id="bus.usb.companion", kind="usb", host="h1"),
                Bus(id="bus.i2c.external", kind="i2c", host="h1"),
            ],
        ),
    )
    payload = build_payload(contract)
    buses = [card for card in payload["cards"] if "speed not declared" in card["title"]]
    assert len(buses) == 1
    assert buses[0]["count"] == 4
    assert buses[0]["title"].startswith("4 cables")
    # every collapsed member is still named, and its exact text is still carried
    for bus in (
        "bus.uart.telemetry",
        "bus.uart.companion",
        "bus.usb.companion",
        "bus.i2c.external",
    ):
        assert bus in buses[0]["raw"]


def test_violations_never_collapse():
    """Two problems that happen to share a shape are still two problems."""
    contract = load_contract(X500)
    payload = build_payload(contract)
    violations = [card for card in payload["cards"] if card["severity"] == "violation"]
    assert all(card["count"] == 1 for card in violations)


def test_x500_and_anymal_cards_keep_their_typed_figures_and_marks(clean_contract):
    x500 = build_payload(load_contract(X500))["cards"][0]
    assert x500["count_figure"] == {
        "value": 1,
        "unit": "findings",
        "kind": "derived",
        "by": "gui._cards",
    }
    assert x500["observed"] == {
        "value": 35.2,
        "unit": "W",
        "kind": "derived",
        "by": "checks.check_budgets",
    }
    assert x500["expected"] == {
        "value": 15.6,
        "unit": "W",
        "kind": "declared",
        "by": "schema.Contract.budgets.limit",
    }
    projected = next(
        alternative for alternative in x500["alternatives"] if alternative["kind"] == "projected"
    )
    assert projected["mark"] == "projected"

    clean_contract.model = parse_urdf(FIXTURES / "anymal_d.urdf")
    mass_budget = next(budget for budget in clean_contract.budgets if budget.kind == "mass_total")
    mass_budget.sources = "links"
    mass_budget.limit.value = 0.0001
    anymal = next(
        card
        for card in build_payload(clean_contract)["cards"]
        if card["shape"] == "budgets/*" and card["observed"] is not None
    )
    assert anymal["observed"]["kind"] == "derived"
    assert anymal["observed"]["by"] == "checks.check_budgets"
    assert anymal["observed"]["value"] > 50
    assert anymal["expected"] == {
        "value": 0.0001,
        "unit": "kg",
        "kind": "declared",
        "by": "schema.Contract.budgets.limit",
    }


def test_run_page_does_not_double_count_a_finding_also_in_the_record_checks():
    """G2 audit 2 §8: the anymal_d evidence's mass-budget violation was scored
    once by `run_record_checks` and again by the run's cross-record stage —
    same quantity, same observed, same expected — and the headline read "14
    things do not add up" against the terminal's 13. Mutation bite: dropping
    the dedup in `build_payload` re-adds the record section's violation count
    to `everything`, turning this red.
    """
    from lockwire.checks import run_record_checks

    contract = load_contract(CLEAN)
    budget = next(b for b in contract.budgets if b.kind == "mass_total")
    budget.limit.value = 0.0001  # force the existing mass budget over its limit
    original = next(f for f in run_record_checks(contract) if f.severity == "violation")
    # Fabricate the run's own copy of the same fact — same quantity/observed/
    # expected, as the real cross-record stage would re-derive it.
    echoed_finding = Finding(
        severity="violation",
        quantity=original.quantity,
        observed=original.observed,
        expected=original.expected,
        stage=4,
        action=original.action,
    )
    payload = build_payload(contract, preflight=([echoed_finding], "bags/clean"))

    assert payload["violations"] == 1, "the echoed run finding must not be counted twice"
    record_card = next(c for c in payload["cards"] if c["severity"] == "violation")
    assert record_card["also_in_run"] is True


# ------------------------------------------------------------ nothing is softened away


# -------------------------------------------------------------------------- easy setup


def test_a_fresh_lockfile_says_what_to_fill_in_next():
    """A new lockfile is nearly empty. The first screen must say so, not look green."""
    gaps = build_payload(load_contract(EMPTY))["setup"]
    titles = " | ".join(gap["title"] for gap in gaps)
    assert "No power rails declared" in titles
    assert "No cables declared" in titles
    assert "No limits set" in titles
    for gap in gaps:
        assert gap["snippet"].strip(), f"{gap['title']} tells you nothing to type"
        assert gap["why"].strip()


def test_a_complete_lockfile_has_nothing_left_to_set_up():
    assert build_payload(load_contract(X500))["setup"] == []


def test_urdf_frames_with_no_mass_are_worded_apart_from_devices(clean_contract):
    """G2 audit 2 §5/§8: on the anymal_d evidence, 32 of the model's links are
    frames (a camera parent, an optical frame…) with no `<inertial>` — not
    physical parts. Wording them the same as a device missing its weight
    ("N parts have no weight recorded") reads as if 32 real parts were left
    unweighed. Mutation bite: collapsing the frame wording back onto the
    generic "part(s) have no weight recorded" sentence turns this red.
    """
    clean_contract.model.links[1].mass = None  # imu_link: a URDF link, not a device

    cards = build_payload(clean_contract)["cards"]
    frame_card = next(
        card for card in cards if "frame" in card["title"].lower() and "mass" in card["raw"]
    )
    assert "frame" in frame_card["title"].lower()
    assert "part" not in frame_card["title"]

    device_card = next(
        card
        for card in cards
        if "physical/mass" in card["raw"] and card["severity"] == "not_scored"
    )
    assert "part" in device_card["title"]


def test_mass_budget_reads_declared_not_measured(clean_contract):
    """G2 audit 2 §5: a mass_total budget sums the model's own YAML
    declarations (URDF link masses + device masses) — it never took a
    measurement. "Measured 57.03 kg" on the anymal_d evidence implies a scale
    reading that never happened. Mutation bite: reverting to the generic
    `Measured {observed}` sentence for `mass_total` turns this red.
    """
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.limit.value = 0.0001

    cards = build_payload(clean_contract)["cards"]
    mass_card = next(c for c in cards if c["shape"] == "budgets/*" and "kg" in c["body"])
    assert "declared" in mass_card["body"].lower()
    assert "Measured" not in mass_card["body"]
    assert "sum of the model" in mass_card["body"]


def test_zero_mass_links_are_counted_in_singular_english(testbot_urdf, tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    assert (
        runner.invoke(app, ["init", str(testbot_urdf), "--out", str(contract_path)]).exit_code == 0
    )
    gaps = build_payload(load_contract(contract_path))["setup"]
    weights = [gap for gap in gaps if "weigh" in gap["title"]]
    assert weights, "the zero-mass link is not surfaced"
    assert weights[0]["title"] == "1 part of the frame weighs nothing"


def test_the_coverage_disclosure_is_not_named_after_its_own_field():
    """`check/scored` is a coverage disclosure, not a part of the robot. The generic
    path rendered it as "I couldn't check scored" — an internal quantity handed to a
    student as though it were something on the bench — on the `lockwire start` screen.
    """
    cards = build_payload(load_contract(EMPTY))["cards"]
    coverage = [card for card in cards if "compared" in card["title"]]
    assert coverage, "the coverage disclosure vanished"
    assert coverage[0]["title"] == "Nothing could be compared yet"
    assert "two numbers that were written down separately" in coverage[0]["body"]
    for card in cards:
        assert "scored" not in card["title"], f"internal field in a title: {card['title']!r}"


def test_only_what_is_wrong_is_drawn_as_wrong():
    """A device drawing exactly its datasheet figure off an overloaded rail is the
    casualty, not the fault. Painting it in fault colour states something untrue,
    on a tool whose stated death condition is crying wolf.
    """
    payload = build_payload(load_contract(X500))
    stop = next(card for card in payload["cards"] if card["severity"] == "violation")

    assert stop["faults"] == ["rail.pm02-5v"], "the rail is the only thing over its limit"
    for victim in ("dev.companion.jetson-orin-nano", "dev.camera.oak-d-lite"):
        assert victim not in stop["faults"], f"{victim} draws its rated power and is not at fault"
        assert victim in stop["affected"], f"{victim} is on the overloaded rail and must show it"
    # selecting the card still lights everything involved: the split is about colour
    assert set(stop["hits"]) == set(stop["faults"]) | set(stop["affected"])


# ----------------------------------------------------------------------------- the CLI


def test_start_makes_a_lockfile_and_writes_no_page(testbot_urdf, tmp_path):
    contract_path = tmp_path / "lockwire.yaml"
    result = runner.invoke(app, ["start", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert contract_path.exists()
    assert not list(tmp_path.glob("*.html"))


def test_start_does_not_discard_a_lockfile_you_already_built(testbot_urdf, tmp_path):
    """Re-running the first-run command must never silently throw away locked entries."""
    contract_path = tmp_path / "lockwire.yaml"
    runner.invoke(app, ["start", str(testbot_urdf), "--out", str(contract_path)])
    contract_path.write_text(contract_path.read_text() + "\n# hand-locked entry\n")

    result = runner.invoke(app, ["start", str(testbot_urdf), "--out", str(contract_path)])
    assert result.exit_code == 0, result.output
    assert "# hand-locked entry" in contract_path.read_text()
    assert "already exists" in result.output


# ------------------------------------------------------------------- what adds up


def test_headline_and_qualifier_carry_the_adds_up_count():
    payload = build_payload(load_contract(X500))
    assert payload["adds_up"], "x500 fixture is expected to have at least one scored pass"
    n = len(payload["adds_up"])
    assert n > 1, "fixture assumption changed"
    assert "add up" in payload["qualifier"]
    assert f"{n} things add up" in payload["qualifier"]
    # the wrong/couldn't-check wording must stay exactly what it was
    # B10 (2026-09-06): the radio joining the power tree adds a second real violation.
    assert "2 things do not add up" in payload["headline"]


def test_adds_up_headline_count_is_singular_for_exactly_one():
    _state, _headline, qualifier = _headline_direct(1)
    assert "1 thing adds up" in qualifier


def _headline_direct(adds_up: int):
    from lockwire.page import _headline

    return _headline("GO", 0, 0, 0, adds_up)


def test_x500_adds_up_rows_are_plain_words_not_quantity_paths():
    """Each row's title must name the physical thing in the same voice `_sentence()`
    uses elsewhere on this page — not the contract's quantity path or budget id.
    """
    payload = build_payload(load_contract(X500))
    names = [row["name"] for row in payload["adds_up"]]
    # B10: radio adds passing rows. B13 (2026-09-06): its link_budget now scores
    # a real pass too (bundled antenna gain published), adding an 8th row.
    assert len(names) == 8, "fixture assumption changed"
    for name in names:
        assert "/" not in name, f"row title still carries a quantity path: {name!r}"
        assert "bud." not in name, f"row title still names a budget id: {name!r}"

    joined = " | ".join(names)
    # B10 (2026-09-06): the radio joining the power tree flips bud.power.pm02-5v.7w-mode
    # to a real violation, so it drops off this passing list; the new TELEM1 rail's
    # voltage-compatibility and rail-power comparisons take its place.
    assert "pixhawk-telem1-5v: 5.20 V of 5.20 V from rail.pm02-5v" in joined
    assert "pixhawk-telem1-5v rail: 5.20 W of 7.80 W" in joined
    assert "radio.rfd900x on the 5.20 V rail" in joined
    assert "Payload mass: 0.67 kg of 1.50 kg allowed" in joined
    # B67: durations under an hour print in minutes, not a fraction of an hour.
    assert "Flight time on the declared pack: 15.8 min, 15.0 min required" in joined
    assert "servo-motor rail" in joined and "246.68 W of 888.00 W" in joined
    assert "jetson-orin-nano on the 5.20 V rail" in joined
    # B13 (2026-09-06): antenna gain published (RFDesign bundle page), so the
    # link budget now scores a real pass with large headroom.
    assert "radio.rfd900x: 41.30 dB fade margin of 10.00 dB required" in joined


# --------------------------------------------------------------------------- scene payload (T1-T3)


def _scene(*parts):
    from lockwire.scene import Scene

    return Scene(
        parts=tuple(parts),
        up="z",
        nodes_with_ids={p.extras_id: p.node for p in parts if p.extras_id},
        source="synthetic.glb",
        bounds=((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        triangles=0,
        vertices=0,
    )


def _part(lw_id, translation, *, name_chain=None):
    from lockwire.scene import ScenePart

    return ScenePart(
        node=lw_id or "structure",
        extras_id=lw_id,
        name_chain=name_chain or ((lw_id,) if lw_id else ("structure",)),
        positions=(1.0000001, 2.0, 3.0),
        normals=None,
        indices=(0, 1, 2),
        color=(0.8, 0.8, 0.8, 1.0),
        translation=translation,
    )


def test_scene_payload_joins_by_extras_id_and_reports_gaps():
    contract = load_contract(CLEAN)
    scene = _scene(
        _part("dev.imu", (0.1, -0.04, 0.02)),
        _part(None, (0.0, 0.0, 0.0), name_chain=("robot_root", "screw_m3")),
        _part("dev.foreign.gizmo", (0.0, 0.0, 0.0)),
    )
    payload = build_payload(contract, scene=scene, scene_source="clean.glb (given)")
    scene_payload = payload["scene"]
    assert scene_payload["source"] == "clean.glb (given)"
    assert len(scene_payload["parts"]) == 3
    ids = {p["id"] for p in scene_payload["parts"]}
    assert ids == {"dev.imu", None, "dev.foreign.gizmo"}
    structure = next(p for p in scene_payload["parts"] if p["id"] is None)
    assert structure["state"] == "plain"
    assert scene_payload["scene_only"] == ["dev.foreign.gizmo"]
    assert "dev.radio.rfd900" in scene_payload["contract_only"]
    # A constant point survives quantization exactly through its local offset.
    positions = scene_payload["parts"][0]["positions"]
    assert positions["type"] == "i16"
    assert positions["offset"] == [1.0000001, 2.0, 3.0]
    assert positions["data"] == "AAAAAAAA"


def test_scene_part_states_follow_the_precedence_table():
    """fault > warn > affected > ok > unchecked > plain, from the page's own cards."""
    contract = load_contract(X500)
    # rail.pm02-5v carries a violation on this fixture (see
    # test_gui_writes_a_page_and_stays_out_of_the_verdict_business).
    scene = _scene(_part("rail.pm02-5v", (0.0, 0.0, 0.0)))
    payload = build_payload(contract, scene=scene, scene_source="x.glb (given)")
    part = payload["scene"]["parts"][0]
    assert part["id"] == "rail.pm02-5v"
    assert part["state"] == "fault"


def test_scene_only_parts_collapse_into_one_plain_card(tmp_path):
    """H1 (2026-09-06): the PX4 Vision SDF names 5 links (base_link, rotor_0..3)
    that `contract_demo.yaml` never declares. Before this fix each one leaked its
    own quantity-shaped card ("I couldn't check unclaimed") because `_shape` never
    blanked the id after "scene/" — they must collapse into one plain-word card
    naming every part, count 5.
    """
    from lockwire.scene_urdf import load_robot_scene

    sdf = """<?xml version="1.0"?>
<sdf version="1.6">
  <model name="px4vision">
    <link name="base_link">
      <visual name="v"><geometry><box><size>0.2 0.2 0.05</size></box></geometry></visual>
    </link>
    <link name="rotor_0">
      <pose>0.2 0.2 0 0 0 0</pose>
      <visual name="v"><geometry><box><size>0.02 0.02 0.01</size></box></geometry></visual>
    </link>
    <link name="rotor_1">
      <pose>-0.2 0.2 0 0 0 0</pose>
      <visual name="v"><geometry><box><size>0.02 0.02 0.01</size></box></geometry></visual>
    </link>
    <link name="rotor_2">
      <pose>-0.2 -0.2 0 0 0 0</pose>
      <visual name="v"><geometry><box><size>0.02 0.02 0.01</size></box></geometry></visual>
    </link>
    <link name="rotor_3">
      <pose>0.2 -0.2 0 0 0 0</pose>
      <visual name="v"><geometry><box><size>0.02 0.02 0.01</size></box></geometry></visual>
    </link>
  </model>
</sdf>
"""
    sdf_path = tmp_path / "model.sdf"
    sdf_path.write_text(sdf)
    scene = load_robot_scene(sdf_path)

    contract = load_contract(DEMO)
    payload = build_payload(contract, scene=scene, scene_source="model.sdf (from the SDF)")
    assert payload["scene"]["scene_only"] == [
        "base_link",
        "rotor_0",
        "rotor_1",
        "rotor_2",
        "rotor_3",
    ]
    scene_only_cards = [c for c in payload["cards"] if c["shape"] == "scene/*/unclaimed"]
    assert len(scene_only_cards) == 1, [c["title"] for c in payload["cards"]]
    card = scene_only_cards[0]
    assert card["count"] == 5
    assert "unclaimed" not in card["title"]
    assert card["title"] == "5 parts not declared"
    assert card["body"].startswith("base_link, rotor_0, rotor_1, rotor_2 and rotor_3: ")

    # X500's own scene has no scene-only ids: unchanged by this fix.
    from lockwire.scene import load_scene

    x500_payload = build_payload(load_contract(X500), scene=load_scene(X500.with_name("x500.glb")))
    assert not any(c["shape"] == "scene/*/unclaimed" for c in x500_payload["cards"])


def test_scene_placement_findings_land_in_the_page_cards():
    contract = load_contract(CLEAN)
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.026)))  # 6mm off
    payload = build_payload(contract, scene=scene, scene_source="clean.glb (given)")
    warnings = [c for c in payload["cards"] if c["severity"] == "warning"]
    assert any("dev.imu" in c["faults"] for c in warnings)
    assert payload["warnings"] >= 1


def test_scene_placement_blind_spots_stay_out_of_grey_and_a_rail_pass_reaches_its_consumers():
    """S3: undeclared placement alone must not grey a part, and a rail's pass
    must colour the devices that draw from it -- on the real X500 fixture+GLB."""
    from lockwire.scene import load_scene

    contract = load_contract(X500)
    scene = load_scene(X500.with_name("x500.glb"))
    payload = build_payload(contract, scene=scene, scene_source="x500.glb (given)")
    states = {p["id"]: p["state"] for p in payload["scene"]["parts"]}

    # rail.servo-motor's budget passes (246.68 W of 888.00 W) -- its four motors go green.
    for i in range(1, 5):
        assert states[f"dev.motor.m{i}"] == "ok", states[f"dev.motor.m{i}"]
    # ESCs have no power block at all: no rail to inherit, and their placement blind
    # spot must not grey them either -- nothing scored, nothing unchecked -> plain.
    for i in range(1, 5):
        assert states[f"dev.esc.esc{i}"] == "plain", states[f"dev.esc.esc{i}"]
    # dev.radio.rfd900x is dragged along by the overloaded rail.pm02-5v -- a casualty,
    # not a pass, so it stays affected even though placement no longer greys it.
    assert states["dev.radio.rfd900x"] == "affected"
    assert states["rail.pm02-5v"] == "fault"
    # Buses carry real not_scored bandwidth cards (not placement) -- still unchecked.
    for bus_id in states:
        if bus_id.startswith("bus."):
            assert states[bus_id] == "unchecked", (bus_id, states[bus_id])


# --------------------------------------------------------------------------- 3D view (T8)


def _find_chrome():
    chrome = Path(
        shutil.which("google-chrome")
        or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    if os.environ.get("CI") and not chrome.is_file():
        raise pytest.UsageError("CI requires installed headless Chrome (google-chrome on PATH)")
    return chrome


def test_chrome_discovery_uses_installed_linux_browser_and_mac_fallback(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/google-chrome")
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    assert _find_chrome() == Path("/usr/bin/google-chrome")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _find_chrome() == Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.UsageError, match="CI requires installed headless Chrome"):
        _find_chrome()


def _large_scene(vertices=70_000, *, copies=1):
    from lockwire.scene import Scene, ScenePart

    # Unshared triangle vertices, like the SDF/DAE importer. The last triangle
    # deliberately references the highest vertex, including the Uint32 boundary.
    positions = tuple(v for i in range(vertices) for v in ((i % 3 == 1), (i % 3 == 2), 0.0))
    indices = (*range(vertices - vertices % 3), 0, 1, vertices - 1)
    part = ScenePart(
        node="body",
        extras_id=None,
        name_chain=("body",),
        positions=positions,
        normals=(0.0, 0.0, 1.0) * vertices,
        indices=indices,
        color=(0.7, 0.7, 0.7, 1.0),
        translation=(0.0, 0.0, 0.0),
    )
    return Scene(
        parts=(part,) * copies,
        up="z",
        nodes_with_ids={},
        source="synthetic",
        bounds=((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        triangles=len(indices) // 3 * copies,
        vertices=vertices * copies,
    )


def test_scene_70000_vertices_pack_uint32():
    import base64
    import struct

    scene = _large_scene()
    payload = build_payload(load_contract(DEMO), scene=scene)["scene"]
    packed = payload["parts"][0]["indices"]
    assert packed["type"] == "u32"
    decoded = struct.unpack(f"<{len(scene.parts[0].indices)}I", base64.b64decode(packed["data"]))
    assert decoded == scene.parts[0].indices
    assert max(decoded) == 69_999


def test_scene_display_decimation_is_uniform_and_preserves_source():
    import base64
    import struct

    scene = _large_scene(450_003)
    before = scene.parts[0]
    payload = build_payload(load_contract(DEMO), scene=scene)["scene"]
    part = payload["parts"][0]
    kind = "I" if part["indices"]["type"] == "u32" else "H"
    raw = base64.b64decode(part["indices"]["data"])
    indices = struct.unpack(f"<{len(raw) // struct.calcsize(kind)}{kind}", raw)
    assert 0 < len(indices) // 3 <= 150_000
    assert payload["triangles"] == scene.triangles
    assert payload["vertices"] == scene.vertices
    assert payload["display_triangles"] == len(indices) // 3
    assert (
        f"model simplified for display: {len(indices) // 3} of {scene.triangles} triangles"
        in payload["honesty"]
    )
    # Compact unused vertices and normals together; every retained face keeps
    # its geometry and winding. Source data remains available to physical checks.
    assert len(base64.b64decode(part["positions"]["data"])) == len(indices) * 6
    assert len(base64.b64decode(part["normals"]["data"])) == len(indices) * 6
    assert scene.parts[0] is before
    assert scene.parts[0].indices == before.indices
    assert (
        "simplified"
        not in build_payload(load_contract(DEMO), scene=_large_scene())["scene"]["honesty"]
    )


@pytest.mark.parametrize("with_normals", [True, False])
def test_scene_decimation_keeps_uniform_faces_and_matching_attributes(with_normals):
    from dataclasses import replace

    from lockwire.scene_view import _display_part

    part = replace(
        _large_scene(9).parts[0],
        positions=tuple(float(i) for i in range(27)),
        normals=tuple(i / 27 for i in range(27)) if with_normals else None,
        indices=(0, 1, 2, 2, 3, 4, 4, 5, 6, 6, 7, 8),
    )
    sampled = _display_part(part, 2)
    assert sampled.indices == (0, 1, 2, 3, 4, 5)
    assert sampled.positions == part.positions[:9] + part.positions[12:21]
    assert sampled.normals == (part.normals[:9] + part.normals[12:21] if with_normals else None)
    assert sampled.node == part.node
    assert sampled.translation == part.translation
    assert sampled.color == part.color
    assert _display_part(part, 1) is part


# --------------------------------------------------------------------------- placement passes add up (S6)


def test_placement_passes_count_as_things_that_add_up():
    """A placement pass must land in `scored`/`adds_up`/the drawer, not just
    colour a part green — same identity as every other scored comparison."""
    contract = load_contract(CLEAN)
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.02)))  # exact match: a pass
    without_scene = build_payload(load_contract(CLEAN))
    with_scene = build_payload(contract, scene=scene, scene_source="clean.glb (given)")

    assert with_scene["scored"] == (without_scene["scored"] or 0) + 1
    assert len(with_scene["adds_up"]) == len(without_scene["adds_up"]) + 1
    assert any("sits where the contract says" in row["name"] for row in with_scene["adds_up"])
    placement_row = next(
        row for row in with_scene["adds_up"] if "sits where the contract says" in row["name"]
    )
    assert placement_row["name"].startswith("imu sits where the contract says")
    assert "0.0 mm" in placement_row["name"]

    accounted = len(with_scene["adds_up"]) + with_scene["violations"] + with_scene["warnings"]
    assert accounted == with_scene["scored"]


# --------------------------------------------------------------------------- window shell


def _command_modes() -> dict[str, str]:
    """The mode table the page ships (free commands, plus a plugin's when one is installed)."""
    return build_payload(load_contract(X500))["command_modes"]


def test_payload_carries_modes_and_faces():
    payload = build_payload(load_contract(X500))
    assert payload["modes"][:4] == ["design", "declare", "check", "plan"]
    assert payload["modes"][-1] == "reference"
    assert set(payload["faces"]) == {"design", "declare", "plan", "reference"}
    assert payload["faces"]["design"]["parts"], "X500 declares devices; the list must not be empty"


def _walk_cli_commands(command, prefix=()):
    """Recurse into every group (a typer sub-app, e.g. `scene`) and yield
    each leaf command's full path, e.g. `("scene", "import")`. A top-level
    leaf yields a length-1 path. Groups are duck-typed by `.commands` (a
    dict) rather than `isinstance(..., click.Group)` — typer vendors its own
    `Command`/`Group` classes (`typer._click.core`, `typer.core.TyperGroup`)
    that are not instances of the standalone `click` package's classes, so an
    isinstance check against `click.Group` silently never recurses. (E0-10 —
    The original walk was flat: `sorted(command.commands)` on the
    top-level group only, so it never saw `scene import`.)"""
    for name, sub in command.commands.items():
        path = prefix + (name,)
        if getattr(sub, "commands", None) is not None:
            yield from _walk_cli_commands(sub, path)
        else:
            yield path


# E0-10: `_COMMAND_MODES` stays keyed at the group level (`"scene": "design"`,
# unchanged) — a group's face covers everything under it by default. But a
# group's *specific* leaves must still be named somewhere, or a new
# subcommand slipping in unguarded (the blindness that let `scene import`
# through twice) goes uncaught. So each group also declares its known leaves
# here; an unlisted leaf under a known group is exactly as "missing" as an
# unlisted top-level command.
_KNOWN_SUBCOMMANDS = {
    "scene": {"import"},
}


def test_parity_every_cli_command_has_a_mode():
    """Walk `lockwire --help`'s command list, recursing into sub-apps
    (`scene import` etc.), and fail the day any command or subcommand has no
    face. Mutation bite (2026-09-07, reverted): deleting the `"scan": "run"`
    entry from `_COMMAND_MODES` made this test fail with `missing =
    ['scan']`, confirming it actually catches the drift it's named for.
    E0-10 mutation bite (2026-09-08, reverted): reverting this test's walker
    to the flat `names = sorted(command.commands)` made it pass while
    `scene import` stayed unguarded — proving the flat walk is blind to
    subcommands; the recursive walk below is what catches it."""
    import typer

    command = typer.main.get_command(app)
    paths = list(_walk_cli_commands(command))
    assert paths, "no commands found — CLI introspection broke"
    modes = _command_modes()

    missing = []
    for path in paths:
        top = path[0]
        if top not in modes:
            missing.append("/".join(path))
        elif len(path) > 1 and path[1] not in _KNOWN_SUBCOMMANDS.get(top, set()):
            missing.append("/".join(path))
    assert not missing, f"commands with no mode/face: {missing}"


def test_declare_groups_are_plain_names():
    form = build_declare_form(load_contract(X500))
    groups = {r["group"] for r in form["rows"]}
    assert not [g for g in groups if "." in g]
    assert "Companion Jetson Orin Nano · 15W" in groups and "15W" not in groups


def test_declare_value_names_are_plain():
    shorts = {r["short"] for r in build_declare_form(load_contract(X500))["rows"]}
    assert (
        not [s for s in shorts if s[0].islower()]
        and "Lowest voltage" in shorts
        and "Radio power" in shorts
    )
    assert not [s for s in shorts if any(w in s for w in ("range min", "tx power", "Structural"))]


def test_export_and_import_are_reference():
    """box eb-16: eb-01 added `export`/`import` to the CLI without a `_COMMAND_MODES`
    entry, which the parity test above caught after merge. Both write/refuse rather
    than owning a design/check/plan/run/health face, so both belong in Reference
    beside `report`/`describe`."""
    assert _command_modes()["export"] == "reference"
    assert _command_modes()["import"] == "reference"


def test_every_finding_keeps_its_exact_machine_text_on_the_page():
    """Plain words are an addition. Losing the precise wording would be a regression."""
    contract = load_contract(X500)
    payload = build_payload(contract)
    raw = "\n".join(card["raw"] for card in payload["cards"])
    for quantity in (
        "budgets/bud.power.pm02-5v",
        "model/structure_hash",
        "topology/buses/bus.i2c.external/bandwidth",
    ):
        assert quantity in raw, f"{quantity} vanished from the page"
    assert "35.20 W (draw mode: nominal)" in raw  # B10: radio's peak draw grows this total
    assert "shed load on rail" in raw


def test_the_adds_up_card_carries_the_limit_raised_mark(limit_raised_contract) -> None:
    """The standing mark reaches the card."""
    payload = build_payload(limit_raised_contract)
    assert [row["mark"] for row in payload["adds_up"]] == [LIMIT_RAISED_MARK_TEXT]


def test_x500_payload_carries_at_least_one_adds_up_row():
    payload = build_payload(load_contract(X500))
    assert payload["adds_up"], "x500 fixture is expected to have scored passes"
    row = payload["adds_up"][0]
    assert row["name"] and row["text"] and row["margin"]


def test_scene_labels_are_pinned_from_violation_cards():
    contract = load_contract(X500)
    scene = _scene(_part("rail.pm02-5v", (0.0, 0.0, 0.0)))
    payload = build_payload(contract, scene=scene, scene_source="x.glb (given)")
    labels = payload["scene"]["labels"]
    assert labels, "the overloaded rail must pin a label"
    assert all(label["severity"] in ("violation", "warning") for label in labels)
    assert any(label["part"] == "rail.pm02-5v" for label in labels)
    # The label's two magnitudes are typed records, not
    # the `"35.20 W vs <= 15.60 W"` string this used to assert on — a formatted
    # number carries no kind and nothing notices. the app formats them.
    assert labels[0]["observed"]["unit"] and labels[0]["expected"]["unit"]
    assert labels[0]["observed"]["kind"] and labels[0]["expected"]["kind"]
    assert isinstance(labels[0]["observed"]["value"], float)
    assert labels[0]["comparator"] in ("<=", ">=", "")


def _qarm_payload(tmp_path):
    from lockwire.start_operation import start_contract

    shutil.copy(FIXTURES / "qarm" / "QARM.urdf", tmp_path / "QARM.urdf")
    path = Path(start_contract(tmp_path / "QARM.urdf")["path"])
    return build_payload(load_contract(path), contract_path=path)


def test_payload_joints_are_in_one_robot_frame(tmp_path):
    page = _qarm_payload(tmp_path)
    by_name = {j["name"]: j for j in page["joints"]}
    revolute = [j["name"] for j in page["joints"] if j["type"] == "revolute"]
    assert revolute == ["YAW", "SHOULDER", "ELBOW", "WRIST"]
    # composed, not local: each joint sits where its parent chain puts it
    model = load_contract(next(tmp_path.glob("*.yaml"))).model
    local = {j.name: j.origin.xyz for j in model.joints}
    assert by_name["ELBOW"]["position"] != list(local["ELBOW"])
    assert {link["name"] for link in page["links"]} == {link.name for link in model.links}


def test_qarm_findings_target_the_joints_and_link_they_are_about(tmp_path):
    page = _qarm_payload(tmp_path)
    targets = [t for card in page["cards"] for t in card["targets"]]
    assert sorted(t["id"] for t in targets if t["kind"] == "joint") == [
        "ELBOW",
        "SHOULDER",
        "WRIST",
        "YAW",
    ]
    # world is a massless fixed root: folded into the frames line, not its own mass warning
    assert not [t for t in targets if t["kind"] == "link" and t["id"] == "world"]
    assert "Frames without mass" in [c.get("title") for c in page["cards"]]


# ------------------------------------------------------------- constraints


def test_x500_overloaded_rail_constraint_is_electrical_bad_and_names_its_parts():
    contract = load_contract(X500)
    from lockwire.scene import load_scene

    payload = build_payload(contract, scene=load_scene(X500.with_name("x500.glb")))
    row = next(c for c in payload["constraints"] if c["id"] == "budget.bud.power.pm02-5v")
    budget = next(b for b in payload["budgets"] if b["id"] == "bud.power.pm02-5v")
    assert (row["domain"], row["status"]) == ("electrical", "bad")
    assert (row["used"], row["limit"]) == (budget["value"], budget["limit"])  # the check's own
    assert row["margin_pct"] == round((15.6 - 35.2) / 15.6 * 100, 1)
    assert (row["used_kind"], row["limit_kind"]) == ("derived", "declared")
    assert {"dev.camera.oak-d-lite", "dev.companion.jetson-orin-nano", "dev.radio.rfd900x"} <= set(
        row["parts"]
    )
    ok = next(c for c in payload["constraints"] if c["id"] == "budget.bud.power.servo-motor")
    assert ok["status"] == "ok"


def test_qarm_joint_constraints_are_mechanical_and_unset(tmp_path):
    rows = [c for c in _qarm_payload(tmp_path)["constraints"] if c["id"].startswith("joint-")]
    assert {c["domain"] for c in rows} == {"mechanical"} and rows
    assert {c["status"] for c in rows} == {"unset"}
    assert {"joint-torque.YAW", "joint-speed.YAW", "joint-travel.YAW"} <= {c["id"] for c in rows}
    assert all(c["used"] is None and c["used_kind"] is None for c in rows)


def test_constraint_with_a_number_always_names_its_kind():
    payload = build_payload(load_contract(X500))
    for c in payload["constraints"]:
        assert (c["used"] is None) == (c["used_kind"] is None), c["id"]
        assert (c["limit"] is None) == (c["limit_kind"] is None), c["id"]


def test_preflight_finding_and_joint_target_name_their_parts():
    f = Finding(
        severity="violation",
        quantity="preflight/live/dev.imu/rate_hz",
        observed="1",
        expected="2",
        action="fix",
    )
    targets = _targets(load_contract(CLEAN), [f])
    assert ("device", "dev.imu") in {(t["kind"], t["id"]) for t in targets}
    assert "imu_link" in next(t for t in targets if t["kind"] == "joint")["parts"]


def test_step_targets_light_the_rail_and_its_consumers(tmp_path: Path):
    from lockwire.page import step_targets
    from lockwire.schema import load_contract

    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "steps:\n  - kind: rail_load\n    rail: rail.servo-motor\n    draw: {value: 5, unit: W}\n"
        "  - kind: rail_load\n    rail: rail.nope\n    draw: {value: 5, unit: W}\n"
    )
    first, unknown = step_targets(load_contract(X500), plan)
    assert first[0]["id"] == "rail.servo-motor" and len(first[0]["parts"]) > 1
    assert unknown == []


def test_reference_joints_use_plain_words():
    from lockwire.page import _reference_face
    from lockwire.schema import Contract, Joint, ModelRecord, Provenance, RobotInfo

    joints = [
        Joint(name="world_base_joint", type="fixed", parent="world", child="base_link"),
        Joint(name="yaw", type="revolute", parent="base_link", child="END-EFFECTOR"),
    ]
    model = ModelRecord(
        source="t.urdf",
        source_sha256="a",
        structure_hash="b",
        joints=joints,
        provenance=Provenance(witness="t", source="t.urdf", locked_at="2026-01-01"),
    )
    sheet = _reference_face(Contract(robot=RobotInfo(name="r"), model=model), None)["drawers"][0][
        "content"
    ]
    assert "| Yaw | Rotates | Base | End Effector |" in sheet and "World" not in sheet
    assert "revolute" not in sheet
    from lockwire.schema import DeviceInstance  # noqa: PLC0415

    dev = DeviceInstance(id="dev.controller-opencr-1-0", model="mdl.opencr-1-0", owner="you")
    face = _reference_face(Contract(robot=RobotInfo(name="anymal"), devices=[dev]), None)
    assert face["devices"][0]["name"] == "Controller OpenCR 1.0"
    assert "mdl." not in face["devices"][0]["text"]


def test_rail_current_limit_violation_is_not_called_a_lockfile_difference(tmp_path):
    out = tmp_path / "x.yaml"
    assert runner.invoke(app, ["start", "--example", "rover", "--out", str(out)]).exit_code == 0
    cards = build_payload(load_contract(out))["cards"]
    card = next(c for c in cards if "rails/rail.jetson-header-5v/current_limit" in c["raw"])
    assert card["title"] == "Jetson Header 5 V over its current limit"
