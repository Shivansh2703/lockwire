from lockwire import api
from pathlib import Path

from lockwire.schema import Material, load_contract

FIXTURE = Path("tests/fixtures/actuator_review_e2e/contract.yaml")


def test_mass_rows_say_their_kind_and_a_links_material():
    contract = load_contract(FIXTURE)
    contract.model.links[1].mass_kind = "measured"
    contract.model.links[0].material = Material(name="PLA", density_kg_m3=1240, source="handbook")
    rows = [
        {"id": f"model/links/{link.name}/mass", "value": 1.0} for link in contract.model.links
    ] + [{"id": "devices/nope/physical/mass", "value": 1.0}]
    api._tag_mass_rows(contract, rows)
    assert [r.get("mass_kind") for r in rows] == ["estimated", "measured", None]
    assert rows[0]["material"] == "PLA" and "material" not in rows[1]
