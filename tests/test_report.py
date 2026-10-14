import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
import pytest
from rich.console import Console
from typer.testing import CliRunner
from lockwire import report_html
from lockwire.checks import run_record_checks
from lockwire.cli import app
from lockwire.plan import AssertionOutcome, PlanReport
from lockwire.report import VERDICT_EXIT_CODES, Finding, RecordReport, render, verdict
from lockwire.report_html import render_html
from lockwire.schema import Physical, Provenance, Quantity, load_contract
from conftest import LIMIT_RAISED_MARK_TEXT


FIXTURES = Path(__file__).parent / "fixtures"
X500 = FIXTURES / "x500" / "contract.yaml"


def _cluster_finding(
    member: str,
    *,
    clustered: bool = True,
    source: str | None = None,
    cluster_root: str = "rail.5v",
) -> Finding:
    return Finding(
        severity="violation",
        quantity=f"devices/{member}/voltage",
        observed="4.4 V",
        expected="at least 4.8 V",
        dependent=member,
        owner="power",
        source=source,
        action=f"inspect {member}",
        cluster_root=cluster_root if clustered else None,
        cluster_members=(member,) if clustered else (),
    )


def _plain_render(findings: list[Finding]) -> tuple[int, str]:
    console = Console(record=True, width=200, color_system=None)
    result = render(findings, console)
    return result, console.export_text()


def test_verdict_uses_the_machine_exit_code_table(monkeypatch):
    decisions = [
        verdict([]),
        verdict(
            [
                Finding(
                    severity="warning",
                    quantity="q",
                    observed="o",
                    expected="e",
                    action="inspect q",
                )
            ]
        ),
        verdict(
            [
                Finding(
                    severity="violation",
                    quantity="q",
                    observed="o",
                    expected="e",
                    action="inspect q",
                )
            ]
        ),
        verdict(RecordReport([], scored=0)),
        verdict(PlanReport(outcomes=[AssertionOutcome(path="steps[0]", status="ERROR")])),
    ]
    assert {(decision.code, decision.exit_code) for decision in decisions} == set(
        VERDICT_EXIT_CODES.items()
    )

    monkeypatch.setitem(VERDICT_EXIT_CODES, "GO", 17)
    assert verdict([]).exit_code == 17


def test_render_wraps_at_the_default_80_column_width():
    """Pinning tests/conftest.py's COLUMNS to 200 for the whole suite so a
    parallel worker's tmp-path tail (``/popen-gwN/``) could not tip a phrase
    assertion over rich's wrap. Measurement showed that afterwards no
    test in the suite exercises the narrow, default 80-column terminal at all —
    this restores that one path, deliberately scoped to `render()` itself
    rather than reopening the pin.

    Every string here is a fixed literal, not a tmp_path or any other value
    whose length depends on the OS, the worker id or the machine — so this is
    sensitive to the console width it is given, and only to that.
    """
    finding = Finding(
        severity="violation",
        quantity="devices/pm02-5v-regulator-bank-primary/voltage",
        observed="4.4 V",
        expected="at least 4.8 V, with margin for a cold-start transient sag",
        dependent="pm02-5v-regulator-bank-primary",
        owner="power",
        source="rail.5v",
        action="inspect pm02-5v-regulator-bank-primary wiring and connector seating",
    )
    narrow = Console(record=True, width=80, color_system=None)
    render([finding], narrow)
    wide = Console(record=True, width=200, color_system=None)
    render([finding], wide)

    narrow_text = narrow.export_text()
    wide_text = wide.export_text()
    assert narrow_text.count("\n") > wide_text.count("\n"), (
        "narrower console should wrap the finding onto more lines"
    )
    # Same content either way — width changes layout, not what is said.
    assert " ".join(narrow_text.split()) == " ".join(wide_text.split())


def _html_data(html: str) -> dict:
    payload = html.split("const DATA=", 1)[1].split(";\nconst NS=", 1)[0]
    return json.loads(payload)


def test_x500_report_contains_real_pm02_no_go(tmp_path):
    out = tmp_path / "report.html"

    result = CliRunner().invoke(app, ["report", str(X500), "-o", str(out)])

    assert result.exit_code == 0
    html = out.read_text()
    finding = next(
        item
        for item in _html_data(html)["findings"]
        if item["quantity"] == "budgets/bud.power.pm02-5v"
    )
    assert finding["observed_quantity"]["value"] == pytest.approx(35.20)
    assert finding["observed_quantity"]["unit"] == "W"
    assert finding["expected_quantity"]["value"] == pytest.approx(15.60)
    assert finding["expected_quantity"]["unit"] == "W"
    # Rendered prose in the DATA payload stays unchanged for humans / existing UI.
    assert finding["observed"] == "35.20 W (draw mode: nominal)"
    assert finding["expected"] == "<= 15.60 W"
    assert '"verdict":"NO-GO"' in html
    # the radio joining the power tree also flips the
    # 7W-mode budget to a real violation (17.2 W > 15.6 W).
    assert '"violations":2' in html


def test_report_renders_witness_and_locked_at():
    """report_html.py must render witness/locked_at alongside source, same as
    gui already does — a finding with provenance shows all three.
    Mutation bite: revert the witness/locked_at threading in checks.py (the
    Finding call sites go back to setting only source) and this fails.
    """
    contract = load_contract(X500)
    html = render_html(contract)
    html_data = _html_data(html)
    finding = next(
        item
        for item in html_data["findings"]
        if item["quantity"] == "topology/buses/bus.can.dronecan/bandwidth"
    )

    assert finding["witness"] == "w0"
    assert finding["locked_at"] == "2026-07-25"
    assert '"witness":"w0"' in html
    assert '"locked_at":"2026-07-25"' in html


