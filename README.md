<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Shivansh2703/lockwire/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/Shivansh2703/lockwire/main/docs/img/logo-light.svg" alt="lockwire" height="64">
</picture>
</p>

**I checked the published robot models of nearly 400 companies. 1 in 3 disagreed with the maker's own specs.** Every one of them still loads, and everyone assumes it is accurate. Lockwire catches the error in your robot model before the real robot does.

https://github.com/user-attachments/assets/f14dd218-d5a4-4e9e-b3cc-fa2e6537e0de

Free and open source. [Read the launch post](https://github.com/Shivansh2703/lockwire/discussions/1).

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
VIOLATION topology/rails/rail.5v/brownout: observed rail.5v sags to 4.65 V with every servo stalled (2.73 A), expected >=
4.75 V, the declared minimum input of dev.jetson
  action: shorten or thicken the wire, cut the pack resistance, or move the
servos to their own rail
VIOLATION topology/wires/w.pack/fuse: observed 9.00 A fuse on 22 AWG wire w.pack, expected fuse <= 7.00 A, the
safe current for this wire
  ...
  action: fit a fuse at or below 7.00 A or upsize wire 'w.pack'
scored 12: 10 add up, 0 do not, 1 warn, 1 model check (links and joints unchanged
since the lock) · 1 not scored · 2 other violation(s) · 1 other warning(s)

NO-GO: 2 violation(s), 2 warning(s)
...
```

Command line: macOS, Linux and Windows. Desktop app: macOS (Apple silicon) and Linux.

`check` exits 1 on a NO-GO. Two real errors in a robot you have not built yet: with both servos stalled
the 5 V rail sags below the Jetson's minimum input and resets it, and a 9 A fuse sits on wire rated
for 7 A. Each finding names the part and where its limit came from, and says what to change
(`uvx lockwire check --all` also shows the two warnings: a servo data wire on a boot pin, and a 28 AWG
servo feed drawing about 5 A). The scored line counts the comparisons that add up and names
the findings outside them. Lockwire found these without being told. Errors like this are in public
robot models today, and maintainers at Seeed and DFKI (MiR100) have merged the fixes:
[the fixes sent upstream](https://github.com/Shivansh2703/lockwire/blob/main/docs/ledger.md).

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

That writes `.github/workflows/lockwire.yml`. The step it writes, plus two optional inputs:

```yaml
- uses: actions/checkout@v4
- uses: Shivansh2703/lockwire@v0
  with:
    contract: lockwire.yaml    # the lockfile
    # model: robot.urdf        # or check a model directly, with no lockfile
    # adopt: "true"            # annotate and report, but never fail the job
```

A NO-GO fails the build, with annotations on the pull request and an HTML report.
Every input, pre-commit and other CI systems: [docs/guides/ci.md](https://github.com/Shivansh2703/lockwire/blob/main/docs/guides/ci.md).

## Let your AI agent check it

```bash
claude mcp add lockwire -- uvx --from "lockwire[mcp]" lockwire mcp
uvx lockwire agent-setup
```

The first line gives Claude Code the checks over MCP. `agent-setup` writes the agent rules into
AGENTS.md and the Claude Code one, plus a pre-commit config and the CI workflow.
Cursor, Claude Desktop and other agents: [docs/guides/agents.md](https://github.com/Shivansh2703/lockwire/blob/main/docs/guides/agents.md).

## No robot file? Try an example

servo-bench is built to fail: a 5 V rail overloaded and a wrist servo too weak, on purpose, so you can see what a catch looks like.

```console
$ uvx lockwire start --example servo-bench
wrote lockwire.yaml: example servo-bench (built to fail: a 5 V rail overloaded and a wrist servo too weak, on purpose, so you can see what a catch looks like). Next: uvx lockwire check lockwire.yaml. Parts: uvx lockwire catalog parts
$ uvx lockwire check
VIOLATION model/joints/wrist/limits/effort: observed 0.40 N*m joint effort limit, expected <= 0.226 N*m, the feetech.scs0009
stall torque (1.8 times that)
  ...
  action: ... or find a stronger servo: uvx lockwire swap lockwire.yaml wrist
VIOLATION budgets/bud.power.servo-5v: observed 32.10 W, expected <= 15.00 W (off by +17.10 W, +114%)
  ...
scored 9: 7 add up, 2 do not · 2 not scored

NO-GO: 2 violation(s), 0 warning(s)
...
```

The examples that pass the check (`lockwire start --example drone`, `rover`, `arm`, `quadruped`, `humanoid`,
`hexapod-param`, `rover-param`) are good starting points; `arm-power` and `servo-bench` show a catch, and so will your own robot.

## Features

- **Check:** every mass, inertia, joint limit, torque and power number against the maker's datasheet and physics, in seconds, with the page it compared against and a fix for each finding.
- **Budgets:** power, torque and bus budgets, brown-out on every rail, and a standing-load check for legged robots.
- **What-if and part swap:** see the verdict before you change a part, or find a stronger servo in the parts catalog.
- **Lockfile and CI:** `lockwire.yaml` pins every checked number; CI fails the build on drift, naming the number that moved, with pull-request annotations and an HTML report.
- **Sim drift:** `check-sim` catches a sim file that has drifted from the lock.
- **Reports:** a PDF or HTML report to share, with no machine paths in it.
- **AI:** the app's assistant explains each finding and suggests a fix the checker proves on a copy first; coding agents run the same check over MCP.

<img src="https://github.com/user-attachments/assets/5fc9697c-02ee-4fc8-a19b-791e1d2c0379" alt="Sixteen robots checked by Lockwire: the nine built-in examples and seven open-source research models">

More in [the guides](https://github.com/Shivansh2703/lockwire/blob/main/docs/guides/index.md). The powered-on
preflight gate, ROS 2 bags and drift tracked across many units are Lockwire Pro, waitlist only: https://lockwirerobotics.com.

## Download the app

The free desktop app does the same visually: [latest release](https://github.com/Shivansh2703/lockwire/releases/latest).

![The free app: the start screen with the example list, then the Rover example open on the Check screen in 3D with a GO with notes verdict (1 warning, 1 advice, 3 not scored), its warning row and its tip margin advice open, then the not-scored list opened with its three rows](https://raw.githubusercontent.com/Shivansh2703/lockwire/main/docs/img/try-example.gif)

![The nine bundled examples in the dark theme, three to a row, each robot in 3D beside its Check verdict. Top row: desk arm NO-GO, servo bench NO-GO, arm GO. Middle row: drone GO, humanoid GO, quadruped GO with notes. Bottom row: rover GO with notes, hexapod GO, parametric rover GO](https://raw.githubusercontent.com/Shivansh2703/lockwire/main/docs/img/every-example-3x3-dark.webp)

The nine example robots that come with the app, each on its Check screen. Two are built to fail on purpose so you can see a catch: the desk arm and the servo bench, both NO-GO. The quadruped and the rover pass with notes, and the other five pass clean. Open any of them from the start screen, no robot file needed.

- **macOS (Apple silicon):** unzip, open Lockwire. The app is not notarized, so the first open warns: open System Settings, Privacy & Security, and click "Open Anyway".
- **Linux:** `chmod +x Lockwire-*.AppImage`, then run it. Ubuntu 22.04 needs `sudo apt install libfuse2`; 24.04 needs `libfuse2t64`.
- **Windows:** coming soon.

Want it installed instead of run through uvx? `pip install lockwire`, then drop the `uvx`.

## Development and licence

See [CONTRIBUTING.md](https://github.com/Shivansh2703/lockwire/blob/main/CONTRIBUTING.md). Apache-2.0, all of it: see [LICENSE](https://github.com/Shivansh2703/lockwire/blob/main/LICENSE).

<!-- mcp-name: io.github.lockwirerobotics/lockwire -->
