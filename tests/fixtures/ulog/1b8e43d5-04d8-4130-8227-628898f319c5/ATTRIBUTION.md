# PX4 labeled ULog fixture 1b8e43d5

These are time-windowed derivations of PX4 public log
`1b8e43d5-04d8-4130-8227-628898f319c5` (unnamed airframe, airframe type
`4002`, Quadrotor, 243 seconds, 2020-11-19). The PX4 metadata sidecar supplies
only numeric ground truth `error_labels: [2]`; it does not name label 2, so
this fixture does not invent a meaning for it.

The derivation rebases each selected physical-topic subscription's first
sample to 0 seconds. Its leading 0–8 second interval precedes the explicit
`Auto activation failed` message at 9.298 seconds in the ULog logging-start
frame (9.044 seconds in the first-timestamp frame), while the checked 9–21
second interval contains that failure message. The sidecar's flight-mode
durations change from mode 2 to mode 3 at 9 seconds, so detector findings in
the checked segment can also reflect that operating-mode transition. This is
a within-flight split; it does not compare this vehicle with another airframe.

The source is published by the PX4 Flight Review project under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution: PX4.
The full 9,394,355-byte source is intentionally not committed; its SHA-256 is
`9519c9aa5b44d8707adc9a2f51ab3b771520d868386abc4057cf2ad35a0e4d58`.

Recreate the fixtures with:

```console
python tests/fixtures/ulog/derive_fixture.py \
  1b8e43d5-04d8-4130-8227-628898f319c5.full.ulg \
  tests/fixtures/ulog/1b8e43d5-04d8-4130-8227-628898f319c5/baseline.ulg \
  --window-seconds 8
python tests/fixtures/ulog/derive_fixture.py \
  1b8e43d5-04d8-4130-8227-628898f319c5.full.ulg \
  tests/fixtures/ulog/1b8e43d5-04d8-4130-8227-628898f319c5/failure.ulg \
  --offset-seconds 9 --window-seconds 12
```

Derived fixture pins:

- `baseline.ulg`: 144,184 bytes,
  SHA-256 `8da50b95e0e00612c501385dd443c92440530fb6aa2863305e232fb50fa23878`
- `failure.ulg`: 192,400 bytes,
  SHA-256 `6e184bacd6eebb55678095d6ea6774f68b34197da77e6ff51d8040218857d70a`
