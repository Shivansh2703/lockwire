"""Pin rules: voltage, current and special pins across wired boards.

Free engine code; ``checks.py`` loads it through ``ENGINE_MODULES``. Reads only the connector fields
(device connectors and pins, pin-to-pin wires) and the pinout cards (``catalog.load_pinouts``).
A finding is a WARNING only when the threshold is a pinout card's own sourced number (cited);
every other number ships as advice (``advice_unsourced``) that never moves a verdict. Missing
input emits nothing. On a contract pin, ``max_current_ma`` is the current declared on that pin.
"""

from __future__ import annotations

import functools
from pathlib import Path

from lockwire.report import Finding, Quantity

LABEL = "checked against recorded files, not hardware"
_MARGIN_V = 0.3  # unsourced slack over a receiver's own supply
# card number keys, first one present wins
_PIN_LIMIT = ("gpio_per_pin", "io_max_drive", "gpio_per_pin_source_typ")
_BOARD_LIMIT = ("gpio_total_abs_max", "gpio_total_source")
_RX_LIMIT = ("supply_abs_max", "operating_max")


@functools.cache
def _cards() -> dict:
    from lockwire.catalog import load_pinouts

    return {c.id: c for c in load_pinouts()}


def _volts(q) -> float | None:
    return q.base() if q is not None else None


class _End:
    """One wire end: the contract's pin laid over the card's pin; each value knows its source."""

    def __init__(self, contract, end):
        self.dev = contract.device(end.device)
        self.card = _cards().get(self.dev.model) if self.dev else None
        self.own = self.base = None
        self.src = None
        if self.dev is None:
            return
        key = str(end.pin)
        for conn in self.dev.connectors or []:
            if end.connector in (None, conn.id):
                self.own = self.own or _find(conn.pins, key)
        for conn in self.card.connectors if self.card else []:
            if end.connector in (None, conn.id) and (p := _find(conn.pins, key)):
                self.base, self.src = p, conn.source
                break
        self.pin = self.own or self.base

    def __bool__(self):
        return self.dev is not None and self.pin is not None

    def get(self, field):
        """(value, source); source is None for a user-declared value."""
        for pin, src in ((self.own, None), (self.base, self.src)):
            if pin is not None and getattr(pin, field) is not None:
                return getattr(pin, field), src
        return None, None

    def number(self, field):
        n = self.card.numbers.get(field) if self.card else None
        return (n.value, n.source) if n else (None, None)

    @property
    def label(self):
        return str(self.get("name")[0] or self.pin.number)

    @property
    def maker(self):
        return self.card.maker if self.card else self.dev.id

    def level(self):
        """The voltage the pin sits at / drives: fixed level, else top of its range."""
        v, src = self.get("voltage_v")
        if v is None:
            rng, src = self.get("voltage_range")
            v = _volts(rng.max) if rng else None
        return (v, src) if v is not None else self.get("voh")

    def drives(self):
        t, d = self.get("type")[0], self.get("direction")[0]
        return d in ("out", "inout") or t in ("output", "bidirectional", "tri_state", "power_out")

    def receives(self):
        return self.get("direction")[0] != "out" and self.get("type")[0] not in (
            "output",
            "power_out",
        )


def _first(end, keys):
    for k in keys:
        if end.number(k)[0] is not None:
            return end.number(k)
    return None, None


def _find(pins, key):
    for p in pins or []:
        if key in (str(p.number), p.name, *(p.aliases or [])):
            return p
    return None


def _finding(wire, observed, expected, action, cite, oq=None, eq=None) -> Finding:
    return Finding(
        severity="warning",
        quantity=f"topology/wires/{wire}",
        observed=observed,
        expected=expected,
        observed_quantity=oq,
        expected_quantity=eq,
        action=f"{action} ({LABEL})",
        source=cite,
        advice_unsourced=cite is None,
    )


