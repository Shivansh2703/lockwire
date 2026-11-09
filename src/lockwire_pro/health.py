"""The health ledger: a sidecar log of device health beside the contract.

Compares now, predicts nothing. Never the contract — this module
only reads a ``PreflightReport``/``RecordReport``/list[Finding] plus the
contract for device identity, and writes/reads a JSON-Lines sidecar next to
the contract file, the same convention as ``hw.py``'s ``<stem>.hw.yaml``.

State derivation (contract, "Ordering rule", as amended): ``faulty`` iff any
violation finding for the device this run, OR the device declares a signal
source (a driver/rate expectation) that produced zero samples this run — a
silent device is an absolute fault, with or without a baseline ("a sensor
not working... is bad no matter the baseline"). ``degrading``
iff neither of those but any band excursion warning OR any limit margin below
``DEGRADING_MARGIN``; ``healthy`` iff at least one *verdict-bearing* comparison was made and none
of the above; ``watched`` iff the device declares no hard limit
anywhere and a comparison was made only against its baseline envelope (the
"range"/fitted-band leg, pass or violation) — a fitted band never becomes a
limit, so this never counts as healthy or faulty, and never raises a fault;
``not_watched`` iff nothing was compared, nothing declared silent, and the
device declares no hard limit and no baseline to compare against (its reason
then says so, rather than "nothing compared yet"). ``stale`` is
never a stored state — the reader applies it when the latest row is older
than ``STALE_DAYS``.

Preflight's ``sanity``/``flow``/``cross_record``/``cross_signal`` stage bodies
now record a ``ScoredRecord`` on the passing branch of a band/rate/limit
comparison too (M1 health follow-up), the same way ``checks.py``'s
``_ScoreBook.passed`` does for record checks — so a device with only passing
signals reads ``healthy`` here, not ``not_watched``. ``enumeration`` and
``coverage`` remain presence/absence, not a band, and are never turned into a
band comparison here — but ``flow.py``'s ``devices/<id>/rate_hz`` finding
("no samples for this device in the preflight source") is a definitive
zero-samples fact for a device that declares a rate expectation, and is
mapped to ``faulty`` below, not ignored.

Rail rows: a rail gets a ledger row only when it has a finding
this run under ``topology/rails/<id>/`` — never an unconditional per-rail row
like devices get, so a contract with rails but no rail-level check run keeps
an unchanged ledger. Battery rows are not produced: nothing in ``checks.py``
emits a quantity for the singular ``Topology.battery`` today (it has no
``id``), so there is no finding to key a row on; the ``kind`` field still
allows "battery" for whenever one exists. No new contract-schema fields:
``kind`` lives only on this sidecar's ``HealthRow``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from lockwire import __version__, _filelock
from lockwire.copy import human_name
from lockwire.edit import contract_revision
from lockwire.report import Finding, RecordReport, ScoredRecord, verdict_word
from lockwire.report import verdict as _verdict
from lockwire.schema import ArtifactRef, Base, Contract, DeviceInstance, read_artifact

HealthState = Literal["healthy", "degrading", "faulty", "watched", "not_watched", "absent"]
ReaderState = HealthState | Literal["stale"]
HealthKind = Literal["device", "rail", "battery"]
# Evidence coverage (E3-j, plan v8 line 902): "absent" is a device with no
# recorded samples at all (own findings empty — the pipeline never touched
# it this run); "silent" is `_silent_device_finding`'s exact case, a device
# observed over the interval that reported nothing; "captured" is everything
# else that produced at least one finding or scored comparison.
HealthCapture = Literal["absent", "silent", "captured"]

# The flow-stage marker for "this declared-rate device produced zero samples
# in the recording this run" (flow.py's exact `observed` string) — the
# definitive silent-device signal. Matched verbatim so a
# reworded stage message fails loud (mutation bite) instead of silently
# un-mapping.
_SILENT_DEVICE_OBSERVED = "no samples for this device in the preflight source"

# Owner default #1 (flagged): the margin below which a passing comparison
# reads "degrading" rather than "healthy". Lives in this one constant.
DEGRADING_MARGIN = 0.10

# Owner default #2 (flagged): a device's latest row older than this reads
# "stale" to a reader, regardless of what state it recorded.
STALE_DAYS = 7

# Owner default #3 (flagged): the GUI's interface poll — a display refresh
# rate, never a freshness guarantee (plan v8 §4 line 518). Reported on the
# health document as its own field so it can never be read as sample age.
INTERFACE_POLL_S = 5.0

# Plain words, everywhere the same (contract "Words on screen").
WORDS: dict[ReaderState, str] = {
    "healthy": "inside its bands",
    "degrading": "drifting",
    "faulty": "over a limit",
    "stale": "not seen for {days} days",
    # Baseline drift with no declared limit is watched, never a fault —
    # the words a fitted-band-only device carries on its row.
    "watched": "watched, no limit declared",
    "not_watched": "nothing compared yet",
    # Declared, recorded nothing — not measured, which is not a fault.
    "absent": "declared, not seen",
}


class SignalBand(Base):
    observed_lo: float
    observed_hi: float
    band_lo: float
    band_hi: float
    unit: str
    outside: bool


class LimitMargin(Base):
    value: float
    limit: float
    unit: str
    margin: float


class HealthRow(Base):
    run: str
    run_id: str
    source: str
    context: str | None
    device: str
    state: HealthState
    reason: str
    signals: dict[str, SignalBand] = {}
    limits: dict[str, LimitMargin] = {}
    n_compared: int
    contract_sha: str
    tool: str
    # Full raw-byte revision for rows written after E1.3. ``contract_sha``
    # remains the legacy display prefix; empty marks historical rows and is
    # never guessed or rewritten by a reader.
    contract_revision: str = ""
    # Sidecar-only (no new contract-schema fields); a plain reader that
    # predates this field parses old rows fine — pydantic defaults it in.
    kind: HealthKind = "device"
    # The hash chain (reimplemented from the described
    # mechanism, nothing copied). Sidecar-only, like `kind`. Rows written
    # before the chain landed carry "" and `--verify` names them as breaks.
    prev: str = ""
    hash: str = ""
    # Evidence coverage (E3-j). Sidecar-only, like `kind`/`prev`/`hash` — a
    # row written before this field landed parses fine and reads `unknown`
    # for the count (never `0`: a zero is a claim this box cannot make for
    # history it did not measure).
    samples_observed: int | None = None
    capture: HealthCapture = "captured"
    reason_code: str = ""
    # The bag this row was scored against, identified by its own content
    # hash (`Recording.sha256`, already computed at load time over manifest +
    # samples — never a new hash invented here). Sidecar-only, like `kind`/
    # `prev`/`hash`/`samples_observed` — a row written before this field
    # landed carries "" and MUST NOT be backfilled with a guessed value; a
    # renderer treats "" as absent, plainly, the same way a rail with no
    # voltage renders `unknown` rather than a number (box e4-pt).
    bag_sha: str = ""


def sidecar_path_for(contract_path: Path) -> Path:
    """Sidecar path: ``lockwire.yaml`` -> ``lockwire.health.jsonl`` (hw.py convention)."""
    return contract_path.with_name(f"{contract_path.stem}.health.jsonl")


def contract_sha(contract_path: Path) -> str:
    """The contract file's short sha — identifies which contract version a row was scored against."""
    return hashlib.sha256(contract_path.read_bytes()).hexdigest()[:12]


