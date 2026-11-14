"""The scene packing and hash are numpy, byte-identical to the per-element loops they replaced."""

import base64
import hashlib
import math
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from lockwire import scene_view
from lockwire.scene_view import _pack_indices, _pack_vectors, scene_hash, scene_payload


def _legacy_pack(values, *, normals=False):  # the pre-numpy loop, kept as the reference
    if normals:
        values = tuple(
            c / n if (n := math.hypot(*values[i : i + 3])) else c
            for i in range(0, len(values), 3)
            for c in values[i : i + 3]
        )
        offset, scale = [0.0, 0.0, 0.0], 1 / 32767
    else:
        low = [min(values[i::3], default=0) for i in range(3)]
        high = [max(values[i::3], default=0) for i in range(3)]
        offset = [(a + b) / 2 for a, b in zip(low, high, strict=True)]
        scale = max((b - a) / 65534 for a, b in zip(low, high, strict=True)) or 1.0
    ints = [round((v - offset[i % 3]) / scale) for i, v in enumerate(values)]
    out = {"type": "i16", "data": base64.b64encode(struct.pack(f"<{len(ints)}h", *ints)).decode()}
    return out if normals else {**out, "scale": scale, "offset": offset}


def _values(count):  # mixed magnitudes, a zero normal, a non-unit normal, exact half-steps
    seed = [0.0, 0.0, 0.0, 2.0, -3.0, 6.0, 0.3, 0.4, 0.5, 1e-9, -7.5, 1234.5678, 1.0, 0.0, 0.0]
    return tuple(seed[i % len(seed)] * (1 + i % 7) / (1 + i % 3) for i in range(count * 3))


@pytest.mark.parametrize("normals", [False, True])
@pytest.mark.parametrize("count", [0, 1, 5, 4000])
def test_pack_vectors_matches_the_loop(count, normals):
    assert _pack_vectors(_values(count), normals=normals) == _legacy_pack(
        _values(count), normals=normals
    )


def test_pack_indices_u16_and_u32():
    assert _pack_indices((0, 1, 65535)) == {
        "type": "u16",
        "data": base64.b64encode(struct.pack("<3H", 0, 1, 65535)).decode(),
    }
    assert _pack_indices((0, 70000))["type"] == "u32"


def test_scene_hash_matches_the_struct_pack_digest():
    part = SimpleNamespace(
        node="a",
        positions=_values(10),
        normals=_values(10),
        indices=tuple(range(30)),
        color=(0.1, 0.2, 0.3, 1.0),
        translation=(1.0, 2.0, 3.0),
    )
    h = hashlib.sha256(b"a")
    for seq in (part.positions, part.normals):
        h.update(struct.pack(f"<{len(seq)}d", *seq))
    h.update(
        struct.pack("<30I", *part.indices)
        + struct.pack("<4d", *part.color)
        + struct.pack("<3d", *part.translation)
    )
    assert scene_hash(SimpleNamespace(parts=(part,))) == h.hexdigest()


@pytest.mark.parametrize("normals", [False, True])
def test_pack_vectors_calls_numpy_once_per_mesh_not_per_value(normals, monkeypatch):
    calls = []
    real = np.rint
    monkeypatch.setattr(np, "rint", lambda *a, **k: calls.append(1) or real(*a, **k))
    _pack_vectors(_values(3000), normals=normals)
    assert len(calls) == 1  # a per-triangle or per-value loop would call it 3000 or 9000 times


def test_payload_packs_each_scene_once_then_serves_the_cache(monkeypatch):
    from lockwire.schema import load_contract
    from lockwire.scene import load_scene
    from tests.test_api_payload import X500

    contract, scene = load_contract(X500), load_scene(X500.with_name("x500.glb"))

    def ask():
        return scene_payload(contract, scene, source="x", cards=[], scored_records=[], labels=[])

    first = ask()
    monkeypatch.setattr(scene_view, "_pack_vectors", lambda *a, **k: pytest.fail("repacked"))
    assert ask()["parts"][0]["positions"] == first["parts"][0]["positions"]


def test_editing_a_mesh_file_in_place_is_not_served_from_the_cache(tmp_path):
    from lockwire.scene_urdf import load_robot_scene
    from lockwire.schema import load_contract
    from tests.test_api_payload import X500

    def stl(x):  # one triangle, third vertex at (x, 1, 0)
        rec = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, x, 1, 0, 0)
        return b"\0" * 80 + struct.pack("<I", 1) + rec

    (tmp_path / "m.urdf").write_text(
        '<robot name="r"><link name="b"><visual><geometry><mesh filename="m.stl"/>'
        "</geometry></visual></link></robot>"
    )
    contract = load_contract(X500)

    def positions():
        scene = load_robot_scene(tmp_path / "m.urdf")
        out = scene_payload(contract, scene, source="m", cards=[], scored_records=[], labels=[])
        return out["parts"][0]["positions"]

    (tmp_path / "m.stl").write_bytes(stl(1.0))
    before = positions()
    (tmp_path / "m.stl").write_bytes(stl(5.0))
    assert positions() != before
