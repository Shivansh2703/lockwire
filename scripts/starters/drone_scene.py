"""One-off: write src/lockwire/examples/drone/scene.glb from the x500 fixture scene with the starter's
descriptive ids (stdlib only; run with `python3 scripts/starters/drone_scene.py`).

The geometry is tests/fixtures/x500/x500.glb; only node names and lockwire ids are renamed (RENAMES,
the same map the drone contract uses; test_cli applies it to the x500 fixture to prove they match)."""

import re
import struct
from pathlib import Path

ROOT = Path(__file__).parents[2]
RENAMES = {
    r"dev\.camera\.oak-d-lite": "dev.depth-camera",
    r"dev\.companion\.jetson-orin-nano": "dev.onboard-computer",
    r"dev\.gnss\.here3-plus": "dev.gnss-receiver",
    r"dev\.radio\.rfd900x": "dev.telemetry-radio",
    r"dev\.motor\.m(\d)": r"dev.motor.motor-\1",
    r"dev\.esc\.esc(\d)": r"dev.esc.esc-\1",
    r"rail\.pm02-5v": "rail.supply-5v",
    r"rail\.pixhawk-telem1-5v": "rail.telemetry-port-5v",
    r"rail\.servo-motor": "rail.motor-power",
    r"bud\.power\.pm02-5v": "bud.power.supply-5v",
    r"bud\.power\.pixhawk-telem1": "bud.power.telemetry-port",
    r"bud\.power\.servo-motor": "bud.power.motors",
}


def rename(text: str) -> str:
    for old, new in RENAMES.items():
        text = re.sub(old + r"(?![\w-])", new, text)
    return text


if __name__ == "__main__":
    data = (ROOT / "tests/fixtures/x500/x500.glb").read_bytes()
    n = struct.unpack("<I", data[12:16])[0]
    doc = rename(data[20 : 20 + n].decode())
    body = doc.encode()
    body += b" " * (-len(body) % 4)
    rest = data[20 + n :]
    out = (
        b"glTF"
        + struct.pack("<II", 2, 12 + 8 + len(body) + len(rest))
        + struct.pack("<I", len(body))
        + b"JSON"
        + body
        + rest
    )
    (ROOT / "src/lockwire/examples/drone/scene.glb").write_bytes(out)
