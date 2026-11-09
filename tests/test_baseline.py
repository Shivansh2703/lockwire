"""End-to-end baseline generation and planted-bag acceptance tests."""

import hashlib
import json
import math
import re
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from lockwire_pro.baseline import (
    _cluster_allocation,
    build_baseline,
    check_recording,
    parse_baseline_review,
)
from lockwire.cli import app
from lockwire_pro.recording import DeviceStream, Recording, load_recording
from lockwire.schema import Band
from lockwire_pro.stats.clusters import ALPHA_GATE, PhysicalCheck, familywise_allocation
from lockwire_pro.stats.drift import (
    cubic_detrend,
    drift_decision_interval,
    trend_cusum_statistic,
)
from lockwire_pro.stats.neff import effective_sample_size
from lockwire_pro.ulog import load_ulog

from conftest import plain, strip_ansi

BAGS = Path(__file__).parent / "fixtures" / "bags"
REAL_FLIGHT = (
    Path(__file__).parent / "fixtures" / "ulog" / "18a01095-f04e-4801-a9f3-4040190fa3bb.ulg"
)
LABELED_FLIGHTS = Path(__file__).parent / "fixtures" / "ulog"


def _manifest(bag_id: str) -> dict:
    return YAML(typ="safe").load((BAGS / bag_id / "manifest.yaml").read_text())


def _fallbacks_from_ground_truth():
    signal_limits: dict[tuple[str, str], Band] = {}
    condition_limits: dict[str, Band] = {}
    nominal_rates: dict[str, float] = {}
    expected: dict[str, tuple[str, tuple[str, ...]]] = {}

    # Preflight demo bags (preflight_*) are gated by test_preflight_demo / the
    # preflight runner, not this baseline checker. Their failure types include
    # stationary-g-excursion under bench-idle, which cannot be scored against
    # the nominal-op clean baseline.
    for path in sorted(BAGS.iterdir()):
        if not path.is_dir() or path.name in {"clean", "clean_b", "__pycache__"}:
            continue
        if path.name.startswith("preflight_"):
            continue
        failure = _manifest(path.name)["planted_failures"][0]
        target_ids: list[str] = []
        for target in failure["targets"]:
            channel = target["channel"]
            reference = target["magnitude"]["reference"]
            unit = {"degC": "C", "Pa": "hPa"}.get(reference["unit"], reference["unit"])
            value = reference["value"] * (0.01 if reference["unit"] == "Pa" else 1.0)
            if channel["kind"] == "environment":
                target_ids.append(f"environment/{channel['name']}")
                condition_limits[channel["name"]] = Band.model_construct(
                    lo=-math.inf,
                    hi=value,
                    unit=unit,
                )
            else:
                target_ids.append(f"device/{channel['device']}/{channel['name']}")
                if failure["type"] == "range-excursion":
                    signal_limits[(channel["device"], channel["name"])] = Band.model_construct(
                        lo=-math.inf,
                        hi=value,
                        unit=unit,
                    )
                elif failure["type"] == "rate-collapse":
                    nominal_rates[channel["device"]] = reference["value"]

        mechanism = {
            "condition-envelope-exit": "condition-envelope",
            "frozen-but-publishing": "cusum",
            "range-excursion": "hard-limit-exceedance",
            "rate-collapse": "rate-check",
        }[failure["type"]]
        expected[path.name] = (mechanism, tuple(target_ids))

    return signal_limits, condition_limits, nominal_rates, expected


@pytest.fixture(scope="module")
def clean_baseline():
    recording = load_recording(BAGS / "clean")
    return build_baseline(
        recording,
        context=recording.context,
        known_good_rationale=(
            "Clean synthetic capture generated from the checked-in healthy channel models."
        ),
        operator="automated fixture generator",
        setup="synthetic bench-idle fixture",
        firmware="not applicable — synthetic fixture",
        calibration="generator parameters recorded in manifest.yaml",
        dropped_messages=0,
        recording_completeness="all generated rows were written",
    )


def test_real_flight_exercises_designed_regime():
    recording = load_ulog(REAL_FLIGHT)
    baseline = build_baseline(recording, context=recording.context)
    profile = baseline.profiles["device/px4.sensor_combined.0/accel_z"]

    assert profile.band is not None
    assert profile.n == 600
    assert 446.0 <= profile.n_eff <= 448.0
    assert profile.n_eff >= 93
    # This must fail if the stack regresses to clamp-domination (n_eff == n):
    # the real flight has to earn its evidence through autocorrelation analysis.
    assert profile.n_eff < profile.n
    assert profile.decorrelation == 2


