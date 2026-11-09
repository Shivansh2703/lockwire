"""Reading real rosbag2 MCAP files, on a laptop with no ROS installed.

Every bag below is written by `mcap_ros2.writer` in this process — real MCAP
bytes with real ROS 2 message schemas, decoded back through the same library a
user would use. That is the property worth protecting: the whole reason this
path exists is that a team can point lockwire at a bag they already have without
installing ROS first, and a test that needed ROS to run would not be testing
that.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest
from mcap.exceptions import McapError

from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire.report import verdict
from lockwire_pro.ros2bag import PARTIAL_BAG_MARKER, Ros2BagError, bag_files, load_ros2_bag
from lockwire.schema import Contract

mcap_ros2_writer = pytest.importorskip(
    "mcap_ros2.writer", reason="needs the optional decoder: pip install 'lockwire[ros2bag]'"
)

IMU_SCHEMA = """\
std_msgs/Header header
geometry_msgs/Quaternion orientation
float64[9] orientation_covariance
geometry_msgs/Vector3 angular_velocity
float64[9] angular_velocity_covariance
geometry_msgs/Vector3 linear_acceleration
float64[9] linear_acceleration_covariance
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
================================================================================
MSG: geometry_msgs/Quaternion
float64 x
float64 y
float64 z
float64 w
================================================================================
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
"""


def _imu(accel_z: float) -> dict:
    zeros = [0.0] * 9
    return {
        "header": {"stamp": {"sec": 0, "nanosec": 0}, "frame_id": "imu_link"},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "orientation_covariance": zeros,
        "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "angular_velocity_covariance": zeros,
        "linear_acceleration": {"x": 0.0, "y": 0.0, "z": accel_z},
        "linear_acceleration_covariance": zeros,
    }


def write_bag(
    path: Path,
    topic: str = "/imu/data",
    count: int = 300,
    period_s: float = 0.01,
    accel_z: float = -9.81,
    start_ns: int = 1_700_000_000_000_000_000,
) -> Path:
    """A genuine MCAP bag with a real sensor_msgs/msg/Imu schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = mcap_ros2_writer.Writer(stream)
        schema = writer.register_msgdef("sensor_msgs/msg/Imu", IMU_SCHEMA)
        for i in range(count):
            stamp = start_ns + int(i * period_s * 1_000_000_000)
            writer.write_message(topic, schema, _imu(accel_z), log_time=stamp, publish_time=stamp)
        writer.finish()
    return path


CONTRACT_DICT = {
    "schema_version": "0.3",
    "robot": {"name": "bagbot"},
    "families": [{"id": "fam.imu", "name": "MEMS IMU"}],
    "device_models": [{"id": "mdl.imu", "family": "fam.imu"}],
    "devices": [
        {
            "id": "dev.imu",
            "model": "mdl.imu",
            "owner": "shivansh",
            "driver": {
                "node": "/imu_driver",
                "topic": "/imu/data",
                "msg_type": "sensor_msgs/msg/Imu",
                "nominal_rate_hz": 100.0,
                "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
            },
        }
    ],
}


@pytest.fixture
def contract() -> Contract:
    return Contract.model_validate(CONTRACT_DICT)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_reads_a_real_mcap_bag_with_no_ros_installed(tmp_path, contract):
    """The load-bearing claim: this ran on a machine with no ROS on it."""
    bag = write_bag(tmp_path / "run.mcap")

    recording = load_ros2_bag(bag, contract)

    assert recording.source == "ros2bag"
    assert set(recording.devices) == {"dev.imu"}
    assert len(recording.devices["dev.imu"].recv_times) == 300
    assert recording.unit_of("accel_z", device="dev.imu") == "m/s^2"
    assert recording.devices["dev.imu"].signals["accel_z"][0] == pytest.approx(-9.81)
    assert recording.sha256
    # E3-FL: a genuine capture's messages are already receipt-order monotonic.
    assert recording.receipt_monotonic is True
    assert recording.partial is False


