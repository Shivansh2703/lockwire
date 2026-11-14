"""Format compatibility: every example and lockfile fixture keeps its baseline check result,
and save_contract writes each one byte-identically to the pre-0.7-format goldens."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.schema import load_contract, save_contract

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
VERDICTS = json.loads((FIXTURES / "format_compat_verdicts.json").read_text())
SAVE_SHA = json.loads((FIXTURES / "format_compat_save_sha.json").read_text())
runner = CliRunner()

# The public snapshot removes some fixtures this file lists (name built indirectly so the snapshot's
# drop pass keeps the file). Only the public tree (Pro package absent) may skip a missing one; in the
# private tree a missing fixture still fails its test.
PRO = "lockwire" + "_pro"
PUBLIC = importlib.util.find_spec(PRO) is None


def _gone(rel):
    return PUBLIC and not (ROOT / rel).exists()


def _params(keys):
    skip = pytest.mark.skip(reason="fixture removed from the public tree")
    return [pytest.param(r, marks=skip) if _gone(r) else r for r in sorted(keys)]


def _is_06(rel):
    # a missing file stays in the list so the test body fails on it, unless the public tree lacks it
    path = ROOT / rel
    return not _gone(rel) and (not path.exists() or 'schema_version: "0.6"' in path.read_text())


@pytest.mark.parametrize("rel", _params(VERDICTS))
def test_check_exit_code_and_verdict_match_baseline(rel, tmp_path):
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["check", str(ROOT / rel), "--json", str(out)])
    if PUBLIC and PRO in result.output:  # a Pro-only recording contract: refused without Pro
        pytest.skip("needs the Pro checks")
    doc = json.loads(out.read_text())
    assert [result.exit_code, doc["verdict"]] == VERDICTS[rel], result.output


@pytest.mark.parametrize("rel", _params(SAVE_SHA))
def test_save_contract_output_is_byte_identical(rel, tmp_path):
    out = tmp_path / "saved.yaml"
    save_contract(load_contract(ROOT / rel), out)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == SAVE_SHA[rel]


CURRENT = [r for r in sorted(VERDICTS) if _is_06(r)]


@pytest.mark.parametrize("rel", CURRENT)
def test_upgrade_on_a_06_file_changes_only_the_schema_version_line(rel, tmp_path):
    src, out = tmp_path / "lock.yaml", tmp_path / "new.yaml"
    src.write_bytes((ROOT / rel).read_bytes())
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    result = runner.invoke(
        app, ["upgrade", str(src), "--out", str(out), "--expected-revision", sha]
    )
    assert result.exit_code == 0, result.output
    before, after = src.read_text().splitlines(), out.read_text().splitlines()
    differing = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert differing == [('schema_version: "0.6"', 'schema_version: "0.7"')]
    assert src.read_bytes() == (ROOT / rel).read_bytes()


# ---- WITNESS-OPT: a model block without provenance is a 0.7-only shape.

_OLD = 'schema_version: "0.6"\nrobot:\n  name: r\n'


def _first_link(path, witness):
    from lockwire.edit import add_entry

    return add_entry(path, "link", {"name": "base"}, source="s", witness=witness)


def test_named_edit_on_a_06_file_stays_06_with_its_old_lines_intact(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    result = _first_link(path, "Maya Chen")
    text = path.read_text()
    assert text.startswith(_OLD) and "format moved" not in " ".join(result.lines)
    assert load_contract(path).model.provenance.witness == "Maya Chen"


def test_anonymous_first_link_on_a_06_file_moves_it_to_07_and_says_so(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    result = _first_link(path, None)
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records an anonymous model" in result.lines
    assert load_contract(path).model.provenance.witness is None


def test_a_06_file_with_an_anonymous_model_is_refused(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    _first_link(path, None)
    path.write_text(path.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="needs lockfile format 0.7"):
        load_contract(path)


_DEVICE_06 = """schema_version: "0.6"
robot: {name: r}
devices:
  - id: d1
    model: m1
    physical: {mass: {value: 1, unit: kg}}
    owner: o
    provenance: {witness: Maya Chen, source: s, locked_at: 2026-01-01}
