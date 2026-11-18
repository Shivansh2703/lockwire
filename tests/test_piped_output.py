"""Output piped to a file or a CI log is never hard-wrapped at 80 columns."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from lockwire import report_html

URL = "https://example.com/a/long/path/that/runs/well/past/eighty/columns/of/text/user_guide.html"


def test_piped_output_keeps_urls_and_yaml_diff_lines_whole(tmp_path):
    """Round 3: piped bodies wrapped at 80 and split URLs and the YAML diffs edits print.
    Mutation bite: drop `console.soft_wrap = not console.is_terminal` in cli.py and this fails."""
    lock = tmp_path / "l.yaml"
    shutil.copy(Path(report_html.__file__).parent / "examples/arm-power/contract.yaml", lock)
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
    env["LOCKWIRE_WITNESS_CONFIG"] = str(tmp_path / "w.yaml")
    args = ["declare", str(lock), "devices/dev.servo.shoulder/thermal/ambient", "25 C"]
    args += ["--source", URL, "--witness", "Sam Tester", "--dry-run"]
    run = subprocess.run(
        [sys.executable, "-m", "lockwire", *args], capture_output=True, text=True, env=env
    )
    assert run.returncode == 0, run.stdout + run.stderr
    hits = [line for line in run.stdout.splitlines() if "example.com" in line]
    assert hits and all(URL in line for line in hits), hits
    assert any(line.startswith("+") and URL in line for line in hits)  # the diff line, whole


def test_piped_check_keeps_not_compared_line_whole(tmp_path):
    """A 'Not scored' row with no command printed on two lines when piped.
    Mutation bite: restore `soft_wrap=" -> " in line` in report.py and this fails."""
    lock = Path(report_html.__file__).parent / "examples/arm-power/contract.yaml"
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
    run = subprocess.run(
        [sys.executable, "-m", "lockwire", "check", str(lock)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    hits = [x for x in run.stdout.splitlines() if "discharge_temp_c" in x]
    assert hits and all("window not compared" in x for x in hits), run.stdout
