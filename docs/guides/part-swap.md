---
title: Part swap
description: Find which servo can replace the one on a joint, and see what the swap does to the joints above it, before you change anything.
---

# Part swap

`lockwire swap` answers one question: which servo can replace the one on this joint, and what
does that do to the rest of the arm? It reads your lockfile and the parts catalog, ranks the
servos, and shows the joints above the swap. By default it only shows; nothing is written.

## Preview a swap

Name the lockfile and the joint:

```bash
lockwire swap lockwire.yaml wrist
```

```text
wrist: feetech.scs0009, gravity load 0.15 N*m (fit = stall >= 2x load)
  fit      dynamixel.xl330-m288   cited      0.52 N*m  3.4x  18 g  different bus
  fit      dynamixel.xc330-m288   cited      0.93 N*m  6.1x  23 g  different bus
  fit      feetech.sts3032        cited      0.44 N*m  2.9x  20.6 g  different bus, rated at 6 V; your supply is 5 V, expect less (needs new supply)
  fit      feetech.st3215         cited      2.94 N*m  19.6x  55 g  different bus, rated at 12 V; your supply is 5 V, expect less (needs new supply)
  tight    dynamixel.xl330-m077   cited      0.21 N*m  1.4x  18 g  different bus
  ...
  fit      feetech.sts3215        unverified 1.91 N*m  12.7x  55 g  different bus, needs at least 6 V, above your supply's 5 V
With dynamixel.xl330-m288 (+4.8 g on wrist's child link), the joints above:
  elbow 0.40 → 0.40 N*m, still fit (4.7x)
  shoulder 0.81 → 0.83 N*m, still fit (2.3x)
note: supply 5 V, as declared for its power rail
preview only; write it with: lockwire swap lockwire.yaml wrist --use dynamixel.xl330-m288
```

The preview lists every candidate (17 here; shortened above). Each row is a servo from the catalog with a stall torque and a mass: its status, whether its
numbers are cited, its stall torque, stall over load (rounded down, so 1.97 shows as 1.9x), its
mass, and flags.

## How candidates are judged

Every servo is judged against the joint's real gravity load: the worst-pose torque `check`
already scores, with the candidate's own mass added.

| Status | Rule |
| --- | --- |
| fit | stall torque is at least 2x the load |
| tight | at least 1x the load |
| too weak | below the load |

Rows are ordered by what the swap asks of you: too weak servos come last. Among the rest, a
servo whose voltage range does not cover your supply comes after every servo that does, since
over-volting can damage a servo. Then a servo on a different bus comes after one on the same
bus. Within that, fit comes before tight. A servo's stall torque is
rated at one voltage (its datasheet says which). Your supply is the voltage declared for the
joint's power rail; with no rail declared it is taken as at most the current servo's highest
input voltage ("your supply is at most N V"). A servo rated above it will not reach its figure
on this joint: within one status, those rows sit after every servo that runs on your supply,
flagged "rated at 6 V; your supply is 5 V, expect less (needs new supply)". Their stall and ratio
columns still show the rated figure. With no current servo, the supply is unknown and the output
says so.

Then a servo that would drop
any joint above it to a worse status comes after every servo that drops none; then cited parts
come before unverified ones ([The parts list](parts.md)); then the lightest first. So the top
row, the one the app picks for you, needs the fewest changes; read its flags before accepting it. Flags that can follow a row:

- **different bus**: the servo talks on a different bus than the one it replaces. It ranks below
  same-bus servos.
- **rated at N V; your supply is M V, expect less (needs new supply)**: its stall
  torque is rated above your supply.
- **max N V, below your supply's M V (needs new supply)** or **needs at least N V, above your
  supply's M V**: the servo needs a new supply, because its highest input voltage is below the
  current servo's highest, or its lowest is above it. It ranks below every servo that covers it.
- **private**: the part comes from your own `~/.lockwire/parts.yaml` ([The parts list](parts.md)).
  Using it prints a reminder: teammates without it in their parts file will see "no catalog
  servo". Your parts file is never copied anywhere.

## What it does to the joints above