def test_labeled_failures_pin_measured_within_flight_detection_record():
    cases = {
        "bff22af6-8b5f-4945-bc49-f17baeedbf72": {
            "label": "6",
            "sizes": {"baseline.ulg": 244_648, "failure.ulg": 154_344},
            "shas": {
                "baseline.ulg": (
                    "4bbef67ff4f5c2cd3ecded29619c1afb0279c40ca085ca1cc79b1ed1f342e659"
                ),
                "failure.ulg": ("a51d665d4a6b5c7d5e5bcbd82efba709c944e7c9faed81cc45a0c80a23c8b983"),
            },
        },
        "1b8e43d5-04d8-4130-8227-628898f319c5": {
            "label": "2",
            "sizes": {"baseline.ulg": 144_184, "failure.ulg": 192_400},
            "shas": {
                "baseline.ulg": (
                    "8da50b95e0e00612c501385dd443c92440530fb6aa2863305e232fb50fa23878"
                ),
                "failure.ulg": ("6e184bacd6eebb55678095d6ea6774f68b34197da77e6ff51d8040218857d70a"),
            },
        },
    }
    measured = {}
    for log_id, case in cases.items():
        fixture_dir = LABELED_FLIGHTS / log_id
        metadata = json.loads((fixture_dir / "metadata.json").read_text())
        assert tuple(str(x) for x in metadata["error_labels"]) == (case["label"],)

        for fixture_name, expected_size in case["sizes"].items():
            fixture = fixture_dir / fixture_name
            payload = fixture.read_bytes()
            assert len(payload) == expected_size
            assert hashlib.sha256(payload).hexdigest() == case["shas"][fixture_name]

        baseline_recording = load_ulog(fixture_dir / "baseline.ulg")
        failure_recording = load_ulog(fixture_dir / "failure.ulg")
        baseline = build_baseline(baseline_recording, context=baseline_recording.context)
        findings = check_recording(failure_recording, baseline)
        measured[case["label"]] = {
            "findings": tuple((finding.mechanism, finding.channels) for finding in findings),
            "baseline_refusals": tuple(
                channel_id
                for channel_id, profile in baseline.profiles.items()
                if profile.status == "under-sampled"
            ),
        }

    # These are within-flight comparisons, not Holybro-X500-vs-other-airframe
    # comparisons. Label 6 uses leading 0–20 s vs 25–35 s: the full source's
    # first calibration-failure sequence begins at 28.212 s. Label 2 uses
    # leading 0–8 s vs 9–21 s: "Auto activation failed" occurs at 9.298 s.
    # The sidecars name only numeric labels, so no failure semantics are invented.
    # Caveat: label 6 is a never-armed ground/commissioning log whose vehicle
    # status is already failsafe throughout the selected physical-data timeline.
    # Label 2's sidecar also changes flight mode at the 9 s split, so its channel
    # findings are evidence of detection in the labeled segment, not evidence that
    # the mechanisms semantically identified the numeric label.
    expected = {
        "6": {
            "findings": (),
            "baseline_refusals": (
                "rate/px4.sensor_accel.0",
                "device/px4.sensor_accel.0/accel_x",
                "device/px4.sensor_accel.0/accel_y",
                "device/px4.sensor_accel.0/accel_z",
                "device/px4.sensor_accel.0/temperature",
                "rate/px4.sensor_baro.0",
                "device/px4.sensor_baro.0/pressure_pa",
                "device/px4.sensor_baro.0/temperature",
                "rate/px4.sensor_gyro.0",
                "device/px4.sensor_gyro.0/gyro_x",
                "device/px4.sensor_gyro.0/gyro_y",
                "device/px4.sensor_gyro.0/gyro_z",
                "device/px4.sensor_gyro.0/temperature",
            ),
        },
        "2": {
            "findings": (
                (
                    "wilks-band-exceedance",
                    ("device/px4.sensor_combined.0/accel_x",),
                ),
                (
                    "wilks-band-exceedance",
                    ("device/px4.sensor_combined.0/accel_z",),
                ),
                ("drift-cusum", ("device/px4.sensor_combined.0/gyro_y",)),
                ("drift-cusum", ("device/px4.sensor_combined.0/gyro_z",)),
            ),
            "baseline_refusals": (),
        },
    }

    # What would have to differ for this test to fail is the detection record
    # itself: losing a pinned finding OR gaining one for a pinned miss, including
    # a change to which baseline channels refuse a distribution-free envelope.
    assert measured == expected
    caught = tuple(
        f"PX4 error_label {label}" for label, record in measured.items() if record["findings"]
    )
    missed = tuple(
        f"PX4 error_label {label}" for label, record in measured.items() if not record["findings"]
    )
    assert caught == ("PX4 error_label 2",)
    assert missed == ("PX4 error_label 6",)


def _per_instance_allocation(recording):
    checks = []
    for device, stream in sorted(recording.devices.items()):
        path = (("host", f"device:{device}"),)
        checks.append(PhysicalCheck(id=f"rate/{device}", paths=path))
        checks.extend(
            PhysicalCheck(id=f"device/{device}/{name}", paths=path)
            for name in sorted(stream.signals)
        )
    return familywise_allocation(checks, alpha_gate=ALPHA_GATE)


def _simulated_redundant_sensor_fwer(recording, allocation, *, trials=500_000):
    rng = np.random.default_rng(20260726)
    common: dict[tuple[str, str], np.ndarray] = {}
    alarms = np.zeros(trials, dtype=bool)
    alpha_by_check = {
        check_id: cluster.alpha / len(cluster.check_ids)
        for cluster in allocation.clusters
        for check_id in cluster.check_ids
    }

    for check_id, alpha in sorted(alpha_by_check.items()):
        if check_id.startswith("rate/"):
            device = check_id.removeprefix("rate/")
            check_name = "rate"
        else:
            _, device, check_name = check_id.split("/", 2)
        match = re.fullmatch(
            r"(?P<host>.+)\.(?P<family>sensor_(?:accel|gyro|baro))\.\d+",
            device,
        )
        independent = rng.standard_normal(trials)
        if match is None:
            statistic = independent
        else:
            correlation_key = (f"{match['host']}.{match['family']}", check_name)
            if correlation_key not in common:
                common[correlation_key] = rng.standard_normal(trials)
            shared = common[correlation_key]
            statistic = math.sqrt(0.9) * shared + math.sqrt(0.1) * independent
        critical = NormalDist().inv_cdf(1.0 - alpha)
        alarms |= statistic >= critical

    return float(np.mean(alarms))


def test_redundant_sensor_instances_share_physical_quantity_cluster_on_real_flight():
    recording = load_ulog(REAL_FLIGHT)
    before = _per_instance_allocation(recording)
    after = _cluster_allocation(recording)

    assert recording.source == "ulog"
    # A regression to per-instance keying makes `after` identical to `before`: its
    # cluster count returns to 9 and its measured FWER returns to the before value.
    assert len(recording.devices) == 9
    assert len(before.clusters) == 9
    assert len(after.clusters) == 4
    assert {cluster.paths for cluster in after.clusters} == {
        (("host", "quantity:px4.sensor_accel"),),
        (("host", "quantity:px4.sensor_baro"),),
        (("host", "device:px4.sensor_combined.0"),),
        (("host", "quantity:px4.sensor_gyro"),),
    }

    before_fwer = _simulated_redundant_sensor_fwer(recording, before)
    after_fwer = _simulated_redundant_sensor_fwer(recording, after)
    assert before_fwer == pytest.approx(0.003810, abs=0.00001)
    assert after_fwer == pytest.approx(0.003996, abs=0.00001)
    assert after_fwer > before_fwer
    assert after_fwer <= ALPHA_GATE


