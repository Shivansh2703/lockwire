import json
from pathlib import Path

import pytest

from ruamel.yaml import YAML
from typer.testing import CliRunner

import lockwire.start_operation as ops
from lockwire.cli import app
from lockwire.copy import human_name
from lockwire.plan import load_plan, validate_plan
from lockwire.scene import load_scene
from lockwire.schema import load_contract

runner = CliRunner()
EXAMPLES = Path(__file__).parents[1] / "src" / "lockwire" / "examples"


def _sources_present(node, path="") -> list[str]:
    """Paths of provenance blocks without a source (the quantities they cover are unsourced)."""
    bad = []
    if isinstance(node, dict):
        if "provenance" in node and not str((node["provenance"] or {}).get("source", "")).strip():
            bad.append(path)
        for k, v in node.items():
            bad += _sources_present(v, f"{path}/{k}")
    elif isinstance(node, list):
        for k, v in enumerate(node):
            bad += _sources_present(v, f"{path}[{k}]")
    return bad


def test_rover_plan_loads_and_scores_against_the_contract():
    from lockwire.plan import load_plan, validate_plan
    from lockwire.schema import load_contract

    folder = EXAMPLES / "rover"
    plan = load_plan(folder / "plan.yaml")
    assert len(plan.steps) == 2
    assert validate_plan(load_contract(folder / "contract.yaml"), plan).scored == 2


def test_rover_every_entry_is_sourced():
    folder = EXAMPLES / "rover"
    doc = YAML(typ="safe").load((folder / "contract.yaml").read_text())
    assert _sources_present(doc) == []
    # entries that carry numbers must carry provenance (the battery is sourced by comment)
    for section in (doc["device_models"], doc["devices"], doc["topology"]["rails"], doc["budgets"]):
        assert all(row.get("provenance") for row in section)


def test_rover_scene_and_card():
    folder = EXAMPLES / "rover"
    glb = folder / "scene.glb"
    assert glb.stat().st_size <= 2_000_000
    assert "dev.left-wheel-motor" in load_scene(glb).nodes_with_ids
    card = json.loads((folder / "starter.json").read_text())
    assert set(card) == {"title", "based_on", "licence", "licence_url", "source_repo"}
    assert (folder / "NOTICE").is_file()


def test_quadruped_names_are_human_and_example_plan_is_scored():
    from lockwire.copy import human_name
    from lockwire.plan import load_plan, validate_plan
    from lockwire.schema import load_contract

    folder = EXAMPLES / "quadruped"
    contract = load_contract(folder / "contract.yaml")
    assert {human_name(d.id) for d in contract.devices} >= {
        "Motor Front Left Knee",
        "Motor Hind Right Hip Abduction",
    }
    assert human_name(contract.topology.rails[0].id) == "Bench Supply 24 V"
    report = validate_plan(contract, load_plan(folder / "plan.yaml"))
    assert [o.status for o in report.outcomes] == ["PASS"]  # a jump fits the 30 A supply


def test_quadruped_every_block_carries_a_provenance_source():
    doc = YAML(typ="safe").load(EXAMPLES / "quadruped/contract.yaml")
    assert _sources_present(doc) == []


def test_quadruped_scene_loads_small_and_matches_the_contract_and_starter_json():
    folder = EXAMPLES / "quadruped"
    assert (folder / "scene.glb").stat().st_size <= 2_000_000
    scene = load_scene(folder / "scene.glb")
    devices = {d["id"] for d in YAML(typ="safe").load(folder / "contract.yaml")["devices"]}
    assert devices <= set(scene.nodes_with_ids)
    meta = json.loads((folder / "starter.json").read_text())
    assert {"title", "based_on", "licence", "licence_url", "source_repo"} <= {
        k for k, v in meta.items() if v
    }
    assert (folder / "NOTICE").is_file()


def test_arm_example_plan_is_scored_against_the_starter():
    from lockwire.plan import load_plan, validate_plan
    from lockwire.schema import load_contract

    folder = ops.EXAMPLES / "arm"
    plan = load_plan(folder / "plan.yaml")
    findings = validate_plan(load_contract(folder / "contract.yaml"), plan)
    assert len(plan.steps) == 2 and list(findings) == []


