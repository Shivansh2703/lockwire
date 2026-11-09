"""Stage 4 `commands` — the watched write layer scored in Test.

The stage is a READ: it scores a command record that already exists, through
the existing ``plan``/``checks`` evaluators (one evaluator), and it
never gates. Absence, emptiness and a truncated row are all ``not_scored``,
never a crash and never a silent pass.

Reconciled against eb-09's LANDED ``command_record.py`` (sha e53f1be, branch
eb-09-0908), which is not an ancestor of this branch: the field names and the
sidecar location come from its code, and ``EB09_ROWS`` below is three real
rows it wrote, checked in verbatim as the anchor.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire_pro import api_pro as api
from lockwire_pro.api_payload_pro import PreflightPayload
from lockwire.cli import app
from lockwire_pro.command_record import CommandEvent, append_command_event
from lockwire_pro.jobs import build_argv
from lockwire_pro.preflight import runner
from lockwire_pro.preflight.runner import STAGES, run_preflight
from lockwire_pro.preflight.source import LiveSource, RecordedSource
from lockwire_pro.preflight.stages import commands
from lockwire_pro.recording import DeviceStream, Recording, load_recording
from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import Finding, verdict
from lockwire.schema import (
    ArtifactRef,
    Contract,
    Driver,
    JointLimits,
    PowerDraw,
    PowerMode,
    Rail,
    save_contract,
)
from lockwire.serve import create_app
from lockwire_pro import serve_pro
from test_saved_replay import FIXTURES, _saved_test, _sha
from test_serve import TOKEN, _auth, _client


def _recording(tmp_path: Path) -> Recording:
    return Recording(
        context="bench",
        units={},
        devices={},
        env={},
        sha256="synthetic-eb06",
        path=tmp_path,  # a recording is a directory; commands.jsonl sits inside it
    )


def _append_command(
    path: Path,
    *,
    command_id: str,
    command: object,
    event: str = "received",
    device: str = "dev.imu",
    source: str | None = "ros2:/arm/command",
    t: float = 1.0,
    clock: str = "unknown",
) -> None:
    append_command_event(
        commands.commands_path_for(path),
        CommandEvent(
            event=event,
            command_id=command_id,
            t=t,
            clock=clock,
            device=device,
            issuer="/arm/command",
            source=source,
            command=command,
        ),
    )


def _check(contract: Contract, tmp_path: Path):
    return commands.check(contract, RecordedSource(_recording(tmp_path)))


def _violations(findings):
    return [f for f in findings if f.severity == "violation"]


def _shared_rail_contract(clean_contract) -> Contract:
    """Two sibling child rails under a 15 W parent; 10 W each is legal alone."""
    contract = _j3_contract(clean_contract)
    child_a = contract.rail("rail.5v")
    assert child_a is not None
    child_a.capacity.value = 100
    child_a.upstream = "rail.parent"
    contract.topology.rails.extend(
        [
            Rail(
                id="rail.parent",
                voltage={"value": 12, "unit": "V"},
                capacity={"value": 15, "unit": "W"},
                upstream="battery",
            ),
            Rail(
                id="rail.childB",
                voltage={"value": 12, "unit": "V"},
                capacity={"value": 100, "unit": "W"},
                upstream="rail.parent",
            ),
        ]
    )
    return contract


def _j3_contract(clean_contract) -> Contract:
    """clean_contract's joint renamed j3 with the owner's own +-1.9 rad limits."""
    joint = clean_contract.model.joints[0]
    joint.name = "j3"
    joint.type = "revolute"
    joint.limits = JointLimits(lower=-1.9, upper=1.9)
    device = clean_contract.device("dev.imu")
    assert device is not None
    device.driver = Driver(command_topic="/arm/command")
    return clean_contract


# ------------------------------------------------------------ the wording


def test_commanded_joint_target_beyond_limit_is_finding_with_wording(clean_contract, tmp_path):
    """The agreed sentence, verbatim — past tense, because lockwire
    refused nothing."""
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    findings = _check(contract, tmp_path)
    violation = next(f for f in _violations(findings))
    assert (
        violation.observed
        == "this write would have been refused: joint j3 target 2.1 rad, limit 1.9"
    )


# ------------------------------------------------- system level names the rail


def test_commands_each_legal_alone_jointly_overload_shared_rail_names_the_rail(
    clean_contract, tmp_path
):
    """The position no per-device layer can take. Each 10 W load is
    inside its own rail's 100 W capacity; together they cross the shared
    parent's 15 W, and the finding must name ``rail.parent``."""
    contract = _shared_rail_contract(clean_contract)
    contract.rail("rail.parent").capacity_enforced_by = "fuse"
    for rail in ("rail.5v", "rail.childB"):
        _append_command(
            tmp_path,
            command_id=rail,
            command={"kind": "rail_load", "rail": rail, "draw": {"value": 10, "unit": "W"}},
        )
        assert _violations(_check(contract, tmp_path)) == []
        commands.commands_path_for(tmp_path).unlink()

    _append_command(
        tmp_path,
        command_id="a",
        command={"kind": "rail_load", "rail": "rail.5v", "draw": {"value": 10, "unit": "W"}},
    )
    _append_command(
        tmp_path,
        command_id="b",
        command={"kind": "rail_load", "rail": "rail.childB", "draw": {"value": 10, "unit": "W"}},
    )
    violations = _violations(_check(contract, tmp_path))
    assert violations, "two commands jointly over the shared rail must be a finding"
    assert any("rail.parent" in f.observed for f in violations), [f.observed for f in violations]
    assert all(f.observed.startswith(commands.REFUSAL_PREFIX) for f in violations)
    assert all("declared enforcement provenance: fuse" in f.expected for f in violations)


