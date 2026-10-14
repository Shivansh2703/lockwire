"""Commit the contract by path, and undo a write, from a source
it names out loud — two offers, one module,
because both are the Python half of "version control" for the contract and
both go through the one door, `lockwire.gitref`.

**Commit** (`commit_contract`): builds nothing about the message —
the source and the witness in it come from `edit.apply_changes`'s own
return (stitched by the app), never re-parsed out of
the YAML here. It shows the exact argv before running anything, and commits
**by path**: the contract and its sidecars, nothing else dirty in the tree.
It never pushes — pushing is the user's call, not the app's.

**Undo** (`undo_plan`/`apply_undo`): inside a
repository git *is* the undo, so this half builds no history of its
own either. What it adds is the one thing plain git does not give you: a
plan you can read before anything runs — the exact argv, and the exact list
of files it touches.

**Blocker 5.** ``git restore -- <file>`` restores from **the index**, not
from HEAD. Run it after someone staged a copy and the file comes back as
the *staged* version, silently, with a zero exit code. Every restore this
module plans therefore carries ``--source=<ref>`` explicitly; a bare
``git restore`` is never produced, and
``test_undo_restores_from_head_not_the_index`` holds that.

**Scope, and what is at risk.** A ``restore`` plan touches exactly the one
path in ``scope``; a change to any neighbouring file — staged, unstaged or
untracked — survives it. The working copy of the named file is what the
undo discards: that is the undo, and the sentence says so before it runs. A
``revert`` plan can touch several files, so :func:`apply_undo` **refuses**
it while the working tree is dirty rather than putting anyone's unsaved
work under a merge.

Outside a repository nothing new is built for undo: a sibling ``<path>.bak``,
if one exists, is the only safety net (its writer, ``edit.backup_before_write``,
was cut as dead code — infra: cuts per docs/PLAN.md — so this branch is now
reachable only when something else left a ``.bak`` beside the file).

The one door to git is `lockwire.gitref`; this module adds no
subprocess call of its own.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from lockwire import gitref
from lockwire.edit import EditConflict, _contract_lock, contract_revision

#: What every operation in the version-control leg says when there is no
#: repository (or no `git` on the PATH) to act on — commit does nothing.
NOT_A_REPO = "not a git repository — nothing committed"

#: argv fragments that must never appear in anything this module builds —
#: swept for by `test_no_code_path_here_pushes` (pushing is the user's
#: call, never the app's).
_PUSH_MARKERS = ("push", "remote", "--set-upstream")


def _witness_present(message: str) -> bool:
    """True when `message` carries a non-empty ``witness: ...`` marker.

    The commit message carries source *and* witness — this is the
    one check `commit_contract` makes on the message itself, since it does
    not build the message (that is `apply_changes`'s job, upstream of here).
    """
    lower = message.lower()
    idx = lower.find("witness:")
    if idx == -1:
        return False
    after = message[idx + len("witness:") :].partition("\n")[0].strip()
    return bool(after)


def _other_dirty_paths(repo: Path, committed: set[str]) -> list[str]:
    """Repo-relative paths `git status --porcelain` still shows dirty.

    Read before the commit runs, so it also answers a `dry_run` preview
    ("these are the files this commit will leave alone").
    """
    others = []
    for line in gitref.status_porcelain(repo).splitlines():
        rest = line[3:]
        if " -> " in rest:  # a rename entry: "old -> new"
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest and rest not in committed:
            others.append(rest)
    return others


def commit_contract(
    contract_path: str | Path,
    *,
    message: str,
    paths: list[str],
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Commit `paths` (the contract and its sidecars) with `message`.

    Returns ``{"argv": [...], "committed": bool, "sha": str | None,
    "untouched": [...]}``. A real commit requires the full raw revision of
    `contract_path`; it is compared while holding the contract lock just
    before Git stages and commits. Refuses (``ValueError``) when `message`
    carries no witness, or when a requested path resolves outside the
    repository — both are caller mistakes, not states to report and continue past.

    Outside a repository, or with no `git` on the PATH, does nothing and
    returns the one sentence the rest of the version-control leg gives
    (`NOT_A_REPO`) instead of an argv.

    Ceilings this does not cover: a pre-commit hook that rejects the commit
    (surfaces as `committed: False`, no exception), a detached HEAD or an
    unborn branch (git accepts a commit in both; this function does not
    special-case either), and a submodule boundary (paths inside a submodule
    are outside `repo_root`'s answer for the superproject and are refused as
    "outside the repository").
    """
    if not _witness_present(message):
        raise ValueError("commit message must carry a witness — no 'witness:' found")
    if not paths:
        raise ValueError("commit_contract needs at least one path")

    contract_path = Path(contract_path).resolve()
    repo = gitref.repo_root(contract_path)
    if repo is None:
        return {"argv": [], "committed": False, "sha": None, "untouched": [], "reason": NOT_A_REPO}

    relpaths: list[str] = []
    for raw in paths:
        rel = gitref.relpath_in_repo(Path(raw).resolve(), repo)
        if rel is None:
            raise ValueError(f"{raw} is outside the repository at {repo}")
        relpaths.append(rel)

    argv = ["git", "commit", "-m", message, "--", *relpaths]
    # The message is free text ("relocate remote antenna"); scan only git's own argv.
    joined = " ".join(a for a in argv if a != message).lower()
    if any(marker in joined for marker in _PUSH_MARKERS):
        raise AssertionError("commit_contract built an argv that pushes — refusing to run it")

    untouched = _other_dirty_paths(repo, set(relpaths))

    if dry_run:
        return {"argv": argv, "committed": False, "sha": None, "untouched": untouched}
    if expected_revision is None:
        raise EditConflict("expected_revision is required before committing; reload first")
    # A known-stale caller must not create a persistent lockfile just to refuse.
    # The comparison below is authoritative for races that follow this precheck.
    if expected_revision != contract_revision(contract_path):
        raise EditConflict("contract changed since it was opened; reload before committing")

    with _contract_lock(contract_path):
        actual_revision = contract_revision(contract_path)
        if expected_revision != actual_revision:
            raise EditConflict("contract changed since it was opened; reload before committing")
        code, _ = gitref.commit(relpaths, message=message, repo=repo)
    if code != 0:
        return {"argv": argv, "committed": False, "sha": None, "untouched": untouched}

    sha = gitref.rev_parse("HEAD", repo=repo)
    return {"argv": argv, "committed": True, "sha": sha, "untouched": untouched}


class UndoRefused(Exception):
    """An undo that cannot be planned safely. The message names the reason."""


def _last_commit_touching(relpath: str, *, repo: Path) -> str | None:
    code, out = gitref.run(["log", "-n", "1", "--format=%H", "--", relpath], repo=repo)
    return out.strip() or None if code == 0 else None


def _files_in_commit(sha: str, *, repo: Path) -> list[str]:
    code, out = gitref.run(["show", "--name-only", "--format=", sha], repo=repo)
    return sorted(line for line in out.splitlines() if line.strip()) if code == 0 else []


def _is_dirty(relpath: str, *, repo: Path) -> bool:
    """True when git reports any change (staged, unstaged or untracked) for this path.

    Asked with a pathspec rather than by parsing the full listing, so a path with
    a space or a quote in it is git's problem, not ours.
    """
    code, out = gitref.run(["status", "--porcelain", "--", relpath], repo=repo)
    return code == 0 and bool(out.strip())


def _dirty_paths(repo: Path) -> list[str]:
    """Every dirty path, for the refusal message only — never for a decision.

    ponytail: porcelain v1 quotes odd names and writes renames as ``old -> new``;
    this is a human sentence, so the raw field is good enough. Use ``_is_dirty``
    wherever the answer changes what runs.
    """
    code, out = gitref.run(["status", "--porcelain"], repo=repo)
    if code != 0:
        return []
    return sorted(line[3:] for line in out.splitlines() if line.strip())


def _resolve(path: Path | str) -> tuple[Path, Path | None, str | None]:
    """(absolute path, repo root or None, repo-relative path or None)."""
    target = Path(path).resolve()
    repo = gitref.repo_root(target)
    if repo is None:
        return target, None, None
    relpath = gitref.relpath_in_repo(target, repo)
    if relpath is None:
        raise UndoRefused(f"{target} is outside the repository at {repo} — refusing")
    return target, repo, relpath


def undo_plan(contract: Path | str, *, source: str = "HEAD") -> dict:
    """Plan the undo of the last write to ``contract``. Runs nothing.

    Returns ``{"kind", "argv", "scope", "repo", "sentence"}`` where ``kind`` is:

    ``restore``  the write is still uncommitted — put the file back to ``source``
                 (always ``--source=``, never the index). Touches one path.
    ``revert``   the write is committed — offer ``git revert`` of that commit,
                 naming every other file the commit touches.
    ``backup``   no repository: an existing sibling ``<path>.bak`` is the
                 only undo there is (nothing in this codebase writes one).
    ``nothing``  no repository and no backup, or a file that does not exist at
                 ``source`` and was never committed.

    Raises :class:`UndoRefused` when ``source`` is not a commit here, or when
    ``contract`` lies outside the repository it was resolved against.
    """
    target, repo, relpath = _resolve(contract)

    if repo is None:
        backup = target.with_name(target.name + ".bak")
        if backup.is_file():
            return {
                "kind": "backup",
                "argv": [],
                "scope": [str(target)],
                "repo": "",
                "sentence": (
                    f"Not a git repository. The last write left {backup.name} beside "
                    f"{target.name}; copying it back is the whole undo."
                ),
            }
        return {
            "kind": "nothing",
            "argv": [],
            "scope": [],
            "repo": "",
            "sentence": f"Not a git repository, and no {backup.name} — nothing to undo.",
        }

    if gitref.rev_parse(source, repo=repo) is None:
        raise UndoRefused(f"{source!r} is not a commit in {repo} — refusing to undo")

    if _is_dirty(relpath, repo=repo):
        if gitref.show(source, relpath, repo=repo) is None:
            return {
                "kind": "nothing",
                "argv": [],
                "scope": [],
                "repo": str(repo),
                "sentence": f"{relpath} does not exist at {source} — nothing to restore it to.",
            }
        return {
            "kind": "restore",
            "argv": ["restore", f"--source={source}", "--", relpath],
            "scope": [relpath],
            "repo": str(repo),
            "sentence": (
                f"git restore --source={source} -- {relpath}\n"
                f"Touches 1 file: {relpath}. Its current contents are replaced by "
                f"{source}'s; nothing else in the working tree is read or written."
            ),
        }

    sha = _last_commit_touching(relpath, repo=repo)
    if sha is None:
        return {
            "kind": "nothing",
            "argv": [],
            "scope": [],
            "repo": str(repo),
            "sentence": f"{relpath} has no commit and no uncommitted change — nothing to undo.",
        }
    scope = _files_in_commit(sha, repo=repo)
    scope = [relpath, *(path for path in scope if path != relpath)]
    others = [p for p in scope if p != relpath]
    tail = (
        f" That commit also touches {len(others)}: " + ", ".join(others) + "."
        if others
        else " That commit touches nothing else."
    )
    return {
        "kind": "revert",
        "argv": ["revert", "--no-edit", sha],
        "scope": scope,
        "repo": str(repo),
        "sentence": (
            f"git revert --no-edit {sha[:12]}\n"
            f"The write to {relpath} is committed, so undoing it is a new commit." + tail
        ),
    }


def apply_undo(
    plan: dict,
    *,
    repo: Path | str | None = None,
    contract: Path | str | None = None,
    expected_revision: str | None = None,
) -> dict:
    """Run the plan that was shown. Returns the plan it ran.

    ``restore`` runs its argv; ``backup`` copies the sibling
    ``.bak`` back; ``nothing`` does nothing. A ``revert`` is **refused** while
    any file in the working tree is dirty — a revert can touch several files and
    must never bury someone's unsaved work in a merge.
    """
    planned = _planned_contract(plan, repo)
    supplied = Path(contract).resolve() if contract is not None else None
    if supplied is not None and planned is not None and supplied != planned:
        raise UndoRefused(
            f"the supplied contract {supplied} is not the contract targeted by this plan "
            f"({planned}) — refusing"
        )
    target = supplied if supplied is not None else planned
    if target is not None:
        # Reject a revision already known to be stale before opening the sibling
        # lock file. The comparison is repeated under the lock below: a writer
        # can still change the contract between this no-touch fast path and lock
        # acquisition.
        if expected_revision is not None and expected_revision != contract_revision(target):
            raise EditConflict(
                "contract changed since it was opened; reload before applying this edit"
            )
        with _contract_lock(target):
            actual = contract_revision(target)
            if expected_revision is not None and expected_revision != actual:
                raise EditConflict(
                    "contract changed since it was opened; reload before applying this edit"
                )
            return _apply_undo(plan, repo=repo, contract=target)
    return _apply_undo(plan, repo=repo, contract=None)


def _planned_contract(plan: dict, repo: Path | str | None) -> Path | None:
    """Find the canonical contract targeted by a plan generated in this module."""
    if plan["kind"] == "backup":
        return Path(plan["scope"][0]).resolve()
    root = Path(repo) if repo is not None else (Path(plan["repo"]) if plan.get("repo") else None)
    scope = plan.get("scope", [])
    if root is not None and scope:
        return (root.resolve() / scope[0]).resolve()
    return None


def _apply_undo(
    plan: dict, *, repo: Path | str | None = None, contract: Path | None = None
) -> dict:
    """Execute a plan after :func:`apply_undo` has locked its contract."""
    kind = plan["kind"]
    if kind == "nothing":
        return plan
    if kind == "backup":
        target = Path(plan["scope"][0]).resolve()
        backup = target.with_name(target.name + ".bak")
        if not backup.is_file():
            raise UndoRefused(f"{backup} is gone — refusing to undo")
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(backup.read_text(encoding="utf-8"))
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
        return plan

    root = Path(repo) if repo is not None else (Path(plan["repo"]) if plan.get("repo") else None)
    root = root.resolve() if root is not None else None
    if root is None or not root.is_dir():
        raise UndoRefused("no git repository for this plan — refusing to run it")
    if kind == "revert":
        dirty = _dirty_paths(root)
        if contract is not None:
            lock_path = contract.with_name(f".{contract.name}.lock")
            lock_relpath = gitref.relpath_in_repo(lock_path, root)
            dirty = [path for path in dirty if path != lock_relpath]
        if dirty:
            raise UndoRefused(
                "the working tree has uncommitted changes ("
                + ", ".join(dirty)
                + ") — commit or discard them before reverting; refusing"
            )
    code, _ = gitref.run(plan["argv"], repo=root)
    if code != 0:
        raise UndoRefused(f"git {' '.join(plan['argv'])} failed (exit {code}) — nothing changed")
    return plan
