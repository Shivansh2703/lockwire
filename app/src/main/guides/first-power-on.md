# First power-on
What to settle in the file before the robot gets power for the first time.

## The idea
- Lockwire never actuates: it does not write to a device, a bus or an actuator, and it does not command the robot. You do not start anything on a live robot either. You tell the person what to run and explain what comes back.
- `check` checks the model: the numbers in the robot file against each other. A NO-GO there means do not power it on yet.
- The powered-on gate (`lockwire preflight`, Lockwire Pro) checks the robot: a recording of the powered machine against the same file.

## Tools, in order
1. `check`: clear every violation first (see the guide "Why is it NO-GO, and the smallest fix").
2. Read the electrical findings before anything else: fuse against wire (`fuse_wire`), each device inside its input range (`voltage_compatibility`), brown-out at stall (`servo_brownout`), and the reverse-polarity and e-stop lines where the file declares those parts.
3. The brief's "Not declared" lines: say plainly what was not checked because a value is missing.
4. `plan`, when the robot has a plan file: a plan that asks a joint for more than its limit is a NO-GO before anything moves.
5. Tell the person the next step is theirs: power the robot, record it, and run the gate on that recording (see "Proving it on the real robot").

## Traps
- GO is about the file. It says the declared numbers add up, not that the wiring on the bench matches the file.
- A check with nothing to compare is not a pass. A robot with many "not compared" lines has not been checked there.
- Lines marked as advice with no source (bulk capacitance, a reverse-polarity guard) are advice. Say so; do not present them as rules.
- The servo recorder only reads. Its own guide says it has not yet been run on real hardware by its authors, and to try it first on a servo that is not holding anything up.
- Never tell the person it is safe. Tell them what Lockwire checked, what it found and what it could not check.