def test_provenance_merge_does_not_merge_hand_authored_px4_shaped_devices(tmp_path):
    recording = Recording(
        context="bench-idle",
        units={
            "arm.sensor_accel.0": {"accel_x": "m/s^2"},
            "arm.sensor_accel.1": {"accel_x": "m/s^2"},
        },
        devices={
            "arm.sensor_accel.0": DeviceStream(
                device="arm.sensor_accel.0",
                recv_times=[0.0, 1.0],
                signals={"accel_x": [0.0, 1.0]},
            ),
            "arm.sensor_accel.1": DeviceStream(
                device="arm.sensor_accel.1",
                recv_times=[0.0, 1.0],
                signals={"accel_x": [1.0, 0.0]},
            ),
        },
        env={},
        sha256="hand-authored-independent-hardware",
        path=tmp_path,
    )

    allocation = _cluster_allocation(recording)

    assert recording.source is None
    assert len(allocation.clusters) == 2
    assert {cluster.paths for cluster in allocation.clusters} == {
        (("host", "device:arm.sensor_accel.0"),),
        (("host", "device:arm.sensor_accel.1"),),
    }


def test_non_instanced_device_keeps_its_existing_cluster_key(tmp_path):
    recording = Recording(
        context="bench-idle",
        units={"dev.imu": {"accel_z": "m/s^2"}},
        devices={
            "dev.imu": DeviceStream(
                device="dev.imu",
                recv_times=[0.0, 1.0],
                signals={"accel_z": [-9.8, -9.8]},
            )
        },
        env={},
        sha256="synthetic",
        path=tmp_path,
    )

    allocation = _cluster_allocation(recording)

    assert len(allocation.clusters) == 1
    assert allocation.clusters[0].paths == (("host", "device:dev.imu"),)


def test_wilks_ranks_index_the_decorrelated_population(tmp_path):
    rate_count = 1499
    rng = np.random.default_rng(58)
    innovations = rng.normal(size=rate_count)
    rate_noise = np.empty(rate_count)
    rate_noise[0] = innovations[0]
    for index in range(1, rate_count):
        rate_noise[index] = 0.42 * rate_noise[index - 1] + innovations[index]
    rates = 50.0 + rate_noise
    recv_times = [0.0]
    for rate in rates:
        recv_times.append(recv_times[-1] + 1.0 / rate)

    recording = Recording(
        context="bench-idle",
        units={"dev.barometer": {}},
        devices={
            "dev.barometer": DeviceStream(
                device="dev.barometer",
                recv_times=recv_times,
            )
        },
        env={},
        sha256="synthetic",
        path=tmp_path,
    )

    baseline = build_baseline(recording, context=recording.context)

    rate = baseline.profiles["rate/dev.barometer"]
    assert rate.n == rate_count
    assert rate.effective_n == math.ceil(rate.n / rate.decorrelation)
    assert rate.status == "locked"
    assert rate.band is not None


def test_same_signal_name_resolves_units_per_device(tmp_path):
    recording = Recording(
        context="bench-idle",
        units={
            "dev.meters": {"position": "m"},
            "dev.centimeters": {"position": "cm"},
        },
        devices={
            "dev.meters": DeviceStream(
                device="dev.meters",
                recv_times=[0.0, 1.0],
                signals={"position": [1.0, 1.01]},
            ),
            "dev.centimeters": DeviceStream(
                device="dev.centimeters",
                recv_times=[0.0, 1.0],
                signals={"position": [100.0, 101.0]},
            ),
        },
        env={},
        sha256="synthetic",
        path=tmp_path,
    )

    baseline = build_baseline(recording, context=recording.context)

    assert baseline.profiles["device/dev.meters/position"].unit == "m"
    assert baseline.profiles["device/dev.centimeters/position"].unit == "cm"
    assert (
        check_recording(
            recording,
            baseline,
            signal_hard_limits={
                ("dev.meters", "position"): Band(lo=0.9, hi=1.1, unit="m"),
                ("dev.centimeters", "position"): Band(lo=90.0, hi=110.0, unit="cm"),
            },
        )
        == []
    )


def test_signal_envelopes_use_cubic_residual_and_recover_external_neff_anchor(clean_baseline):
    residual_imu = clean_baseline.profiles["device/dev.imu/accel_z"]
    residual_barometer = clean_baseline.profiles["device/dev.barometer/pressure_pa"]
    imu_rate = clean_baseline.profiles["rate/dev.imu"]

    assert residual_imu.n == 6000
    # External measurement: 616.6, independently measured before this code existed.
    assert 550.0 < residual_imu.n_eff < 700.0
    assert residual_imu.status == "locked"
    assert residual_imu.band is not None
    assert "accel_z" in clean_baseline.envelopes["dev.imu"].signals

    assert residual_barometer.n == 1500
    assert residual_barometer.n_eff < 93
    assert residual_barometer.status == "under-sampled"
    assert residual_barometer.band is None

    assert imu_rate.n_eff > 93
    assert imu_rate.status == "locked"
    assert imu_rate.band is not None


def _signal_recording(tmp_path, values, *, sample_rate_hz=200.0):
    samples = np.asarray(values, dtype=float)
    return Recording(
        context="bench-idle",
        units={"dev.sensor": {"value": "m"}},
        devices={
            "dev.sensor": DeviceStream(
                device="dev.sensor",
                recv_times=(np.arange(samples.size) / sample_rate_hz).tolist(),
                signals={"value": samples.tolist()},
            )
        },
        env={},
        sha256=f"synthetic-{samples.size}-{sample_rate_hz}",
        path=tmp_path,
    )


