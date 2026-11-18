---
title: The blind-agent demo
description: "An agent that has never seen the robot reads its lockfile over MCP and checks its plan with lockwire plan before anything moves."
---

# The blind-agent demo

`examples/blind_agent_demo.py` shows what changes when an agent that has never seen a robot's
lockfile runs its plan through `lockwire plan` before sending it, instead of after.

The agent stays in charge of the robot; Lockwire only measures. The demo never writes to a
real or simulated actuator: it reads over MCP, calls the real `lockwire plan` command, and
prints what came back.

## What the agent knows going in

Nothing about the robot's lockfile. It is handed one device id at runtime, from
`examples/px4_sitl/agent_manifest.txt`, and makes exactly two calls over MCP:
`describe_system` and `describe_device`. Everything else it acts on (the topic, the message
type, the field, the hard limit) is read from those two Markdown documents at runtime, never
typed into the script.

The device id comes from a file because the demo's lockfile
(`examples/px4_sitl/px4_contract.yaml`) declares no `topology`, `budgets` or `joints`, so
`describe_system` lists no device ids for it. The MCP server also has a `list_devices` tool,
which returns every device's id, model and role; the demo keeps to the two describe tools.

## Three beats

1. **No gate.** The agent's first guess, a round number it tries before it has read anything,
   is "sent" (only printed, never actually sent). Nothing stops it. A real flight would learn
   the value was wrong only afterwards, from the log.
2. **`lockwire plan` first.** The same guess, scored by the real `lockwire plan` command
   before anything moves. It comes back **NO-GO**, with the observed number and the declared
   hard limit. It is not REFUSED: REFUSED (exit 2) means Lockwire would not evaluate the plan,
   and a scored violation is NO-GO (exit 1).
3. **The corrected plan.** Built from the true hard limit the agent read in beat 2, scored
   again: **GO**.

The script exits non-zero only if the safe plan fails, or if the wrong plan passes.

## The self-check: the script searches its own text

Before it does anything, the script scans its own source for the target lockfile's device
ids, unit string and hard-limit numbers. The banned strings are stored base64-encoded, so the
list does not match its own search. If any of them appears in the file, the script refuses to
run.

## What the self-check does not prove

The self-check reads only this one file. An identifier that reaches the agent through an
imported module or a data file would not be seen: a helper module with a hard-coded device id
would still report "clean". Closing that gap needs a walk over every module the script
imports, which this demo does not do.

The guard that keeps the demo to reading and planning has the same limit. It checks this
file's literal text: every quoted `call_tool(...)` name is one of the two describe tools,
every literal `subprocess.run([...])` list contains `"plan"`, and a short list of forbidden
words does not appear. It cannot see a command built at runtime, such as a tool name made from
character codes or an argument list joined from strings.

## Running it

```bash
uv run python examples/blind_agent_demo.py
```

It prints the three beats in order and exits 0 on the healthy path.
