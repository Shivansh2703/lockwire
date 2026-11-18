---
title: AI coding agents
description: Give Claude Code, Cursor or Codex CLI Lockwire's checks, rules and edit hook, so an agent cannot quietly break your robot's physics.
---

# AI coding agents

An agent editing a URDF can change a mass or a joint limit without noticing. Lockwire gives
it three things: the checks as MCP tools, three rules in its instruction file, and (in Claude
Code) an edit hook that blocks a change that breaks the lock.

## What each agent gets

| | MCP tools | Rules file | Edit hook |
| --- | --- | --- | --- |
| Claude Code | yes | `CLAUDE.md` | yes (`agent-setup --claude` or the plugin) |
| Cursor | yes | `AGENTS.md` | no |
| Codex CLI | yes | `AGENTS.md` | no (see below) |

Lockwire's MCP server offers tools only, so every agent above gets the same tools. The agent's
`tools/list` is the live list; the [MCP page](../mcp_server.md) describes each one. To check
a model with no lockfile, an agent calls `check_model(path="robot.urdf")`.

## Claude Code

In your robot's repo, with its model in `robot.urdf`:

```bash
claude mcp add lockwire -- uvx --from "lockwire[mcp]" lockwire mcp
lockwire start robot.urdf
lockwire agent-setup --claude
```

The second line locks the model's physics in `lockwire.yaml`. The third writes the rules, a
pre-commit hook, a CI workflow, and the edit hook in `.claude/settings.local.json`. Before Claude Code
writes or edits a URDF, SDF or `lockwire.yaml`, `lockwire hook-check` checks the new content
against the lock. If it breaks the locked physics, the edit is blocked and the agent sees why.

To try it, save the
[SO-101 arm model](https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/a758567c3978dfeefe282ede0500085a48fe8f78/Simulation/SO101/so101_new_calib.urdf)
as `robot.urdf` in an empty folder, run the last two lines above, and ask Claude Code to set the
base link mass to 9.2. The edit is blocked with:

```text
lockwire: this edit to robot.urdf breaks the locked physics. Undo it, or ask a human to re-lock:
VIOLATION model/links/base_link/mass: model file has mass 9.2 kg, expected locked mass 0.147 kg
  the model changed: a person must review it and re-lock, not you
```

The hook runs the Lockwire of the Python that ran `agent-setup`. If that Python is gone, or
you ran `agent-setup` through `uvx`, it uses a `lockwire` on your PATH, else `uvx lockwire`, so it
keeps working after uv's cache is cleaned. It uses each one only if it runs (`--version` works),
so a stale launcher left by an old install is skipped. If none runs, a model edit is blocked with
one line:

```text
lockwire could not be run, so this robot-model edit is blocked: install or repair it (uv tool install lockwire, then lockwire agent-setup --claude) and retry, or remove the PreToolUse entry with lockwire hook-check from .claude/settings.local.json
```

The hook goes in `.claude/settings.local.json`, Claude Code's file for this machine
only, because it can name a Python on your machine: add that file to `.gitignore` and commit the
other files `agent-setup` writes; each teammate runs `lockwire agent-setup --claude`. Claude Code
reads hooks from both `.claude/settings.json` and `.claude/settings.local.json`. A hook an older
Lockwire wrote into `.claude/settings.json` is moved out the next time you run
`lockwire agent-setup --claude`, which prints
`moved the Lockwire edit hook out of .claude/settings.json into .claude/settings.local.json` and
leaves the rest of that file as it was. If you edited that file by hand, it is left alone and the
run names the entry to delete instead. If Lockwire can't check an edit to a URDF, SDF or `lockwire.yaml`, the edit is blocked with one line naming the error; edits to other files always
go through. To remove the hook, delete the `PreToolUse` entry whose command contains
`lockwire hook-check`.

### The plugin

The `plugin/` folder in the Lockwire repo is a Claude Code plugin with all of it in one
place: the MCP server, the edit hook, a `/lockwire` skill that teaches the rules, and a
read-only `robot-reviewer` agent that runs the checks before a merge and changes nothing.
It has no marketplace entry yet. To use it, clone the repo and start Claude Code with
`claude --plugin-dir path/to/lockwire/plugin`; it loads for that session only.

## Cursor

Put this in `.cursor/mcp.json` in your repo (or `~/.cursor/mcp.json` for every project):

```json
{
  "mcpServers": {
    "lockwire": { "command": "uvx", "args": ["--from", "lockwire[mcp]", "lockwire", "mcp"] }
  }
}
```

Then run `lockwire agent-setup` for the rules in `AGENTS.md`, the pre-commit hook and CI.

## Codex CLI

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.lockwire]
command = "uvx"
args = ["--from", "lockwire[mcp]", "lockwire", "mcp"]
```

Then run `lockwire agent-setup` for the rules in `AGENTS.md`, the pre-commit hook and CI.
Codex CLI has its own pre-tool hooks, but `lockwire hook-check` reads Claude Code's edit
format, so in Codex the pre-commit hook and CI catch a bad edit instead.

## The three rules

`agent-setup` writes these into the agent's instruction file:

1. **Check before editing a model file.** Run `lockwire check` before touching a URDF, SDF
   or `lockwire.yaml`.
2. **Plan before any motion.** Write the motion as a plan and run
   `lockwire plan lockwire.yaml plan.yaml` (or the `validate_plan` tool). Only a GO plan runs.
3. **Never change a declared value without a human witness.** A declared value (a datasheet
   mass, a measured torque) is evidence a person stands behind. Change one only when a human
   gives the new value and their name:

   ```bash
   lockwire declare lockwire.yaml <path> "<value>" --source "<where>" --witness "<name>"
   ```

   The path is `/`-separated, for example, for a device `dev.pi5` in your lock:

   ```bash
   lockwire declare lockwire.yaml devices/dev.pi5/power/draw "5 W" --source "bench meter" --witness "Your Name"
   ```

   An agent never picks a witness name itself, and never re-locks (`lockwire start --force`)
   to make a check pass. Lockwire refuses a blank witness, "you", or an AI assistant's name.

`agent-setup` only adds what is missing; running it again changes nothing.

See also: [the MCP server](../mcp_server.md), [CI](ci.md).
