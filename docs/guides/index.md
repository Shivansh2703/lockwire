---
title: Lockwire documentation
description: Guides for Lockwire, the physics lockfile for robots: check a model, lock it, run it in CI, and give AI agents the same checks.
---

# Lockwire documentation

```bash
uvx lockwire check robot.urdf
```

That checks your robot's model and prints a verdict. Lockwire checks that a robot's physical
facts add up: masses, inertias, joint limits, power, torque, buses. It locks them in
`lockwire.yaml` next to your model and checks every change against that lock, on your
machine, in CI and inside your AI coding agent.

## Guides, in the order you will need them

- [Quick start](../../README.md#check-your-robot): check a model, lock it, add CI, add your agent.
- [Using Lockwire](users.md): every flag of `check` and `start`, datasheets, verdicts, exit
  codes, xacro, the robot catalog, and checking a sim file against the lock.
- [CI](ci.md): the GitHub Action and every input, adopt mode, pre-commit.
- [AI coding agents](agents.md): MCP setup for each agent, the edit hook, the plugin, the rules.
- [Every check](checks.md): what each check compares, generated from the code.
- [The parts list](parts.md): cited and unverified parts, your own parts, adding one.
- [Part swap](part-swap.md): which servo can replace the one on a joint, and what it does to the joints above.
- [From Onshape](onshape.md): get an Onshape robot into a URDF, check it, and export it back out.
- [Servo recorder](servo-recorder.md): record Feetech and Dynamixel servos, read-only.
- [The MCP server](../mcp_server.md): the tools an agent can call.

The powered-on preflight gate, recordings, baselines, health, ROS 2 bags, fleet drift and the other Pro
commands (`lockwire help --all` lists them) are Lockwire Pro, waitlist only:
https://lockwirerobotics.com.
