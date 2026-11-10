"""record-servos edges: a bad rate, a bus that drops mid-recording, the Dynamixel default baud."""

import pytest
from typer.testing import CliRunner

from lockwire import servo_bus
from lockwire.cli import app


def _live(reads):
    def read(ser, info):
        if not reads:
            raise OSError("port gone")
        return reads.pop()

    return read


def test_rate_zero_is_refused_and_a_dropped_bus_keeps_its_samples(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="--rate"):
        servo_bus.record_servos(None, [1], tmp_path / "r", rate_hz=0)
    monkeypatch.setattr(servo_bus, "_port_open", lambda *a: pytest.fail("port opened"))
    argv = ["record-servos", "--port", "p", "--rate", "0", "--out", str(tmp_path)]
    assert CliRunner().invoke(app, argv).exit_code == 2  # refused before the port opens
    settings = lambda ser, i, kind: ({"id": i, "kind": kind}, {"position_deg": "deg"})  # noqa: E731
    monkeypatch.setattr(servo_bus, "_fee_settings", settings)
    monkeypatch.setattr(servo_bus, "_fee_live", _live([{"position_deg": 1.0}] * 3))
    with pytest.raises(OSError, match="saved 3 sample"):
        servo_bus.record_servos(None, [1], tmp_path / "r", rate_hz=99, sleep=lambda s: None)
    assert len((tmp_path / "r" / "samples.jsonl").read_text().splitlines()) == 3
    assert "partial: true" in (tmp_path / "r" / "manifest.yaml").read_text()


def test_dynamixel_defaults_to_its_factory_57600_baud(monkeypatch):
    seen = []
    monkeypatch.setattr(servo_bus, "_port_open", lambda port, baud: seen.append(baud))
    monkeypatch.setattr(servo_bus, "record_servos", lambda *a, **k: _live([])(a, k))
    for kind in ("dxl", "sts"):
        CliRunner().invoke(app, ["record-servos", "--port", "p", "--kind", kind, "--out", "o"])
    assert seen == [57_600, 1_000_000]


def test_bad_ids_are_refused_before_the_port_opens_and_a_silent_bus_leaves_no_folder(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(servo_bus, "_port_open", lambda *a: pytest.fail("port opened"))
    for ids, said in (("6-1", "low ID first"), ("1,254", "broadcast"), ("a", "not a servo ID")):
        argv = ["record-servos", "--port", "p", "--ids", ids, "--out", str(tmp_path / "o")]
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 2 and said in result.output
    monkeypatch.setattr(servo_bus, "_fee_settings", lambda *a: None)
    with pytest.raises(RuntimeError, match="no servo answered"):
        servo_bus.record_servos(None, [1], tmp_path / "o")
    assert not (tmp_path / "o").exists()
