"""`lockwire describe` — a per-device reference file, generated from the contract.

Every rendered line comes from a schema field; there is no inference here. The
golden-file test pins the exact rendering for one fixture device so a
regression in wording or field order is caught even when no other assertion
would notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire import api
from lockwire.cli import app
from lockwire.describe import describe_all, describe_device, describe_system, render_power_tree
from lockwire.schema import (
    Actuator,
    Contract,
    DependencyEdge,
    DeviceInstance,
    DeviceModel,
    Driver,
    Joint,
    JointLimits,
    ModelRecord,
    Provenance,
    Quantity,
    RobotInfo,
    SignalSource,
    Tolerance,
    load_contract,
)

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def x500_contract():
    return load_contract(FIXTURES / "x500" / "contract.yaml")


def test_jetson_orin_nano_render(x500_contract):
    device = x500_contract.device("dev.companion.jetson-orin-nano")
    rendered = describe_device(x500_contract, device)

    assert "## Role" in rendered
    assert "component" in rendered
    assert "mode 7W: 7.00 W" in rendered
    assert "mode 15W: 15.00 W" in rendered
    assert "mode 25W: 25.00 W" in rendered
    assert "capacity: 15.60 W" in rendered
    assert "NVIDIA module datasheet" in rendered


def test_no_driver_signals_renders_literal_line(x500_contract):
    device = x500_contract.device("dev.companion.jetson-orin-nano")
    rendered = describe_device(x500_contract, device)
    assert "Measures: nothing declared." in rendered


def test_imu_hard_limit_band():
    """model.hard_limits renders as a plain Band, no envelope/context — dev.imu's
    mdl.imu.bmi088 carries one (accel_z), the same code path a device-agnostic
    hard limit anywhere in the schema takes."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    device = contract.device("dev.imu")
    assert device is not None
    rendered = describe_device(contract, device)
    assert "accel_z: [-235.4, 235.4] m/s^2" in rendered


def test_public_description_keeps_measured_envelope_bag_identity():
    page = api.describe(FIXTURES / "contract_clean.yaml")["files"]["dev.imu.md"]
    measured = next(
        line
        for line in page.splitlines()
        if "observed envelope [nominal-op] signal accel_x:" in line
    )
    assert "witness: live" in measured
    assert "source: tests/fixtures/bags/clean" in measured
    assert (
        "bag_sha256: 9b7a30c9e2ea678acfb35c2d4ae7b5cf00593b0d37e6d723b749176ac4524865" in measured
    )

    authored = next(
        line
        for line in page.splitlines()
        if "observed envelope [nominal-op] signal accel_z:" in line
    )
    assert "witness: human" in authored
    assert "bag_sha256:" not in authored


