"""`--version` is the first thing anyone runs and the first thing a bug report needs."""

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from lockwire import __version__
from lockwire.cli import app

runner = CliRunner()


def test_version_flag_actually_prints_a_version():
    """It was advertised in --help and exited 2 with `Missing command`: the callback
    needs invoke_without_command, or Click fails while parsing and never runs it."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_a_bare_invocation_still_shows_help():
    """invoke_without_command must not turn `lockwire` into a silent no-op."""
    result = runner.invoke(app, [])
    assert "Usage:" in result.output
    assert "check" in result.output


def test_the_two_places_the_version_lives_agree():
    """`__init__.__version__` and pyproject are separate sources of truth, and a
    release that bumps one is a release that reports the wrong version forever."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert declared == __version__, f"pyproject says {declared}, package says {__version__}"


