import datetime as dt

import pytest

from lockwire import stress
from lockwire.schema import (
    Actuator,
    Connector,
    ConnectorType,
    Contract,
    DeviceInstance,
    DeviceModel,
    PowerDraw,
    Provenance,
    Quantity,
    Rail,
    RobotInfo,
    ThermalLimit,
    DeviceThermal,
    Topology,
)


def prov(link=None, source="t"):
    return Provenance(
        witness="human", source=source, locked_at=dt.date(2026, 1, 1), compared_from=link
    )


def q(v, u):
    return Quantity(value=v, unit=u)


def rail(id, v, **kw):
    return Rail(id=id, voltage=q(v, "V"), capacity=q(500, "W"), **kw)


def dev(id, model, link=None, rail_id=None, watts=None, **kw):
    power = PowerDraw(rail=rail_id, draw=q(watts, "W")) if rail_id else None
    return DeviceInstance(id=id, model=model, owner="o", power=power, provenance=prov(link), **kw)


def regulator(load_w=10, ambient=True, link="ti.lm2596"):
    limit = ThermalLimit(
        temperature=q(125, "C"), kind="shutdown", provenance=prov(source="https://x.test/ds.pdf")
    )
    thermal = DeviceThermal(ambient=q(25, "C")) if ambient else None
    return Contract(
        robot=RobotInfo(name="t"),
        topology=Topology(
            rails=[
                rail("r12", 12),
                rail("r5", 5, source_device="dev.reg", upstream="r12", efficiency=0.8),
            ]
        ),
        device_models=[
            DeviceModel(id="m.reg", family="f", thermal_limit=limit),
            DeviceModel(id="m.load", family="f"),
        ],
        devices=[
            dev("dev.reg", "m.reg", link, thermal=thermal),
            dev("dev.load", "m.load", rail_id="r5", watts=load_w),
        ],
    )


def by(rows):
    return {r.quantity: round(r.pct) for r in rows}


def test_regulator_rows_current_voltage_junction():
    rows = stress.stress_list(regulator())
    assert by(rows) == {
        "output current (r5)": 67,
        "input voltage (r5)": 30,
        "junction rise (r5)": 125,
    }


def test_sorted_hottest_first():
    pcts = [r.pct for r in stress.stress_list(regulator())]
    assert pcts == sorted(pcts, reverse=True) and pcts[0] > pcts[-1]


def test_over_rating_is_a_sourced_warning_with_the_catalog_source():
    (f,) = stress.check_stress(regulator())
    assert not f.advice_unsourced and "https://" in f.source
    assert "junction rise" in f.observed


def test_between_80_and_100_percent_is_unsourced_advice():
    fs = stress.check_stress(regulator(load_w=13.5))  # 2.7 A of 3 A = 90 %
    (f,) = [f for f in fs if "output current" in f.observed]
    assert f.advice_unsourced and f.source is None


def test_missing_inputs_emit_nothing():
    assert by(stress.stress_list(regulator(ambient=False))) == {
        "output current (r5)": 67,
        "input voltage (r5)": 30,
    }
    assert (
        stress.stress_list(regulator(load_w=0)) != []
        and by(stress.stress_list(regulator(link=None))) == {}
    )
    assert stress.check_stress(regulator(link=None)) == []


def test_part_with_no_rating_on_record_adds_nothing():
    assert stress.stress_list(regulator(link="generic.imu.mems-generic")) == []


def esc_motor(motor_cont, esc="hobbywing.xrotor-pro-40a", link=True):
    act = Actuator(continuous_current_a=motor_cont, peak_current_a=50)
    return Contract(
        robot=RobotInfo(name="t"),
        device_models=[
            DeviceModel(id="m.esc", family="f"),
            DeviceModel(id="m.mot", family="f", actuator=act),
        ],
        devices=[
            dev("dev.esc", "m.esc", esc if link else None, drives_joint="j1"),
            dev("dev.mot", "m.mot", drives_joint="j1"),
        ],
    )


def test_esc_matches_motor_on_continuous_not_peak():
    # 40 A continuous ESC, 30 A continuous motor with a 50 A peak: fine
    assert stress.check_driver_motor_match(esc_motor(30)) == []


def test_esc_below_motor_continuous_is_flagged_with_source():
    (f,) = stress.check_driver_motor_match(esc_motor(45))
    assert "40 A continuous" in f.observed and "https://www.hobbywing.com" in f.source


def test_unlinked_esc_is_silent():
    assert stress.check_driver_motor_match(esc_motor(45, link=False)) == []


def test_driver_voltage_over_rating():
    c = Contract(
        robot=RobotInfo(name="t"),
        topology=Topology(rails=[rail("r12", 12)]),
        device_models=[DeviceModel(id="m", family="f")],
        devices=[dev("dev.drv", "m", "ti.drv8833", rail_id="r12", watts=9)],
    )
    assert by(stress.stress_list(c)) == {"voltage": 111, "current": 50}


def test_connector_current_against_its_declared_rating():
    c = Contract(
        robot=RobotInfo(name="t"),
        topology=Topology(rails=[rail("r12", 12)]),
        connector_types=[ConnectorType(id="xt", rated_current_a=2)],
        device_models=[DeviceModel(id="m", family="f")],
        devices=[
            dev(
                "dev.a",
                "m",
                rail_id="r12",
                watts=36,
                connectors=[Connector(id="p1", type="xt")],
                power_inputs=["p1"],
            )
        ],
    )
    assert by(stress.stress_list(c)) == {"connector p1 current": 150}


def test_registered_callables():
    assert stress.ENGINE_CHECKS == (stress.check_stress, stress.check_driver_motor_match)


def junction(kind, link, eff=0.8):
    c = regulator(link=link)
    r = c.topology.rails[1]
    r.regulator_kind, r.efficiency = kind, eff
    return [x for x in stress.stress_list(c) if x.quantity.startswith("junction")]


def test_linear_junction_uses_vin_minus_vout_not_efficiency():
    (r,) = junction("ldo", "ti.lm2596")  # 12 V -> 5 V at 2 A: 14 W x 50 K/W = 700 K
    assert round(r.value) == 700 and "linear regulator loss (Vin - Vout) x Iout" in r.source


def test_switching_junction_uses_efficiency():
    (r,) = junction("buck", "ti.lm2596")  # 10 W x 0.25 x 50 K/W
    assert round(r.value) == 125 and "switching regulator loss" in r.source


def test_unknown_kind_or_switching_without_efficiency_has_no_junction_row():
    assert junction("flux-capacitor", "ti.lm2596") == []
    assert junction("buck", "ti.lm2596", eff=1.0) == []


def test_linear_junction_adds_vin_times_catalog_quiescent_current():
    (r,) = junction("ldo", "advanced-monolithic.ams1117")  # Iq 5 mA, 90 C/W
    assert r.value == pytest.approx((12 - 5) * 2 * 90 + 12 * 0.005 * 90)  # 1260 K + 5.4 K
