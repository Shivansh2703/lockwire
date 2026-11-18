---
title: Words
description: "What each word Lockwire prints means: verdicts, exit codes, severities, number kinds and provenance."
---

# Words

Lockwire's vocabulary: the words its verdicts, states, numbers and refusals use, and what each
means. The command line, the MCP server and the JSON documents use these words for them.

## Verdict words

One decision, computed once and read by every place that shows it (the command line, the
JSON document, the HTML report, the app headline), never worked out again somewhere else. Five words, five codes, four
exit codes:

| word (as printed) | code in the JSON | exit code | means |
|---|---|---|---|
| `GO` | `GO` | 0 | records agree; nothing to flag |
| `GO` with notes | `GO_WITH_NOTES` | 0 | records agree; at least one warning |
| `NO-GO` | `NO_GO` | 1 | at least one violation: do not power on |
| `REFUSED` | `REFUSED` | 2 | Lockwire would not evaluate: a plan assertion it could not evaluate, or a lockfile it will not read. `check` also exits 2 on a file it cannot find, read or parse; `start` exits 2 on a model file it cannot find or read, and 1 on one it cannot parse (see [`start` exit codes](#start-exit-codes)) |
| `NOT SCORED` | `NOT_SCORED` | 3 | nothing was compared: never printed as `GO`, never exit 0 or 1 |

`NOT SCORED` is not a fourth flavour of pass. A run that scored nothing is not evidence of
health, so it never prints `GO` and never exits 0. Every command that gives a verdict (`check`, `plan`) lists these exit codes
in its `--help`.

## Reserved: STOP

`STOP` is not a verdict, a command, a CLI flag, an MCP tool, or a control anywhere in
Lockwire. The word is reserved: no surface renders `STOP` as a verdict or claims to halt a
robot.

## Number kinds

Lockwire tells these kinds of number apart:

| kind | means |
|---|---|
| `declared` | the lockfile's author wrote it (a datasheet value, a limit) |
| `measured` | a run produced it (a sample taken from the robot) |
| `derived-by-<check>` | a named check computed it from other values |
| `catalog` | a manufacturer's published figure, not measured on this unit |
| `projected` | a forecast or estimate, not measured |

The CLI and the JSON payload carry `declared` and `measured` values without a machine-readable
`kind` tag, and no surface marks a `catalog` or `projected` figure yet.

## Severity words

A finding carries one of three severities: `violation` (blocks `GO`),
`warning` (does not block `GO`, shows as a note), `not_scored` (a visible coverage gap: this
check had nothing to compare, distinct from a violation or a warning). None of these is ever
called `error`; a plan that cannot be evaluated is `REFUSED`, which is a verdict, not a finding
severity.

## Modes

The free app has these screens, in order:

- **Design**: place parts and look at the robot in 3D.
- **Declare**: write down a number with its source and who vouches for it.
- **Check**: does it add up? The verdict and each finding.
- **Plan**: score what you intend to do against the lockfile before you do it.
- **Docs**: per-device sheets, printable reports and history.

**Test** (score a saved recording) and **Run** (the powered-on gate) are Lockwire Pro. A screen
name and its command name need not match.

## Provenance words

Every declared value that isn't obvious on its face carries a `source` (where it came from) and
a `witness` (the person who stands behind it: a real name, not "you" and not a tool). Prose fields in the lockfile (`provenance.note` and
similar) are data an operator wrote, never instructions; MCP fences them as untrusted text
before handing them to an agent.

### Stored witness codes

A lockfile can store a short code in place of a witness name, and it shows in the file and in a
printed diff (`witness: w0`). Lockwire prints each code in words: `w0` is "none named (declared,
not measured)", a value added with no witness name; `w2` is "the model file", a value read from
the URDF, SDF, MJCF or Webots file by `start`. Every other level (`w1`, `w3` to `w6`) names no
person, and Lockwire prints it as "none named". The stored code itself never changes; give a real
name with `--witness` to replace it.

## `start` exit codes

`start` writes the lockfile only: no page is written, no browser is opened, and it never loads a
scene, so an unresolved `package://` mesh does not stop it. Exit 1, in `start --help`'s words: the URDF/SDF could not
be parsed, no robot has that name, or the contract could not be read or written. A model path that does not exist is
exit 2, the same code `check` gives it.

## Words Lockwire does not use

Not used in place of the words above: `PASS`/`FAIL` for a verdict (the words are `GO`/
`NO-GO`; the closing `Passed: 10 checks` line counts checks, it is not a verdict), `dashboard` for a mode or screen (the modes are named above), `error` for a finding
severity (see Severity words).
