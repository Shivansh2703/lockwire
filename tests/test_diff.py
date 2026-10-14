"""`lockwire diff` — the fact diff, and the git door underneath it (E0-34).

Every path `diff` can take is named here: no repository, an unknown ref, a ref
where the contract did not exist yet, nothing changed, and a dirty working
tree. Plus the one that matters most: a hostile ref name never reaches a shell.
"""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from lockwire import api
from lockwire import gitref
from lockwire.cli import app

from conftest import plain

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"

# rail.5v in the clean fixture, with provenance so the diff can cite a witness.
RAIL_5V = """    - id: rail.5v
      voltage: {value: 5, unit: V}
      capacity: {value: %s, unit: W}
      upstream: rail.22v
      efficiency: 0.85
      provenance:
        witness: human
        source: bench-measurement.pdf
        locked_at: 2026-01-01
"""

ORIGINAL_RAIL_5V = """    - id: rail.5v
      voltage: {value: 5, unit: V}
      capacity: {value: 5, unit: W}
      upstream: rail.22v
      efficiency: 0.85
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _contract_text(capacity_w: str) -> str:
    text = (FIXTURES / "contract_clean.yaml").read_text(encoding="utf-8")
    assert ORIGINAL_RAIL_5V in text, "fixture rail.5v block moved"
    return text.replace(ORIGINAL_RAIL_5V, RAIL_5V % capacity_w)


def _repo(tmp_path: Path, *capacities: str) -> Path:
    """A repo whose one contract is committed once per given rail.5v capacity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    for capacity in capacities:
        (repo / "lockwire.yaml").write_text(_contract_text(capacity), encoding="utf-8")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", f"capacity {capacity}", cwd=repo)
    return repo


def _run(repo: Path, *args: str):
    return runner.invoke(app, ["diff", *args, "--file", str(repo / "lockwire.yaml")])


