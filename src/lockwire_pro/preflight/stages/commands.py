"""Stage 4 — the watched write layer, scored in Test.

What an agent *wrote* to a device is a signal class beside what a sensor read.
This stage scores the commands recorded beside a capture against the contract
the way ``lockwire plan`` scores an intended action — and it scores them at
system level, so a command inside its own device band that pushes a shared
rail over budget is a finding **that names the rail**.

**One evaluator.** Every verdict here comes from
``plan.validate_plan``; this module builds a ``Plan`` from the record, hands it
over once, and renames the findings. There is no comparison rule in this file.
``checks.py``'s rail rollup is reached through ``validate_plan``'s own
``power_rollup`` call — never re-walked here.

**This is a read, not a gate.** The record is evidence of what
already happened. The stage never rewrites it, never drops a row, and cannot
refuse a command — the wording is deliberately past tense ("would have been
refused"). Absence of a record is ``not_scored``, never a failure and never a
silent pass.

**Commands inside the bag itself are not scored, and this stage says so.**
The spec asks for "the commanded topics and messages in the bag or log ... the
contract can score". Measured 2026-09-08: nothing in ``Recording``,
``ros2bag.py``, ``ulog.py`` or ``schema.py`` distinguishes a commanded channel
from a measured one — a device stream is numeric samples keyed by device id.
Scoring a feedback reading as if it were a write would be a false accusation,
so the bag branch stays unbuilt and is disclosed. ``Driver.command_topic``
admits watched sidecar rows, but bag readers still have no command decoder or
mapping and cannot turn numeric samples into a write.
"""

from __future__ import annotations

import json
from pathlib import Path

from lockwire_pro.command_record import (
    clock_for,
    CommandEvent,
    commands_path_for,
    read_command_records,
    records_from_events,
    verify_commands,
)
from lockwire.plan import Plan, Step, validate_plan
from lockwire_pro.preflight.source import PreflightSource
from lockwire_pro.preflight.stages import command_motion
from lockwire_pro.recording import Recording
from lockwire.report import Finding
from lockwire.schema import Contract, Joint

IMPLEMENTED = True

# The finding wording is the agreed wording: "this write would have been
# refused: joint j3 target 2.1 rad, limit 1.9". The prefix is load-bearing —
# it is what tells an operator lockwire did not stop anything.
REFUSAL_PREFIX = "this write would have been refused: "


def _num(value: float) -> str:
    """Render a commanded magnitude the way an operator wrote it (2.1, not 2.1000000000000001)."""
    return f"{value:g}"


def _joint_unit(joint: Joint) -> str:
    """URDF convention: prismatic joints are metres, everything else radians."""
    return "m" if joint.type == "prismatic" else "rad"


def _declared_channels(contract: Contract) -> set[tuple[str, str]]:
    """Exact observed source identities admitted by the authored command map."""
    return {
        (device.id, f"ros2:{device.driver.command_topic}")
        for device in contract.devices
        if device.driver is not None and device.driver.command_topic is not None
    }


def _read_record(contract: Contract, path: Path) -> tuple[list[Step], list[Finding]]:
    """Score only the intact chain prefix on an exact declared ROS channel."""
    steps: list[Step] = []
    problems: list[Finding] = []
    declared = _declared_channels(contract)
    broken = verify_commands(path)
    events: list[CommandEvent] = []
    row_number = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row_number += 1
        if broken is not None and row_number >= broken[0]:
            if row_number == broken[0]:
                problems.append(
                    _not_scored(
                        f"preflight/commands/record/line/{lineno}",
                        f"command record integrity failed: {broken[1]}; this and later rows were not scored",
                        "an intact command-record hash chain",
                        "repair the record; only preceding verified rows may be scored",
                    )
                )
            continue
        try:
            events.append(CommandEvent.model_validate(json.loads(line)))
        except (ValueError, json.JSONDecodeError):
            problems.append(_unusable_record(f"line/{lineno}", "command event is malformed"))
    for record in records_from_events(events):
        if not record.representable:
            problems.append(
                _unusable_record(
                    record.command_id,
                    "the command payload was unrepresentable when recorded "
                    f"({record.command_text!r})",
                )
            )
            continue
        if record.transport_issue:
            problems.append(
                _unusable_record(
                    record.command_id,
                    f"the command stream reported a transport loss ({record.transport_issue}); "
                    "it is not watched evidence",
                )
            )
        if record.command is None:
            continue
        if (record.device, record.source) not in declared:
            problems.append(_unbound_channel(record.command_id, record.device, record.source))
            continue
        try:
            steps.append(Plan.model_validate({"steps": [record.command]}).steps[0])
        except Exception as exc:  # pydantic ValidationError and friends
            problems.append(
                _unusable_record(
                    record.command_id, f"command is not a command this stage can score ({exc})"
                )
            )
    return steps, problems


def _unusable_record(command_id: str, why: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"preflight/commands/record/{command_id}",
        observed=why,
        expected="one logical command record carrying a scorable command",
        action=f"repair command record {command_id}; the other records were scored",
    )


def _unbound_channel(command_id: str, device: str, source: str | None) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=f"preflight/commands/channel/{command_id}",
        observed=(
            f"command record device={device!r}, source={source!r} is not a declared commanded channel"
        ),
        expected="a device Driver.command_topic matching the observed ros2 source exactly",
        action="declare the watched command topic on the device driver, or correct the recorder source",
    )


def _not_scored(quantity: str, observed: str, expected: str, action: str) -> Finding:
    return Finding(
        severity="not_scored",
        quantity=quantity,
        observed=observed,
        expected=expected,
        action=action,
    )


