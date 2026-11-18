# Wire and fuse
Is each wire thick enough for its current, and can its fuse protect it?

## The idea
- `fuse_wire` (NO-GO): a fuse rated above what its wire can carry cannot protect that wire. The fuse is compared with the wire's published ampacity, never with the load.
- `ampacity`: the wire's published AWG ampacity against the computed rail current, using the worst draw across nominal and every declared power mode. A gauge or installation that is not in the published table is "not scored"; no derating is invented.
- `rail_current`: a rail's declared current limit against its computed draw, at the rail's lowest declared voltage when it has a range.
- `wire_drop`: the voltage a servo's wire loses at stall. It needs the wire's length.
- `connector_rating` and `connector_heat`: the current through a connector against its maker's rating.

## Tools, in order
1. `check`, then `explain-finding` on the wire's finding: the current, the ampacity it was compared with, and where that ampacity comes from.
2. `robot-graph` around the wire or the rail: what it feeds.
3. If a `suggest-fixes` tool is in your list, call it with the finding.
4. `what-if` on one change (the wire's gauge, or the fuse rating), then the card.

## Traps
- A fuse finding has two ways out: a fuse at or below the wire's ampacity, or a thicker wire. A smaller fuse must still carry the load, so test it with `what-if`: a limit below the rail's draw comes back as a rail current finding.
- The finding's source line says which table and which installation the ampacity is for. Quote that line; never quote an ampacity from memory.
- AWG runs backwards: a smaller number is a thicker wire.
- A wire with no declared length has no drop check. Say the length is not declared; do not assume one.
- Nominal draw and worst draw differ. `ampacity` uses the worst across the declared power modes; name which one a figure is.