def test_white_noise_band_is_unchanged_and_drift_check_is_silent(tmp_path):
    baseline_noise = np.random.default_rng(410).normal(size=1200)
    control_noise = np.random.default_rng(411).normal(size=1200)
    baseline = build_baseline(
        _signal_recording(tmp_path, baseline_noise),
        context="bench-idle",
    )
    profile = baseline.profiles["device/dev.sensor/value"]

    assert profile.band is not None
    assert profile.rank_from_edge is not None
    raw_ordered = np.sort(baseline_noise[:: profile.decorrelation])
    assert profile.band.lo == pytest.approx(
        float(raw_ordered[profile.rank_from_edge - 1]),
        abs=0.3,
    )
    assert profile.band.hi == pytest.approx(
        float(raw_ordered[-profile.rank_from_edge]),
        abs=0.3,
    )
    assert check_recording(_signal_recording(tmp_path, control_noise), baseline) == []


def test_nan_sample_is_reported_and_never_scored_as_pass(tmp_path):
    noise = np.random.default_rng(413).normal(size=1200)
    baseline = build_baseline(_signal_recording(tmp_path, noise), context="bench-idle")
    nan_values = noise.copy()
    nan_values[5] = np.nan
    nan_values[6] = np.inf

    findings = check_recording(_signal_recording(tmp_path, nan_values), baseline)
    assert [(f.mechanism, f.channels) for f in findings] == [
        ("non-finite-sample", ("device/dev.sensor/value",))
    ]
    assert "2 of 1200" in findings[0].detail

    limit = {("dev.sensor", "value"): Band(lo=-100.0, hi=100.0, unit="m")}
    hard = check_recording(
        _signal_recording(tmp_path, nan_values), baseline, signal_hard_limits=limit
    )
    assert [f.mechanism for f in hard] == ["non-finite-sample"]
    all_nan = check_recording(
        _signal_recording(tmp_path, np.full(1200, np.nan)), baseline, signal_hard_limits=limit
    )
    assert [f.mechanism for f in all_nan] == ["non-finite-sample"]


def test_planted_slow_drift_is_named_while_residual_envelope_stays_tight(tmp_path):
    noise = np.random.default_rng(412).normal(scale=0.2, size=1200)
    baseline = build_baseline(
        _signal_recording(tmp_path, noise),
        context="bench-idle",
    )
    planted = noise + np.linspace(-8.0, 8.0, noise.size)

    profile = baseline.profiles["device/dev.sensor/value"]
    findings = check_recording(_signal_recording(tmp_path, planted), baseline)

    assert profile.band is not None
    assert profile.band.hi - profile.band.lo < 1.5
    assert np.ptp(planted) > 15.0
    assert [(finding.mechanism, finding.channels) for finding in findings] == [
        ("drift-cusum", ("device/dev.sensor/value",))
    ]


@pytest.mark.parametrize(
    ("sample_count", "sample_rate_hz"),
    [(600, 100.0), (1200, 200.0), (1200, 100.0)],
)
def test_drift_error_budget_is_sample_rate_and_capture_length_invariant(
    sample_count,
    sample_rate_hz,
):
    alpha_channel = 0.0025
    baseline_noise = np.random.default_rng(413).normal(scale=0.2, size=sample_count)
    control_noise = np.random.default_rng(414).normal(scale=0.2, size=sample_count)
    baseline_split = cubic_detrend(baseline_noise)
    control_split = cubic_detrend(control_noise)
    residual_decorrelation = math.ceil(
        sample_count / effective_sample_size(baseline_split.residual)
    )
    decision_interval = drift_decision_interval(
        sample_count=sample_count,
        baseline_statistic=trend_cusum_statistic(baseline_split.trend),
        residual_scale=float(np.std(baseline_split.residual)),
        residual_decorrelation=residual_decorrelation,
        alpha_channel=alpha_channel,
    )
    positions = np.linspace(-1.0, 1.0, sample_count)
    design = np.polynomial.polynomial.polyvander(positions, 3)
    cumulative_design = np.cumsum(design - np.mean(design, axis=0), axis=0)[:-1]
    covariance = np.linalg.inv(design.T @ design)
    leverage = np.einsum(
        "ij,jk,ik->i",
        cumulative_design,
        covariance,
        cumulative_design,
    )
    maximum_standard_error = (
        float(np.std(baseline_split.residual))
        * math.sqrt(residual_decorrelation * float(np.max(leverage)))
        / sample_count
    )
    critical_z = (
        decision_interval - trend_cusum_statistic(baseline_split.trend)
    ) / maximum_standard_error
    capture_false_alarm_bound = 2.0 * (sample_count - 1) * (1.0 - NormalDist().cdf(critical_z))
    planted_split = cubic_detrend(control_noise + np.linspace(-4.0, 4.0, sample_count))

    # The cases vary sample rate and capture duration; the same fixed
    # per-capture alpha stays silent and the same normalized-time drift trips.
    assert trend_cusum_statistic(control_split.trend) <= decision_interval
    assert trend_cusum_statistic(planted_split.trend) > decision_interval
    assert capture_false_alarm_bound <= alpha_channel * (1.0 + 1e-10)


def test_context_envelopes_carry_provenance_and_achieved_evidence(clean_baseline):
    for envelope in clean_baseline.envelopes.values():
        assert envelope.provenance.bag_sha256 == clean_baseline.recording_sha256
        assert envelope.n is not None
        assert envelope.n_eff is not None
        if envelope.rate_hz is not None:
            assert envelope.coverage == 0.95
            assert envelope.confidence is not None
            assert envelope.confidence >= 0.95

    payload = clean_baseline.to_dict()
    under_sampled = payload["channels"]["device/dev.barometer/pressure_pa"]
    assert under_sampled["status"] == "under-sampled"
    assert "band" not in under_sampled


