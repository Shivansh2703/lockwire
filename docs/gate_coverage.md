# What a preflight GO does — and does not — assert

Operator-facing coverage map for `lockwire preflight` on the offline (recording)
path.

A **GO** (exit 0) means: every stage that *can* score against the contract and
the recording found no violations. It does **not** mean the powered-on robot was
fully checked. Everything the gate cannot verify offline is emitted as a named
`not_scored` finding on every run — including a clean pass. Silence that looked
like a pass is the failure mode this product exists to avoid.

---

## What a GO asserts

Against the contract and the recording you passed to `--from`:

- Declared devices that the source can see match their identity descriptors
  (stage 0), and declared drivers/nodes the source can see are up (stage 1),
  when those legs are armed.
- Publish rates and within-recording gaps that the recording can support stay
  inside the declared bands (stage 2 scored legs).
- Signal-range / variance / NaN legs, and record-level budget / envelope
  cross-checks, when those stages are implemented and armed — see "Unbuilt stage" below for which of these are not yet armed on this build.
- Sample-derived cross-checks that the recording and lockfile can actually
  support (stage 4 signal path) — today mainly stationary `|g|≈9.81` when the
  context asserts a stationary platform and the operands exist.

Warnings may still appear on a GO (MEL-style notes). `not_scored` lines always
may. Neither flips the exit code.

---

## What a GO does **not** assert

Two different shapes of "not checked" appear in the output. Read them apart.

### Unbuilt stage (temporary)

Wording: `stage not yet implemented`.

Means: the stage body is not written yet. It will land in a later release. This
is **not** a permanent coverage boundary.

### Permanent offline boundary (needs live hardware)

Wording includes: `permanently out of scope offline (requires live hardware)`.

Means: this gate will **never** score the capability offline. Needs a powered-on
robot. Not an unfinished stub.

<!-- m1b-capabilities -->
- live hardware↔software reconciliation
- comm links under representative load
<!-- /m1b-capabilities -->

An offline GO that lists these two is honest. An offline GO that omitted them would be
overclaiming — the exact false-GO shape that bounced earlier coverage work.

---

## Planted failure modes the gate catches today

Checkable refusals against `contract_clean.yaml` and the planted bags under
`tests/fixtures/bags/`. Each row is a fixture the gate must NO-GO — not a
reassurance.

| planted bag | stage | quantity that refuses |
|---|---|---|
| `rate_collapse` | 2 | `devices/dev.gnss/rate_hz` |
| `range_excursion` | 3 | `devices/dev.barometer/signals/pressure_pa/range` |
| `frozen_but_publishing` | 3 | `devices/dev.imu/signals/accel_z/variance` |
| `condition_envelope_exit` | 4a | `devices/dev.imu/envelopes/nominal-op/conditions` |

**`condition_envelope_exit` was caught only after B113, and why it was not is worth
keeping.** This section originally read *"dispositioned by B101 … leave that bag
alone"* — true at the time, and true for a bad reason. The bag records `nominal-op`
at 45 °C and 1800 m, but `contract_clean` carried condition baselines **only** under
`bench-idle`, a context its reference recording is never in. So the stage had nothing
to score against and disclosed a context mismatch, and the gate **passed a robot at
45 °C**. The missing baseline was not untidy provenance; it was a hole in coverage.

B113 fitted `nominal-op` conditions from `bags/clean`'s own extrema, and the same bag
now refuses (B104 supplies the measured values in the text):

```
lockwire preflight tests/fixtures/contract_clean.yaml \
  --from tests/fixtures/bags/condition_envelope_exit
→ EXIT=1, NO-GO — 1 violation(s), 0 warning(s)
  VIOLATION devices/dev.imu/envelopes/nominal-op/conditions
    observed  outside baselined conditions on temp_c: 45.00 C,
              condition_altitude_m: 1800.00 m — no data supports healthy here
    expected  temp_c in [23.6635, 24.482] C,
              condition_altitude_m in [119.103, 120.719] m
```

**Standing lesson: a fixture that looks "dispositioned" may be uncatchable rather than
handled.** The distinction is invisible from the fixture's own status line, and only
executing it against the contract tells them apart.

Evidence for the freeze path (the failure mode named in the contract's own
`known_failure_modes`): 

