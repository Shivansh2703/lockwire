"""CADN-matcompare: a link's mass and the torque margin in each catalog material, nothing written."""

import math

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.material_compare import compare_materials
from lockwire.schema import load_contract
from tests.test_derive_geometry import _arm_text

runner = CliRunner()
PLATE = {"kind": "box", "size": [0.3, 0.2, 0.003], "origin": {"xyz": [0.0, 0.0, 0.1]}}
G = 9.80665  # 1 permille tolerance below covers 9.81 too
AL, STEEL = "Aluminium 6061-T6", "Mild steel S235JR"


def _plate(tmp_path, monkeypatch, **extra):
    path = tmp_path / "plate.yaml"
    path.write_text(_arm_text(tmp_path, monkeypatch, shape=PLATE, elbow=0.2, **extra))
    return path


def _rows(tmp_path, monkeypatch):
    got = compare_materials(load_contract(_plate(tmp_path, monkeypatch)), "arm")
    return {r["material"]: r for r in got["rows"]}


def test_plate_masses_follow_the_density_ratio_and_the_total_moves_with_them(tmp_path, monkeypatch):
    rows = _rows(tmp_path, monkeypatch)
    assert rows[AL]["link_mass_kg"] == pytest.approx(0.18 * 0.0027 * 1000)
    assert rows[STEEL]["link_mass_kg"] / rows[AL]["link_mass_kg"] == pytest.approx(7850 / 2700)
    gain = rows[STEEL]["link_mass_kg"] - rows[AL]["link_mass_kg"]
    assert rows[STEEL]["total_mass_kg"] - rows[AL]["total_mass_kg"] == pytest.approx(gain)


def test_steel_has_the_lower_margin_by_the_hand_worked_amount(tmp_path, monkeypatch):
    rows = _rows(tmp_path, monkeypatch)
    # the worst shoulder pose is its +1 rad limit; the plate's centre is 0.1 m out, so only the plate's
    # share g * m * 0.1 * sin(1 rad) changes between materials (the forearm's does not)
    drop = G * 0.1 * math.sin(1.0) * (0.18 * 0.0027 * 1000 * (7850 / 2700 - 1))
    assert rows[AL]["worst_joint"] == rows[STEEL]["worst_joint"] == "shoulder"
    assert rows[AL]["worst_margin_nm"] - rows[STEEL]["worst_margin_nm"] == pytest.approx(
        drop, rel=1e-3
    )


def test_printed_rows_carry_the_infill_note_and_metals_do_not(tmp_path, monkeypatch):
    got = compare_materials(load_contract(_plate(tmp_path, monkeypatch)), "arm")
    printed = [r["material"] for r in got["rows"] if r["printed"]]
    assert printed and AL not in printed and "100 percent infill" in got["note"]


def test_a_declared_mass_gets_one_plain_line_and_no_rows(tmp_path, monkeypatch):
    path = _plate(tmp_path, monkeypatch, arm_extra={"mass": {"value": 9.0, "unit": "kg"}})
    got = compare_materials(load_contract(path), "arm")
    assert got["rows"] == [] and "no volume to scale" in got["note"]


def test_cli_prints_a_row_per_material_and_writes_nothing(tmp_path, monkeypatch):
    path = _plate(tmp_path, monkeypatch)
    before = path.read_bytes()
    result = runner.invoke(app, ["materials", "compare", str(path), "--link", "arm"])
    assert result.exit_code == 0 and f"{STEEL}: link 1.413 kg" in result.output
    assert path.read_bytes() == before
    assert runner.invoke(app, ["materials", "compare", str(path), "--link", "nope"]).exit_code == 2
