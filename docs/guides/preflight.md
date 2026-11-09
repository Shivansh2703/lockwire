---
title: Preflight, the powered-on robot gate
description: Run lockwire preflight on a PX4 log, a ROS 2 bag or a live sample and get a go/no-go for the powered robot, with baselines and drift.
---

# Preflight, the powered-on robot gate

`lockwire check` checks the model: the numbers in `lockwire.yaml` against each other.
`lockwire preflight` checks the robot: a recording of the powered-on machine against that same
lockfile. The question is "does the robot I am about to run still behave like the one I
locked?" The answer is a go/no-go.

These commands are part of the same `lockwire` CLI and MCP server, installed with every
`pip install lockwire`.

## Where the recording comes from

| Source | How |
| --- | --- |
| PX4 ULog | `--from flight.ulg` |
| ROS 2 bag, MCAP | `--from run.mcap` or a bag directory. No ROS install needed. SQLite `.db3` bags are refused by name: re-record as MCAP. Reading MCAP needs the `lockwire[ros2bag]` extra. |
| A Lockwire recording directory | `--from recording/`, written by `lockwire sample` |
| A live ROS 2 robot | `--live`, with `--container NAME` (Docker), `--local` (this machine) or `--ssh-host HOST` |

For a ULog or a bag, `--context` says what the robot was doing: `bench-idle`, `armed-idle`
or `nominal-op` (the default). It picks which declared envelopes apply. A bag also needs
each device's `driver` (topic, message type, signals) in the contract; that is how Lockwire
knows which topic is which device.

Record health with no hardware, from the robot's own computer:

```bash
lockwire sample --local --seconds 10 --hz 1 --out recording/
lockwire sample --host robot.local --known-hosts known_hosts --out recording/
```

`--host` takes one read-only snapshot of `/sys` and `/proc` over ssh and needs pinned
host keys.

## Run the gate

```bash
lockwire preflight lockwire.yaml --from flight.ulg
lockwire preflight lockwire.yaml --from run.mcap --context nominal-op --html report.html
lockwire preflight lockwire.yaml --live --container my-robot --seconds 10
```

`--html` and `--json` write the same run as a page or a file. Each run also appends to a
health ledger beside the contract; `--no-ledger` skips that.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | GO, or GO with notes |
| 1 | NO-GO |
| 2 | Refused: the recording or the contract could not be used |
| 3 | NOT SCORED: nothing was compared |

`--strict` makes a GO with notes exit 1 and any unjudged check exit 3.
`--max-not-scored N` exits 3 when more than N checks went unjudged. Use `--strict` when a
green light must mean everything was looked at.

`--profile powered-release` is the strict profile for releasing a robot to power. It needs
an explicit list of evidence classes, one `--require-evidence` for each. The default
profile is `analysis`.

## What the gate checks

The stages run in order. Each emits findings; none stops the others.

1. **Enumeration.** The devices the contract declares against what the recording holds.
2. **Liveness.** Each declared driver or node against what the source shows. A live source
   scores a missing driver as NO-GO; a recording cannot, so it says "not scored".
3. **Flow.** Rate and gaps, with the two rules below.
4. **Sanity.** Values that are not numbers, values outside the envelope band for the
   context, values outside the device's hard limits, and one-shot variance.
5. **Cross-checks.** Budgets summed over the recording, condition envelopes (the recording
   may not extrapolate beyond the conditions the baselines supported), stationary gravity
   when the robot is idle, the commands recorded beside the capture, and whether a
   commanded device moved. Command-versus-motion findings are warnings only.

A device with fewer than 10 samples, or under one second of recording, is not scored.

### Rate and gap rules

- **Rate** is the mean over sliding windows, each `max(1 s, 5 / lo)` long, where `lo` is
  the low end of the declared rate band. One backlog burst or one slow tick does not
  fail a stream; a window holding a single message counts as silence (0 Hz).
- **Gap** fails when two messages are more than `2 / lo` apart: two missed periods. A
  loaded computer delays single messages; two missed periods in a row is a stale stream.

## Reading the verdict

The report opens with the verdict, then the counts: compared, not scored, refused. Each
finding names the device and the quantity (for example `devices/imu0/recv_gap`), what was
observed, what was expected, who owns the device and what to do. Severities:

- **violation** makes the run NO-GO.
- **warning** is a GO with notes.
- **not scored** says what could not be compared and which operand is missing. It is never
  a pass. A run that compared nothing exits 3, not 0.

## Baselines and drift

A baseline is the envelope of a known-good recording, reviewed by a person before it is
trusted.

```bash
lockwire baseline good/ --context nominal-op --rationale "bench run 11-02, new IMU" > baseline.yaml
lockwire baseline-review baseline.yaml
lockwire baseline-check recording/ baseline.yaml --contract lockwire.yaml
```

`baseline` writes the artifact to stdout and decides nothing. `baseline-review` shows the
evidence you need to decide whether to trust it; it approves nothing. There is no approval
step: the baseline counts as accepted when you commit that file and point `baseline-check` at
it. Omitting `--rationale` is recorded as an evidence gap. `baseline-check` compares a later
recording against the baseline you accepted and reports
findings in the same format as `check`, including `drift-cusum`: a slow shift that no single
reading would flag.

To read the ledger of past runs:

```bash
lockwire health lockwire.yaml
lockwire health lockwire.yaml --verify
```

`health` prints each device's current state (exit 1 if one is faulty, 3 if the ledger is
empty). `--verify` walks the ledger's hash chain and names the first line that does not
hold. `--ledger FILE` reads a ledger copied off the robot.

## Actuator corrections

```bash
lockwire actuator-review-draft lockwire.yaml CASE_ID --evidence cases/step.csv@SHA256
```

This prepares one evidence-bound correction for an engineer to review. It never applies
or configures anything.

## What it never does

Lockwire never actuates. It reads recordings and samples; it does not write to a device,
a bus or an actuator, and it does not command the robot. A repository test scans the
source for device writes, bus writes and ROS publishers and fails on any that is not
named and justified; the one allowed publisher reports the gate's own verdict as
diagnostics. That test is a source scan, not a proof of runtime behaviour: it does not
resolve a ROS topic name set at launch time. The ssh sample is read-only.

## Limits

- **Cross-axis effects are disclosed, not modelled.** When a context declares two or more
  condition axes (say temperature and voltage), each axis is checked on its own. The
  report says the interaction between axes is unverified, as "not scored". A single-axis
  envelope needs no such note.
- A recording cannot show a driver that is down, only a live source can.
- Command records are not read out of a bag or a log; they come from a separate record.
- A recording shows the robot as it was. It says nothing about what it will do next.
