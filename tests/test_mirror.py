"""Mirror links and joints across a plane (axis pseudo-vector, params, refusals)."""

from __future__ import annotations
import math
import pytest
import yaml
from typer.testing import CliRunner
from lockwire.cli import app
from lockwire.edit import EditRefusal, _rot, _rpy_matrix, add_entry
from lockwire.schema import load_contract

_BLANK = 'schema_version: "0.7"\nrobot:\n  name: r\n'
_M = [1, -1, 1]  # the xz mirror
_SHOULDER = {"type": "revolute", "origin": {"xyz": [0.1, 0.2, 0.3], "rpy": [0.3, 0.1, 0.5]}}
_ELBOW = {"type": "revolute", "origin": {"xyz": [0.1, "y_off", 0.0], "rpy": [0.0, 0.2, 0.4]}}


def _add(path, kind, fields):
    add_entry(path, kind, fields, source="s", witness="w1")


def _arm(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text(_BLANK)
    _add(path, "param", {"name": "y_off", "value": 0.05, "unit": "m"})
    _add(path, "link", {"name": "base", "mass": "1 kg"})
    box = {"kind": "box", "size": [0.1, 0.02, 0.02], "origin": {"xyz": [0.05, 0.01, 0]}}
    for name in ("left-upper", "left-fore"):
        _add(path, "link", {"name": name, "mass": "0.5 kg", "geometry": [box]})
    ends = (("left-shoulder", "base", "left-upper", [0, 0, 1]), ("left-elbow", "left-upper", "left-fore", [1, 0, 1]))  # fmt: skip
    for (name, parent, child, axis), spec in zip(ends, (_SHOULDER, _ELBOW)):
        _add(path, "joint", {"name": name, "parent": parent, "child": child, "axis": axis, **spec})
    raw = yaml.safe_load(path.read_text())
    for link in raw["model"]["links"]:
        if link["name"] == "left-fore":
            link["com"] = [0.04, 0.03, 0.01]
            link["inertia"] = {"moments_kgm2": [1e-4, 2e-4, 2e-4], "axes": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}  # fmt: skip
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def _mirror(path, **more):
    fields = {"of": "left-upper", "plane": "xz", "rename": {"from": "left", "to": "right"}}
    _add(path, "mirror", {**fields, **more})


def _raw(path):
    model = yaml.safe_load(path.read_text())["model"]
    return {x["name"]: x for x in model["links"]}, {x["name"]: x for x in model["joints"]}


def test_mirror_negates_origin_pose_axis_com_and_swaps_names(tmp_path):
    path = _arm(tmp_path)
    _mirror(path)
    links, joints = _raw(path)
    assert set(links) >= {"right-upper", "right-fore"}
    shoulder, elbow = joints["right-shoulder"], joints["right-elbow"]
    assert shoulder["origin"]["xyz"] == [0.1, -0.2, 0.3]
    assert shoulder["origin"]["rpy"] == pytest.approx([-0.3, 0.1, -0.5])
    assert shoulder["axis"] == [0, 0, -1] and elbow["axis"] == [-1, 0, -1]  # pseudo-vector
    assert (shoulder["parent"], elbow["parent"]) == ("base", "right-upper")
    assert links["right-fore"]["com"] == [0.04, -0.03, 0.01]
    assert links["right-fore"]["geometry"][0]["origin"]["xyz"] == [0.05, -0.01, 0]
    assert links["right-fore"]["inertia"]["moments_kgm2"] == [1e-4, 2e-4, 2e-4]


def test_mirrored_param_name_toggles_sign(tmp_path):
    path = _arm(tmp_path)
    _mirror(path)
    assert _raw(path)[1]["right-elbow"]["origin"]["xyz"] == [0.1, "-y_off", 0.0]


def _fk(contract, chain, qs):
    """Child frame of the last joint in base coordinates: (R, t) of the whole chain."""
    joints = {j.name: j for j in contract.model.joints}
    r, t = [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [0.0, 0.0, 0.0]
    for name, q in zip(chain, qs):
        j = joints[name]
        step = _mul(_rpy_matrix(list(j.origin.rpy)), _rot(list(j.axis), q))
        t = [t[i] + sum(r[i][k] * j.origin.xyz[k] for k in range(3)) for i in range(3)]
        r = _mul(r, step)
    return r, t


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def test_mirrored_arm_moved_by_q_is_the_mirror_image_of_the_original_moved_by_q(tmp_path):
    path = _arm(tmp_path)
    _mirror(path)
    contract = load_contract(path)
    qs = (0.7, -0.4)
    r0, t0 = _fk(contract, ("left-shoulder", "left-elbow"), qs)
    r1, t1 = _fk(contract, ("right-shoulder", "right-elbow"), qs)
    assert t1 == pytest.approx([a * m for a, m in zip(t0, _M)])
    for i in range(3):  # R1 = M R0 M
        for j in range(3):
            assert r1[i][j] == pytest.approx(_M[i] * r0[i][j] * _M[j])


def test_mirror_without_rename_appends_suffix_and_subtree_false_copies_one(tmp_path):
    path = _arm(tmp_path)
    _add(path, "mirror", {"of": "left-upper", "plane": "xz", "subtree": False})
    links, joints = _raw(path)
    assert "left-upper-mirror" in links and "left-fore-mirror" not in links
    assert "left-shoulder-mirror" in joints


def test_check_runs_on_a_mirrored_lockfile(tmp_path):
    path = _arm(tmp_path)
    _mirror(path)
    result = CliRunner().invoke(app, ["check", str(path)])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code in (0, 1) and "drift" not in result.output.lower()


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({"of": "nope"}, "This robot's model has no link 'nope'."),
        ({"of": "base"}, "Link 'base' has no parent joint to copy; it is the root."),
        ({"rename": {"from": "zzz", "to": "y"}}, "link 'left-upper-mirror' (or its joint) already exists"),  # fmt: skip
    ],
)
def test_mirror_refusals_are_one_exact_line(tmp_path, fields, text):
    path = _arm(tmp_path)
    if "rename" in fields:
        _mirror(path, rename=None)  # the first mirror takes the default names
    with pytest.raises(EditRefusal) as error:
        _mirror(path, **fields)
    assert str(error.value) == text


def test_mirror_refuses_a_mesh_and_an_imported_model(tmp_path):
    path = _arm(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["model"]["links"][1]["geometry"] = [{"kind": "mesh", "mesh": "a.stl"}]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(EditRefusal) as error:
        _mirror(path)
    assert str(error.value) == "link 'left-upper' has a mesh shape; a mesh cannot be mirrored"
    raw["model"]["source"] = "urdf"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(EditRefusal, match="edit that file"):
        _mirror(path)


def test_mirror_math_helper_sanity():
    r = _rpy_matrix([0.3, 0.1, 0.5])
    m = [[1, 0, 0], [0, -1, 0], [0, 0, 1]]
    got = _mul(m, _mul(r, m))
    want = _rpy_matrix([-0.3, 0.1, -0.5])
    assert all(math.isclose(got[i][j], want[i][j], abs_tol=1e-12) for i in range(3) for j in range(3))  # fmt: skip