def _wired(contract):
    """Wires with a pin at both ends on two different boards (rows 618-619: the cross-board pass)."""
    wires = contract.topology.wires if contract.topology else []
    for w in wires:
        if w.from_ is None or w.to is None or w.from_.device == w.to.device:
            continue
        a, b = _End(contract, w.from_), _End(contract, w.to)
        if a and b:
            yield w, a, b


def check_voltage(contract, contract_path: Path | None = None) -> list[Finding]:
    """A driver's high into a receiver that cannot take it, or too weak to be read as high."""
    out = []
    for w, a, b in _wired(contract):
        for drv, rx in ((a, b), (b, a)):
            if not (drv.drives() and rx.receives()):
                continue
            dv, rv = drv.level()[0], rx.level()[0]
            if rx.get("type")[0] == "power_in":
                # a supply pin has its own rail: the card-wide logic limit never applies to it
                rng, cite = rx.get("voltage_range")
                limit = _volts(rng.max) if rng else None
            else:
                limit, cite = _first(rx, _RX_LIMIT)
            if limit is None and rv is not None:
                limit = rv + _MARGIN_V  # the receiver's own declared level plus slack: advice
            if dv is not None and rv is not None and limit is not None and dv > limit:
                out.append(
                    _finding(
                        w.id,
                        f"{rx.label} is {rv:g} V; {drv.maker} {drv.label} drives {dv:g} V",
                        f"<= {limit:g} V into {rx.dev.id} {rx.label}",
                        f"level-shift or divide {drv.dev.id} {drv.label} before {rx.dev.id}",
                        cite,
                        Quantity(value=dv, unit="V"),
                        Quantity(value=limit, unit="V"),
                    )
                )
            voh, vih = drv.get("voh")[0] or drv.number("voh_min")[0], rx.get("vih")
            vih_v, vih_src = vih if vih[0] is not None else rx.number("vih_min")
            if voh is not None and vih_v is not None and voh < vih_v:
                out.append(
                    _finding(
                        w.id,
                        f"{drv.dev.id} {drv.label} drives only {voh:g} V high",
                        f">= {vih_v:g} V (VIH of {rx.dev.id} {rx.label})",
                        "add a level shifter or pick a pin that reaches VIH",
                        vih_src,
                        Quantity(value=voh, unit="V"),
                        Quantity(value=vih_v, unit="V"),
                    )
                )
            vol = drv.get("vol")[0] or drv.number("vol_max")[0]
            vil_v, vil_src = rx.get("vil") if rx.get("vil")[0] is not None else rx.number("vil_max")
            if vol is not None and vil_v is not None and vol > vil_v:
                out.append(
                    _finding(
                        w.id,
                        f"{drv.dev.id} {drv.label} drives {vol:g} V low",
                        f"<= {vil_v:g} V (VIL of {rx.dev.id} {rx.label})",
                        "the low level may be read as high; add a pull-down or change the pin",
                        vil_src,
                        Quantity(value=vol, unit="V"),
                        Quantity(value=vil_v, unit="V"),
                    )
                )
    return out


