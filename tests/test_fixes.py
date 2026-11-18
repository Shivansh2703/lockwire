"""suggest-fixes: every fix it returns is signed on a copy of the robot through the served
commands and checked again."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from lockwire import catalog, fixes, serve
from lockwire.checks import run_record_checks
from lockwire.edit import Change, apply_changes, contract_revision
from lockwire.report import Finding, RecordReport
from lockwire.schema import load_contract

EXAMPLES = Path(fixes.__file__).parent / "examples"
FIXTURES = Path(__file__).parent / "fixtures"
WITNESS = "Asha Rao"


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    """Exact fix lists must not depend on how busy the machine is: no time budget here."""
    monkeypatch.setattr(fixes, "BUDGET_S", 1e9)


def _copy(to: Path, starter: str) -> Path:
    """A starter copied into the new folder ``to``; the lockfile's path."""
    source = EXAMPLES / starter
    if source.is_dir():
        return shutil.copytree(source, to) / "contract.yaml"
    to.mkdir()
    return Path(shutil.copy(source, to))


def _standing(lock: Path) -> set[tuple[str, str]]:
    report = run_record_checks(load_contract(lock), contract_path=lock)
    return {(f.severity, f.quantity) for f in report if f.severity in ("violation", "warning", "not_scored")}  # fmt: skip


def _declare(lock: Path, *changes: tuple[str, str]) -> None:
    """Test setup: a pair is a literal edit of the file when the file holds its first half, else
    a declared path and value."""
    for a, b in changes:
        if a in (text := lock.read_text()):
            lock.write_text(text.replace(a, b))
        else:
            apply_changes(lock, [Change(a, b, "test setup", WITNESS)])


def _apply(lock: Path, fix: dict, **more: bool) -> dict:
    """Sign the fix's steps the way its one card does: one served swap-apply (the assistant's
    `swap` tool), or one served batch-edit of its declare steps. The engine's reply."""
    steps = fix["steps"]
    if steps[0]["command"] == "swap":
        command, args = "swap-apply", steps[0]["args"]
    else:
        command, args = "batch-edit", {"changes": [{"op": s["command"], **s["args"], "witness": WITNESS} for s in steps]}  # fmt: skip
    revision = hashlib.sha256(lock.read_bytes()).hexdigest()
    body = {"command": command, "args": {"contract": str(lock), "expected_revision": revision, **args, **more}}  # fmt: skip
    reply = serve.dispatch(TypeAdapter(serve.request_type()).validate_python(body))
    assert reply.refusal is None, reply.refusal
    return reply.payload


