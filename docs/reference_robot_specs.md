# Reference robot: sourced specifications

Published numbers for the Holybro X500 V2 quadrotor behind the drone example
(`lockwire start --example drone`), and for the Jetson companion computer a builder might add to it.
Every value is a published figure, a figure derived from published ones (marked DERIVED), or
`UNKNOWN`. Nothing was estimated to fill a gap.

How firm each figure is: a row that cites the vendor's own page or datasheet is firm. A row marked
secondary source, retailer or forum is weaker and says so. DERIVED means computed from published
figures, with the working shown. UNKNOWN means nothing published was found.

The drone example is the flight stack as Holybro specifies it, with no companion computer, and
`lockwire check` is GO on it. The companion figures below are for adding one.

---

## Two things worth knowing first

### The X500 V2 is a **4S** airframe, not 6S

Holybro's own kit page recommends **4S 3000-5000 mAh, 20C+, XT60**. Every derived electrical number
(pack voltage, watt-hours, rail headroom) is therefore computed on 4S, not 6S.

### A Jetson companion on the PM02 5 V rail overloads it

The overload is a *derivation*, not a quoted figure: the 5 A is not published, it is derived.

| fact | value | how firm |
|---|---|---|
| PM02/PM06 regulated rail | **5.2 V @ 3 A max** | firm: Holybro; the PX4 hardware docs give the same figure |
| Orin Nano **module** `VDD_IN` | **5 V-20 V** supported | secondary source citing the NVIDIA module datasheet |
| Orin Nano **dev kit** barrel jack | **7 V-20 V**, ships a **19 V** brick | firm: NVIDIA quick-start + carrier spec |
| Dev-kit carrier -> module | steps the jack down; module `VDD_IN` is **5 V only** | forum: NVIDIA staff reply |
| Orin Nano power modes | 7 W / 15 W / 25 W | firm: NVIDIA docs |
| **"5 A at 5 V"** | **25 W / 5 V = 5 A** | **DERIVED, not quoted** |

**Why it matters.** The 5 A is the quotient of two figures the same vendor published on different
pages: they disagree only once you put them beside each other.

**Which build it applies to.** The overload holds for a **bare module on a custom carrier**, which
is what a real drone integration builds (the 176 g dev kit and its 19 V brick are dead weight on an
airframe). There, `VDD_IN` is 5 V, peak draw is 5 A, and the PM02's 3 A rail is short by 2 A. It
does **not** hold for the dev kit as shipped: that wants 7-20 V at the jack, so feeding it the
PM02's 5.2 V rail is a **voltage-range incompatibility**, a different and arguably worse failure.
The figures here use the bare-module reading; the 176 g dev-kit mass is the kit, not the module,
and is not used.

---

## Airframe: Holybro X500 V2 frame kit

| spec | value | source |
|---|---|---|
| wheelbase | 500 mm | holybro.com/products/x500-v2-kits |
| dry frame weight | 610 g (frame kit only) | same |
| recommended propeller | 1045 (10×4.5″) | same |
| payload capacity | 1500 g without battery, at 70% throttle | same |
| recommended battery | **4S** 3000-5000 mAh, 20C+, XT60 | same |
| max takeoff weight | **UNKNOWN**: Holybro publishes payload capacity, never a stated MTOW | - |

## Flight controller: Pixhawk 6C

| spec | value | source |
|---|---|---|
| mass | 59.3 g (aluminium case) / 34.6 g (plastic) | docs.holybro.com/autopilot/pixhawk-6c/technical-specification |
| input voltage (main) | max 6 V · USB 4.75-5.25 V · servo rail 0-36 V | same |
| primary IMU | ICM-42688-P | same |
| secondary IMU | BMI088 (BMI055 on earlier units, discontinued) | same + docs.px4.io |
| magnetometer / barometer | IST8310 / MS5611 | same |
| ports | 3× UART, 2× GPS, 2× CAN, 1× I2C, 16 PWM, 2 power inputs | same |
| board current draw | **UNKNOWN**: only per-port limiters published (Telem1 ≤1.5 A) | - |
| TELEM/UART default baud | **UNKNOWN**: the ports are published, a default bitrate is not | - |
| ethernet | **none**: not in the published port list, so no ethernet link is modeled | - |

