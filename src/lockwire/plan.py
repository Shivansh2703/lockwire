"""An agent's intended plan, checked against the contract before anything executes.

Mirrors the contract loader's guards (size cap, no YAML anchors/aliases) so a
plan file is trusted exactly as much as a lockfile. See ``validate_plan`` for
the scoring rules, and ``docs/plan_validation.md`` for the product view.
"""

from pathlib import Path
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from lockwire import units
from lockwire.advice import Alternative
from lockwire.copy import human_name
from lockwire.checks import (
    _worst_rail_power,
    power_rollup,
    unknown_draw_message,
    unusable_effort_finding,
)
from lockwire.report import Finding, RecordReport
from lockwire.schema import (
    MAX_CONTRACT_BYTES,
    Base,
    Band,
    Contract,
    DeviceInstance,
    PowerDraw,
    PowerQuantity,
    Rail,
    _load_yaml_guarded,
    _reject_yaml_anchors_aliases,
    _yaml,
)
from lockwire.units import UnitError


class JointTarget(Base):
    kind: Literal["joint_target"] = "joint_target"
    joint: str
    position: float | None = None  # rad or m
    velocity: float | None = None  # rad/s or m/s
    effort: float | None = None  # N*m or N


class RailLoad(Base):
    """An additional draw placed on a rail."""

    kind: Literal["rail_load"] = "rail_load"
    rail: str  # -> Rail.id
    draw: PowerQuantity


class SetPowerMode(Base):
    """Switch a device to a declared power mode."""

    kind: Literal["set_power_mode"] = "set_power_mode"
    device: str  # -> DeviceInstance.id
    mode: str  # -> PowerMode.name (or "nominal")


class Publish(Base):
    """A ROS message an agent intends to send."""

    kind: Literal["publish"] = "publish"
    topic: str
    msg_type: str
    msg: dict[str, Any] = {}


Step = Annotated[
    JointTarget | RailLoad | SetPowerMode | Publish,
    Field(discriminator="kind"),
]


class Plan(Base):
    steps: list[Step] = []


class AssertionOutcome(Base):
    """Machine-readable disposition for one assertion submitted in a plan."""

    path: str
    status: Literal["PASS", "FAIL", "UNSCORED", "ERROR"]


class PlanReport(RecordReport):
    """Findings plus complete, list-compatible assertion coverage."""

    outcomes: list[AssertionOutcome]
    errors: int

    def __init__(
        self,
        findings: list[Finding] | None = None,
        outcomes: list[AssertionOutcome] | None = None,
    ) -> None:
        self.outcomes = outcomes or []
        self.errors = sum(outcome.status == "ERROR" for outcome in self.outcomes)
        scored = sum(outcome.status in {"PASS", "FAIL"} for outcome in self.outcomes)
        super().__init__(findings, scored=scored)


def load_plan(path: Path) -> Plan:
    """Load a plan.yaml with the same size and anchor guards as ``load_contract``."""
    size = path.stat().st_size
    if size > MAX_CONTRACT_BYTES:
        raise ValueError(f"plan file exceeds size limit ({size} bytes > {MAX_CONTRACT_BYTES})")
    with open(path, encoding="utf-8") as f:
        data = _load_yaml_guarded(_yaml(), f)
    _reject_yaml_anchors_aliases(data)
    return Plan.model_validate(data)


# ---------------------------------------------------------------- power helpers
#
# Rail base watts is the chain-inclusive rollup checks._worst_rail_power uses
# for `lockwire check` (via ampacity): it climbs child rails through
# `upstream`/`efficiency` up to `rail_id`, maximized over nominal and every
# declared PowerMode. Reusing it here (instead of a plan-local direct-rail
# sum) keeps `lockwire plan` and `lockwire check` agreeing on one number per
# rail — see tests/test_plan.py's agreement test.


