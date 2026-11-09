"""Deterministic measurement of the stacked baseline gate (stacked-gate measurement).

Measurement ONLY — no threshold, margin, budget, or check-behavior changes.

The gate under test is ``lockwire_pro.baseline.check_recording``: every Mechanism
family together on one recording. That is the same composed path B45's
``scripts/calibrate_fp_budget.py`` and ``lockwire baseline-check`` use, and the
path the α_gate = 0.005 budget applies to.

B82 closes B76's honesty debt: every false-positive trial names the firing
check family and quantity path (plus the draw seed), and the report includes
a per-family attribution table (zeros included).

Note on naming: ``lockwire check`` is the *record-level* contract gate
(``run_record_checks``). It does not share this false-alarm budget or the
clean-fixture / noise-model machinery. This harness measures the stacked
*baseline/preflight* gate that B44/B45/B60 were measured against individually.

External anchors only: ``ALPHA_GATE`` (0.005) and planted-fault ground truth.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType

import numpy as np
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lockwire_pro.baseline import BaselineArtifact, BaselineFinding, build_baseline, check_recording
from lockwire_pro.recording import DeviceStream, Recording, load_recording
from lockwire.schema import Band
from lockwire_pro.stats.clusters import ALPHA_GATE
from scripts.calibrate_fp_budget import CONFIDENCE, one_sided_binomial_upper

GENERATOR_PATH = REPO_ROOT / "tests" / "fixtures" / "bags" / "generate.py"
BAGS = REPO_ROOT / "tests" / "fixtures" / "bags"

# Offset keeps the baseline seed out of the draw-seed window for any practical N.
DRAW_SEED_OFFSET = 1_000_000
DRIFT_BASELINE_SEED_OFFSET = 2_000_000
WILKS_BASELINE_SEED_OFFSET = 2_000_100
WILKS_DRAW_SEED_OFFSET = 2_000_101

MECHANISM_FAMILIES = (
    "cusum",
    "rate-check",
    "hard-limit-exceedance",
    "condition-envelope",
    "drift-cusum",
    "wilks-band-exceedance",
)


@dataclass(frozen=True, slots=True)
class AttributedFinding:
    """One finding on a false-positive clean trial.

    ``BaselineFinding`` has no severity field on the stacked baseline path; attribution
    is mechanism (check family) + channels (quantity path).
    """

    mechanism: str
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FalsePositiveTrial:
    """One clean draw that produced findings, named for reproduction."""

    seed: int
    findings: tuple[AttributedFinding, ...]


@dataclass(frozen=True, slots=True)
class StackedFalseAlarmResult:
    trials: int
    false_positives: int
    total_findings: int
    proportion: float
    confidence: float
    upper_bound: float
    baseline_seed: int
    draw_seed: int
    false_positive_trials: tuple[FalsePositiveTrial, ...]
    family_false_fires: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class PlantedCatchRow:
    family: str
    construction: str
    seed_note: str
    caught: bool
    finding_mechanisms: tuple[str, ...]
    finding_channels: tuple[str, ...]


def _format_attributed_findings(findings: tuple[AttributedFinding, ...]) -> str:
    if not findings:
        return "(none)"
    return "; ".join(f"{finding.mechanism}[{','.join(finding.channels)}]" for finding in findings)


def _family_false_fire_counts(
    trials: tuple[FalsePositiveTrial, ...],
) -> tuple[tuple[str, int], ...]:
    """Count FP trials that implicated each family (at most one per trial per family)."""
    counts = {family: 0 for family in MECHANISM_FAMILIES}
    for trial in trials:
        implicated = {finding.mechanism for finding in trial.findings}
        for family in implicated:
            if family in counts:
                counts[family] += 1
    return tuple((family, counts[family]) for family in MECHANISM_FAMILIES)


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lockwire_bag_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import generator at {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _manifest(bag_id: str) -> dict:
    return YAML(typ="safe").load((BAGS / bag_id / "manifest.yaml").read_text())


def _authored_limits_from_planted_bags() -> tuple[
    dict[tuple[str, str], Band],
    dict[str, Band],
    dict[str, float],
]:
    """Mirror tests/test_baseline.py::_fallbacks_from_ground_truth limit extraction."""
    signal_limits: dict[tuple[str, str], Band] = {}
    condition_limits: dict[str, Band] = {}
    nominal_rates: dict[str, float] = {}

    for path in sorted(BAGS.iterdir()):
        if not path.is_dir() or path.name in {"clean", "clean_b"}:
            continue
        failure = _manifest(path.name)["planted_failures"][0]
        for target in failure["targets"]:
            channel = target["channel"]
            reference = target["magnitude"]["reference"]
            unit = {"degC": "C", "Pa": "hPa"}.get(reference["unit"], reference["unit"])
            value = reference["value"] * (0.01 if reference["unit"] == "Pa" else 1.0)
            if channel["kind"] == "environment":
                condition_limits[channel["name"]] = Band.model_construct(
                    lo=-math.inf, hi=value, unit=unit
                )
            elif failure["type"] == "range-excursion":
                signal_limits[(channel["device"], channel["name"])] = Band.model_construct(
                    lo=-math.inf,
                    hi=value,
                    unit=unit,
                )
            elif failure["type"] == "rate-collapse":
                nominal_rates[channel["device"]] = reference["value"]

    return signal_limits, condition_limits, nominal_rates


def _signal_recording(values: np.ndarray, *, sample_rate_hz: float = 200.0) -> Recording:
    """Same single-channel shape as tests/test_baseline.py::_signal_recording."""
    samples = np.asarray(values, dtype=float)
    return Recording(
        context="bench-idle",
        units={"dev.sensor": {"value": "m"}},
        devices={
            "dev.sensor": DeviceStream(
                device="dev.sensor",
                recv_times=(np.arange(samples.size) / sample_rate_hz).tolist(),
                signals={"value": samples.tolist()},
            )
        },
        env={},
        sha256=f"stacked-measure-{samples.size}-{sample_rate_hz}",
        path=Path("<stacked-measure-signal>"),
    )


def _finding_summary(
    findings: list[BaselineFinding],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    mechanisms = tuple(finding.mechanism for finding in findings)
    channels = tuple(",".join(finding.channels) for finding in findings)
    return mechanisms, channels


def measure_stacked_false_alarms(
    *,
    trials: int,
    baseline_seed: int,
    draw_seed: int,
) -> StackedFalseAlarmResult:
    """Run the full stacked gate over independent clean draws (B45 calibrator path)."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    if baseline_seed >= draw_seed and baseline_seed < draw_seed + trials:
        raise ValueError("baseline seed must be independent of all draw seeds")

    generator = _load_generator()
    baseline_recording = generator.clean_recording(baseline_seed)
    baseline = build_baseline(baseline_recording, context=baseline_recording.context)

    false_positives = 0
    total_findings = 0
    fp_trials: list[FalsePositiveTrial] = []
    for seed in range(draw_seed, draw_seed + trials):
        findings = check_recording(generator.clean_recording(seed), baseline)
        total_findings += len(findings)
        if findings:
            false_positives += 1
            fp_trials.append(
                FalsePositiveTrial(
                    seed=seed,
                    findings=tuple(
                        AttributedFinding(
                            mechanism=finding.mechanism,
                            channels=finding.channels,
                        )
                        for finding in findings
                    ),
                )
            )

    named_trials = tuple(fp_trials)
    return StackedFalseAlarmResult(
        trials=trials,
        false_positives=false_positives,
        total_findings=total_findings,
        proportion=false_positives / trials,
        confidence=CONFIDENCE,
        upper_bound=one_sided_binomial_upper(false_positives, trials),
        baseline_seed=baseline_seed,
        draw_seed=draw_seed,
        false_positive_trials=named_trials,
        family_false_fires=_family_false_fire_counts(named_trials),
    )


