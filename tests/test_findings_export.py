"""`check --format csv|tsv` and the app's `findings-export` route."""

import csv
import hashlib
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire.serve import create_app

X500 = Path(__file__).parent / "fixtures" / "x500" / "contract.yaml"
COLUMNS = ["severity", "about", "observed", "expected", "units", "source", "action"]
# sha256 of `check contract.yaml --json -` run from the x500 folder, taken BEFORE this change.
# Re-pinned 2026-11-16: six changes each added or reworded output on purpose (loop wording,
# JSON loop output, CLI words, sweep load, mass description, what is missing); verdict and findings unchanged.
# Re-pinned 2026-11-17: the stance check reworded forces.stance.reason (feet by link name); no other field moved.
# Re-pinned 2026-11-17 (R3-CORE): counts gained "advice" (findings listed but never counted); no other byte moved.
# Re-pinned 2026-11-18 (ALL-INTEGRATE): the data branch's x500 rewording (revision) and the engine's restored 0.5.1 observed text together.
# Re-pinned 2026-11-18 (ALL-FINAL): stored text back to the plain words ("no model file in provenance").
JSON_PINNED = "31347afa17efa7d9193502f685214a357405061e35bee8f91888b3ad9e3b1a22"


def _table(delimiter, fmt):
    result = CliRunner().invoke(app, ["check", str(X500), "--format", fmt])
    assert result.exit_code == 1, result.output
    return list(csv.reader(io.StringIO(result.stdout), delimiter=delimiter))


def test_csv_has_header_verdict_row_and_one_row_per_finding():
    table = _table(",", "csv")
    assert table[0] == COLUMNS
    assert table[1][:2] == ["VERDICT", "NO-GO"]
    doc = json.loads(CliRunner().invoke(app, ["check", str(X500), "--json", "-"]).stdout)
    assert len(table) - 2 == len(doc["findings"])


def test_csv_row_is_in_plain_words_with_units_and_fix():
    row = _table(",", "csv")[2]
    assert row[:2] == ["violation", "PM02 5 V power budget"]
    assert row[4] == "W"
    assert row[6] == "Use less power on PM02 5 V, or raise its limit"


def test_tsv_uses_tabs_not_commas():
    result = CliRunner().invoke(app, ["check", str(X500), "--format", "tsv"])
    assert result.stdout.splitlines()[0] == "\t".join(COLUMNS)
    assert _table("\t", "tsv")[1][1] == "NO-GO"


def test_json_output_is_byte_identical_to_before(monkeypatch):
    monkeypatch.chdir(X500.parent)
    out = CliRunner().invoke(app, ["check", "contract.yaml", "--json", "-"]).stdout
    assert hashlib.sha256(out.encode()).hexdigest() == JSON_PINNED


def test_route_returns_the_same_rows_as_the_csv():
    client = TestClient(create_app("t"), base_url="http://127.0.0.1")
    body = {"command": "findings-export", "args": {"contract": str(X500), "format": "csv"}}
    payload = client.post("/v1/run", headers={"X-Lockwire-Token": "t"}, json=body).json()["payload"]
    table = _table(",", "csv")
    got = [[r[c] for c in COLUMNS] for r in payload["rows"]]
    assert got == table[2:]
    assert list(csv.reader(io.StringIO(payload["text"])))[2:] == table[2:]
    assert payload["rows"][0]["plain"].startswith("PM02 5 V power budget: observed 35.20 W")


def test_csv_human_columns_use_the_text_words_not_raw_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(app, ["start", "--example", "arm-power", "--no-check"])
    out = CliRunner().invoke(app, ["check", "--all", "--format", "csv"]).stdout
    raw = ("wire ampacity", "free_air", "Discharge temp c", "a device thermal ambient", "GO_WITH")
    human = [c for row in list(csv.reader(io.StringIO(out)))[1:] for c in row[1:4]]
    assert [w for w in raw if any(w in c for c in human)] == [], out
    assert "28 AWG in free air" in out and "Pack temperature window" in out