def test_out_of_order_log_times_are_reported_not_hidden(tmp_path, contract):
    """E3-FL MUTATION-BITE: the receipt-order fact must go red on a real anomaly.

    Before E3-FL, `_messages` read the bag with mcap's default
    `log_time_order=True`, which silently sorts — a bag written with reversed
    timestamps read back monotonic with no trace. Reading file order instead
    (`log_time_order=False`) makes the anomaly observable as
    `receipt_monotonic=False`, without changing what `recv_times` end up
    holding (still sorted, for every other stage-2/3 check to use).
    """
    bag = tmp_path / "run.mcap"
    bag.parent.mkdir(parents=True, exist_ok=True)
    with bag.open("wb") as stream:
        writer = mcap_ros2_writer.Writer(stream)
        schema = writer.register_msgdef("sensor_msgs/msg/Imu", IMU_SCHEMA)
        base = 1_700_000_000_000_000_000
        # Written out of log-time order: +0.1s, +0.0s, +0.2s.
        for offset_ns in (100_000_000, 0, 200_000_000):
            writer.write_message(
                "/imu/data",
                schema,
                _imu(-9.81),
                log_time=base + offset_ns,
                publish_time=base + offset_ns,
            )
        writer.finish()

    recording = load_ros2_bag(bag, contract)

    assert recording.receipt_monotonic is False
    # Downstream still gets a well-formed, sorted stream — the anomaly is
    # reported, not left to corrupt every other stage-2/3 check.
    assert recording.devices["dev.imu"].recv_times == [0.0, pytest.approx(0.1), pytest.approx(0.2)]


def test_partial_is_never_inferred_only_declared(tmp_path, contract):
    """A caller marks a cut-short capture; the reader never guesses it (E3-QC)."""
    bag = write_bag(tmp_path / "run.mcap")

    assert load_ros2_bag(bag, contract).partial is False
    assert load_ros2_bag(bag, contract, partial=True).partial is True


def test_retained_cancelled_bag_marker_preserves_partial_identity(tmp_path, contract):
    bag_dir = tmp_path / "bag"
    write_bag(bag_dir / "run.mcap")
    complete = load_ros2_bag(bag_dir, contract)
    marker = bag_dir / "lockwire-partial.json"
    marker.write_bytes(PARTIAL_BAG_MARKER)

    retained = load_ros2_bag(bag_dir, contract)
    assert retained.partial is True
    assert retained.sha256 != complete.sha256

    marker.write_bytes(b'{"partial":true}\n')
    with pytest.raises(Ros2BagError, match="invalid partial-capture marker"):
        load_ros2_bag(bag_dir, contract)


def test_log_time_becomes_recv_time_at_the_right_rate(tmp_path, contract):
    """100 Hz in the bag must read back as ~100 Hz of receive times."""
    recording = load_ros2_bag(write_bag(tmp_path / "run.mcap", period_s=0.01), contract)

    times = recording.devices["dev.imu"].recv_times
    assert times[0] == 0.0  # rebased
    assert recording.devices["dev.imu"].duration_s == pytest.approx(2.99, abs=0.01)
    intervals = [b - a for a, b in zip(times, times[1:])]
    assert min(intervals) == pytest.approx(0.01, abs=1e-6)


def test_a_bag_with_tied_log_timestamps_does_not_crash(tmp_path, contract):
    # MUTATION-BITE: two messages sharing one log_time on a topic used to raise
    # TypeError inside sorted((log_time_ns, dict)) — dicts don't order.
    bag = write_bag(tmp_path / "run.mcap", count=2, period_s=0.0)

    recording = load_ros2_bag(bag, contract)

    assert len(recording.devices["dev.imu"].recv_times) == 2


