"""The ROS package's fail-closed decision, tested off-robot.

`lockwire_ros.core` and `lockwire_ros.diagnostics` import nothing from ROS, which
is what lets the two modules that actually hold judgement — when the gate clears
a robot, and how a finding is graded for `/diagnostics` — run in the ordinary
suite instead of only inside a container.

The claim under test throughout: **there is exactly one way to get GO**, and it
is an affirmative document from a clean run. Missing binary, timeout, crash,
unreadable JSON, a document disagreeing with its own process — every one of them
is a refusal. A robot cleared because the gate could not run is the worst
outcome this package could produce.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lockwire_ros import core, diagnostics


def _real_lockwire_executable() -> str | None:
    """Use the Humble harness's core, then the one beside this interpreter, then PATH.

    The interpreter's own bin comes first so a stale `lockwire` elsewhere on PATH
    is never the one tested.
    """
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    return os.environ.get("LOCKWIRE_REAL_EXECUTABLE") or shutil.which("lockwire", path=path)


#: The real `lockwire` console script, if this environment installed it. Absent
#: by design on the ROS-Humble container (module docstring) — the anchor test
#: below skips there rather than erroring, same convention as test_live_core.py.
LOCKWIRE_EXECUTABLE = _real_lockwire_executable()
#: repo root: ros/lockwire_ros/test/test_core.py -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]

CLEAN_DOCUMENT = {
    "artifact_type": "lockwire-preflight-report",
    "verdict": "GO",
    "exit_code": 0,
    "counts": {"violation": 0, "warning": 0, "not_scored": 3},
    "scored": 31,
    "findings": [
        {
            "severity": "not_scored",
            "quantity": "preflight/coverage/live_recon",
            "observed": "permanently out of scope offline (requires live hardware)",
            "expected": "a powered-on robot",
            "action": "check it on the powered-on robot",
        },
    ],
}
NO_GO_DOCUMENT = {
    "verdict": "NO-GO",
    "exit_code": 1,
    "counts": {"violation": 1, "warning": 0, "not_scored": 2},
    "scored": 31,
    "findings": [
        {
            "severity": "violation",
            "quantity": "devices/dev.gnss/rate_hz",
            "observed": "0.20 Hz",
            "expected": "in [4.5, 5.5] Hz",
            "delta": "-4.80 Hz",
            "action": "check the GNSS driver",
            "owner": "nav",
        },
    ],
}


def _fake_lockwire(tmp_path, body: str, name: str = "lockwire"):
    """A stand-in executable, so the subprocess boundary is real rather than mocked.

    The point of this package is that it crosses a process boundary; a mocked
    `subprocess.run` would test the mock. These write a real file and exit with
    a real code.
    """
    script = tmp_path / name
    script.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# interpret(): the decision, without a subprocess
# ---------------------------------------------------------------------------


def test_a_clean_document_is_the_only_route_to_go():
    result = core.interpret(CLEAN_DOCUMENT, exit_code=0)
    assert result.outcome == core.GO
    assert result.clears is True
    assert result.scored == 31


def test_no_go_document_does_not_clear():
    result = core.interpret(NO_GO_DOCUMENT, exit_code=1)
    assert result.outcome == core.NO_GO
    assert result.clears is False
    assert [f["quantity"] for f in result.violations()] == ["devices/dev.gnss/rate_hz"]


def test_real_core_anchor_uses_the_harness_executable(monkeypatch):
    monkeypatch.setenv("LOCKWIRE_REAL_EXECUTABLE", "/opt/lockwire-venv/bin/lockwire")
    monkeypatch.setattr(shutil, "which", lambda _name, **_kw: None)

    assert _real_lockwire_executable() == "/opt/lockwire-venv/bin/lockwire"


def test_real_core_anchor_skips_without_harness_or_path_executable(monkeypatch):
    monkeypatch.delenv("LOCKWIRE_REAL_EXECUTABLE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name, **_kw: None)

    assert _real_lockwire_executable() is None


@pytest.mark.skipif(
    LOCKWIRE_EXECUTABLE is None,
    reason="lockwire core not installed here (by design on ROS-Humble; module docstring)",
)
def test_a_real_no_go_document_from_the_core_is_not_refused_as_unknown(tmp_path):
    """eb-18 / PARITY-ANCHOR: the allow-list must be proved against the CORE's own
    words, not a hand-written fixture that merely repeats what the allow-list
    already expects — that repetition is exactly why this bug shipped and the
    Humble job stayed green through it (every hand-written fixture above used
    the older un-hyphenated spelling too).

    Runs the real `lockwire preflight --json` on a committed contract + bag known
    to fail stage 4 (condition_envelope_exit), then feeds the genuine document —
    not a re-typed copy of it — straight into `interpret()`.
    """
    contract = REPO_ROOT / "tests" / "fixtures" / "contract_clean.yaml"
    recording = REPO_ROOT / "tests" / "fixtures" / "bags" / "condition_envelope_exit"
    assert contract.exists() and recording.exists()
    json_path = tmp_path / "preflight.json"

    completed = subprocess.run(
        [
            LOCKWIRE_EXECUTABLE,
            "preflight",
            str(contract),
            "--from",
            str(recording),
            "--no-ledger",
            "--json",
            str(json_path),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr

    document = json.loads(json_path.read_text())
    assert document["verdict"] == "NO-GO", document  # the producer's real spelling

    result = core.interpret(document, exit_code=completed.returncode)
    assert result.outcome == core.NO_GO, result.reason
    assert result.clears is False


def test_go_with_notes_clears_but_says_so():
    document = dict(
        CLEAN_DOCUMENT,
        verdict="GO_WITH_NOTES",
        counts={"violation": 0, "warning": 2, "not_scored": 0},
    )
    result = core.interpret(document, exit_code=0)
    assert result.clears is True
    assert "warning" in result.reason


def test_a_pass_that_scored_nothing_is_flagged_with_exit_three():
    """The presence floor (a GO that scored nothing proves nothing) carried across the process boundary."""
    document = dict(CLEAN_DOCUMENT, verdict="NOT_SCORED", exit_code=3, scored=0)
    result = core.interpret(document, exit_code=3)
    assert result.scored_nothing is True
    assert "not clearance" in result.reason


@pytest.mark.parametrize("verdict", [None, "", "OK", "PASS", "go", 7])
def test_an_unknown_verdict_is_refused_never_assumed_good(verdict):
    result = core.interpret(dict(CLEAN_DOCUMENT, verdict=verdict), exit_code=0)
    assert result.outcome == core.REFUSED
    assert result.clears is False


def test_a_document_that_disagrees_with_its_own_process_is_refused():
    """Exit 0 with a NO_GO body (or the reverse) means one of them is lying."""
    result = core.interpret(NO_GO_DOCUMENT, exit_code=0)
    assert result.outcome == core.REFUSED
    assert "refusing rather than choosing" in result.reason


def test_a_non_object_document_is_refused():
    assert core.interpret(["not", "a", "document"], exit_code=0).outcome == core.REFUSED


# ---------------------------------------------------------------------------
# run_gate(): the real subprocess boundary
# ---------------------------------------------------------------------------


def test_run_gate_reads_a_real_subprocess_and_clears(tmp_path):
    executable = _fake_lockwire(
        tmp_path,
        """
        import json, sys
        json.dump({0}, open(sys.argv[sys.argv.index("--json") + 1], "w"))
        sys.exit(0)
        """.format(json.dumps(CLEAN_DOCUMENT)),
    )

    result = core.run_gate("lockwire.yaml", "rec/", executable=executable)

    assert result.outcome == core.GO, result.reason
    assert result.scored == 31


def test_run_gate_surfaces_a_real_no_go(tmp_path):
    executable = _fake_lockwire(
        tmp_path,
        """
        import json, sys
        json.dump({0}, open(sys.argv[sys.argv.index("--json") + 1], "w"))
        sys.exit(1)
        """.format(json.dumps(NO_GO_DOCUMENT)),
    )

    result = core.run_gate("lockwire.yaml", "rec/", executable=executable)

    assert result.outcome == core.NO_GO
    assert result.exit_code == 1


def test_a_missing_executable_is_refused_not_passed():
    result = core.run_gate("c.yaml", "rec/", executable="lockwire-does-not-exist-anywhere")
    assert result.outcome == core.REFUSED
    assert "not found" in result.reason
    assert result.clears is False


def test_exit_2_tool_failure_is_refused_not_treated_as_no_go(tmp_path):
    """§3.3 exit 2 means the tool broke. That is 'unknown', not 'robot is bad'."""
    executable = _fake_lockwire(
        tmp_path,
        """
        import sys
        print("preflight failed: no contract at c.yaml", file=sys.stderr)
        sys.exit(2)
        """,
    )

    result = core.run_gate("c.yaml", "rec/", executable=executable)

    assert result.outcome == core.REFUSED
    assert result.outcome != core.NO_GO
    assert "no contract" in result.reason


def test_a_timeout_is_refused_and_does_not_hang(tmp_path):
    executable = _fake_lockwire(
        tmp_path,
        """
        import time
        time.sleep(30)
        """,
    )

    result = core.run_gate("c.yaml", "rec/", executable=executable, timeout_s=1.0)

    assert result.outcome == core.REFUSED
    assert "did not finish" in result.reason


def test_exit_zero_with_unreadable_json_is_refused(tmp_path):
    """The nastiest shape: the process says fine, the evidence is garbage."""
    executable = _fake_lockwire(
        tmp_path,
        """
        import sys
        open(sys.argv[sys.argv.index("--json") + 1], "w").write("{not json")
        sys.exit(0)
        """,
    )

    result = core.run_gate("c.yaml", "rec/", executable=executable)

    assert result.outcome == core.REFUSED
    assert "unreadable" in result.reason


def test_exit_zero_writing_no_json_at_all_is_refused(tmp_path):
    executable = _fake_lockwire(
        tmp_path,
        """
        import os, sys
        os.unlink(sys.argv[sys.argv.index("--json") + 1])
        sys.exit(0)
        """,
    )

    assert core.run_gate("c.yaml", "rec/", executable=executable).outcome == core.REFUSED


def test_each_run_uses_a_fresh_json_path(tmp_path):
    """A fixed path would let a crashed run leave a stale GO for the next one to read."""
    seen = tmp_path / "seen.txt"
    executable = _fake_lockwire(
        tmp_path,
        """
        import json, sys
        path = sys.argv[sys.argv.index("--json") + 1]
        open({0!r}, "a").write(path + "\\n")
        json.dump({1}, open(path, "w"))
        sys.exit(0)
        """.format(str(seen), json.dumps(CLEAN_DOCUMENT)),
    )

    core.run_gate("c.yaml", "rec/", executable=executable)
    core.run_gate("c.yaml", "rec/", executable=executable)

    paths = [line for line in seen.read_text().splitlines() if line]
    assert len(paths) == 2
    assert paths[0] != paths[1]


def test_build_command_passes_the_contract_and_recording_through():
    command = core.build_command("lockwire.yaml", "rec/bench", "/tmp/o.json", "lw")
    assert command == [
        "lw",
        "preflight",
        "lockwire.yaml",
        "--from",
        "rec/bench",
        "--json",
        "/tmp/o.json",
    ]


# ---------------------------------------------------------------------------
# diagnostics mapping
# ---------------------------------------------------------------------------


def test_violations_map_to_error_and_warnings_to_warn():
    document = dict(NO_GO_DOCUMENT)
    document["findings"] = NO_GO_DOCUMENT["findings"] + [
        {
            "severity": "warning",
            "quantity": "devices/dev.imu/conditions",
            "observed": "unbaselined",
            "expected": "a baseline",
            "action": "re-baseline",
        },
    ]
    rows = diagnostics.diagnostic_rows(core.interpret(document, exit_code=1))
    by_name = {row.name: row for row in rows}

    assert by_name["lockwire: preflight"].level == diagnostics.ERROR
    assert by_name["lockwire: devices/dev.gnss/rate_hz"].level == diagnostics.ERROR
    assert by_name["lockwire: devices/dev.imu/conditions"].level == diagnostics.WARN


def test_not_scored_is_collected_at_ok_and_never_raised_to_warn():
    """Raising disclosures to WARN would paint a healthy robot yellow every run."""
    rows = diagnostics.diagnostic_rows(core.interpret(CLEAN_DOCUMENT, exit_code=0))
    by_name = {row.name: row for row in rows}

    assert by_name["lockwire: preflight"].level == diagnostics.OK
    coverage = by_name["lockwire: coverage"]
    assert coverage.level == diagnostics.OK
    # ...but it is still said out loud, by name.
    assert any("live_recon" in key for key, _ in coverage.values)
    assert "not scored" in coverage.message


def test_a_refusal_is_an_error_row_that_cannot_read_as_healthy():
    rows = diagnostics.diagnostic_rows(
        core.run_gate("c.yaml", "rec/", executable="lockwire-does-not-exist-anywhere")
    )
    summary = rows[0]
    assert summary.level == diagnostics.ERROR
    assert "not found" in summary.message


def test_a_pass_that_scored_nothing_is_warn_not_ok():
    document = dict(CLEAN_DOCUMENT, verdict="NOT_SCORED", exit_code=3, scored=0)
    rows = diagnostics.diagnostic_rows(core.interpret(document, exit_code=3))
    assert rows[0].level == diagnostics.WARN


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("GO", 0), ("GO_WITH_NOTES", 0), ("NO-GO", 1), ("REFUSED", 2), ("NOT_SCORED", 3)],
)
@pytest.mark.parametrize("via_process", [False, True])
def test_only_canonical_pairs_survive_pure_and_subprocess(tmp_path, verdict, code, via_process):
    document = dict(CLEAN_DOCUMENT, verdict=verdict, exit_code=code)
    result = core.interpret(document)
    assert result.verdict == verdict and result.exit_code == code
    for bad_code in ((code + 1) % 4, None, "0", False, -1, 7):
        bad = dict(document, exit_code=bad_code)
        executable = _fake_lockwire(
            tmp_path,
            f"import json, sys\njson.dump(json.loads({json.dumps(bad)!r}), open(sys.argv[sys.argv.index('--json') + 1], 'w'))\nsys.exit({bad_code if type(bad_code) is int else code})",
        )
        result = core.run_gate("c", "r", executable=executable) if via_process else core.interpret(bad)
        assert result.outcome == core.REFUSED
    assert core.interpret(document, (code + 1) % 4).outcome == core.REFUSED
    document.pop("exit_code")
    assert core.interpret(document, code).outcome == core.REFUSED


@pytest.mark.parametrize("key", ["verdict", "counts", "findings", "scored"])
def test_malformed_metadata_refuses_without_raising(key):
    assert core.interpret(dict(CLEAN_DOCUMENT, **{key: "bad"}), 0).outcome == core.REFUSED


def test_policy_arguments_cross_the_process_boundary(tmp_path):
    evidence = ["watched-command-stream", "comm-links-under-representative-load"]
    command = core.build_command(
        "c", "r", "j", profile="powered-release", required_evidence=evidence
    )
    assert command[7:9] == ["--profile", "powered-release"]
    assert command[9::2] == ["--require-evidence", "--require-evidence"]
    assert command[10::2] == evidence
    executable = _fake_lockwire(
        tmp_path,
        f"import json, sys\nassert sys.argv[7:] == {command[7:]!r}\njson.dump({CLEAN_DOCUMENT!r}, open(sys.argv[sys.argv.index('--json') + 1], 'w'))",
    )
    assert core.run_gate(
        "c", "r", executable=executable, profile="powered-release", required_evidence=evidence
    ).clears


def test_not_scored_does_not_clear():
    document = dict(CLEAN_DOCUMENT, verdict="NOT_SCORED", exit_code=3, scored=0)
    assert core.interpret(document, exit_code=3).clears is False
