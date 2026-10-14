"""Numerical acceptance for immutable CalculiX linear benchmark decks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "calculix_benchmarks"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
ROW = re.compile(
    r"^\s*(?P<node>\d+)\s+(?P<x>[+-]?[\d.]+[Ee][+-]?\d+)\s+"
    r"(?P<y>[+-]?[\d.]+[Ee][+-]?\d+)\s+(?P<z>[+-]?[\d.]+[Ee][+-]?\d+)\s*$"
)


def parse_node_print(text: str) -> dict[tuple[str, str, int], tuple[float, float, float]]:
    """Read the intentionally small, stable ``*NODE PRINT`` subset of a .dat file."""
    values: dict[tuple[str, str, int], tuple[float, float, float]] = {}
    kind = node_set = None
    for line in text.splitlines():
        match = re.search(r"\b(displacements|forces) \([vf]x,[vf]y,[vf]z\) for set (\S+)", line)
        if match:
            kind = "U" if match.group(1) == "displacements" else "RF"
            node_set = match.group(2)
            continue
        row = ROW.match(line)
        if row and kind and node_set:
            values[(kind, node_set, int(row["node"]))] = tuple(
                float(row[name]) for name in ("x", "y", "z")
            )
    return values


def ccx() -> Path:
    configured = os.environ.get("LOCKWIRE_CCX")
    if not configured:
        pytest.skip(
            "CalculiX numerical gate not run: set LOCKWIRE_CCX to an explicit absolute ccx path"
        )
    binary = Path(configured)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("LOCKWIRE_CCX must name an executable absolute path")
    return binary


def run_deck(binary: Path, deck: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    run_dir = tmp_path / deck.stem
    run_dir.mkdir()
    copied = run_dir / deck.name
    shutil.copy2(deck, copied)
    return subprocess.run(
        [str(binary), "-i", copied.stem],
        cwd=run_dir,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(run_dir),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_node_print_parser_reads_displacements_and_reactions() -> None:
    parsed = parse_node_print(
        " displacements (vx,vy,vz) for set TIP and time 0.1\n"
        "  2 1.500000E-04 2.000000E-04 0.000000E+00\n\n"
        " forces (fx,fy,fz) for set ROOT and time 0.1\n"
        "  1 -6.000000E+03 -8.000000E+03 0.000000E+00\n"
    )
    assert parsed[("U", "TIP", 2)] == (0.00015, 0.0002, 0.0)
    assert parsed[("RF", "ROOT", 1)] == (-6000.0, -8000.0, 0.0)


def test_manifest_closed_form_values_and_refinement_are_independent() -> None:
    cases = {case["id"]: case for case in MANIFEST["cases"]}
    truss = cases["rotated_truss"]["measurements"]
    assert truss[0]["expected"] == pytest.approx(10_000 * 5 / (200e9 * 0.001) * 3 / 5)
    assert truss[1]["expected"] == pytest.approx(10_000 * 5 / (200e9 * 0.001) * 4 / 5)
    assert truss[4]["expected"] == pytest.approx(-10_000 * 3 / 5)
    assert truss[5]["expected"] == pytest.approx(-10_000 * 4 / 5)
    assert cases["cantilever_b31_4"]["measurements"][0]["expected"] == pytest.approx(
        -100 / (3 * 210e9 * (0.01 * 0.01**3 / 12))
    )
    assert -100 / (2 * 210e9 * (0.01 * 0.01**3 / 12)) == pytest.approx(-0.2857142857142857)
    assert cases["propped_frame"]["measurements"][1]["expected"] == pytest.approx(5 * 1600 / 16)
    assert cases["propped_frame"]["measurements"][2]["expected"] == pytest.approx(11 * 1600 / 16)
    assert cases["propped_frame"]["measurements"][0]["expected"] == pytest.approx(
        -7 * 1600 * 3**3 / (768 * 200e9 * (0.1 * 0.1**3 / 12))
    )
    plate_expected = 0.00406235266055 * -100000.0 / (
        210e9 * 0.02**3 / (12 * (1 - 0.3**2))
    )
    assert cases["plate_s4_8"]["measurements"] == []
    assert cases["plate_s4_16"]["measurements"][0]["expected"] == pytest.approx(
        plate_expected, abs=1e-11
    )


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda case: case["id"])
def test_calculix_linear_benchmarks(case: dict[str, object], tmp_path: Path) -> None:
    result = run_deck(ccx(), FIXTURES / str(case["deck"]), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    output = (
        tmp_path / Path(str(case["deck"])).stem / f"{Path(str(case['deck'])).stem}.dat"
    ).read_text()
    parsed = parse_node_print(output)
    for measurement in case["measurements"]:
        actual = parsed[(measurement["kind"], measurement["set"], measurement["node"])][
            measurement["component"] - 1
        ]
        assert actual == pytest.approx(
            measurement["expected"], abs=measurement["abs_tol"], rel=measurement["rel_tol"]
        ), f"{case['id']} {measurement['kind']}{measurement['component']} ({measurement['unit']})"


def test_cantilever_refinement_is_convergent_and_free_tip_rotations_remain_free(
    tmp_path: Path,
) -> None:
    binary = ccx()
    runs = {
        case["id"]: run_deck(binary, FIXTURES / str(case["deck"]), tmp_path)
        for case in MANIFEST["cases"]
        if str(case["id"]).startswith("cantilever")
    }
    assert all(run.returncode == 0 for run in runs.values())
    deflection = {}
    for case in MANIFEST["cases"]:
        if not str(case["id"]).startswith("cantilever"):
            continue
        stem = Path(str(case["deck"])).stem
        parsed = parse_node_print((tmp_path / stem / f"{stem}.dat").read_text())
        check = case["measurements"][0]
        deflection[case["id"]] = parsed[("U", "TIP", check["node"])][1]
        deck = (FIXTURES / str(case["deck"])).read_text()
        assert "TIP, 4" not in deck and "TIP, 5" not in deck and "TIP, 6" not in deck
    exact = MANIFEST["cases"][1]["measurements"][0]["expected"]
    assert abs(deflection["cantilever_b31_8"] - exact) < abs(deflection["cantilever_b31_4"] - exact)


def test_singular_underconstrained_truss_is_detected_as_mechanically_unusable(
    tmp_path: Path,
) -> None:
    result = run_deck(ccx(), FIXTURES / MANIFEST["negative"]["deck"], tmp_path)
    if result.returncode != 0:
        return
    stem = Path(MANIFEST["negative"]["deck"]).stem
    output = (tmp_path / stem / f"{stem}.dat").read_text()
    displacement = parse_node_print(output)[("U", "TIP", 2)]
    assert max(map(abs, displacement)) > 1.0, result.stdout + result.stderr
