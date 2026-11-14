"""Attaching by frame id applies the part's frame orientation, not just its position."""

import types

from lockwire import catalog
from lockwire.schema import load_contract
from lockwire.edit import add_entry
from test_edit import _attachable


def test_attach_by_frame_id_applies_the_frames_orientation(tmp_path, monkeypatch):
    real = catalog.load_catalog()
    part = next(p for p in real.parts if p.id == "dynamixel.ax-12a")
    tilted = part.model_copy(
        update={"frames": [part.frames[0].model_copy(update={"rpy": (0.0, 1.5, 0.0)})]}
    )
    fake = types.SimpleNamespace(parts=[tilted])
    monkeypatch.setattr(catalog, "load_catalog", lambda *a, **k: fake)
    path = _attachable(tmp_path)
    joint = {"type": "fixed"}
    fields = {
        "parent": "base",
        "part": part.id,
        "frame_id": "horn",
        "frame": {"xyz": [None, 0.0, 0.01]},
        "joint": joint,
    }
    add_entry(path, "attach", fields, source="s", witness="w1")
    assert load_contract(path).model.joints[0].origin.rpy == (0.0, 1.5, 0.0)


def test_add_part_options_list_each_parts_frame_ids(tmp_path, monkeypatch):
    from lockwire.page import _add_part_options

    real = catalog.load_catalog()
    drawn = next(p for p in real.parts if "draw_max" in p.numbers)
    servo = next(p for p in real.parts if p.id == "dynamixel.ax-12a")
    powered = servo.model_copy(
        update={"numbers": {**servo.numbers, "draw_max": drawn.numbers["draw_max"]}}
    )
    monkeypatch.setattr(
        catalog, "load_catalog", lambda *a, **k: real.model_copy(update={"parts": [powered, drawn]})
    )
    parts = {p["id"]: p for p in _add_part_options(load_contract(_attachable(tmp_path)))["parts"]}
    assert (parts[servo.id]["frames"], parts[drawn.id]["frames"]) == (["horn"], [])


def test_an_attached_powered_part_still_loads_its_rail(tmp_path):
    """The app sends the device add and the attach for one component; both must land."""
    from lockwire.checks import _worst_rail_power

    path = _attachable(tmp_path)
    add_entry(
        path, "rail", {"id": "rail.t", "voltage": "5 V", "capacity": "1 W"}, source="s", witness="w"
    )
    device = {
        "id": "dev.servo",
        "family": "fam.s",
        "model": "mdl.s",
        "owner": "you",
        "power_draw": "2 W",
        "power_rail": "rail.t",
    }
    add_entry(path, "device", device, source="s", witness="w")
    attach = {
        "parent": "base",
        "part": "dynamixel.ax-12a",
        "name": "servo",
        "frame": {"xyz": [0.0, 0.0, 0.01]},
        "joint": {"type": "fixed"},
    }
    add_entry(path, "attach", attach, source="s", witness="w")
    contract = load_contract(path)
    assert contract.model.joints[0].child == "servo"
    assert _worst_rail_power(contract, "rail.t")[0] == 2.0