### TELEM1 port rail

From the
[Holybro Pixhawk 6C technical specification](https://docs.holybro.com/autopilot/pixhawk-6c/technical-specification)
(Electrical data / Current Ratings):

> "Telem1 Max output current limiter: 1.5A"

The same section publishes no separate 5 V rating for the TELEM ports (only "Max input voltage: 6V"
and "USB Power Input: 4.75~5.25V"), so the TELEM1 rail is modeled as the PM02's 5.2 V passed
through the Pixhawk's power input unchanged (efficiency 1.0, pass-through, not a converter):
capacity = 1.5 A × 5.2 V = **7.8 W** (DERIVED). The RFD900x telemetry radio draws from it (see the
RFD900x table and the Power section).

### Flight controller to companion data link

The Pixhawk 6C publishes **3× UART** and names **Telem1**, but no default baud for those ports.
**USB** appears only as a power-input voltage range (4.75-5.25 V), not as a documented companion
data path. **Ethernet** is absent from the port list. No Jetson-side UART/USB-serial pinout is
published for the bare module either, so a companion link runs over the flight controller's
published TELEM/UART ports, at the PX4 default baud below.

## Companion: NVIDIA Jetson Orin Nano

| spec | value | how firm |
|---|---|---|
| **module-only mass** | **UNKNOWN**: dev-kit 176 g is the kit, not the module; module-only weight unpublished | NVIDIA dev-kit hardware spec page; NVIDIA forum thread 339855, open |
| power modes | 7 W / 15 W / 25 W; JetPack 6.2 adds uncapped MAXN SUPER | firm: NVIDIA docs |
| **module** `VDD_IN` range | **5-20 V** | secondary source citing the NVIDIA module datasheet |
| **dev-kit** DC barrel jack | **7-20 V**, 19 V PSU included | firm: NVIDIA quick-start + carrier spec |
| dev-kit carrier → module | steps down; module sees **5 V only** | forum: NVIDIA staff reply |
| dev-kit power input | use the **19 V** adapter on the barrel jack (row above), not a 5 V USB-C supply | 5 A at 5 V is a *derivation* (25 W / 5 V `VDD_IN`) that applies to the **module**, not the dev kit. See "A Jetson companion on the PM02 5 V rail overloads it". |
| thermal throttle trip point | **UNKNOWN**: Tj max 105 °C comes from a secondary source only, not confirmed against the datasheet | - |

## Sensors and links

| part | spec | value | how firm |
|---|---|---|---|
| Here3+ GNSS | mass | 51.8 g | retailer: irlock.com |
| | protocol | DroneCAN | retailer: irlock.com |
| | bus rate | **1 Mbit/s classic CAN** (the ArduPilot default); 8 Mbit/s only with CAN FD enabled | CubePilot advertises 8 Mbit/s for the Here3+ (vs 1 Mbit/s for the Here3), but that requires **CAN FD**, explicitly enabled. ArduPilot's **default is 1 Mbit/s classic CAN**, and DroneCAN's recommended rates are 1 / 0.5 / 0.25 / 0.125 Mbit/s for compatibility. **Do not put 8 Mbit/s in a bandwidth budget**: it would size the budget against a rate the bus is almost certainly not running at. Model the bus at its *configured* rate. |
| | update rate | 8 Hz | **retailer** source |
| | current draw | **UNKNOWN**: not published | - |
| RFD900x telemetry | weight | 14 g | secondary source; replaced by the 14.5 g figure below |
| | RF band | 902-928 MHz | secondary source |

### RFD900x telemetry radio

From the
[RFD900x DataSheet V1.2 (rfdesign.com.au)](https://files.rfdesign.com.au/Files/documents/RFD900x%20DataSheet%20V1.2.pdf)
unless a row says otherwise. These replace the secondary-source rows above.

| spec | value | source |
|---|---|---|
| power supply voltage | **+5 V nominal (+5 V min, +5.5 V max, +6 V abs max)** | RFD900x DataSheet V1.2 |
| TX current | **1 A peak at 30 dBm** | same |
| RX / standby current | **60 mA typical** | same |
| TX power | 0-30 dBm, adjustable | same |
| RX sensitivity (10⁻⁵ BER, by air data rate) | -111 dBm @ 12 kbps · -107 dBm @ 56 kbps · **-105 dBm @ 64 kbps (factory-default air rate)** · -102 dBm @ 100 kbps · -104 dBm @ 125 kbps · -98 dBm @ 200 kbps · -94 dBm @ 224 kbps | RFD900x DataSheet V1.2 p.5 table; default air rate from store.rfdesign.com.au |
| default UART baud | **57600 bps** (user-selectable: 9600/19200/38400/57600/115200/400k/921k) | same |
| RF connector | SMA | same |
| bundled antenna gain | **3 dBi half-wave dipole**. The datasheet itself names no antenna; RFDesign's modem bundle page lists "4 x Antennas, 900MHz Half wave dipole 3dBi", plus two 2.1 dBi monopole options not used here | [RFD900x Modem Bundle (USB-C)](https://store.rfdesign.com.au/rfd900x-modem-bundle-usb-c-hs-8517-62-00-90/) |
| module mass | **14.5 g** | [store.rfdesign.com.au: RFD900x Modem](https://store.rfdesign.com.au/rfd-900x-modem/) ("Weight: 14.5g"); replaces the 14 g secondary-source figure |
| power draw on the TELEM1 rail | **5.2 W transmit-peak** (1 A × 5.2 V, worst mode) · **0.312 W receive/standby** (60 mA × 5.2 V) | DERIVED: TX/RX current from the datasheet rows above × the PM02's 5.2 V passed through TELEM1 |

### OAK-D Lite camera

| spec | value | how firm |
|---|---|---|
| power | 2.5-3 W base, +1 W AI, +0.5 W depth, +0.5 W encode | firm: Luxonis docs |
| interface | USB-C | same |
| weight | 61 g, **conflicting**, absent from the official page and the datasheet | retailer; not used |
| USB link | **USB 3.1 Gen1 (5 Gbit/s)** | firm: datasheet (below) |

[docs.luxonis.com: OAK-D Lite](https://docs.luxonis.com/hardware/products/OAK-D%20Lite) says
"USB 2/3 (up to 10 Gbps)". The
[OAK-D Lite datasheet](https://github.com/luxonis/oak-hardware/blob/master/DM9095_OAK-D-LITE_DepthAI_USB3C/Datasheet/OAK-D-Lite_Datasheet.pdf)
lists "USB3.1, gen2 10gbps" as an on-chip controller feature, but states in prose: "The data is
then output to a host via USB 3.1 Gen1." So the operating link is **USB 3.1 Gen1, 5 Gbit/s
(5,000,000,000 bit/s)**; the 10 Gbps figure is the controller's ceiling, not the product's data path.

### Companion link default baud

The Pixhawk 6C's own specification states no default baud. PX4 publishes one for the companion
telemetry link, independent of the board:
[docs.px4.io: MAVLink Peripherals](https://docs.px4.io/main/en/peripherals/mavlink_peripherals.html)
gives `SER_TEL2_BAUD = 921600` as the default TELEM2 baud. This is a PX4 default, not a
Pixhawk-6C-specific figure. The RFD900x telemetry link uses the radio's own default, 57600 bps.

### IMU hard limits

| part | accel full-scale | gyro full-scale | source |
|---|---|---|---|
| ICM-42688-P | ±16 g = ±156.9064 m/s² | ±2000 dps = ±34.9066 rad/s | [invensense.tdk.com product page](https://invensense.tdk.com/products/motion-tracking/6-axis/icm-42688-p/) |
| BMI088 | ±24 g = ±235.3596 m/s² | ±2000 dps = ±34.9066 rad/s | [bosch-sensortec.com product page](https://www.bosch-sensortec.com/products/motion-sensors/imus/bmi088/) |

Conversions: 1 g = 9.80665 m/s²; dps → rad/s = dps × π/180. These are the maximum programmable
full-scale ratings (the datasheet's ceiling), used as hard limits, not as an operating envelope,
which would need fitted flight data.

### Magnetometer bus address: IST8310

[Isentek IST8310 datasheet](https://web.archive.org/web/20260308104841/https://tw.isentek.com/userfiles/files/IST8310Datasheet_3DMagneticSensors.pdf)
(archived copy): default I2C address **0x0E** (7-bit) when the CAD0/CAD1 address pins float.

## Propulsion

| spec | value | how firm |
|---|---|---|
| motor | 2216, 920 KV, 3S-4S | firm: Holybro kit page |
| motor weight | **80 g** | firm: Holybro's own spare-parts store, `https://holybro.com/products/spare-parts-x500-v2-kit.json`, SKU 520085 (CW)/520086 (CCW), `"weight": 80.0, "weight_unit": "g"`. Retailers list 63 g and ~150 g; the Holybro figure is used. |
| motor max continuous current / max power | **UNKNOWN** | not on the Holybro kit page, spare-parts store or spec sheet |
| motor hover-only draw | **61.67 W per motor** | DERIVED: Holybro's X500 V2 kit page publishes "Flight time: ~18 minutes hover with no additional payload. Tested with 5000mAh Battery." Total hover power = (5000 mAh × 14.8 V nominal 4S = 74.0 Wh) / (18 min = 0.3 h) = 246.68 W, divided evenly across 4 motors. This is the aircraft's *total* hover draw (propulsion + stock avionics), not a motor-isolated figure, so it modestly overstates true motor draw. There is no max-power figure, since no max-current or max-thrust figure is published. |
| ESC continuous | 20 A | firm: Holybro kit page |
| ESC burst | 30 A (10 s) | secondary source, not confirmed against the vendor page |
| ESC DShot support | DShot150/300/600 | secondary source, not confirmed against the vendor page |
| ESC weight | **50 g** | firm: Holybro's own spare-parts store, same JSON as above, SKU 520081, `"weight": 50.0, "weight_unit": "g"` |

## Power

| spec | PM02 (V3) | PM06 |
|---|---|---|
| input | 2S-12S | 2S-10S (14S variant exists) |
| current sense | up to 120 A | - |
| PCB rating | 60 A cont / 120 A burst | 70 A cont / 120 A burst |
| **regulated 5 V rail** | **5.2 V @ 3 A max** | **5.2 V @ 3 A max** (page also states 5.1-5.3 V) |
| weight | 20 g | 24 g (unconfirmed) |

### The TELEM1 rail adds to the PM02 rail

The Pixhawk 6C's power input runs off the PM02's regulated 5.2 V rail, and its Telem1 limiter
(1.5 A, see "TELEM1 port rail") sets the TELEM1 rail's capacity: 5.2 V × 1.5 A = **7.8 W**, fed from
the PM02 5 V rail. The RFD900x is its only consumer: 5.2 W transmit-peak, 0.312 W receive/standby.
Both add to the PM02 rail unchanged (pass-through efficiency 1.0). With a Jetson in its 7 W mode
and a 5 W camera also on the PM02 rail, the total is 7 W Jetson + 5 W camera + 5.2 W radio =
17.2 W, over the rail's 15.6 W. Moving the Jetson to its own regulator (below) does not change the
Pixhawk's supply: it still comes from the PM02.

### Jetson supply: Castle Creations CC BEC PRO (010-0004-01)

From the
[manufacturer product page](https://web.archive.org/web/20260810045934/https://www.castlecreations.com/en/cc-bec-pro-010-0004-01) (an archived copy: the page is no longer on Castle's site) and
[manufacturer user guide](https://www.freakware.de/technik/CastleCreations/cc_bec_pro_user_guide.pdf).

| spec | value | source |
|---|---|---|
| adjustable output | **4.8-12.5 V**; set to **5 V** for the Jetson | Castle Creations product page |
| peak current | **20 A** | same |
| continuous current by input voltage | **15 A @ 16 V in** · 13 A @ 24 V · 11 A @ 32 V · 9 A @ 40 V · 8 A @ 48 V | Castle Creations product page + user guide |
| maximum input | **50.4 V (12S)** | Castle Creations product page |
| mass | **29 g (1 oz)** | same |
| dimensions | **43×33×24 mm** | same |

**On 4S.** The X500 pack spans 14.8 V nominal to 16.8 V full, so it straddles the
continuous-current table's nearest published row, **15 A @ 16 V in**. Rely on nothing tighter than
**≥13 A-class continuous at 4S**. That 13 A class leaves **2.6× margin** over the Jetson module's
derived 5 A draw (25 W ÷ 5 V). The BEC input goes to the 4S pack, within its 12S maximum; its 5 V
output is dedicated to the Jetson.

**Battery (Tattu 6S 5000 mAh 45C, a cited example; it is 6S while the airframe is specified 4S;
no cited 4S pack is listed here):** 22.2 V, 111 Wh, 702 g (retailer listing), 45C. **Per-cell
internal resistance: UNKNOWN**: published only for Tattu's smart-battery line via its app, never as
a static datasheet spec, so a brownout-margin check has no published figure to use.

---

## Mass and endurance

**Payload mass.** Against Holybro's published 1500 g payload capacity, the flight stack sums to GPS
51.8 g + RFD900x 14.5 g + Pixhawk 6C 59.3 g + PM02 20 g + 4×motor 320 g + 4×ESC 200 g = **665.6 g**
(694.6 g with the 29 g Castle Creations BEC added for a Jetson). Jetson module mass and OAK-D Lite
mass are UNKNOWN and are left out, not guessed. Without the motor and ESC masses the stack sums to
145.1 g (with the 14 g secondary-source radio weight), which is a floor, not the installed mass.

**Endurance.** Holybro's X500 V2 kit page publishes "Flight time: ~18 minutes hover with no
additional payload. Tested with 5000mAh Battery." That figure only holds for the pack it was
measured on, so the battery is that pack: 5000 mAh x 14.8 V nominal 4S = 74.0 Wh (the 3000 mAh
lower bound of Holybro's recommendation gives 44.4 Wh). Total hover power = 74.0 Wh / 0.3 h =
246.68 W, divided evenly across the 4 motors (61.67 W each; see Propulsion for why this is
aircraft-total, not motor-isolated). The motor rail's capacity is 888 W (14.8 V × the PM02's 60 A
continuous rating, DERIVED), so 246.68 W passes with room.

With a Jetson (25 W), an OAK-D Lite (5 W) and the radio at transmit peak (5.2 W, through the TELEM1
rail), total battery draw is 25 W + 5 W + 246.68 W + 5.2 W = 281.88 W. Endurance = 74.0 / 281.88 =
**0.2625 h (~15.75 min)**, which still clears a 15-minute flight requirement, by a thin margin.
Counting only the Jetson and camera (30 W) would give ~1.48 h, which is wrong: it leaves out
propulsion. This is hover only (no climb, cruise or wind margin), and per-motor draw is a derived
approximation, not a motor-only datasheet figure.

## Bus bitrates

- Telemetry radio UART: **57600 bps**, the RFD900x's own published default.
- Companion UART: **921600 bps**, PX4's `SER_TEL2_BAUD` default
  ([docs.px4.io](https://docs.px4.io/main/en/peripherals/mavlink_peripherals.html)). Neither this
  nor the RFD900x figure is specific to the Pixhawk 6C board (its spec sheet publishes no default
  for either port); both are the best published figures for their links.
- Companion USB (OAK-D Lite): **5,000,000,000 bit/s (5 Gbit/s, USB 3.1 Gen1)**, from the
  OAK-D Lite datasheet (see the OAK-D Lite section).
- External I2C: **UNKNOWN**. The Pixhawk 6C spec publishes the port, not its rate.

## Everything that is UNKNOWN

Pixhawk 6C standalone current draw · Pixhawk 6C TELEM/UART default baud (Holybro's spec sheet
publishes none; 921600 bps is a PX4 default, not this board's own figure) · X500 V2 stated MTOW ·
Jetson throttle trip point · Jetson module-only mass (dev-kit 176 g is the kit, not the module;
NVIDIA dev-kit hardware spec page; NVIDIA forum thread 339855, open) · Here3+ current draw · motor
max continuous current / max power (only the hover-derived figure above exists) · ESC burst current
(secondary source only) · OAK-D Lite mass (61 g is a conflicting retailer figure) · per-cell IR for
a standard LiPo pack · external I2C bitrate.

## Lower-confidence figures

Rows marked secondary source, retailer or forum were not confirmed against the maker's own
document. Treat them as less firm than a figure read from a vendor page or datasheet.
