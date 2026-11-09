"""Generate deterministic synthetic recordings with machine-readable planted failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from lockwire_pro.recording import DeviceStream, Recording

DEFAULT_SEED = 20260725
DURATION_S = 30.0
FORMAT_VERSION = "1.0"
ALGORITHM_VERSION = "2"
TIMING_JITTER_FRACTION = 0.02
TIMING_JITTER_MAX_S = 0.0005
TIMING_JITTER_PHI = 0.35
TIMING_JITTER_CLIP_SIGMA = 3.0

UNITS = {
    "dev.barometer": {"pressure_pa": "Pa"},
    "dev.gnss": {
        "gnss_altitude_m": "m",
        "latitude_deg": "deg",
        "longitude_deg": "deg",
    },
    "dev.imu": {
        "accel_x": "m/s^2",
        "accel_y": "m/s^2",
        "accel_z": "m/s^2",
        "gyro_x": "rad/s",
        "gyro_y": "rad/s",
        "gyro_z": "rad/s",
    },
    "environment": {
        "condition_altitude_m": "m",
        "temp_c": "degC",
    },
}


@dataclass(frozen=True)
class NoiseSpec:
    rate_hz: float
    phi: float
    innovation_std: float
    drift_amplitude: float
    drift_period_s: float
    vibration_amplitude: float
    vibration_hz: float


SPECS = {
    "device/dev.barometer/pressure_pa": NoiseSpec(50.0, 0.94, 1.8, 18.0, 24.0, 2.5, 3.0),
    "device/dev.gnss/gnss_altitude_m": NoiseSpec(8.0, 0.72, 0.09, 0.35, 28.0, 0.0, 0.0),
    "device/dev.gnss/latitude_deg": NoiseSpec(8.0, 0.72, 1.2e-7, 4e-7, 28.0, 0.0, 0.0),
    "device/dev.gnss/longitude_deg": NoiseSpec(8.0, 0.72, 1.5e-7, 5e-7, 28.0, 0.0, 0.0),
    "device/dev.imu/accel_x": NoiseSpec(200.0, 0.88, 0.012, 0.035, 18.0, 0.025, 23.0),
    "device/dev.imu/accel_y": NoiseSpec(200.0, 0.88, 0.012, 0.030, 21.0, 0.022, 19.0),
    "device/dev.imu/accel_z": NoiseSpec(200.0, 0.90, 0.015, 0.045, 25.0, 0.035, 27.0),
    "device/dev.imu/gyro_x": NoiseSpec(200.0, 0.84, 0.0015, 0.004, 20.0, 0.003, 31.0),
    "device/dev.imu/gyro_y": NoiseSpec(200.0, 0.84, 0.0015, 0.004, 22.0, 0.003, 29.0),
    "device/dev.imu/gyro_z": NoiseSpec(200.0, 0.86, 0.0018, 0.005, 24.0, 0.004, 17.0),
    "environment/condition_altitude_m": NoiseSpec(2.0, 0.80, 0.10, 0.5, 30.0, 0.0, 0.0),
    "environment/temp_c": NoiseSpec(2.0, 0.92, 0.025, 0.35, 35.0, 0.0, 0.0),
}

BASELINES = {
    "device/dev.barometer/pressure_pa": 101325.0,
    "device/dev.gnss/gnss_altitude_m": 120.0,
    "device/dev.gnss/latitude_deg": 43.6532,
    "device/dev.gnss/longitude_deg": -79.3832,
    "device/dev.imu/accel_x": 0.0,
    "device/dev.imu/accel_y": 0.0,
    "device/dev.imu/accel_z": -9.80665,
    "device/dev.imu/gyro_x": 0.0,
    "device/dev.imu/gyro_y": 0.0,
    "device/dev.imu/gyro_z": 0.0,
    "environment/condition_altitude_m": 120.0,
    "environment/temp_c": 24.0,
}


def _channel_seed(seed: int, channel: str) -> int:
    payload = f"{seed}:{channel}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _normal_stream(rng: random.Random):
    while True:
        u1 = max(rng.random(), 1e-15)
        u2 = rng.random()
        radius = math.sqrt(-2.0 * math.log(u1))
        yield radius * math.cos(2.0 * math.pi * u2)
        yield radius * math.sin(2.0 * math.pi * u2)


def _series(seed: int, channel: str) -> list[float]:
    spec = SPECS[channel]
    count = round(DURATION_S * spec.rate_hz)
    normals = _normal_stream(random.Random(_channel_seed(seed, channel)))
    stationary_std = spec.innovation_std / math.sqrt(1.0 - spec.phi**2)
    state = stationary_std * next(normals)
    values = []
    for index in range(count):
        if index:
            state = spec.phi * state + spec.innovation_std * next(normals)
        t = index / spec.rate_hz
        drift = spec.drift_amplitude * math.sin(2.0 * math.pi * t / spec.drift_period_s)
        thermal = 0.5 * spec.drift_amplitude * math.exp(-t / 6.0)
        vibration = spec.vibration_amplitude * math.sin(2.0 * math.pi * spec.vibration_hz * t)
        values.append(round(BASELINES[channel] + state + drift + thermal + vibration, 9))
    return values


def _base_channels(seed: int) -> dict[str, list[float]]:
    return {channel: _series(seed, channel) for channel in sorted(SPECS)}


def _stream_times(seed: int, stream: str, count: int, rate_hz: float) -> list[float]:
    period_s = 1.0 / rate_hz
    jitter_std_s = min(TIMING_JITTER_FRACTION * period_s, TIMING_JITTER_MAX_S)
    innovation_std_s = jitter_std_s * math.sqrt(1.0 - TIMING_JITTER_PHI**2)
    normals = _normal_stream(random.Random(_channel_seed(seed, f"timing/{stream}")))
    latency_s = max(
        -TIMING_JITTER_CLIP_SIGMA * jitter_std_s,
        min(TIMING_JITTER_CLIP_SIGMA * jitter_std_s, jitter_std_s * next(normals)),
    )
    initial_latency_s = latency_s
    times = []
    for index in range(count):
        if index:
            latency_s = TIMING_JITTER_PHI * latency_s + innovation_std_s * next(normals)
            latency_s = max(
                -TIMING_JITTER_CLIP_SIGMA * jitter_std_s,
                min(TIMING_JITTER_CLIP_SIGMA * jitter_std_s, latency_s),
            )
        times.append(round(index * period_s + latency_s - initial_latency_s, 9))
    return times


def _base_times(seed: int, channels: dict[str, list[float]]) -> dict[str, list[float]]:
    return {
        "dev.barometer": _stream_times(
            seed,
            "dev.barometer",
            len(channels["device/dev.barometer/pressure_pa"]),
            SPECS["device/dev.barometer/pressure_pa"].rate_hz,
        ),
        "dev.gnss": _stream_times(
            seed,
            "dev.gnss",
            len(channels["device/dev.gnss/latitude_deg"]),
            SPECS["device/dev.gnss/latitude_deg"].rate_hz,
        ),
        "dev.imu": _stream_times(
            seed,
            "dev.imu",
            len(channels["device/dev.imu/accel_x"]),
            SPECS["device/dev.imu/accel_x"].rate_hz,
        ),
        "environment": _stream_times(
            seed,
            "environment",
            len(channels["environment/temp_c"]),
            SPECS["environment/temp_c"].rate_hz,
        ),
    }


def clean_recording(seed: int) -> Recording:
    """Draw one deterministic clean realization directly into memory."""
    channels = _base_channels(seed)
    times = _base_times(seed, channels)
    devices: dict[str, DeviceStream] = {}
    for device in ("dev.barometer", "dev.gnss", "dev.imu"):
        prefix = f"device/{device}/"
        devices[device] = DeviceStream(
            device=device,
            recv_times=times[device],
            signals={
                channel.removeprefix(prefix): values
                for channel, values in channels.items()
                if channel.startswith(prefix)
            },
        )
    environment_times = times["environment"]
    environment = {
        channel.removeprefix("environment/"): list(zip(environment_times, values, strict=True))
        for channel, values in channels.items()
        if channel.startswith("environment/")
    }
    digest = hashlib.sha256(f"synthetic-clean:{ALGORITHM_VERSION}:{seed}".encode()).hexdigest()
    return Recording(
        context="nominal-op",
        units={scope: units.copy() for scope, units in UNITS.items()},
        devices=devices,
        env=environment,
        sha256=digest,
        path=Path(f"<synthetic-clean:{seed}>"),
    )


def _noise_manifest() -> dict[str, dict[str, object]]:
    models = {}
    for channel in sorted(SPECS):
        spec = SPECS[channel]
        models[channel] = {
            "ar1_phi": spec.phi,
            "drift": {
                "amplitude": spec.drift_amplitude,
                "period_s": spec.drift_period_s,
            },
            "e_folding_samples": -1.0 / math.log(spec.phi),
            "e_folding_s": -1.0 / (math.log(spec.phi) * spec.rate_hz),
            "innovation_std": spec.innovation_std,
            "integrated_autocorrelation_samples": (1.0 + spec.phi) / (1.0 - spec.phi),
            "model": "ar1_plus_thermal_drift_and_vibration",
            "sample_rate_hz": spec.rate_hz,
            "thermal_transient": {
                "amplitude": 0.5 * spec.drift_amplitude,
                "time_constant_s": 6.0,
            },
            "timing_jitter": {
                "clip_sigma": TIMING_JITTER_CLIP_SIGMA,
                "latency_ar1_phi": TIMING_JITTER_PHI,
                "latency_std_fraction_of_period": TIMING_JITTER_FRACTION,
                "latency_std_max_s": TIMING_JITTER_MAX_S,
                "model": "bounded_ar1_scheduling_latency",
            },
            "vibration": {
                "amplitude": spec.vibration_amplitude,
                "frequency_hz": spec.vibration_hz,
            },
        }
    return models


def _channel(kind: str, name: str, device: str | None = None) -> dict[str, str]:
    result = {"kind": kind, "name": name}
    if device is not None:
        result["device"] = device
    return result


def _target(
    *,
    channel: dict[str, str],
    start_index: int,
    end_index_exclusive: int,
    rate_hz: float,
    magnitude_metric: str,
    magnitude_value: float,
    magnitude_unit: str,
    reference_description: str,
    reference_value: float,
    reference_unit: str,
    times: list[float] | None = None,
) -> dict[str, object]:
    if times is None:
        start_time = start_index / rate_hz
        end_time = (end_index_exclusive - 1) / rate_hz
    else:
        start_time = times[start_index]
        end_time = times[end_index_exclusive - 1]
    return {
        "channel": channel,
        "magnitude": {
            "metric": magnitude_metric,
            "reference": {
                "description": reference_description,
                "unit": reference_unit,
                "value": reference_value,
            },
            "unit": magnitude_unit,
            "value": magnitude_value,
        },
        "sample_range": {
            "end_index_exclusive": end_index_exclusive,
            "start_index": start_index,
        },
        "time_range_s": {"end_inclusive": end_time, "start": start_time},
    }


def _rows(
    channels: dict[str, list[float]],
    times: dict[str, list[float]],
    gnss_indices: list[int],
) -> list[dict[str, object]]:
    events: list[tuple[float, int, dict[str, object]]] = []

    imu_names = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")
    for index in range(len(channels["device/dev.imu/accel_x"])):
        signals = {name: channels[f"device/dev.imu/{name}"][index] for name in imu_names}
        t = times["dev.imu"][index]
        events.append((t, 0, {"dev": "dev.imu", "sig": signals, "t": t}))

    gnss_names = ("latitude_deg", "longitude_deg", "gnss_altitude_m")
    for index in gnss_indices:
        signals = {name: channels[f"device/dev.gnss/{name}"][index] for name in gnss_names}
        t = times["dev.gnss"][index]
        events.append((t, 1, {"dev": "dev.gnss", "sig": signals, "t": t}))

    for index, value in enumerate(channels["device/dev.barometer/pressure_pa"]):
        t = times["dev.barometer"][index]
        events.append((t, 2, {"dev": "dev.barometer", "sig": {"pressure_pa": value}, "t": t}))

    env_count = len(channels["environment/temp_c"])
    for index in range(env_count):
        t = times["environment"][index]
        env = {
            "condition_altitude_m": channels["environment/condition_altitude_m"][index],
            "temp_c": channels["environment/temp_c"][index],
        }
        events.append((t, 3, {"env": env, "t": t}))

    return [row for _, _, row in sorted(events, key=lambda event: (event[0], event[1]))]


def _manifest(
    seed: int,
    bag_id: str,
    failures: list[dict[str, object]],
    *,
    context: str = "nominal-op",
) -> dict[str, object]:
    return {
        "bag": {"duration_s": DURATION_S, "id": bag_id},
        "channel_models": _noise_manifest(),
        "context": context,
        "format_version": FORMAT_VERSION,
        "generator": {
            "algorithm_version": ALGORITHM_VERSION,
            "path": "tests/fixtures/bags/generate.py",
        },
        "planted_failures": failures,
        "seed": seed,
        "units": UNITS,
    }


def _write_bag(
    output: Path,
    bag_id: str,
    seed: int,
    channels: dict[str, list[float]],
    times: dict[str, list[float]],
    gnss_indices: list[int],
    failures: list[dict[str, object]],
    *,
    context: str = "nominal-op",
) -> None:
    bag = output / bag_id
    bag.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(seed, bag_id, failures, context=context)
    (bag / "manifest.yaml").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    with (bag / "samples.jsonl").open("w") as samples:
        for row in _rows(channels, times, gnss_indices):
            samples.write(
                json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
            )


def generate(output: Path, seed: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    base = _base_channels(seed)
    base_times = _base_times(seed, base)
    all_gnss_indices = list(range(len(base["device/dev.gnss/latitude_deg"])))

    _write_bag(output, "clean", seed, base, base_times, all_gnss_indices, [])

    clean_b_seed = seed + 1
    clean_b = _base_channels(clean_b_seed)
    clean_b_times = _base_times(clean_b_seed, clean_b)
    all_clean_b_gnss_indices = list(range(len(clean_b["device/dev.gnss/latitude_deg"])))
    _write_bag(
        output,
        "clean_b",
        clean_b_seed,
        clean_b,
        clean_b_times,
        all_clean_b_gnss_indices,
        [],
    )

    frozen = {name: values.copy() for name, values in base.items()}
    frozen_channel = frozen["device/dev.imu/accel_z"]
    frozen_start = 3000
    frozen_value = frozen_channel[frozen_start - 1]
    frozen_channel[frozen_start:] = [frozen_value] * (len(frozen_channel) - frozen_start)
    frozen_target = _target(
        channel=_channel("device", "accel_z", "dev.imu"),
        start_index=frozen_start,
        end_index_exclusive=len(frozen_channel),
        rate_hz=SPECS["device/dev.imu/accel_z"].rate_hz,
        magnitude_metric="post_freeze_peak_to_peak",
        magnitude_value=0.0,
        magnitude_unit="m/s^2",
        reference_description="value held after the last healthy sample",
        reference_value=frozen_value,
        reference_unit="m/s^2",
        times=base_times["dev.imu"],
    )
    _write_bag(
        output,
        "frozen_but_publishing",
        seed,
        frozen,
        base_times,
        all_gnss_indices,
        [
            {
                "description": "IMU accel_z continues at 200 Hz with an exactly frozen value.",
                "id": "frozen-imu-accel-z",
                "targets": [frozen_target],
                "type": "frozen-but-publishing",
            }
        ],
    )

    collapse_original_start = 80
    collapsed_indices = list(range(collapse_original_start))
    collapsed_indices.extend(range(collapse_original_start, len(all_gnss_indices), 8))
    collapsed_times = [base_times["dev.gnss"][index] for index in collapsed_indices]
    collapse_start = collapse_original_start
    collapse_target = _target(
        channel=_channel("device", "latitude_deg", "dev.gnss"),
        start_index=collapse_start,
        end_index_exclusive=len(collapsed_indices),
        rate_hz=1.0,
        times=collapsed_times,
        magnitude_metric="publish_rate_ratio",
        magnitude_value=0.125,
        magnitude_unit="1",
        reference_description="nominal publish rate",
        reference_value=8.0,
        reference_unit="Hz",
    )
    _write_bag(
        output,
        "rate_collapse",
        seed,
        base,
        base_times,
        collapsed_indices,
        [
            {
                "description": "The complete GNSS message drops from 8 Hz to 1 Hz.",
                "id": "gnss-rate-collapse",
                "targets": [collapse_target],
                "type": "rate-collapse",
            }
        ],
    )

    excursion = {name: values.copy() for name, values in base.items()}
    pressure = excursion["device/dev.barometer/pressure_pa"]
    excursion_start = 750
    excursion_end = 1000
    healthy_upper_pa = 101500.0
    for index in range(excursion_start, excursion_end):
        phase = (index - excursion_start) / (excursion_end - excursion_start - 1)
        pressure[index] += 900.0 + 700.0 * math.sin(math.pi * phase)
        pressure[index] = round(pressure[index], 9)
    max_excess = round(max(pressure[excursion_start:excursion_end]) - healthy_upper_pa, 9)
    excursion_target = _target(
        channel=_channel("device", "pressure_pa", "dev.barometer"),
        start_index=excursion_start,
        end_index_exclusive=excursion_end,
        rate_hz=SPECS["device/dev.barometer/pressure_pa"].rate_hz,
        magnitude_metric="max_upper_bound_excess",
        magnitude_value=max_excess,
        magnitude_unit="Pa",
        reference_description="healthy upper bound",
        reference_value=healthy_upper_pa,
        reference_unit="Pa",
        times=base_times["dev.barometer"],
    )
    _write_bag(
        output,
        "range_excursion",
        seed,
        excursion,
        base_times,
        all_gnss_indices,
        [
            {
                "description": "Barometric pressure wanders above its healthy envelope.",
                "id": "barometer-range-excursion",
                "targets": [excursion_target],
                "type": "range-excursion",
            }
        ],
    )

    condition = {name: values.copy() for name, values in base.items()}
    condition_start = 30
    condition_end = len(condition["environment/temp_c"])
    temp = condition["environment/temp_c"]
    altitude = condition["environment/condition_altitude_m"]
    for index in range(condition_start, condition_end):
        phase = (index - condition_start) / (condition_end - condition_start - 1)
        temp[index] = round(31.0 + 14.0 * phase, 9)
        altitude[index] = round(350.0 + 1450.0 * phase, 9)
    temp_target = _target(
        channel=_channel("environment", "temp_c"),
        start_index=condition_start,
        end_index_exclusive=condition_end,
        rate_hz=SPECS["environment/temp_c"].rate_hz,
        magnitude_metric="max_supported_bound_excess",
        magnitude_value=max(temp[condition_start:condition_end]) - 30.0,
        magnitude_unit="degC",
        reference_description="maximum baselined temperature",
        reference_value=30.0,
        reference_unit="degC",
        times=base_times["environment"],
    )
    altitude_target = _target(
        channel=_channel("environment", "condition_altitude_m"),
        start_index=condition_start,
        end_index_exclusive=condition_end,
        rate_hz=SPECS["environment/condition_altitude_m"].rate_hz,
        magnitude_metric="max_supported_bound_excess",
        magnitude_value=max(altitude[condition_start:condition_end]) - 300.0,
        magnitude_unit="m",
        reference_description="maximum baselined altitude",
        reference_value=300.0,
        reference_unit="m",
        times=base_times["environment"],
    )
    _write_bag(
        output,
        "condition_envelope_exit",
        seed,
        condition,
        base_times,
        all_gnss_indices,
        [
            {
                "description": "Temperature and altitude leave the baselined condition region.",
                "id": "environment-condition-exit",
                "targets": [temp_target, altitude_target],
                "type": "condition-envelope-exit",
            }
        ],
    )

    # --- B93 preflight demo bags: one planted fault per stage, isolated ---
    _write_preflight_bags(output, seed, base, base_times, all_gnss_indices)


def _write_preflight_bags(
    output: Path,
    seed: int,
    base: dict[str, list[float]],
    base_times: dict[str, list[float]],
    all_gnss_indices: list[int],
) -> None:
    """Planted bags for the B93 per-stage isolation demo (contract_demo.yaml)."""

    # Stage 2 — rate collapse on GNSS only (signal values unchanged).
    collapse_original_start = 80
    collapsed_indices = list(range(collapse_original_start))
    collapsed_indices.extend(range(collapse_original_start, len(all_gnss_indices), 8))
    collapsed_times = [base_times["dev.gnss"][index] for index in collapsed_indices]
    collapse_target = _target(
        channel=_channel("device", "latitude_deg", "dev.gnss"),
        start_index=collapse_original_start,
        end_index_exclusive=len(collapsed_indices),
        rate_hz=1.0,
        times=collapsed_times,
        magnitude_metric="publish_rate_ratio",
        magnitude_value=0.125,
        magnitude_unit="1",
        reference_description="nominal publish rate",
        reference_value=8.0,
        reference_unit="Hz",
    )
    _write_bag(
        output,
        "preflight_flow",
        seed,
        base,
        base_times,
        collapsed_indices,
        [
            {
                "description": "GNSS publish rate collapses from 8 Hz to 1 Hz (stage-2 only).",
                "id": "preflight-gnss-rate-collapse",
                "targets": [collapse_target],
                "type": "rate-collapse",
            }
        ],
    )

    # Stage 3 — barometer pressure leaves the demo envelope band.
    # Magnitude is relative to the clean bag's own observed max, never a contract
    # threshold (independence from the fault injector).
    excursion = {name: values.copy() for name, values in base.items()}
    pressure = excursion["device/dev.barometer/pressure_pa"]
    excursion_start = 750
    excursion_end = 1000
    clean_pressure_max = max(base["device/dev.barometer/pressure_pa"])
    for index in range(excursion_start, excursion_end):
        phase = (index - excursion_start) / (excursion_end - excursion_start - 1)
        pressure[index] += 900.0 + 700.0 * math.sin(math.pi * phase)
        pressure[index] = round(pressure[index], 9)
    max_excess = round(max(pressure[excursion_start:excursion_end]) - clean_pressure_max, 9)
    excursion_target = _target(
        channel=_channel("device", "pressure_pa", "dev.barometer"),
        start_index=excursion_start,
        end_index_exclusive=excursion_end,
        rate_hz=SPECS["device/dev.barometer/pressure_pa"].rate_hz,
        magnitude_metric="max_upper_bound_excess",
        magnitude_value=max_excess,
        magnitude_unit="Pa",
        reference_description="clean bag observed maximum pressure_pa",
        reference_value=clean_pressure_max,
        reference_unit="Pa",
        times=base_times["dev.barometer"],
    )
    _write_bag(
        output,
        "preflight_sanity",
        seed,
        excursion,
        base_times,
        all_gnss_indices,
        [
            {
                "description": "Barometric pressure exceeds the demo envelope band (stage-3 only).",
                "id": "preflight-barometer-range-excursion",
                "targets": [excursion_target],
                "type": "range-excursion",
            }
        ],
    )

    # Stage 4a — temperature leaves the nominal-op condition band (env only).
    # Magnitude is relative to the clean bag's own observed max, never a contract
    # threshold (independence from the fault injector).
    condition = {name: values.copy() for name, values in base.items()}
    condition_start = 30
    condition_end = len(condition["environment/temp_c"])
    temp = condition["environment/temp_c"]
    clean_temp_max = max(base["environment/temp_c"])
    for index in range(condition_start, condition_end):
        phase = (index - condition_start) / (condition_end - condition_start - 1)
        temp[index] = round(29.0 + 12.0 * phase, 9)
    temp_target = _target(
        channel=_channel("environment", "temp_c"),
        start_index=condition_start,
        end_index_exclusive=condition_end,
        rate_hz=SPECS["environment/temp_c"].rate_hz,
        magnitude_metric="max_supported_bound_excess",
        magnitude_value=max(temp[condition_start:condition_end]) - clean_temp_max,
        magnitude_unit="degC",
        reference_description="clean bag observed maximum temp_c",
        reference_value=clean_temp_max,
        reference_unit="degC",
        times=base_times["environment"],
    )
    _write_bag(
        output,
        "preflight_cross_record",
        seed,
        condition,
        base_times,
        all_gnss_indices,
        [
            {
                "description": "Environment temp_c leaves the demo nominal-op condition band.",
                "id": "preflight-temp-condition-exit",
                "targets": [temp_target],
                "type": "condition-envelope-exit",
            }
        ],
    )

    # Stage 4b — stationary |g| under bench-idle; lateral accel pushes |g| out of band
    # while accel_z stays inside the declared envelope (stage-3 silent).
    g_fail = {name: values.copy() for name, values in base.items()}
    accel_x = g_fail["device/dev.imu/accel_x"]
    for index in range(len(accel_x)):
        accel_x[index] = round(accel_x[index] + 5.0, 9)
    g_target = _target(
        channel=_channel("device", "accel_x", "dev.imu"),
        start_index=0,
        end_index_exclusive=len(accel_x),
        rate_hz=SPECS["device/dev.imu/accel_x"].rate_hz,
        magnitude_metric="lateral_accel_offset",
        magnitude_value=5.0,
        magnitude_unit="m/s^2",
        reference_description="added lateral accel to push |g| above envelope-derived band",
        reference_value=0.0,
        reference_unit="m/s^2",
        times=base_times["dev.imu"],
    )
    _write_bag(
        output,
        "preflight_cross_signal",
        seed,
        g_fail,
        base_times,
        all_gnss_indices,
        [
            {
                "description": (
                    "Under bench-idle, a +5 m/s^2 accel_x offset pushes mean |g| "
                    "outside the envelope-derived band while accel_z stays in-band."
                ),
                "id": "preflight-stationary-g-excursion",
                "targets": [g_target],
                "type": "stationary-g-excursion",
            }
        ],
        context="bench-idle",
    )

    # Stage 3 — frozen-but-publishing on IMU accel_z (rate/range/validity still pass).
    # Same plant shape as frozen_but_publishing; separate bag so the demo contract
    # set can lead with it without sharing the standing baseline bag's identity.
    demo_frozen = {name: values.copy() for name, values in base.items()}
    demo_frozen_channel = demo_frozen["device/dev.imu/accel_z"]
    demo_frozen_start = 3000
    demo_frozen_value = demo_frozen_channel[demo_frozen_start - 1]
    demo_frozen_channel[demo_frozen_start:] = [demo_frozen_value] * (
        len(demo_frozen_channel) - demo_frozen_start
    )
    demo_frozen_target = _target(
        channel=_channel("device", "accel_z", "dev.imu"),
        start_index=demo_frozen_start,
        end_index_exclusive=len(demo_frozen_channel),
        rate_hz=SPECS["device/dev.imu/accel_z"].rate_hz,
        magnitude_metric="post_freeze_peak_to_peak",
        magnitude_value=0.0,
        magnitude_unit="m/s^2",
        reference_description="value held after the last healthy sample",
        reference_value=demo_frozen_value,
        reference_unit="m/s^2",
        times=base_times["dev.imu"],
    )
    _write_bag(
        output,
        "preflight_frozen",
        seed,
        demo_frozen,
        base_times,
        all_gnss_indices,
        [
            {
                "description": (
                    "IMU accel_z frozen-but-publishing (stage-3 oneshot variance only); "
                    "rate, range, and validity still pass."
                ),
                "id": "preflight-imu-frozen-but-publishing",
                "targets": [demo_frozen_target],
                "type": "frozen-but-publishing",
            }
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).parent)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    generate(args.output, args.seed)


if __name__ == "__main__":
    main()
