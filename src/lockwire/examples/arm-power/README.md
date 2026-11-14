# Two-joint desk arm (arm-power)

A small arm with its power and wiring written down, so you can see Lockwire catch real errors before you power it on.
Run `lockwire check contract.yaml` and it says NO-GO for three reasons. Each has one fix, below. Apply all three and it says GO.

| Problem | What Lockwire says | Limit and source | The one change |
|---|---|---|---|
| The pack wire is too thin for its fuse | 9 A fuse on 22 AWG wire `w.pack` (NO-GO) | at most 7 A for 22 AWG: PowerStream chassis-wiring table; fuse is the Bourns MF-R900 hold current (catalog `ptc.bourns.mf-r900`) | `w.pack`: `awg: 22` to `awg: 18` |
| The 5 V servo lead browns out the Jetson | `rail.5v` sags to 4.65 V with both servos stalled (NO-GO) | at least 4.75 V, the Jetson Nano's minimum input (catalog `nvidia.jetson-nano-devkit`); servo stall current 1.47 A at 5 V (catalog `dynamixel.xl330-m288`); wire resistance from the catalog wire table | `w.servo-feed`: `awg: 28` to `awg: 18` |
| The servo data wire is on a boot pin | ESP32 `IO12` is reserved: a strapping pin (warning) | the ESP32-DevKitC pin card marks it reserved (Espressif datasheet Table 3-1) | `w.bus-data`: `pin: IO12` to `pin: IO13` |

The 28 AWG servo feed also gets an ampacity warning (5.19 A against the PowerStream chassis-wiring 1.4 A for 28 AWG); the same `w.servo-feed` fix clears it.

Every spec number is copied from a catalog row or a pin card, with its source in a comment next to the line that uses it.
Lengths, gauges, gains and which parts are design choices, marked as such in the file.

Try the plan too: `lockwire check contract.yaml --plan plan.yaml` scores the hold against each joint's torque.
Change `effort: 0.52` to `effort: 0.05` on the shoulder and `lockwire check contract.yaml --plan plan.yaml` also warns the arm cannot hold its pose.

Not scored here, because nothing cited feeds them: the plan's minimum move time (the catalog has no joint acceleration),
the Pro tuner (no cited servo rotor inertia), and phase margin (needs the tuner's plant).