def test_baseline_provenance_carries_capture_and_known_good_evidence(clean_baseline):
    payload = clean_baseline.to_dict()
    recording = payload["recording"]
    timing = recording["timing"]
    provenance = payload["provenance"]

    assert timing == {
        "clock": "recording receive-time",
        "start_timestamp_s": 0.0,
        "end_timestamp_s": pytest.approx(29.99518912),
        "duration_s": pytest.approx(29.99518912),
    }
    assert provenance == {
        "known_good_rationale": (
            "Clean synthetic capture generated from the checked-in healthy channel models."
        ),
        "operator": "automated fixture generator",
        "setup": "synthetic bench-idle fixture",
        "firmware": "not applicable — synthetic fixture",
        "calibration": "generator parameters recorded in manifest.yaml",
    }
    assert recording["completeness"] == {
        "dropped_messages": 0,
        "recording_completeness": "all generated rows were written",
    }

    review = parse_baseline_review(payload)
    assert review.duration_s == pytest.approx(29.99518912)
    assert review.known_good_rationale == provenance["known_good_rationale"]
    assert review.operator == provenance["operator"]
    assert review.setup == provenance["setup"]
    assert review.firmware == provenance["firmware"]
    assert review.calibration == provenance["calibration"]
    assert review.dropped_messages == 0
    assert review.recording_completeness == "all generated rows were written"


def test_baseline_provenance_carries_observed_range_for_every_under_sampled_channel(
    clean_baseline,
):
    recording = load_recording(BAGS / "clean")
    payload = clean_baseline.to_dict()
    review = parse_baseline_review(payload)

    expected_ranges = {}
    for device, stream in recording.devices.items():
        for name, samples in stream.signals.items():
            factor = 0.01 if recording.unit_of(name, device=device) == "Pa" else 1.0
            expected_ranges[f"device/{device}/{name}"] = (
                min(samples) * factor,
                max(samples) * factor,
            )
    for name, timed_values in recording.env.items():
        values = [value for _, value in timed_values]
        factor = 0.01 if recording.unit_of(name, device="environment") == "Pa" else 1.0
        expected_ranges[f"environment/{name}"] = (
            min(values) * factor,
            max(values) * factor,
        )
    for device, stream in recording.devices.items():
        rates = np.asarray(
            [
                1.0 / (right - left)
                for left, right in zip(stream.recv_times[:-1], stream.recv_times[1:], strict=True)
                if right > left
            ]
        )
        expected_ranges[f"rate/{device}"] = (float(rates.min()), float(rates.max()))

    under_sampled = [channel for channel in review.channels if channel.status == "under-sampled"]
    assert under_sampled
    for channel in under_sampled:
        assert channel.observed_range is not None
        expected_lo, expected_hi = expected_ranges[channel.id]
        assert channel.observed_range.lo == pytest.approx(expected_lo)
        assert channel.observed_range.hi == pytest.approx(expected_hi)


def test_baseline_provenance_old_v1_artifacts_load_with_explicit_gaps(clean_baseline):
    payload = clean_baseline.to_dict()
    payload["recording"].pop("timing")
    payload["recording"].pop("completeness")
    payload.pop("provenance")
    for channel in payload["channels"].values():
        channel.pop("observed_range")

    review = parse_baseline_review(payload)

    assert review.duration_s is None
    assert review.known_good_rationale is None
    assert review.operator is None
    assert review.dropped_messages is None
    assert all(channel.observed_range is None for channel in review.channels)


def _write_baseline_artifact(path: Path, payload: dict) -> None:
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w") as artifact_file:
        yaml.dump(payload, artifact_file)


def test_baseline_review_provenance_renders_real_values_for_a_new_artifact(
    clean_baseline, tmp_path
):
    artifact_path = tmp_path / "baseline.yaml"
    _write_baseline_artifact(artifact_path, clean_baseline.to_dict())

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    normalized_output = plain(result.output)
    # Mutation bite: changing or omitting any rendered value below breaks its exact assertion.
    assert "start timestamp: 0.0 s" in normalized_output
    assert "end timestamp: 29.99518912 s" in normalized_output
    assert "duration: 29.99518912 s" in normalized_output
    assert (
        "rationale: Clean synthetic capture generated from the checked-in healthy channel models."
        in normalized_output
    )
    assert "operator: automated fixture generator" in normalized_output
    assert "setup: synthetic bench-idle fixture" in normalized_output
    assert "firmware: not applicable — synthetic fixture" in normalized_output
    assert "calibration: generator parameters recorded in manifest.yaml" in normalized_output
    assert "dropped messages: 0" in normalized_output
    assert "assessment: all generated rows were written" in normalized_output


def test_baseline_review_provenance_renders_gap_for_every_legacy_field(clean_baseline, tmp_path):
    payload = clean_baseline.to_dict()
    payload["recording"].pop("timing")
    payload["recording"].pop("completeness")
    payload.pop("provenance")
    for channel in payload["channels"].values():
        channel.pop("observed_range")
    artifact_path = tmp_path / "legacy-baseline.yaml"
    _write_baseline_artifact(artifact_path, payload)

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    plain_output = plain(result.output)
    # Mutation bite: blank/omitted legacy fields or a quiet marker reduce this exact GAP count.
    expected_gap_labels = (
        "start timestamp",
        "end timestamp",
        "duration",
        "rationale",
        "operator",
        "setup",
        "firmware",
        "calibration",
        "dropped messages",
        "assessment",
    )
    for label in expected_gap_labels:
        assert f"{label}: GAP" in plain_output
    under_sampled = [
        channel
        for channel in parse_baseline_review(payload).channels
        if channel.status == "under-sampled"
    ]
    assert plain_output.count("observed range: GAP") == len(under_sampled)


@pytest.mark.parametrize("blank_value", ["", " \t "], ids=["empty", "whitespace"])
def test_baseline_review_hand_written_blank_provenance_renders_gap(
    blank_value, clean_baseline, tmp_path
):
    payload = clean_baseline.to_dict()
    payload["provenance"] = {
        "known_good_rationale": blank_value,
        "operator": blank_value,
        "setup": blank_value,
        "firmware": blank_value,
        "calibration": blank_value,
    }
    payload["recording"]["completeness"]["recording_completeness"] = blank_value
    artifact_path = tmp_path / "hand-written-baseline.yaml"
    _write_baseline_artifact(artifact_path, payload)

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    plain_output = plain(result.output)
    for label in ("rationale", "operator", "setup", "firmware", "calibration", "assessment"):
        assert f"{label}: GAP" in plain_output