def _run_id(source: str, run: str) -> str:
    return hashlib.sha256(f"{source}{run}".encode()).hexdigest()[:12]


def _signal_leg(quantity: str) -> tuple[str, str] | None:
    """``devices/<id>/signals/<channel>/<leg>`` -> ``(channel, leg)``, else ``None``."""
    parts = quantity.split("/")
    if len(parts) < 5 or parts[0] != "devices" or parts[2] != "signals":
        return None
    return parts[3], parts[4]


def _signal_band(finding: Finding) -> tuple[str, SignalBand] | None:
    """Build a degenerate signal-band entry from a range/hard_limit Finding.

    A violation Finding only carries the bound it crossed (``observed`` value
    and the crossed ``expected`` bound), never the whole declared band — so
    ``band_lo``/``band_hi`` here are the min/max of those two numbers, not the
    full envelope. Directionally correct (states the crossed bound, marks
    ``outside``), not a full band reconstruction.
    """
    leg = _signal_leg(finding.quantity)
    if leg is None or leg[1] not in ("range", "hard_limit"):
        return None
    channel, _ = leg
    observed = finding.observed_quantity
    expected = finding.expected_quantity
    if observed is None or expected is None:
        return None
    return channel, SignalBand(
        observed_lo=observed.value,
        observed_hi=observed.value,
        band_lo=min(observed.value, expected.value),
        band_hi=max(observed.value, expected.value),
        unit=observed.unit,
        outside=finding.severity in ("violation", "warning"),
    )


def _signal_band_from_scored(record: ScoredRecord) -> tuple[str, SignalBand] | None:
    """The passing twin of ``_signal_band``: a ``ScoredRecord`` from a band/limit
    comparison that passed (preflight stages record these on the pass branch —
    see ``preflight/stages/sanity.py``, ``cross_signal.py``). Same degenerate
    two-point band as ``_signal_band`` (not the full declared envelope),
    always ``outside=False`` — a ScoredRecord only ever exists for a pass.
    """
    leg = _signal_leg(record.quantity)
    if leg is None:
        return None
    channel, _ = leg
    observed = record.observed_quantity
    expected = record.expected_quantity
    if observed is None or expected is None:
        return None
    return channel, SignalBand(
        observed_lo=observed.value,
        observed_hi=observed.value,
        band_lo=min(observed.value, expected.value),
        band_hi=max(observed.value, expected.value),
        unit=observed.unit,
        outside=False,
    )


def _limit_margin(finding: Finding) -> tuple[str, LimitMargin] | None:
    """A declared-limit comparison, computed only when both operands exist."""
    observed = finding.observed_quantity
    expected = finding.expected_quantity
    if observed is None or expected is None:
        return None
    limit = expected.value
    margin = (limit - observed.value) / limit if limit else 0.0
    return finding.quantity, LimitMargin(
        value=observed.value, limit=limit, unit=observed.unit, margin=margin
    )


def _signal_label(quantity: str) -> str:
    leg = _signal_leg(quantity)
    return leg[0] if leg is not None else quantity


def _silent_device_finding(own: list[Finding]) -> Finding | None:
    """A ``devices/<id>/rate_hz`` disclosure meaning "declared a rate
    expectation, zero samples this run" (``flow.py``) — the
    definitive silent-device signal. Not a band, so it never touches
    ``signals``/``limits``; it only escalates the state.
    """
    for finding in own:
        if finding.quantity.endswith("/rate_hz") and finding.observed == _SILENT_DEVICE_OBSERVED:
            return finding
    return None


def _device_row(
    device: DeviceInstance,
    findings: list[Finding],
    scored_records: list[ScoredRecord],
    *,
    run: str,
    run_id: str,
    source: str,
    context: str | None,
    contract_sha: str,
    contract_revision: str,
    declared: bool,
    declares_limit: bool,
    sample_count: int | None = None,
    bag_sha: str = "",
) -> HealthRow:
    device_id = device.id
    prefix = f"devices/{device_id}/"
    own = [f for f in findings if f.quantity.startswith(prefix)]
    own_scored = [r for r in scored_records if r.quantity.startswith(prefix)]

    violations = [f for f in own if f.severity == "violation"]
    warnings = [f for f in own if f.severity == "warning"]
    silent = _silent_device_finding(own)

    # Evidence coverage (E3-j): the capture value is decided here, once, from
    # the same `own`/`silent` this function already computed — never re-walked.
    if silent is not None:
        capture: HealthCapture = "silent"
    elif not own and not own_scored:
        capture = "absent"
    else:
        capture = "captured"

    signals: dict[str, SignalBand] = {}
    limits: dict[str, LimitMargin] = {}
    for finding in own:
        band = _signal_band(finding)
        if band is not None:
            signals[band[0]] = band[1]
        leg = _signal_leg(finding.quantity)
        if leg is not None and leg[1] == "hard_limit":
            margin = _limit_margin(finding)
            if margin is not None:
                limits[margin[0]] = margin[1]
    for record in own_scored:
        band = _signal_band_from_scored(record)
        if band is not None:
            signals.setdefault(band[0], band[1])

    n_compared = len(own_scored) + len(violations) + len(warnings)
    margins = [entry.margin for entry in limits.values()]

    # Without a declared hard limit anywhere on this device, every
    # "range" (envelope/fitted-band) comparison — pass or violation — is
    # baseline drift, never a verdict; a fitted band never becomes a limit.
    # These are already counted in n_compared and already in `signals` above
    # (drift is shown); pull them out of what decides faulty/degrading/
    # healthy below, so they can only ever read "watched".
    def _is_range(quantity: str) -> bool:
        leg = _signal_leg(quantity)
        return leg is not None and leg[1] == "range"

    drift: list[Finding] = []
    n_drift = 0
    if not declares_limit:
        drift = [f for f in violations + warnings if _is_range(f.quantity)]
        drift_ids = {id(f) for f in drift}
        violations = [f for f in violations if id(f) not in drift_ids]
        warnings = [f for f in warnings if id(f) not in drift_ids]
        n_drift = len(drift) + sum(1 for r in own_scored if _is_range(r.quantity))

    if violations:
        worst = violations[0]
        state: HealthState = "faulty"
        reason = f"{_signal_label(worst.quantity)}: {worst.observed} (expected {worst.expected})"
        reason_code = "violation"
    elif silent is not None:
        # A declared device with no samples is not
        # measured — "absent", never a fault and never GO.
        state = "absent"
        topic = device.driver.topic if device.driver is not None and device.driver.topic else None
        reason = f"no samples: {topic or device_id}"
        reason_code = "silent"
    elif warnings or any(margin < DEGRADING_MARGIN for margin in margins):
        state = "degrading"
        if warnings:
            worst = warnings[0]
            reason = (
                f"{_signal_label(worst.quantity)}: {worst.observed} (expected {worst.expected})"
            )
            reason_code = "warning"
        else:
            worst_margin = min(margins)
            reason = f"margin {worst_margin:.0%} of limit"
            reason_code = "margin"
    elif n_compared - n_drift >= 1:
        state = "healthy"
        reason = f"inside its bands, {n_compared - n_drift} signals compared"
        reason_code = "in_band"
    elif n_drift >= 1:
        # Comparisons happened against the baseline only — no declared
        # limit anywhere for this device — watched, no verdict, never a
        # fault, whether the drift is a violation or a clean pass.
        state = "watched"
        if drift:
            worst = drift[0]
            reason = (
                f"{_signal_label(worst.quantity)}: {worst.observed} vs baseline (no limit declared)"
            )
        else:
            reason = f"inside its baseline band, {n_drift} signal(s) compared (no limit declared)"
        reason_code = "watched"
    else:
        state = "not_watched"
        # The word tells the user what to do — only when the
        # contract itself declares nothing to compare (no hard limit, no
        # baseline) does the reason say so; otherwise it is watched but quiet.
        reason = "nothing compared yet" if declared else "nothing declared to compare against"
        reason_code = "no_comparison" if declared else "not_declared"

    return HealthRow(
        run=run,
        run_id=run_id,
        source=source,
        context=context,
        device=device_id,
        state=state,
        reason=reason,
        signals=signals,
        limits=limits,
        n_compared=n_compared,
        samples_observed=sample_count,
        capture=capture,
        reason_code=reason_code,
        contract_sha=contract_sha,
        contract_revision=contract_revision,
        tool=f"lockwire {__version__}",
        bag_sha=bag_sha,
    )


