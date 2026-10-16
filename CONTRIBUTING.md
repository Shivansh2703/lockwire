# Contributing

Thanks for helping. Bug reports and small fixes are welcome without ceremony.

## Setup

```bash
uv sync
uv run pytest -q tests/test_<module>.py   # tests for the module you touched
uv run ruff check src tests               # lint
uv run ruff format src tests              # format (line length 100)
```

Python 3.12 or newer. Add a test that fails without your change.

## Sign off your commits (DCO)

Every commit needs a `Signed-off-by` line, certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "your message"
```

## Before a big change

Maintainers may ask for changes to some modules to be discussed in an issue first.
Changes to the lockfile schema are breaking changes and are always discussed first.

## Add a starter

A starter is a ready lockfile for one kind of robot (drone, rover, arm...) that
`lockwire start --example <name>` writes for the user. Add one by pull request.

One folder in `src/lockwire/examples/<name>/`:

- `contract.yaml`: the lockfile. Every number has a `provenance` source and a note with the arithmetic. Record a `model:` with the links the scene draws, and give every device a `physical.mount_frame` naming one, so dragging a part onto the body works. Name scene nodes after those links.
- `starter.json`: `title`, `based_on`, `licence`, `licence_url`, `source_repo`, all filled in.
- `scene.glb`: the 3D body, 2 MB or less.
- `NOTICE` (or `LICENSE`): the licence text and credit for the model you used. Not needed if the licence is Apache-2.0, the same as this repo.

`tests/test_starter_rules.py` checks all of that, and that `robot.name` equals the
`title` and is an archetype name ("Rover"), not the model's name ("TurtleBot3").

Build `scene.glb` with a small script; see `scripts/starters/rover_scene.py` and
`scripts/starters/quadruped_scene.py`. Use the model's own meshes only when the
licence is permissive (Apache-2.0, BSD, MIT) and they fit in 2 MB. Otherwise draw
simple boxes and cylinders from its URDF. Never invent a number: if you cannot
find it at its source, leave it out.
