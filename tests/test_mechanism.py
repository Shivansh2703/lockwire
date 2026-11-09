"""The mechanism check, on a named set — the four-bar by Grashof, everything else NOT SCORED.

**Every expected number below is a literal**, and the inequality it comes from is
written out in the docstring of the test that uses it. Nothing here recomputes
``mechanism.py`` and compares it to itself (parity-anchor law): the fixtures carry
link lengths that are exact binary fractions, so both Grashof sums are exact floats
and can be asserted as written constants with no tolerance argument at all.

Grashof, once, for all four tests: for a planar four-bar with ``s`` the shortest link,
``l`` the longest and ``p``, ``q`` the other two, some link turns all the way round
**iff** ``s + l <= p + q``.
"""

from pathlib import Path

from lockwire_pro import api_pro as api
from lockwire import checks
from lockwire.checks import run_record_checks
from lockwire.mechanism import analyse_model
from lockwire.schema import load_contract

FIXTURES = Path(__file__).parent / "fixtures"

# fourbar_grashof: s = 0.125, l = 0.5, p = 0.4375, q = 0.375 (metres).
GRASHOF_S_PLUS_L = 0.625
GRASHOF_P_PLUS_Q = 0.8125
# fourbar_locking: s = 0.25, l = 0.75, p = 0.3125, q = 0.375 (metres).
LOCKING_S_PLUS_L = 1.0
LOCKING_P_PLUS_Q = 0.6875
# fourbar_changepoint: s = 0.125, l = 0.5, p = 0.25, q = 0.375 (metres).
CHANGEPOINT_S_PLUS_L = 0.625
CHANGEPOINT_P_PLUS_Q = 0.625


def _contract(name: str):
    return load_contract(FIXTURES / name / "lockwire.yaml")


def _mechanisms(name: str):
    return analyse_model(_contract(name).model)


def test_the_known_good_four_bar_reports_a_fully_rotating_link() -> None:
    """0.125 + 0.5 = 0.625 <= 0.4375 + 0.375 = 0.8125, so the crank turns all the way round."""
    (mechanism,) = _mechanisms("fourbar_grashof")
    assert mechanism.kind == "four-bar"
    assert mechanism.verdict == "rotates"
    assert mechanism.s_plus_l == GRASHOF_S_PLUS_L
    assert mechanism.p_plus_q == GRASHOF_P_PLUS_Q
    assert "has a fully rotating link" in mechanism.text
    # The link that rotates is named, not merely asserted to exist.
    assert "crank" in mechanism.text

    # Through the evaluator: a pass is a scored comparison, never a finding.
    report = run_record_checks(_contract("fourbar_grashof"))
    assert [f for f in report if f.quantity.startswith("model/mechanisms/")] == []
    passed = [r for r in report.scored_records if r.quantity.startswith("model/mechanisms/")]
    assert len(passed) == 1
    assert passed[0].observed == mechanism.text


def test_the_known_locking_four_bar_is_reported_as_locking() -> None:
    """0.25 + 0.75 = 1.0 > 0.3125 + 0.375 = 0.6875, so no link turns all the way round —
    and ``ground_to_crank`` is declared ``continuous``, which is the contradiction."""
    (mechanism,) = _mechanisms("fourbar_locking")
    assert mechanism.kind == "four-bar"
    assert mechanism.verdict == "locking"
    assert mechanism.s_plus_l == LOCKING_S_PLUS_L
    assert mechanism.p_plus_q == LOCKING_P_PLUS_Q
    assert "no fully rotating link" in mechanism.text

    # The library door (lockwire.api.check) carries the same sentence as a violation.
    document = api.check(FIXTURES / "fourbar_locking" / "lockwire.yaml")
    entries = [f for f in document["findings"] if f["quantity"].startswith("model/mechanisms/")]
    assert len(entries) == 1
    assert entries[0]["severity"] == "violation"
    assert entries[0]["observed"] == mechanism.text
    assert "ground_to_crank" in entries[0]["expected"]


def test_the_change_point_boundary_is_a_warning_not_a_confident_rotates() -> None:
    """0.125 + 0.5 = 0.625 == 0.25 + 0.375 = 0.625, exactly: the Grashof boundary.

    Strict Grashof (``s + l <= p + q``) would call this ``rotates`` — the confident,
    plausible, wrong answer, because at the change-point the linkage passes through a
    folded position and which branch it takes is not decided by the link lengths alone.
    This pins the check's actual choice: ``change-point``, reported as a ``warning``,
    not a scored pass. A later box tightening the inequality toward strict Grashof
    must fail this test before it can reintroduce that false confidence.
    """
    (mechanism,) = _mechanisms("fourbar_changepoint")
    assert mechanism.kind == "four-bar"
    assert mechanism.verdict == "change-point"
    assert mechanism.s_plus_l == CHANGEPOINT_S_PLUS_L
    assert mechanism.p_plus_q == CHANGEPOINT_P_PLUS_Q
    assert "change-point" in mechanism.text
    assert "indeterminate" in mechanism.text

    document = api.check(FIXTURES / "fourbar_changepoint" / "lockwire.yaml")
    entries = [f for f in document["findings"] if f["quantity"].startswith("model/mechanisms/")]
    assert len(entries) == 1
    assert entries[0]["severity"] == "warning"
    assert entries[0]["observed"] == mechanism.text