def test_out_writes_one_file_per_device_plus_system(x500_contract, tmp_path):
    files = describe_all(x500_contract)
    assert "SYSTEM.md" in files
    for device in x500_contract.devices:
        assert f"{device.id}.md" in files
    assert len(files) == len(x500_contract.devices) + 1

    result = runner.invoke(
        app, ["describe", str(FIXTURES / "x500" / "contract.yaml"), "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    written = {p.name for p in tmp_path.iterdir()}
    assert written == set(files)


def test_invalid_contract_path_exits_2():
    result = runner.invoke(app, ["describe", "no/such/contract.yaml"])
    assert result.exit_code == 2


def test_system_render_counts(x500_contract):
    rendered = describe_system(x500_contract)
    assert "## Rails" in rendered
    assert "rail.pm02-5v" in rendered
    n = len(x500_contract.devices)
    assert f"{n} device(s):" in rendered


def test_edges_render_as_a_matrix_on_a_real_fixture():
    """The Reference face's Edges table on tests/fixtures/contract_clean.yaml —
    the only checked-in fixture with edges (both use rel_pct)."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    rendered = describe_system(contract)
    assert "## Edges" in rendered
    assert "| quantity | tolerance | files | owner |" in rendered
    assert "| devices/dev.imu/physical/mass | 5% | src/estimation/ekf.cpp | shivansh |" in rendered
    assert (
        "| model/joints/imu_mount/limits/effort | 5% | src/control/mount.cpp | shivansh |"
        in rendered
    )


def test_edges_table_notes_tolerance_is_scored_against_a_supplied_reading():
    """The column doesn't just declare a tolerance, `check_edge_tolerance` scores it — but only when a caller supplies a current
    reading, which the `check` command does not do today. The note says
    that precisely rather than either overclaiming scoring or asserting the
    stale "not scored by any check" line a real check now falsifies."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    rendered = describe_system(contract)
    assert "scored against a supplied current reading" in rendered
    assert "`check` command supplies none today" in rendered


def test_abs_tolerance_edge_renders_its_value_not_a_guessed_percent():
    """An edge whose tolerance is `abs` (not `rel_pct`) must render the actual
    abs magnitude. The forbidden defect — defaulting the absent `rel_pct` to 0
    and printing "0%" — would pass on every checked-in fixture, since none of
    them declare an abs-tolerance edge."""
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        edges=[
            DependencyEdge(
                quantity="devices/dev.imu/physical/mass",
                tolerance=Tolerance(abs=Quantity(value=0.02, unit="kg")),
                code=["src/estimation/ekf.cpp"],
                owner="shivansh",
            )
        ],
    )
    rendered = describe_system(contract)
    assert "0.02 kg" in rendered
    assert "0%" not in rendered


def test_role_adjusts_from_actuator_and_drives_joint():
    """Role must render 'adjusts' from actuator and drives_joint pairing."""
    model = DeviceModel(
        id="mdl.motor.test",
        family="fam.motor",
        actuator=Actuator(torque_constant=0.1),
    )
    joint = Joint(
        name="joint_a",
        type="revolute",
        parent="link1",
        child="link2",
        limits=JointLimits(effort=5.0),
    )
    model_record = ModelRecord(
        source="test.urdf",
        source_sha256="abc123",
        structure_hash="def456",
        joints=[joint],
        provenance=Provenance(witness="test", source="test.urdf", locked_at="2026-01-01"),
    )
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        model=model_record,
        device_models=[model],
    )
    instance = DeviceInstance(id="dev.motor", model=model.id, drives_joint="joint_a", owner="test")
    contract.devices = [instance]

    rendered = describe_device(contract, instance)

    # Verify Role line contains "adjusts"
    assert "## Role" in rendered
    lines = rendered.split("\n")
    role_idx = next(i for i, line in enumerate(lines) if line == "## Role")
    role_value = lines[role_idx + 2].strip()
    assert role_value == "adjusts", f"Expected 'adjusts', got '{role_value}'"

    # Verify Adjusts section has the joint name and effort limit
    assert "joint joint_a (revolute):" in rendered
    assert "effort limit: 5 N*m" in rendered


def test_role_measures_from_driver_signals():
    """Role must render 'measures' from driver.signals; Measures table must not be empty."""
    model = DeviceModel(id="mdl.sensor.test", family="fam.sensor")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        model=None,
        device_models=[model],
    )
    instance = DeviceInstance(
        id="dev.sensor",
        model=model.id,
        owner="test",
        driver=Driver(
            topic="/test/topic",
            msg_type="sensor_msgs/msg/Imu",
            nominal_rate_hz=100.0,
            payload_bytes=48,
            signals={
                "accel_x": SignalSource(field="linear_acceleration.x", unit="m/s^2"),
                "accel_y": SignalSource(field="linear_acceleration.y", unit="m/s^2"),
            },
        ),
    )
    contract.devices = [instance]

    rendered = describe_device(contract, instance)

    # Verify Role line contains "measures"
    assert "## Role" in rendered
    lines = rendered.split("\n")
    role_idx = next(i for i, line in enumerate(lines) if line == "## Role")
    role_value = lines[role_idx + 2].strip()
    assert role_value == "measures", f"Expected 'measures', got '{role_value}'"

    # Verify Measures table has signal entries and NOT the fallback message
    assert "accel_x" in rendered
    assert "linear_acceleration.x" in rendered
    assert "m/s^2" in rendered
    assert "Measures: nothing declared." not in rendered


