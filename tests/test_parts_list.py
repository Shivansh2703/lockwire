"""from-parts: a pasted parts list matched to the catalog, and its draft lockfile."""

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.schema import load_contract

LIST = """# bom
12x Dynamixel XL430-W250 (6 V)
Raspberry Pi 5 x2
Pixhawk 6C flight controller 80 g
Pololu 4095 step-up
"""


def test_from_parts_matches_lists_mismatches_and_writes_a_loadable_draft(tmp_path):
    parts, out = tmp_path / "parts.txt", tmp_path / "draft.yaml"
    parts.write_text(LIST)
    result = CliRunner().invoke(app, ["from-parts", str(parts), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output.count("MATCHED") == 3
    assert result.output.count("NO MATCH") == 1
    assert "you wrote 6 V, catalog says 6.5 to 12 V" in result.output
    assert "you wrote 80 g, catalog says 59.3 g" in result.output
    devices = load_contract(out).devices
    assert sum(d.model == "mdl.dynamixel-xl430-w250" for d in devices) == 12
    assert sum(d.model == "mdl.rpi-pi5" for d in devices) == 2
    assert len(devices) == 15


def test_part_number_rule_and_plain_device_preference():
    from lockwire.parts_list import match_parts

    rows = {
        r["text"]: r
        for r in match_parts("RPi5\nrangefinder ultrasonic\nJetson Orin Nano\n")["lines"]
    }
    assert (rows["RPi5"]["id"], rows["RPi5"]["confidence"]) == (
        "rpi.pi5",
        0.95,
    )  # only the part-number rule
    assert rows["rangefinder ultrasonic"]["status"] == "MATCHED"
    assert rows["rangefinder ultrasonic"]["confidence"] < 0.95  # token fallback
    assert rows["Jetson Orin Nano"]["id"] == "nvidia.jetson-orin-nano-devkit"


def test_big_parenthesised_number_is_a_name_not_a_quantity():
    """ "(5000)" and "(2207)" are capacities/models; "(4)" and "x4" are quantities."""
    from lockwire.parts_list import _quantity

    assert _quantity("LiPo battery 3S (5000)")[0] == 1
    assert _quantity("Motor (2207)")[0] == 1
    assert _quantity("Motor (4)")[0] == 4
    assert _quantity("Motor x4")[0] == 4
