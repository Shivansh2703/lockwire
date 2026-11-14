"""The force numbers the Design panel renders — every one carrying its kind.

The panel lives inside Design: no eighth mode and **no new CLI command**. These
numbers ride in the documents ``lockwire check --json`` and ``lockwire
describe`` already produce, so every figure on the panel has a CLI twin.

Every number on screen is declared, measured, derived by a named check,
catalog or projected — and says which. ``number`` is the single builder every
figure here goes through, so a number with no kind cannot be constructed.

Stage one: what cannot be answered reads NOT SCORED with the reason
named. The CG is **reused** from ``checks.compute_system_cg``, never
recomputed; reactions come from ``statics.support_reactions``, whose ceilings
are documented in that module and repeated in this document's ``not_scored``
lane so a reader of the payload alone still sees them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from lockwire.checks import _fixed_frame_transforms, _point_in_root
from lockwire.report import CGReport
from lockwire.schema import Contract
from lockwire.statics import STANDARD_G, Indeterminate, merge_inertia, support_reactions, weight_n

# A source of a figure -> the kind it must be labelled with. The whole
# point of the mapping is that "catalog" and "projected" cannot be spelled
# "measured" in one caller and "catalog" in another.
_KIND_BY_SOURCE = {
    "contract": "declared",  # written in the lockfile by a human
    "recording": "measured",  # read off a bag or a live sample
    "catalog": "catalog",  # a part-catalog figure — never "measured"
    "estimate": "projected",  # a projection, not an observation
}

_KINDS = set(_KIND_BY_SOURCE.values()) | {"not_scored", "derived"}

# Stage one is slender, in-plane, static. Named
# here so the payload discloses its own ceilings instead of a reader inferring
# that an absent number means "fine".
_STAGE_ONE_CEILINGS = (
    (
        "stress",
        "no stress is computed: stage one solves reactions, not plates, shells or bolt "
        "preload, and a mount shape is a plate rather than a beam",
    ),
    (
        "deflection",
        "no deflection is computed: it needs Young's modulus and a section, which the "
        "contract schema declares nowhere",
    ),
    (
        "shear_dominated_members",
        "a stubby member (span/depth under about 10) is shear-dominated and would read "
        "stiffer than reality under the Euler-Bernoulli beam model",
    ),
    (
        "dynamics",
        "static loads only: no vibration, shock, fatigue or servo transient is scored",
    ),
)


def kind_for_source(source: str) -> str:
    """The kind a figure from ``source`` must carry."""
    return _KIND_BY_SOURCE[source]


def derived_by(check: str) -> str:
    """The kind for a figure a named check produced."""
    return f"derived-by-{check}"


def number(value: Any, unit: str, kind: str, **extra: Any) -> dict[str, Any]:
    """The one builder every force figure goes through.

    Refuses a kind it does not recognise, so a typo cannot become a new
    provenance word that no reader can interpret.
    """
    if kind not in _KINDS and not kind.startswith("derived-by-"):
        raise ValueError(
            f"unknown kind {kind!r}: expected one of {sorted(_KINDS)} or 'derived-by-<check>'"
        )
    return {"value": value, "unit": unit, "kind": kind, **extra}


def not_scored(unit: str, because: str, **extra: Any) -> dict[str, Any]:
    """A figure that is deliberately absent, carrying the reason."""
    return number(None, unit, "not_scored", not_scored_because=because, **extra)


def _mass_rows(contract: Contract, sources: str) -> list[dict[str, Any]]:
    """Per-part mass, declared where declared and NOT SCORED where absent."""
    rows: list[dict[str, Any]] = []
    if sources in ("devices", "links+devices"):
        for device in contract.devices:
            physical = device.physical
            if physical is None:
                continue
            quantity = f"devices/{device.id}/physical/mass"
            if physical.mass is None:
                rows.append(
                    not_scored(
                        "kg",
                        f"device {device.id!r} declares no mass, so it is not summed as 0 kg",
                        id=quantity,
                    )
                )
            else:
                rows.append(
                    number(physical.mass.base(), "kg", kind_for_source("contract"), id=quantity)
                )
    if sources in ("links", "links+devices") and contract.model is not None:
        for link in contract.model.links:
            quantity = f"model/links/{link.name}/mass"
            if link.mass is None:
                rows.append(
                    not_scored(
                        "kg",
                        f"link {link.name!r} declares no mass, so it is not summed as 0 kg",
                        id=quantity,
                    )
                )
            elif link.mass_note:  # a mass derived from shapes is not "declared"
                rows.append(number(link.mass.base(), "kg", "derived", id=quantity))
            else:
                rows.append(
                    number(link.mass.base(), "kg", kind_for_source("contract"), id=quantity)
                )
    return rows


def _mount_rows(contract: Contract, report: CGReport | None) -> list[dict[str, Any]]:
    """Static reaction at each declared mount frame, or NOT SCORED naming why."""
    frames = sorted(
        {
            device.physical.mount_frame
            for device in contract.devices
            if device.physical is not None and device.physical.mount_frame is not None
        }
    )
    if not frames:
        return []

    def refuse(because: str) -> list[dict[str, Any]]:
        return [not_scored("N", because, id=frame) for frame in frames]

    if report is None:
        return refuse("no mass_total budget exists, so no reacted load is defined")
    if report.cg_m is None:
        return refuse(
            "the centre of gravity is unresolved, so the load has no position"
            + (f" ({report.limitation})" if report.limitation else "")
        )
    if not report.complete:
        return refuse(
            "the centre of gravity is incomplete, so the reacted load is unknown"
            + (f" ({report.limitation})" if report.limitation else "")
        )

    _, transforms, _ = _fixed_frame_transforms(contract)
    placed = {frame: _point_in_root(frame, (0.0, 0.0, 0.0), transforms) for frame in frames}
    unresolved = sorted(frame for frame, point in placed.items() if point is None)
    if unresolved:
        return refuse(
            f"mount frame(s) {', '.join(repr(name) for name in unresolved)} cannot be placed "
            "in the root frame, so the support set is incomplete"
        )

    points = [placed[frame] for frame in frames]
    assert all(point is not None for point in points)
    heights = {round(point[2], 12) for point in points}  # type: ignore[index]
    if len(heights) > 1:
        return refuse(
            "the mount points sit at different heights; stage one solves level, in-plane "
            "support sets only"
        )
    varying = [
        axis
        for axis in (0, 1)
        if max([report.cg_m[axis]] + [point[axis] for point in points])  # type: ignore[index]
        - min([report.cg_m[axis]] + [point[axis] for point in points])  # type: ignore[index]
        > 0.0
    ]
    if len(varying) > 1:
        return refuse(
            "the mount points and the centre of gravity are not in one vertical plane; "
            "stage one solves slender in-plane statics only"
        )
    axis = varying[0] if varying else 0

    load = weight_n(report.total_mass_kg)
    try:
        reactions = support_reactions(load, report.cg_m[axis], [point[axis] for point in points])
    except Indeterminate as refusal:
        return refuse(str(refusal))
    kind = derived_by("statics.support_reactions")
    # A reaction is built from report.total_mass_kg and report.cg_m — the same
    # numbers `mass_total` and `cg` carry, so the same limitation (e.g. a
    # device with no declared mass, silently excluded from the total rather
    # than positioned) travels onto every reaction too.
    return [
        number(
            force,
            "N",
            kind,
            id=frame,
            reaction_n=force,
            moment_nm=moment,
            span_axis="xyz"[axis],
            complete=report.limitation is None,
            limitation=report.limitation,
        )
        for frame, (force, moment) in zip(frames, reactions, strict=True)
    ]


def _mount_shape_rows(contract: Contract) -> list[dict[str, Any]]:
    """Declared mounting geometry consumed by the force panel."""
    rows: list[dict[str, Any]] = []
    for device in contract.devices:
        physical = device.physical
        if physical is None or physical.mount_shape is None:
            continue
        shape = physical.mount_shape
        rows.append(
            {
                "id": device.id,
                "kind": shape.kind,
                "dimensions": number(list(shape.dimensions_mm), "mm", kind_for_source("contract")),
                "fastener_pattern": shape.fastener_pattern,
                "mesh_path": shape.mesh_path,
            }
        )
    return rows


_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_ZERO_POSE = "all joints at zero; the real centre of mass moves as the joints move"


def _own_inertia(link: Any, rotation: Any) -> np.ndarray:
    """A link's principal inertia as a 3x3 in the root frame (zero when it declares none)."""
    if link is None or link.inertia is None:
        return np.zeros((3, 3))
    axes = np.array(link.inertia.axes)
    own = axes.T @ np.diag(link.inertia.moments_kgm2) @ axes
    return np.array(rotation) @ own @ np.array(rotation).T


