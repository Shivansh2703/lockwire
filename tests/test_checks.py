"""Planted-failure suite, record-level edition: every planted record failure is
caught, and the clean contract produces zero findings (false-positive budget)."""

from math import pi
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError
from rich.console import Console

from lockwire.advice import Alternative
from lockwire.checks import (
    AMPACITY_SOURCE,
    EMPTY_SCORE_OBSERVED,
    EMPTY_SCORE_QUANTITY,
    THERMAL_TRIP_SOURCE,
    _ScoreBook,
    check_ampacity,
    check_budgets,
    check_bus_bandwidth,
    check_bus_identities,
    check_compared_from,
    check_edge_tolerance,
    check_envelopes_vs_hard_limits,
    check_zero_mass,
    check_link_budget,
    check_rail_current,
    check_rail_topology,
    check_reference_integrity,
    check_scene_placement,
    check_thermal_budget,
    check_urdf_drift,
    check_voltage_compatibility,
    compute_system_cg,
    free_space_path_loss_db,
    run_record_checks,
)
from lockwire.report import CGReport, Finding, RecordReport, render
from lockwire.scene import Scene, ScenePart
from lockwire.schema import (
    AntennaGain,
    Band,
    Bus,
    Contract,
    DeviceInstance,
    DeviceThermal,
    InputVoltageRange,
    LinkBudget,
    PowerMode,
    Provenance,
    Quantity,
    Radio,
    RobotInfo,
    ThermalLimit,
    Wire,
    load_contract,
)
from lockwire.urdf import parse_urdf


def count_scored_comparisons(contract):
    return run_record_checks(contract).scored or 0


def _violations(findings):
    return [f for f in findings if f.severity == "violation"]


def _bench_idle_conditions_only(contract: Contract) -> Contract:
    """Isolate condition-envelope unit tests to the bench-idle single-axis surface.

    Fitted multi-axis nominal-op conditions on the clean robot. These
    unit tests prove check_condition_envelopes arithmetic against one declared
    baseline, not the full clean robot's operating set — strip other contexts.
    """
    for device in contract.devices:
        for envelope in device.envelopes:
            if envelope.context != "bench-idle":
                envelope.conditions = {}
    return contract


def _assert_clean_baseline(findings):
    """Clean fixture: zero violations/warnings; seven not_scored disclosures.

    not_scored is exempt from the zero-findings budget; a visible coverage gap
    is strictly better than an unearned GO on an unresolved relative URDF path,
    or on a bus (bus.serial0) whose bitrate is known but carries no declared
    traffic (this used to emit nothing at all).

    dev.barometer, dev.gnss and dev.esc.busless carry no
    `physical` block at all — that was previously silently excluded
    from the mass budget (a real gap, not a 0 kg contributor); now it is
    `not_scored` exactly like a `physical` block with a missing `mass`,
    raising the disclosure count from 2 to 5.

    `check_edge_tolerance` scores an edge's tolerance against a
    caller-supplied current reading; `run_record_checks` called with no
    `current_edge_values` (as every call in this module does) has no live
    reading for either fixture edge, so both are honestly `not_scored`
    rather than the prior silent no-op — raising the count from 5 to 7.

    Presence floor (LAW 1): the scored denominator must be positive —
    without it this helper is satisfiable by an empty stub that emits only the
    not_scored lines.
    """
    assert _violations(findings) == []
    assert [f for f in findings if f.severity == "warning"] == []
    not_scored = [f for f in findings if f.severity == "not_scored"]
    assert {f.quantity for f in not_scored} == {
        "model/structure_hash",
        "topology/buses/bus.serial0/bandwidth",
        "devices/dev.barometer/physical/mass",
        "devices/dev.gnss/physical/mass",
        "devices/dev.esc.busless/physical/mass",
        "devices/dev.imu/physical/mass",
        "budgets/bud.mass.devices",  # partial mass sum is not scored
        "model/joints/imu_mount/limits/effort",
    }
    urdf = next(f for f in not_scored if f.quantity == "model/structure_hash")
    assert "relative path" in urdf.observed
    assert "not resolvable" in urdf.observed
    bandwidth = next(f for f in not_scored if f.quantity == "topology/buses/bus.serial0/bandwidth")
    assert (
        bandwidth.observed == "no declared traffic on this bus"
    )  # bites the not-scored disclosure regressing
    for quantity in (
        "devices/dev.barometer/physical/mass",
        "devices/dev.gnss/physical/mass",
        "devices/dev.esc.busless/physical/mass",
    ):
        mass_gap = next(f for f in not_scored if f.quantity == quantity)
        assert (
            mass_gap.observed == "mass not declared"
        )  # bites missing-mass disclosure regressing to a silent skip
    assert isinstance(findings, RecordReport)
    assert findings.scored is not None and findings.scored > 0, (
        "clean fixture scored denominator must be positive — vacuous GO otherwise"
    )


def test_clean_contract_zero_findings(clean_contract):
    _assert_clean_baseline(
        run_record_checks(clean_contract)
    )  # bites false findings on healthy v0.2


def test_voltage_compat_rejects_device_below_declared_input_range(clean_contract):
    assert (
        Path(check_voltage_compatibility.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = Quantity(value=5.2, unit="V")
    rail.provenance = Provenance(
        witness="w0",
        source="rail-regulator-datasheet.pdf",
        locked_at="2026-07-26",
    )
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=7, unit="V"),
        max=Quantity(value=20, unit="V"),
    )
    model.provenance = Provenance(
        witness="w0",
        source="device-datasheet.pdf",
        locked_at="2026-07-26",
    )

    findings = check_voltage_compatibility(clean_contract)

    assert len(findings) == 1
    finding = findings[0]
    assert (
        finding.severity,
        finding.quantity,
        finding.observed,
        finding.expected,
        finding.delta,
        finding.owner,
        finding.source,
    ) == (
        "violation",
        "devices/dev.imu/power/rail",
        "rail 'rail.5v' supplies 5.20 V to device 'dev.imu'",
        "device model 'mdl.imu.bmi088' input range [7.00 V, 20.00 V]",
        "-1.80 V",
        "shivansh",
        (
            "rail rail.5v voltage: rail-regulator-datasheet.pdf; "
            "device model mdl.imu.bmi088 input range: device-datasheet.pdf"
        ),
    )
    # Standing question: fails if the direct power rail is not crossed against both range bounds.


def test_voltage_compat_compatible_device_is_silent(clean_contract):
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"),
        max=Quantity(value=5.5, unit="V"),
    )

    assert check_voltage_compatibility(clean_contract) == []


def test_voltage_compat_output_tolerance_warns_only_past_max(clean_contract):
    device = clean_contract.device("dev.imu")
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = Quantity(value=12.0, unit="V")
    rail.voltage_range = None
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=6.5, unit="V"), max=Quantity(value=12.0, unit="V")
    )
    rail.output_tolerance_pct = 0.0
    assert check_voltage_compatibility(clean_contract) == []
    rail.output_tolerance_pct = 4.0
    (f,) = check_voltage_compatibility(clean_contract)
    assert f.severity == "warning" and "12.48" in f.observed and "4%" in f.observed
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=6.5, unit="V"), max=Quantity(value=12.5, unit="V")
    )
    assert check_voltage_compatibility(clean_contract) == []


def test_voltage_compat_reports_each_declared_range_enforcement(clean_contract):
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = Quantity(value=4.0, unit="V")
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"),
        max=Quantity(value=5.5, unit="V"),
        min_enforced_by="fuse",
        max_enforced_by="driver",
    )

    finding = next(
        f for f in check_voltage_compatibility(clean_contract) if f.severity == "violation"
    )
    assert "fuse (min), driver (max)" in finding.expected
    assert "metadata only; does not prove physical protection" in finding.expected
    # Standing question: fails if an in-range rail voltage produces a false finding.


def test_voltage_compat_undeclared_device_is_silent(clean_contract):
    assert all(model.input_voltage_range is None for model in clean_contract.device_models)
    assert check_voltage_compatibility(clean_contract) == []
    # Standing question: fails if an optional undeclared range is treated as a GAP.


def test_voltage_compat_clean_fixture_has_zero_findings(clean_contract):
    _assert_clean_baseline(run_record_checks(clean_contract))
    # Standing question: fails if integrating the check changes the clean fixture's findings.


@pytest.mark.parametrize("name", ["contract.yaml", "contract_corrected.yaml"])
def test_voltage_compat_x500_fixtures_add_no_findings(name):
    contract_path = Path(__file__).parent / "fixtures" / "x500" / name

    assert check_voltage_compatibility(load_contract(contract_path)) == []
    # Standing question: fails if a change alters an out-of-fence x500 fixture without a declaration.


def test_x500_corrected_variant_has_zero_findings_and_cited_bec_margin():
    contract_path = Path(__file__).parent / "fixtures" / "x500" / "contract_corrected.yaml"
    contract = load_contract(contract_path)

    findings = run_record_checks(contract)
    assert _violations(findings) == []  # bites the original defect regrowing as a violation
    # The fixture declares bus.i2c.external (external compass bus, no
    # published bitrate); published-default
    # bitrates for the other three buses (57600/921600 bps, 5 Gbit/s); no
    # device on them declares nominal_rate_hz+payload_bytes traffic. The check
    # closes the resulting silent skip: those three now disclose
    # as their own not_scored finding (see test_x500.py for the full set).
    unrated = [
        f for f in findings if f.severity == "not_scored" and f.quantity.endswith("/bandwidth")
    ]
    assert {f.quantity for f in unrated} == {
        "topology/buses/bus.i2c.external/bandwidth",
        "topology/buses/bus.can.dronecan/bandwidth",
        "topology/buses/bus.uart.telemetry/bandwidth",
        "topology/buses/bus.uart.companion/bandwidth",
        "topology/buses/bus.usb.companion/bandwidth",
    }  # bites silent skip of None-bitrate buses and no-traffic buses
    assert {f.observed for f in unrated} == {
        "bitrate unrated; bandwidth not scored",
        "no declared traffic on this bus",
    }
    urdf_gaps = [
        f for f in findings if f.severity == "not_scored" and f.quantity == "model/structure_hash"
    ]
    assert len(urdf_gaps) == 1  # bites silent skip of Tier A without URDF provenance
    assert urdf_gaps[0].observed == "no URDF provenance; structure drift not scored"
    # the RFD900x declares a partial link_budget (mission range/fade
    # margin/installation-loss) and carrier_frequency
    # (declared band-centre 915 MHz, labelled assumption); the
    # antenna-gain gap is closed: the antenna-gain gap: RFDesign's own RFD900x modem bundle page
    # publishes a bundled antenna gain (3 dBi half-wave dipole), so
    # link_budget is now fully populated — no not_scored finding for it, just
    # a real (silent, passing) scored comparison with large headroom over the
    # 10 dB requirement at 2 km.
    link_budget_gaps = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == "devices/dev.radio.rfd900x/link_budget"
    ]
    assert len(link_budget_gaps) == 0
    # three devices carry no `physical` block at all (mass
    # unknown) — jetson-orin-nano, camera.oak-d-lite, magnetometer.ist8310 —
    # and this is now `not_scored` exactly like a `physical` block with a
    # missing `mass`, instead of a silent skip out of the mass budget.
    mass_gaps = [
        f for f in findings if f.severity == "not_scored" and f.quantity.endswith("/physical/mass")
    ]
    assert {f.quantity for f in mass_gaps} == {
        "devices/dev.companion.jetson-orin-nano/physical/mass",
        "devices/dev.camera.oak-d-lite/physical/mass",
        "devices/dev.magnetometer.ist8310/physical/mass",
    }
    assert {f.observed for f in mass_gaps} == {"mass not declared"}
    assert [f for f in findings if f.severity == "warning"] == []
    # Jetson, OAK-D and magnetometer masses are unknown, so the payload budget is unscored
    assert [f.quantity for f in findings if f.quantity == "budgets/bud.mass.payload"] == [
        "budgets/bud.mass.payload"
    ]
    assert [
        f
        for f in findings
        if f.severity == "not_scored"
        and f.quantity != "budgets/bud.mass.payload"
        and f not in unrated
        and f not in urdf_gaps
        and f not in link_budget_gaps
        and f not in mass_gaps
    ] == []

    jetson = contract.device("dev.companion.jetson-orin-nano")
    rail = contract.rail("rail.cc-bec-pro-5v")
    assert jetson.power.rail == rail.id  # bites routing the Jetson back onto the PM02 rail
    jetson_current_a = jetson.power.draw.base() / rail.voltage.base()
    continuous_current_a = rail.capacity.base() / rail.voltage.base()
    assert jetson_current_a == pytest.approx(5.0)  # 25 W / 5 V
    assert continuous_current_a == pytest.approx(13.0)  # conservative 4S current class
    assert continuous_current_a / jetson_current_a == pytest.approx(2.6)


def test_hand_written_unresolved_device_owner_is_a_violation(tmp_path, capsys):
    contract_path = tmp_path / "lockwire.yaml"
    contract_path.write_text(
        """
schema_version: "0.2"
robot:
  name: hand-written
families:
  - id: fam.sensor
    name: Sensor
device_models:
  - id: mdl.sensor
    family: fam.sensor
devices:
  - id: dev.sensor
    model: mdl.sensor
    owner: UNRESOLVED
""".lstrip()
    )

    findings = run_record_checks(load_contract(contract_path))

    # Owner+model alone scores nothing — empty-denominator disclosure
    # is additive; the owner violation is still the only violation.
    assert findings.scored == 0
    assert len(_violations(findings)) == 1
    finding = _violations(findings)[0]
    assert (
        finding.severity,
        finding.quantity,
        finding.observed,
        finding.expected,
        finding.owner,
    ) == (
        "violation",
        "devices/dev.sensor/owner",
        "'UNRESOLVED'",
        "an accountable owner",
        None,
    )
    assert render(findings) == 1  # bites an unreviewed contract still returning GO
    assert "NO-GO" in capsys.readouterr().out


def test_blank_device_owner_is_a_violation(clean_contract):
    device = clean_contract.devices[0]
    device.owner = ""

    findings = run_record_checks(clean_contract)

    assert [
        (finding.quantity, finding.observed, finding.expected) for finding in _violations(findings)
    ] == [
        (
            f"devices/{device.id}/owner",
            "''",
            "an accountable owner",
        )
    ]  # bites owner="" returning GO


@pytest.mark.parametrize("owner", ["   ", "???"])
def test_owner_without_identifier_characters_is_a_violation(clean_contract, owner):
    device = clean_contract.devices[0]
    device.owner = owner

    findings = run_record_checks(clean_contract)

    assert [(finding.quantity, finding.observed) for finding in _violations(findings)] == [
        (f"devices/{device.id}/owner", repr(owner))
    ]  # bites whitespace-only or punctuation-only owners returning GO


def test_fully_resolved_contract_stays_clean_under_unresolved_owner_check(clean_contract):
    assert all(device.owner != "UNRESOLVED" for device in clean_contract.devices)
    _assert_clean_baseline(run_record_checks(clean_contract))  # bites making every contract NO-GO


def test_busless_device_skips_reference_integrity(clean_contract):
    assert clean_contract.device("dev.esc.busless").identity is None
    assert check_reference_integrity(clean_contract) == []


def test_busless_device_skips_bus_identity_checks(clean_contract):
    assert clean_contract.device("dev.esc.busless").identity is None
    assert check_bus_identities(clean_contract) == []


def test_busless_device_skips_bus_bandwidth_checks(clean_contract):
    """A device with no bus identity contributes no finding of its own — the
    lone finding left is bus.serial0's pre-existing no-declared-traffic
    disclosure, unrelated to the busless device."""
    assert clean_contract.device("dev.esc.busless").identity is None
    findings = check_bus_bandwidth(clean_contract)
    assert [f.quantity for f in findings] == ["topology/buses/bus.serial0/bandwidth"]


def _cg_arithmetic_contract(clean_contract, sources):
    """Make a rigid one-axis case whose CG can be checked without code-derived values."""
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.sources = sources
    budget.limit = Quantity(value=10.0, unit="kg")
    base, payload = clean_contract.model.links
    base.mass = Quantity(value=2.0, unit="kg")
    base.com = (0.0, 0.0, 0.0)
    payload.mass = Quantity(value=1.0, unit="kg")
    payload.com = (1.0, 0.0, 0.0)
    joint = clean_contract.model.joints[0]
    joint.type = "fixed"
    joint.origin.xyz = (2.0, 0.0, 0.0)
    imu = clean_contract.device("dev.imu")
    imu.physical.mass = Quantity(value=3.0, unit="kg")
    imu.physical.mount_frame = "imu_link"
    imu.physical.mount_xyz = (
        Quantity(value=0.0, unit="m"),
        Quantity(value=0.0, unit="m"),
        Quantity(value=0.0, unit="m"),
    )
    clean_contract.device("dev.radio.rfd900").physical = None
    return clean_contract


def test_cg_computes_clean_fixture_offsets_from_hand_anchored_sum(clean_contract):
    report = compute_system_cg(clean_contract)[0]

    # Fixture arithmetic in base_link, converted to SI:
    # m = 0.012 + 0.030 = 0.042 kg
    # Σ(m*r) = (0.012*(0.100,-0.040,0.020)
    #           + 0.030*(-0.200,0.080,0.050)) kg*m
    #         = (-0.0048, 0.00192, 0.00174) kg*m
    # CG = Σ(m*r)/Σm = (-0.1142857143, 0.0457142857, 0.0414285714) m.
    assert report.cg_m == pytest.approx((-0.1142857143, 0.0457142857, 0.0414285714))
    assert report.complete is True
    assert report.total_mass_kg == pytest.approx(0.042)
    assert len(report.contributors) == 2


def test_cg_without_device_offsets_renders_not_computable(clean_contract, capsys):
    legacy_data = clean_contract.model_dump(mode="python")
    for device in legacy_data["devices"]:
        if device.get("physical") is not None:
            device["physical"].pop("mount_xyz")
    contract_without_offsets = Contract.model_validate(legacy_data)

    report = run_record_checks(contract_without_offsets)
    render(report)
    output = capsys.readouterr().out

    assert report.information[0].cg_m is None
    assert report.information[0].complete is False
    assert "not computable" in output
    # no contributor is positioned, so the CG lane collapses to one line
    # instead of a "position unresolved" line per device.
    assert "none of the 2 contributors" in " ".join(output.split())


