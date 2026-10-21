"""Where a door may write, and what a refusal may say back.

Every writer (`check --json`, `plan --json`, `report`, `describe --out`, and the
sidecar's `report`/`describe`) routes its target through ``confine``: a lockfile is
never an output, and the sidecar, whose caller can be a model, stays inside the
contract's folder (reads are free, writes need a signature).
"""

import re
from pathlib import Path

_LOCKFILE_SUFFIXES = {".yaml", ".yml"}
# pydantic's `[type=..., input_value=..., input_type=...]` carries the file's own values.
_INPUT_ECHO = re.compile(r", input_value=.*, input_type=\w+\]$", re.MULTILINE)


def confine(out: Path | str, contract: Path | str | None, root: Path | str | None = None) -> Path:
    """Resolve ``out``; refuse a lockfile (the contract above all) and, given ``root``, anything outside it."""
    target = Path(out).resolve()
    if target.suffix.lower() in _LOCKFILE_SUFFIXES or (
        contract is not None and target == Path(contract).resolve()
    ):
        raise ValueError(f"refusing to write over a lockfile: {out}")
    if root is not None and not target.is_relative_to(Path(root).resolve()):
        raise ValueError(f"refusing to write outside {root}: {out}")
    return target


def without_values(message: str) -> str:
    """A refusal names the field and the error type, never the value it read."""
    return _INPUT_ECHO.sub("]", message)
