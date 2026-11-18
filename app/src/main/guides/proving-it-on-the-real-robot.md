# Proving it on the real robot
Does the powered robot still behave like the one in the file? The person runs it; you explain it.

## The idea
- `lockwire preflight` (Lockwire Pro) compares a recording of the powered-on robot with the robot file and gives a go/no-go. The recording is a PX4 log, a ROS 2 bag (MCAP), a Lockwire recording folder or a live sample.
- The gate's stages run in order: enumeration (the declared devices against what the recording holds), liveness, flow (rate and gaps), sanity (values outside the envelope band or the device's hard limits) and cross-checks. Each emits findings; none stops the others.
- A baseline is the envelope of a known-good recording, reviewed by a person. `baseline-check` compares a later recording with it and reports drift, including a slow shift that no single reading would flag.
- A device with fewer than 10 samples, or under one second of recording, is not scored.

## Tools, in order
1. `check` first: the file itself must add up before a recording is compared with it.
2. Tell the person the command to run themselves, for example `lockwire preflight lockwire.yaml --from run.mcap`, and what its exit codes mean: 0 is GO or GO with notes, 1 is NO-GO, 2 is refused, 3 is nothing compared.
3. If your tool list has a tool that runs the gate or the baseline check on a recording that already exists, use it on that recording only. Never start a run, a sample or a gate on a live robot.
4. Explain each finding from the result: the device and the quantity, what was observed, what was expected and what to do.
5. `read-guide` with `docs/preflight` has the full page when the app carries it.

## Traps
- A recording shows the robot as it was. It says nothing about what it will do next.
- A recording cannot show a driver that is down; only a live source can. From a recording that line is "not scored".
- "Not scored" is never a pass, and a run that compared nothing exits 3, not 0.
- When a context declares two or more condition axes, each is checked on its own, and the interaction between them is reported as unverified.
- Agreement in simulation is not proof on the robot: `check-sim` compares a sim file with the lock value by value and never runs a simulation.
- Preflight, recordings, baselines and health are Lockwire Pro. On the free app, say so plainly.
