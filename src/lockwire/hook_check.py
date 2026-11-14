"""`lockwire hook-check`: a Claude Code PreToolUse hook that checks a robot-model edit before it lands.

Input is the hook JSON on stdin (code.claude.com/docs/en/hooks: `tool_name`, `tool_input`).
Exit 2 with the violations on stderr blocks the edit. The rule for everything else:
- an edit to a file the hook doesn't check (or a payload it can't read) exits 0;
- an edit to a URDF/SDF/xacro or lockwire.yaml that the check itself fails on exits 2 with
  one line naming the error: on a robot model the hook fails closed, never open.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

MODELS = (".urdf", ".sdf", ".xacro", ".wbt", ".proto")
LOCK = "lockwire.yaml"


def _proposed(tool: str, args: dict, path: Path) -> str:
    if tool == "Write":
        return args["content"]
    text = path.read_text(encoding="utf-8")
    for edit in args.get("edits") or [args]:  # MultiEdit sends `edits`; Edit is one edit
        old, new = edit["old_string"], edit["new_string"]
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def _lock_for(model: Path) -> Path | None:
    """The nearest lockwire.yaml at or above the model whose `model.source` is this file."""
    from lockwire.edit import resolve_source
    from lockwire.schema import load_contract

    for folder in (model.parent, *model.parent.parents):
        lock = folder / LOCK
        if lock.is_file():
            loaded = load_contract(lock)
            named = resolve_source(lock, loaded.model.source) if loaded.model else None
            if named and named.resolve() == model:
                return lock
    return None


def _violations(path: Path, tmp: Path) -> dict[str, list[str]]:
    """Violation lines by quantity when ``tmp`` stands in for ``path``."""
    from lockwire import api
    from lockwire.schema import load_contract

    if path.name == LOCK:
        document = api.check(tmp)
    else:
        lock = _lock_for(path)
        if lock is None:
            return {}  # ponytail: no lock, nothing to compare; a bare model check takes ~5 s
        loaded = load_contract(lock)
        loaded.model.source = str(tmp)  # the checks read the proposed file, not the one on disk
        if loaded.model.provenance and loaded.model.provenance.source.lower().endswith(MODELS):
            loaded.model.provenance.source = str(tmp)
        document = api.check(loaded, contract_path=lock)
    found: dict[str, list[str]] = {}
    for f in document["findings"] if document["exit_code"] == 1 else []:
        if f["severity"] == "violation":
            lines = [f"VIOLATION {f['quantity']}: {f['observed']}, expected {f['expected']}"]
            cmd = f.get("next_command")
            if cmd and " --force" in cmd:  # re-locking discards locked entries: not an agent's call
                cmd = None
                lines.append("  the model changed: a person must review it and re-lock, not you")
            lines += [f"  run: {cmd}"] if cmd else []
            found[f["quantity"]] = [line.replace(str(tmp), str(path)) for line in lines]
    return found


def hook_check(stdin: str) -> tuple[int, str]:
    """(exit code, stderr text) for one PreToolUse payload."""
    try:
        payload = json.loads(stdin)
        tool, args = payload["tool_name"], payload["tool_input"]
        path = Path(args.get("file_path", "")).resolve()
    except Exception as error:  # noqa: BLE001 - can't tell which file, so not ours to block
        return 0, f"lockwire hook-check skipped: {type(error).__name__}: {error}"
    if tool not in ("Write", "Edit", "MultiEdit") or not (
        path.name == LOCK or path.suffix.lower() in MODELS
    ):
        return 0, ""
    try:
        fd, name = tempfile.mkstemp(prefix=".lockwire-hook-", suffix=path.suffix, dir=path.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            tmp.write_text(_proposed(tool, args, path), encoding="utf-8")
            found = _violations(path, tmp)
        finally:
            tmp.unlink(missing_ok=True)
        if found and path.exists():  # block only what this edit breaks, not what was broken
            for quantity in _violations(path, path):
                found.pop(quantity, None)
    except Exception as error:  # noqa: BLE001 - a model edit we couldn't check fails closed
        return (
            2,
            f"lockwire could not check this edit to {path.name} ({type(error).__name__}: {error}); fix that, then retry",
        )
    lines = [line for block in found.values() for line in block]
    if not lines:
        return 0, ""
    head = f"lockwire: this edit to {path.name} breaks the locked physics. Undo it, or ask a human to re-lock:"
    return 2, "\n".join([head, *lines])


def main() -> int:
    code, message = hook_check(sys.stdin.read())
    if message:
        print(message, file=sys.stderr)
    return code
