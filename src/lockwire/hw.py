"""W0 declared hardware-change events — the git-commit-for-the-physical-robot.

A human declares a change; this module records it as a sidecar YAML event log
alongside the contract and routes it to contract entries (and owners) the change
may have invalidated. The event is a declaration, never a locked fact: it must
not mutate the contract and must not be read by checks as an authoritative entry.

Routing walks the contract's own reference structure (device→bus/rail, budgets
that consume mass/power, wires on a rail, dependency edges on mass paths). When
bindings are missing and the declaration cannot be resolved to those refs, the
result names the gap explicitly — silent empty routing is the false-GO shape.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Iterable

from ruamel.yaml import YAML

from lockwire.schema import Base, Contract, Quantity

# Units a free-text W0 declaration is expected to carry. Longest first so
# "mW" wins over "W" and "kg" over "g". Quantity validation still rejects
# anything unknown if this list is extended carelessly.
_PARSE_UNITS = ("kWh", "mWh", "mW", "kg", "mg", "Wh", "kW", "g", "W")
_QUANTITY_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>" + "|".join(_PARSE_UNITS) + r")\b")
_ADD_RE = re.compile(r"^(added|add)\b", re.IGNORECASE)
_REMOVE_RE = re.compile(r"^(removed|remove|deleted|delete)\b", re.IGNORECASE)


def plain_yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.sort_base_mapping_type_on_output = False
    return y


class HardwareEvent(Base):
    """One declared hardware-change witness (W0)."""

    declaration: str
    recorded_at: dt.datetime
    quantities: list[Quantity] = []
    # Optional routing binders — event-log fields, not Contract schema.
    device: str | None = None
    bus: str | None = None
    rail: str | None = None


class HardwareLog(Base):
    """Append-only event log persisted beside the contract."""

    events: list[HardwareEvent] = []


class TouchedEntry(Base):
    """One contract path the event may have invalidated."""

    path: str
    owner: str | None = None
    reason: str


class RoutingResult(Base):
    touched: list[TouchedEntry] = []
    undetermined: list[str] = []


def events_path_for(contract_path: Path) -> Path:
    """Sidecar path: ``lockwire.yaml`` → ``lockwire.hw.yaml``."""
    return contract_path.with_name(f"{contract_path.stem}.hw.yaml")


def parse_quantities(declaration: str) -> list[Quantity]:
    """Pull ``{value, unit}`` tokens out of a free-text declaration."""
    found: list[Quantity] = []
    seen: set[tuple[float, str]] = set()
    for match in _QUANTITY_RE.finditer(declaration):
        value = float(match.group("value"))
        unit = match.group("unit")
        key = (value, unit)
        if key in seen:
            continue
        seen.add(key)
        found.append(Quantity(value=value, unit=unit))
    return found


def change_kind(declaration: str) -> str:
    """Coarse verb from the declaration lead-in: add / remove / change."""
    text = declaration.strip()
    if _ADD_RE.match(text):
        return "add"
    if _REMOVE_RE.match(text):
        return "remove"
    return "change"


def load_log(path: Path) -> HardwareLog:
    if not path.exists():
        return HardwareLog()
    with open(path, encoding="utf-8") as handle:
        data = plain_yaml().load(handle)
    if data is None:
        return HardwareLog()
    return HardwareLog.model_validate(data)


def save_log(log: HardwareLog, path: Path) -> None:
    data = log.model_dump(mode="json", exclude_none=True)
    with open(path, "w", encoding="utf-8") as handle:
        plain_yaml().dump(data, handle)


def append_event(log_path: Path, event: HardwareEvent) -> HardwareLog:
    log = load_log(log_path)
    log.events.append(event)
    save_log(log.revalidate(), log_path)
    return log


def _add_touch(
    touches: dict[str, TouchedEntry],
    path: str,
    *,
    owner: str | None,
    reason: str,
) -> None:
    existing = touches.get(path)
    if existing is None:
        touches[path] = TouchedEntry(path=path, owner=owner, reason=reason)
        return
    # Keep the first owner; merge reasons when the same path is reached twice.
    if reason not in existing.reason:
        touches[path] = TouchedEntry(
            path=path,
            owner=existing.owner if existing.owner is not None else owner,
            reason=f"{existing.reason}; {reason}",
        )


def _rail_ancestors(contract: Contract, rail_id: str) -> list[str]:
    """Rail id plus upstream ancestors, walking the contract's rail tree."""
    ordered: list[str] = []
    seen: set[str] = set()
    current_id: str | None = rail_id
    while current_id is not None and current_id not in seen:
        rail = contract.rail(current_id)
        if rail is None:
            break
        ordered.append(rail.id)
        seen.add(rail.id)
        upstream = rail.upstream
        if upstream is None or upstream == "battery":
            break
        current_id = upstream
    return ordered


def _path_is_mass(path: str) -> bool:
    parts = path.split("/")
    return "mass" in parts or path.endswith("/physical/mass")


def _route_mass(contract: Contract, touches: dict[str, TouchedEntry]) -> None:
    if contract.model is not None:
        _add_touch(
            touches,
            "model",
            owner=None,
            reason="mass/COM change may invalidate the locked model",
        )
    for budget in contract.budgets:
        if budget.kind == "mass_total":
            _add_touch(
                touches,
                f"budgets/{budget.id}",
                owner=budget.owner,
                reason="mass_total budget consumes declared mass",
            )


def _route_bus(contract: Contract, touches: dict[str, TouchedEntry], bus_id: str) -> bool:
    if contract.topology is None:
        return False
    bus = next((row for row in contract.topology.buses if row.id == bus_id), None)
    if bus is None:
        return False
    owners = sorted(
        {
            device.owner
            for device in contract.devices
            if device.identity is not None and device.identity.bus_ref == bus_id
        }
    )
    _add_touch(
        touches,
        f"topology/buses/{bus_id}",
        owner=", ".join(owners) if owners else None,
        reason="device change sits on this bus",
    )
    return True


