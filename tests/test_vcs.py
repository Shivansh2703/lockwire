"""Commit the contract by path, and undo is git from a
source it names — `lockwire.vcs`.

Commit half: four mutation-bite branches (the accept line): no commit without a witness in the message; the pathspec always
narrows the commit to exactly the requested paths, so a dirty neighbour is
left alone; `dry_run` never touches the tree; and no code path here ever
pushes.

Undo half: the anchor in every restore test is **git itself** —
``git show HEAD:<file>`` run as a separate command and diffed against the
file on disk — never ``lockwire.vcs`` asked what it just did.

Parity-anchor law throughout: assertions run `git log` / `git status` /
`git show` as separate subprocess calls compared against literals, never
re-read through `vcs.py` or `gitref.py` themselves.
"""

from __future__ import annotations
import copy
import multiprocessing
import shutil
import subprocess
from pathlib import Path
import pytest
from lockwire import api
from lockwire import gitref, vcs
from lockwire.edit import (
    Change,
    EditConflict,
    _contract_lock,
    apply_changes,
    contract_revision,
)
from lockwire.schema import load_contract


WITNESS_MESSAGE = (
    "declare rail.pm02-5v capacity 20.8 W — source: PM02 datasheet p.2 — witness: Maya Chen"
)

HEAD_TEXT = "name: base\n"
STAGED_TEXT = HEAD_TEXT + "# staged\n"
WORKING_TEXT = STAGED_TEXT + "# working\n"
X500_CONTRACT = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"


def _hold_lock(contract: str, ready, release) -> None:
    with _contract_lock(Path(contract)):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("test did not release the canonical contract lock")


def _run_undo(
    plan: dict,
    contract: str | None,
    revision: str | None,
    started,
    done,
    result,
    start=None,
) -> None:
    started.set()
    try:
        if start is not None and not start.wait(15):
            raise TimeoutError("test did not start the concurrent writers")
        vcs.apply_undo(plan, contract=contract, expected_revision=revision)
    except EditConflict:
        result.put(("undo", "conflict"))
    except BaseException as error:  # noqa: BLE001 — child reports failures to the parent test
        result.put(("undo", f"error: {error!r}"))
    else:
        result.put(("undo", "ok"))
    finally:
        done.set()


def _run_edit(contract: str, revision: str, start, result) -> None:
    if not start.wait(15):
        result.put(("edit", "error: start timed out"))
        return
    try:
        apply_changes(
            Path(contract),
            [
                Change(
                    "topology/rails/rail.pm02-5v/capacity",
                    "20 W",
                    "evidence/spec.pdf",
                    "Pat",
                )
            ],
            expected_revision=revision,
        )
    except EditConflict:
        result.put(("edit", "conflict"))
    except BaseException as error:  # noqa: BLE001 — child reports failures to the parent test
        result.put(("edit", f"error: {error!r}"))
    else:
        result.put(("edit", "ok"))


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _gu_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose contract has three *different* contents at once.

    HEAD, the index and the working tree all differ — that is what makes
    blocker 5 visible: a bare ``git restore`` would hand back the staged copy
    and still exit 0.
    """
    root = tmp_path / "work"
    root.mkdir()
    _gu_git(root, "init", "-q", ".")
    _gu_git(root, "config", "user.email", "test@example.com")
    _gu_git(root, "config", "user.name", "Test")
    (root / "lockwire.yaml").write_text(HEAD_TEXT, encoding="utf-8")
    _gu_git(root, "add", "-A")
    _gu_git(root, "commit", "-qm", "base")
    (root / "lockwire.yaml").write_text(STAGED_TEXT, encoding="utf-8")
    _gu_git(root, "add", "lockwire.yaml")
    (root / "lockwire.yaml").write_text(WORKING_TEXT, encoding="utf-8")
    return root


@pytest.fixture
def aliased_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A valid contract reached by both its canonical path and a symlink alias."""
    root = tmp_path / "alias-work"
    contract = root / "contracts" / "lockwire.yaml"
    contract.parent.mkdir(parents=True)
    shutil.copyfile(X500_CONTRACT, contract)
    _gu_git(root, "init", "-q", ".")
    _gu_git(root, "config", "user.email", "test@example.com")
    _gu_git(root, "config", "user.name", "Test")
    _gu_git(root, "add", "contracts/lockwire.yaml")
    _gu_git(root, "commit", "-qm", "base")

    contract.write_bytes(b"# current formatting-only revision\n" + contract.read_bytes())
    alias = root / "alias.yaml"
    alias.symlink_to(Path("contracts") / "lockwire.yaml")
    return root, contract, alias


