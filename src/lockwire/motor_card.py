"""Motor card consistency, encoder rate and wheeled-base slip.

Read only from the actuator and base fields; a missing input emits nothing.
Tested on recorded files, not on hardware (``LABEL``).
"""

from __future__ import annotations

import math
from pathlib import Path

from lockwire.report import Finding
from lockwire.schema import Contract

LABEL = "tested on recorded files, not on hardware"
KV_TO_KE = 60 / (2 * math.pi)  # Ke [V*s/rad] = 60 / (2*pi*Kv[rpm/V])
SINE_FACTOR = (
    math.sqrt(3) / 2
)  # UNSOURCED, advice only: sine drive, Kt = (sqrt(3)/2) * Ke(line-line)
AGREE = 1.25  # ponytail: unsourced datasheet-rounding slack; advice only, never a verdict
G = 9.80665


def _expected_kt(a) -> float | None:
    if a.kv_rpm_per_v is None:
        return None
    ke = KV_TO_KE / a.kv_rpm_per_v
    return ke * (SINE_FACTOR if a.kt_convention == "sine" else 1.0)


def check_kv_kt(contract: Contract, contract_path: Path | None = None) -> list[Finding]:
    out = []
    for m in contract.device_models:
        a = m.actuator
        want = _expected_kt(a) if a else None
        if want is None or a.torque_constant is None:
            continue
        ratio = max(a.torque_constant, want) / min(a.torque_constant, want)
        if ratio < AGREE:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"device_models/{m.id}/actuator/torque_constant",
                observed=f"Kv and Kt disagree by {ratio:.1f}x: Kt {a.torque_constant:g} N*m/A",
                expected=(
                    f"Kt {want:g} N*m/A from Kv {a.kv_rpm_per_v:g} rpm/V "
                    f"({a.kt_convention or 'trap'} convention, Kt = Ke in SI)"
                ),
                action=f"check the Kv and Kt datasheet rows, and whether Kv is line-to-line ({LABEL})",
                advice_unsourced=True,  # no source for the 1.25x slack, nor for the sqrt(3)/2 sine factor
            )
        )
    return out


def check_encoder_rate(contract: Contract, contract_path: Path | None = None) -> list[Finding]:
    out = []
    for m in contract.device_models:
        a = m.actuator
        if a is None or None in (a.encoder_cpr, a.encoder_max_rate_hz, a.no_load_speed_rad_s):
            continue
        rate = a.encoder_cpr * a.no_load_speed_rad_s / (2 * math.pi)
        if rate <= a.encoder_max_rate_hz:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"device_models/{m.id}/actuator/encoder_max_rate_hz",
                observed=f"encoder needs {rate:.0f} counts/s at no-load speed",
                expected=f"<= {a.encoder_max_rate_hz:g} counts/s encoder rate limit",
                action=f"slow the motor, or use an encoder or counter that keeps up ({LABEL})",
            )
        )
    return out


def check_diff_drive_slip(contract: Contract, contract_path: Path | None = None) -> list[Finding]:
    """Two driven wheels pushing at motor stall must not exceed tyre grip (mu * m * g)."""
    b = contract.base
    if contract.model is None or b is None:
        return []
    if None in (b.wheel_radius_m, b.track_width_m, b.tyre_friction_mu):
        return []
    mass = sum(k.mass.base() for k in contract.model.links if k.mass is not None)
    if mass <= 0:
        return []
    out = []
    for m in contract.device_models:
        a = m.actuator
        kt = a and (a.torque_constant or _expected_kt(a))
        if not kt or a.stall_current_a is None or a.gear_ratio is None:
            continue
        # efficiency taken as 1: an upper bound on push, so a slip warning is never a false one
        push = 2 * kt * a.stall_current_a * a.gear_ratio / b.wheel_radius_m
        grip = b.tyre_friction_mu * mass * G
        if push <= grip:
            continue
        out.append(
            Finding(
                severity="warning",
                quantity=f"device_models/{m.id}/actuator/stall_current_a",
                observed=f"wheels slip before the motors stall: {push:.1f} N push at stall",
                expected=f"<= {grip:.1f} N tyre grip (mu {b.tyre_friction_mu:g}, {mass:.2f} kg)",
                action=f"limit motor current, or raise the tyre friction or the weight on the wheels ({LABEL})",
            )
        )
    return out


ENGINE_CHECKS = (check_kv_kt, check_encoder_rate, check_diff_drive_slip)
