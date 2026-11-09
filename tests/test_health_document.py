"""The health document (box e3-h): built once in `health.py`,
rendered by the CLI, returned by the library, served over MCP — no door
re-derives a state, a foreign-row rule or an exit code.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lockwire_pro.api_pro as api
from lockwire.cli import app
from lockwire_pro.health import (
    build_document,
    chain_last_hash,
    chain_row_hash,
    own_and_foreign,
    read_ledger,
    sidecar_path_for,
)
from lockwire.schema import load_contract

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"

runner = CliRunner()


def _scored_contract(tmp_path: Path) -> Path:
    """A contract with one real preflight run behind it, so the ledger has rows."""
    contract = tmp_path / "lockwire.yaml"
    shutil.copy(CLEAN_CONTRACT, contract)
    result = runner.invoke(app, ["preflight", str(contract), "--from", str(CLEAN_BAG)])
    assert sidecar_path_for(contract).exists(), result.output
    return contract


def test_foreign_rows_are_history_not_state(tmp_path):
    """Rows scored against another contract sha are counted as history and never
    become a device's current state — decided by `own_and_foreign` alone."""
    contract = _scored_contract(tmp_path)
    ledger = sidecar_path_for(contract)
    rows = read_ledger(ledger)
    assert rows

    # Re-stamp every row with a foreign sha: the ledger still has rows, but none
    # of them belongs to this contract version.
    lines = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row["contract_sha"] = "deadbeefcafe"
        lines.append(json.dumps(row))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    document = build_document(contract, load_contract(contract), now=dt.datetime.now(dt.UTC))
    assert document.foreign_rows == len(rows)
    assert document.summary["per_device"] == {}
    for entry in document.devices:
        assert entry["state"] == "not_watched", entry
        assert entry["reason"].startswith("(other contract)")
    # The predicate itself, asked directly: the split is by contract_sha, not by
    # tool, source or run.
    own, foreign = own_and_foreign(rows, contract)
    assert len(own) == len(rows) and foreign == []
    other_contract = tmp_path / "other.yaml"
    shutil.copy(CLEAN_CONTRACT, other_contract)
    other_contract.write_text(
        other_contract.read_text(encoding="utf-8") + "\n# a different contract, a different sha\n",
        encoding="utf-8",
    )
    assert own_and_foreign(rows, other_contract) == ([], rows)


def test_nothing_compared_is_not_scored_exit_3(tmp_path):
    """The presence floor lives in the document: an empty ledger is NOT SCORED
    with exit 3, never GO with exit 0 — and the CLI exits with that same code."""
    contract = tmp_path / "lockwire.yaml"
    shutil.copy(CLEAN_CONTRACT, contract)

    document = build_document(contract, load_contract(contract), now=dt.datetime.now(dt.UTC))
    assert document.scored == 0
    assert document.verdict == "NOT_SCORED"
    assert document.exit_code == 3

    result = runner.invoke(app, ["health", str(contract)])
    assert result.exit_code == 3, result.output
    assert "NOT SCORED" in result.output