@pytest.fixture
def scratch_repo(tmp_path):
    """A repo with a base commit, then two dirty files: the contract and a stranger."""
    repo = tmp_path
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    contract = repo / "lockwire.yaml"
    contract.write_text("schema_version: 1\nrails: []\n", encoding="utf-8")
    other = repo / "other.txt"
    other.write_text("dirt\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    contract.write_text("schema_version: 1\nrails: []\n# edit\n", encoding="utf-8")
    other.write_text("dirt\nmore\n", encoding="utf-8")
    return repo


def test_a_message_mentioning_remote_is_not_mistaken_for_a_push(scratch_repo):
    out = vcs.commit_contract(
        scratch_repo / "lockwire.yaml",
        message="relocate remote antenna mass; witness: shivansh",
        paths=[str(scratch_repo / "lockwire.yaml")],
        dry_run=True,
    )
    assert out["argv"][:2] == ["git", "commit"]


def test_a_message_ending_in_a_bare_witness_label_is_refused(scratch_repo):
    with pytest.raises(ValueError, match="witness"):
        vcs.commit_contract(
            scratch_repo / "lockwire.yaml",
            message="declare rail.pm02-5v capacity 20.8 W; witness:",
            paths=[str(scratch_repo / "lockwire.yaml")],
        )


def test_a_commit_message_without_the_witness_is_refused(scratch_repo):
    with pytest.raises(ValueError, match="witness"):
        vcs.commit_contract(
            scratch_repo / "lockwire.yaml",
            message="declare rail.pm02-5v capacity 20.8 W — source: PM02 datasheet p.2",
            paths=[str(scratch_repo / "lockwire.yaml")],
        )
    # nothing was committed — the tree is exactly as dirty as before the call
    status = _git(["status", "--porcelain"], cwd=scratch_repo)
    assert "lockwire.yaml" in status
    assert "other.txt" in status


def test_only_the_contract_and_its_sidecars_are_committed(scratch_repo):
    _git(["add", "other.txt"], cwd=scratch_repo)

    result = vcs.commit_contract(
        scratch_repo / "lockwire.yaml",
        message=WITNESS_MESSAGE,
        paths=[str(scratch_repo / "lockwire.yaml")],
        expected_revision=contract_revision(scratch_repo / "lockwire.yaml"),
    )
    assert result["committed"] is True
    assert result["argv"][:2] == ["git", "commit"]
    assert result["argv"][-2:] == ["--", "lockwire.yaml"]

    touched = _git(["log", "-1", "--name-only", "--format="], cwd=scratch_repo).split()
    assert touched == ["lockwire.yaml"], touched

    status = _git(["status", "--porcelain"], cwd=scratch_repo).splitlines()
    tracked_or_staged = {line[3:] for line in status if not line.startswith("?? ")}
    untracked = {line[3:] for line in status if line.startswith("?? ")}
    assert tracked_or_staged == {"other.txt"}  # staged neighbour remains outside the commit
    assert untracked == {".lockwire.yaml.lock"}  # shared lock inode is intentionally retained
    assert result["untouched"] == ["other.txt"]

    body = _git(["log", "-1", "--format=%B"], cwd=scratch_repo)
    assert "source: PM02 datasheet p.2" in body
    assert "witness: Maya Chen" in body


def test_commit_refuses_missing_or_stale_raw_revision_without_touching_git_state(scratch_repo):
    contract = scratch_repo / "lockwire.yaml"
    before_head = _git(["rev-parse", "HEAD"], cwd=scratch_repo)
    before_status = _git(["status", "--porcelain"], cwd=scratch_repo)
    before_bytes = contract.read_bytes()
    lock_path = contract.with_name(f".{contract.name}.lock")
    assert not lock_path.exists()

    with pytest.raises(EditConflict, match="expected_revision is required"):
        vcs.commit_contract(contract, message=WITNESS_MESSAGE, paths=[str(contract)])
    assert _git(["rev-parse", "HEAD"], cwd=scratch_repo) == before_head
    assert _git(["status", "--porcelain"], cwd=scratch_repo) == before_status
    assert contract.read_bytes() == before_bytes
    assert not lock_path.exists()

    stale_revision = contract_revision(contract)
    contract.write_bytes(b"# formatting only\n" + before_bytes)
    before_status = _git(["status", "--porcelain"], cwd=scratch_repo)
    before_bytes = contract.read_bytes()
    with pytest.raises(EditConflict, match="contract changed since it was opened"):
        vcs.commit_contract(
            contract,
            message=WITNESS_MESSAGE,
            paths=[str(contract)],
            expected_revision=stale_revision,
        )
    assert _git(["rev-parse", "HEAD"], cwd=scratch_repo) == before_head
    assert _git(["status", "--porcelain"], cwd=scratch_repo) == before_status
    assert contract.read_bytes() == before_bytes
    assert not lock_path.exists()


def test_dry_run_shows_the_argv_and_commits_nothing(scratch_repo):
    before = _git(["status", "--porcelain"], cwd=scratch_repo)
    before_log = _git(["log", "--oneline"], cwd=scratch_repo)

    result = vcs.commit_contract(
        scratch_repo / "lockwire.yaml",
        message=WITNESS_MESSAGE,
        paths=[str(scratch_repo / "lockwire.yaml")],
        dry_run=True,
    )
    assert result["committed"] is False
    assert result["sha"] is None
    assert result["argv"][:2] == ["git", "commit"]
    assert result["argv"][-2:] == ["--", "lockwire.yaml"]

    assert _git(["status", "--porcelain"], cwd=scratch_repo) == before
    assert _git(["log", "--oneline"], cwd=scratch_repo) == before_log


def test_no_code_path_here_pushes():
    """Sweeps the whole door, not one function — the claim is `vcs.py` plus
    the git door it calls through.

    pushing is the owner's hand, never the board's. Two checks make the
    whole-module claim: `vcs.py` never shells out except through `gitref`
    (no `subprocess` import here), and `gitref.py`, the one door,
    exposes no argv that names `push`, `remote` or `--set-upstream`.
    """
    vcs_source = Path(vcs.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in vcs_source
    assert "subprocess." not in vcs_source

    gitref_source = Path(gitref.__file__).read_text(encoding="utf-8")
    for marker in ('"push"', "'push'", '"remote"', "'remote'", "--set-upstream"):
        assert marker not in gitref_source, f"gitref.py builds an argv with {marker}"


def test_outside_a_repository_commit_returns_the_one_sentence_and_does_nothing(tmp_path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text("schema_version: 1\nrails: []\n", encoding="utf-8")
    result = vcs.commit_contract(
        contract,
        message=WITNESS_MESSAGE,
        paths=[str(contract)],
    )
    assert result == {
        "argv": [],
        "committed": False,
        "sha": None,
        "untouched": [],
        "reason": vcs.NOT_A_REPO,
    }
    assert not contract.with_suffix(".yaml.bak").exists()


def test_a_path_outside_the_repository_is_refused(scratch_repo, tmp_path):
    outside = tmp_path.parent / "elsewhere.yaml"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the repository"):
        vcs.commit_contract(
            scratch_repo / "lockwire.yaml",
            message=WITNESS_MESSAGE,
            paths=[str(outside)],
        )


def test_undo_restores_from_head_not_the_index(repo: Path):
    """Blocker 5: plain ``git restore`` defaults to the index, not HEAD.

    The fixture deliberately holds a staged copy that differs from **both** HEAD
    and the working tree. Without that staged copy the test would pass with the
    bug present — restoring the index and restoring HEAD would look the same.
    The argv must carry ``--source=``, and the file afterwards must equal
    ``git show HEAD:lockwire.yaml`` (git is the anchor), not the staged text.
    """
    plan = vcs.undo_plan(repo / "lockwire.yaml")
    assert plan["kind"] == "restore"
    assert "--source=HEAD" in plan["argv"]
    assert "restore" in plan["argv"] and plan["argv"][0] == "restore"

    vcs.apply_undo(plan)

    on_disk = (repo / "lockwire.yaml").read_text(encoding="utf-8")
    assert on_disk == _gu_git(repo, "show", "HEAD:lockwire.yaml")
    assert on_disk != STAGED_TEXT, "restored the staged copy — this is blocker 5"


def test_undo_marks_the_pre_undo_result_stale(aliased_repo):
    """Undo restores the bytes but a result taken before it must say stale."""
    repo, contract, _alias = aliased_repo
    pre_undo_result = api.check(contract)
    vcs.apply_undo(vcs.undo_plan(contract))
    assert contract.read_bytes() == _gu_git(repo, "show", "HEAD:contracts/lockwire.yaml").encode()
    assert api.is_stale(pre_undo_result, contract) is True
    assert api.is_stale(api.check(contract), contract) is False


@pytest.mark.parametrize("app_style", [True, False], ids=["app-alias-token", "library-plan-only"])
def test_alias_undo_waits_for_the_canonical_contract_lock(aliased_repo, app_style):
    repo, contract, alias = aliased_repo
    plan = vcs.undo_plan(alias)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    started = ctx.Event()
    done = ctx.Event()
    result = ctx.Queue()
    holder = ctx.Process(target=_hold_lock, args=(str(contract), ready, release))
    undo = ctx.Process(
        target=_run_undo,
        args=(
            plan,
            str(alias) if app_style else None,
            contract_revision(alias) if app_style else None,
            started,
            done,
            result,
        ),
    )

    holder.start()
    assert ready.wait(10)
    undo.start()
    try:
        assert started.wait(10)
        assert not done.wait(0.5), "alias Undo bypassed the canonical target lock"
        assert not (repo / ".alias.yaml.lock").exists()
    finally:
        release.set()
        holder.join(10)
        undo.join(10)
        for process in (holder, undo):
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert holder.exitcode == 0
    assert undo.exitcode == 0
    assert result.get(timeout=5) == ("undo", "ok")
    assert contract.read_bytes() == _gu_git(repo, "show", "HEAD:contracts/lockwire.yaml").encode()
    assert alias.is_symlink()
    assert (contract.parent / ".lockwire.yaml.lock").is_file()
    assert not (repo / ".alias.yaml.lock").exists()


def test_alias_undo_and_original_edit_with_one_revision_have_exactly_one_winner(aliased_repo):
    repo, contract, alias = aliased_repo
    plan = vcs.undo_plan(alias)
    revision = contract_revision(alias)
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    undo_started = ctx.Event()
    undo_done = ctx.Event()
    result = ctx.Queue()
    edit = ctx.Process(target=_run_edit, args=(str(contract), revision, start, result))
    undo = ctx.Process(
        target=_run_undo,
        args=(plan, str(alias), revision, undo_started, undo_done, result, start),
    )

    edit.start()
    undo.start()
    try:
        assert undo_started.wait(10)
        start.set()
        outcomes = dict([result.get(timeout=20), result.get(timeout=20)])
    finally:
        start.set()
        edit.join(10)
        undo.join(10)
        for process in (edit, undo):
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert edit.exitcode == 0
    assert undo.exitcode == 0
    assert sorted(outcomes.values()) == ["conflict", "ok"]
    assert alias.is_symlink()
    assert not (repo / ".alias.yaml.lock").exists()
    if outcomes["undo"] == "ok":
        assert (
            contract.read_bytes() == _gu_git(repo, "show", "HEAD:contracts/lockwire.yaml").encode()
        )
    else:
        edited = contract.read_bytes()
        assert b"evidence/spec.pdf" in edited
        assert str(repo).encode() not in edited
        load_contract(contract)


def test_alias_plan_and_apply_keep_canonical_relative_paths_and_relative_sources(aliased_repo):
    repo, contract, alias = aliased_repo
    link_target = alias.readlink()
    apply_changes(
        alias,
        [
            Change(
                "topology/rails/rail.pm02-5v/capacity",
                "20 W",
                "evidence/spec.pdf",
                "Pat",
            )
        ],
        expected_revision=contract_revision(alias),
    )
    edited = contract.read_bytes()
    assert b"evidence/spec.pdf" in edited
    assert str(repo).encode() not in edited

    plan = vcs.undo_plan(alias)
    assert plan["scope"] == ["contracts/lockwire.yaml"]
    assert plan["argv"][-1] == "contracts/lockwire.yaml"
    vcs.apply_undo(plan, contract=alias, expected_revision=contract_revision(alias))

    assert contract.read_bytes() == _gu_git(repo, "show", "HEAD:contracts/lockwire.yaml").encode()
    assert alias.is_symlink()
    assert alias.readlink() == link_target
    assert not (repo / ".alias.yaml.lock").exists()


def test_alias_undo_apply_failure_preserves_target_alias_and_plan(aliased_repo, monkeypatch):
    repo, contract, alias = aliased_repo
    plan = vcs.undo_plan(alias)
    proposed = copy.deepcopy(plan)
    target_before = contract.read_bytes()
    link_before = alias.readlink()
    original_run = gitref.run

    def fail_planned_apply(args, *, repo=None):
        if args == plan["argv"]:
            return 1, "injected apply failure"
        return original_run(args, repo=repo)

    monkeypatch.setattr(gitref, "run", fail_planned_apply)
    with pytest.raises(vcs.UndoRefused, match="failed"):
        vcs.apply_undo(plan, contract=alias, expected_revision=contract_revision(alias))

    assert contract.read_bytes() == target_before
    assert alias.is_symlink()
    assert alias.readlink() == link_before
    assert alias.read_bytes() == target_before
    assert plan == proposed
    assert not (repo / ".alias.yaml.lock").exists()


def test_apply_undo_refuses_a_contract_other_than_the_planned_target(repo: Path):
    plan = vcs.undo_plan(repo / "lockwire.yaml")
    other = repo / "other.yaml"
    other.write_text("schema_version: 1\nrails: []\n", encoding="utf-8")
    contract_before = (repo / "lockwire.yaml").read_bytes()
    other_before = other.read_bytes()

    with pytest.raises(vcs.UndoRefused, match="not the contract targeted"):
        vcs.apply_undo(plan, contract=other, expected_revision=contract_revision(other))

    assert (repo / "lockwire.yaml").read_bytes() == contract_before
    assert other.read_bytes() == other_before


def test_undo_names_the_scope_it_touches_before_running(repo: Path):
    """The plan states its scope, and running it touches nothing outside that scope.

    A neighbouring file's uncommitted change — staged, unstaged and untracked —
    survives the undo untouched.
    """
    (repo / "notes.txt").write_text("tracked, then edited\n", encoding="utf-8")
    _gu_git(repo, "add", "notes.txt")
    _gu_git(repo, "commit", "-qm", "notes", "--", "notes.txt")
    (repo / "notes.txt").write_text("UNSAVED WORK\n", encoding="utf-8")
    (repo / "staged.txt").write_text("STAGED WORK\n", encoding="utf-8")
    _gu_git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("UNTRACKED WORK\n", encoding="utf-8")

    plan = vcs.undo_plan(repo / "lockwire.yaml")
    assert plan["scope"] == ["lockwire.yaml"]
    assert "lockwire.yaml" in plan["sentence"]

    vcs.apply_undo(plan)

    assert (repo / "notes.txt").read_text(encoding="utf-8") == "UNSAVED WORK\n"
    assert (repo / "staged.txt").read_text(encoding="utf-8") == "STAGED WORK\n"
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "UNTRACKED WORK\n"


def test_a_committed_write_is_undone_by_revert_naming_the_other_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """Once the write is committed, restore is the wrong verb: the offer is revert,
    and its scope names every other file that commit touched."""
    # Hermetic identity: the revert commit must not pick up the machine's global git config.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")
    _gu_git(repo, "add", "lockwire.yaml")
    (repo / "sibling.txt").write_text("came with the same commit\n", encoding="utf-8")
    _gu_git(repo, "add", "sibling.txt")
    _gu_git(repo, "commit", "-qm", "the write")

    plan = vcs.undo_plan(repo / "lockwire.yaml")
    assert plan["kind"] == "revert"
    assert plan["argv"][0] == "revert"
    assert plan["scope"] == ["lockwire.yaml", "sibling.txt"]
    assert "sibling.txt" in plan["sentence"]

    vcs.apply_undo(plan)
    assert (repo / "lockwire.yaml").read_text(encoding="utf-8") == HEAD_TEXT
    assert not (repo / "sibling.txt").exists()
    assert (
        _git(["show", "-s", "--format=%an <%ae>", "HEAD"], cwd=repo) == "Test <test@example.com>\n"
    )


def test_a_revert_refuses_while_the_working_tree_is_dirty(repo: Path):
    """A revert can touch several files, so it must never run over unsaved work:
    it refuses with the dirty paths named, and changes nothing."""
    _gu_git(repo, "add", "lockwire.yaml")
    _gu_git(repo, "commit", "-qm", "the write")
    (repo / "unsaved.txt").write_text("DO NOT LOSE ME\n", encoding="utf-8")
    _gu_git(repo, "add", "unsaved.txt")

    plan = vcs.undo_plan(repo / "lockwire.yaml")
    assert plan["kind"] == "revert"
    with pytest.raises(vcs.UndoRefused) as caught:
        vcs.apply_undo(plan)
    assert "unsaved.txt" in str(caught.value)
    assert (repo / "unsaved.txt").read_text(encoding="utf-8") == "DO NOT LOSE ME\n"
    assert (repo / "lockwire.yaml").read_text(encoding="utf-8") == WORKING_TEXT


def test_outside_a_repository_with_no_backup_there_is_nothing_to_undo(tmp_path: Path):
    contract = tmp_path / "lockwire.yaml"
    contract.write_text(HEAD_TEXT, encoding="utf-8")
    plan = vcs.undo_plan(contract)
    assert plan["kind"] == "nothing"
    assert plan["argv"] == []
    vcs.apply_undo(plan)
    assert contract.read_text(encoding="utf-8") == HEAD_TEXT


def test_a_source_that_is_not_a_commit_here_is_refused_by_name(repo: Path):
    """A ref that does not resolve, and a hostile one, both refuse — loudly, with
    the ref in the message — rather than falling back to some other version."""
    for bad in ("no-such-tag", "--upload-pack=touch pwned", "; rm -rf /", ""):
        with pytest.raises(vcs.UndoRefused) as caught:
            vcs.undo_plan(repo / "lockwire.yaml", source=bad)
        assert "refusing" in str(caught.value)
    assert (repo / "lockwire.yaml").read_text(encoding="utf-8") == WORKING_TEXT


def test_a_path_outside_the_repository_is_never_restored(repo: Path, tmp_path: Path):
    """A path outside the repo gets no git plan at all, and a symlink inside the repo
    that points outside is judged where it really lives — never restored as if it
    were the repo's own file."""
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("not ours\n", encoding="utf-8")
    assert vcs.undo_plan(outside)["kind"] == "nothing"

    link = repo / "link.yaml"
    link.symlink_to(outside)
    plan = vcs.undo_plan(link)
    assert plan["kind"] == "nothing"
    assert plan["argv"] == []
    vcs.apply_undo(plan)
    assert outside.read_text(encoding="utf-8") == "not ours\n"


def test_no_mcp_tool_exposes_undo():
    """An agent must not undo a human's write: no MCP tool reaches these functions."""
    root = Path(__file__).resolve().parent.parent
    source = (root / "src" / "lockwire" / "mcp_server.py").read_text(encoding="utf-8")
    assert "undo" not in source
    assert "vcs" not in source
