"""Commands against observed motion: a sibling of the command stage.

Per device with a watched command stream, two plain-word WARNINGS (never a
violation, D3): it moved with no command near it, or it was commanded and no
motion followed. "Motion" is the device's own velocity/position channel, else
the IMU angular rate. Called only from ``commands.check``; no verdict of its own.
"""

from __future__ import annotations


from lockwire_pro.preflight.envelopes import signal_view_for_context
from lockwire_pro.recording import Recording
from lockwire.report import Finding
from lockwire.schema import Contract

# ponytail: fixed window, no per-device tuning yet (command timestamps +-0.5 s).
WINDOW_S = 0.5
# ponytail: fixed fallback motion threshold (rad/s or m/s) when no band is declared; no per-device tuning yet.
MOTION_THRESHOLD = 0.05
# ponytail: fixed; the declared band is a 95% noise band, so motion must clear it by this factor.
BAND_MULTIPLE = 3.0


def _own_motion(recording: Recording, device: str) -> list[tuple[float, float]] | None:
    """(time, speed) from the device's own velocity/position channels, else None."""
    stream = recording.devices.get(device)
    if stream is None:
        return None
    rows: dict[float, float] = {}
    for name, values in stream.signals.items():
        low = name.lower()
        if "velocity" in low:
            for t, v in zip(stream.recv_times, values, strict=False):
                rows[t] = max(rows.get(t, 0.0), abs(v))
        elif "position" in low:
            for i in range(1, min(len(values), len(stream.recv_times))):
                dt = stream.recv_times[i] - stream.recv_times[i - 1]
                if dt > 0:
                    t = stream.recv_times[i]
                    rows[t] = max(rows.get(t, 0.0), abs(values[i] - values[i - 1]) / dt)
    return sorted(rows.items()) or None


def _imu_motion(contract: Contract, recording: Recording) -> list[tuple[float, float]] | None:
    """(time, excess) from the first recorded device with gyro_* signals.

    Excess is how far the largest axis sits outside BAND_MULTIPLE times its
    declared noise band (the envelope band for that gyro channel), or outside
    +-MOTION_THRESHOLD when no band is declared; above 0 means the IMU saw motion.
    """
    for dev in contract.devices:
        stream = recording.devices.get(dev.id)
        gyro = {
            k: v for k, v in (stream.signals.items() if stream else ()) if k.startswith("gyro_")
        }
        if not gyro:
            continue
        declared = signal_view_for_context(dev, recording.context).signals
        bands = {
            k: (
                declared[k].signal.band.lo * BAND_MULTIPLE,
                declared[k].signal.band.hi * BAND_MULTIPLE,
            )
            if k in declared
            else (-MOTION_THRESHOLD, MOTION_THRESHOLD)
            for k in gyro
        }
        return [
            (t, max(max(v[i] - bands[k][1], bands[k][0] - v[i], 0.0) for k, v in gyro.items()))
            for i, t in enumerate(stream.recv_times[: min(map(len, gyro.values()))])
        ]
    return None


def compare(
    contract: Contract, recording: Recording, command_times: dict[str, list[float]]
) -> list[Finding]:
    """Findings for each device in ``command_times`` (device -> command timestamps)."""
    out: list[Finding] = []
    for device, times in command_times.items():
        quantity = f"preflight/commands/{device}/motion"
        own = _own_motion(recording, device)
        series = own or _imu_motion(contract, recording)
        if not series:
            out.append(
                Finding(
                    severity="not_scored",
                    quantity=quantity,
                    observed=f"{device} has no velocity or position channel and the recording has no IMU",
                    expected="the device's own motion channel, or an IMU angular rate",
                    action="record the device's feedback or an IMU to compare commands with motion",
                )
            )
            continue
        limit = MOTION_THRESHOLD if own else 0.0  # IMU excess is already past its band
        first, last = series[0][0], series[-1][0]
        near = lambda t: any(abs(t - c) <= WINDOW_S for c in times)  # noqa: E731
        moved = [t for t, speed in series if speed > limit and not near(t)]
        still = [
            c
            for c in times
            if first <= c <= last
            and not any(abs(t - c) <= WINDOW_S and speed > limit for t, speed in series)
        ]
        if moved:
            out.append(
                Finding(
                    severity="warning",
                    quantity=quantity,
                    observed=f"{device} moved with no command at t={moved[0]:g} s",
                    expected=f"a command within {WINDOW_S:g} s of any motion",
                    action="check what moved it, or that the command stream is complete",
                )
            )
        if still:
            out.append(
                Finding(
                    severity="warning",
                    quantity=quantity,
                    observed=f"{device} commanded, no motion seen near t={still[0]:g} s",
                    expected=f"motion within {WINDOW_S:g} s of a command",
                    action="check the command reached the device, or the feedback channel",
                )
            )
    return out
