"""Deterministic, self-contained HTML view of one preflight run."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from lockwire_pro.preflight.report import PreflightReport
from lockwire.report import _SEVERITY_RANK, Finding, verdict
from lockwire.report_html import _report_identity, _safe_json
from lockwire.schema import Contract

_PREFLIGHT_STAGE_NAMES = {
    0: "Enumeration",
    1: "Liveness",
    2: "Flow",
    3: "Sanity",
    4: "Cross-checks + coverage",
}
_MAGNITUDE_MAX_SIGNIFICANT_DIGITS = 17


def _distinct_magnitude_pair(finding: Finding) -> tuple[str, str] | None:
    """Render typed magnitudes so unequal floats can never look equal.

    Start compact, then add significant figures until the two values visibly
    differ. IEEE-754 doubles round-trip within 17 significant digits, so two
    unequal stored values are distinguishable without a unit-specific rule.
    """
    observed = finding.observed_quantity
    expected = finding.expected_quantity
    if observed is None or expected is None:
        return None
    for digits in range(3, _MAGNITUDE_MAX_SIGNIFICANT_DIGITS + 1):
        observed_value = f"{observed.value:.{digits}g}"
        expected_value = f"{expected.value:.{digits}g}"
        if observed.value == expected.value or observed_value != expected_value:
            return (
                f"{observed_value} {observed.unit}",
                f"{expected_value} {expected.unit}",
            )
    raise AssertionError("unequal float magnitudes did not separate at round-trip precision")


def _preflight_payload(
    contract: Contract,
    findings: list[Finding],
    recording: str,
) -> dict[str, Any]:
    ordered = sorted(
        findings,
        key=lambda finding: (
            finding.stage if finding.stage is not None else 99,
            _SEVERITY_RANK[finding.severity],
            finding.quantity,
        ),
    )
    decision = verdict(findings)
    violations = decision.violations
    warnings = decision.warnings
    not_scored = decision.not_scored
    scored = decision.scored
    scored_by_stage = findings.scored_by_stage if isinstance(findings, PreflightReport) else {}
    # Preflight disclosures are never presentation-collapsed: the summary and
    # its visible cards must describe the exact same findings list.
    rows = [finding.model_dump(mode="json") for finding in ordered]
    for row, finding in zip(rows, ordered, strict=True):
        pair = _distinct_magnitude_pair(finding)
        row["precise_observed"] = pair[0] if pair is not None else None
        row["precise_expected"] = pair[1] if pair is not None else None

    stages = []
    for stage, name in _PREFLIGHT_STAGE_NAMES.items():
        stage_findings = [finding for finding in ordered if finding.stage == stage]
        stages.append(
            {
                "stage": stage,
                "name": name,
                "violations": sum(finding.severity == "violation" for finding in stage_findings),
                "warnings": sum(finding.severity == "warning" for finding in stage_findings),
                "not_scored": sum(finding.severity == "not_scored" for finding in stage_findings),
                "scored": scored_by_stage.get(stage),
            }
        )

    verdict_text = {
        "REFUSED": "NO-GO",
        "NO_GO": "NO-GO",
        "GO_WITH_NOTES": "GO WITH NOTES",
        "NOT_SCORED": "NOT SCORED",
        "GO": "GO",
    }[decision.code]
    return {
        "robot": contract.robot.name,
        "contract_id": contract.robot.id or "contract",
        "recording": recording,
        "verdict": verdict_text,
        "verdict_code": decision.code,
        "silent_gate": not ordered,
        "violations": violations,
        "warnings": warnings,
        "not_scored": not_scored,
        "scored": scored,
        "stages": stages,
        "findings": rows,
    }


def _preflight_finding_html(row: dict[str, Any]) -> str:
    severity = str(row["severity"])
    labels = {
        "violation": ("REFUSED", "Gate refusal"),
        "warning": ("WARNING", "Gate warning"),
        "not_scored": ("NOT SCORED", "Coverage disclosure"),
    }
    label, kind = labels[severity]
    precise = ""
    if row["precise_observed"] is not None and row["precise_expected"] is not None:
        precise = (
            '<div class="magnitudes">'
            f"<span><b>measured</b>{escape(str(row['precise_observed']))}</span>"
            f"<span><b>threshold</b>{escape(str(row['precise_expected']))}</span>"
            "</div>"
        )
    routing = "".join(
        f'<p class="route"><b>{escape(key)}</b>{escape(str(row[key]))}</p>'
        for key in ("owner", "dependent", "action")
        if row.get(key)
    )
    return (
        f'<article class="finding {escape(severity)}" data-severity="{escape(severity)}">'
        '<div class="finding-head">'
        f"<span>{escape(label)}</span><small>{escape(kind)}</small>"
        "</div>"
        f'<h3 class="mono">{escape(str(row["quantity"]))}</h3>'
        f"{precise}"
        '<div class="evidence">'
        f"<p><b>observed</b>{escape(str(row['observed']))}</p>"
        f"<p><b>expected</b>{escape(str(row['expected']))}</p>"
        "</div>"
        f"{routing}"
        "</article>"
    )


def render_preflight_html(
    contract: Contract,
    findings: list[Finding],
    recording: str,
) -> str:
    """Return a byte-stable, self-contained view of one preflight run."""
    payload = _preflight_payload(contract, findings, recording)
    data = _safe_json(payload)
    if payload["silent_gate"]:
        verdict = "SILENT GATE"
        verdict_class = "caution"
    else:
        verdict = str(payload["verdict"])
        verdict_class = (
            "bad" if verdict == "NO-GO" else "caution" if verdict == "NOT SCORED" else "good"
        )
    if payload["verdict_code"] == "NOT_SCORED":
        verdict_prose = (
            f"NOT SCORED — nothing compared; {payload['not_scored']} check(s) not scored."
        )
    elif payload["scored"] is not None:
        verdict_prose = (
            f"{payload['scored']} checks scored; "
            f"{payload['not_scored']} could not score — listed below."
        )
    else:
        verdict_prose = f"{payload['not_scored']} checks could not score — listed below."
    stage_html = "".join(
        (
            f'<li class="stage-card{" refused" if stage["violations"] else ""}">'
            f'<span class="stage-num">{stage["stage"]}</span>'
            f"<div><b>{escape(str(stage['name']))}</b>"
            f"<span>{stage['scored'] if stage['scored'] is not None else '—'} scored · "
            f"{stage['violations']} violations · "
            f"{stage['warnings']} warnings · "
            f"{stage['not_scored']} not-scored disclosures</span></div></li>"
        )
        for stage in payload["stages"]
    )
    findings_html = "".join(_preflight_finding_html(row) for row in payload["findings"])
    if not findings_html:
        findings_html = (
            '<div class="zero"><b>The gate emitted nothing.</b>Do not trust a silent gate.</div>'
        )
    return (
        _PREFLIGHT_DOCUMENT.replace("__VERDICT__", escape(verdict))
        .replace("__VERDICT_CLASS__", verdict_class)
        .replace("__ROBOT__", escape(str(payload["robot"])))
        .replace("__CONTRACT__", escape(str(payload["contract_id"])))
        .replace("__RECORDING__", escape(str(payload["recording"])))
        .replace("__VIOLATIONS__", str(payload["violations"]))
        .replace("__WARNINGS__", str(payload["warnings"]))
        .replace("__NOT_SCORED__", str(payload["not_scored"]))
        .replace("__SCORED__", str(payload["scored"]) if payload["scored"] is not None else "—")
        .replace("__VERDICT_PROSE__", escape(verdict_prose))
        .replace("__STAGES__", stage_html)
        .replace("__FINDINGS__", findings_html)
        .replace("__LOCKWIRE_DATA__", data)
        .replace("__IDENTITY__", escape(_report_identity(contract)))
    )


def write_preflight_html(
    contract: Contract,
    findings: list[Finding],
    recording: str,
    out: Path,
) -> None:
    """Write one preflight run without re-running any gate stage."""
    out.write_text(render_preflight_html(contract, findings, recording), encoding="utf-8")


_PREFLIGHT_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lockwire preflight run</title>
<style>
:root{color-scheme:light;--bg:#f4f6f8;--surface:#fff;--ink:#17202a;--muted:#637080;--line:#d8dee6;--accent:#2563eb;--bad:#c62828;--warn:#a85d00;--pass:#087f5b;--gap:#495b73;--gap-bg:#eef3f8;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",sans-serif}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0c0f13;--surface:#151a21;--ink:#edf1f5;--muted:#9aa5b1;--line:#2a323c;--accent:#70a5ff;--bad:#ff8181;--warn:#ffc16b;--pass:#56d6ad;--gap:#b5c9df;--gap-bg:#1b2530}}
:root[data-theme=light]{color-scheme:light;--bg:#f4f6f8;--surface:#fff;--ink:#17202a;--muted:#637080;--line:#d8dee6;--accent:#2563eb;--bad:#c62828;--warn:#a85d00;--pass:#087f5b;--gap:#495b73;--gap-bg:#eef3f8}
:root[data-theme=dark]{color-scheme:dark;--bg:#0c0f13;--surface:#151a21;--ink:#edf1f5;--muted:#9aa5b1;--line:#2a323c;--accent:#70a5ff;--bad:#ff8181;--warn:#ffc16b;--pass:#56d6ad;--gap:#b5c9df;--gap-bg:#1b2530}
*{box-sizing:border-box}html{background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}body{margin:0}.mono{font-family:var(--mono)}.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;min-height:58px;padding:8px max(16px,calc((100vw - 1180px)/2));background:var(--surface);border-bottom:1px solid var(--line)}.brand{min-width:0;flex:1}.brand b,.brand span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.brand span{color:var(--muted);font:11px var(--mono)}.badge{padding:5px 8px;background:var(--gap-bg);border:1px solid var(--line);font:700 10px var(--mono);letter-spacing:.08em}.verdict{padding:8px 10px;border:2px solid var(--pass);color:var(--pass);font:800 12px var(--mono)}.verdict.bad{border-color:var(--bad);color:var(--bad)}.verdict.caution{border-color:var(--warn);color:var(--warn)}.theme{width:42px;height:42px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer;font:18px var(--sans)}main{max-width:1180px;margin:0 auto;padding:22px 16px 48px}.hero{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,1fr);gap:14px;margin-bottom:18px}.panel{background:var(--surface);border:1px solid var(--line)}.verdict-panel{padding:22px}.eyebrow{margin:0 0 6px;color:var(--muted);font:700 10px var(--mono);letter-spacing:.1em;text-transform:uppercase}.verdict-panel h1{margin:0;font:800 clamp(28px,5vw,52px)/1 var(--mono)}.verdict-panel>p:last-child{margin:12px 0 0;color:var(--muted)}.denominator{display:grid;grid-template-columns:repeat(3,1fr)}.count{padding:18px;border-left:1px solid var(--line)}.count:first-child{border:0}.count b{display:block;font:800 28px var(--mono)}.count span{color:var(--muted);font:11px var(--mono)}.count.refused b{color:var(--bad)}.count.gaps b{color:var(--gap)}.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:24px 0 9px}.section-head h2{margin:0;font-size:18px}.section-head span{color:var(--muted);font:11px var(--mono)}.stages{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;padding:0;margin:0;list-style:none}.stage-card{display:flex;gap:10px;min-height:88px;padding:12px;background:var(--surface);border:1px solid var(--line);border-top:5px solid var(--gap)}.stage-card.refused{border-top-color:var(--bad)}.stage-num{font:800 22px var(--mono)}.stage-card b,.stage-card div>span{display:block}.stage-card b{font-size:12px}.stage-card div>span{margin-top:7px;color:var(--muted);font:10px/1.5 var(--mono)}.findings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.finding{min-width:0;padding:16px;background:var(--surface);border:1px solid var(--line);border-left:7px solid}.finding.violation{border-left-color:var(--bad);background:color-mix(in srgb,var(--bad) 5%,var(--surface))}.finding.warning{border-left-color:var(--warn)}.finding.not_scored{border-left-color:var(--gap);background:var(--gap-bg)}.finding-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.finding-head span{font:800 11px var(--mono);letter-spacing:.07em}.finding.violation .finding-head span{color:var(--bad)}.finding.warning .finding-head span{color:var(--warn)}.finding.not_scored .finding-head span{color:var(--gap)}.finding-head small{color:var(--muted)}.finding h3{margin:8px 0 12px;font-size:12px;overflow-wrap:anywhere}.magnitudes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:10px}.magnitudes span{padding:9px;background:var(--bg);font:12px var(--mono);overflow-wrap:anywhere}.magnitudes b,.evidence b,.route b{display:block;margin-bottom:2px;color:var(--muted);font:700 9px var(--mono);letter-spacing:.06em;text-transform:uppercase}.evidence{padding:10px;background:color-mix(in srgb,var(--surface) 65%,transparent)}.evidence p,.route{margin:0 0 8px;overflow-wrap:anywhere}.evidence p:last-child,.route:last-child{margin-bottom:0}.route{margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}.zero{padding:22px;border:2px solid var(--warn);background:var(--surface);color:var(--warn)}.zero b{display:block}.coverage{margin-top:22px;padding:18px}.coverage h2{margin:0 0 7px;font-size:16px}.coverage p{margin:7px 0}.coverage a{color:var(--accent);font-weight:700}.coverage ul{margin-bottom:0}.source{color:var(--muted);font:11px var(--mono);overflow-wrap:anywhere}
@media(max-width:850px){.hero{grid-template-columns:1fr}.stages{grid-template-columns:repeat(2,minmax(0,1fr))}.findings{grid-template-columns:1fr}}
@media(max-width:500px){.badge{display:none}.verdict{font-size:10px}.denominator{grid-template-columns:1fr}.count{border-left:0;border-top:1px solid var(--line)}.stages{grid-template-columns:1fr}.magnitudes{grid-template-columns:1fr}}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.provenance-bar{padding:6px 16px;color:var(--muted);font:10px var(--mono);border-top:1px solid var(--line)}
@media print{.top{position:static}.provenance-bar{position:fixed;bottom:0;left:0;right:0;background:var(--surface)}}
</style>
</head>
<body>
<header class="top">
 <div class="brand"><b class="mono">lockwire</b><span>__ROBOT__ · __CONTRACT__</span></div>
 <span class="badge">PREFLIGHT RUN</span>
 <span class="verdict __VERDICT_CLASS__">__VERDICT__</span>
 <button id="theme" class="theme" aria-label="Toggle color theme">◐</button>
</header>
<main>
 <section class="hero">
  <div class="panel verdict-panel">
   <p class="eyebrow">Offline gate verdict</p>
   <h1>__VERDICT__</h1>
   <p>__VERDICT_PROSE__</p>
   <p>See docs/gate_coverage.md for what a GO asserts.</p>
  </div>
  <div class="panel denominator" aria-label="Finding summary">
   <div class="count"><b>__SCORED__</b><span>scored checks</span></div>
   <div class="count refused"><b>__VIOLATIONS__</b><span>violations</span></div>
   <div class="count"><b>__WARNINGS__</b><span>warnings</span></div>
   <div class="count gaps"><b>__NOT_SCORED__</b><span>not-scored disclosures</span></div>
  </div>
 </section>
 <p class="source">recording: __RECORDING__</p>
 <div class="section-head"><h2>Stages, in run order</h2><span>scored / violations / warnings / not-scored disclosures</span></div>
 <ol class="stages">__STAGES__</ol>
 <div class="section-head"><h2>Findings and disclosures</h2><span>__VIOLATIONS__ violations · __WARNINGS__ warnings · __NOT_SCORED__ not-scored disclosures</span></div>
 <section class="findings" aria-label="All preflight findings">__FINDINGS__</section>
 <section class="panel coverage" id="gate-coverage">
  <h2>What this verdict does — and does not — assert</h2>
  <p>A GO means every stage that could score against this contract and recording found no violations. It is not powered-on robot clearance.</p>
  <p>The permanent offline boundary still requires live hardware:</p>
  <ul><li>live hardware↔software reconciliation</li><li>comm links under representative load</li></ul>
  <p>Coverage statement: docs/gate_coverage.md</p>
 </section>
</main>
<footer class="provenance-bar mono">source: __IDENTITY__</footer>
<script id="lockwire-data" type="application/json">__LOCKWIRE_DATA__</script>
<script>
(function(){"use strict";const button=document.getElementById("theme");button.onclick=()=>{const root=document.documentElement,current=root.getAttribute("data-theme");root.setAttribute("data-theme",current==="dark"?"light":current==="light"?"dark":matchMedia("(prefers-color-scheme: dark)").matches?"light":"dark")}})();
</script>
</body>
</html>
"""
