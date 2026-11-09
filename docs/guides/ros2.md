---
title: ROS 2 with Lockwire
description: Point Lockwire at a ROS 2 bag you already have, declare which topics are which device in three lines, and gate the bag. Then run the same gate live.
---

# ROS 2 with Lockwire

Start from a bag you already have. No ROS install is needed for this part: Lockwire reads
rosbag2 MCAP files (the default since Iron) on a laptop.

```bash
pip install 'lockwire[ros2bag]'
```

## 1. See what the bag holds

```bash
lockwire bag inspect my_bag/          # a bag folder or one .mcap file
lockwire bag inspect --example        # a bundled sample bag, no robot needed
```

It lists each topic with its message type, message count and mean rate, and prints the
driver line to write for it:

```text
  driver: {topic: /joint_states, msg_type: sensor_msgs/msg/JointState, signals: {NAME: {field: "position[name=hip_joint]", unit: UNIT}}}
```

Add `--json` for the same inventory as JSON.

## 2. Write three lines per device

In `lockwire.yaml`, give each device you want gated a `driver` block: the topic, the message
type, and the signals. Each signal is a field path in the message plus the unit you type.
Lockwire never guesses a unit.

```yaml
driver:
  topic: /joint_states
  msg_type: sensor_msgs/msg/JointState
  nominal_rate_hz: 50.0
  signals:
    knee_position: {field: "position[name=knee_joint]", unit: rad}
```

Field paths:

| Path | Reads |
|---|---|
| `linear_acceleration.z` | a nested field |
| `effort[0]` | an array entry by index |
| `position[name=knee_joint]` | the entry whose index matches `knee_joint` in the sibling `name` array (JointState), so a driver that reorders joints still reads the right one |

A path that does not resolve in the bag is an error, never a default. A driver that publishes
an empty `effort` array gets a refusal saying so, not a zero.

## 3. Gate the bag

```bash
lockwire bag check my_bag/ --lock lockwire.yaml
lockwire bag check --example          # the sample bag and its contract: GO
```

`bag check` is `lockwire preflight lockwire.yaml --from my_bag/`: the same gate, verdicts and
exit codes. See [Preflight](preflight.md).

SQLite3 (`.db3`) bags are refused with the exact `ros2 bag convert` command that turns them
into MCAP.

## Next: live

- [Live: one command gates the robot as it runs](ros2-live.md):
  `ros2 run lockwire_ros preflight lockwire.yaml --seconds 10`.
- `lockwire preflight lockwire.yaml --live --container my-robot` samples the running robot's
  topics and gates them the same way. See [Preflight](preflight.md#run-the-gate).
- The [`lockwire_ros` package](../../ros/lockwire_ros/README.md) runs the gate inside your ROS 2
  launch, with a capture node that uses the same field paths.
- [Continuous monitoring](ros2-monitor.md): `ros2 run lockwire_ros monitor` gates the last few
  seconds every period and publishes the verdict on a latched `/lockwire/status`.