def test_absolute_epoch_nanoseconds_do_not_quantise_the_intervals(tmp_path, contract):
    """Why rebasing matters: float64 cannot hold 1.7e18 ns at sub-ms resolution.

    A 1 ms cadence at an absolute epoch would round to garbage if the loader
    kept log_time absolute. This pins the intervals a rate check reads.
    """
    recording = load_ros2_bag(write_bag(tmp_path / "run.mcap", period_s=0.001, count=200), contract)

    intervals = [
        b - a
        for a, b in zip(
            recording.devices["dev.imu"].recv_times,
            recording.devices["dev.imu"].recv_times[1:],
        )
    ]
    assert all(i == pytest.approx(0.001, abs=1e-9) for i in intervals)


def test_a_split_bag_directory_is_read_as_one_recording(tmp_path, contract):
    """rosbag2 splits large bags into numbered parts inside a directory."""
    bag_dir = tmp_path / "rosbag2_2026_08_14"
    write_bag(bag_dir / "rosbag2_0.mcap", count=100, start_ns=1_700_000_000_000_000_000)
    write_bag(bag_dir / "rosbag2_1.mcap", count=100, start_ns=1_700_000_001_000_000_000)

    recording = load_ros2_bag(bag_dir, contract)

    assert len(recording.devices["dev.imu"].recv_times) == 200
    times = recording.devices["dev.imu"].recv_times
    assert times == sorted(times), "split parts must not produce backwards receive times"


def test_a_bag_recording_runs_through_the_real_gate(tmp_path, contract):
    """Loading is not scoring."""
    recording = load_ros2_bag(write_bag(tmp_path / "run.mcap"), contract)

    report = run_preflight(contract, RecordedSource(recording))

    assert verdict(report).exit_code == 0
    assert report.scored > 0  # presence floor


def test_a_rate_collapsed_bag_is_refused_by_the_gate(tmp_path, contract):
    """5 Hz against a 100 Hz nominal is below the 50% gross-liveness floor."""
    bag = write_bag(tmp_path / "slow.mcap", count=30, period_s=0.2)

    report = run_preflight(contract, RecordedSource(load_ros2_bag(bag, contract)))

    decision = verdict(report)
    assert decision.exit_code == 1
    assert any(
        f.quantity == "devices/dev.imu/sustained_rate_liveness"
        for f in report
        if f.severity == "violation"
    ), [f.quantity for f in report if f.severity == "violation"]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_a_db3_bag_is_refused_by_name_not_half_read(tmp_path, contract):
    """A partial implementation that skipped what it could not decode would be worse."""
    db3 = tmp_path / "run.db3"
    db3.write_bytes(b"SQLite format 3\x00")

    with pytest.raises(Ros2BagError, match="SQLite3"):
        load_ros2_bag(db3, contract)


def test_a_db3_only_bag_directory_is_refused(tmp_path, contract):
    bag_dir = tmp_path / "rosbag2_old"
    bag_dir.mkdir()
    (bag_dir / "rosbag2_old_0.db3").write_bytes(b"SQLite format 3\x00")

    with pytest.raises(Ros2BagError, match="SQLite3") as refused:
        load_ros2_bag(bag_dir, contract)
    assert f"ros2 bag convert -i {bag_dir} -o to_mcap.yaml" in str(refused.value)


def test_a_bag_without_the_declared_topic_names_both_sides(tmp_path, contract):
    """Silence here would produce an empty recording and a vacuous GO."""
    bag = write_bag(tmp_path / "other.mcap", topic="/some/other/topic")

    with pytest.raises(Ros2BagError) as caught:
        load_ros2_bag(bag, contract)

    assert "/imu/data" in str(caught.value)  # what the contract wanted
    assert "/some/other/topic" in str(caught.value)  # what the bag had


