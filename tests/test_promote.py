"""A device already placed on a link becomes a link + joint at exactly that placement."""

import math

import pytest

from lockwire import edit, forces
from lockwire.schema import load_contract
from lockwire.edit import Change, EditRefusal, add_entry, apply_changes

_DEV = "dev.sensor.lidar"
_RPY = [0.0, 0.0, math.pi / 2]
_FIELDS = {"id": _DEV, "model": "m.lidar", "family": "f.lidar", "owner": "me"}


def _set(path, leaf, value):
    change = Change(f"devices/{_DEV}/physical/{leaf}", value, source="datasheet", witness="w1")
    apply_changes(path, [change])


def _placed(tmp_path, *, rpy=True, mass=True):
    path = tmp_path / "lockwire.yaml"
    path.write_text('schema_version: "0.7"\nrobot:\n  name: r\n')
    add_entry(path, "link", {"name": "base", "mass": "1 kg"}, source="s", witness="w1")
    add_entry(path, "device", _FIELDS, source="datasheet lidar.pdf", witness="w1")
    edits = [("mount_frame", '"base"'), ("mount_xyz", "0.1 0 0.05 m")]
    edits += [("mass", "0.2 kg")] if mass else []
    edits += [("mount_rpy", _RPY)] if rpy else []
    for leaf, value in edits:
        _set(path, leaf, value)
    return path


def _promote(path, **over):
    joint = {"type": "revolute", "axis": [0, 0, 1], "limits": {"lower": -1, "upper": 1}}
    fields = {"device": _DEV, "joint": joint}
    return add_entry(path, "promote", {**fields, **over}, source="s", witness="w1")


def _world(contract, link):
    """Origin of ``link``'s frame in the root frame, composed up the joint chain."""
    joints = {j.child: j for j in contract.model.joints}
    point = [0.0] * 3
    while link in joints:
        j = joints[link]
        m = edit._rpy_matrix(list(j.origin.rpy))
        point = [sum(m[r][c] * point[c] for c in range(3)) + j.origin.xyz[r] for r in range(3)]
        link = j.parent
    return point


def test_promote_origin_is_the_placement_and_world_position_is_unchanged(tmp_path):
    path = _placed(tmp_path)
    _promote(path)
    c = load_contract(path)
    joint = c.model.joints[0]
    assert (joint.parent, joint.child, joint.type) == ("base", "lidar", "revolute")
    assert joint.origin.xyz == pytest.approx((0.1, 0.0, 0.05))
    assert joint.origin.rpy == pytest.approx(_RPY)
    phys = c.device(_DEV).physical
    assert phys.mount_frame == "lidar" and [q.value for q in phys.mount_xyz] == [0.0] * 3
    assert list(phys.mount_rpy) == [0.0] * 3
    assert _world(c, "lidar") == pytest.approx([0.1, 0.0, 0.05])
    assert c.model.links[1].mass is None and c.device(_DEV).physical.mass.value == 0.2


def _total_mass(path):
    rows = forces._mass_rows(load_contract(path), "links+devices")
    return sum(r["value"] for r in rows if r["value"] is not None)


def test_promote_leaves_total_mass_unchanged(tmp_path):
    path = _placed(tmp_path)
    before = _total_mass(path)
    _promote(path)
    assert before == pytest.approx(1.2) and _total_mass(path) == pytest.approx(before)


def test_promote_without_rpy_says_so_and_uses_zeros(tmp_path):
    path = _placed(tmp_path, rpy=False, mass=False)
    result = _promote(path)
    c = load_contract(path)
    assert c.model.joints[0].origin.rpy == (0.0, 0.0, 0.0)
    assert "no mount_rpy" in result.lines[1] and c.device(_DEV).physical.mount_rpy is None


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"device": "dev.nope"}, "No device 'dev.nope'."),
        (
            {"joint": {"type": "revolute"}},
            "A revolute joint needs a non-zero axis; the axis is undimensioned.",
        ),
    ],
)
def test_promote_refusals_are_one_line_and_leave_the_file_alone(tmp_path, over, message):
    path = _placed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal) as refused:
        _promote(path, **over)
    assert str(refused.value) == message and path.read_bytes() == before


def test_promote_refuses_unplaced_and_non_model_frame(tmp_path):
    path = _placed(tmp_path)
    _set(path, "mount_frame", '"ghost"')
    with pytest.raises(EditRefusal) as refused:
        _promote(path)
    assert (
        str(refused.value)
        == f"Device {_DEV!r} mounts on 'ghost', which is not a link of this model."
    )
    bare = tmp_path / "b.yaml"
    bare.write_text('schema_version: "0.7"\nrobot:\n  name: r\n')
    add_entry(bare, "link", {"name": "base"}, source="s", witness="w1")
    add_entry(bare, "device", _FIELDS, source="s", witness="w1")
    with pytest.raises(EditRefusal) as refused:
        _promote(bare)
    assert str(refused.value) == f"Device {_DEV!r} is not placed; place it first."