def test_arm_starter_has_no_placeholder_mass_and_human_ids():
    doc = YAML(typ="safe").load(ops.EXAMPLES / "arm" / "contract.yaml")
    assert all(k["mass"]["value"] > 0.001 for k in doc["model"]["links"] if "mass" in k)
    from lockwire.copy import human_name

    assert human_name(doc["topology"]["rails"][0]["id"]) == "Supply 12 V"
    assert human_name("dev.shoulder-pan-servo") == "Shoulder Pan Servo"


def test_arm_starter_every_number_has_a_source():
    folder = ops.EXAMPLES / "arm"
    doc = YAML(typ="safe").load(folder / "contract.yaml")
    rows = [*doc["device_models"], *doc["devices"], *doc["topology"]["rails"], *doc["budgets"]]
    assert rows and all(str(r["provenance"]["source"]).strip() for r in rows)


def test_arm_starter_scene_and_card():
    folder = ops.EXAMPLES / "arm"
    scene = load_scene(folder / "scene.glb")
    assert (folder / "scene.glb").stat().st_size <= 2_000_000
    assert "dev.gripper-servo" in scene.nodes_with_ids
    card = json.loads((folder / "starter.json").read_text())
    assert {"title", "based_on", "licence", "licence_url", "source_repo"} <= card.keys()
    assert (folder / "LICENSE").is_file() and Path(folder / "NOTICE").is_file()


def _load(path):
    return YAML(typ="safe").load(path.read_text())


def _quantities(node):
    """Every {value, unit} number under node."""
    if isinstance(node, dict):
        if "value" in node and "unit" in node:
            yield node
        for v in node.values():
            yield from _quantities(v)
    elif isinstance(node, list):
        for v in node:
            yield from _quantities(v)


def _humanoid_assert_every_number_sourced(contract: dict) -> None:
    for section in ("device_models", "devices", "budgets", "buses", "rails"):
        items = contract.get(section) or contract.get("topology", {}).get(section) or []
        for item in items:
            if list(_quantities(item)):
                assert (item.get("provenance") or {}).get("source"), (
                    f"{section}/{item['id']}: number without a source"
                )
    battery = contract.get("topology", {}).get("battery")
    if battery:  # Battery has no provenance field; the rail it feeds names its pack and capacity.
        feeds = [r for r in contract["topology"]["rails"] if r.get("upstream") == "battery"]
        assert feeds and all(
            str(battery["capacity"]["value"]) in r["provenance"]["note"] for r in feeds
        )


def test_humanoid_every_number_is_sourced():
    _humanoid_assert_every_number_sourced(_load(EXAMPLES / "humanoid/contract.yaml"))


def test_humanoid_scene_loads_small_and_joins_the_contract():
    folder = EXAMPLES / "humanoid"
    scene = folder / "scene.glb"
    assert scene.stat().st_size <= 2_000_000
    ids = {d["id"] for d in _load(folder / "contract.yaml")["devices"]}
    joined = set(load_scene(scene).nodes_with_ids)
    assert joined and joined <= ids
    meta = json.loads((folder / "starter.json").read_text())
    assert {"title", "based_on", "licence", "licence_url", "source_repo"} <= meta.keys()
    assert (folder / "NOTICE").is_file()


def test_drone_starter_names_scene_ids_and_plan(tmp_path):
    import re

    from lockwire.copy import human_name
    from lockwire.plan import load_plan, validate_plan
    from lockwire.schema import load_contract

    folder = EXAMPLES / "drone"
    contract = load_contract(folder / "contract.yaml")
    ids = [d.id for d in contract.devices] + [r.id for r in contract.topology.rails]
    assert human_name("dev.esc.esc-1") == "ESC 1" and human_name("rail.supply-5v") == "Supply 5 V"
    assert not [i for i in ids if re.search(r"\.m\d$|esc\d$|servo-motor|here3|rfd900", i)]
    assert set(ids) <= set(load_scene(folder / "scene.glb").nodes_with_ids)
    assert list(validate_plan(contract, load_plan(folder / "plan.yaml"))) == []  # both steps fit


