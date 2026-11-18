"""`lockwire agent-setup`: put the three agent rules, a pre-commit hook and a CI workflow in a repo."""

import json
import re
import sys
from pathlib import Path

import lockwire
from lockwire.cli_line import cli_line

START, END = "<!-- lockwire:start -->", "<!-- lockwire:end -->"
RULES = """{START}
## Lockwire (physical-assumption checks)
{note}
- Run `{lw} check` before editing a model file (URDF, SDF, lockwire.yaml).
- Run `{lw} plan lockwire.yaml plan.yaml` before any motion.
- Never change a declared value without a human witness: `--witness <name>`.

Get started: `{lw} check <your robot.urdf>`, then `{lw} start <your robot.urdf>` to lock it.
{END}
"""


def rules() -> str:
    """The rules with the program token that reached lockwire when they were written."""
    lw = cli_line("lockwire")
    note = f"\nCommands say `{lw}`, what ran setup; type `lockwire` if installed.\n"
    note = "" if lw == "lockwire" else note
    return RULES.format(START=START, END=END, lw=lw, note=note)


PRE_COMMIT = r"""  - repo: https://github.com/Shivansh2703/lockwire
    rev: v{version}
    hooks:
      - id: lockwire-check
        files: (?i)(\.(urdf|xacro|sdf)|(^|/)lockwire\.ya?ml)$
"""
WORKFLOW = """name: lockwire
on: [push, pull_request]
jobs:
  lockwire:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Shivansh2703/lockwire@v0
        with:
          contract: lockwire.yaml
"""
# $0 is the interpreter. Same gate as plugin/hooks/hook-check.sh: model files only, any case.
_GATE = (
    'p=$(cat); case $(printf %s "$p" | tr "[:upper:]" "[:lower:]") in'
    ' *.urdf\\"* | *.sdf\\"* | *.xacro\\"* | *lockwire.yaml\\"*) ;; *) exit 0 ;; esac;'
)
# run CMD: use CMD only if `CMD --version` exits 0 (a stale launcher with no module behind it is
# skipped); its hook-check decides, and a crash blocks the edit. Nothing runs: block with one line.
_RUN = (
    ' run() { "$@" --version >/dev/null 2>&1 </dev/null || return 0;'
    ' printf %s "$p" | "$@" hook-check && exit 0;'
    ' [ $? = 2 ] || echo "lockwire hook-check crashed (see above); re-run lockwire agent-setup'
    ' --claude" >&2; exit 2; };'
)
_LAST = (
    " run lockwire; run uvx --from lockwire lockwire;"
    ' echo "lockwire could not be run, so this robot-model edit is blocked: install or repair it'
    ' (uv tool install lockwire, then lockwire agent-setup --claude) and retry, or remove the'
    ' PreToolUse entry with lockwire hook-check from .claude/settings.local.json"'
    " >&2; exit 2"
)
# $0 is the Python that ran agent-setup: its own lockwire first, as long as it still runs.
GUARD = _GATE + _RUN + ' [ -x "$0" ] && run "$0" -m lockwire;' + _LAST
# An interpreter inside uv's cache (uvx) is gone after a cache clean or on a teammate's machine,
# so name the command instead: a `lockwire` on PATH that runs, else `uvx lockwire`.
GUARD_PORTABLE = _GATE + _RUN + _LAST


