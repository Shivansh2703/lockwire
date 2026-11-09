# Probes: what `lockwire sample` reads, and why each read cannot move hardware

**Lockwire never actuates.** "No persistent device write" is too weak a claim: a transient
command can move hardware, and on many devices a *read* clears a register. So this file argues the
safety of every probe per device class, and names the classes where a read is **not** safe, which
lockwire therefore refuses to touch at all.

The table below is held to the code by `tests/test_sample_remote.py`. The single shared list lives in
`src/lockwire/sample_remote.py` (`PROBES` and `REFUSED`); a class added there without a row here
fails the suite.

**Untested on hardware.** Nothing in `sample --host` has run against a real board. The reasoning
below is from the kernel ABI documentation and the device datasheets, not from a measurement.

## Probed — read-only, and why a read is safe

| device class | paths read | why a read is safe |
|---|---|---|
| `thermal` | `/sys/class/thermal/thermal_zone*/type`, `/sys/class/thermal/thermal_zone*/temp` | sysfs thermal attributes are kernel-cached values re-read from the SoC sensor by the thermal core on its own timer; the open/read does not issue a bus transaction and clears nothing. |
| `cooling` | `/sys/class/thermal/cooling_device*/cur_state` | cur_state is readable and writable; only the write actuates a fan or a frequency cap. The read returns the cooling device's current state and has no side effect. lockwire never opens it for writing. |
| `hwmon` | `/sys/class/hwmon/hwmon*/name`, `.../in*_input`, `.../curr*_input`, `.../temp*_input`, `.../power*_input`, `.../energy*_input` | Only the *_input attributes are read. The kernel hwmon driver owns the bus and performs the conversion; the sysfs read returns its result. The alarm/limit attributes that a read *does* clear on some chips (*_alarm, *_max_alarm) are deliberately not in this list. |
| `loadavg` | `/proc/loadavg` | A procfs counter of the scheduler's own state. No device is involved. |
| `net` | `/proc/net/dev` | Monotonic per-interface counters kept by the network stack. Reading them does not reset them (unlike an ethtool register dump on some NICs). |

## Refused — a read here is not safe, so there is no probe

| device class | why a read is not safe |
|---|---|
| `i2c/smbus` | A read is a bus transaction the host originates: an address write followed by a read. Many registers are read-to-clear (INA219 status, PCA9685 fault bits) and a raw probe of an unknown address can latch a device into a mode. Refused: the kernel driver's hwmon view is read instead. |
| `serial/uart` | Opening a tty asserts modem control lines (DTR/RTS) and resets many boards, and a protocol read requires sending a request frame first — a transient command that can move hardware. Refused entirely. |
| `spi` | SPI has no read without a write: every transfer clocks bytes out as it clocks bytes in. There is no non-actuating probe. Refused. |
| `vcgencmd` | Not unsafe, but not transportable: it is a command run on the board, not a file, so it cannot be mirrored. `sample --host` reports it as an absent category rather than splicing in the *local* machine's answer. |

## The remote path is a transport, not a second sampler

`sample --host` copies the files in the first table into a temporary mirror directory and runs the
*same* `sample_local()` collection against it (one parser, one set of divisors). ssh is invoked
with an argument list, never a shell string; the remote command is the constant `python3 -` with the
program on stdin, so no caller-supplied text reaches any shell. The destination is validated against
a strict charset and passed after `--`, so a hostile "hostname" like `-oProxyCommand=…` cannot be
read by ssh as an option.

Remote sampling requires an existing file of independently verified host keys:
`lockwire sample --host robot.local --known-hosts ./robot_known_hosts --out ./recording`.
That file is the sole host trust source; SSH aliases, routing and authentication
settings still apply. Unknown or changed keys fail without enrolling new keys or
changing the supplied file.

**Ceiling, stated:** one snapshot per invocation. A timed series over ssh would be one round trip per
tick; the real answer is the on-robot sampler E8 packages. No service, daemon or unit file is
installed on the robot by this path.