def test_drone_flight_controller_carries_the_telem1_connector_from_the_pin_card():
    from lockwire.catalog import load_pinouts

    c = load_contract(EXAMPLES / "drone" / "contract.yaml")
    fc = next(d for d in c.devices if d.id == "dev.flight-controller.pixhawk-6c")
    telem = next(x for x in fc.connectors or [] if x.id == "TELEM1")
    board = next(b for b in load_pinouts() if b.id == "holybro-pixhawk-6c")
    card = next(x for x in board.connectors if x.id == "TELEM1")
    assert [(p.number, p.name) for p in telem.pins] == [(p.number, p.name) for p in card.pins]
    assert telem.type in {t.id for t in c.connector_types or []}


def test_humanoid_reads_as_a_robot_with_plain_names():
    folder = EXAMPLES / "humanoid"
    c = _load(folder / "contract.yaml")
    links = {k["name"]: k for k in c["model"]["links"]}
    parts = load_scene(folder / "scene.glb").parts
    assert {p.node for p in parts} <= set(links) and len(
        parts
    ) >= 40  # one part per drawn link, named as the link
    ids = [x["id"] for x in c["devices"] + c["topology"]["rails"] + c["budgets"]]
    assert "Legs 12 V" == human_name("rail.legs-12v") and "Left Knee" in human_name(
        "dev.servo.left-knee"
    )
    assert not [i for i in ids if i.endswith(("-l", "-r", "-a", "-b")) or "motor-12v" in i]


def test_humanoid_plan_is_accepted_against_the_contract():
    folder = EXAMPLES / "humanoid"
    report = validate_plan(load_contract(folder / "contract.yaml"), load_plan(folder / "plan.yaml"))
    assert [o.status for o in report.outcomes] == ["PASS", "PASS"] and not report.errors


@pytest.mark.parametrize("name", ["arm", "drone", "humanoid", "quadruped", "rover"])
def test_every_starter_opens_go_with_no_violation(name, tmp_path, monkeypatch):
    """A bundled starter is a clean starting point; known gaps are accepted, not hidden."""
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["start", "--example", name]).exit_code == 0
    checked = runner.invoke(app, ["check", "lockwire.yaml", "--json", "out.json"])
    doc = json.loads((tmp_path / "out.json").read_text())
    assert not [f["quantity"] for f in doc["findings"] if f["severity"] == "violation"]
    assert checked.exit_code == 0, checked.output
    # every accepted gap still matches its finding (shown as accepted, never silently stale)
    rows = _load(tmp_path / "lockwire.yaml").get("accepted") or []
    assert len(doc.get("accepted") or []) == len(rows)


def test_servo_bench_arm_fails_the_servo_check_on_the_wrist_only():
    from lockwire.checks import check_servo_stall

    c = load_contract(EXAMPLES / "servo-bench.yaml")
    [f] = check_servo_stall(c, c.servo)
    assert f.quantity == "model/joints/wrist/limits/effort"
    assert "in this lockfile's model: joints wrist limits effort 0.4 → 0.226" in f.action
    assert "or find a stronger servo: lockwire swap lockwire.yaml wrist" in f.action
    assert f.expected.startswith("<= 0.226 N*m")  # same precision as the suggested limit


def test_servo_stall_command_names_the_lockfile_as_runnable_and_the_app_by_file_name(tmp_path):
    from pathlib import Path

    from lockwire.checks import check_servo_stall
    from lockwire.page import build_payload

    c = load_contract(EXAMPLES / "servo-bench.yaml")
    [f] = check_servo_stall(c, c.servo, contract_path=Path("sub/dir/lockwire.yaml"))
    assert "lockwire swap sub/dir/lockwire.yaml wrist" in f.action
    [g] = check_servo_stall(c, c.servo, contract_path=tmp_path / "lockwire.yaml")
    assert f"swap {tmp_path}/lockwire.yaml wrist" in g.action  # absolute outside cwd: runnable
    (tmp_path / "lockwire.yaml").write_bytes((EXAMPLES / "servo-bench.yaml").read_bytes())
    cards = build_payload(c, contract_path=tmp_path / "lockwire.yaml")["cards"]
    [card] = [x for x in cards if "stall" in x["raw"]]
    assert (
        card["fix"].endswith("lockwire swap lockwire.yaml wrist") and "joints wrist" in card["fix"]
    )


