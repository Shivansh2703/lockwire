"""The public library door, `lockwire.api`.

Four doors — CLI, MCP, the HTTP sidecar, and this module — must answer with one
document computed once. These tests hold three things: the document a
caller gets is the same one `--json` writes, importing the library does not drag
in a terminal or a web server, and every door routes through the library rather
than re-deriving a verdict beside it.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES
from typer.testing import CliRunner

runner = CliRunner()

X500 = FIXTURES / "x500" / "contract.yaml"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"

#: The nine names the plan asks the library to carry, in its own order.
NINE = ("load", "check", "plan", "describe", "preflight", "health", "diff", "log", "blame")


def test_library_import_pulls_no_server_or_cli_deps():
    """`import lockwire.api` must not reach typer, rich, fastapi, uvicorn or mcp.

    Run in a fresh interpreter on purpose: this process has already imported the
    CLI, so an in-process `sys.modules` check would pass no matter what the
    library imports.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, lockwire.api;"
            "bad=[m for m in ('typer','rich','fastapi','uvicorn','mcp') if m in sys.modules];"
            "print('CLEAN' if not bad else 'LEAKED '+str(bad));"
            "sys.exit(1 if bad else 0)",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "CLEAN", proc.stdout + proc.stderr
    assert proc.returncode == 0


REVIEW_FACT = "devices/dev.companion.jetson-orin-nano/power/draw"
REVIEW_EXPECTED = FIXTURES / "review_note_expected.yaml"


def test_mcp_tools_call_the_library_not_checks_directly(monkeypatch):
    """MCP's document is the library's document plus `cli_line` — nothing else.

    Bitten from the library side: replacing `api.check` makes the MCP tool return
    the replacement, which is only possible if the tool routes through it.
    """
    pytest.importorskip("mcp")
    import asyncio

    from mcp.client import Client

    import lockwire.mcp_server as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for banned in ("run_record_checks", "preflight_document", "run_preflight", "verdict("):
        assert banned not in source, (
            f"mcp_server re-derives {banned} instead of calling the library"
        )

    def sentinel_check(loaded, *, contract_path):
        assert contract_path == str(X500)
        return {"verdict": "SENTINEL"}

    monkeypatch.setattr(mod.api, "check", sentinel_check)

    async def run():
        async with Client(mod.build_server()) as client:
            return await client.call_tool("check_contract", {"contract": str(X500)})

    result = asyncio.run(run())
    assert result.structured_content["verdict"] == "SENTINEL"
    assert result.structured_content["cli_line"] == f"lockwire check {X500}"
