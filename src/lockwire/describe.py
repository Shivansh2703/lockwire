"""Per-device reference file, generated from the lockfile.

`lockwire describe` renders one plain-Markdown page per device: what it
measures, what it adjusts, what limits are enforced, and where each number
comes from. Every line is read from a schema field that already exists in the
contract — nothing here is inferred, predicted, or guessed. A field the
contract does not declare renders as "nothing declared", never a blank or a
made-up value.
"""

from __future__ import annotations

import re

from lockwire.checks import compute_system_cg, unknown_draw_message
from lockwire.copy import human_name, robot_title
from lockwire.forces import force_document, render_markdown
from lockwire.report import format_display, format_magnitude
from lockwire.schema import (
    Band,
    Contract,
    DeviceInstance,
    DeviceModel,
    Joint,
    JointLimits,
    Provenance,
    Rail,
    Tolerance,
)

_JOINT_UNITS = {
    "prismatic": ("m", "N", "m/s"),
}
_DEFAULT_JOINT_UNITS = ("rad", "N*m", "rad/s")


def _joint_units(joint_type: str) -> tuple[str, str, str]:
    """(position unit, effort unit, velocity unit) for a joint's kinematic type."""
    return _JOINT_UNITS.get(joint_type, _DEFAULT_JOINT_UNITS)


def _provenance_suffix(provenance: Provenance | None) -> str:
    if provenance is None:
        return ""
    parts = [f"source: {provenance.source}", f"locked_at: {provenance.locked_at}"]
    if provenance.witness:
        parts.insert(0, f"witness: {provenance.witness}")
    if provenance.bag_sha256 is not None:
        parts.append(f"bag_sha256: {provenance.bag_sha256}")
    return " (" + ", ".join(parts) + ")"


def _provenance_block(label: str, provenance: Provenance | None) -> list[str]:
    if provenance is None:
        return []
    lines = [
        f"- {label}:",
        f"  - witness: {provenance.witness}",
        f"  - source: {provenance.source}",
    ]
    lines.append(f"  - locked_at: {provenance.locked_at}")
    if provenance.bag_sha256 is not None:
        lines.append(f"  - bag_sha256: {provenance.bag_sha256}")
    if provenance.compared_from is not None:
        lines.append(f"  - compared_from: {provenance.compared_from}")
    if provenance.note:
        lines.append(f"  - note: {provenance.note}")
    return lines


def _role(model: DeviceModel | None, instance: DeviceInstance) -> str:
    adjusts = (
        model is not None and model.actuator is not None
    ) or instance.drives_joint is not None
    measures = instance.driver is not None and bool(instance.driver.signals)
    if adjusts and measures:
        return "measures and adjusts"
    if adjusts:
        return "adjusts"
    if measures:
        return "measures"
    return "component"


def _find_joint(contract: Contract, name: str) -> Joint | None:
    if contract.model is None:
        return None
    return next((j for j in contract.model.joints if j.name == name), None)


def _joint_limit_lines(joint: Joint) -> list[str]:
    limits: JointLimits | None = joint.limits
    if limits is None:
        return [f"- joint {joint.name} ({joint.type}): no limits declared"]
    pos_unit, effort_unit, vel_unit = _joint_units(joint.type)
    lines = [f"- joint {joint.name} ({joint.type}):"]
    if limits.lower is not None or limits.upper is not None:
        lo = "?" if limits.lower is None else f"{limits.lower:g} {pos_unit}"
        hi = "?" if limits.upper is None else f"{limits.upper:g} {pos_unit}"
        lines.append(f"  - range: [{lo}, {hi}]")
    for name in ("lower", "upper"):
        value = getattr(limits, f"{name}_enforced_by")
        if value is not None:
            lines.append(f"  - {name} {_enforcement_detail(value)}")
    if limits.effort is not None:
        if limits.effort_limit is not None:
            shown = f"{limits.effort:g} {effort_unit}"
        else:
            shown = "unset (0)" if limits.effort == 0 else f"invalid (negative) {limits.effort:g}"
        lines.append(f"  - effort limit: {shown}")
    if limits.effort_enforced_by is not None:
        lines.append(f"  - effort {_enforcement_detail(limits.effort_enforced_by)}")
    if limits.velocity is not None:
        lines.append(f"  - velocity limit: {limits.velocity:g} {vel_unit}")
    if limits.velocity_enforced_by is not None:
        lines.append(f"  - velocity {_enforcement_detail(limits.velocity_enforced_by)}")
    return lines