def _device_declares_limit(contract: Contract, device: DeviceInstance) -> bool:
    """True iff the contract declares an asserted hard limit for this device
    — the "declared threshold", narrower than `_device_declared` below.
    A baseline-only envelope (fitted or hand-authored) does not count: a
    fitted band never becomes a limit."""
    model = contract.device_model(device.model)
    return model is not None and bool(model.hard_limits)


def _device_declared(contract: Contract, device: DeviceInstance) -> bool:
    """True iff the contract declares *something* to compare this device
    against: a model hard limit, or a baseline (a context envelope with
    signals). Drives the not_watched wording."""
    if _device_declares_limit(contract, device):
        return True
    return any(envelope.signals for envelope in device.envelopes)


def _applicable_count(contract: Contract, device: DeviceInstance) -> int:
    """How many comparisons the contract declares possible for this device:
    every hard limit on its model plus every distinct signal channel any of
    its context envelopes declares. Contract-only (compares, never
    predicts) — never re-derived from findings, which only carry what was
    actually made this run (that is `n_compared`, a different number)."""
    model = contract.device_model(device.model)
    limits = len(model.hard_limits) if model is not None else 0
    channels = {name for envelope in device.envelopes for name in envelope.signals}
    return limits + len(channels)


def _rail_row(
    rail_id: str,
    findings: list[Finding],
    *,
    run: str,
    run_id: str,
    source: str,
    context: str | None,
    contract_sha: str,
    contract_revision: str,
    bag_sha: str = "",
) -> HealthRow:
    """A rail row: only ever called for a rail that already has an own
    finding this run (no unconditional per-rail row like
    devices get, so a ledger with no rail check run is unchanged)."""
    prefix = f"topology/rails/{rail_id}/"
    own = [f for f in findings if f.quantity.startswith(prefix)]
    violations = [f for f in own if f.severity == "violation"]
    warnings = [f for f in own if f.severity == "warning"]

    if violations:
        worst = violations[0]
        state: HealthState = "faulty"
        reason_code = "violation"
    else:
        worst = warnings[0]
        state = "degrading"
        reason_code = "warning"
    reason = f"{worst.quantity.removeprefix(prefix)}: {worst.observed} (expected {worst.expected})"

    return HealthRow(
        run=run,
        run_id=run_id,
        source=source,
        context=context,
        device=rail_id,
        state=state,
        reason=reason,
        n_compared=len(violations) + len(warnings),
        # A rail row exists only because it has a finding this run
        # — it was always captured, never absent/silent.
        capture="captured",
        reason_code=reason_code,
        contract_sha=contract_sha,
        contract_revision=contract_revision,
        tool=f"lockwire {__version__}",
        kind="rail",
        bag_sha=bag_sha,
    )


# --- the hash chain -------------------------------------------------------
# Shared mechanism, not ledger-private: the command record (E-B B-9) chains
# with these same three functions rather than reimplementing them. They take
# plain JSON-shaped dicts, so any append-only JSON-Lines file can use them.

CHAIN_ZERO = "0" * 64


def chain_row_hash(fields: Mapping[str, Any], prev: str) -> str:
    """SHA-256 of one row's canonical JSON: ``prev`` set, ``hash`` excluded.

    Canonical means sorted keys and no whitespace, so a row that is re-read
    and re-serialised in a different key order still hashes the same.
    """
    body = {key: value for key, value in fields.items() if key != "hash"}
    body["prev"] = prev
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chain_last_hash(path: Path) -> str:
    """The last row's ``hash`` in an existing JSON-Lines file, else ``CHAIN_ZERO``.

    An unreadable or unchained last line restarts the chain at zero rather
    than raising — `chain_break` still names the seam.
    """
    if not path.exists():
        return CHAIN_ZERO
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            return str(json.loads(line).get("hash") or CHAIN_ZERO)
        except ValueError:
            return CHAIN_ZERO
    return CHAIN_ZERO