@pytest.mark.parametrize(
    ("sources", "expected_x", "expected_mass"),
    [
        ("devices", 2.0, 3.0),
        ("links", 1.0, 3.0),
        ("links+devices", 1.5, 6.0),
    ],
)
def test_cg_uses_mass_budget_sources_without_accidental_double_counting(
    clean_contract, sources, expected_x, expected_mass
):
    contract = _cg_arithmetic_contract(clean_contract, sources)

    # By hand: device = 3 kg at x=2 -> 2; links = (2*0 + 1*(2+1))/3 -> 1;
    # both = (2*0 + 1*3 + 3*2)/(2+1+3) -> 1.5.
    report = compute_system_cg(contract)[0]
    assert report.cg_m == pytest.approx(  # bites wrong weighted-position arithmetic
        (expected_x, 0.0, 0.0)
    )
    assert report.total_mass_kg == expected_mass  # bites ignoring Budget.sources selection
    assert report.complete is True  # bites calling a rigid, fully positioned result partial
    expected_counts = {"devices": 1, "links": 2, "links+devices": 3}
    assert len(report.contributors) == expected_counts[sources]  # bites hidden double counting


def test_cg_reports_dominant_contributors_at_composed_positions(clean_contract):
    contract = _cg_arithmetic_contract(clean_contract, "links+devices")

    report = compute_system_cg(contract)[0]
    assert (  # bites reducing CG to a bare coordinate without its dominant mass
        report.contributors[0].quantity == "devices/dev.imu/physical/mass"
    )
    assert report.contributors[0].position_m == pytest.approx(  # bites skipping mount-frame origin
        (2.0, 0.0, 0.0)
    )
    payload = next(
        item for item in report.contributors if item.quantity == "model/links/imu_link/mass"
    )
    assert payload.position_m == pytest.approx(  # bites failing to compose link COM through joint
        (3.0, 0.0, 0.0)
    )
    assert payload.moment_kgm == pytest.approx(  # bites losing the contributor's weighted pull
        (3.0, 0.0, 0.0)
    )


def test_cg_composes_fixed_joint_rotation(clean_contract):
    contract = _cg_arithmetic_contract(clean_contract, "links")
    base, payload = contract.model.links
    base.mass = Quantity(value=1.0, unit="kg")
    payload.mass = Quantity(value=1.0, unit="kg")
    contract.model.joints[0].origin.xyz = (1.0, 0.0, 0.0)
    contract.model.joints[0].origin.rpy = (0.0, 0.0, pi / 2)

    # By hand: payload local (1,0,0) rotates to (0,1,0), then translates to
    # (1,1,0); with an equal mass at the root, CG = ((0,0,0)+(1,1,0))/2.
    report = compute_system_cg(contract)[0]
    assert report.cg_m == pytest.approx(  # bites treating joint RPY as translation-only
        (0.5, 0.5, 0.0)
    )


def test_cg_is_structurally_separate_from_findings():
    return_type = get_type_hints(compute_system_cg)["return"]
    report_types = get_type_hints(RecordReport)
    information_type = report_types["information"]

    assert return_type == list[CGReport]  # bites a future CG path returning Finding
    assert information_type == list[CGReport]  # bites mixing CG into the judgement lane
    assert report_types["actuator_case_reports"] == list[Any]
    assert not issubclass(CGReport, Finding)  # bites disguising CG as a Finding subtype
    assert "severity" not in CGReport.model_fields  # bites adding warning/violation semantics to CG


def test_cg_articulation_is_explicit_and_clean_fixture_still_has_zero_findings(clean_contract):
    clean_contract.device("dev.imu").physical.mount_frame = "imu_link"
    report = run_record_checks(clean_contract)

    _assert_clean_baseline(report)  # bites severity-free CG consuming the false-positive budget
    assert len(report.information) == 1  # bites silently dropping CG on an articulated model
    cg = report.information[0]
    assert cg.complete is False  # bites silently choosing an undeclared joint configuration
    assert "configuration-dependent" in cg.limitation  # bites hiding why system CG is partial


def test_cg_renders_as_information_with_contributors(clean_contract, capsys):
    report = run_record_checks(_cg_arithmetic_contract(clean_contract, "links+devices"))

    exit_with_information = render(report)
    output = capsys.readouterr().out
    findings_without_cg = [finding for finding in report if not isinstance(finding, CGReport)]
    exit_without_information = render(RecordReport(findings_without_cg))
    capsys.readouterr()

    assert exit_with_information == exit_without_information  # bites CG gaining decision weight
    assert "INFO" in output and "budgets/bud.mass.devices/cg" in output  # bites hiding CG
    assert "contributor:" in output  # bites rendering a bare coordinate without its witnesses
    assert "warning" not in output.lower()  # bites forcing CG into a judgement to display it


def test_planted_rail_power_breach(clean_contract):
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.draw = Quantity(value=6.0, unit="W")

    findings = _violations(run_record_checks(clean_contract))
    assert len(findings) == 1  # bites duplicate findings for one direct rail overload
    f = findings[0]
    assert f.quantity == "budgets/bud.power.rail5v"  # bites checking the wrong budget
    assert f.owner == "shivansh"  # bites dropping budget ownership from the finding
    assert "dev.radio.rfd900" in f.dependent  # bites losing the overload witness
    assert "rail.5v" in f.action  # bites an action that cannot identify the overloaded rail


def test_draw_mode_peak_breaches_and_reports_selected_mode(clean_contract):
    assert (
        Path(check_budgets.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.modes = [
        PowerMode(name="peak", draw=Quantity(value=6.0, unit="W")),
    ]
    budget = next(b for b in clean_contract.budgets if b.kind == "rail_power")
    budget.draw_mode = "peak"

    findings = [
        f for f in check_budgets(clean_contract) if f.quantity == "budgets/bud.power.rail5v"
    ]

    assert len(findings) == 1
    assert findings[0].observed == "6.40 W (draw mode: peak)"
    assert findings[0].quantity == "budgets/bud.power.rail5v"
    # Standing question: forcing selection to always read nominal makes this breach disappear.


def test_draw_mode_nominal_pass_is_silent_even_when_peak_would_breach(clean_contract):
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.modes = [
        PowerMode(name="peak", draw=Quantity(value=6.0, unit="W")),
    ]
    budget = next(b for b in clean_contract.budgets if b.kind == "rail_power")
    assert budget.draw_mode == "nominal"

    # check_budgets also runs the mass_total budget, which discloses three
    # physical-less devices (busless/no-mount fixture devices) as not_scored —
    # unrelated to draw-mode selection, so scope this assertion to the rail budget.
    assert [
        f for f in check_budgets(clean_contract) if f.quantity == "budgets/bud.power.rail5v"
    ] == []
    # Settled (settled earlier): nominal selection must not invent a near-miss warning for another mode.


def test_draw_mode_single_draw_finding_is_byte_identical(clean_contract):
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.draw = Quantity(value=6.0, unit="W")

    findings = [
        f for f in check_budgets(clean_contract) if f.quantity == "budgets/bud.power.rail5v"
    ]

    expected = Finding(
        severity="violation",
        quantity="budgets/bud.power.rail5v",
        observed="6.40 W",
        expected="<= 5.00 W",
        observed_quantity=Quantity(value=6.40, unit="W"),
        expected_quantity=Quantity(value=5.00, unit="W"),
        delta="+1.40 W (+28%)",
        dependent="dev.imu, dev.radio.rfd900",
        owner="shivansh",
        action="shed load on rail 'rail.5v' or raise its capacity with provenance",
        # the alternatives half of the advice sentence — no
        # sibling rail_power budget in this fixture, so only the projected
        # regulator suggestion, fed from rail.5v's own upstream rail.22v.
        alternatives=(
            Alternative(
                text="add a 1.40 W regulator fed from rail 'rail.22v'",
                value=6.4 - 5.0,
                unit="W",
                kind="projected",
                mark="projected",
                rail="rail.5v",
            ),
        ),
    )
    assert [finding.model_dump_json() for finding in findings] == [expected.model_dump_json()]
    # Standing question: adding mode support must not change an old single-draw finding byte.


def test_x500_7w_mode_budget_fires_once_the_radio_joins_the_rail():
    """The radio's peak-fallback draw (5.2 W) now rolls onto
    rail.pm02-5v too, so the mode-aware budget flips: 7 (Jetson 7W) + 5 (camera)
    + 5.2 (radio) = 17.2 W > 15.6 W — a real violation, not a pass."""
    assert (
        Path(check_budgets.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    contract = load_contract(Path(__file__).parent / "fixtures" / "x500" / "contract.yaml")

    jetson = contract.device("dev.companion.jetson-orin-nano")
    assert jetson.power.draw.base() == pytest.approx(25.0)  # nominal fallback untouched
    assert {mode.name: mode.draw.base() for mode in jetson.power.modes} == {
        "7W": 7.0,
        "15W": 15.0,
        "25W": 25.0,
    }

    radio = contract.device("dev.radio.rfd900x")
    assert radio.power.draw.base() == pytest.approx(5.2)  # transmit-peak fallback
    assert {mode.name: mode.draw.base() for mode in radio.power.modes} == {
        "transmit": 5.2,
        "receive": 0.312,
    }

    mode_budget = next(b for b in contract.budgets if b.id == "bud.power.pm02-5v.7w-mode")
    assert mode_budget.draw_mode == "7W"  # the mode-aware path names its mode

    findings = run_record_checks(contract)
    violations = [f for f in findings if f.severity == "violation"]
    warnings = [f for f in findings if f.severity == "warning"]
    assert len(violations) == 2  # nominal budget, plus the newly-real 7W-mode violation
    unrated = [
        f for f in findings if f.severity == "not_scored" and f.quantity.endswith("/bandwidth")
    ]
    # The fixture declares bus.i2c.external (external compass bus, no
    # published bitrate); published-default
    # bitrates for the other three buses; no device on them declares
    # nominal_rate_hz+payload_bytes traffic. The check closes the
    # resulting silent skip: those three now disclose too (see test_x500.py).
    assert {f.quantity for f in unrated} == {
        "topology/buses/bus.i2c.external/bandwidth",
        "topology/buses/bus.can.dronecan/bandwidth",
        "topology/buses/bus.uart.telemetry/bandwidth",
        "topology/buses/bus.uart.companion/bandwidth",
        "topology/buses/bus.usb.companion/bandwidth",
    }  # bites silent skip of None-bitrate buses and no-traffic buses
    assert warnings == []  # bites promoting unrated buses into warning noise
    assert {f.quantity for f in violations} == {
        "budgets/bud.power.pm02-5v",
        "budgets/bud.power.pm02-5v.7w-mode",
    }
    nominal = next(f for f in violations if f.quantity == "budgets/bud.power.pm02-5v")
    mode_finding = next(f for f in violations if f.quantity == "budgets/bud.power.pm02-5v.7w-mode")
    assert "draw mode: nominal" in nominal.observed  # modes present → mode named
    assert mode_finding.observed == "17.20 W (draw mode: 7W)"  # 7 + 5 + 5.2 = 17.2 W > 15.6 W


def test_x500_draw_mode_15w_budget_fires():
    """Mutating the mode-aware budget to 15W fires: 15+5+5.2=25.2 W > 15.6 W."""
    assert (
        Path(check_budgets.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    contract = load_contract(Path(__file__).parent / "fixtures" / "x500" / "contract.yaml")

    mode_budget = next(b for b in contract.budgets if b.id == "bud.power.pm02-5v.7w-mode")
    mode_budget.draw_mode = "15W"  # mutate in-memory only; fixture stays on 7W

    findings = run_record_checks(contract)
    violations = [f for f in findings if f.severity == "violation"]
    mode_violation = next(
        f for f in violations if f.quantity == "budgets/bud.power.pm02-5v.7w-mode"
    )
    assert mode_violation.observed_quantity is not None
    assert mode_violation.expected_quantity is not None
    assert mode_violation.observed_quantity.value == pytest.approx(25.20)
    assert mode_violation.observed_quantity.unit == "W"
    assert mode_violation.expected_quantity.value == pytest.approx(15.60)
    assert mode_violation.expected_quantity.unit == "W"
    assert mode_violation.observed == "25.20 W (draw mode: 15W)"
    assert mode_violation.expected == "<= 15.60 W"


def test_planted_endurance_breach(clean_contract):
    budget = next(b for b in clean_contract.budgets if b.kind == "endurance")
    budget.limit = Quantity(value=100, unit="h")

    findings = _violations(run_record_checks(clean_contract))
    assert [f.quantity for f in findings] == ["budgets/bud.endurance"]  # bites missed endurance
    assert "h expected endurance" in findings[0].observed  # bites reporting power as endurance


def test_endurance_under_an_hour_prints_in_minutes(clean_contract):
    """Flight time under 1 h reads in minutes, not a fraction of an hour.

    Shrinking the battery pulls both the violation's observed hours and the
    budget's 30-min requirement below 1 h; both, and the delta, must print in
    minutes. Revert the ``_format_hours`` call sites to bare ``:.2f h`` and
    this goes red.
    """
    battery = clean_contract.topology.battery
    battery.capacity = Quantity(value=1.0, unit="Wh")  # draw ~2.82 W -> ~0.35 h actual

    findings = _violations(run_record_checks(clean_contract))
    endurance = next(f for f in findings if f.quantity == "budgets/bud.endurance")
    assert "min expected endurance" in endurance.observed
    assert "h expected endurance" not in endurance.observed
    assert endurance.expected == ">= 30.0 min"
    assert "min" in endurance.delta
    assert "h" not in endurance.delta.replace("min", "")


def test_endurance_pass_under_an_hour_prints_minutes_in_scored_record(clean_contract):
    """Passing side: the scored record's observed/expected/margin also
    print in minutes when the flight time is under an hour."""
    battery = clean_contract.topology.battery
    battery.capacity = Quantity(value=2.0, unit="Wh")  # draw ~2.82 W -> ~0.71 h, still >= 30 min

    report = run_record_checks(clean_contract)
    assert _violations(report) == []
    record = next(r for r in report.scored_records if r.quantity == "budgets/bud.endurance")
    assert record.observed.endswith(" min")
    assert record.expected == ">= 30.0 min"
    assert record.margin.endswith(" min to spare")


def test_planted_mass_breach(clean_contract):
    imu = clean_contract.device("dev.imu")
    imu.physical.mass = Quantity(value=600, unit="g")

    findings = _violations(run_record_checks(clean_contract))
    assert [f.quantity for f in findings] == [  # bites missed device mass
        "budgets/bud.mass.devices"
    ]


def test_device_with_no_declared_mass_is_not_scored_and_excluded_from_mass_budget(clean_contract):
    """A physical block with mount_frame/mount_xyz but no mass behaves
    exactly like no-physical-at-all for mass purposes — not summed as 0."""
    imu = clean_contract.device("dev.imu")
    imu.physical.mass = None

    report = run_record_checks(clean_contract)
    findings_by_quantity = {f.quantity: f for f in report}
    finding = findings_by_quantity["devices/dev.imu/physical/mass"]
    assert finding.severity == "not_scored"  # bites treating a missing mass as a violation
    assert "not declared" in finding.observed

    # the budget is not_scored naming the unknown, never a pass on the partial sum
    mass_finding = findings_by_quantity["budgets/bud.mass.devices"]
    assert mass_finding.severity == "not_scored" and "dev.imu" in mass_finding.observed
    assert not [r for r in report.scored_records if r.quantity == "budgets/bud.mass.devices"]


def test_device_with_no_physical_block_is_not_scored_same_as_declared_mass_missing(clean_contract):
    """No `physical` block at all must be treated the same as `physical.mass:
    None` — both are a coverage gap, never a silently-skipped 0 kg contributor.

    Uses dev.radio.rfd900 (not dev.imu): dev.imu is the target of a dependency
    edge on `devices/dev.imu/physical/mass`, so dropping its whole `physical`
    block also trips reference integrity (path no longer resolves) — a real
    but different finding that would mask the mass-budget bug under test.
    """
    radio = clean_contract.device("dev.radio.rfd900")
    radio.physical = None

    report = run_record_checks(clean_contract)
    findings_by_quantity = {f.quantity: f for f in report}
    finding = findings_by_quantity["devices/dev.radio.rfd900/physical/mass"]
    assert finding.severity == "not_scored"  # bites silently dropping a physical-less device
    assert "not declared" in finding.observed

    mass_finding = findings_by_quantity["budgets/bud.mass.devices"]
    assert mass_finding.severity == "not_scored" and "dev.radio.rfd900" in mass_finding.observed


def test_cg_excludes_device_with_no_declared_mass(clean_contract):
    """CG rolls up mass the same way — an undeclared mass is excluded, not zeroed."""
    imu = clean_contract.device("dev.imu")
    imu.physical.mass = None

    report = compute_system_cg(clean_contract)[0]
    ids = [c.quantity for c in report.contributors]
    assert "devices/dev.imu/physical/mass" not in ids  # bites treating None as a 0 kg contributor
    assert report.limitation is not None
    assert "dev.imu" in report.limitation and "no declared mass" in report.limitation


def test_planted_dangling_model_ref(clean_contract):
    clean_contract.device("dev.radio.rfd900").model = "mdl.nope"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites a dangling device-to-model reference passing silently
        f.quantity == "devices/dev.radio.rfd900/model" for f in findings
    )


def test_planted_dangling_edge_path(clean_contract):
    clean_contract.edges[0].quantity = "devices/dev.gone/physical/mass"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites an unresolvable dependency edge passing silently
        f.quantity == "devices/dev.gone/physical/mass" for f in findings
    )
    assert any("unguarded" in f.action for f in findings)  # bites loss of edge-risk guidance


def test_joint_limit_edge_resolves_and_planted_typo_fails(clean_contract):
    _assert_clean_baseline(
        run_record_checks(clean_contract)
    )  # bites rejecting a valid joint-limit path
    clean_contract.edges[1].quantity = "model/joints/imu_mount/limits/effort_typo"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites joint-limit leaf typos passing generic path resolution
        f.quantity == "model/joints/imu_mount/limits/effort_typo" for f in findings
    )


def test_edge_tolerance_drift_past_the_band_is_a_violation(clean_contract):
    """dev.imu's mass edge declares 1.0 kg at a 5% tolerance (0.05 kg band).
    A supplied current reading 100x the declared value is a real, scored
    drift — the decisive bite the first attempt could not pass, because
    it never compared anything."""
    findings = check_edge_tolerance(clean_contract, {"devices/dev.imu/physical/mass": 100.0})
    violations = [f for f in findings if f.severity == "violation"]
    assert len(violations) == 1
    assert violations[0].quantity == "devices/dev.imu/physical/mass"
    assert violations[0].dependent == "src/estimation/ekf.cpp"


def test_edge_tolerance_drift_inside_the_band_stays_silent(clean_contract):
    """dev.imu declares 12 g; a 2.5% current reading is inside the 5% band —
    silent, and recorded against the ScoreBook denominator (this check now
    performs a real two-operand comparison)."""
    scores = _ScoreBook()
    findings = check_edge_tolerance(
        clean_contract, {"devices/dev.imu/physical/mass": 12.3}, scores=scores
    )
    assert all(f.severity != "violation" for f in findings)  # scored, in-band, silent
    assert "devices/dev.imu/physical/mass" in scores.keys


def test_edge_tolerance_absent_current_reading_is_not_scored(clean_contract):
    """No caller supplied a current reading (the CLI `check` command never
    does) — never a silent pass and never a guessed comparison."""
    findings = check_edge_tolerance(clean_contract)
    assert len(findings) == 2  # both fixture edges
    assert all(f.severity == "not_scored" for f in findings)
    assert any(
        "no current reading supplied" in f.observed
        and f.quantity == "devices/dev.imu/physical/mass"
        for f in findings
    )


def test_edge_tolerance_wired_into_run_record_checks(clean_contract):
    """The pipeline must actually pass current_edge_values through — bites
    the function existing but never being reachable from the pipeline."""
    findings = run_record_checks(
        clean_contract, current_edge_values={"devices/dev.imu/physical/mass": 100.0}
    )
    assert any(
        f.quantity == "devices/dev.imu/physical/mass" and f.severity == "violation"
        for f in findings
    )


def test_nan_edge_reading_is_not_scored(clean_contract):
    findings = run_record_checks(
        clean_contract, current_edge_values={"devices/dev.imu/physical/mass": float("nan")}
    )
    assert any("finite" in f.observed and "physical/mass" in f.quantity for f in findings)


def test_planted_envelope_exceeds_hard_limit(clean_contract):
    imu = clean_contract.device("dev.imu")
    imu.envelopes[0].signals["accel_z"].band.hi = 300.0  # hard limit is ±235.4 m/s^2

    findings = _violations(run_record_checks(clean_contract))
    assert len(findings) == 1  # bites duplicate envelope violations
    assert "re-baseline" in findings[0].action  # bites unsafe acceptance of a sick baseline
    assert (  # bites attributing the bad signal to the wrong envelope
        findings[0].quantity == "devices/dev.imu/envelopes/bench-idle/accel_z"
    )


def test_zero_mass_links_warn(testbot_urdf):
    contract = Contract(robot=RobotInfo(name="testbot"), model=parse_urdf(testbot_urdf))
    # The fixture is read by absolute path, so the source-portability warnings fire
    # here too (tests/test_source_paths.py owns that claim); they are not this test's subject.
    findings = [f for f in run_record_checks(contract) if not f.quantity.endswith("/source")]
    assert [f.severity for f in findings] == ["warning"]  # bites escalating massless links to NO-GO
    assert (  # bites losing the exact massless-link path
        findings[0].quantity == "model/links/sensor_mount/mass"
    )


def test_zero_mass_is_derived_from_links_not_cache(clean_contract):
    imu_link = next(link for link in clean_contract.model.links if link.name == "imu_link")
    imu_link.mass = None
    clean_contract.model.zero_mass_links = []

    findings = run_record_checks(clean_contract)
    assert any(  # bites trusting stale zero_mass_links over the canonical links
        f.quantity == "model/links/massless_frames" and "imu_link" in f.observed for f in findings
    )


def test_massless_leaf_frames_fold_into_one_line(clean_contract):
    m = clean_contract.model
    tip = m.links[1].model_copy(update={"name": "tip", "mass": None})  # leaf under imu_link
    m.links.append(tip)
    mount = m.joints[0].model_copy(update={"name": "tip_mount", "parent": "imu_link"})
    m.joints.append(mount.model_copy(update={"child": "tip"}))
    m.links[0].mass = None  # carries massive imu_link: keeps its own warning
    ours = [
        f.quantity
        for f in run_record_checks(clean_contract)
        if f.quantity.startswith("model/links/")
    ]
    assert [q for q in ours if q.endswith("/mass")] == [
        "model/links/base_link/mass"
    ]  # bites folding links that carry children
    assert ours.count("model/links/massless_frames") == 1  # bites one line per frame


def _add_tool_leaf(contract, name, mass):
    m = contract.model
    m.links.append(m.links[1].model_copy(update={"name": name, "mass": mass}))
    joint = m.joints[0].model_copy(
        update={"name": name + "_j", "parent": "imu_link", "child": name}
    )
    m.joints.append(joint)


def test_massless_tool_leaf_warns_unless_mass_declared(clean_contract):
    _add_tool_leaf(clean_contract, "gripper_tip", None)
    found = [
        f for f in run_record_checks(clean_contract) if f.quantity == "model/links/gripper_tip/mass"
    ]
    assert len(found) == 1 and "tool-like" in found[0].observed  # bites the fold hiding a tool link
    clean_contract.model.links[-1].mass = clean_contract.model.links[1].mass
    assert not [f for f in run_record_checks(clean_contract) if "gripper_tip" in f.quantity]


def test_massless_fixed_root_frame_folds_not_warns(clean_contract):
    m = clean_contract.model
    m.links.append(m.links[0].model_copy(update={"name": "base_footprint", "mass": None}))
    m.joints.append(
        m.joints[0].model_copy(
            update={"name": "fp", "type": "fixed", "parent": "footprint", "child": "base_link"}
        )
    )
    ours = [
        f.quantity
        for f in run_record_checks(clean_contract)
        if f.quantity.startswith("model/links/")
    ]
    assert "model/links/base_footprint/mass" not in ours  # bites a mass warning on a pure frame
    assert "model/links/massless_frames" in ours


def test_massless_base_link_root_keeps_own_warning(clean_contract):
    m = clean_contract.model
    m.links[0].mass, m.joints[0].type = None, "fixed"  # massless, unmounted root, only fixed joints
    for d in clean_contract.devices:
        if d.physical:
            d.physical.mount_frame = "imu_link"
    ours = [
        f.quantity
        for f in run_record_checks(clean_contract)
        if f.quantity.startswith("model/links/")
    ]
    assert "model/links/base_link/mass" in ours  # bites folding a real massless body into frames


def test_render_exit_codes(clean_contract, capsys):
    assert render([]) == 0  # bites treating a clean report as NO-GO

    clean_contract.device("dev.radio.rfd900").power.draw = Quantity(value=6.0, unit="W")
    findings = run_record_checks(clean_contract)
    assert render(findings) == 1  # bites violations returning a success exit code
    out = capsys.readouterr().out
    assert "NO-GO" in out  # bites rendering a violation as GO
    assert "budgets/bud.power.rail5v" in out  # bites omitting the failed quantity from output


def test_planted_cold_endurance_derating(clean_contract):
    from lockwire.schema import CapacityDerating

    budget = next(b for b in clean_contract.budgets if b.kind == "endurance")
    budget.limit = Quantity(value=30, unit="h")
    clean_contract.topology.battery.derating = [
        CapacityDerating(below_c=0, capacity_factor=0.65),
        CapacityDerating(below_c=-10, capacity_factor=0.55),
    ]

    findings = run_record_checks(clean_contract)
    assert _violations(findings) == []  # bites turning condition-limited endurance into NO-GO
    warnings = [f for f in findings if f.severity == "warning"]
    assert len(warnings) == 1  # bites warning once per colder derating row
    f = warnings[0]
    assert f.quantity == "budgets/bud.endurance"  # bites misattributing cold endurance
    assert "<= 0 C" in f.observed  # bites losing the first unsupported temperature
    assert "below 0 C" in f.action  # bites an action without an operating restriction


def test_cold_derating_silent_when_margin_holds(clean_contract):
    from lockwire.schema import CapacityDerating

    clean_contract.topology.battery.derating = [CapacityDerating(below_c=0, capacity_factor=0.65)]
    _assert_clean_baseline(
        run_record_checks(clean_contract)
    )  # bites a false warning with ample cold margin


@pytest.mark.parametrize(
    ("mutation", "quantity"),
    [
        ("bus_ref", "devices/dev.imu/identity/bus_ref"),
        ("device_host", "devices/dev.imu/identity/host"),
        ("bus_host", "topology/buses/bus.usb0/host"),
    ],
)
def test_planted_topology_reference_failure(clean_contract, mutation, quantity):
    if mutation == "bus_ref":
        clean_contract.device("dev.imu").identity.bus_ref = "bus.ghost"
    elif mutation == "device_host":
        clean_contract.device("dev.imu").identity.host = "host.ghost"
    else:
        clean_contract.topology.buses[0].host = "host.ghost"

    findings = _violations(run_record_checks(clean_contract))
    assert any(f.quantity == quantity for f in findings)  # bites dangling bus/host refs going green


def test_planted_mount_frame_failure(clean_contract):
    clean_contract.device("dev.imu").physical.mount_frame = "ghost_link"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites fictional device mount frames passing reference integrity
        f.quantity == "devices/dev.imu/physical/mount_frame" for f in findings
    )


def test_mount_frame_warns_until_model_exists(clean_contract):
    clean_contract.model = None
    clean_contract.device("dev.imu").drives_joint = None

    findings = run_record_checks(clean_contract)
    assert any(  # bites treating an unverifiable mount frame as either silent or hard failure
        f.quantity == "devices/dev.imu/physical/mount_frame" and f.severity == "warning"
        for f in findings
    )


def test_anymal_d_drives_joint_resolves(tmp_path):
    """Once `parse_urdf` records joints, a `drives_joint` reference
    into a real URDF-sourced joint resolves cleanly (no dangling-reference
    violation) instead of every actuator reference failing."""
    from conftest import FIXTURES

    model = parse_urdf(FIXTURES / "anymal_d.urdf")
    contract = Contract(
        robot=RobotInfo(name="anymal_d"),
        model=model,
        devices=[
            DeviceInstance(
                id="dev.lf_hip", model="mdl.actuator", drives_joint="LF_HAA", owner="shivansh"
            )
        ],
    )
    findings = check_reference_integrity(contract)
    assert not any(f.quantity == "devices/dev.lf_hip/drives_joint" for f in findings)


def test_planted_drives_joint_failure(clean_contract):
    clean_contract.device("dev.imu").drives_joint = "ghost_joint"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites a dangling actuator-to-joint pairing passing silently
        f.quantity == "devices/dev.imu/drives_joint" for f in findings
    )


def test_efficiency_aware_rail_rollup_breaches_parent(clean_contract):
    budget = next(b for b in clean_contract.budgets if b.kind == "rail_power")
    budget.rail = "rail.22v"
    budget.limit = Quantity(value=2.7, unit="W")

    findings = _violations(run_record_checks(clean_contract))
    rollup = next(f for f in findings if f.quantity == "budgets/bud.power.rail5v")
    assert rollup.observed == "2.82 W"  # bites omitting the child's 1/0.85 converter loss
    assert "dev.radio.rfd900" in rollup.dependent  # bites losing descendant witnesses


def test_rail_upstream_cycle_is_a_violation_not_a_hang(clean_contract):
    clean_contract.rail("rail.22v").upstream = "rail.5v"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites infinite rail traversal or silent acceptance of a two-rail loop
        "rail.5v -> rail.22v -> rail.5v" in f.observed
        or "rail.22v -> rail.5v -> rail.22v" in f.observed
        for f in findings
    )


