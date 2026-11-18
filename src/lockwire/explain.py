"""Explain one finding: the rule of the check behind it, the inputs, the threshold, the ways out.

Reads only, and writes no prose of its own per check. The rule is the check function's
docstring (its first paragraph is the line ``docs/guides/checks.md`` prints); the inputs with
their sources, the threshold and the ways to clear it are copied from the finding exactly as
``check`` reported it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lockwire import api, checks

NO_FINDINGS = "This check has no finding on this robot: it passed, or it had nothing to compare."
NO_RULE = "No rule text is on record for a finding below whose check is null."


def rule_summary(fn: Callable[..., Any]) -> str:
    """The docstring's first paragraph on one line, RST ``code`` as Markdown `code`."""
    first = (inspect.getdoc(fn) or "").split("\n\n")[0]
    return " ".join(first.split()).replace("``", "`")


def free_checks() -> dict[str, Callable[..., Any]]:
    """Every free check by its guide name (``fuse_wire``): the engine modules' checks, then ``checks.py``."""
    found = {f.__name__: f for f in checks._engine_checks()}
    found |= {
        name: f
        for name, f in inspect.getmembers(checks, inspect.isfunction)
        if name.startswith("check_") and f.__module__ == checks.__name__
    }
    return {name.removeprefix("check_"): f for name, f in found.items()}


def _alone(fn: Callable[..., Any], contract: Any, path: Path) -> list[Any]:
    """Run one check by itself with only what its signature names; a check that needs more says nothing."""
    given = {
        "scores": checks._ScoreBook(),
        "contract_path": path,
        "plan": None,
        "servo": contract.servo,
        "current_edge_values": None,
        "model": None,
    }
    params = list(inspect.signature(fn).parameters.values())[1:]
    if any(p.default is p.empty and p.name not in given for p in params):
        return []
    try:
        return list(fn(contract, **{p.name: given[p.name] for p in params if p.name in given}))
    except Exception:  # noqa: BLE001 - a check that cannot run alone names no rule; the finding is still served
        return []


def _row(finding: dict[str, Any]) -> dict[str, Any]:
    def keep(*keys: str) -> dict[str, Any]:
        return {k: finding[k] for k in keys if finding.get(k) not in (None, [], ())}

    return {
        **keep("severity", "quantity"),
        "inputs": keep("observed", "observed_quantity", "source", "witness", "locked_at", "dependent"),
        "threshold": keep("expected", "expected_quantity", "delta"),
        "clear": keep("action", "next_command", "alternatives"),
    }


def explain(contract: Path | str, finding: str) -> dict[str, Any]:
    """``finding`` is a check's name (``fuse_wire``) or a finding's quantity path."""
    path = Path(contract)
    reported = api.check(path)["findings"]
    loaded = checks._with_mjcf_effort(api.load(path), path)
    known = free_checks()

    # ponytail: the check behind a finding is found by running each check alone and matching the
    # path and observed text it emits; `Finding` does not name its check. A paid check, or one
    # that only speaks when it shares the run's score book, is not found and gets NO_RULE. Name
    # the check on the finding itself if that ever matters.
    said: dict[str, set[tuple[str, str]]] = {}

    def emits(name: str, f: dict[str, Any]) -> bool:
        if name not in said:
            said[name] = {(x.quantity, x.observed) for x in _alone(known[name], loaded, path)}
        return (f["quantity"], f["observed"]) in said[name]

    asked = finding.removeprefix("check_")
    if asked in known:
        rows = [_row(f) | {"check": asked} for f in reported if emits(asked, f)]
    else:
        rows = [
            _row(f) | {"check": next((n for n in known if emits(n, f)), None)}
            for f in reported
            if f["quantity"] == finding
        ]
    rules = {}
    for name in dict.fromkeys([asked] if asked in known else [r["check"] for r in rows if r["check"]]):
        detail = (inspect.getdoc(known[name]) or "").partition("\n\n")[2]
        rules[name] = {"rule": rule_summary(known[name])} | ({"detail": detail} if detail else {})
    out: dict[str, Any] = {"finding": finding, "rules": rules, "findings": rows}
    if not rows:
        out["note"] = (
            NO_FINDINGS
            if rules
            else f"No finding or check is called {finding!r} on this robot. "
            f"Its findings are: {', '.join(f['quantity'] for f in reported) or 'none'}."
        )
    elif any(r["check"] is None for r in rows):
        out["note"] = NO_RULE
    return out
