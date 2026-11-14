"""The GUI page: it must open on something, group honestly, and never soften a finding away."""

import os
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.page import human_name, _sentence, _targets, build_declare_form, build_payload, short_name
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
    # The radio joining the power tree adds a second real
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
    PX4 repro (2026-09-06 usage audit) measured an empty qualifier and zero cards,
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
    assert all(c["severity"] == "not_scored" for c in payload["cards"][1:])  # SDF note after
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
    # The radio joining the power tree adds a second real
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


def test_a_measured_warning_is_never_titled_as_a_check_that_could_not_run(clean_contract):
    obs, exp = "12.00 V nominal, +/-4% -> 12.48 V", "at most 12.00 V max input"
    q = "devices/d.s/power/rail"
    f = Finding(severity="warning", quantity=q, observed=obs, expected=exp, action="")
    title = _sentence(clean_contract, [f])[0]
    assert title == "1 part above its max input voltage (12.48 V vs 12.00 V)"


def test_a_pure_world_frame_is_neither_a_finding_nor_a_missing_weight(clean_contract):
    m, w = clean_contract.model, {"name": "w", "type": "fixed", "parent": "world"}
    m.links.append(m.links[0].model_copy(update={"name": "world", "mass": None}))
    m.joints.append(m.joints[0].model_copy(update=w))
    m.zero_mass_links = ["world"]
    assert "world" not in str((page := build_payload(clean_contract))["cards"] + page["setup"])


# ---------------------------------------------------------------------- the grouping


def test_identical_blind_spots_collapse_into_one_card():
    """Four unrated buses are one sentence, not four near-identical ones.

    The X500 model declared published-default bitrates on the X500
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
    """The anymal_d evidence's mass-budget violation was scored
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
    """On the anymal_d evidence, 32 of the model's links are
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
    """A mass_total budget sums the model's own YAML
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
    # The radio joining the power tree adds a second real violation.
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
    # Radio adds passing rows. Its link_budget now scores
    # a real pass too (bundled antenna gain published), adding an 8th row.
    # 2026-10-22: +6 passes, the Pixhawk's six IMU axes inside their datasheet full-scale limits.
    assert len(names) == 13, (
        "fixture assumption changed"
    )  # payload mass budget no longer a pass (Jetson, OAK-D, magnetometer masses unknown)
    for name in names:
        # units like m/s^2 and rad/s carry a slash; a quantity path does not end in one
        assert "/" not in name.replace("m/s^2", "").replace("rad/s", ""), (
            f"row title still carries a quantity path: {name!r}"
        )
        assert "bud." not in name, f"row title still names a budget id: {name!r}"

    joined = " | ".join(names)
    # The radio joining the power tree flips bud.power.pm02-5v.7w-mode
    # to a real violation, so it drops off this passing list; the new TELEM1 rail's
    # voltage-compatibility and rail-power comparisons take its place.
    assert "pixhawk-telem1-5v: 5.20 V of 5.20 V from rail.pm02-5v" in joined
    assert "pixhawk-telem1-5v rail: 5.20 W of 7.80 W" in joined
    assert "radio.rfd900x on the 5.20 V rail" in joined
    assert "Payload mass" not in joined  # partial mass sum is no longer a pass
    # Durations under an hour print in minutes, not a fraction of an hour.
    assert "Flight time on the declared pack: 15.8 min, 15.0 min required" in joined
    assert "servo-motor rail" in joined and "246.68 W of 888.00 W" in joined
    assert "jetson-orin-nano on the 5.20 V rail" in joined
    # Antenna gain published (RFDesign bundle page), so the
    # link budget now scores a real pass with large headroom.
    assert "radio.rfd900x: 41.30 dB fade margin of 10.00 dB required" in joined


# --------------------------------------------------------------------------- scene payload


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


def test_scene_display_decimation_is_uniform_and_preserves_source(monkeypatch):
    import base64
    import struct

    monkeypatch.setattr("lockwire.scene_view._DISPLAY_TRIANGLES", 150_000)
    monkeypatch.setattr("lockwire.catalog._CACHE", {})

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


def _is_waitlist_stub(sub):
    """cli.py's hidden `_pro_stub` for a Pro name when Pro is absent; a real one is still checked."""
    return bool(sub.hidden and "_pro_stub" in getattr(sub.callback, "__qualname__", ""))


def _walk_cli_commands(command, prefix=()):
    """Recurse into every group (a typer sub-app, e.g. `scene`) and yield
    each leaf command's full path, e.g. `("scene", "import")`. A top-level
    leaf yields a length-1 path. Groups are duck-typed by `.commands` (a
    dict) rather than `isinstance(..., click.Group)` — typer vendors its own
    `Command`/`Group` classes (`typer._click.core`, `typer.core.TyperGroup`)
    that are not instances of the standalone `click` package's classes, so an
    isinstance check against `click.Group` silently never recurses. (
    The original walk was flat: `sorted(command.commands)` on the
    top-level group only, so it never saw `scene import`.)"""
    for name, sub in command.commands.items():
        path = prefix + (name,)
        if _is_waitlist_stub(sub):
            continue  # free package only: a hidden Pro name that answers with the waitlist
        if getattr(sub, "commands", None) is not None:
            yield from _walk_cli_commands(sub, path)
        else:
            yield path


# `_COMMAND_MODES` stays keyed at the group level (`"scene": "design"`,
# unchanged) — a group's face covers everything under it by default. But a
# group's *specific* leaves must still be named somewhere, or a new
# subcommand slipping in unguarded (the blindness that let `scene import`
# through twice) goes uncaught. So each group also declares its known leaves
# here; an unlisted leaf under a known group is exactly as "missing" as an
# unlisted top-level command.
_KNOWN_SUBCOMMANDS = {
    "scene": {"import"},
    "catalog": {"robots", "parts"},
    "materials": {"compare"},
    "bag": {"inspect", "check"},
    "release": {"tag", "record", "show"},
}


