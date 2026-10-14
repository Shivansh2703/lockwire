"""ROS 2 signal units are typed, never inferred.

The rule is "type it", and it is enforced by tests rather than prose: if unit inference is ever added as a fallback,
default, or convenience flag, the cases below go red.

Also pins that the diff is genuinely additive: every committed contract must
still load, and SCHEMA_VERSION must not have moved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lockwire.schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    Driver,
    SignalSource,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# the unit is required and validated
# ---------------------------------------------------------------------------


def test_a_signal_source_carries_its_unit():
    source = SignalSource(field="linear_acceleration.z", unit="m/s^2")
    assert source.field == "linear_acceleration.z"
    assert source.unit == "m/s^2"


def test_a_signal_without_a_unit_is_a_validation_error():
    """If this ever passes, a unit is being inferred somewhere."""
    with pytest.raises(ValidationError):
        SignalSource(field="linear_acceleration.z")


def test_an_unknown_unit_is_rejected_not_guessed():
    with pytest.raises(ValidationError, match="unknown unit"):
        SignalSource(field="linear_acceleration.z", unit="furlongs/fortnight")


def test_an_empty_unit_is_rejected():
    """The blank-string route into "no unit" must close too, not just the missing key."""
    with pytest.raises(ValidationError):
        SignalSource(field="linear_acceleration.z", unit="")


def test_a_blank_field_path_is_rejected():
    with pytest.raises(ValidationError):
        SignalSource(field="   ", unit="m/s^2")


def test_the_unit_is_not_derived_from_the_message_type():
    """The specific shortcut this rule refuses.

    `sensor_msgs/msg/Imu` linear acceleration is m/s^2 by REP 145, so a system
    that inferred units would accept this. It must not.
    """
    with pytest.raises(ValidationError):
        Driver(
            topic="/imu/data",
            msg_type="sensor_msgs/msg/Imu",
            signals={"accel_z": {"field": "linear_acceleration.z"}},
        )


# ---------------------------------------------------------------------------
# Driver wiring
# ---------------------------------------------------------------------------


def test_driver_accepts_a_full_ros2_mapping():
    driver = Driver(
        node="/imu_driver",
        topic="/imu/data",
        msg_type="sensor_msgs/msg/Imu",
        signals={
            "accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"},
            "gyro_x": {"field": "angular_velocity.x", "unit": "rad/s"},
        },
    )
    assert driver.msg_type == "sensor_msgs/msg/Imu"
    assert driver.signals["accel_z"].unit == "m/s^2"
    assert driver.signals["gyro_x"].field == "angular_velocity.x"


def test_signals_without_a_message_type_are_rejected():
    """A dotted field path cannot be resolved against nothing."""
    with pytest.raises(ValidationError, match="no msg_type"):
        Driver(topic="/imu/data", signals={"accel_z": {"field": "x", "unit": "m/s^2"}})


def test_a_driver_with_neither_field_is_unchanged():
    """The earlier shape must keep working — this is what makes the diff additive."""
    driver = Driver(node="/imu_driver", topic="/imu/data", nominal_rate_hz=5.0)
    assert driver.msg_type is None
    assert driver.signals == {}


def test_command_topic_is_optional_but_never_blank():
    assert Driver(command_topic="/arm/command").command_topic == "/arm/command"
    with pytest.raises(ValidationError, match="command_topic must not be blank"):
        Driver(command_topic="   ")


def test_a_message_type_alone_is_allowed():
    """Declaring the type without a mapping is legal; it just captures nothing."""
    assert Driver(topic="/imu/data", msg_type="sensor_msgs/msg/Imu").signals == {}


# ---------------------------------------------------------------------------
# The diff is additive
# ---------------------------------------------------------------------------


def test_schema_version_did_not_move():
    """Typed units remain additive: legacy documents stay supported at schema v0.4."""
    assert SCHEMA_VERSION == "0.4"
    assert {"0.2", "0.3", "0.4"} <= SUPPORTED_SCHEMA_VERSIONS


@pytest.mark.parametrize("contract_name", sorted(p.name for p in FIXTURES.glob("contract_*.yaml")))
def test_every_committed_contract_still_loads(contract_name):
    """The presence floor for 'additive': not one existing lockfile may break."""
    assert load_contract(FIXTURES / contract_name) is not None
