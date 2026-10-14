# PX4 ULog fixture

`18a01095-f04e-4801-a9f3-4040190fa3bb.ulg` is a time-windowed derivation of PX4
public flight log `18a01095-f04e-4801-a9f3-4040190fa3bb` (Holybro X500 V2,
2025-11-07). It retains the first three seconds of each selected physical sensor
subscription, preserving their different sample rates. The source is available
from the URL recorded in the metadata sidecar.

The source log is published by the PX4 Flight Review project under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution: PX4.

The full source is 13,621,614 bytes and is intentionally not committed. Recreate
the fixture with:

```console
python tests/fixtures/ulog/derive_fixture.py \
  18a01095-f04e-4801-a9f3-4040190fa3bb.full.ulg \
  tests/fixtures/ulog/18a01095-f04e-4801-a9f3-4040190fa3bb.ulg
```