def chain_break(rows: Sequence[Mapping[str, Any]]) -> tuple[int, str] | None:
    """The first row that does not hold, as ``(1-based line number, why)``.

    Catches a tampered field, a row deleted from the middle and a reordering
    (both break the next row's ``prev``). It cannot catch rows truncated from
    the *end* — no self-contained chain can, since the remaining prefix is a
    valid chain; that needs an anchor kept elsewhere.
    """
    prev = CHAIN_ZERO
    for index, row in enumerate(rows, start=1):
        stored = str(row.get("hash") or "")
        if not stored:
            return index, "row carries no hash"
        if str(row.get("prev") or "") != prev:
            return index, "prev does not match the previous row's hash"
        if chain_row_hash(row, prev) != stored:
            return index, "hash does not match the row's contents"
        prev = stored
    return None


def read_ledger_lines(path: Path) -> list[dict[str, Any]]:
    """Every ledger row as the raw dict on disk — what `chain_break` verifies.

    Raw rather than ``HealthRow`` on purpose: the hash covers the bytes that
    were written, so a field the model would drop still has to be seen.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({})
    return rows


def append_health_rows(
    contract_path: Path,
    contract: Contract,
    report: list[Finding],
    *,
    source: str,
    context: str | None,
    now: datetime,
    sample_counts: Mapping[str, int] | None = None,
    bag_sha: str | None = None,
) -> int:
    """Append one row per contract device; never raise on write failure's behalf.

    Prints nothing itself — callers print one line:
    ``health: <n> parts recorded -> <path>``.

    ``sample_counts`` (device id -> raw samples observed this run) is where
    a caller with access to the actual recording/source hands over the
    number evidence coverage needs — the write site is the
    only place that number exists (never re-walked). Callers that don't
    have it (e.g. ``baseline-check``) omit it, and the row reads `unknown`
    for the count rather than a false `0`.

    ``bag_sha`` is the recording's own content hash — ``Recording.sha256``,
    already computed at load time — handed over by the write site the same way
    ``sample_counts`` is; one recording per call, so one sha covers every row it
    writes. Omitted (``None``) means the caller could not identify a single bag
    (e.g. a live source that never produced one) and every row reads `""`
    (absent), never a guessed value.
    """
    findings = list(report)
    scored_records = list(getattr(report, "scored_records", None) or [])
    run = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = _run_id(source, run)
    contract_sha_ = contract_sha(contract_path)
    contract_revision_ = contract_revision(contract_path)
    bag_sha_ = bag_sha or ""

    rows = [
        _device_row(
            device,
            findings,
            scored_records,
            run=run,
            run_id=run_id,
            source=source,
            context=context,
            contract_sha=contract_sha_,
            contract_revision=contract_revision_,
            declared=_device_declared(contract, device),
            declares_limit=_device_declares_limit(contract, device),
            sample_count=(sample_counts or {}).get(device.id),
            bag_sha=bag_sha_,
        )
        for device in contract.devices
    ]
    rows.extend(
        _rail_row(
            rail.id,
            findings,
            run=run,
            run_id=run_id,
            source=source,
            context=context,
            contract_sha=contract_sha_,
            contract_revision=contract_revision_,
            bag_sha=bag_sha_,
        )
        for rail in (contract.topology.rails if contract.topology is not None else [])
        if any(f.quantity.startswith(f"topology/rails/{rail.id}/") for f in findings)
    )

    path = sidecar_path_for(contract_path)
    # Chained here, in the one function both writers (`preflight` and
    # `baseline-check`) go through, so neither can append an unchained row.
    # ponytail: flock on a sibling lock file serialises writers on ONE host only; a ledger on a
    # network share needs a real lock service (M25).
    try:
        lock_file = path.with_name(path.name + ".lock").open("a")
    except PermissionError:
        raise PermissionError(
            f"cannot write the health ledger {path}: its folder is read-only"
        ) from None
    with lock_file as lock:
        _filelock.lock(lock.fileno())
        prev = chain_last_hash(path)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                payload = row.model_dump(mode="json")
                payload["prev"] = prev
                prev = chain_row_hash(payload, prev)
                payload["hash"] = prev
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return len(rows)


def read_ledger(path: Path) -> list[HealthRow]:
    """Read every row; a corrupt line is skipped (counted), never a crash."""
    rows, _skipped = read_ledger_with_skipped(path)
    return rows


def read_ledger_with_skipped(path: Path) -> tuple[list[HealthRow], int]:
    """Same rows as ``read_ledger``, plus the corrupt-line count.

    ``read_ledger`` used to be the only place this loop ran, and its skip
    count only ever reached a `print` to stderr (E3-A4-r2 bounce): every
    reader — the CLI table, ``--json``, the GUI page, the MCP payload — called
    `read_ledger`, threw the count away, and so a caller could not tell "5
    rows" from "5 rows, one of them dropped". This is the one loop; both
    functions route through it, so there is still one place that parses a
    line, not two.
    """
    if not path.exists():
        return [], 0
    rows: list[HealthRow] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = HealthRow.model_validate_json(line)
            # A row can be JSON- and schema-valid yet still carry a `run`
            # that isn't a real timestamp (a truncated/mid-write ledger's
            # last line, most often) — `row_age`/`device_state` parse it
            # with the same call and would crash the whole health path on
            # it. Reject it here, the one place every reader routes
            # through, same as a malformed line: skipped and counted, never
            # a crash downstream.
            datetime.fromisoformat(row.run.replace("Z", "+00:00"))
            # Rows written before "absent" existed called a silent device
            # "faulty". The chain is append-only, so fix it at read time: no
            # comparisons made is never a fault.
            if row.n_compared == 0 and row.state == "faulty":
                row = row.model_copy(update={"state": "absent"})
            rows.append(row)
        except ValueError:
            skipped += 1
    if skipped:
        print(f"health: skipped {skipped} corrupt line(s) in {path}", file=sys.stderr)
    return rows, skipped


def own_and_foreign(
    rows: list[HealthRow], contract_path: Path
) -> tuple[list[HealthRow], list[HealthRow]]:
    """Split a ledger into this contract version's rows and everything else.

    Rows scored against a different contract version are history, never the
    current state (reviewer bounce (b), E0-25). Legacy rows without the full
    raw-byte revision remain readable, but are excluded from current health:
    a matching 12-character display prefix cannot establish freshness.

    One predicate, three readers — ``cli.health``'s table and ``--json``,
    ``api.health``, and the GUI health face — so a door cannot quietly
    summarise rows another door refuses to summarise.
    """
    sha = contract_sha(contract_path)
    revision = contract_revision(contract_path)
    own: list[HealthRow] = []
    foreign: list[HealthRow] = []
    for row in rows:
        # Legacy rows predate the full raw-byte token and retain their original
        # bytes/readability, but cannot be presented as current health. A new
        # row must match both the display prefix and exact contract bytes.
        current = (
            row.contract_sha == sha
            and bool(row.contract_revision)
            and row.contract_revision == revision
        )
        (own if current else foreign).append(row)
    return own, foreign


def invalidated_recorded_rows(
    rows: list[HealthRow], before: Contract, after: Contract
) -> tuple[HealthRow, ...]:
    """Return baseline rows whose declared dependency changes in ``after``.

    This is a what-if projection over rows already recorded against ``before``;
    it never re-scores those rows or turns a proposed record check into health
    evidence.  Unrelated contract edits leave their recorded claims available
    as the preceding baseline.
    """
    from lockwire.factdiff import flatten

    before_flat, after_flat = flatten(before), flatten(after)
    changed = {
        path
        for path in set(before_flat) | set(after_flat)
        if before_flat.get(path) != after_flat.get(path)
    }
    if not changed:
        return ()

    def ids_under(prefix: str) -> set[str]:
        prefix_parts = prefix.split("/")
        return {
            path.split("/")[len(prefix_parts)]
            for path in changed
            if path.split("/")[: len(prefix_parts)] == prefix_parts
            and len(path.split("/")) > len(prefix_parts)
        }

    # The robot model supplies frames, links, masses, and joints to every
    # recorded health result. A changed model therefore invalidates all rows.
    if any(path == "model" or path.startswith("model/") for path in changed):
        return tuple(rows)

    device_ids = ids_under("devices")
    model_ids = ids_under("device_models")
    family_ids = ids_under("families")
    rail_ids = ids_under("topology/rails")
    bus_ids = ids_under("topology/buses")

    for contract in (before, after):
        models = {model.id: model for model in contract.device_models}
        model_ids.update(model.id for model in models.values() if model.family in family_ids)
        device_ids.update(device.id for device in contract.devices if device.model in model_ids)
        device_ids.update(
            device.id
            for device in contract.devices
            if device.power is not None and device.power.rail in rail_ids
        )
        device_ids.update(
            device.id
            for device in contract.devices
            if device.identity is not None and device.identity.bus_ref in bus_ids
        )

    # Fitted-band allocation is the shared specification for device signal
    # comparisons; a changed allocation affects every device-row claim.
    if any(
        path == "fitted_band_allocations" or path.startswith("fitted_band_allocations/")
        for path in changed
    ):
        device_ids.update(device.id for device in before.devices)
        device_ids.update(device.id for device in after.devices)

    battery_changed = any(
        path == "topology/battery" or path.startswith("topology/battery/") for path in changed
    )
    return tuple(
        row
        for row in rows
        if (row.kind == "device" and row.device in device_ids)
        or (row.kind == "rail" and (row.device in rail_ids or battery_changed))
        or (row.kind == "battery" and battery_changed)
    )


def _run_instant(row: HealthRow) -> datetime:
    """Parse ``row.run`` into a timestamp for sorting. A row whose timestamp
    will not parse sorts as the oldest possible instant (E0-e3-lb): it can
    never masquerade as the latest reading over a row that does carry a real
    timestamp. Among rows that tie on this key — including two undateable
    rows — the caller's file (append) order decides."""
    try:
        return datetime.fromisoformat(row.run.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def latest_by_device(rows: list[HealthRow]) -> dict[str, HealthRow]:
    """The most recent row per device, by parsed timestamp — not file
    position (E0-e3-lb). Rows are append-only, but an out-of-order append
    (a retry, a clock adjustment, a merged ledger) is possible, and file
    order alone would then report a stale row as current with no error.
    A row whose timestamp will not parse is kept, not dropped, but sorts as
    the oldest possible — it can lose to any dateable row but still wins
    among ties (see ``_run_instant``)."""
    best_key: dict[str, tuple[datetime, int]] = {}
    latest: dict[str, HealthRow] = {}
    for index, row in enumerate(rows):
        key = (_run_instant(row), index)
        if row.device not in best_key or key > best_key[row.device]:
            best_key[row.device] = key
            latest[row.device] = row
    return latest


def parse_run(run: str) -> datetime:
    """Parse a ledger row's ``run`` string into an AWARE instant.

    The one parser for ``row.run`` (E3-a-r3): the writer (``append_health_rows``,
    below) always produces ``now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`` —
    an ISO 8601 string with an explicit (UTC) offset — and every reader of that
    string, ``row_age`` and ``device_series``, parses it back through this one
    function. Two parsers is how the reader and the writer's format drift apart.

    Raises ``ValueError`` for anything ``datetime.fromisoformat`` cannot parse,
    and — deliberately — for a naive result too: a naive ``run`` is a defect of
    the WRITER (it always writes an aware instant), never something a reader
    tolerates by silently assuming UTC.
    """
    parsed = datetime.fromisoformat(run.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"naive run timestamp: {run!r}")
    return parsed


def row_age(row: HealthRow, now: datetime) -> timedelta:
    """How long ago this row was recorded — the one place that math happens
    (``device_state``'s staleness check and the evidence-coverage age both
    reuse it)."""
    run_dt = parse_run(row.run)
    return now.astimezone(UTC) - run_dt


def device_state(row: HealthRow, now: datetime) -> ReaderState:
    """The row's stored state, or "stale" when it is older than ``STALE_DAYS``.

    A ``faulty`` row is the one exception (E3-j, plan v8 line 902): "a stale
    fault keeps showing with its age... a missing update never makes a fault
    look cleared." Staleness is display-layer decoration for a fault, not a
    replacement state — a reader that only looks at this return value must
    still see the fault, with its age reported separately (``row_age`` /
    ``HealthDocument``'s per-device ``coverage.newest_sample_age_s``).
    """
    if row.state == "faulty":
        return "faulty"
    if row_age(row, now) > timedelta(days=STALE_DAYS):
        return "stale"
    return row.state


def history(rows: list[HealthRow], device: str) -> list[HealthRow]:
    """Every row for one device, in ledger (append) order."""
    return [row for row in rows if row.device == device]


def _declared_band(
    contract: Contract, device: DeviceInstance, channel: str
) -> dict[str, Any] | None:
    """The channel's genuine declared band (reuses `DeviceModel.hard_limits`,
    schema.py — never re-derived). ``None`` when the device's model declares no
    hard limit for this channel: a fitted/baseline band never becomes a limit,
    so a watched-only channel gets no band, not an approximated one.
    """
    model = contract.device_model(device.model)
    if model is None:
        return None
    band = model.hard_limits.get(channel)
    if band is None:
        return None
    return {"lo": band.lo, "hi": band.hi, "unit": band.unit}


def device_series(
    rows: list[HealthRow], contract: Contract, device: DeviceInstance
) -> tuple[list[dict[str, Any]], int]:
    """Twenty clean runs make twenty plotted points (E3-a, plan v8 line 884).

    One entry per signal channel the device's own (never foreign) rows
    carry, ordered oldest-to-newest **by each row's own ``run`` timestamp,
    parsed to an aware instant** (``parse_run``, above) — never by the raw
    string and never by ledger position: a
    merged or hand-copied ledger is append-ordered by convention, not by
    guarantee, and a foreign machine's offset misorders against local ones
    once you compare the strings lexically rather than the instants they
    name. Sorting by string reads as sorted and is not chronological — the
    exact bug this box exists to remove.

    A row whose ``run`` cannot be parsed (E3-a-r3) is EXCLUDED from every
    channel's series — never sorted to the head or the tail, since an
    unplaceable point has no correct place on a chart of time — and counted
    once per row (not once per channel it would otherwise have joined); the
    second element of the return value is that count, so a caller can add it
    up across devices as "N rows could not be placed in time", distinct from
    ``series_excluded_foreign``.

    Each point's value reuses the ``SignalBand`` already computed at write
    time by ``_signal_band``/``_signal_band_from_scored`` (never
    re-walked here), carries its run id, its timestamp and its kind (a
    run produced it, so ``"measured"``); the declared band sits once beside
    the series rather than once per point. A device whose model declares no
    hard limit for a channel still gets its points (watched against
    baseline only) — its band is ``None``, not a guessed one.
    """
    channels: dict[str, list[HealthRow]] = {}
    unplaceable = 0
    for row in history(rows, device.id):
        try:
            parse_run(row.run)
        except ValueError:
            unplaceable += 1
            continue
        for channel in row.signals:
            channels.setdefault(channel, []).append(row)

    series: list[dict[str, Any]] = []
    for channel, chan_rows in channels.items():
        ordered_rows = sorted(chan_rows, key=lambda row: parse_run(row.run))
        series.append(
            {
                "channel": channel,
                "band": _declared_band(contract, device, channel),
                "points": [
                    {
                        "value": row.signals[channel].observed_lo,
                        "unit": row.signals[channel].unit,
                        "run": row.run,
                        "run_id": row.run_id,
                        "kind": "measured",
                    }
                    for row in ordered_rows
                ],
            }
        )
    return series, unplaceable


def summary(rows: list[HealthRow], now: datetime, *, skipped: int = 0) -> dict[str, object]:
    """Pure summary shared by the CLI and the page — one source of truth.

    ``skipped`` (E3-A4-r2) is the corrupt/unparseable-row count `read_ledger`
    dropped before ``rows`` ever reached here — the caller's own
    `read_ledger_with_skipped` result, passed through rather than
    re-derived, so this number and the chain's are the same value threaded
    twice, never two counts that happen to agree today.
    """
    latest = latest_by_device(rows)
    per_device = {device: device_state(row, now) for device, row in latest.items()}
    last_run = max((row.run for row in latest.values()), default=None)
    watched = sum(1 for state in per_device.values() if state not in ("not_watched", "absent"))
    rank = {
        "faulty": 0,
        "stale": 1,
        "degrading": 2,
        "healthy": 3,
        "watched": 4,
        "not_watched": 5,
        "absent": 5,
    }
    worst = min(per_device.values(), key=lambda state: rank[state]) if per_device else None
    return {
        "last_run": last_run,
        "watched": watched,
        "worst": worst,
        "per_device": per_device,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# The health document: built once, here, and rendered by every
# door. Nothing below re-derives a state, a foreign-row rule or an exit code —
# `device_state`, `own_and_foreign` and `report.verdict` each decide once.
# ---------------------------------------------------------------------------


class HealthDocument(Base):
    """Everything any door needs to show the health ledger, decided once."""

    path: str
    # A ledger opened from anywhere says where it came from. `path` is
    # absolute (or the given path if it doesn't exist), `copied_at` is the
    # file's mtime as the copy time, or `None` for a ledger that isn't there.
    origin: dict[str, Any]
    contract_sha: str
    devices: list[dict[str, Any]]
    foreign_rows: int
    # How many rows a device's series excluded for being scored against
    # another contract version — same count as `foreign_rows`, named
    # for the series's own accounting so a reviewer never has to infer it.
    series_excluded_foreign: int
    # E3-a-r3: rows excluded from a series because their `run` timestamp could
    # not be parsed to an instant — a distinct reason from `series_excluded_foreign`
    # above, so a reader can tell "foreign" from "unplaceable" apart rather than
    # one merged number. Never sorted anywhere; "N rows could not be placed in time".
    # E3-a5: includes `chain["skipped"]`/`summary["skipped"]` — e3-a4 rejects a
    # schema-valid row with an unparseable `run` before it ever reaches a
    # series, so that same count is this field's, not a second one.
    series_excluded_unplaceable: int
    not_watched: int
    scored: int
    chain: dict[str, Any]
    summary: dict[str, Any]
    verdict: str
    exit_code: int
    # Present only for an independently anchored copied ledger and bundle.
    integrity: dict[str, Any] | None = None
    # E3-j: the GUI's display refresh rate, reported here so no consumer can
    # mistake it for sample freshness (plan v8 §4 line 518) — it is never
    # compared against `coverage.newest_sample_age_s` by this module.
    interface_poll_s: float = INTERFACE_POLL_S
    # True iff no applicable comparison exists anywhere in
    # the contract — branched on comparisons made (`scored == 0`), never on
    # whether a baseline was authored. When true, `devices` is emitted empty
    # (no series, no band for a face to draw chart furniture from) and
    # `next_step` carries the one sentence naming what to author.
    nothing_watched: bool = False
    next_step: str | None = None
    # The CLI line behind `next_step` (which is the short plain-words state).
    next_action: str | None = None

    # Rich `Finding` objects for the terminal; excluded from the JSON document,
    # which carries `devices` and `verdict` instead.
    findings: list[Finding] = []

    def as_dict(self) -> dict[str, Any]:
        excluded = {"findings", "integrity"} if self.integrity is None else {"findings"}
        return self.model_dump(mode="json", exclude=excluded)


def build_document(
    contract_path: Path,
    contract: Contract,
    *,
    now: datetime,
    ledger: Path | None = None,
    health_anchor: tuple[int, str] | None = None,
    bundle_dependencies: tuple[ArtifactRef, ...] | None = None,
    evidence_root: Path | None = None,
    parsed_contract_sha256: str | None = None,
) -> HealthDocument:
    """The whole health document: rows, chain, findings, verdict and exit code.

    Reuses `read_ledger`, `read_ledger_lines`/`chain_break` (eb-03's chain — a
    second walk would give a second answer about tampering), `own_and_foreign`,
    `latest_by_device`, `device_state`, `history`, `summary` and
    `report.verdict`. Compares now, predicts nothing.

    ``ledger`` opens a ledger copied off the robot to anywhere on disk
    instead of the contract's own sidecar — the chain is walked and the
    origin reported the same either way, so a copied file is never a second,
    untrusted code path.
    """
    anchored = any(
        value is not None for value in (health_anchor, bundle_dependencies, evidence_root)
    )
    if anchored:
        if (
            not isinstance(health_anchor, tuple)
            or len(health_anchor) != 2
            or type(health_anchor[0]) is not int
            or health_anchor[0] < 0
            or not isinstance(health_anchor[1], str)
            or len(health_anchor[1]) != 64
            or any(char not in "0123456789abcdef" for char in health_anchor[1])
            or not isinstance(bundle_dependencies, tuple)
            or not bundle_dependencies
            or any(not isinstance(ref, ArtifactRef) for ref in bundle_dependencies)
            or evidence_root is None
        ):
            raise ValueError("remote health needs a caller-held head/count and bundle references")
    path = ledger if ledger is not None else sidecar_path_for(contract_path)
    ledger_bytes = path.read_bytes() if anchored and path.exists() else None
    raw = read_ledger_lines(path)
    broken = chain_break(raw) if raw else None
    rows, skipped = read_ledger_with_skipped(path)
    # `chain["rows"]` stays the raw line count (E3-A4-r2 decision): the hash
    # chain it verifies covers every appended line, corrupt ones included —
    # that is what proves nothing was deleted or reordered, and dropping the
    # corrupt line from this count would make the chain-verify number cover
    # fewer lines than it actually walked. What was WRONG was `chain["rows"]`
    # standing alone as "N rows, no breaks", read as "N usable rows" — so it
    # gains a companion, `skipped`, sourced from the same
    # `read_ledger_with_skipped` call every other reader now threads through,
    # not a second count that happens to match.
    chain = {
        "rows": len(raw),
        "skipped": skipped,
        "ok": bool(raw) and broken is None,
        "line": broken[0] if broken else None,
        "why": broken[1] if broken else None,
    }
    origin = {
        "path": str(path.resolve()) if path.exists() else str(path),
        "copied_at": (
            datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if path.exists()
            else None
        ),
    }

    sha = contract_sha(contract_path)
    own, excluded = own_and_foreign(rows, contract_path)
    # Preserve legacy ledger bytes and parsing, but disclose their missing full
    # identity distinctly from a known foreign revision.
    legacy_revision_unknown = [
        row for row in excluded if row.contract_sha == sha and not row.contract_revision
    ]
    legacy_ids = {id(row) for row in legacy_revision_unknown}
    foreign = [row for row in excluded if id(row) not in legacy_ids]
    foreign_devices = {row.device for row in foreign}
    legacy_revision_unknown_devices = {row.device for row in legacy_revision_unknown}
    latest = latest_by_device(own)

    findings: list[Finding] = []
    devices: list[dict[str, Any]] = []
    not_watched = 0
    # E3-a-r3: a row whose `run` cannot be parsed to an instant is excluded
    # from its device's series (never sorted anywhere) and counted here,
    # summed across every device so a reader can tell "N rows could not be
    # placed in time" apart from `series_excluded_foreign` below — two
    # different exclusion reasons, two distinguishable counts.
    #
    # E3-a5: `device_series`'s own per-loop check (below) can no longer see
    # such a row — e3-a4 (`read_ledger_with_skipped`, this function's
    # `skipped` above) now rejects a schema-valid row with an unparseable
    # `run` at ledger-read time, before it ever reaches `own`/`rows`, so it
    # is counted there instead. Starting from `skipped` rather than 0 carries
    # that count through into this name; `device_series`'s own check stays
    # (defensive, and correct for any future caller that hands it rows that
    # skipped this gate) but is currently unreachable for that reason.
    series_excluded_unplaceable = skipped

    def _series_for(device_obj: DeviceInstance | None) -> list[dict[str, Any]]:
        nonlocal series_excluded_unplaceable
        if device_obj is None:
            return []
        series, unplaceable = device_series(own, contract, device_obj)
        series_excluded_unplaceable += unplaceable
        return series

    contract_devices = {d.id: d for d in contract.devices}
    # A rail/battery id only ever reaches this list through a
    # ledger row — `contract.devices` never contributes one.
    for device_id in sorted(
        {d.id for d in contract.devices}
        | set(latest)
        | foreign_devices
        | legacy_revision_unknown_devices
    ):
        row = latest.get(device_id)
        runs = len(history(rows, device_id))
        device_obj = contract_devices.get(device_id)
        applicable = _applicable_count(contract, device_obj) if device_obj is not None else 0
        if row is None:
            not_watched += 1
            is_foreign = device_id in foreign_devices
            is_legacy_revision_unknown = device_id in legacy_revision_unknown_devices
            devices.append(
                {
                    "id": device_id,
                    "kind": "device",
                    "state": "not_watched",
                    "word": WORDS["not_watched"],
                    "run": None,
                    "reason": (
                        "(revision unknown) recorded result predates full contract identity"
                        if is_legacy_revision_unknown
                        else (
                            "(other contract) nothing compared under this contract"
                            if is_foreign
                            else "nothing compared yet"
                        )
                    ),
                    "runs": runs,
                    # E3-a: no own row for this device, so no channel ever
                    # recorded a point — empty, never absent (`own` is already
                    # foreign-excluded, so a foreign-only device's series is
                    # always empty here, never the foreign row's own values).
                    "series": _series_for(device_obj),
                    # No row, so nothing to reconstruct — never a guess.
                    "contract_sha": None,
                    "bag_sha": None,
                    "recording": None,
                    "drives_joint": device_obj.drives_joint if device_obj else None,
                    "mount_frame": device_obj.physical.mount_frame
                    if device_obj and device_obj.physical
                    else None,
                    "coverage": {
                        "applicable": applicable,
                        "compared": 0,
                        "samples_observed": "unknown",
                        "newest_sample_age_s": None,
                        # Never captured — the two distinct-value rule (E3-j)
                        # only escalates to "silent" once a row exists.
                        "capture": "absent",
                        "reason_code": (
                            "legacy_revision_unknown"
                            if is_legacy_revision_unknown
                            else (
                                "foreign_contract"
                                if is_foreign
                                else ("not_declared" if applicable == 0 else "no_comparison")
                            )
                        ),
                    },
                }
            )
            continue
        state = device_state(row, now)
        if state in ("not_watched", "absent"):
            not_watched += 1
        age = row_age(row, now)
        # E3-j decisive item: a stale FAULT keeps its state word and gains its
        # age beside it — `device_state` already refuses to demote a real
        # fault to "stale" (never swallowed); this only decorates the word.
        if state == "stale":
            word = WORDS["stale"].format(days=STALE_DAYS)
        else:
            word = WORDS[state]
            if state == "faulty" and age > timedelta(days=STALE_DAYS):
                word = f"{word} — last seen {age.days} day(s) ago"
        devices.append(
            {
                "id": device_id,
                "kind": row.kind,
                "state": state,
                "word": word,
                "run": row.run,
                "reason": row.reason,
                "runs": runs,
                "series": _series_for(device_obj),
                # The two shas this row was scored against, plus the
                # exact recording the "why faulty" affordance opens — THIS
                # row's own `source`, never the ledger's latest overall. A row written before `bag_sha`
                # existed carries "" and renders `None` here, plainly absent,
                # rather than a fabricated value.
                "contract_sha": row.contract_sha,
                "bag_sha": row.bag_sha or None,
                "recording": row.source if row.bag_sha else None,
                "drives_joint": device_obj.drives_joint if device_obj else None,
                "mount_frame": device_obj.physical.mount_frame
                if device_obj and device_obj.physical
                else None,
                "coverage": {
                    "applicable": applicable if row.kind == "device" else row.n_compared,
                    "compared": row.n_compared,
                    "samples_observed": (
                        row.samples_observed if row.samples_observed is not None else "unknown"
                    ),
                    "newest_sample_age_s": age.total_seconds(),
                    "capture": row.capture,
                    "reason_code": row.reason_code or "unknown",
                },
            }
        )
        if state == "faulty":
            findings.append(
                Finding(
                    severity="violation",
                    quantity=f"health/{device_id}",
                    observed=row.reason,
                    expected="inside its bands",
                    action="investigate the device before flight",
                )
            )
        elif state == "absent":
            findings.append(
                Finding(
                    severity="not_scored",
                    quantity=f"health/{device_id}",
                    observed=row.reason,
                    expected="every declared device measured",
                    action="record the device again, or reconcile the contract declaration",
                )
            )

    # Presence floor: count comparisons, not rows — an all-not_watched
    # ledger must never read GO.
    scored = sum(1 for row in latest.values() if row.n_compared >= 1)
    decision = _verdict(
        RecordReport(findings, scored=scored, scored_keys=frozenset(f.quantity for f in findings))
    )

    # GO means every declared device was measured, so an absent one caps
    # a GO at NOT SCORED (a violation elsewhere still wins).
    if decision.code in ("GO", "GO_WITH_NOTES") and any(
        row.state == "absent" for row in latest.values()
    ):
        decision = _verdict(RecordReport(findings, scored=0))

    # The day-one branch is on comparisons made, never on
    # whether a baseline was authored — `scored` already counts comparisons
    # (drift included), not declarations, so reusing it here is the fix, not
    # a second walk. `findings` excludes it when a fault was actually raised
    # (a silent device can be faulty with zero comparisons —
    # that is never "nothing watched"), and a foreign-sha row means
    # there IS history to disclose even though it scores nothing under this
    # contract, so that stays the ordinary table rather than the day-one
    # sentence. `devices` (no signals/limits keys on any entry) already
    # carries no series or band for a face to draw chart furniture from.
    nothing_watched = scored == 0 and not findings and not foreign and not legacy_revision_unknown
    # A contract can already declare a hard limit or a
    # baseline (`_device_declared`, reused) and still have nothing_watched
    # True — every device's history is empty (`has_history` in cli.py gates
    # this sentence out once any row exists). Telling that contract to
    # "declare a hard limit" is false; it already has. Say what's missing
    # instead: a recording to compare it against.
    already_declared = nothing_watched and any(
        _device_declared(contract, device) for device in contract.devices
    )
    next_step = "Nothing watched yet" if nothing_watched else None
    if not nothing_watched:
        next_action = None
    elif already_declared:
        next_step = "Nothing compared yet"
        next_action = (
            "nothing has been compared yet against what's already declared for "
            "this contract — run `lockwire preflight <recording-dir> --context "
            "<context>` (or `lockwire baseline` to add a comparison) against a "
            "recording"
        )
    else:
        next_action = (
            "nothing is watched yet — declare a hard limit on a device model, or "
            "run `lockwire baseline <recording-dir> --context <context>` to "
            "author a baseline to compare against"
        )

    integrity = None
    if anchored:
        expected_count, expected_head = health_anchor
        observed_head = str(raw[-1].get("hash") or "") if raw else CHAIN_ZERO
        issues: list[str] = []
        try:
            ledger_after = path.read_bytes()
        except OSError:
            ledger_after = None
        if ledger_bytes is None or ledger_after != ledger_bytes:
            issues.append("ledger changed while the health result was read")
        if broken is not None:
            issues.append(f"ledger hash chain broken at row {broken[0]}: {broken[1]}")
        if len(raw) != expected_count:
            issues.append(f"ledger row count observed {len(raw)}, expected {expected_count}")
        if observed_head != expected_head:
            issues.append(f"ledger head observed {observed_head}, expected {expected_head}")
        contract_digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        if parsed_contract_sha256 != contract_digest:
            issues.append("selected contract bytes changed between parse and health read")
        if not any(ref.sha256 == contract_digest for ref in bundle_dependencies):
            issues.append(f"bundle contract identity {contract_digest} has no matching dependency")
        for ref in bundle_dependencies:
            try:
                read_artifact(ref, evidence_root)
            except (OSError, ValueError) as error:
                issues.append(f"bundle dependency {ref.path}: {error}")
        integrity = {"ok": not issues, "issues": issues}

    return HealthDocument(
        path=str(path),
        origin=origin,
        contract_sha=sha,
        devices=[{**d, "name": human_name(d["id"])} for d in devices],
        foreign_rows=len(foreign),
        series_excluded_foreign=len(foreign),
        series_excluded_unplaceable=series_excluded_unplaceable,
        not_watched=not_watched,
        scored=scored,
        chain=chain,
        summary=summary(own, now, skipped=skipped),
        verdict="NO-GO"
        if integrity is not None and not integrity["ok"]
        else verdict_word(decision.code),
        exit_code=1 if integrity is not None and not integrity["ok"] else decision.exit_code,
        integrity=integrity,
        findings=findings,
        nothing_watched=nothing_watched,
        next_step=next_step,
        next_action=next_action,
    )


def what_if_health(contract_path: Path | str) -> dict[str, Any]:
    """The health provider ``whatif.what_if`` takes: ledger rows and the ones a proposal invalidates.

    Pass as ``what_if(contract, changes, **what_if_health(contract))``. The ledger
    is also byte-watched, so a what-if can never write it.
    """
    ledger = sidecar_path_for(Path(contract_path).resolve())

    def recorded_health(original: Path, current: Contract, proposed: Contract):
        baseline, _foreign = own_and_foreign(read_ledger(ledger), original)
        return baseline, invalidated_recorded_rows(baseline, current, proposed)

    return {"recorded_health": recorded_health, "watch": (("health ledger", ledger),)}
