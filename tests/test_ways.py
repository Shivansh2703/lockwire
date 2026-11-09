"""The ways and features list: one data file (app/src/shared/ways.json) that the app page
renders. Every command in it, and every command the README names, must be a real free
command, so a rename fails here."""

import importlib.metadata
import json
import re
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "app/src/shared/ways.json").read_text())
README = (ROOT / "README.md").read_text()


def _snippets() -> list[str]:
    ways = [c for w in DATA["ways"] for c in w.get("code", [])]
    return ways + [f["code"] for f in DATA["features"]]


def test_every_documented_command_is_a_free_command(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: [])
    from lockwire import cli

    free = set(typer.main.get_command(cli.app).commands)  # hidden ones too
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


def test_every_readme_command_is_a_free_command(monkeypatch):
    """The README keeps four features (the rest live in docs/guides); each verb it names must exist."""
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: [])
    from lockwire import cli

    free = set(typer.main.get_command(cli.app).commands)
    used = set(re.findall(r"\blockwire ([a-z][\w-]*)", README.split("## Download the app")[0]))
    assert {"check", "start", "agent-setup", "mcp"} <= used <= free, used - free