SERVO_DRAWS = [(f"devices/dev.servo{i}/power/draw", "5 W") for i in (1, 2, 3, 4)]
# the servo lead made 6 m long and ended on the shoulder servo: it drops too much at stall
LONG_LEAD = ("    length_m: 0.6\n", '    length_m: 6\n    from: {device: dev.esp32, pin: "1"}\n    to: {device: dev.servo.shoulder, pin: "1"}\n')  # fmt: skip
# the servo rail fed from 40 V: above what the D24V50F5 takes (38 V), inside the D36V50F5's range
FED_40V = ("  rails:\n  - id: rail.servo-5v\n", "  rails:\n  - id: rail.in\n    voltage: {value: 40, unit: V}\n    capacity: {value: 500, unit: W}\n  - id: rail.servo-5v\n    upstream: rail.in\n    efficiency: 0.8\n")  # fmt: skip
AT_6V = ("topology/rails/rail.servo-5v/voltage", "6 V")
CASES = {  # name: starter, changes made first, finding, the first fix's label starts with, its numbers after
    "weak wrist servo": ("servo-bench.yaml", [], "model/joints/wrist/limits/effort", "wrist: swap feetech.scs0009 for dynamixel.xl330-m288", "0.40 N*m joint effort limit; <= 0.52 N*m dynamixel.xl330-m288 stall torque"),
    "brown-out": ("arm-power", [], "topology/rails/rail.5v/brownout", "wire w.servo-feed: 28 AWG to 22 AWG", "rail.5v holds 4.91 V with every servo stalled; >= 4.75 V, the declared minimum input of dev.jetson"),
    "fuse too big": ("arm-power", [], "topology/wires/w.pack/fuse", "fuse on rail.battery: 9 A to 7 A", "7 A fuse on 22 AWG wire w.pack; fuse <= 7 A wire ampacity"),
    "fuse, wire far too thin": ("arm-power", [("topology/wires/w.pack/awg", "28")], "topology/wires/w.pack/fuse", "wire w.pack: 28 AWG to 20 AWG", "9 A fuse on 20 AWG wire w.pack; fuse <= 11 A wire ampacity"),
    "overloaded rail": ("servo-bench.yaml", SERVO_DRAWS, "budgets/bud.power.servo-5v", "regulator for rail.servo-5v: pololu.d24v50f5", "24.10 W; <= 25.00 W"),
    "overloaded rail fed above a regulator's range": ("servo-bench.yaml", [FED_40V, *SERVO_DRAWS], "budgets/bud.power.servo-5v", "regulator for rail.servo-5v: pololu.d36v50f5", "24.10 W; <= 27.50 W"),
    "pack over its C-rate": ("arm-power", [("topology/battery/c_rate_burst", "1")], "topology/battery/c_rate_burst", "pack: pack.gensace.3s-2200-35c-bashing", "2.92 A peak (nominal); <= 154 A (2.2 Ah x 70C)"),
    # the thicker lead also eases the brown-out, which stays: a fix that improves a problem already there is returned
    "wire drop": ("arm-power", [LONG_LEAD], "topology/wires/w.servo-feed/drop", "wire w.servo-feed: 28 AWG to 22 AWG", "servo wire drops 0.47 V at stall (5.0 V -> 4.53 V); >= 3.7 V (dynamixel.xl330-m288 minimum input voltage)"),
}  # fmt: skip


@pytest.mark.parametrize("case", CASES)
def test_every_returned_fix_clears_the_finding_on_a_copy_and_adds_nothing(tmp_path, monkeypatch, case):
    monkeypatch.setenv("HOME", str(tmp_path))  # no private parts.yaml
    starter, setup, finding, first, after = CASES[case]
    lock = _copy(tmp_path / "robot", starter)
    if setup:
        _declare(lock, *setup)
    before, was, listing = lock.read_bytes(), _standing(lock), sorted(lock.parent.iterdir())
    found = fixes.suggest(lock, finding)
    assert lock.read_bytes() == before and sorted(lock.parent.iterdir()) == listing
    assert found["fixes"] and found["fixes"][0]["label"].startswith(first), found
    assert "; ".join(found["fixes"][0]["after"].values()) == after
    for n, fix in enumerate(found["fixes"]):
        trial = shutil.copytree(lock.parent, tmp_path / f"trial{n}") / lock.name
        _apply(trial, fix)
        now = _standing(trial)
        assert finding not in {quantity for _, quantity in now}, fix["label"]
        assert now <= was, (fix["label"], now - was)
        assert "discontinued" not in fix["label"]  # the catalog names a part it marks discontinued
        if fix["part"]:  # a part comes with its cited datasheet figures
            assert all(x["source"].startswith("https://") for x in fix["part"]["numbers"].values())


