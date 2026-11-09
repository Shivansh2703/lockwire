"""The paid half of the CLI: commands that need the recording, baseline, health, preflight
and sidecar modules. Importing this module (from `lockwire_pro.plugin`) adds them to the free
`lockwire.cli.app`, so the free `lockwire.cli` imports none of it."""

import datetime as dt
import hashlib
import io
import json
import math
import os
import signal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML, YAMLError

from lockwire_pro.baseline import (
    baseline_record_report,
    parse_baseline_artifact,
    build_baseline,
    check_recording,
    parse_baseline_review,
    render_baseline_review,
)
from lockwire.cli_line import argv_from_context, cli_line as build_cli_line
from lockwire_pro.health import (
    append_health_rows,
    build_document as build_health_document,
    sidecar_path_for,
)
from lockwire_pro.preflight.runner import SamplesUnavailable, run_preflight
from lockwire_pro.preflight.source import LiveSource
from lockwire_pro.recording import load_recording
from lockwire.report import (
    RecordReport,
    exit_code_help,
    render,
    strict_exit,
)
from lockwire_pro.preflight.report import preflight_policy
from lockwire_pro.preflight.report_html import write_preflight_html
from lockwire.report_json import (
    ARTIFACT_TYPE,
    HEALTH_ARTIFACT_TYPE,
    baseline_check_document,
    envelope,
    preflight_document,
    write_preflight_json,
)
from lockwire.review_draft import ReviewSpecChange
from lockwire_pro.sample_local import DEFAULT_DEVICE, sample_local
from lockwire_pro.sample_remote import sample_remote
from lockwire.scan import (
    docker_fetch,
    docker_runner,
    local_fetch,
    ssh_fetch,
    ssh_runner,
    subprocess_runner,
)
from lockwire.schema import (
    ArtifactRef,
    CONTEXTS,
)
from lockwire.confine import confine
from lockwire.cli import (
    DEFAULT_CONTRACT,
    _refused,
    _require_folder,
    _write_document,
    app,
    console,
    load_contract,
)
from lockwire_pro import api_pro


_STDOUT_JSON = Path("-")


def _json_console(json_out: Path | None) -> Console:
    """The console a `--json` command should print its normal chatter to.

    `--json -` means the JSON document itself owns stdout for a pipe (`| jq`);
    every other print — the rich table, "wrote ...", errors — is rerouted to
    stderr instead of suppressed, so a human at a real terminal still sees it
    and only the document reaches stdout. Anything else (a real path, or no
    `--json` at all) keeps using the shared stdout console unchanged.
    """
    return Console(stderr=True) if json_out == _STDOUT_JSON else console


def _write_json_or_stdout(text: str, out: Path) -> None:
    """Send `text` (already-serialized JSON) to `out`, or to stdout for `-`.

    `-` prints with plain `print`, not `console.print` — bypassing rich means
    no styling/ANSI can ever land in the piped stream. A real path keeps
    writing the exact bytes callers built before `-` existed.
    """
    if out == _STDOUT_JSON:
        print(text)
    else:
        out.write_text(text, encoding="utf-8")


def _parse_draft_evidence(value: str | None) -> ArtifactRef | None:
    """Parse the draft command's one selected evidence artifact."""
    if value is None:
        return None
    relative_path, separator, sha256 = value.rpartition("@")
    if not separator or not relative_path or not sha256:
        raise ValueError("evidence must be RELATIVE_PATH@SHA256")
    return ArtifactRef(path=relative_path, sha256=sha256)


@app.command(
    "actuator-review-draft",
    hidden=True,
    help=(
        "Prepare one evidence-bound actuator correction for engineer review only; "
        "it never applies or configures anything.\n\n" + exit_code_help("check")
    ),
)
def actuator_review_draft(
    ctx: typer.Context,
    contract: Path = typer.Argument(..., help="Contract containing the declared actuator case."),
    case_id: str = typer.Argument(..., help="Declared actuator case id to review."),
    evidence: str | None = typer.Option(
        None, "--evidence", help="Selected RELATIVE_PATH@SHA256 evidence artifact."
    ),
    evidence_root: Path | None = typer.Option(
        None, "--evidence-root", help="Root containing selected evidence paths."
    ),
    expected_revision: str | None = typer.Option(
        None, "--expected-revision", help="Full raw contract SHA-256 used to select evidence."
    ),
    provenance: str = typer.Option("", "--provenance", help="Evidence provenance for review."),
    discrepancy: str = typer.Option("", "--discrepancy", help="Observed discrepancy to review."),
    alternatives: list[str] | None = typer.Option(
        None, "--alternative", help="Repeat a non-applying alternative considered."
    ),
    proposed_target: str | None = typer.Option(
        None,
        "--proposed-target",
        help="One of physical, firmware, configuration, model, or calibration.",
    ),
    proposed_patch: str = typer.Option(
        "", "--proposed-patch", help="Non-applying proposed change."
    ),
    proposed_changes: list[str] | None = typer.Option(
        None,
        "--proposed-change",
        help="Repeat a JSON object with path, value, source, witness, and locked_at for a spec draft.",
    ),
    rationale: str = typer.Option("", "--rationale", help="Why this evidence supports review."),
    correction_category: str | None = typer.Option(
        None,
        "--correction-category",
        help="One of physical, model_calibration, or spec_design.",
    ),
    required_recheck: list[str] | None = typer.Option(
        None, "--required-recheck", help="Repeat a required recheck after review."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the exact `lockwire.api.actuator_review_draft` document."
    ),
) -> None:
    """Prepare a read-only actuator correction draft; it cannot apply a correction."""
    if not contract.exists():
        console.print(f"[red]no contract at {contract}[/]")
        raise typer.Exit(2)
    try:
        from lockwire_pro import api_pro  # paid surface, loaded lazily

        document = api_pro.actuator_review_draft(
            contract,
            case_id,
            evidence=_parse_draft_evidence(evidence),
            evidence_root=evidence_root,
            expected_revision=expected_revision,
            provenance=provenance,
            discrepancy=discrepancy,
            alternatives=alternatives or (),
            proposed_target=proposed_target,
            proposed_patch=proposed_patch,
            proposed_changes=(
                [ReviewSpecChange.model_validate_json(raw).to_change() for raw in proposed_changes]
                if proposed_changes is not None
                else None
            ),
            rationale=rationale,
            correction_category=correction_category,
            required_recheck=required_recheck or (),
        )
    except (OSError, TypeError, ValueError, YAMLError) as error:
        _refused("actuator-review-draft", error)
        raise typer.Exit(2) from error
    _write_document(json_out, document, contract)
    console.print(f"[bold]{document['verdict']}[/] — {document['headline']}")
    console.print(
        "ran: " + build_cli_line("lockwire", "actuator-review-draft", *argv_from_context(ctx)),
        soft_wrap=True,
    )
    raise typer.Exit(document["exit_code"])


