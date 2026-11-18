# What is not declared
Finding the values the robot file is missing, and what each would let Lockwire check.

## The idea
- A check with nothing to compare says so under "not compared yet" instead of passing. A missing input is silence or "not scored": never a pass and never a guess.
- The check result carries a short list of what is missing: its name, what it covers, what declaring it would let Lockwire check, and how to add it. The brief's "Not declared" lines are that list.
- A value you do not have is not declared. Say "not declared"; never fill it with a typical number.

## Tools, in order
1. The brief's "Not declared" lines, or `check` (its `missing_inputs` and `not_compared` parts) for the full list.
2. `declare-form`: every value `declare` could write on this robot, with or without a value today.
3. Ask the person for the number, its unit and where it comes from (a datasheet page, a measurement). See "Reading a datasheet".
4. `what-if` with the value, then `declare` with its source. Some inputs have no command yet; the check's "how" line says so (foot contacts are one). Then tell the person the edit that line describes.

## Traps
- Do not call a robot sound because few findings are listed. Read the engine's not-scored count: `check` returns it and the brief prints it. Never count lines yourself.
- A link with no declared mass is not summed as zero: the total mass is then incomplete, and so is every load that depends on it.
- An ambient temperature, a pack's C rating, a rail's voltage and a wire's length are common gaps. Each one switches on a check that is silent without it.
- A declared value needs a source, and only the person can apply it. You only propose it.
