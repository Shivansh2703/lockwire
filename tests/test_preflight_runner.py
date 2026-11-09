"""Property tests over the preflight stage registry (B86).

No per-stage literal assertions — parallel stage boxes must not edit this file.
Matched identity / liveness paths use in-test synthetic contracts so the
positive path is not dark. B100 aligns the clean fixture ids with the bags;
this file still prefers synthetic matches for stage-0/1 property proofs.
"""

from __future__ import annotations

from pathlib import Path

from lockwire_pro.preflight.runner import (
    STAGES,
    SamplesUnavailable,
    StageEntry,
    _LazySource,
    _dispatch_stage,
    run_preflight,
)
from lockwire_pro.preflight.source import (
    DeviceObservation,
    LivenessObservation,
    PreflightSource,
    RecordedSource,
)
from lockwire_pro.recording import load_recording
from lockwire.report import Finding
from lockwire.schema import (
    Contract,
    DeviceIdentity,
    DeviceInstance,
    Driver,
    RobotInfo,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"


def _clean_pair():
    contract = load_contract(CLEAN_CONTRACT)
    source = RecordedSource(load_recording(CLEAN_BAG))
    return contract, source


def _matched_contract() -> Contract:
    """Minimal contract whose device id can be matched by an in-test source."""
    return Contract(
        robot=RobotInfo(name="matchbot"),
        devices=[
            DeviceInstance(
                id="dev.imu",
                model="mdl.imu",
                owner="test",
                identity=DeviceIdentity(
                    bus_ref="bus.usb0",
                    host="nav-computer",
                    vid_pid="108c:0088",
                ),
                driver=Driver(
                    node="/sensors/imu",
                    topic="/imu/data",
                    nominal_rate_hz=200.0,
                    payload_bytes=64,
                ),
            )
        ],
    )


class _StageScopedSource:
    """Enumerate/liveness double that deliberately cannot provide samples()."""

    def __init__(
        self,
        observations: list[DeviceObservation],
        live: list[LivenessObservation] | None = None,
    ) -> None:
        self._observations = observations
        self._live = live if live is not None else []
        self.samples_calls = 0

    def enumerate(self) -> list[DeviceObservation]:
        return list(self._observations)

    def liveness(self) -> list[LivenessObservation]:
        return list(self._live)

    def samples(self):  # pragma: no cover - raised into runner guard
        self.samples_calls += 1
        raise SamplesUnavailable("stage-scoped double does not provide samples()")


def test_every_registered_stage_findings_carry_that_stage_identity():
    """Property over STAGES: runner stamps each entry's stage onto its findings."""
    contract, source = _clean_pair()
    findings = run_preflight(contract, source)

    for entry in STAGES:
        produced = entry.check(contract, source)
        for finding in produced:
            matches = [
                f
                for f in findings
                if f.quantity == finding.quantity
                and f.observed == finding.observed
                and f.expected == finding.expected
            ]
            assert matches, f"{entry.name} produced {finding.quantity} but runner omitted it"
            assert all(f.stage == entry.stage for f in matches)

    assert all(f.stage is not None for f in findings)
    assert {f.stage for f in findings} <= {entry.stage for entry in STAGES}


def test_stage_contributing_zero_findings_is_implemented_or_emits_not_scored():
    """Unbuilt stages must never return [] — that is the false-GO silence shape.

    Implemented stages may contribute zero findings on a clean fixture.
    Unbuilt stubs must emit not_scored naming themselves as unbuilt.
    """
    contract, source = _clean_pair()

    for entry in STAGES:
        produced = entry.check(contract, source)
        if not produced:
            assert entry.implemented, (
                f"unbuilt stage {entry.name!r} returned [] — silent pass / false-GO"
            )
            continue
        if entry.implemented:
            continue
        assert all(f.severity == "not_scored" for f in produced)


def test_run_preflight_clean_fixture_goes_and_unbuilt_stages_disclose():
    """Clean fixture is GO; any still-unbuilt registry entry must disclose itself.

    When every stage is implemented, the unbuilt set is empty.
    """
    contract, source = _clean_pair()
    findings = run_preflight(contract, source)

    assert all(f.severity != "violation" for f in findings)
    unbuilt_stages = {entry.stage for entry in STAGES if not entry.implemented}
    assert unbuilt_stages <= {f.stage for f in findings if f.severity == "not_scored"}


def test_run_preflight_records_scored_records_for_passing_comparisons():
    """M1 health follow-up (H-a2): a stage comparison that PASSES must still
    leave a trace — ``report.scored_records`` — so a device with only passing
    signals can read ``healthy`` in the health ledger (health.py) instead of
    ``not_watched``. No per-stage literal here (B86 rule for this file): only
    the identity that the presence-floor count matches, and that at least one
    real device path is covered — never a specific stage's channel name.
    """
    contract, source = _clean_pair()
    report = run_preflight(contract, source)

    assert len(report.scored_records) > 0
    # At least one is device-prefixed (sanity/flow/cross_signal); cross_record's
    # budget passes are contract-level (``budgets/...``), not per-device.
    assert any(record.quantity.startswith("devices/") for record in report.scored_records)
    # Never double-counted against violations/warnings for the same comparison
    # (checks.py's presence-floor identity, reused here): a device-prefixed
    # scored_record's quantity never also carries a violation/warning finding.
    findings_by_quantity = {f.quantity for f in report if f.severity != "not_scored"}
    assert not (findings_by_quantity & {r.quantity for r in report.scored_records})


def test_matched_device_agreeing_descriptors_is_clean():
    """(a) Device present in both contract and source, descriptors agree → no stage-0 findings.

    Asserts only on stage 0 (enumeration) — later stages may emit findings or
    consult samples() once they are real; that must not fail this check.
    """
    contract = _matched_contract()
    device = contract.devices[0]
    assert device.identity is not None
    source = _StageScopedSource(
        [
            DeviceObservation(
                device_id=device.id,
                bus_ref=device.identity.bus_ref,
                host=device.identity.host,
                vid_pid=device.identity.vid_pid,
            )
        ]
    )
    findings = [f for f in run_preflight(contract, source) if f.stage == 0]
    assert findings == []


def test_matched_device_descriptor_mismatch_is_violation():
    """(b) Matched device, both operands populated, disagree → violation."""
    contract = _matched_contract()
    device = contract.devices[0]
    assert device.identity is not None
    source = _StageScopedSource(
        [
            DeviceObservation(
                device_id=device.id,
                bus_ref=device.identity.bus_ref,
                host=device.identity.host,
                vid_pid="0000:dead",
            )
        ]
    )
    findings = [f for f in run_preflight(contract, source) if f.stage == 0]
    violations = [f for f in findings if f.severity == "violation"]
    assert any(f.quantity == f"devices/{device.id}/identity/vid_pid" for f in violations)
    assert all(f.stage == 0 for f in violations)


def test_matched_device_observed_none_is_unseen_not_mismatch():
    """(c) Device found, observed descriptors None, declared populated → unseen, not violation.

    Bite: delete enumeration.py's ``if declared is None or seen is None: continue``
    and this test goes red — ``str(None)`` would be reported as a mismatch.
    """
    contract = _matched_contract()
    device = contract.devices[0]
    # RecordedSource's production shape: device_id only, every descriptor None.
    source = _StageScopedSource([DeviceObservation(device_id=device.id)])
    findings = [f for f in run_preflight(contract, source) if f.stage == 0]
    assert findings == []
    assert not any(f.severity == "violation" for f in findings)
    assert not any(f.observed == "None" for f in findings)


def test_matched_liveness_agreeing_fields_is_clean():
    contract = _matched_contract()
    device = contract.devices[0]
    assert device.driver is not None
    source = _StageScopedSource(
        [DeviceObservation(device_id=device.id)],
        live=[
            LivenessObservation(
                device_id=device.id,
                node=device.driver.node,
                topic=device.driver.topic,
                nominal_rate_hz=device.driver.nominal_rate_hz,
                payload_bytes=device.driver.payload_bytes,
            )
        ],
    )
    findings = [f for f in run_preflight(contract, source) if f.stage == 1]
    assert findings == []


def test_matched_liveness_mismatch_is_violation():
    contract = _matched_contract()
    device = contract.devices[0]
    assert device.driver is not None
    source = _StageScopedSource(
        [DeviceObservation(device_id=device.id)],
        live=[
            LivenessObservation(
                device_id=device.id,
                node="/wrong/node",
                topic=device.driver.topic,
            )
        ],
    )
    findings = [
        f for f in run_preflight(contract, source) if f.stage == 1 and f.severity == "violation"
    ]
    assert any(f.quantity == f"devices/{device.id}/driver/node" for f in findings)


def test_matched_liveness_observed_none_is_unseen_not_mismatch():
    """Mirror of (c) for stage 1 — production RecordedSource shape must not NO-GO."""
    contract = _matched_contract()
    device = contract.devices[0]
    source = _StageScopedSource(
        [DeviceObservation(device_id=device.id)],
        live=[LivenessObservation(device_id=device.id)],
    )
    findings = [f for f in run_preflight(contract, source) if f.stage == 1]
    assert findings == []
    assert not any(f.observed == "None" for f in findings)


def test_enumeration_unseen_declaration_is_not_scored_not_nogogo():
    """Device absent from source entirely → not_scored (distinct from found-but-None)."""
    contract = _matched_contract()
    source = _StageScopedSource([])
    findings = [f for f in run_preflight(contract, source) if f.stage == 0]
    assert findings
    assert all(f.severity == "not_scored" for f in findings)
    assert all(f.stage == 0 for f in findings)


def test_undeclared_observed_device_is_not_a_violation():
    """A device the contract does not declare is not a stage-0/1 violation."""
    contract = _matched_contract()
    source = _StageScopedSource(
        [DeviceObservation(device_id="dev.stranger")],
        live=[LivenessObservation(device_id="dev.stranger")],
    )
    findings = run_preflight(contract, source)
    assert not any(f.severity == "violation" and "dev.stranger" in f.quantity for f in findings)


def test_samples_unavailable_becomes_not_scored_not_a_crash():
    """A stage that consults samples() on a scoped double must not detonate the run."""
    contract = _matched_contract()

    def _consumes_samples(_contract: Contract, source: PreflightSource) -> list[Finding]:
        source.samples()
        return []

    entry = StageEntry(
        stage=4, name="synthetic_consumer", check=_consumes_samples, implemented=True
    )
    inner = _StageScopedSource([DeviceObservation(device_id="dev.imu")])
    produced = _dispatch_stage(entry, contract, _LazySource(inner))  # type: ignore[arg-type]
    assert len(produced) == 1
    assert produced[0].severity == "not_scored"
    assert "samples unavailable" in produced[0].observed
    assert inner.samples_calls == 1


def test_lazy_samples_not_touched_when_no_stage_asks():
    """Lazy mechanism: samples() untouched until a stage asks; memoized to one call.

    Property of ``_LazySource`` under controlled dispatch — not of whether
    today's ``STAGES`` registry happens to ignore samples(). Parallel stage
    boxes that start calling ``source.samples()`` must not break this.
    """
    contract = _matched_contract()

    def _ignores(_contract: Contract, _source: PreflightSource) -> list[Finding]:
        return []

    def _asks(_contract: Contract, source: PreflightSource) -> list[Finding]:
        source.samples()
        return []

    class _MemoSource:
        """Minimal double: successful samples() so memoization hits the cache path."""

        def __init__(self) -> None:
            self.samples_calls = 0
            self._payload = object()

        def enumerate(self) -> list[DeviceObservation]:
            return []

        def liveness(self) -> list[LivenessObservation]:
            return []

        def samples(self) -> object:
            self.samples_calls += 1
            return self._payload

    inner = _MemoSource()
    lazy = _LazySource(inner)  # type: ignore[arg-type]

    for entry in (
        StageEntry(0, "ignore_a", _ignores, True),
        StageEntry(1, "ignore_b", _ignores, True),
    ):
        assert _dispatch_stage(entry, contract, lazy) == []
    assert inner.samples_calls == 0

    for entry in (
        StageEntry(2, "ask_a", _asks, True),
        StageEntry(3, "ask_b", _asks, True),
        StageEntry(4, "ask_c", _asks, True),
    ):
        assert _dispatch_stage(entry, contract, lazy) == []
    assert inner.samples_calls == 1


def test_finding_stage_field_defaults_none_for_record_checks():
    finding = Finding(
        severity="warning",
        quantity="model/links/x/mass",
        observed="0",
        expected="> 0",
        action="resolve",
    )
    assert finding.stage is None


def _bag(tmp_path: Path, keep=lambda row: True, **manifest) -> Path:
    """The clean bag with rows filtered by ``keep`` and manifest keys overridden."""
    import json

    tmp_path.mkdir(parents=True, exist_ok=True)
    head = json.loads((CLEAN_BAG / "manifest.yaml").read_text())
    (tmp_path / "manifest.yaml").write_text(json.dumps(head | manifest))
    rows = (CLEAN_BAG / "samples.jsonl").read_text().splitlines()
    (tmp_path / "samples.jsonl").write_text("".join(r + "\n" for r in rows if keep(json.loads(r))))
    return tmp_path


def _verdict_of(bag: Path):
    from lockwire.report import verdict

    report = run_preflight(load_contract(CLEAN_CONTRACT), RecordedSource(load_recording(bag)))
    return report, verdict(report)


def test_capture_failed_with_nodes_up_is_not_scored():
    """a capture that never ran is NOT SCORED (exit 3), never GO,
    even when enumeration and liveness saw every node."""
    from lockwire.report import verdict

    contract = load_contract(CLEAN_CONTRACT)
    source = _StageScopedSource(
        [DeviceObservation(device_id=d.id) for d in contract.devices],
        live=[LivenessObservation(device_id=d.id) for d in contract.devices],
    )
    source.scores_absence = True  # type: ignore[attr-defined]
    decision = verdict(run_preflight(contract, source))
    assert (decision.code, decision.exit_code) == ("NOT_SCORED", 3)


def test_no_declared_device_recorded_is_not_scored(tmp_path):
    """An empty recording gives GO: env rows only,
    every device renamed, or an in-memory recording with no samples file."""
    from lockwire.report import verdict
    from lockwire_pro.recording import Recording

    env_only = _bag(tmp_path / "env", keep=lambda row: "env" in row)
    renamed = _bag(tmp_path / "other", keep=lambda row: row.setdefault("dev", "x") != "x")
    for name in ("samples.jsonl", "manifest.yaml"):
        (renamed / name).write_text((renamed / name).read_text().replace('"dev.', '"other.'))
    for bag in (env_only, renamed):
        assert _verdict_of(bag)[1].exit_code == 3, bag.name

    class _Empty(_StageScopedSource):
        def samples(self):
            return Recording("nominal-op", {}, {}, {}, sha256="0" * 64, path=tmp_path)

    report = run_preflight(load_contract(CLEAN_CONTRACT), _Empty([]))
    assert verdict(report).code == "NOT_SCORED"


def test_declared_device_absent_caps_at_not_scored_naming_it(tmp_path):
    """a recording missing a declared device prints NOT SCORED naming it, exit 3."""
    _, decision = _verdict_of(_bag(tmp_path, keep=lambda row: row.get("dev") != "dev.imu"))
    assert (decision.code, decision.exit_code) == ("NOT_SCORED", 3)
    assert "dev.imu" in decision.summary


def test_device_under_sample_floor_is_not_scored_with_reason(tmp_path):
    """a device record under 10 samples or 1 s is not scored, reason
    printed; all-equal timestamps fall under the same floor."""
    seen = {"n": 0}

    def five_baro(row):
        if row.get("dev") != "dev.barometer":
            return True
        seen["n"] += 1
        return seen["n"] <= 5

    report, decision = _verdict_of(_bag(tmp_path, keep=five_baro))
    assert decision.code == "NOT_SCORED"
    assert "dev.barometer" in decision.summary and "10 samples" in decision.summary
    assert not any("dev.barometer" in r.quantity for r in report.scored_records)


def test_manifest_duration_over_twice_the_span_is_refused(tmp_path):
    """a 30 s manifest over a 0.2 s bag, or one row per device at t=0 (rows4),
    is a refusal, not a verdict."""
    import pytest

    for name, keep in (("s0p2", lambda row: row["t"] < 0.2), ("rows4", lambda row: row["t"] == 0)):
        with pytest.raises(ValueError, match="duration_s"):
            _verdict_of(_bag(tmp_path / name, keep=keep))


def test_partial_recording_caps_at_not_scored(tmp_path):
    """a partial bag still scores, but the verdict is at most
    NOT SCORED with "partial recording" in the headline."""
    report, decision = _verdict_of(_bag(tmp_path, partial=True))
    assert report.scored > 0
    assert decision.code == "NOT_SCORED" and "partial recording" in decision.summary


def test_capped_preflight_stays_not_scored_inside_a_plain_list(tmp_path):
    """page.py concatenates record findings with a preflight's; the plain
    list must not launder a preflight that measured nothing into GO."""
    from lockwire.checks import run_record_checks
    from lockwire.report import verdict

    contract = load_contract(CLEAN_CONTRACT)
    report, _ = _verdict_of(_bag(tmp_path, keep=lambda row: "env" in row))
    assert verdict(list(run_record_checks(contract)) + list(report)).code == "NOT_SCORED"
    assert verdict(list(run_record_checks(contract)) + list(_clean_run())).code == "GO"


def _clean_run():
    contract, source = _clean_pair()
    return run_preflight(contract, source)


def test_json_is_written_on_a_refused_run(tmp_path):
    """after a refused run, --json says REFUSED, never the earlier GO."""
    import json

    from typer.testing import CliRunner

    from lockwire.cli import app

    out = tmp_path / "run.json"
    args = ["preflight", str(CLEAN_CONTRACT), "--no-ledger", "--json", str(out), "--from"]
    first = CliRunner().invoke(app, [*args, str(CLEAN_BAG)])
    assert first.exit_code == 0 and json.loads(out.read_text())["verdict"] == "GO"
    short = _bag(tmp_path / "s0p2", keep=lambda row: row["t"] < 0.2)
    second = CliRunner().invoke(app, [*args, str(short)])
    assert second.exit_code == 2, second.output
    assert json.loads(out.read_text())["verdict"] == "REFUSED"


def test_preflight_json_refuses_to_overwrite_the_contract(tmp_path):
    """--json never lands on a lockfile; a refusal is exit 2 and the contract is untouched."""
    from typer.testing import CliRunner

    from lockwire.cli import app

    contract = tmp_path / "lockwire.yaml"
    contract.write_text(CLEAN_CONTRACT.read_text())
    before = contract.read_text()
    result = CliRunner().invoke(
        app,
        [
            "preflight",
            str(contract),
            "--no-ledger",
            "--json",
            str(contract),
            "--from",
            str(CLEAN_BAG),
        ],
    )
    assert result.exit_code == 2, result.output
    assert contract.read_text() == before
