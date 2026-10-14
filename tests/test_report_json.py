"""The machine-readable preflight document, and the drift it exists to prevent.

`lockwire_ros`'s gate node reads this file instead of scraping rendered prose.
The load-bearing test here is `test_json_verdict_never_disagrees_with_the_terminal`:
if the JSON and the terminal could ever reach different verdicts, a robot would
be cleared by one while the operator read the other.
"""

from __future__ import annotations
from pathlib import Path
import pytest
from rich.console import Console
from typer.testing import CliRunner
from lockwire.checks import run_record_checks
from lockwire.report import render, verdict
from lockwire.report_json import (
    envelope,
    preflight_document,
)
from conftest import LIMIT_RAISED_MARK_TEXT


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
BAGS = FIXTURES / "bags"

# Every committed contract crossed with every committed bag. The parity claim is
# only worth something if it is made over the whole verdict space the suite has,
# not over one happy fixture.
CONTRACTS = sorted(p.name for p in FIXTURES.glob("contract_*.yaml"))
BAG_NAMES = sorted(p.name for p in BAGS.iterdir() if p.is_dir())


def _rendered_exit_code(findings) -> int:
    """The exit code a human's terminal actually produced, via the real renderer."""
    return render(findings, Console(file=open("/dev/null", "w"), force_terminal=False))


# ---------------------------------------------------------------------------
# the envelope every machine door leads with.
# ---------------------------------------------------------------------------

#: Spelled out here on purpose rather than imported from ``report_json``. Every
#: door shares one implementation (``envelope``), so a test that also read its
#: key names from it would have both legs inside the thing under test: renaming
#: the header there and in ``HEADER_KEYS`` would stay green while every consumer
#: broke. This literal is the leg anchored outside.
HEADER = frozenset({"artifact_type", "format_version", "schema_version"})

X500 = FIXTURES / "x500" / "contract.yaml"


@pytest.mark.parametrize("key", sorted(HEADER))
def test_envelope_refuses_a_body_key_that_would_shadow_the_header(key):
    """A colliding body key is refused, not silently merged over.

    The body is merged flat behind the header, which is what makes this
    dangerous: without the guard, `envelope("x", {"format_version": "99"})`
    returns a document whose version is the body's and whose consumers have no
    way to tell. This shape is frozen, so the collision is refused at the
    single point every document passes through.
    """
    with pytest.raises(ValueError, match=key):
        envelope("lockwire-test-report", {key: "shadowed", "keep": 1})


def test_the_preflight_json_carries_the_limit_raised_mark(limit_raised_contract) -> None:
    """The standing mark reaches the JSON. A cleared budget emits no
    finding, so a consumer reading only `findings` cannot see that the limit moved;
    the verdict and exit code beside it are untouched — the mark annotates, it
    never scores."""
    document = preflight_document(limit_raised_contract, [], "test")
    assert document["limits_raised"] == [{"budget": "bud.power.5v", "mark": LIMIT_RAISED_MARK_TEXT}]
    assert document["verdict"] == verdict([]).code
    assert document["exit_code"] == verdict([]).exit_code


def test_limit_raised_mark_survives_a_return_to_breach_in_json(
    limit_raised_then_reraised_contract,
) -> None:
    """A budget's limit was raised and cleared
    once, but the declared draw has since risen back past that same limit —
    the budget is CURRENTLY in breach. r2 made `limits_raised` drop the mark
    here, on the theory that "cleared" beside `verdict: NO_GO` on the same
    budget contradicts itself. That made the most alarming case in the set —
    someone widened a limit and the robot breached regardless — render
    identically to an ordinary breach on a limit nobody ever touched.
    `limits_raised` must still carry the mark: it is the standing line,
    and `verdict`/`findings` already carry the live truth beside it."""
    findings = run_record_checks(limit_raised_then_reraised_contract)
    document = preflight_document(limit_raised_then_reraised_contract, findings, "test")
    assert document["verdict"] == "NO-GO"
    assert document["limits_raised"] == [{"budget": "bud.power.5v", "mark": LIMIT_RAISED_MARK_TEXT}]
