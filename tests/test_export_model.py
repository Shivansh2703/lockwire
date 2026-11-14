"""export_model: a model written as URDF, SDF or MJCF reads back equal with the repo's readers."""

from pathlib import Path

import xml.etree.ElementTree as ET

import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.schema import load_contract
from lockwire.export_model import (
    SUFFIX,
    _primitive,
    _stl_bytes,
    default_out,
    render,
    write,
    write_mesh,
    write_stl,
)
from lockwire.mesh_simple import read_stl
from lockwire.schema import Shape
from lockwire.statics import link_frames
from lockwire.mjcf import parse_mjcf
from lockwire.sdf import parse_sdf
from lockwire.urdf import parse_urdf

from conftest import FIXTURES

SERVO = FIXTURES / "servo_arm.urdf"
READ = {"urdf": parse_urdf, "sdf": parse_sdf, "mjcf": parse_mjcf}
RICH = """<robot name="rich">
  <link name="base"><inertial><origin xyz="0.01 0.02 0.03"/><mass value="2.5"/>
    <inertia ixx="0.02" ixy="0.003" ixz="-0.002" iyy="0.03" iyz="0.004" izz="0.05"/></inertial></link>
  <link name="arm"><inertial><origin xyz="0 0 0.1"/><mass value="0.7"/>
    <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.002" iyz="0" izz="0.003"/></inertial></link>
  <link name="hand"/>
  <joint name="j1" type="revolute"><parent link="base"/><child link="arm"/>
    <origin xyz="0.1 -0.2 0.3" rpy="0.2 -0.4 0.7"/><axis xyz="0 0 1"/>
    <limit effort="5" velocity="2" lower="-1" upper="2"/></joint>
  <joint name="j2" type="prismatic"><parent link="arm"/><child link="hand"/>
    <origin xyz="0 0 0.5" rpy="0 0.3 0"/><axis xyz="1 0 0"/>
    <limit effort="9" velocity="1" lower="0" upper="0.4"/></joint>
</robot>"""


def _same(a, b, fmt):
    """Links and joints of two ModelRecords equal within 1e-9 (MJCF adds its own world)."""
    skip = {"world"} if fmt == "mjcf" else set()
    ours = {k.name: k for k in a.links}
    theirs = {k.name: k for k in b.links if k.name not in skip}
    assert ours.keys() == theirs.keys()
    for name, x in ours.items():
        y = theirs[name]
        assert (x.mass is None) == (y.mass is None)
        if x.mass is None:
            continue
        assert x.mass.base() == pytest.approx(y.mass.base(), abs=1e-9)
        np.testing.assert_allclose(x.com, y.com, atol=1e-9)
        tensors = [
            np.array(k.inertia.axes).T @ np.diag(k.inertia.moments_kgm2) @ np.array(k.inertia.axes)
            for k in (x, y)
        ]
        np.testing.assert_allclose(tensors[0], tensors[1], atol=1e-9)
    joints = {j.name: j for j in b.joints if j.parent not in skip}
    assert {j.name for j in a.joints if j.parent not in skip} == joints.keys()
    for x in a.joints:
        y = joints[x.name]
        assert (x.type, x.parent, x.child, x.axis) == (y.type, y.parent, y.child, y.axis)
        np.testing.assert_allclose(x.origin.xyz, y.origin.xyz, atol=1e-9)
        np.testing.assert_allclose(x.origin.rpy, y.origin.rpy, atol=1e-9)
        keys = ("lower", "upper") if fmt == "mjcf" else ("lower", "upper", "effort", "velocity")
        for key in keys:
            assert getattr(x.limits, key) == pytest.approx(getattr(y.limits, key), abs=1e-9)


@pytest.mark.parametrize("fmt", ["urdf", "sdf", "mjcf"])
@pytest.mark.parametrize("which", ["servo", "rich"])
def test_round_trip(tmp_path, fmt, which):
    src = SERVO
    if which == "rich":
        src = tmp_path / "rich.urdf"
        src.write_text(RICH)
    model = parse_urdf(src)
    out = write(model, fmt, tmp_path / f"out{SUFFIX[fmt]}")
    _same(model, READ[fmt](out), fmt)


def test_refuses_to_overwrite_without_force(tmp_path):
    model = parse_urdf(SERVO)
    out = tmp_path / "robot.urdf"
    out.write_text("mine")
    with pytest.raises(FileExistsError, match="already exists"):
        write(model, "urdf", out)
    assert out.read_text() == "mine"
    write(model, "urdf", out, force=True)
    assert "<robot" in out.read_text()


