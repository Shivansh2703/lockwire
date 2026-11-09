---
title: Lockwire in CI
description: Run the Lockwire check on every push with the GitHub Action or pre-commit, with pull-request annotations and an HTML report.
---

# Lockwire in CI

Commit `lockwire.yaml` next to your model, then run the check on every push. A NO-GO fails
the build.

## GitHub Action

```yaml
name: lockwire
on: [push, pull_request]
jobs:
  lockwire:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Shivansh2703/lockwire@v0
        with:
          contract: lockwire.yaml
```

The Action runs `lockwire check --format github`, which prints each finding as a
pull-request annotation on the lockfile line it is about and writes a job summary. It then
writes an HTML report and keeps it as the `lockwire-report` build artifact, pass or fail.

### Inputs

The same list as the Action's `action.yml` (a test keeps them equal):

| Input | Default | What it does |
| --- | --- | --- |
| `contract` | `lockwire.yaml` | The lockfile to check. |
| `model` | | A URDF, xacro or SDF to check directly when the repo has no lockfile; overrides `contract`. |
| `adopt` | `false` | `true` annotates and summarises but never fails the job (see Adopt mode). A comma list of finding ids or prefixes (`datasheet/,model/links/`) annotates only those. |
| `strict` | `false` | Treat warnings as failures (GO with notes exits 1). |
| `version` | (the Action's own code) | Pin a release from PyPI, e.g. `0.2.6`. |
| `python-version` | `3.12` | Python to run Lockwire with. |
| `mass` | | With `model`, the datasheet mass to compare, e.g. `1.8 kg`. |
| `source` | | Where `mass` comes from, a URL or title. |
| `tolerance-pct` | (Lockwire's default) | The percent `mass` may differ. |
| `baseline` | | A committed file from `lockwire check --write-baseline <file>`; only findings not in it fail. |
| `evidence` | | Actuator evidence, one `CASE_ID=RELATIVE_PATH@SHA256` per line. |
| `evidence-root` | | The folder the evidence paths are relative to. |
| `package-root` | | ROS package roots for `package://` paths in the report, one per line. |
| `badge-json` | | Write a datasheet badge to this path (see Badge). Off when empty. |

A known gap that is fine (a datasheet mass that counts the battery the URDF leaves out) can
be accepted on record instead of failing:
`lockwire accept <finding-id> lockwire.yaml --reason '...' --witness '...'`. It fires again
if the value changes.

### Adopt mode

On a repo with existing findings, start with `adopt: "true"`. The job stays green and adds a
notice saying how many findings would fail it; the annotations and the report are the same.
Fix them, then remove `adopt`.

```yaml
      - uses: Shivansh2703/lockwire@v0
        with:
          contract: lockwire.yaml
          adopt: "true"
```

### Evidence

Actuator cases can be backed by recorded evidence files. Each line
of `evidence` names a case, a file relative to `evidence-root`, and that file's SHA-256, and
becomes one `--actuator-evidence` flag on `lockwire check`:

```yaml
        with:
          evidence: |
            shoulder-stall=evidence/shoulder.csv@<sha256>
          evidence-root: .
```

### Badge

When `lockwire check` recognises your robot, it compares the URDF's total mass and joint
count with the vendor's datasheet. Set `badge-json` to a path and the Action writes that
result as a [shields.io endpoint](https://shields.io/badges/endpoint-badge) file: green
"matches (mass, dof)", red "differs", or grey "not compared", followed by the robot, the day
the sheet was read and what its weight includes.

```yaml
        with:
          contract: lockwire.yaml
          badge-json: badge/datasheet.json
```

Publish that file somewhere public (a `gh-pages` branch, say) and point a badge at it:
`![datasheet](https://img.shields.io/endpoint?url=<public URL of datasheet.json>)`.
The Action creates the folder if it is missing. It needs the Action's own code or a release that has `badge-json`.

## Any other CI

Run the check yourself; the exit code is the result (0 GO, 1 NO-GO, 2 unreadable file,
3 nothing compared):

```bash
uvx lockwire check lockwire.yaml
```

Add `--format github` on GitHub runners for annotations, and `--json report.json` to keep a
machine-readable copy.

## pre-commit

In `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/Shivansh2703/lockwire
    rev: v0.2.6
    hooks:
      - id: lockwire-check
```

The hook runs `lockwire check` on `lockwire.yaml` whenever a commit touches a model file
(`.urdf`, `.xacro`, `.sdf`) or the lockfile. Set `rev:` to the release you installed: each
release adds its tag (`v0.2.6` and later).

`lockwire agent-setup` writes both the workflow and the pre-commit entry for you;
`lockwire agent-setup --ci` writes only the workflow.

See also: [using Lockwire](users.md), [every check](checks.md).
