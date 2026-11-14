"""`lockwire export --format mhs` and the refused `lockwire import`.

`tests/fixtures/contract_clean.yaml` (the `clean_contract` fixture, conftest.py) already carries
one device, `dev.imu`, with all three kinds of band this test must distinguish: a datasheet
`hard_limits.accel_z` on `mdl.imu.bmi088`, a fitted `nominal-op` envelope signal (`accel_x`, in
that envelope's `fitted_bands`), and an inferred `bench-idle` envelope signal (`accel_z`, not in
any `fitted_bands`) stamped `witness: human` — the fixture's own note calls it "hand-authored,
not fitted to a recording". Reused rather than re-authored.
"""

import csv
import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire import api
from lockwire.catalog import Catalog
from lockwire.cli import app
from lockwire.export_mhs import export_mhs
from lockwire.schema import Contract

from conftest import FIXTURES, plain, strip_ansi

runner = CliRunner()


def _by_channel(devices: list[dict], device_id: str, channel: str) -> dict:
    device = next(d for d in devices if d["device_id"] == device_id)
    return next(limit for limit in device["safety_limits"] if limit["channel"] == channel)


def test_export_mhs_renders_every_declared_device(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    rendered_ids = {d["device_id"] for d in result["devices"]}
    assert rendered_ids == {device.id for device in clean_contract.devices}


def test_export_mhs_min_max_alias_band(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    entry = _by_channel(result["devices"], "dev.imu", "accel_z")
    model = clean_contract.device_model("mdl.imu.bmi088")
    band = model.hard_limits["accel_z"]
    assert entry["min"] == band.lo
    assert entry["max"] == band.hi
    assert entry["kind"] == "datasheet"


def test_export_mhs_unit_literal_carried(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    entry = _by_channel(result["devices"], "dev.imu", "accel_z")
    model = clean_contract.device_model("mdl.imu.bmi088")
    assert entry["unit"] == model.hard_limits["accel_z"].unit == "m/s^2"


def test_export_mhs_witness_and_simulated_carried(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    # bench-idle accel_z: envelope provenance witness: human (fixture note: hand-authored,
    # no bag) -> the proxy for "not a real measurement".
    inferred = _by_channel(result["devices"], "dev.imu", "bench-idle/accel_z")
    assert inferred["witness"] == "human"
    assert inferred["simulated"] is True
    # nominal-op accel_x: envelope provenance witness: live, built from a real bag.
    fitted = _by_channel(result["devices"], "dev.imu", "nominal-op/accel_x")
    assert fitted["witness"] == "live"
    assert fitted["simulated"] is False


def test_export_mhs_datasheet_vs_fitted_distinguished(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    datasheet = _by_channel(result["devices"], "dev.imu", "accel_z")
    fitted = _by_channel(result["devices"], "dev.imu", "nominal-op/accel_x")
    inferred = _by_channel(result["devices"], "dev.imu", "bench-idle/accel_z")
    assert datasheet["kind"] == "datasheet"
    assert fitted["kind"] == "fitted"
    assert inferred["kind"] == "inferred"
    # the two accel_z entries (datasheet vs bench-idle-inferred) stay two distinct rows,
    # never collapsed into one field even though they share a bare channel name.
    also_accel_z_inferred = _by_channel(result["devices"], "dev.imu", "bench-idle/accel_z")
    assert (datasheet["min"], datasheet["max"]) != (
        also_accel_z_inferred["min"],
        also_accel_z_inferred["max"],
    )


def test_export_mhs_loss_report_names_rail_budgets(clean_contract: Contract) -> None:
    result = export_mhs(clean_contract)
    report = " ".join(result["loss_report"])
    assert "rail.5v" in report  # named, not a generic "budgets are lost" line
    assert "frame" in report
    assert "provenance" in report or "witness" in report


def test_import_mhs_refused_quotes_d108(tmp_path: Path) -> None:
    """A refusal is REFUSED, exit 2 — not a hand-written exit 1.
    Asserts the code AND the text so a fix that only patches one drifts back visibly."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["import", str(manifest)])
    assert result.exit_code == 2, result.output
    flat = plain(result.output)
    assert "REFUSED" in flat
    assert (
        "The inbound direction — importing a third-party manifest into a contract — is "
        "refused until a standard manifest format exists; import is where lock-in happens." in flat
    )


def test_export_mhs_library_and_cli_share_one_path(clean_contract: Contract) -> None:
    """The CLI's `export --format mhs` and the library `export_mhs()` render the same document.

    Runs both paths against the same contract — the library call directly, and the CLI
    through its runner, writing to stdout — and asserts the two documents are equal AND
    that the CLI's own devices are exactly the contract's declared devices. The equality
    check alone would stay green under a bug in the shared renderer (`render_device`,
    say), since both sides call the identical mutated function and land on the identical
    wrong answer — that a parity check can't distinguish is not hollowness, it's what
    "one path" means. The ground-truth check ties the CLI's output back to the contract
    independently of either call, so it goes red when the shared renderer is wrong, not
    only when the two entry points diverge from each other. A test that only greps
    `export_cmd`'s source text for a substring would catch neither.
    """
    library_doc = export_mhs(clean_contract)

    result = runner.invoke(
        app, ["export", "--format", "mhs", str(FIXTURES / "contract_clean.yaml")]
    )
    assert result.exit_code == 0
    cli_doc = json.loads(strip_ansi(result.output))

    assert cli_doc == library_doc
    assert {d["device_id"] for d in cli_doc["devices"]} == {
        device.id for device in clean_contract.devices
    }


def _bom_contract(tmp_path: Path, mass: str = "{value: 0.1, unit: kg}") -> Path:
    servo = """
- id: dev.{n}
  model: mdl.servo.x
  physical: {mass: {mass}}
  owner: you
  provenance: {witness: w0, source: "https://x.test/ds.pdf", locked_at: 2026-10-15}"""
    servo = servo.replace("{mass}", mass)  # {mass} is a placeholder, braces are YAML
    text = f"""robot: {{name: Bot}}
families: [{{id: fam.servo, name: Servo}}]
device_models:
- {{id: mdl.servo.x, family: fam.servo, input_voltage_range: {{min: {{value: 6, unit: V}}, max: {{value: 12, unit: V}}}}}}
devices:{servo.replace("{n}", "a")}{servo.replace("{n}", "b")}
topology: {{battery: {{capacity: {{value: 20, unit: Wh}}, mass: {{value: 0.3, unit: kg}}}}}}
"""
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return path


def _bom(args: list[str]):
    return runner.invoke(app, ["export", "--format", "bom", *args])


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_bom_groups_identical_parts_with_totals(tmp_path: Path) -> None:
    rows = _csv_rows(_bom([str(_bom_contract(tmp_path))]).stdout)
    assert [r["quantity"] for r in rows] == ["2", "1"]
    assert float(rows[0]["total_mass_kg"]) == pytest.approx(0.2)
    assert rows[0]["voltage"] == "6-12 V" and rows[0]["datasheet"] == "https://x.test/ds.pdf"
    assert "_unverified" not in _bom([str(_bom_contract(tmp_path))]).stdout


def test_bom_unknown_mass_is_an_empty_cell(tmp_path: Path) -> None:
    rows = _csv_rows(_bom([str(_bom_contract(tmp_path, mass="null"))]).stdout)
    assert rows[0]["unit_mass_kg"] == "" and rows[0]["total_mass_kg"] == ""


def test_bom_out_refuses_existing_file_without_force(tmp_path: Path) -> None:
    path, out = _bom_contract(tmp_path), tmp_path / "bom.csv"
    out.write_text("keep")
    assert _bom([str(path), "--out", str(out)]).exit_code == 2 and out.read_text() == "keep"
    assert _bom([str(path), "--out", str(out), "--force"]).exit_code == 0
    assert out.read_text().startswith("item,quantity")


def test_bom_mass_note_only_when_both_masses_exist(tmp_path: Path) -> None:
    c = api.load(_bom_contract(tmp_path))
    assert api.bom_mass_note(c, api.bom_rows(c)) is None  # no robot model
    c.model = api.load(Path(api.__file__).parent / "examples/rover/contract.yaml").model
    assert "kg is not in the parts list" in (api.bom_mass_note(c, api.bom_rows(c)) or "")


def _row(tp, mp, ids=("a.xl430-w250",), fam="fam.servo", kind="datasheet", mass="null", maker=None):
    n = {"value": 57.2, "unit": "g", "kind": kind, "source": "https://x.test/d.pdf"}
    parts = [
        {"id": i, "family": fam, "name": "N", "maker": maker, "numbers": {"mass": n}} for i in ids
    ]
    mp.setattr(api, "load_catalog", lambda: Catalog.model_validate({"parts": parts}))
    text = _bom_contract(tp, mass).read_text().replace("mdl.servo.x", "mdl.servo.XL430-W250")
    (tp / "m.yaml").write_text(text)
    return api.bom_rows(api.load(tp / "m.yaml"))[0]


def test_bom_catalog_mass_fill(tmp_path, monkeypatch) -> None:
    r = _row(tmp_path, monkeypatch)
    assert r["unit_mass_kg"] == "0.0572" and r["source"].endswith("; catalog: a.xl430-w250 (cited)")
    assert _row(tmp_path, monkeypatch, kind="assumed")["_unverified"] == "1"
    assert _row(tmp_path, monkeypatch, mass="{value: 0.1, unit: kg}")["unit_mass_kg"] == "0.1"
    assert _row(tmp_path, monkeypatch, ids=("a.xl430-w250", "b.xl430-w250"))["unit_mass_kg"] == ""
    assert _row(tmp_path, monkeypatch, fam="fam.other")["unit_mass_kg"] == ""


def test_bom_maker_comes_from_the_catalog_part(tmp_path, monkeypatch) -> None:
    assert _row(tmp_path, monkeypatch, maker="Robotis")["maker"] == "Robotis"
    assert _row(tmp_path, monkeypatch)["maker"] == ""  # catalog part without a maker: empty cell


def test_bom_unverified_note_uses_the_flag_not_the_source_text() -> None:
    c = api.load(Path(api.__file__).parent / "examples/rover/contract.yaml")
    row = {"total_mass_kg": "0.1", "source": "x (unverified)", "_unverified": ""}
    assert "unverified" not in (api.bom_mass_note(c, [row]) or "")
    assert "unverified catalog" in (api.bom_mass_note(c, [{**row, "_unverified": "1"}]) or "")


def test_bom_notes_flag_uncited_and_duplicate_rows(tmp_path: Path) -> None:
    bench = Path(api.__file__).parent / "examples/servo-bench.yaml"
    res = _bom([str(bench)])
    rows = _csv_rows(res.stdout)
    assert list(rows[0])[-1] == "notes" and [r["notes"] for r in rows] == ["no cited mass"] * 2
    assert res.stderr.count("no cited mass") == 2
    cited = _csv_rows(_bom([str(_bom_contract(tmp_path))]).stdout)[0]
    assert cited["notes"] == ""
    first, _, rest = _bom_contract(tmp_path).read_text().partition("physical: {mass: {value: 0.1")
    path = tmp_path / "dup.yaml"
    path.write_text(
        first + "physical: {mass: {value: 0.1" + rest.replace("value: 0.1", "value: 0.2", 1)
    )
    dup = _csv_rows(_bom([str(path)]).stdout)
    assert [r["quantity"] for r in dup[:2]] == ["1", "1"]
    assert all("listed twice: merge into one row with qty 2" in r["notes"] for r in dup[:2])


def test_bom_battery_row_is_not_blamed_and_unverified_note_follows_the_flag(
    tmp_path, monkeypatch
) -> None:
    rows = api.bom_rows(api.load(_bom_contract(tmp_path)))
    assert rows[-1]["part"] == "battery" and rows[-1]["notes"] == []
    assert (
        "mass from an unverified catalog entry"
        in _row(tmp_path, monkeypatch, kind="assumed")["notes"]
    )
    assert "mass from an unverified catalog entry" not in _row(tmp_path, monkeypatch)["notes"]


def test_bom_drops_the_pack_row_only_on_a_declared_link() -> None:
    base = api.load(Path(api.__file__).parent / "examples/arm-power/contract.yaml")

    def build(link: bool):
        c = base.model_copy(deep=True)
        c.topology.rails[0].source_device = "dev.battery" if link else None
        rows = api.bom_rows(c)
        return c, rows, [r["part"] for r in rows], api.bom_mass_note(c, rows) or ""

    # same mass, no link: a coincidence never deletes a part; the note says how to link them
    _, _, parts, note = build(False)
    assert parts.count("battery") == 1 and "mdl.pack" in parts
    assert "battery and dev.battery declare the same mass" in note
    # the declared link: the pack feeds rail.battery
    _, rows, parts, note = build(True)
    assert parts.count("battery") == 0 and "same mass" not in note
    assert round(sum(float(r["total_mass_kg"]) for r in rows if r["total_mass_kg"]), 3) == 0.179
    # battery is not a device: nothing to merge with
    c = base.model_copy(deep=True)
    c.topology.rails[0].source_device = "dev.battery"
    c.devices = [d for d in c.devices if d.model != "mdl.pack"]
    rows = api.bom_rows(c)
    assert [r["part"] for r in rows].count("battery") == 1
    assert "same mass" not in (api.bom_mass_note(c, rows) or "")