def test_a_fix_that_would_make_a_new_violation_is_never_returned(tmp_path, monkeypatch):
    """The stronger wrist servos are heavier. With the shoulder's effort limit just above its
    load today, either swap tips the shoulder over its limit, so neither is offered."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "robot", "servo-bench.yaml")
    wrist = "model/joints/wrist/limits/effort"
    swaps = fixes.suggest(lock, wrist)["fixes"]
    assert len(swaps) == 2 and all(f["notes"][-1] == fixes.COMPARED for f in swaps)
    moved = [n.split(": ")[1].split(" gravity")[0] for n in swaps[0]["notes"] if "closer to its bound" in n]
    assert moved == ["shoulder", "elbow"]  # the larger share of margin lost first: 2.7 % before 0.9 %
    # the 5 V rail needs 32.1 W and no catalog regulator reaches it: a bigger one is not a fix
    overload = fixes.suggest(lock, "budgets/bud.power.servo-5v")
    assert overload["fixes"] == [] and overload["tried"] and "none cleared" in overload["reason"]
    _declare(lock, ("model/joints/shoulder/limits/effort", "0.815"))
    was = _standing(lock)
    found = fixes.suggest(lock, wrist)
    assert found["fixes"] == [] and "none cleared" in found["reason"]
    swap, before = {"steps": [{"command": "swap", "args": {"joint": "wrist", "part_id": "dynamixel.xl330-m288"}}]}, lock.read_bytes()  # fmt: skip
    # the swap card's dry run: the engine's diff for the card, nothing written
    assert "+  wrist: dynamixel.xl330-m288" in _apply(lock, swap, dry_run=True)["diff"] and lock.read_bytes() == before  # fmt: skip
    _apply(lock, swap)  # what it turned down, written for real
    assert _standing(lock) - was == {("violation", "model/joints/shoulder/limits/effort")}
    # that new finding is a gravity load over the limit: no servo swap moves it, so none is tried
    assert fixes.suggest(lock, "model/joints/shoulder/limits/effort")["reason"] == fixes.NO_KIND


def test_a_fix_that_would_deepen_a_problem_already_there_is_never_returned(tmp_path, monkeypatch):
    """The review's two measured cases: a stronger shoulder servo draws more and deepens the
    brown-out; a heavier wrist servo adds to a shoulder load that is already over its limit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    shoulder = "model/joints/shoulder/limits/effort"
    for starter, limit, joint, deeper in (("arm-power", "0.9", "shoulder", "topology/rails/rail.5v/brownout"), ("servo-bench.yaml", "0.7", "wrist", shoulder)):  # fmt: skip
        lock = _copy(tmp_path / joint, starter)
        _declare(lock, (shoulder, limit))
        found = fixes.suggest(lock, f"model/joints/{joint}/limits/effort", 5)
        assert found["fixes"] == [], found
        assert any(f"deepen an existing problem: {deeper}, from " in t["why"] for t in found["turned_down"]), found  # fmt: skip
        if starter == "arm-power":  # a swap leaves the declared draw as it was: the fix says which field to update
            first = fixes._servos(load_contract(lock), lock, re.fullmatch(fixes._KINDS[0][0], shoulder))[0]
            assert "the declared draw of dev.servo.shoulder is still the old servo's (7.35 W); update devices/dev.servo.shoulder/power/draw" in first.notes[-1]  # fmt: skip
    # the same shoulder, the wrist on a 65 g servo that is too weak: the one lighter servo that is
    # strong enough eases the shoulder's load and is returned; every heavier one is turned down
    _declare(lock, ("topology/rails/rail.servo-5v/voltage", "12 V"))
    _apply(lock, {"steps": [{"command": "swap", "args": {"joint": "wrist", "part_id": "dynamixel.xc430-w150"}}]})  # fmt: skip
    _declare(lock, ("model/joints/wrist/limits/effort", "2"))
    found = fixes.suggest(lock, "model/joints/wrist/limits/effort", 5)
    assert [f["steps"][0]["args"]["part_id"] for f in found["fixes"]] == ["feetech.st3215"], found
    # the tip margin is compared on the check's own distance: a lighter caster narrows it
    rover = _copy(tmp_path / "rover", "rover")
    was = load_contract(rover)
    before = run_record_checks(was, contract_path=rover)
    _declare(rover, ("model/links/caster_back_link/mass", "0.001 kg"))
    now = load_contract(rover)
    after = run_record_checks(now, contract_path=rover)
    assert "deepen an existing problem: model/tip_margin, from 0.004045989 m to 0.00" in fixes._worse(before, after, fixes._rows(before, was), fixes._rows(after, now))  # fmt: skip

    # a finding the engine holds no number for: a changed text may be worse; an unchanged text that
    # prints a measured number may hide a change, so it counts when the candidate moves what it can depend on
    def line(observed: str) -> RecordReport:
        return RecordReport([Finding(severity="warning", quantity="budgets/b/ripple", observed=observed, expected="-", action="-")])  # fmt: skip

    rows = fixes._rows(before, was)
    assert fixes._worse(before, before, rows, rows) is None
    assert "no number to compare, so it may be worse: budgets/b/ripple" in fixes._worse(line("2 frames"), line("3 frames"), [], [])  # fmt: skip
    assert "can depend on the current and voltage" in fixes._worse(line("0.12 V"), line("0.12 V"), [], [], {"current", "voltage"})  # fmt: skip
    assert fixes._worse(line("0.12 V"), line("0.12 V"), [], [], {"mass"}) is None
    assert fixes._depends("model/links/shin/mass_vs_size") == fixes._depends("topology/wires/w.bus-data") == fixes._depends("model/joints/w/limits/datasheet_range") == set()  # fmt: skip


