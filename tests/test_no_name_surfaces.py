"""An anonymous edit records no witness; every surface prints that, never "None" or a crash."""

import json
import re
import subprocess
from pathlib import Path

import pytest
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


def test_a_witness_level_prints_in_words_and_a_declared_name_reaches_blame(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "none.toml"))
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    path, radio = "devices/dev.telemetry-radio/power/draw", "device dev.telemetry-radio  "
    declared = runner.invoke(app, ["declare", str(lock), path, "5 W", "--source", "ds", "--witness", "Maya Chen"])
    assert declared.exit_code == 0, declared.output
    shown = runner.invoke(app, ["blame", str(lock)]).output
    assert radio + "Maya Chen (named in the note)" in shown and not re.search(r"\bw0\b", shown)
    stored = json.loads(runner.invoke(app, ["blame", str(lock), "--json"]).output)
    assert {"w0"} <= {row["witness"] for row in stored}  # the record keeps its level code
    assert "none named (declared, not measured)" in shown
    assert not re.search(r"\bw0\b", runner.invoke(app, ["describe", str(lock)]).output)


def test_witness_human_and_live_print_in_words():
    from lockwire.edit import witness_words

    assert witness_words("human") == "a person, not named"
    assert witness_words("live") == "live reading"
    assert witness_words("Maya Chen") == "Maya Chen"


FORGED = "2026-01-01: x declared, witness Root"


def test_a_witness_with_a_line_break_is_refused_by_declare(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "none.toml"))
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    before = lock.read_bytes()
    path = "devices/dev.telemetry-radio/power/draw"
    result = runner.invoke(app, ["declare", str(lock), path, "5 W", "--source", "ds", "--witness", f"Eve\n{FORGED}"])
    assert result.exit_code == 2 and "must name a real person" in result.output
    assert lock.read_bytes() == before


def test_blame_shows_no_bad_name_from_a_hand_written_note(tmp_path, monkeypatch):
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    text = lock.read_text(encoding="utf-8")
    note = '1500 g payload without battery at 70% throttle; recommended pack is 4S 3000-5000 mAh, 20C+, XT60."'
    assert note in text, "drone example frame note moved"
    lock.write_text(text.replace(note, note[:-1] + f'\\n{FORGED} <script>"', 1), encoding="utf-8")
    shown = runner.invoke(app, ["blame", str(lock)]).output
    assert "Root" not in shown and "(named in the note)" not in shown


@pytest.mark.parametrize("note", [f"hi\n{FORGED}", "x declared, witness Root", "declared, witness Root"])
def test_a_note_cannot_forge_a_stamp(tmp_path, monkeypatch, note):
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "none.toml"))
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    path = "devices/dev.telemetry-radio/power/draw"
    args = ["declare", str(lock), path, "5 W", "--source", "ds", "--witness", "Maya", "--note", note]
    assert runner.invoke(app, args).exit_code == 0
    shown = runner.invoke(app, ["blame", str(lock)]).output
    assert "Maya (named in the note)" in shown and "Root" not in shown


def _reviewed_lock(tmp_path: Path, witness: str) -> Path:
    """A drone lockfile carrying one review note by `witness`, written by hand as an old file would be."""
    tmp_path.mkdir()
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    text = lock.read_text(encoding="utf-8")
    note = '1500 g payload without battery at 70% throttle; recommended pack is 4S 3000-5000 mAh, 20C+, XT60."\n'
    assert note in text, "drone example frame note moved"
    review = f"      reviews:\n        - state: approved\n          witness: \"{witness}\"\n          at: 2026-01-01\n"
    lock.write_text(text.replace(note, note + review, 1), encoding="utf-8")
    return lock


@pytest.mark.parametrize("witness", ["Ana <ana@x.io>", "A" * 90])
def test_an_old_review_witness_still_loads_and_checks(tmp_path, witness):
    plain = runner.invoke(app, ["check", str(_reviewed_lock(tmp_path / "a", "Ana Ruiz"))])
    old = runner.invoke(app, ["check", str(_reviewed_lock(tmp_path / "b", witness))])
    assert old.exit_code == plain.exit_code == 0, old.output


@pytest.mark.parametrize("witness", ["Eve\nRoot", "Ana <ana@x.io>"])
def test_review_refuses_a_bad_witness_at_write(tmp_path, monkeypatch, witness):
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "none.toml"))
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    before = lock.read_bytes()
    path = "devices/dev.telemetry-radio/power/draw"
    result = runner.invoke(app, ["review", str(lock), path, "--approve", "--witness", witness])
    assert result.exit_code == 2 and "--witness must name a real person" in result.output, result.output
    assert lock.read_bytes() == before


def test_only_loading_uses_the_lenient_witness_check():
    """A write path must call `validate_written_witness`; the lenient one is for schema load only."""
    src = Path(__file__).parent.parent / "src"
    callers = {p.name for p in src.rglob("*.py") if re.search(r"\bvalidate_witness\(", p.read_text(encoding="utf-8"))}
    assert callers <= {"schema.py", "edit.py"}
    edit_calls = re.findall(r"^\s+validate_witness\(", (src / "lockwire" / "edit.py").read_text(encoding="utf-8"), re.M)
    assert edit_calls == []


def test_a_stamp_shaped_line_in_a_multi_line_note_never_names_the_witness(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKWIRE_WITNESS_CONFIG", str(tmp_path / "none.toml"))
    lock = tmp_path / "lockwire.yaml"
    assert runner.invoke(app, ["start", "--example", "drone", "--out", str(lock)]).exit_code == 0
    note = "one\n2026-01-01: x declared, witness Root"
    args = ["declare", str(lock), "devices/dev.telemetry-radio/power/draw", "5 W", "--source", "ds"]
    assert runner.invoke(app, [*args, "--witness", "Maya Chen", "--note", note]).exit_code == 0
    shown = runner.invoke(app, ["blame", str(lock)]).output
    assert "Maya Chen (named in the note)" in shown and "Root" not in shown