def test_baseline_review_renders_fitted_band_on_locked_envelope_line(clean_baseline, tmp_path):
    """The review prints the fitted band it already holds, not a recomputed one."""
    artifact_path = tmp_path / "baseline.yaml"
    payload = clean_baseline.to_dict()
    _write_baseline_artifact(artifact_path, payload)

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    normalized_output = plain(result.output)
    locked = [
        channel for channel in parse_baseline_review(payload).channels if channel.status == "locked"
    ]
    assert locked
    # Mutation bite: swapping a band's lo/hi or dropping the band breaks this exact line.
    for channel in locked:
        assert channel.fitted_band is not None
        expected = (
            f"envelope: LOCKED {channel.fitted_band.lo:.2f}"
            f"–{channel.fitted_band.hi:.2f} {channel.fitted_band.unit}"
        )
        assert expected in normalized_output


def test_baseline_review_without_fitted_bands_renders_no_invented_number(clean_baseline, tmp_path):
    """An older artifact with no serialized band must still render, with a bare LOCKED line."""
    payload = clean_baseline.to_dict()
    for channel in payload["channels"].values():
        channel.pop("band", None)
    artifact_path = tmp_path / "no-band-baseline.yaml"
    _write_baseline_artifact(artifact_path, payload)

    review = parse_baseline_review(payload)
    assert review.channels
    assert all(channel.fitted_band is None for channel in review.channels)

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    normalized_output = plain(result.output)
    locked = [channel for channel in review.channels if channel.status == "locked"]
    assert locked
    # Mutation bite: printing a band that isn't there would insert digits between
    # "LOCKED" and the next label below.
    assert "envelope: LOCKED observed range:" in normalized_output


def test_baseline_review_provenance_renders_observed_range_for_every_under_sampled_channel(
    clean_baseline, tmp_path
):
    artifact_path = tmp_path / "baseline.yaml"
    payload = clean_baseline.to_dict()
    _write_baseline_artifact(artifact_path, payload)

    result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert result.exit_code == 0, result.output
    under_sampled = [
        channel
        for channel in parse_baseline_review(payload).channels
        if channel.status == "under-sampled"
    ]
    assert under_sampled
    # ANSI-stripped but not whitespace-collapsed: the blank-line paragraph
    # boundary below is still layout, and plain() would erase it.
    ansi_free_output = strip_ansi(result.output)
    # Mutation bite: omitting any under-sampled range removes its channel-local exact line.
    for channel in under_sampled:
        assert channel.observed_range is not None
        channel_block = ansi_free_output.split(channel.id, 1)[1].split("\n\n", 1)[0]
        expected = (
            f"observed range: {channel.observed_range.lo:g} to "
            f"{channel.observed_range.hi:g} {channel.observed_range.unit}"
        )
        assert expected in plain(channel_block)


def test_cli_baseline_provenance_rationale_flag_reaches_artifact():
    rationale = "Fixture passed the documented known-good commissioning procedure."
    result = CliRunner().invoke(
        app,
        [
            "baseline",
            str(BAGS / "clean"),
            "--context",
            "bench-idle",
            "--rationale",
            rationale,
        ],
    )

    assert result.exit_code == 0, result.output
    # Mutation bite: dropping or miswiring --rationale leaves this artifact field unequal.
    artifact = YAML(typ="safe").load(result.output)
    assert artifact["provenance"]["known_good_rationale"] == rationale


@pytest.mark.parametrize("rationale", ["", "   "])
def test_baseline_review_empty_rationale_renders_gap(rationale, tmp_path):
    recording = load_recording(BAGS / "clean")
    sibling_payload = build_baseline(
        recording,
        context=recording.context,
        operator=rationale,
        setup=rationale,
        firmware=rationale,
        calibration=rationale,
        recording_completeness=rationale,
    ).to_dict()
    assert sibling_payload["provenance"]["operator"] is None
    assert sibling_payload["provenance"]["setup"] is None
    assert sibling_payload["provenance"]["firmware"] is None
    assert sibling_payload["provenance"]["calibration"] is None
    assert sibling_payload["recording"]["completeness"]["recording_completeness"] is None

    baseline_result = CliRunner().invoke(
        app,
        [
            "baseline",
            str(BAGS / "clean"),
            "--context",
            "bench-idle",
            "--rationale",
            rationale,
        ],
    )
    assert baseline_result.exit_code == 0, baseline_result.output
    artifact = YAML(typ="safe").load(baseline_result.output)
    assert artifact["provenance"]["known_good_rationale"] is None

    artifact_path = tmp_path / "baseline.yaml"
    _write_baseline_artifact(artifact_path, artifact)
    review_result = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])

    assert review_result.exit_code == 0, review_result.output
    assert "rationale: GAP" in plain(review_result.output)


def test_cli_baseline_outputs_a_parseable_artifact():
    result = CliRunner().invoke(
        app,
        ["baseline", str(BAGS / "clean"), "--context", "bench-idle"],
    )

    assert result.exit_code == 0, result.output
    artifact = YAML(typ="safe").load(result.output)
    assert artifact["context"] == "bench-idle"
    assert artifact["recording"]["sha256"] == load_recording(BAGS / "clean").sha256
    assert set(artifact["envelopes"]) == {"dev.barometer", "dev.gnss", "dev.imu"}
    assert artifact["channels"]["device/dev.imu/accel_z"]["status"] == "locked"
    assert artifact["channels"]["device/dev.imu/accel_z"]["band"]
    assert artifact["channels"]["device/dev.imu/accel_z"]["observed_range"]
    assert artifact["recording"]["timing"]["duration_s"] == pytest.approx(29.99518912)
    assert artifact["provenance"]["known_good_rationale"] is None
    assert artifact["recording"]["completeness"]["dropped_messages"] is None


