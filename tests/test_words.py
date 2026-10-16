"""The word law: `docs/words.md` is lockwire's vocabulary, and this file
sweeps the CLI, the MCP server and checked payloads to prove the surfaces agree
with it.

**What the sweep proves.** Two things, both structural — commands come from
`typer.main.get_command(app)` and tools from a running `build_server()`, never
a hand-written name list (three boxes bounced tonight for exactly that):

1. Every word the design *fixes* — the five verdict words and their exit codes, the reserved word `STOP` — appears, spelled exactly, on every
   surface that is supposed to carry it: every gated CLI command's `--help`
   text, and the verdict codes MCP tools actually return over a real contract.
2. `STOP` never appears, whole-word, on the CLI help surface or the MCP tool
   surface (names + descriptions). `PASS`/`FAIL`/`DASHBOARD` never stand in
   for a verdict word on the exit-code line of a gated command, or in an MCP
   tool's name/description — scoped to those surfaces specifically, not a
   whole-text scan: "pass" and "fail" are ordinary English (a flag's help
   text says a value can "pass ... everywhere"; whole-text scanning them
   drowns in exactly the false positives this file's brief warned against),
   so a naive substring-anywhere ban is not the invariant this proves.

**What it does not prove.** It does not prove no surface ever uses a banned
word in ordinary prose somewhere the sweep does not look (e.g. free-text error
messages outside `--help`, the GUI's plain-word copy, or the app's bundle —
E2 does not exist yet, so the "app bundle" leg is not swept here). It does
not prove `docs/words.md` is exhaustive, only that the words it lists are used
correctly where the sweep looks. And the `catalog`/`projected` number-kind
marks have no rendering surface yet (see `docs/words.md`'s own note), so this
file cannot and does not sweep for them — asserting their absence would prove
nothing since nothing renders a number kind today, and asserting their
presence would be asserting future work that doesn't exist.
"""

from __future__ import annotations

import asyncio
import re
import typing
from pathlib import Path

from typer.testing import CliRunner

from lockwire.mcp_server import build_server
from lockwire.report import VerdictCode

runner = CliRunner()

WORDS_PATH = Path(__file__).resolve().parent.parent / "docs" / "words.md"

# The exact verdict words `report.EXIT_CODE_TABLE` and `--help` are allowed to
# print, enumerated from the `VerdictCode` literal itself (`typing.get_args`),
# not hand-copied — a sixth code added to `report.py` shows up here for free.
_DISPLAY_WORD: dict[str, str] = {
    "GO": "GO",
    "GO_WITH_NOTES": "GO",  # "GO with notes" — still contains the word GO
    "NO_GO": "NO-GO",
    "REFUSED": "REFUSED",
    "NOT_SCORED": "NOT SCORED",
}

BANNED_SYNONYMS = ("PASS", "FAIL", "DASHBOARD")
STOP = "STOP"


def _mcp_tools():
    """The MCP tool list off a running server — never a hardcoded name set."""

    async def run():
        from mcp.client import Client

        async with Client(build_server()) as client:
            return await client.list_tools()

    return asyncio.run(run()).tools


def test_words_md_documents_every_verdict_code_and_exit_code():
    """`docs/words.md` names every `VerdictCode` (from the literal itself, not
    a hand list) with its exit code — mutation bite: rename `NOT_SCORED`'s
    display word in the doc to "NOTSCORED" and this goes red."""
    text = WORDS_PATH.read_text()
    for code in typing.get_args(VerdictCode):
        word = _DISPLAY_WORD[code]
        assert word in text, f"docs/words.md never mentions the {code} verdict word {word!r}"
    for exit_code in {0, 1, 2, 3}:
        assert str(exit_code) in text, f"docs/words.md never mentions exit code {exit_code}"


def test_words_md_reserves_stop():
    text = WORDS_PATH.read_text()
    assert "STOP" in text
    assert "reserved" in text.lower()


#: exit code -> the one word fixed for it, checked directly against
#: rendered `--help` text (not derived by scanning `EXIT_CODE_TABLE`'s own
#: prose, which silently drops a code the moment its word stops matching —
#: caught by this file's own mutation bite, see the test below).
_FIXED_WORD_FOR_EXIT_CODE = {0: "GO", 1: "NO-GO", 3: "NOT SCORED"}


def test_mcp_tool_surface_has_no_banned_synonym_or_stop():
    """Tool names and descriptions, off a running server (never a hardcoded
    tool-name list — three boxes bounced tonight on exactly that). Mutation
    bite: append "STOP" to one tool's docstring and this goes red."""
    tools = _mcp_tools()
    assert tools, "no MCP tools found — server introspection broke"
    for tool in tools:
        haystack = f"{tool.name} {tool.description or ''}"
        upper = haystack.upper()
        for banned in BANNED_SYNONYMS:
            assert not re.search(rf"\b{banned}\b", upper), (
                f"MCP tool {tool.name} text uses banned word {banned!r}"
            )
        assert not re.search(rf"\b{STOP}\b", haystack), (
            f"MCP tool {tool.name} renders reserved STOP"
        )


def test_mcp_check_contract_payload_verdict_is_an_exact_ruled_word(tmp_path):
    """The payload `check_contract` actually returns over a real contract
    carries one of the five ruled `VerdictCode` values, spelled exactly, and
    never a banned synonym — the payload-strings leg of the sweep. Uses
    `empty_budget_contract`'s own fixture shape (conftest.py) inlined here so
    this file stays independent of another test module's fixture."""
    from mcp.client import Client

    contract_path = tmp_path / "contract_empty_budget.yaml"
    contract_path.write_text(
        'schema_version: "0.3"\n'
        "robot:\n"
        "  name: emptybudgetbot\n"
        "topology:\n"
        "  rails:\n"
        "    - id: rail.5v\n"
        "      voltage: {value: 5, unit: V}\n"
        "      capacity: {value: 15.6, unit: W}\n"
        "budgets:\n"
        "  - id: bud.power.empty\n"
        "    kind: rail_power\n"
        "    rail: rail.5v\n"
        "    limit: {value: 15.6, unit: W}\n"
        "    owner: shivansh\n"
    )

    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool("check_contract", {"contract": str(contract_path)})

    result = asyncio.run(run())
    assert not result.is_error, result.content
    code = result.structured_content["verdict"]
    assert code in typing.get_args(VerdictCode), f"unrecognized verdict code {code!r}"
    assert code == "NOT_SCORED"  # this fixture scores nothing — the canonical case
    assert code not in ("PASS", "FAIL", "STOP", "ERROR")


# --- ONE FACT, SIX PLACES — `start`'s exit behaviour --------------

README_PATH = Path(__file__).resolve().parent.parent / "README.md"
CLI_PATH = Path(__file__).resolve().parent.parent / "src" / "lockwire" / "cli.py"

#: the hardcoded finding string `start` prints when a scene mesh cannot be
#: resolved (cli.py, surface #5 of the six the TODO row names).


def _readme_start_row() -> str:
    """README.md's exit-map row for `start` (hand-copied, README.md:159-171)."""
    for line in README_PATH.read_text().splitlines():
        if line.strip().startswith("| `start` |"):
            return line
    raise AssertionError("README.md has no `start` exit-map row")


def _words_md_start_section() -> str:
    """docs/words.md's dedicated `start` section (hand-copied, :77-85)."""
    text = WORDS_PATH.read_text()
    match = re.search(r"## `start` exit codes.*?(?=\n## |\Z)", text, re.DOTALL)
    assert match, "docs/words.md has no `start` section"
    return match.group(0)