# ------------------------------------------------------------ one evaluator


def test_commands_stage_uses_existing_plan_and_checks_not_a_new_evaluator(
    clean_contract, tmp_path, monkeypatch
):
    """Every verdict comes from ``validate_plan``, so silencing
    it silences the whole stage. ``checks.py`` is reached through
    ``validate_plan``'s own ``power_rollup`` — the rail test above is what
    proves that leg; this one proves there is no second comparison path here."""
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    _append_command(
        tmp_path,
        command_id="c2",
        command={"kind": "rail_load", "rail": "rail.5v", "draw": {"value": 9999, "unit": "W"}},
    )
    assert _violations(_check(contract, tmp_path)), "fixture must be violating to start with"

    calls: list[int] = []

    def _silent(_contract, plan):
        calls.append(len(plan.steps))
        from lockwire.plan import PlanReport

        return PlanReport([], [])

    monkeypatch.setattr(commands, "validate_plan", _silent)
    findings = _check(contract, tmp_path)
    assert calls == [2], "the whole record must go to validate_plan in one call"
    assert _violations(findings) == [], "the stage has no verdict of its own"


# ------------------------------------------- the record is optional


def test_commands_stage_reads_commands_jsonl_when_present(clean_contract, tmp_path):
    """v8's "when one is present": present and scorable -> scored; the same
    contract and stage with no record -> a named not_scored, never a pass."""
    contract = _j3_contract(clean_contract)
    absent = _check(contract, tmp_path)
    assert _violations(absent) == []
    reasons = [f for f in absent if f.quantity == "preflight/commands/record"]
    assert len(reasons) == 1
    assert "not present" in reasons[0].observed
    assert all(f.severity == "not_scored" for f in absent)

    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    assert _violations(_check(contract, tmp_path))


def _declared_command_contract(clean_contract) -> Contract:
    return _j3_contract(clean_contract)


def _append_observed_command(path: Path, *, source: str) -> None:
    _append_command(
        path,
        command_id="observed-command",
        source=source,
        command={"kind": "joint_target", "joint": "j3", "position": 2.1},
    )


def test_declared_command_channel_admits_a_logical_record_to_existing_plan(
    clean_contract, tmp_path
):
    contract = _declared_command_contract(clean_contract)
    _append_observed_command(tmp_path, source="ros2:/arm/command")

    violations = _violations(_check(contract, tmp_path))

    assert len(violations) == 1
    assert violations[0].observed.startswith(commands.REFUSAL_PREFIX)


def test_unbound_command_channel_is_named_not_scored_and_not_validated(clean_contract, tmp_path):
    contract = _declared_command_contract(clean_contract)
    _append_observed_command(tmp_path, source="ros2:/other/command")

    findings = _check(contract, tmp_path)

    assert _violations(findings) == []
    channel = next(
        f for f in findings if f.quantity == "preflight/commands/channel/observed-command"
    )
    assert channel.severity == "not_scored"
    assert "ros2:/other/command" in channel.observed


