"""The real git-backed producer for the Reference history drawer."""

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import FIXTURES
from lockwire import api
from lockwire import gitref
from lockwire.api_payload import BlamePayload
from lockwire import blame as blame_history
from lockwire.blame import rows
from lockwire.cli import app

from typer.testing import CliRunner

runner = CliRunner()

ORIGINAL = """    - id: rail.5v
      voltage: {value: 5, unit: V}
      capacity: {value: 5, unit: W}
      upstream: rail.22v
      efficiency: 0.85
"""
WITH_PROVENANCE = """    - id: rail.5v
      voltage: {value: 5, unit: V}
      capacity: {value: %s, unit: W}
      upstream: rail.22v
      efficiency: 0.85
      provenance:
        witness: human
        source: bench-measurement.pdf
        locked_at: 2026-01-01
"""

PIXHAWK_DEVICE_PROVENANCE = """    owner: avionics-integration
    provenance:
      witness: w0
      source: docs.holybro.com/autopilot/pixhawk-6c/technical-specification
      locked_at: 2026-07-25
"""

PIXHAWK_DEVICE_COMPARED_FROM = (
    PIXHAWK_DEVICE_PROVENANCE + "      compared_from: x500.flight-controller.pixhawk-6c\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    source = (FIXTURES / "contract_clean.yaml").read_text(encoding="utf-8")
    assert ORIGINAL in source
    for capacity in ("1", "8"):
        (repo / "lockwire.yaml").write_text(
            source.replace(ORIGINAL, WITH_PROVENANCE % capacity), encoding="utf-8"
        )
        _git(repo, "add", "lockwire.yaml")
        _git(repo, "commit", "-qm", f"capacity {capacity}")
    return repo


def test_log_is_the_real_path_scoped_git_history(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "README").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "unrelated change")

    entries = api.log(repo / "lockwire.yaml")

    assert [entry["message"] for entry in entries] == ["capacity 8", "capacity 1"]
    assert [entry["verdict"] for entry in entries] == ["GO", "NO-GO"]
    assert all(
        set(entry) == {"sha", "author", "date", "message", "verdict", "fact_diff"}
        for entry in entries
    )
    newest, oldest = entries
    assert oldest["fact_diff"] is None
    assert newest["fact_diff"]["changes"] == [
        {
            "path": "topology/rails/rail.5v/capacity/value",
            "old": 1.0,
            "new": 8.0,
            "source": "bench-measurement.pdf",
            "witness": "human",
            "limit_raised": True,
        }
    ]