def test_planted_dangling_rail_upstream(clean_contract):
    clean_contract.rail("rail.5v").upstream = "rail.ghost"

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites a rail upstream reference to a nonexistent converter
        f.quantity == "topology/rails/rail.5v/upstream" for f in findings
    )


def test_voltage_converter_without_efficiency_warns(clean_contract):
    rail = clean_contract.rail("rail.5v")
    rail.model_fields_set.discard("efficiency")
    rail.efficiency = 1.0
    rail.model_fields_set.discard("efficiency")

    findings = run_record_checks(clean_contract)
    assert any(  # bites silently assuming a lossless voltage converter
        f.quantity == "topology/rails/rail.5v/efficiency" and f.severity == "warning"
        for f in findings
    )


def test_voltage_converted_units_equal_value_does_not_warn(clean_contract):
    # MUTATION-BITE: 3.3 V and 3300 mV differ in the last float bit after
    # unit conversion; a bit-exact != spuriously warns on a lossless rail.
    rail = clean_contract.rail("rail.5v")
    rail.voltage = Quantity(value=3.3, unit="V")
    rail.model_fields_set.discard("efficiency")
    parent = clean_contract.rail("rail.22v")
    parent.voltage = Quantity(value=3300, unit="mV")

    findings = run_record_checks(clean_contract)
    assert not any(
        f.quantity == "topology/rails/rail.5v/efficiency" and f.severity == "warning"
        for f in findings
    )


def test_ampacity_not_scored_on_nonpositive_rail_voltage(clean_contract):
    # MUTATION-BITE: a zero-placeholder rail voltage used to `continue` silently,
    # legally disabling the fire-limit gate instead of disclosing not_scored.
    clean_contract.rail("rail.5v").voltage = Quantity(value=0, unit="V")
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )

    findings = check_ampacity(clean_contract)
    assert any(
        f.quantity == "topology/wires/wire.rail.5v/ampacity" and f.severity == "not_scored"
        for f in findings
    )


def test_planted_rail_voltage_inversion(clean_contract):
    clean_contract.rail("rail.5v").voltage = Quantity(value=24, unit="V")

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites a child rail recorded above its upstream voltage
        f.quantity == "topology/rails/rail.5v/voltage" for f in findings
    )


def test_planted_torque_budget_breach(clean_contract):
    clean_contract.model.joints[0].limits.effort = 4.0

    findings = _violations(run_record_checks(clean_contract))
    torque = next(f for f in findings if f.quantity == "devices/dev.imu/drives_joint")
    assert "3.00 N*m" in torque.observed  # bites omitting continuous torque times gear ratio
    assert "4.00 N*m" in torque.expected  # bites comparing against the wrong joint effort


def test_zero_joint_effort_limit_is_flagged_not_passed(clean_contract):
    """URDF effort="0" means unset: it must not score as a real limit with 100% headroom."""
    from lockwire.checks import check_torque_budgets

    clean_contract.model.joints[0].limits.effort = 0.0
    findings = check_torque_budgets(clean_contract)
    assert [f.severity for f in findings] == ["warning"]  # bites passing a 0 limit
    assert "unset (0)" in findings[0].observed


def test_zero_effort_warns_on_fresh_check_without_actuator(clean_contract):
    """No declared actuator needed; a bare model joint with effort 0 warns."""
    for d in clean_contract.devices:
        d.drives_joint = None
    clean_contract.model.joints[0].limits.effort = 0.0
    name = clean_contract.model.joints[0].name
    found = [f for f in run_record_checks(clean_contract) if "torque is not checked" in f.observed]
    assert [(f.severity, f.quantity) for f in found] == [
        ("warning", f"model/joints/{name}/limits/effort")
    ]


def test_zero_effort_with_actuator_warns_once(clean_contract):
    clean_contract.model.joints[0].limits.effort = 0.0
    found = [f for f in run_record_checks(clean_contract) if "torque is not checked" in f.observed]
    assert len(found) == 1  # bites a duplicate from check_unusable_effort + check_torque_budgets


def test_planted_bus_bandwidth_breach(clean_contract):
    clean_contract.topology.buses[0].bitrate = 100_000

    findings = _violations(run_record_checks(clean_contract))
    bandwidth = next(f for f in findings if f.quantity == "topology/buses/bus.usb0/bandwidth")
    assert "102400 bit/s" in bandwidth.observed  # bites forgetting bytes-to-bits conversion
    assert "60%" in bandwidth.expected  # bites treating raw bitrate as fully usable throughput


def test_unrated_bus_emits_not_scored_finding(clean_contract):
    """None bitrate must surface as not_scored, not vanish into a silent skip."""
    bus = clean_contract.topology.buses[0]
    bus.bitrate = None
    bus.provenance = Provenance(
        witness="w0",
        source="datasheet-baud-unknown.pdf",
        locked_at="2026-07-26",
    )

    findings = check_bus_bandwidth(clean_contract)

    matches = [f for f in findings if f.quantity == "topology/buses/bus.usb0/bandwidth"]
    assert len(matches) == 1
    finding = matches[0]
    assert finding.severity == "not_scored"  # bites escalating unrated buses to warning/NO-GO
    assert finding.observed == "bitrate unrated; bandwidth not scored"
    assert "bitrate" in finding.expected
    assert finding.source == "datasheet-baud-unknown.pdf"
    assert _violations(findings) == []


def test_bus_with_bitrate_but_no_declared_traffic_is_not_scored(clean_contract):
    """A bus with a known bitrate and no driver declaring traffic used to
    emit nothing at all — not a pass, not a not_scored disclosure. Silent skip.
    """
    bus = clean_contract.topology.buses[1]
    assert bus.id == "bus.serial0"
    assert bus.bitrate is not None  # presence floor: this bus must still have a bitrate

    findings = check_bus_bandwidth(clean_contract)

    matches = [f for f in findings if f.quantity == "topology/buses/bus.serial0/bandwidth"]
    assert len(matches) == 1  # bites reverting to the silent skip
    finding = matches[0]
    assert finding.severity == "not_scored"
    assert finding.observed == "no declared traffic on this bus"
    assert "nominal_rate_hz" in finding.expected
    assert "payload_bytes" in finding.expected
    assert _violations(findings) == []


def test_bus_with_declared_traffic_gets_no_traffic_finding(clean_contract):
    """A driver declaring nominal_rate_hz+payload_bytes on the bus is scored,
    not disclosed as a blind spot (bus.usb0, carrying dev.imu's traffic)."""
    findings = check_bus_bandwidth(clean_contract)

    assert not any(f.quantity == "topology/buses/bus.usb0/bandwidth" for f in findings)


def test_unrated_bus_robot_with_no_bandwidth_concern_still_goes(clean_contract, capsys):
    """Unrated bus is visible but informational — false no-gos are the death condition."""
    clean_contract.topology.buses[0].bitrate = None

    findings = run_record_checks(clean_contract)
    unrated = [
        f
        for f in findings
        if f.severity == "not_scored" and f.quantity == "topology/buses/bus.usb0/bandwidth"
    ]
    assert len(unrated) == 1  # bites reverting to silent skip
    assert _violations(findings) == []  # bites turning not-scored into a violation
    assert [f for f in findings if f.severity == "warning"] == []  # bites warning-channel noise
    assert render(findings) == 0  # bites GO becoming NO-GO on an unrated bus
    out = capsys.readouterr().out
    assert "GO" in out
    assert "NO-GO" not in out
    assert "not scored" in out
    assert "bitrate unrated; bandwidth not scored" in out


