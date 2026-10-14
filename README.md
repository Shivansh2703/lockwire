# lockwire

A checked-in file that pins your robot's physical assumptions (masses, power rails, bus
bandwidth, actuator torque) and a command that tells you when they stop adding up, before you
ever apply power.

## Try it

```bash
pip install lockwire        # not on PyPI yet? pip install git+https://github.com/Shivansh2703/lockwire
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

```bash
lockwire start path/to/robot.urdf     # writes lockwire.yaml from your URDF
lockwire check                        # checks lockwire.yaml; exit 1 on a violation
```

`start` reads links, joints, masses and the structure hash from your URDF or SDF. A fresh
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
Or just `pip install lockwire && lockwire check lockwire.yaml`.

## Pre-commit

```yaml
repos:
  - repo: https://github.com/Shivansh2703/lockwire
    rev: v0.1.0
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
  power/thermal/bus/link/torque budgets, edit / describe / plan / what-if, the MCP server, and
  the HTML report.
- **Lockwire Pro** adds the powered-on preflight gate, recording analysis (rosbag2 / ULog), and
  the desktop app.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Python 3.12 or newer; runtime dependencies are
pydantic, typer, rich, ruamel.yaml and numpy.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