def _parts(contract: Contract, report: CGReport, poses: dict[str, Any]) -> list | None:
    """(mass, COM in root, inertia in root) per contributor, or None when one has no position."""
    from lockwire.scene_urdf import _apply_pose  # noqa: PLC0415

    links = {k.name: k for k in contract.model.links} if contract.model is not None else {}
    parts = []
    for item in report.contributors:
        link = (
            links.get(item.quantity.split("/")[2])
            if item.quantity.startswith("model/links/")
            else None
        )
        position, rotation = item.position_m, _IDENTITY
        if link is not None and link.com is not None:
            pose = poses.get(link.frame or link.name)
            if pose is not None:
                position, rotation = _apply_pose(pose, link.com), pose[0]
        if position is None:
            return None
        parts.append((item.mass_kg, position, _own_inertia(link, rotation)))
    return parts


def _inertia_figure(
    parts: list | None, report: CGReport | None, note: str | None = None
) -> dict[str, Any]:
    """Merged inertia about the system CG, [Ixx, Iyy, Izz, Ixy, Ixz, Iyz] in kg*m^2.

    Every contributor enters ``statics.merge_inertia`` (parallel-axis theorem); a link's own
    principal inertia is rotated into the root frame, a device or a link with no declared
    inertia counts as a point mass. A gap is named, never summed as 0.
    """
    if not parts or report is None:
        return not_scored("kg*m^2", "the centre of mass is incomplete, so no inertia is merged")
    tensor = merge_inertia(parts)[2]
    value = [float(tensor[i, j]) for i, j in ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2))]
    extra = {"note": note} if note else {}
    return number(
        value,
        "kg*m^2",
        derived_by("merge_inertia"),
        frame=report.frame,
        quantity=report.quantity,
        **extra,
    )