@pytest.mark.parametrize("field", ["can_id", "address"])
def test_planted_bus_identity_collision(clean_contract, field):
    imu = clean_contract.device("dev.imu")
    radio = clean_contract.device("dev.radio.rfd900")
    radio.identity.bus_ref = imu.identity.bus_ref
    setattr(imu.identity, field, "42")
    setattr(radio.identity, field, "42")

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites duplicate CAN IDs or addresses on one bus passing silently
        f.quantity == f"topology/buses/bus.usb0/identity/{field}" for f in findings
    )


def test_duplicate_serial_warns_and_downgrades_identity(clean_contract):
    imu = clean_contract.device("dev.imu")
    radio = clean_contract.device("dev.radio.rfd900")
    imu.identity.serial_number = radio.identity.serial_number

    findings = run_record_checks(clean_contract)
    duplicates = [f for f in findings if f.quantity == "devices/identity/serial_number"]
    assert [f.severity for f in duplicates] == ["warning"]  # bites clone serials becoming NO-GO
    assert imu.identity.strength == "model"  # bites retaining false instance-strength identity
    assert radio.identity.strength == "model"  # bites downgrading only one side of a clone pair


@pytest.mark.parametrize("limit", [0.0, -1.0])
def test_non_positive_budget_limit_returns_violation_without_crash(clean_contract, limit):
    budget = next(b for b in clean_contract.budgets if b.kind == "rail_power")
    budget.limit = Quantity(value=limit, unit="W")

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites ZeroDivisionError and negative-limit silent arithmetic
        f.quantity == "budgets/bud.power.rail5v/limit" for f in findings
    )


def test_empty_rail_budget_warns(clean_contract):
    for device in clean_contract.devices:
        device.power = None

    findings = run_record_checks(clean_contract)
    assert any(  # bites a declared rail budget with zero witnesses looking green
        f.quantity == "budgets/bud.power.rail5v"
        and f.severity == "warning"
        and "zero contributing" in f.observed
        for f in findings
    )


def test_rail_budget_with_unknown_consumer_draw_is_not_scored_never_summed_as_zero(
    clean_contract,
):
    """A device with power.rail and no declared draw makes the rail's power
    budget not_scored — an unknown consumer must never be summed as 0 W."""
    imu = clean_contract.device("dev.imu")
    imu.power.draw = None

    report = run_record_checks(clean_contract)
    findings_by_quantity = {f.quantity: f for f in report}
    finding = findings_by_quantity["budgets/bud.power.rail5v"]
    assert finding.severity == "not_scored"  # bites summing the unknown draw as 0 W
    assert "dev.imu" in finding.observed and "rail.5v" in finding.observed
    assert not any(
        r.quantity == "budgets/bud.power.rail5v" for r in report.scored_records
    )  # bites it vacuous-passing despite the gap


def test_endurance_budget_with_unknown_consumer_draw_is_not_scored(clean_contract):
    """An unknown draw anywhere on the path to the battery makes endurance
    unsafe to compute."""
    imu = clean_contract.device("dev.imu")
    imu.power.draw = None

    report = run_record_checks(clean_contract)
    findings_by_quantity = {f.quantity: f for f in report}
    finding = findings_by_quantity["budgets/bud.endurance"]
    assert finding.severity == "not_scored"  # bites summing the unknown draw as 0 W
    assert "dev.imu" in finding.observed


def test_ampacity_with_unknown_consumer_draw_is_not_scored(clean_contract):
    """A wire whose rail has any unknown-draw consumer cannot be safely
    scored for ampacity — the true current is unknown, not 0 A."""
    imu = clean_contract.device("dev.imu")
    imu.power.draw = None
    clean_contract.topology.wires.append(
        Wire(id="wire.rail.5v", awg=16, installation="free_air", rail="rail.5v", owner="shivansh")
    )

    findings = check_ampacity(clean_contract)
    finding = next(f for f in findings if f.quantity == "topology/wires/wire.rail.5v/ampacity")
    assert finding.severity == "not_scored"  # bites computing ampacity around the gap
    assert "dev.imu" in finding.observed


def test_voltage_compat_rail_with_no_declared_voltage_is_not_scored(clean_contract):
    """A rail with no declared voltage can't be compared to a device's
    input range — not_scored, never treated as 0 V."""
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"), max=Quantity(value=5.5, unit="V")
    )
    clean_contract.rail("rail.5v").voltage = None

    findings = check_voltage_compatibility(clean_contract)
    finding = next(f for f in findings if f.quantity == "devices/dev.imu/power/rail")
    assert finding.severity == "not_scored"  # bites crashing or treating voltage as 0
    assert "rail.5v" in finding.observed


def test_voltage_compat_range_vs_device_range_pass(clean_contract):
    """A rail voltage_range is scored against the device's whole range,
    compatible iff rail-range is fully inside device-range (inclusive)."""
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"), max=Quantity(value=5.5, unit="V")
    )
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = None
    rail.voltage_range = InputVoltageRange(
        min=Quantity(value=4.75, unit="V"), max=Quantity(value=5.25, unit="V")
    )

    findings = check_voltage_compatibility(clean_contract)
    assert findings == []  # bites a range-vs-range comparison that finds a false violation

    scores = _ScoreBook()
    check_voltage_compatibility(clean_contract, scores=scores)
    record = next(r for r in scores.records if r.quantity == "devices/dev.imu/power/rail")
    assert record.observed == "[4.75 V, 5.25 V]"
    assert record.expected == "[4.50 V, 5.50 V]"


def test_voltage_compat_range_vs_device_range_violation(clean_contract):
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"), max=Quantity(value=5.5, unit="V")
    )
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = None
    rail.voltage_range = InputVoltageRange(
        min=Quantity(value=4.75, unit="V"), max=Quantity(value=6.0, unit="V")
    )

    findings = check_voltage_compatibility(clean_contract)
    finding = next(f for f in findings if f.quantity == "devices/dev.imu/power/rail")
    assert finding.severity == "violation"
    assert "[4.75 V, 6.00 V]" in finding.observed
    assert "[4.50 V, 5.50 V]" in finding.expected


def test_voltage_compat_range_without_voltage_is_scored_not_no_declared_voltage(clean_contract):
    """A rail with a range and no single voltage is NOT the 'no declared
    voltage' not_scored case — the range is itself the voltage fact."""
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"), max=Quantity(value=5.5, unit="V")
    )
    rail = clean_contract.rail(device.power.rail)
    rail.voltage = None
    rail.voltage_range = InputVoltageRange(
        min=Quantity(value=4.75, unit="V"), max=Quantity(value=5.25, unit="V")
    )

    scores = _ScoreBook()
    findings = check_voltage_compatibility(clean_contract, scores=scores)
    assert not any(f.severity == "not_scored" for f in findings)
    assert "devices/dev.imu/power/rail" in scores.keys


def test_rail_current_pass(clean_contract):
    rail = clean_contract.rail("rail.5v")
    rail.current_limit = Quantity(value=1.0, unit="A")  # 2.4 W / 5 V = 0.48 A, under limit

    scores = _ScoreBook()
    findings = check_rail_current(clean_contract, scores=scores)
    assert not any(f.severity == "violation" for f in findings)
    assert "topology/rails/rail.5v/current_limit" in scores.keys
    record = next(r for r in scores.records if r.quantity == "topology/rails/rail.5v/current_limit")
    assert record.observed == "0.48 A"


def test_rail_current_violation(clean_contract):
    rail = clean_contract.rail("rail.5v")
    rail.current_limit = Quantity(value=0.4, unit="A")  # 0.48 A computed > 0.4 A limit
    rail.current_limit_enforced_by = "fuse"

    findings = check_rail_current(clean_contract)
    finding = next(f for f in findings if f.quantity == "topology/rails/rail.5v/current_limit")
    assert finding.severity == "violation"
    assert "0.48 A" in finding.observed
    assert "0.40 A" in finding.expected
    assert "fuse (current_limit)" in finding.expected


def test_rail_current_not_scored_no_voltage_fact(clean_contract):
    rail = clean_contract.rail("rail.5v")
    rail.current_limit = Quantity(value=1.0, unit="A")
    rail.voltage = None

    findings = check_rail_current(clean_contract)
    finding = next(f for f in findings if f.quantity == "topology/rails/rail.5v/current_limit")
    assert finding.severity == "not_scored"  # bites treating an absent voltage fact as 0 V
    assert "voltage" in finding.observed


def test_rail_current_not_scored_unknown_draw(clean_contract):
    """An unknown consumer draw makes the rail's current sum unsummable —
    not_scored, never summed as 0 A."""
    rail = clean_contract.rail("rail.5v")
    rail.current_limit = Quantity(value=1.0, unit="A")
    clean_contract.device("dev.imu").power.draw = None

    scores = _ScoreBook()
    findings = check_rail_current(clean_contract, scores=scores)
    finding = next(f for f in findings if f.quantity == "topology/rails/rail.5v/current_limit")
    assert finding.severity == "not_scored"  # bites summing the unknown draw as 0 A
    assert "dev.imu" in finding.observed
    assert "topology/rails/rail.5v/current_limit" not in scores.keys


def test_rail_current_absent_current_limit_produces_no_findings(clean_contract):
    assert all(rail.current_limit is None for rail in clean_contract.topology.rails)
    assert check_rail_current(clean_contract) == []
    # Standing question: fails if an optional undeclared current_limit is treated as a GAP.


def test_rail_current_clean_fixture_has_zero_findings(clean_contract):
    _assert_clean_baseline(run_record_checks(clean_contract))
    # Standing question: fails if integrating check_rail_current changes the clean fixture's findings.


def test_rail_topology_with_no_declared_voltage_is_not_scored(clean_contract):
    """An unvoltaged rail skips upstream/derived voltage comparisons
    rather than crashing on ``None.base()``."""
    clean_contract.rail("rail.5v").voltage = None

    findings = run_record_checks(clean_contract)
    finding = next(f for f in findings if f.quantity == "topology/rails/rail.5v/voltage")
    assert finding.severity == "not_scored"
    assert "rail.5v" in finding.observed


def test_rail_with_voltage_range_only_discloses_missing_nominal_voltage(clean_contract):
    """A rail with a voltage_range but no nominal voltage discloses
    the specific situation (range present, nominal absent) in its upstream
    voltage finding, not the generic "no declared voltage" message."""
    rail = clean_contract.rail("rail.5v")
    rail.voltage = None
    rail.voltage_range = InputVoltageRange(
        min=Quantity(value=4.5, unit="V"),
        max=Quantity(value=5.5, unit="V"),
    )

    findings = run_record_checks(clean_contract)
    finding = next(f for f in findings if f.quantity == "topology/rails/rail.5v/voltage")
    assert finding.severity == "not_scored"
    assert "voltage range but no nominal voltage" in finding.observed
    # Verify no violation for this rail
    assert not any(f.severity == "violation" and "rail.5v" in str(f.quantity) for f in findings)


def test_cg_excludes_mass_with_no_mount_frame_with_disclosure(clean_contract):
    """A mass with no mount frame still counts in the mass total (it is a
    mass) but is excluded from CG, disclosed."""
    imu = clean_contract.device("dev.imu")
    imu.physical.mount_xyz = None
    imu.physical.mount_frame = None

    cg_report = compute_system_cg(clean_contract)[0]
    imu_contributor = next(
        c for c in cg_report.contributors if c.quantity == "devices/dev.imu/physical/mass"
    )
    assert imu_contributor.position_m is None  # bites treating it as positioned
    assert not cg_report.complete  # the mass counts but leaves CG incomplete
    assert cg_report.limitation is not None
    assert "dev.imu" in cg_report.limitation and "no mount frame" in cg_report.limitation


def test_reference_integrity_skips_missing_mount_frame(clean_contract):
    """Mount_frame becoming optional must not turn an absent frame into a
    reference-integrity violation."""
    imu = clean_contract.device("dev.imu")
    imu.physical.mount_xyz = None
    imu.physical.mount_frame = None

    findings = _violations(run_record_checks(clean_contract))
    assert not any(
        f.quantity == "devices/dev.imu/physical/mount_frame" for f in findings
    )  # bites scoring a None mount_frame against model links


def test_scene_placement_not_scored_no_mount_frame(clean_contract):
    imu = clean_contract.device("dev.imu")
    imu.physical.mount_xyz = None
    imu.physical.mount_frame = None
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.02)))

    findings = check_scene_placement(clean_contract, scene)
    finding = next(f for f in findings if f.quantity == "devices/dev.imu/placement")
    assert finding.severity == "not_scored"
    assert finding.observed == "no mount frame"  # bites reusing the generic mount_xyz message


def test_empty_endurance_budget_is_not_scored_not_a_warning(empty_endurance_budget_contract):
    """A declared battery with
    zero declared draw is unmeasured, not a warning-worthy anomaly — it must
    never score (never a vacuous "0.00 min" pass). Mutation bite: reverting
    the severity to "warning" turns this red.
    """
    findings = run_record_checks(empty_endurance_budget_contract)
    endurance = next(f for f in findings if f.quantity == "budgets/bud.endurance.empty")
    assert endurance.severity == "not_scored"
    assert endurance.observed == "no draw declared"
    assert not any(  # bites it slipping into scored_records despite no draw
        r.quantity == "budgets/bud.endurance.empty" for r in findings.scored_records
    )


def test_budget_limit_cannot_exceed_rail_capacity(clean_contract):
    clean_contract.rail("rail.5v").capacity = Quantity(value=1, unit="W")
    clean_contract.rail("rail.5v").capacity_enforced_by = "fuse"

    findings = _violations(run_record_checks(clean_contract))
    capacity = next(f for f in findings if f.quantity == "budgets/bud.power.rail5v/limit")
    assert "rail capacity 1.00 W" in capacity.expected  # bites Rail.capacity remaining dead weight
    assert "declared enforcement provenance: fuse" in capacity.expected


def test_clean_contract_with_adequately_gauged_wire_stays_zero(clean_contract):
    """Declaring a wire that fits the load must not create false ampacity findings."""
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )
    _assert_clean_baseline(
        run_record_checks(clean_contract)
    )  # bites false ampacity on healthy load; clean baseline = one URDF not_scored


