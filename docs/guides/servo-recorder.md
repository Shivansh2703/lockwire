---
title: Servo recorder
description: Record the settings and live readings of Feetech STS/SCS or Dynamixel X servos on a bus, read-only, into a recording Lockwire can use.
---

# Servo recorder

`lockwire record-servos` listens to the servos on a serial bus and writes what they report:
their stored settings, then position, speed, load, voltage and temperature at a steady rate.

It is **read-only**. It sends only ping and read packets, and the packet builder refuses any
other instruction, so it never writes a register and cannot change a servo.

**Not yet run on real hardware by us.** It is tested on recorded bytes (real packet layouts
from each vendor's published protocol), not on a live bus. Try it first on a servo that is
not holding anything up, and tell us what you see.

## Install

```bash
pip install 'lockwire[bus]'
```

The extra adds `pyserial`. Without it the command stops and says so.

## Run it

```bash
lockwire record-servos --port /dev/ttyACM0 --ids 1-6 --seconds 10 --out recording/
lockwire record-servos --port /dev/ttyUSB0 --kind dxl --baud 57600 --ids 1-4 --out recording/
```

| Option | Meaning | Default |
| --- | --- | --- |
| `--port` | Serial port of the bus adapter (required) | |
| `--out` | Folder to write (required) | |
| `--ids` | Servo IDs: `1-6` or `1,3,5` | `1-6` |
| `--kind` | `sts` or `scs` (Feetech), `dxl` (Dynamixel X, Protocol 2.0) | `sts` |
| `--baud` | Bus baud rate | `1000000` (Feetech), `57600` (`--kind dxl`) |
| `--rate` | Samples per second | `20` |
| `--seconds` | How long to record | `10` |

If no servo answers, the command stops and says "no servo answered on the bus; check port,
baud rate and power". A servo that answers at the start and then goes quiet is recorded as a
gap, printed as a yellow `gap:` line, and the recording carries on.

## What it records

The folder holds two files:

- `manifest.yaml`: the capture length, the port and rate, the gaps, each servo's settings
  (firmware, ID, voltage and temperature limits; Feetech also max torque, Dynamixel also model
  number and current limit where the model is known) and the unit of every signal.
- `samples.jsonl`: one line per servo per sample, `{"t": seconds, "dev": "servo.1", "sig": {...}}`.

Signals: position (raw count, and degrees for STS and Dynamixel), speed, voltage, temperature,
and load or current. Feetech STS records current in mA; SCS records it as a raw count.

**"Load" is drive duty, not torque.** On Feetech it is the drive duty in per-mille of maximum
(`load_duty_permille`). It is not measured torque. On Dynamixel, register 126 is current on
XM models and load on XL models, so it is recorded raw as `load_or_current_raw` with the model
number beside it.

The folder uses the same recording format as `lockwire sample` (Lockwire Pro).
