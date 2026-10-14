"""README claims checked against live CLI behaviour, not against the
README itself.

Each test here runs the real command, captures what it actually printed or
did, and only then checks the README's sentence against that captured
behaviour. If the behaviour changes (e.g. a schema version is dropped from
``SUPPORTED_SCHEMA_VERSIONS``) and the README is not updated to match, these
tests go red — a plain substring-of-itself assertion could never catch that.
"""

import re
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
FIXTURES = ROOT / "tests" / "fixtures"

runner = CliRunner()


def _contract_at_version(tmp_path: Path, version: str) -> Path:
    """A copy of the clean fixture whose declared ``schema_version`` is ``version``."""
    source = (FIXTURES / "contract_clean.yaml").read_text()
    lines = [
        f'schema_version: "{version}"' if line.startswith("schema_version") else line
        for line in source.splitlines()
    ]
    path = tmp_path / "lockwire.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_readme_matches_the_live_unsupported_schema_version_refusal(tmp_path):
    """The README's supported-schema-version sentence must match what `check`
    actually prints today, not a number typed once and never re-checked.
    """
    contract = _contract_at_version(tmp_path, "0.99")
    result = runner.invoke(app, ["check", str(contract)])
    assert result.exit_code == 2, result.output

    match = re.search(r"this lockwire supports ([0-9.]+(?:, [0-9.]+)*)", result.output)
    assert match, f"no refusal message in live `check` output: {result.output!r}"
    supported = match.group(1)

    readme_text = README.read_text(encoding="utf-8")
    assert f"supports {supported}" in readme_text, (
        "README's supported-schema-version sentence does not match what `check` "
        f"actually printed just now ({supported!r})"
    )


def test_readme_matches_the_live_start_force_field_revert(tmp_path):
    """The README's re-declare-cost sentence must match what `start --force`
    actually does to a hand-edited field, not a description of intended
    behaviour.
    """
    urdf = FIXTURES / "testbot.urdf"
    contract = tmp_path / "lockwire.yaml"

    first = runner.invoke(app, ["start", str(urdf), "--out", str(contract)])
    assert first.exit_code == 0, first.output

    original = contract.read_text(encoding="utf-8")
    edited = original.replace("name: testbot", "name: HAND_EDITED_NAME", 1)
    assert edited != original, "fixture no longer has a robot.name line to hand-edit"
    contract.write_text(edited, encoding="utf-8")

    second = runner.invoke(
        app, ["start", str(urdf), "--out", str(contract), "--force"]
    )
    assert second.exit_code == 0, second.output

    after = contract.read_text(encoding="utf-8")
    assert "HAND_EDITED_NAME" not in after, (
        "start --force no longer silently reverts a hand-edited robot.name — "
        "update the README, this test documents the current behaviour"
    )

    readme_text = README.read_text(encoding="utf-8")
    assert "re-declare every fact in the lockfile by hand" in readme_text
    assert "silently overwritten back to the URDF-derived value" in readme_text