def test_planted_undersized_wire_ampacity_fires(clean_contract):
    """16 AWG free-air NEC limit is 24 A; 125 W on 5 V is 25 A — must fire with those numbers."""
    assert (
        Path(check_ampacity.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    clean_contract.device("dev.imu").power.draw = Quantity(value=0.0, unit="W")
    clean_contract.device("dev.radio.rfd900").power.draw = Quantity(value=125.0, unit="W")
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )

    findings = check_ampacity(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.quantity == "topology/wires/wire.rail.5v/ampacity"
    assert finding.observed_quantity.value == pytest.approx(25.00)
    assert finding.observed_quantity.unit == "A"
    assert finding.expected_quantity.value == pytest.approx(24.00)
    assert finding.expected_quantity.unit == "A"
    assert "16 AWG" in finding.observed
    assert "free_air" in finding.observed
    assert finding.source == AMPACITY_SOURCE
    assert "1.00 A" in finding.delta or "1.00" in finding.delta


def test_ampacity_evaluates_worst_declared_mode_not_defaulted_nominal(clean_contract):
    """Peak above ampacity must fire even when wire.draw_mode stays nominal.

    Reviewer repro: nominal 50 W + declared peak 250 W on 16 AWG free_air (24 A)
    at 5 V → peak is 50 A. Scoring only the silent default yields zero findings
    (confident wrong pass). Option A: worst declared mode must fire and name peak.
    """
    assert (
        Path(check_ampacity.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    clean_contract.device("dev.imu").power.draw = Quantity(value=0.0, unit="W")
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.draw = Quantity(value=50.0, unit="W")
    radio.power.modes = [
        PowerMode(name="peak", draw=Quantity(value=250.0, unit="W")),
    ]
    wire = Wire(
        id="wire.rail.5v",
        awg=16,
        installation="free_air",
        rail="rail.5v",
        owner="shivansh",
    )
    assert wire.draw_mode == "nominal"  # the silent default under test
    clean_contract.topology.wires.append(wire)

    findings = check_ampacity(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.quantity == "topology/wires/wire.rail.5v/ampacity"
    assert finding.observed_quantity.value == pytest.approx(50.00)
    assert finding.observed_quantity.unit == "A"
    assert finding.expected_quantity.value == pytest.approx(24.00)
    assert finding.expected_quantity.unit == "A"
    assert "peak" in finding.observed  # names the mode that breached
    assert "16 AWG" in finding.observed
    assert "free_air" in finding.observed
    assert finding.source == AMPACITY_SOURCE
    assert "26.00 A" in finding.delta or "26.00" in finding.delta


def test_planted_enclosed_wire_uses_published_enclosed_ampacity(clean_contract):
    """Enclosed limit comes from NEC 310.16 (18 A for 16 AWG), not a free-air multiplier."""
    clean_contract.device("dev.imu").power.draw = Quantity(value=0.0, unit="W")
    clean_contract.device("dev.radio.rfd900").power.draw = Quantity(value=100.0, unit="W")
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="enclosed",
            rail="rail.5v",
            owner="shivansh",
        )
    )

    findings = check_ampacity(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.observed_quantity.value == pytest.approx(20.00)
    assert finding.observed_quantity.unit == "A"
    assert finding.expected_quantity.value == pytest.approx(18.00)
    assert finding.expected_quantity.unit == "A"
    assert "enclosed" in finding.observed
    assert finding.source == AMPACITY_SOURCE


def test_unlisted_gauge_emits_not_scored(clean_contract):
    """Gauges absent from the cited NEC tables must not invent an ampacity."""
    clean_contract.topology.wires.append(
        Wire(
            id="wire.signal",
            awg=22,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )

    findings = check_ampacity(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "not_scored"
    assert finding.quantity == "topology/wires/wire.signal/ampacity"
    assert "not scored" in finding.observed
    assert finding.source == AMPACITY_SOURCE
    assert _violations(findings) == []


def test_planted_dangling_wire_rail_ref(clean_contract):
    clean_contract.topology.wires.append(
        Wire(
            id="wire.ghost",
            awg=16,
            installation="free_air",
            rail="rail.ghost",
            owner="shivansh",
        )
    )

    findings = _violations(run_record_checks(clean_contract))
    assert any(  # bites a dangling wire-to-rail reference passing silently
        f.quantity == "topology/wires/wire.ghost/rail" for f in findings
    )


# ---------------------------------------------------------------- URDF drift


def test_urdf_drift_clean_fixture_relative_path_is_not_scored(clean_contract):
    """Relative fixture path cannot resolve without a load dir — not_scored, not a silent GO."""
    findings = check_urdf_drift(clean_contract)
    assert findings != []  # bites the relative-path carve-out returning []
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert findings[0].quantity == "model/structure_hash"
    assert "relative path" in findings[0].observed
    assert "not resolvable" in findings[0].observed
    assert _violations(findings) == []
    assert [f for f in findings if f.severity == "warning"] == []
    _assert_clean_baseline(run_record_checks(clean_contract))


def test_urdf_drift_relative_path_is_not_scored(tmp_path, testbot_urdf):
    """A relative URDF path yields not_scored — never a silent empty list."""
    model = parse_urdf(testbot_urdf)
    model.provenance = Provenance(
        witness="w2",
        source="nested/robot.urdf",
        locked_at="2026-07-26",
    )
    model.source = "nested/robot.urdf"
    contract = Contract(robot=RobotInfo(name="testbot"), model=model)

    findings = check_urdf_drift(contract)
    assert findings != []  # bites the relative-path carve-out returning []
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"  # bites treating unresolved relative as a pass
    assert findings[0].quantity == "model/structure_hash"
    assert "relative path" in findings[0].observed
    assert "not resolvable" in findings[0].observed
    assert _violations(findings) == []


def test_urdf_drift_web_link_says_web_link(testbot_urdf):
    model = parse_urdf(testbot_urdf)
    url = "https://example.com/robot.urdf"
    model.provenance = Provenance(witness="w2", source=url, locked_at="2026-07-26")
    model.source = url
    findings = check_urdf_drift(Contract(robot=RobotInfo(name="testbot"), model=model))
    assert [f.severity for f in findings] == ["not_scored"]
    assert "web link" in findings[0].observed  # bites the relative-path wording
    assert "download the URDF" in findings[0].action  # bites 'restore' advice for a link


def test_urdf_drift_matching_urdf_is_silent(tmp_path, testbot_urdf):
    urdf = tmp_path / "robot.urdf"
    urdf.write_text(testbot_urdf.read_text())
    contract = Contract(robot=RobotInfo(name="testbot"), model=parse_urdf(urdf))

    assert check_urdf_drift(contract) == []  # bites false drift on an unchanged URDF


def test_urdf_drift_added_link_names_the_link(tmp_path, testbot_urdf):
    """Mutation-bite: copy fixture URDF, add a link, check names that link."""
    urdf = tmp_path / "robot.urdf"
    urdf.write_text(testbot_urdf.read_text())
    contract = Contract(robot=RobotInfo(name="testbot"), model=parse_urdf(urdf))

    urdf.write_text(
        testbot_urdf.read_text().replace("</robot>", '  <link name="extra_payload"/>\n</robot>')
    )

    findings = check_urdf_drift(contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.quantity == "model/structure_hash"
    assert "extra_payload" in finding.observed  # bites reporting only 'hash differs'
    assert "link added" in finding.observed


def test_urdf_drift_joint_moved_with_no_recorded_joints_says_so(tmp_path, testbot_urdf):
    """A v0.1 contract (no `joints[]` recorded) still detects a moved joint via the
    hash, but can't name it — the finding must say joints aren't recorded rather
    than silently falling back to 'kinematic topology changed'."""
    from lockwire.urdf import read_urdf_topology

    urdf = tmp_path / "robot.urdf"
    urdf.write_text(testbot_urdf.read_text())
    topology = read_urdf_topology(urdf)
    model = parse_urdf(urdf)
    model.joints = []
    model.structure_hash = topology.topology_hash
    contract = Contract(robot=RobotInfo(name="testbot"), model=model)

    urdf.write_text(testbot_urdf.read_text().replace('xyz="0.1 0.0 -0.02"', 'xyz="0.2 0.0 -0.02"'))

    findings = check_urdf_drift(contract)
    assert len(findings) == 1
    assert "joints not recorded in this contract" in findings[0].observed


def test_urdf_drift_absent_urdf_is_not_scored(tmp_path, testbot_urdf):
    missing = tmp_path / "gone.urdf"
    model = parse_urdf(testbot_urdf)
    model.provenance = Provenance(
        witness="w2",
        source=str(missing),
        locked_at="2026-07-26",
    )
    model.source = str(missing)
    contract = Contract(robot=RobotInfo(name="testbot"), model=model)

    findings = check_urdf_drift(contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"  # bites treating absence as a pass or NO-GO
    assert "absent" in findings[0].observed
    assert _violations(findings) == []


def test_urdf_drift_no_urdf_provenance_is_not_scored(clean_contract):
    clean_contract.model.source = "datasheet.pdf"
    clean_contract.model.provenance = Provenance(
        witness="w0",
        source="datasheet.pdf",
        locked_at="2026-07-26",
    )

    findings = check_urdf_drift(clean_contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"  # bites silent skip on Tier A without a URDF
    assert findings[0].observed == "no URDF provenance; structure drift not scored"
    assert _violations(findings) == []


def test_urdf_drift_unreadable_urdf_is_not_scored(tmp_path, testbot_urdf):
    broken = tmp_path / "broken.urdf"
    broken.write_text("not-xml")
    model = parse_urdf(testbot_urdf)
    model.provenance = Provenance(
        witness="w2",
        source=str(broken),
        locked_at="2026-07-26",
    )
    model.source = str(broken)
    contract = Contract(robot=RobotInfo(name="testbot"), model=model)

    findings = check_urdf_drift(contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"  # bites parse failure becoming a violation
    assert "unreadable" in findings[0].observed
    assert _violations(findings) == []


def _orin_throttle_limit() -> ThermalLimit:
    return ThermalLimit(
        temperature=Quantity(value=99.0, unit="C"),
        kind="throttle",
        provenance=Provenance(
            witness="w0",
            source=THERMAL_TRIP_SOURCE,
            locked_at="2026-07-26",
        ),
    )


def test_planted_thermal_budget_breach_names_estimate_and_limit(clean_contract):
    """P × RθJA + Tamb above 99 °C throttle must fire with typed magnitudes."""
    assert (
        Path(check_thermal_budget.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.
    radio = clean_contract.device("dev.radio.rfd900")
    model = clean_contract.device_model(radio.model)
    model.thermal_limit = _orin_throttle_limit()
    model.thermal_limit.temperature_enforced_by = "firmware"
    # 15 W × 5 C/W + 25 C = 100 C > 99 C throttle
    radio.power.draw = Quantity(value=15.0, unit="W")
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        ambient=Quantity(value=25.0, unit="C"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.quantity == "devices/dev.radio.rfd900/thermal"
    assert finding.observed_quantity is not None
    assert finding.observed_quantity.value == pytest.approx(100.0)
    assert finding.observed_quantity.unit == "C"
    assert finding.expected_quantity is not None
    assert finding.expected_quantity.value == pytest.approx(99.0)
    assert finding.expected_quantity.unit == "C"
    assert "100" in finding.observed
    assert "99" in finding.expected
    assert "firmware (temperature)" in finding.expected
    assert THERMAL_TRIP_SOURCE in finding.source
    assert "heatsink-enclosure-datasheet.pdf" in finding.source
    assert "1.00 C" in finding.delta or "1" in finding.delta


def test_thermal_budget_missing_resistance_is_not_scored(clean_contract):
    """Limit without RθJA must not invent a resistance — not_scored."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = _orin_throttle_limit()
    radio.thermal = DeviceThermal(
        ambient=Quantity(value=25.0, unit="C"),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "not_scored"
    assert finding.quantity == "devices/dev.radio.rfd900/thermal"
    assert "thermal resistance" in finding.observed
    assert "not scored" in finding.observed
    assert THERMAL_TRIP_SOURCE in finding.source
    assert _violations(findings) == []


def test_thermal_budget_missing_ambient_is_not_scored(clean_contract):
    """Limit without declared ambient is not_scored, never a silent guess."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = _orin_throttle_limit()
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "not_scored"
    assert "ambient" in finding.observed
    assert "not scored" in finding.observed
    assert _violations(findings) == []


def test_thermal_budget_under_limit_is_silent(clean_contract):
    """Estimate below the published throttle produces no finding."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = _orin_throttle_limit()
    # 2 W × 5 C/W + 25 C = 35 C << 99 C
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        ambient=Quantity(value=25.0, unit="C"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    assert check_thermal_budget(clean_contract) == []


def test_thermal_budget_undeclared_limit_is_silent(clean_contract):
    """No thermal_limit means the check is not armed — silent, not not_scored."""
    assert check_thermal_budget(clean_contract) == []


def test_thermal_budget_shutdown_kind_is_not_scored(clean_contract):
    """Shutdown alone is context, not the actionable fire threshold."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = ThermalLimit(
        temperature=Quantity(value=105.0, unit="C"),
        kind="shutdown",
        provenance=Provenance(
            witness="w0",
            source=THERMAL_TRIP_SOURCE,
            locked_at="2026-07-26",
        ),
    )
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        ambient=Quantity(value=25.0, unit="C"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert "throttle" in findings[0].observed or "throttle" in findings[0].expected
    assert _violations(findings) == []


def test_thermal_budget_worst_declared_mode_not_defaulted_nominal(clean_contract):
    """Peak dissipation above throttle must fire even when nominal would pass."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = _orin_throttle_limit()
    radio.power.draw = Quantity(value=2.0, unit="W")  # nominal: 2×5+25 = 35 C
    radio.power.modes = [
        PowerMode(name="peak", draw=Quantity(value=15.0, unit="W")),  # 100 C
    ]
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        ambient=Quantity(value=25.0, unit="C"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.observed_quantity.value == pytest.approx(100.0)
    assert finding.expected_quantity.value == pytest.approx(99.0)
    assert "peak" in finding.observed


def test_thermal_budget_clean_fixture_baseline_undisturbed(clean_contract):
    """Integrating the check must not disturb the clean fixture's findings budget."""
    _assert_clean_baseline(run_record_checks(clean_contract))


def test_thermal_budget_fires_at_exact_limit(clean_contract):
    """Estimate landing exactly on the throttle limit must fire a violation."""
    radio = clean_contract.device("dev.radio.rfd900")
    clean_contract.device_model(radio.model).thermal_limit = _orin_throttle_limit()
    # 14.8 W × 5 C/W + 25 C = 99 C (exact)
    radio.power.draw = Quantity(value=14.8, unit="W")
    radio.thermal = DeviceThermal(
        resistance=Quantity(value=5.0, unit="C/W"),
        ambient=Quantity(value=25.0, unit="C"),
        provenance=Provenance(
            witness="w0",
            source="heatsink-enclosure-datasheet.pdf",
            locked_at="2026-07-26",
        ),
    )

    findings = check_thermal_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.observed_quantity.value == pytest.approx(99.0)


def _antenna(gain_dbi: float, source: str) -> AntennaGain:
    return AntennaGain(
        gain=Quantity(value=gain_dbi, unit="dBi"),
        provenance=Provenance(
            witness="w0",
            source=source,
            locked_at="2026-07-26",
        ),
    )


def _plant_radio_link(
    contract,
    *,
    tx_power_dbm: float = 20.0,
    rx_sensitivity_dbm: float = -104.0,
    air_rate_kbit_s: float = 125.0,
    range_m: float = 10_000.0,
    fade_margin_db: float = 25.0,
    installation_loss_db: float = 2.0,
    frequency_hz: float | None = 915e6,
    tx_gain_dbi: float | None = 2.0,
    rx_gain_dbi: float | None = 2.0,
):
    """Install RFD900x-shaped link-budget operands on the clean-fixture radio."""
    radio_dev = contract.device("dev.radio.rfd900")
    model = contract.device_model(radio_dev.model)
    model.radio = Radio(
        tx_power=Quantity(value=tx_power_dbm, unit="dBm"),
        rx_sensitivity=Quantity(value=rx_sensitivity_dbm, unit="dBm"),
        air_rate=Quantity(value=air_rate_kbit_s, unit="kbit/s"),
    )
    model.provenance = Provenance(
        witness="w0",
        source="RFD900x-datasheet-V1.2.pdf",
        locked_at="2026-07-26",
    )
    radio_dev.link_budget = LinkBudget(
        required_range=Quantity(value=range_m, unit="m"),
        fade_margin_required=Quantity(value=fade_margin_db, unit="dB"),
        installation_loss=Quantity(value=installation_loss_db, unit="dB"),
        carrier_frequency=(
            None if frequency_hz is None else Quantity(value=frequency_hz, unit="Hz")
        ),
        tx_antenna=(
            None if tx_gain_dbi is None else _antenna(tx_gain_dbi, "airframe-antenna-datasheet.pdf")
        ),
        rx_antenna=(
            None if rx_gain_dbi is None else _antenna(rx_gain_dbi, "gcs-antenna-datasheet.pdf")
        ),
    )
    return radio_dev


def test_link_budget_insufficient_margin_names_shortfall(clean_contract):
    """Planted failure: fade margin below requirement fires with typed dB magnitudes."""
    assert (
        Path(check_link_budget.__code__.co_filename).resolve()
        == (Path(__file__).parents[1] / "src" / "lockwire" / "checks.py").resolve()
    )  # Verifies this test process imports the current worktree.

    radio = _plant_radio_link(clean_contract)
    radio.link_budget.fade_margin_required_enforced_by = "driver"
    # Pt=20, Gt=Gr=2, L_install=2, Sr=-104 dBm @ 125 kbit/s, 10 km @ 915 MHz.
    fspl = free_space_path_loss_db(10_000.0, 915e6)
    margin = 20.0 + 2.0 + 2.0 - fspl - 2.0 - (-104.0)
    shortfall = 25.0 - margin
    assert margin < 25.0  # precondition: this plant must be a real shortfall

    findings = check_link_budget(clean_contract)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "violation"
    assert finding.quantity == "devices/dev.radio.rfd900/link_budget"
    assert finding.observed_quantity is not None
    assert finding.expected_quantity is not None
    assert finding.observed_quantity.unit == "dB"
    assert finding.expected_quantity.unit == "dB"
    assert finding.observed_quantity.value == pytest.approx(margin)
    assert finding.expected_quantity.value == pytest.approx(25.0)
    assert finding.delta == f"-{shortfall:.2f} dB"
    assert "installation loss" in finding.observed
    assert "RFD900x-datasheet-V1.2.pdf" in (finding.source or "")
    assert "airframe-antenna-datasheet.pdf" in (finding.source or "")
    assert "gcs-antenna-datasheet.pdf" in (finding.source or "")
    assert "driver (fade_margin_required)" in finding.expected


def test_link_budget_adequate_margin_is_silent(clean_contract):
    _plant_radio_link(
        clean_contract,
        tx_power_dbm=30.0,
        range_m=100.0,
        fade_margin_db=20.0,
        installation_loss_db=2.0,
    )
    fspl = free_space_path_loss_db(100.0, 915e6)
    margin = 30.0 + 2.0 + 2.0 - fspl - 2.0 - (-104.0)
    assert margin >= 20.0  # precondition: plant must clear the requirement

    assert check_link_budget(clean_contract) == []


def test_link_budget_undeclared_is_silent(clean_contract):
    assert all(device.link_budget is None for device in clean_contract.devices)
    assert all(model.radio is None for model in clean_contract.device_models)
    assert check_link_budget(clean_contract) == []


def test_link_budget_clean_fixture_baseline_unchanged(clean_contract):
    _assert_clean_baseline(run_record_checks(clean_contract))


def test_link_budget_missing_antenna_is_not_scored(clean_contract):
    _plant_radio_link(clean_contract, tx_gain_dbi=None, rx_gain_dbi=2.0)

    findings = check_link_budget(clean_contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert "tx_antenna" in findings[0].observed
    assert _violations(findings) == []


def test_link_budget_missing_frequency_is_not_scored(clean_contract):
    _plant_radio_link(clean_contract, frequency_hz=None)

    findings = check_link_budget(clean_contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert "carrier_frequency" in findings[0].observed
    assert _violations(findings) == []


def test_link_budget_missing_radio_datasheet_is_not_scored(clean_contract):
    radio_dev = clean_contract.device("dev.radio.rfd900")
    radio_dev.link_budget = LinkBudget(
        required_range=Quantity(value=1000.0, unit="m"),
        fade_margin_required=Quantity(value=20.0, unit="dB"),
        installation_loss=Quantity(value=2.0, unit="dB"),
        carrier_frequency=Quantity(value=915e6, unit="Hz"),
        tx_antenna=_antenna(2.0, "airframe-antenna-datasheet.pdf"),
        rx_antenna=_antenna(2.0, "gcs-antenna-datasheet.pdf"),
    )

    findings = check_link_budget(clean_contract)
    assert len(findings) == 1
    assert findings[0].severity == "not_scored"
    assert "radio datasheet" in findings[0].observed
    assert _violations(findings) == []


def test_link_budget_installation_loss_reduces_margin_by_declared_db(clean_contract):
    """A declared 3 dB installation loss reduces reported margin by exactly 3 dB."""
    _plant_radio_link(clean_contract, installation_loss_db=0.0)
    zero_loss = check_link_budget(clean_contract)
    assert len(zero_loss) == 1
    assert zero_loss[0].observed_quantity is not None
    margin_zero = zero_loss[0].observed_quantity.value

    _plant_radio_link(clean_contract, installation_loss_db=3.0)
    with_loss = check_link_budget(clean_contract)
    assert len(with_loss) == 1
    assert with_loss[0].observed_quantity is not None
    margin_three = with_loss[0].observed_quantity.value

    assert margin_zero - margin_three == pytest.approx(3.0)


def test_link_budget_omitted_installation_loss_rejects_unqualified_margin():
    """Omitting installation_loss fails validation — no unqualified margin path."""
    payload = {
        "required_range": Quantity(value=1000.0, unit="m"),
        "fade_margin_required": Quantity(value=20.0, unit="dB"),
        "carrier_frequency": Quantity(value=915e6, unit="Hz"),
        "tx_antenna": _antenna(2.0, "airframe-antenna-datasheet.pdf"),
        "rx_antenna": _antenna(2.0, "gcs-antenna-datasheet.pdf"),
    }
    with pytest.raises(ValidationError, match="installation_loss"):
        LinkBudget.model_validate(payload)


def test_link_budget_quantity_aliases_reject_wrong_dimension():
    """RF Quantity aliases bind power_level / gain / data_rate / rate dimensions."""
    with pytest.raises(ValidationError, match="RF power-level quantity"):
        Radio(
            tx_power=Quantity(value=20.0, unit="W"),
            rx_sensitivity=Quantity(value=-104.0, unit="dBm"),
            air_rate=Quantity(value=125.0, unit="kbit/s"),
        )
    with pytest.raises(ValidationError, match="RF gain quantity"):
        AntennaGain(
            gain=Quantity(value=2.0, unit="dBm"),
            provenance=Provenance(
                witness="w0",
                source="antenna.pdf",
                locked_at="2026-07-26",
            ),
        )
    with pytest.raises(ValidationError, match="data-rate quantity"):
        Radio(
            tx_power=Quantity(value=20.0, unit="dBm"),
            rx_sensitivity=Quantity(value=-104.0, unit="dBm"),
            air_rate=Quantity(value=125.0, unit="Hz"),
        )
    with pytest.raises(ValidationError, match="carrier-frequency quantity"):
        LinkBudget(
            required_range=Quantity(value=100.0, unit="m"),
            fade_margin_required=Quantity(value=20.0, unit="dB"),
            installation_loss=Quantity(value=2.0, unit="dB"),
            carrier_frequency=Quantity(value=915.0, unit="dBm"),
        )


def test_link_budget_field_validators_reject_nonpositive_operands():
    """Range/frequency/air_rate must be positive; margins and loss must be >= 0."""
    with pytest.raises(ValidationError, match="required_range must be positive"):
        LinkBudget(
            required_range=Quantity(value=0.0, unit="m"),
            fade_margin_required=Quantity(value=20.0, unit="dB"),
            installation_loss=Quantity(value=2.0, unit="dB"),
        )
    with pytest.raises(ValidationError, match="fade_margin_required must be >= 0"):
        LinkBudget(
            required_range=Quantity(value=100.0, unit="m"),
            fade_margin_required=Quantity(value=-1.0, unit="dB"),
            installation_loss=Quantity(value=2.0, unit="dB"),
        )
    with pytest.raises(ValidationError, match="installation_loss must be >= 0"):
        LinkBudget(
            required_range=Quantity(value=100.0, unit="m"),
            fade_margin_required=Quantity(value=20.0, unit="dB"),
            installation_loss=Quantity(value=-0.5, unit="dB"),
        )
    with pytest.raises(ValidationError, match="carrier_frequency must be positive"):
        LinkBudget(
            required_range=Quantity(value=100.0, unit="m"),
            fade_margin_required=Quantity(value=20.0, unit="dB"),
            installation_loss=Quantity(value=2.0, unit="dB"),
            carrier_frequency=Quantity(value=0.0, unit="Hz"),
        )
    with pytest.raises(ValidationError, match="air_rate must be positive"):
        Radio(
            tx_power=Quantity(value=20.0, unit="dBm"),
            rx_sensitivity=Quantity(value=-104.0, unit="dBm"),
            air_rate=Quantity(value=0.0, unit="kbit/s"),
        )


# ---------------------------------------------------------------- presence floor


def _plain_render(findings) -> tuple[int, str]:
    console = Console(record=True, width=200, color_system=None)
    result = render(findings, console)
    return result, console.export_text()


# Clean fixture scored set. Justification line-by-line:
# Each key is recorded only when a check compared two independent populated
# operands. For budget and ampacity rollups that means the summed witness
# MAGNITUDE is > 0 — not merely a non-empty contributor list, which is what
# cycle 3 claimed and what a list of zero-draw devices satisfies while summing
# to nothing. Every earlier version of this comment overstated its guard, and
# that is how three cycles of this defect shipped. Keep it true or delete it.
#   devices/dev.imu       — torque (joint effort vs actuator×gear) AND
#                           envelopes vs hard_limits (accel_z)
#   devices/dev.barometer is no longer scored here: its under-sampled pressure
#                           envelope was removed; the strict hard limit remains.
#   rails/rail.5v         — rail voltage vs upstream rail.22v
#   rails/rail.22v        — rail voltage vs battery voltage
#   budgets/bud.power.rail5v — limit vs rolled-up device draw (imu+radio on rail.5v)
#   budgets/bud.mass.devices — limit vs summed device masses (imu+radio physical)
#   budgets/bud.endurance — required hours vs capacity/draw (draw > 0)
#   buses/bus.usb0        — bitrate ceiling vs imu traffic (rate×payload)
# NOT scored: links (no second source; URDF unresolvable),
#   devices/dev.gnss (rate envelope only, no hard_limit / power / torque),
#   devices/dev.radio.rfd900 (mass/power are budget witnesses, not a device-side
#     D3 comparison; no envelopes, no torque, no bandwidth contributor driver),
#   devices/dev.esc.busless (owner+model only),
#   buses/bus.serial0 (bitrate set but no rate×payload contributor).
# one key per comparison — dev.imu's torque and its two envelope-vs-hard-limit
# comparisons (nominal-op and bench-idle accel_z) no longer collapse onto a shared
# "devices/dev.imu" key.
_CLEAN_SCORED_KEYS = frozenset(
    {
        "devices/dev.imu/drives_joint",
        "devices/dev.imu/envelopes/nominal-op/accel_z",
        "devices/dev.imu/envelopes/bench-idle/accel_z",
        "rails/rail.5v",
        "rails/rail.22v",
        "budgets/bud.power.rail5v",
        "budgets/bud.endurance",
        "buses/bus.usb0",
        "model/joints/imu_mount/limits/effort",  # gravity reach: the vertical mount joint holds
    }
)


def test_empty_model_does_not_claim_records_agree():
    """Empty scoring surface must disclose, never claim agreement.

    Presence floor (LAW 1): ``scored == 0`` is the denominator assertion —
    mutation-bite 1 deletes it and this test goes red (or the production floor
    that populates it). Without the floor, an empty stub prints "records agree".
    """
    contract = load_contract(Path(__file__).parent / "fixtures" / "contract_empty.yaml")
    findings = run_record_checks(contract)

    # THE floor — delete this line and the empty-contract test is decoration again.
    assert findings.scored == 0
    assert count_scored_comparisons(contract) == 0

    disclosures = [
        f for f in findings if f.severity == "not_scored" and f.quantity == EMPTY_SCORE_QUANTITY
    ]
    assert len(disclosures) == 1
    assert disclosures[0].observed == EMPTY_SCORE_OBSERVED

    exit_code, text = _plain_render(findings)
    assert exit_code == 3
    assert "records agree" not in text
    assert "nothing compared" in text
    assert EMPTY_SCORE_OBSERVED in " ".join(text.split())


def test_owner_model_only_device_is_nothing_scored():
    """Case A: owner+model alone is a declaration, not a comparison .

    ``check_device_owners`` and model FK resolution run, but neither compares two
    independent populated operands. Counting ``len(devices)`` would claim
    ``records agree (1 scored)`` — the defect this bounce closes.
    """
    contract = load_contract(Path(__file__).parent / "fixtures" / "contract_owner_only.yaml")
    findings = run_record_checks(contract)

    assert findings.scored == 0
    assert count_scored_comparisons(contract) == 0
    assert findings.scored_keys == frozenset()
    assert any(f.severity == "not_scored" and f.quantity == EMPTY_SCORE_QUANTITY for f in findings)

    exit_code, text = _plain_render(findings)
    assert exit_code == 3
    assert "nothing compared" in text
    assert "records agree" not in text
    assert "(1 scored)" not in text


def test_clean_scored_denominator_is_comparisons_not_declarations(clean_contract):
    """Case B: denominator tracks only declarations a check actually compared.

    Pins the exact key set (see ``_CLEAN_SCORED_KEYS``). Returning a constant
    from ``count_scored_comparisons`` or counting ``len(devices)+…`` goes red.
    """
    findings = run_record_checks(clean_contract)
    _assert_clean_baseline(findings)

    assert findings.scored_keys == _CLEAN_SCORED_KEYS
    # The under-sampled barometer pressure band, dropping its
    # envelope-vs-hard-limit comparison was removed, then restored.
    assert findings.scored == len(_CLEAN_SCORED_KEYS) == 9
    assert count_scored_comparisons(clean_contract) == 9

    # Declarations that must NOT inflate the count (the bounce's constructed case
    # living inside the clean fixture).
    for absent in (
        "devices/dev.gnss",
        "devices/dev.radio.rfd900",
        "devices/dev.esc.busless",
        "buses/bus.serial0",
        "links/base_link",
        "links/imu_link",
    ):
        assert absent not in findings.scored_keys

    exit_code, text = _plain_render(findings)
    assert exit_code == 0
    assert "records agree (9 scored)" in text
    assert "nothing scored" not in text


def test_stripping_scoring_operands_drops_denominator(clean_contract):
    """Mutation-bite 2: strip operands from a scoring declaration → count drops.

    If the denominator still counted declarations, clearing the IMU payload operand
    would leave ``buses/bus.usb0`` in the count. It must leave.
    """
    before = run_record_checks(clean_contract)
    assert before.scored == 9
    assert "buses/bus.usb0" in (before.scored_keys or frozenset())

    imu = clean_contract.device("dev.imu")
    assert imu is not None and imu.driver is not None
    imu.driver.payload_bytes = None

    after = run_record_checks(clean_contract)
    assert after.scored == 8
    assert "buses/bus.usb0" not in (after.scored_keys or frozenset())
    assert count_scored_comparisons(clean_contract) == 8


def test_emptying_clean_contract_in_memory_fires_empty_score_disclosure(clean_contract):
    """Floor tracks scored comparisons, not a hardcoded empty-fixture path."""
    populated = run_record_checks(clean_contract)
    assert populated.scored is not None and populated.scored > 0

    # Empty every surface that can feed a D3 comparison.
    clean_contract.model.joints = []  # before links: a joint with a ghost link is refused
    clean_contract.model.links = []
    for device in clean_contract.devices:
        for envelope in device.envelopes:
            envelope.fitted_bands = {}
    clean_contract.fitted_band_allocations = []
    clean_contract.devices = []
    clean_contract.budgets = []
    clean_contract.edges = []
    if clean_contract.topology is not None:
        clean_contract.topology.rails = []
        clean_contract.topology.buses = []
        clean_contract.topology.wires = []

    findings = run_record_checks(clean_contract)
    assert findings.scored == 0
    assert any(
        f.severity == "not_scored"
        and f.quantity == EMPTY_SCORE_QUANTITY
        and f.observed == EMPTY_SCORE_OBSERVED
        for f in findings
    )
    exit_code, text = _plain_render(findings)
    assert exit_code == 3
    assert "records agree" not in text


def test_clean_go_line_states_scored_denominator(clean_contract):
    """Non-zero denominator appears on the GO line so a thin contract looks thin."""
    findings = run_record_checks(clean_contract)
    _assert_clean_baseline(findings)
    exit_code, text = _plain_render(findings)
    assert exit_code == 0
    assert f"records agree ({findings.scored} scored)" in text
    assert "nothing scored" not in text


# ---------------------------------------------------------------- witness-side gating


def test_rail_power_without_witnesses_is_not_scored(clean_contract):
    """Executed defect: power budget on a rail nothing draws from must not score."""
    for d in clean_contract.devices:
        if d.power is not None:
            d.power.rail = "rail.22v"
    r = run_record_checks(clean_contract)
    assert "budgets/bud.power.rail5v" not in (r.scored_keys or frozenset())


def test_mass_total_without_witnesses_is_not_scored(clean_contract):
    """Mirror: mass_total against an empty contributor set must not score."""
    for d in clean_contract.devices:
        d.physical = None
    if clean_contract.model is not None:
        for link in clean_contract.model.links:
            link.mass = None
    r = run_record_checks(clean_contract)
    assert "budgets/bud.mass.devices" not in (r.scored_keys or frozenset())


def test_no_devices_does_not_score_vacuous_budgets(clean_contract):
    """Executed defect: zero devices scored 4 including two empty-sum budgets.

    Phase 1: with devices cleared, vacuous budgets must leave the denominator.
    Rails still compare voltage vs upstream (real operands). Phase 2 strips the
    remaining scoring surface (rails/buses) and clears budgets so dangling
    ``budget.rail`` refs do not trip reference-integrity — same emptying shape
    as ``test_emptying_clean_contract_in_memory_fires_empty_score_disclosure``.
    """
    for device in clean_contract.devices:
        for envelope in device.envelopes:
            envelope.fitted_bands = {}
    clean_contract.fitted_band_allocations = []
    clean_contract.devices = []
    clean_contract.edges = []
    mid = run_record_checks(clean_contract)
    scored = mid.scored_keys or frozenset()
    assert "budgets/bud.power.rail5v" not in scored
    assert "budgets/bud.mass.devices" not in scored
    assert "budgets/bud.endurance" not in scored

    assert clean_contract.topology is not None
    clean_contract.budgets = []
    clean_contract.topology.rails = []
    clean_contract.topology.buses = []
    clean_contract.topology.wires = []
    clean_contract.model.joints = []  # gravity reach scores the mount joint

    findings = run_record_checks(clean_contract)
    assert findings.scored == 0
    assert any(
        f.severity == "not_scored"
        and f.quantity == EMPTY_SCORE_QUANTITY
        and f.observed == EMPTY_SCORE_OBSERVED
        for f in findings
    )
    exit_code, text = _plain_render(findings)
    assert exit_code == 3
    assert "records agree" not in text
    assert "nothing compared" in text


def test_ampacity_without_witnesses_is_not_scored(clean_contract):
    """Site 6: wire on a rail with zero power witnesses must not score ampacity.

    No shipped fixture declares ``wires:`` — this path is otherwise unreachable.
    """
    for d in clean_contract.devices:
        if d.power is not None:
            d.power.rail = "rail.22v"
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v.empty",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )
    r = run_record_checks(clean_contract)
    assert "wires/wire.rail.5v.empty" not in (r.scored_keys or frozenset())


def test_ampacity_with_witnesses_is_scored(clean_contract):
    """Honest path: ampacity with real rail draw still enters the denominator."""
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )
    r = run_record_checks(clean_contract)
    assert "wires/wire.rail.5v" in (r.scored_keys or frozenset())


# --------------------------------------------------------------- presence floor, part 4
# A non-empty contributor list whose every contribution is ZERO is still nothing.
# Cycle 3 guarded the list; these guard the rolled-up magnitude. Each was shown
# RED on cycle 3's tree (bd6a534) before the fix.


def _zero_every_draw(contract) -> int:
    """Zero every declared draw while leaving devices attached to their rails."""
    zeroed = 0
    for device in contract.devices:
        if device.power is None:
            continue
        if device.power.draw is not None:
            device.power.draw.value = 0.0
            zeroed += 1
        for mode in (device.power.modes or {}).values():
            if getattr(mode, "draw", None) is not None:
                mode.draw.value = 0.0
    return zeroed


def test_zero_draw_witnesses_do_not_score_rail_power(clean_contract):
    """Devices stay on the rail; only the magnitudes go to zero (cycle-4 defect)."""
    assert _zero_every_draw(clean_contract) > 0, "presence floor: nothing was zeroed"

    report = run_record_checks(clean_contract)
    assert "budgets/bud.power.rail5v" not in (report.scored_keys or frozenset())


def test_zero_mass_witnesses_do_not_score_mass_total(clean_contract):
    """A non-empty list of zero-mass witnesses sums to 0.0 kg — not a comparison."""
    weighed = 0
    for device in clean_contract.devices:
        if device.physical is not None and device.physical.mass is not None:
            device.physical.mass.value = 0.0
            weighed += 1
    if clean_contract.model is not None:
        for link in clean_contract.model.links:
            if link.mass is not None:
                link.mass.value = 0.0
                weighed += 1
    assert weighed > 0, "presence floor: nothing was zeroed"

    report = run_record_checks(clean_contract)
    assert "budgets/bud.mass.devices" not in (report.scored_keys or frozenset())


def test_zero_draw_witnesses_do_not_score_ampacity(clean_contract):
    """0 W witnesses give 0 A against a published NEC limit — a vacuous pass.

    The function's own comment forbade exactly this while its guard checked only
    list non-emptiness.
    """
    clean_contract.topology.wires.append(
        Wire(
            id="wire.rail.5v",
            awg=16,
            installation="free_air",
            rail="rail.5v",
            owner="shivansh",
        )
    )
    assert "wires/wire.rail.5v" in (run_record_checks(clean_contract).scored_keys or frozenset()), (
        "honest path must score before the mutation, or this test proves nothing"
    )

    _zero_every_draw(clean_contract)
    report = run_record_checks(clean_contract)
    assert "wires/wire.rail.5v" not in (report.scored_keys or frozenset())


def test_one_zero_draw_device_among_positive_siblings_still_scores(clean_contract):
    """The guard must not overshoot: a real rollup survives one 0 W member.

    Scoring on the SUM rather than on every contributor is deliberate — a device
    that genuinely draws nothing in the scored mode does not erase the rail's
    other loads, and dropping the key there would under-report a real comparison.
    """
    baseline = run_record_checks(clean_contract)
    assert "budgets/bud.power.rail5v" in (baseline.scored_keys or frozenset())

    for device in clean_contract.devices:
        if device.power is not None and device.power.draw is not None:
            device.power.draw.value = 0.0
            break

    report = run_record_checks(clean_contract)
    assert "budgets/bud.power.rail5v" in (report.scored_keys or frozenset())


def test_zero_rate_traffic_does_not_score_bus_bandwidth(clean_contract):
    """Fourth form, found by supe self-checking cycle 4's own fix.

    A device declaring ``nominal_rate_hz = 0`` is a contributor carrying nothing:
    0 bps against the utilisation ceiling passes for no reason. The cycle-3
    review graded this site "soft" rather than a defect; it is the same shape as
    the three it bounced.
    """
    assert "buses/bus.usb0" in (run_record_checks(clean_contract).scored_keys or frozenset()), (
        "honest path must score first, or this test proves nothing"
    )

    zeroed = 0
    for device in clean_contract.devices:
        if device.driver is not None and device.driver.nominal_rate_hz is not None:
            device.driver.nominal_rate_hz = 0.0
            zeroed += 1
    assert zeroed > 0, "presence floor: nothing was zeroed"

    report = run_record_checks(clean_contract)
    assert "buses/bus.usb0" not in (report.scored_keys or frozenset())


def test_zero_dissipation_does_not_score_thermal_budget(clean_contract):
    """Fifth form: 0 W gives Tj = ambient, which passes a throttle limit for free.

    ``_device_worst_draw_w`` returns ``None`` only when a device declares no
    power at all — a declared ``0 W`` returns ``(0.0, "nominal")``, so the
    presence guard was satisfied while nothing was dissipated. Found by
    re-running cycle 4's closing question against the untouched sites.
    """

    def _with_draw(watts: float):
        contract = load_contract(Path(__file__).parent / "fixtures" / "contract_clean.yaml")
        radio = contract.device("dev.radio.rfd900")
        model = contract.device_model(radio.model)
        model.thermal_limit = _orin_throttle_limit()
        radio.power.draw = Quantity(value=watts, unit="W")
        radio.thermal = DeviceThermal(
            resistance=Quantity(value=5.0, unit="C/W"),
            ambient=Quantity(value=25.0, unit="C"),
            provenance=Provenance(
                witness="w0",
                source="heatsink-enclosure-datasheet.pdf",
                locked_at="2026-07-26",
            ),
        )
        return contract

    # Honest path first, or this test proves nothing.
    assert "devices/dev.radio.rfd900" in (
        run_record_checks(_with_draw(15.0)).scored_keys or frozenset()
    )

    report = run_record_checks(_with_draw(0.0))
    assert "devices/dev.radio.rfd900" not in (report.scored_keys or frozenset())


# ---------------------------------------------------------------- charged rails


def _charge_rail(contract, *, volts: float, charged_by: str | None):
    """Put a rail ABOVE its battery — the aircraft charging-bus shape."""
    rail = next(r for r in contract.topology.rails if r.id == "rail.22v")
    rail.voltage = Quantity(value=volts, unit="V")
    rail.charged_by = charged_by
    return rail


def _add_alternator(contract, *, rating: Band | None):
    """A charging device, optionally carrying a published rated output."""
    model = contract.device_models[0].model_copy(deep=True)
    model.id = "mdl.alternator"
    if rating is not None:
        model.hard_limits = {"regulated_voltage": rating}
    else:
        model.hard_limits = {}
    contract.device_models.append(model)

    device = contract.devices[0].model_copy(deep=True)
    device.id = "alternator"
    device.model = "mdl.alternator"
    contract.devices.append(device)
    return device


def test_rail_above_battery_without_charged_by_still_violates(clean_contract):
    """The catch survives. A bus above its pack with nothing declared to hold it
    there is still an unmodeled step-up, and still a violation."""
    _charge_rail(clean_contract, volts=28.0, charged_by=None)

    findings = check_rail_topology(clean_contract)

    voltage = [f for f in findings if f.quantity == "topology/rails/rail.22v/voltage"]
    assert len(voltage) == 1, f"expected the step-up catch to fire, got {findings}"
    assert voltage[0].severity == "violation"
    # Standing question: fails if the charged-rail path ever swallows the general case.


def test_charged_rail_scored_against_its_rating_is_silent(clean_contract):
    """The Cessna case: 28 V bus over a 24 V pack, held there by an alternator whose
    published rated output is 28 V. Two independently authored records that agree."""
    _add_alternator(clean_contract, rating=Band(lo=28.0, hi=28.0, unit="V"))
    _charge_rail(clean_contract, volts=28.0, charged_by="alternator")

    findings = check_rail_topology(clean_contract)

    assert [f for f in findings if f.quantity == "topology/rails/rail.22v/voltage"] == []


def test_charged_rail_outside_its_rating_violates(clean_contract):
    """A charging source is not an escape hatch: the rail is still bounded, just by
    the alternator's rating instead of by the battery."""
    _add_alternator(clean_contract, rating=Band(lo=28.0, hi=28.0, unit="V"))
    _charge_rail(clean_contract, volts=48.0, charged_by="alternator")

    findings = check_rail_topology(clean_contract)

    voltage = [f for f in findings if f.quantity == "topology/rails/rail.22v/voltage"]
    assert len(voltage) == 1, f"expected a violation against the rating, got {findings}"
    assert voltage[0].severity == "violation"
    assert "alternator" in voltage[0].expected


def test_charged_rail_without_a_rating_is_not_scored(clean_contract):
    """The gate has a correct answer for a rating it does not have, and
    reaches it instead of refusing. Not a pass — a disclosed gap."""
    _add_alternator(clean_contract, rating=None)
    _charge_rail(clean_contract, volts=28.0, charged_by="alternator")

    findings = check_rail_topology(clean_contract)

    voltage = [f for f in findings if f.quantity == "topology/rails/rail.22v/voltage"]
    assert len(voltage) == 1, f"expected a disclosure, got {findings}"
    assert voltage[0].severity == "not_scored"


def test_charged_rail_without_a_rating_is_not_counted_as_scored(clean_contract):
    """LAW 1: the presence floor must not inflate. An unrated charging source
    means that rail was NOT compared, and must not be counted as though it were."""
    _add_alternator(clean_contract, rating=None)
    _charge_rail(clean_contract, volts=28.0, charged_by="alternator")
    unrated = count_scored_comparisons(clean_contract)

    clean_contract.device_models[-1].hard_limits = {
        "regulated_voltage": Band(lo=28.0, hi=28.0, unit="V")
    }
    rated = count_scored_comparisons(clean_contract)

    assert rated == unrated + 1, f"rated={rated} unrated={unrated}"


def test_dangling_charged_by_is_a_reference_violation(clean_contract):
    """A charged_by pointing at nothing is a dangling ref like every other."""
    _charge_rail(clean_contract, volts=28.0, charged_by="no-such-device")

    findings = check_reference_integrity(clean_contract)

    dangling = [f for f in findings if f.quantity == "topology/rails/rail.22v/charged_by"]
    assert len(dangling) == 1, f"expected a dangling-ref violation, got {findings}"
    assert dangling[0].severity == "violation"


# ---------------------------------------------------------------- CG coverage signal


def _cg_line(contract) -> str:
    """The rendered CG block, as a demo audience would read it."""
    console = Console(width=200, no_color=True, force_terminal=False)
    with console.capture() as captured:
        render(RecordReport([], information=compute_system_cg(contract)), console=console)
    return captured.get()


def test_cg_prints_coverage_against_the_declared_total(clean_contract):
    """The completeness metric used to divide the model by itself: every CG whose
    contributors were all positioned read as whole, however little of the machine
    it actually held. Coverage is now stated against the budget's declared total."""
    out = _cg_line(clean_contract)

    assert "coverage:" in out, out
    assert "kg declared" in out, out


def test_cg_coverage_is_printed_even_when_every_contributor_is_positioned(clean_contract):
    """The complete branch is exactly the one that used to say nothing — a CG
    computed from 3% of an aeroplane printed bare, with no qualifier, and GO."""
    reports = compute_system_cg(clean_contract)
    assert reports, "fixture must carry a mass budget for this to mean anything"
    assert all(r.complete for r in reports), "fixture is expected fully positioned here"

    assert "coverage:" in _cg_line(clean_contract)
    # Standing question: fails if the complete path ever drops its coverage line again.


def test_cg_coverage_shrinks_when_the_declared_total_grows(clean_contract):
    """Coverage must track the DECLARED denominator, not the model's own sum."""
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.limit = Quantity(value=budget.limit.value * 10, unit=budget.limit.unit)

    report = compute_system_cg(clean_contract)[0]

    assert report.budget_limit_kg is not None
    share = report.positioned_mass_kg / report.budget_limit_kg
    assert share < 0.5, f"expected a small share against a 10x limit, got {share:.3f}"
    assert f"{share:.1%}" in _cg_line(clean_contract)


# ------------------------------------------------------- what adds up (scored records)


def test_check_budgets_rail_power_pass_yields_scored_record_with_headroom_margin(clean_contract):
    """A budget that passes must leave a record: capacity - draw and the percentage,
    not silence. Mutation bite: deleting the ``scores.passed`` call on the rail_power
    pass branch makes this test fail without touching a single Finding."""
    report = run_record_checks(clean_contract)
    record = next(r for r in report.scored_records if r.quantity == "budgets/bud.power.rail5v")
    assert record.observed == "2.40 W"
    assert record.expected == "<= 5.00 W"
    assert record.margin == "2.60 W headroom (52%)"


def test_check_voltage_compatibility_pass_yields_scored_record(clean_contract):
    """The compatible-rail branch (previously silent) must now leave a record."""
    device = clean_contract.device("dev.imu")
    model = clean_contract.device_model(device.model)
    model.input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.75, unit="V"),
        max=Quantity(value=5.5, unit="V"),
    )

    report = run_record_checks(clean_contract)
    records = [r for r in report.scored_records if r.quantity == "devices/dev.imu/power/rail"]
    # Exactly one record per comparison: a duplicate would inflate the presence floor's
    # pass side without touching ``scored`` (review bite, 2026-09-06).
    assert len(records) == 1
    record = records[0]
    assert record.observed == "5.00 V"
    assert record.expected == "[4.75 V, 5.50 V]"
    assert "inside the 4.75 V-5.50 V window" in record.margin


@pytest.mark.parametrize(
    "contract_path",
    [
        pytest.param(path, id=path.name)
        for path in sorted((Path(__file__).parent / "fixtures").glob("*.yaml"))
    ]
    + [
        pytest.param(path, id=f"x500/{path.name}")
        for path in sorted((Path(__file__).parent / "fixtures" / "x500").glob("*.yaml"))
    ],
)
def test_scored_records_plus_findings_equals_scored_for_every_fixture(contract_path):
    """len(scored_records) + violations + warnings == scored (presence floor),
    for every checked-in fixture that scores anything - one scored key per
    comparison, every pass leaves a record, no documented gaps."""
    report = run_record_checks(load_contract(contract_path))
    if not report.scored:
        return
    violations = sum(f.severity == "violation" for f in report)
    warnings = sum(f.severity == "warning" for f in report)
    accounted = len(report.scored_records) + violations + warnings
    assert report.scored == accounted, (
        f"{contract_path.name}: scored={report.scored} accounted={accounted}"
    )


# --------------------------------------------------------------------------- scene placement


def _part(lw_id: str, translation: tuple[float, float, float]) -> ScenePart:
    return ScenePart(
        node=lw_id,
        extras_id=lw_id,
        name_chain=(lw_id,),
        positions=(),
        normals=None,
        indices=(),
        color=(0.8, 0.8, 0.8, 1.0),
        translation=translation,
    )


def _scene(*parts: ScenePart) -> Scene:
    return Scene(
        parts=tuple(parts),
        up="z",
        nodes_with_ids={p.extras_id: p.node for p in parts if p.extras_id},
        source="synthetic.glb",
        bounds=((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        triangles=0,
        vertices=0,
    )


def test_check_scene_placement_agrees_within_tolerance(clean_contract):
    """dev.imu declares mount_xyz (0.1, -0.04, 0.02) m in base_link, the root frame."""
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.02)))
    scores = _ScoreBook()
    findings = check_scene_placement(clean_contract, scene, scores=scores)
    assert not any(f.quantity == "devices/dev.imu/placement" for f in findings)
    passed = [r for r in scores.records if r.quantity == "devices/dev.imu/placement"]
    assert len(passed) == 1


def test_check_scene_placement_warns_past_tolerance(clean_contract):
    """6 mm off in z is past the 5 mm tolerance -> a warning, not a violation."""
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.026)))
    findings = check_scene_placement(clean_contract, scene)
    warning = next(f for f in findings if f.quantity == "devices/dev.imu/placement")
    assert warning.severity == "warning"
    assert "6.0 mm" in (warning.delta or "")


def test_check_scene_placement_bite_tolerance_change_flips_result(clean_contract, monkeypatch):
    """Bite: raising the tolerance to 1 m must turn the 6 mm warning into nothing."""
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.026)))
    monkeypatch.setattr("lockwire.checks.PLACEMENT_TOLERANCE_M", 1.0)
    findings = check_scene_placement(clean_contract, scene)
    assert not any(f.quantity == "devices/dev.imu/placement" for f in findings)


def test_check_scene_placement_not_scored_without_mount_xyz(clean_contract):
    clean_contract.device("dev.imu").physical.mount_xyz = None
    scene = _scene(_part("dev.imu", (0.1, -0.04, 0.02)))
    findings = check_scene_placement(clean_contract, scene)
    finding = next(f for f in findings if f.quantity == "devices/dev.imu/placement")
    assert finding.severity == "not_scored"
    assert finding.observed == "drawn where the model put it"


def test_check_scene_placement_missing_part_is_not_scored(clean_contract):
    scene = _scene()  # neither dev.imu nor dev.radio.rfd900 has a part
    findings = check_scene_placement(clean_contract, scene)
    finding = next(f for f in findings if f.quantity == "devices/dev.imu/placement")
    assert finding.severity == "not_scored"
    assert finding.observed == "not in the model"


def test_check_scene_placement_flags_scene_only_id(clean_contract):
    scene = _scene(
        _part("dev.imu", (0.1, -0.04, 0.02)),
        _part("dev.radio.rfd900", (-0.2, 0.08, 0.05)),
        _part("dev.foreign.gizmo", (0.0, 0.0, 0.0)),
    )
    findings = check_scene_placement(clean_contract, scene)
    unclaimed = [f for f in findings if f.quantity == "scene/dev.foreign.gizmo/unclaimed"]
    assert len(unclaimed) == 1
    assert unclaimed[0].severity == "not_scored"
    assert unclaimed[0].observed == "the model has a part the contract doesn't name"


def test_check_scene_placement_scores_records_pair_up(clean_contract):
    """Same presence-floor identity as every other check, scoped to this check's own book."""
    scene = _scene(
        _part("dev.imu", (0.1, -0.04, 0.02)),
        _part("dev.radio.rfd900", (-0.2, 0.08, 0.05)),
    )
    scores = _ScoreBook()
    findings = check_scene_placement(clean_contract, scene, scores=scores)
    violations = sum(f.severity == "violation" for f in findings)
    warnings = sum(f.severity == "warning" for f in findings)
    accounted = len(scores.records) + violations + warnings
    assert len(scores) == accounted


def test_checks_quotes_why_on_crossed_bound():
    """`lockwire check`/`preflight`'s refusal quotes the declared `why`
    back verbatim, not just `describe`'s reference sheet."""
    contract = load_contract(Path(__file__).parent / "fixtures" / "contract_clean.yaml")
    device = contract.device("dev.imu")
    model = contract.device_model(device.model)
    model.hard_limits["accel_z"] = Band(
        lo=-1.0, hi=1.0, unit="m/s^2", why="the die desolders past this"
    )

    hits = [
        f
        for f in check_envelopes_vs_hard_limits(contract)
        if f.severity == "violation" and f.quantity.endswith("/accel_z")
    ]
    assert hits
    assert "(why: the die desolders past this)" in hits[0].expected


def test_check_mechanisms_is_wired_into_run_record_checks_and_is_silent_on_a_tree() -> None:
    """The registry runs it, and an ordinary robot (no closed loop) pays nothing for it.

    The four-bar verdicts themselves are proved in ``tests/test_mechanism.py`` against
    literal Grashof sums; this is the wiring and the false-positive budget.
    """
    from lockwire.checks import check_mechanisms

    clean = load_contract(Path("tests/fixtures/contract_clean.yaml"))
    assert check_mechanisms(clean) == []
    assert [f for f in run_record_checks(clean) if f.quantity.startswith("model/mechanisms/")] == []

    locking = load_contract(Path("tests/fixtures/fourbar_locking/lockwire.yaml"))
    direct = check_mechanisms(locking)
    registered = [
        f for f in run_record_checks(locking) if f.quantity.startswith("model/mechanisms/")
    ]
    assert len(direct) == 1 and direct[0].severity == "violation"
    assert registered == direct


# ------------------------------------------- max_rate at check time


def test_checks_max_rate_scored_or_documented_as_na(clean_contract):
    """`Band.max_rate` is a rate of change; `check` compares one contract
    against itself and never sees two timed samples, so it cannot compute one.

    That is a decision, not an omission, and it is said out loud: every declared
    `max_rate` reaching a contract check comes back `not_scored`, naming the
    missing operand and where the bound *is* scored (`lockwire plan`, which
    compares consecutive published values). A silent pass here is how a rate
    limit stops biting. It is also not counted as a comparison — nothing was
    compared, so the scored denominator must not move."""
    model = clean_contract.device_models[0]
    channel = next(iter(model.hard_limits), None) or "accel_z"
    limit = model.hard_limits.get(channel) or Band(lo=-50.0, hi=50.0, unit="m/s^2")
    before = count_scored_comparisons(clean_contract)

    model.hard_limits[channel] = limit.model_copy(update={"max_rate": 1.5})
    devices = [d for d in clean_contract.devices if d.model == model.id]
    assert devices, "fixture must have a device on this model for the check to reach it"

    findings = run_record_checks(clean_contract)
    for device in devices:
        finding = next(
            f
            for f in findings
            if f.quantity == f"devices/{device.id}/hard_limits/{channel}/max_rate"
        )
        assert finding.severity == "not_scored"
        assert "1.5" in finding.observed
        assert "lockwire plan" in finding.action
    assert count_scored_comparisons(clean_contract) == before


# ---------------------------------------------------------------- compared-from


def _copied_catalog_mass_contract(*, mass_g: float, compared_from: str) -> Contract:
    return Contract(
        robot=RobotInfo(name="copied-catalog-mass"),
        devices=[
            DeviceInstance(
                id="dev.flight-controller",
                model="mdl.flight-controller",
                owner="test",
                physical={"mass": {"value": mass_g, "unit": "g"}},
                provenance=Provenance(
                    witness="human",
                    source="catalog comparison witness",
                    locked_at="2026-09-27",
                    compared_from=compared_from,
                ),
            )
        ],
    )


def test_compared_from_refuses_unknown_and_family_references_without_a_mass_guess():
    for ref in ("catalog.part.unknown", "fam.flight-controller.autopilot"):
        findings = check_compared_from(_copied_catalog_mass_contract(mass_g=50, compared_from=ref))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity == "warning"
        assert ref in finding.observed
        assert "catalog part id" in finding.expected
        assert "family references are not resolved" in finding.action


def test_compared_from_match_is_not_a_new_warning():
    findings = check_compared_from(
        _copied_catalog_mass_contract(
            mass_g=59.3, compared_from="x500.flight-controller.pixhawk-6c"
        )
    )
    assert findings == []


def test_torque_constraint_row_uses_the_checks_own_numbers(clean_contract):
    """The constraint's used/limit/status match check_torque_budgets' finding."""
    from lockwire.constraints import constraint_rows

    clean_contract.model.joints[0].limits.effort = 4.0
    joint = clean_contract.model.joints[0].name
    row = next(r for r in constraint_rows(clean_contract, []) if r["id"] == f"joint-torque.{joint}")
    assert (row["used"], row["limit"], row["status"]) == (4.0, 3.0, "bad")  # same 3.00 vs 4.00
    assert (row["used_kind"], row["limit_kind"]) == ("declared", "derived")
    clean_contract.model.joints[0].limits.effort = 1.0
    row = next(r for r in constraint_rows(clean_contract, []) if r["id"] == f"joint-torque.{joint}")
    assert row["status"] == "ok"


def test_zero_mass_action_names_the_declare_command(testbot_urdf):
    contract = Contract(robot=RobotInfo(name="testbot"), model=parse_urdf(testbot_urdf))
    [finding] = check_zero_mass(contract, contract_path=Path("my.yaml"))
    assert (  # bites a warning that names no runnable fix
        "lockwire declare my.yaml model/links/sensor_mount/mass" in finding.action
    )


def _actuator(c):
    return c.device_model(next(d for d in c.devices if d.drives_joint).model).actuator


def test_peak_only_actuator_below_effort_limit_is_flagged(clean_contract):
    """A servo declared with only its stall torque can still never reach the limit."""
    from lockwire.checks import check_torque_budgets

    a = _actuator(clean_contract)
    a.torque_continuous = None
    a.torque_peak = 1.0
    clean_contract.model.joints[0].limits.effort = a.gear_ratio * 1.0 + 4.0
    [f] = check_torque_budgets(clean_contract)
    assert f.severity == "violation"  # bites ignoring torque_peak (GO on a peak-only servo)
    assert "peak (stall) geared torque" in f.observed
    assert "joint effort limit" in f.expected


def test_peak_only_actuator_above_effort_limit_passes(clean_contract):
    from lockwire.checks import check_torque_budgets

    a = _actuator(clean_contract)
    a.torque_continuous = None
    a.torque_peak = 1.0
    clean_contract.model.joints[0].limits.effort = a.gear_ratio * 1.0 - 0.5
    assert check_torque_budgets(clean_contract) == []  # bites flagging when peak covers the limit


def test_continuous_failure_is_not_double_reported_with_peak(clean_contract):
    from lockwire.checks import check_torque_budgets

    a = _actuator(clean_contract)
    a.torque_peak = a.torque_continuous * 1.5
    clean_contract.model.joints[0].limits.effort = a.torque_continuous * a.gear_ratio + 1.0
    [f] = check_torque_budgets(clean_contract)  # bites a second finding for the same joint
    assert "continuous" in f.observed


def _reach_contract(clean_contract, effort):
    from tests.test_statics import arm

    clean_contract.model = arm(shoulder_effort=effort)
    return clean_contract


def test_gravity_reach_fires_below_and_is_silent_above(clean_contract):
    from lockwire.checks import check_gravity_reach

    (f,) = check_gravity_reach(_reach_contract(clean_contract, 20.0))
    assert f.severity == "violation" and f.quantity == "model/joints/shoulder/limits/effort"
    assert float(f.observed.split()[0]) == pytest.approx(24.525, abs=0.01)
    assert "shoulder=0deg" in f.observed
    assert check_gravity_reach(_reach_contract(clean_contract, 30.0)) == []


_FOURBAR = "fixtures/fourbar_world/lockwire.yaml"


@pytest.mark.parametrize(
    "kw",
    [
        {"limits": {"lower": -1.2, "upper": 1.2}},  # 9.143 N*m at the limit
        {"limits": {"lower": 0.2, "upper": 1.5}},  # 9.785 N*m at the limit
    ],
)
def test_gravity_reach_samples_joint_limit_endpoints(clean_contract, kw):
    """Limit endpoints are sampled: 2 kg, CoM 0.5 m up, 9 N*m effort; the grid alone misses both."""
    from lockwire.checks import check_gravity_reach
    from tests.test_statics import chain1

    clean_contract.model = chain1(**kw)
    (f,) = check_gravity_reach(clean_contract)
    assert f.severity == "violation"


def _mutate(i, **kw):
    return lambda m: m.joints.__setitem__(i, m.joints[i].model_copy(update=kw))


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (_mutate(0, axis=(0, 0, 0)), "'j'"),  # in domain, zero axis
        (_mutate(0, axis=None), "no axis"),  # in domain, hand-written contract without axis
        (lambda m: m.links.__setitem__(1, m.links[1].model_copy(update={"com": None})), "'l'"),
    ],
)
def test_gravity_reach_not_scored_inside_its_domain(clean_contract, mutate, why):
    """A model it tried to score and could not says why, in one finding."""
    from lockwire.checks import check_gravity_reach
    from tests.test_statics import chain1

    clean_contract.model = chain1()
    mutate(clean_contract.model)
    (f,) = check_gravity_reach(clean_contract)
    assert f.severity == "not_scored" and why in f.observed


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate(0, type="fixed"),  # no rotary joint
        lambda m: m.links.__setitem__(1, m.links[1].model_copy(update={"mass": None})),  # no mass
        lambda m: m.links.append(m.links[0].model_copy(update={"name": "orphan"})),  # two roots
    ],
)
def test_gravity_reach_silent_outside_its_domain(clean_contract, mutate):
    """Trees, prismatic/floating joints and long chains are in the domain; silent only with
    no rotary joint, no link mass or no single root."""
    from lockwire.checks import check_gravity_reach
    from tests.test_statics import chain1

    clean_contract.model = chain1()
    mutate(clean_contract.model)
    assert check_gravity_reach(clean_contract) == []


def test_gravity_reach_scores_trees_and_holds_prismatic_at_zero(clean_contract):
    """The arm starter must score with its gripper (before trees, every starter was silent). A gripper tree scores the shoulder with the fingers (44.15 N*m by hand); a slide
    under a joint is held at zero and named, and the joint still scores (9.81 N*m)."""
    from lockwire.checks import check_gravity_reach
    from lockwire.schema import Joint, Link
    from tests.test_statics import chain1, gripper_arm

    clean_contract.model = gripper_arm()
    clean_contract.model.joints[0].limits.effort = 40.0
    (f,) = check_gravity_reach(clean_contract)
    assert f.quantity == "model/joints/shoulder/limits/effort"
    assert float(f.observed.split()[0]) == pytest.approx(44.145, abs=0.01)
    m = clean_contract.model = chain1(com=(0.5, 0, 0))
    m.links.append(Link(name="mid"))
    m.joints = [m.joints[0].model_copy(update={"parent": "mid"}),
                Joint(name="slide", type="prismatic", parent="base", child="mid", axis=(0, 0, 1))]  # fmt: skip
    (f,) = check_gravity_reach(clean_contract)
    assert "9.81 N*m" in f.observed and "slide held at zero" in f.observed


def test_gravity_reach_holds_a_slide_along_the_lever_at_zero(clean_contract):
    """A 2 kg link slides along x below a y-axis joint, CoM 0.3 m out, slide limits 0 to 0.5 m.
    Held at zero the lever is 0.3 m: 2 * 9.81 * 0.3 = 5.886 N*m. Held at its 0.5 m limit it would
    be 2 * 9.81 * 0.8 = 15.696 N*m and break the 9 N*m effort limit."""
    from lockwire.checks import check_gravity_reach
    from lockwire.schema import Joint, JointLimits, Link
    from tests.test_statics import chain1

    m = clean_contract.model = chain1(com=(0.3, 0, 0))
    m.links = [m.links[0], Link(name="mid"), m.links[1]]
    m.joints = [m.joints[0].model_copy(update={"child": "mid"}),
                Joint(name="slide", type="prismatic", parent="mid", child="l", axis=(1, 0, 0),
                      limits=JointLimits(lower=0.0, upper=0.5))]  # fmt: skip
    scores = _ScoreBook()
    assert check_gravity_reach(clean_contract, scores=scores) == []
    (record,) = scores.records
    assert record.observed == "5.89 N*m" and record.margin == "3.11 N*m headroom"


def test_gravity_reach_passed_entry_is_one_line_and_detail_keeps_the_pose(clean_contract):
    """The Passed list prints one line per joint; the worst pose stays in the record's detail."""
    from lockwire.checks import check_gravity_reach
    from lockwire.page import _scored_title
    from tests.test_statics import chain1

    clean_contract.model = chain1(kg=1.0)  # 1 * 9.81 * 0.5 = 4.905 N*m of 9
    scores = _ScoreBook()
    check_gravity_reach(clean_contract, scores=scores)
    (record,) = scores.records
    assert _scored_title(clean_contract, record) == (
        "j: 4.91 N*m of 9.00 N*m joint effort limit — 4.09 N*m headroom"
    )
    assert "worst of a 0/+-90 deg and limit grid" in record.detail


def test_gravity_reach_scores_each_hip_over_its_own_leg(clean_contract):
    """A quadruped scores each hip (12 rotary joints were silent before): a body with four
    3-joint legs; each hip's grid is its own leg only (3^3 poses, not 3^12). Leg: 1 kg links,
    CoM 0.1 m out, joints 0.1 m apart: 9.81 * (0.1 + 0.2 + 0.3) = 5.886 N*m > 5 N*m."""
    from lockwire.checks import check_gravity_reach
    from lockwire.schema import Joint, JointLimits, Link
    from tests.test_statics import chain1

    links, joints = [Link(name="body")], []
    for leg in range(4):
        for k in range(3):
            child = f"l{leg}{k}"
            links.append(
                Link(name=child, mass={"value": 1, "unit": "kg"}, com=(0.1, 0, 0), frame=child)
            )
            joints.append(Joint(name=f"j{leg}{k}", type="revolute", axis=(0, 1, 0), child=child,
                                  parent=f"l{leg}{k - 1}" if k else "body", origin={"xyz": (0.1 * bool(k), 0, 0)},
                                  limits=JointLimits(effort=5.0 if k == 0 else 100.0)))  # fmt: skip
    clean_contract.model = chain1().model_copy(update={"links": links, "joints": joints})
    found = check_gravity_reach(clean_contract)
    assert [f.quantity.split("/")[2] for f in found] == ["j00", "j10", "j20", "j30"]
    for leg, f in enumerate(found):
        assert float(f.observed.split()[0]) == pytest.approx(5.886, abs=0.01)
        assert f.observed.count("=") == 3 and f"j{leg}2=" in f.observed  # its own leg only


def test_gravity_reach_payload_tips_a_limit_the_unloaded_arm_holds(clean_contract):
    """Payload at the tool tip counts (it used to be 0). Elbow limit 10 N*m: unloaded 9.81 * 1 * 0.5 = 4.905 holds;
    1 kg at the tool origin (1 m past the elbow) adds 9.81: 14.715 N*m, a violation."""
    from lockwire.checks import check_gravity_reach
    from lockwire.schema import Link
    from tests.test_statics import arm

    clean_contract.model = arm(elbow_effort=10.0)
    assert check_gravity_reach(clean_contract) == []
    clean_contract.model.links[3] = Link(name="tool", payload={"value": 1, "unit": "kg"})
    (f,) = check_gravity_reach(clean_contract)
    assert f.quantity == "model/joints/elbow/limits/effort" and "with 1 kg payload" in f.observed
    assert float(f.observed.split()[0]) == pytest.approx(14.715, abs=0.01)


def test_check_on_closed_chain_returns():
    """`lockwire check` ends and names the loop; the cap keeps the slot unhung."""
    import subprocess
    import sys
    from pathlib import Path

    fx = str(Path(__file__).parent / _FOURBAR)
    r = subprocess.run(
        [sys.executable, "-m", "lockwire", "check", fx], capture_output=True, text=True, timeout=30
    )
    assert r.returncode in {0, 1, 3} and "closed chain" in r.stdout + r.stderr


# ------------------------------------------------------------ batch 2 review fixes (2026-10-20)


def test_mass_total_partial_sum_is_not_scored_naming_unknowns(clean_contract):
    """3 devices with no mass must not let the partial sum pass as 'headroom'."""
    scores = _ScoreBook()
    findings = check_budgets(clean_contract, scores=scores)
    gap = next(f for f in findings if f.quantity == "budgets/bud.mass.devices")
    assert gap.severity == "not_scored" and "dev.barometer" in gap.observed
    assert "budgets/bud.mass.devices" not in scores.keys


def test_mass_total_known_masses_over_limit_is_one_counted_violation(clean_contract):
    """Known masses over the limit are ONE counted violation naming the unknowns."""
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.limit = Quantity(value=0.0001, unit="kg")
    scores = _ScoreBook()
    found = [
        f
        for f in check_budgets(clean_contract, scores=scores)
        if f.quantity == "budgets/bud.mass.devices"
    ]
    assert [f.severity for f in found] == ["violation"]
    assert "dev.barometer" in found[0].observed
    assert "budgets/bud.mass.devices" in scores.keys


def test_mass_total_link_without_mass_is_not_scored(clean_contract):
    """A link with no mass is an unknown too, named like a device."""
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.sources = "links"
    budget.limit = Quantity(value=10.0, unit="kg")
    clean_contract.model.links[1].mass = None
    gap = next(f for f in check_budgets(clean_contract) if f.quantity == "budgets/bud.mass.devices")
    assert gap.severity == "not_scored" and "imu_link" in gap.observed


def test_rail_current_scores_the_worst_declared_mode(clean_contract):
    """a 25 W peak on a 5 V / 1 A rail is 5 A, not the 0.48 A nominal."""
    clean_contract.rail("rail.5v").current_limit = Quantity(value=1.0, unit="A")
    radio = clean_contract.device("dev.radio.rfd900")
    radio.power.modes = [PowerMode(name="peak", draw=Quantity(value=25.0, unit="W"))]
    found = next(f for f in check_rail_current(clean_contract) if f.severity == "violation")
    assert "peak" in found.observed


def _imu_on_bus(c, bus_id, rate, payload):
    imu = c.device("dev.imu")
    imu.identity.bus_ref = bus_id
    imu.driver.nominal_rate_hz, imu.driver.payload_bytes = rate, payload


def test_bus_bandwidth_device_without_traffic_declaration_is_not_scored(clean_contract):
    """the radio declares no driver rate; it must be named, not dropped as 'passed'."""
    _imu_on_bus(clean_contract, "bus.serial0", 10, 64)
    scores = _ScoreBook()
    findings = check_bus_bandwidth(clean_contract, scores=scores)
    gap = next(f for f in findings if f.quantity == "topology/buses/bus.serial0/bandwidth")
    assert gap.severity == "not_scored" and "dev.radio.rfd900" in gap.observed
    assert "topology/buses/bus.serial0/bandwidth" not in scores.keys


def test_can_bus_ceiling_counts_framing_overhead(clean_contract):
    """5000 Hz x 8 B is 320 kbit/s payload (under 60% of 1 Mbit) but ~675 kbit/s on the wire."""
    bus = Bus(id="bus.can0", kind="can", host="nav-computer", bitrate=1_000_000)
    clean_contract.topology.buses.append(bus)
    _imu_on_bus(clean_contract, bus.id, 5000, 8)
    findings = check_bus_bandwidth(clean_contract)
    assert [f.severity for f in findings if f.quantity.endswith(f"{bus.id}/bandwidth")] == [
        "violation"
    ]


def test_voltage_compat_warns_on_low_side_tolerance(clean_contract):
    """5 V +/-10% is 4.5 V worst case, under a 4.75 V minimum."""
    device = clean_contract.device("dev.imu")
    rail = clean_contract.rail(device.power.rail)
    rail.voltage, rail.voltage_range, rail.output_tolerance_pct = (
        Quantity(value=5.0, unit="V"),
        None,
        10.0,
    )
    clean_contract.device_model(device.model).input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.75, unit="V"), max=Quantity(value=5.6, unit="V")
    )
    (f,) = check_voltage_compatibility(clean_contract)
    assert f.severity == "warning" and "4.5" in f.observed and f.delta.startswith("-")


