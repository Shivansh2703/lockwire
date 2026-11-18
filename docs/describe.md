---
title: Device pages
description: "lockwire describe writes one plain Markdown page per device from the lockfile, for you or an AI agent."
---

# Device pages: `lockwire describe`

`lockwire describe` writes one plain Markdown page per device, generated from the lockfile: what
the device measures, what it adjusts, which limits are enforced, and where each number comes
from. No language model and no guessing: every line is read from a field the lockfile already
holds. An AI agent can read the page before it touches the robot.

## Usage

```bash
lockwire describe lockwire.yaml               # print every device section to stdout
lockwire describe lockwire.yaml --out docs/devices/   # one <device_id>.md per device, plus SYSTEM.md
```

## Example

For the Jetson in the `arm-power` example (`lockwire start --example arm-power`, then
`lockwire describe lockwire.yaml --out devices/`), `devices/dev.jetson.md` reads:

```markdown
# dev.jetson

- model: mdl.jetson-nano
- family: Compute and control boards (fam.compute)
- owner: you

## Role

component

## Measures

Nothing declared.

## Adjusts

Nothing declared.

## Limits enforced

- input_voltage_range: [4.75 V, 5.25 V] (witness: none named, source: https://developer.download.nvidia.com/embedded/L4T/r32-3-1_Release_v1.0/Jetson_Nano_Developer_Kit_User_Guide.pdf, locked_at: 2026-11-13)
- power draw: 11.25 W on rail.5v
  - rail.5v capacity: 27.50 W at 5.00 V (witness: none named, source: https://www.pololu.com/product/4091, locked_at: 2026-11-13)

## Provenance

- instance:
  - witness: none named
  - source: https://developer.download.nvidia.com/embedded/L4T/r32-3-1_Release_v1.0/Jetson_Nano_Developer_Kit_User_Guide.pdf
  - locked_at: 2026-11-13
  - note: Catalog nvidia.jetson-nano-devkit draw_max 11.25 W (10 W module budget + 1.25 W carrier board, no peripherals).
- model:
  - witness: none named
  - source: https://developer.download.nvidia.com/embedded/L4T/r32-3-1_Release_v1.0/Jetson_Nano_Developer_Kit_User_Guide.pdf
  - locked_at: 2026-11-13
  - note: Catalog row nvidia.jetson-nano-devkit: module requires at least 4.75 V; 5.25 V is the output of the supply NVIDIA names (no maximum is published); draw_max 11.25 W.
```

## `SYSTEM.md`

Alongside the per-device pages, `--out` writes one `SYSTEM.md`: the robot name and schema
version, rails, buses, batteries, wires, budgets, and the model record's joints, mass and forces, with a closing
count of how many devices measure, adjust, or are component-only.

## What it never does

- It never infers a role, a unit or a value from an id or a guess. Every field traces to an
  entry in the lockfile, and a gap reads `Nothing declared.`, not a blank or an invented number.
- It never predicts a reading or a limit that is not already in the lockfile.
- It never runs a model or calls out to anything. It only renders data that is already
  checked in.
