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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lockwire.catalog import _cached, _stamp
from lockwire.copy import human_name
from lockwire.scene import Scene, ScenePart, SceneRefusal
from lockwire.schema import Contract

_ROUND = 4
# Measured on a 1.09M-triangle robot (XYZ DEUX): the app draws all of it at 60 fps, so the cap sits
# above that; only models bigger than this are clustered down.
_DISPLAY_TRIANGLES = 2_000_000


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

    numpy, not a per-element Python loop (1.09M triangles took 22 s that way); every step is
    the same double arithmetic and round-half-even as before, so the bytes are identical.
    """
    import numpy as np

    flat = np.asarray(values, dtype=np.float64)
    if normals:
        # File normals need not be unit length (STL/OBJ/DAE); unscaled they overflow int16.
        xyz = flat.reshape(-1, 3)
        # math.hypot, not np.sqrt: its rounding is what the bytes were pinned with
        length = np.fromiter(map(math.hypot, *(c.tolist() for c in xyz.T)), dtype=np.float64, count=len(xyz))
        flat = np.divide(xyz, length[:, None], out=xyz.copy(), where=length[:, None] != 0).ravel()
        offset = [0.0, 0.0, 0.0]
        scale = 1 / 32767
    else:
        low = flat.reshape(-1, 3).min(axis=0).tolist() if flat.size else [0, 0, 0]
        high = flat.reshape(-1, 3).max(axis=0).tolist() if flat.size else [0, 0, 0]
        offset = [(a + b) / 2 for a, b in zip(low, high, strict=True)]
        scale = max((b - a) / 65534 for a, b in zip(low, high, strict=True)) or 1.0
    integers = np.rint((flat.reshape(-1, 3) - offset) / scale).astype("<i2")
    result = {"type": "i16", "data": base64.b64encode(integers.tobytes()).decode("ascii")}
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
    import numpy as np

    def doubles(values: tuple[float, ...]) -> bytes:
        return np.asarray(values, dtype="<f8").tobytes()

    hasher = hashlib.sha256()
    for part in scene.parts:
        hasher.update(part.node.encode("utf-8"))
        hasher.update(doubles(part.positions))
        if part.normals is not None:
            hasher.update(doubles(part.normals))
        hasher.update(np.asarray(part.indices, dtype="<u4").tobytes())
        hasher.update(doubles(part.color))
        hasher.update(doubles(part.translation))
    return hasher.hexdigest()


def _pack_indices(values: tuple[int, ...]) -> dict[str, str]:
    import numpy as np

    wide = max(values, default=0) > 65535
    raw = np.asarray(values, dtype="<u4" if wide else "<u2").tobytes()
    return {"type": "u32" if wide else "u16", "data": base64.b64encode(raw).decode("ascii")}


_MIN_SHARE = 64  # a part this small (or with a smaller share) is never thinned below this


def _cluster(pos: Any, tri: Any, origin: Any, cell: float) -> tuple[Any, Any]:
    """Vertex clustering on a uniform grid: one averaged vertex per occupied cell;
    triangles remapped, degenerate (two or more corners in one cell) and duplicate
    (same cells, either winding; first wins) triangles dropped."""
    import numpy as np

    cells = np.floor((pos - origin) / cell).astype(np.int64)
    span = cells.max(axis=0) + 1
    key = (cells[:, 0] * span[1] + cells[:, 1]) * span[2] + cells[:, 2]
    _, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    count = len(first)
    sums = np.stack([np.bincount(inverse, pos[:, k], count) for k in range(3)], axis=1)
    centre = sums / np.bincount(inverse, minlength=count)[:, None]
    corner = inverse[tri]
    keep = (
        (corner[:, 0] != corner[:, 1])
        & (corner[:, 1] != corner[:, 2])
        & (corner[:, 0] != corner[:, 2])
    )
    corner = corner[keep]
    ordered = np.sort(corner, axis=1)
    if count**3 < 2**62:
        ident = (ordered[:, 0] * count + ordered[:, 1]) * count + ordered[:, 2]
        _, unique_rows = np.unique(ident, return_index=True)
    else:
        _, unique_rows = np.unique(ordered, axis=0, return_index=True)
    return centre, corner[np.sort(unique_rows)]


def _normals(pos: Any, tri: Any) -> Any:
    import numpy as np

    face = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    out = np.zeros_like(pos)
    for k in range(3):  # area-weighted: the cross product's length is twice the face area
        for axis in range(3):
            out[:, axis] += np.bincount(tri[:, k], face[:, axis], len(pos))
    length = np.linalg.norm(out, axis=1, keepdims=True)
    return np.divide(out, length, out=np.zeros_like(out), where=length > 0)


def _display_part(part: ScenePart, share: int) -> ScenePart:
    """Honest simplification for the picture only: grid vertex clustering, the cell
    size found by bisection so the shown triangles stay within ``share``. Returns the
    part itself when it already fits. Never drops a part; if no cell size fits it
    keeps the part whole.

    ponytail: clustering is not guaranteed manifold on thin features and moves
    vertices; use quadric edge collapse if display fidelity needs more.
    """
    import numpy as np

    count = len(part.indices) // 3
    if count <= share:
        return part
    pos = np.asarray(part.positions, dtype=np.float64).reshape(-1, 3)
    tri = np.asarray(part.indices, dtype=np.int64).reshape(-1, 3)
    origin = pos.min(axis=0)
    extent = float((pos.max(axis=0) - origin).max())
    area = float(
        np.linalg.norm(
            np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]]), axis=1
        ).sum()
        / 2
    )
    if extent <= 0 or area <= 0:
        return part
    # a closed surface keeps about two triangles per occupied cell: start the search there
    low, high, best = min(extent, math.sqrt(2 * area / share) / 4), extent, None
    for _ in range(12):
        mid = math.sqrt(low * high)  # geometric: the useful sizes span decades
        found = _cluster(pos, tri, origin, mid)
        if 0 < len(found[1]) <= share:
            best, high = found, mid  # fits: try a finer cell for more detail
        elif len(found[1]) == 0:
            high = mid  # too coarse: nothing survives
        else:
            low = mid
    if best is None:
        return part
    centre, faces = best
    used, faces = np.unique(faces, return_inverse=True)
    faces = faces.reshape(-1, 3)
    centre = centre[used]
    return replace(
        part,
        positions=tuple(centre.ravel().tolist()),
        normals=tuple(_normals(centre, faces).ravel().tolist())
        if part.normals is not None
        else None,
        indices=tuple(faces.ravel().tolist()),
    )


def _display_parts(parts: tuple[ScenePart, ...], budget: int) -> list[ScenePart]:
    """Spend ``budget`` over the whole robot in proportion to each part's triangles;
    every part keeps at least ``_MIN_SHARE`` (or all of itself when smaller)."""
    total = sum(len(part.indices) // 3 for part in parts)
    if total <= budget:
        return list(parts)
    return [
        _display_part(part, max(_MIN_SHARE, budget * (len(part.indices) // 3) // total))
        for part in parts
    ]


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

    ``decimate`` gates the display cap (``_DISPLAY_TRIANGLES``): ``gui --out`` (a
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
    digest = scene_hash(scene)
    # Cached per model file state (path, mtime, size) like the catalog; the geometry hash
    # also keys it because a URDF's meshes are separate files the root stamp does not cover.
    display_parts = (
        _cached(
            ("display", _stamp(Path(scene.asset_path)) if scene.asset_path else None, digest),
            lambda: _display_parts(scene.parts, _DISPLAY_TRIANGLES),
        )
        if decimate
        else scene.parts
    )
    display_triangles = sum(len(part.indices) // 3 for part in display_parts)
    simplified = display_triangles < triangles
    honesty = "shapes come from the model; positions are declared only where the contract says so"
    if simplified:
        honesty += f"; model simplified for display: {display_triangles} of {triangles} triangles"
    # Packing 1M triangles is the page's biggest repeat cost: pack once per geometry hash,
    # model file state and display mode (a mesh file edit changes the hash; so does a path).
    packed = _cached(
        ("packed", _stamp(Path(scene.asset_path)) if scene.asset_path else None, digest, decimate),
        lambda: [
            (
                _pack_vectors(p.positions),
                _pack_vectors(p.normals, normals=True) if p.normals is not None else None,
                _pack_indices(p.indices),
            )
            for p in display_parts
        ],
    )
    parts = [
        {
            "id": part_id,
            "node": part.node,
            "name": human_name(re.sub(r"\.\d+$", "", part.node)),
            "state": states.get(part_id, "plain") if part_id is not None else "plain",
            "positions": dict(pos_pack),
            "normals": dict(norm_pack) if norm_pack is not None else None,
            "indices": dict(idx_pack),
            "color": _round(part.color),
            "translation": _round(part.translation),
            # structure with no contract id earns mechanical-only (plus
            # the whole-scene overlays) rather than falling out of the sweep.
            "group": group,
            "lenses": lenses.get(part_id, sorted({"mechanical", *_OVERLAY_LENSES})),
            "mount_frame": mounts.get(part_id),
            "triangles_original": len(original.indices) // 3,
            "triangles_shown": len(part.indices) // 3,
        }
        for original, part, part_id, group, (pos_pack, norm_pack, idx_pack) in zip(
            scene.parts, display_parts, resolved.part_ids, groups, packed, strict=True
        )
    ]
    return {
        **scene_asset_identity(contract, scene, contract_path),
        "source": source,
        "hash": digest,
        "parts": parts,
        "labels": labels,
        "scene_only": list(resolved.scene_only),
        "contract_only": list(resolved.contract_only),
        "bounds": [_round(scene.bounds[0]), _round(scene.bounds[1])],
        "triangles": scene.triangles,
        "vertices": scene.vertices,
        "display_triangles": display_triangles,
        "triangles_original": triangles,
        "triangles_shown": display_triangles,
        "simplified": simplified,
        "honesty": honesty,
    }
