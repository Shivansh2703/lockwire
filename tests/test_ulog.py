import struct
from pathlib import Path

import pytest

from lockwire_pro.baseline import build_baseline
from lockwire_pro.recording import Recording
from lockwire_pro.ulog import load_ulog

FIXTURES = Path(__file__).parent / "fixtures" / "ulog"
LOG_ID = "18a01095-f04e-4801-a9f3-4040190fa3bb"
ULOG = FIXTURES / f"{LOG_ID}.ulg"


def test_real_ulog_loads_as_recording_and_builds_baseline():
    recording = load_ulog(ULOG)

    assert isinstance(recording, Recording)
    assert recording.context == "nominal-op"
    assert recording.sha256 == "add3ac008e0b2da6d537e1df0b9296134e973192634c840c3738ccef4edbe8b4"
    assert set(recording.devices) == {
        "px4.sensor_accel.0",
        "px4.sensor_accel.1",
        "px4.sensor_accel.2",
        "px4.sensor_baro.0",
        "px4.sensor_baro.1",
        "px4.sensor_combined.0",
        "px4.sensor_gyro.0",
        "px4.sensor_gyro.1",
        "px4.sensor_gyro.2",
    }
    raw_counts = {device: len(stream.recv_times) for device, stream in recording.devices.items()}
    assert raw_counts["px4.sensor_combined.0"] > 93
    assert raw_counts["px4.sensor_baro.0"] < 93
    assert recording.unit_of("accel_x", device="px4.sensor_accel.0") == "m/s^2"
    assert recording.unit_of("pressure_pa", device="px4.sensor_baro.0") == "Pa"

    baseline = build_baseline(recording, context=recording.context)

    assert baseline.recording_sha256 == recording.sha256
    assert baseline.envelopes.keys() == recording.devices.keys()
    assert "device/px4.sensor_accel.0/accel_z" in baseline.profiles
    assert baseline.profiles["rate/px4.sensor_combined.0"].status == "locked"
    assert baseline.profiles["rate/px4.sensor_baro.0"].status == "under-sampled"


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"not a ULog file!", "not a ULog file"),
        (ULOG.read_bytes()[:15], "not a ULog file"),
    ],
    ids=["bad-magic", "truncated-header"],
)
def test_malformed_ulog_header_raises_cleanly(tmp_path, contents, message):
    malformed = tmp_path / "malformed.ulg"
    malformed.write_bytes(contents)

    with pytest.raises(ValueError, match=message):
        load_ulog(malformed)


def test_mid_message_truncation_raises_cleanly(tmp_path):
    malformed = tmp_path / "truncated-message.ulg"
    malformed.write_bytes(ULOG.read_bytes()[:-1])

    with pytest.raises(ValueError, match="ends inside a message payload"):
        load_ulog(malformed)


def test_corrupt_message_length_raises_cleanly(tmp_path):
    contents = ULOG.read_bytes()[:16] + struct.pack("<HB", 0xFFFF, ord("I"))
    malformed = tmp_path / "corrupt-length.ulg"
    malformed.write_bytes(contents)

    with pytest.raises(ValueError, match="ends inside a message payload"):
        load_ulog(malformed)


def _minimal_ulog(*messages: bytes) -> bytes:
    """Build a ULog header plus the given already-framed messages."""
    return b"ULog\x01\x12\x35" + b"\x00" * 9 + b"".join(messages)


def _frame(message_type: str, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), ord(message_type)) + payload


@pytest.mark.parametrize(
    ("message", "needle"),
    [
        (_frame("F", b"sensor_accel:\xff;uint64_t timestamp"), "format description"),
        (_frame("A", struct.pack("<BH", 0, 1) + b"sensor_accel\xff"), "subscription name"),
    ],
    ids=["format-description", "subscription"],
)
def test_invalid_utf8_in_ulog_text_raises_named_value_error(tmp_path, message, needle):
    malformed = tmp_path / "bad-utf8.ulg"
    malformed.write_bytes(_minimal_ulog(message))

    with pytest.raises(
        ValueError, match=rf"invalid UTF-8 in ULog {needle} at offset \d+: b'\\xff'"
    ) as caught:
        load_ulog(malformed)

    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


# ---------------------------------------------------------------------------
# A dead channel is absent, a glitching one is a fault
# ---------------------------------------------------------------------------


def _stream(**signals):
    from lockwire_pro.recording import DeviceStream

    stream = DeviceStream(device="dev.x")
    stream.recv_times = [0.0, 0.1, 0.2]
    stream.signals = dict(signals)
    return stream