def test_start_example_copy_differs_from_the_shipped_file_by_the_version_line_only(tmp_path):
    for name in ops.example_names():
        out = Path(ops.start_example(name, tmp_path / f"{name}.yaml")["path"])
        folder = ops.EXAMPLES / name
        src = folder / "contract.yaml" if folder.is_dir() else ops.EXAMPLES / f"{name}.yaml"
        a, b = src.read_text().splitlines(), out.read_text().splitlines()
        assert len(a) == len(b), name
        assert [x for x, y in zip(a, b) if x != y] == [
            x for x in a if x.startswith("schema_version:")
        ], name


@pytest.mark.parametrize(
    ("rel", "exit_code"), [("servo-bench.yaml", 1), ("rover/contract.yaml", 0)]
)
def test_starter_verdict_pinned_with_wires_and_no_new_not_scored(rel, exit_code):
    c = load_contract(EXAMPLES / rel)
    assert c.topology.wires[0].length_m and c.topology.wires[0].awg
    result = runner.invoke(app, ["check", str(EXAMPLES / rel), "--all"])
    assert result.exit_code == exit_code
    assert f"{c.topology.wires[0].id}: " in result.output  # the wire reports its ampacity headroom
    assert (
        "bitrate unrated" not in result.output
    )  # a bus without a cited bitrate adds a NOT SCORED line


@pytest.mark.parametrize(
    ("rel", "model", "field", "value"),
    [
        ("servo-bench.yaml", "mdl.servo.mg996r", "stall_current_a", 1.4),
        ("arm/contract.yaml", "mdl.servo.sts3215-12v", "stall_current_a", 2.7),
        ("arm/contract.yaml", "mdl.servo.sts3215-12v", "no_load_current_a", 0.2),
    ],
)
def test_starter_servo_electrical_fields_are_the_cited_numbers(rel, model, field, value):
    c = load_contract(EXAMPLES / rel)
    m = next(m for m in c.device_models if m.id == model)
    assert getattr(m.actuator, field) == value


def _resized(tmp_path, **values):
    """A tmp copy of the rover-param lockfile with the named params set (text replace)."""
    import re

    text = (EXAMPLES / "rover-param" / "contract.yaml").read_text()
    for name, value in values.items():
        text, n = re.subn(rf"(name: {name}\n\s+value: )[-\d.]+", rf"\g<1>{value}", text)
        assert n == 1, name
    path = tmp_path / "rover.yaml"
    path.write_text(text)
    return path


def _check_json(path, tmp_path):
    out = tmp_path / "out.json"
    assert runner.invoke(app, ["check", str(path), "--json", str(out)]).exit_code == 0
    return json.loads(out.read_text())


def _tip_mm(path, tmp_path):
    import re

    [f] = [
        f for f in _check_json(path, tmp_path)["findings"] if f["quantity"] == "model/tip_margin"
    ]
    return int(re.search(r"is (\d+) mm inside", f["observed"]).group(1))


def test_rover_param_is_a_starter_stored_in_the_lockfile():
    assert "rover-param" in ops.example_names()
    assert any(s["name"] == "rover-param" for s in ops.starters())
    contract = load_contract(EXAMPLES / "rover-param" / "contract.yaml")
    assert contract.model.source == "lockfile" and len(contract.params) >= 4


def test_rover_param_wheelbase_300_to_400_mm_widens_the_tip_margin(tmp_path):
    assert (
        _tip_mm(_resized(tmp_path), tmp_path) == 150
    )  # track 450 mm: wheelbase 300 is the short side
    wide = _resized(tmp_path, half_wheelbase=0.2)
    assert _tip_mm(wide, tmp_path) == 200


def test_rover_param_every_link_mass_is_derived_with_its_cited_density(tmp_path):
    rows = _check_json(_resized(tmp_path), tmp_path)["forces"]["masses"]
    assert len(rows) == 5
    assert {r["kind"] for r in rows} == {"derived"}
    assert all("aalco.co.uk" in r["mass_note"] for r in rows)


def test_rover_param_chassis_mass_follows_the_volume_not_one_length(tmp_path):
    def chassis(**values):
        rows = _check_json(_resized(tmp_path, **values), tmp_path)["forces"]["masses"]
        return next(r["value"] for r in rows if r["id"] == "model/links/chassis/mass")

    base = chassis()
    assert chassis(chassis_length=0.6, chassis_width=0.45) == pytest.approx(base * 1.5 * 1.5)


