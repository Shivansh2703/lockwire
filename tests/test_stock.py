"""Add_entry kind "stock" (plate, rod, tube, T-slot) sized by typed or named dimensions."""

import pytest

from lockwire import catalog
from lockwire.edit import EditRefusal, add_entry
from lockwire.schema import load_contract

AL = "Aluminium 6061-T6"
_BLANK = 'schema_version: "0.7"\nrobot:\n  name: r\n'


def _robot(tmp_path):
    path = tmp_path / "lockwire.yaml"
    path.write_text(_BLANK)
    add_entry(path, "link", {"name": "base", "mass": "1 kg"}, source="s", witness="w1")
    param = {"name": "plate_len", "value": 0.3, "unit": "m"}
    add_entry(path, "param", param, source="s", witness="w1")
    return path


def _stock(path, stock="stock.plate", dims=None, **over):
    dims = dims or {"length": 0.3, "width": 0.2, "thickness": 0.003}
    fields = {
        "stock": stock, "dimensions": dims, "material": AL, "parent": "base",
        "frame": {"xyz": [0, 0, 0.1]}, "joint": {"type": "fixed"}, "name": "part",
    }  # fmt: skip
    return add_entry(path, "stock", {**fields, **over}, source="s", witness="w1")


def _part(path):
    return load_contract(path).model.links[-1]


def test_plate_mass_is_derived_and_labelled(tmp_path):
    path = _robot(tmp_path)
    _stock(path)
    link = _part(path)
    assert link.mass.value == pytest.approx(0.3 * 0.2 * 0.003 * 2700)
    assert "derived" in link.mass_note
    assert link.material.name == AL


def test_named_length_changes_the_mass_proportionally(tmp_path):
    path = _robot(tmp_path)
    _stock(path, dims={"length": "plate_len", "width": 0.2, "thickness": 0.003})
    before = _part(path).mass.value
    path.write_text(path.read_text().replace("value: 0.3", "value: 0.6"))
    assert _part(path).mass.value == pytest.approx(2 * before)


def test_tslot_uses_mass_per_metre_not_box_volume(tmp_path):
    path = _robot(tmp_path)
    row = next(r for r in catalog.stock() if r.id == "tslot.20x20")
    _stock(path, "tslot.20x20", {"length": 0.5}, material=None)
    link = _part(path)
    assert link.mass.value == pytest.approx(row.mass_per_length.value * 0.5)
    assert link.mass.value < 0.02 * 0.02 * 0.5 * 2700  # the solid box would be far heavier
    assert "maker's mass per metre" in path.read_text()


def test_hollow_tube_mass_is_the_true_cross_section(tmp_path):
    path = _robot(tmp_path)
    _stock(path, "stock.square_tube", {"outer_size": 0.02, "wall": 0.002, "length": 0.5})
    assert _part(path).mass.value == pytest.approx((0.02**2 - 0.016**2) * 0.5 * 2700)


@pytest.mark.parametrize(
    ("over", "line"),
    [
        ({"stock": "stock.plat"}, "No stock shape 'stock.plat'. Did you mean stock.plate"),
        ({"dimensions": {"length": 0.3, "width": 0.2}}, "stock.plate needs dimension 'thickness'."),
        (
            {"dimensions": {"length": 0.3, "width": 0.2, "thickness": 0}},
            "stock dimension thickness must be positive, got 0.",
        ),
        (
            {
                "stock": "stock.square_tube",
                "dimensions": {"outer_size": 0.02, "wall": 0.011, "length": 0.5},
            },
            "thicker than half the outer size",
        ),
        ({"material": "Unobtainium"}, "No catalog material named 'Unobtainium'."),
        ({"parent": "nope"}, "This robot's model has no link 'nope'; add it first."),
        ({"frame": None}, "attach needs a frame"),
    ],
)
def test_refusals_leave_the_file_untouched(tmp_path, over, line):
    path = _robot(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match=line):
        _stock(path, **over)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("stock", "dims"),
    [
        ("tslot.20x20", {"length": "plate_len"}),
        ("stock.square_tube", {"outer_size": 0.02, "wall": 0.002, "length": "plate_len"}),
    ],
)
def test_declared_mass_stock_refuses_a_named_dimension(tmp_path, stock, dims):
    path = _robot(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match="takes numbers for its dimensions"):
        _stock(path, stock, dims)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("stock", "dims"),
    [
        ("stock.plate", {"length": 0.3, "width": 0.2, "thickness": 0.003}),
        ("stock.square_tube", {"outer_size": 0.02, "wall": 0.002, "length": 0.5}),
    ],
)
def test_stock_without_a_material_is_refused(tmp_path, stock, dims):
    path = _robot(tmp_path)
    before = path.read_bytes()
    with pytest.raises(EditRefusal, match=f"^{stock} needs a material.$"):
        _stock(path, stock, dims, material=None)
    assert path.read_bytes() == before
