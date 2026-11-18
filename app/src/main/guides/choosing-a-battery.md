# Choosing a battery
Will this pack run the robot: voltage window, current, sag and temperature?

## The idea
Lockwire compares a declared pack with the robot in these checks:
- `battery_cell_window`: the pack's cell window (cells x the chemistry's cell minimum and maximum) against each pack-fed rail's range. A pack with no chemistry gets one "not compared" line.
- `battery_c_rate`: peak pack current (worst mode) against capacity x the continuous and the burst C-rate.
- `battery_sag`: nominal minus peak current x internal resistance, against the lowest pack-fed rail minimum.
- `battery_temperature`: the declared ambient range against the pack's charge and discharge windows.
- `battery_energy_density`: more than 300 Wh per kg means the capacity or the mass is wrong.
- `charger` and `battery_cutoff`: the charger setpoint, float mode and current, and the low-voltage cutoff, against the chemistry.
- Power budgets follow the rail tree with each converter's efficiency, so a load on a child rail counts against its converter, the rails above it and endurance.

## Tools, in order
1. The brief or `describe-system`: the rails, the power tree and what the pack feeds.
2. `check`: which battery checks ran, and which are listed as not compared (a missing C rating, chemistry or ambient).
3. `catalog-parts` for batteries: real packs with sourced numbers. Only propose a part the catalog returned.
4. If a `suggest-fixes` tool is in your list and the finding is a sag, a C-rate or an overloaded rail, call it.
5. `what-if` on the declared value you would change, then the card.

## Traps
- A battery check with a missing input says "not compared". It has not passed. List what is not declared before saying a pack is fine.
- Continuous C and burst C are different limits. Name which one a number is.
- Wh and Ah are different units. Quote the unit the engine printed and do not convert.
- A catalog part is "cited" only when every number names the maker's datasheet URL; otherwise it is "unverified". Say which.
- Do not size a pack by your own arithmetic. Change a value with `what-if` and read the engine's result.
