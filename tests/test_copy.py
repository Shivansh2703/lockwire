"""One plain-words map. Every check the engine can emit has a short plain label."""

import re
import pytest
from lockwire.copy import (
    CHECK_LABELS,
    check_label,
    commit_summary,
    fix_text,
    group_title,
    human_name,
    instead_row,
)


# Every contract path shape the engine emits (grep of `quantity=` in src/lockwire).
QUANTITIES = [
    "budgets/bud.power.pm02-5v",
    "budgets/bud.x/limit",
    "budgets/bud.x/rail",
    "budgets/bud.x/cg",
    "devices/dev.imu/rate_hz",
    "devices/dev.imu/recv_gap",
    "devices/dev.imu/enumeration",
    "devices/dev.imu/liveness",
    "devices/dev.imu/liveness/topic",
    "devices/dev.imu/thermal",
    "devices/dev.imu/sustained_rate_liveness",
    "devices/dev.imu/power/rail",
    "devices/dev.imu/signals/accel_x/variance",
    "devices/dev.imu/signals/accel_x/range",
    "devices/dev.imu/signals/accel_x/nan",
    "devices/dev.imu/signals/accel_x/hard_limit",
    "devices/dev.imu/stationary_|g|",
    "devices/dev.imu/drives_joint",
    "devices/dev.imu/physical/mass",
    "devices/dev.imu/physical/mount_frame",
    "devices/dev.imu/placement",
    "devices/dev.imu/model",
    "devices/dev.imu/owner",
    "devices/dev.imu/link_budget",
    "devices/dev.imu/identity/host",
    "devices/dev.imu/identity/bus_ref",
    "devices/dev.imu/dds_requested_deadline",
    "topology/wires/w1/ampacity",
    "topology/rails/rail.x/voltage",
    "topology/rails/rail.x/current_limit",
    "topology/rails/rail.x/upstream",
    "topology/rails/rail.x/efficiency",
    "topology/rails/rail.x/charged_by",
    "topology/buses/bus.x/bandwidth",
    "topology/buses/bus.x/host",
    "model/structure_hash",
    "model/links/base/mass",
    "model/joints/j1/limits/effort",
    "model/joints/j1/origin/xyz",
    "model/joints/j1/parent",
    "scene/base_link/unclaimed",
    "check/scored",
    "plan/steps/0/rail_load/rail",
    "plan/steps/0/joint_target/effort",
    "preflight/stage/2/flow",
    "preflight/stage/2/timestamps/monotonicity/live_path",
    "preflight/stage/2/timestamps/wall_clock_currency",
    "preflight/stage/4/cross_record/conditions",
    "preflight/cross_signal/stationary_|g|",
    "preflight/cross_signal/redundant_sensors",
    "preflight/cross_signal/camera_histogram",
    "preflight/recording",
    "preflight/commands/record/c1",
    "preflight/commands/channel/c1",
    "preflight/commands/in_recording",
    "preflight/coverage/comm_links_under_representative_load",
    "preflight/coverage/live_hardware_software_reconciliation",
    "preflight/live/i2c-sysfs",
    "preflight/live/device-tree",
    "devices/identity/serial_number",
    "device_models/m1/family",
]


@pytest.mark.parametrize("quantity", QUANTITIES)
def test_every_check_has_a_plain_label(quantity):
    label = check_label(quantity)
    assert 1 <= len(label.split()) <= 6, label
    assert not re.search(r"[_|/.{}]|dev\.|rail\.", label), label
    assert label.split(" \u00b7 ")[0] in CHECK_LABELS.values(), (
        f"{quantity} fell through to the fallback: {label!r}"
    )


def test_the_test_rows_the_owner_named_read_plainly():
    assert (
        check_label("preflight/stage/2/timestamps/monotonicity/live_path") == "Clock moves forward"
    )
    assert check_label("preflight/cross_signal/stationary_|g|") == "IMU at rest"
    assert check_label("devices/dev.x/enumeration") == "Device seen"


def test_part_names_are_human_and_never_ids():
    assert human_name("dev.depth_camera_front_lower_camera") == "Depth Front Lower Camera"
    assert human_name("rail.pm02-5v") == "PM02 5 V"
    assert human_name("dev.esc.esc1") == "ESC1"
    assert human_name("dev.imu.icm-42688-p") == "IMU ICM-42688-P"
    assert human_name("dev.jetson") == "Jetson"
    assert human_name("rail.payload-12v") == "Payload 12 V"
    assert human_name("dev.computer1") == "Computer 1"
    assert human_name("bus.can.dronecan") == "CAN Dronecan"
    assert (
        human_name("depth_camera_front_lower_depth_frame") == "Depth Camera Front Lower Depth Frame"
    )
    assert human_name("LF_HAA_drive") == "Left Front Hip Abduction Drive"


def test_instead_rows_are_short_with_a_kind_word_and_no_ids():
    row = instead_row(
        "move the device to rail 'rail.pixhawk-telem1-5v', which has 2.60 W free",
        "derived-by-check_budgets",
    )
    assert row == {"text": "Move to Pixhawk TELEM1 5 V rail, 2.60 W free", "kind": "derived"}
    assert instead_row("add a 3.20 W regulator fed from rail 'battery'", "projected") == {
        "text": "Add 3.20 W regulator",
        "kind": "projected",
    }


def test_repeats_get_a_count_title_and_commits_read_as_sentences():
    assert group_title("devices/*/drives_joint").format(n=12) == "12 drives unmapped"
    assert commit_summary("docs: checkpoint completed work\n\nbody") == "Checkpoint completed work"


def test_fix_text_has_no_ids_or_provenance():
    for action in (
        "shed load on rail 'rail.pm02-5v' or raise its capacity with provenance",
        "remove mass or re-lock the budget limit with provenance",
    ):
        out = fix_text(action)
        assert "rail." not in out and "provenance" not in out and "'" not in out


def test_whole_robot_budgets_get_their_own_word():
    assert check_label("budgets/bud.mass-model") == "Budget total \u00b7 mass"
    assert check_label("budgets/bud.endurance") != check_label("budgets/bud.mass-model")
