"""Join a glTF scene to a contract for the page payload.

``resolve_scene`` assigns each ``ScenePart`` a contract id — the join key
(extras id, else a name in its chain that matches a contract id, else None =
structure) — and reports the coverage gap both ways. Part *state* (fault,
warn, affected, ok, unchecked, plain) is computed here from data the page has
already scored: cards and scored records. No check ever runs twice for a
picture.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lockwire.copy import human_name
from lockwire.scene import Scene, ScenePart, SceneRefusal
from lockwire.schema import Contract

_ROUND = 4
_DISPLAY_TRIANGLES = 150_000


def contract_ids(contract: Contract) -> set[str]:
    """Every id a scene part may legitimately claim (devices, rails, buses, links)."""
    ids = {device.id for device in contract.devices}
    if contract.topology is not None:
        ids |= {rail.id for rail in contract.topology.rails}
        ids |= {bus.id for bus in contract.topology.buses}
    if contract.model is not None:
        ids |= {link.name for link in contract.model.links}
    return ids


def _part_id(part: ScenePart, ids: set[str]) -> str | None:
    if part.extras_id is not None:
        return part.extras_id
    for name in part.name_chain:  # root -> self; first match wins
        if name in ids:
            return name
    return None


@dataclass(frozen=True)
class ResolvedScene:
    part_ids: tuple[str | None, ...]  # parallel to scene.parts
    scene_only: tuple[str, ...]  # claimed in the scene, absent from the contract
    contract_only: tuple[str, ...]  # in the contract, no part claims it


def resolve_scene(contract: Contract, scene: Scene) -> ResolvedScene:
    ids = contract_ids(contract)
    part_ids = tuple(_part_id(part, ids) for part in scene.parts)
    claimed = {pid for pid in part_ids if pid is not None}
    return ResolvedScene(
        part_ids=part_ids,
        scene_only=tuple(sorted(claimed - ids)),
        contract_only=tuple(sorted(ids - claimed)),
    )


def part_groups(contract: Contract, part_ids: tuple[str | None, ...]) -> list[dict[str, str]]:
    """the drop-down each scene part sits under, parallel to ``part_ids``. A part
    belongs to the contract component it resolved to (device, rail, bus or link); a part
    with no contract id, or one the contract does not know, goes under "other". The label
    is ``copy.human_name`` of the id, the same plain name every other face shows."""
    kinds: dict[str, str] = (
        {link.name: "link" for link in contract.model.links} if contract.model else {}
    )
    kinds |= {d.id: "device" for d in contract.devices}
    if contract.topology is not None:
        kinds |= {r.id: "rail" for r in contract.topology.rails}
        kinds |= {b.id: "bus" for b in contract.topology.buses}

    return [
        {"id": pid, "label": human_name(pid), "kind": kinds[pid]}
        if pid in kinds
        else {"id": "other", "label": "Other", "kind": "other"}
        for pid in part_ids
    ]


#: Every lens a scene part belongs to, keyed by the same
#: contract id the wiring diagram joins on (``report_html._graph``'s node
#: sets). ``off`` and ``health`` are whole-scene overlays — every part in the
#: scene is drawn under them — so they are added to every id's set below,
#: never left for a part to fall out of.
_OVERLAY_LENSES: tuple[str, ...] = ("off", "health")


def part_lenses(contract: Contract) -> dict[str, list[str]]:
    """id -> every lens that id is drawn in, mirroring ``report_html._graph``'s
    node membership: a link is mechanical; a device is mechanical always, plus
    power/data when it declares that half of the contract; a rail is power; a
    bus is data. Structure with no contract id (``_part_id`` returns ``None``)
    is not covered here — ``scene_payload`` falls back to mechanical-only for
    it, since it is bolted-on geometry with no electrical or data role."""
    lenses: dict[str, set[str]] = {}
    for link in contract.model.links if contract.model is not None else []:
        lenses.setdefault(link.name, set()).add("mechanical")
    for device in contract.devices:
        claimed = lenses.setdefault(device.id, set())
        claimed.add("mechanical")
        if device.power is not None:
            claimed.add("power")
        if device.identity is not None:
            claimed.add("data")
    if contract.topology is not None:
        for rail in contract.topology.rails:
            lenses.setdefault(rail.id, set()).add("power")
        for bus in contract.topology.buses:
            lenses.setdefault(bus.id, set()).add("data")
    return {id_: sorted(members | set(_OVERLAY_LENSES)) for id_, members in lenses.items()}


def ids_in_quantity(contract: Contract, quantity: str) -> list[str]:
    """The contract ids a quantity path is about — same resolution ``gui._faults`` uses."""
    found: list[str] = []
    if quantity.startswith("budgets/"):
        wanted = quantity.removeprefix("budgets/").split("/", 1)[0]
        for budget in contract.budgets:
            if budget.id == wanted and budget.rail and budget.rail not in found:
                found.append(budget.rail)
    for part in quantity.split("/"):
        if "." in part and not part.startswith("bud.") and part not in found:
            found.append(part)
    return found


def _rail_consumers(contract: Contract, rail_id: str) -> set[str]:
    """Everything a scored ``rail_id`` vouches for: its direct devices, plus every
    rail downstream of it (``upstream == rail_id``), recursed, plus theirs."""
    consumers = {
        device.id
        for device in contract.devices
        if device.power is not None and device.power.rail == rail_id
    }
    if contract.topology is not None:
        for rail in contract.topology.rails:
            if rail.upstream == rail_id:
                consumers.add(rail.id)
                consumers |= _rail_consumers(contract, rail.id)
    return consumers


def _bus_consumers(contract: Contract, bus_id: str) -> set[str]:
    """Devices identified onto a scored ``bus_id``."""
    return {
        device.id
        for device in contract.devices
        if device.identity is not None and device.identity.bus_ref == bus_id
    }


def part_states(
    contract: Contract, cards: list[dict[str, Any]], scored_records: list[Any]
) -> dict[str, str]:
    """id -> state. Precedence: fault > warn > affected > ok > unchecked > plain.

    A scored rail or bus reaches past its own id: every device that draws from
    it directly, every rail downstream of it (``upstream`` chases the tree,
    recursively) and that rail's own devices, and every device identified onto
    a scored bus, all read ``ok`` too -- unless a worse state already claimed
    them (``setdefault`` never demotes fault/warn/affected). A rail's pass adds
    up because everything feeding it was rolled in; the parts that fed it earn
    the same colour.

    Placement blind spots (``devices/<id>/placement`` not_scored -- shape
    ``.../placement``) never grey a part: an undeclared mount point is a real
    gap the card and honesty line already say, but "nobody has scored this
    part's placement" is not the same claim as "nobody has scored this part" --
    a device with nothing scored and nothing unchecked but its placement reads
    plain. A real placement WARNING (past tolerance) still colours warn, via
    the loop above this one.
    """
    state: dict[str, str] = {}
    for card in cards:
        if card["severity"] == "violation":
            for i in card["faults"]:
                state.setdefault(i, "fault")
    for card in cards:
        if card["severity"] == "warning":
            for i in card["faults"]:
                state.setdefault(i, "warn")
    for card in cards:
        for i in card["affected"]:
            state.setdefault(i, "affected")
    rail_ids = {rail.id for rail in contract.topology.rails} if contract.topology else set()
    bus_ids = {bus.id for bus in contract.topology.buses} if contract.topology else set()
    for record in scored_records:
        for i in ids_in_quantity(contract, record.quantity):
            state.setdefault(i, "ok")
            if i in rail_ids:
                for consumer in _rail_consumers(contract, i):
                    state.setdefault(consumer, "ok")
            elif i in bus_ids:
                for consumer in _bus_consumers(contract, i):
                    state.setdefault(consumer, "ok")
    for card in cards:
        if card["severity"] == "not_scored" and not card.get("shape", "").endswith("/placement"):
            for i in card["faults"]:
                state.setdefault(i, "unchecked")
    return state


def _round(values: tuple[float, ...]) -> list[float]:
    return [round(v, _ROUND) for v in values]


def _pack_vectors(values: tuple[float, ...], *, normals: bool = False) -> dict[str, Any]:
    """Little-endian int16 xyz; quantization error is at most half a scale step.

    Positions use a local midpoint and one scale per part. Normals already lie
    in [-1, 1], so their fixed 1/32767 scale is implicit in the page decoder.
    Packing affects the picture only, never the placement checks.
    """
    if normals:
        # File normals need not be unit length (STL/OBJ/DAE); unscaled they overflow int16.
        values = tuple(
            c / n if (n := math.hypot(*values[i : i + 3])) else c
            for i in range(0, len(values), 3)
            for c in values[i : i + 3]
        )
        offset = [0.0, 0.0, 0.0]
        scale = 1 / 32767
    else:
        low = [min(values[i::3], default=0) for i in range(3)]
        high = [max(values[i::3], default=0) for i in range(3)]
        offset = [(a + b) / 2 for a, b in zip(low, high, strict=True)]
        scale = max((b - a) / 65534 for a, b in zip(low, high, strict=True)) or 1.0
    integers = [round((v - offset[i % 3]) / scale) for i, v in enumerate(values)]
    result = {
        "type": "i16",
        "data": base64.b64encode(struct.pack(f"<{len(integers)}h", *integers)).decode("ascii"),
    }
    if not normals:
        result["scale"] = scale
        result["offset"] = offset
    return result


def scene_hash(scene: Scene) -> str:
    """Sha256 identity of the scene's **source** geometry — one part at a
    time, node name then positions/normals/indices/color/translation as
    doubles, so the hash is stable across runs and independent of display
    decimation or int16 packing (both happen after this). This is what a
    caller's ``scene.have`` is compared against: same geometry, same hash, so a
    client that already has it need not re-download 86% of the reply.
    """
    hasher = hashlib.sha256()
    for part in scene.parts:
        hasher.update(part.node.encode("utf-8"))
        hasher.update(struct.pack(f"<{len(part.positions)}d", *part.positions))
        if part.normals is not None:
            hasher.update(struct.pack(f"<{len(part.normals)}d", *part.normals))
        hasher.update(struct.pack(f"<{len(part.indices)}I", *part.indices))
        hasher.update(struct.pack("<4d", *part.color))
        hasher.update(struct.pack("<3d", *part.translation))
    return hasher.hexdigest()


def _pack_indices(values: tuple[int, ...]) -> dict[str, str]:
    wide = max(values, default=0) > 65535
    raw = struct.pack(f"<{len(values)}{'I' if wide else 'H'}", *values)
    return {"type": "u32" if wide else "u16", "data": base64.b64encode(raw).decode("ascii")}


def _display_part(part: ScenePart, stride: int) -> ScenePart:
    """Uniform face sampling for the picture only; compact all vertex attributes.

    ponytail: sampling preserves individual faces, not a watertight surface.
    Use mesh simplification if display fidelity needs more than this bounded view.
    """
    if stride == 1:
        return part
    kept = [
        index
        for start in range(0, len(part.indices), 3 * stride)
        for index in part.indices[start : start + 3]
    ]
    remap = {index: i for i, index in enumerate(dict.fromkeys(kept))}
    return replace(
        part,
        positions=tuple(v for i in remap for v in part.positions[i * 3 : i * 3 + 3]),
        normals=tuple(v for i in remap for v in part.normals[i * 3 : i * 3 + 3])
        if part.normals is not None
        else None,
        indices=tuple(remap[i] for i in kept),
    )


def scene_asset_identity(
    contract: Contract, scene: Scene, contract_path: Path | None
) -> dict[str, Any]:
    """Bind only the selected root file, never its external asset graph."""
    expected = None
    if contract_path is not None and contract.model is not None and scene.asset_path is not None:
        declared = Path(contract.model.source)
        if not declared.is_absolute():
            declared = contract_path.parent / declared
        if declared.resolve() == Path(scene.asset_path).resolve():
            expected = contract.model.source_sha256
            if scene.asset_sha256 != expected:
                raise SceneRefusal(
                    f"{scene.asset_path}: selected asset sha256 {scene.asset_sha256} "
                    f"does not match declared source sha256 {expected}."
                )
    return {
        "asset_path": scene.asset_path,
        "asset_sha256": scene.asset_sha256,
        "expected_asset_sha256": expected,
    }


def scene_payload(
    contract: Contract,
    scene: Scene,
    *,
    source: str,
    cards: list[dict[str, Any]],
    scored_records: list[Any],
    labels: list[dict[str, Any]],
    decimate: bool = True,
    contract_path: Path | None = None,
) -> dict[str, Any]:
    """Everything a client draws for the scene, in one deterministic dict.

    ``decimate`` gates the 150,000-triangle display cap: ``gui --out`` (a
    static page, one shot, no re-fetch) wants it; the sidecar's API wants the
    parts un-decimated (v8 line 317) since the app can re-sample for its own
    viewport. The hash covers the source geometry regardless of ``decimate``
    (``scene_hash``), so a decimated and an un-decimated reply of the same
    scene carry the same hash.
    """
    resolved = resolve_scene(contract, scene)
    states = part_states(contract, cards, scored_records)
    lenses = part_lenses(contract)
    groups = part_groups(contract, resolved.part_ids)
    mounts = {
        d.id: d.physical.mount_frame
        for d in contract.devices
        if d.physical and d.physical.mount_frame
    }
    triangles = sum(len(part.indices) // 3 for part in scene.parts)
    stride = max(1, (triangles + _DISPLAY_TRIANGLES - 1) // _DISPLAY_TRIANGLES) if decimate else 1
    display_parts = [_display_part(part, stride) for part in scene.parts]
    display_triangles = sum(len(part.indices) // 3 for part in display_parts)
    honesty = "shapes come from the model; positions are declared only where the contract says so"
    if stride > 1:
        honesty += f"; model simplified for display: {display_triangles} of {triangles} triangles"
    parts = [
        {
            "id": part_id,
            "node": part.node,
            "name": human_name(re.sub(r"\.\d+$", "", part.node)),
            "state": states.get(part_id, "plain") if part_id is not None else "plain",
            "positions": _pack_vectors(part.positions),
            "normals": _pack_vectors(part.normals, normals=True)
            if part.normals is not None
            else None,
            "indices": _pack_indices(part.indices),
            "color": _round(part.color),
            "translation": _round(part.translation),
            # structure with no contract id earns mechanical-only (plus
            # the whole-scene overlays) rather than falling out of the sweep.
            "group": group,
            "lenses": lenses.get(part_id, sorted({"mechanical", *_OVERLAY_LENSES})),
            "mount_frame": mounts.get(part_id),
        }
        for part, part_id, group in zip(display_parts, resolved.part_ids, groups, strict=True)
    ]
    return {
        **scene_asset_identity(contract, scene, contract_path),
        "source": source,
        "hash": scene_hash(scene),
        "parts": parts,
        "labels": labels,
        "scene_only": list(resolved.scene_only),
        "contract_only": list(resolved.contract_only),
        "bounds": [_round(scene.bounds[0]), _round(scene.bounds[1])],
        "triangles": scene.triangles,
        "vertices": scene.vertices,
        "display_triangles": display_triangles,
        "honesty": honesty,
    }
