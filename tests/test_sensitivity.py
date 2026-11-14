"""One-at-a-time sensitivity of gravity torque margins."""

from pathlib import Path

import pytest

from lockwire.api import load
from lockwire.schema import JointOrigin
from lockwire.sensitivity import sensitivity

BENCH = Path(__file__).parents[1] / "src/lockwire/examples/servo-bench.yaml"


def _bench():
    return load(BENCH)


@pytest.mark.parametrize("joint", ["shoulder", "elbow", "wrist"])
def test_hand_mass_is_the_top_driver_on_servo_bench(joint):
    assert sensitivity(_bench())[joint][0].input == "mass of hand"


def test_wrist_swing_is_five_percent_of_the_hand_load():
    top = sensitivity(_bench())["wrist"][0]
    assert top.plus_nm == pytest.approx(-0.09 * 9.80665 * 0.17 * 0.05, rel=1e-3)


def test_both_sides_are_perturbed_in_opposite_directions():
    top = sensitivity(_bench())["wrist"][0]
    assert top.minus_nm > 0 > top.plus_nm


def test_ranking_follows_relative_change_not_absolute():
    # A tiny lever arm with a big absolute step would rank first; 5% of a small number must not.
    c = _bench()
    c.model.links[3].com = (0.01, 0.0, 0.0)
    c.model.links[2].com = (0.001, 0.0, 0.0)
    c.model.joints[2].origin = JointOrigin(xyz=(0.002, 0.0, 0.0))
    assert sensitivity(c)["elbow"][0].input == "mass of hand"


def test_no_model_gives_nothing():
    assert sensitivity(_bench().model_copy(update={"model": None})) == {}


def test_joint_without_effort_limit_is_left_out_not_scored():
    c = _bench()
    c.model.joints[2].limits = c.model.joints[2].limits.model_copy(update={"effort": None})
    assert "wrist" not in sensitivity(c)


def test_sensitivity_leaves_the_contract_untouched():
    c = _bench()
    before = c.model_dump()
    sensitivity(c)
    assert c.model_dump() == before
