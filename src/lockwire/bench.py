"""`lockwire bench`: score detection and forwarding separately, on a corpus.

Two measurements, never merged into one number (detection and forwarding):

1. **Detection** — for each generated out-of-bound plan, does the *existing*
   evaluator (`plan.validate_plan`, reused — this module never
   re-walks a plan) flag it? Categorized as ``detected`` / ``missed`` (a
   violating case not flagged; "leaked" is a detection
   metric only, and a correctly-forwarded violation is a detection, not a
   leak) / ``false_alarm`` (a compliant case wrongly flagged) /
   ``unscorable_correct`` / ``unscorable_mismatch``.
2. **Forwarding** — every case reaches the simulated driver byte-identical,
   in order, exactly once, *regardless of verdict* (lockwire forwards
   the command unchanged, never originates one, never alters one, never
   withholds one — lockwire has no gate to leak past; scoring forwarding on
   the verdict would build the wrong reading).

Self-proving: a corpus that scores well against an evaluator with
the checks deleted is measuring itself, not the product. `run_bench` takes
the evaluator as a parameter for exactly this reason — see
`tests/test_bench.py::test_checks_deleted_detection_rate_collapses`.

Ceiling, published in `format_report` itself, not only here: this corpus
(`tests/fixtures/bench/corpus.yaml`) is hand-authored by the same project
that wrote the checks it exercises. A bench that only scores cases it wrote
to be catchable is marketing, not measurement.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from lockwire.plan import Plan, PlanReport, Step, validate_plan
from lockwire.report import Verdict, verdict
from lockwire.schema import Contract, _load_yaml_guarded, _reject_yaml_anchors_aliases, _yaml

DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "bench" / "corpus.yaml"
)

Category = Literal["violating", "compliant", "unscorable"]
Evaluator = Callable[[Contract, Plan], PlanReport]


@dataclass(frozen=True, slots=True)
class BenchCase:
    id: str
    category: Category
    step: Step


def load_corpus(path: Path) -> list[BenchCase]:
    """Load the corpus YAML (id/category/step per case). Same guarded loader
    as `plan.load_plan` and `schema.load_contract` — a corpus file is trusted
    exactly as much as any other checked-in lockfile-shaped document."""
    with open(path, encoding="utf-8") as f:
        data = _load_yaml_guarded(_yaml(), f)
    _reject_yaml_anchors_aliases(data)
    cases = []
    for raw in data["cases"]:
        # Reuse the plan's own discriminated Step union to validate the case
        # instead of a second step parser.
        step = Plan.model_validate({"steps": [raw["step"]]}).steps[0]
        cases.append(BenchCase(id=raw["id"], category=raw["category"], step=step))
    return cases


def _score(contract: Contract, step: Step, evaluator: Evaluator) -> Verdict:
    return verdict(evaluator(contract, Plan(steps=[step])))


def _classify(case: BenchCase, v: Verdict) -> str:
    if case.category == "violating":
        return "detected" if v.code == "NO_GO" else "missed"
    if case.category == "compliant":
        return "false_alarm" if v.code == "NO_GO" else "correct_pass"
    return "unscorable_correct" if v.code == "NOT_SCORED" else "unscorable_mismatch"


def score_and_forward(
    contract: Contract, step: Step, evaluator: Evaluator = validate_plan
) -> tuple[Verdict, Step]:
    """Score one step and forward it, unaltered, to the simulated driver.

    The forward always happens: lockwire has no gate, so nothing it
    scores is ever withheld, including a step scored NO-GO or NOT SCORED.
    Returns ``(verdict, the forwarded step)``; the caller appends the
    forwarded step to its sink so byte-identity is checkable independently
    of the verdict.
    """
    forwarded = step.model_copy(deep=True)
    return _score(contract, step, evaluator), forwarded


@dataclass(frozen=True, slots=True)
class BenchResult:
    corpus_size: int
    detected: int
    missed: int
    false_alarms: int
    correct_pass: int
    unscorable_correct: int
    unscorable_mismatch: int
    forwarded: int

    @property
    def violating_total(self) -> int:
        return self.detected + self.missed

    @property
    def detection_rate(self) -> float | None:
        if self.violating_total == 0:
            return None
        return self.detected / self.violating_total

    @property
    def exit_code(self) -> int:
        # The bench tool's own exit reflects whether a hole was caught, not
        # the verdict words of the individual plans it scored (accept line).
        return 1 if self.missed else 0


_BUCKETS = (
    "detected",
    "missed",
    "false_alarm",
    "correct_pass",
    "unscorable_correct",
    "unscorable_mismatch",
)


def run_bench(
    contract: Contract, cases: list[BenchCase], evaluator: Evaluator = validate_plan
) -> tuple[BenchResult, list[Step]]:
    """Run every case: score it (detection) and forward it (always). Returns
    the tallied result plus the simulated driver's received-step sink, in
    the order the corpus was walked."""
    counts = dict.fromkeys(_BUCKETS, 0)
    sink: list[Step] = []
    for case in cases:
        v, forwarded = score_and_forward(contract, case.step, evaluator)
        sink.append(forwarded)
        counts[_classify(case, v)] += 1
    result = BenchResult(
        corpus_size=len(cases),
        detected=counts["detected"],
        missed=counts["missed"],
        false_alarms=counts["false_alarm"],
        correct_pass=counts["correct_pass"],
        unscorable_correct=counts["unscorable_correct"],
        unscorable_mismatch=counts["unscorable_mismatch"],
        forwarded=len(sink),
    )
    return result, sink


def format_report(result: BenchResult, corpus_path: Path) -> str:
    """The published detection rate, plain words — what the corpus is, how
    many cases, what a catch counts as, and the self-scoring ceiling, all in
    the output itself (not only in a docstring)."""
    rate = result.detection_rate
    rate_str = "n/a (no violating cases in the corpus)" if rate is None else f"{rate:.0%}"
    return "\n".join(
        [
            f"corpus: {corpus_path} — {result.corpus_size} hand-authored cases",
            f"detection rate: {rate_str} "
            f"({result.detected}/{result.violating_total} violating cases flagged NO-GO; "
            'a "catch" = the existing plan evaluator scores the case NO_GO)',
            f"missed (leaked — a violating case the evaluator did not flag): {result.missed}",
            f"false alarms (a compliant case wrongly flagged NO-GO): {result.false_alarms}",
            f"unscorable cases correctly NOT SCORED: {result.unscorable_correct}"
            + (
                f" ({result.unscorable_mismatch} scored when they should not have been)"
                if result.unscorable_mismatch
                else ""
            ),
            f"forwarded to the simulated driver: {result.forwarded}/{result.corpus_size}, "
            "unchanged, in order, once each, regardless of verdict (never withheld)",
            "ceiling: this corpus is hand-authored by the same project that wrote the checks "
            "it exercises; a bench that scores only cases it authored to be catchable is "
            "marketing, not an independent measurement.",
        ]
    )