"""


def _declare_mass(path, witness):
    from lockwire.edit import Change, apply_changes

    return apply_changes(path, [Change("devices/d1/physical/mass", "2 kg", "ds.pdf", witness)])


def test_a_named_declare_on_a_06_device_stays_06(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_DEVICE_06)
    _declare_mass(path, "Maya Chen")
    assert path.read_text().startswith('schema_version: "0.6"\n')


def test_an_anonymous_declare_moves_a_06_file_to_07_and_a_06_file_with_one_is_refused(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_DEVICE_06)
    result = _declare_mass(path, None)
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records an anonymous model" in result.lines
    assert "Maya Chen" not in path.read_text() and "ds.pdf" in path.read_text()
    path.write_text(path.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="without a witness needs lockfile format 0.7"):
        load_contract(path)


# ---- GAP-meshimport: a mesh scale is a 0.7-only field.


def test_a_named_edit_with_no_scale_leaves_a_06_file_at_06_byte_for_byte(tmp_path):
    from lockwire.edit import add_entry

    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    add_entry(path, "link", {"name": "base"}, source="s", witness="Maya Chen")
    assert path.read_text().startswith(_OLD)


def test_a_mesh_import_with_scale_moves_a_06_file_to_07_with_the_notice(tmp_path):
    import struct

    from lockwire.edit import add_entry

    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    (tmp_path / "t.stl").write_bytes(b"\0" * 80 + struct.pack("<I", 1) + tri)
    fields = {"name": "m", "mesh": tmp_path / "t.stl", "scale": "mm"}
    result = add_entry(path, "link", fields, source="s", witness="Maya Chen")
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records a mesh scale" in result.lines


# ---- groups are a 0.7-only field.


def test_an_edit_with_no_group_leaves_a_06_file_at_06(tmp_path):
    from lockwire.edit import add_entry

    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    add_entry(path, "link", {"name": "base"}, source="s", witness="Maya Chen")
    assert path.read_text().startswith(_OLD)


def test_adding_a_group_moves_a_06_file_to_07_with_the_notice(tmp_path):
    from lockwire.edit import add_entry

    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    add_entry(path, "link", {"name": "base"}, source="s", witness="Maya Chen")
    fields = {"name": "g", "members": ["base"]}
    result = add_entry(path, "group", fields, source="s", witness="Maya Chen")
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records groups" in result.lines


def test_a_06_file_that_uses_groups_is_refused(tmp_path):
    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    from lockwire.edit import add_entry

    add_entry(path, "link", {"name": "base"}, source="s", witness="Maya Chen")
    add_entry(path, "group", {"name": "g", "members": ["base"]}, source="s", witness="Maya Chen")
    path.write_text(path.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="groups need lockfile format 0.7"):
        load_contract(path)


# ---- holes are a 0.7-only field.


def _plate_06(tmp_path):
    from lockwire.edit import add_entry

    path = tmp_path / "lock.yaml"
    path.write_text(_OLD)
    geometry = [{"kind": "box", "size": [0.1, 0.1, 0.01]}]
    add_entry(path, "link", {"name": "p", "geometry": geometry}, source="s", witness="Maya Chen")
    return path


def test_adding_holes_moves_a_06_file_to_07_with_the_notice_and_06_with_holes_is_refused(tmp_path):
    from lockwire.edit import add_entry

    path = _plate_06(tmp_path)
    assert path.read_text().startswith('schema_version: "0.6"\n')
    fields = {"link": "p", "diameter_m": 0.003, "positions": [[0, 0]]}
    result = add_entry(path, "holes", fields, source="s", witness="Maya Chen")
    assert path.read_text().startswith('schema_version: "0.7"\n')
    assert "format moved to 0.7: this file now records holes" in result.lines
    path.write_text(path.read_text().replace('"0.7"', '"0.6"', 1))
    with pytest.raises(ValueError, match="holes need lockfile format 0.7"):
        load_contract(path)