def test_redact_paths_leaves_no_absolute_path(monkeypatch):
    """--redact-paths shortens an absolute filesystem path to its
    basename and drops the owner/witness names, so a page meant to leave the
    machine doesn't carry either. Mutation bite: redact in the template
    instead of the shared string path (report_html._redact_path /
    _redact_witness) — leave one absolute path in a finding's source line —
    and this goes red.
    """
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    finding = Finding(
        severity="warning",
        quantity="devices/dev.imu/accel_z",
        observed="1 m/s^2",
        expected="2 m/s^2",
        owner="shivansh",
        source="/Users/shivansh/private/datasheet.pdf",
        witness="shivansh",
        action="check it",
    )
    monkeypatch.setattr(report_html, "run_record_checks", lambda contract: RecordReport([finding]))

    full = render_html(contract)
    redacted = render_html(contract, redact_paths=True)

    assert "/Users/shivansh/private/datasheet.pdf" in full
    assert "/Users/shivansh" not in redacted
    assert "datasheet.pdf" in redacted

    full_data = _html_data(full)["findings"][0]
    redacted_data = _html_data(redacted)["findings"][0]
    assert full_data["owner"] == "shivansh"
    assert full_data["witness"] == "shivansh"
    assert redacted_data["owner"] is None
    assert redacted_data["witness"] is None
    assert redacted_data["source"] == "datasheet.pdf"


def test_footer_names_the_redaction():
    """The footer prints the exact command line the page was made
    with, so a reader can tell a redacted page from a full one."""
    contract = load_contract(FIXTURES / "contract_clean.yaml")

    redacted = render_html(
        contract,
        redact_paths=True,
        cli_line="lockwire report lockwire.yaml --out <this file> --redact-paths",
    )
    plain = render_html(contract, cli_line="lockwire report lockwire.yaml --out <this file>")

    assert "--redact-paths" in redacted
    assert "--redact-paths" not in plain


def test_redact_paths_sweeps_the_whole_document_not_a_field_list(monkeypatch):
    """The redaction only covered source/owner/witness — an
    absolute path in ``cli_line`` (a JSON field) or in the free-text
    observed/expected/action fields survived. Rather than test a field list
    (which a new leaky field would silently dodge), this builds a contract
    where EVERY string-shaped finding field carries an absolute path and
    asserts the redacted document's bytes contain no ``/Users/`` anywhere —
    a claim about the whole document, not about the fields anyone thought to
    name.

    Mutation bite: skip the boundary sweep in ``report_html._payload`` (or
    weaken its regex to a whole-field match like ``_redact_path``'s) and one
    of the observed/expected/action/cli_line assertions goes red, naming the
    still-leaking field.
    """
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    leaky_path = "/Users/shivansh/private/robot-notes/datasheet.pdf"
    # an Electron app is cross-platform, so a contract authored on
    # Windows carries Windows-shaped paths (drive-letter, and UNC) into the
    # report. The POSIX-only regex missed both on the first respin.
    windows_path = r"C:\Users\shivansh\Documents\datasheet.pdf"
    unc_path = r"\\fileserver\shivansh\datasheet.pdf"
    finding = Finding(
        severity="warning",
        quantity="devices/dev.imu/accel_z",
        observed=(f"measured against the recording at {leaky_path}, also seen at {windows_path}"),
        expected=f"threshold from the sheet at {leaky_path}, also at {unc_path}",
        owner="shivansh",
        source=leaky_path,
        witness="shivansh",
        action=f"open {leaky_path} and confirm the datasheet value",
    )
    monkeypatch.setattr(report_html, "run_record_checks", lambda contract: RecordReport([finding]))

    redacted = render_html(
        contract,
        redact_paths=True,
        cli_line=f"lockwire report {leaky_path} --out <this file> --redact-paths",
    )

    assert "/Users/" not in redacted
    assert "shivansh" not in redacted
    # still a real, renderable document — a redactor that emptied the page
    # would trivially pass a bytes-only assertion.
    data = _html_data(redacted)
    row = data["findings"][0]
    assert "datasheet.pdf" in row["observed"]
    assert "datasheet.pdf" in row["expected"]
    assert "datasheet.pdf" in row["action"]
    assert row["source"] == "datasheet.pdf"
    assert data["cli_line"] is not None
    assert "datasheet.pdf" in data["cli_line"]
    assert "<title>" in redacted


def test_bus_bitrate_and_budget_limit_sources_transfer_to_findings():
    contract = load_contract(FIXTURES / "contract_clean.yaml")

    def provenance(source: str) -> Provenance:
        return Provenance(
            witness="w0",
            source=source,
            locked_at=date(2026, 7, 26),
        )

    bus = contract.topology.buses[0]
    bus.bitrate = 100_000
    bus.provenance = provenance("USB configured bitrate record")
    budget = next(item for item in contract.budgets if item.kind == "mass_total")
    budget.limit = Quantity(value=1, unit="g")
    budget.provenance = provenance("mass budget decision")

    findings = {finding.quantity: finding for finding in run_record_checks(contract)}

    assert findings["topology/buses/bus.usb0/bandwidth"].source == ("USB configured bitrate record")
    assert findings["budgets/bud.mass.devices"].source == "mass budget decision"


