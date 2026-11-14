"""from-parts: a pasted parts list matched to the catalog, and its draft lockfile."""

from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.parts_list import match_parts, write_draft
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


def test_part_number_the_candidate_lacks_blocks_a_word_overlap_match():
    rows = {r["text"]: r for r in match_parts("Pololu 4095 step-up\nPololu step-up")["lines"]}
    assert rows["Pololu 4095 step-up"]["status"] == "NO MATCH"
    assert rows["Pololu step-up"]["id"] == "pololu.s13v10f5"  # same words, no number: fallback


def test_quantity_and_glued_unit_lines_still_match():
    text = "12x Dynamixel XL430-W250 (6 V)\nPixhawk 6C flight controller 80g\nPixhawk 6C flight controller 80 g"
    assert all(r["status"] == "MATCHED" for r in match_parts(text)["lines"])


def _ids(*lines):
    return [(r["status"], r["id"]) for r in match_parts("\n".join(lines))["lines"]]


def test_key_shared_by_several_parts_is_settled_by_the_kind_word_never_by_order():
    assert _ids("XT60 connector 35A") == [("MATCHED", "conn.xt60")]


def test_shared_key_with_no_single_kind_word_hit_is_no_match():
    assert _ids("3S 2200 XT60 pack") == [("NO MATCH", None)]  # six XT60 packs share "pack"


def test_bare_shared_key_goes_to_the_part_whose_id_it_is():
    assert _ids("XT60") == [("MATCHED", "conn.xt60")]  # the packs carry it only in their names
    assert _ids("U2D2") == [("MATCHED", "robotis.u2d2")]
    assert _ids("U2D2 servo adapter") == [("MATCHED", "robotis.u2d2")]  # family word split on "-"
    assert _ids("U2D2 power hub") == [("MATCHED", "robotis.u2d2-power-hub")]


def test_a_tie_goes_to_the_exact_name_or_the_strictly_best_overlap():
    assert _ids("Gens ace 2200mAh 3S 60C Adventure (XT60)") == [
        ("MATCHED", "pack.gensace.3s-2200-60c-adventure")
    ]
    assert _ids("DIN 934 hex nut M2") == [("MATCHED", "hw.din934.m2")]
    assert _ids("DIN 934 hex nut M6") == [("MATCHED", "hw.din934.m6")]


def test_a_tie_that_stays_open_names_its_candidates_and_fills_nothing(tmp_path):
    doc = match_parts("3S 2200 XT60 pack")
    row = doc["lines"][0]
    assert row["status"] == "NO MATCH" and row["note"].startswith("could be: pack.gensace.3s-2200-")
    assert "35c-bashing" in row["note"] and "60c-adventure" in row["note"]
    write_draft(doc, tmp_path / "d.yaml")
    assert load_contract(tmp_path / "d.yaml").devices == []


def test_a_maker_the_line_names_beats_the_chip_that_owns_the_id():
    assert _ids("TB6612FNG driver") == [("MATCHED", "toshiba.tb6612fng")]
    assert _ids("Pololu TB6612FNG driver") == [("MATCHED", "pololu.tb6612fng-carrier")]


def test_a_tie_on_a_later_catalog_part_goes_to_its_exact_name_not_the_first_part():
    (row,) = match_parts("Gens ace 2200mAh 3S 35C Bashing (XT60)")["lines"]
    assert (row["status"], row["id"]) == ("MATCHED", "pack.gensace.3s-2200-35c-bashing")
