import datetime as dt
import math

import pytest

from lockwire import checks, motor_card
from lockwire.schema import (
    Actuator,
    Contract,
    DeviceModel,
    Link,
    MobileBase,
    ModelRecord,
    Provenance,
    Quantity,
    RobotInfo,
)

KE_100 = 60 / (2 * math.pi * 100)  # Kv 100 rpm/V -> 0.0955 N*m/A


def contract(act=None, base=None, kg=2.0):
    prov = Provenance(witness="human", source="datasheet", locked_at=dt.date(2026, 1, 1))
    model = ModelRecord(
        source="x",
        source_sha256="0",
        structure_hash="0",
        links=[Link(name="body", mass=Quantity(value=kg, unit="kg"))],
        provenance=prov,
    )
    models = [DeviceModel(id="m1", family="f", actuator=act)] if act else []
    return Contract(robot=RobotInfo(name="t"), model=model, device_models=models, base=base)


def kvkt(**kw):
    return motor_card.check_kv_kt(contract(Actuator(kv_rpm_per_v=100, **kw)))


def test_kv_kt_disagree_decisive():
    (f,) = kvkt(torque_constant=0.2)
    assert "Kv and Kt disagree by 2.1x" in f.observed


def test_kv_kt_agree_with_60_over_2pi_factor():
    assert kvkt(torque_constant=KE_100) == []


def test_convention_sine_vs_trap():
    assert kvkt(torque_constant=0.070, kt_convention="sine") == []
    assert len(kvkt(torque_constant=0.070, kt_convention="trap")) == 1


def test_kv_kt_is_advice_never_a_verdict():
    assert kvkt(torque_constant=0.2)[0].advice_unsourced


def test_sine_convention_finding_is_advice_unsourced():
    (f,) = kvkt(torque_constant=0.2, kt_convention="sine")  # sqrt(3)/2 factor has no citation
    assert f.advice_unsourced and "sine convention" in f.expected


def test_no_kv_emits_nothing():
    assert motor_card.check_kv_kt(contract(Actuator(torque_constant=0.2))) == []


def test_encoder_too_slow_for_top_speed():
    a = dict(encoder_cpr=4096, no_load_speed_rad_s=300)  # 195562 counts/s
    assert len(motor_card.check_encoder_rate(contract(Actuator(encoder_max_rate_hz=1e5, **a)))) == 1
    assert motor_card.check_encoder_rate(contract(Actuator(encoder_max_rate_hz=3e5, **a))) == []


def slip(radius, mu=3.5):
    act = Actuator(torque_constant=0.05, stall_current_a=10, gear_ratio=5)
    base = MobileBase(wheel_radius_m=radius, track_width_m=0.2, tyre_friction_mu=mu)
    return motor_card.check_diff_drive_slip(contract(act, base))


def test_diff_drive_slip_uses_radius_not_diameter():
    # push 100 N at r=0.05 vs 68.6 N grip; reading 0.10 (a diameter) as radius gives 50 N
    assert "wheels slip" in slip(0.05)[0].observed
    assert slip(0.10) == []


def test_missing_inputs_emit_nothing():
    empty = Contract(robot=RobotInfo(name="t"))
    for run in motor_card.ENGINE_CHECKS:
        assert run(empty) == []
        assert run(contract(Actuator(), MobileBase(wheel_radius_m=0.05))) == []


def test_tested_on_recorded_files_label():
    assert motor_card.LABEL == "tested on recorded files, not on hardware"
    enc = Actuator(encoder_cpr=4096, no_load_speed_rad_s=300, encoder_max_rate_hz=1e5)
    found = kvkt(torque_constant=0.2) + motor_card.check_encoder_rate(contract(enc)) + slip(0.05)
    assert len(found) == 3
    for f in found:
        assert motor_card.LABEL in f.action
        assert f.witness != motor_card.LABEL


@pytest.mark.skipif("motor_card" not in checks.ENGINE_MODULES, reason="engine module not merged")
def test_runs_through_run_record_checks():
    c = contract(Actuator(kv_rpm_per_v=100, torque_constant=0.2))
    report = checks.run_record_checks(c)
    assert any("Kv and Kt disagree" in f.observed for f in report)