def test_a_joint_the_format_lacks_is_named(tmp_path):
    model = parse_urdf(SERVO)
    model.joints[0].type = "planar"
    with pytest.raises(ValueError, match="joint 'shoulder' is a planar joint"):
        render(model, "mjcf", "x")


def test_default_out_is_beside_the_lockfile(tmp_path):
    assert default_out(tmp_path / "lockwire.yaml", "sdf") == tmp_path / "lockwire.sdf"


def _verdict(run, path, flags):
    res = run(app, ["check", str(path), *flags])
    line = next(x for x in res.output.splitlines() if x.startswith(("GO", "NO-GO", "NOT SCORED")))
    return line, res.exit_code


# MJCF with no flags is covered by test_mjcf_export_scores_the_same_checks_and_exit_as_its_urdf.
@pytest.mark.parametrize(
    ("fmt", "flags"),
    [(f, m) for f in ("urdf", "sdf") for m in ([], ["--mass", "0.65 kg"], ["--mass", "1 kg"])]
    + [("mjcf", ["--mass", "0.65 kg"]), ("mjcf", ["--mass", "1 kg"])],
)
def test_check_gives_the_same_verdict_and_exit_code_as_the_original(tmp_path, fmt, flags):
    out = write(parse_urdf(SERVO), fmt, tmp_path / f"out{SUFFIX[fmt]}")
    mine, theirs = (
        _verdict(CliRunner().invoke, out, flags),
        _verdict(CliRunner().invoke, SERVO, flags),
    )
    if fmt == "mjcf":  # wording differs ("GO with notes"); only the exit code holds
        assert mine[1] == theirs[1]
    else:
        assert mine == theirs
    assert (mine[1] == 1) == (flags[-1:] == ["1 kg"])


def test_export_never_writes_into_the_source_model(tmp_path):
    src = tmp_path / "servo_arm.urdf"
    src.write_text(SERVO.read_text())
    model = parse_urdf(src)
    with pytest.raises(ValueError, match="source model"):
        write(model, "urdf", src, force=True)
    assert src.read_text() == SERVO.read_text()


def test_export_refuses_a_cwd_relative_source_from_another_directory(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    src = tmp_path / "models" / "servo_arm.urdf"
    src.write_text(SERVO.read_text())
    monkeypatch.chdir(tmp_path)
    model = parse_urdf(Path("models/servo_arm.urdf"))
    with pytest.raises(ValueError, match="source model"):
        write(model, "urdf", src, force=True)
    assert src.read_text() == SERVO.read_text()


def test_export_refuses_a_contract_relative_source_given_its_folder(tmp_path, monkeypatch):
    (tmp_path / "proj" / "models").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    src = tmp_path / "proj" / "models" / "servo_arm.urdf"
    src.write_text(SERVO.read_text())
    model = parse_urdf(src).model_copy(update={"source": "models/servo_arm.urdf"})
    monkeypatch.chdir(tmp_path / "elsewhere")
    for out in (src, Path("../proj/models/servo_arm.urdf")):
        with pytest.raises(ValueError, match="source model"):
            write(model, "urdf", out, force=True, source_root=tmp_path / "proj")
    assert src.read_text() == SERVO.read_text()


def test_mcp_tool_writes_beside_the_contract_and_refuses_a_second_time(tmp_path):
    from test_mcp_server import _call

    lock = tmp_path / "lockwire.yaml"
    assert CliRunner().invoke(app, ["start", str(SERVO), "--out", str(lock)]).exit_code == 0
    args = {"contract": str(lock)}
    first = _call("export_model", args)
    assert first.structured_content["written"] == str(tmp_path / "lockwire.urdf")
    assert _call("export_model", args).is_error


def test_mjcf_keeps_the_effort_limit_as_an_actuator_force_range(tmp_path):
    from xml.etree import ElementTree as ET

    from lockwire.check_sim import _read_mjcf

    out = write(parse_urdf(SERVO), "mjcf", tmp_path / "out.xml")
    joints = _read_mjcf(ET.parse(out).getroot())[1]
    assert (joints["shoulder"].effort, joints["wrist"].effort) == (10.0, 1.0)


def test_roundtrip_designed_robot_checks_the_same_as_its_urdf(tmp_path, monkeypatch):
    """A robot that lives in the lockfile: lockwire.yaml and its exported URDF give one verdict.

    The joint's zero effort limit is a warning only while the limit survives the export."""
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["start", str(SERVO), "--force"]).exit_code == 0
    data = yaml.safe_load(Path("lockwire.yaml").read_text())
    data["model"] |= {"source": "lockfile"}
    data["model"]["provenance"]["source"] = "lockfile"
    data["model"]["joints"][0]["limits"]["effort"] = 0
    Path("lockwire.yaml").write_text(yaml.safe_dump(data))
    urdf = write(load_contract(Path("lockwire.yaml")).model, "urdf", tmp_path / "robot.urdf")
    mine = _verdict(CliRunner().invoke, urdf, [])
    assert mine == _verdict(CliRunner().invoke, Path("lockwire.yaml"), [])
    assert "1 warning(s)" in mine[0]


