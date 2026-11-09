"""Stage 4a — record-level cross-checks folded into the gate (B89 / B102).

Wiring, not checking: this stage calls the existing ``check_budgets`` and
``check_condition_envelopes`` (checks.py) and stamps their findings. The real
assertions here are about the wiring's own decisions, ruled pre-dispatch
(Rules 1-3), the context gate, and the no-silent-downgrade rule:

- Context gate: evaluate a device's ``ContextEnvelope`` only against a
  recording whose context matches. Mismatch → ``warning`` naming the
  unbaselined operating context, the contexts that do have condition
  baselines, and the re-baseline remedy — never a violation, never
  ``not_scored`` (the no-silent-downgrade rule). Band scoring stays gated.
- Rule 1: reduce a recorded time series to per-axis MIN/MAX, call the
  record-level check twice (once per vector), dedupe on (quantity, observed).
  A mean reduction would hide a brief excursion — test proves this matters.
- Rule 2: an axis declared with conflicting units across matching-context
  envelopes is refused (``not_scored``), never silently resolved.
- Rule 3: an axis with no environment channel, no samples, or an
  unconvertible unit is refused the same way, never defaulted to a number.
"""

from __future__ import annotations

from pathlib import Path

from lockwire_pro.preflight.runner import run_preflight
from lockwire_pro.preflight.source import RecordedSource
from lockwire_pro.preflight.stages import cross_record
from lockwire_pro.preflight.stages.cross_record import check
from lockwire_pro.recording import Recording, load_recording
from lockwire.schema import (
    Band,
    Budget,
    ContextEnvelope,
    Contract,
    DeviceInstance,
    FittedBandAllocation,
    FittedBandEvidence,
    Provenance,
    Quantity,
    RobotInfo,
    load_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_CONTRACT = FIXTURES / "contract_clean.yaml"
MISMATCH_CONTRACT = FIXTURES / "contract_context_mismatch.yaml"
CLEAN_BAG = FIXTURES / "bags" / "clean"
CONDITION_EXIT_BAG = FIXTURES / "bags" / "condition_envelope_exit"

_PROVENANCE = Provenance(
    witness="live",
    source="synthetic-b89",
    locked_at="2026-07-27",
)


def _contract(
    *,
    conditions: dict[str, Band],
    context: str = "bench-idle",
    device_id: str = "dev.synth",
    depends: list[str] | None = None,
    owner: str = "b89",
) -> Contract:
    """Minimal contract with one device declaring the given condition axes."""
    return Contract(
        robot=RobotInfo(name="b89-synthetic"),
        devices=[
            DeviceInstance(
                id=device_id,
                model="mdl.synth",
                owner=owner,
                depends=depends if depends is not None else ["src/x.cpp"],
                envelopes=[
                    ContextEnvelope(
                        context=context,
                        conditions=conditions,
                        provenance=_PROVENANCE,
                    )
                ],
            )
        ],
    )


def _recording(
    *,
    tmp_path: Path,
    env: dict[str, list[tuple[float, float]]],
    env_units: dict[str, str],
    context: str = "bench-idle",
) -> Recording:
    return Recording(
        context=context,
        units={"environment": env_units},
        devices={},
        env=env,
        sha256="synthetic-b89",
        path=tmp_path,
    )


def test_implemented_flag_replaces_unbuilt_stub():
    assert cross_record.IMPLEMENTED is True


def test_fitted_condition_branch_reports_budget_operands(tmp_path):
    path = "devices/dev.synth/envelopes/bench-idle/conditions/temp_c"
    contract = Contract(
        robot=RobotInfo(name="fitted-condition"),
        devices=[
            DeviceInstance(
                id="dev.synth",
                model="mdl.synth",
                owner="test",
                envelopes=[
                    ContextEnvelope(
                        context="bench-idle",
                        conditions={"temp_c": Band(lo=0.0, hi=10.0, unit="C")},
                        fitted_bands={
                            "conditions/temp_c": FittedBandEvidence(
                                rank=1,
                                n_eff=100,
                                alpha=0.005,
                            )
                        },
                        provenance=Provenance(
                            witness="live",
                            source="known-good",
                            locked_at="2026-07-29",
                            bag_sha256="a" * 64,
                        ),
                    )
                ],
            )
        ],
        fitted_band_allocations=[
            FittedBandAllocation(
                context="bench-idle",
                channels=["environment/temp_c"],
                fitted_bands=[path],
                denominator=1,
            )
        ],
    )
    recording = _recording(
        tmp_path=tmp_path,
        env={"temp_c": [(float(i), 20.0 + (i % 2)) for i in range(100)]},
        env_units={"temp_c": "C"},
    )

    findings = check(contract, RecordedSource(recording))
    violations = [finding for finding in findings if finding.severity == "violation"]
    assert len(violations) == 1
    assert violations[0].quantity == "devices/dev.synth/conditions/temp_c"
    assert "K=" in violations[0].observed
    assert "tail probability=" in violations[0].observed


def test_context_mismatch_is_warning_never_violation_or_not_scored():
    """Recording nominal-op + condition baselines only for bench-idle → warn, don't violate.

    B113 moved this shape off contract_clean onto contract_context_mismatch.
    Scoring the bench-idle band against nominal-op data would be a false no-go;
    silence (not_scored) would be the monotonicity defect the context gate closes.
    """
    contract = load_contract(MISMATCH_CONTRACT)
    for bag in (CLEAN_BAG, CONDITION_EXIT_BAG):
        source = RecordedSource(load_recording(bag))
        findings = check(contract, source)

        mismatches = [
            f
            for f in findings
            if f.severity == "warning"
            and f.quantity == "devices/dev.imu/conditions"
            and "nominal-op" in f.observed
            and "bench-idle" in f.observed
        ]
        assert mismatches, f"no context-mismatch warning for {bag.name}"
        # Severity is the contract — prose-only asserts would miss a not_scored downgrade.
        assert all(f.severity == "warning" for f in mismatches)
        assert not any(
            f.severity == "not_scored" and f.quantity.endswith("/conditions") for f in findings
        ), f"context mismatch still emitted as not_scored for {bag.name}"
        assert not any(
            f.severity == "violation" and "conditions" in f.quantity for f in findings
        ), f"context mismatch produced a condition violation for {bag.name}"
        # Remedy names re-baseline; claim is about condition baselines, not all envelopes.
        for finding in mismatches:
            assert "re-baseline" in finding.action
            assert "condition baselin" in finding.observed


def test_mismatch_fixture_emits_exactly_one_context_mismatch_warning():
    """Presence floor: exactly one warning, for the examined device.

    Recording is nominal-op; only ``dev.imu`` declares condition axes (under
    bench-idle). A vacuous run that examined nothing cannot satisfy this.
    """
    contract = load_contract(MISMATCH_CONTRACT)
    recording = load_recording(CLEAN_BAG)
    assert recording.context == "nominal-op"

    devices_with_condition_baselines = [
        device
        for device in contract.devices
        if any(envelope.conditions for envelope in device.envelopes)
    ]
    assert len(devices_with_condition_baselines) > 0, (
        "presence floor: stage must examine at least one device with condition baselines"
    )

    findings = check(contract, RecordedSource(recording))
    warnings = [
        f for f in findings if f.severity == "warning" and f.quantity.endswith("/conditions")
    ]
    assert len(warnings) == 1
    assert warnings[0].quantity == "devices/dev.imu/conditions"
    assert "nominal-op" in warnings[0].observed
    assert "bench-idle" in warnings[0].observed
    assert "re-baseline" in warnings[0].action


def test_clean_fixture_has_no_context_mismatch_warning():
    """B113: clean robot is baselined for nominal-op — standing zero-warning law."""
    contract = load_contract(CLEAN_CONTRACT)
    recording = load_recording(CLEAN_BAG)
    assert recording.context == "nominal-op"
    assert any(
        envelope.context == recording.context and envelope.conditions
        for device in contract.devices
        for envelope in device.envelopes
    ), "presence floor: clean must declare condition baselines for the recording context"

    findings = check(contract, RecordedSource(recording))
    assert not any(f.severity == "warning" and f.quantity.endswith("/conditions") for f in findings)


def test_matching_context_excursion_fires_with_dependent_and_owner(tmp_path):
    """Gate does not disable the check: matching bench-idle + out-of-band → violation."""
    contract = _contract(
        conditions={"temp_c": Band(lo=18, hi=24, unit="C")},
        context="bench-idle",
        device_id="dev.imu",
        depends=["src/estimation/ekf.cpp"],
        owner="shivansh",
    )
    recording = _recording(
        tmp_path=tmp_path,
        env={"temp_c": [(0.0, 20.0), (1.0, 35.0)]},
        env_units={"temp_c": "C"},
        context="bench-idle",
    )
    findings = check(contract, RecordedSource(recording))

    violations = [f for f in findings if f.severity == "violation" and "conditions" in f.quantity]
    assert len(violations) == 1
    v = violations[0]
    assert v.quantity == "devices/dev.imu/envelopes/bench-idle/conditions"
    assert "temp_c: 35.00 C" in v.observed
    assert v.observed_quantity is not None
    assert v.observed_quantity.value == 35.0
    assert v.observed_quantity.unit == "C"
    assert v.expected_quantity is not None
    assert v.expected_quantity.value == 24.0
    assert v.expected_quantity.unit == "C"
    assert v.dependent == "src/estimation/ekf.cpp"
    assert v.owner == "shivansh"
    # Warning-severity mismatch must not swallow matched-context scoring.
    assert not any(f.severity == "warning" and "conditions" in f.quantity for f in findings)


def test_reduction_uses_min_max_not_mean(tmp_path):
    """Rule 1: a brief excursion whose MEAN stays inside the span must still violate.

    Ten samples at 20 (well inside [18, 24]) and one spike to 40 (well
    outside). The mean of these eleven samples is comfortably inside the
    span; only the MAX reveals the excursion. Retargeted to a matching
    bench-idle recording so the context gate does not short-circuit the proof.
    """
    contract = _contract(conditions={"temp_c": Band(lo=18, hi=24, unit="C")})
    samples = [(float(i), 20.0) for i in range(10)] + [(10.0, 40.0)]
    mean = sum(value for _, value in samples) / len(samples)
    assert 18 <= mean <= 24, "test setup: mean must be inside the span"

    recording = _recording(
        tmp_path=tmp_path,
        env={"temp_c": samples},
        env_units={"temp_c": "C"},
        context="bench-idle",
    )
    source = RecordedSource(recording)
    findings = check(contract, source)

    violations = [f for f in findings if f.severity == "violation"]
    assert len(violations) == 1
    assert "temp_c" in violations[0].observed


def test_axis_with_no_environment_channel_is_not_scored_no_violation(tmp_path):
    """Rule 3: a declared axis absent from the recording is refused, not defaulted."""
    contract = _contract(conditions={"humidity_pct": Band(lo=20, hi=60, unit="%")})
    recording = _recording(tmp_path=tmp_path, env={}, env_units={}, context="bench-idle")
    source = RecordedSource(recording)
    findings = check(contract, source)

    disclosures = [
        f for f in findings if f.severity == "not_scored" and "humidity_pct" in f.observed
    ]
    assert disclosures
    assert not any(f.severity == "violation" for f in findings)


def test_axis_declared_with_conflicting_units_is_not_scored_and_excluded(tmp_path):
    """Rule 2: same axis, different declared units under matching context → refuse.

    Both devices declare under bench-idle so the context gate admits the path;
    ``env`` value of 50 degC would violate either [18, 24] band if scored —
    asserting no violation proves the axis was excluded, not resolved to one unit.
    """
    contract = Contract(
        robot=RobotInfo(name="b89-synthetic"),
        devices=[
            DeviceInstance(
                id="dev.a",
                model="mdl.synth",
                owner="b89",
                envelopes=[
                    ContextEnvelope(
                        context="bench-idle",
                        conditions={"temp_c": Band(lo=18, hi=24, unit="C")},
                        provenance=_PROVENANCE,
                    )
                ],
            ),
            DeviceInstance(
                id="dev.b",
                model="mdl.synth",
                owner="b89",
                envelopes=[
                    ContextEnvelope(
                        context="bench-idle",
                        conditions={"temp_c": Band(lo=18, hi=24, unit="degC")},
                        provenance=_PROVENANCE,
                    )
                ],
            ),
        ],
    )
    recording = _recording(
        tmp_path=tmp_path,
        env={"temp_c": [(0.0, 50.0)]},
        env_units={"temp_c": "degC"},
        context="bench-idle",
    )
    source = RecordedSource(recording)
    findings = check(contract, source)

    conflict = [
        f
        for f in findings
        if f.severity == "not_scored" and "temp_c" in f.observed and "conflict" in f.observed
    ]
    assert len(conflict) == 1
    assert "C" in conflict[0].observed and "degC" in conflict[0].observed
    assert not any("temp_c" in f.observed for f in findings if f.severity == "violation")


def test_budget_findings_arrive_stamped_stage_4():
    """check_budgets findings are dispatched through this stage and stamped 4."""
    contract = load_contract(CLEAN_CONTRACT)
    contract.budgets.append(
        Budget(
            id="bud.test.negative",
            kind="mass_total",
            limit=Quantity(value=-1.0, unit="kg"),
            owner="b89-test",
        )
    )
    contract.revalidate()
    source = RecordedSource(load_recording(CLEAN_BAG))

    findings = run_preflight(contract, source)
    budget_findings = [f for f in findings if f.quantity.startswith("budgets/bud.test.negative")]

    assert budget_findings
    assert all(f.stage == 4 for f in budget_findings)
    assert any(f.severity == "violation" for f in budget_findings)


def test_context_gate_still_blocks_scoring_mismatched_band(tmp_path):
    """Mutation-bite target for removing the context gate.

    Clean-shaped: condition band only under bench-idle, recording is
    nominal-op with an out-of-band temperature. With the gate intact → warning,
    no violation. Without the gate → the bench-idle band would score and
    fire a violation against nominal-op data.
    """
    contract = _contract(
        conditions={"temp_c": Band(lo=18, hi=24, unit="C")},
        context="bench-idle",
        device_id="dev.imu",
    )
    # 24.48 °C — the monotonicity example temperature, outside [18, 24].
    recording = _recording(
        tmp_path=tmp_path,
        env={"temp_c": [(0.0, 24.48)]},
        env_units={"temp_c": "C"},
        context="nominal-op",
    )
    findings = check(contract, RecordedSource(recording))

    assert any(
        f.severity == "warning" and f.quantity == "devices/dev.imu/conditions" for f in findings
    )
    assert not any(f.severity == "violation" for f in findings)