@pytest.mark.parametrize(
    ("source", "scored"),
    [("ros2:/arm/command", True), ("ros2:/other/command", False)],
)
def test_public_preflight_from_binds_observed_command_channel(
    clean_contract, tmp_path, source, scored
):
    """The public CLI consumes the retained sidecar under its authored topic."""
    contract = _j3_contract(clean_contract)
    contract_path = tmp_path / "contract.yaml"
    save_contract(contract, contract_path)
    recording = tmp_path / "recording"
    shutil.copytree(Path(__file__).parent / "fixtures" / "bags" / "clean", recording)
    _append_observed_command(recording, source=source)
    result_path = tmp_path / "preflight.json"

    # Stage 4 aggregates cross-record, cross-signal, coverage, and commands.
    # Capture the same public recording without its command sidecar so this
    # assertion distinguishes a command admission from those other checks.
    commands_path = commands.commands_path_for(recording)
    command_bytes = commands_path.read_bytes()
    commands_path.unlink()
    baseline_path = tmp_path / "baseline.json"
    baseline = CliRunner().invoke(
        app,
        [
            "preflight",
            str(contract_path),
            "--from",
            str(recording),
            "--no-ledger",
            "--json",
            str(baseline_path),
        ],
    )
    assert baseline.exit_code != 2, baseline.output
    baseline_scored = int(
        json.loads(baseline_path.read_text(encoding="utf-8"))["scored_by_stage"]["4"]
    )
    commands_path.write_bytes(command_bytes)

    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(contract_path),
            "--from",
            str(recording),
            "--no-ledger",
            "--json",
            str(result_path),
        ],
    )

    assert result.exit_code != 2, result.output
    document = json.loads(result_path.read_text(encoding="utf-8"))
    stage = [row for row in document["findings"] if row["stage"] == 4]
    if scored:
        assert result.exit_code == 1
        assert int(document["scored_by_stage"]["4"]) > baseline_scored
        assert any(
            row["severity"] == "violation" and row["observed"].startswith(commands.REFUSAL_PREFIX)
            for row in stage
        )
    else:
        assert int(document["scored_by_stage"]["4"]) == baseline_scored
        assert not any(row["severity"] == "violation" for row in stage)
        assert any(
            row["quantity"] == "preflight/commands/channel/observed-command"
            and row["severity"] == "not_scored"
            and "ros2:/other/command" in row["observed"]
            for row in stage
        )


def test_empty_record_is_not_scored_not_a_pass(clean_contract, tmp_path):
    contract = _j3_contract(clean_contract)
    commands.commands_path_for(tmp_path).write_text("", encoding="utf-8")
    findings = _check(contract, tmp_path)
    assert all(f.severity == "not_scored" for f in findings)
    reason = next(f for f in findings if f.quantity == "preflight/commands/record")
    assert "no scorable command" in reason.observed