def test_every_planted_failure_once_and_measured_clean_false_positive_rate(clean_baseline, capsys):
    signal_limits, condition_limits, nominal_rates, expected = _fallbacks_from_ground_truth()
    kwargs = {
        "signal_hard_limits": signal_limits,
        "condition_hard_limits": condition_limits,
        "nominal_rates_hz": nominal_rates,
    }

    clean_b_findings = check_recording(load_recording(BAGS / "clean_b"), clean_baseline, **kwargs)
    measured_false_positive_rate = float(bool(clean_b_findings))
    print(
        "measured clean-B false-positive rate: "
        f"{measured_false_positive_rate:.6f} per preflight "
        f"(budget: 0.005000; findings: {len(clean_b_findings)})"
    )
    assert "measured clean-B false-positive rate:" in capsys.readouterr().out
    assert clean_b_findings == []

    for bag_id, (mechanism, target_ids) in expected.items():
        findings = check_recording(load_recording(BAGS / bag_id), clean_baseline, **kwargs)
        assert len(findings) == 1, (bag_id, findings)
        assert findings[0].mechanism == mechanism
        if mechanism == "rate-check":
            target_device = target_ids[0].split("/")[1]
            assert findings[0].channels == (f"rate/{target_device}",)
        else:
            assert set(findings[0].channels) == set(target_ids)


def test_clustered_findings_preserve_shared_root_and_flagged_member_ids(clean_baseline):
    recording = load_recording(BAGS / "clean")
    limits = {
        ("dev.imu", "accel_x"): Band.model_construct(lo=-math.inf, hi=-math.inf, unit="m/s^2"),
        ("dev.imu", "accel_y"): Band.model_construct(lo=-math.inf, hi=-math.inf, unit="m/s^2"),
    }

    findings = check_recording(recording, clean_baseline, signal_hard_limits=limits)

    assert len(findings) == 2
    assert {finding.cluster_root for finding in findings} == {"dev.imu"}
    assert {finding.cluster_members for finding in findings} == {
        ("device/dev.imu/accel_x",),
        ("device/dev.imu/accel_y",),
    }


def _write_signal_recording(path: Path, values, *, sample_rate_hz: float = 200.0) -> Path:
    """Persist a one-channel recording directory for CliRunner baseline-check tests."""
    path.mkdir(parents=True, exist_ok=True)
    samples = np.asarray(values, dtype=float)
    manifest = {
        "context": "bench-idle",
        "units": {"dev.sensor": {"value": "m"}},
    }
    YAML().dump(manifest, path / "manifest.yaml")
    lines = [
        json.dumps(
            {
                "t": float(index) / sample_rate_hz,
                "dev": "dev.sensor",
                "sig": {"value": float(value)},
            },
            separators=(",", ":"),
        )
        for index, value in enumerate(samples)
    ]
    (path / "samples.jsonl").write_text("\n".join(lines) + "\n")
    return path


def test_baseline_check_cli_surfaces_planted_drift_as_named_finding(tmp_path):
    noise = np.random.default_rng(412).normal(scale=0.2, size=1200)
    baseline_dir = _write_signal_recording(tmp_path / "baseline", noise)
    planted_dir = _write_signal_recording(
        tmp_path / "planted",
        noise + np.linspace(-8.0, 8.0, noise.size),
    )
    artifact_path = tmp_path / "baseline.yaml"
    payload = build_baseline(load_recording(baseline_dir), context="bench-idle").to_dict()
    with artifact_path.open("w") as handle:
        YAML().dump(payload, handle)

    result = CliRunner().invoke(
        app,
        ["baseline-check", str(planted_dir), str(artifact_path)],
    )

    assert result.exit_code == 1, result.output
    plain_output = plain(result.output)
    assert "drift-cusum" in plain_output
    assert "VIOLATION" in plain_output
    assert "device/dev.sensor/value" in plain_output
    assert "NO-GO" in plain_output
    assert "observed drift-cusum, expected within blessed baseline" in plain_output


def test_baseline_check_cli_clean_recording_is_go(tmp_path, clean_baseline):
    artifact_path = tmp_path / "clean-baseline.yaml"
    with artifact_path.open("w") as handle:
        YAML().dump(clean_baseline.to_dict(), handle)

    result = CliRunner().invoke(
        app,
        ["baseline-check", str(BAGS / "clean"), str(artifact_path)],
    )

    assert result.exit_code == 0, result.output
    plain_output = plain(result.output)
    assert "VIOLATION" not in plain_output
    assert "GO — records agree (15 scored), no findings" in plain_output  # counts carried


def test_baseline_check_json_refuses_the_contract_path(tmp_path, clean_baseline):
    artifact_path = tmp_path / "clean-baseline.yaml"
    with artifact_path.open("w") as handle:
        YAML().dump(clean_baseline.to_dict(), handle)
    contract = tmp_path / "lockwire.yaml"
    contract.write_text("name: keep\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "baseline-check",
            str(BAGS / "clean"),
            str(artifact_path),
            "--contract",
            str(contract),
            "--no-ledger",
            "--json",
            str(contract),
        ],
    )

    assert result.exit_code == 2, result.output
    assert contract.read_text(encoding="utf-8") == "name: keep\n"


def test_baseline_check_and_review_agree_rejecting_negative_dropped_messages(
    tmp_path, clean_baseline
):
    """Gate and review must reject the same corrupt completeness field.

    Mutation bite: restoring a bare ``int()`` on the check adapter lets
    ``baseline-check`` GO while ``baseline-review`` still exits 1.
    """
    payload = clean_baseline.to_dict()
    payload["recording"]["completeness"]["dropped_messages"] = -5
    artifact_path = tmp_path / "negative-dropped.yaml"
    _write_baseline_artifact(artifact_path, payload)

    review = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])
    check = CliRunner().invoke(
        app,
        ["baseline-check", str(BAGS / "clean"), str(artifact_path)],
    )

    # a malformed artifact is a tool failure (exit 2), not a robot NO-GO (exit 1).
    assert review.exit_code == 2, review.output
    assert check.exit_code == 2, check.output
    needle = "dropped_messages must be a non-negative integer"
    review_text = plain(review.output)
    check_text = plain(check.output)
    assert needle in review_text
    assert needle in check_text
    assert "GO —" not in check_text


