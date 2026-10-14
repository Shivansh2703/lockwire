# Reference robot: sourced specifications

Published, cited numbers for the X500 reference robot used in the examples. **Fabrication policy:
every value here is either a cited published figure or `UNKNOWN`.** Nothing was estimated to fill a
gap. These numbers are baked into a fixture that a safety-checking tool trusts, so a
plausible-looking invention is worse than a hole.

Figures were collected in July and September 2026 from the vendor pages and datasheets cited in each
row; retrieval dates are given where a value was fetched directly.

---

## Two findings worth knowing first

### 1. The X500 V2 is a **4S** airframe, not 6S

Holybro's own kit page recommends **4S 3000-5000 mAh, 20C+, XT60**. Every derived electrical number
(pack voltage, watt-hours, rail headroom) is therefore computed on 4S, not 6S.

### 2. The reference build has a **real power-tree violation**

The violation survives only under a specific reading, and it is a *derivation* rather than a quoted
figure. Both halves matter, because a demo built on the loose version fails the first time someone
checks it. An earlier version of this document called the 5 A figure a datasheet number and said the
defect was discoverable from datasheets alone. Neither was true as written: the 5 A is not
published, it is derived.

| fact | value | status |
|---|---|---|
| PM02/PM06 regulated rail | **5.2 V @ 3 A max** | primary: PX4 hardware docs + Holybro |
| Orin Nano **module** `VDD_IN` | **5 V-20 V** supported | NVIDIA module datasheet |
| Orin Nano **dev kit** barrel jack | **7 V-20 V**, ships a **19 V** brick | NVIDIA quick-start + carrier spec |
| Dev-kit carrier -> module | steps the jack down; module `VDD_IN` is **5 V only** | NVIDIA forum (staff) |
| Orin Nano power modes | 7 W / 15 W / 25 W | NVIDIA docs |
| **"5 A at 5 V"** | **25 W / 5 V = 5 A** | **DERIVED, not quoted** |

**Why this is a good demo.** The 5 A is not a number anyone published. It is the quotient of two
figures that *were* published, on different pages, by the same vendor. That is the product's thesis:
the records disagree once you put them beside each other. A quoted number proves nothing; a derived
contradiction is the whole point.

**The reading that must be stated explicitly.** The violation holds for a **bare module on a custom
carrier**, which is what a real drone integration builds (the 176 g dev kit and its 19 V brick are
dead weight on an airframe). There, `VDD_IN` is 5 V, peak draw is 5 A, and the PM02's 3 A rail is
short by 2 A. It does **not** hold for the dev kit as shipped: that wants 7-20 V at the jack, so
feeding it the PM02's 5.2 V rail is a **voltage-range incompatibility**, a different and arguably
worse failure. The fixture models the bare-module reading and cites module power semantics; the
176 g dev-kit mass is the kit, not the module, and is not used.

**The lesson, worth keeping:** an unverified number in a reference record was later quoted as fact,
and it survived until every value had to carry its own citation. That is the failure mode lockwire
exists to catch, found here in lockwire's own paperwork.

---

## Airframe — Holybro X500 V2 frame kit

| spec | value | source |
|---|---|---|
| wheelbase | 500 mm | holybro.com/products/x500-v2-kits |
| dry frame weight | 610 g (frame kit only) | ” |
| recommended propeller | 1045 (10×4.5″) | ” |
| payload capacity | 1500 g without battery, at 70% throttle | ” |
| recommended battery | **4S** 3000–5000 mAh, 20C+, XT60 | ” |
| max takeoff weight | **UNKNOWN** — Holybro publishes payload capacity, never a stated MTOW | — |

## Flight controller — Pixhawk 6C

| spec | value | source |
|---|---|---|
| mass | 59.3 g (aluminium case) / 34.6 g (plastic) | docs.holybro.com/autopilot/pixhawk-6c/technical-specification |
| input voltage (main) | max 6 V · USB 4.75–5.25 V · servo rail 0–36 V | ” |
| primary IMU | ICM-42688-P | ” |
| secondary IMU | BMI088 (BMI055 on earlier units, discontinued) | ” + docs.px4.io |
| magnetometer / barometer | IST8310 / MS5611 | ” |
| ports | 3× UART, 2× GPS, 2× CAN, 1× I2C, 16 PWM, 2 power inputs | ” |
| board current draw | **UNKNOWN** — only per-port limiters published (Telem1 ≤1.5 A) | — |
| TELEM/UART default baud | **UNKNOWN** — ports are published; no default bitrate appears in the cited Holybro technical specification | — |
| ethernet | **none** — not in the published port list; inventing an ethernet companion link would be fabrication | — |

