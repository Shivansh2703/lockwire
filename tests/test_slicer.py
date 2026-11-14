"""Mass source priority and slicer import."""

import struct
from pathlib import Path
import zipfile

import pytest

from lockwire.edit import apply_changes
from lockwire.schema import load_contract
from lockwire.slicer import (
    SlicerRefusal,
    estimate_kg,
    mass_changes,
    pick_mass,
    read_slicer_grams,
)

FOOTER = (
    "; filament used [mm] = 14000.0\n; filament used [cm3] = 33.7\n; filament used [g] = 42.1\n"
)


def _stl_cube(path, side):
    s = side
    corners = [(x, y, z) for x in (0, s) for y in (0, s) for z in (0, s)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]  # fmt: skip
    body = b"".join(
        struct.pack("<12fH", 0, 0, 0, *(c for i in f for c in corners[i]), 0) for f in faces
    )
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 12) + body)


def test_gcode_grams_read_and_cm3_is_not_grams(tmp_path):
    p = tmp_path / "a.gcode"
    p.write_text("G1 X1\n" + FOOTER)
    assert read_slicer_grams(p) == 42.1


def test_cm3_only_footer_is_refused(tmp_path):
    p = tmp_path / "a.gcode"
    p.write_text("; filament used [cm3] = 33.7\n")
    with pytest.raises(SlicerRefusal):
        read_slicer_grams(p)


def test_multi_extruder_grams_add(tmp_path):
    p = tmp_path / "a.gcode"
    p.write_text("; filament used [g] = 10.5, 2.0\n")
    assert read_slicer_grams(p) == 12.5


def test_3mf_used_g_summed(tmp_path):
    p = tmp_path / "a.3mf"
    xml = '<config><plate><filament id="1" used_m="1" used_g="30.5"/><filament id="2" used_g="1.5"/></plate></config>'
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", xml)
    assert read_slicer_grams(p) == 32.0


def test_3mf_entity_bomb_and_junk_refused(tmp_path):
    p = tmp_path / "a.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", '<!DOCTYPE x [<!ENTITY a "b">]><config/>')
    with pytest.raises(SlicerRefusal):
        read_slicer_grams(p)
    junk = tmp_path / "b.3mf"
    junk.write_bytes(b"not a zip")
    with pytest.raises(SlicerRefusal):
        read_slicer_grams(junk)


def test_3mf_oversize_member_refused_in_one_line(tmp_path, monkeypatch):
    p = tmp_path / "a.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", "<config>" + " " * 100 + "</config>")
    monkeypatch.setattr("lockwire.slicer.MAX_3MF_MEMBER_BYTES", 50)
    with pytest.raises(SlicerRefusal, match="too large") as e:
        read_slicer_grams(p)
    assert "\n" not in str(e.value)


def test_3mf_too_many_members_refused(tmp_path, monkeypatch):
    p = tmp_path / "a.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", '<config><filament used_g="1"/></config>')
        z.writestr("extra.txt", "x")
    monkeypatch.setattr("lockwire.slicer.MAX_3MF_MEMBERS", 1)
    with pytest.raises(SlicerRefusal, match="too many parts"):
        read_slicer_grams(p)


def test_priority_weighed_beats_slicer_beats_estimate():
    assert pick_mass(0.040, 0.0421, 0.05) == (0.040, "measured")
    assert pick_mass(None, 0.0421, 0.05) == (0.0421, "calculated")
    assert pick_mass(None, None, 0.05) == (0.05, "estimated")
    assert pick_mass() is None


def test_mesh_volume_times_density(tmp_path):
    p = tmp_path / "c.stl"
    _stl_cube(p, 0.1)
    assert estimate_kg(p, 1000) == pytest.approx(1.0)


def test_slicer_grams_set_link_mass_with_kind_calculated(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text((Path(__file__).parent / "fixtures" / "armtree.yaml").read_text())
    gc = tmp_path / "a.gcode"
    gc.write_text(FOOTER)
    kg, kind = pick_mass(slicer_kg=read_slicer_grams(gc) / 1000)
    apply_changes(contract, mass_changes("elbow", kg, kind, "slicer a.gcode", "human"))
    link = next(x for x in load_contract(contract).model.links if x.name == "elbow")
    assert link.mass.base() == pytest.approx(0.0421)
    assert link.mass_kind == "calculated"
