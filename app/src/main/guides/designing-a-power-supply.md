# Designing a power supply
Proposing a battery pack and a regulator per rail, built only from cited catalog parts.

## The idea
- A power tree is a pack, then one regulator for each rail the robot needs (its output voltage and current).
- Every part must be a cited part that `catalog-parts` returned in this conversation, given by its id and name exactly as listed. A part you name from memory is refused.
- Each part becomes its own card, added as a device. The person applies the ones they choose; nothing is written before that.

## Tools, in order
1. `catalog-parts` for the cells and regulators, with queries such as battery, pack, buck, ldo or bec. Read each part's numbers and its label.
2. Pick a pack and, for each rail, one regulator whose own numbers cover that rail's voltage and current.
3. `propose-power-tree` with the owner and up to two alternative trees, each a pack and its rails (vout, iout, regulator).
4. After the person applies a card, `check` shows what the rails and budgets say now.

## Traps
- Only parts labelled cited count. An unverified part is refused, even when its numbers look right.
- Use the id and the name exactly as `catalog-parts` printed them. A name you shorten is a different part.
- Do not invent a part or a number. If no cited part fits a rail, say so and name the rail.
- A regulator's rating holds at its stated input voltage. Check the pack's voltage is in its input range from the catalog numbers, and quote them.
- A part that two alternative trees share gets one card, not two. Say the trees are alternatives, so the person applies the cards of only one.
