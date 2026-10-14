"""Minimal unit handling: enough to sum and compare contract quantities.

Deliberately a table, not a units library. Every unit the schema accepts is
listed here; an unknown unit fails validation instead of passing through.
"""

import math

# unit -> (dimension, factor to the dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {
    # mass -> kg
    "kg": ("mass", 1.0),
    "g": ("mass", 1e-3),
    "mg": ("mass", 1e-6),
    # power -> W
    "W": ("power", 1.0),
    "mW": ("power", 1e-3),
    "kW": ("power", 1e3),
    # energy -> Wh (Wh over J: battery math reads naturally)
    "Wh": ("energy", 1.0),
    "mWh": ("energy", 1e-3),
    "kWh": ("energy", 1e3),
    "J": ("energy", 1.0 / 3600.0),
    # time -> s
    "s": ("time", 1.0),
    "ms": ("time", 1e-3),
    "min": ("time", 60.0),
    "h": ("time", 3600.0),
    # rate -> Hz
    "Hz": ("rate", 1.0),
    # electrical
    "V": ("voltage", 1.0),
    "mV": ("voltage", 1e-3),
    "A": ("current", 1.0),
    "mA": ("current", 1e-3),
    # length -> m
    "m": ("length", 1.0),
    "cm": ("length", 1e-2),
    "mm": ("length", 1e-3),
    "m^2": ("area", 1.0),
    "m^4": ("second_moment", 1.0),
    # temperature (absolute compare only — never summed)
    "C": ("temperature", 1.0),
    "degC": ("temperature", 1.0),  # bag spelling for the same Celsius scale
    # pressure -> hPa
    "Pa": ("pressure", 0.01),
    "hPa": ("pressure", 1.0),
    "kPa": ("pressure", 10.0),
    "MPa": ("pressure", 1e4),
    "GPa": ("pressure", 1e7),
    # scalar angular measure -> rad (not semantic position or orientation roles)
    "deg": ("angle", math.pi / 180.0),
    # kinematics
    "m/s^2": ("acceleration", 1.0),
    "m/s": ("velocity", 1.0),
    "rad/s": ("angular_rate", 1.0),
    "rad/s^2": ("angular_acceleration", 1.0),
    "rad": ("angle", 1.0),
    "N": ("force", 1.0),
    "N*m": ("torque", 1.0),
    "ohm": ("resistance", 1.0),
    "kg*m^2": ("inertia", 1.0),
    "kg/m^3": ("density", 1.0),
    "N/m": ("linear_stiffness", 1.0),
    "N*m/rad": ("rotational_stiffness", 1.0),
    "N*s/m": ("linear_damping", 1.0),
    "N*m*s/rad": ("rotational_damping", 1.0),
    "N/m^3": ("contact_stiffness", 1.0),
    "N*s/m^3": ("contact_damping", 1.0),
    "1/s": ("inverse_time", 1.0),
    # dimensionless
    "1": ("dimensionless", 1.0),
    "%": ("dimensionless", 0.01),
    # thermal resistance -> C/W (temperature C/degC stay on temperature)
    "C/W": ("thermal_resistance", 1.0),
    # RF power level -> dBm (logarithmic; never share power with W/mW/kW —
    # scale-factor convert or sum of dBm and watts is arithmetic nonsense)
    "dBm": ("power_level", 1.0),
    # RF gain / ratio -> dB (log-space only; never convertible to a linear qty)
    "dB": ("gain", 1.0),
    "dBi": ("gain", 1.0),
    # data rate -> bit/s
    "bit/s": ("data_rate", 1.0),
    "kbit/s": ("data_rate", 1e3),
    "Mbit/s": ("data_rate", 1e6),
}


class UnitError(ValueError):
    """Unknown unit or dimension mismatch."""


def known(unit: str) -> bool:
    return unit in _UNITS


def dimension(unit: str) -> str:
    try:
        return _UNITS[unit][0]
    except KeyError:
        raise UnitError(f"unknown unit {unit!r}") from None


def to_base(value: float, unit: str) -> float:
    """Convert to the dimension's base unit (kg, W, Wh, s, Hz, ...)."""
    try:
        return value * _UNITS[unit][1]
    except KeyError:
        raise UnitError(f"unknown unit {unit!r}") from None


def convert(value: float, unit: str, target: str) -> float:
    if dimension(unit) != dimension(target):
        raise UnitError(f"cannot convert {unit!r} to {target!r}: different dimensions")
    return to_base(value, unit) / _UNITS[target][1]
