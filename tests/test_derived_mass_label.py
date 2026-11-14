"""A mass derived from shapes is not labelled "declared"; the note rides along."""

from lockwire import api, factdiff
from lockwire.report_html import render_html
from lockwire.schema import load_contract
from tests.test_derive_geometry import _arm_text, _write

NOTE = "derived from its shapes and material"


def _rows(path):
    masses = api.check_document(load_contract(path), [])  # rows come from the forces lane
    return {r["id"].split("/")[2]: r for r in masses["forces"]["masses"] if r.get("id")}


def test_check_json_labels_a_derived_mass_and_leaves_a_declared_row_as_it_was(
    tmp_path, monkeypatch
):
    rows = _rows(_write(tmp_path, monkeypatch))
    assert rows["arm"]["kind"] == "derived"
    assert NOTE in rows["arm"]["mass_note"] and "alloy sheet" in rows["arm"]["mass_note"]
    assert rows["fore"] == {  # captured on the base commit
        "value": 1.5,
        "unit": "kg",
        "kind": "declared",
        "id": "model/links/fore/mass",
        "mass_kind": "estimated",
    }


def test_the_saved_report_and_the_fact_diff_show_the_marker(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch)
    assert "(derived from its shapes)" in render_html(load_contract(path))
    declared = tmp_path / "declared.yaml"
    declared.write_text(
        _arm_text(tmp_path, monkeypatch, arm_extra={"mass": {"value": 9.0, "unit": "kg"}})
    )
    assert "(derived from its shapes)" not in render_html(load_contract(declared))
    diff = factdiff.fact_diff(load_contract(declared), load_contract(path))
    assert any(NOTE in line for line in factdiff.render_lines(diff))
    assert _rows(declared)["arm"]["kind"] == "declared"