@app.command(hidden=True)
def baseline(
    recording: Path = typer.Argument(
        ..., exists=True, file_okay=False, readable=True, help="Known-good recording directory."
    ),
    context: str = typer.Option(
        ..., "--context", help=f"Operating context: {', '.join(CONTEXTS)}."
    ),
    rationale: str = typer.Option(
        None,
        "--rationale",
        help="Why this recording is known-good (omission remains an explicit evidence gap).",
    ),
) -> None:
    """Derive a reviewable envelope artifact from a known-good recording."""
    try:
        artifact = build_baseline(
            load_recording(recording),
            context=context,
            known_good_rationale=rationale,
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        console.print(f"[red]baseline failed:[/] {error}")
        raise typer.Exit(2) from error

    yaml = YAML()
    yaml.default_flow_style = False
    output = io.StringIO()
    yaml.dump(artifact.to_dict(), output)
    typer.echo(output.getvalue(), nl=False)


@app.command(hidden=True)
def baseline_review(
    artifact: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Baseline artifact to review."
    ),
) -> None:
    """Show the evidence a human needs before deciding whether to bless a baseline."""
    try:
        data = YAML(typ="safe").load(artifact)
        review = parse_baseline_review(data)
    except (OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]baseline review failed:[/] {error}")
        raise typer.Exit(2) from error

    render_baseline_review(review, console)


@app.command(hidden=True)
def baseline_check(
    recording: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        readable=True,
        help="Recording directory to compare against the blessed baseline.",
    ),
    baseline: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Blessed baseline artifact (YAML from `lockwire baseline`).",
    ),
    contract: Path = typer.Option(
        DEFAULT_CONTRACT,
        "--contract",
        help="Contract file the baseline's devices belong to (for the health ledger).",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write this same run as a versioned JSON document."
    ),
    no_ledger: bool = typer.Option(
        False, "--no-ledger", help="Skip appending to the health ledger sidecar."
    ),
) -> None:
    """Compare a recording to a blessed baseline and render findings.

    Sibling of ``baseline`` / ``baseline-review``: those produce and inspect the
    artifact; this is the operator path that surfaces named baseline findings
    (including ``drift-cusum``) through the same report grammar as ``check``.
    """
    try:
        artifact = parse_baseline_artifact(YAML(typ="safe").load(baseline))
        loaded_recording = load_recording(recording)
        findings = check_recording(loaded_recording, artifact)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]baseline check failed:[/] {error}")
        raise typer.Exit(2) from error

    from lockwire_pro import api_pro  # paid surface, loaded lazily

    passed = api_pro.passed_baseline_channels(findings, artifact)
    report = baseline_record_report(findings, passed)
    _write_document(json_out, baseline_check_document(report, str(recording)), contract)
    exit_code = render(report, console)

    if not no_ledger:
        try:
            loaded_contract = load_contract(contract)
            written = append_health_rows(
                contract,
                loaded_contract,
                report,
                source=f"baseline-check {recording} {baseline}",
                bag_sha=loaded_recording.sha256,
                context=artifact.context,
                now=dt.datetime.now(dt.UTC),
            )
            console.print(f"health: {written} parts recorded -> {sidecar_path_for(contract)}")
        except Exception as error:  # noqa: BLE001 - a ledger failure must never change the verdict
            console.print(f"[yellow]health ledger not written: {error}[/]")

    raise typer.Exit(exit_code)


def _chain_verified_line(chain: dict[str, Any], path: str) -> str:
    """The "chain verified" line — E3-A4-r2: `chain['rows']` alone once read
    as "N usable rows" while a corrupt/unparseable row it had already
    dropped stayed silently uncounted (the false-reassurance bounce). `rows`
    stays the raw appended-line count (what the hash chain actually walked
    to prove nothing was deleted or reordered); `skipped` — the same value
    `read_ledger`/`read_ledger_with_skipped` print to stderr — now always
    rides beside it so the two numbers can't drift apart in the reader's head."""
    return (
        f"health: chain verified — {chain['rows']} rows "
        f"({chain['skipped']} skipped), no breaks ({path})"
    )