def _route_rail(contract: Contract, touches: dict[str, TouchedEntry], rail_id: str) -> bool:
    if contract.rail(rail_id) is None:
        return False
    ancestors = _rail_ancestors(contract, rail_id)
    ancestor_set = set(ancestors)
    owners = sorted(
        {
            device.owner
            for device in contract.devices
            if device.power is not None and device.power.rail == rail_id
        }
    )
    for ancestor in ancestors:
        _add_touch(
            touches,
            f"topology/rails/{ancestor}",
            owner=", ".join(owners) if owners else None,
            reason=(
                "device change draws on this rail"
                if ancestor == rail_id
                else "upstream rail in the power rollup of the touched rail"
            ),
        )
    for budget in contract.budgets:
        if budget.kind == "rail_power" and budget.rail in ancestor_set:
            _add_touch(
                touches,
                f"budgets/{budget.id}",
                owner=budget.owner,
                reason="rail_power budget consumes draw on the touched rail tree",
            )
        elif budget.kind == "endurance":
            _add_touch(
                touches,
                f"budgets/{budget.id}",
                owner=budget.owner,
                reason="endurance budget rolls device draw through the rail tree to the battery",
            )
    if contract.topology is not None:
        for wire in contract.topology.wires:
            if wire.rail in ancestor_set:
                _add_touch(
                    touches,
                    f"topology/wires/{wire.id}",
                    owner=wire.owner,
                    reason="wire ampacity depends on rolled-up draw on this rail",
                )
    return True


def _route_device(contract: Contract, touches: dict[str, TouchedEntry], device_id: str) -> bool:
    device = contract.device(device_id)
    if device is None:
        return False
    _add_touch(
        touches,
        f"devices/{device_id}",
        owner=device.owner,
        reason="bound device entry",
    )
    for edge in contract.edges:
        if edge.quantity == device_id or edge.quantity.startswith(f"devices/{device_id}/"):
            reason = "dependency edge on the bound device"
            if _path_is_mass(edge.quantity):
                reason = "dependency edge on the bound device's mass"
            _add_touch(
                touches,
                edge.quantity,
                owner=edge.owner,
                reason=reason,
            )
    return True


def route_event(contract: Contract, event: HardwareEvent) -> RoutingResult:
    """Name contract entries (and owners) the event may have invalidated."""
    touches: dict[str, TouchedEntry] = {}
    undetermined: list[str] = []

    dims = {quantity.dimension() for quantity in event.quantities}
    kind = change_kind(event.declaration)
    device_shaped = kind in ("add", "remove") or event.device is not None

    if "mass" in dims:
        _route_mass(contract, touches)

    device = contract.device(event.device) if event.device is not None else None
    if event.device is not None and device is None:
        undetermined.append(
            f"device {event.device!r} not in contract — cannot bind bus/rail from it"
        )

    bus_id = event.bus
    if bus_id is None and device is not None and device.identity is not None:
        bus_id = device.identity.bus_ref

    rail_id = event.rail
    if rail_id is None and device is not None and device.power is not None:
        rail_id = device.power.rail

    needs_electrical = device_shaped or "power" in dims

    if bus_id is not None:
        if not _route_bus(contract, touches, bus_id):
            undetermined.append(f"bus {bus_id!r} not in contract topology")
    elif needs_electrical:
        undetermined.append(
            "bus not bound — pass --bus or --device to route to a bus; "
            "device addition without a bus cannot be routed to topology/buses"
        )

    if rail_id is not None:
        if not _route_rail(contract, touches, rail_id):
            undetermined.append(f"rail {rail_id!r} not in contract topology")
    elif needs_electrical:
        undetermined.append(
            "rail not bound — pass --rail or --device to route to power/endurance/ampacity; "
            "device addition without a rail cannot be routed to those budgets"
        )

    if event.device is not None:
        _route_device(contract, touches, event.device)

    if not touches and not undetermined:
        undetermined.append(
            "could not determine any touched entries from the declaration — "
            "include a mass/power quantity and/or bind --device/--bus/--rail"
        )

    touched = sorted(touches.values(), key=lambda row: row.path)
    return RoutingResult(touched=touched, undetermined=undetermined)


def build_event(
    declaration: str,
    *,
    recorded_at: dt.datetime | None = None,
    quantities: Iterable[Quantity] | None = None,
    device: str | None = None,
    bus: str | None = None,
    rail: str | None = None,
) -> HardwareEvent:
    """Assemble an event, parsing quantities from the declaration when omitted."""
    parsed = list(quantities) if quantities is not None else parse_quantities(declaration)
    when = recorded_at if recorded_at is not None else dt.datetime.now(dt.UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return HardwareEvent(
        declaration=declaration,
        recorded_at=when,
        quantities=parsed,
        device=device,
        bus=bus,
        rail=rail,
    )


def record_hardware_change(
    contract: Contract,
    contract_path: Path,
    declaration: str,
    *,
    recorded_at: dt.datetime | None = None,
    device: str | None = None,
    bus: str | None = None,
    rail: str | None = None,
) -> tuple[HardwareEvent, Path, RoutingResult]:
    """Persist a W0 event beside the contract and return its routing.

    Does not mutate ``contract`` or ``contract_path``.
    """
    event = build_event(
        declaration,
        recorded_at=recorded_at,
        device=device,
        bus=bus,
        rail=rail,
    )
    log_path = events_path_for(contract_path)
    append_event(log_path, event)
    return event, log_path, route_event(contract, event)