def _bag_disclosure() -> Finding:
    """Commands living inside the bag itself are out of reach — say so, never imply coverage."""
    return _not_scored(
        "preflight/commands/in_recording",
        "commands inside the recording itself are not scored: bag readers have no command "
        "decoder or mapping, so numeric samples cannot be told from a write",
        "a bag command decoder and mapping to the declared Driver.command_topic",
        "a GO here does not mean the bag's own commanded topics were decoded or checked",
    )


def _refusal(contract: Contract, step: Step, finding: Finding) -> str:
    """The agreed wording, with the joint case named exactly as he wrote it."""
    joints = {joint.name: joint for joint in contract.model.joints} if contract.model else {}
    joint = joints.get(getattr(step, "joint", ""))
    position = getattr(step, "position", None)
    if (
        joint is not None
        and position is not None
        and joint.limits is not None
        and finding.quantity.endswith("/joint_target/position")
    ):
        bounds = [b for b in (joint.limits.lower, joint.limits.upper) if b is not None]
        if bounds:
            # ponytail: name the declared bound nearest the target. validate_plan
            # already decided the verdict; this only picks which number to print,
            # so no comparison rule is duplicated. Revisit if a joint ever gets
            # more than two position bounds.
            limit = min(bounds, key=lambda bound: abs(position - bound))
            return (
                f"{REFUSAL_PREFIX}joint {joint.name} target {_num(position)} "
                f"{_joint_unit(joint)}, limit {_num(limit)}"
            )
    return f"{REFUSAL_PREFIX}{finding.observed}, {finding.expected}"


def _score(contract: Contract, recording: Recording) -> tuple[list[Finding], int]:
    """Findings and the count of determined comparisons. Shared with the runner's counter."""
    path = commands_path_for(recording.path)
    if not path.exists():
        return [
            _not_scored(
                "preflight/commands/record",
                f"no command record beside this recording ({path.name} not present)",
                "a commands.jsonl written by the watched write layer",
                "run the write layer alongside the capture to score commands",
            ),
            _bag_disclosure(),
        ], 0

    steps, problems = _read_record(contract, path)
    if not steps:
        return [
            *problems,
            _not_scored(
                "preflight/commands/record",
                f"{path.name} carries no scorable command",
                "at least one command row to score",
                "check the write layer actually recorded commands",
            ),
            _bag_disclosure(),
        ], 0

    # One Plan, one call: the whole record is handed to the existing evaluator
    # together so two commands each legal alone still meet at their shared rail
    # (validate_plan accumulates RailLoad per rail and rolls it up through
    # power_rollup — that walk is not repeated here).
    report = validate_plan(contract, Plan(steps=steps))

    findings: list[Finding] = list(problems)
    for finding in report:
        index = _step_index(finding.quantity)
        step = steps[index] if index is not None else None
        tail = finding.quantity.split("/", 3)[-1] if index is not None else finding.quantity
        text = (
            _refusal(contract, step, finding)
            if step is not None and finding.severity in {"violation", "warning"}
            else finding.observed
        )
        findings.append(
            finding.model_copy(
                update={"quantity": f"preflight/commands/{index}/{tail}", "observed": text}
            )
        )
    findings.append(_bag_disclosure())
    return findings, report.scored


def _step_index(quantity: str) -> int | None:
    """``plan/steps/3/joint_target/position`` -> 3."""
    parts = quantity.split("/")
    if len(parts) >= 3 and parts[0] == "plan" and parts[1] == "steps" and parts[2].isdigit():
        return int(parts[2])
    return None


def check(contract: Contract, source: PreflightSource) -> list[Finding]:
    """Score the recorded command stream through ``validate_plan``. Never a gate."""
    recording = source.samples()
    findings, _ = _score(contract, recording)
    return [*findings, *_motion_findings(contract, recording)]


def _motion_findings(contract: Contract, recording: Recording) -> list[Finding]:
    """Commands vs observed motion, per device with a declared, intact watched stream."""
    path = commands_path_for(recording.path)
    if not path.exists() or verify_commands(path) is not None:
        return []
    declared = _declared_channels(contract)
    # Command times and motion times may only be compared on one known
    # clock. No rebase exists (the record stores no offset, the manifest no start
    # epoch), so a record whose clock differs from the recording's is skipped.
    bag_clock = clock_for(recording.source)
    times: dict[str, list[float]] = {}
    clocks: dict[str, set[str]] = {}
    for record in read_command_records(path):
        if record.received_at is not None and (record.device, record.source) in declared:
            times.setdefault(record.device, []).append(record.received_at)
            clocks.setdefault(record.device, set()).add(record.clock)
    skipped = [d for d in times if clocks[d] != {bag_clock} or bag_clock == "unknown"]
    out = [
        _not_scored(
            f"preflight/commands/{d}/motion",
            "command times and recording times are on different clocks; not compared",
            "command times and recording times on the same known clock",
            "record commands and motion from one source so their times line up",
        )
        for d in skipped
    ]
    times = {d: t for d, t in times.items() if d not in skipped}
    return [*out, *command_motion.compare(contract, recording, times)]


def scored(contract: Contract, source: PreflightSource) -> int:
    """Determined comparisons, for the runner's per-stage scored count."""
    _, count = _score(contract, source.samples())
    return count


def has_watched_evidence(findings: list[Finding], count: int) -> bool:
    """An intact declared record must actually score, with no record/channel gaps.

    The separate in-recording decoder disclosure says nothing about the watched
    sidecar's integrity. Scoring an intact prefix is useful analysis, but cannot
    establish required evidence for a record whose remainder is missing.
    """
    return count > 0 and not any(
        f.severity == "not_scored"
        and f.quantity != "preflight/commands/in_recording"
        and not f.quantity.endswith("/motion")  # the motion comparison is not record integrity
        for f in findings
    )