@pytest.mark.parametrize(
    "bad",
    ["{not json", '"a bare string"', '{"command_id": "x", "command": {"kind": "nonesuch"}}'],
)
def test_truncated_or_malformed_row_names_the_line_and_scores_the_rest(
    clean_contract, tmp_path, bad
):
    """A robot that lost power mid-write leaves a half-line. One bad row is
    disqualified by line number; the good row beside it is still scored."""
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    with commands.commands_path_for(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(bad + "\n")
    findings = _check(contract, tmp_path)
    unusable = [f for f in findings if f.quantity == "preflight/commands/record/line/2"]
    assert len(unusable) == 1 and unusable[0].severity == "not_scored"
    assert _violations(findings), "the well-formed row beside it must still be scored"


def test_changed_command_with_stale_row_hash_is_not_scored(clean_contract, tmp_path):
    contract = _j3_contract(clean_contract)
    _append_observed_command(tmp_path, source="ros2:/arm/command")
    path = commands.commands_path_for(tmp_path)
    source = RecordedSource(_recording(tmp_path))
    command_bytes = path.read_bytes()
    path.unlink()
    baseline = run_preflight(contract, source)
    path.write_bytes(command_bytes)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["command"]["position"] = 3.0
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    report = run_preflight(contract, source)

    assert report.scored_by_stage[4] == baseline.scored_by_stage[4]
    assert commands.scored(contract, source) == 0
    assert not any(f.stage == 4 and f.severity == "violation" for f in report)
    integrity = next(f for f in report if f.quantity == "preflight/commands/record/line/1")
    assert integrity.severity == "not_scored"
    assert "hash does not match" in integrity.observed


def test_stage_never_rewrites_the_record(clean_contract, tmp_path):
    """lockwire measures the write layer, it does not edit it."""
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    path = commands.commands_path_for(tmp_path)
    before = path.read_bytes()
    _check(contract, tmp_path)
    assert path.read_bytes() == before


def test_disallowed_observed_power_mode_is_a_finding_without_rewriting_record(
    clean_contract, tmp_path
):
    device = clean_contract.device("dev.imu")
    assert device is not None
    device.power = PowerDraw(
        rail="rail.5v",
        draw={"value": 1.0, "unit": "W"},
        modes=[PowerMode(name="high", draw={"value": 2.0, "unit": "W"})],
        choices=["nominal"],
    )
    _j3_contract(clean_contract)
    _append_command(
        tmp_path,
        command_id="c1",
        command={"kind": "set_power_mode", "device": device.id, "mode": "high"},
    )
    path = commands.commands_path_for(tmp_path)
    before = path.read_bytes()

    findings = _check(clean_contract, tmp_path)

    violation = next(f for f in _violations(findings) if f.quantity.endswith("set_power_mode/mode"))
    assert violation.observed.startswith(commands.REFUSAL_PREFIX)
    assert "not allowed" in violation.observed
    assert path.read_bytes() == before
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_one_logical_command_is_scored_once_across_its_lifecycle_events(clean_contract, tmp_path):
    """eb-09 writes received/forwarded/completed for one command; correlating
    by identity means one finding, not three."""
    contract = _j3_contract(clean_contract)
    command = {"kind": "joint_target", "joint": "j3", "position": 2.1}
    for event in ("received", "forwarded", "completed"):
        _append_command(tmp_path, command_id="c1", event=event, command=command)
    assert len(_violations(_check(contract, tmp_path))) == 1


# ------------------------------------------------- eb-09's real bytes

# Three rows written by eb-09's own `append_command_event` (branch eb-09-0908,
# sha e53f1be), captured verbatim 2026-09-08. eb-09 is not an ancestor of this
# branch, so this literal — not a helper in this file — is what anchors the
# stage to the real record format: field names, the three lifecycle events, the
# hash chain columns and every field this stage must ignore.
EB09_ROWS = """\
{"event": "received", "command_id": "c1", "t": 1.0, "clock": "ros2bag", "device": "dev.arm", "issuer": "/cmd", "command": {"kind": "joint_target", "joint": "j3", "position": 2.1}, "command_text": null, "representable": true, "command_class": "joint_target", "device_verdict": null, "system_verdict": null, "decided_by": null, "margins": {}, "outcome": null, "settle_s": null, "contract_sha": "", "witness": null, "source": null, "device_mapping": null, "schema_version": 1, "tool": "lockwire 0.1.0", "prev": "0000000000000000000000000000000000000000000000000000000000000000", "hash": "567bd07144036cc9f6d6e12e24ac6921573504fa66226c74863f0c460037dd18"}
{"event": "forwarded", "command_id": "c1", "t": 1.0, "clock": "ros2bag", "device": "dev.arm", "issuer": "/cmd", "command": {"kind": "joint_target", "joint": "j3", "position": 2.1}, "command_text": null, "representable": true, "command_class": "joint_target", "device_verdict": null, "system_verdict": null, "decided_by": null, "margins": {}, "outcome": null, "settle_s": null, "contract_sha": "", "witness": null, "source": null, "device_mapping": null, "schema_version": 1, "tool": "lockwire 0.1.0", "prev": "567bd07144036cc9f6d6e12e24ac6921573504fa66226c74863f0c460037dd18", "hash": "6286ba7e18ef4c78d8d3102a884490eb3ddd4d749bd51ea7d3783ab1c37c5eb2"}
{"event": "completed", "command_id": "c1", "t": 1.0, "clock": "ros2bag", "device": "dev.arm", "issuer": "/cmd", "command": {"kind": "joint_target", "joint": "j3", "position": 2.1}, "command_text": null, "representable": true, "command_class": "joint_target", "device_verdict": null, "system_verdict": null, "decided_by": null, "margins": {}, "outcome": null, "settle_s": null, "contract_sha": "", "witness": null, "source": null, "device_mapping": null, "schema_version": 1, "tool": "lockwire 0.1.0", "prev": "6286ba7e18ef4c78d8d3102a884490eb3ddd4d749bd51ea7d3783ab1c37c5eb2", "hash": "ae62ca01549e58e7ddee215ea8d1bcd6cb26d29098ff3b5ae17e91087259465f"}
"""


def test_historical_eb09_record_without_source_is_not_scored(clean_contract, tmp_path):
    """Historical eb-09 bytes lack a recorder source, so they remain visible but unbound."""
    contract = _j3_contract(clean_contract)
    commands.commands_path_for(tmp_path).write_text(EB09_ROWS, encoding="utf-8")
    findings = _check(contract, tmp_path)
    assert _violations(findings) == []
    channel = next(f for f in findings if f.quantity == "preflight/commands/channel/c1")
    assert channel.severity == "not_scored"
    assert "source=None" in channel.observed


def test_unrepresentable_payload_is_disclosed_not_skipped(clean_contract, tmp_path):
    """eb-09 keeps a payload it could not represent as JSON, with its text
    (never withheld). It cannot be scored — and the stage must say so, not skip it."""
    contract = _j3_contract(clean_contract)
    _append_command(tmp_path, command_id="c1", command=b"\x00raw")
    findings = _check(contract, tmp_path)
    unusable = [f for f in findings if f.quantity == "preflight/commands/record/c1"]
    assert len(unusable) == 1
    assert "unrepresentable" in unusable[0].observed
    assert all(f.severity == "not_scored" for f in findings)


# ------------------------------------------------------------ runner wiring


def test_stage_is_registered_and_runs_under_run_preflight(clean_contract, tmp_path):
    """The registry row and the scored counter both exist — a missing counter
    entry is a KeyError inside ``run_preflight``, not a quiet skip."""
    assert any(entry.name == "commands" for entry in STAGES)
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 2.1}
    )
    report = run_preflight(contract, RecordedSource(_recording(tmp_path)))
    stamped = [f for f in report if f.quantity.startswith("preflight/commands/")]
    assert stamped and all(f.stage == 4 for f in stamped)