def agent_setup(root: Path, *, ci_only: bool = False) -> list[str]:
    """Write what is missing under ``root``; return one line per file. Re-running changes nothing."""
    lines = []

    def put(name: str, text: str, *, append: bool = False, head: str = "") -> None:
        # ponytail: fixed names under root, so no `confine` (it refuses .yaml targets by design).
        path = root / name
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old is not None and (not append or text.split("\n")[0] in old):
            lines.append(f"kept {name}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            head + text if old is None else old.rstrip("\n") + "\n\n" + text, encoding="utf-8"
        )
        lines.append(f"wrote {name}")

    for name in () if ci_only else ("CLAUDE.md", "AGENTS.md"):
        put(name, rules(), append=True)
    # ponytail: appends after the last line, so a config whose `repos:` is not last needs a hand merge.
    if not ci_only:
        # the hook pins the lockwire that wrote it: an older one refuses the lockfiles this one writes
        entry = PRE_COMMIT.format(version=lockwire.__version__)
        put(".pre-commit-config.yaml", entry, append=True, head="repos:\n")
        if lines[-1] == "kept .pre-commit-config.yaml":
            old = stale_pin((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
            if old:  # told, not rewritten: the file is the user's
                lines[-1] = (
                    f"kept .pre-commit-config.yaml, but its Lockwire pin rev: {old} is older than this "
                    f"Lockwire and may not read its lockfiles; change that line to rev: v{lockwire.__version__}"
                )
    put(".github/workflows/lockwire.yml", WORKFLOW)
    return lines


def stale_pin(config: str) -> str | None:
    """The rev of the Lockwire pre-commit entry when it is older than this Lockwire, else None."""
    found = re.search(r"Shivansh2703/lockwire\s*\n\s*rev:\s*['\"]?([^\s'\"]+)", config)
    if found is None:
        return None
    number = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3])  # noqa: E731
    return found[1] if number(found[1]) < number(lockwire.__version__) else None


def hook_is_portable() -> bool:
    """True when this Python lives in uv's cache (uvx): the hook then names no machine path."""
    return any(
        p.startswith(("archive-v", "environments-v", "builds-v"))
        for p in Path(sys.executable).parts
    )


def _is_ours(hook: dict) -> bool:
    return "lockwire hook-check" in str(hook.get("command", ""))


def _move_out_of_shared(root: Path) -> str | None:
    """Drop the hook an older run wrote into the committed ``.claude/settings.json``.

    Rewrites the file only when re-saving it leaves every other byte as it was (it was saved
    in the same JSON layout); otherwise names the entry to delete by hand.
    """
    path = root / ".claude" / "settings.json"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        settings = json.loads(text)
        groups = settings["hooks"]["PreToolUse"]
    except (ValueError, KeyError, TypeError):
        return None
    if not any(_is_ours(h) for g in groups for h in g.get("hooks", [])):
        return None
    if json.dumps(settings, indent=2) + "\n" != text:
        return (
            "your .claude/settings.json still has an older Lockwire edit hook: delete its "
            "PreToolUse entry whose command contains `lockwire hook-check`"
        )
    for group in groups:
        group["hooks"] = [h for h in group.get("hooks", []) if not _is_ours(h)]
    groups[:] = [g for g in groups if g.get("hooks")]
    if not groups:
        del settings["hooks"]["PreToolUse"]
    if not settings["hooks"]:
        del settings["hooks"]
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return (
        "moved the Lockwire edit hook out of .claude/settings.json into .claude/settings.local.json"
    )


def claude_hook(root: Path) -> list[str]:
    """Merge the PreToolUse edit hook into ``root/.claude/settings.local.json``; re-running adds nothing.

    The local file, not the committed ``settings.json``: the hook can name this machine's
    Python. Claude Code merges hook lists from both files, so a hook an older run left in
    ``settings.json`` is moved out. The command names this interpreter, not a bare ``lockwire``
    on PATH: a stale or broken install there exits 1, and Claude Code lets an edit through on
    any exit but 2. GUARD passes other files untouched and turns a gone interpreter (a cleaned
    uvx cache) or a crash into 2.
    """
    command = (
        cli_line("sh", "-c", GUARD_PORTABLE)
        if hook_is_portable()
        else cli_line("sh", "-c", GUARD, sys.executable)
    )
    moved = _move_out_of_shared(root)
    lines = [moved] if moved else []
    path = root / ".claude" / "settings.local.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    ours = [h for g in groups for h in g.get("hooks", []) if _is_ours(h)]
    if [h["command"] for h in ours] == [command]:
        return [*lines, "kept .claude/settings.local.json"]
    for hook in ours:  # an older entry (bare `lockwire hook-check`) now runs the guard
        hook["command"] = command
    if not ours:
        groups.append(
            {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": command}]}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return [
        *lines,
        f"wrote .claude/settings.local.json (edit hook: {command}; to remove it, delete the "
        "PreToolUse entry whose command contains `lockwire hook-check`)",
    ]
