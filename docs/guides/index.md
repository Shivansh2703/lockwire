---
title: Lockwire documentation
description: "Guides for Lockwire, the physics lockfile for robots: check a model, lock it, run it in CI, and give AI agents the same checks."
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

- [Quick start](../QUICKSTART.md): check a model, lock it, add CI, add your agent.
- [Using Lockwire](users.md): every flag of `check` and `start`, datasheets, verdicts, exit
  codes, xacro, the robot catalog, and checking a sim file against the lock.
- [CI](ci.md): the GitHub Action and every input, adopt mode, pre-commit.
- [AI coding agents](agents.md): MCP setup for each agent, the edit hook, the plugin, the rules.
- [Every check](checks.md): what each check compares, generated from the code.
- [The parts list](parts.md): cited and unverified parts, your own parts, adding one.
- [Closed loops](closed-loops.md): which linkages are scored and which are not yet.
- [Part swap](part-swap.md): which servo can replace the one on a joint, and what it does to the joints above.
- [From Onshape](onshape.md): get an Onshape robot into a URDF, check it, and export it back out.
- [Servo recorder](servo-recorder.md): record Feetech and Dynamixel servos, read-only.
- [The MCP server](../mcp_server.md): the tools an agent can call.
- [Plan validation](../plan_validation.md): score an agent's plan against the lockfile before it runs.
- [Device pages](../describe.md): `lockwire describe` writes one plain page per device, for you or an agent.
- [Words](../words.md): what each verdict, severity and exit code means.
- [The blind-agent demo](../blind-agent-demo.md): an agent that reads the lockfile over MCP and checks its plan first.

The powered-on preflight gate, recording checks, baselines, health, ROS 2 bags, fleet drift and the other Pro
commands (`lockwire help --all` lists them) are Lockwire Pro, waitlist only:
https://lockwirerobotics.com.

## Reference

- [Reference robot specs](../reference_robot_specs.md): the published figures behind the `drone` example.
- [Webots robot masses](../reports/webots-masses.md): the masses in Webots' robot models against their makers' figures.
