import pytest
from pydantic import ValidationError

from lockwire.checks import _ScoreBook, check_compared_from, check_model_values
from lockwire.cli import app
from lockwire.schema import Contract, Link, Quantity, RobotInfo, load_contract, save_contract
from lockwire.urdf import parse_urdf
from tests.test_checks import _copied_catalog_mass_contract

URDF = '<robot name="r"><link name="a"><inertial><mass value="{}"/></inertial></link></robot>'
ABS = {"abs": {"value": 0.05, "unit": "kg"}}


def _model_check(tmp_path, monkeypatch, tol, model_kg):
    monkeypatch.chdir(tmp_path)
    urdf = tmp_path / "robot.urdf"
    urdf.write_text(URDF.format(1.2))
    contract = Contract(robot=RobotInfo(name="r"), model=parse_urdf(urdf))
    contract.model.links[0].mass_tolerance = tol
    urdf.write_text(URDF.format(model_kg))
    scores = _ScoreBook()
    return check_model_values(contract, scores=scores, contract_path=tmp_path / "l.yaml"), scores


@pytest.mark.parametrize(
    "tol, kg, inside",
    [(ABS, 1.22, 1), (ABS, 1.25, 1), (ABS, 1.15, 1), (ABS, 1.26, 0), (ABS, 1.14, 0)]
    + [({"rel_pct": 5}, 1.26, 1), ({"rel_pct": 5}, 1.27, 0), (None, 1.22, 0), (None, 1.2, 1)],
)
def test_mass_inside_its_band_is_no_drift_and_outside_names_the_band(
    tmp_path, monkeypatch, tol, kg, inside
):
    found, scores = _model_check(tmp_path, monkeypatch, tol, kg)
    assert bool(found) is not inside
    if tol and inside:
        assert "inside the band" in scores.records[0].margin and "±" in scores.records[0].expected
    if tol and not inside:
        assert "±" in found[0].expected and "locked mass 1.2 kg" in found[0].expected
    if not tol and not inside:
        assert found[0].expected == "locked mass 1.2 kg"


def test_a_device_mass_band_covers_a_catalog_copy_inside_it():
    ref = "x500.flight-controller.pixhawk-6c"  # catalog mass 59.3 g
    for tol, warned in ((None, 1), ({"rel_pct": 5}, 0), ({"rel_pct": 1}, 1)):
        c = _copied_catalog_mass_contract(mass_g=57, compared_from=ref)
        c.devices[0].physical.mass_tolerance = tol
        assert len(check_compared_from(c)) == warned


def test_a_band_needs_a_mass_and_never_goes_negative():
    for bad in ({"rel_pct": -1}, {"abs": {"value": -0.05, "unit": "kg"}}, {"min": -1, "max": 1}):
        with pytest.raises(ValidationError):
            Link(name="a", mass=Quantity(value=1, unit="kg"), mass_tolerance=bad)
    with pytest.raises(ValidationError, match="needs a mass"):
        Link(name="a", mass_tolerance=ABS)


def test_a_lock_without_a_band_writes_no_key_and_declare_writes_one(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "robot.urdf").write_text(URDF.format(1.22))
    lock = tmp_path / "lockwire.yaml"
    save_contract(Contract(robot=RobotInfo(name="r"), model=parse_urdf(tmp_path / "robot.urdf")), lock)
    first = lock.read_bytes()
    assert b"mass_tolerance" not in first
    save_contract(load_contract(lock), lock)
    assert lock.read_bytes() == first
    run = CliRunner().invoke
    args = ["declare", str(lock), "model/links/a/mass", "1.2 kg", "--source", "scale", "--witness", "Pat Lee"]
    assert run(app, args).exit_code == 0 and run(app, ["check", str(lock)]).exit_code == 1
    args[2:4] = ["model/links/a/mass_tolerance", "0.05 kg"]
    declared = run(app, args)
    assert declared.exit_code == 0 and "abs:" in lock.read_text()
    assert "= \u00b1 0.05 kg" in declared.stdout and "'abs'" not in declared.stdout
    assert run(app, ["check", str(lock)]).exit_code == 0


def test_tolerance_words():
    from lockwire.edit import tolerance_text

    assert tolerance_text({"rel_pct": 2.0}) == "\u00b1 2 %"
    assert tolerance_text({"min": 1.18, "max": 1.3}, "kg") == "1.18 to 1.3 kg"