def _enforcement_detail(value: str) -> str:
    return (
        f"declared enforcement provenance: {value} "
        "(metadata only; does not prove physical protection)"
    )


def _measures_section(instance: DeviceInstance) -> list[str]:
    driver = instance.driver
    if driver is None or not driver.signals:
        return ["Measures: nothing declared."]
    lines = ["| signal | field | unit |", "| --- | --- | --- |"]
    for name, source in sorted(driver.signals.items()):
        lines.append(f"| {name} | {source.field} | {source.unit} |")
    if driver.topic is not None:
        lines.append(f"- topic: {driver.topic}")
    if driver.msg_type is not None:
        lines.append(f"- msg_type: {driver.msg_type}")
    if driver.nominal_rate_hz is not None:
        lines.append(f"- nominal_rate_hz: {driver.nominal_rate_hz:g}")
    if driver.payload_bytes is not None:
        lines.append(f"- payload_bytes: {driver.payload_bytes}")
    return lines


def _adjusts_section(
    contract: Contract, model: DeviceModel | None, instance: DeviceInstance
) -> list[str]:
    lines: list[str] = []
    actuator = model.actuator if model is not None else None
    if actuator is not None:
        if actuator.gear_ratio is not None:
            lines.append(f"- gear_ratio: {actuator.gear_ratio:g}")
        if actuator.torque_constant is not None:
            lines.append(f"- torque_constant: {actuator.torque_constant:g} N*m/A")
        if actuator.torque_continuous is not None:
            lines.append(f"- torque_continuous: {actuator.torque_continuous:g} N*m")
        if actuator.torque_peak is not None:
            lines.append(f"- torque_peak: {actuator.torque_peak:g} N*m")
        if actuator.encoder_cpr is not None:
            lines.append(f"- encoder_cpr: {actuator.encoder_cpr}")
    if instance.drives_joint is not None:
        joint = _find_joint(contract, instance.drives_joint)
        if joint is None:
            lines.append(f"- drives_joint: {instance.drives_joint} (not found in model record)")
        else:
            lines.extend(_joint_limit_lines(joint))
    if not lines:
        return ["Adjusts: nothing declared."]
    return lines


