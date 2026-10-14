"""Non-mutating comparisons of a contract and an edit proposal.

This is deliberately a contract-record check only.  It has no robot observations,
health writes, or simulation-validity claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from lockwire.checks import run_record_checks
from lockwire.edit import Change, propose_changes
from lockwire.report import RecordReport
from lockwire.schema import Contract, load_contract


ProvenanceState = Literal["recorded", "missing_evidence"]
ResultKind = Literal["finding", "passed_comparison"]


@dataclass(frozen=True)
class FactResult:
    """One contract-check result and only the provenance that check supplied.

    A passed record comparison currently carries no provenance fields, so it is
    explicitly marked ``missing_evidence`` rather than borrowing a source from
    an unrelated fact.
    """

    quantity: str
    kind: ResultKind
    severity: str | None
    source: str | None
    witness: str | None
    provenance_state: ProvenanceState


@dataclass(frozen=True)
class ContractCheckResult:
    """The existing record-check report plus per-result provenance states."""

    report: RecordReport
    facts: tuple[FactResult, ...]


@dataclass(frozen=True)
class RecordedHealthResult:
    """Historical health rows and the subset invalidated by a proposal.

    ``baseline`` is only pre-existing ledger evidence. ``invalidated`` names
    claims that stop supporting the proposed specification; neither field is
    derived from the fresh current/proposed record checks.
    """

    baseline: tuple[Any, ...]
    invalidated: tuple[Any, ...]


@dataclass(frozen=True)
class WhatIfResult:
    """Clearly separate current and proposed contract-check results."""

    current: ContractCheckResult
    proposed: ContractCheckResult
    recorded_health: RecordedHealthResult
    diff: str


@dataclass(frozen=True)
class _Bytes:
    exists: bool
    value: bytes | None


def _bytes(path: Path) -> _Bytes:
    return _Bytes(path.exists(), path.read_bytes() if path.exists() else None)


def _assert_unchanged(path: Path, before: _Bytes, *, name: str) -> None:
    if _bytes(path) != before:
        raise RuntimeError(f"what-if changed the original {name}")


def _facts(report: RecordReport) -> tuple[FactResult, ...]:
    facts: list[FactResult] = []
    for finding in report:
        recorded = finding.source is not None and finding.witness is not None
        facts.append(
            FactResult(
                quantity=finding.quantity,
                kind="finding",
                severity=finding.severity,
                source=finding.source,
                witness=finding.witness,
                provenance_state="recorded" if recorded else "missing_evidence",
            )
        )
    for record in report.scored_records:
        facts.append(
            FactResult(
                quantity=record.quantity,
                kind="passed_comparison",
                severity=None,
                source=None,
                witness=None,
                provenance_state="missing_evidence",
            )
        )
    return tuple(facts)


def _check(contract, contract_path: Path) -> ContractCheckResult:
    report = run_record_checks(contract, contract_path=contract_path)
    return ContractCheckResult(report=report, facts=_facts(report))


def what_if(
    contract_path: Path | str,
    changes: list[Change],
    recorded_health: Callable[[Path, Contract, Contract], tuple[Any, Any]] | None = None,
    watch: tuple[tuple[str, Path], ...] = (),
) -> WhatIfResult:
    """Compare current contract checks with a validated edit proposal.

    The edit primitive produces the proposal in memory; record checks still
    receive the original contract path so relative assets keep their real
    resolution root.  The original contract and every ``watch`` file (name, path)
    are byte-checked in ``finally``, including refusal and interruption paths.

    ``recorded_health(original, current, proposed)`` is the optional provider of
    the ledger rows and the ones the proposal invalidates; without it that part
    is empty, as it is for a contract with no health ledger.
    """

    original = Path(contract_path).resolve()
    original_before = _bytes(original)
    watched = [(name, path, _bytes(path)) for name, path in watch]
    try:
        proposal = propose_changes(original, changes)
        current_contract = load_contract(original)
        current = _check(current_contract, original)
        proposed = _check(proposal.contract, original)
        baseline, invalidated = (
            recorded_health(original, current_contract, proposal.contract)
            if recorded_health is not None
            else ((), ())
        )
        return WhatIfResult(
            current=current,
            proposed=proposed,
            recorded_health=RecordedHealthResult(
                baseline=tuple(baseline), invalidated=tuple(invalidated)
            ),
            diff=proposal.result.diff,
        )
    finally:
        _assert_unchanged(original, original_before, name="contract")
        for name, path, before in watched:
            _assert_unchanged(path, before, name=name)
