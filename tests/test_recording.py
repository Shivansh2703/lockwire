import json
from pathlib import Path

import pytest

from lockwire_pro.recording import load_recording
from lockwire_pro.ulog import load_ulog

COMMITTED_BAGS = tuple(
    sorted(path.parent for path in (Path(__file__).parent / "fixtures").rglob("manifest.yaml"))
)
ULOG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "ulog" / "18a01095-f04e-4801-a9f3-4040190fa3bb.ulg"
)


def _write_recording(tmp_path, *, context="nominal-op", units=None, rows=None):
    recording = tmp_path / "rec"
    recording.mkdir()
    units = units or {
        "dev.imu": {"accel_z": "m/s^2"},
        "environment": {"temp_c": "degC"},
    }
    manifest = ["context: " + context, "units:"]
    for device, device_units in units.items():
        manifest.append(f"  {device}:")
        manifest.extend(f"    {name}: {unit}" for name, unit in device_units.items())
    (recording / "manifest.yaml").write_text("\n".join(manifest) + "\n")
    samples = "".join(json.dumps(row) + "\n" for row in (rows or []))
    (recording / "samples.jsonl").write_text(samples)
    return recording


def test_well_formed_recording_loads_cleanly(tmp_path):
    path = _write_recording(
        tmp_path,
        rows=[
            {"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}},
            {"t": 0.1, "env": {"temp_c": 21.3}},
            {"t": 0.2, "dev": "dev.imu", "sig": {"accel_z": -9.80}},
        ],
    )

    recording = load_recording(path)

    # Negative control: catches a loader that rejects a valid whitelisted context.
    assert recording.context == "nominal-op"
    # Negative control: catches loss of exact time/value index alignment on valid input.
    assert recording.devices["dev.imu"].recv_times == [0.0, 0.2]
    # Negative control: catches signal values being dropped while validating rows.
    assert recording.devices["dev.imu"].signals["accel_z"] == [-9.81, -9.8]
    # Negative control: catches environment validation discarding valid samples.
    assert recording.env["temp_c"] == [(0.1, 21.3)]
    # A manifest with no `partial` key is a complete bag, not an unmarked partial one.
    assert recording.partial is False


def test_manifest_partial_true_is_read_back(tmp_path):
    """E3-QC: a cancelled live capture is marked in the manifest, not inferred."""
    path = _write_recording(
        tmp_path, rows=[{"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}}]
    )
    with (path / "manifest.yaml").open("a") as manifest:
        manifest.write("partial: true\n")

    assert load_recording(path).partial is True


def test_manifest_partial_rejects_non_boolean(tmp_path):
    path = _write_recording(tmp_path, rows=[])
    with (path / "manifest.yaml").open("a") as manifest:
        manifest.write("partial: sort-of\n")

    with pytest.raises(ValueError, match="partial must be true or false"):
        load_recording(path)


def test_load_recording_rejects_deep_nesting_manifest_no_traceback(tmp_path):
    # MUTATION-BITE: 6 KB of `[` blows ruamel's recursive descent before any
    # other manifest check runs, raising a bare RecursionError.
    recording = tmp_path / "rec"
    recording.mkdir()
    (recording / "manifest.yaml").write_text("[" * 6000 + "]" * 6000)
    (recording / "samples.jsonl").write_text("")
    with pytest.raises(ValueError, match=r"nesting too deep"):
        load_recording(recording)


def test_rejects_ragged_device_messages(tmp_path):
    path = _write_recording(
        tmp_path,
        units={"dev.imu": {"accel_x": "m/s^2", "accel_z": "m/s^2"}},
        rows=[
            {
                "t": 0.0,
                "dev": "dev.imu",
                "sig": {"accel_x": 0.1, "accel_z": -9.81},
            },
            {"t": 0.1, "dev": "dev.imu", "sig": {"accel_z": -9.80}},
        ],
    )

    # Ragged-alignment bug: catches omitted fields desynchronizing times and values.
    with pytest.raises(ValueError, match=r"ragged signals.*missing=\['accel_x'\]"):
        load_recording(path)


def test_recording_hash_includes_manifest(tmp_path):
    path = _write_recording(
        tmp_path,
        units={"dev.imu": {"accel_z": "m/s^2"}},
        rows=[{"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}}],
    )
    original = load_recording(path)

    (path / "manifest.yaml").write_text(
        "context: nominal-op\nunits:\n  dev.imu:\n    accel_z: rad/s\n"
    )
    changed = load_recording(path)

    # Catches manifest edits (especially unit edits) being absent from provenance hash.
    assert changed.sha256 != original.sha256


@pytest.mark.parametrize(
    ("units", "rows", "missing"),
    [
        (
            {"environment": {"temp_c": "degC"}},
            [{"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}}],
            "accel_z",
        ),
        (
            {"dev.imu": {"accel_z": "m/s^2"}},
            [{"t": 0.0, "env": {"temp_c": 21.3}}],
            "temp_c",
        ),
    ],
)
def test_rejects_samples_without_declared_units(tmp_path, units, rows, missing):
    path = _write_recording(tmp_path, units=units, rows=rows)

    # Undeclared-unit bug: catches signal or environment units failing late.
    with pytest.raises(ValueError, match=rf"no unit declared.*{missing!r}"):
        load_recording(path)


def test_rejects_unknown_context(tmp_path):
    path = _write_recording(tmp_path, context="field-test")

    # Context-validation bug: catches arbitrary strings bypassing the whitelist.
    with pytest.raises(ValueError, match=r"unknown context 'field-test'.*bench-idle"):
        load_recording(path)


@pytest.mark.parametrize("unknown_key", ["contxt", "unts"])
def test_manifest_extra_rejects_unknown_top_level_key(tmp_path, unknown_key):
    path = _write_recording(tmp_path)
    with (path / "manifest.yaml").open("a") as manifest:
        manifest.write(f"{unknown_key}: typo\n")

    # Unknown-key bug: the rest of this empty recording is valid, so removing
    # top-level screening makes this assertion fail because loading succeeds.
    with pytest.raises(ValueError, match=rf"unknown manifest keys.*{unknown_key!r}"):
        load_recording(path)


@pytest.mark.parametrize("bag", COMMITTED_BAGS, ids=lambda bag: bag.name)
def test_manifest_extra_committed_bags_still_load(bag):
    recording = load_recording(bag)

    # preflight_cross_signal is deliberately bench-idle so stage-4b stationary
    # |g| scores; every other committed bag stays nominal-op.
    expected = "bench-idle" if bag.name == "preflight_cross_signal" else "nominal-op"
    assert recording.context == expected


def test_manifest_extra_ulog_does_not_consume_manifest_allowlist(monkeypatch):
    monkeypatch.setattr("lockwire_pro.recording._MANIFEST_KEYS", frozenset())

    recording = load_ulog(ULOG_FIXTURE)

    # An empty allowlist rejects every recording manifest. This load proves the
    # ULog path does not cross that boundary and is not covered by the bag audit.
    assert recording.context == "nominal-op"


def test_same_signal_can_have_different_units_per_device(tmp_path):
    path = _write_recording(
        tmp_path,
        units={
            "dev.celsius": {"temp": "degC"},
            "dev.kelvin": {"temp": "K"},
        },
        rows=[
            {"t": 0.0, "dev": "dev.celsius", "sig": {"temp": 20.0}},
            {"t": 0.0, "dev": "dev.kelvin", "sig": {"temp": 293.15}},
        ],
    )

    recording = load_recording(path)

    assert recording.unit_of("temp", device="dev.celsius") == "degC"
    assert recording.unit_of("temp", device="dev.kelvin") == "K"
    with pytest.raises(KeyError, match="device-specific units"):
        recording.unit_of("temp")


@pytest.mark.parametrize("device", ["environment", "Environment", " environment "])
def test_rejects_reserved_environment_device_scope(tmp_path, device):
    path = _write_recording(
        tmp_path,
        units={"environment": {"temp": "degC"}},
        rows=[{"t": 0.0, "dev": device, "sig": {"temp": 20.0}}],
    )

    # Reserved-scope bug: fails if a spelling a human might use can load as a device.
    with pytest.raises(KeyError, match=r"reserved unit scope 'environment'"):
        load_recording(path)


def test_rejects_reserved_environment_manifest_scope_without_device_rows(tmp_path):
    path = _write_recording(
        tmp_path,
        units={
            "environment": {"pressure": "kPa"},
            "Environment": {"temp": "degC"},
        },
        rows=[{"t": 0.0, "env": {"pressure": 101.3}}],
    )

    # Manifest-scope bug: fails only if reserved manifest keys are not checked;
    # no device row exists for the row-path guard to reject.
    with pytest.raises(KeyError, match=r"reserved unit scope 'environment'"):
        load_recording(path)


def test_reserved_environment_scope_allows_normal_device_name(tmp_path):
    path = _write_recording(
        tmp_path,
        units={"dev.environment": {"temp": "degC"}},
        rows=[{"t": 0.0, "dev": "dev.environment", "sig": {"temp": 20.0}}],
    )

    recording = load_recording(path)

    # Negative control: fails if the guard rejects IDs that merely contain the word.
    assert recording.devices["dev.environment"].signals["temp"] == [20.0]


def test_rejects_flat_unit_manifest(tmp_path):
    path = _write_recording(tmp_path)
    (path / "manifest.yaml").write_text("context: nominal-op\nunits:\n  accel_z: m/s^2\n")

    with pytest.raises(ValueError, match="units must map each device"):
        load_recording(path)


def test_env_order_bite_keeps_device_receive_time_guard_independent(tmp_path):
    path = _write_recording(
        tmp_path,
        rows=[
            {"t": 1.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}},
            {"t": 0.5, "dev": "dev.imu", "sig": {"accel_z": -9.80}},
        ],
    )

    # Sort-assumption bug: catches silently decreasing per-device receive times.
    with pytest.raises(ValueError, match=r"receive time 0.5.*precedes previous time 1.0"):
        load_recording(path)


def test_env_order_rejects_out_of_order_receive_times(tmp_path):
    path = _write_recording(
        tmp_path,
        units={"environment": {"temp_c": "degC"}},
        rows=[
            {"t": 1.0, "env": {"temp_c": 21.3}},
            {"t": 0.5, "env": {"temp_c": 21.4}},
        ],
    )

    # Environment-ordering bug: no device rows can trigger the device-path guard.
    with pytest.raises(
        ValueError,
        match=r"receive time 0.5.*environment channel 'temp_c'.*previous time 1.0",
    ):
        load_recording(path)


def test_env_order_allows_in_order_receive_times(tmp_path):
    path = _write_recording(
        tmp_path,
        units={"environment": {"temp_c": "degC"}},
        rows=[
            {"t": 0.5, "env": {"temp_c": 21.3}},
            {"t": 1.0, "env": {"temp_c": 21.4}},
        ],
    )

    recording = load_recording(path)

    assert recording.env["temp_c"] == [(0.5, 21.3), (1.0, 21.4)]


def test_dds_deadline_events_preserve_sample_order_and_reject_forged_count(tmp_path):
    path = _write_recording(
        tmp_path,
        rows=[
            {"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}},
            {"t": 0.1, "dev": "dev.imu", "dds_deadline_missed": 1},
            {"t": 0.2, "dev": "dev.imu", "sig": {"accel_z": -9.80}},
        ],
    )
    with (path / "manifest.yaml").open("a") as manifest:
        manifest.write("dds_deadlines:\n  dev.imu: 0.1\n")
    loaded = load_recording(path)
    assert loaded.dds_deadlines == {"dev.imu": 0.1}
    assert loaded.dds_deadline_misses == {"dev.imu": [(0.1, 1)]}
    assert loaded.devices["dev.imu"].recv_times == [0.0, 0.2]
    assert loaded.devices["dev.imu"].signals["accel_z"] == [-9.81, -9.8]
    assert loaded.row_count == 2  # DDS event rows are not device samples

    samples = path / "samples.jsonl"
    samples.write_text(
        samples.read_text().replace('"dds_deadline_missed": 1', '"dds_deadline_missed": 0')
    )
    with pytest.raises(ValueError, match="miss count must be positive"):
        load_recording(path)


def test_undeclared_dds_deadline_event_refuses(tmp_path):
    path = _write_recording(
        tmp_path,
        rows=[
            {"t": 0.0, "dev": "dev.imu", "sig": {"accel_z": -9.81}},
            {"t": 0.1, "dev": "dev.imu", "dds_deadline_missed": 1},
        ],
    )
    with pytest.raises(ValueError, match="undeclared or malformed DDS deadline event"):
        load_recording(path)


def test_rejects_non_finite_recording_number(tmp_path):
    # NaN timestamps/readings never reach scoring.
    rows = [{"t": float("nan"), "dev": "dev.imu", "sig": {"accel_z": 1.0}}]
    with pytest.raises(ValueError, match="non-finite"):
        load_recording(_write_recording(tmp_path, rows=rows))