def _rail_worst_w_with_device_pinned(
    contract: Contract, rail_id: str, device_id: str, w: float
) -> tuple[float, set[str]]:
    """``_worst_rail_power(rail_id)`` with ``device_id`` pinned to ``w`` watts.

    Used for a SetPowerMode step: the agent is committing this one device to
    a named mode, so its contribution should not also vary across the
    rollup's own worst-mode search. Swap the device's declared draw for a
    single fixed value (no other modes) in a shallow contract copy, then call
    the same helper again — the efficiency chain from the device's rail up to
    ``rail_id`` is whatever _worst_rail_power already does, not reimplemented.
    Returns ``(watts, unknown_consumers)`` — the caller must not score a peak
    when unknown_consumers is non-empty.
    """
    device = contract.device(device_id)
    assert device is not None and device.power is not None
    pinned_power = device.power.model_copy(
        update={"draw": PowerQuantity(value=w, unit="W"), "modes": []}
    )
    pinned_device = device.model_copy(update={"power": pinned_power})
    devices = [pinned_device if d.id == device_id else d for d in contract.devices]
    swapped = contract.model_copy(update={"devices": devices})
    watts, _, _, unknown = _worst_rail_power(swapped, rail_id)
    return watts, unknown


def _unknown_finding_for(contract: Contract, path: str, unknown: set[str]) -> Finding:
    """One not_scored Finding naming an unknown-draw consumer found while
    walking a rail chain (never silently score a peak around a gap)."""
    device_id = sorted(unknown)[0]
    device = contract.device(device_id)
    device_rail = device.power.rail if device is not None and device.power is not None else "?"
    return Finding(
        severity="not_scored",
        quantity=path,
        observed=unknown_draw_message(device_id, device_rail),
        expected="a declared draw for this device",
        action=f"declare {device_id}'s power draw, or accept this step as not scored",
    )


