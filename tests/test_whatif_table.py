"""`lockwire what-if` says what moved; the full reports sit behind --json -."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire import api, plugins
from lockwire.cli import app
from lockwire.edit import Change
from lockwire.whatif_table import whatif_table

FIVEBAR = Path(__file__).parent / "fixtures" / "fivebar_wide.sdf"
ARM = Path(__file__).parents[1] / "src" / "lockwire" / "examples" / "arm"
MASS = "model/links/lower_l/mass"


def _what_if(contract, path, value, *flags):
    args = ["what-if", str(contract), "--change", f"{path}={value}", "--source", "t"]
    return CliRunner().invoke(app, [*args, "--witness", "op", *flags])


@pytest.fixture(scope="module")
def fivebar(tmp_path_factory):
    lock = tmp_path_factory.mktemp("five") / "l.yaml"
    assert CliRunner().invoke(app, ["start", str(FIVEBAR), "--out", str(lock)]).exit_code == 0
    return lock, _what_if(lock, MASS, "1.5 kg"), _what_if(lock, MASS, "1.5 kg", "--json", "-")


def test_fivebar_heavier_link_shows_the_torques_the_verdict_and_the_echo_apart(fivebar):
    _, human, as_json = fivebar
    assert human.exit_code == 1 and json.loads(as_json.stdout)["diff"]  # NO-GO after: exit 1
    out = human.stdout
    assert "from 1 kg to 1.5 kg" in out and "GO with notes → NO-GO" in out
    row_a = next(line for line in out.splitlines() if line.startswith("A, "))  # check's names: label_name
    assert all(x in row_a for x in ("3.35", "5.02", "5.00", "passed", "violation"))
    assert "3.32" in out and "4.99" in out  # joint b, still under its limit
    assert len(out.splitlines()) < 40
    new, _, echo = out.partition("echoed back")
    assert MASS not in new.partition("New:")[2] and MASS in echo


def _side(comparisons, findings, verdict):
    return {"verdict": verdict, "comparisons": comparisons, "findings": findings}


def test_table_pairs_a_comparison_with_a_finding_and_names_new_and_gone():
    effort = "model/joints/a/limits/effort"
    cmp = {"quantity": effort, "observed": "3 N*m (x)", "expected": "<= 5 N*m"}
    old = {"severity": "warning", "quantity": "w", "observed": "old", "expected": "e"}
    bad = {**cmp, "severity": "violation", "observed": "6 N*m z"}
    table = whatif_table(
        {"current": _side([cmp], [old], "GO"), "proposed": _side([], [bad], "NO-GO")}
    )
    assert [(r["before"], r["after"], r["status"]) for r in table["rows"]] == [
        ("3 N*m", "6 N*m", "passed → violation")
    ]
    assert table["new"] == [] and len(table["resolved"]) == 1  # a finding shows once


def _finding(quantity, observed, severity="warning"):
    return {"severity": severity, "quantity": quantity, "observed": observed, "expected": "e"}


def test_quantities_on_one_side_only_are_shown_once_each():
    passed = {"quantity": "q.cmp", "observed": "1 A", "expected": "<= 2 A"}
    only_before = _side([passed], [_finding("q.gone", "g")], "GO")
    only_after = _side([], [_finding("q.nsc", "n", "not_scored")], "GO")
    table = whatif_table({"current": only_before, "proposed": only_after})
    assert [(r["quantity"], r["status"]) for r in table["rows"]] == [("q.cmp", "gone")]
    assert len(table["new"]) == 1 and "q.nsc" in table["new"][0]
    assert len(table["resolved"]) == 1 and "q.gone" in table["resolved"][0]
    table = whatif_table({"current": only_after, "proposed": only_before})
    assert [(r["quantity"], r["status"]) for r in table["rows"]] == [("q.cmp", "new passed")]


def test_same_number_with_other_words_is_a_changed_row_not_nothing():
    cur = _finding("topology/wires/w.x/ampacity", "5.19 A on 28 AWG (draw mode: nominal)")
    new = {**cur, "observed": "5.19 A on 28 AWG (draw mode: stall)"}
    table = whatif_table({"current": _side([], [cur], "GO"), "proposed": _side([], [new], "GO")})
    row = table["rows"][0]
    assert (row["before"], row["after"]) == ("mode: nominal)", "mode: stall)")
    assert row["label"] == "X, wire current" and row["status"] == "still warning"


def _drift(observed):
    return _finding("model/structure_hash", observed, "violation")


def test_joint_drift_is_the_echo_only_when_it_names_just_that_joint():
    path = ("model/joints/a/limits/effort",)
    for text, is_echo in (
        ("joint moved: a (limits changed)", True),
        ("joint moved: a (limits changed), b (limits changed)", False),
        ("joint moved: b (limits changed)", False),
    ):
        proposed = _side([], [_drift(text)], "NO-GO")
        table = whatif_table({"current": _side([], [], "GO"), "proposed": proposed}, path)
        assert bool(table["echo"]) is is_echo and len(table["new"]) == (not is_echo)
        assert ("only because" in table["verdict_after"]) is is_echo
    other = whatif_table({"current": _side([], [], "GO"), "proposed": proposed}, ("model/x",))
    assert other["echo"] == [] and "only because" not in other["verdict_after"]


def test_a_real_new_violation_next_to_the_echo_gets_no_only_because():
    bad = _finding("q.real", "real", "violation")
    proposed = _side([], [_drift("joint moved: a (limits changed)"), bad], "NO-GO")
    table = whatif_table(
        {"current": _side([], [], "GO"), "proposed": proposed}, ("model/joints/a/limits/upper",)
    )
    assert len(table["echo"]) == 1 and table["verdict_after"] == "NO-GO"


def test_arm_link_mass_gives_rows_for_the_joints_it_loads(tmp_path):
    shutil.copytree(ARM, tmp_path / "arm")
    result = _what_if(
        tmp_path / "arm" / "contract.yaml", "model/links/upper_arm_link/mass", "0.3 kg"
    )
    row = next(line for line in result.stdout.splitlines() if "Shoulder lift" in line)
    assert "0.86" in row and "1.03" in row and "other quantities unchanged" in result.stdout


def test_a_change_that_alters_nothing_says_so_and_a_refusal_still_exits_2(fivebar):
    lock = fivebar[0]
    same = _what_if(lock, MASS, "1 kg")
    assert same.exit_code == 0 and "Nothing changed" in same.stdout
    assert _what_if(lock, MASS, "zz").exit_code == 2


def test_json_flag_prints_the_library_document_only(fivebar):
    lock, _, as_json = fivebar
    change = Change(path=MASS, value="1.5 kg", source="t", witness="op")
    assert json.loads(as_json.stdout) == plugins.pick("what_if", api.what_if)(lock, [change])


def test_a_joint_limit_change_is_echoed_once_and_a_link_com_change_too(fivebar):
    lock = fivebar[0]
    for path, value in (
        ("model/joints/a/limits/upper", "0.1"),
        ("model/links/lower_l/com", "[0.04,0,-0.095]"),
    ):
        out = _what_if(lock, path, value).stdout
        assert (
            out.count("VIOLATION") == 1 and out.partition("echoed back")[2].count("VIOLATION") == 1
        )
        assert "New:" not in out
    assert (
        "NO-GO (only because the model file still has the old value)"
        in _what_if(lock, "model/joints/a/limits/upper", "0.1").stdout
    )


def test_arm_power_draw_change_with_same_current_is_not_nothing(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    start = ["start", "--example", "arm-power", "--out", str(lock)]
    assert CliRunner().invoke(app, start).exit_code == 0
    out = _what_if(lock, "devices/dev.servo.shoulder/power/draw", "2 W").stdout
    assert "Nothing changed" not in out and "mode: stall)" in out and "Servo feed" in out


def test_help_says_short_answer_and_json_gives_full_reports():
    out = " ".join(CliRunner().invoke(app, ["what-if", "--help"]).stdout.split())
    assert "short before-and-after answer" in out and "--json gives the full reports" in out


def test_what_if_exits_1_on_no_go_even_with_json_and_0_when_not(fivebar):
    lock, _, as_json = fivebar
    assert as_json.exit_code == 1 and json.loads(as_json.stdout)["proposed"]["verdict"] == "NO-GO"
    assert _what_if(lock, MASS, "1 kg").exit_code == 0


def test_rows_read_in_the_tools_words(tmp_path, monkeypatch):
    """rover-param tip margin: advice (not "still warning"), the unit kept, capital headers;
    rover rail voltage: "not scored", not the state name."""
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["start", "--example", "rover-param"]).exit_code == 0
    out = _what_if(Path("lockwire.yaml"), "params/half_wheelbase/value", "0.02").stdout
    assert "Quantity" in out and "is 150 mm" in out and "is 20 mm" in out
    assert "still warning" not in out and out.split("Tip margin", 1)[1].split("\n")[0].rstrip().endswith("advice")
    (tmp_path / "r").mkdir()
    monkeypatch.chdir(tmp_path / "r")
    assert CliRunner().invoke(app, ["start", "--example", "rover"]).exit_code == 0
    out = _what_if(Path("lockwire.yaml"), "topology/rails/rail.controller-12v/voltage", "12 V").stdout
    assert "not scored →" in out and "not_scored" not in out and "Controller 12 V," in out
