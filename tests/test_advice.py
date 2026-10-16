"""The alternatives half of the advice sentence.

``check_budgets`` already knows a rail_power budget is over its limit; this
suite tests the second half — what else could carry it — via the checked-in
``over_budget_contract`` fixture below and the ``advice.build_rail_alternatives``
builder directly. Parity-anchor law: the "every alternative carries a kind"
test reads the alternatives back off the *contract's own check output* — a
checked-in fixture, not the emitter's own re-derivation — so it is anchored
outside the builder under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lockwire.advice import Alternative, build_rail_alternatives
from lockwire.checks import run_record_checks
from lockwire.schema import Contract, load_contract

_CONTRACT_YAML = """\
schema_version: "0.3"
robot:
  name: advicebot
topology:
  rails:
    - id: rail.over
      voltage: {value: 5, unit: V}
      capacity: {value: 10, unit: W}
      upstream: battery
    - id: rail.free
      voltage: {value: 5, unit: V}
      capacity: {value: 20, unit: W}
      upstream: battery
    - id: rail.unknown
      voltage: {value: 5, unit: V}
      capacity: {value: 10, unit: W}
      upstream: battery
  battery:
    capacity: {value: 50, unit: Wh}
devices:
  - id: dev.over
    model: mdl.a
    owner: shivansh
    power: {rail: rail.over, draw: {value: 8, unit: W}}
  - id: dev.free
    model: mdl.b
    owner: shivansh
    power: {rail: rail.free, draw: {value: 2, unit: W}}
  - id: dev.unknown
    model: mdl.c
    owner: shivansh
    power: {rail: rail.unknown}
budgets:
  - id: bud.over
    kind: rail_power
    rail: rail.over
    limit: {value: 5, unit: W}
    owner: shivansh
  - id: bud.free
    kind: rail_power
    rail: rail.free
    limit: {value: 12, unit: W}
    owner: shivansh
  - id: bud.unknown
    kind: rail_power
    rail: rail.unknown
    limit: {value: 5, unit: W}
    owner: shivansh
"""


@pytest.fixture
def over_budget_contract(tmp_path: Path) -> Contract:
    """rail.over draws 8 W against a 5 W limit; rail.free has 10 W headroom
    against its own 12 W limit; rail.unknown carries an undeclared draw, so it
    must never be offered as an alternative."""
    path = tmp_path / "contract_rail_over_budget.yaml"
    path.write_text(_CONTRACT_YAML)
    return load_contract(path)


def _rail_finding(contract: Contract):
    findings = run_record_checks(contract)
    (finding,) = [
        f for f in findings if f.severity == "violation" and f.quantity == "budgets/bud.over"
    ]
    return finding


def test_every_alternative_number_carries_its_kind(over_budget_contract: Contract) -> None:
    finding = _rail_finding(over_budget_contract)
    assert finding.alternatives, "no alternatives on an over-budget rail"
    for alt in finding.alternatives:
        assert alt.kind, f"alternative with no kind: {alt.text!r}"
    kinds = {alt.kind for alt in finding.alternatives}
    assert "derived-by-check_budgets" in kinds
    assert "projected" in kinds


def test_a_move_names_the_load_and_how_much_it_relieves(over_budget_contract: Contract) -> None:
    (move,) = [a for a in _rail_finding(over_budget_contract).alternatives if a.kind != "projected"]
    assert move.text == "move 'dev.over' to rail 'rail.free' (10.00 W free); clears the overload"
    assert (move.rail, move.device) == ("rail.free", "dev.over")  # typed, never parsed from text
    (partial, _) = build_rail_alternatives(
        over_budget_contract,
        over_rail_id="rail.over",
        deficit_w=20.0,
        rail_totals={"rail.free": 2.0},
        rail_unknown={},
    )
    assert partial.text.endswith("; frees 8.00 W of the 20.00 W over")


def test_a_suggested_rating_is_marked_projected(over_budget_contract: Contract) -> None:
    finding = _rail_finding(over_budget_contract)
    projected = [a for a in finding.alternatives if "regulator" in a.text]
    assert projected, "no regulator alternative offered"
    assert all(a.kind == "projected" for a in projected)


def test_raising_the_limit_is_never_offered_as_an_alternative(
    over_budget_contract: Contract,
) -> None:
    finding = _rail_finding(over_budget_contract)
    for alt in finding.alternatives:
        assert "capacity" not in alt.text
        assert "raise" not in alt.text.lower()


def test_headroom_excludes_rails_with_unknown_consumers(over_budget_contract: Contract) -> None:
    finding = _rail_finding(over_budget_contract)
    for alt in finding.alternatives:
        assert "rail.unknown" not in alt.text


def test_a_rail_with_no_alternative_says_so_rather_than_offering_nothing(
    tmp_path: Path,
) -> None:
    """Directly at the builder: no sibling rail_power budget, and a zero
    deficit so no regulator rating can be projected either — the shared
    builder must still say so, never return an empty list."""
    path = tmp_path / "contract_no_sibling.yaml"
    path.write_text(
        """\
schema_version: "0.3"
robot:
  name: nosiblingbot
topology:
  rails:
    - id: rail.over
      capacity: {value: 10, unit: W}
      upstream: battery
  battery:
    capacity: {value: 50, unit: Wh}
budgets:
  - id: bud.over
    kind: rail_power
    rail: rail.over
    limit: {value: 5, unit: W}
    owner: shivansh
"""
    )
    contract = load_contract(path)
    alternatives = build_rail_alternatives(
        contract,
        over_rail_id="rail.over",
        deficit_w=0.0,
        rail_totals={},
        rail_unknown={},
    )
    assert alternatives, "an empty list silently offers nothing"
    assert all(isinstance(a, Alternative) for a in alternatives)
    assert alternatives[0].kind == "none"


def test_rails_for_voltage_never_returns_a_rail_outside_the_range(
    over_budget_contract: Contract,
) -> None:
    from lockwire.advice import input_range, rails_for_voltage

    assert {r.id for r in rails_for_voltage(over_budget_contract, 4.75, 5.25)} == {
        "rail.over",
        "rail.free",
        "rail.unknown",
    }
    assert rails_for_voltage(over_budget_contract, 11.0, 13.0) == []  # a 12 V part: no 5 V rail
    assert input_range(None, None, None) is None  # no voltage -> never guessed