def _bag_baseline() -> BaselineArtifact:
    recording = load_recording(BAGS / "clean")
    return build_baseline(recording, context=recording.context)


def measure_planted_catch(*, seed: int) -> list[PlantedCatchRow]:
    """One representative planted fault per Mechanism family; stacked gate each time."""
    signal_limits, condition_limits, nominal_rates = _authored_limits_from_planted_bags()
    bag_baseline = _bag_baseline()
    rows: list[PlantedCatchRow] = []

    bag_cases: tuple[tuple[str, str, dict], ...] = (
        ("cusum", "frozen_but_publishing", {}),
        (
            "rate-check",
            "rate_collapse",
            {"nominal_rates_hz": nominal_rates},
        ),
        (
            "hard-limit-exceedance",
            "range_excursion",
            {"signal_hard_limits": signal_limits},
        ),
        (
            "condition-envelope",
            "condition_envelope_exit",
            {"condition_hard_limits": condition_limits},
        ),
    )
    for family, bag_id, kwargs in bag_cases:
        findings = check_recording(load_recording(BAGS / bag_id), bag_baseline, **kwargs)
        mechanisms, channels = _finding_summary(findings)
        rows.append(
            PlantedCatchRow(
                family=family,
                construction=f"bags/{bag_id}",
                seed_note=f"checked-in bag (manifest seed={_manifest(bag_id)['seed']})",
                caught=family in mechanisms,
                finding_mechanisms=mechanisms,
                finding_channels=channels,
            )
        )

    # Drift plant: tests/test_baseline.py::test_planted_slow_drift_is_named_...
    drift_seed = seed + DRIFT_BASELINE_SEED_OFFSET
    drift_noise = np.random.default_rng(drift_seed).normal(scale=0.2, size=1200)
    drift_baseline = build_baseline(_signal_recording(drift_noise), context="bench-idle")
    drift_planted = drift_noise + np.linspace(-8.0, 8.0, drift_noise.size)
    drift_findings = check_recording(_signal_recording(drift_planted), drift_baseline)
    drift_mechanisms, drift_channels = _finding_summary(drift_findings)
    rows.append(
        PlantedCatchRow(
            family="drift-cusum",
            construction="white-noise + linear ramp (suite plant)",
            seed_note=f"baseline/draw rng seed={drift_seed}",
            caught="drift-cusum" in drift_mechanisms,
            finding_mechanisms=drift_mechanisms,
            finding_channels=drift_channels,
        )
    )

    # Wilks plant: inflated residual population (suite white-noise shape; scale=3
    # trips wilks-band-exceedance without first firing drift-cusum).
    wilks_baseline_seed = seed + WILKS_BASELINE_SEED_OFFSET
    wilks_draw_seed = seed + WILKS_DRAW_SEED_OFFSET
    wilks_baseline_noise = np.random.default_rng(wilks_baseline_seed).normal(size=1200)
    wilks_baseline = build_baseline(
        _signal_recording(wilks_baseline_noise),
        context="bench-idle",
    )
    wilks_planted = np.random.default_rng(wilks_draw_seed).normal(scale=3.0, size=1200)
    wilks_findings = check_recording(_signal_recording(wilks_planted), wilks_baseline)
    wilks_mechanisms, wilks_channels = _finding_summary(wilks_findings)
    rows.append(
        PlantedCatchRow(
            family="wilks-band-exceedance",
            construction="harness-constructed (isolation-tuned; ×3)",
            seed_note=(f"baseline rng seed={wilks_baseline_seed}; draw rng seed={wilks_draw_seed}"),
            caught="wilks-band-exceedance" in wilks_mechanisms,
            finding_mechanisms=wilks_mechanisms,
            finding_channels=wilks_channels,
        )
    )

    missing = set(MECHANISM_FAMILIES) - {row.family for row in rows}
    if missing:
        raise RuntimeError(f"catch table missing families: {sorted(missing)}")
    return rows