def check_output_clash(contract, contract_path: Path | None = None) -> list[Finding]:
    """A net (wires joined at shared pins) with two or more push-pull outputs."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ends: dict = {}  # pin key -> (_End, end), the pin's wires' ends
    nets: dict = {}  # root -> [wire ids]
    for w, a, b in _wired(contract):
        ka, kb = (
            (e.dev.id, ref.connector, str(e.pin.number)) for e, ref in ((a, w.from_), (b, w.to))
        )
        ends[ka], ends[kb] = a, b
        parent[find(ka)] = find(kb)
    wires = {}
    for w, a, b in _wired(contract):
        wires.setdefault(find((a.dev.id, w.from_.connector, str(a.pin.number))), []).append(w.id)
    for key, e in ends.items():
        if e.get("type")[0] == "output" or e.get("direction")[0] == "out":
            nets.setdefault(find(key), []).append(e)
    return [
        _finding(
            wires[root][0],
            f"{' and '.join(f'{e.dev.id} {e.label}' for e in outs)} all drive one net",
            "one driver per net",
            "connect an output to an input, or use an open-collector pin",
            None,  # a declared pin type is the user's own; advice only
        )
        for root, outs in nets.items()
        if len(outs) > 1
    ]


def check_current(contract, contract_path: Path | None = None) -> list[Finding]:
    """Declared pin currents: each against the board's per-pin limit, their sum against its total."""
    out = []
    for dev in contract.devices:
        pins = [(c, p) for c in dev.connectors or [] for p in c.pins or [] if p.max_current_ma]
        if not pins:
            continue
        ends = [(p, _End(contract, _Ref(dev.id, c.id, p.number))) for c, p in pins]
        for p, e in ends:
            lim, src = (
                (e.base.max_current_ma, e.src)
                if e.base and e.base.max_current_ma
                else (_first(e, _PIN_LIMIT))
            )
            if lim is not None and p.max_current_ma > lim:
                out.append(
                    _finding(
                        f"{dev.id}.{e.label}",
                        f"{e.label} on {dev.id} carries {p.max_current_ma:g} mA",
                        f"<= {lim:g} mA per pin",
                        "spread the load over more pins or buffer it with a driver",
                        src,
                        Quantity(value=p.max_current_ma, unit="mA"),
                        Quantity(value=lim, unit="mA"),
                    )
                )
        total = sum(p.max_current_ma for p, _ in ends)
        lim, src = _first(ends[0][1], _BOARD_LIMIT)
        lim, src = (lim, src) if lim is not None else (dev.gpio_total_ma, None)
        if lim is not None and total > lim:
            out.append(
                _finding(
                    dev.id,
                    f"{dev.id} pins together carry {total:g} mA",
                    f"<= {lim:g} mA for the whole board",
                    "power the loads from a supply rail instead of the pins",
                    src,
                    Quantity(value=total, unit="mA"),
                    Quantity(value=lim, unit="mA"),
                )
            )
    return out


class _Ref:
    def __init__(self, device, connector, pin):
        self.device, self.connector, self.pin = device, connector, pin


def check_special_pins(contract, contract_path: Path | None = None) -> list[Finding]:
    """Wired pins that are reserved or flash, input-only used as outputs, ADC2 with the radio on."""
    out = []
    wires = contract.topology.wires if contract.topology else []
    for w in wires:
        for end in (w.from_, w.to):
            e = _End(contract, end) if end else None
            if not e:
                continue
            caps = e.get("capabilities")[0] or []
            who = f"{e.dev.id} {e.label}"
            if e.get("reserved")[0]:
                reason, src = e.get("reason")
                out.append(
                    _finding(
                        w.id,
                        f"{who} is reserved: {reason}",
                        "a free pin",
                        "move the wire to a pin that is not reserved",
                        src,
                    )
                )
            if "input_only" in caps and e.own and e.own.direction in ("out", "inout"):
                out.append(
                    _finding(
                        w.id,
                        f"{who} is input-only but declared as an output",
                        "an input",
                        "use a pin that can drive",
                        e.src,
                    )
                )
            if "adc2" in caps and e.dev.radio_on:
                out.append(
                    _finding(
                        w.id,
                        f"{who} is on ADC2 and the radio is on",
                        "ADC1 while Wi-Fi runs",
                        "read this signal on an ADC1 pin",
                        None,  # the Espressif ADC2/Wi-Fi note is not carried as a card number
                    )
                )
    return out


def erc_report(contract) -> list[dict]:
    """The system ERC (row 586): one row per wire pair or net issue, with its source or advice."""
    rows = []
    for fn in ENGINE_CHECKS:
        for f in fn(contract):
            rows.append(
                {
                    "where": f.quantity,
                    "problem": f.observed,
                    "limit": f.expected,
                    "basis": f.source or "advice: not yet sourced",
                }
            )
    return rows


ENGINE_CHECKS = (check_voltage, check_output_clash, check_current, check_special_pins)