@app.command(
    hidden=True,
    help="Read the health ledger sidecar and print each device's current state."
    f"\n\n{exit_code_help('health')}",
)
def health(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        help="Contract whose health ledger sidecar to read.",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write the summary as a versioned JSON document."
    ),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Walk the ledger's hash chain and name the first line that does not hold.",
    ),
    ledger: Path | None = typer.Option(
        None,
        "--ledger",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Read this ledger file instead of the contract's own sidecar — "
        "e.g. one copied off the robot.",
    ),
    anchor_rows: int | None = typer.Option(None, "--anchor-rows"),
    anchor_head: str | None = typer.Option(None, "--anchor-head"),
    bundle_dependency: list[str] | None = typer.Option(
        None, "--bundle-dependency", help="Repeat RELATIVE_PATH@SHA256 for held bundle files."
    ),
    evidence_root: Path | None = typer.Option(None, "--evidence-root"),
) -> None:
    """Read the health ledger sidecar and print each device's current state.

    Never actuates, never predicts — this only compares what the ledger
    already recorded. Degrading/stale are notes, not failures.
    """
    console = _json_console(json_out)
    anchored = any(
        value is not None for value in (anchor_rows, anchor_head, bundle_dependency, evidence_root)
    )
    try:
        parsed_digest = hashlib.sha256(contract.read_bytes()).hexdigest() if anchored else None
        loaded = load_contract(contract)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
        console.print(f"[red]health failed:[/] {error}")
        raise typer.Exit(2) from error

    now = dt.datetime.now(dt.UTC)
    try:
        references = (
            tuple(_parse_draft_evidence(value) for value in bundle_dependency)
            if bundle_dependency is not None
            else None
        )
        document = build_health_document(
            contract,
            loaded,
            now=now,
            ledger=ledger,
            health_anchor=(anchor_rows, anchor_head)
            if anchor_rows is not None or anchor_head is not None
            else None,
            bundle_dependencies=references,
            evidence_root=evidence_root,
            parsed_contract_sha256=parsed_digest,
        )
    except (ValueError, OSError) as error:
        console.print(f"[red]health failed:[/] {error}")
        raise typer.Exit(2) from error
    path = document.path
    chain = document.chain
    origin = document.origin

    if verify:
        if document.integrity is not None and not document.integrity["ok"]:
            for issue in document.integrity["issues"]:
                console.print(f"[red]health integrity:[/] {issue}")
            raise typer.Exit(1)
        if not chain["rows"]:
            console.print(f"health: no rows to verify in {path} — NOT SCORED")
            raise typer.Exit(3)
        if not chain["ok"]:
            console.print(
                f"[red]health: chain broken at line {chain['line']}[/] — {chain['why']} ({path})"
            )
            raise typer.Exit(1)
        console.print(_chain_verified_line(chain, path))
        raise typer.Exit(0)

    # Every open — not only `--verify` — says where the ledger came
    # from and walks the chain, so a copied, edited ledger never reads clean
    # just because nobody asked for `--verify`.
    if origin["copied_at"] is not None:
        console.print(f"health: ledger {origin['path']} (copied {origin['copied_at']})")
    if chain["rows"]:
        if chain["ok"]:
            console.print(_chain_verified_line(chain, path))
        else:
            console.print(
                f"[red]health: chain broken at line {chain['line']}[/] — {chain['why']} ({path})"
            )

    # `nothing_watched` alone conflates "never run" with "ran and
    # compared nothing" — a device can have recorded history (`runs > 0`)
    # even when this run scored nothing. Day one means no rows; hiding a
    # recorded run behind the same sentence made the two states
    # indistinguishable at the CLI, so only suppress the table when there is
    # truly no history for any device.
    has_history = any(entry["runs"] for entry in document.devices)
    if document.nothing_watched and not has_history:
        # No applicable comparison exists anywhere yet —
        # one sentence naming what to author, no table of empty rows, no
        # band, no series.
        console.print(f"health: {document.next_action}")
    else:
        table = Table(title=f"health — {path}")
        table.add_column("device")
        table.add_column("state")
        table.add_column("last run")
        table.add_column("reason")
        table.add_column("runs recorded", justify="right")
        for entry in document.devices:
            # A rail/battery kind is shown as a "(kind)" prefix rather than its own
            # column — a column widens every row and starves the
            # "reason" column's wrap width for every device, not just rails.
            # Parens, not brackets: rich's Table markup parses "[rail]" as a
            # (unknown, silently dropped) style tag.
            label = f"({entry['kind']}) {entry['id']}" if entry["kind"] != "device" else entry["id"]
            table.add_row(
                label, entry["word"], entry["run"] or "-", entry["reason"], str(entry["runs"])
            )
        console.print(table)
        if document.foreign_rows:
            console.print(
                f"{document.foreign_rows} row(s) recorded against another contract version — "
                "kept as history, never as current state"
            )
        if document.not_watched:
            console.print(f"{document.not_watched} part(s) not watched")
        # Each row's own contract sha + bag sha, printed as a compact
        # line per row rather than two more table columns — adding columns to
        # the table above narrows the existing ones enough to change their
        # word-wrap, which several tests assert exact substrings against.
        # "-" means this specific row carries none (a historical row from
        # before `bag_sha` existed) — never a guessed value.
        for entry in document.devices:
            console.print(
                f"  {entry['id']}: contract sha {entry.get('contract_sha') or '-'} "
                f"· bag sha {entry.get('bag_sha') or '-'}"
            )
        # "Why faulty" opens THAT recording — this row's
        # own `source`, resolved by device (never the ledger's newest row
        # overall, which can belong to a different device entirely).
        for entry in document.devices:
            if entry["state"] != "faulty":
                continue
            recording = entry.get("recording")
            if recording:
                console.print(f"why {entry['id']} is faulty: inspect {recording}")
            else:
                console.print(f"why {entry['id']} is faulty: no recording on record for this row")

    # The verdict is the document's, printed here and never recomputed: `render`
    # is handed the document's own scored count and findings.
    if document.integrity is None:
        render(
            RecordReport(
                document.findings,
                # exit 3 is the document's NOT SCORED cap; render must not re-clear it
                scored=0 if document.exit_code == 3 else document.scored,
                scored_keys=frozenset(f.quantity for f in document.findings),
            ),
            console,
        )
    else:
        for issue in document.integrity["issues"]:
            console.print(f"[red]health integrity:[/] {issue}")
        console.print(f"health integrity: {document.verdict}")

    if json_out is not None:
        body = envelope(HEALTH_ARTIFACT_TYPE, document.summary)
        if json_out == _STDOUT_JSON:
            _write_json_or_stdout(json.dumps(body, indent=2, default=str), json_out)
        else:
            _write_document(json_out, body, contract)

    # A broken chain is never silently accepted, even outside
    # `--verify`: the rows still print, but the exit code says NO-GO.
    exit_code = 1 if chain["rows"] and not chain["ok"] else document.exit_code
    raise typer.Exit(exit_code)


