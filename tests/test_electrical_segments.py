"""Declared electrical operands are scored only inside their sourced scope."""

from lockwire_pro import api_pro as api
from lockwire.checks import run_record_checks
from lockwire.schema import Contract


SOURCE = {"witness": "human", "source": "supplier/datasheet.pdf", "locked_at": "2026-09-27"}


def _contract():
    return {
        "robot": {"name": "electrical fixture"},
        "topology": {
            "hosts": [{"id": "host"}],
            "logic_pairs": [
                {
                    "id": "gpio",
                    "driver": "mcu",
                    "receiver": "sensor",
                    "operating_supply_min": {"value": 3.0, "unit": "V"},
                    "operating_supply_max": {"value": 3.3, "unit": "V"},
                    "voh_min": {"value": 2.4, "unit": "V"},
                    "vol_max": {"value": 0.4, "unit": "V"},
                    "vih_min": {"value": 2.0, "unit": "V"},
                    "vil_max": {"value": 0.8, "unit": "V"},
                    "provenance": SOURCE,
                }
            ],
            "buses": [
                {
                    "id": "i2c",
                    "kind": "i2c",
                    "host": "host",
                    "i2c_segment": {
                        "unbuffered": True,
                        "mode": "Fast",
                        "vdd_min_v": 3.0,
                        "vdd_max_v": 3.3,
                        "pullup_min_ohm": 1500,
                        "pullup_max_ohm": 2000,
                        "capacitance_max_pf": 100,
                        "rise_time_max_ns": 300,
                        "sink_current_min_ma": 3.0,
                        "vol_max_v": 0.4,
                        "provenance": SOURCE,
                    },
                },
                {
                    "id": "can",
                    "kind": "can",
                    "host": "host",
                    "can_segment": {
                        "linear": True,
                        "nodes": ["a", "b", "c"],
                        "termination_nodes": ["a", "c"],
                        "termination_ohm": [120, 120],
                        "cable_impedance_ohm": 120,
                        "impedance_tolerance_ohm": 10,
                        "provenance": SOURCE,
                    },
                },
            ],
        },
    }


def _electrical_findings(document):
    roots = (
        "topology/logic_pairs/gpio",
        "topology/buses/i2c/i2c_segment",
        "topology/buses/can/can_segment",
    )
    return [
        row
        for row in document["findings"]
        if any(row["quantity"] == root or row["quantity"].startswith(root + "/") for root in roots)
    ]


def test_public_check_scores_declared_electrical_claims_without_can_timing_go():
    data = _contract()
    checked = api.check(Contract.model_validate(data))
    scoped = _electrical_findings(checked)
    assert [(row["quantity"], row["severity"]) for row in scoped] == [
        ("topology/buses/can/can_segment/timing", "not_scored")
    ]
    assert "CAN timing not evaluated" in scoped[0]["observed"]
    records = run_record_checks(Contract.model_validate(data)).scored_records
    quantities = {record.quantity for record in records}
    assert quantities >= {
        "topology/logic_pairs/gpio/high_margin",
        "topology/logic_pairs/gpio/low_margin",
        "topology/buses/i2c/i2c_segment/rise_time",
        "topology/buses/i2c/i2c_segment/capacitance",
        "topology/buses/i2c/i2c_segment/pullup_sink",
        "topology/buses/can/can_segment/termination",
    }
    electrical = [
        record
        for record in records
        if record.quantity.startswith(
            (
                "topology/logic_pairs/",
                "topology/buses/i2c/i2c_segment/",
                "topology/buses/can/can_segment/",
            )
        )
    ]
    assert all(
        record.provenance is not None and record.provenance.source == SOURCE["source"]
        for record in electrical
    )


def test_public_check_rejects_wrong_directed_margin_mode_ceiling_sink_and_can_end():
    data = _contract()
    pair = data["topology"]["logic_pairs"][0]
    pair["vih_min"]["value"] = 2.5
    i2c = data["topology"]["buses"][0]["i2c_segment"]
    i2c["rise_time_max_ns"] = 1000  # cannot relax Fast-mode 300 ns ceiling
    i2c["pullup_max_ohm"] = 4000
    i2c["pullup_min_ohm"] = 500
    i2c["capacitance_max_pf"] = 450
    can = data["topology"]["buses"][1]["can_segment"]
    can["termination_nodes"] = ["a", "b"]
    scoped = _electrical_findings(api.check(Contract.model_validate(data)))
    failures = {row["quantity"] for row in scoped if row["severity"] == "violation"}
    assert failures >= {
        "topology/logic_pairs/gpio/high_margin",
        "topology/buses/i2c/i2c_segment/rise_time",
        "topology/buses/i2c/i2c_segment/capacitance",
        "topology/buses/i2c/i2c_segment/pullup_sink",
        "topology/buses/can/can_segment/termination",
    }


def test_public_check_rejects_negative_low_logic_margin():
    data = _contract()
    data["topology"]["logic_pairs"][0]["vil_max"]["value"] = 0.3
    scoped = _electrical_findings(api.check(Contract.model_validate(data)))
    assert any(
        row["quantity"] == "topology/logic_pairs/gpio/low_margin" and row["severity"] == "violation"
        for row in scoped
    )


def test_public_check_keeps_unsourced_and_buffered_claims_unscored():
    data = _contract()
    data["topology"]["logic_pairs"][0]["provenance"] = None
    data["topology"]["buses"][0]["i2c_segment"]["unbuffered"] = False
    data["topology"]["buses"][1]["can_segment"]["provenance"] = {**SOURCE, "source": ""}
    scoped = _electrical_findings(api.check(Contract.model_validate(data)))
    assert {row["quantity"] for row in scoped if row["severity"] == "not_scored"} >= {
        "topology/logic_pairs/gpio",
        "topology/buses/i2c/i2c_segment",
        "topology/buses/can/can_segment",
    }
    assert all(row["severity"] != "violation" for row in scoped)
