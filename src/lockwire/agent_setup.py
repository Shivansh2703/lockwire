"""`lockwire agent-setup`: put the three agent rules, a pre-commit hook and a CI workflow in a repo."""

import json
import sys
from pathlib import Path

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
    rev: v0.2.6
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
GUARD = (
    'p=$(cat); case $(printf %s "$p" | tr "[:upper:]" "[:lower:]") in'
    ' *.urdf\\"* | *.sdf\\"* | *.xacro\\"* | *lockwire.yaml\\"*) ;; *) exit 0 ;; esac;'
    ' [ -x "$0" ] || { echo "lockwire: hook interpreter is gone; re-run lockwire agent-setup'
    ' --claude" >&2; exit 2; };'
    ' printf %s "$p" | "$0" -m lockwire hook-check && exit 0;'
    ' [ $? = 2 ] || echo "lockwire hook-check crashed (see above); re-run lockwire agent-setup'
    ' --claude" >&2; exit 2'
)


def agent_setup(root: Path, *, ci_only: bool = False) -> list[str]:
    """Write what is missing under ``root``; return one line per file. Re-running changes nothing."""
    lines = []

    def put(name: str, text: str, *, append: bool = False, head: str = "") -> None:
        # ponytail: fixed names under root, so no `confine` (it refuses .yaml targets by design).
        path = root / name
        old = path.read_text() if path.exists() else None
        if old is not None and (not append or text.split("\n")[0] in old):
            lines.append(f"kept {name}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head + text if old is None else old.rstrip("\n") + "\n\n" + text)
        lines.append(f"wrote {name}")

    for name in () if ci_only else ("CLAUDE.md", "AGENTS.md"):
        put(name, rules(), append=True)
    # ponytail: appends after the last line, so a config whose `repos:` is not last needs a hand merge.
    if not ci_only:
        put(".pre-commit-config.yaml", PRE_COMMIT, append=True, head="repos:\n")
    put(".github/workflows/lockwire.yml", WORKFLOW)
    return lines


def claude_hook(root: Path) -> str:
    """Merge the PreToolUse edit hook into ``root/.claude/settings.json``; re-running adds nothing.

    The command names this interpreter, not a bare ``lockwire`` on PATH: a stale or broken
    install there exits 1, and Claude Code lets an edit through on any exit but 2. GUARD passes
    other files untouched and turns a gone interpreter (a cleaned uvx cache) or a crash into 2.
    """
    command = cli_line("sh", "-c", GUARD, sys.executable)
    path = root / ".claude" / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    ours = [
        h
        for g in groups
        for h in g.get("hooks", [])
        if "lockwire hook-check" in str(h.get("command", ""))
    ]
    if [h["command"] for h in ours] == [command]:
        return "kept .claude/settings.json"
    for hook in ours:  # an older entry (bare `lockwire hook-check`) now runs the guard
        hook["command"] = command
    if not ours:
        groups.append(
            {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": command}]}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return (
        f"wrote .claude/settings.json (edit hook: {command}; to remove it, delete the "
        "PreToolUse entry whose command contains `lockwire hook-check`)"
    )
