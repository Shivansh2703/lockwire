"""Harness list from wires, connectors and pins."""

from pathlib import Path

import yaml

from lockwire import harness
from lockwire.schema import Contract

HERE = Path(__file__).parent
DRONE = HERE.parent / "src" / "lockwire" / "examples" / "drone" / "contract.yaml"

# WireViz's documented keys, https://github.com/wireviz/WireViz/blob/master/docs/syntax.md
TOP = {"connectors", "cables", "connections"}
CONNECTOR_KEYS = {"pins", "pinlabels", "pincount", "type", "subtype", "color", "notes"}
CABLE_KEYS = {"wirecount", "gauge", "length", "colors", "color_code", "shield", "notes", "type"}


def drone(with_wires=True) -> Contract:
    raw = yaml.safe_load(DRONE.read_text())
    if with_wires:
        extra = yaml.safe_load((HERE / "fixtures" / "harness" / "drone_wires.yaml").read_text())
        for d in raw["devices"]:
            d["connectors"] = extra["connectors"].get(d["id"])
        raw["topology"]["wires"] = extra["wires"]
    return Contract.model_validate(raw)


def test_colours_are_iec_60757_codes_and_convention():
    c = drone()
    by_wire = {r.wire: r.colour for r in harness.connection_table(c)}
    assert by_wire == {"wire.esc-1-pos": "RD", "wire.esc-1-neg": "BK", "wire.esc-1-sig": "WH"}


def test_connection_table_rows():
    rows = harness.connection_table(drone())
    assert [(r.from_, r.to, r.awg, r.net) for r in rows][:2] == [
        ("dev.power-module.pm02-v3.J1.1", "dev.esc.esc-1.P1.1", 14, "motor-power"),
        ("dev.power-module.pm02-v3.J1.2", "dev.esc.esc-1.P1.2", 14, "gnd"),
    ]
    assert len(rows) == 3


def test_bom_counts_length_connectors_contacts():
    bom = harness.harness_bom(drone())
    assert {(w["awg"], w["colour"]): w["length_m"] for w in bom["wires"]}[(14, "RD")] == 0.20
    assert [w["unmeasured"] for w in bom["wires"] if w["awg"] == 26] == [1]
    assert bom["connectors"] == {"JST-GH": 1, "XT60": 2}
    assert bom["contacts"] == 6


def test_wireviz_yaml_uses_only_documented_keys():
    doc = yaml.safe_load(harness.wireviz_yaml(drone()))
    assert set(doc) == TOP
    assert all(set(v) <= CONNECTOR_KEYS for v in doc["connectors"].values())
    assert all(set(v) <= CABLE_KEYS for v in doc["cables"].values())
    assert doc["cables"]["wire.esc-1-pos"]["gauge"] == "14 AWG"
    assert doc["connections"][0] == [
        {"dev.power-module.pm02-v3.J1": ["1"]},
        {"wire.esc-1-pos": [1]},
        {"dev.esc.esc-1.P1": ["1"]},
    ]


def test_missing_input_gives_empty_results():
    c = drone(with_wires=False)
    assert harness.connection_table(c) == []
    assert harness.harness_bom(c) == {"wires": [], "connectors": {}, "contacts": 0}
    assert harness.wireviz_yaml(c) == ""
    assert harness.ENGINE_CHECKS == ()


def _gaugeless() -> Contract:
    c = drone()
    c.topology.wires[2].__dict__["awg"] = None  # a signal wire with no declared gauge
    return c


def test_gaugeless_wire_prints_no_none_in_table_bom_or_wireviz():
    c = _gaugeless()
    assert [r.awg for r in harness.connection_table(c)][2] is None
    assert None in [w["awg"] for w in harness.harness_bom(c)["wires"]]
    assert "None" not in str(harness.wireviz_dict(c))
    assert "gauge" not in harness.wireviz_dict(c)["cables"][c.topology.wires[2].cable or c.topology.wires[2].id]