def test_budget_limit_exceeding_rail_capacity_cites_budget_source(monkeypatch):
    contract = load_contract(FIXTURES / "contract_clean.yaml")

    def provenance(source: str) -> Provenance:
        return Provenance(
            witness="w0",
            source=source,
            locked_at=date(2026, 7, 26),
        )

    rail = contract.rail("rail.5v")
    budget = next(item for item in contract.budgets if item.id == "bud.power.rail5v")
    rail.provenance = provenance("rail capacity datasheet")
    budget.limit = Quantity(value=8, unit="W")
    budget.provenance = provenance("budget limit decision")

    finding = next(
        finding
        for finding in run_record_checks(contract)
        if finding.quantity == "budgets/bud.power.rail5v/limit"
    )
    _, text_report = _plain_render([finding])
    monkeypatch.setattr(
        report_html,
        "run_record_checks",
        lambda contract: RecordReport([finding]),
    )
    html = render_html(contract)
    html_data = _html_data(html)

    assert finding.observed == "8.00 W"
    assert finding.source == "budget limit decision"
    assert "source: budget limit decision" in text_report
    assert "rail capacity datasheet" not in text_report
    assert html_data["findings"][0]["source"] == "budget limit decision"
    assert "rail capacity datasheet" not in html


def test_rail_capacity_exceeded_cites_rail_source(monkeypatch):
    contract = load_contract(FIXTURES / "contract_clean.yaml")

    def provenance(source: str) -> Provenance:
        return Provenance(
            witness="w0",
            source=source,
            locked_at=date(2026, 7, 26),
        )

    rail = contract.rail("rail.5v")
    budget = next(item for item in contract.budgets if item.id == "bud.power.rail5v")
    rail.provenance = provenance("rail capacity datasheet")
    budget.limit = Quantity(value=1, unit="W")
    budget.provenance = provenance("budget limit decision")

    finding = next(
        finding
        for finding in run_record_checks(contract)
        if finding.quantity == "budgets/bud.power.rail5v"
    )
    _, text_report = _plain_render([finding])
    monkeypatch.setattr(
        report_html,
        "run_record_checks",
        lambda contract: RecordReport([finding]),
    )
    html = render_html(contract)
    html_data = _html_data(html)

    assert finding.observed == "2.40 W"
    assert finding.source == "rail capacity datasheet"
    assert "source: rail capacity datasheet" in text_report
    assert "budget limit decision" not in text_report
    assert html_data["findings"][0]["source"] == "rail capacity datasheet"
    # The rule this guards is that the *finding* cites the rail, not the budget
    # limit's own decision. The whole-document proxy for that became too broad
    # once budget rows started carrying their limit's provenance: the
    # budget row is where "budget limit decision" belongs, and the finding is
    # where it must still never appear.
    assert "budget limit decision" not in json.dumps(html_data["findings"])
    budget_row = next(row for row in html_data["budgets"] if row["id"] == "bud.power.rail5v")
    assert budget_row["source"] == "budget limit decision"


def test_empty_rail_budget_row_is_not_scored_not_a_pass(empty_budget_contract):
    """A budget with zero contributing power
    witnesses must never render as a pass or as "0.00 / limit W" — that reads
    as a checked, healthy rail when nothing was measured at all. Mutation
    bite: reverting to `"pass"` for every id not in `failed` turns this red.
    """
    html_data = _html_data(render_html(empty_budget_contract))
    row = next(b for b in html_data["budgets"] if b["id"] == "bud.power.empty")

    assert row["state"] == "not_scored"  # bites it reading as "pass"
    assert row["value"] is None  # bites the vacuous "0.00 W"
    assert row["note"] == "no draw declared on this rail"


def test_empty_endurance_budget_row_is_not_scored_not_a_pass(empty_endurance_budget_contract):
    """A battery with zero
    declared draw must never render as a pass or "0.00 / 15.00 min" — that
    reads as a measured, healthy endurance when nothing was ever drawn.
    """
    html_data = _html_data(render_html(empty_endurance_budget_contract))
    row = next(b for b in html_data["budgets"] if b["id"] == "bud.endurance.empty")

    assert row["state"] == "not_scored"  # bites it reading as "pass"
    assert row["value"] is None  # bites the vacuous "0.00 min"
    assert row["note"] == "no draw declared"


def test_finding_without_source_renders_exactly_as_before(monkeypatch):
    finding = _cluster_finding("dev.camera", clustered=False)
    expected_text = (
        "VIOLATION devices/dev.camera/voltage — observed 4.4 V, expected at least 4.8 V\n"
        "  dependent: dev.camera\n"
        "  owner: power\n"
        "  action: inspect dev.camera\n"
        "\nNO-GO — 1 violation(s), 0 warning(s)\n"
    )

    assert _plain_render([finding]) == (1, expected_text)

    contract = load_contract(FIXTURES / "contract_clean.yaml")
    monkeypatch.setattr(
        report_html,
        "run_record_checks",
        lambda contract: RecordReport([finding]),
    )
    html_data = _html_data(render_html(contract))
    assert html_data["findings"][0]["source"] is None


def test_cluster_root_source_survives_collapse(monkeypatch):
    findings = RecordReport(
        [
            _cluster_finding("dev.able", cluster_root="dev.zephyr"),
            _cluster_finding(
                "dev.zephyr",
                source="rail capacity datasheet",
                cluster_root="dev.zephyr",
            ),
        ]
    )

    _, text_report = _plain_render(findings)
    monkeypatch.setattr(report_html, "run_record_checks", lambda contract: findings)
    html_data = _html_data(render_html(load_contract(FIXTURES / "contract_clean.yaml")))

    assert "source: rail capacity datasheet" in text_report
    assert html_data["findings"][0]["source"] == "rail capacity datasheet"
    assert html_data["findings"][0]["member_findings"][0]["source"] is None
    assert html_data["findings"][0]["member_findings"][1]["source"] == ("rail capacity datasheet")