def test_rover_param_materials_are_the_catalog_row():
    from lockwire import catalog

    links = load_contract(EXAMPLES / "rover-param" / "contract.yaml").model.links
    assert len(links) == 5
    assert all(link.material == catalog.material("Aluminium 6061-T6") for link in links)


def _hex_variant(tmp_path, **values):
    import re

    text = (EXAMPLES / "hexapod-param" / "contract.yaml").read_text()
    for name, value in values.items():
        text, n = re.subn(rf"(name: {name}\n\s+value: )[-\d.]+", rf"\g<1>{value}", text)
        assert n == 1, name
    path = tmp_path / "hexapod.yaml"
    path.write_text(text)
    return path


def test_hexapod_param_is_a_starter_with_six_legs_and_passes_check(tmp_path):
    assert "hexapod-param" in ops.example_names()
    assert any(s["name"] == "hexapod-param" for s in ops.starters())
    model = load_contract(EXAMPLES / "hexapod-param" / "contract.yaml").model
    assert model.source == "lockfile" and (len(model.links), len(model.joints)) == (19, 18)
    assert _check_json(_hex_variant(tmp_path), tmp_path)["exit_code"] == 0


def test_hexapod_param_every_link_mass_is_derived_and_servos_are_the_cited_part(tmp_path):
    from lockwire import catalog

    rows = _check_json(_hex_variant(tmp_path), tmp_path)["forces"]["masses"]
    assert len(rows) == 19 and {r["kind"] for r in rows} == {"derived"}
    assert all("aalco.co.uk" in r["mass_note"] for r in rows)
    c = load_contract(EXAMPLES / "hexapod-param" / "contract.yaml")
    part = next(p for p in catalog.load_catalog().parts if p.id == "feetech.st3215")
    act = c.device_models[0].actuator
    assert act.stall_current_a == part.numbers["stall_current"].value
    assert c.devices[0].physical.mass.value == part.numbers["mass"].value
    assert len(c.devices) == 18


def test_hexapod_param_leg_length_moves_the_mass_and_inertia(tmp_path):
    def forces(**values):
        return _check_json(_hex_variant(tmp_path, **values), tmp_path)["forces"]

    a, b = forces(), forces(tibia_length=0.2)
    assert b["mass_total"]["value"] > a["mass_total"]["value"]
    assert b["inertia"]["value"] != a["inertia"]["value"]


def test_templates_every_actuated_joint_has_the_catalog_servo_effort_and_range():
    from lockwire import catalog, units

    path = EXAMPLES / "hexapod-param" / "contract.yaml"
    c = load_contract(path)
    part = next(p for p in catalog.load_catalog().parts if p.id == "feetech.st3215")
    effort = units.to_base(part.numbers["stall_torque"].value, part.numbers["stall_torque"].unit)
    travel = units.to_base(
        part.numbers["rotation_range"].value, part.numbers["rotation_range"].unit
    )
    joints = {j.name: j for j in c.model.joints}
    driven = [joints[d.drives_joint] for d in c.devices]
    assert len(driven) == 18
    assert all(j.limits and j.limits.effort == effort for j in driven)
    assert all(j.limits.upper - j.limits.lower == pytest.approx(travel, rel=1e-6) for j in driven)
    assert (
        path.read_text().count("2.942 N*m = catalog feetech.st3215") == 18
    )  # the cited source, per joint
    rover = load_contract(EXAMPLES / "rover-param" / "contract.yaml")
    assert rover.devices == []  # wheels are continuous joints with no actuator row to cite


def test_hexapod_param_femur_length_moves_the_femur_gravity_torque(tmp_path):
    from lockwire import checks

    def femur(**values):
        loads = checks.gravity_loads(load_contract(_hex_variant(tmp_path, **values)))
        return loads["front-left-femur-servo"]

    assert femur(femur_length=0.16) > femur() > 0


# ---- arm-power: a small arm whose power, wiring and pin problems the 0.5.0 checks catch.

ARM_POWER = EXAMPLES / "arm-power"
ARM_BROWNOUT, ARM_FUSE, ARM_PIN = (
    "topology/rails/rail.5v/brownout",
    "topology/wires/w.pack/fuse",
    "topology/wires/w.bus-data",
)
# the single change per problem (old text, new text); each appears once in contract.yaml
ARM_FIXES = {
    ARM_BROWNOUT: ("awg: 28", "awg: 18"),  # w.servo-feed: 28 AWG -> 18 AWG
    ARM_FUSE: ("awg: 22", "awg: 18"),  # w.pack: 22 AWG -> 18 AWG
    ARM_PIN: ("pin: IO12", "pin: IO13"),  # w.bus-data: reserved strapping pin -> free pin
}


