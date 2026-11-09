"""`lockwire check --format github`: annotations and the step summary CI shows on the PR."""

from pathlib import Path

from ruamel.yaml import YAML
from typer.testing import CliRunner

from lockwire.cli import app

ROOT = Path(__file__).parent.parent
NO_GO = "tests/fixtures/contract_clean.yaml"


def test_github_format_annotates_and_summarises(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    result = CliRunner().invoke(app, ["check", NO_GO, "--format", "github"])
    assert result.exit_code == 1, result.output
    errors = [line for line in result.output.splitlines() if line.startswith("::error ")]
    assert errors == [line for line in errors if "file=tests/fixtures/testbot.urdf::" in line]
    assert errors[0].startswith("::error file=tests/fixtures/testbot.urdf::model/structure_hash: ")
    assert "; run: lockwire start " in errors[0]
    assert "::notice::8 check(s) not compared" in result.output
    text = summary.read_text()
    assert text.startswith("## Lockwire: NO-GO")
    assert "| violation | model/structure_hash |" in text
    assert "- devices/dev.gnss/physical/mass: mass not declared" in text


def test_plain_format_writes_no_annotations(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    result = CliRunner().invoke(app, ["check", str(ROOT / NO_GO)])
    assert "::error" not in result.output and not (tmp_path / "s.md").exists()


def test_action_passes_github_format_and_adopt_never_fails():
    action = YAML(typ="safe").load((ROOT / "action.yml").read_text())
    assert {"adopt", "evidence", "evidence-root", "package-root"} <= set(action["inputs"])
    run = next(s["run"] for s in action["runs"]["steps"] if s.get("name") == "lockwire check")
    assert "--format github" in run
    assert "code=${PIPESTATUS[0]}" in run and 'if [ "$LW_ADOPT" = "true" ]' in run
    adopt = run.split('if [ "$LW_ADOPT" = "true" ]', 1)[1].split("\nfi", 1)[0]
    assert "::notice::adopt mode" in adopt and "exit 0" in adopt
    assert "adopt mode: lockwire could not run (exit 2)" in adopt


def test_contract_finding_points_at_its_lockfile_line(monkeypatch, tmp_path):
    from lockwire.report import Finding, github_output

    monkeypatch.chdir(tmp_path)
    (tmp_path / "l.yaml").write_text("devices:\n  dev.imu:\n    physical:\n      mass: 1\n")
    f = Finding(
        severity="warning",
        quantity="devices/dev.imu/physical/mass",
        observed="1 kg",
        expected="2 kg",
        action="fix",
    )
    assert github_output([f], tmp_path / "l.yaml")[0].startswith("::warning file=l.yaml,line=4::")


def test_github_summary_says_when_strict_failed_a_go_with_notes(tmp_path):
    from lockwire.report import Finding, github_summary

    f = Finding(severity="warning", quantity="q", observed="o", expected="e", action="fix")
    assert "turned this into a failure" in github_summary([f], 1)
    assert "turned this into a failure" not in github_summary([f])


def test_github_format_works_in_model_file_mode(monkeypatch):
    monkeypatch.chdir(ROOT)
    args = ["check", "tests/fixtures/so101/so101.urdf", "--format", "github", "--mass", "0.7 kg"]
    result = CliRunner().invoke(app, [*args, "--source", "x"])
    assert result.exit_code == 1, result.output
    assert "::error file=tests/fixtures/so101/so101.urdf::" in result.output


def test_baseline_fails_only_new_findings_and_adopt_prefix_never_fails(monkeypatch, tmp_path):
    import shutil

    shutil.copy(ROOT / "tests/fixtures/turtlebot3/turtlebot3_waffle_pi.urdf", tmp_path / "r.urdf")
    monkeypatch.chdir(tmp_path)
    run = CliRunner().invoke
    run(app, ["start", "r.urdf", "--no-check"])
    assert run(app, ["check", "--write-baseline", "b.json"]).exit_code == 1  # datasheet gap
    quiet = run(app, ["check", "--baseline", "b.json", "--format", "github"])
    assert quiet.exit_code == 0 and quiet.output.startswith("GO — no new findings since b.json")
    assert len(quiet.output.splitlines()) == 1  # no annotations for baselined findings
    urdf = tmp_path / "r.urdf"
    urdf.write_text(
        urdf.read_text().replace('<mass value="1.3729096e+00"/>', '<mass value="1.5"/>')
    )
    edited = run(app, ["check", "--baseline", "b.json", "--format", "github"])
    assert (
        edited.exit_code == 1 and "::error file=r.urdf::model/links/base_link/mass" in edited.output
    )
    # Lock drift can't be adopted, baselined or accepted: only a re-lock clears it.
    adopted = run(app, ["check", "--adopt", "datasheet/,model/"])
    assert adopted.exit_code == 1 and "without them: NO-GO" in adopted.output
    assert run(app, ["check", "--write-baseline", "b2.json"]).exit_code == 1
    assert run(app, ["check", "--baseline", "b2.json"]).exit_code == 1
    accept = ["accept", "model/links/base_link/mass", "--reason", "r", "--witness", "w"]
    refused = run(app, accept)
    assert refused.exit_code == 2 and "drift between the model and its lock" in refused.output


def test_joint_edit_after_lock_is_drift_too(monkeypatch, tmp_path):
    urdf = tmp_path / "r.urdf"
    src = (ROOT / "tests/fixtures/turtlebot3/turtlebot3_waffle_pi.urdf").read_text()
    urdf.write_text(src)
    monkeypatch.chdir(tmp_path)
    run = CliRunner().invoke
    run(app, ["start", "r.urdf", "--no-check"])
    urdf.write_text(src.replace('xyz="0.0 0.144 0.023"', 'xyz="0.0 0.150 0.023"'))
    assert run(app, ["check", "--adopt", "datasheet/,model/"]).exit_code == 1
    assert run(app, ["check", "--write-baseline", "b.json"]).exit_code == 1
    assert run(app, ["check", "--baseline", "b.json"]).exit_code == 1
    accept = ["accept", "model/structure_hash", "--reason", "r", "--witness", "w"]
    assert run(app, accept).exit_code == 2


def test_action_maps_model_mass_baseline_and_adopt_list_to_check_flags(tmp_path):
    import os
    import subprocess

    action = YAML(typ="safe").load((ROOT / "action.yml").read_text())
    run = next(s["run"] for s in action["runs"]["steps"] if s.get("name") == "lockwire check")
    stub = tmp_path / "lockwire"
    stub.write_text("#!/bin/sh\nprintf '[%s]' \"$@\"\n")
    stub.chmod(0o755)
    inputs = {"MODEL": "r.urdf", "MASS": "1.8 kg", "SOURCE": "sheet", "TOLERANCE_PCT": "3"}
    inputs |= {"BASELINE": "b.json", "ADOPT": "datasheet/", "CONTRACT": "lockwire.yaml"}
    env = {f"LW_{k}": v for k, v in inputs.items()} | {"RUNNER_TEMP": str(tmp_path)}
    env["PATH"] = f"{tmp_path}:{os.environ['PATH']}"
    out = subprocess.run(["bash", "-c", run], env=env, capture_output=True, text=True).stdout
    assert out == (
        "[check][r.urdf][--format][github][--mass][1.8 kg][--source][sheet]"
        "[--tolerance-pct][3][--baseline][b.json][--adopt][datasheet/]"
    )
