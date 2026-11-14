"""An anonymous edit records no witness; every surface prints that, never "None" or a crash."""

import json
import re
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app

NO_NAME = "no name on record"
runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _repo(tmp_path: Path, capacity: str = "5") -> Path:
    """A committed contract whose provenance blocks carry no witness line."""
    text = (FIXTURES / "contract_clean.yaml").read_text(encoding="utf-8")
    text = re.sub(r"^\s+witness: (?!live).*\n", "", text, flags=re.M)
    text = text.replace('schema_version: "0.3"', 'schema_version: "0.7"')
    path = tmp_path / "lockwire.yaml"
    dev = "  - id: dev.imu\n    model: mdl.imu.bmi088\n"
    assert dev in text, "fixture dev.imu block moved"
    text = text.replace(
        dev, dev + "    provenance:\n      source: imu.pdf\n      locked_at: 2026-01-01\n"
    )
    old = "      capacity: {value: 5, unit: W}\n      upstream: rail.22v\n      efficiency: 0.85\n"
    assert old in text, "fixture rail.5v block moved"
    new = (
        old.replace("value: 5,", f"value: {capacity},")
        + "      provenance:\n        source: bench.pdf\n        locked_at: 2026-01-01\n"
    )
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e.x", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_blame_prints_no_name_on_record(tmp_path):
    path = _repo(tmp_path)
    assert "witness: human" not in path.read_text()
    result = runner.invoke(app, ["blame", str(path)])
    assert result.exit_code == 0, result.output
    assert NO_NAME in result.output and "None" not in result.output
    rows = json.loads(runner.invoke(app, ["blame", str(path), "--json"]).output)
    assert NO_NAME in {row["witness"] for row in rows}


def test_describe_prints_no_name_on_record(tmp_path):
    path = _repo(tmp_path)
    result = runner.invoke(app, ["describe", str(path)])
    assert result.exit_code == 0, result.output
    assert f"witness: {NO_NAME}" in result.output


def test_describe_provenance_block_names_no_name():
    from lockwire.describe import _provenance_block
    from lockwire.schema import Provenance

    block = _provenance_block("instance", Provenance(source="s.pdf", locked_at="2026-01-01"))
    assert f"  - witness: {NO_NAME}" in block


def test_diff_prints_no_name_on_record(tmp_path):
    path = _repo(tmp_path)
    _git("init", "-q", ".", cwd=tmp_path)
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "a", cwd=tmp_path)
    _repo(tmp_path, "8")
    result = runner.invoke(app, ["diff", "HEAD", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert f"[{NO_NAME} · bench.pdf]" in result.output and "None" not in result.output


def _accepted_contract(tmp_path):
    from lockwire.schema import AcceptedFinding, Provenance, load_contract

    contract = load_contract(_repo(tmp_path))
    pin = AcceptedFinding(
        id="x/q",
        observed="1",
        reason="r",
        provenance=Provenance(source="s", locked_at="2026-01-01"),
    )
    return contract.model_copy(update={"accepted": [pin]})


def _finding():
    from lockwire.checks import Finding

    return Finding(severity="warning", quantity="x/q", observed="1", expected="2", action="a")


def test_accepted_findings_action_names_no_name(tmp_path):
    from lockwire.checks import _set_aside_accepted

    _, accepted = _set_aside_accepted(_accepted_contract(tmp_path), [_finding()])
    assert f"witness {NO_NAME}" in accepted[0].action


def test_page_accepted_rows_name_no_name(tmp_path):
    from lockwire.page import _accepted_rows

    (row,) = _accepted_rows(_accepted_contract(tmp_path), [_finding()])
    assert row["witness"] == NO_NAME


def test_fact_diff_lines_name_no_name():
    from lockwire.factdiff import FactDiff, FieldChange, render_lines

    diff = FactDiff(changes=[FieldChange("a/b", 1, 2, None, "s.pdf")])
    assert f"[{NO_NAME} · s.pdf]" in render_lines(diff)[0]
