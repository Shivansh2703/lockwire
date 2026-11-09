"""E5.7: run the dynamics fixture decks through the pinned CalculiX and score them.

Reuses the retained two-DOF harmonic/transient parsers and their closed-form
references. Skips cleanly without
``LOCKWIRE_CCX``, exactly like ``tests/test_calculix_benchmarks.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lockwire_pro.analysis import (
    _parse_two_dof_harmonic,
    _parse_two_dof_transient,
    _two_dof_harmonic_reference,
    _two_dof_transient_reference,
)

FIXTURES = Path(__file__).parent / "fixtures" / "calculix_dynamics"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def ccx() -> Path:
    configured = os.environ.get("LOCKWIRE_CCX")
    if not configured:
        pytest.skip(
            "CalculiX numerical gate not run: set LOCKWIRE_CCX to an explicit absolute ccx path"
        )
    binary = Path(configured)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("LOCKWIRE_CCX must name an explicit executable absolute path")
    return binary


def run_deck(binary: Path, deck: str, tmp_path: Path) -> str:
    run_dir = tmp_path / Path(deck).stem
    run_dir.mkdir()
    copied = run_dir / deck
    shutil.copy2(FIXTURES / deck, copied)
    result = subprocess.run(
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
    assert result.returncode == 0, result.stdout + result.stderr
    return (run_dir / f"{copied.stem}.dat").read_text()


def test_harmonic_amplitude_matches_closed_form(tmp_path: Path) -> None:
    text = run_deck(ccx(), "two_dof_modal_harmonic.inp", tmp_path)
    harmonic = MANIFEST["sdof"]["two_positive_dof"]["harmonic"]
    frequency, amplitude = _parse_two_dof_harmonic(text, harmonic["frequency_hz"])
    expected = _two_dof_harmonic_reference(harmonic["frequency_hz"])
    assert expected == pytest.approx(harmonic["x_amplitude_m"], rel=1e-6)
    assert amplitude == pytest.approx(expected, rel=1e-3)


def test_transient_samples_match_closed_form(tmp_path: Path) -> None:
    text = run_deck(ccx(), "two_dof_modal_transient_damped.inp", tmp_path)
    samples = _parse_two_dof_transient(text)
    for sample in MANIFEST["sdof"]["forced_damped_transient"]["samples_m"]:
        time_s = sample["time_s"]
        if time_s == 0.0:
            continue
        displacement, _velocity = samples[time_s]
        expected, _ = _two_dof_transient_reference(time_s)
        assert expected == pytest.approx(sample["u_m"], rel=1e-6)
        assert displacement == pytest.approx(expected, rel=1e-3, abs=1e-9)
