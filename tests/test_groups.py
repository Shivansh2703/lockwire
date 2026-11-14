"""A group is a named set of links and devices; it changes no mass, inertia or joint."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.edit import EditRefusal, add_entry, delete_entry, rename_entry
from lockwire.schema import load_contract, save_contract

W = {"source": "s", "witness": "Maya Chen"}
_DEVICES = """\
families:
  - {id: fam.x, name: X}
device_models:
  - {id: mdl.x, family: fam.x}
devices:
  - {id: dev.cam, model: mdl.x, owner: o}
"""


def _file(tmp_path, version="0.7"):
    path = tmp_path / "lock.yaml"
    path.write_text(f'schema_version: "{version}"\nrobot:\n  name: r\n{_DEVICES}')
    for name in ("base", "arm", "hand"):
        add_entry(path, "link", {"name": name}, **W)
    return path


def _group(path, name, *members):
    return add_entry(path, "group", {"name": name, "members": list(members)}, **W)


def _groups(path):
    return {g.name: g.members for g in load_contract(path).groups or []}


def test_add_then_ungroup_leaves_the_parts_alone(tmp_path):
    path = _file(tmp_path)
    _group(path, "wrist", "arm", "dev.cam")
    assert _groups(path) == {"wrist": ["arm", "dev.cam"]}
    delete_entry(path, "group", "wrist", **W)
    contract = load_contract(path)
    assert contract.groups is None and [x.name for x in contract.model.links] == ["base", "arm", "hand"]
    assert contract.device("dev.cam") is not None


def test_group_names_are_unique(tmp_path):
    path = _file(tmp_path)
    _group(path, "g", "arm")
    with pytest.raises(EditRefusal, match="group 'g' already exists"):
        _group(path, "g", "hand")


def test_a_missing_member_is_refused_by_name(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(EditRefusal, match="names 'nope', which is no link or device"):
        _group(path, "g", "arm", "nope")
    assert load_contract(path).groups is None


def test_a_part_in_a_second_group_is_refused_naming_the_first(tmp_path):
    path = _file(tmp_path)
    _group(path, "first", "arm")
    with pytest.raises(EditRefusal, match="'arm' is already in group 'first'"):
        _group(path, "second", "hand", "arm")


def test_an_empty_group_is_refused(tmp_path):
    with pytest.raises(EditRefusal, match="at least one member"):
        _group(_file(tmp_path), "g")


def test_rename_updates_membership_and_the_group_itself(tmp_path):
    path = _file(tmp_path)
    _group(path, "wrist", "arm", "dev.cam")
    rename_entry(path, "link", "arm", "forearm", **W)
    rename_entry(path, "device", "dev.cam", "dev.eye", **W)
    assert _groups(path) == {"wrist": ["forearm", "dev.eye"]}
    rename_entry(path, "group", "wrist", "hand-set", **W)
    assert list(_groups(path)) == ["hand-set"]


def test_deleting_a_device_removes_it_from_its_group(tmp_path):
    path = _file(tmp_path)
    _group(path, "wrist", "arm", "dev.cam")
    result = delete_entry(path, "device", "dev.cam", **W)
    assert _groups(path) == {"wrist": ["arm"]}
    assert "removed dev.cam from group wrist" in result.lines


def test_deleting_the_last_member_drops_the_group_and_says_so(tmp_path):
    path = _file(tmp_path)
    _group(path, "solo", "hand")
    result = delete_entry(path, "link", "hand", **W)
    assert load_contract(path).groups is None
    assert "group solo had no members left and was deleted" in result.lines


def test_an_edit_that_uses_groups_moves_a_06_file_to_07(tmp_path):
    path = _file(tmp_path, "0.6")
    result = _group(path, "g", "arm")
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records groups" in result.lines


def test_a_06_file_with_groups_is_refused(tmp_path):
    path = _file(tmp_path)
    _group(path, "g", "arm")
    path.write_text(path.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="groups need lockfile format 0.7"):
        load_contract(path)


def test_groups_round_trip_and_an_unknown_group_key_is_refused(tmp_path):
    path = _file(tmp_path)
    _group(path, "g", "arm", "dev.cam")
    first = load_contract(path)
    out = tmp_path / "saved.yaml"
    save_contract(first, out)
    assert load_contract(out).model_dump() == first.model_dump()
    path.write_text(path.read_text().replace("members:", "colour: red\n    members:", 1))
    with pytest.raises(ValueError, match="colour"):
        load_contract(path)


def test_check_output_is_identical_with_and_without_groups(tmp_path):
    path = _file(tmp_path)
    runner = CliRunner()
    before = runner.invoke(app, ["check", str(path)])
    _group(path, "g", "arm", "dev.cam")
    after = runner.invoke(app, ["check", str(path)])
    assert (before.exit_code, before.output) == (after.exit_code, after.output)