def _rail_capacity_chain(
    contract: Contract, rail_id: str, rail_extra: dict[str, float]
) -> list[tuple[Rail, float, float, set[str]]]:
    """The named rail plus every ancestor up to the battery, each with its own
    rolled-up watts (``_worst_rail_power``, which already includes descendant
    devices) plus the plan-time extra load propagated onto it. Callers walk
    this list and stop at the first unknown-consumer gap or capacity breach."""
    out: list[tuple[Rail, float, float, set[str]]] = []
    seen: set[str] = set()
    current = contract.rail(rail_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        base_w, _, _, unknown = _worst_rail_power(contract, current.id)
        total_w = base_w + rail_extra.get(current.id, 0.0)
        out.append((current, total_w, current.capacity.base(), unknown))
        if current.upstream in (None, "battery"):
            break
        current = contract.rail(current.upstream)
    return out


def _with_enforcement_provenance(expected: str, *limits: tuple[str, str | None]) -> str:
    """Annotate an existing checked limit without changing plan scoring."""
    declarations = [f"{value} ({name})" for name, value in limits if value is not None]
    if not declarations:
        return expected
    return (
        f"{expected} (declared enforcement provenance: {', '.join(declarations)}; "
        "metadata only; does not prove physical protection)"
    )


def _resolve_field(msg: dict[str, Any], field: str) -> tuple[float | None, str | None]:
    """Resolve a dotted path, distinguishing an absent value from a non-number."""
    node: Any = msg
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, "missing"
        node = node[part]
    if isinstance(node, bool) or not isinstance(node, int | float):
        return None, "nonnumeric"
    return float(node), None


def _score_max_rate(
    out: list[Finding],
    record: "Callable[[str, Literal['PASS', 'FAIL', 'UNSCORED', 'ERROR']], None]",
    *,
    path: str,
    band: Band,
    previous: float | None,
    current: float,
    rate_hz: float | None,
) -> None:
    """Score one published value's rate of change against ``Band.max_rate``.

    A step whose endpoints are both inside the band but
    whose rate of change between them exceeds the declared bound is a finding.

    Compare now, predict eventually, never actuate: this
    scores and reports only; nothing here reaches a device.

    Where the rate cannot be computed it is said out loud. A plan carries no
    timestamps, so the elapsed time between two publishes on a topic comes from
    the contract's own ``driver.nominal_rate_hz``. Missing either operand — the
    previous value or the declared rate — is ``not_scored`` naming which one is
    missing, never a silent pass: a rate limit that quietly stops applying is a
    rate limit that has stopped biting.
    """
    if previous is None:
        out.append(
            Finding(
                severity="not_scored",
                quantity=path,
                observed=f"{current:g} {band.unit} is the first value for this signal in the plan",
                expected=f"two published values, to compare against max_rate {band.max_rate:g} {band.unit}/s",
                action="add the preceding publish step, or accept this rate bound as not scored",
            )
        )
        record(path, "UNSCORED")
        return
    if rate_hz is None:
        out.append(
            Finding(
                severity="not_scored",
                quantity=path,
                observed="the driver declares no nominal_rate_hz, so the time between steps is unknown",
                expected=f"a declared publish rate, to compare against max_rate {band.max_rate:g} {band.unit}/s",
                action="declare driver.nominal_rate_hz for this topic to score the rate of change",
            )
        )
        record(path, "UNSCORED")
        return
    rate = abs(current - previous) * rate_hz
    assert band.max_rate is not None
    if rate > band.max_rate:
        out.append(
            Finding(
                severity="violation",
                quantity=path,
                observed=(
                    f"{rate:g} {band.unit}/s "
                    f"({previous:g} -> {current:g} {band.unit} at {rate_hz:g} Hz)"
                ),
                expected=f"<= max_rate {band.max_rate:g} {band.unit}/s{band.why_suffix}",
                action=(
                    "both endpoints are inside the band but the move between them is "
                    "faster than the declared rate — slow the step or split it"
                ),
            )
        )
        record(path, "FAIL")
    else:
        record(path, "PASS")


def validate_plan(contract: Contract, plan: "Plan") -> PlanReport:
    """Check an agent's intended plan against the contract before anything executes.

    Never invents a limit: anything the contract does not declare comes back
    ``not_scored``, with ``expected`` naming what is missing.
    """
    if not plan.steps:
        return PlanReport(
            [
                Finding(
                    severity="not_scored",
                    quantity="plan/steps",
                    observed="no steps",
                    expected="at least one step",
                    action="add steps to the plan before validating",
                )
            ],
            [AssertionOutcome(path="plan/steps", status="UNSCORED")],
        )

    # Whole-plan RailLoad totals, keyed by rail id (D-accumulate: two steps on
    # the same rail add up, not just the last one).
    railload_totals: dict[str, float] = {}
    for step in plan.steps:
        if isinstance(step, RailLoad) and contract.rail(step.rail) is not None:
            railload_totals[step.rail] = railload_totals.get(step.rail, 0.0) + step.draw.base()

    # Propagate every named rail's whole-plan RailLoad total up through the
    # rail tree once (a load on a child rail must also be checked
    # against every ancestor's capacity, and two individually-legal loads on
    # sibling children must combine at a shared parent). Each total is
    # expressed as one synthetic device on its rail and handed to
    # `power_rollup`, whose upstream/efficiency walk is the only copy of that
    # walk — same shallow-model_copy pattern as
    # _rail_worst_w_with_device_pinned. Real devices are swapped out because
    # their watts are already in `_worst_rail_power`'s base figure.
    synthetic = [
        DeviceInstance(
            id=f"plan.railload.{rail_id}",
            model="plan.railload",
            owner="lockwire plan",
            power=PowerDraw(rail=rail_id, draw=PowerQuantity(value=extra_w, unit="W")),
        )
        for rail_id, extra_w in railload_totals.items()
    ]
    rail_extra, *_ = power_rollup(contract.model_copy(update={"devices": synthetic}), "nominal")

    out: list[Finding] = []
    outcomes: list[AssertionOutcome] = []

    def record(path: str, status: Literal["PASS", "FAIL", "UNSCORED", "ERROR"]) -> None:
        outcomes.append(AssertionOutcome(path=path, status=status))

    joints = {joint.name: joint for joint in contract.model.joints} if contract.model else {}

    # The value a signal last carried in this plan, in its
    # hard limit's unit, keyed by (topic, signal) — the left operand a
    # `Band.max_rate` comparison needs. A step can sit inside the band at both
    # ends and still be destructive in between; nothing else in the plan
    # remembers the previous value.
    #
    # Compare now, predict eventually, never actuate. The rate
    # bound is *scored* here and never enforced on a device — a crossing is a
    # Finding, exactly like every other lockwire comparison.
    previous_published: dict[tuple[str, str], float] = {}

    for i, step in enumerate(plan.steps):
        base = f"plan/steps/{i}"

        if isinstance(step, JointTarget):
            joint = joints.get(step.joint)
            if joint is None:
                path = f"{base}/joint_target/joint"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"joint {step.joint!r} not found",
                        expected="a joint declared in the contract model",
                        action="check the joint name against the contract",
                        alternatives=tuple(
                            Alternative(
                                text=f"use declared model joint {candidate!r}",
                                value=1.0,
                                unit="count",
                                kind="model",
                            )
                            for candidate in sorted(joints)
                        ),
                    )
                )
                record(path, "UNSCORED")
                continue

            limits = joint.limits
            if step.position is not None:
                path = f"{base}/joint_target/position"
                if limits is None or limits.lower is None or limits.upper is None:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=f"{step.position}",
                            expected="the contract declares no position limits for this joint",
                            action="declare limits.lower/upper for this joint to score position",
                        )
                    )
                    record(path, "UNSCORED")
                elif not (limits.lower <= step.position <= limits.upper):
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=path,
                            observed=f"{step.position}",
                            expected=_with_enforcement_provenance(
                                f"inside [{limits.lower}, {limits.upper}]",
                                ("lower", limits.lower_enforced_by),
                                ("upper", limits.upper_enforced_by),
                            ),
                            action="pick a position inside the joint's declared limits",
                        )
                    )
                    record(path, "FAIL")
                else:
                    record(path, "PASS")

            if step.velocity is not None:
                path = f"{base}/joint_target/velocity"
                if limits is None or limits.velocity is None:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=f"{step.velocity}",
                            expected="the contract declares no velocity limit for this joint",
                            action="declare limits.velocity for this joint to score velocity",
                        )
                    )
                    record(path, "UNSCORED")
                elif abs(step.velocity) > limits.velocity:
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=path,
                            observed=f"{step.velocity}",
                            expected=_with_enforcement_provenance(
                                f"<= {limits.velocity}",
                                ("velocity", limits.velocity_enforced_by),
                            ),
                            action="reduce the commanded velocity",
                        )
                    )
                    record(path, "FAIL")
                else:
                    record(path, "PASS")

            if step.effort is not None:
                path = f"{base}/joint_target/effort"
                if limits is None or limits.effort is None:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=f"{step.effort}",
                            expected="the contract declares no effort limit for this joint",
                            action="declare limits.effort for this joint to score effort",
                        )
                    )
                    record(path, "UNSCORED")
                elif limits.effort_limit is None:
                    out.append(unusable_effort_finding(path, limits.effort))
                    # no outcome row; the warning alone says torque is not checked
                elif abs(step.effort) > limits.effort_limit:
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=path,
                            observed=f"{step.effort}",
                            expected=_with_enforcement_provenance(
                                f"<= {limits.effort}",
                                ("effort", limits.effort_enforced_by),
                            ),
                            action="reduce the commanded effort",
                        )
                    )
                    record(path, "FAIL")
                else:
                    record(path, "PASS")

                driver_device = next(
                    (d for d in contract.devices if d.drives_joint == joint.name), None
                )
                actuator_path = f"{base}/joint_target/effort_actuator"
                if driver_device is None:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=actuator_path,
                            observed="no device drives this joint",
                            expected="a device with drives_joint set to this joint",
                            action="declare which device drives this joint to score actuator effort",
                        )
                    )
                    record(actuator_path, "UNSCORED")
                else:
                    model = contract.device_model(driver_device.model)
                    actuator = model.actuator if model is not None else None
                    if (
                        actuator is None
                        or actuator.torque_continuous is None
                        or actuator.gear_ratio is None
                    ):
                        out.append(
                            Finding(
                                severity="not_scored",
                                quantity=actuator_path,
                                observed="actuator missing torque_continuous or gear_ratio",
                                expected="both declared on the driving device's model",
                                action="declare the actuator's continuous torque and gear ratio",
                            )
                        )
                        record(actuator_path, "UNSCORED")
                    else:
                        continuous = actuator.torque_continuous * actuator.gear_ratio
                        peak = (
                            actuator.torque_peak * actuator.gear_ratio
                            if actuator.torque_peak is not None
                            else None
                        )
                        if abs(step.effort) > continuous:
                            if peak is not None and abs(step.effort) <= peak:
                                out.append(
                                    Finding(
                                        severity="warning",
                                        quantity=actuator_path,
                                        observed=f"{step.effort} N*m",
                                        expected=f"<= {continuous:.2f} N*m continuous geared torque",
                                        action="sustained effort above continuous rating shortens actuator life",
                                    )
                                )
                                record(actuator_path, "PASS")
                            else:
                                out.append(
                                    Finding(
                                        severity="violation",
                                        quantity=actuator_path,
                                        observed=f"{step.effort} N*m",
                                        expected=f"<= {continuous:.2f} N*m continuous geared torque",
                                        action="reduce the commanded effort or select a stronger actuator",
                                    )
                                )
                                record(actuator_path, "FAIL")
                        else:
                            record(actuator_path, "PASS")

            if step.position is None and step.velocity is None and step.effort is None:
                path = f"{base}/joint_target"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"joint {step.joint!r} selected with no target values",
                        expected="at least one of position, velocity, or effort",
                        action="add a target value before validating this step",
                    )
                )
                record(path, "UNSCORED")

        elif isinstance(step, RailLoad):
            rail = contract.rail(step.rail)
            if rail is None:
                path = f"{base}/rail_load/rail"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"rail {step.rail!r} not found",
                        expected="a rail declared in the contract topology",
                        action=f"This robot has no rail named {human_name(step.rail)}",
                    )
                )
                record(path, "UNSCORED")
                continue
            path = f"{base}/rail_load/draw"
            for checked_rail, total_w, capacity_w, unknown in _rail_capacity_chain(
                contract, step.rail, rail_extra
            ):
                if unknown:
                    out.append(_unknown_finding_for(contract, path, unknown))
                    record(path, "UNSCORED")
                    break
                if total_w > capacity_w:
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=path,
                            observed=f"{total_w:.2f} W rolled-up draw on {checked_rail.id}",
                            expected=_with_enforcement_provenance(
                                f"<= {capacity_w:.2f} W capacity",
                                ("capacity", checked_rail.capacity_enforced_by),
                            ),
                            action=f"shed load on rail {checked_rail.id!r} or raise its capacity",
                        )
                    )
                    record(path, "FAIL")
                    break
            else:
                record(path, "PASS")

        elif isinstance(step, SetPowerMode):
            device = contract.device(step.device)
            if device is None or device.power is None:
                path = f"{base}/set_power_mode/device"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"device {step.device!r} not found or declares no power",
                        expected="a device with a power draw declared",
                        action=(
                            f"This robot has no device named {human_name(step.device)}"
                            if device is None
                            else f"{human_name(step.device)} has no power modes declared"
                        ),
                    )
                )
                record(path, "UNSCORED")
                continue
            mode = next((m for m in device.power.modes if m.name == step.mode), None)
            mode_w = mode.draw.base() if mode is not None else None
            if mode_w is None and step.mode != "nominal":
                path = f"{base}/set_power_mode/mode"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"mode {step.mode!r} not declared on {step.device!r}",
                        expected="a power mode declared on this device",
                        action="check the mode name against the device's declared modes",
                    )
                )
                record(path, "UNSCORED")
                continue
            if mode_w is None:
                if device.power.draw is None:
                    path = f"{base}/set_power_mode/draw"
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=unknown_draw_message(device.id, device.power.rail),
                            expected="a declared draw for this device",
                            action=(
                                f"declare {device.id}'s power draw, or accept this "
                                "step as not scored"
                            ),
                        )
                    )
                    record(path, "UNSCORED")
                    continue
                mode_w = device.power.draw.base()
            if device.power.choices is not None and step.mode not in device.power.choices:
                path = f"{base}/set_power_mode/mode"
                out.append(
                    Finding(
                        severity="violation",
                        quantity=path,
                        observed=(
                            f"mode {step.mode!r} is declared on {step.device!r} "
                            "but not allowed by this contract"
                        ),
                        expected="one of: " + ", ".join(device.power.choices),
                        action=f"pick an allowed power mode for {step.device!r}",
                    )
                )
                record(path, "FAIL")
                continue
            rail = contract.rail(device.power.rail)
            if rail is None:
                path = f"{base}/set_power_mode/rail"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"rail {device.power.rail!r} not found",
                        expected="a rail declared in the contract topology",
                        action="fix the device's rail reference",
                    )
                )
                record(path, "UNSCORED")
                continue
            pinned_w, unknown = _rail_worst_w_with_device_pinned(
                contract, device.power.rail, device.id, mode_w
            )
            total_w = pinned_w + railload_totals.get(device.power.rail, 0.0)
            capacity_w = rail.capacity.base()
            path = f"{base}/set_power_mode/draw"
            if unknown:
                out.append(_unknown_finding_for(contract, path, unknown))
                record(path, "UNSCORED")
            elif total_w > capacity_w:
                out.append(
                    Finding(
                        severity="violation",
                        quantity=path,
                        observed=f"{total_w:.2f} W rolled-up draw on {device.power.rail} at mode {step.mode!r}",
                        expected=_with_enforcement_provenance(
                            f"<= {capacity_w:.2f} W capacity",
                            ("capacity", rail.capacity_enforced_by),
                        ),
                        action=f"pick a lower power mode for {step.device!r} or shed other load",
                    )
                )
                record(path, "FAIL")
            else:
                record(path, "PASS")

        elif isinstance(step, Publish):
            driver_device = next(
                (
                    d
                    for d in contract.devices
                    if d.driver is not None and d.driver.topic == step.topic
                ),
                None,
            )
            if driver_device is None:
                path = f"{base}/publish/topic"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"no driver declares topic {step.topic!r}",
                        expected="a device driver with a matching topic",
                        action="check the topic name against the contract's drivers",
                    )
                )
                record(path, "UNSCORED")
                continue
            driver = driver_device.driver
            assert driver is not None
            if driver.msg_type is not None and driver.msg_type != step.msg_type:
                path = f"{base}/publish/msg_type"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"message type {step.msg_type!r}",
                        expected=f"declared message type {driver.msg_type!r}",
                        action="use the driver's declared message type before validating fields",
                    )
                )
                record(path, "ERROR")
                continue
            if not driver.signals:
                path = f"{base}/publish/signals"
                out.append(
                    Finding(
                        severity="not_scored",
                        quantity=path,
                        observed=f"driver for topic {step.topic!r} has an empty signal map",
                        expected="at least one signal mapped to a message field",
                        action="declare driver.signals mappings before scoring this publish step",
                    )
                )
                record(path, "UNSCORED")
                continue
            model = contract.device_model(driver_device.model)
            for signal_name, source in driver.signals.items():
                path = f"{base}/publish/{signal_name}"
                value, resolution = _resolve_field(step.msg, source.field)
                if value is None:
                    observed = (
                        f"mapped field {source.field!r} is missing from the message"
                        if resolution == "missing"
                        else f"mapped field {source.field!r} is not numeric"
                    )
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=observed,
                            expected="a numeric value at the mapped field path",
                            action="populate the mapped field with a numeric value before validating",
                        )
                    )
                    record(path, "UNSCORED")
                    continue
                band: Band | None = (
                    model.hard_limits.get(signal_name) if model is not None else None
                )
                if band is None:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=f"{value} {source.unit}",
                            expected=f"hard_limits[{signal_name!r}] declared on the device model",
                            action="declare a hard limit for this signal to score it",
                        )
                    )
                    record(path, "UNSCORED")
                    continue
                try:
                    in_band = band.contains(Band(lo=value, hi=value, unit=source.unit))
                except UnitError:
                    out.append(
                        Finding(
                            severity="not_scored",
                            quantity=path,
                            observed=(
                                f"signal unit {source.unit!r} vs hard limit unit {band.unit!r}"
                            ),
                            expected=(
                                "the signal and its hard limit declare the same physical dimension"
                            ),
                            action="fix the contract: the signal's unit and hard_limits[...] "
                            "unit must share a dimension",
                        )
                    )
                    record(path, "UNSCORED")
                    continue
                if not in_band:
                    out.append(
                        Finding(
                            severity="violation",
                            quantity=path,
                            observed=f"{value} {source.unit}",
                            expected=f"inside the contract's declared hard limit {band}",
                            action=(
                                "correct the value or the declared range before publishing; "
                                "physical truth is indeterminate from this comparison"
                            ),
                        )
                    )
                    record(path, "FAIL")
                else:
                    record(path, "PASS")

                converted = units.convert(value, source.unit, band.unit)
                key = (step.topic, signal_name)
                if band.max_rate is not None:
                    _score_max_rate(
                        out,
                        record,
                        path=f"{path}/rate",
                        band=band,
                        previous=previous_published.get(key),
                        current=converted,
                        rate_hz=driver.nominal_rate_hz,
                    )
                previous_published[key] = converted

    if contract.model is not None:
        provenance = contract.model.provenance
        for finding in out:
            if (
                "/joint_target/" in finding.quantity
                and finding.quantity.rsplit("/", 1)[-1] in {"joint", "position", "velocity", "effort"}
            ):
                finding.source = provenance.source
                finding.witness = provenance.witness
                finding.locked_at = provenance.locked_at

    return PlanReport(out, outcomes)
