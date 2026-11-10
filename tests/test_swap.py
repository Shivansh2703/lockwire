"""Part Swap: ranking by gravity load, knock-on lines, the one-edit accept."""

import difflib
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire import catalog
from lockwire.cli import app
from lockwire.edit import _dump, _load_doc_guarded
from lockwire.schema import load_contract
from lockwire.checks import check_servo_stall
from lockwire.swap import apply_swap, preview, shown_ratio, status

ARM = Path(__file__).parents[1] / "src/lockwire/examples/arm/contract.yaml"
BENCH = ARM.parents[1] / "servo-bench.yaml"
CAT = Path(__file__).parent / "fixtures/swap/catalog.yaml"


@pytest.fixture
def arm(tmp_path, monkeypatch):
    """A 0.5 copy of the SO-101 starter on feetech.sts3215, re-indented once by the edit
    path itself so the swap's diff shows only what the swap wrote."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no private parts.yaml
    _, doc, style = _load_doc_guarded(ARM)
    text = _dump(doc, style).replace('schema_version: "0.2"', 'schema_version: "0.5"')
    path = tmp_path / "lockwire.yaml"
    path.write_text(text + "servo: feetech.sts3215\n")
    return path


def test_wrist_sts3250_raises_the_arm_loads_and_stays_fit(arm):
    pv = preview(load_contract(arm), "wrist_flex", "feetech.sts3250")
    rows = {k.joint: k for k in pv.knock_on}
    for name in ("shoulder_lift", "elbow_flex"):
        assert rows[name].after > rows[name].before and rows[name].status == "fit"
        assert not rows[name].dropped
    out = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex"]).output
    assert "shoulder_lift" in out and "still fit" in out
    assert "warning" not in out


def test_a_heavier_swap_warns_when_the_shoulder_drops_to_tight(arm, monkeypatch):
    rows = {
        k.joint: k for k in preview(load_contract(arm), "wrist_flex", "test.heavy", CAT).knock_on
    }
    assert (rows["shoulder_lift"].was, rows["shoulder_lift"].status) == ("fit", "tight")
    monkeypatch.setattr(catalog, "_SEED_PATH", CAT)
    out = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex", "--use", "test.heavy"]).output
    assert "warning: now tight" in out


def test_a_same_mass_swap_leaves_loads_unchanged_and_writes_no_device(arm):
    pv = preview(load_contract(arm), "wrist_flex", "test.twin", CAT)
    assert pv.delta_g == 0 and all(k.after == pytest.approx(k.before) for k in pv.knock_on)
    apply_swap(arm, "wrist_flex", "test.twin", catalog=CAT)
    assert not [d for d in load_contract(arm).devices if "swap" in d.id]


def test_no_candidates_says_so(arm, monkeypatch):
    monkeypatch.setattr(catalog, "_SEED_PATH", CAT.with_name("empty.yaml"))
    out = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex"])
    assert out.exit_code == 0 and "no cited alternatives yet" in out.output


def test_use_writes_servo_and_one_device_keeps_the_version_and_a_second_swap_replaces_it(arm):
    before = arm.read_text()
    old = load_contract(arm)
    result = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex", "--use", "feetech.sts3250"])
    assert result.exit_code == 0, result.output
    after = arm.read_text()
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), n=0))[2:]
    gone = [ln[1:] for ln in diff if ln[0] == "-"]
    added = [ln[1:] for ln in diff if ln[0] == "+"]
    assert gone == ["servo: feetech.sts3215"]  # the version line stays 0.5
    assert added[-3:] == ["servo:", "  wrist_flex: feetech.sts3250", "  '*': feetech.sts3215"]
    assert added[0:4] == [  # the generic mass-only model, never an unrelated part's (item 3)
        "- id: fam.part-swap.servo-mass",
        "  name: Servo mass",
        "- id: mdl.part-swap.servo-mass",
        "  family: fam.part-swap.servo-mass",
    ]
    device = added[4:-3]  # one device block, nothing else
    assert device[0].strip() == "- id: dev.wrist_flex-servo-swap"
    assert sum(ln.lstrip().startswith("- ") for ln in device) == 1
    new = load_contract(arm)
    (dev,) = [d for d in new.devices if d.id == "dev.wrist_flex-servo-swap"]
    assert (dev.physical.mass.value, dev.physical.mount_frame) == (19.5, "wrist_link")
    assert len(new.devices) == len(old.devices) + 1 and new.schema_version == "0.5"
    assert new.model.structure_hash == old.model.structure_hash
    apply_swap(arm, "wrist_flex", "dynamixel.xm430-w350")
    again = load_contract(arm)
    (dev,) = [d for d in again.devices if "swap" in d.id]
    assert dev.physical.mass.value == 27.0 and len(again.devices) == len(new.devices)


def test_ranking_puts_status_first_then_no_upstream_drop_then_cited_then_lightest(arm):
    pv = preview(load_contract(arm), "wrist_flex", catalog=CAT)
    # test.heavy is a cited fit but tips the shoulder to tight: below every safe fit
    assert [c.id for c in pv.candidates] == [
        "test.twin",
        "test.fit-unverified",
        "test.heavy",
        "test.weak",
    ]
    assert [c.status for c in pv.candidates][-1] == "too weak"
    assert pv.candidates[2].flags == ["different bus"]


def test_mcp_swap_previews_and_writes_only_with_part_and_revision(arm):
    from lockwire.edit import contract_revision
    from tests.test_mcp_server import _call

    sc = _call("swap", {"contract": str(arm), "joint": "wrist_flex"}).structured_content
    assert sc["chosen"] == sc["candidates"][0]["id"] and not sc["written"]
    rev = contract_revision(arm)
    assert _call(
        "swap", {"contract": str(arm), "joint": "wrist_flex", "expected_revision": rev}
    ).is_error
    args = {"contract": str(arm), "joint": "wrist_flex", "part_id": "feetech.sts3250"}
    assert not _call("swap", {**args, "expected_revision": rev}).is_error
    assert load_contract(arm).servo["wrist_flex"] == "feetech.sts3250"


def test_exactly_twice_the_load_is_fit():
    assert status(2.0, 1.0) == ("fit", 2.0)  # fit at stall >= 2x load, inclusive
    assert (shown_ratio(1.97), shown_ratio(2.0)) == (1.9, 2.0)  # never "2.0x" beside tight


@pytest.mark.parametrize("drives", ["", "    drives_joint: wrist\n"])
def test_swap_mass_device_never_wears_an_unrelated_parts_model(tmp_path, monkeypatch, drives):
    monkeypatch.setenv("HOME", str(tmp_path))
    bench = tmp_path / "lockwire.yaml"
    text = (ARM.parents[1] / "servo-bench.yaml").read_text()
    host = "  - id: dev.servo1\n    model: mdl.servo.mg996r\n"  # an MG996R, not the SCS0009
    bench.write_text(text.replace(host, host + drives))
    _, result = apply_swap(bench, "wrist", "feetech.sts3032")
    c = load_contract(bench)
    (dev,) = [d for d in c.devices if d.id == "dev.wrist-servo-swap"]
    assert dev.model == "mdl.part-swap.servo-mass" and dev.owner == "you"
    assert any("link now carries the servo's" in ln for ln in result.lines)
    assert check_servo_stall(c, c.servo) == []  # sts3032 stalls at 0.441 >= the 0.4 limit
    apply_swap(bench, "wrist", "dynamixel.xl330-m288")  # a second swap adds no second model
    assert len([m for m in load_contract(bench).device_models if "part-swap" in m.id]) == 1


def test_a_private_part_is_marked_and_its_use_warns_teammates(arm, tmp_path):
    (tmp_path / ".lockwire").mkdir()
    twin = CAT.read_text().split("  - id: test.twin\n")[1].split("  - id:")[0]
    (tmp_path / ".lockwire/parts.yaml").write_text("  - id: acme.servo.a100\n" + twin)
    out = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex"]).output
    assert any("acme.servo.a100" in ln and "private" in ln for ln in out.splitlines())
    used = CliRunner().invoke(app, ["swap", str(arm), "wrist_flex", "--use", "acme.servo.a100"])
    assert (
        "is a private part: teammates without it" in used.output
        and "swapped wrist_flex to Same mass" in used.output
    )


def test_servo_bench_score_line_adds_up():
    bench = ARM.parents[1] / "servo-bench.yaml"
    out = CliRunner().invoke(app, ["check", "--all", str(bench)]).output
    m = re.search(r"scored (\d+): (\d+) add up, (\d+) do not", out)
    assert m and int(m[1]) == int(m[2]) + int(m[3])


def test_an_unknown_servo_points_private_part_owners_to_parts_yaml(tmp_path):
    c = load_contract(ARM)
    (action,) = [f.action for f in check_servo_stall(c, "acme.private-9") if f.quantity == "servo"]
    assert (
        "add it to ~/.lockwire/parts.yaml" in action and "otherwise fix the servo: line" in action
    )


def _undeclared_supply():
    c = load_contract(BENCH)
    c.topology.rails[0].voltage = None
    return c


def test_supply_and_bus_changes_are_flagged_and_never_the_silent_top_pick():
    pv = preview(load_contract(BENCH), "wrist")
    assert pv.notes == ["supply 5 V, as declared for its power rail"]
    assert "needs at least 6 V, above your supply's 5 V" in pv.candidates[-1].flags[-1]
    pv = preview(_undeclared_supply(), "wrist")  # falls back to the current servo's max input
    assert "max input voltage" in pv.notes[0]
    ids = [c.id for c in pv.candidates]
    sts3250, xl330 = (next(c for c in pv.candidates if c.id == i) for i in ids[4:6])
    assert ids[0] == "feetech.sts3215"  # was a 6 V-max part on this 7.4 V supply
    assert "max 6 V, below your supply's 7.4 V" in xl330.flags[-1]
    assert "rated at 12 V; your supply is at most 7.4 V" in sts3250.flags[-1]
    assert ids.index("feetech.sts3250") > ids.index("feetech.sts3215")  # a 7.4 V fit


def test_a_voltage_mismatch_ranks_below_a_bus_change(tmp_path):
    head, tail = catalog._SEED_PATH.read_text().split("- id: feetech.sts3250")
    tail = tail.replace("max:\n      value: 12", "max:\n      value: 6", 1)  # same bus, 6 V max
    (tmp_path / "c.yaml").write_text(head + "- id: feetech.sts3250" + tail)
    pv = preview(_undeclared_supply(), "shoulder", catalog=tmp_path / "c.yaml")
    ids = [c.id for c in pv.candidates]
    assert ids.index("dynamixel.xc430-w150") < ids.index("feetech.sts3250")


def test_full_mass_is_named_when_the_old_servo_is_unknown(tmp_path):
    so101 = tmp_path / "lockwire.yaml"
    so101.write_text(ARM.read_text())  # no servo: line, so the old mass is unknown
    lines = apply_swap(so101, "wrist_flex", "feetech.sts3250", dry_run=True)[1].lines
    assert any("full 74.5 g" in ln for ln in lines)
    assert not any("mass difference" in ln for ln in lines)
    assert any("supply 12 V, as declared for its power rail" in ln for ln in lines)
    assert lines[0] != "swapped wrist_flex to feetech.sts3250" and not any(
        "mdl." in ln or "dev." in ln for ln in lines
    )


def test_a_fit_tops_the_shoulder_and_too_weak_ranks_last():
    pv = preview(load_contract(ARM.parents[1] / "servo-bench.yaml"), "shoulder")
    assert pv.candidates[0].status == "fit"
    statuses = [c.status for c in pv.candidates]  # a bus or supply change outranks fit vs tight
    assert statuses == sorted(statuses, key=lambda s: s == "too weak")


def test_a_note_says_when_no_candidate_runs_on_the_supply():
    c = load_contract(BENCH)
    c.topology.rails[0].voltage.value = 100
    assert "none of these servos runs on your 100 V supply" in " ".join(preview(c, "wrist").notes)


def test_unknown_joint_lists_the_joints(arm):
    with pytest.raises(ValueError, match="joints: gripper, wrist_roll"):
        preview(load_contract(arm), "elbowz")


def test_use_repeats_the_supply_flag_last(tmp_path):
    lock = tmp_path / "lockwire.yaml"
    lock.write_text(BENCH.read_text())
    out = CliRunner().invoke(app, ["swap", str(lock), "wrist", "--use", "feetech.sts3215"])
    assert out.exit_code == 0, out.output
    assert out.output.strip().splitlines()[-1].startswith("warning: feetech.sts3215:")
    assert "needs at least 6 V" in out.output.splitlines()[-1]
