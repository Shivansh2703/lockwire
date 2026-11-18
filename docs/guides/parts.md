---
title: The parts list
description: What cited and unverified mean on a catalog part, how to keep private parts on your own machine, and how to contribute a part.
---

# The parts list

Lockwire ships a catalog of parts you might build a robot from: servos, computers, batteries,
sensors. `lockwire from-parts` matches a list you paste against it. Every part carries a label
that says how far to trust its numbers.

## Cited and unverified

- **cited**: every number on the part comes from the maker's own datasheet, and each one names
  the `https://` URL it was read from.
- **unverified**: anything else. A number from a retail listing counts as unverified, so does a
  number read from a file, an assumed placeholder, or a datasheet figure with no URL.

The label is worked out from the numbers, never typed in. Fix a source and the label follows.

## Where you see it

Browse the catalog first: `lockwire catalog parts raspberry` lists matching parts with their numbers.

Add one part to your lockfile by its id, with no number typed:

```bash
lockwire start --example rover
lockwire add device lockwire.yaml --part rpi.pi5 --rail rail.controller-5v
lockwire check lockwire.yaml
```

`add` prints the change as a diff. The new device has no `witness:` line: with `--part` and no
`--witness`, nobody is named yet. Pass `--witness "Your Name"` to name the person who stands behind it. Other values in the example
carry a stored witness code such as `w0` ([what each code means](../words.md#stored-witness-codes)).

This check ends in `NO-GO` (exit 1), and that is the point: the Pi 5 pushes the 5 V controller rail over its limit.
The lines that matter:

```text
VIOLATION rail.controller-5v subsystem: 2 members affected: budgets/bud.power.controller-5v, topology/rails/rail.controller-5v/current_limit
    observed 21.55 W (draw mode: nominal), expected <= 20.00 W (off by +1.55 W, +8%)
    observed 4.47 A computed draw on rail 'rail.controller-5v' (mode: start-up), expected <= 4.00 A published current_limit (off by +0.47 A, +12%)
NO-GO: 2 violation(s), 1 warning(s) · 1 advice (not yet sourced, outside the verdict)
```

Moving the Pi to the other 5 V rail does not clear it: `rail.jetson-header-5v` is fed from
`rail.controller-5v`, so its draw still counts there. What clears it here is taking load off the rail.
If the Pi replaces the rover's Jetson, delete the Jetson. Every edit names a witness: the person who
stands behind it, recorded next to the change and its source. Use your own name:

```bash
lockwire delete device lockwire.yaml --id dev.jetson-nano-computer --source "the Pi 5 replaces it" --witness "Your Name"
lockwire check lockwire.yaml
```

```text
GO with notes: 1 warning(s) · 1 advice (not yet sourced, outside the verdict)
```

`--part` fills the model, mass, draw and source from the catalog. A part with a draw goes on a rail:
if only one rail fits its voltage it is picked for you, and if several fit, `add` lists them and
you pass `--rail`. Your lockfile needs rails, so start from `lockwire start --example <robot>`.
A `--rail` the part cannot run on (say the 11.1 V battery for a 5 V part) is refused, and the message lists the rails that fit. A part with only a nominal voltage fits rails within ±5% of it. A part the catalog marks `unverified` is written with a source that starts "unverified catalog entry:", and `add` says so; check its numbers against the datasheet.

Write one part per line (a count like `x6` or `6x` is read), then match the list:

```bash
lockwire from-parts parts.txt
lockwire from-parts parts.txt --out lockwire.yaml   # also write a draft lockfile
```

```text
┏━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┓
┃ line ┃ qty ┃ part                    ┃ result  ┃ catalog id                     ┃ match ┃ label      ┃
┡━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━┩
│ 1    │ 6   │ SO-101 servo STS3215 x6 │ MATCHED │ feetech.sts3215                │ 0.95  │ unverified │
│ 2    │ 1   │ Jetson Orin Nano        │ MATCHED │ nvidia.jetson-orin-nano-devkit │ 0.95  │ cited      │
└──────┴─────┴─────────────────────────┴─────────┴────────────────────────────────┴───────┴────────────┘
```

On a narrow terminal the columns wrap and long catalog ids split across lines.

A number you wrote that disagrees with the catalog (a mass, a voltage) is printed as a
`MISMATCH` line. It is information; a successful run exits 0.

## Your own parts

Put parts the public catalog does not have in `~/.lockwire/parts.yaml`. It is a list, with the
same shape as the bundled catalog's entries:

```yaml
- id: acme.servo.a100
  family: fam.servo
  name: Acme A100 servo
  numbers:
    mass:
      value: 55
      unit: g
      kind: datasheet
      source: https://acme.example/a100.pdf, "Weight 55 g"
```

- The file stays on your machine. Lockwire never uploads or publishes it; it is merged in
  when it is there.
- An `id` that is already in the public catalog is refused, so a private part cannot
  quietly change a public one.
- If the file is broken (not valid YAML, not a list, an entry with no `id`, a bad number),
  every command that reads the catalog stops with one plain line naming the file and the
  problem.

## Add a part to the public catalog

Open a pull request. [CONTRIBUTING.md](../../CONTRIBUTING.md#add-a-part) lists what a part
needs and how to test it.

See also: [Part swap](part-swap.md), which ranks these parts for one joint of your robot.