def test_cli_health_renders_the_document(tmp_path):
    """The CLI prints the document's fields and exits with the document's exit
    code — it computes no scored count, no state and no verdict of its own."""
    contract = _scored_contract(tmp_path)
    document = build_document(contract, load_contract(contract), now=dt.datetime.now(dt.UTC))
    # Wide enough that rich's table does not ellipsise the device ids.
    result = runner.invoke(app, ["health", str(contract)], env={"COLUMNS": "200"})

    assert result.exit_code == document.exit_code, result.output
    assert f"{document.not_watched} part(s) not watched" in result.output
    for entry in document.devices:
        assert entry["id"] in result.output
    assert f"scored {document.scored}" in result.output or document.scored == 0

    # The claim is about the whole command body, not one printed string: the CLI
    # must not name any of the deciding helpers itself.
    body = next(
        node
        for node in ast.parse(Path(api.__file__).with_name("cli_pro.py").read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "health"
    )
    names = {n.id for n in ast.walk(body) if isinstance(n, ast.Name)}
    assert names.isdisjoint(
        {"read_ledger", "latest_by_device", "device_state", "own_and_foreign", "chain_break"}
    ), f"`lockwire health` re-derives the document: {names}"


def test_api_health_document_matches_cli(tmp_path):
    """The library returns the same document the CLI rendered, JSON-clean, and
    its `summary` is what `health --json` writes, under the same envelope
    every `--json` writer uses (E0-25)."""
    contract = _scored_contract(tmp_path)
    out = tmp_path / "health.json"
    result = runner.invoke(app, ["health", str(contract), "--json", str(out)])

    document = api.health_document(contract)
    assert json.loads(json.dumps(document)) == document  # JSON-clean, no objects
    assert document["exit_code"] == result.exit_code
    written = json.loads(out.read_text())
    assert written["artifact_type"] == "lockwire-health-report"
    body = {
        k: v
        for k, v in written.items()
        if k not in {"artifact_type", "format_version", "schema_version"}
    }
    assert body == json.loads(json.dumps(document["summary"]))
    assert document["chain"]["ok"] is True


def test_mcp_health_tool_returns_the_document(tmp_path):
    """MCP's `health` tool is the library's document plus `cli_line` — nothing
    else. Bitten from the library side: replacing `api.health_document` makes
    the tool return the replacement, which is only possible if it routes there."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp.client import Client

    import lockwire.mcp_server as mod

    contract = _scored_contract(tmp_path)

    async def call(name: str, args: dict) -> dict:
        async with Client(mod.build_server()) as client:
            return (await client.call_tool(name, args)).structured_content

    real = asyncio.run(call("health", {"contract": str(contract)}))
    assert real["verdict"] == api.health_document(contract)["verdict"]
    assert real["cli_line"] == f"lockwire health {contract}"

    original = api.health_document
    try:
        api.health_document = lambda contract, **kw: {"verdict": "SENTINEL"}
        assert asyncio.run(call("health", {"contract": str(contract)}))["verdict"] == "SENTINEL"
    finally:
        api.health_document = original


def test_only_one_place_decides_which_ledger_rows_are_current(tmp_path):
    """`own_and_foreign` is the only comparison against `contract_sha` in `src/`.

    The whole claim, not one caller: any second filter anywhere under `src/`
    fails this, which is the bug this box exists to prevent (two doors reading
    the same ledger and disagreeing about a foreign row).
    """
    src_dirs = [Path(api.__file__).parent, Path(api.__file__).parents[1] / "lockwire"]

    def compares_contract_sha(node: ast.Compare) -> bool:
        return any(
            isinstance(attribute, ast.Attribute) and attribute.attr == "contract_sha"
            for attribute in ast.walk(node)
        )

    # Mutation-bite the selector, not a longer field with the same prefix.
    selector = ast.parse("row.contract_sha == sha").body[0].value
    prediction = ast.parse("prediction.contract_sha256 != contract_sha256").body[0].value
    assert isinstance(selector, ast.Compare) and compares_contract_sha(selector)
    assert isinstance(prediction, ast.Compare) and not compares_contract_sha(prediction)

    offenders = []
    for path in sorted(p for d in src_dirs for p in d.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if compares_contract_sha(node) and path.name != "health.py":
                offenders.append(f"{path.name}: {ast.unparse(node)}")
    assert offenders == [], f"a second foreign-row rule: {offenders}"


def test_skip_count_surfaces_instead_of_a_corroborating_lie(tmp_path):
    """E3-A4-r2 — the bounce this box exists to fix.

    A garbage-timestamp last row is dropped by `read_ledger` (valid JSON,
    valid `HealthRow`, unparseable `run`) but is still a legitimately-hashed
    ledger LINE, so the chain-verify walk (which reads raw bytes, not
    `HealthRow`s) sees it and reports no break. Before this box, the CLI's
    "N rows, no breaks" line, `chain["rows"]`, `document.summary["skipped"]`
    and `api.health`/the GUI/the MCP payload all had no way to say a row was
    silently discarded — the state shown looked current when it was the
    previous genuine row. This asserts every one of those now carries the
    count."""
    contract = _scored_contract(tmp_path)
    ledger = sidecar_path_for(contract)
    good_rows = read_ledger(ledger)
    garbage = json.loads(good_rows[-1].model_dump_json())
    garbage["run"] = "not-a-timestamp"
    # A real chain-valid append (like `append_health_rows` writes): the hash
    # covers the row's own bytes, so a merely schema-invalid `run` does not
    # by itself break the chain — that IS the scenario this box fixes (a
    # row that is chain-intact yet must not be read as current).
    prev = chain_last_hash(ledger)
    garbage["prev"] = prev
    garbage["hash"] = chain_row_hash(garbage, prev)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(garbage) + "\n")

    document = build_document(contract, load_contract(contract), now=dt.datetime.now(dt.UTC))

    # The chain walk still sees the extra (garbage-timestamp) line and finds
    # no break — that is a true statement about the hash chain, not the bug.
    assert document.chain["rows"] == len(good_rows) + 1
    assert document.chain["ok"] is True
    # What was missing: the row `read_ledger` silently dropped must show up
    # somewhere the operator can see it, right beside the "no breaks" claim.
    assert document.chain["skipped"] == 1
    assert document.summary["skipped"] == 1

    # The CLI's rendered line: was "N rows, no breaks" (a corroborating lie
    # over a discarded row); must now name the skip.
    result = runner.invoke(app, ["health", str(contract), "--verify"])
    assert "1 skipped" in result.output, result.output

    # The library door and the MCP door read the same document.
    lib_doc = api.health_document(contract)
    assert lib_doc["chain"]["skipped"] == 1
    lib_summary = api.health(contract)
    assert lib_summary["skipped"] == 1
