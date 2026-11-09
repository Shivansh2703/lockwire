"""``lockwire sample --local`` (milestone 1): the robot's own
compute, sampled with no hardware, as a normal lockwire recording."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lockwire.cli import app
from lockwire_pro.recording import load_recording
from lockwire_pro.sample_local import discover_readers, sample_local
from lockwire.schema import (
    Band,
    Contract,
    ContextEnvelope,
    DeviceFamily,
    DeviceInstance,
    DeviceModel,
    Provenance,
    RobotInfo,
    SignalEnvelope,
    load_contract,
)


def _write_fake_tree(root: Path) -> tuple[Path, Path]:
    """A fake /sys + /proc tree: one thermal zone, an INA3221-shaped hwmon,
    loadavg, and one network interface with a nonzero rx_dropped counter."""
    sysfs_root = root / "sys"
    proc_root = root / "proc"

    thermal = sysfs_root / "class" / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "type").write_text("cpu-thermal\n")
    (thermal / "temp").write_text("45123\n")  # -> 45.123 degC

    cooling = sysfs_root / "class" / "thermal" / "cooling_device0"
    cooling.mkdir(parents=True)
    (cooling / "cur_state").write_text("0\n")

    hwmon = sysfs_root / "class" / "hwmon" / "hwmon0"
    hwmon.mkdir(parents=True)
    (hwmon / "name").write_text("ina3221\n")
    (hwmon / "in1_input").write_text("5000\n")  # -> 5.0 V
    (hwmon / "curr1_input").write_text("1500\n")  # -> 1.5 A
    (hwmon / "power1_input").write_text("7500000\n")  # -> 7.5 W (microWatt)
    (hwmon / "energy1_input").write_text("2500000\n")  # -> 2.5 J (microJoule)

    proc_root.mkdir(parents=True)
    (proc_root / "loadavg").write_text("0.50 0.40 0.30 1/200 12345\n")
    (proc_root / "net").mkdir()
    (proc_root / "net" / "dev").write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    "
        "packets errs drop fifo colls carrier compressed\n"
        "  eth0: 100 1 0 3 0 0 0 0 200 2 0 0 0 0 0 0\n"
    )

    return sysfs_root, proc_root


def test_discover_readers_finds_fake_sources(tmp_path):
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    readers, absent, _unreadable = discover_readers(sysfs_root, proc_root)
    signals = {r.signal for r in readers}
    assert "thermal/cpu-thermal" in signals
    assert "hwmon/ina3221/in1" in signals
    assert "hwmon/ina3221/curr1" in signals
    assert "loadavg/1m" in signals
    assert "net/eth0/rx_dropped" in signals
    assert "cooling/cooling_device0/cur_state" in signals
    # This fake tree never puts vcgencmd on PATH.
    assert "vcgencmd" in absent


def test_sample_local_writes_a_loadable_recording(tmp_path):
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    sample_local(seconds=0.2, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)

    recording = load_recording(out)
    assert "dev.compute" in recording.devices
    stream = recording.devices["dev.compute"]
    assert stream.recv_times == sorted(stream.recv_times)  # nondecreasing recv-time

    # Milli-unit conversion: 45123 milli-degC -> 45.123 degC.
    assert stream.signals["thermal/cpu-thermal"][0] == pytest.approx(45.123)
    assert stream.signals["hwmon/ina3221/in1"][0] == pytest.approx(5.0)
    assert stream.signals["hwmon/ina3221/curr1"][0] == pytest.approx(1.5)
    assert stream.signals["net/eth0/rx_dropped"][0] == pytest.approx(3.0)

    assert recording.units["dev.compute"]["thermal/cpu-thermal"] == "degC"
    assert recording.units["dev.compute"]["hwmon/ina3221/in1"] == "V"
    assert recording.units["dev.compute"]["hwmon/ina3221/curr1"] == "A"


def test_hwmon_power_and_energy_are_micro_units(tmp_path):
    """Kernel ABI doc: power*_input is microWatt, energy*_input is microJoule
    (/1e6), unlike in/curr/temp which are milli-units (/1e3). Review bounce:
    power1_input=7500000 was previously reported as 7500.0 W instead of 7.5 W."""
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)

    recording = load_recording(out)
    stream = recording.devices["dev.compute"]
    assert stream.signals["hwmon/ina3221/power1"][0] == pytest.approx(7.5)
    assert stream.signals["hwmon/ina3221/energy1"][0] == pytest.approx(2.5)
    assert recording.units["dev.compute"]["hwmon/ina3221/power1"] == "W"
    assert recording.units["dev.compute"]["hwmon/ina3221/energy1"] == "J"


def test_hwmon_power_divisor_mutation_bite(tmp_path):
    """Mutation bite: set power's divisor back to 1e3 (mirroring the bug the
    reviewer found) and this test must fail."""
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)
    recording = load_recording(out)
    value = recording.devices["dev.compute"].signals["hwmon/ina3221/power1"][0]
    # Raw sysfs value is 7500000 microWatt; a /1e3 bug would report 7500.0 W.
    assert value == pytest.approx(7.5)
    assert value != pytest.approx(7500.0)


def test_unreadable_hwmon_file_is_listed(tmp_path):
    """A hwmon input file that exists but can't be read (e.g. mode 000) is
    listed under manifest["generator"]["unreadable"], not silently dropped."""
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    unreadable_file = sysfs_root / "class" / "hwmon" / "hwmon0" / "in2_input"
    unreadable_file.write_text("1234\n")
    unreadable_file.chmod(0o000)
    out = tmp_path / "rec"
    try:
        sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)
    finally:
        unreadable_file.chmod(0o644)  # so tmp_path cleanup can remove it

    manifest_text = (out / "manifest.yaml").read_text()
    assert "unreadable" in manifest_text
    assert "hwmon/ina3221/in2" in manifest_text
    recording = load_recording(out)
    assert "hwmon/ina3221/in2" not in recording.devices["dev.compute"].signals


def test_sample_local_one_real_samples_jsonl_line(tmp_path):
    """One real line, verbatim, for the report back."""
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)
    first_line = (out / "samples.jsonl").read_text().splitlines()[0]
    assert '"dev": "dev.compute"' in first_line
    assert '"thermal/cpu-thermal": 45.123' in first_line


def _tiny_compute_contract(context: str) -> Contract:
    return Contract(
        robot=RobotInfo(name="hb-test"),
        families=[DeviceFamily(id="fam.compute", name="Main compute")],
        device_models=[DeviceModel(id="mdl.compute", family="fam.compute")],
        devices=[
            DeviceInstance(
                id="dev.compute",
                model="mdl.compute",
                owner="hb-test",
                envelopes=[
                    ContextEnvelope(
                        context=context,
                        signals={
                            "thermal/cpu-thermal": SignalEnvelope(
                                band=Band(lo=0.0, hi=90.0, unit="degC"),
                            ),
                        },
                        provenance=Provenance(
                            witness="live", source="synthetic-hb", locked_at="2026-09-07"
                        ),
                    )
                ],
            )
        ],
    )


def test_preflight_scores_the_recorded_thermal_signal(tmp_path):
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    # under 10 samples or 1 s is NOT SCORED, so record past the floor
    sample_local(seconds=1.2, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)

    recording = load_recording(out)
    contract = _tiny_compute_contract(recording.context)
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_dump_contract(contract))

    result = CliRunner().invoke(app, ["preflight", str(contract_path), "--from", str(out)])
    assert "thermal/cpu-thermal" in result.output
    # Inside the declared 0-90 degC band -> never a NO-GO from this signal.
    assert result.exit_code in (0, 1)


def test_contract_flag_warns_on_undeclared_signals(tmp_path, capsys):
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    contract = _tiny_compute_contract("bench-idle")
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_dump_contract(contract))

    sample_local(
        seconds=0.1,
        hz=10,
        out_dir=out,
        sysfs_root=sysfs_root,
        proc_root=proc_root,
        contract_path=contract_path,
    )
    captured = capsys.readouterr()
    # thermal/cpu-thermal is declared; hwmon/loadavg/net/cooling signals are not.
    assert "thermal/cpu-thermal" not in captured.out
    assert "hwmon/ina3221/in1" in captured.out
    assert "not compared" in captured.out


def test_absent_sources_are_listed_and_not_fatal(tmp_path):
    """A tree with nothing at all still produces a valid (near-empty) recording."""
    sysfs_root = tmp_path / "sys"
    proc_root = tmp_path / "proc"
    sysfs_root.mkdir()
    proc_root.mkdir()
    out = tmp_path / "rec"
    sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)

    manifest_text = (out / "manifest.yaml").read_text()
    for category in ("thermal", "hwmon", "cooling", "loadavg", "net", "vcgencmd"):
        assert category in manifest_text
    recording = load_recording(out)
    assert recording.devices["dev.compute"].signals == {}


def _dump_contract(contract: Contract) -> str:
    import io

    from ruamel.yaml import YAML

    y = YAML()
    output = io.StringIO()
    y.dump(contract.model_dump(mode="json", exclude_none=True), output)
    return output.getvalue()


@pytest.mark.skipif(
    not Path("/sys/class/thermal").exists(), reason="Linux-only: needs a real thermal sysfs tree"
)
def test_real_tree_samples_for_two_seconds(tmp_path):
    out = tmp_path / "rec"
    sample_local(seconds=2.0, hz=1.0, out_dir=out)
    recording = load_recording(out)
    assert "dev.compute" in recording.devices


def test_thermal_conversion_mutation_bite(tmp_path):
    """Mutation bite: drop the /1000 on the thermal conversion and the unit
    test must fail. Left as documentation of the check the real code passes;
    see the commit body for the revert."""
    sysfs_root, proc_root = _write_fake_tree(tmp_path)
    out = tmp_path / "rec"
    sample_local(seconds=0.1, hz=10, out_dir=out, sysfs_root=sysfs_root, proc_root=proc_root)
    recording = load_recording(out)
    value = recording.devices["dev.compute"].signals["thermal/cpu-thermal"][0]
    # Raw sysfs value is 45123 milli-degC; a broken /1000 would report 45123.0.
    assert value == pytest.approx(45.123)
    assert value != pytest.approx(45123.0)


def test_load_contract_roundtrip_sanity(tmp_path):
    """Guards the yaml-dump helper the other tests rely on."""
    contract = _tiny_compute_contract("bench-idle")
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(_dump_contract(contract))
    loaded = load_contract(contract_path)
    assert loaded.devices[0].id == "dev.compute"
