"""Tests for the preflight machine-state seam (B85).

No findings, no stage logic, no CLI — only ``PreflightSource`` / ``RecordedSource``.
"""

from pathlib import Path

from lockwire_pro.preflight import (
    DeviceObservation,
    LivenessObservation,
    PreflightSource,
    RecordedSource,
)
from lockwire_pro.recording import DeviceStream, Recording, load_recording

BAGS = Path(__file__).parent / "fixtures" / "bags"


def _hand_recording(
    tmp_path: Path,
    *,
    units: dict[str, dict[str, str]],
    devices: dict[str, DeviceStream],
    env: dict[str, list[tuple[float, float]]] | None = None,
) -> Recording:
    return Recording(
        context="bench-idle",
        units=units,
        devices=devices,
        env=env or {},
        sha256="synthetic-b85",
        path=tmp_path,
    )


def test_recorded_source_satisfies_protocol():
    recording = load_recording(BAGS / "clean")
    source = RecordedSource(recording)
    assert isinstance(source, PreflightSource)


def test_clean_bag_enumerates_and_lives_only_message_producing_devices():
    recording = load_recording(BAGS / "clean")
    source = RecordedSource(recording)

    present = {obs.device_id for obs in source.enumerate()}
    live = {obs.device_id for obs in source.liveness()}

    assert present == {"dev.barometer", "dev.gnss", "dev.imu"}
    assert live == present
    assert all(isinstance(obs, DeviceObservation) for obs in source.enumerate())
    assert all(isinstance(obs, LivenessObservation) for obs in source.liveness())
    # Recorded bags carry no bus identity descriptors.
    assert all(obs.effective_strength() is None for obs in source.enumerate())
    assert all(obs.node is None and obs.topic is None for obs in source.liveness())


def test_samples_returns_recording_including_env_channels():
    recording = load_recording(BAGS / "clean")
    source = RecordedSource(recording)

    samples = source.samples()
    assert samples is recording
    assert "temp_c" in samples.env
    assert "condition_altitude_m" in samples.env
    assert "environment" not in {obs.device_id for obs in source.enumerate()}


def test_zero_message_declared_device_is_absent_and_not_live(tmp_path):
    """Negative control: units declares a device; devices holds only live ones.

    A manifest-declared device with zero messages must be ABSENT from
    enumerate() and NOT LIVE in liveness() — never silently present.
    Mutation-bite target: a liveness() that reports every units-declared
    device (minus environment) makes this assertion red.
    """
    recording = _hand_recording(
        tmp_path,
        units={
            "dev.imu": {"accel_z": "m/s^2"},
            "dev.ghost": {"accel_z": "m/s^2"},
            "environment": {"temp_c": "degC"},
        },
        devices={
            "dev.imu": DeviceStream(
                device="dev.imu",
                recv_times=[0.0, 0.1],
                signals={"accel_z": [-9.81, -9.80]},
            ),
            # Explicit empty stream: still zero messages → absent / not live.
            "dev.empty": DeviceStream(device="dev.empty"),
        },
        env={"temp_c": [(0.0, 21.3)]},
    )
    source = RecordedSource(recording)

    present_ids = [obs.device_id for obs in source.enumerate()]
    live_ids = [obs.device_id for obs in source.liveness()]

    assert present_ids == ["dev.imu"]
    assert live_ids == ["dev.imu"]
    assert "dev.ghost" not in present_ids
    assert "dev.ghost" not in live_ids
    assert "dev.empty" not in present_ids
    assert "dev.empty" not in live_ids
    assert "environment" not in present_ids
    assert "environment" not in live_ids


def test_device_observation_effective_strength_ladder():
    assert DeviceObservation(device_id="d", serial_number="SN1").effective_strength() == "instance"
    assert DeviceObservation(device_id="d", vid_pid="1234:5678").effective_strength() == "model"
    assert DeviceObservation(device_id="d", can_id="42").effective_strength() == "model"
    assert (
        DeviceObservation(device_id="d", bus_ref="bus.usb", host="host.0").effective_strength()
        == "positional"
    )
    assert (
        DeviceObservation(
            device_id="d",
            serial_number="SN1",
            strength="model",
        ).effective_strength()
        == "model"
    )
    assert DeviceObservation(device_id="d").effective_strength() is None
