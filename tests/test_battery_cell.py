"""Battery cell model (src/lockwire/battery.py). Fixture data only: the feature is
tested on recorded files, not on hardware. (tests/test_battery.py is the Pro Battery Book.)"""

import datetime as dt

import pytest

from lockwire import battery, checks
from lockwire.schema import CapacityDerating, OcvPoint, Provenance, Quantity, load_contract

from conftest import FIXTURES

# Fixture cell A (fictional, numbers are fixture values, no datasheet): nominal 3.6 V,
# 18 mOhm resistance standing in for R0. 6S2P pack, 20 A:
#   R_pack = 18 mOhm * 6 / 2 = 54 mOhm; sag = 20 A * 0.054 Ohm = 1.08 V.
CELL_V, CELL_R_MOHM = 3.6, 18.0


def _contract(rail_min=17.2, sourced=True, curve=True, r0=54.0, derating=0.65):
    c = load_contract(FIXTURES / "contract_clean.yaml")
    rail = next(r for r in c.topology.rails if r.id == "rail.22v")
    rail.upstream = "battery"
    rail.voltage_range = {
        "min": Quantity(value=rail_min, unit="V"),
        "max": Quantity(value=26.0, unit="V"),
    }
    if sourced:
        rail.provenance = Provenance(
            witness="human", source="rail datasheet p3", locked_at=dt.date(2026, 11, 1)
        )
    b = c.topology.battery
    b.internal_resistance_mohm = r0
    b.reserve_pct = 10
    b.derating = [CapacityDerating(below_c=0, capacity_factor=derating)]
    if curve:
        b.ocv_curve = [
            OcvPoint(soc_pct=s, voltage_v=v) for s, v in ((0, 18.0), (50, 21.6), (100, 25.2))
        ]
    return c


@pytest.fixture(autouse=True)
def twenty_amps(monkeypatch):
    monkeypatch.setattr(checks, "_pack_amps", lambda c, nominal: (20.0, "nominal", set()))


def test_sag_at_20a_matches_cited_cell_hand_calc():
    _, r_pack = battery.pack_from_cell(CELL_V, CELL_R_MOHM, 6, 2)
    assert battery.sag_v(20.0, r_pack) == pytest.approx(1.08, rel=0.02)


def test_rc_branch_settles_to_r0_plus_r1_and_is_zero_at_t0():
    assert battery.sag_v(20.0, 54.0, 10.0, 2000.0) == pytest.approx(1.28)
    assert battery.sag_v(20.0, 54.0, 10.0, 2000.0, t_s=0.0) == pytest.approx(1.08)


def test_cold_factor_scales_resistance_not_just_capacity():
    assert battery.corner(54.0, 10.0, capacity_factor=0.5) == (108.0, 20.0)
    assert battery.corner(54.0, resistance_growth=1.5) == (81.0, None)


def test_ocv_interpolates_and_clamps():
    pts = _contract().topology.battery.ocv_curve
    assert battery.ocv_at(pts, 25) == pytest.approx(19.8)
    assert battery.ocv_at(pts, 150) == 25.2


def test_cold_corner_fires_where_nominal_passes():
    (f,) = battery.check_cell_sag(_contract())
    assert f.severity == "warning" and "cold corner" in f.observed
    assert battery.check_cell_sag(_contract(rail_min=16.9)) == []


def test_nominal_corner_fires_first_and_names_the_battery_curve():
    (f,) = battery.check_cell_sag(_contract(rail_min=19.0))
    assert "nominal corner" in f.observed and f.quantity == "topology/battery/ocv_curve"


def test_missing_input_emits_nothing():
    assert battery.check_cell_sag(_contract(curve=False)) == []
    assert battery.check_cell_sag(_contract(r0=None)) == []


def test_unsourced_rail_minimum_is_advice_sourced_is_cited_warning():
    (adv,) = battery.check_cell_sag(_contract(sourced=False))
    assert adv.advice_unsourced and adv.source is None
    (src,) = battery.check_cell_sag(_contract())
    assert not src.advice_unsourced and src.source == "rail datasheet p3"


def test_finding_carries_the_recorded_files_label():
    (f,) = battery.check_cell_sag(_contract())
    assert (
        battery.LABEL == "tested on recorded files, not on hardware" and battery.LABEL in f.action
    )


@pytest.mark.skipif("battery" not in checks.ENGINE_MODULES, reason="engine module not landed")
def test_run_record_checks_runs_the_cell_check():
    report = checks.run_record_checks(_contract())
    assert any(f.quantity == "topology/battery/ocv_curve" for f in report)


# ---- pack builder and reserve ----------------------------------------------------
# Fixture cell B (fictional, numbers are fixture values, no datasheet): 3.6 V nominal, 5.0 Ah,
# 69 g, 9.8 A max continuous, 35 mOhm. 4S2P by hand: 14.4 V, 10 Ah, 144 Wh, 8 x 69 g = 0.552 kg,
# 2 x 9.8 = 19.6 A, 35 mOhm x 4 / 2 = 70 mOhm.
FIXTURE_CELL_B = battery.Cell(3.6, 5.0, 0.069, 9.8, 35.0)


def test_4s2p_of_a_fixture_cell_b_gives_hand_numbers():
    p = battery.build_pack(FIXTURE_CELL_B, 4, 2)
    assert (p.voltage_v, p.capacity_ah, p.max_a, p.r_mohm) == pytest.approx(
        (14.4, 10.0, 19.6, 70.0)
    )
    assert p.mass_kg == pytest.approx(0.552) and p.energy_wh == pytest.approx(144.0)


def _plan_contract(reserve=10, watts=60.0, hours=1.5, duration=True):
    from lockwire.plan import Plan, Segment

    c = _contract()
    c.topology.battery.reserve_pct = reserve
    if watts is not None:
        c.device("dev.radio.rfd900").power.draw = Quantity(value=watts, unit="W")
    seg = Segment(
        duration_s=hours * 3600 if duration else None, device_modes={"dev.radio.rfd900": 1.0}
    )
    return c, Plan(segments=[seg])


def test_reserve_fires_when_plan_eats_the_reserve_once():
    # 99.9 Wh x (1 - 10%) = 89.91 Wh usable; plan needs 60 W x 1.5 h = 90 Wh.
    c, plan = _plan_contract()
    (f,) = battery.check_reserve(c, plan=plan)
    assert f.severity == "warning" and f.observed_quantity.value == pytest.approx(90.0)
    assert f.expected_quantity.value == pytest.approx(89.91)


def test_reserve_silent_when_plan_fits_after_one_reserve():
    # 9% reserve: 90.909 Wh usable >= 90 Wh needed (reserve applied twice would be 82.7 and fire).
    c, plan = _plan_contract(reserve=9)
    assert battery.check_reserve(c, plan=plan) == []


def test_reserve_missing_input_emits_nothing():
    for kw in ({"reserve": None}, {"watts": None}, {"duration": False}):
        c, plan = _plan_contract(**kw)
        if kw.get("watts", 1) is None:
            c.device("dev.radio.rfd900").power.draw = None
        assert battery.check_reserve(c, plan=plan) == []
    assert battery.check_reserve(_plan_contract()[0]) == []


def test_reserve_finding_carries_label_in_action_not_witness():
    c, plan = _plan_contract()
    (f,) = battery.check_reserve(c, plan=plan)
    assert battery.LABEL in f.action and f.witness is None and f.advice_unsourced


def test_reserve_is_registered_as_an_engine_check():
    assert battery.check_reserve in battery.ENGINE_CHECKS
