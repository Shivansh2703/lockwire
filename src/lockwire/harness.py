"""Harness list from the contract's wires, connectors and pins.

Plain functions over ``contract.topology.wires`` and ``contract.devices[*].connectors``.
Missing input (no pin-to-pin wires) gives empty results, never a finding. No CLI, no check.
WireViz (GPL) is never imported or run: ``wireviz_yaml`` only writes its documented keys.

Skipped on purpose: a PDF build sheet (no PDF writer in the repo), an HTML page (report_html
renders a whole contract report, it has no slot for a harness table), and the Pro
straight-line length x routing factor.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, NamedTuple

import yaml

from lockwire.schema import Contract, Wire, WireEnd

ENGINE_CHECKS: tuple = ()  # checks.py's registry imports this; the harness adds no finding

# Colour codes are the two-letter codes of IEC 60757 (the set WireViz uses). Which colour a
# wire gets is NOT an IEC rule: IEC 60445/60446 do not fix DC power colours for robots, so
# this table is only the common robotics convention, +V red and ground black.
CONVENTION = {"+V": "RD", "GND": "BK"}
IEC_60757 = {
    "BK": "BK", "BN": "BN", "RD": "RD", "OG": "OG", "YE": "YE", "GN": "GN", "BU": "BU",
    "VT": "VT", "GY": "GY", "WH": "WH", "PK": "PK", "TQ": "TQ",
    "BLACK": "BK", "BROWN": "BN", "RED": "RD", "ORANGE": "OG", "YELLOW": "YE",
    "GREEN": "GN", "BLUE": "BU", "VIOLET": "VT", "PURPLE": "VT", "GREY": "GY",
    "GRAY": "GY", "WHITE": "WH", "PINK": "PK", "TURQUOISE": "TQ",
}  # fmt: skip


class Row(NamedTuple):
    from_: str  # "device.connector.pin"
    to: str
    wire: str
    awg: int | None
    colour: str | None
    net: str | None


def _end_name(e: WireEnd) -> str:
    return f"{e.device}.{e.connector}" if e.connector else e.device


def _pin(contract: Contract, e: WireEnd):
    for d in contract.devices:
        if d.id == e.device:
            for c in d.connectors or []:
                if c.id == e.connector:
                    return next((p for p in c.pins or [] if str(p.number) == str(e.pin)), None)
    return None


def _role(pin) -> str | None:
    if pin is None:
        return None
    name = (pin.name or "").upper()
    if pin.voltage_v == 0 or name in {"GND", "GROUND", "VSS", "0V", "V-"}:
        return "GND"
    if (pin.voltage_v or 0) > 0 or name.startswith(("V", "+")):
        return "+V"
    return None


def wire_colour(contract: Contract, wire: Wire) -> str | None:
    """IEC 60757 code: the wire's own colour if declared, else the convention from its pins."""
    if wire.colour:
        return IEC_60757.get(wire.colour.strip().upper())
    for end in (wire.from_, wire.to):
        role = _role(_pin(contract, end)) if end else None
        if role:
            return CONVENTION[role]
    return None


def _pin_wires(contract: Contract) -> list[Wire]:
    return [w for w in contract.topology.wires if w.from_ and w.to]


def connection_table(contract: Contract) -> list[Row]:
    rows = []
    for w in _pin_wires(contract):
        a, b = w.from_, w.to
        net = w.net or (_pin(contract, a) or _pin(contract, b) or _NoPin).net
        rows.append(Row(f"{_end_name(a)}.{a.pin}", f"{_end_name(b)}.{b.pin}", w.id, w.awg,
                        wire_colour(contract, w), net))  # fmt: skip
    return rows


class _NoPin:
    net = None


def harness_bom(contract: Contract) -> dict[str, Any]:
    """Wire length per (awg, colour); connectors per type; contacts = wire ends landed."""
    wires: dict[tuple[int | None, str | None], dict[str, float]] = defaultdict(
        lambda: {"length_m": 0.0, "count": 0, "unmeasured": 0}
    )
    connectors: set[tuple[str, str]] = set()
    contacts = 0
    types: dict[tuple[str, str], str] = {}
    for d in contract.devices:
        for c in d.connectors or []:
            types[(d.id, c.id)] = c.type or c.id
    for w in _pin_wires(contract):
        slot = wires[(w.awg, wire_colour(contract, w))]
        slot["count"] += 1
        if w.length_m is None:
            slot["unmeasured"] += 1
        else:
            slot["length_m"] += w.length_m
        for e in (w.from_, w.to):
            contacts += 1
            if e.connector:
                connectors.add((e.device, e.connector))
    by_type: dict[str, int] = defaultdict(int)
    for key in connectors:
        by_type[types.get(key, key[1])] += 1
    return {
        "wires": [{"awg": g, "colour": c, **v} for (g, c), v in sorted(wires.items(), key=str)],
        "connectors": dict(sorted(by_type.items())),
        "contacts": contacts,
    }


# WireViz syntax: https://github.com/wireviz/WireViz/blob/master/docs/syntax.md
def wireviz_dict(contract: Contract) -> dict[str, Any]:
    wires = _pin_wires(contract)
    if not wires:
        return {}
    conns: dict[str, dict[str, str]] = {}  # name -> pin -> label
    for w in wires:
        for e in (w.from_, w.to):
            pin = _pin(contract, e)
            conns.setdefault(_end_name(e), {})[str(e.pin)] = pin.name if pin and pin.name else ""
    connectors = {}
    for name, pins in sorted(conns.items()):
        entry: dict[str, Any] = {"pins": list(pins)}
        if any(pins.values()):
            entry["pinlabels"] = [p or str(k) for k, p in pins.items()]
        connectors[name] = entry
    groups: dict[str, list[Wire]] = defaultdict(list)
    for w in wires:
        groups[w.cable or w.id].append(w)
    cables, connections = {}, []
    for name, ws in groups.items():
        cable: dict[str, Any] = {"wirecount": len(ws)}
        if ws[0].awg is not None:  # a signal wire may leave its gauge undeclared: print none
            cable["gauge"] = f"{ws[0].awg} AWG"
        colours = [wire_colour(contract, w) for w in ws]
        if all(colours):
            cable["colors"] = colours
        if all(w.length_m for w in ws):
            cable["length"] = max(w.length_m for w in ws)
        if any(w.shielded for w in ws):
            cable["shield"] = True
        cables[name] = cable
        for i, w in enumerate(ws, 1):
            connections.append(
                [
                    {_end_name(w.from_): [str(w.from_.pin)]},
                    {name: [i]},
                    {_end_name(w.to): [str(w.to.pin)]},
                ]
            )
    return {"connectors": connectors, "cables": cables, "connections": connections}


def wireviz_yaml(contract: Contract) -> str:
    doc = wireviz_dict(contract)
    return yaml.safe_dump(doc, sort_keys=False) if doc else ""
