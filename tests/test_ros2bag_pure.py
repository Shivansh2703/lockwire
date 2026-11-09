"""Pure-function tests for ros2bag internals that need no rosbag/mcap extra.

`lockwire_pro.ros2bag` imports `mcap`/`mcap_ros2` lazily inside `_messages`, so the
module itself — and anything not touching that function — imports fine in a
bare venv. Kept separate from test_ros2bag.py, whose module-level
`importorskip` skips everything below it when the extra is missing.
"""

from lockwire_pro.ros2bag import _origin_ns


def test_origin_ns_with_tied_timestamps_does_not_raise():
    # MUTATION-BITE: two messages sharing one log_time on a topic used to hit
    # `sorted((log_time_ns, dict))`, which raises TypeError comparing dicts.
    collected = {
        "dev.imu": [(100, {"a": 1.0}), (100, {"a": 2.0}), (50, {"a": 0.0})],
        "dev.gps": [(200, {"b": 1.0})],
    }
    assert _origin_ns(collected) == 50


def test_origin_ns_picks_earliest_timestamp_across_devices():
    collected = {
        "dev.a": [(30, {}), (10, {})],
        "dev.b": [(20, {})],
    }
    assert _origin_ns(collected) == 10
