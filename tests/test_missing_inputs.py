"""The block's own instruction, followed as printed with the placeholders filled, removes its item."""

import json
import re
import shlex
import shutil

import pytest
from typer.testing import CliRunner

from lockwire.api_payload import CheckPayload
from lockwire.cli import app
from lockwire.missing import missing_inputs, missing_lines
from lockwire.report import Finding

runner = CliRunner()
INERTIAL = '<inertial><mass value="1"/><inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="2"/></inertial>'
FILLED = {"<where this number comes from>": "datasheet p1", "<your name>": "Ada Lovelace"}


def urdf(mass=True, effort=5, mesh=False):
    geom = '<mesh filename="meshes/a.stl"/>' if mesh else '<box size="1 1 1"/>'
    link = f'<link name="{{}}">{INERTIAL if mass else ""}<collision><geometry>{geom}</geometry></collision></link>'
    joint = '<joint name="{}" type="revolute"><parent link="{}"/><child link="{}"/><axis xyz="0 1 0"/>'
    joint += f'<limit lower="-1" upper="1" effort="{effort}" velocity="1"/></joint>'
    body = "".join(link.format(n) for n in ("base", "arm", "tip"))
    return f'<robot name="r">{body}{joint.format("j1", "base", "arm")}{joint.format("j2", "arm", "tip")}</robot>'


def check(args, out):
    runner.invoke(app, ["check", *args, "--json", str(out)], catch_exceptions=False)
    return json.loads(out.read_text())


def follow(how, values=()):
    """Run each command of `how` through the CLI, placeholders filled; `values` fill the value slots."""
    values = list(values)
    for step in how.split(", then the same")[0].split(" && "):
        argv = [FILLED.get(a) or (values.pop(0) if re.fullmatch(r"<.*>", a) else a) for a in shlex.split(step, comments=True)[1:]]
        result = runner.invoke(app, argv, catch_exceptions=False)
        assert result.exit_code == 0, (argv, result.output)


def starter(name):
    def make(work, monkeypatch):
        monkeypatch.chdir(work)
        runner.invoke(app, ["start", "--example", name])
        return ["lockwire.yaml"]

    return make


def bare(text):
    def make(work, monkeypatch):
        monkeypatch.chdir(work)
        (work / "meshes").mkdir()
        (work / "r.urdf").write_text(text)
        return ["r.urdf"]

    return make


def locked_model(work, monkeypatch):
    """A lock that records r.urdf, with r.urdf gone."""
    monkeypatch.chdir(work)
    (work / "r.urdf").write_text(urdf())
    runner.invoke(app, ["start", "r.urdf", "--out", "lock.yaml"], catch_exceptions=False)
    shutil.move(work / "r.urdf", work / "gone.urdf")
    return ["lock.yaml"]


def add_contacts(work):
    rows = ", ".join(f"{{link: {n}_FOOT, xyz: [0, 0, 0]}}" for n in ("FL", "FR", "HL", "HR"))
    (work / "lockwire.yaml").open("a").write(f"contacts: [{rows}]\n")


EDITS = {  # the file edit each instruction describes, done on the scratch copy
    "link_masses": lambda w: (w / "r.urdf").write_text(urdf()),
    "effort_limits": lambda w: (w / "r.urdf").write_text(urdf()),
    "meshes": lambda w: (w / "meshes" / "a.stl").write_text("solid a\nendsolid a\n"),
    "foot_contacts": add_contacts,
    "model_file": lambda w: shutil.move(w / "gone.urdf", w / "r.urdf"),
}
# kind, setup, values for the value slots of the printed command (none for a file edit)
CASES = [
    ("link_masses", bare(urdf(mass=False)), ()),
    ("effort_limits", bare(urdf(effort=0)), ()),
    ("meshes", bare(urdf(mesh=True)), ()),
    ("foot_contacts", starter("quadruped"), ()),
    ("model_file", locked_model, ()),
    ("battery_chemistry", starter("drone"), ("lipo",)),
    ("device_masses", starter("drone"), ("25 g",)),
    ("bus_traffic", starter("drone"), ("50", "8", "50", "8")),
    ("battery_c_rate", starter("humanoid"), ("20",)),
    ("ambient", starter("humanoid"), ("25 C",)),
    ("rail_voltages", starter("rover"), ("5 V",)),
]


@pytest.mark.parametrize(("key", "setup", "values"), CASES, ids=[c[0] for c in CASES])
def test_the_instruction_as_printed_works(key, setup, values, tmp_path, monkeypatch):
    work = tmp_path / "w"
    work.mkdir()
    argv = setup(work, monkeypatch)
    before = check(argv, tmp_path / "a.json")
    CheckPayload.model_validate(before)
    item = next(i for i in before["missing_inputs"]["items"] if i["key"] == key)
    EDITS[key](work) if key in EDITS else follow(item["how"], values)
    after = check(argv, tmp_path / "b.json")
    assert key not in [i["key"] for i in (after.get("missing_inputs") or {"items": []})["items"]]
    assert after["counts"]["violation"] <= before["counts"]["violation"]


def frame_urdf(mesh):
    """base_link is a bare frame; arm has shapes and no mass; the mesh file exists; effort 0."""
    arm = f'<link name="arm"><visual><geometry><mesh filename="{mesh.as_uri()}"/></geometry></visual></link>'
    box = f'<link name="b">{INERTIAL}</link><link name="base_link"/>'
    fixed = '<joint name="j0" type="fixed"><parent link="base_link"/><child link="arm"/></joint>'
    rev = '<joint name="j1" type="revolute"><parent link="arm"/><child link="b"/><axis xyz="0 1 0"/>'
    return f'<robot name="r">{arm}{box}{fixed}{rev}<limit lower="-1" upper="1" effort="0" velocity="1"/></joint></robot>'


