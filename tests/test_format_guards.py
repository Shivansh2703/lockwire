"""A pin-to-pin signal wire has no rail; every wire.rail reader must skip it quietly."""

from pathlib import Path

from ruamel.yaml import YAML
from typer.testing import CliRunner

from lockwire.checks import check_ampacity, check_reference_integrity
from lockwire.cli import app
from lockwire.schema import load_contract

BENCH = Path(__file__).parent.parent / "src" / "lockwire" / "examples" / "servo-bench.yaml"
SIGNAL = {
    "id": "wire.pwm-signal",
    "awg": 26,  # not in the ampacity table: an unguarded read would score it
    "installation": "free_air",
    "owner": "you",
    "from": {"device": "dev.pi5", "pin": 12},
    "to": {"device": "dev.servo1", "pin": "signal"},
}


def _with_signal_wire(tmp_path: Path) -> Path:
    yaml = YAML()
    doc = yaml.load(BENCH.read_text())
    doc["topology"]["wires"].append(SIGNAL)
    out = tmp_path / "lockwire.yaml"
    yaml.dump(doc, out)
    return out


def test_check_runs_clean_past_a_signal_wire(tmp_path):
    path = _with_signal_wire(tmp_path)
    result = CliRunner().invoke(app, ["check", str(path), "--json", str(tmp_path / "out.json")])
    report = (tmp_path / "out.json").read_text()
    assert result.exit_code != 2 and "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert SIGNAL["id"] not in report and SIGNAL["id"] not in result.output
    assert "'None'" not in report and "rail None" not in report


def test_reference_integrity_skips_a_wire_with_no_rail(tmp_path):
    contract = load_contract(_with_signal_wire(tmp_path))
    assert [f for f in check_reference_integrity(contract) if SIGNAL["id"] in f.quantity] == []


def test_ampacity_skips_a_wire_with_no_rail(tmp_path):
    contract = load_contract(_with_signal_wire(tmp_path))
    assert [f for f in check_ampacity(contract) if SIGNAL["id"] in f.quantity] == []
