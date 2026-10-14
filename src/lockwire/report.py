"""Record-check findings and severity-free derived information.

Finding severities:
- violation  -> no-go (exit code 1)
- warning    -> go-with-note (exit code 0)
- not_scored -> visible coverage gap; does not affect GO/NO-GO or warning counts
Every finding renders in the same grammar regardless of which check produced it.

Derived information is deliberately a separate type: it describes the locked
robot but cannot affect GO/NO-GO. In particular, CG has no defensible envelope
yet and therefore must never be represented as a Finding.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from lockwire.advice import Alternative
from lockwire.schema import Quantity, Provenance

if TYPE_CHECKING:  # rich is the terminal door's dependency, not the library's
    from rich.console import Console  # (`import lockwire.api` must not pull it)

Severity = Literal["violation", "warning", "not_scored"]
LARGE_EVIDENCE_GAP = 10
_SEVERITY_RANK = {"violation": 0, "warning": 1, "not_scored": 2}

VerdictCode = Literal["REFUSED", "NO_GO", "GO_WITH_NOTES", "GO", "NOT_SCORED"]
#: The machine verdict/exit contract. ``verdict`` consumes it and ``emit_ts``
#: exports the same values for the app boundary; human-facing help remains in
#: ``EXIT_CODE_TABLE`` below and is never parsed as protocol data.
VERDICT_EXIT_CODES: dict[VerdictCode, int] = {
    "REFUSED": 2,
    "NO_GO": 1,
    "GO_WITH_NOTES": 0,
    "GO": 0,
    "NOT_SCORED": 3,
}


def verdict_word(code: VerdictCode) -> str:
    """The terminal spelling of a verdict code, for anything a human or an
    external consumer reads as prose (the JSON document's ``verdict`` field
    included). Only ``NO_GO`` differs from its Python identifier — the
    display layer (``report_html.py``, the terminal itself) has always
    hyphenated it; this is that same spelling, applied at every JSON boundary."""
    return "NO-GO" if code == "NO_GO" else code


def _console() -> Console:
    """A default rich Console, imported at call time.

    Every rendering entry point takes an explicit ``console``; only the terminal
    path falls through to here. Importing rich at module scope would put it on
    ``import lockwire.api``'s path, which is forbidden.
    """
    from rich.console import Console

    return Console()


def format_magnitude(value: float, unit: str) -> str:
    """Canonical Finding magnitude display for humans.

    Checkers populate typed ``Quantity`` fields; this owns the prose form so
    machine consumers never have to re-parse the rendered string.
    """
    return f"{value:.2f} {unit}"


#: A `declare` that clears a violation by RAISING the limit rather
#: than lowering the draw leaves this standing line in the entry's provenance
#: note. It is an annotation only — verdict words and exit codes never move.
LIMIT_RAISED_MARK = "cleared by re-declaring the limit on "


def limit_raised_mark(provenance: object | None) -> str | None:
    """The standing mark on an entry's provenance note, if one was written.

    One reader for every surface that renders the mark (the GUI card, the HTML
    report's budget rows, the preflight JSON) so a raised limit cannot be
    visible on one screen and invisible on another.

    The mark must not be gated on the budget's CURRENT severity (returning None
    whenever the budget was back in breach, on the reasoning that "cleared by
    re-declaring the limit" beside `state: breach` reads as two contradicting
    present-tense claims). Such a gate would make the most alarming case in the
    set ("someone widened a limit and the robot breached regardless anyway")
    render identically to the most ordinary one (a limit nobody ever touched,
    in plain breach). The mark is the standing line in the file, so it renders
    whenever the provenance note carries it, regardless of current state; the
    state/verdict fields beside it already carry the live truth.
    """
    note = getattr(provenance, "note", None)
    if not note:
        return None
    marks = [
        line.strip()
        for line in str(note).splitlines()
        if line.strip().startswith(LIMIT_RAISED_MARK)
    ]
    return marks[-1] if marks else None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    quantity: str  # contract path, '/'-separated
    observed: str
    expected: str
    # Typed magnitudes when the finding is about a physical quantity. Optional:
    # many findings are structural prose (missing refs, unrated bitrate, …).
    # Rendered ``observed``/``expected`` stay for humans; these are the
    # machine-readable form.
    observed_quantity: Quantity | None = None
    expected_quantity: Quantity | None = None
    delta: str | None = None
    dependent: str | None = None  # code paths / downstream entries affected
    owner: str | None = None
    source: str | None = None
    witness: str | None = None
    locked_at: dt.date | None = None
    action: str
    cluster_root: str | None = None
    cluster_members: tuple[str, ...] = ()
    # Preflight stage identity (contract §3.2). Record-check findings leave this
    # None; render groups/labels only when set. Additive to report.py — not a
    # lockfile/schema change, no further sign-off.
    stage: int | None = None
    # The alternatives half of the advice sentence.
    # Additive to report.py, same pattern as cluster_root/stage above — not a
    # lockfile/schema change. Empty for every finding except a rail_power
    # budget violation.
    alternatives: tuple[Alternative, ...] = ()

    @model_validator(mode="after")
    def _keep_compared_magnitudes_visibly_distinct(self) -> Self:
        """Expand colliding prose magnitudes using lossless float rendering."""
        observed = self.observed_quantity
        expected = self.expected_quantity
        if (
            observed is None
            or expected is None
            or observed.unit != expected.unit
            or observed.value == expected.value
        ):
            return self

        observed_compact = format_magnitude(observed.value, observed.unit)
        expected_compact = format_magnitude(expected.value, expected.unit)
        if observed_compact != expected_compact:
            return self

        self.observed = self.observed.replace(
            observed_compact,
            f"{observed.value!r} {observed.unit}",
            1,
        )
        self.expected = self.expected.replace(
            expected_compact,
            f"{expected.value!r} {expected.unit}",
            1,
        )
        return self


class ScoredRecord(BaseModel):
    """A comparison that PASSED: two populated operands, no violation.

    Findings only ever carry problems and blind spots; this is the missing
    "it agrees" half. ``quantity`` is the contract path (exact-text toggle
    only); ``observed``/``expected``/``margin`` are already plain words for
    direct rendering — never a quantity path or code identifier.
    """

    model_config = ConfigDict(extra="forbid")

    quantity: str
    observed: str
    expected: str
    margin: str
    provenance: Provenance | None = None
    # Typed magnitudes, optional (mirrors Finding.observed_quantity/expected_quantity):
    # a preflight band/limit pass can carry the compared numbers so a reader
    # (health.py) can show the band without re-deriving it. Record checks never
    # set these — plain strings remain their whole story.
    observed_quantity: Quantity | None = None
    expected_quantity: Quantity | None = None
    uncertainty: Quantity | None = None


class CGContributor(BaseModel):
    """One mass term in the CG sum, retained so the result is explainable."""

    model_config = ConfigDict(extra="forbid")

    quantity: str
    mass_kg: float
    position_m: tuple[float, float, float] | None
    moment_kgm: tuple[float, float, float] | None


class CGReport(BaseModel):
    """Severity-free CG information; absence of ``severity`` is load-bearing."""

    model_config = ConfigDict(extra="forbid")

    quantity: str
    sources: Literal["devices", "links", "links+devices"]
    frame: str | None
    cg_m: tuple[float, float, float] | None
    complete: bool
    total_mass_kg: float
    positioned_mass_kg: float
    contributors: list[CGContributor]
    limitation: str | None = None
    # The budget's own declared total. Without it, coverage can only be measured
    # against the model — a denominator that is itself the thing in question, so
    # a lockfile holding 3% of an aeroplane reported "100% positioned" and GO.
    budget_limit_kg: float | None = None


class RecordReport(list[Finding]):
    """Findings plus a separate informational lane.

    Remaining list-like preserves the existing check API: callers filtering or
    comparing findings still see findings only, while ``render`` can display
    derived information without smuggling it into a warning.

    ``scored`` is the presence-floor denominator: how many
    checks compared populated operands and reached a pass or violation.
    ``None`` means the caller did not compute a denominator. ``scored_keys``
    names record-check declarations when set — a violation whose quantity is
    in ``scored_keys`` is a comparison (it counts toward ``scored``); a
    violation whose quantity is NOT in ``scored_keys`` (reference integrity,
    duplicate-identity, structure drift…) was never compared to a second
    source and does not. The identity ``scored == len(scored_records) +
    comparison_violations`` holds — reference/structure
    violations are reported on their own line, never folded into "do not
    add up".
    """

    information: list[CGReport]
    scored: int | None
    scored_keys: frozenset[str] | None
    scored_records: list[ScoredRecord]
    actuator_case_reports: list[
        Any
    ]  # items are actuator_case.ActuatorCaseReport; untyped so this module stays free of it

    def __init__(
        self,
        findings: list[Finding] | None = None,
        information: list[CGReport] | None = None,
        scored: int | None = None,
        scored_keys: frozenset[str] | None = None,
        scored_records: list[ScoredRecord] | None = None,
        actuator_case_reports: list[Any] | None = None,
    ) -> None:
        super().__init__(findings or [])
        self.information = information or []
        self.scored = scored
        self.scored_keys = scored_keys
        self.scored_records = scored_records or []
        self.actuator_case_reports = list(actuator_case_reports or [])


def _vector(values: tuple[float, float, float]) -> str:
    return "(" + ", ".join(f"{value:.3f}" for value in values) + ")"


def _cg_coverage(report: CGReport) -> str:
    """State what fraction of the DECLARED mass the CG was computed from.

    ``complete`` only ever meant "every contributor I have could be positioned",
    so it divides the model by itself and reads 100% on a lockfile that holds a
    fraction of the real machine. The declared budget total is the independent
    denominator, and it is the one that decides whether a CG means anything.
    """
    positioned = report.positioned_mass_kg
    placed = sum(1 for c in report.contributors if c.position_m is not None)
    tally = (
        f"all {len(report.contributors)} contributors positioned"
        if report.complete
        else f"{placed} of {len(report.contributors)} contributors positioned"
    )
    if report.budget_limit_kg is None or report.budget_limit_kg <= 0:
        return f"{positioned:.3f} kg positioned; no declared total to measure against; {tally}"
    share = positioned / report.budget_limit_kg
    return f"{positioned:.3f} of {report.budget_limit_kg:.3f} kg declared ({share:.1%}); {tally}"


def _render_cg(report: CGReport, console: Console) -> None:
    """Render CG as information, never with a judgement tag or action."""
    frame = report.frame or "unresolved root frame"
    positioned = any(contributor.position_m is not None for contributor in report.contributors)
    if report.cg_m is None and not positioned:
        count = len(report.contributors)
        console.print(
            f"[cyan]INFO[/] {report.quantity} — not computable: none of the {count} "
            f"contributors declares a mount position (sources: {report.sources})"
        )
        if report.limitation:
            console.print(f"  limitation: {report.limitation}")
        return
    if report.cg_m is None:
        result = "not computable"
    elif report.complete:
        result = f"{_vector(report.cg_m)} m in {frame}"
    else:
        result = (
            f"partial {_vector(report.cg_m)} m in {frame} "
            f"from {report.positioned_mass_kg:.3f}/{report.total_mass_kg:.3f} kg"
        )
    console.print(f"[cyan]INFO[/] {report.quantity} — {result} (sources: {report.sources})")
    if report.cg_m is not None:
        console.print(f"  coverage: {_cg_coverage(report)}")
    for contributor in report.contributors:
        if contributor.position_m is None:
            position = "position unresolved"
            moment = ""
        else:
            position = f"at {_vector(contributor.position_m)} m"
            moment = f", m·r {_vector(contributor.moment_kgm or (0.0, 0.0, 0.0))} kg·m"
        console.print(
            f"  contributor: {contributor.quantity} — "
            f"{contributor.mass_kg:.3f} kg {position}{moment}"
        )
    if report.limitation:
        console.print(f"  limitation: {report.limitation}")


def _severity_tag(severity: Severity) -> str:
    if severity == "violation":
        return "[bold red]VIOLATION[/]"
    if severity == "warning":
        return "[yellow]warning[/]"
    return "[cyan]not scored[/]"


def _presentation_groups(findings: list[Finding]) -> list[tuple[Finding, ...]]:
    """Group correlated findings for display without changing the underlying list."""
    clustered: dict[tuple[Severity, str], list[Finding]] = defaultdict(list)
    groups: list[tuple[Finding, ...]] = []
    for finding in sorted(
        findings,
        key=lambda item: (_SEVERITY_RANK[item.severity], item.quantity),
    ):
        if finding.cluster_root is None:
            groups.append((finding,))
        else:
            clustered[(finding.severity, finding.cluster_root)].append(finding)
    groups.extend(tuple(group) for group in clustered.values())
    return sorted(
        groups,
        key=lambda group: (
            _SEVERITY_RANK[group[0].severity],
            group[0].cluster_root or group[0].quantity,
        ),
    )


def _cluster_members(group: tuple[Finding, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                member
                for finding in group
                for member in (finding.cluster_members or (finding.quantity,))
            }
        )
    )


def _cluster_root_finding(group: tuple[Finding, ...]) -> Finding | None:
    """Select the finding about the physical root, independent of member sort order."""
    cluster_root = group[0].cluster_root
    if cluster_root is None:
        return None
    return next(
        (
            finding
            for finding in group
            if finding.dependent == cluster_root or cluster_root in finding.cluster_members
        ),
        None,
    )


def _render_finding_detail(
    finding: Finding,
    console: Console,
    *,
    observed_indent: str = "",
    metadata_indent: str = "  ",
) -> None:
    line = f"{observed_indent}observed {finding.observed}, expected {finding.expected}"
    if finding.delta:
        line += f" (Δ {finding.delta})"
    console.print(line)
    if finding.dependent:
        console.print(f"{metadata_indent}dependent: {finding.dependent}")
    if finding.owner:
        console.print(f"{metadata_indent}owner: {finding.owner}")
    if finding.source:
        console.print(f"{metadata_indent}source: {finding.source}")
    console.print(f"{metadata_indent}action: {finding.action}")


def _render_finding_groups(groups: list[tuple[Finding, ...]], console: Console) -> None:
    for group in groups:
        finding = group[0]
        tag = _severity_tag(finding.severity)
        members = _cluster_members(group)
        if finding.cluster_root is not None and len(members) > 1:
            console.print(
                f"{tag} {finding.cluster_root} subsystem: {len(members)} members affected"
                f" — {', '.join(members)}"
            )
            for member_finding in group:
                console.print(f"  {member_finding.quantity}:")
                _render_finding_detail(
                    member_finding,
                    console,
                    observed_indent="    ",
                    metadata_indent="    ",
                )
        else:
            console.print(f"{tag} {finding.quantity} — ", end="")
            _render_finding_detail(finding, console)


@dataclass(frozen=True, slots=True)
class Verdict:
    """The one GO/NO-GO decision, computed once and read by every consumer.

    ``render`` prints ``summary`` and returns ``exit_code``; the JSON document
    (``report_json``) reports ``code`` and these same counts. Deriving the
    verdict twice — once for a terminal and once for a machine — is how a gate
    ends up disagreeing with itself, so it is decided in exactly one place.

    ``NOT_SCORED`` is kept distinct from ``GO`` deliberately: a zero denominator
    is not clearance, and a consumer that cannot tell
    the two apart will read vacuity as health. ``NOT_SCORED`` exits **3** —
    distinct from GO 0, NO-GO 1 and REFUSED 2 — so a script can tell "nothing
    compared" from pass and fail without parsing the summary text.
    """

    code: VerdictCode
    exit_code: int
    summary: str  # rich markup, exactly as render has always printed it
    violations: int
    warnings: int
    not_scored: int
    scored: int | None


def verdict(findings: list[Finding]) -> Verdict:
    """Decide GO/NO-GO and the summary line, without printing anything."""
    violations = sum(f.severity == "violation" for f in findings)
    warnings = sum(f.severity == "warning" for f in findings)
    not_scored = sum(f.severity == "not_scored" for f in findings)
    scored = findings.scored if isinstance(findings, RecordReport) else None
    errors = getattr(findings, "errors", 0)

    def decided(code: VerdictCode, summary: str) -> Verdict:
        return Verdict(
            code=code,
            exit_code=VERDICT_EXIT_CODES[code],
            summary=summary,
            violations=violations,
            warnings=warnings,
            not_scored=not_scored,
            scored=scored,
        )

    if errors:
        return decided(
            "REFUSED",
            f"\n[bold red]REFUSED[/] — {errors} plan assertion error(s)",
        )
    if violations:
        return decided(
            "NO_GO",
            f"\n[bold red]NO-GO[/] — {violations} violation(s), {warnings} warning(s)",
        )
    # Presence floor: a zero denominator is never a pass, even
    # with a warning riding along — "GO with notes" over nothing compared
    # reads as clearance to a consumer that only checks for the word GO. This
    # must be decided before the warnings branch below, not after: box-A
    # review (b06f38d) caught a run with scored == 0 and a warning printing
    # "GO with notes", exit 0.
    #
    # ``scored`` is only ever an int here for a `RecordReport` (or subclass:
    # `PreflightReport`, `PlanReport`) instance — each sets a real count in
    # its own ``__init__``, never ``None``. ``scored`` is ``None`` only when
    # ``findings`` is a *plain* ``list[Finding]``, not a `RecordReport`:
    # `cli.baseline_findings_as_report` (a blessed-baseline diff has no
    # scored/not_scored denominator to begin with — not a "nothing compared"
    # case, there is nothing to score there ever) and `gui.build_payload`'s
    # ``everything = record_findings + run_findings`` (list concatenation
    # drops the `RecordReport`/`PreflightReport` type even though both
    # operands carry real counts — this loses information regardless of
    # whether anything was actually scored, so it is not provably a
    # nothing-compared case and is left as today's behaviour).
    if scored == 0:
        warning_phrase = f"{warnings} warning(s); " if warnings else ""
        return decided(
            "NOT_SCORED",
            f"\n[bold yellow]NOT SCORED[/] — nothing compared; {warning_phrase}"
            f"{not_scored} check(s) not scored",
        )
    missing = getattr(findings, "missing_required_evidence", [])
    if getattr(findings, "profile", "analysis") == "powered-release" and missing:
        return decided(
            "REFUSED",
            "\n[bold red]REFUSED[/] — required evidence missing: " + ", ".join(missing),
        )
    if warnings:
        return decided("GO_WITH_NOTES", f"\n[bold green]GO[/] with notes — {warnings} warning(s)")
    if scored is not None:
        if not_scored:
            return decided(
                "GO",
                f"\n[bold green]GO[/] — records agree ({scored} scored); "
                f"{not_scored} check(s) not scored",
            )
        return decided("GO", f"\n[bold green]GO[/] — records agree ({scored} scored), no findings")
    if not_scored:
        return decided(
            "GO",
            f"\n[bold green]GO[/] — records agree; {not_scored} check(s) not scored",
        )
    return decided("GO", "\n[bold green]GO[/] — records agree, no findings")


def verdict_headline(violations: int, warnings: int, unchecked: int, adds_up: int = 0) -> str:
    """The one plain-words sentence for a verdict — shared by every door
    that renders it: `gui.py`'s HTML page (`_headline`) and `api.check`'s
    `CheckPayload.headline`. Reports what `verdict` already decided; does not
    recompute a verdict itself, and takes no `VerdictCode` because the wording
    below depends only on these counts, never on the code.
    """
    if violations:
        thing = "thing" if violations == 1 else "things"
        return (
            f"Don't power it on yet - {violations} {thing} "
            f"{'does' if violations == 1 else 'do'} not add up"
        )
    if adds_up == 0:
        return "Nothing could be compared yet"
    if warnings:
        note = "note" if warnings == 1 else "notes"
        return f"Nothing stops you - {warnings} {note} worth reading"
    if unchecked:
        return "Nothing I checked disagrees"
    return "Everything I checked agrees"


# The one exit-code table: `--help` for each gated command and README's table
# (README.md:162-166) both render from this, so the two surfaces cannot drift
# apart the way they did before — `preflight --help` naming 0/1/2 while
# its own NOT_SCORED path exits 3.
EXIT_CODE_TABLE: dict[str, dict[int, str]] = {
    "upgrade": {
        0: "new 0.4 contract written; legacy file retained",
        2: "refused source revision, version, contents, or output path",
    },
    "what-if": {
        0: "comparison produced (see each side verdict)",
        2: "refused proposal or contract",
    },
    "check": {
        0: "GO / GO with notes",
        1: "NO-GO",
        2: "contract could not be found, read, or parsed",
        3: "NOT SCORED (nothing compared)",
    },
    "plan": {
        0: "GO / GO with notes",
        1: "NO-GO",
        2: "REFUSED (a plan assertion could not be evaluated), or plan/contract "
        "file could not be read or parsed",
        3: "NOT SCORED (nothing compared)",
    },
    "preflight": {
        0: "GO variants",
        1: "NO-GO",
        2: "refused run (recording or contract unusable)",
        3: "NOT SCORED (nothing compared)",
    },
    "declare": {
        0: "declared",
        2: "refused (no contract, or a bad --source/--witness/value)",
    },
    "review": {
        0: "reviewed",
        2: "refused (no contract, nothing declared there yet, or a bad --witness/--note)",
    },
    # Pose and mount edits. Same shape as `declare` — they are the same
    # writer with a narrower door — plus the refusals only these edits can hit.
    "place": {
        0: "placed",
        2: "refused (no contract, an unknown device/link, a missing/wrong --unit, "
        "or a bad --source/--witness)",
    },
    "mount-shape": {
        0: "mount shape written",
        2: "refused (no contract, unknown device, invalid geometry/mesh path, "
        "or a bad --source/--witness)",
    },
    "move": {
        0: "moved",
        2: "refused (no contract, an unknown target, a missing/wrong --unit, "
        "or a bad --source/--witness)",
    },
    "rotate": {
        0: "rotated",
        2: "refused (no contract, a device or root-link target that records no "
        "orientation, a missing/wrong --unit, or a bad --source/--witness)",
    },
    "reparent": {
        0: "re-parented",
        2: "refused (no contract, a parent that does not resolve, a cycle, an "
        "orphaned subtree, or a bad --source/--witness)",
    },
    "import": {
        2: "REFUSED (the inbound direction is refused until Anthropic publishes the "
        "standard)",
    },
    "hw": {
        0: "recorded (including honest undetermined routing)",
        2: "refused (no contract, or the event could not be recorded)",
    },
    "health": {
        0: "GO",
        1: "NO-GO (a device is faulty)",
        2: "contract could not be read",
        3: "NOT SCORED (no rows in the ledger)",
    },
    "bench": {
        0: "clean corpus run (no missed detections)",
        1: "a detection hole: at least one violating case in the corpus was not flagged",
        2: "contract or corpus file could not be found, read, or parsed",
    },
    "scene import": {
        2: "REFUSED (missing --witness/--source-person, unreadable contract or scene, "
        "or the import itself could not be built or written)",
    },
    "start": {
        0: "the contract was written (or an existing one kept); no page is written",
        1: "the URDF/SDF could not be parsed, or the contract could not be read or written",
    },
}


def exit_code_help(command: str) -> str:
    """Render `command`'s exit-code line from `EXIT_CODE_TABLE`, for `--help`."""
    codes = EXIT_CODE_TABLE[command]
    parts = " · ".join(f"{code} {meaning}" for code, meaning in sorted(codes.items()))
    return f"Exit codes: {parts}."


