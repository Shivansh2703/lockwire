"""The HTML report's own script, run under node with a tiny DOM stand-in (no browser)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire import report_html
from lockwire.cli import app
from lockwire.report_html import render_html
from lockwire.schema import load_contract

EXAMPLES = Path(report_html.__file__).parent / "examples"
NINE = [EXAMPLES / "servo-bench.yaml", *sorted(EXAMPLES.glob("*/contract.yaml"))]

# Enough DOM for the page script: every element records its children; each lens button is
# clicked after load and the graph's child count read back. Errors are collected, not thrown.
DOM_STUB = """
const els = {};
function mk(tag) {
  return { tag, children: [], attrs: {}, dataset: {}, style: {}, hidden: false, textContent: "",
    className: "", classList: { add() {}, toggle() {}, remove() {} },
    append(...c) { this.children.push(...c) }, replaceChildren(...c) { this.children = c },
    setAttribute(k, v) { this.attrs[k] = v }, getAttribute(k) { return this.attrs[k] ?? null },
    getBoundingClientRect() { return { width: WIDTH, height: 460 } }, querySelectorAll() { return [] } };
}
const lenses = ["mechanical", "power", "data"].map(l => { const b = mk("button"); b.dataset.lens = l; return b });
globalThis.document = { documentElement: mk("html"), getElementById: id => (els[id] = els[id] || mk("div")),
  createElement: mk, createElementNS: (_, t) => mk(t), querySelectorAll: s => (s === "[data-lens]" ? lenses : []) };
globalThis.matchMedia = () => ({ matches: false });
const errors = [], drawn = {};
try { PAGE_SCRIPT } catch (e) { errors.push(String(e)) }
for (const b of lenses) {
  try { b.onclick(); drawn[b.dataset.lens] = els.graph.children.length } catch (e) { errors.push(b.dataset.lens + ": " + e) }
}
console.log(JSON.stringify({ errors, drawn, plan: (els.plan || mk()).children.map(c => c.textContent),
  addsup: (els.addsup || mk()).children.map(c => c.textContent) }));
"""


def _run_page(html: str, tmp_path: Path, width: int) -> dict:
    script = re.search(r"<script>\n(.*?)</script>", html, re.S).group(1)
    page = tmp_path / "page.cjs"
    page.write_text(DOM_STUB.replace("WIDTH", str(width)).replace("PAGE_SCRIPT", script))
    run = subprocess.run(
        ["node", str(page)], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(run.stdout)


def _data(html: str) -> dict:
    return json.loads(html.split("const DATA=", 1)[1].split(";\nconst NS=", 1)[0])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    "lock", NINE, ids=lambda p: p.parent.name if p.stem == "contract" else p.stem
)
def test_every_example_page_loads_and_draws_every_lens_without_a_script_error(lock, tmp_path):
    """Round 3: the drone page threw `reading 'flatMap'` on load and drew an empty Power
    diagram (a group too wide for one row was pushed as a bare list). Mutation bite: put
    `rows.push(row)` back in the layout and drone (560 px) and humanoid fail here."""
    html = render_html(load_contract(lock))
    graph = _data(html)["graph"]
    for width in (560, 900):
        out = _run_page(html, tmp_path, width)
        assert out["errors"] == [], (width, out["errors"])
        for lens in ("mechanical", "power", "data"):
            assert bool(out["drawn"][lens]) == bool(graph[lens]["nodes"]), (
                width,
                lens,
                out["drawn"],
            )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_header_counts_what_check_prints(tmp_path):
    """Round 3: rover-param's check says `GO: records agree (1 scored)` while the page said
    `lockwire plan · 0 add up ...` and "Nothing scored yet." The header is check's own lines.
    Mutation bite: restore the `lockwire plan` header and this fails."""
    lock = EXAMPLES / "rover-param" / "contract.yaml"
    out = _run_page(render_html(load_contract(lock), lock), tmp_path, 900)
    check = CliRunner().invoke(app, ["check", str(lock)], env={"COLUMNS": "10000"}).output
    head, verdict_line = out["plan"][:2]
    assert head == "lockwire check · " + next(
        x for x in check.splitlines() if x.startswith("scored ")
    )
    assert verdict_line == "GO: records agree (1 scored), 1 advice (not yet sourced, outside the verdict)"
    assert verdict_line in check
    assert "Nothing scored yet." not in out["addsup"]


def test_footer_names_out_only_when_it_was_given(tmp_path):
    """Round 3: the footer said `--out '<this file>'` for a run that gave no --out.
    Mutation bite: always append the placeholder in cli.py's report and this fails."""
    lock = tmp_path / "robot.yaml"
    shutil.copy(EXAMPLES / "rover-param" / "contract.yaml", lock)
    assert CliRunner().invoke(app, ["report", str(lock)]).exit_code == 0
    assert "--out" not in _data((tmp_path / "robot-report.html").read_text())["cli_line"]
    out = tmp_path / "given.html"
    assert CliRunner().invoke(app, ["report", str(lock), "--out", str(out)]).exit_code == 0
    assert _data(out.read_text())["cli_line"].endswith("--out '<this file>'")