@app.command(hidden=True)
def sample(
    local: bool = typer.Option(
        False,
        "--local",
        help="Sample this machine's own /sys + /proc as a recording.",
    ),
    host: str | None = typer.Option(
        None,
        "--host",
        help=(
            "Sample [user@]HOST's own /sys + /proc over ssh, read-only "
            "One snapshot; --seconds/--hz do not apply."
        ),
    ),
    port: int | None = typer.Option(
        None, "--port", help="ssh port for --host. Optional: plain ssh is the default."
    ),
    known_hosts: Path | None = typer.Option(
        None, "--known-hosts", help="Explicit pinned SSH host keys; required with --host."
    ),
    seconds: float = typer.Option(5.0, "--seconds", help="How long to sample."),
    hz: float = typer.Option(1.0, "--hz", help="Sampling frequency, in Hz."),
    out: Path = typer.Option(..., "--out", help="Recording directory to write."),
    device: str = typer.Option(
        DEFAULT_DEVICE, "--device", help="Device id to record signals under."
    ),
    contract: Path | None = typer.Option(
        None,
        "--contract",
        help="Warn (not fail) for recorded signals this contract does not declare.",
    ),
    sysfs_root: Path = typer.Option(Path("/sys"), "--sysfs-root", hidden=True),
    proc_root: Path = typer.Option(Path("/proc"), "--proc-root", hidden=True),
) -> None:
    """Record health sources with no hardware needed: the robot's own computer."""
    _require_folder(out)
    if local and host is not None:
        console.print("[red]sample failed:[/] pass --local or --host, not both")
        raise typer.Exit(2)
    if not local and host is None:
        console.print("[red]sample failed:[/] pass --local or --host <[user@]host>")
        raise typer.Exit(2)
    if host is not None:
        try:
            sample_remote(
                host=host,
                known_hosts=known_hosts,
                port=port,
                out_dir=out,
                device=device,
                contract_path=contract,
            )
        except (OSError, ValueError, YAMLError) as error:
            console.print(f"[red]sample failed:[/] {error}")
            raise typer.Exit(2) from error
        console.print(f"[green]wrote {out}[/]")
        return
    try:
        sample_local(
            seconds=seconds,
            hz=hz,
            out_dir=out,
            device=device,
            sysfs_root=sysfs_root,
            proc_root=proc_root,
            contract_path=contract,
        )
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
        _refused("sample", error)
        raise typer.Exit(2) from error
    console.print(f"[green]wrote {out}[/]")