def test_a_contract_with_no_signal_mappings_is_refused(tmp_path):
    """Without the required fields there is nothing to read; say so rather than return empty."""
    plain = Contract.model_validate(
        {
            **CONTRACT_DICT,
            "devices": [
                {
                    "id": "dev.imu",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {"topic": "/imu/data"},
                }
            ],
        }
    )
    with pytest.raises(Ros2BagError, match="nothing to read"):
        load_ros2_bag(write_bag(tmp_path / "run.mcap"), plain)


def test_a_bad_field_path_is_refused_not_defaulted(tmp_path):
    broken = Contract.model_validate(
        {
            **CONTRACT_DICT,
            "devices": [
                {
                    "id": "dev.imu",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {
                        "topic": "/imu/data",
                        "msg_type": "sensor_msgs/msg/Imu",
                        "signals": {"accel_z": {"field": "linear_acceleration.w", "unit": "m/s^2"}},
                    },
                }
            ],
        }
    )
    with pytest.raises(Ros2BagError, match="does not resolve"):
        load_ros2_bag(write_bag(tmp_path / "run.mcap"), broken)


def test_two_devices_on_one_topic_is_refused(tmp_path):
    """A bag message cannot belong to two devices; guessing would be worse."""
    duplicated = Contract.model_validate(
        {
            **CONTRACT_DICT,
            "devices": [
                {
                    "id": f"dev.imu{n}",
                    "model": "mdl.imu",
                    "owner": "shivansh",
                    "driver": {
                        "topic": "/imu/data",
                        "msg_type": "sensor_msgs/msg/Imu",
                        "signals": {"accel_z": {"field": "linear_acceleration.z", "unit": "m/s^2"}},
                    },
                }
                for n in (1, 2)
            ],
        }
    )
    with pytest.raises(Ros2BagError, match="two devices"):
        load_ros2_bag(write_bag(tmp_path / "run.mcap"), duplicated)


def test_an_unknown_context_is_refused(tmp_path, contract):
    with pytest.raises(Ros2BagError, match="unknown context"):
        load_ros2_bag(write_bag(tmp_path / "run.mcap"), contract, context="in-flight")


def test_a_missing_bag_is_refused(tmp_path, contract):
    with pytest.raises(Ros2BagError, match="no such bag"):
        load_ros2_bag(tmp_path / "nope.mcap", contract)


def test_an_empty_bag_directory_is_refused(tmp_path, contract):
    empty = tmp_path / "rosbag2_empty"
    empty.mkdir()
    with pytest.raises(Ros2BagError, match="no .mcap files"):
        load_ros2_bag(empty, contract)


def test_bag_files_lists_split_parts_in_order(tmp_path):
    bag_dir = tmp_path / "rosbag2_split"
    write_bag(bag_dir / "rosbag2_split_1.mcap", count=1)
    write_bag(bag_dir / "rosbag2_split_0.mcap", count=1)

    assert [p.name for p in bag_files(bag_dir)] == [
        "rosbag2_split_0.mcap",
        "rosbag2_split_1.mcap",
    ]


@pytest.mark.parametrize("junk", [b"\x01\x02junk", b"\x89MCAP0\r\n" + bytes(range(256)) * 3])
def test_a_corrupt_bag_is_refused_with_one_plain_line(tmp_path, contract, junk):
    from typer.testing import CliRunner

    from lockwire.cli import app

    (tmp_path / "bad.mcap").write_bytes(junk)
    lock = tmp_path / "lockwire.yaml"
    lock.write_text(json.dumps(CONTRACT_DICT))
    argv = ["preflight", str(lock), "--from", str(tmp_path / "bad.mcap"), "--no-ledger"]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2 and "not a readable MCAP" in result.output
    assert not isinstance(result.exception, (struct.error, McapError))


# ---------------------------------------------------------------------------
# bag-first: JointState arrays, `bag inspect`, `bag check`, the sample
# ---------------------------------------------------------------------------


