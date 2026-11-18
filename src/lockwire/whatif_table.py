"""The short answer to "what if I change this": only what moved between the two what-if reports.

Pure: takes the document ``api.what_if`` returns and changes nothing in it.
"""

from __future__ import annotations

import re
from typing import Any

from lockwire.checks import is_lock_drift
from lockwire.copy import check_label, label_name
from lockwire.report import Finding, delta_text, plain_text

_RANK = {"violation": 0, "warning": 1, "not_scored": 2, "advice": 3}
_STATE = {"not_scored": "not scored"}  # the tool's words for a state name
_UNIT = re.compile(r"[A-Za-z%°*/^\d]{1,8}[,:;]?")
_NUMERIC = re.compile(r"[-+(]?\d")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?\s*[A-Za-z*/%°]+")


def verdict_words(code: str) -> str:
    return {"GO_WITH_NOTES": "GO with notes", "NOT_SCORED": "NOT SCORED"}.get(code, code)


def finding_line(finding: dict[str, Any]) -> str:
    """The first line `check` prints for a finding, as plain text."""
    tag = {"violation": "VIOLATION", "warning": "warning"}.get(finding["severity"], "not scored")
    line = plain_text(f"{tag} {finding['quantity']}: observed {finding['observed']}, expected {finding['expected']}")
    return line + delta_text(finding["delta"]) if finding.get("delta") else line


def _words(quantity: str) -> str:
    """What the row is about: the kind and id from the path, then the check's own word."""
    parts = quantity.split("/")
    if len(parts) < 3:
        return check_label(quantity)
    if len(parts) == 3:
        return label_name(parts[2])
    return f"{label_name(parts[2])}, {check_label(quantity).lower()}"  # check's names


def _short(text: str) -> str:
    match = _NUMBER.match(text)
    return match.group().strip() if match else text


def _differs(before: str, after: str) -> tuple[str, str]:
    """The two texts cut to the words that differ, with one word of context each side."""
    a, b = before.split(), after.split()
    i = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    lo = max(i - 1, 0)
    if j and _UNIT.fullmatch(a[-j]) and all(_NUMERIC.match(w[len(w) - j - 1]) for w in (a, b)):
        j -= 1  # "is 150 mm", not "is 150": a number keeps its unit
    cut = [" ".join(w[lo : len(w) - j] or w) for w in (a, b)]
    return tuple(c if len(c) <= 80 else c[:79] + "…" for c in cut)  # type: ignore[return-value]


def _side(report: dict[str, Any]) -> dict[str, tuple[str, str, str]]:
    """quantity -> (status, observed, expected): findings of one quantity's worst severity joined,
    and a finding outranks the comparison it came from."""
    out = {c["quantity"]: ("passed", c["observed"], c["expected"]) for c in report["comparisons"]}
    rank = {f["quantity"]: 9 for f in report["findings"]}
    for f in report["findings"]:
        rank[f["quantity"]] = min(rank[f["quantity"]], _RANK.get(_state(f), 4))
    for q, r in rank.items():
        same = [f for f in report["findings"] if f["quantity"] == q and _RANK.get(_state(f), 4) == r]
        out[q] = (
            _state(same[0]),
            " / ".join(f["observed"] for f in same),
            " / ".join(f["expected"] for f in same),
        )
    return out


def _state(finding: dict[str, Any]) -> str:
    """A finding's state; one the verdict counts as advice (not yet sourced) is "advice"."""
    return "advice" if finding.get("advice_unsourced") else finding["severity"]


_JOINT_MOVED = re.compile(r"joint moved: (\S+) \([^)]*\)")


def _is_echo(finding: dict[str, Any], changed: tuple[str, ...]) -> bool:
    quantity = finding["quantity"]
    if not is_lock_drift(Finding.model_construct(**finding)):
        return False
    if any(path == quantity or path.startswith(quantity + "/") for path in changed):
        return True
    # A joint's limits edited: the structure drift names only that joint (urdf.describe_structure_drift
    # builds plain text, no structured data).
    named = _JOINT_MOVED.fullmatch(finding["observed"])
    return bool(named) and any(p.startswith(f"model/joints/{named[1]}/") for p in changed)


def _invalidated(document: dict[str, Any]) -> list[str]:
    """One line per recorded device-health claim the (paid) what-if says the change invalidates."""
    rows = (document.get("recorded_health") or {}).get("invalidated") or []
    return [f"{r.get('device', '?')}: {r.get('reason', r.get('state', ''))}" for r in rows]


def whatif_table(document: dict[str, Any], changed_paths: tuple[str, ...] = ()) -> dict[str, Any]:
    """Rows of what differs, new and resolved finding lines, the change echoing back, the rest.

    The echo is a lock-drift violation that is new on the proposed side and is the change itself:
    the lock now disagrees with the model file by exactly what was asked about. A quantity whose
    finding or row text differs in any way is a changed quantity. Each finding shows once: a
    quantity on both sides is a row, a finding on one side only is under New or Resolved.
    """
    cur, new = document["current"], document["proposed"]
    seen = {(f["severity"], f["quantity"]) for f in cur["findings"]}
    echo = [
        f
        for f in new["findings"]
        if (f["severity"], f["quantity"]) not in seen and _is_echo(f, changed_paths)
    ]
    echoed = {f["quantity"] for f in echo}
    before, after = _side(cur), _side(new)
    rows, changed = [], set()
    for q in sorted((before.keys() | after.keys()) - echoed):
        b, a = before.get(q), after.get(q)
        if b == a:
            continue
        changed.add(q)
        if (b is None and a[0] != "passed") or (a is None and b[0] != "passed"):
            continue  # a finding on one side only: New or Resolved, with its whole line
        was, now = (_STATE.get(x[0], x[0]) if x else None for x in (b, a))
        if b is None:
            status = f"new {now}"
        elif a is None:
            status = "gone"
        elif was != now:
            status = f"{was} → {now}"
        else:
            status = now if now == "advice" else f"still {now}"
        shown = (_short(b[1]) if b else "-", _short(a[1]) if a else "-")
        if b and a:  # same number and status: show the words that differ; no number: the same
            plain = not (_NUMBER.match(b[1]) and _NUMBER.match(a[1]))
            if plain or (shown[0] == shown[1] and b[0] == a[0]):
                shown = _differs(*((b[1], a[1]) if b[1] != a[1] else (b[2], a[2])))
        rows.append(
            {
                "quantity": q,
                "label": _words(q),
                "before": shown[0],
                "after": shown[1],
                "limit": (a or b)[2],
                "status": status,
            }
        )
    new_lines = [f for f in new["findings"] if f["quantity"] not in before and f not in echo]
    gone_lines = [f for f in cur["findings"] if f["quantity"] not in after]
    real_new_violation = any(
        a[0] == "violation" and (q not in before or before[q][0] != "violation")
        for q, a in after.items()
        if q not in echoed
    )
    note = ""
    if echo and not real_new_violation and new["verdict"] == "NO-GO" != cur["verdict"]:
        note = " (only because the model file still has the old value)"
    return {
        "verdict_before": verdict_words(cur["verdict"]),
        "verdict_after": verdict_words(new["verdict"]) + note,
        "rows": rows,
        "new": [finding_line(f) for f in new_lines],
        "resolved": [finding_line(f) for f in gone_lines],
        "echo": [finding_line(f) for f in echo],
        "invalidated": _invalidated(document),
        "unchanged": len((before.keys() | after.keys()) - echoed - changed),
    }