def test_a_pack_limit_is_stated_as_the_check_scores_it_capacity_times_rate(tmp_path, monkeypatch):
    """The fourth look's case: the 4000 mAh pack's burst rate is lower (50C against 70C) but its
    burst limit, 4 Ah x 50C, is higher than 2.2 Ah x 70C: more headroom, not less."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "pack", "arm-power")
    _declare(lock, ("topology/battery/c_rate_continuous", "0.5"))
    found = fixes.suggest(lock, "topology/battery/c_rate_continuous", 5)["fixes"]
    notes = next(f["notes"] for f in found if f["part"]["id"] == "pack.gensace.3s-4000-60c")
    assert "the pack's burst limit goes from 154 A (2.2 Ah x 70C) to 200 A (4 Ah x 50C); rows scored against it have more headroom" in notes  # fmt: skip


def test_moved_rows_rank_by_the_share_of_margin_lost_not_by_raw_gap():
    volts = fixes._Row("v", True, -0.106, 4.856, 4.75, "V", "brown-out")  # 0.106 V of headroom
    load = fixes._Row("t", True, -0.2, 0.1, 0.3, "N*m", "load")  # 0.2 N*m of headroom
    after = [fixes._Row("v", True, -0.09, 4.84, 4.75, "V", "brown-out"), fixes._Row("t", True, -0.1, 0.2, 0.3, "N*m", "load")]  # fmt: skip
    # the brown-out ends nearer its bound in raw units (0.09 < 0.1), but the load loses half its margin, the brown-out 15 %
    assert [m.split(" ")[0] for m in fixes._margins([volts, load], after)] == ["load", "brown-out"]


def test_a_robot_that_cannot_stand_gets_no_fix_that_raises_its_standing_load(tmp_path, monkeypatch):
    """The review's quadruped: a 12 kg trunk whose rear hips are already over their limit standing,
    and a tail servo too weak. Every stronger tail servo is heavier and loads the rear hips more;
    the ST3215 is 0.4 g heavier and adds 0.14 mN*m, which the printed 2.41 N*m hides."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = shutil.copytree(FIXTURES / "tail-quadruped", tmp_path / "q") / "lockwire.yaml"
    found = fixes.suggest(lock, "model/joints/tail_joint/limits/effort", 5)
    assert found["fixes"] == [], found
    why = next(t["why"] for t in found["turned_down"] if "st3215" in t["label"])
    assert re.search(r"deepen an existing problem: model/joints/hip_r[lr]/standing/all_feet, from 2\.41203\d N\*m to 2\.41217\d N\*m", why), why  # fmt: skip