# Powered-release policy uses the same preflight and report path exercised above.

CLASSES = [
    "watched-command-stream",
    "live-hardware-software-reconciliation",
    "comm-links-under-representative-load",
]


@pytest.mark.parametrize("required", [[value] for value in CLASSES] + [CLASSES])
@pytest.mark.parametrize("bag", ["clean", "rate_collapse"])
def test_selected_missing_classes_refuse_and_analysis_findings_survive(
    clean_contract, required, bag
):
    source = RecordedSource(load_recording(FIXTURES / "bags" / bag))
    analysis = run_preflight(clean_contract, source)
    release = run_preflight(
        clean_contract, source, profile="powered-release", required_evidence=required
    )
    assert release == analysis and release.scored == analysis.scored
    assert verdict(analysis).code == ("GO" if bag == "clean" else "NO_GO")
    assert release.missing_required_evidence == required
    assert (verdict(release).code, verdict(release).exit_code) == (
        ("REFUSED", 2) if bag == "clean" else ("NO_GO", 1)
    )


@pytest.mark.parametrize(
    "severity,scored,code",
    [("warning", 1, "REFUSED"), ("violation", 1, "NO_GO"), ("not_scored", 0, "NOT_SCORED")],
)
def test_required_evidence_preserves_real_failure_and_presence_floor(severity, scored, code):
    report = PreflightReport(
        [Finding(severity=severity, quantity="x", observed="x", expected="x", action="x")],
        scored_by_stage={0: scored},
        profile="powered-release",
        required_evidence=CLASSES,
        missing_required_evidence=CLASSES,
    )
    assert verdict(report).code == code
    assert verdict(report).exit_code == {"REFUSED": 2, "NO_GO": 1, "NOT_SCORED": 3}[code]
    assert len(report) == 1


