"""Stage 4b — sample-derived cross-checks (B90).

Most of this stage is honest refusal. ACCEPT asserts distinct reason strings for
each unscorable leg (not a count). Mutation-bite: drop one refusal → red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.preflight.stages import cross_signal
from lockwire_pro.preflight.stages.cross_signal import (
    REASON_INCOMPLETE_ACCEL_TRIO,
    REASON_NO_IMAGE_DATA,
    REASON_NO_REDUNDANCY_DECL,
    REASON_NONSTATIONARY,
    check,
)
from lockwire_pro.recording import DeviceStream, Recording, load_recording
from lockwire.schema import (
    Band,
    ContextEnvelope,
    Contract,
    DeviceInstance,
    FittedBandAllocation,
    FittedBandEvidence,
    Provenance,
    RobotInfo,
    SignalEnvelope,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"


def _source(
    tmp_path: Path,
    *,
    context: str,
    devices: dict[str, DeviceStream],
    units: dict[str, dict[str, str]] | None = None,
) -> RecordedSource:
    if units is None:
        units = {
            device_id: {name: "m/s^2" for name in stream.signals}
            for device_id, stream in devices.items()
        }
    recording = Recording(
        context=context,
        units=units,
        devices=devices,
        env={},
        sha256="synthetic-b90",
        path=tmp_path,
    )
    return RecordedSource(recording)


def _imu_stream(*, accel_z: list[float], accel_x: list[float] | None = None) -> DeviceStream:
    n = len(accel_z)
    signals: dict[str, list[float]] = {
        "accel_x": accel_x if accel_x is not None else [0.0] * n,
        "accel_y": [0.0] * n,
        "accel_z": accel_z,
    }
    return DeviceStream(
        device="dev.imu",
        recv_times=[float(i) * 0.01 for i in range(n)],
        signals=signals,
    )


def test_implemented_flag_replaces_unbuilt_stub():
    assert cross_signal.IMPLEMENTED is True


def test_fitted_residual_band_is_not_a_gravity_witness(tmp_path):
    path = "devices/dev.imu/envelopes/bench-idle/signals/accel_z"
    contract = Contract(
        robot=RobotInfo(name="fitted-gravity"),
        devices=[
            DeviceInstance(
                id="dev.imu",
                model="mdl.imu",
                owner="test",
                envelopes=[
                    ContextEnvelope(
                        context="bench-idle",
                        signals={
                            "accel_z": SignalEnvelope(band=Band(lo=-0.2, hi=0.2, unit="m/s^2"))
                        },
                        fitted_bands={
                            "signals/accel_z": FittedBandEvidence(
                                rank=1,
                                n_eff=100,
                                alpha=0.005,
                            )
                        },
                        provenance=Provenance(
                            witness="live",
                            source="known-good",
                            locked_at="2026-07-29",
                            bag_sha256="a" * 64,
                        ),
                    )
                ],
            )
        ],
        fitted_band_allocations=[
            FittedBandAllocation(
                context="bench-idle",
                channels=["device/dev.imu/accel_z"],
                fitted_bands=[path],
                denominator=1,
            )
        ],
    )
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={"dev.imu": _imu_stream(accel_z=[-9.81, -9.80, -9.82, -9.79])},
    )

    findings = check(contract, source)
    gravity = [finding for finding in findings if "stationary_|g|" in finding.quantity]
    assert len(gravity) == 1
    assert gravity[0].severity == "not_scored"
    assert "machine-fitted residual limits" in gravity[0].observed
    assert "asserted" in gravity[0].expected


def test_clean_bag_emits_distinct_not_scored_reasons_for_each_unscorable_leg():
    """Clean fixture is nominal-op: all three legs refuse, each with its own reason."""
    contract = load_contract(CLEAN_CONTRACT)
    source = RecordedSource(load_recording(CLEAN_BAG))
    findings = check(contract, source)

    assert findings, "stage must never return []"
    assert all(f.severity == "not_scored" for f in findings)

    observed = " | ".join(f.observed for f in findings)
    assert REASON_NONSTATIONARY in observed
    assert REASON_NO_REDUNDANCY_DECL in observed
    assert REASON_NO_IMAGE_DATA in observed

    # Distinct legs — mutation-bite target: silently skipping one reddens these.
    assert any("stationary_|g|" in f.quantity for f in findings)
    assert any(f.quantity.endswith("redundant_sensors") for f in findings)
    assert any(f.quantity.endswith("camera_histogram") for f in findings)


def test_nonstationary_gating_is_condition_guard_not_coverage_gap(tmp_path):
    """nominal-op → not_scored naming the guard; docstring/expected call it correct design."""
    contract = load_contract(CLEAN_CONTRACT)
    source = _source(
        tmp_path,
        context="nominal-op",
        devices={"dev.imu": _imu_stream(accel_z=[-9.81, -9.80, -9.82])},
    )
    findings = check(contract, source)
    g_findings = [f for f in findings if "stationary_|g|" in f.quantity]
    assert len(g_findings) == 1
    assert REASON_NONSTATIONARY in g_findings[0].observed
    assert "correct design" in g_findings[0].expected
    assert "coverage gap" in g_findings[0].expected


def test_bench_idle_scores_g_within_envelope_without_violation(tmp_path):
    contract = load_contract(CLEAN_CONTRACT)
    # Envelope accel_z [-10.5, -9.0] → |g| band [9.0, 10.5]; samples near 9.81 pass.
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={"dev.imu": _imu_stream(accel_z=[-9.81, -9.80, -9.82, -9.79])},
    )
    findings = check(contract, source)
    assert not any(f.severity == "violation" for f in findings)
    assert not any("stationary_|g|" in f.quantity for f in findings)
    # Refusal legs still speak — stage never silent.
    assert any(REASON_NO_REDUNDANCY_DECL in f.observed for f in findings)
    assert any(REASON_NO_IMAGE_DATA in f.observed for f in findings)


def test_armed_idle_is_stationary_and_can_violate(tmp_path):
    contract = load_contract(CLEAN_CONTRACT)
    device = contract.device("dev.imu")
    assert device is not None
    # CLEAN fixture only locks bench-idle. Tolerance is envelope-derived, so
    # armed-idle must carry its own accel band — tight enough that |g|≈5
    # falls outside (band [-10.5, -9.0] → |g| ∈ [9.0, 10.5]).
    armed = device.envelopes[0].model_copy(deep=True)
    armed.context = "armed-idle"
    device.envelopes.append(armed)
    source = _source(
        tmp_path,
        context="armed-idle",
        devices={"dev.imu": _imu_stream(accel_z=[-5.0, -5.1, -4.9])},
    )
    findings = check(contract, source)
    violations = [f for f in findings if f.severity == "violation"]
    assert len(violations) == 1
    assert violations[0].quantity == "devices/dev.imu/stationary_|g|"
    assert violations[0].observed_quantity is not None
    assert violations[0].observed_quantity.value == pytest.approx(5.0)
    assert violations[0].observed_quantity.unit == "m/s^2"
    assert violations[0].expected_quantity is not None
    assert violations[0].expected_quantity.value == pytest.approx(9.81)
    assert violations[0].expected_quantity.unit == "m/s^2"


def test_single_axis_accel_is_not_scored_never_fake_magnitude(tmp_path):
    """Incomplete trio must not report subset |axis| under the |g| check's name.

    Reviewer demo: single-axis stationary ≈0.03 against band [9, 10.5] was a
    false-NO-GO violation. Healthy robot, no author remedy — refuse instead.
    """
    contract = load_contract(CLEAN_CONTRACT)
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={
            "dev.imu": DeviceStream(
                device="dev.imu",
                recv_times=[0.0, 0.01, 0.02],
                signals={"accel_x": [0.03, 0.02, 0.04]},
            )
        },
        units={"dev.imu": {"accel_x": "m/s^2"}},
    )
    findings = check(contract, source)
    g = next(f for f in findings if "stationary_|g|" in f.quantity)
    assert g.severity == "not_scored"
    assert REASON_INCOMPLETE_ACCEL_TRIO in g.observed
    assert "accel_x" in g.observed
    assert not any(f.severity == "violation" for f in findings)
    # Refusal legs still speak — stage never silent.
    assert any(REASON_NO_REDUNDANCY_DECL in f.observed for f in findings)
    assert any(REASON_NO_IMAGE_DATA in f.observed for f in findings)


def test_stationary_without_envelope_is_not_scored(tmp_path):
    contract = load_contract(CLEAN_CONTRACT)
    device = contract.device("dev.imu")
    assert device is not None
    device.envelopes.clear()
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={"dev.imu": _imu_stream(accel_z=[-9.81, -9.80])},
    )
    findings = check(contract, source)
    g = next(f for f in findings if "stationary_|g|" in f.quantity)
    assert g.severity == "not_scored"
    assert "no device envelope" in g.observed


def test_stationary_without_matching_contract_device_is_not_scored(tmp_path):
    contract = load_contract(CLEAN_CONTRACT)
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={
            "dev.imu.unknown": DeviceStream(
                device="dev.imu.unknown",
                recv_times=[0.0, 0.01],
                signals={"accel_z": [-9.81, -9.80]},
            )
        },
        units={"dev.imu.unknown": {"accel_z": "m/s^2"}},
    )
    findings = check(contract, source)
    g = next(f for f in findings if "stationary_|g|" in f.quantity)
    assert g.severity == "not_scored"
    assert "no matching contract device" in g.observed


def test_stage_never_returns_empty_on_error_shaped_inputs(tmp_path):
    """No accel + stationary still yields findings (refusals + |g| gap)."""
    contract = load_contract(CLEAN_CONTRACT)
    source = _source(
        tmp_path,
        context="bench-idle",
        devices={
            "dev.baro": DeviceStream(
                device="dev.baro",
                recv_times=[0.0, 0.1],
                signals={"pressure_pa": [101325.0, 101330.0]},
            )
        },
        units={"dev.baro": {"pressure_pa": "Pa"}},
    )
    findings = check(contract, source)
    assert findings
    assert any("no accelerometer samples" in f.observed for f in findings)
    assert any(REASON_NO_REDUNDANCY_DECL in f.observed for f in findings)
    assert any(REASON_NO_IMAGE_DATA in f.observed for f in findings)


def test_redundancy_and_histogram_actions_are_plain_operator_sentences():
    """G2 audit 2 §5: the redundant-sensors and camera-histogram refusals told
    the operator to "await B94 schema declaration" / "await B95 format work"
    — a filed-box number and a roadmap milestone leaking into user-facing
    output. Say what's missing in plain words instead. Mutation bite:
    reverting to the box-number wording turns this red.
    """
    contract = load_contract(CLEAN_CONTRACT)
    source = RecordedSource(load_recording(CLEAN_BAG))
    findings = check(contract, source)

    redundancy = next(f for f in findings if f.quantity.endswith("redundant_sensors"))
    histogram = next(f for f in findings if f.quantity.endswith("camera_histogram"))
    for finding in (redundancy, histogram):
        for field in (finding.expected, finding.action):
            assert "B94" not in field
            assert "B95" not in field
            assert "filed box" not in field
            assert "await" not in field.lower()


def test_band_straddling_zero_keeps_a_healthy_gravity_in_band(tmp_path):
    """accel_z (-11, 11) gives |g| band [0, 11], so 9.81 is not a violation."""
    contract = load_contract(CLEAN_CONTRACT)
    imu = contract.device("dev.imu")
    for env in imu.envelopes:
        if env.context == "bench-idle":
            env.signals["accel_z"].band = Band(lo=-11.0, hi=11.0, unit="m/s^2")
    source = _source(
        tmp_path, context="bench-idle", devices={"dev.imu": _imu_stream(accel_z=[-9.81, -9.81])}
    )
    assert not any(f.severity == "violation" for f in check(contract, source))