ROVER = FIXTURES.parent.parent / "src" / "lockwire" / "examples" / "rover-param"
HEXAPOD = FIXTURES.parent.parent / "src" / "lockwire" / "examples" / "hexapod-param"


def _model(folder):
    return load_contract(folder / "contract.yaml").model


def _tris(path):
    return np.array(read_stl(path)[0]).reshape(-1, 3, 3)


def _box(tris):
    pts = tris.reshape(-1, 3)
    return pts.min(axis=0), pts.max(axis=0)


def _volume(tris):
    return abs(np.einsum("ij,ij->", tris[:, 0], np.cross(tris[:, 1], tris[:, 2]))) / 6


def test_a_box_link_is_twelve_triangles_with_the_right_bounding_box(tmp_path):
    write_stl(_model(ROVER), tmp_path)
    tris = _tris(tmp_path / "chassis.stl")
    assert len(tris) == 12
    lo, hi = _box(tris)
    np.testing.assert_allclose([*lo, *hi], [-0.2, -0.15, -0.005, 0.2, 0.15, 0.005], atol=1e-6)


def test_a_cylinder_volume_is_within_one_percent_of_pi_r_squared_l(tmp_path):
    model = _model(ROVER)
    write_stl(model, tmp_path)
    shape = model.links[1].geometry[0]
    exact = np.pi * shape.radius**2 * shape.length
    assert _volume(_tris(tmp_path / f"{model.links[1].name}.stl")) == pytest.approx(exact, rel=0.01)


def test_the_assembly_bounding_box_is_the_union_of_the_placed_parts(tmp_path):
    model = _model(ROVER)
    write_stl(model, tmp_path)
    frames = link_frames(model)
    placed = [
        _tris(tmp_path / f"{k.name}.stl") @ frames[k.name][1].T + frames[k.name][0]
        for k in model.links
    ]
    lo, hi = _box(np.concatenate(placed))
    got = _box(_tris(tmp_path / "assembly.stl"))
    np.testing.assert_allclose([*got[0], *got[1]], [*lo, *hi], atol=1e-5)
    assert got[1][1] - got[0][1] > 0.3  # the wheels stand outside the chassis' own width


@pytest.mark.parametrize(
    "shape",
    [
        Shape(kind="box", size=(1, 2, 3)),
        Shape(kind="cylinder", radius=1, length=2),
        Shape(kind="sphere", radius=1),
    ],
)
@pytest.mark.parametrize("n", [3, 32])
def test_every_primitive_is_watertight(shape, n):
    edges = {}
    for tri in np.round(_primitive(shape, n), 9):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            key = frozenset((tuple(tri[a]), tuple(tri[b])))
            edges[key] = edges.get(key, 0) + 1
    assert set(edges.values()) == {2}
    assert _volume(_primitive(shape, n)) > 0


@pytest.mark.parametrize("folder", [ROVER, HEXAPOD])
def test_a_round_trip_writes_one_file_per_shaped_link(tmp_path, folder):
    model = _model(folder)
    written, notes = write_stl(model, tmp_path)
    shaped = {f"{k.name}.stl" for k in model.links if k.geometry}
    assert {p.name for p in written} == shaped | {"assembly.stl"}
    assert {p.name for p in tmp_path.iterdir()} == {p.name for p in written}


def test_mm_scales_by_a_thousand_and_other_units_are_refused(tmp_path):
    write_stl(_model(ROVER), tmp_path / "mm", units="mm")
    np.testing.assert_allclose(_box(_tris(tmp_path / "mm" / "chassis.stl"))[1][0], 200, atol=1e-3)
    with pytest.raises(ValueError, match="m or mm"):
        write_stl(_model(ROVER), tmp_path / "x", units="in")