def test_scene_placement_rotates_into_the_mount_frame(clean_contract):
    """imu_link is yawed 90 deg; a point 0.05 m along its x sits 0.05 m along world y."""
    joint = clean_contract.model.joints[0]
    joint.type = "fixed"
    joint.origin.xyz, joint.origin.rpy = (0.0, 0.0, 0.1), (0.0, 0.0, pi / 2)
    imu = clean_contract.device("dev.imu")
    imu.physical.mount_frame = "imu_link"
    imu.physical.mount_xyz = tuple(Quantity(value=v, unit="m") for v in (0.05, 0.0, 0.0))
    scene = _scene(_part("imu_link", (0.0, 0.0, 0.1)), _part("dev.imu", (0.0, 0.05, 0.1)))
    findings = check_scene_placement(clean_contract, scene)
    assert not any(f.quantity == "devices/dev.imu/placement" for f in findings)


def test_massless_tool0_frame_is_not_a_tool_link(clean_contract):
    """Tool0/tcp are frames on UR/ABB arms; a gripper word still warns."""
    for name in ("tool0", "tcp_frame"):
        _add_tool_leaf(clean_contract, name, None)
    found = [f for f in run_record_checks(clean_contract) if "tool-like" in f.observed]
    assert found == []


def test_rail_current_nonpositive_voltage_is_not_scored(clean_contract):
    """0 V crashed with ZeroDivisionError; negative was skipped silently."""
    rail = clean_contract.rail("rail.5v")
    rail.current_limit = Quantity(value=1.0, unit="A")
    for volts in (0.0, -5.0):
        rail.voltage, rail.voltage_range = Quantity(value=volts, unit="V"), None
        (f,) = [
            f for f in check_rail_current(clean_contract) if f.quantity.endswith("current_limit")
        ]
        assert f.severity == "not_scored" and "voltage" in f.observed


