# Plan validation

`lockwire plan CONTRACT PLAN` checks an agent's intended plan against the
contract before the first command executes. `lockwire check` asks "does this
robot's record agree with itself?"; `lockwire plan` asks "is this specific
sequence of actions safe against what the contract declares?" — a plan can be
rejected even when the contract it targets is a clean GO.

A plan (`plan.yaml`) is a list of `steps`, each a discriminated `kind`. It is
loaded with the same size cap and anchor/alias rejection as a lockfile — a
plan file is trusted exactly as much as a contract.

## The four kinds

```yaml
steps:
  - kind: joint_target      # command a joint
    joint: shoulder_lift
    position: 1.2            # rad or m, optional
    velocity: 0.5             # optional
    effort: 40.0              # optional
  - kind: rail_load          # place extra draw on a rail
    rail: rail.pm02-5v
    draw: {value: 10, unit: W}
  - kind: set_power_mode     # switch a device to a declared mode
    device: dev.companion.jetson-orin-nano
    mode: "25W"
  - kind: publish            # a ROS message the agent intends to send
    topic: /imu/data
    msg_type: sensor_msgs/msg/Imu
    msg: {linear_acceleration: {z: 120.0}}
```

**`joint_target`** — checked against the joint's declared `lower/upper`,
`velocity`, and `effort` limits, and against the effort rating of whichever
device declares `drives_joint` for that joint (`torque_continuous ×
gear_ratio`; between continuous and `torque_peak × gear_ratio` is a warning,
not a violation). A `continuous` joint has no position to violate, so
`position` is not scored even when given; `velocity` and `effort` still are.

**`rail_load`** — extra draw placed on a rail. Every device already on that
rail is rolled up at its worst declared mode, every `rail_load` step on the
same rail in the plan is added (they accumulate across the whole plan, not
just the one step), and the total is checked against the rail's `capacity`.

**`set_power_mode`** — recomputes that rail's total with the named device at
the named mode (plus any `rail_load` steps), and checks it the same way.

**`publish`** — matched to the device whose `driver.topic` equals the step's
`topic`. Each of that driver's declared `signals` that resolves to a field
inside `msg` is compared, in its declared unit, against the device model's
`hard_limits` for that signal. A message field with no matching signal is
ignored — the plan is free to publish fields lockwire has no opinion about.

## What is scored against what

Every comparison uses a limit the contract actually declares. Anything the
contract does not declare is reported as not scored — never silently passed:
an unknown joint or rail, a joint missing the limit being checked, a device
with no driving actuator declared, a signal with no `hard_limits` entry, all
come back `not_scored` naming the gap. This is the whole-plan check that
per-call device limits leave open: `lockwire check` catches limits declared
wrong, `lockwire plan` catches a specific plan that would exceed limits
declared right.

Findings use lockwire's existing grammar (severity, contract-path `quantity`,
`observed`/`expected` prose, `action`) and the same verdict: any violation is
NO-GO (exit 1), warnings alone are GO with notes (exit 0), a plan assertion
error (bad file, unparseable plan) is REFUSED (exit 2), and a plan whose every
assertion lands on `not_scored` — nothing to compare — is NOT SCORED (exit 3): distinct from a clean pass so a script never reads "nothing compared" as
clearance.
