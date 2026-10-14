# `lockwire mcp`

An MCP server that lets an agent ask lockwire whether a contract is consistent, what a
device is, and whether its plan is safe — before it touches hardware. Every tool runs
lockwire's existing checks and returns their findings as structured JSON; nothing here
recomputes a verdict or infers anything new.

## Run it

```bash
uv sync --extra mcp   # the mcp SDK is optional; a plain install doesn't carry it
lockwire mcp           # speaks MCP over stdio
```

Without the extra, `lockwire mcp` exits 2 and prints `install the mcp extra: uv sync --extra mcp`.

## Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "lockwire": {
      "command": "/absolute/path/to/lockwire",
      "args": ["mcp"]
    }
  }
}
```

Use an absolute path to the `lockwire` entry point (`which lockwire` after `uv sync`) — MCP
clients launch this command without your shell's `PATH` or virtualenv activated.

## Tools

- **`check_contract(contract)`** — record-level checks: do the records in the contract agree?
- **`describe_device(contract, device_id)`** — one device's Markdown reference (role, measures,
  adjusts, limits, provenance). Unknown device id → an error result.
- **`describe_system(contract)`** — the system-level Markdown reference: rails, buses, budgets,
  joints.
- **`validate_plan(contract, plan)`** — an agent's intended plan, in the same shape as
  `plan.yaml`, scored against the contract before anything executes. A malformed plan → an
  error result.
- **`preflight(contract, recording)`** (Lockwire Pro) — the same go/no-go run `lockwire preflight --json`
  writes, against a recorded source.
- **`list_devices(contract)`** — every device's id, model, and derived role.

All six take `contract` as a path resolved against the server's working directory, and refuse
one that doesn't exist. `check_contract`, `validate_plan`, and `preflight` return
`{"verdict", "exit_code", "findings"}`; a bad input (unknown device, malformed plan, missing
recording) comes back as an MCP error result with a plain-text message, never a stack trace.

## Example: `validate_plan`

Call:

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

Response (each rail load fits alone but not together — the two do not just sum, they roll up
through the rail's efficiency, which is why 950 W of draw reads back as 888 W of capacity):

```json
{
  "verdict": "NO-GO",
  "exit_code": 1,
  "findings": [
    {
      "severity": "violation",
      "quantity": "plan/steps/0/rail_load/draw",
      "observed": "950.00 W rolled-up draw on rail.servo-motor",
      "expected": "<= 888.00 W capacity",
      "action": "shed load on rail 'rail.servo-motor' or raise its capacity"
    }
  ]
}
```

The reference file and the plan check are the two things an agent-hardware layer needs from
the physical side; both are generated from the same verified contract.
