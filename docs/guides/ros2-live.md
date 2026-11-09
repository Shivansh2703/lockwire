---
title: Live ROS 2 preflight in one command
description: Gate a running ROS 2 robot with one ros2 command, or put the gate in front of your bringup launch file so nothing starts on a NO-GO.
---

# Live ROS 2 preflight in one command

`lockwire_ros` is a small ROS 2 package that subscribes to the topics your `lockwire.yaml`
declares, records a few seconds, and runs `lockwire preflight` on what it heard. It works
on Humble, Jazzy, Kilted and Lyrical.

```bash
ros2 run lockwire_ros preflight lockwire.yaml --seconds 10
```

Exit code: `0` GO, `1` NO-GO, `2` the gate could not run (no topic published, core
missing, bad contract), `3` nothing was scored. Everything but `0` is a refusal.

## Install

1. The core, in its own Python 3.12+ environment: `pip install lockwire`.
2. The ROS package, built in your workspace (it is not in a rosdistro yet):

```bash
cp -r lockwire/ros/lockwire_ros ~/ws/src/
cd ~/ws && colcon build --packages-select lockwire_ros && . install/setup.bash
```

The package never imports `lockwire`; it runs the `lockwire` executable, so it works on
Humble's Python 3.10.

## Tell it which topics to read

Each device that should be checked live needs a `driver` block:

```yaml
devices:
  - id: dev.imu
    driver:
      node: /imu_driver
      topic: /imu/data
      msg_type: sensor_msgs/msg/Imu
      nominal_rate_hz: 100.0
      signals:
        accel_z: {field: linear_acceleration.z, unit: m/s^2}
```

A signal is a numeric field reached by a path. Array fields work too, on
`sensor_msgs/msg/JointState` for example: `position[2]` by index, or
`position[name=knee_joint]` by joint name. The field path syntax is in
[ROS 2 with Lockwire](ros2.md).

A topic that is silent for the whole window is named in the log. If every declared topic
is silent, the gate refuses with exit 2: silence is never a pass.

## Run it

```bash
ros2 run lockwire_ros preflight lockwire.yaml --seconds 10 --context bench-idle
```

`--context` is `bench-idle`, `armed-idle` or `nominal-op` and picks which envelopes
apply. `--out DIR` keeps the recording (and the run's `lockwire-preflight.json` beside
it), `--json` prints the core's JSON document, and `--profile` / `--require-evidence`
pass straight to the core.

Each run appends its result to the robot's health ledger, `lockwire.health.jsonl`, next to
the contract file (the same ledger `lockwire preflight` writes), and prints that path
(`health: ... -> ...`; not with `--json`). Add `--no-ledger` to keep a trial run out of it.

## Gate your bringup

Nothing in your bringup starts until the gate says GO, and a refusal makes `ros2 launch`
exit nonzero:

```bash
ros2 launch lockwire_ros lockwire_gate.launch.py contract:=lockwire.yaml \
    live:=true capture_s:=10 bringup:=/path/to/bringup.launch.py
```

From Python, `gate_bringup(on_pass=[...], contract="lockwire.yaml", live=True, capture_s=10)`
in `lockwire_ros.launch_gate` does the same thing; the package
[README](../../ros/lockwire_ros/README.md) has a ten-line example. Include the gate with
`bringup:=` (or `on_pass`), not next to your nodes: a bare include still fails the launch
on NO-GO, but sibling actions would start alongside the gate instead of after it.

Plain `ros2 launch` would report success even when the gate refused
([ros2/launch#586](https://github.com/ros2/launch/issues/586)); the gate raises inside the
launch loop so the exit code is right.