@app.command(
    help="Run the generated go/no-go gate against a recording (offline) or a live robot."
    f"\n\n{exit_code_help('preflight')}"
)
def preflight(
    contract: Path = typer.Argument(
        DEFAULT_CONTRACT,
        help="Contract file to gate against.",
    ),
    recording: Path | None = typer.Option(
        None,
        "--from",
        help=(
            "Recording directory, PX4 .ulg, or rosbag2 .mcap file / bag directory "
            "(RecordedSource; the offline path). Exclusive with --live."
        ),
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Sample a running ROS 2 robot instead of a recording. Exclusive with --from.",
    ),
    container: str | None = typer.Option(
        None,
        "--container",
        help="Docker container name/id to sample --live against (via docker exec/cp).",
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Sample --live against the machine lockwire is running on (no container).",
    ),
    ssh_host: str | None = typer.Option(None, "--ssh-host", help="Declared SSH live target host."),
    ssh_port: int = typer.Option(22, "--ssh-port", help="SSH live target port."),
    ssh_user: str = typer.Option("root", "--ssh-user", help="SSH live target user."),
    ssh_identity_file: Path | None = typer.Option(
        None, "--ssh-identity-file", help="SSH private key."
    ),
    ssh_known_hosts: Path | None = typer.Option(
        None, "--ssh-known-hosts", help="Pinned SSH host keys."
    ),
    ssh_target: str | None = typer.Option(
        None, "--ssh-target", help="Caller-declared target name."
    ),
    ssh_image: str | None = typer.Option(
        None, "--ssh-image", help="Caller-declared image identity; not measured by SSH."
    ),
    seconds: float = typer.Option(
        10.0,
        "--seconds",
        help="How long to record with --live before scoring the capture.",
    ),
    ros_distro: str = typer.Option(
        "jazzy",
        "--ros-distro",
        help="ROS 2 distro to source inside --container for --live (e.g. jazzy).",
    ),
    keep_bag: Path | None = typer.Option(
        None,
        "--keep-bag",
        help="Keep the --live capture here instead of a temp dir that gets cleaned up.",
    ),
    context: str = typer.Option(
        "nominal-op",
        "--context",
        help=(
            "Context to read a foreign format under (.ulg / .mcap). Selects which "
            "declared envelopes apply. Ignored for a lockwire recording directory, "
            "which states its own context."
        ),
    ),
    html: Path | None = typer.Option(
        None,
        "--html",
        help="Also write a self-contained graphical view of this same run.",
    ),
    json_out: Path | None = typer.Option(
        None,
        "--json",
        help="Also write this same run as JSON, for an out-of-process consumer. "
        "verdict is spelled NO-GO (hyphenated), not the Python identifier NO_GO.",
    ),
    no_ledger: bool = typer.Option(
        False,
        "--no-ledger",
        help="Skip appending to the health ledger sidecar; preflight appends by default.",
    ),
    profile: str = typer.Option("analysis", "--profile", help="analysis or powered-release."),
    required_evidence: list[str] | None = typer.Option(
        None,
        "--require-evidence",
        help="Required evidence class; repeat for each class. "
        "powered-release requires a nonempty explicit list.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as failures (a GO with notes exits 1) and any unjudged check as exit 3.",
    ),
    max_not_scored: int | None = typer.Option(
        None,
        "--max-not-scored",
        help="Exit 3 when more than N checks went unjudged (default: unlimited; 0 with --strict).",
    ),
) -> None:
    """Run the generated go/no-go gate against a recording (offline) or a live robot.

    Exit codes (contract §3.3), rendered for `--help` from
    ``report.EXIT_CODE_TABLE["preflight"]``: 0 GO variants, 1 NO-GO, 2 refused
    run, 3 NOT SCORED.
    """
    console = _json_console(json_out)

    def _refuse(message: str) -> typer.Exit:
        """Print a tool-side refusal (exit 2) and, with --json, write it."""
        console.print(f"[red]preflight failed:[/] {message}")
        if json_out is not None:
            document = envelope(
                ARTIFACT_TYPE,
                {
                    "source": str(recording) if recording is not None else "live",
                    "verdict": "REFUSED",
                    "exit_code": 2,
                    "reason": message,
                    "findings": [],
                },
            )
            try:
                target = json_out if json_out == _STDOUT_JSON else confine(json_out, contract)
                _write_json_or_stdout(json.dumps(document, indent=2), target)
            except (OSError, ValueError) as error:
                console.print(f"[red]preflight failed:[/] could not write {json_out}: {error}")
        return typer.Exit(2)

    # Do not use typer exists=/readable= gates here: those surface as typer's
    # own exit 2 and would make the mutation-bite (Exit(2)→Exit(1)) invisible.
    try:
        profile, required_evidence = preflight_policy(profile, required_evidence)
    except ValueError as error:
        raise _refuse(str(error)) from error
    if not contract.exists() or not contract.is_file():
        raise _refuse(f"no contract at {contract}")
    if not math.isfinite(seconds):
        raise _refuse("--seconds must be a finite number")

    if live and recording is not None:
        raise _refuse("--live is exclusive with --from")
    ssh_selected = ssh_host is not None
    if ssh_selected and (container is not None or local):
        raise _refuse("--ssh-host is exclusive with --container/--local")
    if ssh_selected and (
        not live or not all((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image))
    ):
        raise _refuse(
            "SSH live capture needs --live, key, known hosts, target and image declarations"
        )
    if not ssh_selected and any((ssh_identity_file, ssh_known_hosts, ssh_target, ssh_image)):
        raise _refuse("SSH options need --ssh-host")
    if live and container is not None and local:
        raise _refuse("--container is exclusive with --local")
    if live and container is None and not local and not ssh_selected:
        raise _refuse("--live needs --container NAME, --local, or --ssh-host")
    if not live and recording is None:
        raise _refuse("pass --from RECORDING or --live")

    try:
        loaded = load_contract(contract)
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
        raise _refuse(str(error)) from error

    if live:
        target = (
            f"SSH {ssh_target} at {ssh_user}@{ssh_host}:{ssh_port} (declared image {ssh_image})"
            if ssh_selected
            else f"container {container}"
            if container
            else "local"
        )
        # Runner timeout must clear the recorder's own timeout (seconds+3) with slack
        # for process teardown, or the runner would abort the capture prematurely.
        runner_timeout = seconds + 10
        if ssh_selected:
            assert ssh_host is not None and ssh_identity_file is not None
            assert ssh_known_hosts is not None
            if not ssh_identity_file.is_file() or not ssh_known_hosts.is_file():
                raise _refuse("SSH key and known-hosts files must exist")
            try:
                live_runner = ssh_runner(
                    ssh_host,
                    port=ssh_port,
                    user=ssh_user,
                    identity_file=ssh_identity_file,
                    known_hosts=ssh_known_hosts,
                    timeout=runner_timeout,
                    ros_distro=ros_distro,
                )
            except ValueError as error:
                raise _refuse(str(error)) from error
        elif container:
            try:
                live_runner = docker_runner(
                    container, timeout=runner_timeout, ros_distro=ros_distro
                )
            except ValueError as error:
                raise _refuse(str(error)) from error
        else:

            def live_runner(command: tuple[str, ...], _timeout: float = runner_timeout):
                return subprocess_runner(command, timeout=_timeout)

        # A tool failure must never be attributed to the robot: probe for
        # ros2 before any stage runs. A missing ros2 (127, "command not found")
        # would otherwise make every stage score declared drivers as absent —
        # refuse up front instead, before anything is scored or written. 127
        # specifically: some other nonzero (a real `ros2 --help` failure) is
        # not this refusal's business.
        probe = live_runner(("ros2", "--help"))
        if ssh_selected and probe.returncode != 0:
            detail = probe.stderr.strip() or str(probe.returncode)
            raise _refuse(f"verified SSH target probe failed: {detail}")
        if probe.returncode == 127:
            raise _refuse(
                "ros2 is not on this machine's PATH — run on "
                "the robot, use --container, or source your ROS 2 workspace"
            )

        source = LiveSource(
            contract=loaded,
            runner=live_runner,
            fetch=(
                (
                    lambda remote, local: ssh_fetch(
                        ssh_host,
                        port=ssh_port,
                        user=ssh_user,
                        identity_file=ssh_identity_file,
                        known_hosts=ssh_known_hosts,
                        remote_dir=remote,
                        local_dir=local,
                        timeout=runner_timeout + 50,
                    )
                )
                if ssh_selected
                else docker_fetch(container)
                if container
                else local_fetch
            ),
            robot=ssh_target if ssh_selected else "live-target",
            seconds=seconds,
            keep_dir=keep_bag,
            # Reachability (E3-QC): a SIGTERM/SIGINT handler installed just
            # below calls `source.cancel()` before this process exits, so it
            # needs the recorder's container-side pid — `cancellable=True` is
            # what makes `_capture_once` learn it while the capture is in
            # flight rather than only after it returns.
            cancellable=True,
        )
        topics = sorted(
            {
                device.driver.topic
                for device in loaded.devices
                if device.driver is not None and device.driver.topic
            }
        )
        topics_desc = ", ".join(topics) if topics else "no declared topics"
        console.print(
            f"capturing {seconds:g} s of {topics_desc} on {target} (ros2 bag record, mcap)"
        )
        # A signal during the capture is what `Job.cancel()` (`lockwire_pro.jobs`)
        # sends this process's group: without a handler here the local `docker
        # exec` client dies and the container-side recorder keeps recording
        # (stopping our own recorder is not actuation). The handler
        # kills-and-proves the recorder first. The signal is re-raised after
        # the finalized partial bag is fetched and surfaced (E3-QC).
        _live_prior_handlers: dict[int, Any] = {}
        _live_cancel_signal: int | None = None

        def _cancel_live_capture(signum: int, _frame: Any) -> None:
            nonlocal _live_cancel_signal
            if source.cancel():
                _live_cancel_signal = signum
            else:
                console.print(
                    "[red]live capture cancellation could not prove the recorder stopped[/]"
                )

        for _sig in (signal.SIGTERM, signal.SIGINT):
            _live_prior_handlers[_sig] = signal.signal(_sig, _cancel_live_capture)
        try:
            try:
                captured = source.samples()
            except SamplesUnavailable as error:
                console.print(f"[yellow]{error}[/]")
            else:
                suffix = " (partial capture)" if captured.partial else ""
                console.print(f"captured {source.message_count} messages{suffix}")
                if source.capture_dir:
                    mcap_files = sorted(source.capture_dir.glob("*.mcap"))
                    if mcap_files:
                        console.print(f"  {mcap_files[0].name}")
        finally:
            for _sig, _handler in _live_prior_handlers.items():
                signal.signal(_sig, _handler)
        if _live_cancel_signal is not None:
            os.kill(os.getpid(), _live_cancel_signal)
        recording_desc = f"live: {target}, {seconds:g} s, {source.message_count} msgs"
    else:
        assert recording is not None  # validated above
        try:
            from lockwire_pro import api_pro  # paid surface, loaded lazily

            source = api_pro.open_recording(recording, loaded, context=context)
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
            raise _refuse(str(error)) from error
        recording_desc = str(recording)

    # Broad catch is intentional: stage bodies are not an enumerable failure set.
    # typer.Exit from render stays outside this try so NO-GO (1) is never remapped to 2.
    try:
        policy_args = (
            {"profile": profile, "required_evidence": required_evidence}
            if (profile != "analysis" or required_evidence)
            else {}
        )
        findings = run_preflight(loaded, source, **policy_args)
    except Exception as error:
        raise _refuse(str(error)) from error
    exit_code = strict_exit(
        findings,
        render(findings, console),
        console,
        strict=strict,
        max_not_scored=max_not_scored,
    )
    if not no_ledger:
        try:
            source_desc = (
                f"preflight --from {recording}"
                if recording is not None
                else f"preflight --live {recording_desc}"
            )
            # Evidence coverage (E3-j): the raw per-device sample count only
            # exists here, at the write site, where the actual Recording is
            # in hand — `source.samples()` is cached on both source kinds
            # (RecordedSource/LiveSource), so this never re-captures.
            # The bag sha rides the same cached `source.samples()` call —
            # a `Recording` always carries its own `sha256` (over manifest +
            # samples, computed at load time); never a new hash invented here.
            try:
                sampled = source.samples()
                sample_counts = {
                    device_id: len(stream.recv_times)
                    for device_id, stream in sampled.devices.items()
                }
                bag_sha = getattr(sampled, "sha256", None)
            except SamplesUnavailable:
                sample_counts = None
                bag_sha = None
            written = append_health_rows(
                contract,
                loaded,
                findings,
                source=source_desc,
                sample_counts=sample_counts,
                bag_sha=bag_sha,
                context=context,
                now=dt.datetime.now(dt.UTC),
            )
            console.print(f"health: {written} parts recorded -> {sidecar_path_for(contract)}")
        except Exception as error:  # noqa: BLE001 - a ledger failure must never change the verdict
            console.print(f"[yellow]health ledger not written: {error}[/]")
    # A failed side-output must never erase a NO-GO (B115 bounce). It escalates a
    # GO to 2 — an unwritten artifact leaves the operator without the evidence the
    # flag promised — and both writes are attempted before either raises, so a
    # failing --html cannot silently skip --json and leave a consumer reading a
    # stale file from an earlier run.
    write_failed = False
    if html is not None:
        try:
            write_preflight_html(loaded, findings, recording_desc, html)
        except OSError as error:
            console.print(f"[red]preflight failed:[/] could not write {html}: {error}")
            write_failed = True
        else:
            console.print(f"[green]wrote {html}[/]")
    if json_out is not None:
        try:
            if json_out == _STDOUT_JSON:
                # Same bytes write_preflight_json would put on disk (indent=2,
                # trailing newline) — `-` only changes the destination.
                document = preflight_document(loaded, findings, recording_desc)
                _write_json_or_stdout(json.dumps(document, indent=2), json_out)
            else:
                write_preflight_json(loaded, findings, recording_desc, confine(json_out, contract))
        except ValueError as error:
            raise _refuse(str(error)) from error
        except OSError as error:
            console.print(f"[red]preflight failed:[/] could not write {json_out}: {error}")
            write_failed = True
        else:
            if json_out != _STDOUT_JSON:
                console.print(f"[green]wrote {json_out}[/]")
    if write_failed and exit_code == 0:
        raise typer.Exit(2)
    raise typer.Exit(exit_code)


