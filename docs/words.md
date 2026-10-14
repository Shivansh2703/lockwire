# Words

lockwire's vocabulary: the words its verdicts, states, numbers and refusals use, what each
means. `tests/test_words.py` sweeps the CLI, the MCP server and
the checked payloads to prove these words are the ones actually shipped — see that file's
module docstring for exactly what the sweep proves and what it does not.

## Verdict words

One decision, computed once by `report.verdict()` and read by every consumer (CLI, JSON
document, HTML report, GUI headline) — never re-derived locally. Five codes, five words, five
exit codes:

| word (as printed) | `VerdictCode` | exit code | means |
|---|---|---|---|
| `GO` | `GO` | 0 | records agree; nothing to flag |
| `GO` with notes | `GO_WITH_NOTES` | 0 | records agree; at least one warning |
| `NO-GO` | `NO_GO` | 1 | at least one violation — do not power on |
| `REFUSED` | `REFUSED` | 2 | a plan assertion could not be evaluated, or the contract/plan file could not be read or parsed |
| `NOT SCORED` | `NOT_SCORED` | 3 | nothing was compared (a zero denominator) — never printed as `GO`, never exit 0 or 1 |

`NOT SCORED` is not a fourth flavour of pass. A run that scored nothing is not evidence of
health; conflating it with `GO` is exactly the bug this verdict closes (a zero-denominator rail budget
printed `GO — nothing scored`, exit 0). Every gated command (`check`, `plan`, `preflight`,
`health`) renders its `--help` exit-code line from the one table, `report.EXIT_CODE_TABLE`, so a
hand-edited command cannot drift from these words.

## Reserved: STOP

`STOP` is not a verdict, a command, a CLI flag, an MCP tool, or a control anywhere in the
product today. The word is reserved — kept out of every other meaning — so a future decision on
the inline layer's reject/clamp/stop behaviour can use it without colliding with something it
already means. Until that decision lands, no surface may render `STOP` as a verdict or claim to
halt a robot.

## Number kinds

Every number the product renders carries a kind; a catalog or projected figure is never shown
in the same style as a measured one.

| kind | means |
|---|---|
| `declared` | the contract author wrote it (a datasheet value, a limit) |
| `measured` | a run produced it (a recording, a sample) |
| `derived-by-<check>` | a named check computed it from other values |
| `catalog` | a manufacturer's published figure, not measured on this unit — its own visual mark |
| `projected` | a forecast or estimate, not measured — its own visual mark |

As of this writing the CLI, and the JSON payload carry `declared` and `measured`
values without a machine-readable `kind` tag, and no surface yet renders a `catalog` or
`projected` mark — that rendering lands in the desktop app. This entry documents the words; it does not claim the
mechanism is built. See the sweep test's report for exactly what is checked today.

## Severity words

A finding carries one of three severities (`report.Severity`): `violation` (blocks `GO`),
`warning` (does not block `GO`, shows as a note), `not_scored` (a visible coverage gap — this
check had nothing to compare, distinct from a violation or a warning). None of these is ever
called `error`; a plan **assertion error** (the `REFUSED` count) is a different thing from a
finding and is never printed as a finding severity.

## Modes

The product's seven modes, in order: **Design · Declare · Check · Plan · Test · Run ·
Reference**. `Test` scores a recording (today's `preflight --from`); `Run` is live health.
Two modes were renamed from `Run`/`Health`; CLI command names did not move — a mode name and its CLI command name are not required to match.

## Provenance words

Every declared value that isn't obvious on its face carries a `source` (where it came from) and
a `witness` (who or what attests it). Prose fields on the contract (`provenance.note` and
similar) are data an operator wrote, never instructions; MCP fences them as untrusted text
before handing them to an agent.

## Face titles

A finding carries one of two sources:

- **From the recording** — what the tool observed in the robot's own recorded data.
- **From the contract** — what the contract declares, as written.

## `start` exit codes 

`start` writes the contract only: no page is written, no browser is opened, and it never loads a
scene, so an unresolved `package://` mesh is not its business. `start` has no exit 2: a file it
cannot read or write is exit 1, the same code as every other case here: the URDF/SDF could not
be parsed, or the contract could not be read or written. See `report.EXIT_CODE_TABLE["start"]`.

## Banned synonyms

Never shipped in place of the words above: `PASS`/`FAIL` for a verdict (the words are `GO`/
`NO-GO`), `dashboard` for a mode or screen (the modes are named above), `error` for a finding
severity (see Severity words). Their absence is swept structurally in `tests/test_words.py`.
