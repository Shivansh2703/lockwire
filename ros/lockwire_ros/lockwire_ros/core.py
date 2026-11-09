"""Invoke the lockwire core out of process, and refuse whenever the answer is unclear.

**Why this is a subprocess and not an import.** lockwire requires Python >= 3.12
(numpy >= 2.5 enforces it). ROS 2 Humble ships Python 3.10 and is supported to
May 2027. A node that did ``import lockwire`` would therefore never run on
Humble at all. Crossing a process boundary lets the node run on whatever Python
its distro ships while the core runs on its own — which is the project rule
("the binary IS the compatibility story") applied to ROS.

**Everything here fails closed.** The one thing this module must never do is
report GO for a run whose verdict it does not actually know. A gate that answers
"probably fine" when the tool crashed, timed out, went missing, or wrote
unreadable JSON is worse than no gate, because a crew stops looking. So every
unknown is a ``REFUSED`` outcome carrying the reason, and there is exactly one
code path that can produce ``GO`` — a clean exit 0 with a parsed document that
says so.

Python 3.10 compatible on purpose: no ``Self``, no ``match``, no 3.11+ syntax.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

#: Exit map from the lockwire contract §3.3.
EXIT_GO = 0
EXIT_NO_GO = 1
EXIT_TOOL_FAILED = 2

#: Verdicts the JSON document may carry — the exact spellings ``report.verdict_word``
#: writes at the JSON boundary (``lockwire.report.VerdictCode``): only ``NO_GO``
#: is hyphenated on the wire, and the zero-denominator word is
#: ``NOT_SCORED``. This allow-list previously used the older, un-hyphenated
#: Python identifiers, so a real NO-GO document was refused as an unknown verdict.
VERDICT_GO = "GO"
VERDICT_GO_WITH_NOTES = "GO_WITH_NOTES"
VERDICT_NOT_SCORED = "NOT_SCORED"
VERDICT_NO_GO = "NO-GO"
VERDICT_REFUSED = "REFUSED"
_VERDICT_EXITS = {
    VERDICT_GO: 0,
    VERDICT_GO_WITH_NOTES: 0,
    VERDICT_NO_GO: 1,
    VERDICT_REFUSED: 2,
    VERDICT_NOT_SCORED: 3,
}

#: Outcomes this module reports upward. ``REFUSED`` means "we do not know", and
#: it is deliberately not spelled the same as ``NO_GO`` so an operator reading a
#: log can tell a robot that failed from a gate that could not run.
GO = "GO"
NO_GO = "NO_GO"
REFUSED = "REFUSED"


@dataclass(frozen=True)
class GateResult:
    """One preflight invocation, reduced to something a lifecycle callback can act on."""

    outcome: str  # GO | NO_GO | REFUSED
    reason: str
    exit_code: Optional[int] = None
    verdict: Optional[str] = None
    scored: Optional[int] = None
    counts: Dict[str, int] = field(default_factory=dict)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    document: Optional[Dict[str, Any]] = None
    #: What the core printed on stdout — the human verdict text, shown verbatim.
    output: str = ""

    @property
    def clears(self) -> bool:
        """True only for an affirmative GO. Anything else holds the door shut."""
        return self.outcome == GO and not self.scored_nothing

    @property
    def scored_nothing(self) -> bool:
        """A pass whose denominator is zero — never to be shown as plain health.

        Presence floor: a GO that scored nothing proves nothing. The core reports this as its own verdict code so a
        consumer cannot mistake "nothing was checked" for "everything is fine".
        """
        return self.verdict == VERDICT_NOT_SCORED

    def violations(self) -> List[Dict[str, Any]]:
        return [f for f in self.findings if f.get("severity") == "violation"]


def _refused(reason: str, exit_code: Optional[int] = None) -> GateResult:
    return GateResult(outcome=REFUSED, reason=reason, exit_code=exit_code)


def build_command(
    contract: str,
    recording: str,
    json_path: str,
    executable: str = "lockwire",
    profile: str = "analysis",
    required_evidence: Optional[List[str]] = None,
    no_ledger: bool = False,
) -> List[str]:
    """Build argv, forwarding the profile and every required evidence name.

    The core CLI owns policy validation; invalid policy inputs cannot clear it.
    """
    command = [executable, "preflight", contract, "--from", recording, "--json", json_path]
    if profile != "analysis" or required_evidence:
        command += ["--profile", profile]
    for evidence in required_evidence or []:
        command += ["--require-evidence", evidence]
    if no_ledger:
        command.append("--no-ledger")
    return command


def run_gate(
    contract: str,
    recording: str,
    executable: str = "lockwire",
    timeout_s: float = 120.0,
    env: Optional[Dict[str, str]] = None,
    profile: str = "analysis",
    required_evidence: Optional[List[str]] = None,
    no_ledger: bool = False,
    json_out: Optional[str] = None,
) -> GateResult:
    """Run one preflight and reduce it to a GateResult.

    ``recording`` is a lockwire recording directory (or a ``.ulg``); the capture
    node writes one from live topics, and a team that records separately can
    point straight at theirs.

    The JSON lands in a fresh temporary file every call. That is deliberate: a
    fixed path would let a crashed run leave yesterday's document behind for
    this run to read, and clearing a robot on a stale GO is the worst failure
    this package could have. ``json_out`` keeps the document at that path instead
    (any old file there is removed first, for the same reason).
    """
    if json_out:
        json_path = json_out
        try:
            os.unlink(json_path)
        except FileNotFoundError:
            pass
    else:
        handle, json_path = tempfile.mkstemp(prefix="lockwire-preflight-", suffix=".json")
        os.close(handle)
    command = build_command(
        contract, recording, json_path, executable, profile, required_evidence, no_ledger
    )
    try:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=env,
            )
        except FileNotFoundError:
            return _refused(
                "lockwire executable {0!r} not found on PATH — the gate could not "
                "run, which is not a pass".format(executable)
            )
        except subprocess.TimeoutExpired:
            # A hung gate must not become a silent clearance, and must not hang
            # bringup forever either.
            return _refused("lockwire preflight did not finish within {0:g}s".format(timeout_s))
        except OSError as error:
            return _refused("could not run lockwire preflight: {0}".format(error))

        if completed.returncode == EXIT_TOOL_FAILED:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            refused = _refused(
                "lockwire preflight failed to run (exit 2): {0}".format(
                    detail[-1] if detail else "no detail on stderr"
                ),
                exit_code=completed.returncode,
            )
            return replace(refused, output=completed.stdout or "")

        try:
            with open(json_path, "r", encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, ValueError) as error:
            return _refused(
                "lockwire preflight exited {0} but its JSON was unreadable: {1}".format(
                    completed.returncode, error
                ),
                exit_code=completed.returncode,
            )
    finally:
        try:
            if not json_out:
                os.unlink(json_path)
        except OSError:
            pass

    return replace(interpret(document, completed.returncode), output=completed.stdout or "")


def interpret(document: Dict[str, Any], exit_code: Optional[int] = None) -> GateResult:
    """Turn a preflight document into an outcome, refusing anything ambiguous.

    Split out from ``run_gate`` so the decision can be tested without a
    subprocess, and so every rejection reason has a name.
    """
    if not isinstance(document, dict):
        return _refused("preflight document is not an object", exit_code)

    verdict = document.get("verdict")
    reported_exit = document.get("exit_code")

    if not isinstance(verdict, str) or verdict not in _VERDICT_EXITS:
        return _refused(
            "preflight document carries no known verdict: {0!r}".format(verdict), exit_code
        )

    if type(reported_exit) is not int or reported_exit != _VERDICT_EXITS[verdict]:
        return _refused(
            "preflight verdict and exit_code are missing, malformed or contradictory", exit_code
        )

    # The document states its own exit code. If the process disagreed with it,
    # something is wrong with one of them and we do not get to pick a winner.
    if exit_code is not None and (type(exit_code) is not int or exit_code != reported_exit):
        return _refused(
            "preflight exited {0} but its document says {1} — refusing rather than "
            "choosing which to believe".format(exit_code, reported_exit),
            exit_code,
        )

    counts = document.get("counts", {})
    findings = document.get("findings", [])
    scored = document.get("scored")
    if (
        not isinstance(counts, dict)
        or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in counts.items()
        )
        or not isinstance(findings, list)
        or any(not isinstance(finding, dict) for finding in findings)
        or (scored is not None and (type(scored) is not int or scored < 0))
    ):
        return _refused(
            "preflight document has malformed counts, findings or scored metadata", exit_code
        )

    common = dict(
        exit_code=exit_code if exit_code is not None else reported_exit,
        verdict=verdict,
        scored=scored,
        counts=dict(counts),
        findings=list(findings),
        document=document,
    )

    if verdict == VERDICT_REFUSED:
        return GateResult(outcome=REFUSED, reason="preflight policy REFUSED clearance", **common)

    if verdict == VERDICT_NO_GO:
        violations = common["counts"].get("violation", 0)
        return GateResult(
            outcome=NO_GO,
            reason="NO-GO — {0} violation(s)".format(violations),
            **common,
        )
    if verdict == VERDICT_NOT_SCORED:
        return GateResult(
            outcome=GO,
            reason=(
                "GO, but nothing was scored — this contract checked no operands "
                "against this recording, so it is not clearance"
            ),
            **common,
        )
    if verdict == VERDICT_GO_WITH_NOTES:
        return GateResult(
            outcome=GO,
            reason="GO with notes — {0} warning(s)".format(common["counts"].get("warning", 0)),
            **common,
        )
    return GateResult(
        outcome=GO,
        reason="GO — records agree ({0} scored)".format(common["scored"]),
        **common,
    )