bag_app = typer.Typer(
    help="ROS 2 bags: see what a bag holds, write three lines, gate it.", no_args_is_help=True
)
app.add_typer(bag_app, name="bag")

_BAG_ARG = typer.Argument(None, help="A rosbag2 .mcap file or bag folder.")
_EXAMPLE = typer.Option(False, "--example", help="Use the bundled sample bag (no robot needed).")


def _example_bag(folder: Path) -> tuple[Path, Path]:
    """The sample bag and its contract, both written into ``folder``."""
    import shutil

    from lockwire_pro.ros2bag_example import CONTRACT, write_example_bag

    return write_example_bag(folder), Path(shutil.copy(CONTRACT, folder / "lockwire.yaml"))


@bag_app.command("inspect")
def bag_inspect(
    bag: Path | None = _BAG_ARG,
    example: bool = _EXAMPLE,
    json_output: bool = typer.Option(False, "--json", help="Print the inventory as JSON."),
) -> None:
    """List a bag's topics, types, counts and rates, and the contract lines to write."""
    import tempfile

    from lockwire_pro.ros2bag import Ros2BagError, inspect_bag

    if (bag is None) == (not example):
        console.print("[red]bag inspect failed:[/] pass a bag path or --example, not both")
        raise typer.Exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            target = _example_bag(Path(tmp))[0] if example else bag
            assert target is not None
            inventory = inspect_bag(target)
        except Ros2BagError as error:
            console.print(f"[red]bag inspect failed:[/] {error}")
            raise typer.Exit(2) from error
    if example:
        inventory["bag"] = "--example"
    if json_output:
        print(json.dumps(inventory, indent=2))
        return
    table = Table(title=f"{inventory['bag']}  ({inventory['duration_s']} s)")
    for column in ("topic", "type", "messages", "rate Hz"):
        table.add_column(column)
    for row in inventory["topics"]:
        rate = "-" if row["rate_hz"] is None else f"{row['rate_hz']:g}"
        table.add_row(row["topic"], row["msg_type"], str(row["messages"]), rate)
    console.print(table)
    console.print("For each device you want gated, add to its entry in lockwire.yaml:")
    for row in inventory["topics"]:
        field = row["example_field"] or "FIELD"
        console.print(
            f"  driver: {{topic: {row['topic']}, msg_type: {row['msg_type']}, "
            f'signals: {{NAME: {{field: "{field}", unit: UNIT}}}}}}',
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    console.print("Then: lockwire bag check <bag> --lock lockwire.yaml", markup=False)


@bag_app.command("check")
def bag_check(
    bag: Path | None = _BAG_ARG,
    lock: Path = typer.Option(DEFAULT_CONTRACT, "--lock", help="Contract to gate the bag against."),
    context: str = typer.Option("nominal-op", "--context", help="Which declared envelopes apply."),
    example: bool = _EXAMPLE,
) -> None:
    """Gate a bag against the contract: `preflight --from BAG`, nothing more."""
    import tempfile

    if (bag is None) == (not example):
        console.print("[red]bag check failed:[/] pass a bag path or --example, not both")
        raise typer.Exit(2)
    preflight_command = typer.main.get_command(app).commands["preflight"]  # type: ignore[attr-defined]
    with tempfile.TemporaryDirectory() as tmp:
        if example:
            bag, lock = _example_bag(Path(tmp))
        argv = [str(lock), "--from", str(bag), "--context", context]
        code = preflight_command.main(argv, prog_name="lockwire preflight", standalone_mode=False)
    raise typer.Exit(code or 0)


def register_tools(server) -> None:
    """The paid MCP tools, added to the free server by `lockwire_pro.plugin`."""
    from mcp.server.mcpserver.exceptions import ToolError

    from typing import Annotated

    from lockwire.mcp_server import CONTRACT, REVISION, _arg, _cli_line, _load_contract
    from lockwire.mcp_server import _with_cli_line
    from lockwire.review_draft import CorrectionCategory, NonSpecTarget

    @server.tool()
    def prepare_actuator_review_draft(
        contract: CONTRACT,
        case_id: Annotated[str, _arg("The actuator case id the correction is for.")],
        evidence: Annotated[
            ArtifactRef | None, _arg("The evidence file {path, sha256} behind it.")
        ] = None,
        evidence_root: Annotated[
            str | None, _arg("Folder the evidence path is relative to.")
        ] = None,
        expected_revision: REVISION = None,
        provenance: Annotated[str, _arg("Where the evidence came from.")] = "",
        discrepancy: Annotated[str, _arg("What disagrees, in one line.")] = "",
        alternatives: Annotated[
            list[str] | None, _arg("Other explanations the engineer should rule out.")
        ] = None,
        proposed_target: Annotated[
            NonSpecTarget | None, _arg("What the correction would change.")
        ] = None,
        proposed_patch: Annotated[str, _arg("The proposed change, as text.")] = "",
        rationale: Annotated[str, _arg("Why this correction.")] = "",
        correction_category: Annotated[
            CorrectionCategory | None, _arg("The kind of correction.")
        ] = None,
        required_recheck: Annotated[
            list[str] | None, _arg("Checks to re-run after the correction.")
        ] = None,
    ) -> dict[str, Any]:
        """Prepare one evidence-bound actuator correction draft for engineer review only; it never applies a correction or changes robot state."""
        _load_contract(contract)
        try:
            document = api_pro.actuator_review_draft(
                contract,
                case_id,
                evidence=evidence,
                evidence_root=evidence_root,
                expected_revision=expected_revision,
                provenance=provenance,
                discrepancy=discrepancy,
                alternatives=alternatives or (),
                proposed_target=proposed_target,
                proposed_patch=proposed_patch,
                rationale=rationale,
                correction_category=correction_category,
                required_recheck=required_recheck or (),
            )
        except (OSError, TypeError, ValueError, YAMLError) as error:
            raise ToolError(f"REFUSED — {error}") from error

        line = ["actuator-review-draft", contract, case_id]
        if evidence is not None:
            line += ["--evidence", f"{evidence.path}@{evidence.sha256}"]
        if evidence_root is not None:
            line += ["--evidence-root", evidence_root]
        if expected_revision is not None:
            line += ["--expected-revision", expected_revision]
        if provenance:
            line += ["--provenance", provenance]
        if discrepancy:
            line += ["--discrepancy", discrepancy]
        for alternative in alternatives or ():
            line += ["--alternative", alternative]
        if proposed_target is not None:
            line += ["--proposed-target", proposed_target]
        if proposed_patch:
            line += ["--proposed-patch", proposed_patch]
        if rationale:
            line += ["--rationale", rationale]
        if correction_category is not None:
            line += ["--correction-category", correction_category]
        for recheck in required_recheck or ():
            line += ["--required-recheck", recheck]
        return _with_cli_line(document, _cli_line(*line))

    @server.tool()
    def preflight(
        contract: CONTRACT, recording: Annotated[str, _arg("The recording folder to gate.")]
    ) -> dict[str, Any]:
        """Run the go/no-go preflight gate against a recording, the same run `lockwire preflight --json` writes; refuses a bad contract or recording path."""
        loaded = _load_contract(contract)
        try:
            document = api_pro.preflight(loaded, recording)
        except (OSError, KeyError, TypeError, ValueError, YAMLError) as error:
            raise ToolError(str(error)) from error
        return _with_cli_line(document, _cli_line("preflight", contract, "--from", recording))

    @server.tool()
    def health(
        contract: CONTRACT,
        ledger: Annotated[
            str | None, _arg("The health ledger; default: beside the contract.")
        ] = None,
        *,
        anchor_rows: Annotated[int | None, _arg("Row count you hold for a copied ledger.")] = None,
        anchor_head: Annotated[str | None, _arg("Head hash you hold for a copied ledger.")] = None,
        bundle_dependencies: Annotated[
            list[dict[str, str]] | None, _arg("Bundle files {path, sha256} to verify.")
        ] = None,
        evidence_root: Annotated[
            str | None, _arg("Folder the bundle paths are relative to.")
        ] = None,
    ) -> dict[str, Any]:
        """Read a health ledger, optionally verifying a copied ledger against a caller-held head/count and content-addressed bundle files."""
        _load_contract(contract)
        try:
            references = (
                tuple(ArtifactRef.model_validate(ref) for ref in bundle_dependencies)
                if bundle_dependencies is not None
                else None
            )
            document = api_pro.health_document(
                contract,
                ledger=ledger,
                health_anchor=(anchor_rows, anchor_head)
                if anchor_rows is not None or anchor_head is not None
                else None,
                bundle_dependencies=references,
                evidence_root=evidence_root,
            )
        except (ValueError, OSError) as error:
            raise ToolError(str(error)) from error
        parts = ["health", contract]
        if ledger is not None:
            parts.extend(("--ledger", ledger))
        if references is not None:
            parts.extend(("--anchor-rows", str(anchor_rows), "--anchor-head", str(anchor_head)))
            for ref in references:
                parts.extend(("--bundle-dependency", f"{ref.path}@{ref.sha256}"))
            parts.extend(("--evidence-root", str(evidence_root)))
        return _with_cli_line(document, _cli_line(*parts))