```
lockwire preflight tests/fixtures/contract_clean.yaml \
  --from tests/fixtures/bags/frozen_but_publishing
→ EXIT=1 on stage-3 devices/dev.imu/signals/accel_z/variance
  (oneshot_variance_min)
```

---

## Contract `known_failure_modes` — caught vs gap

The clean contract declares one named failure mode on the IMU model:

- `frozen-but-publishing after brownout`

**Caught today.** Stage 3 scores `oneshot_variance_min` against min windowed
population variance; the `frozen_but_publishing` bag refuses as above. Every
named `known_failure_modes` entry on this contract is caught — there is no
unnamed silence here.

A gap that is **not** a `known_failure_modes` entry but still matters for reading
a GO: stages 0 and 1 can disclose absence as `not_scored` through a recording,
but their **violation** paths (wrong identity / driver down) are not reachable
via `RecordedSource` offline. That is a permanent offline reachability limit for
those legs, not an unbuilt stub — the live path (LiveSource) is where
those refusals land. Distinct from the live-hardware capability list above: those two
capabilities are never scored offline at all; stage 0/1 *enumeration and
liveness matching* do score offline when ids align.

---

## Named `not_scored` channels this phase already emits

Besides the two live-hardware items above, a clean offline run also discloses:

### Stage 2 (B87) — live-path legs not exercised through a recording

- `preflight/stage/2/timestamps/monotonicity/live_path` — recv-time monotonicity
  on the live path has never been exercised; RecordedSource monotonicity is
  enforced upstream at load time, so the property cannot fail here.
- `preflight/stage/2/timestamps/wall_clock_currency` — wall-clock currency is a
  live-path property; not scored against a recording.

### Stage 4 signal (B90) — legs the offline operands cannot support

- `preflight/cross_signal/stationary_|g|` — when the recording context does not
  assert a stationary platform (`bench-idle` / `armed-idle`), gating refuses
  rather than inventing a gravity check under motion. That guard is correct
  design, not missing coverage.
- `preflight/cross_signal/redundant_sensors` — lockfile schema has no redundancy
  declaration naming peer sensors (filed follow-on).
- `preflight/cross_signal/camera_histogram` — recording format carries no image
  data or histogram summary channel (filed follow-on).

### Coverage stage — permanent live-hardware boundary

The two capabilities listed in the marked block above. Always emitted. Never
violations or warnings. Never change the exit code.

---

## Live capture (`lockwire_ros`) — what it changes, and what it does not

`lockwire_ros`'s gate node can subscribe to the topics the contract declares,
write a recording from what actually arrives, and gate that. The recording is
seconds old and came off the running robot. It is tempting to read that as a
stronger GO than the same command against a month-old bag.

**It is not, and the live-hardware list above does not move.** A live capture is still fed
through `RecordedSource` — the gate cannot tell a fresh recording from a stale
one, and nothing in the exit map or the disclosure set changes. Concretely:

- **Stages 2–4 now score real, current robot data.** That is the genuine gain:
  rate, gaps, signal range, variance and the record-level cross-checks are
  evaluated against what the machine is doing now instead of what it did once.
- **Stage 0/1 refusals are still unreachable.** Wrong identity and driver-down
  are violation paths `RecordedSource` cannot produce (see the section above).
  A device that is *absent* publishes nothing, so it shows up as a missing
  stream — never as a scored absence.
- **`live hardware↔software reconciliation` is therefore still not asserted.**
  It needs the live bus/graph path (`LiveSource`), not a live *recording*.
- **`comm links under representative load` is untouched.** Nothing here drives
  a link at load.

One thing the capture path does add, and it is a refusal rather than coverage:
**a capture that receives no messages refuses to write a recording at all.** An
empty recording would reach the gate as `GO — nothing scored`, which exits 0. So
a dead driver becomes a refusal at capture time instead of a vacuous pass at
gate time. That is the capture node's own guard, not a new gate check.

**Read a live-captured GO exactly as you read any other GO**: the `not_scored`
lines still say what it did not cover, and the two capabilities above are still
among them.

## How to read a clean pass

```
GO — records agree; N check(s) not scored
```

Read the `not_scored` lines. If you needed live hardware↔software reconciliation
or comm links under representative load before clearing the machine, this GO is
not that clearance — check those on the powered-on robot (or by hand).
