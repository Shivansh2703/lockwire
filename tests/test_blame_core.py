"""Fact-only provenance attribution: no API or CLI transport in this box."""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from conftest import FIXTURES
from lockwire.blame import rows


X500 = FIXTURES / "x500" / "contract.yaml"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True, env=env
    ).stdout


def _init(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")


def _commit(repo: Path, message: str, *, instant: int = 1_700_000_000) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": f"{instant} +0000",
        "GIT_COMMITTER_DATE": f"{instant} +0000",
    }
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-qm", message, env=env)
    return _git(repo, "rev-parse", "HEAD").strip()


def _provenance(node: object) -> list[dict[str, object]]:
    if isinstance(node, dict):
        found = [node["provenance"]] if isinstance(node.get("provenance"), dict) else []
        return found + [
            item
            for key, value in node.items()
            if key != "provenance"
            for item in _provenance(value)
        ]
    if isinstance(node, list):
        return [item for value in node for item in _provenance(value)]
    return []


def test_rows_conserve_each_provenance_record_and_its_stored_default_marker():
    expected = _provenance(YAML(typ="safe").load(X500))
    actual, in_repository = rows(X500)
    assert in_repository is True
    assert Counter((row["witness"], row["source"], row["locked_at"]) for row in actual) == Counter(
        (str(row["witness"]), str(row["source"]), str(row["locked_at"])) for row in expected
    )


@pytest.mark.parametrize("witness", ["Priya (default)", "Priya"])
def test_rows_report_only_the_witness_marker_stored_in_the_contract(tmp_path: Path, witness: str):
    contract = tmp_path / "contract.yaml"
    text = X500.read_text(encoding="utf-8").replace("witness: w0", f"witness: {witness}", 1)
    contract.write_text(text, encoding="utf-8")
    actual, _ = rows(contract)
    assert next(row for row in actual if row["witness"] == witness)["witness"] == witness


def test_rows_are_honest_outside_a_repo_and_for_an_untracked_file(tmp_path: Path):
    outside = tmp_path / "outside.yaml"
    shutil.copy(X500, outside)
    outside_rows, in_repository = rows(outside)
    assert in_repository is False
    assert all("last_changing_commit" not in row for row in outside_rows)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    contract = repo / "untracked.yaml"
    shutil.copy(X500, contract)
    untracked_rows, in_repository = rows(contract)
    assert in_repository is True
    assert all("last_changing_commit" not in row for row in untracked_rows)


def test_witness_only_edit_owns_the_whole_provenance_block(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    contract = repo / "contract.yaml"
    shutil.copy(X500, contract)
    _commit(repo, "base")
    contract.write_text(
        contract.read_text(encoding="utf-8").replace("witness: w0", "witness: w1", 1),
        encoding="utf-8",
    )
    changed = _commit(repo, "witness only")
    actual, _ = rows(contract)
    assert next(row for row in actual if row["witness"] == "w1")["last_changing_commit"] == changed


def test_rename_and_unrelated_later_edit_do_not_steal_attribution(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    old = repo / "old.yaml"
    shutil.copy(X500, old)
    _commit(repo, "base")
    old.write_text(
        old.read_text(encoding="utf-8").replace("witness: w0", "witness: w1", 1), encoding="utf-8"
    )
    witness_commit = _commit(repo, "witness only")
    _git(repo, "mv", "old.yaml", "new.yaml")
    _commit(repo, "rename")
    new = repo / "new.yaml"
    new.write_text(
        new.read_text(encoding="utf-8").replace("id: x500-v2-px4", "id: x500-v2-px4-r2", 1),
        encoding="utf-8",
    )
    _commit(repo, "unrelated")
    actual, _ = rows(new)
    assert (
        next(row for row in actual if row["witness"] == "w1")["last_changing_commit"]
        == witness_commit
    )

    program = (
        "import json, sys; from lockwire.blame import rows; "
        "print(json.dumps(rows(Path(sys.argv[1]))[0], sort_keys=True))"
    )
    # The parent process's hash seed cannot prove set-order independence. Each
    # child imports the module fresh with the requested seed.
    outputs = []
    for seed in range(1, 7):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; " + program, str(new)],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        outputs.append(json.loads(result.stdout))
    assert all(output == actual for output in outputs)


def test_rows_refuse_an_invalid_contract_before_reading_its_source_positions(tmp_path: Path):
    contract = tmp_path / "invalid.yaml"
    contract.write_text('schema_version: "0.1"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        rows(contract)
