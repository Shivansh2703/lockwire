import csv
import io

import pytest

from lockwire.plan import Plan
from lockwire.power_budget import ENGINE_CHECKS, budget_csv, device_points, rail_columns
from lockwire.schema import Contract, DeviceInstance, PowerDraw, Rail, RobotInfo, Topology


def _dev(id, rail, draw, modes=()):
    power = {"rail": rail, "draw": {"value": draw, "unit": "W"} if draw else None}
    power["modes"] = [{"name": n, "draw": {"value": v, "unit": "W"}} for n, v in modes]
    return DeviceInstance(id=id, model="m", owner="t", power=PowerDraw.model_validate(power))


@pytest.fixture
def contract():
    rails = [
        Rail(id="r5", capacity={"value": 50, "unit": "W"}, upstream="r12", efficiency=0.5),
        Rail(id="r12", capacity={"value": 99, "unit": "W"}, upstream="battery"),
    ]
    devices = [
        _dev("motor", "r5", 10, [("idle", 2), ("stall", 40)]),
        _dev("cam", "r5", 4),
        _dev("ghost", "r5", None),
    ]
    return Contract(robot=RobotInfo(name="t"), topology=Topology(rails=rails), devices=devices)


def test_no_engine_findings():
    assert ENGINE_CHECKS == ()


def test_load_columns_light_typical_max_per_rail(contract):
    cols = rail_columns(contract)
    assert cols["r5"] == pytest.approx({"light": 6.0, "typical": 14.0, "max": 44.0})
    assert cols["r12"] == pytest.approx({"light": 12.0, "typical": 28.0, "max": 88.0})
    assert all(c["light"] <= c["typical"] <= c["max"] for c in cols.values())


def test_duty_weights_typical_and_shows_in_csv(contract):
    plan = Plan(segments=[{"duration_s": 10, "device_modes": {"motor": 0.5}}])
    contract.topology.rails[0].voltage = {"value": 5, "unit": "V"}
    assert device_points(contract, plan)["motor"]["typical"] == pytest.approx(5.0)
    rows = {r["device"]: r for r in csv.DictReader(io.StringIO(budget_csv(contract, plan)))}
    assert rows["motor"]["duty_derived_typical_over_nominal"] == "0.5"
    assert rows["motor"]["typical_w"] == "5"


def test_missing_input_is_unknown_not_zero(contract):
    assert "ghost" not in device_points(contract)
    rows = {r["device"]: r for r in csv.DictReader(io.StringIO(budget_csv(contract)))}
    assert rows["ghost"]["typical_w"] == "unknown"
    assert rows["cam"]["max_w"] == "4"
    assert rail_columns(contract)["r5"]["typical"] == pytest.approx(14.0)
