"""Holes and bolt patterns on a shape (0.7 only)."""

import pytest
import yaml

from lockwire.catalog import hole_size
from lockwire.edit import EditRefusal, add_entry
from lockwire.schema import Hole, load_contract, save_contract

BLANK = 'schema_version: "0.7"\nrobot:\n  name: r\n'
PARAMS = "params:\n  - name: w\n    value: 0.02\n"


def _plate(tmp_path, text=BLANK, shape=None):
    path = tmp_path / "lock.yaml"
    path.write_text(text)
    shape = shape or {"kind": "box", "size": [0.1, 0.1, 0.01]}
    add_entry(path, "link", {"name": "plate", "geometry": [shape]}, source="s", witness="w1")
    return path


def _holes(path, **fields):
    return add_entry(path, "holes", {"link": "plate", **fields}, source="s", witness="w1")


def _stored(path):
    return load_contract(path).model.links[0].geometry[0].holes


def test_round_trip_keeps_param_names_raw_and_resolves_in_memory(tmp_path):
    path = _plate(tmp_path, BLANK + PARAMS)
    _holes(path, diameter_m=0.003, positions=[[0, 0]])
    doc = yaml.safe_load(path.read_text())
    doc["model"]["links"][0]["geometry"][0]["holes"][0]["xyz"] = ["w", "-w", 0.0]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    loaded = load_contract(path)
    assert _stored(path)[0].xyz == (0.02, -0.02, 0.0)
    out = tmp_path / "saved.yaml"
    save_contract(loaded, out)
    assert yaml.safe_load(out.read_text())["model"]["links"][0]["geometry"][0]["holes"][0]["xyz"] == ["w", "-w", 0.0]


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"diameter_m": 0.003, "fastener": "M3", "fit": "tap"},
        {"fastener": "M3"},
        {"fit": "tap"},
        {"diameter_m": 0.003, "fit": "tap"},
        {"diameter_m": -1.0},
    ],
)
def test_a_hole_needs_exactly_one_size(kw):
    with pytest.raises(ValueError):
        Hole(xyz=(0, 0, 0), **kw)


def test_an_unknown_size_is_not_on_record_and_the_file_has_no_diameter(tmp_path):
    path = _plate(tmp_path)
    result = _holes(path, fastener="M42", fit="clearance", positions=[[0, 0]])
    hole = _stored(path)[0]
    assert hole.resolved_diameter_m is None
    assert hole.diameter_note == "diameter not on record for M42 clearance"
    assert "1 M42 clearance holes: diameter not on record for M42 clearance" in result.lines


def test_m3_clearance_hole_is_3_4_mm_per_the_cited_table(tmp_path):
    path = _plate(tmp_path)
    result = _holes(path, fastener="M3", fit="clearance", positions=[[0, 0], [1, 1]])
    hole = _stored(path)[0]
    assert hole.resolved_diameter_m == pytest.approx(0.0034)
    assert (hole.resolved_fit_class, hole.diameter_note) == ("Medium Fit", None)
    assert hole.resolved_source
    assert f"2 M3 clearance holes: 3.4 mm (Medium Fit, {hole.resolved_source})" in result.lines


def test_a_tap_hole_resolves_to_the_tap_row(tmp_path):
    path = _plate(tmp_path)
    _holes(path, fastener="M3", fit="tap", positions=[[0, 0]])
    assert _stored(path)[0].resolved_diameter_m == pytest.approx(hole_size("M3", "tap")["diameter_m"])
    assert _stored(path)[0].resolved_diameter_m != pytest.approx(0.0034)


def test_the_saved_file_has_no_diameter_for_fastener_holes(tmp_path):
    path = _plate(tmp_path)
    _holes(path, fastener="M3", fit="clearance", positions=[[0, 0]])
    assert "diameter_m" not in path.read_text()


def test_save_contract_writes_no_diameter_for_a_resolved_fastener_hole(tmp_path):
    path = _plate(tmp_path)
    _holes(path, fastener="M3", fit="clearance", positions=[[0, 0]])
    contract = load_contract(path)
    assert contract.model.links[0].geometry[0].holes[0].resolved_diameter_m == pytest.approx(0.0034)
    save_contract(contract, path)
    assert "diameter_m" not in path.read_text()


def test_add_holes_by_positions_and_centred_grid(tmp_path):
    path = _plate(tmp_path)
    result = _holes(path, diameter_m=0.003, depth_m=0.005, positions=[[0.01, 0.02]])
    assert "holes do not reduce the derived mass" in result.lines[0]
    _holes(path, diameter_m=0.003, grid={"pitch_m": 0.02, "nx": 2, "ny": 3})
    holes = _stored(path)
    assert [(h.xyz[0], h.xyz[1]) for h in holes[1:3]] == [(-0.01, -0.02), (0.01, -0.02)]
    assert (len(holes), holes[0].depth_m, holes[0].xyz[2]) == (7, 0.005, 0.0)
    assert sum(h.xyz[1] for h in holes[1:]) == pytest.approx(0.0)


def test_holes_do_not_change_the_derived_mass(tmp_path):
    path = _plate(tmp_path, shape={"kind": "box", "size": [0.1, 0.1, 0.01]})
    before = load_contract(path).model.links[0].mass
    _holes(path, diameter_m=0.003, positions=[[0, 0]])
    assert load_contract(path).model.links[0].mass == before


@pytest.mark.parametrize(
    "fields",
    [
        {"link": "nope", "diameter_m": 0.003, "positions": [[0, 0]]},
        {"shape_index": 4, "diameter_m": 0.003, "positions": [[0, 0]]},
        {"positions": [[0, 0]]},
        {"diameter_m": 0.003, "fastener": "M3", "fit": "tap", "positions": [[0, 0]]},
        {"diameter_m": 0, "positions": [[0, 0]]},
        {"diameter_m": 0.003, "depth_m": -1, "positions": [[0, 0]]},
        {"diameter_m": 0.003},
        {"diameter_m": 0.003, "positions": [[0, 0]], "grid": {"pitch_m": 1, "nx": 1, "ny": 1}},
        {"diameter_m": 0.003, "grid": {"pitch_m": 0, "nx": 1, "ny": 1}},
    ],
)
def test_refusals_are_one_plain_line_and_leave_the_file_alone(tmp_path, fields):
    path = _plate(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal) as e:
        add_entry(path, "holes", {"link": "plate", **fields}, source="s", witness="w1")
    assert "\n" not in str(e.value)
    assert path.read_bytes() == before


def test_a_mesh_shape_is_refused(tmp_path):
    path = _plate(tmp_path, shape={"kind": "mesh", "mesh": "a.stl"})
    with pytest.raises(EditRefusal, match="not a mesh"):
        _holes(path, diameter_m=0.003, positions=[[0, 0]])


def test_an_unknown_hole_key_is_refused_naming_the_field(tmp_path):
    path = _plate(tmp_path)
    _holes(path, diameter_m=0.003, positions=[[0, 0]])
    doc = yaml.safe_load(path.read_text())
    doc["model"]["links"][0]["geometry"][0]["holes"][0]["thread"] = "x"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ValueError, match="thread"):
        load_contract(path)