def _joint_bag(path: Path, frames: list[tuple[list[str], list[float]]]) -> Path:
    from lockwire_pro.ros2bag_example import write_bag as write_any

    header = {"stamp": {"sec": 0, "nanosec": 0}, "frame_id": ""}
    msgs = [
        (i * 20_000_000, {"header": header, "name": n, "position": p, "velocity": [], "effort": []})
        for i, (n, p) in enumerate(frames)
    ]
    return write_any(path, [("/joint_states", "sensor_msgs/msg/JointState", msgs)])


def _joint_contract(field: str) -> Contract:
    device = {**CONTRACT_DICT["devices"][0], "driver": {
        "topic": "/joint_states", "msg_type": "sensor_msgs/msg/JointState",
        "signals": {"knee": {"field": field, "unit": "rad"}}}}  # fmt: skip
    return Contract.model_validate({**CONTRACT_DICT, "devices": [device]})


def test_a_joint_is_read_by_name_even_when_the_driver_reorders_joints(tmp_path):
    bag = _joint_bag(
        tmp_path / "j.mcap", [(["hip", "knee"], [0.1, 0.2]), (["knee", "hip"], [0.3, 0.4])]
    )
    recording = load_ros2_bag(bag, _joint_contract("position[name=knee]"))
    assert recording.devices["dev.imu"].signals["knee"] == [0.2, 0.3]
    by_index = load_ros2_bag(bag, _joint_contract("position[1]"))
    assert by_index.devices["dev.imu"].signals["knee"] == [0.2, 0.4]


@pytest.mark.parametrize(
    ("field", "said"),
    [
        ("effort[name=knee]", "effort has 0 entries"),
        ("position[name=ankle]", "present: ['hip', 'knee']"),
    ],
)
def test_a_joint_field_that_is_not_in_the_bag_is_refused(tmp_path, field, said):
    bag = _joint_bag(tmp_path / "j.mcap", [(["hip", "knee"], [0.1, 0.2])])
    with pytest.raises(Ros2BagError, match=re.escape(said)):
        load_ros2_bag(bag, _joint_contract(field))


def test_a_joint_name_that_appears_twice_is_refused_not_first_match():
    from lockwire.fieldpath import FieldPathError, resolve

    with pytest.raises(FieldPathError, match="'knee' appears 2 times in name"):
        resolve({"name": ["knee", "knee"], "position": [0.1, 0.2]}, "position[name=knee]")


def test_bag_inspect_lists_topics_rates_and_the_lines_to_write():
    from typer.testing import CliRunner

    from lockwire.cli import app

    result = CliRunner().invoke(app, ["bag", "inspect", "--example", "--json"])
    assert result.exit_code == 0, result.output
    inventory = json.loads(result.output)
    rows = {row["topic"]: row for row in inventory["topics"]}
    assert rows["/joint_states"] == {
        "topic": "/joint_states", "msg_type": "sensor_msgs/msg/JointState", "messages": 150,
        "example_field": "position[name=hip_joint]", "rate_hz": 50.0}  # fmt: skip
    assert rows["/imu/data"]["rate_hz"] == 100.0 and rows["/battery_state"]["messages"] == 30
    assert inventory["duration_s"] == pytest.approx(2.99)
    human = CliRunner().invoke(app, ["bag", "inspect", "--example"]).output
    assert 'field: "voltage", unit: UNIT' in human  # the unit is typed, never guessed


def test_bag_check_on_the_sample_is_go_and_a_db3_is_refused_with_the_command(tmp_path):
    from typer.testing import CliRunner

    from lockwire.cli import app

    go = CliRunner().invoke(app, ["bag", "check", "--example"])
    assert go.exit_code == 0 and "GO — records agree" in go.output, go.output
    db3 = tmp_path / "run.db3"
    db3.write_bytes(b"SQLite format 3\x00")
    refused = CliRunner().invoke(app, ["bag", "inspect", str(db3)])
    assert refused.exit_code == 2 and "ros2 bag convert -i" in refused.output
