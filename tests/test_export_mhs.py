"""`lockwire export --format mhs` and the refused `lockwire import`.

`tests/fixtures/contract_clean.yaml` (the `clean_contract` fixture, conftest.py) already carries
one device, `dev.imu`, with all three kinds of band this test must distinguish: a datasheet
`hard_limits.accel_z` on `mdl.imu.bmi088`, a fitted `nominal-op` envelope signal (`accel_x`, in
that envelope's `fitted_bands`), and an inferred `bench-idle` envelope signal (`accel_z`, not in
any `fitted_bands`) stamped `witness: human` — the fixture's own note calls it "hand-authored,
not fitted to a recording". Reused rather than re-authored.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

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
        "refused until Anthropic publishes the standard; import is where lock-in happens."
        in flat
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
