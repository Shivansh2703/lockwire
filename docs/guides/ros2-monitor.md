---
title: ROS 2 continuous monitoring
description: Run the Lockwire gate on a running ROS 2 robot every few seconds and read the verdict on a latched /lockwire/status topic.
---

# ROS 2 continuous monitoring

One command, from the [`lockwire_ros` package](../../ros/lockwire_ros/README.md):

```bash
ros2 run lockwire_ros monitor --ros-args -p contract:=lockwire.yaml -p window_s:=10.0 -p period_s:=10.0
```

Every `period_s` it gates the last `window_s` seconds of the topics your contract's drivers
declare, and publishes the verdict.

## What it publishes

| topic | type | QoS |
|---|---|---|
| `/lockwire/status` | `diagnostic_msgs/DiagnosticStatus` | RELIABLE, TRANSIENT_LOCAL, depth 1 (latched) |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | default, depth 10 |

Levels: GO is OK, GO with notes is WARN, NOT SCORED is WARN, NO-GO is ERROR. If the gate
cannot run (no messages in the window, a contract field that does not resolve or is not a
number, the core missing, crashing or timing out), the status is ERROR and the message says
why; a bad field names the device, topic and field. The node keeps running through all of
these: it publishes every period, never only once, and ERROR clears when the cause does.

## Read it

```bash
ros2 topic echo --qos-durability transient_local --qos-reliability reliable /lockwire/status
```

Because the topic is latched, this shows the last verdict straight away, even if you start it
long after the monitor.

## Gate on it

The monitor holds nothing back by itself. A node that must stop on a bad verdict subscribes to
`/lockwire/status` with the same QoS and stops when the level is ERROR. For the start-time gate
(nothing starts until the robot passes), use `gate_node`; see the package README.