def test_stl_refuses_to_overwrite_without_force_and_writes_nothing_new(tmp_path):
    (tmp_path / "assembly.stl").write_bytes(b"mine")
    with pytest.raises(FileExistsError, match="force"):
        write_stl(_model(ROVER), tmp_path)
    assert (tmp_path / "assembly.stl").read_bytes() == b"mine"
    assert not (tmp_path / "chassis.stl").exists()
    write_stl(_model(ROVER), tmp_path, force=True)
    assert (tmp_path / "assembly.stl").read_bytes() != b"mine"


def test_stl_never_writes_into_the_source_models_folder(tmp_path):
    model = _model(ROVER).model_copy(update={"source": "robot.urdf"})
    with pytest.raises(ValueError, match="holds the source model"):
        write_stl(model, tmp_path, source_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_stl_mesh_is_placed_other_meshes_and_empty_links_are_listed(tmp_path):
    (tmp_path / "part.stl").write_bytes(
        _stl_bytes(_primitive(Shape(kind="box", size=(1, 1, 1)), 3), 1)
    )
    (tmp_path / "arm.dae").write_text("<COLLADA/>")
    model = _model(ROVER)
    first, second = model.links[0], model.links[1]
    first.geometry = [
        Shape(kind="mesh", mesh="part.stl", origin={"xyz": (5, 0, 0)}),
        Shape(kind="mesh", mesh="arm.dae"),
        Shape(kind="mesh", mesh="gone.stl"),
    ]
    second.geometry = None
    out = tmp_path / "out"
    _, notes = write_stl(model, out, source_root=tmp_path)
    assert notes == [
        "not exported: arm.dae",
        "not exported: gone.stl (not found)",
        f"{second.name}: has no shape",
    ]
    lo, hi = _box(_tris(out / f"{first.name}.stl"))
    np.testing.assert_allclose([lo[0], hi[0]], [4.5, 5.5], atol=1e-6)
    assert not (out / f"{second.name}.stl").exists()


def test_step_is_refused_in_one_line(tmp_path):
    with pytest.raises(ValueError, match="STEP export is not built"):
        render(_model(ROVER), "step", "x")
    with pytest.raises(ValueError, match="STEP export is not built"):
        default_out(tmp_path / "lockwire.yaml", "step")


def test_mcp_export_model_writes_the_stl_folder_beside_the_contract(tmp_path):
    from test_mcp_server import _call

    lock = tmp_path / "lockwire.yaml"
    lock.write_text((ROVER / "contract.yaml").read_text())
    args = {"contract": str(lock), "format": "stl"}
    first = _call("export_model", args).structured_content
    assert first["written"] == str(tmp_path / "lockwire-stl")
    assert (tmp_path / "lockwire-stl" / "assembly.stl").is_file()
    assert _call("export_model", args).is_error


def _obj(path):
    rows = [ln.split() for ln in path.read_text().splitlines()]
    v = np.array([[float(x) for x in r[1:]] for r in rows if r[0] == "v"])
    f = np.array([[int(x) - 1 for x in r[1:]] for r in rows if r[0] == "f"])
    return v[f]


def _dae(path):
    root = ET.parse(path).getroot()
    q = lambda tag: root.iter("{http://www.collada.org/2005/11/COLLADASchema}" + tag)  # noqa: E731
    return root, int(next(q("triangles")).get("count")), next(q("float_array")).text


def test_obj_and_dae_have_the_stl_triangle_count_for_every_file(tmp_path):
    model = _model(ROVER)
    for fmt in ("stl", "obj", "dae"):
        write_mesh(model, tmp_path / fmt, fmt)
    for stl in (tmp_path / "stl").glob("*.stl"):
        n = len(_tris(stl))
        assert len(_obj(tmp_path / "obj" / (stl.stem + ".obj"))) == n
        assert _dae(tmp_path / "dae" / (stl.stem + ".dae"))[1] == n


def test_obj_and_dae_assemblies_sit_where_the_stl_assembly_does(tmp_path):
    model = _model(ROVER)
    for fmt in ("stl", "obj", "dae"):
        write_mesh(model, tmp_path / fmt, fmt)
    frames = link_frames(model)  # placed from the frames, not from the code under test
    want = _box(np.concatenate([
        _tris(tmp_path / "stl" / f"{k.name}.stl") @ frames[k.name][1].T + frames[k.name][0]
        for k in model.links
    ]))
    dae = np.array(_dae(tmp_path / "dae" / "assembly.dae")[2].split(), dtype=float)
    for got in (_box(_obj(tmp_path / "obj" / "assembly.obj")), _box(dae.reshape(-1, 3, 3))):
        np.testing.assert_allclose([*got[0], *got[1]], [*want[0], *want[1]], atol=1e-5)


def test_dae_unit_element_follows_the_chosen_units(tmp_path):
    write_mesh(_model(ROVER), tmp_path, "dae", units="mm")
    unit = next(_dae(tmp_path / "chassis.dae")[0].iter("{http://www.collada.org/2005/11/COLLADASchema}unit"))
    assert (unit.get("name"), float(unit.get("meter"))) == ("millimeter", 0.001)


def test_obj_indices_are_one_based_and_a_bad_format_is_refused(tmp_path):
    write_mesh(_model(ROVER), tmp_path, "obj")
    assert "f 1 2 3" in (tmp_path / "chassis.obj").read_text().splitlines()
    with pytest.raises(ValueError, match="stl, obj or dae"):
        write_mesh(_model(ROVER), tmp_path / "x", "step")


def _mesh_robot(tmp_path):
    (tmp_path / "meshes").mkdir()
    mesh = tmp_path / "meshes" / "base.stl"
    mesh.write_bytes(_stl_bytes(_primitive(Shape(kind="box", size=(0.1, 0.1, 0.1)), 8), 1.0))
    urdf = tmp_path / "r.urdf"
    urdf.write_text(
        '<robot name="r"><link name="base"><visual><geometry><mesh filename="meshes/base.stl"/>'
        "</geometry></visual></link></robot>"
    )
    model = parse_urdf(urdf)
    model.links[0].geometry = [Shape(kind="box", size=(0.1, 0.1, 0.1))]
    return model, mesh


def test_mesh_export_never_replaces_the_models_own_mesh(tmp_path):
    model, mesh = _mesh_robot(tmp_path)
    before = mesh.read_bytes()
    with pytest.raises(ValueError, match="one of your model's own files"):
        write_mesh(model, tmp_path / "meshes", "stl", force=True, source_root=tmp_path)
    assert mesh.read_bytes() == before
    link = tmp_path / "out"
    link.symlink_to(tmp_path / "meshes")
    with pytest.raises(ValueError, match="one of your model's own files"):
        write_mesh(model, link, "stl", force=True, source_root=tmp_path)
    assert mesh.read_bytes() == before


def test_urdf_export_never_replaces_the_models_own_mesh_through_a_symlink(tmp_path):
    model, mesh = _mesh_robot(tmp_path)
    before = mesh.read_bytes()
    alias = tmp_path / "alias.urdf"
    alias.symlink_to(mesh)
    with pytest.raises(ValueError, match="one of your model's own files"):
        write(model, "urdf", alias, force=True, source_root=tmp_path)
    assert mesh.read_bytes() == before


def test_a_continuous_joint_exports_to_sdf_as_continuous_and_still_counts_as_moving(tmp_path):
    """Rover: the SDF export was a limitless revolute, read back as 0 moving joints (NO-GO, dof 0 vs 2)."""
    contract = ROVER.parent / "rover" / "contract.yaml"
    out = write(load_contract(contract).model, "sdf", tmp_path / "rover.sdf")
    assert 'type="continuous"' in out.read_text()
    mine = _verdict(CliRunner().invoke, out, [])
    assert mine[1] == 0
    assert mine[0].startswith("GO with notes")


def test_an_sdf_link_with_mass_and_no_inertia_gets_the_urdf_no_inertia_warning(tmp_path):
    out = write(load_contract(ROVER.parent / "rover" / "contract.yaml").model, "sdf", tmp_path / "r.sdf")
    res = CliRunner().invoke(app, ["check", str(out), "--all"])
    assert "no_inertia" in res.output


EXAMPLES = FIXTURES.parent.parent / "src" / "lockwire" / "examples"


def _mass_and_moving(model):
    return (
        round(sum(k.mass.base() for k in model.links if k.mass), 9),
        sum(j.type != "fixed" for j in model.joints),
    )


@pytest.mark.parametrize("example", ["arm-power", "hexapod-param", "rover-param"])
def test_mjcf_of_a_shaped_example_derives_inertia_and_keeps_mass_and_joints(tmp_path, example):
    model = _model(EXAMPLES / example)
    urdf = write(model, "urdf", tmp_path / "r.urdf")
    out = write(model, "mjcf", tmp_path / "r.xml")
    assert "derived from the link's shapes" in out.read_text()
    assert _mass_and_moving(parse_mjcf(out)) == _mass_and_moving(parse_urdf(urdf))


def test_mjcf_of_a_massed_link_with_no_shapes_says_what_to_add(tmp_path):
    with pytest.raises(ValueError, match="declare the link's inertia, or give it shapes"):
        render(load_contract(EXAMPLES / "arm" / "contract.yaml").model, "mjcf", "x")
