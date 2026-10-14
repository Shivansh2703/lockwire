# The blind-agent demo

`examples/blind_agent_demo.py` shows what changes when an agent that has
never seen a robot's contract routes its plan through `lockwire plan` before
sending it, instead of after.

The principle this demonstrates: **the agent stays
the actuator and lockwire is the wire with a meter on it.** Forward
unchanged, never originate, never alter, never withhold. This demo never
writes to a real or simulated actuator — it only reads over MCP and
calls the real `lockwire plan` CLI, then prints what came back.

## What the agent knows going in

Nothing about the target contract. It is handed one device id at runtime by
`examples/px4_sitl/agent_manifest.txt` — analogous to what a fleet discovery
service would tell a real agent before it ever touches a contract — and
makes exactly two calls over MCP: `describe_system` and `describe_device`.
Everything else it acts on (the topic, the message type, the field, the
hard limit) is parsed out of those two Markdown documents at runtime, never
typed into the script.

## Three beats

1. **No gate.** The agent's first guess — a round number it tries before it
   has read anything — is "sent" (only printed; it is never actually sent). Nothing here stops it. A real flight would only learn the value was
   wrong afterward, from the log.
2. **`lockwire plan` first.** The same guess, scored by the real
   `lockwire plan` CLI before anything moves. It comes back **NO-GO**, with
   the observed number and the declared hard limit (the margin) — not
   REFUSED. REFUSED (exit 2) is reserved for a refusal *to evaluate*
   (e.g. a malformed plan); a scored violation is NO-GO (exit 1).
   Reporting a violation as REFUSED would be a misuse of that verdict.
3. **The corrected plan.** Built from the true hard limit the agent read in
   step 2, scored again — **GO**.

The script's own exit code is non-zero if the true contract's safe plan
fails, or if the wrong plan is wrongly passed — never on anything else.

## The self-check: greps itself clean

Before it does anything, the script scans its own source for the target
contract's device ids, unit string, and hard-limit numbers, base64-encoded
in the script so the banned words themselves never appear as plain text (a
plain string list would trip its own grep). If any decode to a substring
present in the file, the script refuses to run.

## What the self-grep does not prove (a named ceiling, not a hidden one)

The self-grep scans **only this one file's own text**. It cannot see an
identifier that reached the blind agent's code path through an **imported**
module or fixture instead of being typed directly into
`examples/blind_agent_demo.py` — a helper module that hardcoded a device id
and was merely imported here would still report "clean." Closing that hole
needs a check over the whole reachable code path (a bytecode/AST walk of
every module the script imports, not a text grep of one file), which this
demo does not do. `test_demo_script_greps_clean_of_px4_identifiers`
documents the same ceiling in its docstring.

The never-commands guard (`test_demo_never_commands_only_reads_and_plans`)
carries the identical ceiling and it earns the same honesty here. What it
checks is a static, literal-text scan of this one file: every quoted
`call_tool(...)` name is one of the two describe tools, every literal-list
`subprocess.run([...])` call contains `"plan"`, and a short forbidden-word
list does not appear. What it cannot see is a command assembled at runtime —
a tool name built from character codes instead of typed as a string literal,
an argv list built with `.format`/concatenation instead of a literal list, or
a device id reached through the manifest rather than through this file's own
text. Closing that gap needs the same whole-code-path analysis the self-grep
ceiling names (a bytecode/AST walk or runtime taint tracking), which this box
deliberately does not build — a static check here is the chosen scope, not an
oversight the guard's name should be allowed to paper over.

## The discovery gap

A device id has to come from somewhere before `describe_device(contract,
device_id)` can be called — describe_system's own Markdown does not list
device ids (it only exists on this contract's page if `topology`/`budgets`/
`joints` are declared, none of which this fixture has). Rather than
hardcoding a device id in the script (which the self-grep would catch, and
which would defeat the point), this demo reads it from
`examples/px4_sitl/agent_manifest.txt` at runtime, standing in for a fleet
discovery service. This is a real, named gap in the "only two MCP tools"
design, not a workaround pretending otherwise: a production blind agent
needs a third source for "which device," whether that is a manifest, a
`list_devices` MCP call, or an operator prompt.

## Running it

```bash
uv run python examples/blind_agent_demo.py
```

Prints the three beats in order, exits 0 on the healthy path.

```bash
uv run pytest -q tests/test_blind_agent_demo.py
```