A heavier servo on the wrist is more load for the elbow and shoulder. Under the ranking,
`swap` lists every joint above the swap, with its load before and after and its status. A line
says "warning" only when the status drops, for example
`shoulder 0.81 → 1.05 N*m, warning: now tight (1.8x), was fit` for the 74.5 g STS3250. Joints
with no catalog servo to judge against show the loads and no status.

The command line shows the knock-on for the top candidate. An AI agent can ask for any other
part's knock-on without writing anything (see the MCP section).

## Make the swap: `--use`

```bash
lockwire swap lockwire.yaml wrist --use feetech.sts3032
```

One accepted swap is one edit to the lockfile:

- the joint's `servo:` entry now names the part;
- the file's `schema_version` line is left as it is;
- one device, `dev.<joint>-servo-swap`, carries the extra mass (new servo minus old) on the
  joint's child link; when the old servo's mass is unknown it carries the new servo's full
  mass, and the output says so. When the new servo is lighter, no device is written. Its model is the
  joint's own servo device's model only when that device is the servo being replaced; otherwise
  it is `mdl.part-swap.servo-mass` (mass only), added in the same edit and named in the output;
- a second swap on the same joint replaces that device, it does not stack.

A swap does not change the version line, so a swapped 0.5 file still opens in Lockwire 0.2.6.
New files from `start` and `upgrade` are written at `0.6`, which 0.2.6 and older do
not open. The device is a mass difference, not a part, and its note says so. Your model file is never touched.

## Worked example

The `servo-bench` example has a wrist servo that is too weak on purpose.

```bash
lockwire start --example servo-bench
lockwire check lockwire.yaml
```

```text
VIOLATION model/joints/wrist/limits/effort — observed 0.40 N*m joint effort limit, expected <= 0.226 N*m, the feetech.scs0009 stall torque (Δ 1.8x the stall torque)
  action: a simulator will let wrist push 0.4 N*m, which a feetech.scs0009 cannot; in this lockfile's model: joints wrist limits effort 0.4 → 0.226, then re-lock (ignore if another motor drives it); or find a stronger servo: lockwire swap lockwire.yaml wrist
...
NO-GO — 2 violation(s), 0 warning(s)
```

Preview the wrist as above. The top row, the XL330, asks only for a different bus and adds
4.8 g, so nothing above it drops. To see a warning, take the STS3215 instead: it is unverified
and adds 41.8 g, so the shoulder drops to tight:

```bash
lockwire swap lockwire.yaml wrist --use feetech.sts3215
```

```text
With feetech.sts3215 (+41.8 g on wrist's child link), the joints above:
  elbow 0.40 → 0.48 N*m, still fit (4.0x)
  shoulder 0.81 → 0.97 N*m, warning: now tight (1.9x), was fit
...
swapped wrist to Feetech STS3215 smart servo (7.4 V class)
supply 5 V, as declared for its power rail
added a mass-only part so the new servo's weight is counted (no limits or signals)
the hand link now carries the servo's mass difference
```

`check` again: the wrist line is gone. The remaining violation is the 5 V rail, which is a
different problem.

```text
scored 9: 8 add up, 1 do not · 2 not scored
NO-GO — 1 violation(s), 0 warning(s)
```

## The other fix: lower the effort limit

The servo finding above has a second way out: keep the servo, and change the model so the
joint no longer asks for more than the servo can give. The finding's `action` line is the
exact edit, for the file your model comes from:

- URDF or xacro: `in robot.urdf: joint wrist <limit effort="0.4"> → "0.226"`
- SDF: `in robot.sdf: joint wrist <effort>0.4</effort> → 0.226`
- no model file named: `in this lockfile's model: joints wrist limits effort 0.4 → 0.226`

The new value is the servo's stall torque, three decimals, rounded down to 0.001 so it never
exceeds it.
Lockwire never edits your model file. You make the edit, then re-lock. If a different motor
really drives that joint, ignore the finding.

## For AI agents (MCP)

The MCP server has a `swap` tool ([The MCP server](../mcp_server.md)). With a contract and a
joint it previews. With a `part_id` it shows the knock-on for that part. It writes only when
it is given a `part_id` and an `expected_revision`, the SHA-256 of the contract bytes the
agent read, so a file that changed underneath is refused rather than overwritten. The result
carries `written` and the equivalent `cli_line`.
