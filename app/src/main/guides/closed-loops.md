# Closed loops
Linkages that close on themselves: four-bars, five-bar legs, parallel linkages.

## The idea
- `gravity_reach` walks a kinematic tree. A closed loop in an SDF file (a five-bar leg) is solved instead: the loop is closed at each pose of a grid over its driven joints' ranges, and each driven joint holds its share of the load. A passive pin holds nothing. "Driven" means the joint has a positive effort limit.
- That is still the base held with no ground contact. It is not a standing load.
- A loop it does not solve is "not scored" with the reason: not planar, driven joints that do not exactly fix the loop, a joint moving above or below the loop, or no SDF file on disk to read the pins from.
- A grid pose where the linkage locks or cannot be reached is left out of the peak and counted in the output.
- `mechanisms`: a loop joint declared `continuous` (it spins all the way round) is compared with the exact Grashof condition on the loop's four link lengths. A loop with nothing declared continuous gets no finding. A loop that is declared continuous but is not a four-bar is "not scored", naming the kind found.
- `urdf_sanity` lists loops among the structural mistakes it reports for a model.

## Tools, in order
1. `check`: look for the loop's lines among the findings and the not-scored lines.
2. `explain-finding` on that line: which rule applied, and why the loop was or was not solved.
3. `robot-graph` around a loop joint: which links and joints form the loop.
4. On Pro, `loads` gives the force and moment on each joint.

## Traps
- "Not scored" on a loop is not a pass. Say the loop's load was not checked and give the engine's reason.
- Which joints are driven decides the answer: a pin with an effort limit above zero is treated as driven.
- A loop's torque covers only the poses that could be solved. If poses were left out, say so, from the output.
- Do not work out linkage forces yourself. If the engine did not solve the loop, the honest answer is that it is not checked.
