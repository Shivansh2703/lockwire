import json
from pathlib import Path

from conftest import plain
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


def test_rover_start_then_check_is_no_go(tmp_path):
    out = tmp_path / "x.yaml"
    assert runner.invoke(app, ["start", "--example", "rover", "--out", str(out)]).exit_code == 0
    assert (tmp_path / "x.glb").is_file()
    result = runner.invoke(app, ["check", str(out)])
    assert result.exit_code == 1, result.output
    out_text = plain(result.output)
    assert "rail.jetson-header-5v/current_limit" in out_text
    assert out_text.count("VIOLATION") == 1  # one fault, not a budget and a current limit
    assert "NO-GO" in out_text


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


def test_quadruped_start_then_check_names_the_supply_finding(tmp_path):
    out = tmp_path / "x.yaml"
    assert runner.invoke(app, ["start", "--example", "quadruped", "--out", str(out)]).exit_code == 0
    assert out.with_suffix(".glb").is_file()
    checked = runner.invoke(app, ["check", str(out)])
    assert checked.exit_code == 1
    assert (
        "bud.power.bench-supply-24v" in checked.output
        and "678.60 W (draw mode: jump)" in checked.output
    )
    assert "bud.power.bench-supply-24v.walk" not in checked.output  # the walking budget passes


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
    findings = validate_plan(contract, load_plan(folder / "plan.yaml"))
    assert any("dev.motor.front-left-knee" in str(f) for f in findings)


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


def test_arm_starter_is_no_go_on_the_supply_budget(tmp_path):
    out = tmp_path / "x.yaml"
    assert runner.invoke(app, ["start", "--example", "arm", "--out", str(out)]).exit_code == 0
    assert (tmp_path / "x.glb").is_file()
    result = runner.invoke(app, ["check", str(out)])
    assert result.exit_code == 1, result.output
    text = plain(result.output)
    assert (
        "observed 194.40 W (draw mode: stall)" in text
    )  # 6 x 2.7 A x 12 V; only the failure says it
    assert "supply-12v-no-load" not in text  # the no-load budget passes


def test_arm_example_plan_is_scored_against_the_starter():
    from lockwire.plan import load_plan, validate_plan
    from lockwire.schema import load_contract

    folder = ops.EXAMPLES / "arm"
    plan = load_plan(folder / "plan.yaml")
    findings = validate_plan(load_contract(folder / "contract.yaml"), plan)
    assert len(plan.steps) == 2
    assert [f.severity for f in findings] == ["violation", "violation"]
    assert "194.40 W" in findings[0].observed


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


def test_humanoid_starts_and_checks_no_go_on_the_motor_converters(tmp_path):
    out = tmp_path / "x.yaml"
    assert runner.invoke(app, ["start", "--example", "humanoid", "--out", str(out)]).exit_code == 0
    assert out.is_file() and out.with_suffix(".glb").is_file()
    checked = runner.invoke(app, ["check", str(out)])
    text = " ".join(checked.output.split())
    assert checked.exit_code == 1 and "NO-GO" in text
    # 12 servos on a 54 W converter: only the failing contract says this.
    assert "observed 275.52 W (draw mode: stall), expected <= 54.00 W" in text


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
    assert not [
        i for i in ids if re.search(r"\.m\d$|esc\d$|oak|jetson|servo-motor|here3|rfd900", i)
    ]
    assert set(ids) <= set(load_scene(folder / "scene.glb").nodes_with_ids)
    findings = validate_plan(
        contract, load_plan(folder / "plan.yaml")
    )  # step 1 is over the supply rail, step 2 fits
    assert [f.quantity for f in findings if f.severity == "violation"] == [
        "plan/steps/0/set_power_mode/draw"
    ]


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
    assert {o.path for o in report.outcomes} >= {
        "plan/steps/0/set_power_mode/draw",
        "plan/steps/1/rail_load/draw",
    } and not report.errors
