import json
from pathlib import Path

from typer.testing import CliRunner

from lockwire_pro.preflight import report_html
from lockwire.cli import app
from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.recording import load_recording
from lockwire.report import Finding, verdict as real_verdict
from lockwire_pro.preflight.report_html import render_preflight_html
from lockwire.schema import Quantity, load_contract

from conftest import plain

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT_PATH = FIXTURES / "contract_clean.yaml"
BAGS = FIXTURES / "bags"


def _run(bag: str) -> tuple:
    contract = load_contract(CONTRACT_PATH)
    source = RecordedSource(load_recording(BAGS / bag))
    return contract, run_preflight(contract, source)


def _data(html: str) -> dict:
    payload = html.split('<script id="lockwire-data" type="application/json">', 1)[1].split(
        "</script>", 1
    )[0]
    return json.loads(payload)


def test_preflight_html_verdict_is_sourced_from_report_verdict(monkeypatch):
    """Same requirement as the record page: `render_preflight_html` must decide
    GO/NO-GO/NOT_SCORED via `report.verdict`, not a local re-derivation
    plus a bolted-on scored==0 special case.

    Mutation bite: reverting to the local `"NO-GO" if violations else (...)`
    derivation stops calling `verdict` and this fails (call count stays 0).
    """
    contract, findings = _run("clean")
    calls = []

    def spy(f):
        calls.append(f)
        return real_verdict(f)

    monkeypatch.setattr(report_html, "verdict", spy, raising=False)
    render_preflight_html(contract, findings, str(BAGS / "clean"))
    assert calls, "render_preflight_html never called report.verdict()"


def test_clean_page_keeps_all_twenty_not_scored_disclosures_first_class():
    """3 physical-less devices disclose as not_scored for mass instead of a
    silent skip, raising this count from 15 to 18. The `commands` stage's two standing disclosures
    take it to 20."""
    contract, findings = _run("clean")
    html = render_preflight_html(contract, findings, str(BAGS / "clean"))
    data = _data(html)

    assert len(data["findings"]) > 0  # Presence floor: no vacuous clean-page claim.
    # a mass budget with an unknown mass is not scored, so one more disclosure (20 -> 21)
    assert len(data["findings"]) == 21
    assert data["scored"] == findings.scored
    assert data["scored"] > 0  # Presence floor beside the zero-violation assertion.
    assert not any(row["severity"] == "violation" for row in data["findings"])
    assert all(row["severity"] == "not_scored" for row in data["findings"])
    assert data["violations"] == 0
    assert data["warnings"] == 0
    assert data["not_scored"] == 21
    assert f"{data['scored']} checks scored; 21 could not score — listed below." in html
    assert f"<b>{data['scored']}</b><span>scored checks</span>" in html
    assert "<span>violations</span>" in html
    assert "<span>warnings</span>" in html
    assert "<span>not-scored disclosures</span>" in html
    assert html.count('class="finding not_scored"') == 21
    assert html.count('data-severity="not_scored"') == 21
    assert "<details" not in html
    assert ".finding{min-width:0;padding:16px" in html
    assert ".finding.violation{border-left-color:var(--bad)" in html
    assert ".finding.not_scored{border-left-color:var(--gap)" in html


def test_empty_findings_show_silent_gate_not_confident_go():
    contract = load_contract(CONTRACT_PATH)

    html = render_preflight_html(contract, [], "empty-source")
    data = _data(html)

    assert data["silent_gate"] is True
    assert data["scored"] is None
    assert data["not_scored"] == 0
    assert "SILENT GATE" in html
    assert "The gate emitted nothing." in html
    assert "Do not trust a silent gate." in html
    assert ">GO<" not in html
    assert 'class="verdict caution"' in html


def test_five_stages_render_in_order_with_finding_counts():
    contract, findings = _run("clean")

    html = render_preflight_html(contract, findings, str(BAGS / "clean"))
    data = _data(html)

    assert [row["stage"] for row in data["stages"]] == [0, 1, 2, 3, 4]
    # Stage 4 re-stamps every record-check finding, so its
    # 3 new physical-less-device mass disclosures land there: 6 -> 9.
    # The `commands` stage is stage 4 too: 9 -> 11.
    # bud.mass.devices (unknown masses) is one more stage-4 disclosure: 11 -> 12.
    assert [row["not_scored"] for row in data["stages"]] == [1, 0, 2, 6, 12]
    # contract-only budget rows no longer count at stage 4: 5 -> 2.
    assert [row["scored"] for row in data["stages"]] == [1, 3, 6, 16, 2]
    assert sum(row["scored"] for row in data["stages"]) == data["scored"] == 28
    positions = [html.index(f'<span class="stage-num">{stage}</span>') for stage in range(5)]
    assert positions == sorted(positions)
    assert all(
        {"scored", "violations", "warnings", "not_scored"} <= row.keys() for row in data["stages"]
    )
    assert "scored / violations / warnings / not-scored disclosures" in html