def test_role_component_when_nothing_declared():
    """Role must render 'component' when no actuator, drives_joint, or signals."""
    model = DeviceModel(id="mdl.passive.test", family="fam.passive")
    contract = Contract(
        robot=RobotInfo(name="testbot"),
        model=None,
        device_models=[model],
    )
    instance = DeviceInstance(id="dev.passive", model=model.id, owner="test")
    contract.devices = [instance]

    rendered = describe_device(contract, instance)

    # Verify Role line contains "component"
    assert "## Role" in rendered
    lines = rendered.split("\n")
    role_idx = next(i for i, line in enumerate(lines) if line == "## Role")
    role_value = lines[role_idx + 2].strip()
    assert role_value == "component", f"Expected 'component', got '{role_value}'"

    # Verify both fallback messages are present
    assert "Measures: nothing declared." in rendered
    assert "Adjusts: nothing declared." in rendered


def test_golden_jetson_orin_nano(x500_contract):
    device = x500_contract.device("dev.companion.jetson-orin-nano")
    rendered = describe_device(x500_contract, device)
    golden = (FIXTURES / "describe" / "x500_jetson.md").read_text(encoding="utf-8")
    assert rendered == golden


# --- `why` and `max_rate` on a Band ----------------------------------
# The `why` (<=512 chars) and `max_rate` (units/s) fields on a Band are part of
# the reference sheet and must stay there.


def _imu_with(**band_kwargs):
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    device = contract.device("dev.imu")
    model = contract.device_model(device.model)
    limit = model.hard_limits["accel_z"]
    model.hard_limits["accel_z"] = limit.model_copy(update=band_kwargs)
    return contract, device


def test_describe_prints_why_on_reference_sheet_when_present():
    contract, device = _imu_with(why="the die desolders past this")
    assert "(why: the die desolders past this)" in describe_device(contract, device)


def test_describe_renders_max_rate_when_present():
    contract, device = _imu_with(max_rate=12.5)
    assert "max rate 12.5 m/s^2/s" in describe_device(contract, device)


def test_describe_labels_ratings_envelopes_and_optional_enforcement():
    contract, device = _imu_with(enforced_by="firmware")
    device.envelopes[0].signals["accel_z"].band = (
        contract.device_model(device.model)
        .hard_limits["accel_z"]
        .model_copy(update={"enforced_by": "driver"})
    )
    device.power.choices = ["nominal"]

    rendered = describe_device(contract, device)

    assert "declared datasheet/model rating accel_z:" in rendered
    assert "observed envelope [bench-idle] signal accel_z:" in rendered
    assert "declared enforcement: firmware" in rendered
    assert "declared enforcement: driver" in rendered
    assert "allowed modes: nominal" in rendered


def test_describe_explains_limit_enforcement_metadata_without_physical_claim():
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    rail = contract.rail("rail.5v")
    assert rail is not None
    rail.capacity_enforced_by = "fuse"
    rendered = describe_system(contract)
    assert "declared enforcement provenance: fuse" in rendered
    assert "metadata only; does not prove physical protection" in rendered


def test_describe_quotes_why_on_crossed_bound():
    """The reference sheet a crossed bound sends the engineer to carries the
    reason verbatim, beside the band it belongs to."""
    contract, device = _imu_with(lo=-1.0, hi=1.0, why="the die desolders past this")
    rendered = describe_device(contract, device)
    assert "accel_z: [-1, 1] m/s^2 (why: the die desolders past this)" in rendered


# ------------------------------------------------------------- power tree


def _rail(contract, rail_id):
    return next(r for r in contract.topology.rails if r.id == rail_id)


def _with_rail(contract, rail_id, **update):
    rails = [r.model_copy(update=update) if r.id == rail_id else r for r in contract.topology.rails]
    topology = contract.topology.model_copy(update={"rails": rails})
    return contract.model_copy(update={"topology": topology})