def render(findings: list[Finding], console: Console | None = None) -> int:
    """Print information and findings; only violation/warning affect the exit path."""
    console = console or _console()

    # stage=None findings (record checks) keep the historical unlabelled grammar.
    # Staged preflight findings are grouped and labelled by stage identity.
    # Findings lead (violations/warnings/not-scored — what someone came to see),
    # then the information lane, then the verdict — never buried under INFO noise.
    unstaged = [f for f in findings if f.stage is None]
    staged = [f for f in findings if f.stage is not None]
    _render_finding_groups(_presentation_groups(unstaged), console)
    for stage_id in sorted({f.stage for f in staged if f.stage is not None}):
        console.print(f"[bold]stage {stage_id}[/]")
        _render_finding_groups(
            _presentation_groups([f for f in staged if f.stage == stage_id]),
            console,
        )

    if isinstance(findings, RecordReport):
        for information in sorted(findings.information, key=lambda item: item.quantity):
            _render_cg(information, console)

    decision = verdict(findings)
    # Record checks only (not staged preflight): one line stating how many
    # comparisons agreed, how many didn't, and how many couldn't be scored.
    if type(findings) is RecordReport and findings.scored is not None:
        scored_keys = findings.scored_keys or frozenset()
        # only a violation that IS a comparison (its quantity was
        # actually scored against a second source) belongs in "do not add up".
        # A dangling reference or a structure drift never compared anything, so
        # it cannot have "not added up" — it is reported on its own line below.
        comparison_violations = sum(
            1
            for finding in findings
            if finding.severity == "violation" and finding.quantity in scored_keys
        )
        reference_violations = decision.violations - comparison_violations
        if findings.scored == 0:
            # A warning (an empty budget, say) is not a failed comparison — with
            # a zero denominator there is nothing to have agreed or disagreed,
            # so never render it as "do not add up".
            warning = "warning" if decision.warnings == 1 else "warnings"
            console.print(
                f"scored 0: nothing compared · {decision.warnings} {warning} · "
                f"{decision.not_scored} not scored"
            )
        else:
            adds_up = len(findings.scored_records)
            console.print(
                f"scored {findings.scored}: {adds_up} add up, {comparison_violations} do not · "
                f"{decision.not_scored} not scored"
            )
        if reference_violations:
            noun = "reference" if reference_violations == 1 else "references"
            console.print(f"{noun}: {reference_violations} broken")
    console.print(decision.summary)
    return decision.exit_code