@pytest.mark.parametrize("omit_disclosures", [False, True])
def test_live_enumeration_is_not_reconciliation_or_representative_load(
    clean_contract, monkeypatch, omit_disclosures
):
    recorded = RecordedSource(load_recording(FIXTURES / "bags" / "clean"))
    # Exercise the LiveSource path without any target commands or hardware.
    for method in ("enumerate", "liveness", "samples"):
        monkeypatch.setattr(LiveSource, method, lambda self, name=method: getattr(recorded, name)())
    if omit_disclosures:
        monkeypatch.setattr(
            runner,
            "STAGES",
            tuple(
                replace(entry, check=lambda *_: []) if entry.name == "coverage" else entry
                for entry in runner.STAGES
            ),
        )
    report = run_preflight(
        clean_contract,
        LiveSource(clean_contract, runner=None),
        profile="powered-release",
        required_evidence=CLASSES[1:],
    )
    assert report.missing_required_evidence == CLASSES[1:]
    assert verdict(report).code == "NO_GO"  # LiveSource also scores observed device absence.


@pytest.mark.parametrize(
    "profile,required",
    [("powered-release", []), ("powered-release", ["unknown"]), ("unknown", CLASSES)],
)
def test_invalid_policy_refuses_runner_and_cli(clean_contract, profile, required):
    with pytest.raises(ValueError):
        build_argv(
            "preflight",
            contract="robot.yaml",
            recording="bag",
            profile=profile,
            required_evidence=required,
        )
    with pytest.raises(ValueError):
        create_app(TOKEN, policy=serve_pro.policy(profile, required))
    with pytest.raises(ValueError):
        api.RunJobController().start("robot.yaml", profile=profile, required_evidence=required)
    with pytest.raises(ValueError):
        api.preflight(
            clean_contract, FIXTURES / "bags" / "clean", profile=profile, required_evidence=required
        )
    with pytest.raises(ValueError):
        run_preflight(
            clean_contract,
            RecordedSource(load_recording(FIXTURES / "bags" / "clean")),
            profile=profile,
            required_evidence=required,
        )
    flags = [item for value in required for item in ["--require-evidence", value]]
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(FIXTURES / "contract_clean.yaml"),
            "--from",
            str(FIXTURES / "bags" / "clean"),
            "--profile",
            profile,
            "--no-ledger",
            *flags,
        ],
    )
    assert result.exit_code == 2 and "preflight failed" in result.output


@pytest.mark.parametrize("gap", ["none", "integrity", "channel"])
def test_declared_watched_commands_cannot_clear_without_sensor_samples(
    clean_contract, tmp_path, gap
):
    """, a watched command stream with no
    sensor samples cannot clear; the run is NOT SCORED whatever the stream's gaps."""
    contract = _j3_contract(clean_contract)
    _append_command(
        tmp_path, command_id="c1", command={"kind": "joint_target", "joint": "j3", "position": 1}
    )
    if gap == "channel":
        _append_command(
            tmp_path,
            command_id="c2",
            command={"kind": "joint_target", "joint": "j3", "position": 1},
            source="ros2:/other",
        )
    elif gap == "integrity":
        with (tmp_path / "commands.jsonl").open("a") as stream:
            stream.write('{"broken":true}\n')
    report = run_preflight(
        contract,
        RecordedSource(_recording(tmp_path)),
        profile="powered-release",
        required_evidence=[CLASSES[0]],
    )
    assert any(f.quantity == "preflight/commands/in_recording" for f in report)
    assert report.missing_required_evidence == ([] if gap == "none" else [CLASSES[0]])
    assert verdict(report).code == "NOT_SCORED"  # (was GO_WITH_NOTES / REFUSED)


def test_api_payload_jobs_and_enforced_server_policy(tmp_path):
    contract, recording, *_ = _saved_test(tmp_path)
    document = api.preflight(
        contract, recording, profile="powered-release", required_evidence=CLASSES
    )
    assert PreflightPayload.model_validate(document).missing_required_evidence == CLASSES
    argv = build_argv(
        "preflight",
        contract=contract,
        recording=recording,
        profile="powered-release",
        required_evidence=CLASSES,
    )
    assert argv[-8:] == [
        "--profile",
        "powered-release",
        *[item for value in CLASSES for item in ["--require-evidence", value]],
    ]
    server = create_app(TOKEN, policy=serve_pro.policy("powered-release", CLASSES))
    with _client(server) as client:
        args = {"contract": str(contract), "recording": str(recording), "profile": "analysis"}
        reply = client.post(
            "/v1/run", headers=_auth(), json={"command": "preflight", "args": args}
        ).json()
        assert reply["payload"]["verdict"] == "REFUSED" and reply["exit_code"] == 2
        client.post("/v1/run", headers=_auth(), json={"command": "run-start", "args": args})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = client.post(
                "/v1/run", headers=_auth(), json={"command": "run-poll", "args": {}}
            ).json()["payload"]
            if state["status"] != "running":
                break
            time.sleep(0.05)
    assert state["status"] == "completed" and state["exit_code"] == 2
    assert state["document"]["missing_required_evidence"] == CLASSES


