---
title: The MCP server
description: Give an AI agent Lockwire's checks over MCP, so it can check a robot and its plan before anything moves.
---

# The MCP server

`lockwire mcp` serves Lockwire's checks to an AI agent over MCP (stdio). Every tool runs
Lockwire's own checks and returns their findings as JSON; nothing here makes up a verdict.

## Add it

Claude Code, one line, nothing to install first:

```bash
claude mcp add lockwire -- uvx --from "lockwire[mcp]" lockwire mcp
```

Claude Desktop (`claude_desktop_config.json`) and Cursor (`.cursor/mcp.json` in the repo, or
`~/.cursor/mcp.json`) take the same JSON:

```json
{
  "mcpServers": {
    "lockwire": { "command": "uvx", "args": ["--from", "lockwire[mcp]", "lockwire", "mcp"] }
  }
}
```

Already installed with `pipx install "lockwire[mcp]"`? Use command `lockwire`, args `["mcp"]`.
If the app cannot find `uvx` or `lockwire`, give the full path from `which uvx`. Without the
`mcp` extra, `lockwire mcp` exits 2 and says which extra to add.

Then ask the agent: "Check my robot's lockwire.yaml and explain the NO-GO."

## Tools

- **`check_contract`**: run every check on a lockfile; returns the verdict, exit code and findings, each with a `next_command` where one applies.
- **`check_model`**: check a URDF, xacro or SDF on its own, no lockfile needed, optionally against a datasheet mass. Writes nothing. `path` (required) is the model file; `model` is an optional `list_robots` id to compare with, recognised from the file when left out.
- **`check_sim_config`**: compare a MuJoCo (MJCF) or SDF sim file with the lock, value by value. Never runs a simulation.
- **`list_robots`**: the catalog robots whose vendor datasheet the checks compare with.
- **`list_parts`**: the catalog parts (private ones too) with their sourced numbers, then the stock shapes (`kind` "stock": `recipe`, `dimensions`, `named_dimensions`, `mass_basis`, `material_required`); `query` words narrow the list.
- **`upgrade_contract`**: write a current-format lockfile from an older one, leaving the old file unchanged.
- **`export_model`**: write the contract's robot model as a URDF, SDF or MJCF file beside the contract (`format`, default `urdf`); refuses to overwrite an existing file. Those three carry no geometry. `format` `stl`, `obj` or `dae` writes a `<name>-<format>` folder beside the contract instead: one binary STL, OBJ or COLLADA file per shaped link plus an `assembly` file at the zero pose, in metres; meshes that are not STL are listed in `not_exported`.
- **`compare_proposal`**: what a proposed change would do to the checks, without writing the lockfile.
- **`describe_device`**: one device's reference: role, measures, limits, where each value came from.
- **`describe_system`**: the whole robot's reference: rails, buses, budgets, joints.
- **`validate_plan`**: score an intended plan (same shape as `plan.yaml`) before it runs.
- **`list_devices`**: every device's id, model and role.
- **`review_value`**: approve or question one declared value, with a note and a human witness.
- **`add_link`**: add a link (a shape with a mass) to the robot model. A proposal (a diff, nothing written) until you pass `apply=true`; needs a `source` and a real person as `witness`; `expected_revision` refuses a stale file.
- **`add_joint`**: join two existing links with a joint. Same proposal, `apply`, witness and revision rules; refuses a second parent or a loop.
- **`add_device`**: add a device to the lockfile. Same proposal, `apply`, witness and revision rules.
- **`add_shape`**: add one box, cylinder, sphere or mesh shape to an existing link. A proposal (a diff, nothing written) until you pass `apply=true`; needs a `source` and a real person as `witness`; `expected_revision` refuses a stale file.
- **`add_param`**: declare a named dimension (value, optional unit, min and max) that shapes and joint origins can use by name. Same proposal, `apply`, witness and revision rules; refuses a duplicate name or a value outside min..max.
- **`set_dimension`**: set an existing named dimension to a new value; every shape and joint using it follows. Same rules; refuses an unknown name or a value outside min..max.
- **`attach_part`**: attach a catalog part to a link at a given frame, joined by a joint you name; no offset or limit is guessed. Same rules; refuses an unknown part or link or a missing frame.
- **`delete_part`**: delete a link, joint or device. Same rules; refuses a link a joint still holds. Then `export_model` writes the URDF.
- **`rename`**: rename a link, joint, device, rail, wire, param or group and every reference to it in one write. Same proposal, `apply`, witness and revision rules; refuses an unknown name or one already taken.
- **`match_parts_list`**: match a pasted parts list to the catalog. Writes nothing.
- **`swap`**: rank catalog servos for one joint by its real gravity load and list the joints above it, load before and after ([Part swap](guides/part-swap.md)). Previews by default; writes only with a `part_id` and an `expected_revision`.

The free server has 24 tools, all listed above. The Pro build adds 22 more (for example `preflight`, `health`, `prepare_actuator_review_draft`, `plate_drawing`, `tip_sweep`); Pro is waitlist only
(https://lockwirerobotics.com) and none of them is part of free lockwire.

Paths are resolved against the server's working directory, and a path that does not exist
is refused. Every parameter carries a description in `tools/list`. Findings come worst first
(violation, warning, not scored), and a finding leaves out the fields it has no value for. A bad input (unknown device, malformed plan) comes back as an MCP error with a
plain message, never a stack trace.

## Example: `validate_plan`

Two loads planned on the servo rail of the x500 drone example (in Lockwire's test fixtures):

```json
{
  "contract": "tests/fixtures/x500/contract.yaml",
  "plan": {
    "steps": [
      {"kind": "rail_load", "rail": "rail.servo-motor", "draw": {"value": 500, "unit": "W"}},
      {"kind": "rail_load", "rail": "rail.servo-motor", "draw": {"value": 450, "unit": "W"}}
    ]
  }
}
```

The planned draw rolls up with what the rail already carries, through the rail's
efficiency, and lands over its capacity. The answer, shortened to one finding:

```json
{
  "verdict": "NO-GO",
  "exit_code": 1,
  "findings": [
    {
      "severity": "violation",
      "quantity": "plan/steps/0/rail_load/draw",
      "observed": "1196.68 W rolled-up draw on rail.servo-motor",
      "expected": "<= 888.00 W capacity",
      "action": "shed load on rail 'rail.servo-motor' or raise its capacity"
    }
  ]
}
```

## Rules, edit hook and plugin

- `lockwire agent-setup` writes the three Lockwire rules into `AGENTS.md` and `CLAUDE.md`,
  plus a pre-commit hook and a CI workflow. Running it again changes nothing.
- `lockwire agent-setup --claude` also adds an edit hook to `.claude/settings.json`: before
  Claude Code edits a URDF, SDF or `lockwire.yaml`, `lockwire hook-check` checks the new
  content and blocks an edit that breaks the locked physics.
- The `plugin/` folder in the Lockwire repo is a Claude Code plugin: the same MCP server, the
  edit hook, a `/lockwire` skill and a read-only robot reviewer. Load it with
  `claude --plugin-dir path/to/lockwire/plugin` (that session only; no marketplace entry yet).

More in [the agents guide](guides/agents.md).
