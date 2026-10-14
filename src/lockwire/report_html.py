"""Deterministic, self-contained HTML views for lockwire contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path, PureWindowsPath
from typing import Any

from lockwire import __version__, gitref
from lockwire.checks import power_rollup, run_record_checks, unknown_draw_message
from lockwire.report import (
    CGReport,
    Finding,
    GRAPH_LAYOUT_JS,
    RecordReport,
    _SEVERITY_RANK,
    _cg_coverage,
    _cluster_members,
    _cluster_root_finding,
    _presentation_groups,
    limit_raised_mark,
    verdict,
)
from lockwire.schema import Contract
from lockwire.units import convert


def _redact_witness(value: str | None) -> str | None:
    """A device owner or a declare/lock witness — a person's or supplier's
    name. Dropped outright, not shortened: there is no partial form of a name
    that stays useful and stops identifying someone (--redact-paths)."""
    return None if value else value


def _redact_path(value: str | None) -> str | None:
    """Shorten an absolute filesystem path to its basename; anything else (a
    URL, a prose citation, a relative path) is returned unchanged
    (--redact-paths)."""
    if value and Path(value).is_absolute():
        return Path(value).name
    return value


# --redact-paths' per-field calls above (source/owner/witness) only
# catch the fields their author thought of; a five-minute adversarial pass
# found two more (cli_line, and the free-text observed/expected/action
# fields) sitting one hop away. Rather than grow that allowlist field by
# field forever, this sweeps every string in the ASSEMBLED payload dict —
# the single point where the report becomes JSON — for an embedded absolute
# path, so a field added to the payload later is covered by construction
# instead of by someone remembering to redact it. A whole-string match (the
# per-field helpers above) can't do this: a "source" field IS a path, but
# "observed"/"action" are prose with a path embedded in a sentence.
# POSIX absolute (/...), Windows drive-letter (C:\...), and UNC (\\server\...)
# forms. The POSIX-only pattern passed on a Mac and leaked a
# username through an unmatched `C:\Users\alice\...` — the same lesson
# this code illustrates (a matcher only covers the shapes its author pictured)
# recurring on its own author. Checked by feeding each shape through this
# function, not by inspection (an inspected-only ceiling claimed
# `\\?\` uncovered when it wasn't, which is worse than no claim at all on a
# security surface). Still NOT covered, confirmed leaking: a drive-relative
# Windows path with no drive letter (`\Users\alice\...`), and a path with
# mixed `/`/`\` separators (e.g. `C:/Users\alice\...`, copy-pasted from a
# mixed-shell log). A `\\?\` extended-length prefix IS redacted, but only
# INCIDENTALLY — the broad UNC alternative (`\\\\...`) happens to swallow it
# whole and `PureWindowsPath` still resolves its basename; nothing here
# targets that shape deliberately, so a future narrowing of the UNC pattern
# (to be less broad, say) could silently drop this coverage and no test
# would notice.
_ABS_PATH_IN_TEXT_RE = re.compile(r"/[^\s\"'<>]+" r"|[A-Za-z]:\\[^\s\"'<>]+" r"|\\\\[^\s\"'<>]+")
_ABS_PATH_TRAILING_PUNCTUATION = ".,;:!?)]}"


def _redact_paths_in_text(value: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        token = match.group(0)
        trailing = ""
        while token and token[-1] in _ABS_PATH_TRAILING_PUNCTUATION:
            trailing = token[-1] + trailing
            token = token[:-1]
        if not token:
            return match.group(0)
        if "\\" in token:
            # Windows drive-letter or UNC form. `Path` on this (POSIX) host
            # never treats `\` as a separator, so a plain `Path(token).name`
            # would return the whole token unshortened, silently failing to
            # redact — use `PureWindowsPath` explicitly regardless of host OS.
            wpath = PureWindowsPath(token)
            if not wpath.is_absolute():
                return match.group(0)
            return wpath.name + trailing
        path = Path(token)
        if not path.is_absolute():
            return match.group(0)
        return path.name + trailing

    return _ABS_PATH_IN_TEXT_RE.sub(_sub, value)


def _redact_paths_in_payload(value: Any) -> Any:
    """Recurse through the payload's dicts/lists and scrub every string.

    Applied once, at the end of :func:`_payload`, so it covers the whole
    document — not the fields ``--redact-paths`` currently names.

    Ceiling: this over-redacts. Any string that merely *looks*
    like an absolute path is rewritten, including a deliberately-named id
    such as ``/foo/bar`` that carries no personal information at all. That
    trade is accepted on purpose — a --redact-paths report is handed to
    someone outside, and a stray shortened id is a cosmetic cost while a
    surviving real path is a leak — but if a real id shape starts colliding
    with this in practice, the fix is to scope the sweep back to known
    path-shaped fields (or a stricter path regex) rather than widen the
    allowlist forever.

    No recursion depth bound: a contract-derived payload (robot → devices →
    findings, a few levels deep, no cycles) is nowhere near stack limits, so
    this is fine for the shapes this function actually receives; it is not
    guarded against a self-referential structure.
    """
    if isinstance(value, str):
        return _redact_paths_in_text(value)
    if isinstance(value, dict):
        return {key: _redact_paths_in_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_paths_in_payload(item) for item in value]
    return value


def _budget_rows(
    contract: Contract, findings: list[Finding], *, redact_paths: bool = False
) -> list[dict[str, Any]]:
    failed = {
        finding.quantity.removeprefix("budgets/").split("/", 1)[0]
        for finding in findings
        if finding.severity == "violation" and finding.quantity.startswith("budgets/")
    }
    rows: list[dict[str, Any]] = []
    for budget in contract.budgets:
        value: float | None
        unit: str
        direction = "max"
        note: str | None = None
        # Each budget selects its own draw mode (default "nominal"); roll power
        # through the same draw-mode-aware gate function the check uses, not a
        # nominal-only copy, or a named-mode budget shows the wrong rail total.
        (
            rail_totals,
            rail_witnesses,
            battery_w,
            _battery_witnesses,
            rail_unknown,
            battery_unknown,
        ) = power_rollup(contract, budget.draw_mode)
        if budget.kind == "rail_power":
            value, unit = rail_totals.get(budget.rail or "", 0.0), "W"
            # A rail with zero contributing witnesses was never measured — 0 W is
            # a vacuous "pass", not a healthy reading.
            if not rail_witnesses.get(budget.rail or "", set()):
                value = None
                note = "no draw declared on this rail"
            unknown_consumers = rail_unknown.get(budget.rail or "", set())
            if unknown_consumers:
                # any consumer of unknown draw makes this rail unsafe to sum.
                value = None
                note = unknown_draw_message(
                    sorted(unknown_consumers)[0],
                    contract.device(sorted(unknown_consumers)[0]).power.rail,
                )
        elif budget.kind == "mass_total":
            value, unit = 0.0, "kg"
            if budget.sources in ("devices", "links+devices"):
                value += sum(
                    device.physical.mass.base()
                    for device in contract.devices
                    if device.physical is not None and device.physical.mass is not None
                )
            if budget.sources in ("links", "links+devices") and contract.model is not None:
                value += sum(
                    link.mass.base() for link in contract.model.links if link.mass is not None
                )
        else:
            unit, direction = "min", "min"
            battery = contract.topology.battery if contract.topology is not None else None
            value = battery.capacity.base() / battery_w * 60.0 if battery and battery_w else None
            # Zero declared draw with a battery present is unmeasured, not a
            # missing-battery violation — same vacuous-pass shape as the empty
            # rail budget above. A missing battery
            # is its own violation and stays a "breach" row, not this note.
            if battery is not None and not battery_w:
                note = "no draw declared"
            if battery is not None and battery_unknown:
                # an unknown consumer reaching the battery makes endurance
                # unsafe to compute — never sum it as 0 W.
                value = None
                device_id = sorted(battery_unknown)[0]
                note = unknown_draw_message(device_id, contract.device(device_id).power.rail)
        limit = convert(budget.limit.value, budget.limit.unit, unit)
        if note is not None:
            state = "not_scored"
        elif budget.id in failed:
            state = "breach"
        else:
            state = "pass"
        rows.append(
            {
                "id": budget.id,
                "kind": budget.kind,
                "value": value,
                "limit": limit,
                "unit": unit,
                "direction": direction,
                "state": state,
                "note": note,
                # A limit without its provenance is an assertion, not a locked
                # constraint. The same three fields the findings already
                # carry, read off the same `budget.provenance` the checks read.
                "source": budget.provenance.source if budget.provenance is not None else None,
                "witness": budget.provenance.witness if budget.provenance is not None else None,
                "locked_at": (
                    budget.provenance.locked_at.isoformat()
                    if budget.provenance is not None and budget.provenance.locked_at is not None
                    else None
                ),
                # the standing mark when this limit was raised to clear a
                # violation. Rendered, never scored — no verdict or exit code moves.
                # rendered regardless of `state` — the mark must survive a
                # later re-breach (see report.limit_raised_mark's docstring).
                "limit_raised": limit_raised_mark(budget.provenance),
            }
        )
        if redact_paths:
            last = rows[-1]
            last["source"] = _redact_path(last["source"])
            last["witness"] = _redact_witness(last["witness"])
    return rows


def _cg_row(report: CGReport) -> dict[str, Any]:
    if report.cg_m is None:
        result = "not computable"
    else:
        vector = "(" + ", ".join(f"{value:.3f}" for value in report.cg_m) + ") m"
        result = vector if report.complete else f"partial {vector}"
    return {
        "quantity": report.quantity,
        "result": result,
        "frame": report.frame or "unresolved root frame",
        "sources": report.sources,
        # Coverage against the DECLARED total, not against the model itself — the
        # same signal the terminal prints. See report._cg_coverage.
        "mass": _cg_coverage(report),
        "limitation": report.limitation,
    }


#: The only place a lens's title/hint is authored. The GUI and
#: report JS read `graph[lens].label`/`.caption` off the payload; neither
#: composes a lens word itself.
_LENS_META: dict[str, dict[str, str]] = {
    "off": {"label": "off", "caption": "Drag to orbit, scroll to zoom"},
    "mechanical": {
        "label": "Mechanical",
        "caption": "What's bolted to what, and how heavy it is",
    },
    "power": {
        "label": "Power",
        "caption": "What feeds what, and how many watts down each wire",
    },
    "data": {
        "label": "Data",
        "caption": "Which computer talks to which part, over which cable",
    },
    "health": {
        "label": "Health",
        "caption": "Which parts the health ledger logs healthy, degrading, or stale",
    },
}


def _graph(contract: Contract) -> dict[str, dict[str, Any]]:
    topology = contract.topology
    links = contract.model.links if contract.model is not None else []
    joints = contract.model.joints if contract.model is not None else []

    mechanical_nodes = [
        {
            "id": link.name,
            "label": link.name,
            "sub": f"{link.mass.base():.3f} kg" if link.mass is not None else "mass unknown",
            "kind": "link",
        }
        for link in links
    ]
    mechanical_nodes += [
        {
            "id": device.id,
            "label": device.id,
            "sub": (
                f"{device.physical.mass.base():.3f} kg"
                if device.physical is not None and device.physical.mass is not None
                else "mass unknown"
            ),
            "kind": "device",
        }
        for device in contract.devices
    ]
    mechanical_edges = [
        {"from": joint.parent, "to": joint.child, "label": joint.name} for joint in joints
    ]
    mechanical_edges += [
        {"from": device.physical.mount_frame, "to": device.id, "label": "mount"}
        for device in contract.devices
        if device.physical is not None and device.physical.mount_frame is not None
    ]

    rails = topology.rails if topology is not None else []

    def _rail_sub(rail: object) -> str:
        sub = (
            f"{rail.voltage.base():g} V · cap {rail.capacity.base():.2f} W"
            if rail.voltage is not None
            else f"voltage unknown · cap {rail.capacity.base():.2f} W"
        )
        if rail.voltage_range is not None:
            sub += f" · range {rail.voltage_range.min.base():g}-{rail.voltage_range.max.base():g} V"
        if rail.current_limit is not None:
            sub += f" · limit {rail.current_limit.base():g} A"
        return sub

    power_nodes = [
        {
            "id": rail.id,
            "label": rail.id,
            "sub": _rail_sub(rail),
            "kind": "rail",
        }
        for rail in rails
    ]
    if topology is not None and topology.battery is not None:
        power_nodes.insert(
            0,
            {
                "id": "battery",
                "label": "battery",
                "sub": f"{topology.battery.capacity.base():.2f} Wh",
                "kind": "source",
            },
        )
    power_nodes += [
        {
            "id": device.id,
            "label": device.id,
            "sub": (
                f"{device.power.draw.base():.2f} W"
                if device.power.draw is not None
                else "draw unknown"
            ),
            "kind": "device",
        }
        for device in contract.devices
        if device.power is not None
    ]
    power_edges = [
        {"from": rail.upstream, "to": rail.id, "label": f"η {rail.efficiency:.2f}"}
        for rail in rails
        if rail.upstream is not None
    ]
    power_edges += [
        {
            "from": device.power.rail,
            "to": device.id,
            "label": (
                f"{device.power.draw.base():.2f} W"
                if device.power.draw is not None
                else "draw unknown"
            ),
        }
        for device in contract.devices
        if device.power is not None
    ]

    hosts = topology.hosts if topology is not None else []
    buses = topology.buses if topology is not None else []
    data_nodes = [
        {"id": host.id, "label": host.id, "sub": host.notes or "host", "kind": "host"}
        for host in hosts
    ]
    data_nodes += [
        {
            "id": bus.id,
            "label": bus.id,
            "sub": f"{bus.kind} · {bus.bitrate:g} bit/s" if bus.bitrate else bus.kind,
            "kind": "bus",
        }
        for bus in buses
    ]
    data_nodes += [
        {
            "id": device.id,
            "label": device.id,
            "sub": (
                device.driver.topic
                if device.driver is not None and device.driver.topic is not None
                else "device"
            ),
            "kind": "device",
        }
        for device in contract.devices
        if device.identity is not None
    ]
    data_edges = [{"from": bus.host, "to": bus.id, "label": bus.kind} for bus in buses]
    data_edges += [
        {"from": device.identity.bus_ref, "to": device.id, "label": "identity"}
        for device in contract.devices
        if device.identity is not None
    ]
    return {
        "off": {"nodes": [], "edges": [], **_LENS_META["off"]},
        "mechanical": {
            "nodes": mechanical_nodes,
            "edges": mechanical_edges,
            **_LENS_META["mechanical"],
        },
        "power": {"nodes": power_nodes, "edges": power_edges, **_LENS_META["power"]},
        "data": {"nodes": data_nodes, "edges": data_edges, **_LENS_META["data"]},
        "health": {"nodes": [], "edges": [], **_LENS_META["health"]},
    }


def _finding_rows(findings: list[Finding], *, redact_paths: bool = False) -> list[dict[str, Any]]:
    """Return deterministic presentation rows shared by both HTML views."""
    rendered_findings: list[dict[str, Any]] = []
    for group in _presentation_groups(findings):
        row = group[0].model_dump(mode="json")
        members = _cluster_members(group)
        if group[0].cluster_root is not None and len(members) > 1:
            root_finding = _cluster_root_finding(group)
            row["source"] = root_finding.source if root_finding is not None else None
            row["witness"] = root_finding.witness if root_finding is not None else None
            row["locked_at"] = (
                root_finding.locked_at.isoformat()
                if root_finding is not None and root_finding.locked_at is not None
                else None
            )
            row["cluster_members"] = members
            row["member_findings"] = [finding.model_dump(mode="json") for finding in group]
        if redact_paths:
            row["source"] = _redact_path(row["source"])
            row["owner"] = _redact_witness(row["owner"])
            row["witness"] = _redact_witness(row["witness"])
            for member in row.get("member_findings", []):
                member["source"] = _redact_path(member["source"])
                member["owner"] = _redact_witness(member["owner"])
                member["witness"] = _redact_witness(member["witness"])
        rendered_findings.append(row)
    return rendered_findings


#: Plain-word verdict badges this page has always shown: NO-GO and NOT SCORED
#: are distinguished, GO/GO-with-notes render identically here.
_HTML_VERDICT_TEXT = {
    "REFUSED": "NO-GO",
    "NO_GO": "NO-GO",
    "GO_WITH_NOTES": "GO",
    "NOT_SCORED": "NOT SCORED",
    "GO": "GO",
}


def _payload(
    contract: Contract,
    report: RecordReport,
    *,
    redact_paths: bool = False,
    cli_line: str | None = None,
) -> dict[str, Any]:
    findings = sorted(
        report, key=lambda finding: (_SEVERITY_RANK[finding.severity], finding.quantity)
    )
    decision = verdict(report)
    outline: list[dict[str, str]] = []
    if contract.topology is not None:
        outline.extend({"id": rail.id, "type": "rail"} for rail in contract.topology.rails)
        outline.extend({"id": bus.id, "type": "bus"} for bus in contract.topology.buses)
    outline.extend({"id": device.id, "type": "device"} for device in contract.devices)
    payload = {
        "robot": contract.robot.name,
        "contract_id": contract.robot.id or "contract",
        "verdict": _HTML_VERDICT_TEXT[decision.code],
        "violations": decision.violations,
        "warnings": decision.warnings,
        # the page's summary line was a pair (to fix · notes), which
        # leaves "not checked" — the count that makes a pass conditional —
        # invisible. Three named parts, counted off the same `decision` the
        # verdict itself comes from; the verdict word is untouched.
        "headline": {
            "adds_up": len(report.scored_records),
            "does_not_add_up": decision.violations,
            "not_checked": decision.not_scored,
        },
        "outline": outline,
        "budgets": _budget_rows(contract, findings, redact_paths=redact_paths),
        "findings": _finding_rows(findings, redact_paths=redact_paths),
        "adds_up": [record.model_dump(mode="json") for record in report.scored_records],
        "information": [_cg_row(item) for item in report.information],
        "graph": _graph(contract),
        # The exact command line the page was made with, so a reader can tell
        # a redacted page from a full one.
        "cli_line": cli_line,
    }
    if redact_paths:
        # boundary sweep — catches cli_line and the free-text
        # observed/expected/action fields the per-field calls above miss.
        # See _redact_paths_in_payload.
        payload = _redact_paths_in_payload(payload)
    return payload


def _report_identity(contract: Contract) -> str:
    """The report's own identity: which commit produced this document,
    or — outside a repository — a content hash of the contract, and the
    document says which. `render_html`/`render_preflight_html` are documented
    byte-stable, so this stays a pure function of the contract and the
    repository's current state, never the wall clock.

    Reuses `gitref`'s one door to git rather than a second subprocess
    call; the repo it asks about is wherever the process runs, which for a
    `lockwire report`/`lockwire preflight` invocation is the caller's own
    contract repo.
    """
    repo = gitref.repo_root(Path.cwd())
    if repo is not None:
        sha = gitref.rev_parse("HEAD", repo=repo)
        if sha is not None:
            dirty = bool(gitref.status_porcelain(repo).strip())
            return f"commit {sha[:12]}" + ("-dirty" if dirty else "")
    digest = hashlib.sha256(contract.model_dump_json(exclude_none=True).encode()).hexdigest()[:12]
    return f"content hash {digest} (no repository)"


def _safe_json(value: Any) -> str:
    """Embed JSON in a script without permitting an HTML end-tag breakout."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _now_utc() -> str:
    """The instant "now", in the footer's format. A seam, not a cache: a
    long-lived caller (`lockwire serve`, which dispatches `report`/`gui`
    per-request for the process's whole life — `serve.py`) must get a fresh
    stamp on every document, not the process's start time (caching it at
    import would make every document under `serve` lie about when it was
    generated). Tests pin it with `monkeypatch.setattr` instead of
    relying on two calls landing in the same wall-clock second.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def document_identity(contract_path: Path | None) -> str:
    """``contract path · commit/hash · lockwire <version> · generated <UTC>``.

    Git-native, the way git/GitHub already identify things: inside a repo the
    full commit sha plus ``git describe`` (through :mod:`lockwire.gitref`, the
    one door); outside a repo, or with ``git`` unavailable, a labelled
    sha256 content hash of the contract file so the reader never has to guess
    which kind of identity they are looking at. Display-only — no contract
    field, no ``schema.py`` change.

    The "generated" stamp is captured once per call, so a document
    rendered now says now — under a long-lived process (`lockwire serve`) two
    documents rendered a second apart carry different stamps, as they must.
    """
    path_label = str(contract_path) if contract_path is not None else "(no contract path)"
    return (
        f"{path_label} · {_repo_or_hash_identity(contract_path)} · "
        f"lockwire {__version__} · generated {_now_utc()}"
    )


def _repo_or_hash_identity(contract_path: Path | None) -> str:
    start = contract_path if contract_path is not None else Path.cwd()
    repo = gitref.repo_root(start)
    if repo is not None:
        sha = gitref.rev_parse("HEAD", repo=repo)
        if sha is not None:
            described = gitref.describe(repo=repo)
            return f"commit {sha[:12]} ({described})" if described else f"commit {sha[:12]}"
    if contract_path is not None and contract_path.exists():
        digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()[:12]
        return f"hash {digest}"
    return "hash unavailable"


def render_html(
    contract: Contract,
    contract_path: Path | None = None,
    *,
    redact_paths: bool = False,
    cli_line: str | None = None,
) -> str:
    """Return a byte-stable report document for ``contract``."""
    report = run_record_checks(contract)
    data = _safe_json(_payload(contract, report, redact_paths=redact_paths, cli_line=cli_line))
    return (
        _DOCUMENT.replace("__LOCKWIRE_DATA__", data)
        .replace("__GRAPH_LAYOUT_JS__", GRAPH_LAYOUT_JS)
        .replace("__DOCUMENT_IDENTITY__", escape(document_identity(contract_path)))
        .replace("__IDENTITY__", escape(_report_identity(contract)))
    )


def write_html_report(
    contract: Contract,
    out: Path,
    *,
    contract_path: Path | None = None,
    redact_paths: bool = False,
    cli_line: str | None = None,
) -> None:
    """Write a self-contained report, replacing ``out`` if it exists."""
    out.write_text(
        render_html(contract, contract_path, redact_paths=redact_paths, cli_line=cli_line),
        encoding="utf-8",
    )


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lockwire contract report</title>
<style>
:root{color-scheme:light;--bg:#fafbfc;--surface:#fff;--ink:#1b1f24;--muted:#6b737c;--line:#e2e5e9;--accent:#2563eb;--bad:#dc2626;--warn:#d97706;--pass:#059669;--track:#eef0f3;--plan:#f3f4f6;--node:#fff;--edge:#9aa3ad;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",sans-serif}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0c0e12;--surface:#14181f;--ink:#e8ecf1;--muted:#8b939e;--line:#262b33;--accent:#60a5fa;--bad:#f87171;--warn:#fbbf24;--pass:#34d399;--track:#1c2129;--plan:#10141a;--node:#14181f;--edge:#5a6470}}
:root[data-theme=light]{color-scheme:light;--bg:#fafbfc;--surface:#fff;--ink:#1b1f24;--muted:#6b737c;--line:#e2e5e9;--accent:#2563eb;--bad:#dc2626;--warn:#d97706;--pass:#059669;--track:#eef0f3;--plan:#f3f4f6;--node:#fff;--edge:#9aa3ad}
:root[data-theme=dark]{color-scheme:dark;--bg:#0c0e12;--surface:#14181f;--ink:#e8ecf1;--muted:#8b939e;--line:#262b33;--accent:#60a5fa;--bad:#f87171;--warn:#fbbf24;--pass:#34d399;--track:#1c2129;--plan:#10141a;--node:#14181f;--edge:#5a6470}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 var(--sans)}body{overflow-x:hidden}.mono{font-family:var(--mono)}button{font:inherit}.app{min-height:100vh}.top{height:54px;position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--surface);border-bottom:1px solid var(--line)}.brand{min-width:0;flex:1}.brand b,.brand span{display:block;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.brand span{font-size:11px;color:var(--muted)}.badge{padding:4px 8px;background:var(--plan);border:1px solid var(--line);font:700 10px var(--mono);letter-spacing:.08em}.status{padding:9px 12px;border:1px solid;color:var(--pass);font:700 12px var(--mono)}.status.bad{color:var(--bad)}.status.warn{color:var(--warn)}.theme{min-width:44px;height:44px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}.shell{display:flex;flex-direction:column}.tree,.side{background:var(--surface);padding:12px}.tree{border-bottom:1px solid var(--line)}.side{background:var(--bg);display:flex;flex-direction:column;gap:12px}.head{display:flex;justify-content:space-between;color:var(--muted);font:700 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}.tree ul{list-style:none;padding:0;margin:0}.tree li{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:6px;padding:7px 5px;border-left:3px solid transparent;font:11px var(--mono)}.tree li span:nth-child(2){overflow:hidden;text-overflow:ellipsis}.tree li.bad{border-left-color:var(--bad);background:color-mix(in srgb,var(--bad) 8%,transparent)}.mark{color:var(--pass);font-weight:800}.bad .mark{color:var(--bad)}.stage{min-width:0;padding:12px}.plan{background:var(--plan);border:1px solid var(--line);padding:10px 12px;margin-bottom:10px;overflow:auto;font:11px/1.55 var(--mono)}.plan b{display:block;color:var(--ink);margin-bottom:4px}.plan .bad{color:var(--bad)}.plan .warn{color:var(--warn)}.plan .note{color:var(--muted)}.plan .ok{color:var(--pass)}.tabs{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);margin-bottom:8px}.tabs button{height:40px;border:0;border-right:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer}.tabs button:last-child{border:0}.tabs button[aria-pressed=true]{background:var(--ink);color:var(--bg);font-weight:700}.graphbox,.panel{background:var(--surface);border:1px solid var(--line);overflow:hidden}.caption,.panelhead{display:flex;justify-content:space-between;padding:8px 10px;border-bottom:1px solid var(--line);font:11px var(--mono);color:var(--muted)}.caption b{color:var(--ink)}svg{display:block;width:100%;height:460px;min-width:0}.edge{fill:none;stroke:var(--edge);stroke-width:1.25}.edge.hit{stroke:var(--accent);stroke-width:2}.node rect{fill:var(--node);stroke:var(--line);stroke-width:1.2}.node text{fill:var(--ink);font:600 10px var(--mono)}.node text.sub{fill:var(--muted);font-size:8.5px;font-weight:400}.node.hit rect{stroke:var(--accent);stroke-width:2.5}.node.breach rect{stroke:var(--bad);stroke-width:2}.dim{opacity:.16}.budget{display:grid;grid-template-columns:20px minmax(0,1fr) auto;gap:4px 8px;padding:11px;border-top:1px solid var(--line);font:11px var(--mono)}.budget:first-child{border:0}.budget.breach{padding-top:15px;padding-bottom:15px;background:color-mix(in srgb,var(--bad) 8%,transparent)}.budget .sign{color:var(--pass);font-weight:800}.budget.breach .sign{color:var(--bad)}.budget.not_scored .sign{color:var(--muted)}.budget.not_scored .fill{background:var(--muted)}.budget .raised{grid-column:1/4;color:var(--warn)}.bar{grid-column:2/4;height:6px;background:var(--track);overflow:hidden}.fill{height:100%;background:var(--pass)}.breach .fill{background:var(--bad)}.empty,.info{padding:12px;color:var(--muted);font:11px/1.5 var(--mono)}.empty b{display:block;color:var(--pass)}.finding{width:100%;text-align:left;padding:12px;border:0;border-top:1px solid var(--line);background:var(--surface);color:var(--ink);cursor:pointer}.finding:first-child{border-top:0}.finding[aria-pressed=true]{box-shadow:inset 3px 0 var(--accent);background:color-mix(in srgb,var(--accent) 6%,var(--surface))}.finding h3{margin:0 0 5px;font:700 11px var(--mono);color:var(--bad)}.finding.warning h3{color:var(--warn)}.finding.not_scored h3{color:var(--muted)}.finding p{margin:3px 0;font:11px/1.45 var(--mono);overflow-wrap:anywhere}.finding .meta{color:var(--muted)}.finding .hint{color:var(--accent);margin-top:8px;font-weight:700}.info b{color:var(--ink)}.identity{padding:8px 12px;border-top:1px solid var(--line);color:var(--muted);font:11px var(--mono);overflow-wrap:anywhere}:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(min-width:1000px){.shell{display:grid;grid-template-columns:260px minmax(0,1fr) 320px;min-height:calc(100vh - 54px)}.tree{border:0;border-right:1px solid var(--line)}.stage{padding:14px 16px}.side{border-left:1px solid var(--line);padding:14px}.tree,.stage,.side{overflow-y:auto;max-height:calc(100vh - 54px)}}
@media(max-width:420px){.badge{display:none}.top{gap:6px}.status{padding:8px}.stage,.tree,.side{padding-left:8px;padding-right:8px}svg{height:390px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
.budget{overflow-x:auto}
.budget .meta{grid-column:2/4;color:var(--muted);overflow-wrap:anywhere}
.footer{padding:9px 12px;border-top:1px solid var(--line);color:var(--muted);font:11px var(--mono);overflow-wrap:anywhere}
.provenance-bar{padding:6px 16px;color:var(--muted);font:10px var(--mono);border-top:1px solid var(--line)}
@media print{.top{position:static}.budget,.plan,.tree,.stage,.side{overflow:visible}svg{height:auto;max-height:none}.provenance-bar{position:fixed;bottom:0;left:0;right:0;background:var(--surface)}}
</style>
</head>
<body>
<div class="app">
 <header class="top"><div class="brand"><b>lockwire</b><span id="subtitle"></span></div><span class="badge">REPORT</span><span id="status" class="status"></span><button id="theme" class="theme" aria-label="Toggle color theme">◐</button></header>
 <div class="shell">
  <aside class="tree"><div class="head"><span>Contract</span><span id="treeCount"></span></div><ul id="outline"></ul></aside>
  <main class="stage">
   <div class="plan" id="plan"></div>
   <div class="tabs" role="tablist" aria-label="Contract lens"><button data-lens="mechanical">Mechanical</button><button data-lens="power" aria-pressed="true">Power</button><button data-lens="data">Data</button></div>
   <section class="graphbox"><div class="caption"><b id="lensTitle">Power</b><span id="lensDesc">battery → rails → devices · watts</span></div><svg id="graph" role="img" aria-label="Robot contract graph"></svg></section>
  </main>
  <aside class="side">
   <section class="panel"><div class="panelhead"><b>Budgets</b><span id="budgetCount"></span></div><div id="budgets"></div></section>
   <section class="panel"><div class="panelhead"><b>Findings</b><span id="findingCount"></span></div><div id="findings"></div></section>
   <section class="panel"><div class="panelhead"><b>What adds up</b><span id="addsUpCount"></span></div><div id="addsup"></div></section>
   <section class="panel"><div class="panelhead"><b>Derived information</b><span>honest lane</span></div><div id="information"></div></section>
  </aside>
 </div>
 <footer class="identity" id="identity">__DOCUMENT_IDENTITY__</footer>
 <footer class="footer">ran: <span id="cliline" class="mono"></span></footer>
 <footer class="provenance-bar mono">source: __IDENTITY__</footer>
</div>
<script>
(function(){"use strict";
const DATA=__LOCKWIRE_DATA__;
const NS="http://www.w3.org/2000/svg";
const $=id=>document.getElementById(id);
let lens="power",active=null;
function text(tag,value,cls){const e=document.createElement(tag);if(cls)e.className=cls;e.textContent=value;return e}
function implicated(f){const values=[f.quantity,f.dependent||"",...(f.cluster_members||[])].join(" ");const hits=new Set();Object.values(DATA.graph).forEach(g=>g.nodes.forEach(n=>{if(values.includes(n.id)||f.quantity.split("/").includes(n.id))hits.add(n.id)}));DATA.budgets.forEach(b=>{if(f.quantity.includes(b.id)&&b.kind==="rail_power"){const match=DATA.graph.power.nodes.find(n=>n.id.includes(b.id.split(".").slice(-1)[0]));if(match)hits.add(match.id)}});return hits}
function findingMark(s){return s==="violation"?"✗ ":s==="warning"?"~ ":"○ "}
function findingCls(s){return s==="violation"?"bad":s==="warning"?"warn":"note"}
function renderOutline(){const ul=$("outline");ul.replaceChildren();DATA.outline.forEach(row=>{const bad=DATA.findings.some(f=>f.severity==="violation"&&implicated(f).has(row.id));const li=document.createElement("li");if(bad)li.className="bad";li.append(text("span",bad?"!":"+", "mark"),text("span",row.id),text("span",row.type));ul.append(li)});$("treeCount").textContent=DATA.outline.length+" entries"}
function renderPlan(){const box=$("plan");box.replaceChildren();box.append(text("b","lockwire plan · "+DATA.headline.adds_up+" add up · "+DATA.headline.does_not_add_up+" do not add up · "+DATA.headline.not_checked+" not checked"));if(!DATA.findings.length)box.append(text("div","✓ records agree · no findings","ok"));DATA.findings.forEach(f=>box.append(text("div",findingMark(f.severity)+f.quantity+"  "+f.observed+"  /  "+f.expected,findingCls(f.severity))))}
function fmt(v){return v===null?"not computable":Number(v).toFixed(2)}
function renderBudgets(){const root=$("budgets");root.replaceChildren();let pass=0,notScored=0;DATA.budgets.forEach(b=>{if(b.state==="pass")pass++;if(b.state==="not_scored")notScored++;const row=document.createElement("div");row.className="budget "+b.state;const ratio=b.value===null?0:(b.direction==="max"?b.value/b.limit:b.limit/b.value);const sign=b.state==="breach"?"!":b.state==="not_scored"?"·":"✓";row.append(text("span",sign,"sign"),text("b",b.id),text("span",b.note||(fmt(b.value)+" / "+fmt(b.limit)+" "+b.unit)));const bar=document.createElement("div");bar.className="bar";const fill=document.createElement("div");fill.className="fill";fill.style.width=Math.min(100,Math.max(0,ratio*100))+"%";bar.append(fill);row.append(bar);if(b.limit_raised)row.append(text("span",b.limit_raised,"raised"));const prov=[["witness",b.witness],["source",b.source],["locked_at",b.locked_at]].filter(([,v])=>v).map(([k,v])=>k+": "+v).join(" · ");row.append(text("span",prov||"limit has no provenance","meta"));root.append(row)});if(!DATA.budgets.length)root.append(text("div","No budgets declared.","empty"));const scoredTotal=DATA.budgets.length-notScored;$("budgetCount").textContent=pass+" / "+scoredTotal+" pass"+(notScored?" · "+notScored+" not scored":"")}
function renderFindings(){const root=$("findings");root.replaceChildren();$("findingCount").textContent=DATA.findings.length;if(!DATA.findings.length){const e=text("div","","empty");e.append(text("b","✓ GO — records agree"),text("span","No findings."));root.append(e);return}DATA.findings.forEach((f,i)=>{const b=document.createElement("button");b.type="button";b.className="finding "+f.severity;b.setAttribute("aria-pressed",String(active===i));const clustered=f.cluster_root&&f.cluster_members&&f.cluster_members.length>1;b.append(text("h3",findingMark(f.severity)+(clustered?f.cluster_root+" subsystem: "+f.cluster_members.length+" members affected — "+f.cluster_members.join(", "):f.quantity)));if(clustered)f.member_findings.forEach(m=>b.append(text("p",m.quantity+": observed "+m.observed+", expected "+m.expected,"meta")));else b.append(text("p","observed "+f.observed+", expected "+f.expected+(f.delta?" (Δ "+f.delta+")":"")));[["dependent",f.dependent],["owner",f.owner],["source",f.source],["witness",f.witness],["locked_at",f.locked_at],["action",f.action]].forEach(([k,v])=>{if(v)b.append(text("p",k+": "+v,"meta"))});b.append(text("p",active===i?"Highlighting · tap again to clear":"Tap to highlight on graph","hint"));b.onclick=()=>{active=active===i?null:i;renderFindings();renderGraph()};root.append(b)})}
function renderAddsUp(){const root=$("addsup");root.replaceChildren();$("addsUpCount").textContent=DATA.adds_up.length;if(!DATA.adds_up.length){root.append(text("div","Nothing scored yet.","empty"));return}DATA.adds_up.forEach(r=>{const row=text("div","","info");row.append(text("b",r.quantity),text("div","observed "+r.observed+", expected "+r.expected),text("div","margin: "+r.margin));root.append(row)})}
function renderInfo(){const root=$("information");root.replaceChildren();if(!DATA.information.length){root.append(text("div","No derived information.","empty"));return}DATA.information.forEach(i=>{const row=text("div","","info");row.append(text("b",i.quantity+" — "+i.result),text("div","frame: "+i.frame+" · sources: "+i.sources),text("div",i.mass));if(i.limitation)row.append(text("div","limitation: "+i.limitation));root.append(row)})}
__GRAPH_LAYOUT_JS__
function svgEl(tag,attrs){const e=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));return e}
function renderGraph(){const source=DATA.graph[lens],svg=$("graph"),box=svg.getBoundingClientRect();const W=Math.max(560,box.width||900),H=Math.max(320,box.height||460);const graph=layout({nodes:source.nodes.map(n=>({...n})),edges:source.edges},W,H);svg.setAttribute("viewBox","0 0 "+W+" "+H);svg.replaceChildren();const hit=active===null?null:implicated(DATA.findings[active]);graph.edges.forEach(e=>{const a=graph.byId[e.from],b=graph.byId[e.to];if(!a||!b)return;const yes=hit&&(hit.has(a.id)||hit.has(b.id));const path=svgEl("path",{d:edgePath(a,b,graph.nodes),class:"edge"+(yes?" hit":"")+(hit&&!yes?" dim":"")});svg.append(path)});graph.nodes.forEach(n=>{const yes=hit&&hit.has(n.id);const g=svgEl("g",{class:"node "+(yes?"hit ":"")+(hit&&!yes?"dim ":"")+(DATA.findings.some(f=>f.severity==="violation"&&implicated(f).has(n.id))?"breach":"")});g.append(svgEl("rect",{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:4}));const label=svgEl("text",{x:n.x,y:n.y-2,"text-anchor":"middle"});label.textContent=n.label;g.append(label);const sub=svgEl("text",{x:n.x,y:n.y+13,"text-anchor":"middle",class:"sub"});sub.textContent=n.sub;g.append(sub);svg.append(g)})}
function setLens(value){lens=value;document.querySelectorAll("[data-lens]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.lens===lens)));const meta={mechanical:["Mechanical","links · joints · mounts"],power:["Power","battery → rails → devices · watts"],data:["Data","hosts · buses · devices"]}[lens];$("lensTitle").textContent=meta[0];$("lensDesc").textContent=meta[1];renderGraph()}
$("subtitle").textContent=DATA.contract_id+" · "+DATA.robot;$("cliline").textContent=DATA.cli_line||"(no CLI line recorded)";$("status").textContent=DATA.verdict;if(DATA.verdict==="NO-GO")$("status").classList.add("bad");else if(DATA.verdict==="NOT SCORED")$("status").classList.add("warn");document.querySelectorAll("[data-lens]").forEach(b=>b.onclick=()=>setLens(b.dataset.lens));$("theme").onclick=()=>{const r=document.documentElement,c=r.getAttribute("data-theme");r.setAttribute("data-theme",c==="dark"?"light":c==="light"?"dark":matchMedia("(prefers-color-scheme: dark)").matches?"light":"dark")};renderOutline();renderPlan();renderBudgets();renderFindings();renderAddsUp();renderInfo();setLens("power");
})();
</script>
</body>
</html>
"""
