# PX4 labeled ULog fixture bff22af6

These are time-windowed derivations of PX4 public log
`bff22af6-8b5f-4945-bc49-f17baeedbf72` (Generic Quadcopter / Quadrotor x,
131 seconds, 2025-02-18). The PX4 metadata sidecar supplies only numeric
ground truth `error_labels: [6]`; it does not name label 6, so this fixture
does not invent a meaning for it.

The derivation rebases each selected physical-topic subscription's first
sample to 0 seconds. Its leading 0–20 second interval precedes the first
explicit calibration-failure message at 28.212 seconds in the ULog
logging-start frame (28.006 seconds in the first-timestamp frame), while the
checked 25–35 second interval contains that failure sequence (28.212–31.644
seconds, logging-start frame). The vehicle is never armed and
`vehicle_status.failsafe` is true throughout the recorded physical interval;
this is a ground/commissioning log, not an in-air failure capture.

The source is published by the PX4 Flight Review project under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution: PX4.
The full 8,535,709-byte source is intentionally not committed; its SHA-256 is
`230d35ffca46722a870eccf2653e3839a032176157d1ad61d01558243556e78a`.

Recreate the fixtures with:

```console
python tests/fixtures/ulog/derive_fixture.py \
  bff22af6-8b5f-4945-bc49-f17baeedbf72.full.ulg \
  tests/fixtures/ulog/bff22af6-8b5f-4945-bc49-f17baeedbf72/baseline.ulg \
  --window-seconds 20
python tests/fixtures/ulog/derive_fixture.py \
  bff22af6-8b5f-4945-bc49-f17baeedbf72.full.ulg \
  tests/fixtures/ulog/bff22af6-8b5f-4945-bc49-f17baeedbf72/failure.ulg \
  --offset-seconds 25 --window-seconds 10
```

Derived fixture pins:

- `baseline.ulg`: 244,648 bytes,
  SHA-256 `4bbef67ff4f5c2cd3ecded29619c1afb0279c40ca085ca1cc79b1ed1f342e659`
- `failure.ulg`: 154,344 bytes,
  SHA-256 `a51d665d4a6b5c7d5e5bcbd82efba709c944e7c9faed81cc45a0c80a23c8b983`
