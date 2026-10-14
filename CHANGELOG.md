# Changelog

## 0.1.0 — first public release

- The physics lockfile: a checked-in YAML contract for a robot's devices, frames, power and
  signals, validated by a strict schema.
- Deterministic checks, no simulation needed: power, thermal, bus, link and torque budgets.
- A static check of each device's declared envelope against its limits.
- Commands to change a contract (`add`, `place`, `move`, `declare`, `apply`), read it back in
  plain words (`describe`), and score a plan or a proposed change before anything moves (`plan`,
  `what-if`).
- An MCP server so an agent can read the contract and score plans over MCP.
- A self-contained HTML report.
- A GitHub Action and a pre-commit hook to verify the lockfile in CI.

The powered preflight gate, recording analysis and the desktop app are Lockwire Pro.
