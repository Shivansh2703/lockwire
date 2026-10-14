"""Build-time gate: the extras the frozen binary CLAIMS must be importable when it is built.

`packaging/lockwire.spec` calls `check()` before `Analysis`, so no build can skip it.
It lives here rather than inline in the spec because a spec file cannot be imported —
PyInstaller injects `SPECPATH`, `Analysis`, `PYZ` and friends into its namespace — and a
guard nobody can unit-test is a guard nobody can prove.

**Why it is a hard failure rather than a warning.** Measured 2026-09-08 (box e1-d2's
reviewer, twice): `uvicorn` is not a lockwire dependency at all — `uv pip show uvicorn`
says `Required-by: mcp` — so it reaches the build host only through `--extra mcp`. The
spec's one unguarded `collect_submodules("uvicorn")` returns `[]` rather than raising
when uvicorn is absent, and the extras beside it were wrapped in a helper that returned
`[]` on purpose. Both **fail open**: a build host without the extras produced a green,
plausible 39 MB binary (the good one is 56 MB) whose PYZ carried `uvicorn 0 · mcap 0 ·
mcp 0` where the good one carried 42/24/131.

That binary is worse than a build failure in two ways. Its `lockwire mcp` door exits 2
on first use — the CLI offers the door unconditionally, so the shortfall is invisible
until a user opens it — and the message it prints is `install the mcp extra: uv sync
--extra mcp`, advice that **cannot be followed inside a shipped binary**, because there
is no venv to sync. The only place the shortfall can still be fixed is the build host.
So that is where it stops.

Ceiling: this asserts the modules are importable *on the build host*, not that they were
bundled. That is the check that catches the measured failure (a host without the extra),
and it is the one a build can run before spending five minutes on an Analysis. Proving
the frozen bundle then lists every MCP tool is a separate gate on the release script,
still open in `TODO.md`.
"""

from __future__ import annotations

import importlib.util

# Import name -> the `uv sync --extra <name>` that supplies it, or None for a module the
# base install already carries. `uvicorn` is here because the mcp SDK's server needs it
# and nothing else in the tree declares it; `mcap_ros2` is `mcap-ros2-support`'s import
# name. Add an extra to pyproject and this map must grow with it — asserted in
# tests/test_packaging_extras.py against pyproject itself.
FREE_CLAIMED: dict[str, str | None] = {  # what the free spec (packaging/lockwire.spec) freezes
    "lockwire": None,
    "rich": None,
    "mcp": "mcp",
    "uvicorn": "mcp",
    "fastapi": "serve",
}
CLAIMED: dict[str, str | None] = {
    **FREE_CLAIMED,
    "mcap": "ros2bag",
    "mcap_ros2": "ros2bag",
    # MAVLink frame admission imports this package lazily; a frozen build must
    # fail before Analysis when the optional dialect dependency is absent.
    "pymavlink": "mavlink",
}


def missing(claimed: dict[str, str | None] = CLAIMED) -> list[tuple[str, str | None]]:
    """The claimed modules that are not importable here, in declaration order.

    `find_spec` rather than `collect_submodules`: "is it importable" is the actual
    claim, and it is the question `collect_submodules` answers with `[]` — the fail-open
    that bought this file. `find_spec` returns None for an absent top-level module and
    raises `ModuleNotFoundError` when a parent package is missing; both mean absent.
    """
    absent = []
    for module, extra in claimed.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except ImportError:
            found = False
        if not found:
            absent.append((module, extra))
    return absent


def check(claimed: dict[str, str | None] = CLAIMED) -> None:
    """Fail the build, legibly, when the binary would ship a door it cannot open."""
    absent = missing(claimed)
    if not absent:
        return
    extras = sorted({extra for _, extra in absent if extra})
    lines = [
        "lockwire.spec: refusing to freeze a binary that cannot do what it offers.",
        "",
        "Not importable on this build host:",
        *(
            f"  {module}  (from: {'uv sync --extra ' + extra if extra else 'the base install'})"
            for module, extra in absent
        ),
        "",
        "The frozen CLI offers these doors unconditionally, so a build without them is",
        "green, smaller, and dies on first use — and the message it prints then tells the",
        "user to run `uv sync`, which a shipped binary has no venv to do. The build host",
        "is the last place this is fixable.",
    ]
    if extras:
        lines += [
            "",
            "Fix: uv sync " + " ".join(f"--extra {e}" for e in extras) + "  then rebuild.",
        ]
    raise SystemExit("\n".join(lines))
