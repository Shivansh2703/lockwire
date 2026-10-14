# `lockwire describe`

A per-device reference file, generated from the contract. Every device gets a plain-Markdown
page: what it measures, what it adjusts, what limits are enforced, and where each number comes
from. No LLM, no guessing — every line is read from a schema field that already exists in the
lockfile. This is the artifact an AI-agent hardware layer reads before touching the machine.

## Usage

```bash
lockwire describe lockwire.yaml               # print every device section to stdout
lockwire describe lockwire.yaml --out docs/devices/   # one <device_id>.md per device, plus SYSTEM.md
```

## Example

For a companion computer with three declared power modes:

```markdown
# dev.companion.jetson-orin-nano

- model: mdl.companion.jetson-orin-nano
- family: fam.companion.sbc — Companion computer
- owner: companion-compute
- identity strength: positional
- bus: bus.uart.companion

## Role

component

## Measures

Measures: nothing declared.

## Adjusts

Adjusts: nothing declared.

## Limits enforced

- power draw: 25.00 W on rail.pm02-5v
  - mode 7W: 7.00 W
  - mode 15W: 15.00 W
  - mode 25W: 25.00 W
  - rail rail.pm02-5v capacity: 15.60 W at 5.20 V (witness: w0, source: ..., locked_at: 2026-07-25)
```

## `SYSTEM.md`

Alongside the per-device pages, `--out` writes one `SYSTEM.md`: the robot name and schema
version, rails, buses, batteries, wires, budgets, and the model record's joints — with a closing
count of how many devices measure, adjust, or are component-only.

## What it never does

- Never infers a role, a unit, or a value from an id string or a guess — every field traces to a
  schema entry, and a gap renders as `nothing declared`, not a blank or invented number.
- Never predicts a reading or a limit that isn't already locked in the contract.
- Never runs a model or calls out to anything — it is a pure rendering of data that was already
  checked in.

Designed to be MHS-ready: the shape of a per-device reference file, generated from a verified
contract.