def test_d57_a_channel_that_is_never_finite_is_dropped():
    """No sensor is reporting it, so it is absent — and absence is not_scored."""
    from lockwire_pro.ulog import _drop_dead_channels

    nan = float("nan")
    devices = {"dev.x": _stream(temperature=[nan, nan, nan], voltage=[16.8, 16.7, 16.6])}
    units = {"dev.x": {"temperature": "degC", "voltage": "V"}}

    _drop_dead_channels(devices, units)

    assert "temperature" not in devices["dev.x"].signals
    assert "temperature" not in units["dev.x"], "a dropped channel must not keep a unit"
    assert devices["dev.x"].signals["voltage"] == [16.8, 16.7, 16.6]


def test_d57_a_channel_that_is_sometimes_finite_keeps_its_nans():
    """A sensor doing something wrong. Stage 3's NaN leg has to be able to see it.

    Dropping these would delete the evidence of an intermittently faulting
    sensor, which is precisely what the gate exists to catch.
    """
    import math

    from lockwire_pro.ulog import _drop_dead_channels

    nan = float("nan")
    devices = {"dev.x": _stream(temperature=[21.0, nan, 21.2])}
    units = {"dev.x": {"temperature": "degC"}}

    _drop_dead_channels(devices, units)

    kept = devices["dev.x"].signals["temperature"]
    assert len(kept) == 3
    assert any(math.isnan(v) for v in kept), "the fault evidence must survive"
    assert units["dev.x"]["temperature"] == "degC"


def test_d57_infinity_counts_as_dead_too():
    """`inf` is as unmeasured as `nan`; the guard keys on isfinite, not isnan."""
    from lockwire_pro.ulog import _drop_dead_channels

    inf = float("inf")
    devices = {"dev.x": _stream(current=[inf, -inf, inf])}
    units = {"dev.x": {"current": "A"}}

    _drop_dead_channels(devices, units)

    assert "current" not in devices["dev.x"].signals


def test_d57_an_all_finite_channel_is_untouched():
    from lockwire_pro.ulog import _drop_dead_channels

    devices = {"dev.x": _stream(voltage=[16.8, 16.7, 16.6])}
    units = {"dev.x": {"voltage": "V"}}

    _drop_dead_channels(devices, units)

    assert devices["dev.x"].signals["voltage"] == [16.8, 16.7, 16.6]
    assert units["dev.x"] == {"voltage": "V"}


def test_d57_an_empty_channel_is_not_mistaken_for_a_dead_one():
    """No samples at all is a different thing from samples that are all NaN."""
    from lockwire_pro.ulog import _drop_dead_channels

    devices = {"dev.x": _stream(voltage=[])}
    units = {"dev.x": {"voltage": "V"}}

    _drop_dead_channels(devices, units)

    assert "voltage" in devices["dev.x"].signals


def test_d57_the_committed_real_flight_still_loads_unchanged():
    """Regression floor: the fixtures that worked before dead-channel handling must be untouched."""
    from lockwire_pro.ulog import load_ulog

    recording = load_ulog(ULOG)

    assert recording.devices, "the committed real flight must still produce devices"
    for device_id, stream in recording.devices.items():
        assert stream.signals, f"{device_id} lost every channel"


def test_d57_decoding_a_non_finite_field_does_not_refuse_the_file():
    """The half of dead-channel handling the fixtures cannot reach.

    No committed .ulg contains a NaN, so nothing else in this suite exercises
    the decode path that used to raise `ULog physical field 'temperature'
    contains a non-finite value` and abort the whole load. Reinstating that
    raise passes every other test in this file — measured, not assumed — which
    is exactly the "code no fixture reaches" shape. This is the pin.
    """
    import math

    from lockwire_pro.ulog import _value

    assert math.isnan(_value({"temperature": float("nan")}, "temperature"))
    assert math.isinf(_value({"current": float("inf")}, "current"))
    # Indexed array fields take the same path.
    assert math.isnan(
        _value({"accelerometer_m_s2": [1.0, float("nan"), 3.0]}, "accelerometer_m_s2[1]")
    )
    # ...and finite values are unchanged.
    assert _value({"voltage_v": 16.8}, "voltage_v") == 16.8


def test_older_battery_log_without_temperature_loads_with_the_channel_absent(tmp_path):
    fmt = "battery_status:uint64_t timestamp;float voltage_v;float current_a;float remaining;"
    raw = _minimal_ulog(
        _frame("F", fmt.encode()),
        _frame("A", struct.pack("<BH", 0, 0) + b"battery_status"),
        *(
            _frame("D", struct.pack("<H", 0) + struct.pack("<Qfff", t, 22.2, 1.0, 0.8))
            for t in (1_000_000, 1_100_000)
        ),
    )
    path = tmp_path / "old.ulg"
    path.write_bytes(raw)
    signals = load_ulog(path).devices["px4.battery_status.0"].signals
    assert set(signals) == {"voltage", "current", "remaining"}