def _limits_section(
    contract: Contract, model: DeviceModel | None, instance: DeviceInstance
) -> list[str]:
    lines: list[str] = []
    model_prov_suffix = _provenance_suffix(model.provenance) if model is not None else ""

    def band_details(band: Band) -> str:
        return f", declared enforcement: {band.enforced_by}" if band.enforced_by else ""

    if model is not None and model.hard_limits:
        for channel, band in sorted(model.hard_limits.items()):
            # ``str(Band)`` already carries the declared `why`.
            rate = f", max rate {band.max_rate:g} {band.unit}/s" if band.max_rate else ""
            lines.append(
                f"- declared datasheet/model rating {channel}: "
                f"{band}{rate}{band_details(band)}{model_prov_suffix}"
            )

    if model is not None and model.input_voltage_range is not None:
        rng = model.input_voltage_range
        lines.append(
            "- input_voltage_range: "
            f"[{format_magnitude(rng.min.value, rng.min.unit)}, "
            f"{format_magnitude(rng.max.value, rng.max.unit)}]{model_prov_suffix}"
        )
        for name in ("min", "max"):
            value = getattr(rng, f"{name}_enforced_by")
            if value is not None:
                lines.append(f"  - input_voltage_range {name} {_enforcement_detail(value)}")

    if model is not None and model.thermal_limit is not None:
        tl = model.thermal_limit
        lines.append(
            f"- thermal_limit ({tl.kind}): {format_magnitude(tl.temperature.value, tl.temperature.unit)}"
            f"{_provenance_suffix(tl.provenance)}"
        )
        if tl.temperature_enforced_by is not None:
            lines.append(
                f"  - thermal_limit temperature {_enforcement_detail(tl.temperature_enforced_by)}"
            )

    if instance.power is not None:
        power = instance.power
        if power.draw is not None:
            lines.append(
                f"- power draw: {format_magnitude(power.draw.value, power.draw.unit)} on {power.rail}"
            )
        else:
            lines.append(f"- power draw: {unknown_draw_message(instance.id, power.rail)}")
        for mode in power.modes:
            lines.append(
                f"  - mode {mode.name}: {format_magnitude(mode.draw.value, mode.draw.unit)}"
            )
        if power.choices is not None:
            lines.append(f"  - allowed modes: {', '.join(power.choices)}")
        rail = contract.rail(power.rail)
        if rail is not None:
            voltage = (
                f"at {format_magnitude(rail.voltage.value, rail.voltage.unit)}"
                if rail.voltage is not None
                else "at an undeclared voltage"
            )
            lines.append(
                f"  - rail {rail.id} capacity: {format_magnitude(rail.capacity.value, rail.capacity.unit)}"
                f" {voltage}{_provenance_suffix(rail.provenance)}"
            )
            if rail.capacity_enforced_by is not None:
                lines.append(
                    f"  - rail {rail.id} capacity {_enforcement_detail(rail.capacity_enforced_by)}"
                )
            if rail.voltage_range is not None:
                lines.append(
                    f"  - rail {rail.id} voltage range: "
                    f"[{format_magnitude(rail.voltage_range.min.value, rail.voltage_range.min.unit)}, "
                    f"{format_magnitude(rail.voltage_range.max.value, rail.voltage_range.max.unit)}]"
                )
            if rail.current_limit is not None:
                lines.append(
                    f"  - rail {rail.id} current limit: "
                    f"{format_magnitude(rail.current_limit.value, rail.current_limit.unit)}"
                )
                if rail.current_limit_enforced_by is not None:
                    lines.append(
                        f"  - rail {rail.id} current_limit "
                        f"{_enforcement_detail(rail.current_limit_enforced_by)}"
                    )

    if instance.link_budget is not None:
        lb = instance.link_budget
        lines.append(
            f"- link_budget required_range: {format_magnitude(lb.required_range.value, lb.required_range.unit)}"
        )
        lines.append(
            f"- link_budget fade_margin_required: "
            f"{format_magnitude(lb.fade_margin_required.value, lb.fade_margin_required.unit)}"
        )
        if lb.fade_margin_required_enforced_by is not None:
            lines.append(
                f"  - link_budget fade_margin_required "
                f"{_enforcement_detail(lb.fade_margin_required_enforced_by)}"
            )
        lines.append(
            f"- link_budget installation_loss: "
            f"{format_magnitude(lb.installation_loss.value, lb.installation_loss.unit)}"
        )
        if lb.carrier_frequency is not None:
            lines.append(
                f"- link_budget carrier_frequency: "
                f"{format_magnitude(lb.carrier_frequency.value, lb.carrier_frequency.unit)}"
            )
        if lb.tx_antenna is not None:
            lines.append(
                f"- link_budget tx_antenna gain: {format_magnitude(lb.tx_antenna.gain.value, lb.tx_antenna.gain.unit)}"
                f"{_provenance_suffix(lb.tx_antenna.provenance)}"
            )
        if lb.rx_antenna is not None:
            lines.append(
                f"- link_budget rx_antenna gain: {format_magnitude(lb.rx_antenna.gain.value, lb.rx_antenna.gain.unit)}"
                f"{_provenance_suffix(lb.rx_antenna.provenance)}"
            )

    if instance.thermal is not None:
        th = instance.thermal
        if th.resistance is not None:
            lines.append(
                f"- thermal resistance: {format_magnitude(th.resistance.value, th.resistance.unit)}"
                f"{_provenance_suffix(th.provenance)}"
            )
        if th.ambient is not None:
            lines.append(
                f"- thermal ambient: {format_magnitude(th.ambient.value, th.ambient.unit)}"
            )

    for envelope in instance.envelopes:
        env_prov_suffix = _provenance_suffix(envelope.provenance)
        if envelope.rate_hz is not None:
            lines.append(
                f"- observed envelope [{envelope.context}] rate_hz: "
                f"{envelope.rate_hz}{band_details(envelope.rate_hz)}{env_prov_suffix}"
            )
        for name, signal in sorted(envelope.signals.items()):
            lines.append(
                f"- observed envelope [{envelope.context}] signal {name}: "
                f"{signal.band}{band_details(signal.band)}{env_prov_suffix}"
            )
        for name, band in sorted(envelope.conditions.items()):
            lines.append(
                f"- observed envelope [{envelope.context}] condition {name}: "
                f"{band}{band_details(band)}{env_prov_suffix}"
            )

    if not lines:
        return ["Limits enforced: nothing declared."]
    return lines


