# Brown-out and battery sag
Does the supply hold up when the servos pull their worst current?

## The idea
- `servo_brownout` (NO-GO): every servo on a rail at stall at once pulls a device that rail feeds below that device's declared minimum input. The chain it follows is pack resistance, then the regulator, then the wire run.
- `battery_sag`: pack nominal voltage minus peak current times pack internal resistance, against the lowest minimum of the rails the pack feeds.
- `wire_drop`: a servo's own wire drops its supply below the servo's catalog minimum input voltage at stall. It needs the wire's length.
- `rail_operating_point`: a rail's loaded voltage (its voltage minus the worst wire drop at nominal draw) below the rail's own declared minimum.
- Every term the engine cannot compute (no wire length, no pack resistance, no catalog stall current) is left out, which can only shrink the sag. So a brown-out NO-GO is true, and a pass with terms missing is not proof.

## Tools, in order
1. `check`, then `explain-finding` on the brown-out, sag or wire-drop finding: the voltage reached, the minimum required, and the engine's own ways out.
2. `robot-graph` around the rail, or `describe-system`: what the rail feeds and through which wire.
3. If a `suggest-fixes` tool is in your list, call it with the finding.
4. `what-if` on one change, then the card.

## Traps
- Stall current is the catalog figure at its rated voltage. At a lower voltage the engine scales it down, never up. Do not redo this by hand.
- A servo that is not in the catalog adds no stall current, so its rail can pass without having been tested. Say so.
- Offer the ways out the finding's action line lists, and test the one you pick with `what-if` before proposing it.
- `bulk_capacitance` (470 uF per servo) is advice with no source. Present it as advice, never as the fix for a NO-GO.
- Nominal draw and stall draw are different numbers. Name which one a figure is.
- Use the minimum voltage `explain-finding` returns. Never invent a margin.
