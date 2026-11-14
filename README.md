<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/logo-dark.svg">
  <img src="docs/img/logo-light.svg" alt="lockwire" height="64">
</picture>
</p>

**From idea to robot: catch the error in your robot model before the real robot does.**

## Check your robot

Needs Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Point it at your own URDF, xacro or
SDF (nothing is written):

```bash
uvx lockwire check robot.urdf
```

No robot file yet? This writes a small example arm, with its power and wiring, into the current folder
and checks it:

```bash
uvx lockwire start --example arm-power
uvx lockwire check
```

```console
VIOLATION topology/rails/rail.5v/brownout — observed rail.5v sags to 4.65 V with every servo stalled (2.73 A), expected >=
4.75 V, the declared minimum input of dev.jetson
  action: shorten or thicken the wire, cut the pack resistance, or move the
servos to their own rail
VIOLATION topology/wires/w.pack/fuse — observed 9.00 A fuse on 22 AWG wire w.pack, expected fuse <= 7.00 A wire
ampacity
  ...
  action: fit a fuse at or below 7.00 A or upsize wire 'w.pack'
...
NO-GO — 2 violation(s), 2 warning(s)
```

Command line: macOS, Linux and Windows. Desktop app: macOS (Apple silicon) and Linux. Windows app coming soon.

`check` exits 1 on a NO-GO. Two real errors in a robot you have not built yet: with both servos stalled
the 5 V rail sags below the Jetson's minimum input and resets it, and a 9 A fuse sits on wire that
carries 7 A. Each line names the part and the page its limit came from, and says what to change
(`lockwire check --all` also shows the two warnings: a servo data wire on a boot pin, and a 28 AWG
servo feed drawing about 5 A). Lockwire found these without being told. Errors like this are in public
robot models today, and maintainers at Seeed and DFKI (MiR100) have merged the fixes:
[the fixes sent upstream](docs/ledger.md).

## Lock it

```bash
uvx lockwire start robot.urdf
```

This writes `lockwire.yaml`, the robot's physics lockfile: every link's mass, centre of mass and
inertia, every joint limit, and a hash of the model's structure. Commit it next to your model.
From now on `uvx lockwire check` checks the lockfile, and a mass or joint limit edited in the
model without re-locking is a NO-GO that names the value.

## Run it in CI

```bash
uvx lockwire agent-setup --ci
```

That writes `.github/workflows/lockwire.yml`. Its step, with the three inputs most repos need:

```yaml
- uses: actions/checkout@v4
- uses: Shivansh2703/lockwire@v0
  with:
    contract: lockwire.yaml    # the lockfile
    # model: robot.urdf        # or check a model directly, with no lockfile
    # adopt: "true"            # annotate and report, but never fail the job
```

A NO-GO fails the build, with annotations on the pull request and an HTML report.
Every input, pre-commit and other CI systems: [docs/guides/ci.md](docs/guides/ci.md).

## Let your AI agent check it

```bash
claude mcp add lockwire -- uvx --from "lockwire[mcp]" lockwire mcp
uvx lockwire agent-setup
```

The first line gives Claude Code the checks over MCP. `agent-setup` writes the agent rules into
AGENTS.md and the Claude Code one, plus a pre-commit hook and the CI workflow.
Cursor, Claude Desktop and other agents: [docs/guides/agents.md](docs/guides/agents.md).

## No robot file? Try an example

servo-bench is built to fail: a 5 V rail overloaded and a wrist servo too weak, on purpose, so you can see what a catch looks like.

```console
$ uvx lockwire start --example servo-bench
wrote lockwire.yaml — example servo-bench (built to fail: a 5 V rail overloaded and a wrist servo too weak, on purpose, so you can see what a catch looks like). Next: uvx lockwire check lockwire.yaml
$ uvx lockwire check
VIOLATION model/joints/wrist/limits/effort — observed 0.40 N*m joint effort limit, expected <= 0.226 N*m, the feetech.scs0009
stall torque (Δ 1.8x the stall torque)
  ...
  action: ... or find a stronger servo: lockwire swap lockwire.yaml wrist
VIOLATION budgets/bud.power.servo-5v — observed 32.10 W, expected <= 15.00 W (Δ +17.10 W (+114%))
  ...
NO-GO — 2 violation(s), 0 warning(s)
```

The examples that pass the check (`--example drone`, `rover`, `arm`, `quadruped`, `humanoid`) are good
starting points; `arm-power` and `servo-bench` show a catch, and so will your own robot.

### Features

- **Check:** see which physical numbers in your model do not add up, and the command that fixes each one.
- **Lock:** keep those numbers in `lockwire.yaml`, so any drift is caught.
- **CI:** the same check on every push, with pull-request annotations and an HTML report.
- **Agents:** your AI coding agent runs the same check before it changes the robot.

Everything else (datasheet comparisons, power, torque and bus budgets, part swap, the servo
recorder, drift on a single robot, the Python library): [the guides](docs/guides/index.md). The powered-on
preflight gate, ROS 2 bags and the drift ledger across units are Lockwire Pro, waitlist only: https://lockwirerobotics.com.

## Download the app

The free desktop app does the same visually: [latest release](https://github.com/Shivansh2703/lockwire/releases/latest).

![Open the app, pick the Rover example, and the Check screen shows the TurtleBot3 in 3D with a GO with notes verdict: 0 problems, 4 checks not compared yet](docs/img/try-example.gif)

- **macOS (Apple silicon):** unzip, open Lockwire. The app is not notarized, so the first open warns: open System Settings, Privacy & Security, and click "Open Anyway".
- **Linux:** `chmod +x Lockwire-*.AppImage`, then run it. Ubuntu 22.04 needs `sudo apt install libfuse2`; 24.04 needs `libfuse2t64`.
- **Windows:** coming soon.

Want it installed instead of run through uvx? `pip install lockwire`, then drop the `uvx`.

## Development and licence

See [CONTRIBUTING.md](CONTRIBUTING.md). Apache-2.0, all of it: see [LICENSE](LICENSE).

<!-- mcp-name: io.github.lockwirerobotics/lockwire -->