def test_no_instruction_to_edit_a_downloaded_model_or_to_fix_what_is_fine(tmp_path, monkeypatch):
    from test_cli import _fake_robot_descriptions

    mesh = tmp_path / "a.stl"
    mesh.write_text("solid a\nendsolid a\n")
    (tmp_path / "cache").mkdir()
    cached = tmp_path / "cache" / "ur5e.urdf"
    cached.write_text(frame_urdf(mesh))
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_robot_descriptions(monkeypatch, cached)
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.chdir(work)
    assert not (check(["ur5e"], tmp_path / "a.json").get("missing_inputs") or {}).get("items")
    # a file the person owns: the frame is not a missing mass, and a mesh already on disk is not missing
    (work / "r.urdf").write_text(frame_urdf(mesh))
    items = {i["key"]: i for i in check(["r.urdf"], tmp_path / "b.json")["missing_inputs"]["items"]}
    assert "arm" in items["link_masses"]["how"] and "base_link" not in items["link_masses"]["how"]
    assert "meshes" not in items


def test_the_mesh_instruction_names_mesh_files_and_works_as_told(tmp_path, monkeypatch):
    own = tmp_path / "ws" / "own"
    own.mkdir(parents=True)
    (own / "package.xml").write_text("<package><name>own</name></package>")
    mesh = '<mesh filename="package://other/meshes/base.dae"/>'
    (own / "r.urdf").write_text(f'<robot name="r"><link name="a"><visual><geometry>{mesh}</geometry></visual></link></robot>')
    how = next(i["how"] for i in check([str(own / "r.urdf")], tmp_path / "a.json")["missing_inputs"]["items"] if i["key"] == "meshes")
    assert "base.dae" in how and "package.xml" in how and "other/package.xml" not in how
    (tmp_path / "ws" / "other" / "meshes").mkdir(parents=True)
    (tmp_path / "ws" / "other" / "package.xml").write_text("<package><name>other</name></package>")
    (tmp_path / "ws" / "other" / "meshes" / "base.dae").write_text("x")
    after = check([str(own / "r.urdf")], tmp_path / "b.json").get("missing_inputs") or {"items": []}
    assert "meshes" not in [i["key"] for i in after["items"]]


def test_the_rail_line_asks_for_the_measured_voltage_and_names_the_feed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["start", "--example", "rover"])
    how = next(i["how"] for i in check(["lockwire.yaml"], tmp_path / "a.json")["missing_inputs"]["items"] if i["key"] == "rail_voltages")
    assert "# use the rail's real measured voltage; it is fed from 11.1 V" in how and "\n" not in how


def test_the_foot_line_names_every_foot_up_to_six(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["start", "--example", "quadruped"])
    how = next(i["how"] for i in check(["lockwire.yaml"], tmp_path / "a.json")["missing_inputs"]["items"] if i["key"] == "foot_contacts")
    assert "(FL_FOOT, FR_FOOT, HL_FOOT and HR_FOOT)" in how
    legs = "".join(f'<link name="leg{n}"/><joint name="j{n}" type="fixed"><parent link="b"/><child link="leg{n}"/></joint>' for n in range(8))
    (tmp_path / "e.urdf").write_text(f'<robot name="r"><link name="b">{INERTIAL}</link>{legs}</robot>')
    how = check(["e.urdf"], tmp_path / "b.json")["missing_inputs"]["items"][0]["how"]
    assert "(leg0, leg1, leg2, leg3, leg4, leg5 and 2 more)" in how


def test_the_not_declared_line_commands_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.urdf").write_text(urdf(mass=False))
    nd = check(["r.urdf"], tmp_path / "a.json")["missing_inputs"]["not_declared"]
    follow(nd["first"])
    rail = missing_lines(check(["r.urdf"], tmp_path / "c.json")["missing_inputs"])[-1].split("Start a rail: ")[1]
    follow(rail.removesuffix("."), ("main", "60 W"))  # the printed command, whole
    assert "main" in (tmp_path / "lockwire.yaml").read_text()
    follow(nd["start"][0], ("50 Wh",))
    assert "not_declared" not in (check(["lockwire.yaml"], tmp_path / "b.json").get("missing_inputs") or {})


def test_a_command_pasted_unchanged_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["start", "--example", "drone"])
    how = check(["lockwire.yaml"], tmp_path / "a.json")["missing_inputs"]["items"][0]["how"]
    assert runner.invoke(app, shlex.split(how)[1:]).exit_code != 0


def test_only_gaps_are_listed_and_the_block_caps_at_three():
    odd = Finding(severity="not_scored", quantity="x/odd", observed="something new", expected="e", action="do it")
    warn = Finding(severity="warning", quantity="x/warn", observed="not a gap", expected="e", action="a")
    assert missing_inputs([odd, warn]) is None
    item = {"name": "n", "covers": "c", "unlocks": "u", "how": "h"}
    block = missing_lines({"items": [item] * 4}, top=3)
    assert sum(line.startswith("    How:") for line in block) == 3 and "  and 1 more: run with --all" in block


def test_anymal_link_masses_count_names_the_bare_frame_it_leaves_out(tmp_path, monkeypatch):
    """ANYmal: check warns two links have no mass (base and lidar_parent); lidar_parent is a bare
    frame, so it is said as left out instead of the count reading 1 against 2 warnings."""
    import shutil
    from pathlib import Path

    from typer.testing import CliRunner

    from lockwire.cli import app

    shutil.copy(Path(__file__).parent / "fixtures" / "anymal_d.urdf", tmp_path)
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["start", "anymal_d.urdf"]).exit_code == 0
    out = CliRunner().invoke(app, ["check"]).output
    assert "link masses (1 link, plus 1 bare frame left out: lidar_parent):" in out
