"""The build refuses to freeze a binary that cannot do what its CLI offers.

Box E6-MX. The defect these tests hold shut, measured 2026-09-08: every extra in
`packaging/lockwire.spec` failed OPEN. `collect_submodules` returns `[]` for a package
that is not installed rather than raising, so a build host without `--extra mcp` produced
a green, plausible 39 MB binary (the good one is 56 MB) whose PYZ carried `uvicorn 0 ·
mcap 0 · mcp 0`, whose `lockwire mcp` door exits 2, and whose advice to the user was
`uv sync --extra mcp` — impossible to follow inside a shipped binary.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

PACKAGING = Path(__file__).parent.parent / "packaging"
# The paid spec carries the full gate; the public tree has only the free one.
SPEC = next(
    p for p in (PACKAGING / "pro" / "lockwire.spec", PACKAGING / "lockwire.spec") if p.exists()
)
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

sys.path.insert(0, str(PACKAGING))
import require_extras  # noqa: E402 — loaded from packaging/, which is not a package

PRO: dict[str, str | None] = {}  # the paid build claims the ros2bag and mavlink extras too
if (PACKAGING / "pro").exists():
    sys.path.insert(0, str(PACKAGING / "pro"))
    from pro_extras import PRO_CLAIMED as PRO  # noqa: E402

needs_pro = pytest.mark.skipif(not PRO, reason="the public tree has no paid extras")


def test_every_claimed_extra_is_gated_against_pyproject():
    """Every extra declared in pyproject is named in the gate's map.

    The anchored leg (PARITY-ANCHOR): the expected set is read off
    `[project.optional-dependencies]`, which `require_extras` does not produce and cannot
    influence. Adding an extra to pyproject and forgetting to gate it is exactly the way a
    seventh door quietly starts failing open, and it is what this catches.
    """
    declared = set(
        tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    )
    gated = {extra for extra in (require_extras.CLAIMED | PRO).values() if extra}
    assert gated == declared, (
        f"extras declared in pyproject but not gated in require_extras.CLAIMED: {declared - gated}; "
        f"gated but not declared: {gated - declared}"
    )


@needs_pro
def test_mavlink_extra_is_gated_at_its_actual_import_root():
    """The optional MAVLink protocol package is a claimed frozen-binary input."""
    assert PRO["pymavlink"] == "mavlink"


@needs_pro
def test_missing_pymavlink_refuses_the_build_with_the_mavlink_fix(monkeypatch):
    """A missing MAVLink import stops before PyInstaller can freeze it out."""
    original_find_spec = require_extras.importlib.util.find_spec

    def missing_pymavlink(name: str):
        return None if name == "pymavlink" else original_find_spec(name)

    monkeypatch.setattr(require_extras.importlib.util, "find_spec", missing_pymavlink)
    with pytest.raises(SystemExit) as caught:
        require_extras.check({"pymavlink": PRO["pymavlink"]})
    message = str(caught.value)
    assert "pymavlink" in message
    assert "uv sync --extra mavlink" in message
    assert "refusing to freeze" in message


def test_every_claimed_module_is_importable_here():
    """Every module the gate claims is importable in this dev environment.

    If this goes red the gate is over-claiming — it would fail a legitimate build — which
    is the failure mode of the guard itself, not of the tree it guards.
    """
    assert require_extras.missing() == []


def test_a_missing_extra_fails_the_build_naming_the_extra_and_the_fix():
    """An absent extra stops the build, says which extra, and says how to get it."""
    with pytest.raises(SystemExit) as caught:
        require_extras.check({"no_such_module_e6mx": "mcp"})
    message = str(caught.value)
    assert "no_such_module_e6mx" in message
    assert "uv sync --extra mcp" in message
    assert "refusing to freeze" in message


def test_a_missing_base_module_fails_without_advertising_an_extra():
    """A base-install module has no `--extra` to suggest, and the message must not invent one."""
    with pytest.raises(SystemExit) as caught:
        require_extras.check({"no_such_module_e6mx": None})
    message = str(caught.value)
    assert "the base install" in message
    assert "--extra" not in message


def test_a_module_whose_parent_package_is_absent_counts_as_missing():
    """`find_spec` RAISES for a submodule of an absent package; that is absent, not an error.

    Without the `except ImportError`, this call escapes as ModuleNotFoundError from inside
    a PyInstaller spec — loud, but not the legible failure the build host needs.
    """
    assert require_extras.missing({"no_such_pkg_e6mx.sub": "mcp"}) == [
        ("no_such_pkg_e6mx.sub", "mcp")
    ]


def test_the_gate_runs_in_the_spec_before_analysis():
    """The spec calls the gate, and calls it before Analysis — a gate after the bundle is built
    is a gate that already spent the five minutes it exists to save."""
    spec = SPEC.read_text(encoding="utf-8")
    assert "\nrequire_extras.check(" in spec, "the spec must call the extras gate"
    assert spec.index("\nrequire_extras.check(") < spec.index("a = Analysis(")


def test_the_spec_keeps_no_fail_open_extras_helper():
    """No `_optional`-style helper survives in the spec: returning [] for an absent package
    is the fail-open this box exists to remove, and it looks like prudence."""
    spec = SPEC.read_text(encoding="utf-8")
    assert "def _optional" not in spec
    assert "return []" not in spec


def test_collect_submodules_still_fails_open_which_is_why_the_gate_exists():
    """The premise, anchored outside our code: PyInstaller answers `[]`, never raises.

    If a future PyInstaller starts raising, this goes red and the gate's docstring — which
    explains itself entirely in terms of this behaviour — needs rereading, not re-pinning.
    """
    from PyInstaller.utils.hooks import collect_submodules

    assert collect_submodules("no_such_module_e6mx") == []
    assert importlib.util.find_spec("no_such_module_e6mx") is None


def test_free_claims_are_a_subset_and_name_no_paid_extra():
    assert set(require_extras.FREE_CLAIMED) <= set(require_extras.CLAIMED)
    assert not {"mcap", "mcap_ros2", "pymavlink"} & set(require_extras.FREE_CLAIMED)