def test_the_verdict_names_both_sides_of_the_inequality() -> None:
    """Both sums are in the sentence a reader sees, on both four-bar verdicts —
    a verdict without its two numbers is a claim nobody can check."""
    for name, s_plus_l, p_plus_q in (
        ("fourbar_grashof", GRASHOF_S_PLUS_L, GRASHOF_P_PLUS_Q),
        ("fourbar_locking", LOCKING_S_PLUS_L, LOCKING_P_PLUS_Q),
    ):
        (mechanism,) = _mechanisms(name)
        assert mechanism.s_plus_l == s_plus_l, name
        assert mechanism.p_plus_q == p_plus_q, name
        assert "s + l = " in mechanism.text, name
        assert "p + q = " in mechanism.text, name
        # The rendered numbers, not just the fields.
        assert f"{s_plus_l:g}" in mechanism.text, name
        assert f"{p_plus_q:g}" in mechanism.text, name


def test_an_unnamed_mechanism_is_not_scored_and_names_its_kind() -> None:
    """The whole not-in-the-named-set path, not one fixture.

    A five-bar read from disk, plus every synthetic loop shape the module can meet:
    each must decline **naming the kind it found** and must report no numbers at all.
    Scoring any of these as a four-bar built from four of its links is the confident,
    plausible, wrong answer this check exists to prevent.
    """
    (five,) = _mechanisms("fivebar")
    assert five.kind == "five-bar"
    assert five.verdict == "not-scored"
    assert five.s_plus_l is None and five.p_plus_q is None
    assert "five-bar" in five.text and "not scored" in five.text

    document = api.check(FIXTURES / "fivebar" / "lockwire.yaml")
    entries = [f for f in document["findings"] if f["quantity"].startswith("model/mechanisms/")]
    assert len(entries) == 1
    assert entries[0]["severity"] == "not_scored"
    assert "five-bar" in entries[0]["observed"]

    # Every other way out of the named set, built from the Grashof fixture's own
    # geometry so only the named difference can change the answer.
    base = _contract("fourbar_grashof").model
    assert base is not None
    for label, expect, mutate in (
        ("prismatic joint", "prismatic", lambda js: js[1].model_copy(update={"type": "prismatic"})),
        (
            "axes not recorded",
            "does not record",
            lambda js: js[1].model_copy(update={"axis": None}),
        ),
        (
            "non-parallel axes",
            "not parallel",
            lambda js: js[1].model_copy(update={"axis": (1.0, 0.0, 0.0)}),
        ),
        (
            "offset along the axis",
            "one plane",
            lambda js: js[1].model_copy(
                update={"origin": js[1].origin.model_copy(update={"xyz": (0.125, 0.0, 0.125)})}
            ),
        ),
    ):
        joints = list(base.joints)
        joints[1] = mutate(joints)
        model = base.model_copy(update={"joints": joints})
        (mechanism,) = analyse_model(model)
        assert mechanism.verdict == "not-scored", label
        assert expect in mechanism.text, (label, mechanism.text)
        assert mechanism.s_plus_l is None and mechanism.p_plus_q is None, label


def test_the_sentence_is_a_payload_string_not_composed_downstream() -> None:
    """No surface re-assembles the verdict: the finished sentence arrives in the payload.

    The distinctive phrases below are produced only by ``mechanism.py``. If a copy of
    any of them appears in a JS template, two places would then decide what a robot's
    mechanism verdict says, and they would drift.
    """
    src = Path(__file__).parent.parent / "src" / "lockwire"
    phrases = (
        "fully rotating link",
        "Grashof",
        "rocks between two limits",
        "change-point",
    )
    for module in ("lockwire/page.py", "lockwire/report_html.py"):
        text = (src.parent / module).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase not in text, f"{module} carries a copy of the mechanism wording: {phrase}"

    # And the sentence really is what the payload carries, character for character.
    (mechanism,) = _mechanisms("fourbar_locking")
    document = api.check(FIXTURES / "fourbar_locking" / "lockwire.yaml")
    observed = [
        f["observed"] for f in document["findings"] if f["quantity"].startswith("model/mechanisms/")
    ]
    assert observed == [mechanism.text]


def test_a_loop_nothing_declares_continuous_costs_the_false_positive_budget_nothing() -> None:
    """A four-bar of limited revolute joints is a double-rocker — a legitimate machine.

    Nothing is declared to turn all the way round, so there is no second operand and
    the check says nothing: no finding, no scored comparison. Without this, the check
    would fire on every parallel linkage in every model.
    """
    base = _contract("fourbar_locking").model
    assert base is not None
    joints = [j.model_copy(update={"type": "revolute"}) for j in base.joints]
    contract = _contract("fourbar_locking").model_copy(
        update={"model": base.model_copy(update={"joints": joints})}
    )
    report = run_record_checks(contract)
    assert [f for f in report if f.quantity.startswith("model/mechanisms/")] == []
    assert [r for r in report.scored_records if r.quantity.startswith("model/mechanisms/")] == []


def test_a_rocker_declared_continuous_is_a_violation_naming_the_joint_that_turns() -> None:
    """crank 0.1, ground 0.4, coupler 0.35, rocker 0.3.

    0.1 + 0.4 = 0.5 <= 0.35 + 0.3 = 0.65, so Grashof holds, but only the two joints at
    the ends of the crank turn fully; ``rocker_to_ground`` swings between dead centres.
    """
    name = "fourbar_rocker_continuous"
    (mechanism,) = _mechanisms(name)
    assert mechanism.verdict == "rotates"
    assert mechanism.rotating_joints == ("crank_to_coupler", "ground_to_crank")
    (finding,) = [
        f for f in checks.check_mechanisms(_contract(name)) if f.quantity.startswith("model/mech")
    ]
    assert finding.severity == "violation"
    assert "rocker_to_ground" in finding.action
    assert "crank_to_coupler" in finding.observed


def test_the_grashof_boundary_tolerates_float_rounding() -> None:
    """0.1 + 0.7 and 0.3 + 0.5 are equal on paper, not in floats."""
    (mechanism,) = _mechanisms("fourbar_float_boundary")
    assert mechanism.verdict == "change-point"