def describe_device(contract: Contract, device: DeviceInstance, human: bool = False) -> str:
    """Render one device's Markdown reference section."""
    model = contract.device_model(device.model)
    family = contract.family(model.family) if model is not None else None

    lines = [f"# {human_name(device.id) if human else device.id}", ""]
    lines.append(f"- model: {human_name(device.model) if human else device.model}")
    if family is not None:
        lines.append(f"- family: {family.id} — {family.name}")
    lines.append(f"- owner: {device.owner}")
    if device.identity is not None:
        lines.append(f"- identity strength: {device.identity.effective_strength()}")
        lines.append(f"- bus: {device.identity.bus_ref}")
    lines.append("")

    lines.append("## Role")
    lines.append("")
    lines.append(_role(model, device))
    lines.append("")

    lines.append("## Measures")
    lines.append("")
    lines.extend(_measures_section(device))
    lines.append("")

    lines.append("## Adjusts")
    lines.append("")
    lines.extend(_adjusts_section(contract, model, device))
    lines.append("")

    lines.append("## Limits enforced")
    lines.append("")
    lines.extend(_limits_section(contract, model, device))
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    prov_lines = _provenance_block("instance", device.provenance) + _provenance_block(
        "model", model.provenance if model is not None else None
    )
    lines.extend(prov_lines if prov_lines else ["Provenance: nothing declared."])
    lines.append("")

    return "\n".join(lines)


def _tolerance_str(tolerance: Tolerance) -> str:
    """Render whichever of the two exactly-one-required fields is set.

    Never guess the other one and never default it: a rel_pct
    edge shows its percent, an abs edge shows its magnitude, and nothing here
    falls back to 0 or any other stand-in number for the field that is
    genuinely not this edge's kind of tolerance.
    """
    if tolerance.rel_pct is not None:
        return f"{tolerance.rel_pct:g}%"
    if tolerance.abs is not None:
        return format_magnitude(tolerance.abs.value, tolerance.abs.unit)
    return "not declared"  # unreachable: Tolerance's own validator requires exactly one


def _edges_table(contract: Contract) -> list[str]:
    if not contract.edges:
        return []
    lines = [
        "## Edges",
        "",
        "| quantity | tolerance | files | owner |",
        "| --- | --- | --- | --- |",
    ]
    for edge in contract.edges:
        lines.append(
            f"| {edge.quantity} | {_tolerance_str(edge.tolerance)} | "
            f"{', '.join(edge.code)} | {edge.owner} |"
        )
    lines.append("")
    lines.append(
        "_tolerance is scored against a supplied current reading "
        "(`check_edge_tolerance`); the `check` command supplies none today, "
        "so it renders `not_scored` until a caller passes one._"
    )
    lines.append("")
    return lines


_JOINT_WORDS = {
    "revolute": "Rotates",
    "continuous": "Rotates",
    "prismatic": "Slides",
    "fixed": "Fixed",
}


def _joints_table(contract: Contract, human: bool = False) -> list[str]:
    if contract.model is None or not contract.model.joints:
        return []
    lines = ["## Joints", "", "| name | type | parent | child |", "| --- | --- | --- | --- |"]
    for joint in contract.model.joints:
        if human:
            if joint.parent.lower() == "world":  # the fixed ground frame is never a part
                continue
            kind = _JOINT_WORDS.get(str(joint.type), str(joint.type).capitalize())
            cells = (
                human_name(joint.name),
                kind,
                human_name(joint.parent),
                human_name(joint.child),
            )
        else:
            cells = (joint.name, joint.type, joint.parent, joint.child)
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _mag(human: bool, value: float, unit: str) -> str:
    """The app's plain reading (``format_display``) or the engineer's fixed two decimals."""
    return format_display(value, unit) if human else format_magnitude(value, unit)


def _rail_voltage_label(rail: Rail, human: bool = False) -> str:
    """an undeclared voltage is painted "unknown" — never blank, never invented."""
    if rail.voltage is None:
        return "unknown"
    return _mag(human, rail.voltage.value, rail.voltage.unit)


def _rail_loads(contract: Contract, rail: Rail, depth: int, human: bool = False) -> list[str]:
    indent = "  " * depth
    lines = []
    for device in contract.devices:
        power = device.power
        if power is None or power.rail != rail.id:
            continue
        draw = (
            _mag(human, power.draw.value, power.draw.unit)
            if power.draw is not None
            else unknown_draw_message(device.id, power.rail)
        )
        lines.append(
            f"{indent}- {human_name(device.id)} · {draw}"
            if human
            else f"{indent}- load {device.id}: {draw}"
        )
    return lines