def format_plain(
    fp: StackedFalseAlarmResult,
    catches: list[PlantedCatchRow],
    *,
    seed: int,
) -> str:
    lines = [
        "=== B76 stacked-gate measurement (plain) ===",
        f"master_seed={seed}",
        f"baseline_seed={fp.baseline_seed}",
        f"draw_seed={fp.draw_seed} .. {fp.draw_seed + fp.trials - 1}",
        f"alpha_gate={ALPHA_GATE:.6f} (external budget constant)",
        "",
        "stacked false-alarm rate:",
        (
            f"  {fp.false_positives}/{fp.trials} = {fp.proportion:.6f}; "
            f"{fp.confidence:.0%} one-sided exact binomial CI "
            f"[0.000000, {fp.upper_bound:.6f}]; "
            f"budget={ALPHA_GATE:.6f}; findings={fp.total_findings}"
        ),
        (
            f"  vs budget: upper_bound={fp.upper_bound:.6f} "
            f"{'<= ' if fp.upper_bound <= ALPHA_GATE else '> '}"
            f"alpha_gate={ALPHA_GATE:.6f}"
            + (
                ""
                if fp.upper_bound <= ALPHA_GATE
                else " (wide CI at this N — not a certified breach; board runs full N)"
            )
        ),
        "",
        "false-positive attribution (by check family):",
        f"{'family':<24} {'false_fires':>11}",
        "-" * 36,
    ]
    for family, count in fp.family_false_fires:
        lines.append(f"{family:<24} {count:>11}")
    lines.extend(
        [
            "",
            "false-positive trials (named findings; reproduce via draw seed):",
        ]
    )
    if not fp.false_positive_trials:
        lines.append("  (none)")
    else:
        for trial in fp.false_positive_trials:
            lines.append(f"  seed={trial.seed}: {_format_attributed_findings(trial.findings)}")
    lines.extend(
        [
            "",
            "planted-fault catch table:",
            (f"{'family':<24} {'caught':<8} {'construction':<42} {'findings':<40} seed_note"),
            "-" * 140,
        ]
    )
    for row in catches:
        findings = (
            "; ".join(
                f"{mechanism}[{channels}]"
                for mechanism, channels in zip(
                    row.finding_mechanisms,
                    row.finding_channels,
                    strict=True,
                )
            )
            or "(none)"
        )
        lines.append(
            f"{row.family:<24} {'yes' if row.caught else 'NO':<8} "
            f"{row.construction:<42} {findings:<40} {row.seed_note}"
        )
    lines.append("")
    caught_n = sum(row.caught for row in catches)
    lines.append(f"catch summary: {caught_n}/{len(catches)} families caught")
    return "\n".join(lines)