def test_clean_contract_report_is_go_without_findings():
    html = render_html(load_contract(FIXTURES / "contract_clean.yaml"))
    html_data = _html_data(html)

    assert '"verdict":"GO"' in html
    # Clean fixture emits five not_scored findings (URDF relative path,
    # bus.serial0's known-bitrate-but-no-declared-traffic disclosure,
    # and three devices with no `physical` block at all
    # treats that the same as a `physical` block with a missing mass);
    # apply the same filtering logic as _assert_clean_baseline (test_checks.py)
    findings = html_data["findings"]
    violations = [f for f in findings if f["severity"] == "violation"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    not_scored = [f for f in findings if f["severity"] == "not_scored"]
    assert violations == []
    assert warnings == []
    assert {f["quantity"] for f in not_scored} == {
        "model/structure_hash",
        "topology/buses/bus.serial0/bandwidth",
        "devices/dev.barometer/physical/mass",
        "devices/dev.gnss/physical/mass",
        "devices/dev.esc.busless/physical/mass",
        # check_edge_tolerance discloses both fixture edges as
        # not_scored when no current reading is supplied (nothing here supplies one).
        "devices/dev.imu/physical/mass",
        "model/joints/imu_mount/limits/effort",
    }
    urdf = next(f for f in not_scored if f["quantity"] == "model/structure_hash")
    assert "relative path" in urdf["observed"]
    assert "not resolvable" in urdf["observed"]
    assert '"violations":0' in html


def test_clustered_findings_render_as_one_alarm_in_text_and_html(monkeypatch):
    members = ("dev.camera", "dev.compute", "dev.lidar", "dev.radio")
    findings = RecordReport([_cluster_finding(member) for member in members])

    gate_result, text_report = _plain_render(findings)
    monkeypatch.setattr(report_html, "run_record_checks", lambda contract: findings)
    html_data = _html_data(render_html(load_contract(FIXTURES / "contract_clean.yaml")))

    assert text_report.count("VIOLATION") == 1
    assert len(html_data["findings"]) == 1
    assert "rail.5v" in text_report
    assert html_data["findings"][0]["cluster_root"] == "rail.5v"
    assert "4 members affected" in text_report
    assert len(html_data["findings"][0]["cluster_members"]) == 4
    for member in members:
        assert member in text_report
        assert member in html_data["findings"][0]["cluster_members"]

    # Presentation collapse cannot change the gate decision or its raw counts.
    uncollapsed_result, uncollapsed_text = _plain_render(
        [_cluster_finding(member, clustered=False) for member in members]
    )
    assert gate_result == uncollapsed_result == 1
    assert "4 violation(s), 0 warning(s)" in text_report
    assert "4 violation(s), 0 warning(s)" in uncollapsed_text
    assert html_data["verdict"] == "NO-GO"
    assert html_data["violations"] == 4
    assert html_data["warnings"] == 0


def test_single_member_cluster_renders_exactly_as_an_ordinary_finding(monkeypatch):
    clustered = _cluster_finding("dev.camera")
    ordinary = _cluster_finding("dev.camera", clustered=False)

    assert _plain_render([clustered]) == _plain_render([ordinary])
    assert _plain_render([clustered])[1] == (
        "VIOLATION devices/dev.camera/voltage — observed 4.4 V, expected at least 4.8 V\n"
        "  dependent: dev.camera\n"
        "  owner: power\n"
        "  action: inspect dev.camera\n"
        "\nNO-GO — 1 violation(s), 0 warning(s)\n"
    )

    contract = load_contract(FIXTURES / "contract_clean.yaml")
    monkeypatch.setattr(
        report_html,
        "run_record_checks",
        lambda contract: RecordReport([clustered]),
    )
    clustered_data = _html_data(render_html(contract))
    monkeypatch.setattr(
        report_html,
        "run_record_checks",
        lambda contract: RecordReport([ordinary]),
    )
    ordinary_data = _html_data(render_html(contract))

    for field in ("severity", "quantity", "observed", "expected", "action"):
        assert clustered_data["findings"][0][field] == ordinary_data["findings"][0][field]
    assert "member_findings" not in clustered_data["findings"][0]


def test_zero_findings_text_grammar_is_unchanged():
    result, text_report = _plain_render([])

    assert result == 0
    assert text_report == "\nGO — records agree, no findings\n"


def test_stage_none_finding_renders_exactly_as_before():
    """Record-check findings (stage=None) keep the unlabelled historical grammar."""
    finding = Finding(
        severity="violation",
        quantity="budgets/bud.power.rail5v",
        observed="6.00 W",
        expected="<= 5.00 W",
        action="reduce draw",
    )
    assert finding.stage is None
    result, text = _plain_render([finding])
    assert result == 1
    assert text == (
        "VIOLATION budgets/bud.power.rail5v — observed 6.00 W, expected <= 5.00 W\n"
        "  action: reduce draw\n"
        "\nNO-GO — 1 violation(s), 0 warning(s)\n"
    )
    assert "stage " not in text


def test_staged_findings_are_grouped_and_labelled_by_stage():
    findings = [
        Finding(
            severity="not_scored",
            quantity="preflight/stage/2/flow",
            observed="stage not yet implemented (M1a in progress)",
            expected="stage 2",
            action="await",
            stage=2,
        ),
        Finding(
            severity="not_scored",
            quantity="devices/dev.imu/enumeration",
            observed="not observed by preflight source",
            expected="present",
            action="disclose",
            stage=0,
        ),
    ]
    result, text = _plain_render(findings)
    assert result == 0
    assert "stage 0" in text
    assert "stage 2" in text
    # Stage headers appear before their findings; stage 0 before stage 2.
    assert text.index("stage 0") < text.index("devices/dev.imu/enumeration")
    assert text.index("stage 0") < text.index("stage 2")
    assert text.index("stage 2") < text.index("preflight/stage/2/flow")


def test_hostile_source_script_markup_is_escaped():
    contract = load_contract(X500)
    contract.rail("rail.pm02-5v").provenance.source = "</script><script>alert('source')</script>"

    html = render_html(contract)

    assert "</script><script>alert('source')</script>" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert" in html


def test_user_controlled_script_markup_is_escaped():
    contract = load_contract(FIXTURES / "contract_clean.yaml")
    contract.devices[0].owner = "<script>alert('owned')</script>"
    contract.devices[0].id = "dev.<script>alert('id')</script>"

    html = render_html(contract)

    assert "<script>alert" not in html
    assert "\\u003cscript\\u003ealert" in html


def test_report_is_deterministic(monkeypatch):
    """`render_html(c) == render_html(c)` must hold within one render, not
    because the "generated" clock happens to land in the same wall-clock
    second twice (relying on that made a long-lived process
    like `lockwire serve` stamp every document with its own start time,
    which is false). Pin the clock so the property is proved, not hoped for.
    """
    contract = load_contract(X500)
    monkeypatch.setattr(report_html, "_now_utc", lambda: "2026-01-01T00:00:00Z")

    assert render_html(contract) == render_html(contract)


def test_report_generated_stamp_reflects_render_time(monkeypatch):
    """Two documents rendered at different instants carry
    DIFFERENT "generated" stamps — the bite that proves the process-import-
    time cache (`_PROCESS_GENERATED_AT`) is gone. Under `lockwire serve`
    (a long-lived process dispatching `report`/`gui` per-request) that cache
    made every document lie about when it was generated; this asserts the
    fix holds even when two renders are far enough apart to land in
    different wall-clock seconds.

    Mutation bite: reintroduce a module-level `_PROCESS_GENERATED_AT`
    computed once and read `_now_utc` (or `document_identity`) from it
    instead of calling the clock per invocation, and this goes red.
    """
    stamps = iter(["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"])
    monkeypatch.setattr(report_html, "_now_utc", lambda: next(stamps))

    first = report_html.document_identity(X500)
    second = report_html.document_identity(X500)

    assert first != second
    assert "2026-01-01T00:00:00Z" in first
    assert "2026-01-01T00:00:01Z" in second


def test_record_html_verdict_is_sourced_from_report_verdict(monkeypatch):
    """`report_html._payload` must decide GO/NO-GO by calling `report.verdict`, the
    same function `report_json`/`mcp_server`/the terminal already use — not by
    re-summing severities locally, which is a second implementation of the exit
    map that can silently drift from the others.

    Mutation bite: reverting `_payload` to `"NO-GO" if violations else "GO"`
    stops calling `verdict` and this fails (call count stays 0).
    """
    import lockwire.report as report_module

    calls = []
    real_verdict = report_module.verdict

    def spy(findings):
        calls.append(findings)
        return real_verdict(findings)

    monkeypatch.setattr(report_html, "verdict", spy, raising=False)

    render_html(load_contract(X500))
    assert calls, "render_html never called report.verdict()"


def test_html_rail_rollup_matches_the_gate_for_a_non_nominal_draw_mode():
    """report_html._budget_rows had its own nominal-only power rollup; the gate's
    checks._power_rollup is draw-mode-aware. On a budget that selects a named
    mode (x500's bud.power.pm02-5v.7w-mode selects the Jetson's 7W mode: 7 W +
    5 W OAK-D Lite + 5.2 W radio peak-fallback = 17.2 W), the
    rail total the HTML shows must equal the gate's total for that mode, not
    the nominal 35.2 W total.

    Mutation bite: restoring the nominal-only copy shows 35.20 W here again.
    """
    from lockwire.checks import power_rollup, run_record_checks

    contract = load_contract(X500)
    report = run_record_checks(contract)
    rows = report_html._budget_rows(contract, list(report))
    row = next(r for r in rows if r["id"] == "bud.power.pm02-5v.7w-mode")

    gate_totals, _witnesses, _battery_w, _battery_witnesses, _rail_unknown, _battery_unknown = (
        power_rollup(contract, "7W")
    )
    assert row["value"] == pytest.approx(gate_totals["rail.pm02-5v"])
    assert row["value"] == pytest.approx(17.2)


def test_html_mass_budget_and_graph_survive_a_device_with_no_declared_mass():
    """A physical block with no mass must not crash the HTML report — the
    mass_total budget row excludes it from the sum and the graph sub-label
    reads 'mass unknown', the same as a device with no physical block at all."""
    contract = load_contract(X500)
    device = contract.device("dev.companion.jetson-orin-nano")
    device.physical = Physical(mount_frame="x500_frame")

    rows = report_html._budget_rows(contract, list(run_record_checks(contract)))
    mass_row = next(r for r in rows if r["kind"] == "mass_total")
    other_devices_kg = sum(
        d.physical.mass.base()
        for d in contract.devices
        if d.physical is not None and d.physical.mass is not None
    )
    assert mass_row["value"] == pytest.approx(other_devices_kg)  # bites summing None as 0/crashing

    graph = report_html._graph(contract)
    node = next(n for n in graph["mechanical"]["nodes"] if n["id"] == device.id)
    assert node["sub"] == "mass unknown"  # bites the .base() crash on a None mass


def test_x500_html_report_carries_at_least_one_adds_up_row():
    """A passing comparison must show up as a row in the report_html findings
    section too, not only in the GUI. Mutation bite: deleting
    ``scores.passed`` calls empties ``payload["adds_up"]`` and this fails."""
    html = render_html(load_contract(X500))
    html_data = _html_data(html)

    assert html_data["adds_up"], "x500 fixture is expected to have scored passes"
    row = html_data["adds_up"][0]
    assert row["observed"] and row["expected"] and row["margin"]
    assert "What adds up" in html
    assert row["quantity"] in html


def test_terminal_order_puts_the_violation_before_any_info_line():
    """The terminal reads top-down — findings (what someone came to see) lead,
    the CG information lane follows, the verdict is last. Mutation bite: restoring
    INFO-first (rendering `information` before the finding groups) turns this red.
    """
    report = run_record_checks(load_contract(X500))
    _, text = _plain_render(report)

    lines = [line for line in text.splitlines() if line.strip()]
    violation_index = next(i for i, line in enumerate(lines) if line.startswith("VIOLATION"))
    info_index = next(i for i, line in enumerate(lines) if line.startswith("INFO"))
    verdict_index = next(i for i, line in enumerate(lines) if "NO-GO" in line or "GO" in line)

    assert violation_index < info_index  # bites INFO printing before the finding it buries
    assert verdict_index == len(lines) - 1  # bites the verdict moving off the last line


def test_cg_collapsed_to_one_line_when_no_contributor_is_positioned():
    """With zero positioned contributors the CG lane collapses to a single
    line instead of one 'position unresolved' line per device. Mutation bite:
    restoring the always-list-every-contributor loop makes this red (multiple
    'contributor:' lines and a shorter, unqualified 'not computable' message).
    """
    report = run_record_checks(load_contract(X500))
    _, text = _plain_render(report)

    assert "none of the 12 contributors" in text
    assert "contributor:" not in text  # bites the per-contributor listing coming back
    assert "limitation:" in text  # the limitation line is kept


def test_scored_line_reports_adds_up_do_not_and_not_scored():
    """One plain-words line, directly before the verdict, states how many
    record comparisons agreed, how many didn't, and how many couldn't be
    scored. Mutation bite: dropping this line, or computing any of its three
    counts wrong, turns this red.
    """
    report = run_record_checks(load_contract(X500))
    _, text = _plain_render(report)

    lines = [line for line in text.splitlines() if line.strip()]
    scored_index = next(i for i, line in enumerate(lines) if line.startswith("scored "))
    verdict_index = next(i for i, line in enumerate(lines) if "NO-GO" in line or "GO" in line)

    # the radio's new rail/voltage/budget comparisons add 3
    # scored units, and its 5.2 W peak draw flips bud.power.pm02-5v.7w-mode to
    # a second violation: scored 9 (7 add up, 2 do not) vs the prior 6 (5, 1).
    # its link_budget now also scores a real pass (bundled
    # antenna gain published), moving one row from not-scored to adds-up:
    # 8 add up, 6 not scored.
    # one scored key per comparison — the radio's link_budget
    # comparison no longer shares its scored key with its voltage-compat
    # comparison, so the denominator itself moves 9 -> 10 (8 add up unchanged).
    # three devices with no `physical` block at all
    # (Jetson, camera, magnetometer) are now `not_scored` for mass instead of
    # a silent skip, moving not-scored 6 -> 9 (8 add up unchanged).
    assert lines[scored_index] == "scored 10: 8 add up, 2 do not · 9 not scored"
    assert scored_index == verdict_index - 1  # directly before the verdict line


def test_scored_zero_with_a_warning_is_not_reported_as_do_not_add_up(empty_budget_contract):
    """An empty rail budget scores zero comparisons
    and fires a WARNING (not a violation) — the terminal line must not read
    "0 add up, 1 do not", which claims one comparison failed when none was
    even made. Mutation bite: leaving `do_not = violations + warnings` turns
    this red.
    """
    report = run_record_checks(empty_budget_contract)
    _, text = _plain_render(report)
    lines = [line for line in text.splitlines() if line.strip()]
    scored_index = next(i for i, line in enumerate(lines) if line.startswith("scored "))

    assert report.scored == 0
    assert lines[scored_index] == "scored 0: nothing compared · 1 warning · 1 not scored"


def test_scored_zero_stays_not_scored_even_with_a_warning(empty_budget_contract):
    """Edge case: the warning branch used to be checked
    before the scored == 0 branch, so a run that compared nothing but carried a
    warning printed "GO with notes", exit 0 — the word GO and a 0/1 exit for a
    nothing-compared verdict, exactly what must never happen. Mutation bite: swap the
    scored == 0 and warnings branches back and this goes red.
    """
    from lockwire.report import verdict

    report = run_record_checks(empty_budget_contract)
    decision = verdict(report)

    assert report.scored == 0
    assert decision.code == "NOT_SCORED"
    assert decision.exit_code == 3
    assert "1 warning(s)" in decision.summary
    assert "GO" not in decision.summary


def test_scored_line_counts_comparison_violations_only(clean_contract):
    """G2 audit 2 §5: a reference-integrity violation (dangling drives_joint,
    never compared to a second source) is not a "do not add up" — only a
    violation whose quantity is a scored comparison counts there. Non-comparison
    violations get their own line. Mutation bite: counting all violations in
    ``do_not`` (the old ``decision.violations``) turns this into
    "scored 9: 8 add up, 2 do not", double-counting the dangling reference.
    """
    clean_contract.device("dev.gnss").drives_joint = "ghost_joint"  # reference-only violation
    budget = next(b for b in clean_contract.budgets if b.kind == "mass_total")
    budget.limit.value = 0.0001  # comparison violation: mass now exceeds it

    report = run_record_checks(clean_contract)
    _, text = _plain_render(report)
    lines = [line for line in text.splitlines() if line.strip()]
    scored_index = next(i for i, line in enumerate(lines) if line.startswith("scored "))
    references_index = next(
        i for i, line in enumerate(lines) if line.startswith(("reference:", "references:"))
    )

    # check_edge_tolerance discloses both fixture edges as not_scored
    # when no current reading is supplied — 5 -> 7.
    assert lines[scored_index] == "scored 9: 8 add up, 1 do not · 7 not scored"
    assert lines[references_index] == "reference: 1 broken"
    assert references_index == scored_index + 1


# ------------------------------------------------------------------- shared graph layout


REPORT_GRAPH_HARNESS = """
const W = 826, H = 460;
const src = SRC_GRAPH;
LAYOUT_SOURCE
EDGEPATH_SOURCE
const out = layout(src, W, H);
const byId = {};
out.nodes.forEach(n => byId[n.id] = n);
let overlaps = 0, escaped = 0;
for (let i = 0; i < out.nodes.length; i++) {
  const a = out.nodes[i];
  if (a.x - a.w / 2 < 0 || a.x + a.w / 2 > W || a.y - a.h / 2 < 0 || a.y + a.h / 2 > H) escaped++;
  for (let j = i + 1; j < out.nodes.length; j++) {
    const b = out.nodes[j];
    if (Math.abs(a.x - b.x) < (a.w + b.w) / 2 && Math.abs(a.y - b.y) < (a.h + b.h) / 2) overlaps++;
  }
}
function segsOf(d) {
  const toks = d.replace(/([MCL])/g, " $1 ").trim().split(/\\s+/);
  let i = 0, cur = null;
  const cmds = [];
  while (i < toks.length) {
    const t = toks[i];
    if (t === "M") { cur = {x: parseFloat(toks[i+1]), y: parseFloat(toks[i+2])}; i += 3; }
    else if (t === "L") {
      const p = {x: parseFloat(toks[i+1]), y: parseFloat(toks[i+2])};
      cmds.push({type: "L", p0: cur, p1: p}); cur = p; i += 3;
    } else if (t === "C") {
      const nums = toks.slice(i+1, i+7).map(parseFloat);
      const p1 = {x: nums[0], y: nums[1]}, p2 = {x: nums[2], y: nums[3]}, p3 = {x: nums[4], y: nums[5]};
      cmds.push({type: "C", p0: cur, p1: p1, p2: p2, p3: p3});
      cur = p3; i += 7;
    } else i++;
  }
  return cmds;
}
function sample(cmd, t) {
  if (cmd.type === "L") return {x: cmd.p0.x + (cmd.p1.x - cmd.p0.x) * t,
    y: cmd.p0.y + (cmd.p1.y - cmd.p0.y) * t};
  const p0 = cmd.p0, p1 = cmd.p1, p2 = cmd.p2, p3 = cmd.p3, u = 1 - t;
  return {x: u*u*u*p0.x + 3*u*u*t*p1.x + 3*u*t*t*p2.x + t*t*t*p3.x,
    y: u*u*u*p0.y + 3*u*u*t*p1.y + 3*u*t*t*p2.y + t*t*t*p3.y};
}
let crossings = 0;
const bad = [];
out.edges.forEach(e => {
  const a = byId[e.from], b = byId[e.to];
  const d = edgePath(a, b, out.nodes);
  segsOf(d).forEach(cmd => {
    for (let s = 0; s <= 40; s++) {
      const pt = sample(cmd, s / 40);
      out.nodes.forEach(n => {
        if (n === a || n === b) return;
        const shrink = 2, hw = n.w/2 - shrink, hh = n.h/2 - shrink;
        if (Math.abs(pt.x - n.x) < hw && Math.abs(pt.y - n.y) < hh) {
          crossings++; bad.push(e.from + "->" + e.to + " crosses " + n.id);
        }
      });
    }
  });
});
console.log(JSON.stringify({overlaps: overlaps, escaped: escaped, crossings: crossings,
  bad: [...new Set(bad)]}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_x500_report_power_graph_has_no_overlaps_off_canvas_or_crossings(tmp_path):
    """The report drew its Power graph in a fixed 500x460 band with no
    wrapping, no parent grouping, and boxes fixed at 128x44 — cramming x500's
    eight power nodes (rail.pixhawk-telem1-5v, dev.companion, dev.camera and
    all four motors) into overlapping boxes with clipped labels.

    826x460 below is the report's own real SVG box for this contract, measured
    once with headless Chrome (`--dump-dom` on the rendered report at the
    1440x1000 window this project screenshots at) — the exact size
    `renderGraph()`'s `getBoundingClientRect()` sees. Mutation bite: reverting
    to the old fixed-band `layout(graph)` fails every assertion here.
    """
    html = render_html(load_contract(X500))
    payload = _html_data(html)
    src_graph = payload["graph"]["power"]
    layout_source = re.search(r"^function layout\(src,W,H\)\{.*?^\}", html, re.S | re.M)
    edgepath_source = re.search(r"^function edgePath\(a,b,nodes\)\{.*?^\}", html, re.S | re.M)
    assert layout_source is not None, "the layout function moved"
    assert edgepath_source is not None, "the edgePath function moved"
    script = tmp_path / "report_graph.mjs"
    script.write_text(
        REPORT_GRAPH_HARNESS.replace("SRC_GRAPH", json.dumps(src_graph))
        .replace("LAYOUT_SOURCE", layout_source.group(0))
        .replace("EDGEPATH_SOURCE", edgepath_source.group(0))
    )
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30, check=True
    )
    report = json.loads(result.stdout)
    assert report["overlaps"] == 0, f"{report['overlaps']} boxes overlap in the report's graph"
    assert report["escaped"] == 0, f"{report['escaped']} boxes fall off the report's canvas"
    assert report["crossings"] == 0, f"edges cross a stranger's box: {report['bad']}"


def test_report_has_print_stylesheet():
    """Print-to-PDF must not keep the sticky header or clip scrollable panes.

    Mutation bite: deleting the `@media print` block makes this go red.
    """
    html = render_html(load_contract(FIXTURES / "contract_clean.yaml"))
    assert "@media print" in html
    print_block = re.search(r"@media print\{.*?\}\}", html).group(0)
    assert "position:static" in print_block
    assert "overflow:visible" in print_block


def test_headline_is_a_triple():
    """The report's summary line was a pair (to fix / notes), which left
    'not checked' — the count that makes a pass conditional — invisible. Three
    named parts, and the page's own line names all three.

    Mutation bite: delete "not_checked" from `_payload`'s headline (or drop it
    from the rendered plan line) and this fails.
    """
    contract = load_contract(X500)
    html = render_html(contract)
    headline = _html_data(html)["headline"]

    assert sorted(headline) == ["adds_up", "does_not_add_up", "not_checked"]
    assert all(isinstance(value, int) for value in headline.values())
    assert headline["does_not_add_up"] == _html_data(html)["violations"]
    assert headline["not_checked"] > 0
    for phrase in (" add up ", " do not add up ", " not checked"):
        assert phrase in html


def test_report_footer_identity_in_repo():
    """Inside a repo the footer is the contract path plus the full
    commit sha (through `gitref`, the one git door), not a guess.

    Mutation bite: delete the `__DOCUMENT_IDENTITY__` replace call from
    `render_html` and this goes red (the placeholder itself would leak
    through instead of a real identity string).
    """
    contract_path = FIXTURES / "contract_demo.yaml"
    identity = report_html.document_identity(contract_path)

    assert str(contract_path) in identity
    assert "commit " in identity
    assert f"lockwire {report_html.__version__}" in identity
    assert re.search(r"generated \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", identity)

    html = render_html(load_contract(contract_path), contract_path)
    assert identity.split(" · generated ")[0] in html


def test_report_footer_identity_no_repo(tmp_path, monkeypatch):
    """Outside a repo (or with `git` unavailable) the footer degrades to a
    labelled content hash rather than raising or silently omitting identity —
    The fallback, exercised without depending on where pytest's tmp_path
    happens to sit relative to a real repo.
    """
    monkeypatch.setattr(report_html.gitref, "repo_root", lambda path: None)
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text((FIXTURES / "contract_demo.yaml").read_text())

    identity = report_html.document_identity(contract_path)

    assert "commit " not in identity
    assert re.search(r"hash [0-9a-f]{12}\b", identity)


def test_the_html_report_budget_row_carries_the_limit_raised_mark(limit_raised_contract) -> None:
    """The standing mark reaches the report — the budget row that
    would otherwise be a plain green pass says the limit was raised."""
    findings = run_record_checks(limit_raised_contract)
    rows = report_html._budget_rows(limit_raised_contract, list(findings))
    assert rows[0]["limit_raised"] == LIMIT_RAISED_MARK_TEXT
    html = render_html(limit_raised_contract)
    assert LIMIT_RAISED_MARK_TEXT in html
    assert "b.limit_raised" in html, "the row markup must render the mark, not just carry it"


def test_limit_raised_mark_survives_a_return_to_breach(
    limit_raised_then_reraised_contract,
) -> None:
    """Raised, then re-breached. The budget's
    note still carries the standing "cleared by re-declaring the limit" mark, and
    the declared draw now exceeds that same limit again, so the row is
    `state: breach`. r2 dropped the mark here on the theory that "cleared"
    beside a NO_GO on the same budget contradicts itself — but that made the
    most alarming case in the set (someone widened a limit and the robot
    breached regardless) render identically to the most ordinary one (a limit
    nobody ever touched). The mark must survive: it is the standing line,
    and the state/verdict fields beside it already carry the live truth."""
    findings = run_record_checks(limit_raised_then_reraised_contract)
    rows = report_html._budget_rows(limit_raised_then_reraised_contract, list(findings))
    assert rows[0]["state"] == "breach"
    assert rows[0]["limit_raised"] == LIMIT_RAISED_MARK_TEXT
    html = render_html(limit_raised_then_reraised_contract)
    assert LIMIT_RAISED_MARK_TEXT in html


# ------------------------------------------------------------- deliverable


def test_the_emitted_report_carries_its_identity_line():
    """The emitted document names which commit (or,
    outside a repository, which content hash) produced it — read off the
    **emitted HTML**, never a Python string the same module produced
    (the parity-anchor law).

    Mutation bite: drop `__IDENTITY__`'s substitution (or blank
    `_report_identity`) and this fails.
    """
    contract = load_contract(X500)
    html = render_html(contract)
    assert re.search(
        r"commit [0-9a-f]{12}(-dirty)?|content hash [0-9a-f]{12} \(no repository\)", html
    )