def _arm_variant(tmp_path, *fixes, text=None):
    text = text or (ARM_POWER / "contract.yaml").read_text()
    for old, new in fixes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path = tmp_path / "arm.yaml"
    path.write_text(text)
    return path


def _arm_check(path, tmp_path, *extra):
    out = tmp_path / "out.json"
    res = runner.invoke(app, ["check", str(path), "--json", str(out), *extra])
    return res.exit_code, json.loads(out.read_text())


def _arm_findings(path, tmp_path, *extra):
    return {f["quantity"]: f for f in _arm_check(path, tmp_path, *extra)[1]["findings"]}


def test_arm_power_is_a_starter_that_starts(tmp_path):
    assert "arm-power" in ops.example_names()
    assert any(s["name"] == "arm-power" for s in ops.starters())
    out = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "arm-power", "--out", str(out)]).exit_code == 0
    model = load_contract(out).model
    assert model.source == "lockfile" and (len(model.links), len(model.joints)) == (4, 3)
    assert (tmp_path / "lockwire.plan.yaml").is_file()


def test_arm_power_as_shipped_is_no_go_with_the_three_named_findings(tmp_path):
    code, doc = _arm_check(ARM_POWER / "contract.yaml", tmp_path)
    found = {f["quantity"]: f for f in doc["findings"]}
    assert code == 1 and doc["counts"]["violation"] == 2 and doc["counts"]["warning"] == 2
    assert (
        found["topology/wires/w.servo-feed/ampacity"]["severity"] == "warning"
    )  # AMP-THIN, sourced 28 AWG row
    assert {ARM_BROWNOUT, ARM_FUSE, ARM_PIN} <= set(found)
    assert found[ARM_BROWNOUT]["severity"] == found[ARM_FUSE]["severity"] == "violation"
    assert found[ARM_PIN]["severity"] == "warning"


def test_arm_power_findings_carry_their_number_limit_and_source(tmp_path):
    found = _arm_findings(ARM_POWER / "contract.yaml", tmp_path)
    brown, fuse, pin = found[ARM_BROWNOUT], found[ARM_FUSE], found[ARM_PIN]
    assert brown["observed_quantity"]["value"] == pytest.approx(4.65, abs=0.01)
    assert brown["expected_quantity"]["value"] == 4.75  # the Jetson's catalog minimum input
    assert (fuse["observed_quantity"]["value"], fuse["expected_quantity"]["value"]) == (9, 7)
    assert "powerstream.com/Wire_Size.htm" in fuse["source"]  # 22 AWG chassis column
    assert "IO12 is reserved" in pin["observed"] and "espressif.com" in pin["source"]


@pytest.mark.parametrize("quantity", sorted(ARM_FIXES))
def test_arm_power_each_documented_fix_removes_only_its_finding(quantity, tmp_path):
    found = _arm_findings(_arm_variant(tmp_path, ARM_FIXES[quantity]), tmp_path)
    assert quantity not in found
    assert {ARM_BROWNOUT, ARM_FUSE, ARM_PIN} - {quantity} <= set(found)


def test_arm_power_with_all_three_fixes_is_go(tmp_path):
    code, doc = _arm_check(_arm_variant(tmp_path, *ARM_FIXES.values()), tmp_path)
    assert (code, doc["verdict"]) == (0, "GO") and doc["counts"]["violation"] == 0


def test_arm_power_without_the_fuse_there_is_no_fuse_over_wire_finding(tmp_path):
    path = _arm_variant(tmp_path, ("    current_limit_enforced_by: fuse\n", ""))
    assert ARM_FUSE not in _arm_findings(path, tmp_path)


