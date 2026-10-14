"""Verify the source-only real-scenarios fixture without simulator dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
LEG_JOINTS = {
    f"{leg}_{joint}"
    for leg in ("LF", "RF", "LH", "RH")
    for joint in ("HAA", "HFE", "KFE")
}


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def verify_assets() -> None:
    sources = json.loads((ROOT / "sources.json").read_text())
    entries = []
    for line in (ROOT / "assets.lock").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        source, path, size, blob, sha256 = line.split("\t")
        entries.append((source, path, int(size), blob, sha256))

    assert len(entries) == sources["expected_asset_count"]
    assert sum(entry[2] for entry in entries) == sources["expected_asset_bytes"]
    assert sources["expected_asset_bytes"] <= sources["hard_asset_limit_bytes"]
    for source, path, size, blob, sha256 in entries:
        data = (ROOT / "official" / source / path).read_bytes()
        assert len(data) == size, f"size changed: {source}/{path}"
        assert git_blob_sha1(data) == blob, f"Git blob changed: {source}/{path}"
        assert hashlib.sha256(data).hexdigest() == sha256, f"SHA-256 changed: {source}/{path}"

    px4 = sources["sources"]["px4-autopilot"]
    models = sources["sources"]["px4-gazebo-models"]
    assert px4["release"] == "v1.16.2"
    assert px4["revision"] == "54f0455ffcd755534539a7cf33a09a20bf71d29d"
    assert px4["integration_tree_entry"]["sha"] == models["revision"]
    assert px4["integration_tree_entry"]["mode"] == "160000"
    assert models["revision"] != models["excluded_incompatible_revision"]
    gitmodules = (ROOT / "official/px4-autopilot/.gitmodules").read_text()
    assert "path = Tools/simulation/gz" in gitmodules
    assert "url = https://github.com/PX4/PX4-gazebo-models.git" in gitmodules


def verify_official_models() -> None:
    px4_root = ROOT / "official/px4-gazebo-models/models"
    x500 = (px4_root / "x500/model.sdf").read_text()
    x500_base = (px4_root / "x500_base/model.sdf").read_text()
    assert "model://x500_base" in x500
    assert x500.count("gz::sim::systems::MulticopterMotorModel") == 4
    assert {match for match in re.findall(r"model://x500_base/([^<]+)", x500_base)}
    for relative in re.findall(r"model://x500_base/([^<]+)", x500_base):
        assert (px4_root / "x500_base" / relative).is_file(), relative
    assert "<init_from>CF.png</init_from>" in (
        px4_root / "x500_base/meshes/NXP-HGD-CF.dae"
    ).read_text()
    for sensor in ("air_pressure", "magnetometer", "imu", "navsat"):
        assert f'type="{sensor}"' in x500_base

    anymal_root = ROOT / "official/anymal-d"
    urdf = ET.parse(anymal_root / "urdf/anymal.urdf").getroot()
    joints = {joint.attrib["name"]: joint for joint in urdf.findall("joint")}
    assert LEG_JOINTS <= joints.keys()
    for name in LEG_JOINTS:
        limit = joints[name].find("limit")
        assert limit is not None and float(limit.attrib["effort"]) == 80.0
    for mesh in urdf.findall(".//mesh"):
        prefix = "package://anymal_d_simple_description/"
        filename = mesh.attrib["filename"]
        assert filename.startswith(prefix)
        assert (anymal_root / filename.removeprefix(prefix)).is_file(), filename
    for dae in (anymal_root / "meshes").glob("*.dae"):
        for image in re.findall(r"<init_from>([^<]+\.(?:jpg|png))</init_from>", dae.read_text()):
            assert (dae.parent / image).is_file(), f"{dae.name}: {image}"


def verify_control_inputs() -> None:
    overlay = ET.parse(ROOT / "anymal_control_overlay.xml").getroot()
    control = overlay.find("ros2_control")
    assert control is not None
    assert {joint.attrib["name"] for joint in control.findall("joint")} == LEG_JOINTS
    for joint in control.findall("joint"):
        command = joint.find("command_interface")
        assert command is not None and command.attrib["name"] == "effort"
        assert {state.attrib["name"] for state in joint.findall("state_interface")} == {
            "position",
            "velocity",
            "effort",
        }
    plugin = overlay.find("./gazebo/plugin")
    assert plugin is not None
    assert plugin.findtext("hold_joints") == "false"
    assert overlay.find('./gazebo[@reference="imu_link"]/sensor[@type="imu"]') is not None

    controller = (ROOT / "controllers.yaml").read_text()
    assert "LOCKWIRE TEST configuration" in controller
    assert "command_interfaces: [effort]" in controller
    assert "state_interfaces: [position, velocity]" in controller
    for joint in LEG_JOINTS:
        assert f"{joint}: {{p: 120.0, i: 0.0, d: 4.0}}" in controller

    world = ET.parse(ROOT / "scenario_world.sdf").getroot()
    assert world.find('.//sensor[@name="ground_contacts"][@type="contact"]') is not None
    assert world.find('.//plugin[@name="gz::sim::systems::Contact"]') is not None
    assert world.findall(".//include") == []


def verify_disabled_negative(negative: dict) -> None:
    target = "anymal_effort_controller"
    controller = negative["controller"]
    assert controller["name"] == target
    assert controller["required_lifecycle_state"] == "inactive"
    assert controller["hold_joints"] is False
    assert controller["trajectory_command_allowed"] is False

    assert negative["actions"] == [
        {"controller_manager_action": "deactivate", "controller": target},
        {"release": "gravity", "duration_s": 2.0},
    ]
    assert negative["capture"] == {
        "begins_after_controller_inactive": True,
        "controller_commands_allowed": False,
    }

    acceptance = negative["acceptance"]
    assert acceptance["controller_must_remain_inactive"] is True
    assert acceptance["implicit_hold_must_be_disabled"] is True
    assert acceptance["gravity_window_s"] == 2.0
    assert acceptance["minimum_actual_position_change_rad"] == 0.01
    assert acceptance["or_minimum_actual_speed_rad_s"] == 0.02
    assert acceptance["command_echo_may_not_satisfy"] is True


def verify_disabled_negative_mutations(negative: dict) -> None:
    def wrong_declared_controller(value: dict) -> None:
        value["controller"]["name"] = "wrong_controller"

    def wrong_target(value: dict) -> None:
        value["actions"][0]["controller"] = "wrong_controller"

    def remove_gravity_release(value: dict) -> None:
        value["actions"].pop(1)

    def wrong_duration(value: dict) -> None:
        value["actions"][1]["duration_s"] = 1.0

    def inactive_flag_weakened(value: dict) -> None:
        value["acceptance"]["controller_must_remain_inactive"] = False

    def hold_enabled(value: dict) -> None:
        value["controller"]["hold_joints"] = True

    def position_threshold_weakened(value: dict) -> None:
        value["acceptance"]["minimum_actual_position_change_rad"] = 0.0

    def speed_threshold_weakened(value: dict) -> None:
        value["acceptance"]["or_minimum_actual_speed_rad_s"] = 0.0

    def command_during_capture(value: dict) -> None:
        value["capture"]["controller_commands_allowed"] = True

    mutations = {
        "wrong declared controller": wrong_declared_controller,
        "wrong controller target": wrong_target,
        "removed gravity release": remove_gravity_release,
        "wrong gravity duration": wrong_duration,
        "continued-inactivity flag weakened": inactive_flag_weakened,
        "implicit hold enabled": hold_enabled,
        "position threshold weakened": position_threshold_weakened,
        "speed threshold weakened": speed_threshold_weakened,
        "controller command allowed during capture": command_during_capture,
    }
    for name, mutate in mutations.items():
        changed = deepcopy(negative)
        mutate(changed)
        try:
            verify_disabled_negative(changed)
        except (AssertionError, KeyError, TypeError):
            continue
        raise AssertionError(f"disabled-negative mutation was accepted: {name}")


def verify_acceptance_contract() -> None:
    data = json.loads((ROOT / "scenarios.json").read_text())
    assert data["scope"]["source_only"] is True
    assert data["scope"]["runtime_status"] == "PENDING"
    assert data["scope"]["product_status"] == "PENDING"
    assert {"animation", "command_echo", "desired_state_as_actual_state"} <= set(
        data["scope"]["forbidden_evidence"]
    )
    scenarios = data["scenarios"]
    flight = scenarios["px4_x500_takeoff_hover_land"]
    assert flight["acceptance"]["deadline_s"] == 90.0
    assert flight["acceptance"]["takeoff_altitude_m"]["target"] == 2.0
    assert flight["acceptance"]["hover_duration_s"]["minimum"] == 10.0
    assert flight["acceptance"]["command_echo_may_not_satisfy"] is True

    squat = scenarios["anymal_d_effort_squat"]
    assert squat["controller"]["command_interface"] == "effort"
    assert squat["test_trajectory"]["cycles"] == 2
    assert squat["test_trajectory"]["squat_delta_rad"] == {"HFE": 0.15, "KFE": 0.15}
    assert set(squat["telemetry"]) >= {
        "position",
        "velocity",
        "effort",
        "contact",
        "sensors",
    }
    negative = scenarios["anymal_d_controller_disabled_negative"]
    verify_disabled_negative(negative)
    verify_disabled_negative_mutations(negative)


if __name__ == "__main__":
    verify_assets()
    verify_official_models()
    verify_control_inputs()
    verify_acceptance_contract()
    print("PASS: disabled-negative static mutations rejected 9/9 invalid variants")
    print("PASS: 54 exact official assets / 30,454,659 bytes; E5.10 source contract verified")