@pytest.mark.parametrize("legacy", [True, False])
def test_saved_policy_replay_pins_requirements_and_old_output_hash(tmp_path, legacy):
    contract, recording, root, manifest, ref, target = _saved_test(tmp_path)
    policy = {} if legacy else {"profile": "powered-release", "required_evidence": CLASSES}
    document = api.preflight(contract, recording, **policy)
    hashed = {
        k: v
        for k, v in document.items()
        if not legacy or k not in {"profile", "required_evidence", "missing_required_evidence"}
    }
    manifest.update(
        policy,
        output_sha256=_sha(json.dumps(hashed, sort_keys=True, separators=(",", ":")).encode()),
    )
    path = root / ref.path
    path.write_text(json.dumps(manifest))
    ref = ArtifactRef(path=ref.path, sha256=_sha(path.read_bytes()))
    kwargs = dict(saved_manifest=ref, evidence_root=root, selected_target_runner=target)
    assert api.preflight(contract, recording, **kwargs) == document
    with pytest.raises(ValueError, match="policy differs"):
        api.preflight(
            contract,
            recording,
            **kwargs,
            profile="powered-release" if legacy else "analysis",
            required_evidence=CLASSES,
        )
    if not legacy:
        for mutation in [
            {"profile": "analysis"},
            {"required_evidence": [CLASSES[0]]},
            {"profile": None, "required_evidence": None},
        ]:
            changed = {**manifest, **mutation}
            changed = {k: v for k, v in changed.items() if v is not None}
            path.write_text(json.dumps(changed))
            kwargs["saved_manifest"] = ArtifactRef(path=ref.path, sha256=_sha(path.read_bytes()))
            with pytest.raises(ValueError, match="output differs"):
                api.preflight(contract, recording, **kwargs)


def test_lossy_command_stream_is_not_watched_evidence(clean_contract, tmp_path):
    """a stream that admits lost messages cannot satisfy watched-command-stream."""
    contract = _j3_contract(clean_contract)
    append_command_event(
        commands.commands_path_for(tmp_path),
        CommandEvent(
            event="received", command_id="c1", t=1.0, clock="unknown", device="dev.imu",
            issuer="/arm/command", source="ros2:/arm/command",
            command={"kind": "joint_target", "joint": "j3", "position": 0.5},
            transport_issue="sequence gap: 40 messages lost",
        ),
    )  # fmt: skip
    report = run_preflight(
        contract,
        RecordedSource(_recording(tmp_path)),
        profile="powered-release",
        required_evidence=[CLASSES[0]],
    )
    assert report.missing_required_evidence == [CLASSES[0]]
    assert any("40 messages lost" in f.observed for f in report)


# ------------------------------------- commands vs observed motion


def _moving(tmp_path: Path, **signals: list[float]) -> Recording:
    """dev.imu sampled every 0.1 s from 0 to 3 s; one list per signal."""
    n = len(next(iter(signals.values())))
    stream = DeviceStream("dev.imu", [i / 10 for i in range(n)], signals)
    return replace(_recording(tmp_path), devices={"dev.imu": stream}, source="ros2bag")


def _motion(contract, recording):
    return [
        f
        for f in commands.check(contract, RecordedSource(recording))
        if f.quantity.endswith("/motion")
    ]


def _command_at(path: Path, t: float, clock: str = "ros2bag") -> None:
    _append_command(
        path,
        command_id=f"c{t}",
        t=t,
        command={"kind": "joint_target", "joint": "j3", "position": 0.5},
        clock=clock,  # the bag's clock, as a bridge that rebased its times would say
    )


def test_uncommanded_motion_is_a_warning(clean_contract, tmp_path):
    contract = _j3_contract(clean_contract)
    velocity = [0.0] * 10 + [0.5] * 5 + [0.0] * 16  # moves at 1.0-1.4 s
    recording = _moving(tmp_path, joint_velocity=velocity)
    _command_at(tmp_path, 10.0)  # a command exists, but far from the motion

    (finding,) = _motion(contract, recording)

    assert finding.severity == "warning"
    assert finding.quantity == "preflight/commands/dev.imu/motion"
    assert finding.observed == "dev.imu moved with no command at t=1 s"


