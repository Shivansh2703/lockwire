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

Write one part per line (a count like `x6` or `6x` is read), then match the list:

```bash
lockwire from-parts parts.txt
lockwire from-parts parts.txt --out lockwire.yaml   # also write a draft lockfile
```

```text
 line  qty  part                     result   catalog id                       confidence  label
 1     6    SO-101 servo STS3215 x6  MATCHED  feetech.sts3215                  0.95        unverified
 2     1    Jetson Orin Nano         MATCHED  nvidia.jetson-orin-nano-devkit   0.95        cited
```

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

## Thrust tables

`lockwire fly` (Pro) reads a motor and prop's thrust from `src/lockwire/thrust_tables.yaml`: the
maker's own bench rows (throttle %, thrust g, current A, power W) at one cell count, with the URL
they were read from. Name a table from your motor's device model with `thrust_table: <id>`. A
motor with no table is reported as not scored, never guessed. To add one, open a pull request
with a new entry and the maker's URL; the cited/unverified label follows the rule above.