#: The graph-layout JS shared by this page and the HTML report (report_html.py) —
#: one BFS-banded, parent-grouped, wrapping layout and one box-avoiding edge
#: router, so a graph never looks different — or overlaps — depending on which
#: page drew it. Embedded verbatim into both documents' __GRAPH_LAYOUT_JS__ slot.
GRAPH_LAYOUT_JS = """function layout(src,W,H){
  const nodes=src.nodes.map(n=>({...n})),edges=src.edges;
  const byId={};nodes.forEach(n=>byId[n.id]=n);
  /* Both lines have to fit. The sub line is monospace and is usually the longer of the
     two — sizing off the title alone puts "12.6 V . cap 1000.00 W" outside its own box
     on an ordinary rail. Ratios are the measured advances of the two faces, rounded up. */
  nodes.forEach(n=>{
    const title=n.label.length*7.2, sub=(n.sub||"").length*6.9;
    n.w=Math.max(140,Math.min(300,Math.max(title,sub)+30));n.h=52});
  const inc={};nodes.forEach(n=>inc[n.id]=0);
  edges.forEach(e=>{if(e.to in inc)inc[e.to]++});
  const lvl={};let front=nodes.filter(n=>!inc[n.id]).map(n=>n.id);
  if(!front.length&&nodes.length)front=[nodes[0].id];
  let depth=0;const seen=new Set();
  while(front.length){const next=[];front.forEach(id=>{if(seen.has(id))return;seen.add(id);
    lvl[id]=depth;edges.forEach(e=>{if(e.from===id)next.push(e.to)})});front=next;depth++}
  nodes.forEach(n=>{if(!(n.id in lvl))lvl[n.id]=depth});
  const bands={};nodes.forEach(n=>{(bands[lvl[n.id]]=bands[lvl[n.id]]||[]).push(n)});
  const keys=Object.keys(bands).map(Number).sort((a,b)=>a-b);
  const PADX=34,PADY=34,GAPX=26,NH=52,rows=[];
  /* every child's first parent, so a band can be ordered and wrapped by who feeds it */
  const parentOf={};edges.forEach(e=>{if(!(e.to in parentOf))parentOf[e.to]=e.from});
  let prevOrder=[];
  keys.forEach(k=>{
    const isRoot=k===0;
    const bandNodes=bands[k].slice();
    if(!isRoot){
      const prevIndex={};prevOrder.forEach((id,i)=>{prevIndex[id]=i});
      bandNodes.sort((a,b)=>{
        const pa=parentOf[a.id] in prevIndex?prevIndex[parentOf[a.id]]:prevOrder.length;
        const pb=parentOf[b.id] in prevIndex?prevIndex[parentOf[b.id]]:prevOrder.length;
        if(pa!==pb)return pa-pb;
        return a.label<b.label?-1:a.label>b.label?1:0});
    }
    /* group by parent: a parent's children move (and wrap) together */
    const groups=[];
    bandNodes.forEach(n=>{const p=isRoot?n.id:(n.id in parentOf?parentOf[n.id]:null);
      const last=groups[groups.length-1];
      if(last&&last.parent===p)last.nodes.push(n);else groups.push({parent:p,nodes:[n]})});
    /* wrap per group, not per band: a row holds whole groups; an over-wide group wraps itself */
    let row=[];
    groups.forEach(g=>{
      const gw=g.nodes.reduce((a,n)=>a+n.w,0)+GAPX*(g.nodes.length-1);
      if(gw>W-PADX*2){
        if(row.length){rows.push(row);row=[]}
        let sub=[],subW=0;
        g.nodes.forEach(n=>{const need=(sub.length?GAPX:0)+n.w;
          if(sub.length&&subW+need>W-PADX*2){rows.push({isRoot:isRoot,groups:[{parent:g.parent,nodes:sub}]});sub=[];subW=0}
          sub.push(n);subW+=(sub.length>1?GAPX:0)+n.w});
        if(sub.length)rows.push({isRoot:isRoot,groups:[{parent:g.parent,nodes:sub}]});
        return}
      const rowW=row.reduce((a,gr)=>a+gr.nodes.reduce((s,n)=>s+n.w,0)+GAPX*(gr.nodes.length-1),0)+
        (row.length?GAPX:0);
      if(row.length&&rowW+gw>W-PADX*2){rows.push({isRoot:isRoot,groups:row});row=[]}
      row.push(g)});
    if(row.length)rows.push({isRoot:isRoot,groups:row});
    prevOrder=bandNodes.map(n=>n.id)});
  const pitch=rows.length>1?Math.max(88,Math.min(165,(H-PADY*2-NH)/(rows.length-1))):0;
  const blockH=(rows.length-1)*pitch+NH;
  const startY=rows.length>1?Math.max(PADY,(H-blockH)/2)+NH/2:H/2;
  rows.forEach((rowInfo,ri)=>{
    const y=startY+ri*pitch,row=rowInfo.groups.flatMap(g=>g.nodes);
    if(rowInfo.isRoot){
      const sum=row.reduce((a,n)=>a+n.w,0);
      const gap=row.length>1?Math.max(GAPX,Math.min(96,(W-PADX*2-sum)/(row.length-1))):0;
      let x=(W-(sum+gap*(row.length-1)))/2;
      row.forEach(n=>{n.x=x+n.w/2;n.y=y;x+=n.w+gap});
      return}
    /* centre each group under its parent's x; shift right only to clear the group before it */
    let cursor=PADX;const placed=[];
    rowInfo.groups.forEach(g=>{
      const gw=g.nodes.reduce((a,n)=>a+n.w,0)+GAPX*(g.nodes.length-1);
      const parentNode=g.parent?byId[g.parent]:null;
      let left=parentNode&&parentNode.x!==undefined?parentNode.x-gw/2:cursor;
      left=Math.max(left,cursor);
      placed.push({g:g,left:left,gw:gw});cursor=left+gw+GAPX});
    if(cursor-GAPX>W-PADX){
      const shift=(cursor-GAPX)-(W-PADX);let c=PADX;
      placed.forEach(p=>{const left=Math.max(p.left-shift,c);p.left=left;c=left+p.gw+GAPX})}
    placed.forEach(p=>{let x=p.left;p.g.nodes.forEach(n=>{n.x=x+n.w/2;n.y=y;x+=n.w+GAPX})})});
  return {nodes:nodes,edges:edges,byId:byId};
}
/* The straight parent-to-child line skips over whatever band sits between them —
   a rail feeding a device two rows down runs straight through a sibling's box on
   the row in between. Route around it: for every row the edge's y-span crosses,
   hold the path at a clear x (outside every box on that row, widened by 6px) for
   the row's whole height, so the detour itself never grazes the box it dodges. */
function edgePath(a,b,nodes){
  const y1=a.y+a.h/2,y2=b.y-b.h/2;
  const lo=Math.min(y1,y2),hi=Math.max(y1,y2),down=y2>=y1;
  const rowsMap={};
  nodes.forEach(n=>{if(n===a||n===b)return;
    const top=n.y-n.h/2,bot=n.y+n.h/2;
    if(top<hi&&bot>lo)(rowsMap[n.y]=rowsMap[n.y]||[]).push(n)});
  const rowYs=Object.keys(rowsMap).map(Number).sort((p,q)=>down?p-q:q-p);
  const pts=[{x:a.x,y:y1}];
  rowYs.forEach(ry=>{
    const rowNodes=rowsMap[ry];
    const top=Math.min(...rowNodes.map(n=>n.y-n.h/2))-6,
      bot=Math.max(...rowNodes.map(n=>n.y+n.h/2))+6;
    const enterY=down?top:bot,exitY=down?bot:top;
    const t=(enterY-y1)/(y2-y1);
    let x=a.x+(b.x-a.x)*t,moved=true,guard=0;
    while(moved&&guard++<10){moved=false;
      rowNodes.forEach(n=>{const margin=n.w/2+6;
        if(Math.abs(x-n.x)<margin){x=x<n.x?n.x-margin:n.x+margin;moved=true}})}
    pts.push({x:x,y:enterY});pts.push({x:x,y:exitY})});
  pts.push({x:b.x,y:y2});
  let d="M"+pts[0].x+" "+pts[0].y;
  for(let i=1;i<pts.length;i++){
    const p0=pts[i-1],p1=pts[i];
    if(p0.x===p1.x){d+=" L "+p1.x+" "+p1.y;continue}
    const midY=(p0.y+p1.y)/2;
    d+=" C "+p0.x+" "+midY+", "+p1.x+" "+midY+", "+p1.x+" "+p1.y}
  return d;
}"""
