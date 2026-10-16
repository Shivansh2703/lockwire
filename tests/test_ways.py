"""The ways and features list: one data file (app/src/shared/ways.json) that the app page
renders. Every command in it must be a real free command, and the README must carry
the same snippets, so a rename or a drift fails here."""

import importlib.metadata
import json
import re
from pathlib import Path

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "app/src/shared/ways.json").read_text())
README = (ROOT / "README.md").read_text()


def _snippets() -> list[str]:
    ways = [c for w in DATA["ways"] for c in w.get("code", [])]
    return ways + [f["code"] for f in DATA["features"]]


def test_every_documented_command_is_a_free_command(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: [])
    from lockwire import cli

    out = CliRunner().invoke(cli.app, ["--help"]).output
    free = set(re.findall(r"^│ ([a-z][\w-]*)\s", out, re.M))
    used = {
        m
        for s in _snippets()
        for m in re.findall(r"(?:^|\n)(?:claude mcp add lockwire -- )?lockwire ([a-z][\w-]*)", s)
    }
    assert {
        "start",
        "check",
        "plan",
        "what-if",
        "mcp",
        "declare",
        "report",
        "describe",
        "diff",
        "gearbox",
        "scene",
        "upgrade",
        "add",
        "move",
    } <= used
    assert used <= free, used - free


def test_every_python_snippet_names_a_real_function():
    import lockwire.api as lw

    assert callable(lw.check)
    assert any("lw.check(" in s for s in _snippets())


def test_readme_lists_the_same_ways_and_features():
    for w in DATA["ways"]:
        assert f"**{w['title']}.**" in README
        for c in w.get("code", []):
            assert c in README, c
    for f in DATA["features"]:
        assert f"**{f['name']}:**" in README and f"`{f['code']}`" in README
    assert "—" not in README.split("## Other ways to use it")[1].split("## A real-world")[0]
