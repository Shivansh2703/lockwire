import pytest

from lockwire.checks import check_fuse_wire
from lockwire.presets import NOT_SOURCED, PRESETS, Preset, choose_preset
from lockwire.schema import Quantity, Wire


def _kind(c, rid):
    c.robot.id = rid
    return c


def test_preset_rover_starter_picks_rover_without_a_flag(clean_contract):
    assert choose_preset(_kind(clean_contract, "turtlebot3-burger")).name == "rover"
    assert choose_preset(_kind(clean_contract, "x500-v2-px4")).name == "drone"


def test_preset_unknown_robot_gets_no_bundle(clean_contract):
    assert choose_preset(_kind(clean_contract, "my-robot")) is None


def test_preset_explicit_override_wins_and_bad_name_is_refused(clean_contract):
    assert choose_preset(_kind(clean_contract, "turtlebot3-burger"), "frc").name == "frc"
    with pytest.raises(ValueError, match="unknown preset"):
        choose_preset(clean_contract, "nope")


def test_preset_frc_numbers_are_unsourced_advice():
    assert (PRESETS["frc"].source, NOT_SOURCED) == (None, "not yet sourced")


def _fuse_on_odd_gauge(c, rid, monkeypatch):
    monkeypatch.setitem(PRESETS, "frc", Preset("frc", {40: 1.0}))
    rail = next(r for r in c.topology.rails if r.id == "rail.5v")
    rail.current_limit = Quantity(value=5, unit="A")
    rail.current_limit_enforced_by = "fuse"
    c.topology.wires.append(
        Wire(id="w", awg=40, installation="free_air", rail="rail.5v", owner="o")
    )
    c.robot.id = rid
    return [
        f for f in check_fuse_wire(c) if f.quantity.endswith("/fuse") and "preset" in f.expected
    ]


def test_preset_default_is_advice_never_a_violation(clean_contract, monkeypatch):
    import lockwire.presets as p

    monkeypatch.setattr(p, "override", "frc")
    (f,) = _fuse_on_odd_gauge(clean_contract, "x", monkeypatch)
    assert (f.severity, f.advice_unsourced, NOT_SOURCED in f.expected) == ("warning", True, True)


def test_preset_missing_input_emits_nothing(clean_contract, monkeypatch):
    assert _fuse_on_odd_gauge(clean_contract, "unknown-robot", monkeypatch) == []
