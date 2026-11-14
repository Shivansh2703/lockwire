"""The family the app's power-tree cards give a new model (agent.ts UNCLASSIFIED_FAMILY) must
not trip a keyword check: the servo-bus check reads model id plus family text."""

from lockwire.checks import SERVO_BROADCAST_ID, check_servo_bus
from lockwire.schema import DeviceIdentity, DeviceInstance, DeviceModel


def _servo_bus_findings(contract, family):
    contract.device_models.append(DeviceModel(id="mdl.part", family=family))
    ident = DeviceIdentity(
        bus_ref="bus.serial0", host="nav-computer", address=str(SERVO_BROADCAST_ID)
    )
    contract.devices.append(DeviceInstance(id="dev.a", model="mdl.part", owner="o", identity=ident))
    return check_servo_bus(contract)


def test_unclassified_family_fires_no_servo_keyword_check(clean_contract):
    assert _servo_bus_findings(clean_contract, "fam.unclassified") == []


def test_a_maker_word_in_the_family_does_fire_it(clean_contract):
    assert _servo_bus_findings(clean_contract, "fam.dynamixel") != []