def _rail_subtree(
    contract: Contract, rail: Rail, depth: int, seen: set[str], human: bool = False
) -> list[str]:
    """One rail plus its converter edge (if any), its loads, and its children.

    The converter is its own node, never the rail standing in for
    it. A converter is drawn only where one actually exists — ``upstream ==
    "battery"`` or a parent rail id AND ``efficiency != 1.0``; the fixture
    convention (x500) is that ``efficiency == 1.0`` is a pass-through, not a
    converter, and ``upstream is None`` means no conversion edge was sourced at
    all (``power_rollup`` treats it identically: no efficiency applied).
    """
    lines: list[str] = []
    if rail.upstream is not None and rail.efficiency != 1.0:
        indent = "  " * depth
        lines.append(
            f"{indent}- {'Converter' if rail.voltage is None else _rail_voltage_label(rail, True) + ' converter'}"
            if human
            else f"{indent}- converter (efficiency: {rail.efficiency:g})"
        )
        depth += 1
    indent = "  " * depth
    capacity = _mag(human, rail.capacity.value, rail.capacity.unit)
    if human:
        detail = (
            f"up to {capacity}, voltage unknown"
            if rail.voltage is None
            else f"{_rail_voltage_label(rail, human)}, up to {capacity}"
        )
        lines.append(f"{indent}- {human_name(rail.id)} · {detail}")
    else:
        lines.append(
            f"{indent}- rail {rail.id} ({_rail_voltage_label(rail, human)}, capacity {capacity})"
        )
    lines.extend(_rail_loads(contract, rail, depth + 1, human))
    for child in contract.topology.rails if contract.topology is not None else []:
        if child.upstream == rail.id and child.id not in seen:
            lines.extend(_rail_subtree(contract, child, depth + 1, seen | {child.id}, human))
    return lines


def render_power_tree(contract: Contract, human: bool = False) -> list[str]:
    """The rail roll-up (``Rail.upstream``/``efficiency``, ``checks.power_rollup``)
    rendered as a power tree: source -> converter -> rail -> loads. ZERO NEW
    PHYSICS — this paints what the contract already declares and computes,
    nothing inferred or guessed."""
    topology = contract.topology
    if topology is None or not topology.rails:
        return []
    battery = topology.battery
    root = "Battery"
    if (
        human and battery is not None
    ):  # the battery's own figures ride on the root: no second "Battery" heading
        root += f" · {_mag(human, battery.capacity.value, battery.capacity.unit)}"
        if battery.voltage is not None:
            root += f", {_mag(human, battery.voltage.value, battery.voltage.unit)}"
    lines = [root if human else "battery"]
    for rail in topology.rails:
        if rail.upstream in (None, "battery"):
            lines.extend(_rail_subtree(contract, rail, 1, {rail.id}, human))
    if not human and any(rail.upstream is not None for rail in topology.rails):
        lines.append(
            "(converters are drawn where the contract declares an upstream and an efficiency below 1; "
            "a converter declared any other way is not shown)"
        )
    return lines