def test_commandless_command_is_a_warning(clean_contract, tmp_path):
    contract = _j3_contract(clean_contract)
    recording = _moving(tmp_path, gyro_x=[0.001] * 31, gyro_y=[0.0] * 31, gyro_z=[0.0] * 31)
    _command_at(tmp_path, 1.0)

    (finding,) = _motion(contract, recording)

    assert finding.severity == "warning"
    assert finding.observed == "dev.imu commanded, no motion seen near t=1 s"
    # the same command with the IMU turning inside the window is silent
    spin = [0.0] * 12 + [0.5] * 3 + [0.0] * 16
    assert (
        _motion(contract, _moving(tmp_path, gyro_x=spin, gyro_y=[0.0] * 31, gyro_z=[0.0] * 31))
        == []
    )


def test_clean_bag_gains_no_comparison_finding(clean_contract):
    bag = Path(__file__).parent / "fixtures" / "bags" / "clean"
    recording = load_recording(bag)

    assert _motion(clean_contract, recording) == []  # no watched stream, no comparison
    result = CliRunner().invoke(
        app, ["preflight", str(FIXTURES / "contract_clean.yaml"), "--from", str(bag), "--no-ledger"]
    )
    assert "/motion" not in result.output
    assert "GO — records agree (28 scored); 21 check(s) not scored" in " ".join(
        result.output.split()
    )


def test_no_feedback_and_no_imu_is_not_scored(clean_contract, tmp_path):
    contract = _j3_contract(clean_contract)
    _command_at(tmp_path, 1.0)

    (finding,) = _motion(contract, replace(_recording(tmp_path), source="ros2bag"))

    assert finding.severity == "not_scored"
    assert finding.quantity == "preflight/commands/dev.imu/motion"
    assert "no velocity or position channel and the recording has no IMU" in finding.observed


def test_motion_warning_is_go_with_notes_and_strict_makes_it_exit_1(
    clean_contract, tmp_path, monkeypatch
):
    # a native bag has no source, hence no known clock; make it a ros2bag-clock one
    monkeypatch.setattr(commands, "clock_for", lambda source: "ros2bag")
    contract_path = tmp_path / "contract.yaml"
    save_contract(_j3_contract(clean_contract), contract_path)
    bag = tmp_path / "bag"
    shutil.copytree(Path(__file__).parent / "fixtures" / "bags" / "clean", bag)
    _command_at(bag, 1.0)  # inside the limit; the quiet IMU shows no motion
    argv = ["preflight", str(contract_path), "--from", str(bag), "--no-ledger"]

    result = CliRunner().invoke(app, argv)

    flat = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "GO with notes" in flat and "commanded, no motion seen near t=1 s" in flat
    assert CliRunner().invoke(app, [*argv, "--strict"]).exit_code == 1


def test_commands_on_another_clock_are_not_compared(clean_contract, tmp_path):
    # The production bridge stamps wall-clock epoch seconds and says "unknown";
    # a bag's times are rebased to 0-3 s. Comparing them called every sample
    # "moved with no command". Different clocks, say so, compare nothing.
    contract = _j3_contract(clean_contract)
    recording = _moving(tmp_path, joint_velocity=[0.5] * 31)
    _command_at(tmp_path, 1.7e9, clock="unknown")

    (finding,) = _motion(contract, recording)

    assert finding.severity == "not_scored"
    assert finding.quantity == "preflight/commands/dev.imu/motion"
    assert "different clocks; not compared" in finding.observed


def test_motion_band_multiple_keeps_noise_silent(clean_contract, tmp_path):
    # A commanded IMU with only noise-level gyro (0.02 rad/s; declared band
    # +-0.0116) is not "moved with no command" at 3x the band; at 1x it would be.
    contract = _j3_contract(clean_contract)
    noise = [0.02] * 31
    recording = _moving(tmp_path, gyro_x=noise, gyro_y=[0.0] * 31, gyro_z=[0.0] * 31)
    recording = replace(recording, context="nominal-op")  # the context the bands are declared in
    _command_at(tmp_path, 1.0)

    assert [f.observed for f in _motion(contract, recording)] == [
        "dev.imu commanded, no motion seen near t=1 s"
    ]