### TELEM1 current limiter (powers `rail.pixhawk-telem1-5v`)

Fetched 2026-09-06 from
[docs.holybro.com/autopilot/pixhawk-6c/technical-specification](https://docs.holybro.com/autopilot/pixhawk-6c/technical-specification)
(Electrical data / Current Ratings). Quoted verbatim:

> "Telem1 Max output current limiter: 1.5A"

The same section publishes no separate peripheral 5 V rating for the TELEM ports (only "Max input
voltage: 6V" and "USB Power Input: 4.75~5.25V" are stated), so the fixtures model
`rail.pixhawk-telem1-5v` as the PM02's 5.2 V passed through the Pixhawk's power input unchanged
(`efficiency: 1.0`, pass-through, not a converter): capacity = 1.5 A × 5.2 V = **7.8 W**. This is
the first sourced TELEM/companion rail capacity for this board; the RFD900x telemetry radio routes
its declared draw onto it (see the RFD900x row below and the Power section).

### FC-companion data link

The Pixhawk 6C source record establishes **3× UART** and names **Telem1** (current-limiter note).
It does **not** publish a default baud for those ports. **USB** appears only as a power-input
voltage range (4.75–5.25 V), not as a documented companion data path. **Ethernet** is absent
from the port list, so an ethernet link to the companion is not modeled.

The companion section does not publish a Jetson-side UART/USB-serial pinout for the bare-module
reading. The fixtures therefore model the MAVLink path from the **FC's published TELEM/UART
ports only**: a `serial` bus on the flight controller (`bus.uart.companion`) with **bitrate
omitted (UNKNOWN)**. Cost to bandwidth checks: `check_bus_bandwidth` skips any bus whose
`bitrate is None`, so utilization on this path cannot be scored until a cited baud is locked.

## Companion — NVIDIA Jetson Orin Nano

| spec | value | confidence |
|---|---|---|
| **module-only mass** | **UNKNOWN** — dev-kit 176 g is the kit, not the module; module-only weight unpublished | NVIDIA dev-kit hardware spec page; NVIDIA forum thread 339855, open |
| power modes | 7 W / 15 W / 25 W; JetPack 6.2 adds uncapped MAXN SUPER | NVIDIA docs ✅ |
| **module** `VDD_IN` range | **5–20 V** | NVIDIA module datasheet ✅ |
| **dev-kit** DC barrel jack | **7–20 V**, 19 V PSU included | NVIDIA quick-start + carrier spec ✅ |
| dev-kit carrier → module | steps down; module sees **5 V only** | NVIDIA staff forum reply |
| ~~input (USB-C standard mode) 5 V @ 5 A~~ | **RETRACTED** | was "search-synthesised, unverified" and is wrong as stated: USB-C on the dev kit needs the **19 V** adapter. The 5 A is a *derivation* (25 W / 5 V `VDD_IN`) valid for the **module**, not a USB-C figure. See finding 2. |
| thermal throttle trip point | **UNKNOWN** — Tj max 105 °C found only via search synthesis; primary PDF would not fetch | — |

## Sensors and links

| part | spec | value | note |
|---|---|---|---|
| Here3+ GNSS | mass | 51.8 g | irlock.com |
| | protocol | DroneCAN | ” |
| | ~~8 Mbit/s~~ | **CAPABILITY, NOT OPERATING RATE** | CubePilot advertises 8 Mbit/s for the Here3+ (vs 1 Mbit/s for the Here3), but that requires **CAN FD**, explicitly enabled. ArduPilot's **default is 1 Mbit/s classic CAN**, and DroneCAN's recommended rates are 1 / 0.5 / 0.25 / 0.125 Mbit/s for compatibility. **Do not put 8 Mbit/s in a bandwidth budget** — it would size the budget against a rate the bus is almost certainly not running at. Model the bus at its *configured* rate. |
| | update rate | 8 Hz | **retailer** source |
| | current draw | **UNKNOWN** — a public ArduPilot thread asked the same question and got no answer | — |
| RFD900x telemetry | weight | 14 g | search-engine paraphrase (unchanged; not re-verified) |
| | RF band | 902–928 MHz | search-engine paraphrase (unchanged; not re-verified) |

### RFD900x: first-hand datasheet figures

The search-engine paraphrase rows above are **superseded** for the fields below. Fetched directly from
[RFD900x DataSheet V1.2 (rfdesign.com.au)](https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf)
on 2026-09-06.

| spec | value | source |
|---|---|---|
| power supply voltage | **+5 V nominal (+5 V min, +5.5 V max, +6 V abs max)** | RFD900x DataSheet V1.2, fetched 2026-09-06 |
| TX current | **1 A peak at 30 dBm** | ” |
| RX / standby current | **60 mA typical** | ” |
| TX power | 0–30 dBm, adjustable | ” |
| RX sensitivity (10⁻⁵ BER, by air data rate) | -111 dBm @ 12 kbps · -107 dBm @ 56 kbps · **-105 dBm @ 64 kbps (factory-default air rate)** · -102 dBm @ 100 kbps · -104 dBm @ 125 kbps · -98 dBm @ 200 kbps · -94 dBm @ 224 kbps | RFD900x DataSheet V1.2 p.5 table, read from the PDF text; default air rate from store.rfdesign.com.au |
| default UART baud | **57600 bps** (user-selectable: 9600/19200/38400/57600/115200/400k/921k) | ” |
| RF connector | SMA | ” |
| bundled antenna gain | **UNKNOWN** in the datasheet itself — no antenna model or dBi figure appears there. Resolved from RFDesign's own RFD900x modem bundle page: **3 dBi half-wave dipole** (bundle lists "4 x Antennas, 900MHz Half wave dipole 3dBi", plus two 2.1 dBi monopole options not used here) | [RFD900x Modem Bundle (USB-C)](https://store.rfdesign.com.au/rfd900x-modem-bundle-usb-c-hs-8517-62-00-90/), fetched 2026-09-06 |
| module mass | **14.5 g** | [store.rfdesign.com.au — RFD900x Modem](https://store.rfdesign.com.au/rfd-900x-modem/), fetched 2026-09-06, first-hand ("Weight: 14.5g"). Supersedes the earlier 14 g search-engine-paraphrase figure. |
| power draw on `rail.pixhawk-telem1-5v` | **5.2 W transmit-peak** (1 A × 5.2 V, fallback/worst mode) · **0.312 W receive/standby** (60 mA × 5.2 V) | TX/RX current from the datasheet rows above; 5.2 V is the PM02's rail voltage passed through TELEM1 (see "TELEM1 current limiter" under Flight controller). Routed onto that rail because the section publishes a sourced TELEM1 capacity. |
| OAK-D Lite | power | 2.5–3 W base, +1 W AI, +0.5 W depth, +0.5 W encode | Luxonis docs ✅ |
| | interface | USB-C | ” |
| | weight | 61 g — **conflicting**, absent from the official page | — |

### FC-companion default baud

The FC's own technical specification still states no default baud (unchanged). PX4's own docs
publish a default for the companion telemetry link independent of the board:
[docs.px4.io — MAVLink Peripherals](https://docs.px4.io/main/en/peripherals/mavlink_peripherals.html)
states `SER_TEL2_BAUD = 921600` as the default TELEM2 companion-link baud (fetched 2026-09-06).
This is a PX4-project default, not a Holybro Pixhawk-6C-specific figure — flagged as such in the
fixture. The RFD900x telemetry-radio link uses its own published default, 57600 bps (see above).

### IMU hard limits

| part | accel full-scale | gyro full-scale | source |
|---|---|---|---|
| ICM-42688-P | ±16 g = ±156.9064 m/s² | ±2000 dps = ±34.9066 rad/s | [invensense.tdk.com product page](https://invensense.tdk.com/products/motion-tracking/6-axis/icm-42688-p/), fetched 2026-09-06 |
| BMI088 | ±24 g = ±235.3596 m/s² | ±2000 dps = ±34.9066 rad/s | [bosch-sensortec.com product page](https://www.bosch-sensortec.com/products/motion-sensors/imus/bmi088/), fetched 2026-09-06 |

Conversions: 1 g = 9.80665 m/s²; dps → rad/s = dps × π/180. These are the maximum programmable
full-scale ratings (the datasheet's ceiling), used as `hard_limits` bands — not an operating
envelope, which would require fitted flight data this static contract does not have.

### Magnetometer bus identity: IST8310

[Isentek IST8310 datasheet](https://tw.isentek.com/userfiles/files/IST8310Datasheet_3DMagneticSensors.pdf):
default I2C slave address **0x0E** (7-bit) when the CAD0/CAD1 address pins float.

### OAK-D Lite USB link

[docs.luxonis.com — OAK-D Lite](https://docs.luxonis.com/hardware/products/OAK-D%20Lite), fetched
2026-09-06: published as "USB 2/3 (up to 10 Gbps)". No published weight/mass (confirms the
earlier UNKNOWN row above; the "61 g" figure remains unresolved/conflicting).

**Resolution:** the official Luxonis datasheet PDF
[oak-hardware/DM9095_OAK-D-LITE_DepthAI_USB3C/Datasheet/OAK-D-Lite_Datasheet.pdf](https://github.com/luxonis/oak-hardware/blob/master/DM9095_OAK-D-LITE_DepthAI_USB3C/Datasheet/OAK-D-Lite_Datasheet.pdf)
(fetched first-hand 2026-09-06) — lists "USB3.1, gen2 10gbps" as an on-die controller
feature, but states in prose: "The data is then output to a host via USB 3.1 Gen1." The **actual
operating link is USB 3.1 Gen1 (5 Gbit/s)**; the 10 Gbps figure is the controller PHY's ceiling,
not the product's declared data path. `bus.usb.companion.bitrate` is declared as 5,000,000,000
bit/s on that basis. No weight/mass figure appears anywhere in this datasheet either — the "61 g"
retailer figure stays unresolved/conflicting and is not declared.

## Propulsion

| spec | value | note |
|---|---|---|
| motor | 2216, 920 KV, 3S–4S | Holybro kit page |
| motor weight | **80 g**, resolves the earlier 63 g/~150 g retailer conflict | Holybro's own spare-parts store, fetched first-hand 2026-09-06: `https://holybro.com/products/spare-parts-x500-v2-kit.json`, SKU 520085 (CW)/520086 (CCW), `"weight": 80.0, "weight_unit": "g"`. First-party Holybro figure supersedes both prior retailer numbers. |
| motor max continuous current / max power | **UNKNOWN** | still not published anywhere checked (Holybro kit page, spare-parts store, or spec sheet) |
| motor hover-only draw (derived) | **61.67 W per motor** | Derived, not a datasheet figure: Holybro's X500 V2 kit page publishes "Flight time: ~18 minutes hover with no additional payload. Tested with 5000mAh Battery." Total hover power = (5000 mAh × 14.8 V nominal 4S = 74.0 Wh) / (18 min = 0.3 h) = 246.68 W, divided evenly across 4 motors. This is the aircraft's *total* hover draw (propulsion + stock avionics), not a motor-isolated figure — folding baseline avionics in modestly overstates true motor draw. Declared on `dev.motor.*` as mode `"hover"`; no `"max"` mode exists since no max-current/max-thrust figure is published. |
| ESC continuous | 20 A | Holybro kit page |
| ESC burst | 30 A (10 s) | search aggregation, unconfirmed |
| ESC DShot support | DShot150/300/600 | search aggregation, unconfirmed |
| ESC weight | **50 g** | Holybro's own spare-parts store, fetched first-hand 2026-09-06: same JSON as above, SKU 520081, `"weight": 50.0, "weight_unit": "g"`. Resolves the earlier UNKNOWN. |

## Power

| spec | PM02 (V3) | PM06 |
|---|---|---|
| input | 2S–12S | 2S–10S (14S variant exists) |
| current sense | up to 120 A | — |
| PCB rating | 60 A cont / 120 A burst | 70 A cont / 120 A burst |
| **regulated 5 V rail** | **5.2 V @ 3 A max** | **5.2 V @ 3 A max** (page also states 5.1–5.3 V) |
| weight | 20 g | 24 g (unconfirmed) |

### Pixhawk TELEM1 rail: `rail.pixhawk-telem1-5v`

The Pixhawk 6C's own power input runs off the PM02's regulated 5.2 V rail; its published Telem1
per-port current limiter (1.5 A — see "TELEM1 current limiter" under Flight controller above)
gives the first sourced capacity for a TELEM/companion rail. Modeled as a 1.0-efficiency
pass-through (no separate peripheral-voltage figure is published for TELEM1): 5.2 V × 1.5 A =
**7.8 W**, `upstream: rail.pm02-5v`. The RFD900x telemetry radio is the sole declared consumer:
5.2 W transmit-peak fallback, 0.312 W receive/standby — both roll up onto `rail.pm02-5v` unchanged
(pass-through efficiency 1.0), growing its total real draw and flipping `bud.power.pm02-5v.7w-mode`
(7 W Jetson + 5 W camera + 5.2 W radio = 17.2 W > 15.6 W) from a pass to a violation. Both fixtures
(`contract.yaml` and `contract_corrected.yaml`) carry this rail identically — in the corrected
build the Pixhawk's own supply still comes from PM02 even though the Jetson moved to the dedicated
Castle Creations BEC.

### Jetson supply — Castle Creations CC BEC PRO (010-0004-01)

Checked against the
[manufacturer product page](https://www.castlecreations.com/en/cc-bec-pro-010-0004-01) and
[manufacturer user guide](https://www.freakware.de/technik/CastleCreations/cc_bec_pro_user_guide.pdf).

| spec | value | source |
|---|---|---|
| adjustable output | **4.8–12.5 V**; set to **5 V** for the Jetson role in the corrected fixture | Castle Creations product page |
| peak current | **20 A** | ” |
| continuous current by input voltage | **15 A @ 16 V in** · 13 A @ 24 V · 11 A @ 32 V · 9 A @ 40 V · 8 A @ 48 V | Castle Creations product page + user guide |
| maximum input | **50.4 V (12S)** | Castle Creations product page |
| mass | **29 g (1 oz)** | ” |
| dimensions | **43×33×24 mm** | ” |

**4S caveat.** The X500 pack spans 14.8 V nominal to 16.8 V full, so it straddles the
continuous-current table's nearest published row: **15 A @ 16 V in**. The corrected fixture
quotes that row but relies on nothing tighter than **≥13 A-class continuous at 4S**. The
enforced 13 A class leaves **2.6× margin** over the Jetson module's derived 5 A draw
(25 W ÷ 5 V). The BEC input is connected to the 4S pack, within its 12S maximum; its 5 V
output is dedicated to the Jetson.

**Battery (Tattu 6S 5000 mAh 45C used as a cited example; note this is 6S, while the airframe is
specified 4S, so a 4S reference pack still needs sourcing):** 22.2 V, 111 Wh, 702 g (retailer
listing), 45C. **Per-cell internal resistance: UNKNOWN**: published only for Tattu's smart-battery
line via its app, never as a static datasheet spec. A brownout-margin check therefore has no
published operand to use.

---

## Mass-total and endurance budgets

`bud.mass.payload` (mass_total, sources: devices) first summed GPS 51.8 g + RFD900x 14 g +
Pixhawk 6C 59.3 g (aluminium case) + PM02 20 g = 145.1 g against Holybro's published 1500 g payload
capacity. That was a floor, since motor/ESC masses were then UNKNOWN or conflicting and excluded.

The motor/ESC mass conflict is now resolved (see Propulsion above: Holybro's own spare-parts store
publishes 80 g/motor and 50 g/ESC, superseding the earlier conflicting retailer figures), as is the
RFD900x mass (14.5 g, first-hand from RFDesign's own store page). Current sum: GPS
51.8 g + RFD900x 14.5 g + Pixhawk 6C 59.3 g + PM02 20 g + 4×motor 320 g + 4×ESC 200 g = **665.6 g**
(694.6 g in `contract_corrected.yaml`, which also carries the 29 g Castle Creations BEC).
This is closer to the real installed mass than the earlier floor, though Jetson module mass and
OAK-D Lite mass remain UNKNOWN and stay excluded rather than guessed.

`bud.endurance.mission` (endurance) first used `topology.battery` = 44.4 Wh (3000 mAh lower-bound
recommendation x 14.8 V nominal 4S) against a stated 15-minute flight-ops requirement, with only
Jetson + OAK-D Lite draw declared (30 W). The computed ~1.48 h figure covered avionics/payload draw
only; propulsion was UNKNOWN and excluded, so the number was not the aircraft's real endurance.

Propulsion is now included. Holybro's X500 V2 kit page publishes a genuine flight-time
anchor: "Flight time: ~18 minutes hover with no additional payload. Tested with 5000mAh Battery."
`topology.battery` is switched to match that tested pack (5000 mAh x 14.8 V nominal 4S = 74.0 Wh,
replacing the 3000 mAh lower-bound reading, since the flight-time figure is only valid against the
pack it was measured on). Total hover power = 74.0 Wh / 0.3 h = 246.68 W is divided evenly
across the 4 motors (61.67 W each, declared on `dev.motor.*` as mode `"hover"`; see the
Propulsion section above for the full caveat on this being aircraft-total, not motor-isolated,
power) and summed onto `rail.servo-motor` — which also gets its own `bud.power.servo-motor`
rail_power budget (246.68 W observed vs. 888 W declared capacity, a clean real pass). Total
battery draw for the endurance budget is now Jetson 25 W + OAK-D Lite 5 W + propulsion 246.68 W
+ radio 5.2 W transmit-peak (rolled up through `rail.pixhawk-telem1-5v`)
= 281.88 W. Computed endurance = 74.0 / 281.88 = **0.2625 h (~15.75 min)**, which still clears the
15-minute requirement, though by a thinner real margin than before the radio joined the power
tree — consistent with the published ~18-min hover figure being reduced by the added
companion/camera/radio payload. This is still hover-only (no climb/cruise/wind margin), and
per-motor draw remains a derived approximation, not an exact motor-only datasheet figure — but it
is no longer a number that silently ignores propulsion.

## Bus bitrates

Three buses carry a published default bitrate, and the OAK-D Lite ambiguity is resolved via its
official datasheet:

- `bus.uart.telemetry`: **57600 bps** — the RFD900x's own published default UART baud.
- `bus.uart.companion`: **921600 bps** — PX4's `SER_TEL2_BAUD` default
  ([docs.px4.io](https://docs.px4.io/main/en/peripherals/mavlink_peripherals.html)). Neither this
  nor the RFD900x figure above is a Holybro-Pixhawk-6C-board-specific number (the board's own
  spec sheet publishes no default for either port) — both are the best published figures
  available for their respective links.
- `bus.usb.companion`: **5,000,000,000 bit/s (5 Gbit/s, USB 3.1 Gen1)** — resolved from Luxonis's
  ambiguous "USB 2/3 (up to 10 Gbps)" marketing line by reading the OAK-D Lite's own datasheet PDF
  directly: it lists "USB3.1, gen2 10gbps" as an on-die controller-PHY feature, but states in
  prose that "data is then output to a host via USB 3.1 Gen1" — the actual operating link. See the
  OAK-D Lite section above.

**Reporting note.** Declaring a bitrate on a bus with no device declaring
`nominal_rate_hz`+`payload_bytes` traffic does not make `check_bus_bandwidth` *score* that bus (pass
or violate). It removes the bus from `not_scored` output entirely, because the check's `not_scored`
finding is gated on `bitrate is None`, and the "zero traffic declared" case with a known bitrate
emits **no finding at all**. So these three buses emit nothing in `lockwire check` output (neither
pass, violation, nor not_scored); only `bus.i2c.external` remains visibly flagged as unrated. Recording
a documented published default is worth that trade, but it is worth a second look before a contract
puts more devices onto these buses.

## Everything that came back UNKNOWN

Pixhawk 6C standalone current draw · Pixhawk 6C TELEM/UART default baud (Holybro's own spec sheet
publishes none; the 921600 bps used for `bus.uart.companion` is a PX4-project default, not this
board's own figure) · X500 V2 stated MTOW · Jetson throttle trip point · Jetson
module-only mass (dev-kit 176 g is the kit, not the module; module-only weight unpublished —
NVIDIA dev-kit hardware spec page; NVIDIA forum thread 339855, open) · Here3+ current draw ·
motor max continuous current / max power (Holybro publishes neither the kit page, spare-parts
store, nor a standalone motor spec sheet with this figure — only the hover-derived approximation
above exists) · ESC burst current (search-only, unconfirmed) · OAK-D Lite mass (61 g remains a
per-cell IR for a standard LiPo pack · bus.i2c.external bitrate (Pixhawk 6C
spec publishes the port, not its rate). RFD900x bundled antenna gain, listed as UNKNOWN in the
datasheet itself, is resolved from RFDesign's own RFD900x modem bundle page; see the RFD900x
table above.

## Methodology caveat

Several PDFs (Jetson datasheets, Here3 manual) did not render as extractable text, so those values
trace to a search engine's summarisation layer rather than a page that was parsed. They are flagged
inline. Treat them as lower confidence than a directly fetched HTML page or PDF, and re-verify
before any of them becomes a locked contract value.