def test_log_api_and_cli_follow_a_renamed_contract_and_keep_the_fact_diff(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    old = repo / "lockwire.yaml"
    _git(repo, "mv", old.name, "renamed.yaml")
    _git(repo, "commit", "-qm", "rename")
    new = repo / "renamed.yaml"
    new.write_text(
        new.read_text(encoding="utf-8").replace("capacity: {value: 8", "capacity: {value: 13"),
        encoding="utf-8",
    )
    _git(repo, "add", new.name)
    _git(repo, "commit", "-qm", "capacity 13")
    monkeypatch.chdir(repo)

    entries = api.log(new)

    assert [entry["message"] for entry in entries] == [
        "capacity 13",
        "rename",
        "capacity 8",
        "capacity 1",
    ]
    assert entries[0]["fact_diff"]["changes"] == [
        {
            "path": "topology/rails/rail.5v/capacity/value",
            "old": 8.0,
            "new": 13.0,
            "source": "bench-measurement.pdf",
            "witness": "human",
            "limit_raised": True,
        }
    ]
    assert entries[1]["fact_diff"] == {
        "changed": False,
        "changes": [],
        "added": [],
        "removed": [],
        "findings_new": [],
        "findings_gone": [],
    }
    assert entries[-1]["fact_diff"] is None

    result = runner.invoke(app, ["log", "--json", "--", str(new)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == entries


def test_log_uses_git_revision_ranges_and_names_unknown_refs(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    oldest = _git(repo, "rev-list", "--max-parents=0", "HEAD")

    assert [entry["message"] for entry in api.log(repo / "lockwire.yaml", f"{oldest}..HEAD")] == [
        "capacity 8"
    ]
    with pytest.raises(ValueError, match="no such ref"):
        api.log(repo / "lockwire.yaml", "missing..HEAD")


def test_log_outside_a_repository_is_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    contract = tmp_path / "lockwire.yaml"
    contract.write_text("schema_version: 1\n", encoding="utf-8")

    assert api.log(contract) == []


def test_blame_library_delegates_to_the_fact_row_producer(monkeypatch, tmp_path):
    sentinel = ([{"path": "declared/fact"}], True)
    monkeypatch.setattr(blame_history, "rows", lambda contract: sentinel)

    assert api.blame(tmp_path / "lockwire.yaml") == sentinel


def test_blame_cli_transports_the_library_rows(monkeypatch, tmp_path):
    sentinel = ([{"path": "declared/fact", "witness": "tester", "source": "bench"}], True)
    monkeypatch.setattr("lockwire.api.blame", lambda contract: sentinel)

    result = runner.invoke(app, ["blame", str(tmp_path / "lockwire.yaml"), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == sentinel[0]


def test_blame_api_and_cli_preserve_the_real_git_rows(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    expected = rows(repo / "lockwire.yaml")

    actual = api.blame(repo / "lockwire.yaml")
    assert "blame" in api.FUNCTIONS
    assert isinstance(actual, tuple) and len(actual) == 2
    assert isinstance(actual[0], list) and all(isinstance(row, dict) for row in actual[0])
    assert isinstance(actual[1], bool)
    assert actual == expected
    assert BlamePayload.model_validate(actual[0]).model_dump(exclude_unset=True) == actual[0]
    result = runner.invoke(app, ["blame", str(repo / "lockwire.yaml"), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected[0]


def test_blame_publicly_carries_compared_from_identity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    contract = repo / "lockwire.yaml"
    contract.write_text(
        (FIXTURES / "x500" / "contract.yaml")
        .read_text(encoding="utf-8")
        .replace(PIXHAWK_DEVICE_PROVENANCE, PIXHAWK_DEVICE_COMPARED_FROM, 1),
        encoding="utf-8",
    )
    _git(repo, "add", "lockwire.yaml")
    _git(repo, "commit", "-qm", "record catalog comparison")
    monkeypatch.chdir(repo)

    rows, _ = api.blame(contract)
    row = next(row for row in rows if row.get("compared_from"))
    assert row["compared_from"] == "x500.flight-controller.pixhawk-6c"
    assert BlamePayload.model_validate(rows).model_dump(exclude_unset=True) == rows

    json_result = runner.invoke(app, ["blame", str(contract), "--json"])
    assert json_result.exit_code == 0, json_result.output
    assert next(item for item in json.loads(json_result.output) if item.get("compared_from")) == row

    text_result = runner.invoke(app, ["blame", str(contract)])
    assert text_result.exit_code == 0, text_result.output
    assert "compared_from=x500.flight-controller.pixhawk-6c" in text_result.output


def test_blame_public_transport_preserves_rows_across_rename_and_unrelated_edit(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    old = repo / "lockwire.yaml"
    old.write_text(
        old.read_text(encoding="utf-8").replace("witness: human", "witness: human-w1"),
        encoding="utf-8",
    )
    _git(repo, "add", "lockwire.yaml")
    _git(repo, "commit", "-qm", "witness")
    witness = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "lockwire.yaml", "renamed.yaml")
    _git(repo, "commit", "-qm", "rename")
    new = repo / "renamed.yaml"
    (repo / "README").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "unrelated")
    monkeypatch.chdir(repo)

    expected = rows(new)
    assert (
        next(row for row in expected[0] if row["witness"] == "human-w1")["last_changing_commit"]
        == witness
    )
    assert api.blame(new) == expected
    assert (
        BlamePayload.model_validate(api.blame(new)[0]).model_dump(exclude_unset=True) == expected[0]
    )

    json_result = runner.invoke(app, ["blame", str(new), "--json"])
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output) == expected[0]

    text_result = runner.invoke(app, ["blame", str(new)])
    assert text_result.exit_code == 0, text_result.output
    rendered = [line for line in text_result.output.splitlines() if line]
    assert len(rendered) == len(expected[0])
    assert set(rendered) == {
        f"{row['path']}  {row['witness']}  {row['source']}  "
        f"{row.get('last_changing_commit', 'unattributed')}"
        for row in expected[0]
    }
    assert "not a git repository" not in text_result.output


def test_follow_is_single_path_full_history_only_and_retains_rename_attribution(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    old = repo / "old.yaml"
    old.write_text(
        (FIXTURES / "x500" / "contract.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(repo, "add", "old.yaml")
    _git(repo, "commit", "-qm", "base")
    old.write_text(
        old.read_text(encoding="utf-8").replace("witness: w0", "witness: w1", 1), encoding="utf-8"
    )
    _git(repo, "add", "old.yaml")
    _git(repo, "commit", "-qm", "witness")
    witness = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "old.yaml", "new.yaml")
    _git(repo, "commit", "-qm", "rename")
    new = repo / "new.yaml"
    history = gitref.log(None, ["new.yaml"], repo=repo, follow=True)
    assert history is not None and witness in [row[0] for row in history]
    assert gitref.log("HEAD~1..HEAD", ["new.yaml"], repo=repo, follow=True) is None
    assert gitref.log(None, ["new.yaml", "other.yaml"], repo=repo, follow=True) is None
    actual, _ = rows(new)
    assert next(row for row in actual if row["witness"] == "w1")["last_changing_commit"] == witness


@pytest.mark.parametrize(
    "field,value", [("source", 123), ("witness", None), ("value", []), ("invented", "metadata")]
)
def test_blame_carrier_rejects_malformed_actual_rows(tmp_path, monkeypatch, field, value):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    actual, _ = api.blame(repo / "lockwire.yaml")
    malformed = [dict(actual[0], **{field: value}), *actual[1:]]
    with pytest.raises(ValidationError):
        BlamePayload.model_validate(malformed)


def test_blame_carrier_preserves_unknown_commit_without_inventing_identity(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text((FIXTURES / "x500" / "contract.yaml").read_text())
    actual, in_repository = api.blame(contract)
    assert not in_repository
    assert all("last_changing_commit" not in row for row in actual)
    assert BlamePayload.model_validate(actual).model_dump(exclude_unset=True) == actual