def _zero_poses(contract: Contract) -> dict[str, Any]:
    """Every link's pose in the root with all joints at zero (the pose the 3D view draws)."""
    from lockwire.scene_urdf import _IDENTITY_POSE, _compose  # noqa: PLC0415

    model = contract.model
    if model is None:
        return {}
    kids: dict[str, list[Any]] = {}
    for joint in model.joints:
        kids.setdefault(joint.parent, []).append(joint)
    children = {joint.child for joint in model.joints}
    poses = {k.name: _IDENTITY_POSE for k in model.links if k.name not in children}
    stack = list(poses)
    while stack:
        for joint in kids.get(stack.pop(), []):
            if joint.child not in poses:
                poses[joint.child] = _compose(
                    poses[joint.parent], joint.origin.xyz, joint.origin.rpy
                )
                stack.append(joint.child)
    return poses


def _budgetless_cg(contract: Contract) -> CGReport | None:
    """The CG of a robot that declares no mass_total budget: link masses if the model has them,
    else device masses. The panel shows the robot's mass whether or not a budget checks it."""
    from lockwire.checks import compute_system_cg  # noqa: PLC0415
    from lockwire.schema import Budget  # noqa: PLC0415

    links = [k.mass.base() for k in contract.model.links if k.mass] if contract.model else []
    devices = [d.physical.mass.base() for d in contract.devices if d.physical and d.physical.mass]
    total = sum(links) or sum(devices)
    if total <= 0:
        return None
    budget = Budget(
        id="mass",
        kind="mass_total",
        limit={"value": total, "unit": "kg"},  # type: ignore[arg-type]
        sources="links" if links else "devices",
        owner="the mass panel",
    )
    reports = compute_system_cg(contract.model_copy(update={"budgets": [budget]}))
    return reports[0] if reports else None


