"""The one door to git. Nothing else in lockwire may shell out to git.

Every call goes through :func:`_git`: a list argv, ``shell=False``, no string
interpolation anywhere. A ref that arrives from the command line is validated
by :func:`valid_ref` before it can become an argument, so a hostile name
(``; rm -rf /``, ``--upload-pack=touch pwned``, ``--output=…``) is refused
rather than parsed as an option — argv already defeats the shell, and the
leading-dash rule defeats the option smuggling argv does not.

Repo questions are answered by ``git rev-parse``, never by looking for a
``.git`` directory: inside a worktree ``.git`` is a *file* holding a
``gitdir:`` pointer, and every lockwire worktree is laid out that way.

`log` and `blame` belong to the commands that need them; they add
their command here, on top of :func:`_git`, rather than opening a second door.
"""

import subprocess
from pathlib import Path
from typing import Sequence

TIMEOUT_S = 20


def valid_ref(ref: str) -> bool:
    """True when ``ref`` is safe to pass to git as an argument.

    Rejects the empty string, anything starting with ``-`` (git would read it
    as an option) and any control character. Whether the ref *exists* is
    ``rev_parse``'s question, not this one's.
    """
    if not ref or ref.startswith("-"):
        return False
    return all(ch.isprintable() for ch in ref) and " " not in ref


def _git(args: Sequence[str], *, cwd: Path) -> tuple[int, str]:
    """Run git with a list argv. Returns (returncode, stdout); no git -> (127, "")."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


def repo_root(path: Path) -> Path | None:
    """Root of the working tree containing ``path``, or None outside a repo.

    ``path`` may be a file or a directory; a missing directory is not a repo.
    """
    start = path if path.is_dir() else path.parent
    if not start.is_dir():
        return None
    code, out = _git(["rev-parse", "--show-toplevel"], cwd=start)
    if code != 0 or not out.strip():
        return None
    return Path(out.strip())


def rev_parse(ref: str, *, repo: Path) -> str | None:
    """Full commit sha for ``ref``, or None when the ref is unsafe or unknown."""
    if not valid_ref(ref):
        return None
    code, out = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo)
    if code != 0 or not out.strip():
        return None
    return out.strip()


def relpath_in_repo(path: Path, repo: Path) -> str | None:
    """``path`` as a repo-root-relative POSIX path, or None if it is outside."""
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def show(ref: str, relpath: str, *, repo: Path) -> str | None:
    """Text of ``relpath`` at ``ref``, or None when it did not exist there."""
    if not valid_ref(ref):
        return None
    code, out = _git(["show", f"{ref}:{relpath}"], cwd=repo)
    return None if code != 0 else out


def parent_relpath_after_rename(ref: str, relpath: str, *, repo: Path) -> str:
    """Path of ``relpath`` in ``ref``'s first parent across a detected rename.

    History callers walk newest to oldest.  A rename commit contains the new
    path, while its parent contains the old one; this translates exactly that
    boundary and otherwise leaves the path unchanged.
    """
    if not valid_ref(ref):
        return relpath
    code, out = _git(["diff-tree", "--no-commit-id", "--name-status", "-r", "-M", ref], cwd=repo)
    if code != 0:
        return relpath
    for line in out.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R") and len(paths) == 2 and paths[1] == relpath:
            return paths[0]
    return relpath


def blame(relpath: str, *, repo: Path) -> str | None:
    """Raw porcelain blame for one repo-relative path, or None on failure."""
    code, out = _git(["blame", "--porcelain", "--", relpath], cwd=repo)
    return out if code == 0 else None


def commit(relpaths: Sequence[str], *, message: str, repo: Path) -> tuple[int, str]:
    """``git commit -m <message> -- <relpaths>``. Commit by path.

    List argv throughout — ``message`` and every path travel as separate argv
    elements, never interpolated into a string a shell could re-parse. The
    trailing pathspec (``--``) is what makes this a commit *by path*: nothing
    else staged or dirty is swept in. Result-shaping (the returned dict, the
    witness gate) lives in ``vcs.py``, not here.
    """
    return _git(["commit", "-m", message, "--", *relpaths], cwd=repo)


def status_porcelain(repo: Path) -> str:
    """Raw ``git status --porcelain`` output, or "" when the command fails."""
    code, out = _git(["status", "--porcelain"], cwd=repo)
    return out if code == 0 else ""


_LOG_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_LOG_FIELD_SEP}%an{_LOG_FIELD_SEP}%aI{_LOG_FIELD_SEP}%s"


def log(
    revision_range: str | None, paths: Sequence[str], *, repo: Path, follow: bool = False
) -> list[tuple[str, str, str, str]] | None:
    """Commit, author, date and subject for ``paths``, newest first."""
    if follow and (revision_range is not None or len(paths) != 1):
        return None
    if revision_range is not None and not valid_ref(revision_range.split("..", 1)[0]):
        return None
    args = ["log"]
    if follow:
        args.append("--follow")
    args.append(f"--format={_LOG_FORMAT}")
    if revision_range is not None:
        args.append(revision_range)
    if paths:
        args.extend(["--", *paths])
    code, out = _git(args, cwd=repo)
    if code != 0:
        return None
    return [tuple(line.split(_LOG_FIELD_SEP, 3)) for line in out.splitlines() if line]


def describe(*, repo: Path) -> str | None:
    """``git describe --tags --always --dirty``, or None when it fails.

    A document's footer wants a human ref (tag-relative, or the
    short sha ``--always`` falls back to) alongside the full commit sha
    ``rev_parse`` already gives. ``git`` missing or the command failing both
    degrade to None rather than raising — the caller falls back further.
    """
    code, out = _git(["describe", "--tags", "--always", "--dirty"], cwd=repo)
    return out.strip() if code == 0 and out.strip() else None


def run(args: Sequence[str], *, repo: Path) -> tuple[int, str]:
    """Run a prepared git argv in ``repo`` — the door for callers that build their
    own subcommand (``lockwire.vcs``'s undo shows the argv on screen before it runs,
    so the list it hands back here must be the list that executes).

    Same guarantees as :func:`_git`: a list argv, ``shell=False``, a timeout. Any
    ref inside ``args`` is the caller's to validate with :func:`valid_ref` first.

    NOTE: ``commit`` and ``status_porcelain`` above could be
    expressed through this. They are left as-is rather than collapsed, because
    that would be a separate change to already-reviewed code.
    """
    return _git(args, cwd=repo)