def test_parity_every_cli_command_has_a_mode():
    """Walk `lockwire --help`'s command list, recursing into sub-apps
    (`scene import` etc.), and fail the day any command or subcommand has no
    face. Mutation bite (2026-09-07, reverted): deleting the `"scan": "run"`
    entry from `_COMMAND_MODES` made this test fail with `missing =
    ['scan']`, confirming it actually catches the drift it's named for.
    Mutation bite (2026-09-08, reverted): reverting this test's walker
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
    """The CLI once added `export`/`import` to the CLI without a `_COMMAND_MODES`
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
    assert "35.20 W (draw mode: nominal)" in raw  # Radio's peak draw grows this total
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


def test_payload_gives_the_position_move_sets_in_mm(tmp_path):
    from lockwire.schema import Physical, Quantity

    page = _qarm_payload(tmp_path)
    model = load_contract(next(tmp_path.glob("*.yaml"))).model
    local = {j.name: j.origin.xyz for j in model.joints}
    for joint in page["joints"]:  # the joint's own origin in its parent, not the composed position
        assert joint["origin_mm"] == pytest.approx([v * 1000 for v in local[joint["name"]]])
    contract = _example("servo-bench.yaml")
    device = contract.devices[0]
    device.physical = (device.physical or Physical()).model_copy(update={"mount_frame": "base", "mount_xyz": tuple(Quantity(value=v, unit="cm") for v in (1, 2, 3))})
    shown = {d["id"]: d["mount_mm"] for d in build_payload(contract)["faces"]["reference"]["devices"]}
    assert shown[device.id] == pytest.approx([10, 20, 30])  # cm to mm
    assert shown[contract.devices[1].id] is None  # nothing declared, nothing invented


def test_payload_lists_link_solids_with_named_sizes_resolved_and_the_ignore_list(tmp_path):
    from lockwire.schema import CollisionPair, Param, Shape

    page = _qarm_payload(tmp_path)
    contract = load_contract(next(tmp_path.glob("*.yaml")))
    first, second = contract.model.links[0], contract.model.links[1]
    first.geometry = [Shape(kind="box", size=(0.1, "w", 0.1)), Shape(kind="sphere", radius="w")]
    first.collision = [Shape(kind="sphere", radius=0.5), Shape(kind="cylinder", radius="w", length=1.0)]
    second.geometry = [Shape(kind="box", size=(0.1, "w", 0.1))]
    contract.params = [Param(name="w", value=20.0, unit="mm")]
    contract.collision_pairs = [CollisionPair(link1=first.name, link2=second.name, reason="bolted")]
    page = build_payload(contract)
    by_name = {link["name"]: link["solids"] for link in page["links"]}
    assert [s["kind"] for s in by_name[first.name]] == ["sphere", "cylinder"]  # collision wins over geometry
    assert by_name[second.name][0]["size"] == [0.1, 0.02, 0.1]  # 20 mm resolved to metres
    assert page["collision_pairs"] == [{"link1": first.name, "link2": second.name, "reason": "bolted"}]


