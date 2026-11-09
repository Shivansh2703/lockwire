---
title: Using Lockwire
description: Check a robot model, compare it with a datasheet, lock it, read the verdict and exit codes, and check a sim file against the lock.
---

# Using Lockwire

Every command here works with `uvx lockwire ...` (nothing installed) or `lockwire ...`
after `pip install lockwire`.

## Check a model file

```bash
lockwire check robot.urdf
```

`check` takes a `.urdf`, `.xacro` or `.sdf` file as well as a lockfile. On a model file it
locks it in a temporary place, runs the model checks and prints the verdict. Nothing is
written next to your model. Pass `--lock path/to/lockwire.yaml` to keep that lock instead.

No URDF handy? `uvx --from "lockwire[robots]" lockwire check ur5e` checks a public robot by
name from `robot_descriptions` (`go2`, `panda`, ...); a real file of that name always wins.

## Compare with a datasheet

```bash
lockwire check robot.urdf --mass "20.6 kg" --source "https://vendor.example/datasheet.pdf" --tolerance-pct 5
```

- `--mass`: the datasheet's total mass, with its unit.
- `--source`: where that figure comes from: a URL or a cited title.
- `--tolerance-pct`: how far the model may differ, in percent (default 5).
- `--witness`: who stands behind the figure (default: you).

`start` takes the same four flags, so the figure goes into the lockfile with its source.

### The robot catalog

Lockwire ships the datasheet figures of popular robots, each with its source:

```bash
lockwire catalog robots
```

`check` recognises a catalog robot by its name or its link names and compares the model with
the vendor's figure. If it does not recognise yours, name it: `--model ur5e`.

## Lock it

```bash
lockwire start robot.urdf
```

This writes `lockwire.yaml` and then runs the check (`--no-check` skips it). An existing
lockfile is kept; `--force` starts over from the model and discards what was declared in it
by hand. `--out` writes the lockfile somewhere else. `lockwire start --example drone` writes
a ready example instead (also `rover`, `arm`, `quadruped`, `humanoid`).

Then check the lockfile, locally and in CI:

```bash
lockwire check lockwire.yaml
```

A mass, centre of mass, inertia or joint limit edited in the model after locking is a
NO-GO naming the value.

Re-running `lockwire start --force` on the same model is destructive:
re-declare every fact in the lockfile by hand, because no command carries those facts forward.
Any field you hand-edited (including `robot.name`) is silently overwritten back to the URDF-derived value.

A lockfile whose `schema_version` this lockwire does not recognise is refused before any check
runs: `check` prints `REFUSED — contract declares schema_version '0.99'; this lockwire supports 0.2, 0.3, 0.4, 0.5`
and exits 2. A lockfile at an older supported version is accepted as-is, with no automatic upgrade.

## Xacro templates

A `.xacro` file, or a `.urdf` that is really a xacro template, is expanded first. Pass its
arguments with `--xacro-arg`, once per argument:

```bash
lockwire check arm.urdf.xacro --xacro-arg name=myarm --xacro-arg arm_type=ur5e
lockwire start arm.urdf.xacro --xacro-arg name=myarm --xacro-arg arm_type=ur5e
```

## Read the verdict

- **GO**: everything that could be compared adds up.
- **GO with notes**: nothing failed, but there are warnings (a link with no mass, say).
- **NO-GO**: something does not add up. Each violation shows the value observed, the value
  expected and what to do.
- **NOT SCORED**: nothing could be compared, so nothing is claimed.

`check` prints the violations in full and counts the warnings; `--all` prints every finding
and the checks that passed.

Under every verdict, **Not compared yet** lists the checks that had nothing to judge, with
the reason. That is the honest part: a check Lockwire could not run is never counted as
passed.

Most findings end with a `run:` line, and the report ends with a `->` line: the exact
command to run next. In JSON (`--json -` prints one JSON document on stdout, the human
report goes to stderr) the same command is the finding's `next_command`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | GO, or GO with notes |
| 1 | NO-GO |
| 2 | the file could not be found, read or parsed |
| 3 | NOT SCORED: nothing could be compared |

To make unjudged checks count:

- `--max-not-scored N` exits 3 when more than N checks went unjudged.
- `--strict` treats warnings as failures (GO with notes exits 1) and exits 3 on any unjudged
  check.

## Check a sim file against the lock

Your MuJoCo or Gazebo file can drift from the URDF you locked: a vendor updates the URDF and
the sim file keeps the old masses. `check-sim` reads a MuJoCo (MJCF) or SDF file beside the
lock and compares it value by value: masses, centres of mass, inertia, joint limits, effort,
damping, friction and armature.

```bash
lockwire check-sim robot.xml
```

Without `--lock` it uses the `lockwire.yaml` beside the sim file, else the one in the current
folder. When the sim file agrees it prints one line, such as
`lockwire: robot.xml agrees with lockwire.yaml (23 values compared)`, and exits 0. When it
disagrees it names each mismatch and exits 1. `--verbose` prints every detail even when it agrees,
including values the lock holds but the sim format has no field for (a battery, MJCF joint
velocity limits), listed under "Sim cannot express". Exit codes are the same as `check`.
It never runs a simulation, and agreement does not mean the sim behaves like the real robot.

**Check before every sim launch.** Put it in front of the launch command, so a drifted sim file
stops the launch:

```bash
lockwire check-sim robot.xml && python -m mujoco.viewer --mjcf robot.xml   # MuJoCo
lockwire check-sim robot.sdf && ros2 launch my_robot sim.launch.py          # ROS 2 / Gazebo
```

The check took about half a second on the SO-101 fixture. The two launch lines were not run here
(no MuJoCo or ROS 2 on the machine that wrote this); swap in your own launch command.

## Plan a motion

Before a robot (or an agent) moves, write the motion as a plan and check it:

```bash
echo 'steps: [{kind: joint_target, joint: hip, effort: 3.0}]' > plan.yaml
lockwire plan lockwire.yaml plan.yaml
```

A plan that asks a joint for more than its limit is a NO-GO, before anything executes.

## More commands

`lockwire --help` lists the nine everyday commands. These also work but are not listed there:
`init`, `upgrade`, `check-sim`, `describe`, `export`, `scan`, `hw`, `review`, `place`,
`mount-shape`, `move`, `rotate`, `reparent`, `apply`, `add`, `gearbox`, `agent-setup`, `log`,
`blame`, `scene`, `catalog`. Run `lockwire <command> --help` for any of them.

See also: [every check](checks.md), [CI](ci.md), [AI agents](agents.md).