def force_document(contract: Contract, cg_reports: list[CGReport]) -> dict[str, Any]:
    """The force lane of the check document: mass, CG and mount reactions with kinds."""
    report = cg_reports[0] if cg_reports else _budgetless_cg(contract)
    sources = report.sources if report is not None else "links+devices"
    if report is None:
        mass_total = not_scored("kg", "no mass_total budget is declared")
        centre = not_scored("m", "no mass_total budget is declared")
    else:
        # report.limitation fires for a gap `complete` alone misses: a device
        # with no declared mass is dropped from `contributors` entirely, so it
        # never counts against `complete` (position-completeness) even though
        # the total below is missing that mass, not summing it as zero. Carry
        # the limitation onto the number itself so the mark travels with it
        # (a figure this shape must not print as if it were whole).
        mass_total = number(
            report.total_mass_kg,
            "kg",
            derived_by("compute_system_cg"),
            quantity=report.quantity,
            complete=report.limitation is None,
            limitation=report.limitation,
        )
        centre = (
            number(
                list(report.cg_m),
                "m",
                derived_by("compute_system_cg"),
                frame=report.frame,
                quantity=report.quantity,
                complete=report.complete,
                limitation=report.limitation,
            )
            if report.cg_m is not None
            else not_scored(
                "m",
                report.limitation or "no positioned mass contributor",
                quantity=report.quantity,
            )
        )
    inertia_note = None
    if report is not None and not report.complete:
        # Joints move, so the engine will not name one centre. Show the one with every joint at
        # zero, the pose the 3D view draws, and say so on the figure.
        zero = _parts(contract, report, _zero_poses(contract))
        if zero:
            total, com, _ = merge_inertia(zero)
            centre = number(
                [float(v) for v in com],
                "m",
                derived_by("merge_inertia"),
                frame=report.frame,
                quantity=report.quantity,
                complete=True,
                note=_ZERO_POSE,
            )
            inertia_note = _ZERO_POSE
        parts = zero
    else:
        parts = report and _parts(contract, report, _fixed_frame_transforms(contract)[1])
    return {
        "gravity": number(
            STANDARD_G,
            "m/s^2",
            kind_for_source("contract"),
            note="standard gravity, the constant preflight scores |g| against",
        ),
        "mass_total": mass_total,
        "cg": centre,
        "inertia": _inertia_figure(parts, report, inertia_note),
        "masses": _mass_rows(contract, sources),
        "mounts": _mount_rows(contract, report),
        "mount_shapes": _mount_shape_rows(contract),
        "not_scored": [not_scored("", because, id=name) for name, because in _STAGE_ONE_CEILINGS],
    }


def _rendered(entry: dict[str, Any]) -> str:
    """One figure as a reader sees it — the number and its kind, or why it is absent."""
    if entry["kind"] == "not_scored":
        return f"NOT SCORED — {entry['not_scored_because']}"
    value = entry["value"]
    text = (
        "(" + ", ".join(f"{component:.3f}" for component in value) + ")"
        if isinstance(value, list)
        else f"{value:g}"
    )
    rendered = f"{text} {entry['unit']}".strip() + f" (kind: {entry['kind']})"
    limitation = entry.get("limitation")
    if limitation:
        rendered += f" — INCOMPLETE: {limitation}"
    return rendered


def render_markdown(document: dict[str, Any]) -> list[str]:
    """The Markdown face of ``force_document`` — the payload's numbers, not a second set.

    ``describe`` renders through here so the reference file and the check
    document cannot print different figures (or a figure without its kind).
    """
    lines = ["## Forces", ""]
    lines.append(f"- total mass: {_rendered(document['mass_total'])}")
    lines.append(f"- centre of gravity: {_rendered(document['cg'])}")
    lines.append(f"- gravity: {_rendered(document['gravity'])}")
    for row in document["masses"]:
        lines.append(f"- mass {row['id']}: {_rendered(row)}")
    for row in document["mounts"]:
        moment = f", moment {row['moment_nm']:g} N*m" if row["kind"] != "not_scored" else ""
        lines.append(f"- reaction at mount {row['id']}: {_rendered(row)}{moment}")
    for row in document["mount_shapes"]:
        suffix = f", fasteners {row['fastener_pattern']}" if row["fastener_pattern"] else ""
        if row["mesh_path"]:
            suffix += f", mesh {row['mesh_path']}"
        lines.append(
            f"- mount shape {row['id']}: {row['kind']}, {_rendered(row['dimensions'])}{suffix}"
        )
    for row in document["not_scored"]:
        lines.append(f"- {row['id']}: {_rendered(row)}")
    lines.append("")
    return lines
