"""The lockwire GUI: one self-contained page per robot, written for the person holding it.

`report_html` renders two engineer-facing documents and is unchanged. This module
builds the page payload the desktop app and the sidecar serve: the same contract graph, the same
findings, the same numbers — said in words a robotics student can act on, with the
exact machine text one click away so nothing is softened out of existence.

No screen here predicts anything. Every sentence is a comparison between two
records that already exist.
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from lockwire import plugins
from lockwire.checks import _ScoreBook, check_scene_placement, run_record_checks
from lockwire.constraints import constraint_rows
from lockwire.describe import describe_all, engineer_detail, render_power_tree
from lockwire.edit import resolve_witness
from lockwire.report import (
    Finding,
    format_display,
    RecordReport,
    VerdictCode,
    limit_raised_mark,
    verdict,
    verdict_headline,
    _SEVERITY_RANK,
)
from lockwire.report_html import (
    _budget_rows,
    _graph,
    _redact_path,
    _redact_witness,
    document_identity,
)
from lockwire.scene import Scene
from lockwire.scene_view import _rail_consumers
from lockwire.scene_view import scene_payload as _scene_payload
from lockwire.copy import (
    check_label,
    fix_text,
    group_title,
    human_name,
    instead_row,
    robot_title,
    value_name,
)
from lockwire.schema import Contract, Provenance, Quantity

_KIND_PREFIXES = ("dev.", "rail.", "bus.", "bud.", "host.")

_TAGS = {
    "violation": "Stop",
    "warning": "Worth a look",
    "not_scored": "Couldn't check",
}


def short_name(identifier: str) -> str:
    """Drop the machine prefix from a contract id, keeping everything that names it."""
    for prefix in _KIND_PREFIXES:
        if identifier.startswith(prefix):
            return identifier[len(prefix) :]
    return identifier


def _names(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [short_name(part.strip()) for part in raw.split(",") if part.strip()]


def _join(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


_COLLECTIONS = frozenset(
    {
        "scene",
        "devices",
        "buses",
        "rails",
        "hosts",
        "budgets",
        "signals",
        "envelopes",
        "links",
        "joints",
        "cross_signal",
        "coverage",
        "stage",
    }
)


def _shape(finding: Finding) -> str:
    """A grouping key: the contract path with the members of a collection blanked out.

    ``devices/dev.imu/signals/accel_x/variance`` and its five siblings become
    ``devices/*/signals/*/variance``, so one blind spot repeated across six channels
    reads as one line instead of six near-identical ones. Only the member segment is
    blanked — the kind of check it was is what remains, and that is what the reader
    is being told.
    """
    parts = finding.quantity.split("/")
    shaped = []
    for index, part in enumerate(parts):
        member = "." in part or (index > 0 and parts[index - 1] in _COLLECTIONS)
        shaped.append("*" if member else part)
    return "/".join(shaped)


def _weak_servo(finding: Finding) -> bool:
    """A drives_joint finding from the torque check (servo too weak), not a bad joint reference."""
    return _shape(finding).endswith("drives_joint") and "geared torque" in finding.observed


def _nm(text: str) -> str:
    """ "10.00 N*m ..." -> "10 N·m"."""
    m = re.search(r"([\d.]+) N\*m", text)
    return f"{float(m[1]):g} N·m" if m else text


def finding_label(finding: Finding) -> str:
    """A plain 1-4 word name for the check a finding came from (copy.CHECK_LABELS)."""
    return "Servo too weak" if _weak_servo(finding) else check_label(finding.quantity)


def _instead(alternatives: Any) -> list[dict[str, str]]:
    return [
        {**instead_row(a.text, a.kind), "rail": a.rail, "device": a.device} for a in alternatives
    ]


def finding_labels(contract: Contract, findings: list[Finding]) -> list[dict[str, Any]]:
    """Per finding (same order): its label and typed targets, for Plan and Test rows."""
    out = []
    for f in findings:
        targets = _targets(contract, [f])
        label = finding_label(f)
        # One name for one fault: a rail over its capacity reads like Check's card title.
        budget = _budget(contract, f.quantity)
        rail = next((t for t in targets if t["kind"] == "rail"), None)
        if (
            f.severity == "violation"
            and rail
            and (
                (budget is not None and budget.kind == "rail_power")
                or f.quantity.endswith("/set_power_mode/draw")
            )
        ):
            label = f"{human_name(short_name(rail['id']))} rail overloaded"
        out.append(
            {
                "label": label,
                "targets": targets,
                "whole": _whole(contract, f),
                "instead": _instead(f.alternatives),
                "fix": fix_text(f.action),
            }
        )
    return out


def _subject(finding: Finding) -> str:
    """The thing a finding is about: the last id-looking segment of its path."""
    for part in reversed(finding.quantity.split("/")):
        if "." in part:
            return short_name(part)
    return finding.quantity.split("/")[-1]


def _budget(contract: Contract, quantity: str) -> Any | None:
    """The budget a ``budgets/<id>`` finding is about, so we can name its rail."""
    if not quantity.startswith("budgets/"):
        return None
    wanted = quantity.removeprefix("budgets/").split("/", 1)[0]
    for budget in contract.budgets:
        if budget.id == wanted:
            return budget
    return None


def _magnitude(finding: Finding, which: str) -> str:
    """The bare number, without the parenthetical notes the console line carries."""
    quantity = getattr(finding, f"{which}_quantity", None)
    if quantity is None:
        return getattr(finding, which)
    other = getattr(finding, f"{'observed' if which == 'expected' else 'expected'}_quantity", None)
    return format_display(quantity.value, quantity.unit, other.value if other else None)


def _upper_first(text: str) -> str:
    """Capitalise a machine fragment so it can open a sentence. `str.capitalize()`
    would lower-case the rest, which destroys ids and units like `V` or `NaN`."""
    return text[:1].upper() + text[1:] if text else text


def _sentence(contract: Contract, group: list[Finding]) -> tuple[str, str]:
    """Return (title, body) for one card, in words that name the physical thing."""
    first = group[0]
    shape = _shape(first)
    seen: list[str] = []
    for finding in group:
        name = _subject(finding)
        if name not in seen:
            seen.append(name)
    subjects = _join(seen)
    stopped = first.severity == "violation"
    if first.severity == "not_scored":
        # A blind spot is not a measurement, so it never gets a measurement's grammar.
        # `observed` is written as a machine fragment and starts lower-case; dropped
        # straight after a full stop it reads as a sentence that lost its opening.
        measured = (
            f"{len(group)} checks here could not run, on {subjects}. Nothing failed - "
            "but nothing was verified either. The exact reasons are below."
            if len(group) > 1
            else f"Nothing failed here - the check could not run. {_upper_first(first.observed)}."
        )
    else:
        measured = f"Measured {first.observed}. Needs {first.expected}."
        if first.delta:
            measured += f" Off by {first.delta}."

    if _weak_servo(first):
        return (
            f"{human_name(_subject(first)).removeprefix('Servo ')}: servo {_nm(first.observed)}, "
            f"joint limit {_nm(first.expected)}",
            f"The servo gives {'at most ' if 'peak (stall)' in first.observed else ''}{_nm(first.observed)}"
            f"{' even at stall' if 'peak (stall)' in first.observed else ' continuously'}, but the joint's effort limit is "
            f"{_nm(first.expected)}. It is paired with the joint correctly; it is too weak for it.",
        )

    if shape == "budgets/*":
        budget = _budget(contract, first.quantity)
        kind = budget.kind if budget is not None else None
        label = short_name(budget.rail) if budget is not None and budget.rail else _subject(first)
        if kind == "rail_power":
            # A contract can budget the same rail in more than one draw mode; two
            # cards titled identically read as a duplicate, so name the mode.
            mode = getattr(budget, "draw_mode", "nominal") if budget is not None else "nominal"
            in_mode = f" in the {mode} mode" if mode and mode != "nominal" else ""
            if stopped:
                loads = _join(_names(first.dependent))
                on_it = f" On it: {loads}." if loads else ""
                return (
                    f"{human_name(label)} rail overloaded{in_mode}",
                    f"It can give {_magnitude(first, 'expected')}, and what's plugged into it "
                    f"wants {_magnitude(first, 'observed')}{in_mode}.{on_it} Power this on and "
                    f"everything on that rail browns out together.",
                )
            return (f"{human_name(label)} rail load{in_mode}", measured)
        if kind == "mass_total" and first.severity != "not_scored":  # a gap is not a total
            # G2 audit 2 §5: this total is summed from the model's own YAML
            # declarations (URDF link masses + device masses) — no scale was
            # ever read. "Measured" implies a recording; say what it actually is.
            declared = f"Declared {first.observed} (sum of the model's links and devices)."
            declared += f" Needs {first.expected}."
            if first.delta:
                declared += f" Off by {first.delta}."
            return (
                "Robot over weight limit" if stopped else "Total weight",
                declared,
            )
        if kind == "endurance":
            return (
                "Runs shorter than asked" if stopped else "Run time estimate",
                measured,
            )
        return (
            f"{human_name(label)} over budget" if stopped else f"{human_name(label)} budget",
            measured,
        )

    if shape == "topology/buses/*/bandwidth":
        if stopped:
            return (
                f"{human_name(_subject(first))} bus overloaded",
                f"{measured} Messages will start arriving late or not at all.",
            )
        if first.observed == "no declared traffic on this bus":
            return (
                f"{len(group)} {'cable' if len(group) == 1 else 'cables'}: traffic not declared",
                f"{subjects} — a bitrate is written down, but nothing says what's "
                f"actually sent over them, so I can't tell you whether it fits. "
                f"That's a blind spot, not a fault.",
            )
        return (
            f"{len(group)} {'cable' if len(group) == 1 else 'cables'}: speed not declared",
            f"{subjects} — without a bitrate I can't tell you whether the messages "
            f"you're sending fit down them. That's a blind spot, not a fault.",
        )

    if shape == "check/scored":
        # `scored` is a coverage disclosure, not a part of the robot. Left to the
        # generic path it came out as "I couldn't check scored", which names an
        # internal quantity to a student as though it were something on the bench.
        if contract.model is None and not contract.devices:  # a blank "New robot"
            return (
                "Nothing could be compared yet",
                "Nothing to check yet. Add a part to start checking.",
            )
        return (
            "Nothing could be compared yet",
            "Every check here compares two numbers that were written down separately - "
            "what a part draws against what its rail can give, a joint's limit against "
            "its motor. This lockfile doesn't have a single pair like that yet, so "
            "nothing was actually checked, and a pass would not have meant anything.",
        )

    if shape == "model/structure_hash":
        return (
            "CAD file not linked",
            "No URDF file is recorded against this lockfile, so if someone moves a mount "
            "or swaps the frame, nothing here notices.",
        )

    if first.quantity == "model/links/massless_frames":  # _shape blanks the last segment
        return ("Frames without mass", measured)

    if shape == "model/links/*/mass":
        # G2 audit 2 §5: a URDF link with no `<inertial>` is usually a fixed
        # reference frame (a camera parent, an optical frame), not a physical
        # part — wording it as "N parts have no weight recorded" reads as N
        # real components left unweighed.
        thing = "frame declares" if len(group) == 1 else "frames declare"
        return (
            f"{len(group)} {'frame' if len(group) == 1 else 'frames'} missing mass",
            f"{subjects} — likely fixed/reference frames in the URDF rather than "
            "physical parts; missing from your weight total and your centre of gravity.",
        )
    if shape.endswith("/physical/mass") or shape.endswith("/mass"):
        thing = "part has" if len(group) == 1 else "parts have"
        return (
            f"{len(group)} {'part' if len(group) == 1 else 'parts'} missing mass",
            f"{subjects} — missing from your weight total and your centre of gravity.",
        )

    if shape.endswith("/identity") or "collision" in shape:
        return (f"{human_name(_subject(first))}: address clash", measured)

    if shape == "devices/*/placement":
        if first.severity == "not_scored":
            return (
                f"{len(group)} {'position' if len(group) == 1 else 'positions'} not declared",
                measured,
            )
        return (
            f"{human_name(_subject(first))} off position"
            if len(group) == 1
            else f"{len(group)} parts off position",
            measured,
        )

    if shape == "scene/*/unclaimed":
        # H1 (2026-09-06): the raw quantity ("unclaimed") used to leak straight
        # into the generic title below, once per scene-only id — collapse them
        # into one plain-word card naming every part.
        thing = "part" if len(group) == 1 else "parts"
        return (
            f"{len(group)} {thing} not declared",
            f"{_join([finding.quantity.split('/')[1] for finding in group])}: "
            "the model names parts the contract doesn't declare — declare them "
            "(lockwire add device …), or give the model extras.lockwire.id for "
            "the parts that are devices, or leave them as structure by removing the id",
        )

    who = human_name(_subject(first))
    if shape == "topology/rails/*/current_limit" and stopped:
        return (f"{who} over its current limit", measured)
    if first.stage is not None and stopped:
        return (f"{who}: recording differs from lockfile", measured)
    if stopped:
        return (f"{who} differs from lockfile", measured)
    if len(group) > 1:
        return (f"{len(group)} checks couldn't run", measured)
    return (f"Couldn't check {who}", measured)


def _raw_text(group: list[Finding], *, redact_paths: bool = False) -> str:
    """The exact machine wording, unchanged, for anyone who wants to see it."""
    blocks = []
    for finding in group:
        lines = [f"{finding.severity.upper()} {finding.quantity}"]
        lines.append(f"  observed   {finding.observed}")
        lines.append(f"  expected   {finding.expected}")
        owner = finding.owner
        source = finding.source
        if redact_paths:
            owner = _redact_witness(owner)
            source = _redact_path(source)
        for label, value in (
            ("delta", finding.delta),
            ("dependent", finding.dependent),
            ("owner", owner),
            ("source", source),
            ("action", finding.action),
        ):
            if value:
                lines.append(f"  {label:<10} {value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _faults(contract: Contract, group: list[Finding]) -> list[str]:
    """The ids a finding is actually *about* — the thing that is wrong.

    A budget's own id is never a graph node, so a rail-power card resolves to the
    rail it constrains — otherwise the headline finding highlights nothing.

    Dependents are deliberately excluded: a device drawing exactly its datasheet
    figure off an overloaded rail is the casualty, not the fault, and painting it
    in fault colour tells the reader something untrue. See ``_affected``.
    """
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in found:
            found.append(value)

    for finding in group:
        budget = _budget(contract, finding.quantity)
        if budget is not None:
            add(budget.rail)
        for part in finding.quantity.split("/"):
            if "." in part and not part.startswith("bud."):
                add(part)
    return found


def _whole(contract: Contract, finding: Finding) -> bool:
    """A robot-wide budget (mass, endurance): about no single part and no rail."""
    budget = _budget(contract, finding.quantity)
    return finding.quantity.startswith("budgets/") and not (budget is not None and budget.rail)


def _targets(contract: Contract, group: list[Finding]) -> list[dict[str, Any]]:
    """What a card is about, as typed ids (joint, link, device, rail), read from the finding's
    quantity path and dependent list, never from its text. A device also targets the joint
    it drives. Order is first mention; no duplicates."""
    model = contract.model
    joints = {j.name for j in model.joints} if model else set()
    links = {link.name for link in model.links} if model else set()
    devices = {d.id: d for d in contract.devices}
    rails = {r.id for r in contract.topology.rails} if contract.topology else set()
    found: list[dict[str, Any]] = []

    def add(kind: str, id_: str | None) -> None:
        if id_ and not any(t["kind"] == kind and t["id"] == id_ for t in found):
            # The scene parts it lights: a rail lights its own part (if drawn) and every device
            # on it; anything else is its own part. Ids are contract ids, as scene_view joins them.
            parts = [id_, *sorted(_rail_consumers(contract, id_))] if kind == "rail" else [id_]
            if kind == "joint":  # mesh robots key meshes by the moving link, not the joint
                parts += [j.child for j in (model.joints if model else []) if j.name == id_]
            found.append({"kind": kind, "id": id_, "label": human_name(id_), "parts": parts})

    def device(id_: str) -> None:
        add("device", id_)
        add("joint", devices[id_].drives_joint)

    for finding in group:
        budget = _budget(contract, finding.quantity)
        if budget is not None and budget.rail:
            add("rail", budget.rail)
        elif _whole(contract, finding):
            continue  # a whole-robot total (mass, endurance) is about no single part
        parts = finding.quantity.split("/")
        if len(parts) >= 3 and parts[0] == "model" and parts[1] == "joints" and parts[2] in joints:
            add("joint", parts[2])
        elif len(parts) >= 3 and parts[0] == "model" and parts[1] == "links" and parts[2] in links:
            add("link", parts[2])
        elif len(parts) >= 2 and parts[0] == "devices" and parts[1] in devices:
            device(parts[1])
        elif (
            len(parts) >= 3 and parts[0] == "topology" and parts[1] == "rails" and parts[2] in rails
        ):
            add("rail", parts[2])
        elif len(parts) >= 3 and parts[0] == "topology" and parts[1] == "buses":
            for d in devices.values():  # a bus finding is about the parts that talk on it
                if d.identity and d.identity.bus_ref == parts[2]:
                    device(d.id)
        else:  # preflight/... and other paths: first known id among the segments and dependents
            names = [*parts, *(n.strip() for n in (finding.dependent or "").split(","))]
            for known, kind in (
                (devices, "device"),
                (joints, "joint"),
                (links, "link"),
                (rails, "rail"),
            ):
                match = next((n for n in names if n in known), None)
                if match:
                    device(match) if kind == "device" else add(kind, match)
                    break
        for name in (finding.dependent or "").split(","):
            name = name.strip()
            if name in devices:
                device(name)
            elif name in rails:
                add("rail", name)
    return found


def step_targets(contract: Contract, plan_path: str | Path) -> list[list[dict[str, Any]]]:
    """Typed targets per plan step (index-aligned), through ``_targets`` so a rail lights its
    consumers exactly as a finding does. A step naming an id the contract lacks gets []."""
    from lockwire.plan import load_plan  # noqa: PLC0415

    def path(step: Any) -> str:
        return {
            "rail_load": f"topology/rails/{getattr(step, 'rail', '')}",
            "set_power_mode": f"devices/{getattr(step, 'device', '')}",
            "joint_target": f"model/joints/{getattr(step, 'joint', '')}",
        }.get(step.kind, "")

    return [
        _targets(
            contract,
            [Finding(severity="not_scored", quantity=q, observed="", expected="", action="")],
        )
        if (q := path(step))
        else []
        for step in load_plan(Path(plan_path)).steps
    ]


def _robot_model(contract: Contract) -> dict[str, list[dict[str, Any]]]:
    """The model's joints and links with world-frame positions (parent chain composed), so the
    robot view draws it with no kinematics and no mesh files. Link visuals are not recorded in
    the contract, so none are emitted (the renderer draws capsules between joint origins)."""
    from lockwire.scene_urdf import _IDENTITY_POSE, _apply_r, _compose  # noqa: PLC0415

    model = contract.model
    if model is None:
        return {"joints": [], "links": []}
    kids: dict[str, list[Any]] = {}
    for joint in model.joints:
        kids.setdefault(joint.parent, []).append(joint)
    children = {joint.child for joint in model.joints}
    link_pose = {link.name: _IDENTITY_POSE for link in model.links if link.name not in children}
    joint_rows: dict[str, dict[str, Any]] = {}
    stack = list(link_pose)
    while stack:
        name = stack.pop()
        for joint in kids.get(name, []):
            frame = _compose(link_pose[name], joint.origin.xyz, joint.origin.rpy)
            if joint.child in link_pose:
                continue  # a cycle or second parent: first one wins
            link_pose[joint.child] = frame
            axis = joint.axis or (1.0, 0.0, 0.0)
            limits = joint.limits
            joint_rows[joint.name] = {
                "name": joint.name,
                "type": joint.type,
                "parent": joint.parent,
                "child": joint.child,
                "parent_name": human_name(joint.parent),
                "child_name": human_name(joint.child),
                "position": list(frame[1]),
                "axis": list(_apply_r(frame[0], axis)),
                "lower": limits.lower if limits else None,
                "upper": limits.upper if limits else None,
                "effort": limits.effort if limits else None,
            }
            stack.append(joint.child)
    return {
        "joints": [joint_rows[j.name] for j in model.joints if j.name in joint_rows],
        "links": [
            {"name": link.name, "position": list(link_pose[link.name][1])}
            for link in model.links
            if link.name in link_pose
        ],
    }


def _affected(contract: Contract, group: list[Finding]) -> list[str]:
    """The ids dragged in by someone else's fault — casualties, not causes.

    These still light up when the card is selected (they are part of the story),
    but they are drawn differently, because the legend promises the reader that
    fault colour means *this is what is wrong*.
    """
    blamed = set(_faults(contract, group))
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in blamed and value not in found:
            found.append(value)

    for finding in group:
        for name in (finding.dependent or "").split(","):
            add(name.strip() or None)
        for member in finding.cluster_members:
            add(member)
    return found


def _hits(contract: Contract, group: list[Finding]) -> list[str]:
    """Everything a card points at: what is wrong, plus what it drags down with it."""
    return _faults(contract, group) + _affected(contract, group)


def _consumer_files(contract: Contract, group: list[Finding]) -> list[str] | None:
    """The repo file(s) a `DependencyEdge` names as depending on a finding's quantity
    . Display only: this is exactly `DependencyEdge.code`, keyed off the exact
     quantity path an edge declares — never a filename inferred from a device id or a
     naming convention. ``None`` when no edge names this quantity: not every
     finding comes from an edge, and that absence must render as absence, not a guess.
    """
    by_quantity = {edge.quantity: edge for edge in contract.edges}
    found: list[str] = []
    for finding in group:
        edge = by_quantity.get(finding.quantity)
        if edge is None:
            continue
        for path in edge.code:
            if path not in found:
                found.append(path)
    return found or None


def _grouped(findings: list[Finding]) -> list[list[Finding]]:
    """One group per card: identical blind spots collapse; violations never do."""
    ordered = sorted(
        findings,
        key=lambda finding: (_SEVERITY_RANK[finding.severity], finding.quantity),
    )
    groups: list[list[Finding]] = []
    index: dict[tuple[str, str], list[Finding]] = {}
    for finding in ordered:
        if finding.severity == "violation":
            groups.append([finding])
            continue
        shape = _shape(finding)
        # A bus's bandwidth can be not_scored two different, non-overlapping ways
        # (no bitrate at all vs. a bitrate with no declared traffic) — same shape,
        # different blind spot, so they must not collapse into one card (B67).
        key = (
            (finding.severity, shape, finding.observed)
            if finding.severity == "not_scored" and shape == "topology/buses/*/bandwidth"
            else (finding.severity, shape)
        )
        if key in index:
            index[key].append(finding)
            continue
        bucket = [finding]
        index[key] = bucket
        groups.append(bucket)
    return groups


def _finding_key(finding: Finding) -> tuple[str, str, str]:
    """Identity of a finding for cross-section dedup: same quantity, same
    observed-vs-expected pair. Two findings this equal on are the same fact,
    not two."""
    return (finding.quantity, finding.observed, finding.expected)


def _dedupe_run_findings(
    record_findings: list[Finding], run_findings: list[Finding]
) -> tuple[list[Finding], set[tuple[str, str, str]]]:
    """G2 audit 2 §8: a finding present in both the record checks and the run's
    cross-record stage (e.g. the mass budget, scored again at stage 4) is one
    fact, not two — it stays in the record section and drops out of the run
    section, so neither the headline count nor the run's own count double it.
    Returns the run findings with duplicates removed, and the keys that were
    echoed (so the surviving record card can say "also seen in the run").
    """
    record_keys = {_finding_key(finding) for finding in record_findings}
    kept: list[Finding] = []
    echoed: set[tuple[str, str, str]] = set()
    for finding in run_findings:
        key = _finding_key(finding)
        if key in record_keys:
            echoed.add(key)
            continue
        kept.append(finding)
    return kept, echoed


def _card(
    *,
    severity: str,
    title: str,
    body: str,
    fix: str,
    raw: str,
    hits: list[str],
    faults: list[str],
    affected: list[str],
    count: int,
    observed: dict[str, Any] | None,
    expected: dict[str, Any] | None,
    alternatives: list[dict[str, Any]],
    figure_note: str | None,
    shape: str,
    also_in_run: bool,
    label: str = "",
    group_title: str = "",
    instead: list[dict[str, str]] | None = None,
    consumer: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
    whole: bool = False,
    count_by: str = "gui._cards",
    quantity: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """The one constructor for every `api_payload.Card`-shaped dict `gui.py` emits.

    e3-cf3 (e3-cf2's flag): `_cards()` and `_empty_state_card()` used to build this
    shape by hand, independently — an asymmetric field addition between them is
    exactly what put `consumer` on one and not the other. Routing both through this
    function makes that impossible: a field added here reaches both producers by
    construction, not by someone remembering to touch the second call site.
    """
    return {
        "severity": severity,
        "tag": _TAGS[severity],
        "title": title,
        "body": body,
        "fix": fix,
        "raw": raw,
        "hits": hits,
        "faults": faults,
        "affected": affected,
        "targets": targets or [],
        "whole": whole,
        "consumer": consumer,
        "count": count,
        "count_figure": {
            "value": count,
            "unit": "findings",
            "kind": "derived",
            "by": count_by,
        },
        "observed": observed,
        "expected": expected,
        "alternatives": alternatives,
        "figure_note": figure_note,
        "shape": shape,
        "label": label,
        "group_title": group_title,
        "instead": instead or [],
        "also_in_run": also_in_run,
        "quantity": quantity,
        "owner": owner,
    }


def _cards(
    contract: Contract,
    findings: list[Finding],
    *,
    echoed: set[tuple[str, str, str]] = frozenset(),
    redact_paths: bool = False,
) -> list[dict[str, Any]]:
    """One card per problem. Identical blind spots collapse; violations never do."""
    cards = []
    for group in _grouped(findings):
        title, body = _sentence(contract, group)
        observed, expected, figure_note = _card_figures(group[0])
        cards.append(
            _card(
                severity=group[0].severity,
                title=title,
                body=body,
                fix=fix_text(group[0].action),
                raw=_raw_text(group, redact_paths=redact_paths),
                hits=_hits(contract, group),
                faults=_faults(contract, group),
                affected=_affected(contract, group),
                targets=_targets(contract, group),
                whole=_whole(contract, group[0]),
                consumer=_consumer_files(contract, group),
                count=len(group),
                observed=observed,
                expected=expected,
                alternatives=[
                    alternative.model_dump()
                    for finding in group
                    for alternative in finding.alternatives
                ],
                figure_note=figure_note,
                shape=_shape(group[0]),
                label=finding_label(group[0]),
                group_title=group_title(_shape(group[0]), weak=_weak_servo(group[0])),
                instead=_instead([a for f in group for a in f.alternatives]),
                also_in_run=any(_finding_key(finding) in echoed for finding in group),
                quantity=group[0].quantity,
                owner=group[0].owner,
            )
        )
    return cards


def _card_figures(
    finding: Finding,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Keep kinds only where this boundary can name the value's real producer.

    ``Finding`` carries typed quantities but no per-instance number-kind provenance. A
    column-wide label would lie for computed preflight bounds, so cards preserve
    the budget check's two known sources and disclose every other typed pair as
    unavailable until its producer supplies kinds of its own.
    """
    if finding.observed_quantity is None and finding.expected_quantity is None:
        return None, None, None
    if finding.stage is None and finding.quantity.startswith("budgets/"):
        if finding.quantity.endswith("/limit"):
            return (
                _figure(
                    finding.observed_quantity,
                    "declared",
                    "schema.Contract.budgets.limit",
                ),
                _figure(
                    finding.expected_quantity,
                    "declared",
                    "schema.Contract.topology.rails.capacity",
                ),
                "Both numbers are as declared in your contract: the budget's limit and the rail's capacity.",
            )
        return (
            _figure(finding.observed_quantity, "derived", "checks.check_budgets"),
            _figure(
                finding.expected_quantity,
                "declared",
                "schema.Contract.budgets.limit",
            ),
            "Observed is the check's sum of your declared draws; Expected is your declared limit.",
        )
    return None, None, "Numbers not shown: this check can't yet say where they came from."


def _figure(quantity: Quantity | None, kind: str, by: str) -> dict[str, Any] | None:
    """One `api_payload.Figure` as the plain dict `build_payload` deals in.

    A number on screen is typed here and formatted by
    the renderer. This door never hands the page a preformatted numeric string.
    """
    if quantity is None:
        return None
    return {"value": quantity.value, "unit": quantity.unit, "kind": kind, "by": by}


def _comparator(expected: str) -> str:
    """The bound `Finding.expected` opens with, if any — a symbol, not a number."""
    for symbol in ("<=", ">="):
        if expected.startswith(symbol):
            return symbol
    return ""


def _scene_labels(contract: Contract, findings: list[Finding]) -> list[dict[str, Any]]:
    """One pinned label per violation/warning card: which part, how bad, in plain figures.

    Pinned WITHOUT a click — these are the labels the scene draws by default.
    The two magnitudes are typed (`api_payload.Figure`), not the
    `"35.20 W vs <= 15.60 W"` string this emitted until 2026-09-08: a formatted
    number carries no number-kind and nothing notices. `scene_js` formats them.
    """
    labels: list[dict[str, Any]] = []
    for group in _grouped(findings):
        first = group[0]
        if first.severity not in ("violation", "warning"):
            continue
        faults = _faults(contract, group)
        if not faults:
            continue
        title, _body = _sentence(contract, group)
        labels.append(
            {
                "part": faults[0],
                "severity": first.severity,
                "title": title,
                "observed": _figure(first.observed_quantity, "derived", "checks.run_record_checks"),
                "comparator": _comparator(first.expected),
                "expected": _figure(first.expected_quantity, "declared", "schema.Contract"),
            }
        )
    return labels


def _setup_gaps(contract: Contract) -> list[dict[str, str]]:
    """What this lockfile still doesn't say — the first-run screen, told honestly.

    These are not findings. A brand-new lockfile is mostly empty by definition, and
    the GUI's job on day one is to say what to fill in next rather than to look green.
    """
    gaps: list[dict[str, str]] = []
    topology = contract.topology

    if contract.model is None or not contract.model.links:
        gaps.append(
            {
                "title": "No robot model yet",
                "why": "Nothing describes the frame, so weights and mounts can't be checked.",
                "snippet": "lockwire init path/to/robot.urdf",
            }
        )
    elif contract.model.zero_mass_links:
        count = len(contract.model.zero_mass_links)
        names = ", ".join(contract.model.zero_mass_links[:6])
        more = "" if count <= 6 else " (and more)"
        weigh = "part of the frame weighs" if count == 1 else "parts of the frame weigh"
        gaps.append(
            {
                "title": f"{count} {weigh} nothing",
                "why": f"{names}{more} came out of the URDF with zero mass, so your weight "
                "total is wrong by however much they really weigh.",
                "snippet": '# in your URDF, give each link a real <mass value="..."/>',
            }
        )

    if topology is None or not topology.rails:
        gaps.append(
            {
                "title": "No power rails declared",
                "why": "Until rails exist I can't tell you whether your parts overload them — "
                "the check that finds most real build defects.",
                "snippet": (
                    "topology:\n"
                    "  rails:\n"
                    "    - id: rail.5v\n"
                    "      voltage: {value: 5.0, unit: V}\n"
                    "      capacity: {value: 15.6, unit: W}\n"
                    "      upstream: battery\n"
                    "      efficiency: 0.9"
                ),
            }
        )

    if topology is None or not topology.buses:
        gaps.append(
            {
                "title": "No cables declared",
                "why": "Without buses I can't check bandwidth or spot two devices claiming "
                "the same address.",
                "snippet": (
                    "topology:\n"
                    "  buses:\n"
                    "    - id: bus.can.main\n"
                    "      kind: can\n"
                    "      host: flight-controller\n"
                    "      bitrate: 1000000"
                ),
            }
        )

    if not contract.devices:
        gaps.append(
            {
                "title": "No parts declared",
                "why": "The lockfile knows the frame but not what's bolted to it, so almost "
                "nothing can be checked yet.",
                "snippet": (
                    "devices:\n"
                    "  - id: dev.camera.mine\n"
                    "    power: {rail: rail.5v, draw: {value: 5.0, unit: W}}"
                ),
            }
        )

    if not contract.budgets:
        gaps.append(
            {
                "title": "No limits set",
                "why": "Budgets are the lines you don't want to cross — rail power, total "
                "weight, runtime. Without them there's nothing to fail against.",
                "snippet": (
                    "budgets:\n"
                    "  - id: bud.power.5v\n"
                    "    kind: rail_power\n"
                    "    rail: rail.5v\n"
                    "    limit: {value: 15.6, unit: W}"
                ),
            }
        )

    return gaps


_STATE_BY_VERDICT_CODE: dict[VerdictCode, str] = {
    "REFUSED": "stop",
    "NO_GO": "stop",
    "GO_WITH_NOTES": "warn",
    "NOT_SCORED": "warn",
    "GO": "go",
}


def _headline(
    code: VerdictCode, violations: int, warnings: int, unchecked: int, adds_up: int = 0
) -> tuple[str, str, str]:
    """The one line at the top: state, headline, and the qualifier under it.

    ``state`` comes from ``report.verdict()``'s decision (``code``), not a local
    recount of severities — the same decision `lockwire check` and the JSON/MCP
    surfaces already render. The headline sentence itself is
    ``report.verdict_headline`` (E2-hl): the same text `api.check`'s
    `CheckPayload.headline` now carries, read once and shared rather than
    reimplemented here a second time. The wording below is unchanged.
    """
    state = _STATE_BY_VERDICT_CODE[code]
    headline = verdict_headline(violations, warnings, unchecked, adds_up)
    adds_up_phrase = f"{adds_up} thing{' adds' if adds_up == 1 else 's add'} up" if adds_up else ""
    couldnt_check_phrase = (
        f"{unchecked} thing{'' if unchecked == 1 else 's'} I couldn't check" if unchecked else ""
    )
    tail = " · ".join(phrase for phrase in (adds_up_phrase, couldnt_check_phrase) if phrase)
    if violations:
        return (state, headline, tail)
    if adds_up == 0:
        # Presence floor (B110): a zero denominator is never a pass, and a
        # warning over nothing compared ("Nothing stops you") reads as one.
        # Amber, not green — the verdict code (decision.code) is unchanged;
        # only this display collapses to "nothing to show yet" (A2, usage
        # audit finding 4). NOT SCORED is amber, not grey ("neutral").
        return ("warn", headline, couldnt_check_phrase)
    if warnings:
        return (state, headline, tail)
    if unchecked:
        return (state, headline, f"{tail} - a pass does not cover those")
    return (state, headline, tail)


_NOTHING_DECLARED_NEXT_STEP = (
    "nothing declared yet — add what is on it: lockwire add device|rail|bus …, "
    "or declare from the model: lockwire scene import"
)


def _nothing_declared(contract: Contract) -> bool:
    """the empty-state face. True when the lockfile has no devices, rails,
    buses or budgets — the honest state of a lockfile straight out of `start`,
    which knows only the frame."""
    topology = contract.topology
    return (
        not contract.devices
        and not contract.budgets
        and (topology is None or (not topology.rails and not topology.buses))
    )


def _empty_state_card() -> dict[str, Any]:
    """The one card `_cards` cannot produce on its own: there is nothing to check
    yet, so there is no finding to name. Built through `_card()`, the same
    constructor `_cards()` uses, so it is the same shape by construction rather
    than by two hand-written dict literals staying in sync."""
    return _card(
        severity="not_scored",
        title="Nothing to check yet",
        body=_NOTHING_DECLARED_NEXT_STEP,
        fix=_NOTHING_DECLARED_NEXT_STEP,
        raw="",
        hits=[],
        faults=[],
        affected=[],
        count=0,
        observed=None,
        expected=None,
        alternatives=[],
        figure_note=None,
        shape="setup/nothing_declared",
        also_in_run=False,
        count_by="gui._empty_state_card",
    )


def _scored_name(quantity: str) -> str:
    """Plain name for a scored comparison: the id it's about, prefix stripped."""
    skip = {"topology", "budgets", "devices", "wires", "buses", "rails"}
    for part in quantity.split("/"):
        if part not in skip:
            return short_name(part)
    return short_name(quantity)


def _scored_title(contract: Contract, record: Any) -> str:
    """Plain-word row title for a passed comparison, in `_sentence()`'s voice: name
    the physical thing, what it measured against what it's allowed, then the margin.
    The exact quantity path and machine text stay behind the "Show the exact
    finding" toggle (``_adds_up_rows``' ``raw`` field) — this is only the title.
    """
    quantity = record.quantity
    budget = _budget(contract, quantity)
    if budget is not None and budget.kind == "rail_power":
        rail_name = short_name(budget.rail) if budget.rail else _scored_name(quantity)
        mode = f", in its {budget.draw_mode} mode" if budget.draw_mode != "nominal" else ""
        limit = record.expected.removeprefix("<= ")
        return f"{rail_name} rail{mode}: {record.observed} of {limit} — {record.margin}"
    if budget is not None and budget.kind == "mass_total":
        limit = record.expected.removeprefix("<= ")
        return f"Payload mass: {record.observed} of {limit} allowed — {record.margin}"
    if budget is not None and budget.kind == "endurance":
        required = record.expected.removeprefix(">= ")
        return f"Flight time on the declared pack: {record.observed}, {required} required"
    if quantity.endswith("/power/rail"):
        device_id = quantity.split("/")[1]
        return f"{short_name(device_id)} on the {record.observed} rail: {record.margin}"
    if quantity.startswith("devices/") and quantity.endswith("/placement"):
        device_id = quantity.split("/")[1]
        return f"{short_name(device_id)} sits where the contract says — {record.margin}"
    # ampacity / bandwidth / link budget / torque / thermal, and anything else
    # scored: the same "observed of limit — margin" shape, named by its subject.
    subject = quantity.split("/")[2] if quantity.startswith("model/joints/") else None
    subject = subject or _scored_name(quantity)
    limit = record.expected.removeprefix("<= ").removeprefix(">= ")
    return f"{subject}: {record.observed} of {limit} — {record.margin}"


def _scored_mark(contract: Contract, record: Any) -> str | None:
    """the standing "limit raised" mark on the budget this pass is about.

    A budget cleared by raising its limit passes, so it reaches the reader only
    here — as a row in "what adds up". Without the mark the row is a plain green
    pass and nothing on the page says the goalposts moved.
    """
    budget = _budget(contract, record.quantity)
    return limit_raised_mark(budget.provenance) if budget is not None else None


def _adds_up_rows(contract: Contract, records: list[Any]) -> list[dict[str, str | None]]:
    """One row per passed comparison: a plain-word title, the two compared values,
    and the margin. The quantity path and machine text stay behind ``raw``."""
    return [
        {
            "name": _scored_title(contract, record),
            "text": f"{record.observed} vs {record.expected}",
            "margin": record.margin,
            "mark": _scored_mark(contract, record),
            "raw": (
                f"{record.quantity}\n"
                f"  observed: {record.observed}\n"
                f"  expected: {record.expected}\n"
                f"  margin: {record.margin}"
            ),
        }
        for record in records
    ]


# ------------------------------------------------------------- the window shell
#
# Mode bar + live health strip + CLI line + export. `lockwire --help` lists 20
# commands (measured 2026-09-08: add, apply, baseline, baseline-check,
# baseline-review, check, declare, describe, health, hw, init, mcp, plan,
# preflight, report, sample, scan, scene, start); the parity test walks that list
# against `_COMMAND_MODES` below and fails the day a command has no mode.

_MODE_ORDER = ["design", "declare", "check", "plan", "reference"]
_MODE_LABELS = {
    "design": "Design",
    "declare": "Declare",
    "check": "Check",
    "plan": "Plan",
    "reference": "Reference",
}

_COMMAND_MODES = {
    "add": "design",
    "gearbox": "design",
    "from-parts": "design",  # drafts the part list a robot is built from
    "scene": "design",
    # "E5's Design mode places, moves, rotates, re-parents parts and edits
    # mounting geometry." These commands are that mode's write door.
    "place": "design",
    "mount-shape": "design",
    "move": "design",
    "rotate": "design",
    "reparent": "design",
    "declare": "declare",
    "apply": "declare",
    "review": "declare",  # approve/question a declared value, same mode group
    "check": "check",
    "what-if": "check",
    "hw": "check",
    "plan": "plan",
    "mcp": "plan",
    "scan": "reference",
    "start": "reference",
    "init": "reference",
    "upgrade": "reference",  # pinned legacy->0.4 migration, writes and retains, no mode face of its own
    "report": "reference",
    "describe": "reference",
    "serve": "reference",  # the sidecar launcher: infra, not a mode of its own
    # A detection/forwarding corpus report, not part of the contract-findings
    # page (eb-05 brief's paths table: "page: no") — reference like report/describe.
    "bench": "reference",
    "export": "reference",  # writes an artefact for someone else to consume
    "import": "reference",  # measured: refuses unconditionally, never reads a manifest
    # into a contract, so it doesn't contradict the outward-only rule and isn't a
    # declare-side round trip either — a refusal-only command, grouped with export
    "diff": "reference",  # a read-only comparison of two facts, no design/check/plan face
    "log": "reference",  # read-only contract history, served without a new app mode
    "blame": "reference",  # read-only declared-fact provenance, alongside contract history
    "api-schema": "reference",  # E1-b: the payload union's JSON schema
    "api-types": "reference",  # e1-d3: the app's TypeScript types, generated from the same union
}


def _design_face(contract: Contract) -> dict[str, Any]:
    """Render the Design face showing declared parts with their power info.

    The design mode governs; this function shows what's declared in the
    contract (devices, rails, power draw) as the base for editing."""
    parts = []
    for device in contract.devices:
        power = device.power
        rail = short_name(power.rail) if power is not None else "—"
        draw = _figure(power.draw if power is not None else None, "declared", "schema.Contract")
        parts.append({"id": short_name(device.id), "rail": rail, "draw": draw})
    return {
        "title": "What's declared",
        "parts": parts,
        "cli": "lockwire add device --model <model-id> --rail <rail-id> <contract>",
    }


def _declare_face(contract_path: Path | None) -> dict[str, Any]:
    path = str(contract_path) if contract_path is not None else "lockwire.yaml"
    return {
        "title": "Declare a fact",
        "lines": [
            f"lockwire declare {path} devices.dev.<id>.physical.mass 0.5 "
            f'--source "datasheet" --witness "you"',
            "lockwire apply changes.yaml",
            f"lockwire add device --model <model-id> --rail <rail-id> {path}",
            # a reviewer approves or questions a declared value, in the
            # same face — the notes land beside the fact they judge, not in a
            # section of their own.
            f"lockwire review {path} devices.dev.<id>.physical.mass "
            f'--approve --witness "your name"',
        ],
    }


_UNION_ORIGINS = (Union, types.UnionType)


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``Annotated``/``Optional`` wrappers down to the underlying type,
    e.g. ``Optional[Annotated[Quantity, AfterValidator(...)]]`` -> ``Quantity``.

    Handles both ``Optional[X]``/``Union[X, None]`` (origin ``typing.Union``)
    and the ``X | None`` syntax the schema mostly writes (origin
    ``types.UnionType``) — pydantic does not normalise one to the other in
    ``FieldInfo.annotation``, and checking only ``typing.Union`` silently
    stopped the walk at every ``SomeModel | None`` field (measured: it never
    reached a single device's ``physical``/``power``/``thermal`` block).
    """
    ann = annotation
    while True:
        if getattr(ann, "__metadata__", None) is not None:  # Annotated[X, ...]
            ann = get_args(ann)[0]
            continue
        if get_origin(ann) in _UNION_ORIGINS:
            args = [a for a in get_args(ann) if a is not type(None)]
            if len(args) == 1:
                ann = args[0]
                continue
        break
    return ann


def _declare_rows(
    model_cls: type[BaseModel],
    obj: BaseModel | None,
    path: str,
    label_parts: tuple[str, ...],
    provenance: Provenance | None,
    group: str | None = None,
    short_parts: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Every ``schema.Quantity`` reachable from ``model_cls``, present or not
    (E4-df). Walked from the model's own fields — same recursion `declare`'s
    own path navigation uses (dict-shaped blocks by field name, id/name-keyed
    lists by entry) — so a path this emits is a path `declare` accepts, and
    nothing here re-derives a value the contract does not already hold.

    ``mount_xyz``-shaped fields (``tuple[Quantity, Quantity, Quantity]``) are
    ONE declarable quantity, not three — `declare` writes the whole tuple in
    one call (`edit.parse_value`'s ``mount_xyz`` special case), so splitting
    it into per-axis rows would offer a path `declare` cannot accept.

    Scoped to `schema.Quantity`-typed fields only: a bare unitless number
    (``efficiency``, ``gear_ratio``, ``capacity_factor``) is not a "quantity"
    in the sense and is out of this box's charter.
    """
    rows: list[dict[str, Any]] = []
    if "provenance" in model_cls.model_fields:
        provenance = getattr(obj, "provenance", None) if obj is not None else None

    def names(name: str) -> dict[str, str]:
        # The short pair the list shows ("Base" / "mass"); `label` stays the full one.
        if group is not None:
            owner, rest = group, (*short_parts, name)  # `group` arrives already human
        elif label_parts:
            owner, rest = human_name(label_parts[0]), (*label_parts[1:], name)
        else:
            owner, rest = human_name(name), ()
        return {"group": owner, "short": value_name(rest) if rest else owner}

    for name, field in model_cls.model_fields.items():
        if name == "provenance":
            continue
        base = _unwrap_optional(field.annotation)
        value = getattr(obj, name, None) if obj is not None else None
        child_path = name if not path else f"{path}/{name}"
        origin = get_origin(base)
        if isinstance(base, type) and issubclass(base, Quantity):
            rows.append(
                {
                    "path": child_path,
                    "label": _humanize_path((*label_parts, name)),
                    **names(name),
                    "unit": value.unit if isinstance(value, Quantity) else None,
                    "kind": "declared",
                    "value": value.value if isinstance(value, Quantity) else None,
                    "provenance": provenance,
                }
            )
        elif origin is tuple:
            inner = [_unwrap_optional(a) for a in get_args(base)]
            if inner and all(isinstance(t, type) and issubclass(t, Quantity) for t in inner):
                rows.append(
                    {
                        "path": child_path,
                        "label": _humanize_path((*label_parts, name)),
                        **names(name),
                        "unit": value[0].unit if value else None,
                        "kind": "declared",
                        "value": [q.value for q in value] if value else None,
                        "provenance": provenance,
                    }
                )
        elif origin is list:
            args = get_args(base)
            item_type = args[0] if args else None
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                for item in value or []:
                    ident = getattr(item, "id", None) or getattr(item, "name", None)
                    if ident is None:
                        continue  # not id/name-addressable -> `declare` cannot reach it either
                    rows.extend(
                        _declare_rows(
                            item_type,
                            item,
                            f"{child_path}/{ident}",
                            (*label_parts, str(ident)),
                            provenance,
                            f"{group} · {human_name(str(ident))}"
                            if group
                            else human_name(str(ident)),
                            (),
                        )
                    )
        elif isinstance(base, type) and issubclass(base, BaseModel):
            rows.extend(
                _declare_rows(
                    base,
                    value,
                    child_path,
                    (*label_parts, name),
                    provenance,
                    group,
                    (*short_parts, name) if group is not None else (),
                )
            )
    return rows


def _humanize_path(parts: tuple[str, ...]) -> str:
    return " ".join(part.replace("_", " ") for part in parts)


def _add_part_options(contract: Contract) -> dict[str, Any]:
    """The Add part picker: powered catalog parts (those with a draw) and the rails each fits."""
    from lockwire.advice import input_range, rails_for_voltage  # noqa: PLC0415
    from lockwire.catalog import load_catalog  # noqa: PLC0415

    parts = []
    for part in load_catalog().parts:
        if "draw_max" not in part.numbers:
            continue
        value = {k: n.value for k, n in part.numbers.items()}
        band = input_range(
            value.get("input_voltage_min"),
            value.get("input_voltage_max"),
            value.get("nominal_voltage"),
        )
        parts.append(
            {
                "id": part.id,
                "name": part.name,
                "numbers": [{"key": k, **n.model_dump()} for k, n in part.numbers.items()],
                "fits": [r.id for r in rails_for_voltage(contract, *band)] if band else [],
            }
        )
    return {
        "rails": [r.id for r in (contract.topology.rails if contract.topology else [])],
        "parts": parts,
    }


def build_declare_form(contract: Contract) -> dict[str, Any]:
    """The Declare form's payload (E4-df): every declarable quantity in
    `contract`, plus a pre-filled witness box. this box's face (E4-b/c/d)
    renders and computes nothing, so every word and number it shows must
    already be here as a string or a kinded figure."""
    return {
        "rows": _declare_rows(Contract, contract, "", (), None),
        "witness": {"prefill": resolve_witness(None)},
        "add_part": _add_part_options(contract),
    }


def _plan_face(
    contract_path: Path | None,
    plan_path: Path | None,
    outcomes: list[Any] | None,
) -> dict[str, Any]:
    cpath = str(contract_path) if contract_path is not None else "lockwire.yaml"
    face: dict[str, Any] = {
        "title": "Check a plan before it runs",
        "cli": f"lockwire plan {cpath} plan.yaml",
        "outcomes": None,
    }
    if plan_path is not None and outcomes is not None:
        face["cli"] = f"lockwire plan {cpath} {plan_path}"
        face["outcomes"] = [{"path": o.path, "status": o.status} for o in outcomes]
    return face


def _reference_face(contract: Contract, contract_path: Path | None) -> dict[str, Any]:
    """The Reference face: four drawers over what the CLI already produces —
     describe, report, scan — plus the per-device reference sheets
    . Each drawer's ``cli_line`` uses the command's real flags
     (``report``'s printed line is ``-o/--out`` + ``--no-open``, never
     ``--html`` — measured, that flag does not exist)."""
    cpath = str(contract_path) if contract_path is not None else "lockwire.yaml"
    files = describe_all(contract, human=True)
    devices = [
        {"id": name.removesuffix(".md"), "name": human_name(name.removesuffix(".md")), "text": text}
        for name, text in files.items()
        if name != "SYSTEM.md"
    ]
    drawers = [
        {
            "title": "Reference sheets",
            "content": files["SYSTEM.md"],
            "provenance": "generated from the contract",
            "cli_line": f"lockwire describe {cpath}",
        },
        {
            "title": "Engineer report",
            "content": "A kept, versioned, print-ready document: an identity line and "
            "provenance on every page — what a student hands a supervisor.\n\n"
            + engineer_detail(contract),
            "provenance": "generated from the contract, deterministic",
            "cli_line": f"lockwire report {cpath} -o report.html --no-open",
        },
        {
            "title": "Hardware scan",
            "content": "Enumerates this machine's own hardware into a draft — never "
            "authoritative, always reviewed before it is locked.",
            "provenance": "from this machine, unreviewed",
            "cli_line": "lockwire scan --robot <robot-name>",
        },
    ]
    tree = render_power_tree(contract, human=True)
    return {
        "title": "Reference",
        "devices": devices,
        "drawers": drawers,
        "power_tree": "\n".join(tree) if tree else None,
    }


def _faces(
    contract: Contract,
    *,
    contract_path: Path | None,
    plan_path: Path | None,
    plan_outcomes: list[Any] | None,
) -> dict[str, Any]:
    return {
        "design": _design_face(contract),
        "declare": _declare_face(contract_path),
        "plan": _plan_face(contract_path, plan_path, plan_outcomes),
        "reference": _reference_face(contract, contract_path),
    }


def build_payload(
    contract: Contract,
    *,
    preflight: tuple[list[Finding], str] | None = None,
    scene: Scene | None = None,
    scene_source: str | None = None,
    contract_path: Path | None = None,
    plan_path: Path | None = None,
    plan_outcomes: list[Any] | None = None,
    cli_line: str | None = None,
    redact_paths: bool = False,
) -> dict[str, Any]:
    """Everything the page draws, in one deterministic dict."""
    record = run_record_checks(contract, contract_path=contract_path)
    record_findings = list(record)
    scored_records = list(record.scored_records) if isinstance(record, RecordReport) else []

    placement_scores: _ScoreBook | None = None
    if scene is not None:
        placement_scores = _ScoreBook()
        record_findings += check_scene_placement(contract, scene, scores=placement_scores)
        # A placement pass ("the model agrees with the declared mount point") is
        # a thing that adds up exactly like any other passed comparison — it must
        # land in scored/adds_up/the drawer, not just colour a part green.
        scored_records += placement_scores.records

    run_findings_raw = list(preflight[0]) if preflight is not None else []
    run_findings, echoed = _dedupe_run_findings(record_findings, run_findings_raw)
    everything = record_findings + run_findings

    decision = verdict(everything)
    violations = decision.violations
    warnings = decision.warnings
    unchecked = decision.not_scored
    scored = record.scored if isinstance(record, RecordReport) else None
    if placement_scores is not None:
        scored = (scored or 0) + len(placement_scores)
    adds_up = len(scored_records)
    state, headline, qualifier = _headline(decision.code, violations, warnings, unchecked, adds_up)
    cards = _cards(contract, record_findings, echoed=echoed, redact_paths=redact_paths)
    if not cards and _nothing_declared(contract):
        # a lockfile fresh out of `start` has nothing to compare, so the
        # generic per-severity grammar above produces no card at all — the page
        # then falls back to "Nothing disagrees", which reads as a pass over a
        # page that checked nothing. Say what to do next instead.
        qualifier = _NOTHING_DECLARED_NEXT_STEP
        cards = [_empty_state_card()]

    payload: dict[str, Any] = {
        "robot": robot_title(contract.robot.name),
        "contract_id": contract.robot.id or "contract",
        "state": state,
        "headline": headline,
        "qualifier": qualifier,
        "scored": scored,
        "violations": violations,
        "warnings": warnings,
        "unchecked": unchecked,
        "adds_up": _adds_up_rows(contract, scored_records),
        "cards": cards,
        **_robot_model(contract),
        "graph": _graph(contract),
        "budgets": _budget_rows(contract, record_findings, redact_paths=redact_paths),
        "setup": _setup_gaps(contract),
        "scene": None,
        "modes": _MODE_ORDER,
        "mode_labels": _MODE_LABELS,
        "command_modes": _COMMAND_MODES,
        "faces": _faces(
            contract,
            contract_path=contract_path,
            plan_path=plan_path,
            plan_outcomes=plan_outcomes,
        ),
        "cli_line": cli_line,
        "document_identity": document_identity(contract_path),
    }
    plugins.ensure()
    for extra in plugins.PAGE_EXTRAS:
        payload.update(
            extra(
                contract=contract,
                contract_path=contract_path,
                preflight=preflight,
                run_findings=run_findings,
                redact_paths=redact_paths,
                payload=payload,
            )
        )
    if scene is not None:
        # scored_records already carries the placement passes (merged above).
        payload["scene"] = _scene_payload(
            contract,
            scene,
            source=scene_source or scene.source,
            contract_path=contract_path,
            cards=cards,
            scored_records=scored_records,
            labels=_scene_labels(contract, record_findings),
        )
    payload["constraints"] = constraint_rows(
        contract,
        payload["budgets"],
        health_devices=payload.get("health_devices"),
        scene_part_ids={p["id"] for p in payload["scene"]["parts"] if p["id"]}
        if payload["scene"] is not None
        else None,
    )
    return payload