@pytest.mark.parametrize(
    ("path", "field", "value", "needle"),
    [
        (
            ("recording", "completeness"),
            "dropped_messages",
            1.5,
            "dropped_messages must be a non-negative integer",
        ),
        (
            ("recording", "timing"),
            "duration_s",
            "not-a-number",
            "duration_s must be numeric or null",
        ),
        (("provenance",), "operator", 12, "operator must be a string or null"),
        (
            ("recording", "completeness"),
            "recording_completeness",
            False,
            "recording_completeness must be a string or null",
        ),
    ],
)
def test_baseline_check_and_review_agree_on_shared_field_guards(
    tmp_path, clean_baseline, path, field, value, needle
):
    payload = clean_baseline.to_dict()
    bucket = payload
    for key in path:
        bucket = bucket[key]
    bucket[field] = value
    artifact_path = tmp_path / "corrupt-shared-field.yaml"
    _write_baseline_artifact(artifact_path, payload)

    review = CliRunner().invoke(app, ["baseline-review", str(artifact_path)])
    check = CliRunner().invoke(
        app,
        ["baseline-check", str(BAGS / "clean"), str(artifact_path)],
    )

    # a malformed artifact is a tool failure (exit 2), not a robot NO-GO (exit 1).
    assert review.exit_code == 2, review.output
    assert check.exit_code == 2, check.output
    review_text = plain(review.output)
    check_text = plain(check.output)
    assert needle in review_text
    assert needle in check_text


def _variant(tmp_path, *, drop=(), env_only=False, extra_channel=False):
    """A copy of the clean bag with devices removed or an unknown channel added."""
    out = tmp_path / "variant"
    out.mkdir()
    manifest = json.loads((BAGS / "clean" / "manifest.yaml").read_text())
    manifest["units"]["dev.imu"]["mag_x"] = "uT"
    (out / "manifest.yaml").write_text(json.dumps(manifest))
    rows = []
    for line in (BAGS / "clean" / "samples.jsonl").read_text().splitlines():
        row = json.loads(line)
        if env_only and "env" not in row or row.get("dev") in drop:
            continue
        if extra_channel and row.get("dev") == "dev.imu":
            row["sig"]["mag_x"] = 0.1
        rows.append(json.dumps(row))
    (out / "samples.jsonl").write_text("\n".join(rows) + "\n")
    return out


def _baseline_check(clean_baseline, tmp_path, **variant):
    artifact = tmp_path / "base.yaml"
    _write_baseline_artifact(artifact, clean_baseline.to_dict())
    recording = _variant(tmp_path, **variant)
    json_out = tmp_path / "out.json"
    result = CliRunner().invoke(
        app,
        ["baseline-check", str(recording), str(artifact), "--no-ledger", "--json", str(json_out)],
    )
    return (
        result,
        plain(result.output),
        json.loads(json_out.read_text()) if json_out.exists() else None,
    )


@pytest.mark.parametrize("variant", [{"drop": ("dev.imu",)}, {"env_only": True}])
def test_baseline_check_declared_device_absent_is_not_scored_exit_3(
    clean_baseline, tmp_path, variant
):
    """a declared device missing from the recording cannot be GO."""
    result, output, document = _baseline_check(clean_baseline, tmp_path, **variant)

    assert result.exit_code == 3, output
    assert "NOT SCORED" in output
    assert "dev.imu" in output
    assert document["verdict"] == "NOT_SCORED"


def test_baseline_check_absent_device_headline_names_it_when_others_compared(
    clean_baseline, tmp_path
):
    """One device compared, one absent: say which, not "nothing compared"."""
    result, output, _ = _baseline_check(clean_baseline, tmp_path, drop=("dev.imu",))

    assert result.exit_code == 3, output
    assert "dev.imu declared and not seen" in output
    assert "nothing compared" not in output


def test_baseline_check_extra_channel_is_not_scored_not_an_error(clean_baseline, tmp_path):
    """a channel the baseline never saw is not scored, never a bare KeyError."""
    result, output, _ = _baseline_check(clean_baseline, tmp_path, extra_channel=True)

    assert result.exit_code == 0, output
    assert "mag_x" in output
    assert "not scored" in output


def test_baseline_tool_failure_is_exit_2_with_one_plain_line(tmp_path):
    """exit 1 means the robot is wrong; a tool failure is exit 2."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("artifact_version: '1'\n")
    recording = _variant(tmp_path)
    for args in (["baseline-review", str(bad)], ["baseline-check", str(recording), str(bad)]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, result.output


def test_fitted_envelope_sees_a_level_shift(tmp_path):
    """constant 100 against band [-1, 1] must not pass because the residual is detrended."""
    from types import SimpleNamespace

    from lockwire.schema import FittedBandEvidence
    from lockwire_pro.preflight.stages.sanity import _range_findings

    recording = _signal_recording(tmp_path, np.full(400, 100.0))
    findings = _range_findings(
        device=SimpleNamespace(id="dev.sensor", owner=None),
        quantity="dev.sensor/value",
        raw=np.full(400, 100.0) + np.random.default_rng(1).normal(0, 0.1, 400),
        recording=recording,
        channel="value",
        limit=Band(lo=-1.0, hi=1.0, unit="m"),
        role="band",
        fitted=FittedBandEvidence(rank=1, n_eff=100, alpha=0.005),
    )

    assert [f.severity for f in findings] == ["violation"]


@pytest.mark.parametrize("flag", ["--json", "--out"])
def test_paid_writers_exit_2_on_a_missing_directory(flag, tmp_path):
    """`health --json` and `sample --out` into a missing folder."""
    target = str(tmp_path / "nodir" / "x")
    if flag == "--json":
        args = [
            "health",
            str(Path(__file__).parent / "fixtures" / "contract_clean.yaml"),
            flag,
            target,
        ]
    else:
        args = ["sample", "--local", "--seconds", "0.1", flag, target]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert len(plain(result.output).strip().splitlines()) == 1, result.output
    assert not (tmp_path / "nodir").exists()
