# lockwire

**Find out your robot won't work before you buy the parts.**

Every robot is a stack of numbers from a dozen datasheets: what each part weighs, what each power rail can supply, how much the bus can carry, how much torque each joint gets. Lockwire keeps those numbers in one file and tells you the moment they stop adding up. Use it while you're still planning a build, then keep it in CI so a part swap can't quietly break it later.

Prefer clicking to typing? The free desktop app does the same visually: Open, Design, Declare, Plan, Check.

## Plan a robot from scratch

Start from a URDF (one line is enough), add the parts you're thinking of buying, and check:

```bash
pip install "git+https://github.com/Shivansh2703/lockwire@v0.2.0"   # not on PyPI
echo '<robot name="rover"><link name="base_link"/></robot>' > robot.urdf
lockwire start robot.urdf
lockwire add rail   lockwire.yaml --id rail.5v --voltage "5 V" --capacity "15 W" --source "UBEC datasheet: 5 V 3 A" --witness Sam
lockwire add device lockwire.yaml --id dev.pi5   --family fam.pi    --model mod.pi5    --rail rail.5v --draw "12 W" --mass "46 g" --mount-frame base_link --owner you --source "Raspberry Pi 5 product brief" --witness Sam
lockwire add device lockwire.yaml --id dev.servo --family fam.servo --model mod.mg996r --rail rail.5v --draw "6 W"  --mass "55 g" --mount-frame base_link --owner you --source "MG996R datasheet" --witness Sam
lockwire add budget lockwire.yaml --id bud.5v --kind rail_power --rail rail.5v --limit "15 W" --owner you --source "UBEC datasheet" --witness Sam
lockwire check lockwire.yaml
```

`--witness` is the person who read the number off the datasheet; use a real name, not "you".

```console
VIOLATION budgets/bud.5v — observed 18.00 W, expected <= 15.00 W (Δ +3.00 W (+20%))
  dependent: dev.pi5, dev.servo
  owner: you
  source: UBEC datasheet: 5 V 3 A
  action: shed load on rail 'rail.5v' or raise its capacity with provenance
warning model/links/base_link/mass — observed zero or missing mass, expected a positive mass with units
  …
scored 2: 0 add up, 1 do not · 0 not scored

NO-GO — 1 violation(s), 1 warning(s)
```

The check exits 1. A Pi 5 and one servo on a common 3 A regulator: caught before you ordered either. Swap in a 5 A regulator and it passes.

## A real-world example: the X500 drone

```bash
git clone https://github.com/Shivansh2703/lockwire && cd lockwire     # the X500 example lives in the repo, not the wheel
lockwire check tests/fixtures/x500/contract.yaml
```

Output (trimmed):

```console
$ lockwire check tests/fixtures/x500/contract.yaml
VIOLATION budgets/bud.power.pm02-5v — observed 35.20 W (draw mode: nominal), expected <= 15.60 W (Δ +19.60 W (+126%))
  dependent: dev.camera.oak-d-lite, dev.companion.jetson-orin-nano, dev.radio.rfd900x
  owner: power-integration
  action: shed load on rail 'rail.pm02-5v' or raise its capacity with provenance
VIOLATION budgets/bud.power.pm02-5v.7w-mode — observed 17.20 W (draw mode: 7W), expected <= 15.60 W (Δ +1.60 W (+10%))
  … (same rail, Jetson in its 7 W mode: the radio's transmit peak still tips it over)
not scored devices/dev.camera.oak-d-lite/physical/mass — observed mass not declared, expected a positive mass with units
  … (more not-scored lines: things the lockfile does not declare yet, listed, never hidden)
scored 10: 8 add up, 2 do not · 9 not scored

NO-GO — 2 violation(s), 0 warning(s)
$ echo $?
1
```

## What just happened

A Holybro PM02 power module publishes a regulated 5 V rail at 3 A: 15.6 W. An NVIDIA Jetson
Orin Nano publishes a 25 W power mode at 5 V: 5 A. Put a camera on the same rail and the
published parts **cannot power the published companion computer**. This is a custom
integration of published parts, and the two vendors' own numbers still do not add up.

Nobody printed "this build is broken". Nobody printed "5 A" either: that is 25 W / 5 V, a
quotient of figures published on different pages. The records disagree with each other, and
you find out before you apply power. Give the Jetson its own regulator
(`tests/fixtures/x500/contract_corrected.yaml`) and the same check passes.

## Your own robot

`lockwire start` reads links, joints, masses and the structure hash from your URDF or SDF. A fresh
lockfile knows the frame and nothing else; fill in devices, rails and buses as you go, and
`check` lists what it still could not check.

A lockfile whose `schema_version` this lockwire does not recognise is refused before any check runs.
`check` prints `REFUSED — contract declares schema_version '0.99'; this lockwire supports 0.2, 0.3, 0.4` and exits 2.
A lockfile at an older supported version is accepted as-is, with no automatic upgrade.

Re-running `lockwire start --force` on the same URDF is destructive:
re-declare every fact in the lockfile by hand, because no command carries those facts forward.
Any field you hand-edited (including `robot.name`) is silently overwritten back to the URDF-derived value.

## Exit codes

| command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `check` | `GO`, `GO with notes` | `NO-GO` | a contract that could not be found, read, or parsed | `NOT SCORED` (nothing compared) |
| `start` | the contract was written (or an existing one kept); no page is written | the URDF/SDF could not be parsed, or the contract could not be read or written | — | — |

`NOT SCORED` (exit 3) means no comparison had both operands: it is not a pass and not a failure.
The `scored N: …` line above the verdict is where a script should look.

## In CI

```yaml
- uses: actions/checkout@v4
- uses: Shivansh2703/lockwire@v0        # composite action: installs lockwire, runs the check, uploads an HTML report
  with:
    contract: lockwire.yaml
```

Inputs: `contract` (default `lockwire.yaml`), `version` (default latest), `python-version` (default 3.12).
Or just `pip install "git+https://github.com/Shivansh2703/lockwire@v0.2.0" && lockwire check lockwire.yaml`.

## Pre-commit

```yaml
repos:
  - repo: https://github.com/Shivansh2703/lockwire
    rev: v0.2.0
    hooks:
      - id: lockwire-check
```

Runs `lockwire check` (on `lockwire.yaml` in the repo root) whenever a lockfile changes.

## What it checks

Each check compares **two separately declared figures**, each with its own provenance line.
It shows who said what; it cannot verify that a datasheet is true.

| check | source A | source B |
|---|---|---|
| rail power budget | device draw (datasheet) | rail capacity (power module datasheet) |
| rail tree rollup | child rail draw / efficiency | parent rail capacity |
| torque budget | joint effort limit (URDF) | actuator continuous torque x gear ratio |
| bus bandwidth | message rate x payload | bus bitrate x utilisation ceiling |
| mass budget | per-device mass | declared total |
| endurance | pack energy | summed draw |
| voltage compatibility | rail voltage | device input voltage range |
| reference integrity | every `bus_ref`, `mount_frame`, dependency edge | the thing it points at |

No physics simulation, no machine learning, no network. The answer is the same twice.

## Free and Pro

- **Free forever (this repo):** the lockfile, all deterministic checks including
  power/thermal/bus/link/torque budgets, edit / describe / plan / what-if, the MCP server, the
  HTML report, and the desktop planning app (Open, Design, Declare, Plan, Check).
- **Lockwire Pro** adds the powered-on preflight gate (Test, Run), recording analysis
  (rosbag2 / ULog), and health.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Python 3.12 or newer; runtime dependencies are
pydantic, typer, rich, ruamel.yaml and numpy.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