def test_power_tree_draws_a_converter_node_distinct_from_the_rail():
    """The converter is its own node, never the net standing in
    for the output rail — a lossy edge (efficiency != 1.0) must render a
    ``converter`` line that is neither rail's name."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    tree = "\n".join(render_power_tree(contract))
    assert "converter (efficiency: 0.85)" in tree
    assert "rail rail.22v" in tree
    assert "rail rail.5v" in tree
    # the converter line names neither rail
    converter_line = next(line for line in tree.splitlines() if "converter" in line)
    assert "rail.22v" not in converter_line
    assert "rail.5v" not in converter_line


def test_power_tree_rail_without_voltage_renders_unknown():
    """The decisive check: a rail with no declared voltage must
    paint "unknown" in the tree, never a number (guessed or defaulted)."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    mutated = _with_rail(contract, "rail.22v", voltage=None)
    tree = "\n".join(render_power_tree(mutated))
    assert "rail rail.22v (unknown, capacity 20.00 W)" in tree
    # the number that used to sit there must be gone from that rail's line
    unknown_line = next(line for line in tree.splitlines() if "rail rail.22v" in line)
    assert "22.20" not in unknown_line


def test_power_tree_discloses_the_converter_ceiling_as_a_legend():
    """The tree only draws a converter node where the contract declares an
    efficiency below 1 — a real converter declared with a different
    efficiency signal is not shown. That ceiling must be visible to a reader
    of the rendered output, not only to a maintainer of the code, so any rail
    with ``upstream`` set must carry a plain-words legend line saying so."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    tree = "\n".join(render_power_tree(contract))
    assert any(rail.upstream is not None for rail in contract.topology.rails)
    assert (
        "converters are drawn where the contract declares an upstream and an efficiency below 1; "
        "a converter declared any other way is not shown"
    ) in tree


def test_describe_prints_the_same_force_numbers_the_payload_carries():
    """The reference file and the check document must not disagree.

    Anchored outside both: the two-mass fixture's CG is the hand-computed
    ``(2*0.0 + 6*1.0) / 8 = 0.75 m`` literal, so the two faces agreeing on a
    wrong number would still fail here.
    """
    contract_path = FIXTURES / "twomass" / "lockwire.yaml"
    rendered = describe_system(load_contract(contract_path))
    forces = api.check(contract_path)["forces"]

    assert "## Forces" in rendered
    assert (
        "- centre of gravity: (0.750, 0.000, 0.000) m (kind: derived-by-compute_system_cg)"
        in rendered
    )
    assert "- total mass: 8 kg (kind: derived-by-compute_system_cg)" in rendered
    assert (
        "- reaction at mount base: 78.48 N (kind: derived-by-statics.support_reactions), "
        "moment 58.86 N*m" in rendered
    )

    # Every figure the payload carries names its kind on the page too: no number
    # reaches the reader stripped of its provenance.
    for row in forces["masses"] + forces["mounts"]:
        assert row["id"] in rendered
        if row["kind"] == "not_scored":
            assert row["not_scored_because"] in rendered
        else:
            assert f"(kind: {row['kind']})" in rendered


def test_describe_renders_the_concrete_catalog_part_for_a_copied_mass():
    contract = Contract(
        robot=RobotInfo(name="copied-catalog-mass"),
        devices=[
            DeviceInstance(
                id="dev.flight-controller",
                model="mdl.flight-controller",
                owner="test",
                physical={"mass": {"value": 50, "unit": "g"}},
                provenance=Provenance(
                    witness="human",
                    source="catalog comparison witness",
                    locked_at="2026-09-27",
                    compared_from="x500.flight-controller.pixhawk-6c",
                ),
            )
        ],
    )
    device = contract.device("dev.flight-controller")
    assert device is not None
    assert "compared_from: x500.flight-controller.pixhawk-6c" in describe_device(contract, device)


def test_zero_effort_limit_described_as_unset():
    from lockwire.describe import _joint_limit_lines

    joint = Joint(name="j", type="revolute", parent="a", child="b", limits=JointLimits(effort=0.0))
    assert "  - effort limit: unset (0)" in _joint_limit_lines(joint)


def test_human_power_tree_has_no_ids():
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    tree = "\n".join(render_power_tree(contract, human=True))
    assert "dev." not in tree and "rail." not in tree and "efficiency" not in tree
