# Sizing a template robot
Sizing a robot started from the resizable rover or hexapod, as one card Lockwire checks before it is shown.

## The idea
- Two templates can be resized: the Resizable rover (rover-param) and the Power-focused hexapod (hexapod-param). The person starts one from New robot on the start screen.
- `propose-robot` sizes the open robot only. Its template must be the one that is open; any other is refused.
- The only numbers you choose are the template's named sizes, in metres, inside their ranges. A wrong name or a value out of range is refused, and the reply lists every size with its range.
- The carried mass is read from the person's own words in this turn (such as "carries 2 kg"). You never type a mass, a torque or a density: link masses are derived from the template's cited material.
- Lockwire runs the change on a copy and checks it. Only a robot it calls GO becomes a card; anything else comes back with its verdict and the findings that change.

## Tools, in order
1. For the rover, `catalog-parts` for a battery pack and a motor driver (queries such as pack, driver). The hexapod already carries its servos and pack: add a part only if the person asks for one.
2. `propose-robot` once, with the open robot's template, the sizes the person's words imply, and the parts, each by its id and name exactly as `catalog-parts` returned it.
3. If the check is not GO, change a size or a part and call it again. If a change is refused, the reply gives the reason.

## Traps
- Sizes are in metres: 42 cm is 0.42.
- If the person did not state a carried mass, none is set. Ask them rather than guess.
- A part already in the robot cannot be added again; the engine refuses it.
- Say the verdict as Lockwire gave it. Never say the robot is GO before the card shows it.