def test_a_pack_fix_carries_the_new_packs_weight_and_says_what_else_moves(tmp_path, monkeypatch):
    """The review's arm-power case: the pack device weighs 150 g (not the battery's 143 g) and
    rides on the forearm. Each pack fix sets the device to the new pack's catalog mass, says how
    much nearer the shoulder's gravity load comes to its limit, and with the shoulder at 0.28 N*m
    the heavier packs, which would put it over, are turned down."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "pack", "arm-power")
    _declare(lock, ("topology/battery/c_rate_burst", "1"), ("model: mdl.pack\n  physical:\n    mass:\n      value: 143\n      unit: g\n    mount_frame: base", "model: mdl.pack\n  physical:\n    mass:\n      value: 150\n      unit: g\n    mount_frame: forearm"))  # fmt: skip
    packs = {f["part"]["id"]: f for f in fixes.suggest(lock, "topology/battery/c_rate_burst", 5)["fixes"]}
    light, heavy = packs["pack.gensace.3s-2200-35c-bashing"], packs["pack.gensace.3s-2200-60c-adventure"]
    assert light["steps"][-1]["args"]["path"] == "devices/dev.battery/physical/mass" and light["steps"][-1]["args"]["value"] == "143 g"  # fmt: skip
    assert not any("closer to its bound" in n for n in light["notes"])  # lighter: nothing loses margin
    assert "it moves a passing row closer to its bound: shoulder gravity load 0.2529 to 0.2941 N*m, of 0.52 allowed" in heavy["notes"]  # fmt: skip
    _declare(lock, ("model/joints/shoulder/limits/effort", "0.28"))
    found = fixes.suggest(lock, "topology/battery/c_rate_burst", 5)
    assert [f["part"]["id"] for f in found["fixes"]] == ["pack.gensace.3s-2200-35c-bashing"], found
    assert all("new problem: model/joints/shoulder/limits/effort" in t["why"] for t in found["turned_down"]), found  # fmt: skip
    _declare(lock, ("    source_device: dev.battery\n", ""))  # no device is the pack now
    assert "the pack's weight is not on the robot model" in " ".join(fixes.suggest(lock, "topology/battery/c_rate_burst")["fixes"][0]["notes"])  # fmt: skip


def test_an_unverified_part_says_so_and_ranks_last_and_a_discontinued_servo_is_not_offered(tmp_path, monkeypatch):  # fmt: skip
    """The review's case: at 6 V the elbow's STS3032 is too weak and the only servo that clears it
    is the STS3215, whose voltage figures the catalog has from a retailer's page."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "robot", "servo-bench.yaml")
    elbow = "model/joints/elbow/limits/effort"
    _declare(lock, AT_6V, ("elbow: feetech.sts3215", "elbow: feetech.sts3032"))
    (fix,) = fixes.suggest(lock, elbow)["fixes"]
    assert fix["part"]["id"] == "feetech.sts3215"
    assert "this part's datasheet figures are not fully verified: input voltage min, input voltage max are not" in " ".join(fix["notes"])  # fmt: skip
    _declare(lock, (elbow, "0.9"))  # now a verified servo clears it too, and Part Swap ranks it second (another bus)
    ids = [f["part"]["id"] for f in fixes.suggest(lock, elbow)["fixes"]]
    assert ids == ["dynamixel.xc330-m288", "feetech.sts3215"]
    gone = catalog.Availability(part=ids[0], status="discontinued", source="https://example.com", read_on="2026-11-16")  # fmt: skip
    seed = catalog.load_catalog()
    monkeypatch.setattr(fixes, "load_catalog", lambda: seed.model_copy(update={"availability": [gone]}))  # fmt: skip
    assert [f["part"]["id"] for f in fixes.suggest(lock, elbow)["fixes"]] == ids[1:]