def describe_system(contract: Contract, human: bool = False) -> str:
    """Render the system-level Markdown reference: topology, budgets, joints."""
    robot = contract.robot
    lines = [f"# {robot_title(robot.name) if human else robot.name}", ""]
    lines.append(f"- schema_version: {contract.schema_version}")
    if robot.id is not None:
        lines.append(f"- id: {robot.id}")
    lines.append("")

    topology = contract.topology
    tree = render_power_tree(contract, human)
    if topology is not None and topology.rails:
        lines.append("## Rails")
        lines.append("")
        if human:
            lines.append(
                "| rail | voltage | voltage range | capacity | current limit | fed from | charged by |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        else:
            lines.append(
                "| id | voltage | voltage_range | capacity | current_limit | upstream | efficiency | charged_by |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for rail in topology.rails:
            voltage = (
                _mag(human, rail.voltage.value, rail.voltage.unit)
                if rail.voltage is not None
                else "unknown"
            )
            voltage_range = (
                f"[{_mag(human, rail.voltage_range.min.value, rail.voltage_range.min.unit)}, "
                f"{_mag(human, rail.voltage_range.max.value, rail.voltage_range.max.unit)}]"
                if rail.voltage_range is not None
                else "-"
            )
            current_limit = (
                _mag(human, rail.current_limit.value, rail.current_limit.unit)
                if rail.current_limit is not None
                else "-"
            )
            capacity = _mag(human, rail.capacity.value, rail.capacity.unit)
            if rail.capacity_enforced_by is not None:
                capacity += f" ({_enforcement_detail(rail.capacity_enforced_by)})"
            if rail.current_limit_enforced_by is not None:
                current_limit += f" ({_enforcement_detail(rail.current_limit_enforced_by)})"
            if human:
                lines.append(
                    f"| {human_name(rail.id)} | {voltage} | {voltage_range} | {capacity} | {current_limit} | "
                    f"{human_name(rail.upstream) if rail.upstream else '-'} | "
                    f"{human_name(rail.charged_by) if rail.charged_by else '-'} |"
                )
                continue
            lines.append(
                f"| {rail.id} | {voltage} | {voltage_range} | "
                f"{capacity} | {current_limit} | "
                f"{rail.upstream or '-'} | {rail.efficiency:g} | {rail.charged_by or '-'} |"
            )
        lines.append("")

        if tree:
            lines.append("## Power tree")
            lines.append("")
            lines.extend(tree)
            lines.append("")

    if topology is not None and topology.buses:
        lines.append("## Buses")
        lines.append("")
        lines.append("| bus | kind | speed |" if human else "| id | kind | bitrate |")
        lines.append("| --- | --- | --- |")
        for bus in topology.buses:
            bitrate = "-" if bus.bitrate is None else f"{bus.bitrate:g}"
            if human:
                bitrate = "-" if bus.bitrate is None else _bit_rate(bus.bitrate)
                lines.append(f"| {_bus_name(bus.id)} | {bus.kind.upper()} | {bitrate} |")
                continue
            lines.append(f"| {bus.id} | {bus.kind} | {bitrate} |")
        lines.append("")

    if topology is not None and topology.battery is not None and not (human and tree):
        battery = topology.battery
        lines.append("## Battery")
        lines.append("")
        lines.append(
            f"- {'Capacity' if human else 'capacity'}: {_mag(human, battery.capacity.value, battery.capacity.unit)}"
        )
        if battery.voltage is not None:
            lines.append(
                f"- {'Voltage' if human else 'voltage'}: {_mag(human, battery.voltage.value, battery.voltage.unit)}"
            )
        lines.append("")

    if topology is not None and topology.wires:
        lines.append("## Wires")
        lines.append("")
        lines.append("| id | awg | installation | rail |")
        lines.append("| --- | --- | --- | --- |")
        for wire in topology.wires:
            lines.append(f"| {wire.id} | {wire.awg} | {wire.installation} | {wire.rail} |")
        lines.append("")

    lines.extend(_edges_table(contract))

    if contract.budgets:
        lines.append("## Budgets")
        lines.append("")
        lines.append("| budget | limit |" if human else "| id | kind | limit |")
        lines.append("| --- | --- |" if human else "| --- | --- | --- |")
        for budget in contract.budgets:
            limit = _mag(human, budget.limit.value, budget.limit.unit)
            if budget.limit_enforced_by is not None:
                limit += f" ({_enforcement_detail(budget.limit_enforced_by)})"
            if human:
                lines.append(f"| {_budget_name(budget.id)} | {limit} |")
                continue
            lines.append(f"| {budget.id} | {budget.kind} | {limit} |")
        lines.append("")

    lines.extend(_joints_table(contract, human))

    if contract.actuator_cases:
        lines.append("## Actuator cases")
        lines.append("")
        lines.append(
            "| id | actuator device | joint | rail | DC delivery | feedback loss max | model input manifest | clock mappings |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for case in contract.actuator_cases:
            mappings = ", ".join(
                f"{ref.path} ({ref.sha256})" for ref in case.clock_mapping_artifacts
            )
            if case.dc_delivery is None:
                dc_delivery = "not declared"
            else:
                target = case.dc_delivery
                artifacts = ", ".join(
                    f"{ref.path} ({ref.sha256})" for ref in target.provenance_artifacts
                )
                dc_delivery = (
                    f"{target.applicability}; condition {target.condition_id}; "
                    f"{target.source_node} -> {target.receiver_node} -> {target.return_node}; "
                    f"source >= {target.source_voltage_min}; peak <= {target.peak_current_max}; "
                    f"Rmax source/out/return/contact {target.source_resistance_max}/"
                    f"{target.outbound_resistance_max}/{target.return_resistance_max}/"
                    f"{target.contact_resistance_max}; {artifacts}"
                )
            lines.append(
                f"| {case.id} | {case.actuator_device} | {case.joint} | {case.rail} | "
                f"{dc_delivery} | "
                f"{case.feedback_loss_max if case.feedback_loss_max is not None else 'not declared'} | "
                f"{case.model_input_manifest.path} ({case.model_input_manifest.sha256}) | "
                f"{mappings} |"
            )
        lines.append("")

    # the same force numbers the check document carries, rendered from that
    # same payload so the two faces cannot print different figures — or a figure
    # without its kind. The human face shows three plain lines; the rest is `engineer_detail`.
    if human:
        lines.extend(_forces_plain(force_document(contract, compute_system_cg(contract))))
    else:
        lines.extend(render_markdown(force_document(contract, compute_system_cg(contract))))
        lines.append(_device_count_line(contract))
        lines.append("")

    return "\n".join(lines)


def describe_all(contract: Contract, human: bool = False) -> dict[str, str]:
    """Filename -> Markdown for every device (ids are unique, see Contract._unique_ids), plus SYSTEM.md."""
    result = {
        f"{device.id}.md": describe_device(contract, device, human) for device in contract.devices
    }
    result["SYSTEM.md"] = describe_system(contract, human)
    return result


def engineer_detail(contract: Contract) -> str:
    """What the plain Forces lines leave out: every mass row, kind and ceiling, plus the device tally."""
    lines = render_markdown(force_document(contract, compute_system_cg(contract)))
    return "\n".join([*lines, _device_count_line(contract), ""])


def _device_count_line(contract: Contract) -> str:
    measures = adjusts = component = 0
    for device in contract.devices:
        role = _role(contract.device_model(device.model), device)
        if role == "measures and adjusts":
            measures += 1
            adjusts += 1
        elif role == "measures":
            measures += 1
        elif role == "adjusts":
            adjusts += 1
        else:
            component += 1
    return f"{len(contract.devices)} device(s): {measures} measure, {adjusts} adjust, {component} component-only."


def _forces_plain(document: dict) -> list[str]:
    """Total mass, centre of gravity, gravity: units, the one display format, no kinds or engine ids."""

    def one(entry: dict) -> str:
        if entry["kind"] == "not_scored":
            # the common true reason in plain words; any other stays in the Engineer report
            because = (
                "no total-mass budget declared"
                if "no mass_total budget" in entry.get("not_scored_because", "")
                else "details in the Engineer report"
            )
            return f"NOT SCORED — {because}"
        value = entry["value"]
        unit = entry["unit"]
        if isinstance(value, list):  # one unit for the whole vector, so no per-component g/mm steps
            text = "(" + ", ".join(format_display(c, "") for c in value) + f") {unit}"
        else:
            text = format_display(value, unit)
        text = text.replace("^2", "²").strip()
        return text + (" · Incomplete" if entry.get("limitation") else "")

    return [
        "## Forces",
        "",
        f"- Total mass: {one(document['mass_total'])}",
        f"- Centre of gravity: {one(document['cg'])}",
        f"- Gravity: {one(document['gravity'])}",
        "",
    ]


def _bit_rate(bits: float) -> str:
    for scale, unit in ((1e9, "Gbit/s"), (1e6, "Mbit/s"), (1e3, "kbit/s")):
        if bits >= scale:
            return f"{bits / scale:g} {unit}"
    return f"{bits:g} bit/s"


def _bus_name(identifier: str) -> str:
    return human_name(identifier).replace("CAN DroneCAN", "CAN (DroneCAN)")


def _budget_name(identifier: str) -> str:
    """ "bud.power.pm02-5v" -> "PM02 5 V power"; "bud.mass.total" -> "Total mass"; "bud.endurance.x" -> "Endurance"."""
    head, *rest = re.split(r"[.-]", identifier.removeprefix("bud."), maxsplit=1)
    tail = human_name(rest[0]) if rest else ""
    if head == "power" and tail:
        return f"{tail} power"
    if head == "mass" and tail:
        return f"{tail} mass"
    return human_name(head) if head in ("endurance", "power", "mass") else human_name(identifier)