def _report_date(seed: int, measured_on: date | None) -> date:
    """Prefer an explicit date; else parse YYYYMMDD seeds; else refuse wall-clock drift."""
    if measured_on is not None:
        return measured_on
    text = str(seed)
    if len(text) != 8 or not text.isdigit():
        raise ValueError(
            "pass measured_on=... or use a YYYYMMDD --seed so the dated markdown is deterministic"
        )
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def format_markdown(
    fp: StackedFalseAlarmResult,
    catches: list[PlantedCatchRow],
    *,
    seed: int,
    measured_on: date | None = None,
) -> str:
    report_date = _report_date(seed, measured_on)
    if fp.upper_bound <= ALPHA_GATE:
        vs = "within"
    else:
        vs = "upper > α_gate (CI width; certify at full N)"
    lines = [
        f"## B76 stacked-gate measurement — {report_date.isoformat()}",
        "",
        "Measurement only. Gate path: `check_recording` (stacked Mechanism families).",
        "",
        f"- master seed: `{seed}`",
        f"- baseline seed: `{fp.baseline_seed}`",
        f"- draw seeds: `{fp.draw_seed}` .. `{fp.draw_seed + fp.trials - 1}` ({fp.trials} trials)",
        f"- budget anchor: `ALPHA_GATE = {ALPHA_GATE:.6f}`",
        "",
        "### Stacked false-alarm rate",
        "",
        (
            f"| trials | false positives | proportion | "
            f"{fp.confidence:.0%} upper bound | α_gate | vs budget |"
        ),
        "| ---: | ---: | ---: | ---: | ---: | --- |",
        (
            f"| {fp.trials} | {fp.false_positives} | {fp.proportion:.6f} | "
            f"{fp.upper_bound:.6f} | {ALPHA_GATE:.6f} | {vs} |"
        ),
        "",
        (
            f"Narrative line (B45 style): measured clean false-positive proportion "
            f"{fp.false_positives}/{fp.trials} = {fp.proportion:.6f}; "
            f"{fp.confidence:.0%} one-sided exact binomial CI "
            f"[0.000000, {fp.upper_bound:.6f}]; gate alpha={ALPHA_GATE:.6f}; "
            f"findings={fp.total_findings}."
        ),
        "",
        "### False-positive attribution",
        "",
        "| family | false fires |",
        "| --- | ---: |",
    ]
    for family, count in fp.family_false_fires:
        lines.append(f"| `{family}` | {count} |")
    lines.extend(
        [
            "",
            "Per-trial findings (reproduce via draw seed; mechanism[channels]):",
            "",
            "| draw seed | findings |",
            "| ---: | --- |",
        ]
    )
    if not fp.false_positive_trials:
        lines.append("| _(none)_ | — |")
    else:
        for trial in fp.false_positive_trials:
            lines.append(f"| `{trial.seed}` | {_format_attributed_findings(trial.findings)} |")
    lines.extend(
        [
            "",
            "### Planted-fault catch table",
            "",
            "| family | caught | construction | findings | seed note |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in catches:
        findings = (
            "; ".join(
                f"{mechanism}[{channels}]"
                for mechanism, channels in zip(
                    row.finding_mechanisms,
                    row.finding_channels,
                    strict=True,
                )
            )
            or "(none)"
        )
        lines.append(
            f"| `{row.family}` | {'yes' if row.caught else '**NO**'} | "
            f"{row.construction} | {findings} | {row.seed_note} |"
        )
    caught_n = sum(row.caught for row in catches)
    lines.extend(
        [
            "",
            f"Catch summary: **{caught_n}/{len(catches)}** families caught under the stacked gate.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=int,
        default=20260726,
        help="master seed (baseline_seed=seed; draw_seed=seed+1_000_000)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=50,
        help="number of independent clean draws for the stacked false-alarm rate",
    )
    parser.add_argument(
        "--baseline-seed",
        type=int,
        default=None,
        help="override baseline seed (default: --seed)",
    )
    parser.add_argument(
        "--draw-seed",
        type=int,
        default=None,
        help="override first draw seed (default: --seed + 1_000_000)",
    )
    parser.add_argument(
        "--markdown-only",
        action="store_true",
        help="emit only the pasteable markdown block",
    )
    args = parser.parse_args(argv)

    baseline_seed = args.seed if args.baseline_seed is None else args.baseline_seed
    draw_seed = args.seed + DRAW_SEED_OFFSET if args.draw_seed is None else args.draw_seed

    fp = measure_stacked_false_alarms(
        trials=args.n,
        baseline_seed=baseline_seed,
        draw_seed=draw_seed,
    )
    catches = measure_planted_catch(seed=args.seed)

    if args.markdown_only:
        print(format_markdown(fp, catches, seed=args.seed))
    else:
        print(format_plain(fp, catches, seed=args.seed))
        print()
        print("=== pasteable markdown ===")
        print(format_markdown(fp, catches, seed=args.seed))

    # Measurement harness always exits 0 — numbers go in the report; findings are boxes.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