def test_qarm_findings_target_the_joints_and_link_they_are_about(tmp_path):
    page = _qarm_payload(tmp_path)
    targets = [t for card in page["cards"] for t in card["targets"]]
    # QARM.urdf states velocity="0" on all four joints: one effort card names them all,
    # and each joint gets its own velocity card
    assert (
        sorted(t["id"] for t in targets if t["kind"] == "joint")
        == ["ELBOW"] * 2 + ["SHOULDER"] * 2 + ["WRIST"] * 2 + ["YAW"] * 2
    )
    velocity = [c for c in page["cards"] if c["shape"].endswith("/velocity")]
    assert sorted(c["targets"][0]["id"] for c in velocity) == ["ELBOW", "SHOULDER", "WRIST", "YAW"]
    # world is a massless fixed root: no body at all, so neither a mass warning nor a frames line
    assert not [t for t in targets if t["kind"] == "link" and t["id"] == "world"]
    assert "Frames without mass" not in [c.get("title") for c in page["cards"]]


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
    from lockwire.schema import Contract, Joint, Link, ModelRecord, Provenance, RobotInfo

    joints = [
        Joint(name="world_base_joint", type="fixed", parent="world", child="base_link"),
        Joint(name="yaw", type="revolute", parent="base_link", child="END-EFFECTOR"),
    ]
    model = ModelRecord(
        source="t.urdf",
        source_sha256="a",
        structure_hash="b",
        # Ghost-link refusal: every joint end must be a declared link.
        links=[Link(name=n) for n in ("world", "base_link", "END-EFFECTOR")],
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
    out.write_text(
        out.read_text().replace("{value: 5, unit: A}", "{value: 2.5, unit: A}")
    )  # one pin
    cards = build_payload(load_contract(out))["cards"]
    card = next(c for c in cards if "rails/rail.jetson-header-5v/current_limit" in c["raw"])
    assert card["title"] == "Jetson Header 5 V over its current limit"


def test_weak_servo_card_names_the_torque_gap_not_an_unmapped_drive():
    from lockwire.page import _cards, finding_label

    def weak(joint: str) -> Finding:
        return Finding(
            severity="violation",
            quantity=f"devices/dev.servo.{joint}/drives_joint",
            observed="1.91 N*m continuous geared torque",
            expected=">= 10.00 N*m joint effort limit",
            action="select a stronger actuator",
        )

    findings = [weak("elbow_flex"), weak("gripper")]
    cards = _cards(load_contract(CLEAN), findings)
    assert [c["title"] for c in cards] == [
        "Elbow Flex: servo 1.91 N·m, joint limit 10 N·m",
        "Gripper: servo 1.91 N·m, joint limit 10 N·m",
    ]
    assert cards[0]["group_title"] == "{n} servos can't reach their joint limit"
    assert finding_label(findings[0]) == "Servo too weak"
    assert "gives 1.91 N·m continuously" in cards[0]["body"]
    peak = weak("elbow_flex").model_copy(update={"observed": "1.91 N*m peak (stall) geared torque"})
    assert "gives at most 1.91 N·m even at stall" in _cards(load_contract(CLEAN), [peak])[0]["body"]
    # a real bad joint reference keeps its own wording
    bad_ref = Finding(
        severity="violation",
        quantity="devices/dev.servo.x/drives_joint",
        observed="'nope'",
        expected="a joint name in model/joints",
        action="fix the driven-joint reference",
    )
    assert _cards(load_contract(CLEAN), [bad_ref])[0]["group_title"] == "{n} drives unmapped"


def test_page_payload_carries_accepted_findings():
    """An accepted finding reaches the app: hidden from the report is the failure."""
    from lockwire.checks import run_record_checks
    from lockwire.schema import AcceptedFinding, Provenance

    contract = load_contract(X500)
    f = next(f for f in run_record_checks(contract) if f.severity == "violation")
    prov = Provenance(witness="sam", source="bench", locked_at="2026-11-06")
    contract.accepted = [
        AcceptedFinding(id=f.quantity, observed=f.observed, reason="on purpose", provenance=prov)
    ]
    row = build_payload(contract)["accepted"][0]
    assert (row["reason"], row["witness"], row["date"]) == ("on purpose", "sam", "2026-11-06")


def test_accepted_rows_name_their_device_when_titles_collide():
    """Six humanoid servos share one finding title; each accepted row names its own device."""
    path = Path(__file__).parents[1] / "src/lockwire/examples/humanoid/contract.yaml"
    rows = build_payload(load_contract(path), contract_path=path)["accepted"]
    titles = [r["title"] for r in rows]
    assert len(set(titles)) == len(titles)
    assert titles[0].startswith("dev.servo.left-hip-yaw: above its max input voltage")


def test_mass_vs_size_card_names_the_part_once_in_plain_words():
    pack1 = Path(__file__).parent / "fixtures" / "pack1" / "pack1.urdf"
    cards = build_payload(Contract(robot=RobotInfo(name="p"), model=parse_urdf(pack1)))["cards"]
    card = next(c for c in cards if c["shape"] == "model/links/*/mass_vs_size")
    assert (
        card["title"] == "Mass vs size: Heavy, Offset and Spin"
    )  # once, not "Mass vs size: Mass Vs Size"
    assert card["fix"] == "Check the units of Heavy's mass and shape, then lock it again"


def test_model_changed_says_whether_the_model_drifted_from_its_lock(tmp_path):
    # The app shows "Update from model file" only when this is true.
    urdf = tmp_path / "r.urdf"
    shutil.copy(Path(__file__).parent / "fixtures" / "pack1" / "pack1.urdf", urdf)
    contract = tmp_path / "lockwire.yaml"
    assert CliRunner().invoke(app, ["start", str(urdf), "--out", str(contract)]).exit_code == 0
    assert build_payload(load_contract(contract), contract_path=contract)["model_changed"] is False
    urdf.write_text(urdf.read_text().replace('<mass value="', '<mass value="9', 1))
    assert build_payload(load_contract(contract), contract_path=contract)["model_changed"] is True


def test_lockfile_servo_line_flags_the_joint_and_lights_its_link():
    model = parse_urdf(Path(__file__).parent / "fixtures" / "servo_arm.urdf")
    contract = Contract(robot=RobotInfo(name="arm"), model=model, servo="feetech.sts3215")
    cards = build_payload(contract)["cards"]
    card = next(c for c in cards if c["shape"] == "model/joints/*/limits/effort")
    assert card["severity"] == "violation" and "upper" in card["targets"][0]["parts"]
    assert card["title"] == "Shoulder above the servo's stall torque"  # not "differs from lockfile"


def test_servo_stall_shape_survives_a_joint_name_with_a_slash(tmp_path):
    urdf = tmp_path / "arm.urdf"
    src = (Path(__file__).parent / "fixtures" / "servo_arm.urdf").read_text()
    urdf.write_text(src.replace('name="shoulder"', 'name="left/shoulder"'))
    contract = Contract(
        robot=RobotInfo(name="arm"), model=parse_urdf(urdf), servo="feetech.sts3215"
    )
    card = next(c for c in build_payload(contract)["cards"] if "stall" in c["title"])
    assert card["shape"] == "model/joints/*/limits/effort"


def test_lockfile_servo_per_joint_pattern_compares_only_matching_joints():
    model = parse_urdf(Path(__file__).parent / "fixtures" / "servo_arm.urdf")
    contract = Contract(robot=RobotInfo(name="arm"), model=model, servo={"wr*": "feetech.sts3215"})
    shapes = [c["shape"] for c in build_payload(contract)["cards"]]
    assert "model/joints/*/limits/effort" not in shapes  # shoulder (10 N*m) is not an STS3215


def _servo_bench_with(**rail_update):
    import lockwire

    c = load_contract(Path(lockwire.__file__).parent / "examples" / "servo-bench.yaml")
    rails = [r.model_copy(update=rail_update) for r in c.topology.rails]
    return c.model_copy(update={"topology": c.topology.model_copy(update={"rails": rails})})


def test_power_stage_watts_lost_is_input_minus_output():
    [stage] = build_payload(_servo_bench_with(upstream="battery", efficiency=0.8))["power_stages"]
    assert stage["watts_out"] == pytest.approx(32.1)
    assert stage["watts_in"] == pytest.approx(32.1 / 0.8)
    assert stage["watts_lost"] == pytest.approx(32.1 / 0.8 - 32.1)
    assert stage["amps_out"] == pytest.approx(32.1 / 5)
    assert sum(x["watts"] for x in stage["loads"]) == pytest.approx(32.1)


def test_power_stage_without_upstream_loses_nothing():
    [stage] = build_payload(_servo_bench_with(efficiency=0.8))["power_stages"]
    assert stage["watts_lost"] == 0


def test_power_stages_empty_without_rails(clean_contract):
    c = clean_contract.model_copy(update={"topology": None})
    assert build_payload(c)["power_stages"] == []


def test_payload_joint_carries_its_own_rpy_in_radians(tmp_path):
    page = _qarm_payload(tmp_path)
    model = load_contract(next(tmp_path.glob("*.yaml"))).model
    own = {j.name: list(j.origin.rpy) for j in model.joints}
    rolled = [j["name"] for j in page["joints"] if any(abs(v) > 1.5 for v in j["rpy"])]
    assert rolled and all(j["rpy"] == own[j["name"]] for j in page["joints"])


# --------------------------------------------------------------------------- Tip-over headroom and polygon


def _example(name):
    from importlib.resources import files

    return load_contract(Path(str(files("lockwire") / "examples" / name)))


def test_headroom_is_available_over_needed_and_the_wrist_is_the_tightest():
    joints = {j["name"]: j["headroom"] for j in build_payload(_example("servo-bench.yaml"))["joints"]}
    wrist = joints["wrist"]
    assert wrist["needed_nm"] == pytest.approx(0.15009, abs=1e-4)  # hand 0.09 kg at 0.17 m
    assert wrist["available_nm"] == pytest.approx(0.2263, abs=1e-3)  # the SCS0009 stall, not the 0.4 declared
    assert wrist["ratio"] == pytest.approx(wrist["available_nm"] / wrist["needed_nm"])
    # ratio, not absolute torque: the shoulder needs the most torque, the wrist has the least spare
    assert min(joints, key=lambda n: joints[n]["ratio"]) == "wrist"
    assert max(joints, key=lambda n: joints[n]["needed_nm"]) == "shoulder"


def test_headroom_is_left_out_for_a_joint_with_no_torque_data():
    contract = _example("servo-bench.yaml")
    contract.model.joints[2].limits.effort = None
    wrist = next(j for j in build_payload(contract)["joints"] if j["name"] == "wrist")
    assert wrist["headroom"] is None


def test_support_polygon_only_when_the_engine_has_one():
    assert build_payload(_example("servo-bench.yaml"))["support_polygon"] is None
    rover = build_payload(_example("rover/contract.yaml"))["support_polygon"]
    assert len(rover["points"]) == 3


# --------------------------------------------------------------------------- Tool-tip readout


def test_joint_carries_the_tool_tip_and_its_own_jacobian_column():
    joints = {j["name"]: j["tip"] for j in build_payload(_example("servo-bench.yaml"))["joints"]}
    assert joints["shoulder"]["xyz"] == pytest.approx([0.4, 0.0, 0.05])  # the hand, not the base at the origin
    assert joints["shoulder"]["per_unit"] == pytest.approx([0.0, 0.0, -0.4])  # the arm's 0.4 m reach
    assert joints["elbow"]["per_unit"] == pytest.approx([0.0, 0.0, -0.2])  # a shorter lever
    assert joints["wrist"]["per_unit"] == pytest.approx([0.0, 0.0, 0.0])  # the tip sits on the wrist


def test_no_tip_readout_when_the_engine_has_no_tip():
    contract = _example("servo-bench.yaml")
    contract.model.joints[1].axis = None  # a movable joint with no axis: the engine returns None
    assert all(j["tip"] is None for j in build_payload(contract)["joints"])


def test_free_page_has_no_soc_chart_even_with_a_battery_and_a_plan(clean_contract, tmp_path, monkeypatch):
    from lockwire import plugins

    monkeypatch.setattr(plugins, "PAGE_EXTRAS", [])
    plan = tmp_path / "plan.yaml"
    plan.write_text("segments:\n  - duration_s: 60\n    device_modes: {}\n")
    payload = build_payload(clean_contract, plan_path=plan)
    assert "battery_chart" not in payload


# ------------------------------------------------ motion preview series


def _pendulum_contract(**limits):
    from types import SimpleNamespace

    from test_statics import chain1

    return SimpleNamespace(model=chain1(com=(0, 0, -1.0), kg=1.0, limits=limits))


def test_motion_preview_series_are_the_move_and_the_limits_ride_along():
    import math

    from lockwire.page import motion_preview
    from lockwire.plan import Plan

    plan = Plan(segments=[{"pose": {"j": 0.0}, "duration_s": 5}, {"pose": {"j": 0.3}, "duration_s": 5}])
    out = motion_preview(_pendulum_contract(velocity=1.0, acceleration=10.0), plan)
    (joint,) = out["joints"]
    assert (max(joint["velocity"]), max(joint["acceleration"])) == pytest.approx((1.0, 10.0))
    assert out["time_s"][-1] == pytest.approx(0.4)  # d/v + v/a
    ramp_up = 1.0 * 10.0 + 9.81 * math.sin(0.5 * 10.0 * 0.1**2)  # m l^2 qdd + m g l sin q
    assert max(joint["torque"]) == pytest.approx(ramp_up, abs=1e-6)
    assert (joint["velocity_limit"], joint["acceleration_limit"], joint["torque_limit"]) == (1.0, 10.0, 9.0)


def test_motion_preview_is_nothing_without_a_speed_limit_or_a_move():
    from lockwire.page import motion_preview
    from lockwire.plan import Plan

    move = Plan(segments=[{"pose": {"j": 0.0}}, {"pose": {"j": 0.3}}])
    assert motion_preview(_pendulum_contract(), move) is None
    assert motion_preview(_pendulum_contract(velocity=1.0, acceleration=10.0), Plan(segments=[{"pose": {"j": 0.0}}])) is None

# --------------------------------------------------------------------------- PNL-CTL controls panel data

MOTOR = {"torque_constant": 0.1, "terminal_resistance_ohm": 0.5, "no_load_speed_rad_s": 300.0}


def _controls_page(**kw):
    from test_controls import shoulder

    return build_payload(shoulder(**kw))


def test_controls_motor_curves_have_torque_on_x_speed_and_efficiency_in_their_units():
    [card] = _controls_page(**MOTOR)["controls_motors"]
    tq, sp, ef = card["torque_nm"], card["speed_rad_s"], card["efficiency_pct"]
    assert (tq[0], sp[0], ef[0]) == (0.0, pytest.approx(300.0), 0.0)  # no load: no torque, full speed
    assert tq[-1] == pytest.approx(0.1 * 60.0)  # stall torque = Kt * V / R, V = Kt * w0 = 30 V
    assert sp[-1] == pytest.approx(0.0, abs=1e-9) and ef[-1] == pytest.approx(0.0, abs=1e-9)
    k = next(i for i, t in enumerate(tq) if t == pytest.approx(3.0))  # i = 30 A: w = 150 rad/s
    assert (sp[k], ef[k]) == (pytest.approx(150.0), pytest.approx(50.0))
    assert {"Torque constant", "No-load speed"} <= {f["label"] for f in card["facts"]}


def test_controls_joint_step_is_position_over_seconds_with_its_readouts():
    from test_controls import J_TOTAL

    [joint] = _controls_page()["controls_joints"]
    facts = {f["label"]: f for f in joint["facts"]}
    assert joint["t_s"][0] == 0.0 and joint["position"][0] == 0.0
    assert max(joint["position"]) == pytest.approx(1 + facts["Overshoot"]["value"] / 100, rel=1e-3)
    assert joint["position"][-1] == pytest.approx(1.0, abs=0.02)  # the shown window ends settled
    assert (facts["Phase margin"]["unit"], facts["Rise time"]["unit"]) == ("deg", "s")
    assert J_TOTAL > 0 and joint["label"] == "Shoulder"


def test_controls_empty_when_the_robot_has_no_controller_or_motor_constants(clean_contract):
    page = build_payload(clean_contract)
    assert page["controls_joints"] == [] and all(not m["efficiency_pct"] for m in page["controls_motors"])
    assert _controls_page(gains=False)["controls_joints"] == []


def test_controls_advice_with_no_source_says_so():
    [joint] = _controls_page(loop=1.0)["controls_joints"]  # crossover far above loop_rate/10
    [line] = [a for a in joint["advice"] if "crossover" in a.lower()]
    assert line.startswith("Crossover")
    assert "Advice: not yet sourced." in line
    assert _controls_page()["controls_joints"][0]["advice"] == []


# ---- requirements in the page and the report (Pro only) and the typed limit bar

_PLAN_WITH_REQ = "steps: []\nrequirements:\n  - id: no.such.check\n    text: Carry a 2 kg payload\n"


def _plan_file(tmp_path):
    path = tmp_path / "plan.yaml"
    path.write_text(_PLAN_WITH_REQ, encoding="utf-8")
    return path


def test_requirements_are_empty_in_the_free_page_and_report(clean_contract, tmp_path, monkeypatch):
    from lockwire import plugins
    from lockwire.plan import load_plan
    from lockwire.report_html import render_html

    plugins.ensure()
    monkeypatch.delitem(plugins.OVERRIDES, "requirement_rows", raising=False)
    plan_path = _plan_file(tmp_path)
    assert build_payload(clean_contract, plan_path=plan_path)["requirements"] == []
    assert "Carry a 2 kg payload" not in render_html(clean_contract, plan=load_plan(plan_path))


def test_a_broken_plan_file_adds_no_requirement_rows(clean_contract, tmp_path):
    bad = tmp_path / "plan.yaml"
    bad.write_text("steps: [", encoding="utf-8")
    from lockwire.page import _requirements

    assert _requirements(bad, [], []) == []


def test_the_weak_servo_card_carries_typed_torque_figures_for_the_limit_bar():
    from lockwire.page import _cards
    from lockwire.schema import Quantity

    weak = Finding(
        severity="violation",
        quantity="devices/dev.servo.wrist/drives_joint",
        observed="0.23 N*m continuous geared torque",
        expected=">= 0.40 N*m joint effort limit",
        observed_quantity=Quantity(value=0.2263, unit="N*m"),
        expected_quantity=Quantity(value=0.4, unit="N*m"),
        action="select a stronger actuator",
    )
    [card] = _cards(load_contract(CLEAN), [weak])
    assert (card["observed"]["value"], card["observed"]["unit"]) == (0.2263, "N*m")
    assert card["expected"]["value"] == 0.4


def test_requirement_rows_use_the_plan_beside_the_lockfile(tmp_path, monkeypatch):
    from lockwire import plugins

    lock = tmp_path / "bot.yaml"
    shutil.copy(CLEAN, lock)
    (tmp_path / "bot.plan.yaml").write_text(_PLAN_WITH_REQ, encoding="utf-8")
    plugins.ensure()
    pro = "requirement_rows" in plugins.OVERRIDES
    rows = build_payload(load_contract(lock), contract_path=lock)["requirements"]
    assert [r["status"] for r in rows] == (["unproven"] if pro else [])
    monkeypatch.delitem(plugins.OVERRIDES, "requirement_rows", raising=False)
    assert build_payload(load_contract(lock), contract_path=lock)["requirements"] == []


def test_requirement_typed_torque_figures_reach_the_weak_servo_card():
    test_the_weak_servo_card_carries_typed_torque_figures_for_the_limit_bar()


def _wired(*wires):
    from lockwire.schema import Wire

    c = _servo_bench_with()
    ws = [Wire.model_validate(w) for w in wires]
    return c.model_copy(
        update={"topology": c.topology.model_copy(update={"wires": [*c.topology.wires, *ws]})}
    )


_PIN_WIRE = {"awg": 22, "installation": "free_air", "owner": "you"}


def test_wiring_one_entry_per_pin_wire_and_only_the_rail_wire_carries_a_rail():
    page = build_payload(
        _wired(
            {
                "id": "wire.feed",
                "rail": "rail.servo-5v",
                "from": {"device": "dev.a", "connector": "J1", "pin": 1},
                "to": {"device": "dev.b", "pin": "A3"},
                **_PIN_WIRE,
            },
            {
                "id": "wire.tx",
                "from": {"device": "dev.a", "connector": "J1", "pin": 2},
                "to": {"device": "dev.b", "pin": "A4"},
                "net": "uart",
                **_PIN_WIRE,
            },
        )
    )
    by = {w["id"]: w for w in page["wiring_wires"]}
    assert by["wire.feed"]["rail"] == "rail.servo-5v" and by["wire.tx"]["rail"] is None
    assert by["wire.feed"]["awg"] == _PIN_WIRE["awg"]  # the declared gauge rides the payload
    assert by["wire.feed"]["from_pin"] == "dev.a|J1|1" and by["wire.tx"]["net"] == "uart"
    assert sorted(d["name"] for d in page["wiring_devices"]) == ["A", "B"]
    assert [
        p["label"]
        for d in page["wiring_devices"]
        if d["id"] == "dev.a"
        for c in d["connectors"]
        for p in c["pins"]
    ] == ["1", "2"]


def test_wiring_is_empty_without_pin_to_pin_wires():
    page = build_payload(_servo_bench_with())  # its only wire is a bare rail feed
    assert page["wiring_devices"] == [] and page["wiring_wires"] == []


def test_wiring_lists_every_pin_before_the_first_wire_exists():
    from lockwire.schema import Connector, Pin

    base = _servo_bench_with()
    pins = [Pin(number=n) for n in range(1, 11)]  # over 8 pins, none wired
    devices = [
        d.model_copy(update={"connectors": [Connector(id="J1", type="2x5", pins=pins)]}) if i == 0 else d
        for i, d in enumerate(base.devices)
    ]
    page = build_payload(base.model_copy(update={"devices": devices}))
    [dev] = page["wiring_devices"]
    assert len(dev["connectors"][0]["pins"]) == 10 and page["wiring_wires"] == []


def test_wiring_pin_card_carries_every_declared_pin_with_its_names():
    from lockwire.schema import Connector, Pin

    pins = [Pin(number=10, name="GPIO15", aliases=["BCM15"], functions=["RXD"], os_device="/dev/ttyAMA0")]
    pins += [Pin(number=n) for n in range(1, 10)]  # over 8 pins: the diagram shows only wired ones
    base = _servo_bench_with()
    dev = base.devices[0].id
    c = _wired(
        {
            "id": "wire.tx",
            "from": {"device": dev, "connector": "J1", "pin": 10},
            "to": {"device": "dev.b", "pin": "A4"},
            **_PIN_WIRE,
        }
    )
    devices = [
        d.model_copy(update={"connectors": [Connector(id="J1", type="2x20", pins=pins)]})
        if d.id == dev
        else d
        for d in c.devices
    ]
    page = build_payload(c.model_copy(update={"devices": devices}))
    conn = next(d for d in page["wiring_devices"] if d["id"] == dev)["connectors"][0]
    assert [p["label"] for p in conn["pins"]] == ["10 GPIO15"]
    assert [p["number"] for p in conn["card"]] == ["10", *map(str, range(1, 10))]
    assert conn["card"][0] == {
        "number": "10",
        "name": "GPIO15",
        "aliases": ["BCM15"],
        "functions": ["RXD"],
        "capabilities": [],
        "os_device": "/dev/ttyAMA0",
    }
    assert conn["type"] == "2x20"


def _wiring_html():
    from lockwire.report_html import render_html

    return render_html(
        _wired(
            {
                "id": "wire.feed",
                "from": {"device": "dev.a", "connector": "J1", "pin": 1},
                "to": {"device": "dev.b", "pin": "A3"},
                "length_m": 0.25,
                "colour": "red",
                **_PIN_WIRE,
            },
            {
                "id": "wire.tx",
                "from": {"device": "dev.a", "connector": "J1", "pin": 2},
                "to": {"device": "dev.b", "pin": "A4"},
                "net": "uart",
                **_PIN_WIRE,
            },
        )
    )


def test_wiring_report_lists_every_wire_with_ends_spec_and_length():
    html = _wiring_html()
    assert 'class="harness"' in html and html.count("<tr data-from=") == 2
    assert "<td>Wire Feed</td><td>A J1 1</td><td>B A3</td><td>22 AWG, RD</td><td>0.25 m</td></tr>" in html
    assert "<td>Wire Tx</td><td>A J1 2</td><td>B A4</td><td>22 AWG</td><td></td></tr>" in html


def test_wiring_report_diagram_has_clickable_pins_and_print_rules():
    html = _wiring_html()
    assert html.count('class="wpin"') == 4 and 'data-net="uart"' in html
    assert "@media print" in html and "break-before:page" in html


def test_wiring_report_emits_nothing_for_a_robot_without_wires():
    from lockwire.report_html import render_html

    html = render_html(_servo_bench_with())
    assert 'id="wiring"' not in html and 'class="harness"' not in html


def _example(name):
    import lockwire

    return load_contract(Path(lockwire.__file__).parent / "examples" / name)


def test_power_columns_keep_light_typical_and_worst_case_apart():
    cols = {c["rail"]: c for c in build_payload(_example("rover/contract.yaml"))["power_columns"]}
    ctl = cols["rail.controller-5v"]
    assert (ctl["light_w"], ctl["typical_w"]) == (pytest.approx(17.45), pytest.approx(17.45))
    assert ctl["max_w"] == pytest.approx(18.25)  # the worst-case mode, not a copy of typical


def test_power_stress_rows_carry_percent_of_rating_and_the_unsourced_flag():
    from test_stress import regulator

    rows = build_payload(regulator(load_w=13.5))["power_stress"]
    amps = next(r for r in rows if r["quantity"].startswith("output current"))
    assert amps["pct"] == pytest.approx(90) and amps["over_advice"] is True
    assert amps["quantity"] == f"output current ({human_name('r5')})"  # never the raw rail id
    assert build_payload(regulator(load_w=1))["power_stress"][0]["over_advice"] is False


def test_power_pack_usable_energy_takes_the_reserve_off_once():
    c = _example("drone/contract.yaml")
    battery = c.topology.battery.model_copy(update={"reserve_pct": 20.0})
    c = c.model_copy(update={"topology": c.topology.model_copy(update={"battery": battery})})
    pack = build_payload(c)["power_pack"]
    assert pack["capacity_wh"] == pytest.approx(74.0) and pack["usable_wh"] == pytest.approx(59.2)
    assert build_payload(c.model_copy(update={"topology": None}))["power_pack"] is None


def test_power_typical_follows_a_sibling_plan_and_says_so(tmp_path):
    import lockwire

    src = Path(lockwire.__file__).parent / "examples" / "servo-bench.yaml"
    lock = tmp_path / "bench.yaml"
    shutil.copy(src, lock)
    [plain] = build_payload(load_contract(lock), contract_path=lock)["power_columns"]
    assert plain["typical_from_plan"] is False
    (tmp_path / "bench.plan.yaml").write_text(
        "segments:\n  - duration_s: 10\n    device_modes: {dev.servo1: 0.5}\n"
    )
    [col] = build_payload(load_contract(lock), contract_path=lock)["power_columns"]
    assert col["typical_from_plan"] is True
    assert col["typical_w"] == pytest.approx(plain["typical_w"] - 3.5)  # servo1 at half duty
    other = tmp_path / "other.plan.yaml"
    other.write_text("segments:\n  - duration_s: 10\n    device_modes: {dev.servo1: 0.0}\n")
    [col2] = build_payload(load_contract(lock), contract_path=lock, plan_path=other)["power_columns"]
    assert col2["typical_w"] == pytest.approx(plain["typical_w"] - 7.0)


def test_power_panels_are_empty_without_a_budget(clean_contract):
    p = build_payload(clean_contract.model_copy(update={"topology": None, "devices": []}))
    assert p["power_columns"] == [] and p["power_stress"] == []


def _arm_payload(path):
    return build_payload(load_contract(path), contract_path=path)


def _torque(page):
    """The biggest 'Measured X N*m' gravity torque among the page's cards."""
    return max(float(m) for c in page["cards"] for m in re.findall(r"Measured ([\d.]+) N\*m", c["body"]))


def test_editing_a_named_size_in_the_app_moves_the_torque_and_the_drawn_link(tmp_path, monkeypatch):
    """The app's declare of params/arm_length/value redraws the link and moves the torque."""
    from test_derive_geometry import _write

    from lockwire.serve import DeclareArgs, _handle_declare

    path = _write(tmp_path, monkeypatch)  # arm_length 0.2 m, a cylinder as long as it
    before = _arm_payload(path)
    declare = DeclareArgs(
        contract=str(path), path="params/arm_length/value", value=0.3, source="app", witness="me"
    )
    assert _handle_declare(declare).refusal is None
    after = _arm_payload(path)
    arm = lambda page: next(link for link in page["links"] if link["name"] == "arm")  # noqa: E731
    assert [arm(p)["solids"][0]["length"] for p in (before, after)] == [0.2, 0.3]
    assert after["params"] == [{"name": "arm_length", "value": 0.3, "unit": None}]
    assert _torque(after) > _torque(before) * 1.3


def test_a_declared_mass_is_not_moved_by_a_named_size_and_a_derived_one_says_so(
    tmp_path, monkeypatch
):
    from test_derive_geometry import _write

    path = _write(tmp_path, monkeypatch)
    links = {link["name"]: link for link in _arm_payload(path)["links"]}
    assert links["arm"]["mass_note"].endswith("derived from its shapes and material")
    assert links["fore"]["mass_note"] is None  # declared 1.5 kg: nothing derived, nothing said
    held = tmp_path / "held.yaml"
    held.write_text(_write(tmp_path, monkeypatch, arm_extra={"mass": {"value": 9.0, "unit": "kg"}}).read_text())
    assert _arm_payload(held)["links"][1]["mass_note"] is None
    held.write_text(held.read_text().replace("value: 0.2\n", "value: 0.6\n"))
    assert load_contract(held).model.links[1].mass.value == 9.0  # a longer arm, the declared mass stays
    bare = _write(tmp_path, monkeypatch, density=False)
    assert "no material density" in _arm_payload(bare)["links"][1]["mass_note"]




def test_the_page_offers_exactly_the_catalog_materials_with_sources_and_plastic_caveats(tmp_path, monkeypatch):
    from lockwire.catalog import materials
    from test_derive_geometry import _write

    options = _arm_payload(_write(tmp_path, monkeypatch))["materials"]
    assert [(o["name"], o["density_kg_m3"]) for o in options] == [(m.name, m.density_kg_m3) for m in materials()]
    assert all(o["source_label"] and o["source_url"].startswith("http") and o["source"] for o in options)
    assert [o["name"].split()[0] for o in options if o["caveat"]] == ["PLA", "PETG", "ABS"]
    assert "infill" in options[3]["caveat"]
    assert "Polymaker PolyLite PLA Technical Data Sheet" in options[3]["source_label"]


def test_the_page_carries_each_links_compare_table_from_the_engine(tmp_path, monkeypatch):
    from test_derive_geometry import _write

    path = _write(tmp_path, monkeypatch)
    path.write_text(path.read_text().replace("name: aluminium", "name: Aluminium 6061-T6"))
    got = {c["link"]: c for c in _arm_payload(path)["material_compare"]}
    rows = {r["material"]: r for r in got["arm"]["rows"]}
    assert len(rows) == 7 and [r["material"] for r in got["arm"]["rows"] if r["current"]] == ["Aluminium 6061-T6"]
    assert rows["Mild steel S235JR"]["total_mass_kg"] > rows["Aluminium 6061-T6"]["total_mass_kg"]
    steel = rows["Mild steel S235JR"]
    assert steel["delta_mass_kg"] == pytest.approx(steel["total_mass_kg"] - rows["Aluminium 6061-T6"]["total_mass_kg"])
    assert steel["delta_mass_kg"] > 0 > steel["delta_margin_nm"]
    assert got["fore"]["rows"] == [] and "no volume to scale" in got["fore"]["note"]  # declared 1.5 kg


def test_a_derived_mass_row_carries_its_marker_and_density_source_a_declared_one_does_not(tmp_path, monkeypatch):
    from test_derive_geometry import _write

    path = _write(tmp_path, monkeypatch)
    rows = {r["id"].split("/")[2]: r for r in _arm_payload(path)["forces"]["masses"]}
    assert rows["arm"]["kind"] == "derived"
    assert "density source:" in rows["arm"]["mass_note"]
    assert rows["fore"]["kind"] != "derived" and rows["fore"].get("mass_note") is None


def _gaugeless_wired():
    c = _wired({"id": "wire.tx", "from": {"device": "dev.a", "connector": "J1", "pin": 2},
                "to": {"device": "dev.b", "pin": "A4"}, **_PIN_WIRE})  # fmt: skip
    c.topology.wires[-1].__dict__["awg"] = None  # a signal wire may omit its gauge
    return c


def test_gaugeless_signal_wire_sends_null_awg_and_prints_no_gauge():
    from lockwire.report_html import render_html

    c = _gaugeless_wired()
    assert build_payload(c)["wiring_wires"][-1]["awg"] is None
    html = render_html(c)
    assert "None AWG" not in html and "<td>Wire Tx</td><td>A J1 2</td><td>B A4</td><td></td><td></td></tr>" in html


def test_the_page_carries_the_robot_graph_and_each_card_its_involved_parts_from_rel1(monkeypatch):
    import lockwire.page as page

    graph = {"nodes": [{"id": "link:a", "kind": "link", "label": "a"}], "edges": []}
    monkeypatch.setattr(page, "robot_graph", lambda c: graph)
    monkeypatch.setattr(page, "involved", lambda c, f: ["link:a", "joint:j"])
    payload = build_payload(load_contract(X500))
    assert payload["robot_graph"] == graph
    assert payload["cards"] and all(c["involves"] == ["joint:j", "link:a"] for c in payload["cards"])


def test_a_real_example_fills_the_graph_and_the_cards_name_their_parts():
    payload = build_payload(load_contract(X500))
    ids = {n["id"] for n in payload["robot_graph"]["nodes"]}
    assert ids and any(c["involves"] for c in payload["cards"])
    assert all(set(c["involves"]) <= ids for c in payload["cards"])


def test_add_part_lists_stock_and_kit_with_outline_and_mass_source():
    """The Structure tab's data. A plate's mass is derived from material, a T-slot's from the maker; only a body-carrying kit part is outline only."""
    from lockwire.catalog import structure_parts

    ap = build_declare_form(Contract(robot=RobotInfo(name="r")))["add_part"]
    stock = {s["id"]: s for s in ap["stock"]}
    assert stock["stock.plate"]["mass_from"] == "material" and not stock["stock.plate"]["outline_only"]
    assert stock["tslot.20x20"]["mass_from"] == "mass_per_length" and stock["tslot.20x20"]["outline_only"]
    assert stock["stock.round_tube"]["outline_only"]
    kit = {k["id"]: k["outline_only"] for k in ap["kit"]}
    assert len(kit) == len(structure_parts()) and kit["gobilda.1501-0006-0100"] and not kit["gobilda.1120-0005-0144"]


def test_power_rail_node_carries_its_rail_sweep_and_a_rail_without_data_carries_none(tmp_path):
    from lockwire.checks import rail_sweep
    from test_checks import _brownout_lock

    def rail(**kw):
        contract = load_contract(_brownout_lock(tmp_path, **kw))
        node = next(n for n in build_payload(contract)["graph"]["power"]["nodes"] if n["id"] == "r5")
        return contract, node

    contract, node = rail()
    assert node["nominal_v"] == 5.0
    assert [(p["load"], p["volts"]) for p in node["sweep"]] == rail_sweep(contract, "r5")
    assert node["sweep"][-1]["volts"] < 4.5  # sagged under stall, never the nominal
    assert rail(servo="mg996r")[1].get("sweep") is None  # no stall data: nothing claimed


def test_wire_findings_and_graph_name_parts_not_ids():
    payload = build_payload(_example("arm-power/contract.yaml"))
    titles = [c["title"] for c in payload["cards"]]
    assert not [t for t in titles if re.search(r"\bW\b|\bW\.", t)]
    assert "Servo Feed" in " ".join(titles)
    for t in titles:
        head, _, tail = t.partition(": ")
        assert head != tail
    labels = [n["label"] for n in payload["robot_graph"]["nodes"] if n["kind"] in ("device", "rail", "wire")]
    assert labels and not [x for x in labels if re.match(r"(dev|rail|w)\.", x)]
    assert human_name("w.servo-feed") == "Servo Feed"


# --------------------------------------------------------------------------- UIA-1 titles say the catch


def _titles(name):
    return [c["title"] for c in build_payload(_example(name))["cards"]]


def test_arm_power_titles_carry_both_numbers_and_the_right_noun():
    titles = _titles("arm-power/contract.yaml")
    assert "5 V rail sags to 4.65 V, Jetson needs 4.75 V" in titles
    assert "Pack wire: 9 A fuse on wire good for 7 A" in titles
    assert not any("differs from lockfile" in t for t in titles)


def test_drifted_mass_title_says_changed_from_locked_with_both_numbers_not_missing():
    drift = Finding(
        severity="violation",
        quantity="model/links/link1/mass",
        observed="model file has mass 0.25",
        expected="locked mass 0.079119962",
        action="re-lock",
    )
    title = _sentence(Contract(robot=RobotInfo(name="r")), [drift])[0]
    assert title == "LINK1 mass changed from its locked value: 0.25 kg now, 0.07912 kg locked"
    assert "missing" not in title
    gap = drift.model_copy(update={"severity": "warning"})
    assert "missing mass" in _sentence(Contract(robot=RobotInfo(name="r")), [gap])[0]


def test_no_example_card_title_says_its_own_name_twice():
    for name in ("arm-power", "hexapod-param", "quadruped", "rover", "rover-param", "drone"):
        for title in _titles(f"{name}/contract.yaml"):
            head, _, tail = title.partition(": ")
            assert not tail or head.casefold() != tail.casefold(), title