def test_diff_marks_a_raised_limit(tmp_path):
    """A rail limit raised between two commits is named, with its provenance."""
    repo = _repo(tmp_path, "1", "8")

    result = _run(repo, "HEAD~1", "HEAD", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["changed"] is True
    (change,) = payload["changes"]
    assert change["path"] == "topology/rails/rail.5v/capacity/value"
    assert change["old"] == 1.0
    assert change["new"] == 8.0
    assert change["witness"] == "human"
    assert change["source"] == "bench-measurement.pdf"
    assert change["limit_raised"] is True
    # the raise cleared a rail-power finding: the verdict delta says so
    assert payload["findings_gone"], payload
    assert not payload["findings_new"]


def test_api_diff_matches_the_literal_expected_fact_change(tmp_path):
    """The library door, `api.diff`, called directly — not through the CLI at all.

    Anchored on literal values a test author typed, not on anything either door
    computes: the CLI's own JSON output is not consulted here (that would be a
    parity test, and parity between two doors that share `factdiff.fact_diff`
    proves only that they agree, not that either is right — PARITY-ANCHOR LAW).
    A rename or corruption in `api.diff`'s own call site (e.g. its returned key
    names), or a break in the shared `fact_diff`/`FactDiff.as_json` it calls,
    both make one of these literal asserts fail.
    """
    repo = _repo(tmp_path, "1", "8")

    payload = api.diff(repo / "lockwire.yaml", "HEAD~1", "HEAD")

    assert payload["changed"] is True
    (change,) = payload["changes"]
    assert change["path"] == "topology/rails/rail.5v/capacity/value"
    assert change["old"] == 1.0
    assert change["new"] == 8.0
    assert change["witness"] == "human"
    assert change["source"] == "bench-measurement.pdf"
    assert change["limit_raised"] is True
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["findings_gone"], payload
    assert not payload["findings_new"]


def test_api_diff_discovers_the_contract_repo_from_an_unrelated_cwd(tmp_path, monkeypatch):
    """Library callers need not run from the contract's repository."""
    repo = _repo(tmp_path, "1", "8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git("init", "-q", ".", cwd=unrelated)
    _git("config", "user.email", "test@example.com", cwd=unrelated)
    _git("config", "user.name", "test", cwd=unrelated)
    (unrelated / "README").write_text("unrelated\\n", encoding="utf-8")
    _git("add", "-A", cwd=unrelated)
    _git("commit", "-qm", "unrelated change", cwd=unrelated)
    monkeypatch.chdir(unrelated)

    payload = api.diff(repo / "lockwire.yaml", "HEAD~1", "HEAD")

    (change,) = payload["changes"]
    assert (change["old"], change["new"]) == (1.0, 8.0)


def test_diff_exit_code_flag(tmp_path):
    """--exit-code exits 1 on a dirty contract, 0 when nothing changed."""
    repo = _repo(tmp_path, "5")
    contract = repo / "lockwire.yaml"

    clean = _run(repo, "--exit-code")
    assert clean.exit_code == 0, clean.output
    assert "no fact changes" in plain(clean.output)

    contract.write_text(_contract_text("9"), encoding="utf-8")
    dirty = _run(repo, "--exit-code")
    assert dirty.exit_code == 1, dirty.output
    assert "limit raised" in plain(dirty.output)

    # without the flag a change is still just a report
    assert _run(repo).exit_code == 0


def test_diff_outside_a_repository_is_one_sentence(tmp_path):
    """No repo, no git history: one sentence, exit 0 — not a traceback."""
    contract = tmp_path / "lockwire.yaml"
    contract.write_text(_contract_text("5"), encoding="utf-8")

    result = runner.invoke(app, ["diff", "--file", str(contract), "--exit-code"])

    assert result.exit_code == 0, result.output
    assert "not a git repository" in plain(result.output)


def test_diff_two_paths_needs_no_repository(tmp_path):
    """Paths work in place of refs, and that road never touches git."""
    (tmp_path / "a.yaml").write_text(_contract_text("1"), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(_contract_text("8"), encoding="utf-8")

    result = runner.invoke(app, ["diff", str(tmp_path / "a.yaml"), str(tmp_path / "b.yaml")])

    assert result.exit_code == 0, result.output
    assert "topology/rails/rail.5v/capacity/value: 1.0 -> 8.0" in plain(result.output)


def test_diff_unknown_ref_is_named(tmp_path):
    repo = _repo(tmp_path, "5")

    result = _run(repo, "no-such-branch")

    assert result.exit_code == 2, result.output
    assert "no such ref 'no-such-branch'" in plain(result.output)


def test_diff_contract_absent_at_that_ref(tmp_path):
    """The ref exists but predates the contract: say so, don't invent a diff."""
    repo = _repo(tmp_path)  # no contract yet
    (repo / "README").write_text("first\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "no contract yet", cwd=repo)
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "lockwire.yaml").write_text(_contract_text("5"), encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "contract", cwd=repo)

    result = _run(repo, first)
    assert result.exit_code == 0, result.output
    assert "lockwire.yaml did not exist at" in plain(result.output)

    assert _run(repo, first, "--exit-code").exit_code == 1


def test_diff_missing_contract_file(tmp_path):
    repo = _repo(tmp_path, "5")
    (repo / "lockwire.yaml").unlink()

    result = _run(repo)

    assert result.exit_code == 2, result.output
    assert "no contract at" in plain(result.output)


def test_diff_ref_side_refuses_an_unsupported_schema_version(tmp_path):
    """The git-ref path refuses too — not just the on-disk path (E0-65 R2).

    Base regressed to a bare "diff failed:" line with no REFUSED word when the
    bad version came in over a ref rather than a file on disk. Commit a bad
    schema_version at HEAD (side b) and assert REFUSED, exit 2, and the word
    "side b" so a revert of the cli.py fix reddens this test.
    """
    repo = _repo(tmp_path, "5")
    contract = repo / "lockwire.yaml"
    bad_text = (FIXTURES / "contract_clean.yaml").read_text(encoding="utf-8").replace(
        'schema_version: "0.3"', 'schema_version: "0.1"'
    )
    contract.write_text(bad_text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "bad schema version", cwd=repo)

    result = _run(repo, "HEAD~1", "HEAD")

    assert result.exit_code == 2, result.output
    output = plain(result.output)
    assert "REFUSED" in output, output
    assert "side b" in output, output


def test_hostile_ref_name_cannot_reach_a_shell(tmp_path):
    """Argv, never a shell string: a ref that is a command stays a bad ref."""
    repo = _repo(tmp_path, "5")
    marker = tmp_path / "pwned"
    hostile = [
        f"; touch {marker}",
        f"$(touch {marker})",
        f"`touch {marker}`",
        f"--upload-pack=touch {marker}",
        "--output=/dev/null",
        "-C/tmp",
    ]

    for ref in hostile:
        # `--` so click hands the leading-dash cases through as the ref they are
        result = runner.invoke(app, ["diff", "--file", str(repo / "lockwire.yaml"), "--", ref])
        assert result.exit_code == 2, (ref, result.output)
        assert "no such ref" in plain(result.output), (ref, result.output)
        assert not marker.exists(), ref

    # the refusal is in gitref itself, not only in the CLI
    assert gitref.valid_ref("--upload-pack=x") is False
    assert gitref.valid_ref("") is False
    assert gitref.valid_ref("HEAD~1") is True


def test_repo_root_reads_a_worktree_whose_dotgit_is_a_file(tmp_path):
    """In a worktree `.git` is a FILE; repo detection must still work (and stop
    at the worktree, not the parent checkout)."""
    repo = _repo(tmp_path, "5")
    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "sidebranch", str(worktree), cwd=repo)

    assert (worktree / ".git").is_file()
    assert gitref.repo_root(worktree / "lockwire.yaml") == worktree.resolve()
    assert gitref.rev_parse("HEAD", repo=worktree) is not None
    assert gitref.repo_root(tmp_path) is None  # a plain directory is not a repo