def test_typed_magnitudes_remain_visibly_distinct_at_small_scale():
    contract = load_contract(CONTRACT_PATH)
    finding = Finding(
        severity="violation",
        quantity="devices/dev.imu/recv_gap",
        observed="0.01 s",
        expected="<= 0.01 s at 200 Hz cadence",
        observed_quantity=Quantity(value=0.0054104139999999745, unit="s"),
        expected_quantity=Quantity(value=0.005, unit="s"),
        owner="controls",
        dependent="src/imu.cpp",
        action="inspect receive jitter",
        stage=2,
    )

    html = render_preflight_html(contract, [finding], "precision-bite")
    row = _data(html)["findings"][0]

    assert row["precise_observed"] != row["precise_expected"]
    assert row["precise_observed"] == "0.00541 s"
    assert row["precise_expected"] == "0.005 s"
    assert "measured</b>0.00541 s" in html
    assert "threshold</b>0.005 s" in html


def test_condition_nogo_shows_both_axes_bands_and_routing():
    contract, findings = _run("condition_envelope_exit")

    html = render_preflight_html(
        contract,
        findings,
        str(BAGS / "condition_envelope_exit"),
    )
    data = _data(html)
    violation = next(row for row in data["findings"] if row["severity"] == "violation")

    assert data["verdict"] == "NO-GO"
    assert data["violations"] == 1
    assert violation["stage"] == 4
    assert "temp_c: 45.00 C" in html
    assert "condition_altitude_m: 1800.00 m" in html
    assert "temp_c in [0, 40] C" in html
    assert "condition_altitude_m in [0, 300] m" in html
    assert violation["owner"] in html
    assert violation["dependent"] in html


def test_page_is_deterministic_self_contained_and_states_offline_boundary():
    contract, findings = _run("clean")

    first = render_preflight_html(contract, findings, str(BAGS / "clean"))
    second = render_preflight_html(contract, findings, str(BAGS / "clean"))

    assert first == second
    assert '<meta charset="utf-8">' in first
    assert '<meta name="viewport"' in first
    assert "http://" not in first
    assert "https://" not in first
    assert 'href="docs/gate_coverage.md"' not in first
    assert first.count("docs/gate_coverage.md") == 2
    assert f"{findings.scored} checks scored" in first
    assert "It is not powered-on robot clearance." in first
    assert "live hardware↔software reconciliation" in first
    assert "comm links under representative load" in first


def test_user_text_matching_template_marker_does_not_corrupt_payload():
    contract, findings = _run("clean")
    contract.robot.name = "__VERDICT__"

    html = render_preflight_html(contract, findings, "__ROBOT__")
    data = _data(html)

    assert data["robot"] == "__VERDICT__"
    assert data["recording"] == "__ROBOT__"


def test_preflight_html_option_writes_same_run_and_preserves_exit_code(tmp_path):
    out = tmp_path / "preflight.html"
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_PATH),
            "--from",
            str(BAGS / "clean"),
            "--html",
            str(out),
            "--no-ledger",
        ],
    )

    assert result.exit_code == 0, result.output
    assert out.is_file()
    flat = " ".join(plain(result.output).split())
    assert "GO" in flat
    assert "wrote " in flat
    assert "preflight.html" in flat
    # The stage-4 `commands` stage always
    # discloses two not_scored facts — no command record beside this recording, and
    # commands inside the bag itself being unscorable: 18 -> 20.
    assert _data(out.read_text())["not_scored"] == 21
    assert _data(out.read_text())["scored"] == 28


def _raise_html_write_error(*_args) -> None:
    raise OSError("simulated write failure")


def test_preflight_html_write_failure_preserves_nogo_exit(monkeypatch, tmp_path):
    monkeypatch.setattr("lockwire_pro.cli_pro.write_preflight_html", _raise_html_write_error)
    out = tmp_path / "preflight.html"

    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_PATH),
            "--from",
            str(BAGS / "condition_envelope_exit"),
            "--html",
            str(out),
            "--no-ledger",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "NO-GO" in plain(result.output)
    assert "could not write" in plain(result.output)
    assert "preflight.html: simulated write failure" in plain(result.output)
    assert "wrote " not in plain(result.output)


def test_preflight_html_write_failure_makes_go_a_tool_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("lockwire_pro.cli_pro.write_preflight_html", _raise_html_write_error)
    out = tmp_path / "preflight.html"

    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(CONTRACT_PATH),
            "--from",
            str(BAGS / "clean"),
            "--html",
            str(out),
            "--no-ledger",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "GO" in plain(result.output)
    assert "could not write" in plain(result.output)
    assert "preflight.html: simulated write failure" in plain(result.output)
    assert "wrote " not in plain(result.output)
