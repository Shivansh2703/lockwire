import os
import re
from pathlib import Path

import pytest

# Neutralise ambient colour env vars at import time (not a fixture): CLI modules
# build their rich/click consoles at import, so a fixture would run too late.
# FORCE_COLOR/CLICOLOR_FORCE make rich/click inject ANSI into CliRunner's
# captured output regardless of NO_COLOR, which breaks plain-text assertions
# whenever the suite runs under a colour-forcing shell (e.g. agent sessions).
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("CLICOLOR_FORCE", None)
os.environ["NO_COLOR"] = "1"
# Typer forces a terminal (ANSI box borders) when GITHUB_ACTIONS or PY_COLORS is set, so
# tests that parse the help panel saw no commands in CI only. Its own off switch:
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

# Pin terminal width so rich does not wrap phrase assertions at the default 80
# columns when running under xdist with long tmpdir paths (/popen-gwN/ tails).
# Same cause as FORCE_COLOR above: environment changing rich's rendering,
# breaking plain-text assertions.
os.environ["COLUMNS"] = "200"

from lockwire.schema import Contract, load_contract

FIXTURES = Path(__file__).parent / "fixtures"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True, scope="session")
def _isolate_sidecar_log_dir(tmp_path_factory):
    """serve.py's ``_log_dir()`` honours ``LOCKWIRE_LOG_DIR`` as a test-only escape
    hatch. Set it here, once, before any test — including the ones that Popen a
    real ``lockwire serve`` subprocess and so inherit this env var — otherwise the
    suite writes into the developer's and CI's real ``~/Library/Logs`` (bounce,
    measured writing into a real machine's log folder on every run)."""
    os.environ["LOCKWIRE_LOG_DIR"] = str(tmp_path_factory.mktemp("lockwire-logs"))


def strip_ansi(output: str) -> str:
    """Colour codes removed, line structure INTACT.

    Use this when an assertion scopes itself with a layout boundary — a blank-line
    paragraph split, say — that ``plain()`` would collapse away and thereby widen
    the assertion to match anywhere in the output.
    """
    return _ANSI.sub("", output)


def plain(output: str) -> str:
    """Console text as a human reads it: no colour codes, no layout-driven line breaks."""
    return " ".join(strip_ansi(output).split())


@pytest.fixture
def clean_contract() -> Contract:
    return load_contract(FIXTURES / "contract_clean.yaml")


@pytest.fixture
def empty_budget_contract(tmp_path: Path) -> Contract:
    """One rail, one budget on it, no device on the
    rail and nothing else in the contract — so the rail budget has zero
    contributing witnesses AND it is the only thing that could ever score.
    Reproduces "an empty budget reads as a pass" without any other check's
    scored comparisons masking the zero denominator.
    """
    path = tmp_path / "contract_empty_budget.yaml"
    path.write_text(
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
    return load_contract(path)


@pytest.fixture
def empty_endurance_budget_contract(tmp_path: Path) -> Contract:
    """A battery and an
    endurance budget, no device declaring power draw — the budget has zero
    declared draw AND is the only thing that could ever score.
    """
    path = tmp_path / "contract_empty_endurance_budget.yaml"
    path.write_text(
        'schema_version: "0.3"\n'
        "robot:\n"
        "  name: emptyendurancebot\n"
        "topology:\n"
        "  battery:\n"
        "    capacity: {value: 74, unit: Wh}\n"
        "budgets:\n"
        "  - id: bud.endurance.empty\n"
        "    kind: endurance\n"
        "    limit: {value: 15, unit: min}\n"
        "    owner: shivansh\n"
    )
    return load_contract(path)


@pytest.fixture
def testbot_urdf() -> Path:
    return FIXTURES / "testbot.urdf"


# A budget whose limit was raised to clear a violation carries a
# standing mark in its provenance note. One fixture, read by all three surfaces
# the design names (the GUI card, the HTML report, the preflight JSON), so the
# mark cannot be asserted in three slightly different spellings.
LIMIT_RAISED_MARK_TEXT = "cleared by re-declaring the limit on 2026-09-08, witness human"


def _limit_raised_yaml(mark: str, draw_w: float = 12) -> str:
    return (
        'schema_version: "0.3"\n'
        "robot:\n"
        "  name: markbot\n"
        "families:\n"
        "  - id: fam.compute\n"
        "    name: Companion computer\n"
        "device_models:\n"
        "  - id: mdl.compute.generic\n"
        "    family: fam.compute\n"
        "topology:\n"
        "  rails:\n"
        "    - id: rail.5v\n"
        "      voltage: {value: 5, unit: V}\n"
        "      capacity: {value: 30, unit: W}\n"
        "devices:\n"
        "  - id: dev.jetson\n"
        "    model: mdl.compute.generic\n"
        "    owner: shivansh\n"
        "    power:\n"
        "      rail: rail.5v\n"
        f"      draw: {{value: {draw_w}, unit: W}}\n"
        "budgets:\n"
        "  - id: bud.power.5v\n"
        "    kind: rail_power\n"
        "    rail: rail.5v\n"
        "    limit: {value: 20, unit: W}\n"
        "    owner: shivansh\n"
        "    provenance:\n"
        "      witness: human\n"
        "      source: test\n"
        "      locked_at: 2026-09-08\n"
        f'      note: "{mark}"\n'
    )


@pytest.fixture
def limit_raised_contract(tmp_path: Path) -> Contract:
    """A passing rail budget whose limit carries the standing mark."""
    path = tmp_path / "contract_limit_raised.yaml"
    path.write_text(_limit_raised_yaml(LIMIT_RAISED_MARK_TEXT))
    return load_contract(path)


@pytest.fixture
def limit_raised_then_reraised_contract(tmp_path: Path) -> Contract:
    """A budget carries the standing "cleared by re-declaring the
    limit" mark in its provenance note, but the declared draw (25 W) now
    exceeds that same 20 W limit again — the limit was raised, then the load
    rose back past it. The budget is CURRENTLY in breach even though its note
    still reads "cleared". The mark survives the re-breach (rendered in both
    report_html and report_json) because the mark grants no severity condition:
    the mark documents that a limit was raised, regardless of whether the
    robot currently passes or breaches. Distinguishing "raised then breached
    anyway" from ordinary breaches is important.
    """
    path = tmp_path / "contract_limit_raised_then_reraised.yaml"
    path.write_text(_limit_raised_yaml(LIMIT_RAISED_MARK_TEXT, draw_w=25))
    return load_contract(path)
