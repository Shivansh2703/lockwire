"""A check that throws inside the served engine is a refusal (exit 2, no verdict) that says
it is a bug in lockwire, never a verdict, for every command the app reaches through dispatch."""

from pathlib import Path

import pytest

from conftest import FIXTURES
from lockwire import checks, plan, serve
from lockwire.edit import contract_revision


def _body(command: str, tmp_path: Path):
    lock = tmp_path / "contract.yaml"
    lock.write_bytes((FIXTURES / "contract_clean.yaml").read_bytes())
    (tmp_path / "testbot.urdf").write_bytes((FIXTURES / "testbot.urdf").read_bytes())
    plan = tmp_path / "plan.yaml"
    plan.write_text("steps:\n  - kind: rail_load\n    rail: rail.5v\n    draw: {value: 1, unit: W}\n")
    args = {
        "check": {"contract": str(lock)},
        "page": {"contract": str(lock)},
        "plan": {"contract": str(lock), "plan": str(plan)},
        "what-if": {
            "contract": str(lock),
            "expected_revision": contract_revision(lock),
            "change": {"path": "topology/rails/rail.5v/capacity", "value": "1 W", "source": "docs/datasheet.pdf", "witness": "Maya Chen"},  # fmt: skip
        },
        "suggest-fixes": {"contract": str(lock), "finding": "topology/rails/rail.5v/capacity"},
    }[command]
    return serve.commands()[command].command.model_validate({"command": command, "args": args})


@pytest.mark.parametrize("error", [NameError("x is not defined"), ZeroDivisionError("division by zero"), KeyError("k"), TypeError("t"), ValueError("v"), OSError("/Users/someone/secret.yaml")])  # fmt: skip
@pytest.mark.parametrize("command", ["check", "page", "what-if", "plan", "suggest-fixes"])
def test_a_crashing_check_is_a_refusal_never_a_verdict(command, error, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(checks, "check_tip_over", boom)  # check, page, what-if, suggest-fixes run it
    monkeypatch.setattr(plan, "power_rollup", boom)  # plan has its own scorer
    reply = serve.dispatch(_body(command, tmp_path))
    print("REPLY", command, type(error).__name__, reply.exit_code, reply.verdict, repr(reply.refusal), None if reply.payload is None else list(reply.payload))  # noqa: T201
    assert reply.exit_code == 2 and reply.verdict is None and reply.payload is None
    assert reply.refusal == f"lockwire stopped: {type(error).__name__}; this is a bug in lockwire, not a verdict"  # the real name, never CheckCrashed  # fmt: skip
    assert "/Users" not in reply.refusal


@pytest.mark.parametrize("error", [KeyError("k"), ValueError("/Users/someone/x"), RuntimeError("r")])
def test_a_bug_building_the_checks_report_is_exit_2_not_a_load_refusal(error, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr("lockwire.api.check_document", boom)
    reply = serve.dispatch(_body("check", tmp_path))
    assert (reply.exit_code, reply.verdict, reply.payload) == (2, None, None)
    assert reply.refusal == f"lockwire stopped: {type(error).__name__}; this is a bug in lockwire, not a verdict"  # fmt: skip


def test_a_real_load_problem_keeps_its_plain_reason_and_exit_1(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("robot: [unclosed\n")
    for lock in (bad, tmp_path / "missing.yaml"):
        body = {"command": "check", "args": {"contract": str(lock)}}
        reply = serve.dispatch(serve.commands()["check"].command.model_validate(body))
        assert reply.exit_code == 1 and reply.verdict is None and reply.refusal
        assert "a bug in lockwire" not in reply.refusal
