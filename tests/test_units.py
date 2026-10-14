import pytest

from lockwire.units import UnitError, convert, dimension, known, to_base


def test_mass_to_base():
    assert to_base(12, "g") == pytest.approx(0.012)


def test_convert_within_dimension():
    assert convert(30, "min", "h") == pytest.approx(0.5)
    assert convert(2.5, "kW", "W") == pytest.approx(2500)


def test_unknown_unit_raises():
    with pytest.raises(UnitError):
        to_base(1, "furlong")


def test_cross_dimension_convert_raises():
    with pytest.raises(UnitError):
        convert(1, "kg", "W")


def test_dimension():
    assert dimension("Wh") == "energy"
    assert dimension("m/s^2") == "acceleration"


def test_thermal_and_rf_units_known():
    assert known("C/W")
    assert dimension("C/W") == "thermal_resistance"
    assert known("dBm")
    assert dimension("dBm") == "power_level"
    assert known("dB")
    assert dimension("dB") == "gain"
    assert known("dBi")
    assert dimension("dBi") == "gain"
    assert known("bit/s")
    assert dimension("bit/s") == "data_rate"
    assert known("kbit/s")
    assert dimension("kbit/s") == "data_rate"
    assert known("Mbit/s")
    assert dimension("Mbit/s") == "data_rate"


def test_dbm_not_convertible_to_watts():
    with pytest.raises(UnitError):
        convert(1, "dBm", "W")
    with pytest.raises(UnitError):
        convert(1, "W", "dBm")


def test_db_gain_not_convertible_to_linear():
    with pytest.raises(UnitError):
        convert(1, "dB", "W")
    with pytest.raises(UnitError):
        convert(1, "dBi", "1")
    with pytest.raises(UnitError):
        convert(1, "dB", "dBm")