def test_arm_power_spec_numbers_are_the_catalog_rows(tmp_path):
    from lockwire import catalog

    parts = {p.id: p.numbers for p in catalog.load_catalog().parts}
    c = load_contract(ARM_POWER / "contract.yaml")
    xl, jet = parts["dynamixel.xl330-m288"], parts["nvidia.jetson-nano-devkit"]
    assert c.servo == "dynamixel.xl330-m288"
    assert c.device_models[0].actuator.stall_current_a == xl["stall_current"].value
    assert {j.limits.effort for j in c.model.joints if j.type == "revolute"} == {
        xl["stall_torque"].value
    }
    assert c.device("dev.servo.shoulder").physical.mass.value == xl["mass"].value
    assert c.device_models[1].input_voltage_range.min.value == jet["input_voltage_min"].value
    assert (
        c.rail("rail.battery").current_limit.value
        == parts["ptc.bourns.mf-r900"]["hold_current"].value
    )
    reg = parts["pololu.d36v50f5"]
    assert c.rail("rail.5v").min_input_v == reg["input_voltage_min"].value
    assert c.rail("rail.5v").efficiency == reg["efficiency_min"].value / 100
    assert c.rail("rail.5v").capacity.value == reg["output_current_max"].value * 5


def test_arm_power_every_link_mass_is_derived_from_the_catalog_material(tmp_path):
    from lockwire import catalog

    rows = _check_json_any(ARM_POWER / "contract.yaml", tmp_path)["forces"]["masses"]
    assert len(rows) == 4 and {r["kind"] for r in rows} == {"derived"}
    links = load_contract(ARM_POWER / "contract.yaml").model.links
    assert all(link.material == catalog.material("Aluminium 6061-T6") for link in links)


def _check_json_any(path, tmp_path):
    return _arm_check(path, tmp_path)[1]


def test_arm_power_plan_hold_passes_and_asks_for_no_reach(tmp_path):
    plan = ARM_POWER / "plan.yaml"
    assert not [f for f in _arm_findings(ARM_POWER / "contract.yaml", tmp_path, "--plan", str(plan)) if "hold_torque" in f]  # fmt: skip
    got = validate_plan(load_contract(ARM_POWER / "contract.yaml"), load_plan(plan))
    assert not [f for f in got if f.quantity.endswith("/reach_m")]


def test_arm_power_plan_hold_fires_when_the_shoulder_is_too_weak(tmp_path):
    weak = _arm_variant(tmp_path, text=(ARM_POWER / "contract.yaml").read_text().replace("effort: 0.52", "effort: 0.05"))  # fmt: skip
    found = _arm_findings(weak, tmp_path, "--plan", str(ARM_POWER / "plan.yaml"))
    hold = found["plan/segments/hold/hold_torque/shoulder"]
    assert hold["severity"] == "warning" and "<= 0.05" in hold["expected"]


def test_arm_power_readme_names_each_problem_and_its_fix():
    text = (ARM_POWER / "README.md").read_text()
    assert all(k in text for k in ("w.pack", "w.servo-feed", "IO12", "IO13", "awg: 18"))


def test_arm_power_has_no_broken_references():
    from lockwire.checks import check_reference_integrity

    assert check_reference_integrity(load_contract(ARM_POWER / "contract.yaml")) == []


def test_the_built_to_fail_bench_is_a_starter_and_both_failing_examples_show_two_failures(tmp_path):
    from lockwire.page import build_payload
    from lockwire.schema import load_contract

    assert any(s["name"] == "servo-bench" for s in ops.starters())
    for name in ("servo-bench", "arm-power"):  # the chip says "problems to find" / "built to fail"
        ops.start_example(name, tmp_path / f"{name}.yaml")
        assert build_payload(load_contract(tmp_path / f"{name}.yaml"))["violations"] == 2, name


def test_each_start_chip_line_matches_what_check_really_finds(tmp_path):
    import re
    from pathlib import Path

    from lockwire.page import build_payload
    from lockwire.schema import load_contract

    tsx = (Path(__file__).parent.parent / "app/src/renderer/OpenFace.tsx").read_text(
        encoding="utf-8"
    )
    claimed = dict(re.findall(r'"?([\w-]+)"?: \["(fails|warns|clean)"', tsx))
    assert set(claimed) == {s["name"] for s in ops.starters()}
    for name, kind in claimed.items():
        ops.start_example(name, tmp_path / f"{name}.yaml")
        p = build_payload(load_contract(tmp_path / f"{name}.yaml"))
        assert ("fails" if p["violations"] else "warns" if p["warnings"] else "clean") == kind, name