def test_filters_the_check_cannot_see(tmp_path, monkeypatch):
    """A pack with another cell count and a servo that tips a joint above it are never offered:
    the check has no finding for either, so each filter is the only guard."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "pack", "arm-power")  # a 3S robot
    _declare(lock, ("topology/battery/c_rate_burst", "1"))
    packs = fixes.suggest(lock, "topology/battery/c_rate_burst", 5)["fixes"]
    assert len(packs) == 4 and all(f["part"]["numbers"]["cells_series"]["value"] == 3 for f in packs)
    lock = _copy(tmp_path / "bench", "servo-bench.yaml")
    _declare(lock, AT_6V)
    servos = fixes.suggest(lock, "model/joints/wrist/limits/effort", 5)["fixes"]
    # the STS3215 is strong enough, but its 55 g drops the shoulder from fit to tight
    assert [f["part"]["id"] for f in servos] == ["dynamixel.xl330-m288", "feetech.sts3032", "dynamixel.xc330-m288"]  # fmt: skip
    assert "feetech.scs0009 speaks feetech-scs and dynamixel.xl330-m288 speaks dynamixel-p2, so an adapter or controller for dynamixel-p2 is needed" in servos[0]["notes"][1]  # fmt: skip


def test_served_command_reads_only_and_says_why_it_has_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = _copy(tmp_path / "robot", "arm-power")
    before, listing = lock.read_bytes(), sorted(lock.parent.iterdir())

    def run(finding: str, limit: int = 3) -> dict:
        args = fixes.SuggestFixesArgs(contract=str(lock), finding=finding, limit=limit)
        return fixes.handle(args).payload

    (fuse,) = run("fuse", 1)["fixes"]  # the one finding that ends in /fuse
    # the fix lowers a bound itself: said with both numbers; and what was not compared is said
    assert fuse["notes"][-2:] == ["the fuse limit on rail.battery goes from 9 A to 7 A; rows scored against it have less headroom", fixes.COMPARED]  # fmt: skip
    other = run("topology/wires/w.bus-data")
    assert other["fixes"] == [] and other["reason"] == fixes.NO_KIND
    assert "no single finding is named 'nope'" in run("nope")["reason"]
    monkeypatch.setattr(fixes, "BUDGET_S", 0.0)  # past the time budget: it stops and says so
    late = run("fuse")
    assert (late["tried"], late["fixes"]) == (0, []) and late["reason"].startswith("the search stopped early")
    go = fixes.suggest(EXAMPLES / "drone/contract.yaml", "anything")
    assert (go["robot_verdict"], go["fixes"]) == ("GO", []) and "no violation or warning" in go["reason"]
    assert lock.read_bytes() == before and sorted(lock.parent.iterdir()) == listing
    assert "no contract at" in fixes.handle(fixes.SuggestFixesArgs(contract=str(tmp_path / "none.yaml"), finding="x")).refusal  # fmt: skip


REPLIES = Path(__file__).parents[1] / "app/src/main/engine-replies.gen.json"


def _replies_robots(root: Path) -> dict[str, Path]:
    """The robots the table runs on, laid out as engine-replies.test.ts lays them out (the app side needs the files its own guards open)."""
    arm = _copy(root / "arm", "arm-power")
    shutil.copy(arm.parent / "plan.yaml", arm.parent / "contract.plan.yaml")  # where main looks for a robot's plan
    bench = _copy(root / "bench", "servo-bench.yaml")
    so101 = shutil.copytree(FIXTURES / "so101", root / "so101") / "lockwire.yaml"
    rover = _copy(root / "rover", "rover-param")
    return {"arm": arm, "bench": bench, "so101": so101, "rover": rover}


PACK, PACK_NAME = "pack.gensace.3s-5000-60c-short", "Gens ace 5000mAh 3S 60C short-size (XT60)"
QUOTE = "Stall torque 0.52 N*m"


def addDevice(id: str, family: str, model: str, source: str, owner: str) -> dict:  # noqa: N802 - the app's own name (assistantDesign.ts)
    return {"kind": "device", "id": id, "source": source, "fields": {"family": family, "model": model, "owner": owner}}


def _replies_table(r: dict[str, Path]) -> dict[str, tuple[str, dict, dict]]:
    """Per tool of the assistant's table that reaches the engine: (served command, the model's input, the arguments main sends).
    engine-replies.test.ts checks main builds exactly these arguments from that input, and fails a tool with no row here."""
    arm, bench, so101 = (str(r[k]) for k in ("arm", "bench", "so101"))
    me, rev = "your name", {"arm": contract_revision(r["arm"]), "bench": contract_revision(r["bench"])}
    row = lambda command, given, args: (command, given, args)  # noqa: E731
    read = lambda command, given=None, **more: row(command, given or {}, {"contract": arm, **(given or {}), **more})  # noqa: E731
    dry = {"contract": arm, "witness": me, "expected_revision": rev["arm"], "dry_run": True}
    propose = lambda command, given: row(command, given, {**given, **dry})  # noqa: E731
    change = {"path": "devices/dev.servo.shoulder/power/draw", "value": "1 W", "source": "datasheet"}
    page = row("page", {}, {"contract": arm})
    part = {"datasheet": "sheet.pdf", "id": "dev.new-sensor", "family": "fam.compute", "model": "mdl.jetson-nano", "owner": "you", "numbers": [{"name": "stall_torque", "value": 0.52, "unit": "N*m", "page": 2, "quote": QUOTE}]}  # fmt: skip
    return {
        "check": read("check"),
        "what-if": row("what-if", {"change": change}, {"contract": arm, "expected_revision": rev["arm"], "change": {**change, "witness": me}}),
        "plan": read("plan", plan=str(r["arm"].parent / "contract.plan.yaml")),
        "describe": read("describe"), "declare-form": read("declare-form"), "report": read("report"),
        "page": read("page", {"section": "joints"}), "log": read("log"), "diff": read("diff", {"a": "HEAD"}, revisions_only=True),
        "declare": propose("declare", change),
        "add": propose("add", {"kind": "param", "fields": {"name": "reach", "value": 0.3}, "source": "datasheet"}),
        "review": propose("review", {"path": change["path"], "state": "approved"}),
        "place": propose("place", {"target": "dev.esp32", "on": "upper-arm", "offset": [0, 0, 10], "unit": "mm", "source": "measured"}),
        "move": propose("move", {"target": "dev.jetson", "offset": [0, 0, 10], "unit": "mm", "source": "measured"}),
        "rotate": propose("rotate", {"target": "forearm", "rpy": [0, 0, 5], "unit": "deg", "source": "measured"}),
        "reparent": propose("reparent", {"target": "dev.esp32", "to": "upper-arm", "source": "measured"}),
        "mount-shape": propose("mount-shape", {"target": "dev.jetson", "shape": {"kind": "plate", "dimensions_mm": [100, 80, 3]}, "source": "measured"}),
        "swap-preview": row("swap-preview", {"joint": "wrist"}, {"contract": bench, "joint": "wrist"}),
        "catalog-parts": row("catalog-parts", {"query": "servo", "limit": 2}, {"query": "servo", "limit": 2}),
        "catalog-robots": row("catalog-robots", {}, {}),
        "parts-match": row("parts-match", {"parts": "feetech.sts3215"}, {"parts": "feetech.sts3215"}),
        "describe-system": read("describe-system"),
        "describe-device": read("describe-device", {"device_id": "dev.battery"}),
        "explain-finding": read("explain-finding", {"finding": "fuse"}),
        "suggest-fixes": read("suggest-fixes", {"finding": "fuse", "limit": 1}),
        "swap": row("swap-apply", {"joint": "wrist", "part_id": "dynamixel.xl330-m288"}, {"contract": bench, "joint": "wrist", "part_id": "dynamixel.xl330-m288", "expected_revision": rev["bench"], "dry_run": True}),
        "robot-graph": row("page", {}, {"contract": arm, "section": "robot_graph"}),
        "select-part": row("page", {"id": "joint:shoulder"}, {"contract": arm}),
        "show-only": row("page", {"parts": ["link:forearm"]}, {"contract": arm}),
        "open-graph": page,
        "check-sim": row("check-sim", {"sim": "so101_drift.xml"}, {"contract": so101, "sim": str(r["so101"].parent / "so101_drift.xml")}),
        # The free Agent tools reach the engine through the card's dry run (propose-part, propose-power-tree) or the check before (propose-robot).
        "propose-part": row("add", part, {**addDevice("dev.new-sensor", "fam.compute", "mdl.jetson-nano", f"sheet.pdf: stall_torque 0.52 N*m (p.2: \"{QUOTE}\")", "you"), **dry}),
        "propose-power-tree": row("add", {"owner": "you", "trees": [{"name": "main", "pack": {"id": PACK, "name": PACK_NAME}, "rails": []}]}, {**addDevice(PACK, "fam.unclassified", PACK_NAME, f"Lockwire parts library: {PACK} (cited in the catalog)", "you"), **dry}),
        "propose-robot": row("check", {"template": "rover-param", "dimensions": [{"name": "chassis_length", "value": 0.42}]}, {"contract": str(r["rover"])}),
    }  # fmt: skip


def record_replies(tmp_path: Path, robots: dict[str, Path], table: dict[str, tuple[str, dict, dict]], out: Path, files: dict[str, list[str]] | None = None) -> None:
    """Run each row on the served dispatch and keep, per tool, the model's input, the arguments sent and the reply: the envelope,
    and of the payload only what the app's reply check reads (its `verdict` and `exit_code`, when present), so the file is the
    same whatever plugin adds to a payload. Paths read ROBOT, revisions REV. `files`: per tool, the paths (under ROBOT) its own
    guard in main opens, which the app test lays out. The robot folders are hashed: a read, or a dry run, writes nothing.
    Stale: rewritten, fails."""
    snapshot = lambda: {p: p.read_bytes() for p in (tmp_path / "ROBOT").rglob("*") if p.is_file()}  # noqa: E731
    before = snapshot()
    revs = {contract_revision(p): "REV" for p in robots.values()}
    place = lambda v: json.loads(re.sub("|".join(map(re.escape, [str(tmp_path / "ROBOT"), *revs])), lambda m: "ROBOT" if m[0] not in revs else "REV", json.dumps(v)))  # noqa: E731
    got, seen = {}, {}
    for tool, (command, given, args) in table.items():
        key = json.dumps([command, args], sort_keys=True)
        if key not in seen:
            body = {"command": command, "args": args}
            seen[key] = serve.dispatch(TypeAdapter(serve.request_type()).validate_python(body)).model_dump(mode="json")
        reply = dict(seen[key])
        doc = reply["payload"]
        reply["payload"] = {k: doc[k] for k in ("verdict", "exit_code") if k in doc} if isinstance(doc, dict) else doc and "-"  # fmt: skip
        reply = {**reply, "version": "-", "cli_line": place(reply["cli_line"])}
        got[tool] = {"command": command, "input": given, "args": place(args), "reply": place(reply), **({"files": files[tool]} if files and tool in files else {})}
    assert snapshot() == before, "a read or a dry run wrote into the robot's folder"
    text = json.dumps(got, indent=1, sort_keys=True) + "\n"
    if not out.exists() or out.read_text() != text:
        out.write_text(text)
        pytest.fail(f"{out.name} was stale and is rewritten: commit it")


def test_the_assistants_served_replies_are_the_committed_ones_the_app_test_reads(tmp_path, monkeypatch):
    """Every free tool of the assistant's table that reaches the engine, run for real on a copy of a shipped robot with
    the arguments main sends; engine-replies.test.ts passes each reply through the app's real reply check. The paid rows have
    their own twin, written by a paid test."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    robots = _replies_robots(tmp_path / "ROBOT")
    record_replies(tmp_path, robots, _replies_table(robots), REPLIES)
