import hashlib
import math
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from lockwire_pro.recording import load_recording

BAGS_ROOT = Path(__file__).parent / "fixtures" / "bags"
GENERATOR = BAGS_ROOT / "generate.py"
BAG_IDS = {
    "clean",
    "clean_b",
    "condition_envelope_exit",
    "frozen_but_publishing",
    "range_excursion",
    "rate_collapse",
}
# B93 planted-failure demo bags (generated alongside the standing set).
PREFLIGHT_BAG_IDS = {
    "preflight_cross_record",
    "preflight_cross_signal",
    "preflight_flow",
    "preflight_frozen",
    "preflight_sanity",
}
ALL_BAG_IDS = BAG_IDS | PREFLIGHT_BAG_IDS
PREFLIGHT_BENCH_IDLE = {"preflight_cross_signal"}


def _hash_tree(path):
    return {
        file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def _load_manifest(path):
    return YAML(typ="safe").load((path / "manifest.yaml").read_text())


def test_generator_is_byte_deterministic_for_same_seed(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    for output in (first, second):
        subprocess.run(
            [sys.executable, str(GENERATOR), "--output", str(output), "--seed", "8675309"],
            check=True,
        )

    first_hashes = _hash_tree(first)
    second_hashes = _hash_tree(second)
    assert first_hashes
    assert first_hashes == second_hashes


def test_checked_in_bags_match_default_generator_output(tmp_path):
    regenerated = tmp_path / "regenerated"
    subprocess.run([sys.executable, str(GENERATOR), "--output", str(regenerated)], check=True)

    checked_in_hashes = {
        file.relative_to(BAGS_ROOT).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(BAGS_ROOT.rglob("*"))
        if file.is_file() and file.name in {"manifest.yaml", "samples.jsonl"}
    }
    assert checked_in_hashes == _hash_tree(regenerated)


@pytest.mark.parametrize("bag_id", sorted(ALL_BAG_IDS))
def test_generated_bag_loads_cleanly(bag_id):
    recording = load_recording(BAGS_ROOT / bag_id)

    expected_context = "bench-idle" if bag_id in PREFLIGHT_BENCH_IDLE else "nominal-op"
    assert recording.context == expected_context
    assert set(recording.devices) == {"dev.barometer", "dev.gnss", "dev.imu"}
    assert set(recording.env) == {"condition_altitude_m", "temp_c"}
    assert len(recording.sha256) == 64


def test_bag_set_and_manifests_are_well_formed():
    # generate.py lives in BAGS_ROOT; importing it creates __pycache__/, which is not a bag.
    generated_dirs = {
        path.name for path in BAGS_ROOT.iterdir() if path.is_dir() and path.name != "__pycache__"
    }
    assert generated_dirs == ALL_BAG_IDS

    for bag_id in sorted(ALL_BAG_IDS):
        manifest = _load_manifest(BAGS_ROOT / bag_id)
        assert set(manifest) == {
            "bag",
            "channel_models",
            "context",
            "format_version",
            "generator",
            "planted_failures",
            "seed",
            "units",
        }
        assert manifest["format_version"] == "1.0"
        assert manifest["bag"] == {"duration_s": 30.0, "id": bag_id}
        expected_context = "bench-idle" if bag_id in PREFLIGHT_BENCH_IDLE else "nominal-op"
        assert manifest["context"] == expected_context
        assert manifest["generator"] == {
            "algorithm_version": "2",
            "path": "tests/fixtures/bags/generate.py",
        }
        assert isinstance(manifest["seed"], int)
        assert isinstance(manifest["units"], dict) and manifest["units"]
        assert set(manifest["units"]) == {
            "dev.barometer",
            "dev.gnss",
            "dev.imu",
            "environment",
        }
        assert all(
            isinstance(device_units, dict) and device_units
            for device_units in manifest["units"].values()
        )
        assert isinstance(manifest["channel_models"], dict) and manifest["channel_models"]
        failures = manifest["planted_failures"]
        assert isinstance(failures, list)
        assert len(failures) == (0 if bag_id in {"clean", "clean_b"} else 1)

        for model in manifest["channel_models"].values():
            assert set(model) == {
                "ar1_phi",
                "drift",
                "e_folding_samples",
                "e_folding_s",
                "innovation_std",
                "integrated_autocorrelation_samples",
                "model",
                "sample_rate_hz",
                "thermal_transient",
                "timing_jitter",
                "vibration",
            }
            assert 0.0 < model["ar1_phi"] < 1.0
            assert model["innovation_std"] > 0.0
            assert model["sample_rate_hz"] > 0.0
            assert model["e_folding_samples"] > 0.0
            assert model["e_folding_s"] > 0.0
            assert model["integrated_autocorrelation_samples"] > 1.0
            assert model["timing_jitter"] == {
                "clip_sigma": 3.0,
                "latency_ar1_phi": 0.35,
                "latency_std_fraction_of_period": 0.02,
                "latency_std_max_s": 0.0005,
                "model": "bounded_ar1_scheduling_latency",
            }

        for failure in failures:
            assert set(failure) == {"description", "id", "targets", "type"}
            assert all(
                isinstance(failure[key], str) and failure[key]
                for key in ("description", "id", "type")
            )
            assert isinstance(failure["targets"], list) and failure["targets"]
            for target in failure["targets"]:
                assert set(target) == {
                    "channel",
                    "magnitude",
                    "sample_range",
                    "time_range_s",
                }
                assert set(target["sample_range"]) == {
                    "end_index_exclusive",
                    "start_index",
                }
                assert set(target["time_range_s"]) == {"end_inclusive", "start"}
                assert set(target["magnitude"]) == {"metric", "reference", "unit", "value"}
                assert set(target["magnitude"]["reference"]) == {
                    "description",
                    "unit",
                    "value",
                }
                assert all(
                    isinstance(target["magnitude"][key], str) and target["magnitude"][key]
                    for key in ("metric", "unit")
                )
                assert all(
                    isinstance(target["magnitude"]["reference"][key], str)
                    and target["magnitude"]["reference"][key]
                    for key in ("description", "unit")
                )
                assert math.isfinite(target["magnitude"]["value"])
                assert math.isfinite(target["magnitude"]["reference"]["value"])


def test_clean_b_is_an_independent_draw_with_realistic_timing_jitter():
    clean_a_manifest = _load_manifest(BAGS_ROOT / "clean")
    clean_b_manifest = _load_manifest(BAGS_ROOT / "clean_b")
    clean_a = load_recording(BAGS_ROOT / "clean")
    clean_b = load_recording(BAGS_ROOT / "clean_b")

    assert clean_b_manifest["seed"] == clean_a_manifest["seed"] + 1
    assert clean_b_manifest["generator"] == clean_a_manifest["generator"]
    assert clean_b.sha256 != clean_a.sha256

    for recording in (clean_a, clean_b):
        streams = [device.recv_times for device in recording.devices.values()]
        streams.extend([[time for time, _ in samples] for samples in recording.env.values()])
        for times in streams:
            deltas = {round(right - left, 9) for left, right in pairwise(times)}
            assert len(deltas) > 1
            assert min(deltas) > 0.0


@pytest.mark.parametrize("bag_id", sorted(ALL_BAG_IDS - {"clean", "clean_b"}))
def test_planted_ranges_name_channels_that_exist_in_the_bag(bag_id):
    recording = load_recording(BAGS_ROOT / bag_id)
    manifest = _load_manifest(BAGS_ROOT / bag_id)

    for failure in manifest["planted_failures"]:
        for target in failure["targets"]:
            channel = target["channel"]
            if channel["kind"] == "device":
                assert set(channel) == {"device", "kind", "name"}
                stream = recording.devices[channel["device"]]
                assert channel["name"] in stream.signals
                times = stream.recv_times
            else:
                assert channel["kind"] == "environment"
                assert set(channel) == {"kind", "name"}
                times = [time for time, _ in recording.env[channel["name"]]]

            assert isinstance(
                recording.unit_of(
                    channel["name"],
                    device=channel.get("device"),
                ),
                str,
            )
            start = target["sample_range"]["start_index"]
            end = target["sample_range"]["end_index_exclusive"]
            assert isinstance(start, int) and isinstance(end, int)
            assert 0 <= start < end <= len(times)
            assert times[start] == pytest.approx(target["time_range_s"]["start"])
            assert times[end - 1] == pytest.approx(target["time_range_s"]["end_inclusive"])
