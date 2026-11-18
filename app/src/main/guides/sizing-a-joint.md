# Sizing a joint
Is this joint's servo strong enough, and what would a different one change?

## The idea
- Lockwire scores each joint's static gravity torque at its worst pose against the joint's effort limit (check `gravity_reach`). A link's declared payload and a device mounted on a link count as mass.
- A joint's effort limit is also compared with the stall torque of the servo that drives it (`servo_stall`), and its modelled demand with the actuator's continuous geared output (`torque_budgets`).
- Part swap ranks catalog servos against the joint's real gravity load with the candidate's own mass added: fit is a stall torque of at least 2x the load, tight is at least 1x, too weak is below it.
- A heavier servo is more load for the joints above it. Part swap lists each of them before and after.
- `gravity_reach` holds the base fixed with no ground contact. A robot standing on its feet loads its legs with its whole weight: `standing_load` (needs declared contacts) and `standing_sweep` cover that.

## Tools, in order
1. `check`: find the joint's finding. The brief's "Moving joints" lines show each joint's worst gravity load beside its limit.
2. `explain-finding` with the finding's path: the rule, the inputs with their sources, the threshold.
3. `swap-preview` with the joint: the ranked servos, and what the top one does to the joints above.
4. If a `suggest-fixes` tool is in your list, call it with the finding: the engine searches the fixes itself.
5. `what-if` on the change you would propose, then the card. A servo swap itself is made by the person from the app.

## Traps
- Stall is not continuous. `torque_budgets` compares with continuous output because peak torque is not a sustainable design budget. A servo that only just holds at stall is not a servo that holds all day.
- A servo's stall torque is rated at one voltage. On a lower supply it will not reach that figure; part swap flags it ("rated at N V; your supply is M V, expect less").
- Units: datasheets often give kg*cm and Lockwire works in N*m. Quote the engine's converted figure with its source line; do not convert it yourself.
- A forgotten link mass: a link with no declared mass is not counted, so the joint looks stronger than it is. Read the brief's "Not declared" lines before trusting a load.
- Lowering the effort limit to the servo's stall torque is right only when that servo really drives the joint. The finding's action line gives the exact edit.
- Margins: use the threshold `explain-finding` returns. Never invent one.
