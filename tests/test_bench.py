"""`lockwire bench`: detection (via `plan`'s own evaluator) and forwarding scored
separately, on the corpus at tests/fixtures/bench/corpus.yaml."""

from pathlib import Path

from typer.testing import CliRunner

from lockwire.bench import (
    DEFAULT_CORPUS_PATH,
    BenchCase,
    format_report,
    load_corpus,
    run_bench,
    score_and_forward,
)
from lockwire.cli import app
from lockwire.plan import AssertionOutcome, PlanReport, validate_plan
from lockwire.schema import load_contract

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"
X500 = FIXTURES / "x500" / "contract.yaml"


def _cases() -> list[BenchCase]:
    return load_corpus(DEFAULT_CORPUS_PATH)


def _case(case_id: str) -> BenchCase:
    return next(c for c in _cases() if c.id == case_id)


def test_valid_violating_command_is_nogo_and_forwards():
    contract = load_contract(X500)
    case = _case("pm02-overload-small")
    v, forwarded = score_and_forward(contract, case.step)
    assert v.code == "NO_GO"
    assert v.exit_code == 1
    # Byte-identical, unaltered: forwarding never depends on the verdict.
    assert forwarded.model_dump_json() == case.step.model_dump_json()


def test_unscorable_command_is_not_scored_and_forwards():
    contract = load_contract(X500)
    case = _case("unknown-rail")
    v, forwarded = score_and_forward(contract, case.step)
    assert v.code == "NOT_SCORED"
    assert v.exit_code == 3
    assert forwarded.model_dump_json() == case.step.model_dump_json()


def test_detection_categorizes_missed_false_alarm_unscorable():
    contract = load_contract(X500)
    result, _sink = run_bench(contract, _cases())
    # The corpus is 2 violating / 2 compliant / 1 unscorable, and the real
    # evaluator scores every one correctly — each bucket is a distinct field,
    # not one undifferentiated count (mutation bite: merge them and these
    # exact per-bucket values stop matching).
    assert result.detected == 2
    assert result.missed == 0
    assert result.false_alarms == 0
    assert result.correct_pass == 2
    assert result.unscorable_correct == 1
    assert result.unscorable_mismatch == 0
    assert (
        result.detected
        + result.missed
        + result.false_alarms
        + result.correct_pass
        + result.unscorable_correct
        + result.unscorable_mismatch
        == result.corpus_size
    )


def test_forwarding_scored_bytes_order_count_regardless_of_verdict():
    contract = load_contract(X500)
    cases = _cases()
    result, sink = run_bench(contract, cases)
    assert result.forwarded == len(cases) == len(sink)
    # In order, once each, byte-identical — spanning violating, compliant and
    # unscorable cases, so forwarding provably does not depend on the verdict.
    for case, forwarded in zip(cases, sink, strict=True):
        assert forwarded.model_dump_json() == case.step.model_dump_json()


def test_seeded_detection_hole_raises_missed_count_and_exits_nonzero():
    contract = load_contract(X500)
    cases = _cases()

    def hole_evaluator(c, plan):
        step = plan.steps[0]
        # Seed a real detection hole on one named violating case: score it as
        # if it passed, exactly the shape a broken/regressed check produces.
        if getattr(step, "rail", None) == "rail.pm02-5v" and step.draw.value == 0.1:
            return PlanReport(
                [], [AssertionOutcome(path="plan/steps/0/rail_load/draw", status="PASS")]
            )
        return validate_plan(c, plan)

    result, _sink = run_bench(contract, cases, evaluator=hole_evaluator)
    assert result.missed == 1
    assert result.exit_code != 0


def test_checks_deleted_detection_rate_collapses():
    """The self-proving mutation an evaluator that always passes
    (checks deleted) must make the detection rate collapse — a bench that
    scores well against a gate that does nothing is measuring itself."""
    contract = load_contract(X500)
    cases = _cases()

    def always_pass(c, plan):
        return PlanReport([], [AssertionOutcome(path="plan/steps/0", status="PASS")])

    real_result, _ = run_bench(contract, cases)
    stubbed_result, _ = run_bench(contract, cases, evaluator=always_pass)

    assert real_result.detection_rate == 1.0
    assert stubbed_result.detection_rate == 0.0
    assert stubbed_result.missed == real_result.violating_total


def test_bench_publishes_detection_rate():
    contract = load_contract(X500)
    result, _sink = run_bench(contract, _cases())
    report = format_report(result, DEFAULT_CORPUS_PATH)
    assert "detection rate" in report
    assert "100%" in report
    assert "ceiling" in report  # the self-authored-corpus limitation, in the output itself

    cli_result = runner.invoke(app, ["bench", str(X500)])
    assert cli_result.exit_code == 0, cli_result.stdout
    assert "detection rate" in cli_result.stdout
