"""The shareable report draws each finding's value against its limit, self-contained."""

import re
from pathlib import Path

from typer.testing import CliRunner

from lockwire.cli import app
from tests.test_report import X500, _html_data


def test_report_html_has_the_bars_and_stays_self_contained(tmp_path: Path):
    out = tmp_path / "report.html"
    assert CliRunner().invoke(app, ["report", str(X500), "-o", str(out)]).exit_code in (0, 1, 2)
    html = out.read_text()
    assert "function barOf(f)" in html
    assert 'box.className="rbar"+' in html and "against a limit of" in html
    assert "const rb=barOf(f);if(rb)b.append(rb)" in html  # the finding row actually appends it
    assert "const rd=t=>" in html  # prose fallback for the servo-stall finding
    assert any(f.get("observed_quantity") and f.get("expected_quantity") for f in _html_data(html)["findings"])
    assert not re.search(r"""(?:src|href)=["']https?://""", html)