def test_output_tolerance_that_looks_like_a_fraction_warns(clean_contract):
    """0.05 meant as 5% passes the schema; the check names the field."""
    device = clean_contract.device("dev.imu")
    clean_contract.rail(device.power.rail).output_tolerance_pct = 0.05
    clean_contract.device_model(device.model).input_voltage_range = InputVoltageRange(
        min=Quantity(value=4.0, unit="V"), max=Quantity(value=6.0, unit="V")
    )
    found = [f for f in check_voltage_compatibility(clean_contract) if f.severity == "warning"]
    assert any("output_tolerance_pct" in f.quantity for f in found)


def _lock_with_unset_axes(tmp_path, joint_type, axis_xml=""):
    """A URDF with one joint, locked the way older versions wrote it (no axis)."""
    from lockwire.schema import _model_structure_hash

    urdf = tmp_path / "robot.urdf"
    urdf.write_text(
        '<robot name="r"><link name="a"/><link name="b"/>'
        f'<joint name="j" type="{joint_type}"><parent link="a"/><child link="b"/>'
        '<limit lower="-1" upper="1" effort="1" velocity="1"/></joint></robot>'
    )
    model = parse_urdf(urdf)
    for joint in model.joints:
        joint.axis = None
    model.structure_hash = _model_structure_hash(model.links, model.joints)
    if axis_xml:
        urdf.write_text(urdf.read_text().replace("<limit", f"{axis_xml}<limit"))
    return Contract(robot=RobotInfo(name="r"), model=model)


@pytest.mark.parametrize("joint_type", ["revolute", "fixed"])
def test_old_lock_with_no_axis_reads_unchanged(tmp_path, joint_type):
    """A lock written before axes were filled in still reads as an unchanged URDF."""
    assert check_urdf_drift(_lock_with_unset_axes(tmp_path, joint_type)) == []


def test_real_axis_change_still_reported(tmp_path):
    """Turning a revolute joint to a different axis is still a moved joint."""
    contract = _lock_with_unset_axes(tmp_path, "revolute", '<axis xyz="0 0 1"/>')
    findings = check_urdf_drift(contract)
    assert [f.severity for f in findings] == ["violation"]
    assert "axis changed" in findings[0].observed
