"""`check --format csv|tsv` and the app's `findings-export` route (FIND-EXP)."""

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
JSON_PINNED = "54e5352158d7823c455842f22138908d0936c594ddfbec4ae4e51a8184370107"


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
